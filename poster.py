"""
poster.py — Планировщик публикаций (Слой 4 из handbook)

Этот модуль делает одну вещь: берёт готовые посты из буфера
и публикует их в Telegram-каналы по расписанию.

Логика работы:
  1. Каждый час (или по расписанию канала) — проверяем нужно ли постить
  2. Берём следующий ready пост из буфера
  3. Публикуем в канал
  4. Помечаем как published
  5. Проверяем уровень буфера — если мало, запускаем генерацию
  6. Если буфер пустой — алерт администратору

Расписание по умолчанию: 4 поста в день (09:00, 12:00, 16:00, 20:00 МСК)
Можно задать своё расписание в карточке канала (поле post_times).

Запуск встроен в bot.py через APScheduler — отдельно запускать не нужно.
"""

import asyncio
import html as html_lib
import random
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from io import BytesIO

import aiohttp
from loguru import logger
from telegram import InputFile
from telegram.error import TimedOut

from buffer_manager import buffer
from config import cfg
from database import db
from image_fetcher import fetch_image_url
from image_generator import generate_image as generate_ai_image


# Минимальный интервал между публикациями в канале (минуты). Если с прошлой
# публикации (плановой / РСЯ-перекрытия / ручного поста админа) прошло меньше —
# ближайший слот пропускаем, чтобы не было двух постов подряд.
MIN_PUBLISH_GAP_MIN = 40

# Лимит подписи Telegram для медиа (send_photo/video/...). Длиннее — Telegram
# отвергает весь пост; поэтому подпись медиа обрезаем, чтобы картинка всё же вышла.
TG_CAPTION_LIMIT = 1024
_AMBIGUOUS_SEND = object()


_HTML_INLINE_TAGS = {
    "a", "b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre",
    "tg-spoiler",
}
_HTML_BLOCK_TAGS = {"br", "p", "div", "li"}


def _is_html_parse_mode(parse_mode) -> bool:
    return str(parse_mode or "").lower() == "html"


class _HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag.lower() in _HTML_BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts).strip()


class _HTMLCaptionClipper(HTMLParser):
    def __init__(self, visible_limit: int):
        super().__init__(convert_charrefs=True)
        self.remaining = max(0, visible_limit - 3)
        self.parts: list[str] = []
        self.stack: list[str] = []
        self.truncated = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.remaining <= 0:
            self.truncated = True
            return
        if tag == "br":
            self.parts.append("\n")
            self.remaining -= 1
            return
        if tag not in _HTML_INLINE_TAGS:
            return
        if tag == "a":
            href = ""
            for key, value in attrs:
                if key.lower() == "href":
                    href = value or ""
                    break
            if not href:
                return
            self.parts.append(f'<a href="{html_lib.escape(href, quote=True)}">')
        else:
            self.parts.append(f"<{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag not in self.stack:
            return
        while self.stack:
            open_tag = self.stack.pop()
            self.parts.append(f"</{open_tag}>")
            if open_tag == tag:
                break

    def handle_data(self, data):
        if self.remaining <= 0:
            if data:
                self.truncated = True
            return
        if len(data) <= self.remaining:
            self.parts.append(html_lib.escape(data))
            self.remaining -= len(data)
            return
        piece = data[:self.remaining].rstrip()
        if piece:
            self.parts.append(html_lib.escape(piece))
        self.remaining = 0
        self.truncated = True

    def clipped(self) -> str:
        if self.truncated:
            self.parts.append("...")
        while self.stack:
            self.parts.append(f"</{self.stack.pop()}>")
        return "".join(self.parts).strip()


def _html_visible_text(value: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(value or "")
        parser.close()
        return parser.text()
    except Exception:
        return html_lib.unescape(str(value or ""))


def _clip_plain_caption(caption: str | None) -> str | None:
    if caption and len(caption) > TG_CAPTION_LIMIT:
        return caption[:TG_CAPTION_LIMIT - 3].rstrip() + "..."
    return caption


def _clip_html_caption(caption: str | None) -> str | None:
    if not caption:
        return caption
    if len(_html_visible_text(caption)) <= TG_CAPTION_LIMIT:
        return caption
    parser = _HTMLCaptionClipper(TG_CAPTION_LIMIT)
    try:
        parser.feed(caption)
        parser.close()
        return parser.clipped()
    except Exception:
        return _clip_plain_caption(_html_visible_text(caption))


def _clip_caption(caption, parse_mode=None):
    if _is_html_parse_mode(parse_mode):
        return _clip_html_caption(caption)
    return _clip_plain_caption(caption)


def _caption_for_parse_mode(caption, parse_mode, original_parse_mode=None):
    if parse_mode is None and _is_html_parse_mode(original_parse_mode):
        return _clip_plain_caption(_html_visible_text(caption))
    return _clip_caption(caption, parse_mode)


def _is_ambiguous_send(value) -> bool:
    return value is _AMBIGUOUS_SEND


class Poster:
    """Публикует посты из буфера в Telegram-каналы."""

    # Расписание по умолчанию (часы по МСК = UTC+3)
    # Переводим в UTC: 09:00 МСК = 06:00 UTC и т.д.
    DEFAULT_POST_HOURS_UTC = [6, 9, 13, 17]  # 09, 12, 16, 20 МСК

    def __init__(self):
        self.bot = None  # устанавливается при старте через set_bot()

    def set_bot(self, bot):
        """Привязывает Telegram бот для публикации и алертов."""
        self.bot = bot

    # --------------------------------------------------------
    # Главный метод — вызывается планировщиком каждую минуту
    # --------------------------------------------------------

    async def tick(self):
        """
        Основной цикл постера — вызывается каждую минуту.
        Проверяет каждый канал: нужно ли сейчас постить?
        """
        if self.bot is None:
            logger.warning("Постер не инициализирован (bot=None), пропускаю")
            return

        now_utc = datetime.now(timezone.utc)
        current_minute = now_utc.hour * 60 + now_utc.minute
        current_weekday_msk = (now_utc + timedelta(hours=3)).weekday()

        channels = self._load_active_channels()
        if not channels:
            return

        logger.debug(
            f"Постер: проверяю {len(channels)} каналов | "
            f"время UTC: {now_utc.strftime('%H:%M')}"
        )

        for channel in channels:
            try:
                await self._process_channel(channel, current_minute, current_weekday_msk)
            except Exception as e:
                logger.error(f"Ошибка постера для {channel['channel_id']}: {e}")

    @staticmethod
    def _schedule_entry_to_utc_min(value) -> int | None:
        if isinstance(value, int) and 0 <= value <= 23:
            return value * 60
        if isinstance(value, str):
            import re
            m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", value.strip())
            if m:
                hour = int(m.group(1))
                minute = int(m.group(2) or 0)
                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    return hour * 60 + minute
        return None

    @classmethod
    def _schedule_utc_minutes(cls, entries) -> set[int]:
        return {
            m for m in (cls._schedule_entry_to_utc_min(v) for v in (entries or []))
            if m is not None
        }

    async def _process_channel(self, channel: dict, current_minute: int, current_weekday_msk: int):
        """
        Обрабатывает один канал: постит если пришло время,
        проверяет буфер и при необходимости запускает генерацию.
        """
        channel_id = channel["channel_id"]

        # Если расписание отключено — постим только через детектор РСЯ
        if channel.get("schedule_disabled", False):
            return

        # Проверяем: пришло ли время для этого канала?
        # ВАЖНО: пустое/отсутствующее расписание = автопубликации НЕТ (без дефолт-часов!).
        # Иначе канал без post_times_utc постил по дефолту, хотя «расписание не задано».
        post_minutes = self._schedule_utc_minutes(channel.get("post_times_utc") or [])
        if not post_minutes or current_minute not in post_minutes:
            return  # расписание не задано или не наш слот — пропускаем

        schedule_days = channel.get("schedule_days")
        if isinstance(schedule_days, list):
            allowed_days = {int(d) for d in schedule_days if isinstance(d, int) and 0 <= d <= 6}
            if allowed_days and current_weekday_msk not in allowed_days:
                return

        # Правило 1: есть ожидающее РСЯ-перекрытие → слот пропускаем,
        # перекрытие выйдет само (иначе был бы дубль: плановый + перекрытие рядом).
        if buffer.has_pending_overlay(channel_id):
            logger.info(f"Слот {channel_id} пропущен: ждёт РСЯ-перекрытие")
            return

        # Правило 2: только что публиковали (< MIN_GAP) → слот пропускаем,
        # чтобы не было двух постов подряд (ручной пост / перекрытие / прошлый слот).
        mins = self.minutes_since_published(channel_id)
        if mins is not None and mins < MIN_PUBLISH_GAP_MIN:
            logger.info(
                f"Слот {channel_id} пропущен: публиковали {mins:.0f} мин назад "
                f"(< {MIN_PUBLISH_GAP_MIN} мин)"
            )
            return

        # Берём следующий пост из буфера
        post = buffer.get_next(channel_id)

        if post is None:
            # Буфер пуст — алерт администратору
            logger.warning(f"⚠️ Буфер пуст! Канал: {channel_id}")
            await self._alert_empty_buffer(channel_id)
            return

        # Публикуем пост
        result = await self._publish(post)

        if result["success"]:
            buffer.mark_published(post["id"])
            self.record_published(channel_id)
            await self._boost_published_post(post, result)
            logger.success(
                f"Опубликован пост в {channel_id} | "
                f"формат: {post.get('format', '?')} | "
                f"id: {post['id'][:8]}..."
            )

            # Уведомляем администратора о публикации
            await self._notify_published(post, result)

            # Проверяем буфер после публикации
            await self._check_buffer_after_post(channel)
        else:
            logger.error(f"Не удалось опубликовать пост в {channel_id}")
            await self._alert(
                f"❌ <b>Ошибка публикации</b>\n"
                f"Канал: {channel_id}\n"
                f"Пост: {post['id'][:8]}...\n"
                f"Проверь логи."
            )

    # --------------------------------------------------------
    # Скачивание WB картинки через прокси
    # --------------------------------------------------------

    async def _download_wb_image(self, url: str) -> bytes | None:
        """
        Скачивает картинку с WB CDN (wbbasket.ru).

        Telegram не поддерживает webp-URL напрямую, поэтому скачиваем байты
        здесь и отправляем через InputFile(BytesIO).

        Формула _get_basket() приближённая — при 404 пробуем соседние
        корзины (basket ±1 .. ±3), это решает большинство случаев несовпадения.
        """
        import re

        # Собираем список прокси
        proxy_list = list(cfg.WB_PROXY_URLS) if cfg.WB_PROXY_URLS else []
        if not proxy_list and cfg.WB_PROXY_URL:
            proxy_list = [cfg.WB_PROXY_URL]

        proxy = random.choice(proxy_list) if proxy_list else None

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.wildberries.ru/",
            "Accept": "image/webp,image/*,*/*",
        }

        # Генерируем список URL для попыток:
        # 1) оригинальный URL
        # 2) соседние корзины ±1..3 (формула приближённая — для разных
        #    диапазонов vol шаг разный, поэтому нужны fallback-попытки)
        urls_to_try = [url]
        m = re.match(r"(https://basket-)(\d+)(\.wbbasket\.ru/.+)", url)
        if m:
            prefix, basket_str, suffix = m.group(1), m.group(2), m.group(3)
            basket_num = int(basket_str)
            for delta in [-1, 1, -2, 2, -3, 3]:
                candidate = basket_num + delta
                if 1 <= candidate <= 50:
                    urls_to_try.append(f"{prefix}{candidate:02d}{suffix}")

        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            for try_url in urls_to_try:
                try:
                    async with session.get(
                        try_url,
                        proxy=proxy,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=12),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            if len(data) > 1000:
                                basket_used = re.search(r"basket-(\d+)", try_url).group(1)
                                orig_basket = re.search(r"basket-(\d+)", url).group(1)
                                if basket_used != orig_basket:
                                    logger.info(
                                        f"WB CDN: basket скорректирован "
                                        f"{orig_basket}→{basket_used} | {len(data)} байт"
                                    )
                                else:
                                    logger.debug(
                                        f"WB CDN OK: {len(data)} байт | basket-{basket_used}"
                                    )
                                return data
                            # Файл есть но слишком маленький — возможно заглушка
                            logger.warning(
                                f"WB CDN: подозрительно мал ({len(data)} байт) | {try_url[:70]}"
                            )
                        elif resp.status == 404:
                            logger.debug(f"WB CDN 404: {try_url[:80]}")
                        else:
                            logger.warning(f"WB CDN HTTP {resp.status}: {try_url[:70]}")
                except asyncio.TimeoutError:
                    logger.warning(f"WB CDN: таймаут | {try_url[:70]}")
                except Exception as e:
                    logger.warning(f"WB CDN ошибка: {type(e).__name__}: {e}")

        logger.warning(f"WB CDN: не удалось скачать (все {len(urls_to_try)} попыток) | {url[:70]}")
        return None

    # --------------------------------------------------------
    # Проверка и самолечение картинки
    # --------------------------------------------------------

    def _load_channel_by_id(self, channel_id: str) -> dict | None:
        """Загружает карточку канала по handle (для политики картинок)."""
        import json
        from pathlib import Path
        channels_dir = Path(__file__).parent / "channels"
        for jf in channels_dir.glob("*.json"):
            try:
                with open(jf, encoding="utf-8") as f:
                    ch = json.load(f)
                if ch.get("channel_id") == channel_id:
                    return ch
            except Exception:
                continue
        return None

    def record_published(self, channel_id: str):
        """Запоминает время последней публикации в канале (в карточку JSON).
        Используется для минимального интервала между постами (MIN_PUBLISH_GAP_MIN)."""
        import json
        from pathlib import Path
        channels_dir = Path(__file__).parent / "channels"
        now = datetime.now(timezone.utc).isoformat()
        for jf in channels_dir.glob("*.json"):
            try:
                with open(jf, encoding="utf-8") as f:
                    ch = json.load(f)
                if ch.get("channel_id") == channel_id:
                    ch["last_published_utc"] = now
                    with open(jf, "w", encoding="utf-8") as wf:
                        json.dump(ch, wf, ensure_ascii=False, indent=2)
                    return
            except Exception:
                continue

    def minutes_since_published(self, channel_id: str) -> float | None:
        """Сколько минут прошло с последней публикации, или None если не было."""
        ch = self._load_channel_by_id(channel_id)
        ts = ch.get("last_published_utc") if ch else None
        if not ts:
            return None
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds() / 60
        except Exception:
            return None

    async def _regenerate_image(self, post: dict) -> str | None:
        """
        Перегенерирует картинку для поста (когда Telegram отклонил исходную).

        Источник: stock (Pexels/Unsplash) → AI/FLUX, по политике image_source канала
        (auto = сток, потом FLUX). Игнорирует use_images: раз пост заявлял картинку,
        стараемся её честно дать. Сохраняет новый URL в БД и в post.
        Возвращает новый URL или None.
        """
        channel_id = post["channel_id"]
        channel = self._load_channel_by_id(channel_id) or {}
        image_source = channel.get("image_source", "auto")
        topic = post.get("topic", "") or channel.get("topic", "")

        new_url = None
        # 1) сток
        if image_source in ("stock", "auto"):
            try:
                new_url = await fetch_image_url(
                    topic=topic,
                    channel_topic=channel.get("topic", ""),
                    subreddits=channel.get("reddit_image_subreddits"),
                    channel_name=channel.get("name", ""),
                    image_keywords=channel.get("image_keywords"),
                )
            except Exception as e:
                logger.warning(f"Сток-перегенерация не удалась [{channel_id}]: {e}")
        # 2) AI/FLUX
        if not new_url and image_source in ("ai", "auto") and cfg.FAL_API_KEY:
            try:
                new_url = await generate_ai_image(
                    topic=topic,
                    channel_topic=channel.get("topic", ""),
                    channel_name=channel.get("name", ""),
                )
            except Exception as e:
                logger.warning(f"AI-перегенерация не удалась [{channel_id}]: {e}")

        # Сохраняем результат в БД и в post
        try:
            with db.connect() as conn:
                conn.execute(
                    "UPDATE posts SET image_url = ? WHERE id = ?",
                    (new_url, post["id"]),
                )
        except Exception as e:
            logger.warning(f"Не удалось обновить image_url в БД: {e}")
        post["image_url"] = new_url

        if new_url:
            logger.success(f"Картинка перегенерирована [{channel_id}]: {new_url[:60]}")
        else:
            logger.warning(f"Перегенерация картинки не дала результата [{channel_id}]")
        return new_url

    # --------------------------------------------------------
    # Публикация поста
    # --------------------------------------------------------

    async def _send_by_file_id(self, channel_id, file_id, media_type, caption, parse_mode):
        """Публикует relay-референс по file_id (медиа уже на серверах Telegram).
        Без скачивания и без лимита 50 МБ. Пробует оба parse_mode."""
        for pm in (parse_mode, None):
            cap = _caption_for_parse_mode(caption, pm, parse_mode) or None
            try:
                if media_type == "video":
                    return await self.bot.send_video(chat_id=channel_id, video=file_id, caption=cap, parse_mode=pm)
                elif media_type == "animation":
                    return await self.bot.send_animation(chat_id=channel_id, animation=file_id, caption=cap, parse_mode=pm)
                elif media_type == "document":
                    return await self.bot.send_document(chat_id=channel_id, document=file_id, caption=cap, parse_mode=pm)
                else:
                    return await self.bot.send_photo(chat_id=channel_id, photo=file_id, caption=cap, parse_mode=pm)
            except Exception as e:
                logger.warning(f"send by file_id [{media_type}, parse={pm}] в {channel_id}: {e}")
        return None

    async def _send_album(self, channel_id, album_json, caption, parse_mode):
        """Публикует альбом (media_group) по списку file_id. Подпись — на первом кадре.
        Telegram разрешает до 10 элементов в группе."""
        import json
        from telegram import InputMediaPhoto, InputMediaVideo
        try:
            data = json.loads(album_json or "{}")
        except Exception:
            return None
        members = data.get("members", [])
        items = data.get("items", {})

        for pm in (parse_mode, None):
            cap = _caption_for_parse_mode(caption, pm, parse_mode) or None
            media = []
            for i, mid in enumerate(members[:10]):  # лимит Telegram — 10
                it = items.get(str(mid))
                if not it:
                    continue
                c = cap if i == 0 else None  # подпись только на первом
                if it.get("type") == "video":
                    media.append(InputMediaVideo(it["file_id"], caption=c, parse_mode=pm))
                else:
                    media.append(InputMediaPhoto(it["file_id"], caption=c, parse_mode=pm))
            if not media:
                return None
            # send_media_group требует 2..10 элементов; если остался один — шлём как одиночное
            if len(media) == 1:
                only = members[:10][0]
                it = items.get(str(only)) if members else None
                if it:
                    return await self._send_by_file_id(channel_id, it["file_id"], it.get("type"), caption, parse_mode)
                return None
            try:
                messages = await self.bot.send_media_group(chat_id=channel_id, media=media)
                if not messages:
                    return None
                return min(messages, key=lambda m: int(getattr(m, "message_id", 0) or 0))
            except Exception as e:
                logger.warning(f"send_media_group [parse={pm}] в {channel_id}: {e}")
        return None

    async def _send_local_media(self, channel_id, path, media_type, caption, parse_mode):
        """Отправляет локальный медиа-файл (референс «как есть»): фото или видео.
        Пробует оба parse_mode. Подпись может быть пустой."""
        for pm in (parse_mode, None):
            cap = _caption_for_parse_mode(caption, pm, parse_mode) or None
            try:
                with open(path, "rb") as fh:
                    photo_or_video = InputFile(fh)
                    if media_type == "video":
                        return await self.bot.send_video(
                            chat_id=channel_id, video=photo_or_video, caption=cap, parse_mode=pm
                        )
                    else:
                        return await self.bot.send_photo(
                            chat_id=channel_id, photo=photo_or_video, caption=cap, parse_mode=pm
                        )
            except Exception as e:
                logger.warning(f"send media [{media_type}, parse={pm}] в {channel_id}: {e}")
        return None

    async def _publish(self, post: dict) -> dict:
        """
        Отправляет пост в Telegram-канал.

        Для WB постов (wbbasket.ru):
          - Скачивает webp через резидентный прокси
          - Отправляет как InputFile(BytesIO) — Telegram принимает байты напрямую
          - Если картинку не удалось скачать/отправить — WB-пост не публикуется текстом

        Для остальных постов — стандартная логика:
          1. parse_mode + картинка (URL)
          2. plain text + картинка
          3. parse_mode без картинки
          4. plain text без картинки
        """

        channel_id = post["channel_id"]
        content = post["content"]
        image_url = post.get("image_url")
        post_parse_mode = post.get("parse_mode", "Markdown")
        is_wb_product = post.get("format") == "wb_product"

        # Предохранитель: запретный контент в готовом посте (война/Украина/дрон/ЛГБТ)
        # не публикуем — помечаем skipped (ловит уже сгенерированные до фикса посты).
        try:
            from ai_client import _contains_forbidden
            if _contains_forbidden(content):
                buffer.mark_skipped(post["id"])
                logger.warning(f"Пост [{channel_id}] с запретным контентом → skipped, не публикую")
                return {"success": False, "used_image": False}
        except Exception:
            pass

        # Цель публикации: числовой chat_id (переживает смену @username и приватность),
        # иначе @handle. channel_id оставляем для логов/буфера.
        _card = self._load_channel_by_id(channel_id) or {}
        target = _card.get("chat_id_num") or channel_id

        media_type = post.get("media_type")

        # ---- Relay-референс: альбом (media_group) ----
        tg_file_id = post.get("tg_file_id")
        if media_type == "album" and tg_file_id:
            sent_message = await self._send_album(target, tg_file_id, content, post_parse_mode)
            if sent_message:
                return {"success": True, "used_image": True, "message": sent_message}
            logger.warning(f"Не отправил альбом [{channel_id}] — пробую как текст")

        # ---- Relay-референс: одиночное медиа по file_id (без скачивания, любой размер) ----
        elif tg_file_id:
            sent_message = await self._send_by_file_id(target, tg_file_id, media_type, content, post_parse_mode)
            if sent_message:
                return {"success": True, "used_image": True, "message": sent_message}
            logger.warning(f"Не отправил по file_id [{channel_id}] — пробую как текст")

        # ---- Легаси-референс с локальным медиа-файлом (фото/видео «как есть») ----
        media_path = post.get("media_path")
        if media_path:
            import os
            if os.path.exists(media_path):
                sent = await self._send_local_media(
                    target, media_path, media_type, content, post_parse_mode
                )
                if sent:
                    try:
                        os.remove(media_path)  # файл больше не нужен
                    except OSError:
                        pass
                    return {"success": True, "used_image": True, "message": sent}
                logger.warning(f"Не удалось отправить медиа-файл [{channel_id}] — публикую как текст")
            else:
                logger.warning(f"Медиа-файл пропал [{channel_id}]: {media_path}")

        # ---- WB CDN: скачиваем картинку через прокси ----
        wb_image_bytes: bytes | None = None
        if image_url and "wbbasket.ru" in image_url:
            wb_image_bytes = await self._download_wb_image(image_url)
            if wb_image_bytes:
                logger.info(f"WB CDN: картинка скачана ({len(wb_image_bytes)} байт)")
            else:
                logger.warning(f"WB CDN: не удалось скачать картинку [{channel_id}]")
                if is_wb_product:
                    buffer.mark_skipped(post["id"])
                    logger.warning(f"WB-пост без картинки не публикую [{channel_id}]")
                    return {"success": False, "used_image": False, "reason": "wb_image_unavailable"}
                image_url = None

        # ---- Вспомогательные отправщики (пробуют оба parse_mode) ----
        async def _send_with_image():
            for pm in (post_parse_mode, None):
                cap = _caption_for_parse_mode(content, pm, post_parse_mode)  # подпись медиа ≤1024
                try:
                    if wb_image_bytes:
                        photo = InputFile(BytesIO(wb_image_bytes), filename="product.webp")
                    else:
                        photo = image_url
                    return await self.bot.send_photo(
                        chat_id=target, photo=photo, caption=cap, parse_mode=pm
                    )
                except TimedOut:
                    logger.warning(
                        f"send_photo [parse={pm}] in {channel_id}: TimedOut; "
                        "skip retry to avoid duplicate"
                    )
                    return _AMBIGUOUS_SEND
                except Exception as e:
                    logger.warning(f"send_photo [parse={pm}] в {channel_id} не удалась: {e}")
            return None

        async def _send_text():
            for pm in (post_parse_mode, None):
                try:
                    return await self.bot.send_message(chat_id=target, text=content, parse_mode=pm)
                except Exception as e:
                    logger.warning(f"send_message [parse={pm}] в {channel_id} не удалась: {e}")
            return None

        # ---- 1) Пробуем с картинкой ----
        if image_url or wb_image_bytes:
            sent_message = await _send_with_image()
            if _is_ambiguous_send(sent_message):
                return {"success": True, "used_image": True, "message": None, "ambiguous_timeout": True}
            if sent_message:
                return {"success": True, "used_image": True, "message": sent_message}

            # 2) Telegram отверг картинку. Для не-WB — перегенерируем и пробуем ещё раз.
            if image_url and not wb_image_bytes:
                logger.warning(f"Telegram отклонил картинку [{channel_id}] — перегенерирую")
                new_url = await self._regenerate_image(post)
                if new_url:
                    image_url = new_url
                    sent_message = await _send_with_image()
                    if _is_ambiguous_send(sent_message):
                        return {"success": True, "used_image": True, "message": None, "ambiguous_timeout": True}
                    if sent_message:
                        return {"success": True, "used_image": True, "message": sent_message}

            if is_wb_product:
                buffer.mark_skipped(post["id"])
                logger.warning(f"WB-пост без картинки не публикую [{channel_id}]")
                return {"success": False, "used_image": False, "reason": "wb_image_unavailable"}

            logger.warning(f"Публикую БЕЗ картинки (не удалось ни одной): {channel_id}")

        # ---- 3) Без картинки ----
        if is_wb_product:
            buffer.mark_skipped(post["id"])
            logger.warning(f"WB-пост без image_url не публикую [{channel_id}]")
            return {"success": False, "used_image": False, "reason": "wb_image_missing"}

        sent_message = await _send_text()
        if sent_message:
            return {"success": True, "used_image": False, "message": sent_message}

        # Пост нечем публиковать (медиа-файл пропал, нет file_id/URL/текста) —
        # помечаем skipped, чтобы не долбиться им каждый тик.
        dead = (not (content or "").strip() and not image_url
                and not post.get("tg_file_id")
                and (not media_path or not __import__("os").path.exists(media_path)))
        if dead:
            buffer.mark_skipped(post["id"])
            logger.warning(f"Пост [{channel_id}] нечем публиковать (медиа пропало/пусто) → skipped")
            return {"success": False, "used_image": False}

        logger.error(f"Все попытки публикации в {channel_id} провалились")
        return {"success": False, "used_image": False}

    async def _boost_published_post(self, post: dict, result: dict):
        """Send successfully published bot posts into Boost using the real Telegram message id."""
        message = result.get("message")
        if not message:
            return
        try:
            from boost_manager import handle_boost_channel_post_dry_run

            boost_result = await handle_boost_channel_post_dry_run(message, defer_media_groups=False)
            event = boost_result.get("event") or {}
            logger.info(
                "Boost published post result | status={} reason={} channel={} message_id={} event_id={}",
                boost_result.get("status"),
                boost_result.get("reason"),
                post.get("channel_id"),
                getattr(message, "message_id", None),
                event.get("id"),
            )
        except Exception as e:
            logger.exception(f"Boost published post handler failed [{post.get('channel_id')}]: {e}")

    # --------------------------------------------------------
    # Проверка буфера и запуск генерации
    # --------------------------------------------------------

    async def _check_buffer_after_post(self, channel: dict):
        """
        После каждой публикации проверяем уровень буфера.
        Если постов мало — запускаем фоновую генерацию.
        """

        channel_id = channel["channel_id"]
        level = buffer.get_level(channel_id)
        status = buffer.check_status(channel_id)

        if status == "critical":
            # Буфер критически мал — алерт + срочная генерация
            logger.warning(f"🚨 Критический уровень буфера [{channel_id}]: {level} постов")
            await self._alert(
                f"🚨 <b>Критически мало постов!</b>\n"
                f"Канал: {channel_id}\n"
                f"Осталось: {level} постов\n"
                f"Запускаю экстренную генерацию..."
            )
            await self._run_emergency_generation(channel_id)

        elif status == "emergency":
            # Мало — тихо запускаем генерацию в фоне
            logger.info(f"⚡ Мало постов [{channel_id}]: {level} — запускаю генерацию")
            asyncio.create_task(self._run_emergency_generation(channel_id))

    async def _run_emergency_generation(self, channel_id: str):
        """Запускает экстренную генерацию для канала."""
        try:
            from content_generator import generator
            result = await generator.run_emergency(channel_id)
            generated = result.get("generated", 0)

            if generated > 0:
                logger.success(f"Экстренная генерация [{channel_id}]: +{generated} постов")
                await self._alert(
                    f"✅ <b>Экстренная генерация завершена</b>\n"
                    f"Канал: {channel_id}\n"
                    f"Добавлено постов: {generated}"
                )
            else:
                logger.error(f"Экстренная генерация [{channel_id}]: 0 постов!")
                await self._alert(
                    f"❌ <b>Экстренная генерация не дала постов!</b>\n"
                    f"Канал: {channel_id}\n"
                    f"Проверь RSS-источники и вечнозелёные темы."
                )
        except Exception as e:
            logger.error(f"Ошибка экстренной генерации [{channel_id}]: {e}")

    # --------------------------------------------------------
    # Алерты администратору
    # --------------------------------------------------------

    async def _notify_published(self, post: dict, result: dict):
        """Уведомление об успешной публикации поста по расписанию."""
        channel_id = post["channel_id"]
        fmt = post.get("format", "?")
        topic = post.get("topic", "")[:60]
        has_image = "🖼️ с картинкой" if result.get("used_image") else "📝 без картинки"

        # Превью текста — первые 100 символов контента
        content_preview = post.get("content", "")[:100].strip()
        if len(post.get("content", "")) > 100:
            content_preview += "..."

        # Уровень буфера после публикации
        remaining = buffer.get_level(channel_id)
        buffer_emoji = "✅" if remaining >= 6 else "⚠️" if remaining >= 3 else "🚨"

        text = (
            f"📤 <b>Опубликован пост</b>\n"
            f"Канал: {channel_id}\n"
            f"Формат: {fmt} · {has_image}\n"
            f"Тема: {topic}\n"
            f"───────────────\n"
            f"<i>{content_preview}</i>\n"
            f"───────────────\n"
            f"{buffer_emoji} В очереди осталось: {remaining} постов"
        )

        await self._alert(text)

    async def _alert(self, text: str):
        """Отправляет сообщение администратору."""
        try:
            await self.bot.send_message(
                chat_id=cfg.ADMIN_CHAT_ID,
                text=text,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Не удалось отправить алерт: {e}")

    async def _alert_empty_buffer(self, channel_id: str):
        """Алерт о пустом буфере + попытка экстренной генерации."""
        await self._alert(
            f"📭 <b>Буфер пуст!</b>\n"
            f"Канал: {channel_id}\n"
            f"Пост не был опубликован.\n"
            f"Запускаю генерацию..."
        )
        await self._run_emergency_generation(channel_id)

    # --------------------------------------------------------
    # Вспомогательные
    # --------------------------------------------------------

    def _load_active_channels(self) -> list[dict]:
        """Загружает все активные карточки каналов из БД."""
        import json
        from pathlib import Path

        channels_dir = Path(__file__).parent / "channels"
        channels = []
        for json_file in channels_dir.glob("*.json"):
            if json_file.name.startswith("example_"):
                continue
            try:
                with open(json_file, encoding="utf-8") as f:
                    ch = json.load(f)
                if ch.get("active", True):
                    channels.append(ch)
            except Exception as e:
                logger.error(f"Ошибка загрузки канала {json_file.name}: {e}")
        return channels

    async def post_now(self, channel_id: str) -> dict:
        """
        Публикует следующий пост немедленно (для /post_now команды).
        Возвращает результат: {'success': True, 'post': {...}}
        """
        post = buffer.get_next(channel_id)
        if post is None:
            return {"success": False, "error": "Буфер пуст"}

        result = await self._publish(post)
        if result["success"]:
            buffer.mark_published(post["id"])
            self.record_published(channel_id)
            await self._boost_published_post(post, result)
            return {"success": True, "post": post, "used_image": result["used_image"]}
        else:
            reason = result.get("reason") or "telegram_send_failed"
            return {
                "success": False,
                "error": "Ошибка отправки в Telegram",
                "reason": reason,
                "post": post,
            }


# ============================================================
# ЕДИНСТВЕННЫЙ ЭКЗЕМПЛЯР
# ============================================================
poster = Poster()
