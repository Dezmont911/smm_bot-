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
from content_safety import (
    build_content_brief,
    evaluate_topic_candidate,
    validate_generated_post,
    validate_imported_post,
)
from userbot_reader import (
    read_candidates, forward_to_bot, normalize_handle,
)

CHANNELS_DIR = Path(__file__).parent / "channels"
DEFAULT_TAKE = 10  # сколько постов добираем за один «возьми ещё»

# Минимальная длина ТЕКСТ-ТОЛЬКО референса (без медиа). Короче — это навигационная
# шелуха донора («Серия тут», «Прошлая серия тут», пустые посты): стандалоном выглядит
# пусто, поэтому не импортируем. Посты С медиа фильтр не трогает (там подпись может быть
# любой длины). Каналы на референсах — медийные, чистого короткого текста там почти нет.
MIN_REF_TEXT_CHARS = 25

# Лёгкий фильтр: пропускаем явную рекламу (ссылки/цены НЕ трогаем — иначе режем WB)
AD_MARKERS = (
    "реклама", "рекламa", "erid", "ерид", "по вопросам рекламы", "#ad", "промокод",
    "пост не совсем по нашей теме", "не совсем по нашей теме", "финансовая рекомендация",
    "комиссия для продавцов", "для продавцов", "стоматолог", "клиник", "имплант",
    "лечение зуб", "трансфер", "проживание", "путевка", "путёвка",
    "ссылка на чат в whatsapp", "telegram / whatsapp",
)


def _is_ad(text: str) -> bool:
    t = (text or "").lower()
    t = re.sub(r"https?://\S+", " ", t)
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


_LINK_RE = re.compile(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)


def _extract_links(text_html: str) -> list[tuple[str, str]]:
    """
    Достаёт гиперссылки (url, видимый_текст) из HTML-текста донора. Только http(s),
    дедуп по url, без t.me-упоминаний самого донора (служебные ссылки). Нужно, чтобы
    при перефразе (он отдаёт plain-текст) не терять партнёрские/товарные ссылки.
    """
    if not text_html:
        return []
    out, seen = [], set()
    for url, label in _LINK_RE.findall(text_html):
        url = url.strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        if url in seen:
            continue
        seen.add(url)
        label = re.sub(r"<[^>]+>", "", label or "").strip()
        out.append((url, label))
    return out


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


async def _store_reference_post(channel: dict, channel_id: str, handle: str,
                                p: dict, do_rephrase: bool):
    """
    Создаёт запись(и) в буфере для одного поста донора.

    Возвращает список msg_id для пересылки боту (медиа), пустой список для
    текстового поста, или None если пост пустой (не добавлен).
    """
    # Ключ — по ЭФФЕКТИВНОМУ источнику (что увидит бот в forward_from_*): если донор
    # репостит из другого канала, это оригинальный канал+id. Иначе — сам донор+id.
    topic = ref_topic(p.get("match_user") or handle, p.get("match_id") or p["id"])
    raw = p.get("text", "")
    raw_for_safety = raw or re.sub(r"<[^>]+>", " ", p.get("text_html") or "")
    kind = p.get("media_kind")

    import_validation = validate_imported_post(
        channel,
        {
            "channel_id": channel_id,
            "content": p.get("text_html") or raw,
            "format": "reference",
            "topic": topic,
            "media_type": "album" if p.get("group_id") else kind,
        },
    )
    if not import_validation.get("allowed"):
        logger.warning(
            f"Reference import skipped [{channel_id}] {handle}/{p.get('id')}: "
            f"{import_validation.get('reason_code')}"
        )
        return None

    safety = None
    brief = None
    if raw_for_safety.strip():
        safety = evaluate_topic_candidate(
            channel, {"topic": raw_for_safety, "source": "reference_import"}
        )
        if safety["decision"] in ("blocked", "review") or not safety.get("safe_topic"):
            logger.warning(
                f"Reference skipped [{channel_id}] {handle}/{p.get('id')}: "
                f"{safety.get('reason_code')}"
            )
            return None
        brief = build_content_brief(channel, safety, "reference")

    # «Как есть» — HTML (со ссылками); перефраз — простой текст без формата
    if do_rephrase and raw:
        from ai_client import rephrase_text  # ленивый импорт (тяжёлая зависимость)
        try:
            content = await rephrase_text(raw, channel)
        except Exception as e:
            logger.warning(f"Перефраз {handle}/{p['id']} не удался: {e} — беру оригинал")
            content = raw
        parse_mode = None
        # Перефраз отдаёт plain-текст и теряет гиперссылки (<a href>). Если в оригинале
        # были ссылки (например партнёрская/на товар) — сохраняем их: экранируем
        # перефраз под HTML и переклеиваем ссылки CTA-строкой в конце (баг #13).
        links = _extract_links(p.get("text_html") or "")
        if links:
            import html as _html
            body = _html.escape(content or "")
            cta = "\n".join(
                f'<a href="{url}">{(label or url).strip()}</a>' for url, label in links
            )
            content = (body + ("\n\n" if body else "") + cta).strip()
            parse_mode = "HTML"
    else:
        content = p.get("text_html") or raw
        parse_mode = "HTML"

    content = _strip_filtered_sentences(content)  # вырезаем «MAX» и пр.

    if not content and not kind:
        return None  # пустой пост без медиа
    if content:
        if safety is None:
            safety = evaluate_topic_candidate(
                channel, {"topic": content, "source": "reference_import"}
            )
            if safety["decision"] in ("blocked", "review") or not safety.get("safe_topic"):
                logger.warning(
                    f"Reference skipped [{channel_id}] {handle}/{p.get('id')}: "
                    f"{safety.get('reason_code')}"
                )
                return None
            brief = build_content_brief(channel, safety, "reference")
        validation = validate_generated_post(
            channel,
            {"channel_id": channel_id, "content": content, "format": "reference", "topic": topic},
            safety,
            brief or {},
        )
        if not validation.get("allowed"):
            logger.warning(
                f"Reference validation skipped [{channel_id}] {handle}/{p.get('id')}: "
                f"{validation.get('reason_code')}"
            )
            return None

    if kind == "album":
        # members в JSON — origin-id (как увидит бот), для пересылки — id донора
        member_match_ids = [m.get("match_id", m["id"]) for m in p.get("members", [])]
        forward_ids = [m["id"] for m in p.get("members", [])]
        buffer.add({
            "channel_id": channel_id, "content": content or "",
            "format": "reference", "topic": topic,
            "media_type": "album", "status": "awaiting_media",
            "parse_mode": parse_mode,
            "tg_file_id": json.dumps({"members": member_match_ids, "items": {}}),
        })
        return forward_ids
    elif kind:
        buffer.add({
            "channel_id": channel_id, "content": content or "",
            "format": "reference", "topic": topic,
            "media_type": kind, "status": "awaiting_media",
            "parse_mode": parse_mode,
        })
        return [p["id"]]
    else:
        buffer.add({
            "channel_id": channel_id, "content": content,
            "format": "reference", "topic": topic,
            "status": "ready", "parse_mode": parse_mode,
        })
        return []


async def import_for_channel(channel: dict, count: int = DEFAULT_TAKE) -> dict:
    """
    Добирает `count` постов СУММАРНО для канала, равномерно распределяя между
    всеми донорами (round-robin: по одному с каждого по кругу — лента идёт
    вперемешку, а не блоками).

    Дедуп — по РЕАЛЬНОМУ наличию: берём только то, чего у нас ещё нет
    (buffer.source_exists). Опубликованные/в очереди — пропускаем; удалённые
    и очищенные — снова доступны. Никаких меток-окон.

    Возвращает статистику: added / skipped_dups / skipped_limits / refs.
    """
    refs = channel.get("reference_channels", [])
    channel_id = channel["channel_id"]
    if not refs:
        return {"channel_id": channel_id, "added": 0, "refs": 0}

    skipped_dups = 0
    skipped_limits = 0

    # --- Фаза 1: очередь свежих кандидатов с КАЖДОГО донора ---
    queues = []
    pool = max(count * 10, 60)
    for ref in refs:
        handle = normalize_handle(ref.get("handle", ""))
        if not handle:
            continue
        try:
            data = await read_candidates(handle, limit=pool)
        except Exception as e:
            logger.warning(f"Референс {handle} [{channel_id}] чтение: {e}")
            await _notify_admin(f"❌ <b>Импорт референса</b> {handle} → {channel_id}\n<code>{e}</code>")
            continue
        skipped_limits += len(data.get("skipped", []))
        queues.append({
            "ref": ref, "handle": handle,
            "cands": list(reversed(data["posts"])),  # от свежих к старым
            "media": [], "added": 0,
        })

    # --- Фаза 2: round-robin — по одному с каждого донора, пока не наберём `count` ВСЕГО ---
    added_total = 0
    while added_total < count and any(q["cands"] for q in queues):
        progressed = False
        for q in queues:
            if added_total >= count:
                break
            stored = None
            while q["cands"]:
                p = q["cands"].pop(0)
                topic = ref_topic(p.get("match_user") or q["handle"], p.get("match_id") or p["id"])
                if buffer.source_exists(channel_id, topic):
                    skipped_dups += 1
                    continue
                raw = p.get("text", "")
                raw_html = p.get("text_html", "")
                if q["ref"].get("skip_ads", True) and (raw or raw_html) and _is_ad(f"{raw}\n{raw_html}"):
                    logger.debug(f"Референс {q['handle']}: пропуск рекламы (id={p['id']})")
                    continue
                # Текст-только пост (без медиа) короче порога → навигационная шелуха
                # донора («Серия тут», пустышки). Стандалоном выглядит пусто — пропускаем.
                if not p.get("media_kind") and len((raw or "").strip()) < MIN_REF_TEXT_CHARS:
                    logger.debug(
                        f"Референс {q['handle']}: пропуск короткого текст-поста "
                        f"'{(raw or '').strip()[:20]}' (id={p['id']})"
                    )
                    skipped_limits += 1
                    continue
                media_ids = await _store_reference_post(
                    channel, channel_id, q["handle"], p, q["ref"].get("rephrase", True)
                )
                if media_ids is None:
                    continue  # пустой пост — берём следующего
                stored = media_ids
                break
            if stored is None:
                continue  # у этого донора годных кандидатов не осталось
            q["media"].extend(stored)
            q["added"] += 1
            added_total += 1
            progressed = True
        if not progressed:
            break  # ни один донор больше не может добавить

    # --- Фаза 3: пересылаем медиа боту, отдельно по каждому донору ---
    for q in queues:
        if q["media"]:
            try:
                await forward_to_bot(q["handle"], q["media"])
            except Exception as e:
                logger.error(f"Пересылка медиа {q['handle']} → бот: {e}")
                await _notify_admin(
                    f"⚠️ <b>Пересылка медиа</b> {q['handle']} → {channel_id}\n<code>{e}</code>\n"
                    f"Текстовые посты импортированы, медиа-посты подвиснут как awaiting_media."
                )
        if q["added"]:
            logger.info(f"Референс {q['handle']} → {channel_id}: +{q['added']}")

    logger.info(f"Импорт референсов → {channel_id}: всего +{added_total} с {len(queues)} донор(ов) "
                f"(дубли {skipped_dups}, лимиты {skipped_limits})")
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


# Порог «буфер просел» для авто-добора референсов. Слепого ежедневного импорта
# больше нет — добираем ТОЛЬКО когда в очереди канала меньше LOW_BUFFER_MIN постов.
LOW_BUFFER_MIN = 5


async def import_all(count: int = DEFAULT_TAKE) -> dict:
    """Проход по ВСЕМ каналам с референсами (берём новые/добираем старые).
    Не на расписании — оставлен для ручного «импортнуть всё» при необходимости."""
    channels = _load_active_channels()
    total = 0
    for ch in channels:
        try:
            res = await import_for_channel(ch, count=count)
            total += res.get("added", 0)
        except Exception as e:
            logger.error(f"Импорт референсов [{ch.get('channel_id')}]: {e}")
            await _notify_admin(f"❌ <b>Импорт референсов</b> [{ch.get('channel_id')}]\n<code>{e}</code>")
    logger.info(f"Импорт референсов (все): каналов {len(channels)}, постов +{total}")
    return {"channels": len(channels), "added": total}


async def import_low_buffer(min_level: int = LOW_BUFFER_MIN, target: int | None = None) -> dict:
    """Авто-добор для каналов с ДОНОРОМ и просевшим буфером (< min_level).

    Приоритет источника: сначала добираем с донора (референсы) ДО `target`. Если
    донор пуст/исчерпан и буфер всё ещё ниже target — фолбэк: добиваем генерацией
    (для marketplace — WB-парсером) через generator.run_for_channel. Так донорский
    контент в приоритете, но буфер не пустеет.

    Слепого ежедневного импорта нет: ручной долив — кнопкой «📥 Взять».
    """
    from config import cfg
    target = target or getattr(cfg, "BUFFER_TARGET", LOW_BUFFER_MIN)
    channels = _load_active_channels()  # только активные с reference_channels
    topped = 0
    total = 0
    for ch in channels:
        cid = ch.get("channel_id")
        try:
            level = buffer.get_level(cid)
        except Exception:
            level = 0
        if level >= min_level:
            continue  # буфер в норме — не трогаем
        gap = target - level
        if gap <= 0:
            continue

        # 1) приоритет — донор
        try:
            res = await import_for_channel(ch, count=gap)
            added = res.get("added", 0)
            total += added
            if added:
                topped += 1
            logger.info(f"Авто-добор [{cid}]: буфер {level} < {min_level}, с донора +{added}")
        except Exception as e:
            logger.error(f"Авто-добор [{cid}] (донор): {e}")
            await _notify_admin(f"❌ <b>Авто-добор референсов</b> [{cid}]\n<code>{e}</code>")

        # 2) фолбэк — донор не закрыл нехватку → генерация/WB до target
        try:
            new_level = buffer.get_level(cid)
        except Exception:
            new_level = level
        if new_level < target:
            need = target - new_level
            try:
                from content_generator import generator
                r = await generator.run_for_channel(ch, target_count=need)
                gen = r.get("generated", 0)
                total += gen
                if gen:
                    logger.info(f"Авто-добор [{cid}]: фолбэк (донор пуст) +{gen} генерацией/WB")
            except Exception as e:
                logger.error(f"Авто-добор [{cid}] (фолбэк генерация): {e}")

    logger.info(f"Авто-добор (буфер < {min_level}, цель {target}): затронуто каналов {topped}, постов +{total}")
    return {"topped": topped, "added": total}
