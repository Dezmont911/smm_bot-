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

from pathlib import Path

from loguru import logger

from config import cfg

# Сессия лежит рядом с кодом: /opt/smm_bot/userbot.session
SESSION_PATH = str(Path(__file__).parent / "userbot")


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
    from telethon import TelegramClient
    from telethon.tl.functions.channels import GetFullChannelRequest
    from telethon.errors import (
        UsernameNotOccupiedError, UsernameInvalidError, ChannelPrivateError,
    )

    uname = normalize_handle(username).lstrip("@")
    if not uname:
        raise ValueError("Пустой username")

    client = TelegramClient(SESSION_PATH, cfg.TELEGRAM_API_ID, cfg.TELEGRAM_API_HASH)
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
    limit: int = 10,
    with_media: bool = True,
    media_dir: str | None = None,
) -> dict:
    """
    Читает посты канала-донора для импорта референсов.

    after_id = 0 → первый импорт: берём последние `limit` постов.
    after_id > 0 → только НОВЫЕ посты (id > after_id), для ежедневного слежения.

    Если with_media — фото/видео скачиваются в media_dir, путь кладётся в результат.

    Возвращает: {"handle", "posts": [{"id","text","media_path","media_type"}], "max_id"}
    Посты идут от старых к новым (порядок публикации).
    """
    from telethon import TelegramClient

    uname = normalize_handle(username).lstrip("@")
    if not uname:
        raise ValueError("Пустой username")
    if with_media and not media_dir:
        media_dir = str(Path(__file__).parent / "media")
    if media_dir:
        Path(media_dir).mkdir(parents=True, exist_ok=True)

    client = TelegramClient(SESSION_PATH, cfg.TELEGRAM_API_ID, cfg.TELEGRAM_API_HASH)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise UserbotNotAuthorized("Сессия юзербота не авторизована")

        entity = await client.get_entity(uname)
        real_username = getattr(entity, "username", None) or uname

        # Собираем сообщения (новые сверху)
        collected = []
        fetch_limit = 200 if after_id else limit
        async for msg in client.iter_messages(entity, limit=fetch_limit):
            if after_id and msg.id <= after_id:
                break
            collected.append(msg)
        if not after_id:
            collected = collected[:limit]
        collected.reverse()  # от старых к новым

        posts = []
        for msg in collected:
            text = (msg.message or "").strip()
            media_path, media_type = None, None
            if with_media and getattr(msg, "media", None):
                kind = _media_kind(msg)
                if kind:
                    ext = "jpg" if kind == "photo" else "mp4"
                    path = f"{media_dir}/{real_username}_{msg.id}.{ext}"
                    try:
                        await client.download_media(msg, file=path)
                        media_path, media_type = path, kind
                    except Exception as e:
                        logger.warning(f"Не скачал медиа {real_username}/{msg.id}: {e}")
            # Пропускаем совсем пустые (ни текста, ни медиа)
            if not text and not media_path:
                continue
            posts.append({
                "id": msg.id, "text": text,
                "media_path": media_path, "media_type": media_type,
            })

        max_id = max([m.id for m in collected], default=after_id)
        logger.info(f"read_new_posts @{real_username}: новых {len(posts)} (after_id={after_id}, max_id={max_id})")
        return {"handle": "@" + real_username, "posts": posts, "max_id": max_id}
    finally:
        await client.disconnect()
