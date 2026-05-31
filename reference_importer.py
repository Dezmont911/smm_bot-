"""
reference_importer.py — импорт постов из каналов-доноров (референсов).

Для каждого канала в карточке может быть список `reference_channels`:
  [{"handle": "@donor", "rephrase": bool, "take_media": bool,
    "skip_ads": bool, "last_id": int}]

Логика:
  • Первый импорт (last_id=0) — последние 10 постов донора.
  • Дальше — только НОВЫЕ посты (id > last_id), для ежедневного слежения.
  • Режим «как есть»: медиа берётся 1:1, текст — либо как есть, либо перефраз.
  • Лёгкий фильтр рекламы по словам-триггерам (реклама/ЕРИД) — можно выключить.
  • Импортированные посты кладутся в буфер как готовые (status=ready).
"""

import json
from pathlib import Path

from loguru import logger

from buffer_manager import buffer
from userbot_reader import read_new_posts, normalize_handle

CHANNELS_DIR = Path(__file__).parent / "channels"
MEDIA_DIR = str(Path(__file__).parent / "media")
FIRST_IMPORT_LIMIT = 10

# Лёгкий фильтр: пропускаем явную рекламу (ссылки/цены НЕ трогаем — иначе режем WB)
AD_MARKERS = ("реклама", "рекламa", "erid", "ерид", "по вопросам рекламы", "#ad", "промокод")


def _is_ad(text: str) -> bool:
    t = (text or "").lower()
    return any(m in t for m in AD_MARKERS)


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
    logger.warning(f"Не нашёл файл карточки для {cid} — last_id не сохранён")


async def import_for_channel(channel: dict) -> dict:
    """Импортирует новые посты из всех референсов одного канала. Возвращает статистику."""
    from ai_client import rephrase_text  # ленивый импорт (тяжёлая зависимость)

    refs = channel.get("reference_channels", [])
    channel_id = channel["channel_id"]
    if not refs:
        return {"channel_id": channel_id, "added": 0, "refs": 0}

    added_total = 0
    changed = False

    for ref in refs:
        handle = normalize_handle(ref.get("handle", ""))
        if not handle:
            continue
        after = int(ref.get("last_id", 0) or 0)
        first = after == 0
        take_media = ref.get("take_media", True)
        do_rephrase = ref.get("rephrase", True)
        skip_ads = ref.get("skip_ads", True)

        try:
            data = await read_new_posts(
                handle, after_id=after,
                limit=FIRST_IMPORT_LIMIT if first else 50,
                with_media=take_media, media_dir=MEDIA_DIR,
            )
        except Exception as e:
            logger.warning(f"Референс {handle} для {channel_id}: ошибка чтения — {e}")
            continue

        new_max = after
        added_ref = 0
        for p in data["posts"]:
            new_max = max(new_max, p["id"])
            text = p["text"]

            if skip_ads and text and _is_ad(text):
                logger.debug(f"Референс {handle}: пропуск рекламы (id={p['id']})")
                continue

            content = text
            if do_rephrase and text:
                content = await rephrase_text(text, channel)

            # Пустые без медиа — пропускаем; без текста с медиа — оставляем как есть
            if not content and not p.get("media_path"):
                continue

            post = {
                "channel_id": channel_id,
                "content": content or "",
                "format": "reference",
                "topic": f"reference {data['handle']} #{p['id']}",
                "status": "ready",
            }
            if p.get("media_path"):
                post["media_path"] = p["media_path"]
                post["media_type"] = p["media_type"]
            buffer.add(post)
            added_ref += 1

        if new_max != after:
            ref["last_id"] = new_max
            changed = True
        added_total += added_ref
        logger.info(f"Референс {handle} → {channel_id}: +{added_ref} постов")

    if changed:
        _save_card(channel)
    return {"channel_id": channel_id, "added": added_total, "refs": len(refs)}


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


async def import_all() -> dict:
    """Ежедневный проход по всем каналам с референсами."""
    channels = _load_active_channels()
    total = 0
    for ch in channels:
        try:
            res = await import_for_channel(ch)
            total += res.get("added", 0)
        except Exception as e:
            logger.error(f"Импорт референсов [{ch.get('channel_id')}]: {e}")
    logger.info(f"Ежедневный импорт референсов: каналов {len(channels)}, постов +{total}")
    return {"channels": len(channels), "added": total}
