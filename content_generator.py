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
from pathlib import Path
from datetime import datetime, timezone

from loguru import logger

from ai_client import generate_post
from buffer_manager import buffer
from database import db
from image_fetcher import fetch_image_url
from rss_parser import rss
from config import cfg


class ContentGenerator:
    """Генерирует посты для каналов и пополняет буфер."""

    # Сколько постов генерировать за один утренний запуск
    POSTS_PER_MORNING = 10

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

        # Считаем сколько нужно догенерировать
        daily_target = channel.get("daily_posts_count", self.POSTS_PER_MORNING)
        if target_count is None:
            if force:
                # Ручной запуск — генерируем полный дневной лимит поверх буфера
                target_count = daily_target
            else:
                # Авто-запуск — добираем только до лимита
                target_count = max(0, daily_target - current_level)

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

        # Получаем темы из источников
        topics, sources_used = await self._collect_topics(channel, target_count)

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

        # Генерируем посты
        generated = 0
        skipped = 0
        last_format = None  # для контроля ротации форматов

        for topic_data in topics[:target_count]:
            try:
                # Выбираем формат с учётом ротации (не повторять подряд)
                format_name = self._pick_format(channel, last_format)

                # Генерируем пост — передаём историю тем чтобы не повторяться
                post = await generate_post(
                    channel, topic_data["topic"], format_name,
                    used_topics=used_topics,
                )

                # Картинка: сначала берём из RSS, если нет — ищем через API
                if topic_data.get("image_url"):
                    post["image_url"] = topic_data["image_url"]
                    post["has_image"] = True
                elif cfg.UNSPLASH_ACCESS_KEY or cfg.PEXELS_API_KEY:
                    image_url = await fetch_image_url(
                        topic=topic_data["topic"],
                        channel_topic=channel.get("topic", ""),
                    )
                    if image_url:
                        post["image_url"] = image_url
                        post["has_image"] = True
                        logger.debug(f"Картинка найдена через API [{channel_id}]")
                    else:
                        post["has_image"] = False
                else:
                    post["has_image"] = False

                # Анти-повтор: проверяем похожие посты в буфере
                if await self._is_duplicate(channel_id, post["content"]):
                    logger.debug(f"Пропускаю дубликат [{channel_id}]: {topic_data['topic'][:40]}")
                    skipped += 1
                    continue

                # Добавляем в буфер — сразу готов к публикации
                buffer.add(post)

                last_format = format_name
                generated += 1

                logger.success(
                    f"Пост добавлен [{channel_id}] "
                    f"формат={format_name} тема={topic_data['topic'][:40]}"
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

        # --- Источник 1: RSS (приоритет из handbook) ---
        try:
            articles = await rss.fetch_for_channel(channel, limit=count)
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

        # --- Источник 2: Вечнозелёные темы (если RSS не хватило) ---
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

        logger.debug(
            f"Тем собрано: {len(topics)} "
            f"(RSS: {sum(1 for t in topics if t['source']=='rss')}, "
            f"вечнозелёных: {sum(1 for t in topics if t['source']=='evergreen')})"
        )

        return topics, sources_used

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

    async def _is_duplicate(self, channel_id: str, content: str) -> bool:
        """
        Проверяет, нет ли уже похожего поста в буфере.
        Использует простое сравнение по общим словам.

        Возвращает True если пост слишком похож на уже существующий.
        """
        try:
            with db.connect() as conn:
                recent_posts = conn.execute(
                    """
                    SELECT content FROM posts
                    WHERE channel_id = ? AND status IN ('ready', 'pending_review')
                    ORDER BY generated_at DESC
                    LIMIT 20
                    """,
                    (channel_id,),
                ).fetchall()

            if not recent_posts:
                return False

            # Простое сравнение: если > 80% слов совпадают — дубликат
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
        with db.connect() as conn:
            rows = conn.execute(
                """SELECT topic FROM posts
                   WHERE channel_id = ?
                     AND status IN ('published', 'ready')
                     AND topic != ''
                   ORDER BY generated_at DESC
                   LIMIT ?""",
                (channel_id, limit),
            ).fetchall()
        return [row["topic"] for row in rows if row["topic"]]

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
