"""
userbot_reader.py — чтение публичного Telegram-канала через сессию Telethon-юзербота.

Юзербот (личный аккаунт) авторизован отдельно (scripts/run_userbot_step1/2).
Сессия: /opt/smm_bot/userbot.session. В отличие от Bot API, юзербот может
прочитать описание и историю ЛЮБОГО публичного канала по @username, не будучи
подписанным. Используется для авто-добавления канала: @username → история →
channel_analyzer → готовая карточка (без ручного описания).

Telethon импортируется ЛЕНИВО внутри функции, чтобы модуль грузился и там,
где telethon не установлен (локальная разработка).
"""

import asyncio
from pathlib import Path

from loguru import logger

from config import cfg

# Сессия лежит рядом с кодом: /opt/smm_bot/userbot.session
SESSION_PATH = str(Path(__file__).parent / "userbot")

# Лимит размера медиа: бот всё равно не отправит файл >50 МБ через Bot API,
# поэтому тяжёлые посты (длинные видео и т.п.) пропускаем целиком, не качая.
MAX_MEDIA_MB = 45
MAX_MEDIA_BYTES = MAX_MEDIA_MB * 1024 * 1024
# Потолок времени на скачивание одного медиа (сек) — чтобы сбой файлового DC
# не вешал импорт навсегда (именно это однажды заморозило бота).
DOWNLOAD_TIMEOUT = 60

# Параметры подключения юзербота: ограничиваем переподключения/таймауты, иначе
# при недоступности DC Telethon уходит в бесконечный ретрай и блокирует event loop.
_CLIENT_KWARGS = dict(connection_retries=2, retry_delay=1, request_retries=2, timeout=20)

# Лимиты для relay-режима (file_id, без скачивания). Видео/документы крупнее —
# пропускаем, чтобы не тащить в канал тяжесть. Размер в МБ, длительность в секундах.
RELAY_MAX_VIDEO_MB = 100
RELAY_MAX_VIDEO_SEC = 5 * 60   # 5 минут
RELAY_MAX_DOC_MB = 100


def bot_user_id() -> int:
    """ID бота-публикатора (число до ':' в токене). Юзербот пересылает медиа ему в ЛС."""
    return int(cfg.BOT_TOKEN.split(":")[0])


def _classify_and_check(msg, max_video_mb, max_video_sec, max_doc_mb):
    """
    Определяет тип медиа сообщения и проверяет лимиты для relay.

    Возвращает (kind, ok, reason):
      kind   — 'photo' / 'video' / 'animation' / 'document' / None (нет медиа)
      ok     — проходит ли по лимитам
      reason — текст причины отказа (если ok=False), иначе None
    """
    if not getattr(msg, "media", None):
        return None, True, None  # текстовый пост — медиа нет, относим к ok

    f = getattr(msg, "file", None)
    size = getattr(f, "size", 0) or 0
    size_mb = size / 1024 / 1024
    duration = getattr(f, "duration", None)  # сек, для видео/аудио

    # Фото — всегда лёгкое, пропускаем без проверок
    if getattr(msg, "photo", None):
        return "photo", True, None

    # GIF / animation
    if getattr(msg, "gif", None) or getattr(msg, "video_note", None):
        kind = "animation"
        if size_mb > max_video_mb:
            return kind, False, f"анимация {size_mb:.0f} МБ > {max_video_mb} МБ"
        return kind, True, None

    # Видео (msg.video или документ video/*)
    if getattr(msg, "video", None):
        if size_mb > max_video_mb:
            return "video", False, f"видео {size_mb:.0f} МБ > {max_video_mb} МБ"
        if duration and duration > max_video_sec:
            return "video", False, f"видео {int(duration)}с > {max_video_sec}с"
        return "video", True, None

    # Прочие документы (файлы, аудио)
    if getattr(msg, "document", None):
        if size_mb > max_doc_mb:
            return "document", False, f"документ {size_mb:.0f} МБ > {max_doc_mb} МБ"
        return "document", True, None

    # Неизвестное медиа — пропускаем на всякий случай
    return None, False, "неподдерживаемый тип медиа"


async def read_candidates(
    username: str,
    after_id: int = 0,
    before_id: int = 0,
    limit: int = 10,
    max_video_mb: int = RELAY_MAX_VIDEO_MB,
    max_video_sec: int = RELAY_MAX_VIDEO_SEC,
    max_doc_mb: int = RELAY_MAX_DOC_MB,
) -> dict:
    """
    Читает посты донора для relay-импорта (БЕЗ скачивания) и проверяет лимиты.

    Направление:
      after_id > 0  → новые (id > after_id);
      before_id > 0 → архив (id < before_id);
      оба = 0       → первый импорт (последние посты).

    Тяжёлые/длинные медиа пропускаются (с указанием причины в skipped).
    Сами файлы НЕ качаются — здесь только метаданные. Пересылку делает forward_to_bot().

    Возвращает:
      {"handle", "posts": [{"id","text","media_kind"}], "max_id","min_id",
       "skipped": [{"id","reason"}]}
    Посты — от старых к новым.
    """
    uname = normalize_handle(username).lstrip("@")
    if not uname:
        raise ValueError("Пустой username")

    client = _make_client()
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise UserbotNotAuthorized("Сессия юзербота не авторизована")

        entity = await client.get_entity(uname)
        real_username = getattr(entity, "username", None) or uname

        fetch_limit = 300 if (after_id or before_id) else max(limit * 5, 50)
        iter_kwargs = {"limit": fetch_limit}
        if before_id:
            iter_kwargs["offset_id"] = before_id  # выдаёт id < before_id (архив)

        max_id = after_id
        min_id = before_id or None
        posts, skipped = [], []
        async for msg in client.iter_messages(entity, **iter_kwargs):
            if after_id and msg.id <= after_id:
                break
            if msg.id > max_id:
                max_id = msg.id
            if min_id is None or msg.id < min_id:
                min_id = msg.id
            if len(posts) >= limit:
                break

            text = (msg.message or "").strip()
            kind, ok, reason = _classify_and_check(msg, max_video_mb, max_video_sec, max_doc_mb)

            if not ok:
                skipped.append({"id": msg.id, "reason": reason})
                logger.info(f"Пропуск {real_username}/{msg.id}: {reason}")
                continue
            # совсем пустые (ни текста, ни медиа) — мимо
            if not text and not kind:
                continue

            posts.append({"id": msg.id, "text": text, "media_kind": kind})

        posts.reverse()  # от старых к новым
        if min_id is None:
            min_id = max_id
        logger.info(
            f"read_candidates @{real_username}: годных {len(posts)}, пропущено {len(skipped)} "
            f"(after={after_id}, before={before_id}, max_id={max_id}, min_id={min_id})"
        )
        return {
            "handle": "@" + real_username, "posts": posts,
            "max_id": max_id, "min_id": min_id, "skipped": skipped,
        }
    finally:
        await client.disconnect()


async def forward_to_bot(username: str, msg_ids: list[int]) -> int:
    """
    Пересылает указанные сообщения донора В ЛС бота-публикатора (обычный форвард,
    БЕЗ drop_author — чтобы у бота сохранился forward_from_chat/_message_id для
    матчинга). Server-side: файлы не качаются, лимита 50 МБ нет.

    Возвращает число успешно пересланных. Бросает понятную ошибку, если юзербот
    не может писать боту (не нажат /start) или донор запретил пересылку.
    """
    if not msg_ids:
        return 0
    from telethon.errors import ChatForwardsRestrictedError

    uname = normalize_handle(username).lstrip("@")
    client = _make_client()
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise UserbotNotAuthorized("Сессия юзербота не авторизована")
        donor = await client.get_entity(uname)
        try:
            bot_entity = await client.get_entity(bot_user_id())
        except Exception:
            raise RuntimeError(
                "Юзербот не может открыть диалог с ботом. Зайди в Telegram под "
                "аккаунтом-юзерботом и нажми /start у бота (разово)."
            )
        try:
            sent = await client.forward_messages(bot_entity, msg_ids, donor, drop_author=False)
        except ChatForwardsRestrictedError:
            raise RuntimeError(
                f"Донор @{uname} запретил пересылку/сохранение контента — такие каналы не поддерживаются."
            )
        sent_list = sent if isinstance(sent, list) else [sent]
        n = len([m for m in sent_list if m])
        logger.info(f"forward_to_bot @{uname}: переслано {n}/{len(msg_ids)} в ЛС бота")
        return n
    finally:
        await client.disconnect()


def _make_client():
    """Создаёт TelegramClient с ограниченными ретраями/таймаутами."""
    from telethon import TelegramClient
    return TelegramClient(
        SESSION_PATH, cfg.TELEGRAM_API_ID, cfg.TELEGRAM_API_HASH, **_CLIENT_KWARGS
    )


def normalize_handle(text: str) -> str:
    """Достаёт @username из любого формата: @x, t.me/x, https://t.me/x, x."""
    t = (text or "").strip()
    if "t.me/" in t:
        t = t.split("t.me/")[-1]
    t = t.strip().lstrip("@").strip("/").split()[0].split("?")[0]
    return "@" + t if t else ""


class UserbotNotAuthorized(RuntimeError):
    """Сессия юзербота отсутствует или не авторизована."""


async def read_channel(username: str, limit: int = 40) -> dict:
    """
    Читает публичный канал по @username.

    Возвращает:
      {"handle": "@real_username", "title": "...", "about": "...",
       "posts": [текст, ...], "post_count": N}

    Бросает:
      UserbotNotAuthorized — нет/невалидна сессия юзербота
      ValueError — канал не найден / это не канал
    """
    from telethon.tl.functions.channels import GetFullChannelRequest
    from telethon.errors import (
        UsernameNotOccupiedError, UsernameInvalidError, ChannelPrivateError,
    )

    uname = normalize_handle(username).lstrip("@")
    if not uname:
        raise ValueError("Пустой username")

    client = _make_client()
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise UserbotNotAuthorized(
                "Сессия юзербота не авторизована — запусти scripts/run_userbot_step1/2"
            )

        try:
            entity = await client.get_entity(uname)
        except (UsernameNotOccupiedError, UsernameInvalidError):
            raise ValueError(f"Канал @{uname} не найден (проверь username)")
        except ChannelPrivateError:
            raise ValueError(f"Канал @{uname} закрытый — нужен доступ/приглашение")

        # Должен быть канал (broadcast), а не пользователь/бот
        if not getattr(entity, "broadcast", False) and not getattr(entity, "megagroup", False):
            raise ValueError(f"@{uname} — это не канал")

        title = getattr(entity, "title", uname)
        real_username = getattr(entity, "username", None) or uname

        about = ""
        try:
            full = await client(GetFullChannelRequest(entity))
            about = (full.full_chat.about or "").strip()
        except Exception as e:
            logger.debug(f"Описание @{uname} недоступно: {type(e).__name__}")

        posts: list[str] = []
        async for msg in client.iter_messages(entity, limit=limit):
            txt = (msg.message or "").strip()
            if len(txt) >= 30:  # отсекаем подписи/эмодзи-однострочники
                posts.append(txt)

        logger.info(f"Юзербот прочитал @{real_username}: постов с текстом {len(posts)}")
        return {
            "handle": "@" + real_username,
            "title": title,
            "about": about,
            "posts": posts,
            "post_count": len(posts),
        }
    finally:
        await client.disconnect()


def _media_kind(msg) -> str | None:
    """Тип медиа сообщения: 'photo' / 'video' / None (текст или неподдерживаемое)."""
    if getattr(msg, "photo", None):
        return "photo"
    if getattr(msg, "video", None):
        return "video"
    doc = getattr(msg, "document", None)
    mime = getattr(doc, "mime_type", "") if doc else ""
    if mime.startswith("image/"):
        return "photo"
    if mime.startswith("video/"):
        return "video"
    return None


async def read_new_posts(
    username: str,
    after_id: int = 0,
    before_id: int = 0,
    limit: int = 10,
    with_media: bool = True,
    media_dir: str | None = None,
) -> dict:
    """
    Читает посты канала-донора для импорта референсов.

    Направление сбора:
      after_id = before_id = 0 → первый импорт: последние `limit` постов.
      after_id > 0  → ВПЕРЁД: только новые посты (id > after_id).
      before_id > 0 → НАЗАД: посты из архива старше (id < before_id).

    Если with_media — фото/видео скачиваются в media_dir, путь кладётся в результат.
    Тяжёлые посты (>MAX_MEDIA_MB) пропускаются целиком, добираются следующие.

    Возвращает: {"handle", "posts": [...], "max_id", "min_id"}.
      max_id — наибольший id среди просмотренных (верхняя граница окна),
      min_id — наименьший id среди просмотренных (нижняя граница окна).
    Посты идут от старых к новым (порядок публикации).
    """
    uname = normalize_handle(username).lstrip("@")
    if not uname:
        raise ValueError("Пустой username")
    if with_media and not media_dir:
        media_dir = str(Path(__file__).parent / "media")
    if media_dir:
        Path(media_dir).mkdir(parents=True, exist_ok=True)

    client = _make_client()
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise UserbotNotAuthorized("Сессия юзербота не авторизована")

        entity = await client.get_entity(uname)
        real_username = getattr(entity, "username", None) or uname

        # Сканируем сообщения (всегда новыми сверху). Для «назад» задаём offset_id,
        # чтобы Telethon отдавал сообщения СТАРШЕ before_id. Тяжёлые медиа-посты
        # пропускаем целиком и добираем следующие, пока не наберём `limit`.
        fetch_limit = 300 if (after_id or before_id) else max(limit * 5, 50)
        iter_kwargs = {"limit": fetch_limit}
        if before_id:
            iter_kwargs["offset_id"] = before_id  # выдаёт id < before_id (архив)

        max_id = after_id            # верхняя граница окна
        min_id = before_id or None   # нижняя граница окна
        posts = []
        async for msg in client.iter_messages(entity, **iter_kwargs):
            if after_id and msg.id <= after_id:
                break
            # двигаем границы окна по ВСЕМ просмотренным (даже пропущенным тяжёлым)
            if msg.id > max_id:
                max_id = msg.id
            if min_id is None or msg.id < min_id:
                min_id = msg.id
            if len(posts) >= limit:
                break

            text = (msg.message or "").strip()
            media_path, media_type = None, None

            if with_media and getattr(msg, "media", None):
                kind = _media_kind(msg)
                if kind:
                    size = getattr(getattr(msg, "file", None), "size", 0) or 0
                    if size > MAX_MEDIA_BYTES:
                        logger.info(
                            f"Пропуск тяжёлого поста {real_username}/{msg.id}: "
                            f"{size / 1024 / 1024:.0f} МБ > {MAX_MEDIA_MB} МБ"
                        )
                        continue  # весь пост мимо, ищем следующий
                    ext = "jpg" if kind == "photo" else "mp4"
                    path = f"{media_dir}/{real_username}_{msg.id}.{ext}"
                    try:
                        await asyncio.wait_for(
                            client.download_media(msg, file=path),
                            timeout=DOWNLOAD_TIMEOUT,
                        )
                        media_path, media_type = path, kind
                    except asyncio.TimeoutError:
                        logger.warning(
                            f"Таймаут скачивания {real_username}/{msg.id} "
                            f"({DOWNLOAD_TIMEOUT}с) — пропускаю пост"
                        )
                        continue
                    except Exception as e:
                        logger.warning(f"Не скачал медиа {real_username}/{msg.id}: {e} — пропускаю пост")
                        continue

            # Пропускаем совсем пустые (ни текста, ни медиа)
            if not text and not media_path:
                continue
            posts.append({
                "id": msg.id, "text": text,
                "media_path": media_path, "media_type": media_type,
            })

        posts.reverse()  # от старых к новым (порядок публикации)
        if min_id is None:
            min_id = max_id
        logger.info(
            f"read_new_posts @{real_username}: постов {len(posts)} "
            f"(after_id={after_id}, before_id={before_id}, max_id={max_id}, min_id={min_id})"
        )
        return {"handle": "@" + real_username, "posts": posts, "max_id": max_id, "min_id": min_id}
    finally:
        await client.disconnect()
