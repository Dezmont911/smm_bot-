"""
reference_importer.py — импорт постов из каналов-доноров (референсы), relay-режим.

Как это работает (без скачивания файлов на сервер):
  1. Юзербот (Telethon) читает донора (публичный — прав не нужно) и проверяет
     лимиты медиа (видео ≤100 МБ и ≤5 мин, документы ≤100 МБ). Негодные — пропуск с логом.
  2. Для каждого годного поста создаём запись в буфере:
       • только текст            → сразу status='ready';
       • есть медиа              → status='awaiting_media' (ждём file_id).
  3. Юзербот ПЕРЕСЫЛАЕТ медиа-сообщения в ЛС бота (server-side, без скачивания,
     без лимита 50 МБ). Бот ловит форвард, достаёт file_id, привязывает к записи
     (матч по topic = 'ref:донор:msg_id') и переводит её в 'ready'.
  4. Публикует по расписанию сам бот: send_photo/send_video(file_id) — без шапки
     «переслано», источник не виден. Подпись = оригинал или перефраз (по флагу).

Умный импорт без дублей. На каждого донора в карточке храним окно:
  • max_imported_id — самый новый уже взятый пост (легаси-имя: last_id);
  • min_imported_id — самый старый уже взятый пост (легаси-имя: oldest_id).
«Возьми ещё N»: сначала НОВЫЕ (id > max), если мало — добираем СТАРЫЕ (id < min),
потом обновляем обе метки. Дедуп — по topic исходного сообщения.
"""

import json
import re
import asyncio
from pathlib import Path

from loguru import logger

from buffer_manager import buffer
from userbot_reader import (
    read_candidates, forward_to_bot, normalize_handle,
)

CHANNELS_DIR = Path(__file__).parent / "channels"
DEFAULT_TAKE = 10  # сколько постов добираем за один «возьми ещё»

# Лёгкий фильтр: пропускаем явную рекламу (ссылки/цены НЕ трогаем — иначе режем WB)
AD_MARKERS = ("реклама", "рекламa", "erid", "ерид", "по вопросам рекламы", "#ad", "промокод")


def _is_ad(text: str) -> bool:
    t = (text or "").lower()
    return any(m in t for m in AD_MARKERS)


# Слова-фильтры: предложение, где встречается такое слово (целиком, регистронезависимо),
# вырезается из текста референса. Напр. промо мессенджера MAX в постах доноров.
FILTER_WORDS = ("max",)
_FILTER_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in FILTER_WORDS) + r")\b", re.IGNORECASE
)


def _strip_filtered_sentences(text: str) -> str:
    """
    Удаляет ПРЕДЛОЖЕНИЯ, содержащие слова из FILTER_WORDS (целиком), сохраняя
    структуру абзацев. «maximum» не трогаем — фильтр по границе слова.
    """
    if not text:
        return text
    out_lines = []
    for line in text.split("\n"):
        sentences = re.split(r"(?<=[.!?…])\s+", line)
        kept = [s for s in sentences if not _FILTER_RE.search(s)]
        out_lines.append(" ".join(kept).strip())
    result = "\n".join(out_lines)
    return re.sub(r"\n{3,}", "\n\n", result).strip()


def ref_topic(handle: str, msg_id: int) -> str:
    """
    Единый ключ исходного сообщения донора. Используется и при создании записи,
    и ботом при матчинге пересланного медиа. Формат: 'ref:донор:msg_id'.
    """
    h = normalize_handle(handle).lstrip("@").lower()
    return f"ref:{h}:{msg_id}"


def _save_card(channel: dict):
    """Сохраняет карточку канала обратно в её JSON (по channel_id)."""
    cid = channel.get("channel_id")
    for jf in CHANNELS_DIR.glob("*.json"):
        try:
            with open(jf, encoding="utf-8") as f:
                if json.load(f).get("channel_id") == cid:
                    with open(jf, "w", encoding="utf-8") as wf:
                        json.dump(channel, wf, ensure_ascii=False, indent=2)
                    return
        except Exception:
            continue
    logger.warning(f"Не нашёл файл карточки для {cid} — метки не сохранены")


async def _notify_admin(text: str):
    """
    Шлёт сообщение администратору в Telegram (ошибки импорта видно прямо в чате,
    а не только в логах на сервере). Использует бот-инстанс постера.
    """
    try:
        from poster import poster
        from config import cfg
        if poster.bot:
            await poster.bot.send_message(chat_id=cfg.ADMIN_CHAT_ID, text=text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Не смог отправить алёрт админу: {e}")


async def import_for_channel(channel: dict, count: int = DEFAULT_TAKE) -> dict:
    """
    Добирает `count` постов для всех референсов одного канала (relay-режим).

    Дедуп — по РЕАЛЬНОМУ наличию: сканируем донора от свежих к старым и берём
    только то, чего у нас ещё нет (buffer.source_exists). Опубликованные и
    лежащие в очереди — пропускаем; удалённые/очищенные — снова доступны
    (их строк в БД нет, значит можно взять заново). Никаких меток-окон.

    Возвращает статистику: added / skipped_dups / skipped_limits / refs.
    """
    from ai_client import rephrase_text  # ленивый импорт (тяжёлая зависимость)

    refs = channel.get("reference_channels", [])
    channel_id = channel["channel_id"]
    if not refs:
        return {"channel_id": channel_id, "added": 0, "refs": 0}

    added_total = 0
    skipped_dups = 0
    skipped_limits = 0

    for ref in refs:
        handle = normalize_handle(ref.get("handle", ""))
        if not handle:
            continue
        do_rephrase = ref.get("rephrase", True)
        skip_ads = ref.get("skip_ads", True)

        # Берём пул свежих кандидатов с запасом — из него отфильтруем уже взятые
        pool = max(count * 10, 60)
        try:
            data = await read_candidates(handle, limit=pool)
        except Exception as e:
            logger.warning(f"Референс {handle} [{channel_id}] чтение: {e}")
            await _notify_admin(f"❌ <b>Импорт референса</b> {handle} → {channel_id}\n<code>{e}</code>")
            continue
        skipped_limits += len(data.get("skipped", []))

        media_to_forward = []   # msg_id для пересылки боту (медиа)
        added_ref = 0
        # posts идут старые→новые; берём от СВЕЖИХ (reversed)
        for p in reversed(data["posts"]):
            if added_ref >= count:
                break
            topic = ref_topic(handle, p["id"])

            # уже есть у нас (в очереди/опубликовано) — пропускаем; удалённого нет в БД
            if buffer.source_exists(channel_id, topic):
                skipped_dups += 1
                continue

            raw = p.get("text", "")
            if skip_ads and raw and _is_ad(raw):
                logger.debug(f"Референс {handle}: пропуск рекламы (id={p['id']})")
                continue

            # «Как есть» — HTML (со ссылками); перефраз — простой текст без формата
            if do_rephrase and raw:
                try:
                    content = await rephrase_text(raw, channel)
                except Exception as e:
                    logger.warning(f"Перефраз {handle}/{p['id']} не удался: {e} — беру оригинал")
                    content = raw
                parse_mode = None
            else:
                content = p.get("text_html") or raw
                parse_mode = "HTML"

            # Вырезаем предложения со словами-фильтрами (напр. промо «MAX»)
            content = _strip_filtered_sentences(content)

            kind = p.get("media_kind")
            if not content and not kind:
                continue  # пустой пост без медиа

            if kind == "album":
                member_ids = [m["id"] for m in p.get("members", [])]
                buffer.add({
                    "channel_id": channel_id, "content": content or "",
                    "format": "reference", "topic": topic,
                    "media_type": "album", "status": "awaiting_media",
                    "parse_mode": parse_mode,
                    "tg_file_id": json.dumps({"members": member_ids, "items": {}}),
                })
                media_to_forward.extend(member_ids)
            elif kind:
                buffer.add({
                    "channel_id": channel_id, "content": content or "",
                    "format": "reference", "topic": topic,
                    "media_type": kind, "status": "awaiting_media",
                    "parse_mode": parse_mode,
                })
                media_to_forward.append(p["id"])
            else:
                buffer.add({
                    "channel_id": channel_id, "content": content,
                    "format": "reference", "topic": topic,
                    "status": "ready", "parse_mode": parse_mode,
                })
            added_ref += 1

        # Пересылаем медиа боту (записи awaiting_media уже созданы → нет гонки)
        if media_to_forward:
            try:
                await forward_to_bot(handle, media_to_forward)
            except Exception as e:
                logger.error(f"Пересылка медиа {handle} → бот: {e}")
                await _notify_admin(
                    f"⚠️ <b>Пересылка медиа</b> {handle} → {channel_id}\n<code>{e}</code>\n"
                    f"Текстовые посты импортированы, медиа-посты подвиснут как awaiting_media."
                )

        added_total += added_ref
        logger.info(f"Референс {handle} → {channel_id}: +{added_ref} (дубли {skipped_dups}, лимиты {skipped_limits})")

    return {
        "channel_id": channel_id, "added": added_total, "refs": len(refs),
        "skipped_dups": skipped_dups, "skipped_limits": skipped_limits,
    }


def _load_active_channels() -> list[dict]:
    channels = []
    for jf in CHANNELS_DIR.glob("*.json"):
        if jf.name.startswith("example_"):
            continue
        try:
            with open(jf, encoding="utf-8") as f:
                ch = json.load(f)
            if ch.get("active", True) and ch.get("reference_channels"):
                channels.append(ch)
        except Exception:
            continue
    return channels


async def import_all(count: int = DEFAULT_TAKE) -> dict:
    """Ежедневный проход по всем каналам с референсами (берём новые/добираем старые)."""
    channels = _load_active_channels()
    total = 0
    for ch in channels:
        try:
            res = await import_for_channel(ch, count=count)
            total += res.get("added", 0)
        except Exception as e:
            logger.error(f"Импорт референсов [{ch.get('channel_id')}]: {e}")
            await _notify_admin(f"❌ <b>Импорт референсов</b> [{ch.get('channel_id')}]\n<code>{e}</code>")
    logger.info(f"Ежедневный импорт референсов: каналов {len(channels)}, постов +{total}")
    return {"channels": len(channels), "added": total}
