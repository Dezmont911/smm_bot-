"""
content_generator.py — Дирижёр системы (Слой 2 из handbook)

Этот модуль склеивает все части вместе:
  RSS парсер → темы → Claude API → буфер постов

Алгоритм (из handbook):
  1. Взять карточку канала
  2. Спарсить RSS → получить свежие инфоповоды
  3. Если RSS пустой → взять вечнозелёную тему из БД
  4. Для каждой темы сгенерировать пост через Claude
  5. Проверить на анти-повтор (похожий пост уже есть?)
  6. Положить в буфер
  7. Проверить уровень буфера → нужно ли ещё генерировать

Запускается:
  - Каждое утро в 06:00 (через планировщик)
  - Экстренно, если буфер упал ниже порога
  - Вручную через команду бота /generate

Использование:
    from content_generator import generator
    result = await generator.run_for_channel(channel)
    result = await generator.run_morning_batch()  # все каналы сразу
"""

import json
import re
from pathlib import Path
from datetime import datetime, timezone

from loguru import logger

from ai_client import generate_post
from buffer_manager import buffer
from database import db
from image_fetcher import fetch_image_url
from image_generator import generate_image as generate_ai_image
from rss_parser import rss
from web_scraper import scraper as web_scraper
from topic_search import get_topics
from content_router import resolve, pick_format, pick_hook
from content_safety import (
    build_content_brief,
    evaluate_topic_candidate,
    validate_generated_post,
)
import dedup
from config import cfg


def _has_meaningful_text(s: str) -> bool:
    """True, если в строке есть хотя бы одно «слово» (≥2 буквы подряд).
    Отсекает пустое / «1» / «а» / «123» / «!!!» как тему канала."""
    return bool(re.search(r"[а-яёa-z]{2,}", (s or ""), re.IGNORECASE))


def _meaningful_base(*parts: str) -> str:
    """Первый осмысленный кусок из переданных (тема, название) для синтез-резерва."""
    for p in parts:
        p = (p or "").split(",")[0].strip()
        if _has_meaningful_text(p):
            return p
    return ""


class ContentGenerator:
    """Генерирует посты для каналов и пополняет буфер."""

    # Сколько постов генерировать за один утренний запуск
    POSTS_PER_MORNING = 10

    # Жёсткий потолок на ОДНУ генерацию (ручную/авто) — больше 10 за раз не нужно
    MAX_GENERATE_PER_RUN = 10

    # Порог схожести для анти-повтора (0.0 = разные, 1.0 = одинаковые)
    # Посты со схожестью выше этого порога не добавляются в буфер
    SIMILARITY_THRESHOLD = 0.80

    # --------------------------------------------------------
    # Генерация для одного канала
    # --------------------------------------------------------

    async def run_for_channel(
        self,
        channel: dict,
        target_count: int | None = None,
        force: bool = False,
    ) -> dict:
        """
        Полный цикл генерации для одного канала.

        Аргументы:
            channel      — карточка канала (словарь)
            target_count — сколько постов добавить в буфер
                           (если None — добирает до BUFFER_MIN)

        Возвращает:
            {
                "channel_id":   "@mychannel",
                "generated":    8,    # сколько постов создано
                "skipped":      1,    # сколько пропущено (повтор/ошибка)
                "buffer_level": 10,   # уровень буфера после генерации
                "sources_used": ["rss", "evergreen"],
            }
        """
        channel_id = channel["channel_id"]
        current_level = buffer.get_level(channel_id)
        safe_profile = channel.get("safe_profile") if isinstance(channel.get("safe_profile"), dict) else {}
        if safe_profile and (
            safe_profile.get("supported") is False
            or safe_profile.get("risk_level") == "blocked"
        ):
            logger.warning(
                f"Генерация отклонена [{channel_id}]: safe_profile="
                f"{safe_profile.get('risk_level')} supported={safe_profile.get('supported')}"
            )
            return {
                "channel_id": channel_id,
                "generated": 0,
                "skipped": 0,
                "buffer_level": current_level,
                "sources_used": [],
                "reason": "safe_profile запрещает автогенерацию",
            }

        # Считаем сколько нужно догенерировать
        daily_target = channel.get("daily_posts_count", self.POSTS_PER_MORNING)
        if target_count is None:
            if force:
                # Ручной запуск — генерируем полный дневной лимит поверх буфера
                target_count = daily_target
            else:
                # Авто-запуск — добираем только до лимита
                target_count = max(0, daily_target - current_level)

        # Жёсткий потолок на одну генерацию — 10 постов (15-20 за раз уже много).
        target_count = min(target_count, self.MAX_GENERATE_PER_RUN)

        if target_count == 0:
            logger.info(f"Буфер в норме [{channel_id}]: {current_level} постов, генерация не нужна")
            return {
                "channel_id": channel_id,
                "generated": 0,
                "skipped": 0,
                "buffer_level": current_level,
                "sources_used": [],
            }

        logger.info(
            f"Начинаю генерацию [{channel_id}]: "
            f"нужно {target_count} постов, в буфере {current_level}"
        )

        # Авто-регистрация канала в БД если его там ещё нет
        self._ensure_channel_registered(channel)

        # ---- Marketplace-каналы (WB/Ozon) — отдельный pipeline ----
        if channel.get("channel_type") == "marketplace":
            return await self._run_marketplace(channel, target_count)

        # ---- Стоп-кран: тема/название самого канала запретные ----
        # Иначе синтез-резерв (Источник 4) построил бы «углы» из запретной темы и
        # жёг бы LLM на гарантированных отказах. Сразу выходим с понятной причиной.
        try:
            from ai_client import _contains_forbidden
            if _contains_forbidden(f"{channel.get('topic','')} {channel.get('name','')}"):
                logger.warning(f"Генерация отклонена [{channel_id}]: тема канала запрещена")
                return {
                    "channel_id": channel_id, "generated": 0, "skipped": 0,
                    "buffer_level": current_level, "sources_used": [],
                    "reason": "тема канала содержит запрещённый контент — измени тему канала",
                }
        except Exception:
            pass

        # ---- Стоп-кран: у контент-канала нет ОСМЫСЛЕННОЙ ТЕМЫ ----
        # Тема — главный сигнал для генерации. Без неё бот пишет мусор «по названию»
        # (канал-пустышка «uebancoin» сгенерил крипто-новости). Требуем именно тему,
        # а не название. Тема выводится ИИ из постов канала — если их мало, темы нет.
        if not _has_meaningful_text(channel.get("topic", "")):
            logger.warning(f"Генерация отклонена [{channel_id}]: у канала нет темы")
            return {
                "channel_id": channel_id, "generated": 0, "skipped": 0,
                "buffer_level": current_level, "sources_used": [],
                "reason": "у канала не определена тема (в канале мало постов для анализа) — "
                          "добавь постов в канал и нажми «🔄 Подобрать тему заново» в настройках",
            }

        # Получаем темы из источников. Берём ПУЛ кандидатов больше нужного:
        # часть отсеется как уже использованные (вкл. недавно очищенные) и дубли
        # по смыслу, поэтому без запаса буфер не добирается (RSS-топ исчерпывается).
        # Минимум 10 кандидатов даже для target=1 (перегенерация одного поста):
        # иначе при малом пуле все темы оказываются уже использованными → 0 постов.
        candidate_count = min(max(target_count * 3, 10), target_count + 25)
        topics, sources_used = await self._collect_topics(channel, candidate_count)

        if not topics:
            logger.error(f"Нет тем для генерации [{channel_id}]")
            await self._log_error(channel_id, "generation", "Не удалось получить темы из RSS и вечнозелёного банка")
            return {
                "channel_id": channel_id,
                "generated": 0,
                "skipped": 0,
                "buffer_level": current_level,
                "sources_used": [],
            }

        # Загружаем последние 20 тем канала для дедупликации
        used_topics = self._get_used_topics(channel_id, limit=20)
        if used_topics:
            logger.debug(f"Дедупликация [{channel_id}]: {len(used_topics)} использованных тем")

        # Стратегия канала (стиль/архетип/temperature/веса форматов/хуки) — один раз
        strategy = resolve(channel)
        logger.debug(
            f"Стратегия [{channel_id}]: архетип={strategy['archetype']}, "
            f"t={strategy['temperature']}, форматы={strategy['format_bias']}"
        )

        # Генерируем посты
        generated = 0
        skipped = 0
        last_format = None  # для контроля ротации форматов

        for topic_data in topics:
            # Набрали нужное число постов — останавливаемся (остальной пул — запас)
            if generated >= target_count:
                break
            try:
                # Пропускаем темы, которые уже использовались: иначе Claude получит
                # противоречие ("напиши про X" + "не повторяй X") и вернёт мета-ответ.
                safety = evaluate_topic_candidate(channel, topic_data)
                logger.info(
                    f"Topic safety [{channel_id}]: source={safety.get('source')} "
                    f"decision={safety.get('decision')} reason={safety.get('reason_code')} "
                    f"raw={safety.get('raw_topic', '')[:70]}"
                )
                if safety["decision"] in ("blocked", "review") or not safety.get("safe_topic"):
                    skipped += 1
                    continue

                safe_topic = safety["safe_topic"]

                if self._topic_already_used(safe_topic, used_topics):
                    logger.info(
                        f"Пропускаю уже использованную тему [{channel_id}]: "
                        f"{safe_topic[:50]}"
                    )
                    skipped += 1
                    continue

                # Формат — по весам архетипа (не повторяя предыдущий), хук — ротация структуры
                format_name = pick_format(strategy, last_format)
                hook = pick_hook(strategy)
                content_brief = build_content_brief(channel, safety, format_name)
                logger.debug(
                    f"Content brief [{channel_id}]: topic={content_brief.get('topic')} "
                    f"angle={content_brief.get('angle')} goal={content_brief.get('post_goal')}"
                )

                # Генерируем пост — writer получает только safe_topic/content_brief, не raw_topic
                post = await generate_post(
                    channel, safe_topic, format_name,
                    used_topics=used_topics, strategy=strategy, hook=hook,
                    content_brief=content_brief,
                )

                # ── Выбор картинки по image_source ─────────────────────────
                # image_source в карточке канала — ЕДИНСТВЕННОЕ правило:
                #   "rss"        — только из RSS-статьи, иначе без картинки
                #   "stock"      — RSS → Pexels/Unsplash → FLUX (как гарантия)
                #   "ai"         — RSS → fal.ai FLUX
                #   "auto"       — RSS → Pexels/Unsplash → fal.ai FLUX (дефолт)
                #   "none"/"off" — без картинки вообще
                # Контент-пост гарантированно получает картинку: если сток
                # промахнулся, дорисовываем через FLUX (last resort, ~$0.003).
                # Поле use_images больше НЕ управляет логикой (оставлено в карточках
                # для совместимости) — источником истины служит image_source.
                # ────────────────────────────────────────────────────────────
                image_source = channel.get("image_source", "auto")
                images_off = image_source in ("none", "off")

                # Картинку подбираем по СОДЕРЖАНИЮ поста, а не по сырому заголовку
                # темы (заголовки вроде «Almost ready to go!» давали картинку не в
                # тему — чемодан вместо томатов). Контент — источник истины.
                image_basis = (post.get("content") or "").strip()[:500] or safe_topic

                image_url = None

                # Шаг 1: RSS-картинка — всегда первый приоритет, бесплатно
                if topic_data.get("image_url"):
                    image_url = topic_data["image_url"]
                    logger.debug(f"Картинка из RSS [{channel_id}]")

                # Шаг 2: если RSS не дал — добываем по image_source
                if not image_url and not images_off and image_source != "rss":

                    # Сток (Pexels/Unsplash/Reddit)
                    if image_source in ("stock", "auto"):
                        has_stock = (
                            cfg.UNSPLASH_ACCESS_KEY
                            or cfg.PEXELS_API_KEY
                            or channel.get("reddit_image_subreddits")
                        )
                        if has_stock:
                            image_url = await fetch_image_url(
                                topic=image_basis,
                                channel_topic=channel.get("topic", ""),
                                subreddits=channel.get("reddit_image_subreddits"),
                                channel_name=channel.get("name", ""),
                                image_keywords=channel.get("image_keywords"),
                            )
                            if image_url:
                                logger.debug(f"Картинка из Pexels/Unsplash [{channel_id}]")

                    # AI/FLUX — основной источник для "ai", гарантия для "stock"/"auto"
                    if not image_url and image_source in ("ai", "stock", "auto"):
                        if cfg.FAL_API_KEY:
                            image_url = await generate_ai_image(
                                topic=image_basis,
                                channel_topic=channel.get("topic", ""),
                                channel_name=channel.get("name", ""),
                            )
                            if image_url:
                                logger.info(f"Картинка сгенерирована через FLUX [{channel_id}]")
                        else:
                            logger.debug(f"FAL_API_KEY не задан, FLUX пропущен [{channel_id}]")

                    if not image_url:
                        logger.warning(
                            f"Пост без картинки [{channel_id}] — все источники промахнулись"
                        )

                post["image_url"] = image_url
                post["has_image"] = bool(image_url)

                validation = validate_generated_post(channel, post, safety, content_brief)
                logger.info(
                    f"Output validation [{channel_id}]: decision={validation.get('decision')} "
                    f"reason={validation.get('reason_code')}"
                )
                if not validation.get("allowed"):
                    skipped += 1
                    await self._log_error(
                        channel_id,
                        "generation_validation",
                        f"{validation.get('reason_code')}: {validation.get('notes', '')}",
                    )
                    continue

                # Считаем эмбеддинг поста (для семантич. дедупа и хранения)
                cand_vec = await dedup.aembed(post["content"])

                # Анти-повтор: семантически (по смыслу), с лексическим фолбэком
                if await self._is_duplicate(channel_id, post["content"], cand_vec):
                    logger.info(f"Пропускаю дубликат по смыслу [{channel_id}]: {safe_topic[:40]}")
                    skipped += 1
                    continue

                # Сохраняем вектор вместе с постом
                if cand_vec is not None:
                    post["embedding_blob"] = dedup.to_blob(cand_vec)

                # Добавляем в буфер — сразу готов к публикации
                buffer.add(post)

                last_format = format_name
                generated += 1
                # Запоминаем тему внутри батча — чтобы следующие итерации
                # не взяли её же под другим форматом
                used_topics.append(safe_topic)

                logger.success(
                    f"Пост добавлен [{channel_id}] "
                    f"формат={format_name} тема={safe_topic[:40]}"
                )

            except Exception as e:
                logger.error(f"Ошибка генерации поста [{channel_id}]: {e}")
                skipped += 1
                await self._log_error(channel_id, "generation", str(e))

        new_level = buffer.get_level(channel_id)

        logger.info(
            f"Генерация завершена [{channel_id}]: "
            f"создано={generated}, пропущено={skipped}, "
            f"буфер={current_level}→{new_level}"
        )

        return {
            "channel_id": channel_id,
            "generated": generated,
            "skipped": skipped,
            "buffer_level": new_level,
            "sources_used": list(set(sources_used)),
        }

    # --------------------------------------------------------
    # Утренний запуск для всех каналов
    # --------------------------------------------------------

    async def run_morning_batch(self, force: bool = False) -> dict:
        """
        Утренняя генерация для всех активных каналов.
        Запускается в 06:00 планировщиком.

        Аргументы:
            force — если True, генерирует полный дневной лимит поверх буфера
                    (ручной запуск через /generate)

        Возвращает сводку по всем каналам.
        """
        logger.info("=== Утренняя генерация началась ===")
        start_time = datetime.now(timezone.utc)

        # Загружаем все активные карточки каналов
        channels = self._load_all_channels()
        if not channels:
            logger.warning("Нет активных каналов для генерации")
            return {"total_generated": 0, "channels": []}

        results = []
        total_generated = 0
        total_skipped = 0

        for channel in channels:
            try:
                result = await self.run_for_channel(channel, force=force)
                results.append(result)
                total_generated += result["generated"]
                total_skipped += result["skipped"]
            except Exception as e:
                logger.error(f"Критическая ошибка генерации для {channel['channel_id']}: {e}")
                await self._log_error(channel["channel_id"], "generation", f"Критическая ошибка: {e}")

        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()

        logger.info(
            f"=== Утренняя генерация завершена === "
            f"каналов={len(channels)}, постов={total_generated}, "
            f"пропущено={total_skipped}, время={elapsed:.1f}с"
        )

        return {
            "total_generated": total_generated,
            "total_skipped": total_skipped,
            "channels_processed": len(channels),
            "elapsed_seconds": elapsed,
            "channels": results,
        }

    async def run_for_all_channels(self, force: bool = True) -> dict:
        """Совместимый alias для UI: массовая генерация через безопасный batch path."""
        batch = await self.run_morning_batch(force=force)
        return {
            r.get("channel_id"): r
            for r in batch.get("channels", [])
            if isinstance(r, dict) and r.get("channel_id")
        }

    # --------------------------------------------------------
    # Ступенчатая генерация (вместо ночного батча на все каналы)
    # --------------------------------------------------------

    async def run_top_up_cycle(self, batch_per_channel: int = 5, max_total: int = 30) -> dict:
        """
        Подливает посты понемногу: раз в час догенерирует небольшими порциями
        ТОЛЬКО каналы с просевшим буфером. Распределяет нагрузку по дню вместо
        одного большого батча ночью и держит темы свежими.

        Каналы с донором (reference_channels) ЗДЕСЬ ПРОПУСКАЕМ — у них приоритет
        добора с донора, ими занимается reference_importer.import_low_buffer
        (с фолбэком на генерацию, если донор пуст). Иначе был бы двойной залив.

        batch_per_channel — сколько постов максимум за раз на канал.
        max_total — потолок постов за один цикл (чтобы не было пика).
        """
        target = getattr(cfg, "BUFFER_TARGET", cfg.BUFFER_MIN)
        channels = self._load_all_channels()
        # доноры — мимо (их добирает import_low_buffer, приоритет донора)
        channels = [c for c in channels if not c.get("reference_channels")]
        # сначала самые «голодные» каналы
        channels.sort(key=lambda c: buffer.get_level(c["channel_id"]))

        total = 0
        results = []
        for ch in channels:
            if total >= max_total:
                break
            cid = ch["channel_id"]
            level = buffer.get_level(cid)
            if level >= target:
                continue  # буфер в норме — пропускаем
            need = min(batch_per_channel, max_total - total, target - level)
            if need <= 0:
                continue
            try:
                r = await self.run_for_channel(ch, target_count=need)
                total += r.get("generated", 0)
                results.append(r)
            except Exception as e:
                logger.error(f"Ступенчатая генерация: ошибка [{cid}]: {e}")
                await self._log_error(cid, "generation", f"top_up: {e}")

        if results:
            logger.info(
                f"Ступенчатая генерация: каналов={len(results)}, постов={total}"
            )
        return {"generated": total, "channels": results}

    # --------------------------------------------------------
    # Экстренная генерация (буфер упал ниже порога)
    # --------------------------------------------------------

    async def run_emergency(self, channel_id: str) -> dict:
        """
        Экстренная генерация когда буфер упал ниже BUFFER_EMERGENCY.
        Генерирует минимальный запас чтобы не остановиться.
        """
        logger.warning(f"⚡ Экстренная генерация [{channel_id}]")

        channel = self._load_channel_by_id(channel_id)
        if not channel:
            logger.error(f"Канал не найден: {channel_id}")
            return {"channel_id": channel_id, "generated": 0}

        # Генерируем минимальный запас — до уровня EMERGENCY
        target = cfg.BUFFER_MIN - buffer.get_level(channel_id)
        return await self.run_for_channel(channel, target_count=max(target, 4))

    # --------------------------------------------------------
    # Marketplace pipeline (WB / Ozon)
    # --------------------------------------------------------

    async def _run_marketplace(self, channel: dict, target_count: int) -> dict:
        """
        Генерирует посты для маркетплейс-каналов (WB/Ozon).
        Использует публичный wb_parser (card.wb.ru через прокси) — Seller API
        (wb_partner) убран: он требует товаров продавца, которых нет.
        """
        from wb_parser import wb_parser as _parser
        parser_name = "wb_parser"

        channel_id = channel["channel_id"]

        logger.info(
            f"WB-pipeline [{channel_id}]: запрос {target_count} постов "
            f"(парсер: {parser_name})"
        )

        try:
            posts = await _parser.generate_posts(channel, count=target_count)
        except Exception as e:
            logger.error(f"WB-pipeline [{channel_id}]: ошибка парсера: {e}")
            posts = []

        if not posts:
            logger.warning(f"WB-pipeline [{channel_id}]: все парсеры вернули 0 товаров")
            return {
                "channel_id": channel_id,
                "generated": 0,
                "skipped": 0,
                "buffer_level": buffer.get_level(channel_id),
                "sources_used": [parser_name],
            }

        generated = 0
        skipped = 0

        for post_data in posts:
            try:
                # Проверка анти-повтора — не публикуем один товар дважды
                article = post_data.get("wb_article", "")
                if article and self._wb_article_in_buffer(channel_id, article):
                    logger.debug(f"WB дубликат [{channel_id}]: арт. {article}")
                    skipped += 1
                    continue

                # Формируем запись для буфера
                # topic содержит артикул — используется для анти-повтора
                buf_post = {
                    "channel_id": channel_id,
                    "content": post_data["content"],
                    "image_url": post_data.get("image_url"),
                    "has_image": bool(post_data.get("image_url")),
                    "format": "wb_product",
                    "topic": f"WB арт.{article} [{post_data.get('wb_category', '')}]",
                    "source": "wb_parser",
                    # ВАЖНО: переносим parse_mode из парсера (HTML), иначе ссылка
                    # на товар <a href> публикуется голым текстом.
                    "parse_mode": post_data.get("parse_mode", "HTML"),
                }
                safety = evaluate_topic_candidate(
                    channel, {"topic": buf_post["topic"], "source": "wb_parser"}
                )
                if safety["decision"] in ("blocked", "review") or not safety.get("safe_topic"):
                    logger.warning(
                        f"WB-pipeline [{channel_id}]: пост пропущен safety="
                        f"{safety.get('reason_code')} арт. {article}"
                    )
                    skipped += 1
                    continue
                brief = build_content_brief(channel, safety, "wb_product")
                validation = validate_generated_post(channel, buf_post, safety, brief)
                if not validation.get("allowed"):
                    logger.warning(
                        f"WB-pipeline [{channel_id}]: пост пропущен validation="
                        f"{validation.get('reason_code')} арт. {article}"
                    )
                    skipped += 1
                    continue
                buffer.add(buf_post)
                generated += 1
                logger.success(f"WB-пост добавлен [{channel_id}]: арт. {article}")

            except Exception as e:
                logger.error(f"WB-pipeline [{channel_id}]: ошибка добавления поста: {e}")
                skipped += 1

        new_level = buffer.get_level(channel_id)
        logger.info(
            f"WB-pipeline завершён [{channel_id}]: "
            f"создано={generated}, пропущено={skipped}, буфер={new_level}"
        )

        return {
            "channel_id": channel_id,
            "generated": generated,
            "skipped": skipped,
            "buffer_level": new_level,
            "sources_used": [parser_name],
        }

    def _wb_article_in_buffer(self, channel_id: str, article: str) -> bool:
        """
        Проверяет, есть ли товар с таким артикулом уже в буфере.
        Ищет по полю topic, куда мы записываем 'WB арт.{article}'.
        """
        try:
            with db.connect() as conn:
                row = conn.execute(
                    """
                    SELECT id FROM posts
                    WHERE channel_id = ? AND status = 'ready'
                      AND topic LIKE ?
                    LIMIT 1
                    """,
                    (channel_id, f"%арт.{article}%"),
                ).fetchone()
            return row is not None
        except Exception:
            return False  # если ошибка — не блокируем

    # --------------------------------------------------------
    # Вспомогательные методы
    # --------------------------------------------------------

    async def _collect_topics(
        self, channel: dict, count: int
    ) -> tuple[list[dict], list[str]]:
        """
        Собирает темы из всех источников в нужном количестве.

        Возвращает (список тем, список использованных источников).
        Каждая тема: {"topic": "текст темы", "image_url": "..." или None}
        """
        topics = []
        sources_used = []
        channel_id = channel["channel_id"]

        # --- Источник 0: Веб-поиск Claude (если topic_source == "search") ---
        # Claude сам ищет свежие инфоповоды в интернете по теме канала.
        # Не зависит от RSS-лент и не порождает повторяющихся заголовков.
        if channel.get("topic_source") == "search":
            try:
                used = self._get_used_topics(channel_id, limit=20)
                found = await get_topics(channel, count=count, used_topics=used)
                for t in found:
                    topics.append({"topic": t, "image_url": None, "source": "search"})
                if found:
                    sources_used.append("search")
                    logger.info(f"Тем из веб-поиска [{channel_id}]: {len(found)}")
                else:
                    logger.warning(
                        f"Веб-поиск не дал тем [{channel_id}] — откат на RSS/вечнозелёные"
                    )
            except Exception as e:
                logger.warning(f"Веб-поиск недоступен [{channel_id}]: {e}")
            # Если поиск дал достаточно — RSS/web не дёргаем
            if len(topics) >= count:
                return topics[:count], sources_used

        # --- Источник 1: RSS (приоритет из handbook) ---
        try:
            articles = await rss.fetch_for_channel(channel, limit=count - len(topics))
            if articles:
                for article in articles:
                    topics.append({
                        "topic": f"{article['title']}. {article['summary'][:200]}",
                        "image_url": article.get("image_url"),
                        "source": "rss",
                    })
                sources_used.append("rss")
                logger.debug(f"Тем из RSS: {len(articles)}")
        except Exception as e:
            logger.warning(f"RSS недоступен [{channel['channel_id']}]: {e}")

        # --- Источник 2: Web-скрапинг (если RSS дал мало тем) ---
        # Запускается когда RSS пустой или дал меньше половины нужного
        rss_count = len(topics)
        if rss_count < max(2, count // 2):
            try:
                web_articles = await web_scraper.scrape_for_channel(
                    channel, limit=count - rss_count
                )
                if web_articles:
                    topics.extend(web_articles)
                    sources_used.append("web")
                    logger.debug(f"Тем из web_scraper: {len(web_articles)}")
            except Exception as e:
                logger.warning(f"web_scraper недоступен [{channel['channel_id']}]: {e}")

        # --- Источник 3: Вечнозелёные темы (резерв) ---
        while len(topics) < count:
            eg_topic = buffer.get_evergreen_topic(channel["channel_id"])
            if not eg_topic:
                break
            topics.append({
                "topic": eg_topic,
                "image_url": None,
                "source": "evergreen",
            })
            if "evergreen" not in sources_used:
                sources_used.append("evergreen")

        # --- Отсев тем с ЯВНЫМИ запретными словами ДО генерации ---
        # (имплицитные Claude отсекает сам, мета-ответ ловит детектор отказов)
        topics = self._drop_forbidden_topics(channel_id, topics)

        # --- Отсев «мусорных» тем: заголовки-артефакты («# Вечнозелёные темы…»),
        # мета/отказы («Я не могу помочь…»), 18+/запретка прямо в теме. Источник 4
        # (fallback) строится из темы канала — его не трогаем. ---
        try:
            from ai_client import is_valid_topic
            before = len(topics)
            topics = [t for t in topics
                      if t.get("source") == "fallback" or is_valid_topic(t.get("topic", ""))]
            if before - len(topics):
                logger.info(f"Мусорные/мета-темы отсеяны [{channel_id}]: {before - len(topics)}")
        except Exception as e:
            logger.warning(f"Фильтр валидности тем пропущен: {e}")

        # --- Гейт релевантности: отсев off-topic тем ДО генерации (эмбеддинги) ---
        # Бьёт по дрейфу: Reddit/новости иногда дают темы не в тему канала.
        # evergreen/fallback не трогаем (они по построению на-тему). Если отсев
        # оставил мало — Источник 4 ниже доберёт резервом по теме канала.
        topics = await self._filter_relevant(channel, topics, count)

        # --- Источник 4: АБСОЛЮТНЫЙ резерв — углы по теме самого канала ---
        # Срабатывает, только если все источники выше пусты (нет ни новостей, ни
        # вечнозелёных в карточке). Гарантирует, что буфер не останется пустым:
        # лучше пост по теме канала, чем ничего. Темы из живого тона выйдут норм.
        if len(topics) < count:
            base = _meaningful_base(channel.get("topic", ""))
            if base:
                ANGLES = [
                    "интересный неочевидный факт", "разбор для новичка",
                    "частая ошибка и как её избежать", "практический совет из опыта",
                    "мифы и правда", "свежий взгляд на привычное",
                    "топ-подборка по теме", "короткая история из практики",
                ]
                for a in ANGLES:
                    if len(topics) >= count:
                        break
                    topics.append({
                        "topic": f"{base}: {a}",
                        "image_url": None,
                        "source": "fallback",
                    })
                if any(t["source"] == "fallback" for t in topics) and "fallback" not in sources_used:
                    sources_used.append("fallback")
                    logger.warning(
                        f"Все источники тем пусты [{channel_id}] — резерв по теме канала «{base}»"
                    )

        logger.debug(
            f"Тем собрано: {len(topics)} "
            f"(search: {sum(1 for t in topics if t['source']=='search')}, "
            f"RSS: {sum(1 for t in topics if t['source']=='rss')}, "
            f"web: {sum(1 for t in topics if t['source']=='web')}, "
            f"вечнозелёных: {sum(1 for t in topics if t['source']=='evergreen')}, "
            f"резерв: {sum(1 for t in topics if t['source']=='fallback')})"
        )

        return topics, sources_used

    def _drop_forbidden_topics(self, channel_id: str, topics: list[dict]) -> list[dict]:
        """Убирает темы с ЯВНЫМИ запретными словами (политика/война/ЛГБТ/Украина и пр.)
        до генерации — чтобы Claude не получал их и не выдавал мета-отказ."""
        try:
            from ai_client import DEFAULT_FORBIDDEN_TOPICS
        except Exception:
            return topics
        terms = [t.strip().lower() for t in DEFAULT_FORBIDDEN_TOPICS if t.strip()]
        kept, dropped = [], 0
        for t in topics:
            # синтез-резерв (fallback) по теме канала не трогаем
            if t.get("source") == "fallback":
                kept.append(t)
                continue
            text = (t.get("topic") or "").lower()
            if any(term in text for term in terms):
                dropped += 1
                continue
            kept.append(t)
        if dropped:
            logger.info(f"Запретные темы отсеяны [{channel_id}]: {dropped}")
        return kept

    # Порог косинуса: тема считается «в теме канала», если близость к профилю ≥ порога.
    RELEVANCE_MIN = 0.28

    async def _filter_relevant(self, channel: dict, topics: list[dict], count: int) -> list[dict]:
        """
        Отсев off-topic тем по эмбеддингам (reuse dedup). evergreen/fallback не трогаем.
        In-scope fallback: не оставляем меньше floor — добираем лучших из отсеянных,
        чтобы гейт никогда не приводил к пустому буферу. Порядок тем сохраняем.
        """
        if not topics or dedup.backend() != "embedding":
            return topics  # без эмбеддингов гейт не работает — пропускаем как есть

        channel_id = channel["channel_id"]
        dna = channel.get("channel_dna") if isinstance(channel.get("channel_dna"), dict) else {}
        profile = " ".join(p for p in [
            channel.get("topic", ""),
            channel.get("audience", ""),
            dna.get("audience", ""),
            dna.get("goal", ""),
            dna.get("offer", ""),
            ", ".join(dna.get("allowed_topic_types", []) or []),
            ", ".join(channel.get("image_keywords", []) or []),
        ] if p).strip()
        if not profile:
            return topics
        prof_vec = await dedup.aembed(profile)
        if prof_vec is None:
            return topics

        results, dropped = [], []
        for t in topics:
            if t.get("source") in ("evergreen", "fallback"):
                results.append(t)
                continue
            vec = await dedup.aembed((t.get("topic") or "")[:300])
            if vec is None:
                results.append(t)
                continue
            sim = dedup.cosine(prof_vec, vec)
            if sim >= self.RELEVANCE_MIN:
                results.append(t)
            else:
                dropped.append((sim, t))

        # Гарантируем минимум кандидатов: добираем лучших из отсеянных
        floor = max(3, count // 2)
        if len(results) < floor and dropped:
            dropped.sort(key=lambda x: x[0], reverse=True)
            results += [t for _, t in dropped[:floor - len(results)]]

        if dropped:
            ex = "; ".join(f"{sim:.2f} {t.get('topic','')[:35]}" for sim, t in dropped[:3])
            logger.info(
                f"Гейт релевантности [{channel_id}]: отсеяно off-topic {len(dropped)} "
                f"(порог {self.RELEVANCE_MIN}), оставлено {len(results)} | примеры: {ex}"
            )
        return results

    def _pick_format(self, channel: dict, last_format: str | None) -> str:
        """
        Выбирает формат поста с учётом ротации.
        Не даёт повторить один формат два раза подряд.
        """
        import random

        format_map = {
            "совет дня": "совет",
            "факт/статистика": "факт",
            "вопрос аудитории": "вопрос",
            "мини-разбор": "разбор",
            "инфоповод": "инфоповод",
        }

        available = channel.get("post_formats", list(format_map.keys()))
        mapped = [format_map.get(f, f) for f in available]

        # Убираем последний использованный формат
        if last_format and len(mapped) > 1:
            mapped = [f for f in mapped if f != last_format]

        return random.choice(mapped)

    async def _is_duplicate(self, channel_id: str, content: str, cand_vec=None) -> bool:
        """
        Проверяет, нет ли уже похожего поста в буфере/истории канала.

        Семантический путь (если есть эмбеддинг cand_vec): сравнивает по СМЫСЛУ
        с сохранёнными векторами недавних постов — ловит перефраз. Порог cfg.DEDUP_THRESHOLD.
        Лексический фолбэк (если эмбеддинги недоступны): сравнение по общим словам.

        Возвращает True если пост слишком похож на уже существующий.
        """
        # ---- Семантический дедуп ----
        if cand_vec is not None:
            try:
                with db.connect() as conn:
                    rows = conn.execute(
                        """
                        SELECT embedding FROM posts
                        WHERE channel_id = ?
                          AND embedding IS NOT NULL
                          AND (
                            status IN ('ready', 'pending_review')
                            OR (status = 'published'
                                AND generated_at > datetime('now', '-14 days'))
                          )
                        ORDER BY generated_at DESC
                        LIMIT 80
                        """,
                        (channel_id,),
                    ).fetchall()

                others = [dedup.from_blob(r["embedding"]) for r in rows if r["embedding"]]
                if others:
                    sim = dedup.max_similarity(cand_vec, others)
                    if sim > cfg.DEDUP_THRESHOLD:
                        logger.info(f"Семантический дубль [{channel_id}]: близость={sim:.3f}")
                        return True
                return False
            except Exception as e:
                logger.warning(f"Семантический дедуп ошибка: {e} — лексический фолбэк")

        # ---- Лексический фолбэк (по общим словам) ----
        try:
            with db.connect() as conn:
                recent_posts = conn.execute(
                    """
                    SELECT content FROM posts
                    WHERE channel_id = ?
                      AND (
                        status IN ('ready', 'pending_review')
                        OR (status = 'published'
                            AND generated_at > datetime('now', '-14 days'))
                      )
                    ORDER BY generated_at DESC
                    LIMIT 40
                    """,
                    (channel_id,),
                ).fetchall()

            if not recent_posts:
                return False

            new_words = set(content.lower().split())
            for row in recent_posts:
                existing_words = set(row["content"].lower().split())
                if not existing_words:
                    continue
                intersection = new_words & existing_words
                similarity = len(intersection) / len(existing_words)
                if similarity > self.SIMILARITY_THRESHOLD:
                    return True

        except Exception as e:
            logger.warning(f"Ошибка проверки дубликата: {e}")

        return False

    async def _log_error(self, channel_id: str, error_type: str, message: str):
        """Записывает ошибку в таблицу error_log."""
        try:
            now = datetime.now(timezone.utc).isoformat()
            with db.connect() as conn:
                conn.execute(
                    "INSERT INTO error_log (channel_id, error_type, message, occurred_at) VALUES (?, ?, ?, ?)",
                    (channel_id, error_type, message, now),
                )
        except Exception:
            pass  # Ошибка логирования не должна ломать основной поток

    def _load_all_channels(self) -> list[dict]:
        """
        Загружает все активные карточки каналов из папки channels/.
        Возвращает список словарей.
        """
        channels_dir = Path(__file__).parent / "channels"
        channels = []

        for json_file in channels_dir.glob("*.json"):
            if json_file.name.startswith("example_"):
                continue  # пропускаем шаблон
            try:
                with open(json_file, encoding="utf-8") as f:
                    channel = json.load(f)
                if channel.get("active", True):
                    channels.append(channel)
            except Exception as e:
                logger.error(f"Ошибка загрузки карточки {json_file}: {e}")

        logger.debug(f"Загружено активных каналов: {len(channels)}")
        return channels

    def _ensure_channel_registered(self, channel: dict):
        """
        Регистрирует канал в таблице channels если его там ещё нет.
        Вызывается автоматически перед каждой генерацией.
        """
        with db.connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO channels
                   (tg_handle, name, topic, tone, config_json, active)
                   VALUES (?, ?, ?, ?, ?, 1)""",
                (
                    channel["channel_id"],
                    channel.get("name", ""),
                    channel.get("topic", ""),
                    channel.get("tone", ""),
                    json.dumps(channel, ensure_ascii=False),
                ),
            )

    def _get_used_topics(self, channel_id: str, limit: int = 20) -> list[str]:
        """
        Возвращает последние N тем опубликованных и готовых постов канала.
        Передаётся в Claude для дедупликации — чтобы не повторял темы.
        """
        # Учитываем и недавно отброшенные (skipped) посты: иначе после очистки
        # буфера те же темы из RSS/поиска сгенерируются повторно (одинаковые посты).
        with db.connect() as conn:
            rows = conn.execute(
                """SELECT topic FROM posts
                   WHERE channel_id = ?
                     AND status IN ('published', 'ready', 'skipped')
                     AND topic != ''
                   ORDER BY generated_at DESC
                   LIMIT ?""",
                (channel_id, limit),
            ).fetchall()
        return [row["topic"] for row in rows if row["topic"]]

    @staticmethod
    def _normalize_topic(topic: str) -> str:
        """Приводит тему к каноничному виду для сравнения (lower, схлопывание пробелов)."""
        return re.sub(r"\s+", " ", (topic or "").lower()).strip()

    def _topic_already_used(self, topic: str, used_topics: list[str]) -> bool:
        """
        True, если тема уже встречалась среди использованных
        (точное совпадение или почти полное пересечение слов).
        Защищает от повторяющихся RSS-заголовков (еженедельные треды Reddit и т.п.),
        из-за которых Claude уходит в отказ вместо написания поста.
        """
        t = self._normalize_topic(topic)
        if not t:
            return False
        tw = set(t.split())
        for used in used_topics:
            u = self._normalize_topic(used)
            if not u:
                continue
            if t == u:
                return True
            uw = set(u.split())
            if tw and uw:
                overlap = len(tw & uw) / min(len(tw), len(uw))
                if overlap >= 0.9:  # почти идентичные темы
                    return True
        return False

    def _load_channel_by_id(self, channel_id: str) -> dict | None:
        """Находит карточку канала по его handle."""
        for channel in self._load_all_channels():
            if channel.get("channel_id") == channel_id:
                return channel
        return None


# ============================================================
# ЕДИНСТВЕННЫЙ ЭКЗЕМПЛЯР
# ============================================================
generator = ContentGenerator()


# ============================================================
# ТЕСТ — запускается напрямую: python content_generator.py
# ============================================================
if __name__ == "__main__":
    import asyncio

    async def test():
        print("🎬 Тест полного цикла генерации\n")
        print("Цепочка: RSS → темы → Claude → буфер\n")
        print("=" * 60)

        # Инициализируем БД
        db.init()

        # Загружаем тестовый канал
        with open("channels/example_channel.json", encoding="utf-8") as f:
            import json as _json
            channel = _json.load(f)

        # Регистрируем в БД если нет
        with db.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM channels WHERE tg_handle = ?",
                (channel["channel_id"],)
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO channels (tg_handle, name, topic, tone, config_json) VALUES (?, ?, ?, ?, ?)",
                    (channel["channel_id"], channel["name"], channel["topic"],
                     channel["tone"], str(channel)),
                )

        # Добавляем вечнозелёные темы
        from buffer_manager import buffer
        buffer.add_evergreen_topics(
            channel["channel_id"],
            channel.get("evergreen_topics", [])
        )

        level_before = buffer.get_level(channel["channel_id"])
        print(f"📊 Буфер до генерации: {level_before} постов")
        print(f"🎯 Цель: сгенерировать до {generator.POSTS_PER_MORNING} постов\n")

        # Запускаем генерацию
        result = await generator.run_for_channel(channel, target_count=3)

        # Выводим результат
        print("\n" + "=" * 60)
        print(f"✅ Генерация завершена!")
        print(f"   Создано постов:    {result['generated']}")
        print(f"   Пропущено:         {result['skipped']}")
        print(f"   Уровень буфера:    {result['buffer_level']}")
        print(f"   Источники:         {', '.join(result['sources_used'])}")

        # Показываем что лежит в буфере
        print(f"\n📦 Посты в буфере (статус pending_review):")
        with db.connect() as conn:
            posts = conn.execute(
                """SELECT format, topic, substr(content, 1, 80) as preview
                   FROM posts
                   WHERE channel_id = ? AND status = 'pending_review'
                   ORDER BY generated_at DESC LIMIT 5""",
                (channel["channel_id"],)
            ).fetchall()

        for i, post in enumerate(posts, 1):
            print(f"\n  {i}. [{post['format']}] {post['topic'][:50]}")
            print(f"     {post['preview']}...")

        print("\n✅ Полный цикл работает!")

    asyncio.run(test())
