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
import html as html_lib
from urllib.parse import urlparse
from pathlib import Path

from loguru import logger

from buffer_manager import buffer
from content_safety import (
    build_content_brief,
    evaluate_topic_candidate,
    MARKETPLACE_PRODUCT_LINK_MARKERS,
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
        if "<a " in line.lower() or "href=" in line.lower() or re.search(r"https?://", line, re.IGNORECASE):
            out_lines.append(line.strip())
            continue
        sentences = re.split(r"(?<=[.!?…])\s+", line)
        kept = [s for s in sentences if not _FILTER_RE.search(s)]
        out_lines.append(" ".join(kept).strip())
    result = "\n".join(out_lines)
    return re.sub(r"\n{3,}", "\n\n", result).strip()


_LINK_RE = re.compile(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_URL_RE = re.compile(r'https?://[^\s<>"\]\)]+', re.IGNORECASE)
_MARKDOWN_LINK_RE = re.compile(r'\[([^\]]{1,120})\]\((https?://[^)\s]+)\)', re.IGNORECASE)
_LABEL_URL_RE = re.compile(r'\b([^\n()]{1,80}?)\s*\((https?://[^)\s]+)\)', re.IGNORECASE)
_GENERIC_LINK_LABEL_RE = re.compile(
    r"^\s*(?:🔗\s*)?(?:ссылка(?:\s+на\s+товар)?|тут|здесь|подробнее|читать|смотреть(?:\s+на\s+\w+)?)\s*$",
    re.IGNORECASE,
)
_TRAILING_LINK_PHRASE_RE = re.compile(
    r"\s+(?:по|по этой|по товарной)\s+ссылке(?:\s+ниже)?\s*$",
    re.IGNORECASE,
)
_LINK_PLACEHOLDER_SUFFIX_RE = re.compile(
    r"\s*(?:[-–—:]\s*)?(?:ссылка(?:\s+на\s+товар)?|тут|здесь|подробнее)\s*$",
    re.IGNORECASE,
)
_TRAILING_MARKETPLACE_CTA_RE = re.compile(
    r"\s*[🛒🔗👉➡️•\-–—:]*\s*"
    r"(?:купить|заказать|смотреть|посмотреть|найти|перейти|забрать|открыть)\s+"
    r"(?:на|в)\s+"
    r"(?:ali\s*express|aliexpress|алиэкспресс|wildberries|вайлдберриз|wb|ozon|озон|"
    r"яндекс(?:\s+маркет)?|yandex(?:\s+market)?)\s*$",
    re.IGNORECASE,
)
_MARKETPLACE_CTA_NAMES = (
    "aliexpress", "ali express", "алиэкспресс",
    "wildberries", "вайлдберриз", "wb",
    "ozon", "озон",
    "яндекс маркет", "yandex market",
)
_MARKETPLACE_CTA_VERBS = (
    "купить", "заказать", "смотреть", "посмотреть", "найти", "перейти", "забрать", "открыть",
)
_FOOTER_LINE_MARKERS = (
    "подпишись", "подписывайся", "наш канал", "больше тут", "больше здесь",
    "больше новостей", "читать в канале", "по вопросам рекламы", "реклама",
    "рекламодател", "прайс", "промокод", "партнерский материал",
    "партнёрский материал", "erid", "ерид",
)
_TELEGRAM_HOSTS = {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}


def _source_handle_variants(handle: str | None) -> set[str]:
    h = normalize_handle(handle or "").lstrip("@").lower()
    if not h:
        return set()
    return {
        f"@{h}",
        f"t.me/{h}",
        f"telegram.me/{h}",
        f"https://t.me/{h}",
        f"http://t.me/{h}",
        f"https://telegram.me/{h}",
        f"http://telegram.me/{h}",
    }


def _is_marketplace_channel(channel: dict | None) -> bool:
    return bool(channel and channel.get("channel_type") == "marketplace")


def _clean_url(url: str) -> str:
    url = html_lib.unescape((url or "").strip())
    return url.rstrip(".,;:!?)»”'")


def _url_host(url: str) -> str:
    try:
        return (urlparse(_clean_url(url)).netloc or "").lower()
    except Exception:
        return ""


def _is_telegram_url(url: str) -> bool:
    host = _url_host(url)
    return host in _TELEGRAM_HOSTS


def _is_telegram_invite(url: str) -> bool:
    if not _is_telegram_url(url):
        return False
    path = (urlparse(_clean_url(url)).path or "").lower()
    return path.startswith("/+") or "/joinchat/" in path


def _is_marketplace_product_url(url: str) -> bool:
    low = _clean_url(url).lower()
    return any(marker in low for marker in MARKETPLACE_PRODUCT_LINK_MARKERS)


def _reference_link_label(url: str, label: str | None = None) -> str:
    label = re.sub(r"<[^>]+>", "", label or "").strip()
    label = html_lib.unescape(label)
    low = _clean_url(url).lower()
    if label and not _GENERIC_LINK_LABEL_RE.fullmatch(label) and not label.lower().startswith("http"):
        return label
    if "wildberries." in low or "wb.ru" in low:
        return "Смотреть на Wildberries"
    if "ozon." in low or "ozon.onelink" in low:
        return "Смотреть на Ozon"
    if "aliexpress" in low or "ali.click" in low:
        return "Смотреть на Aliexpress"
    if "market.yandex" in low or "yandex.ru/cc" in low:
        return "Смотреть на Яндекс Маркете"
    return label or "Смотреть товар"


def _looks_like_source_footer_line(line: str, handle: str | None) -> bool:
    if not line or not handle:
        return False
    plain = re.sub(r"<[^>]+>", " ", line)
    plain = re.sub(r"&[a-zA-Z0-9#]+;", " ", plain)
    low = re.sub(r"\s+", " ", plain).strip().lower()
    return any(marker in low for marker in _source_handle_variants(handle))


def _looks_like_reference_footer_line(line: str, handle: str | None, channel: dict | None = None) -> bool:
    if not line:
        return False
    plain = re.sub(r"<[^>]+>", " ", line)
    plain = html_lib.unescape(plain)
    low = re.sub(r"\s+", " ", plain).strip().lower()
    if not low:
        return False
    if _looks_like_source_footer_line(line, handle):
        return True
    if _is_telegram_invite_in_text(line):
        return True
    if any(marker in low for marker in _FOOTER_LINE_MARKERS):
        if _is_marketplace_channel(channel) and _is_marketplace_product_url(line):
            return False
        return True
    only_mentions = re.sub(r"[@\w./:+-]+", "", low).strip(" .,:;!—–|()[]«»")
    if not only_mentions and (re.search(r"@\w{4,}", low) or "t.me/" in low or "telegram.me/" in low):
        return True
    return False


def _is_telegram_invite_in_text(text: str) -> bool:
    low = (text or "").lower()
    return bool(re.search(r"(?:https?://)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)", low))


def _strip_source_footer(text: str, handle: str | None) -> str:
    """Remove donor self-promo/footer lines near the end of a reference post."""
    if not text or not handle:
        return text
    lines = str(text).splitlines()
    if not lines:
        return text
    cutoff = max(0, len(lines) - 7)
    kept = []
    for idx, line in enumerate(lines):
        if idx >= cutoff and _looks_like_source_footer_line(line, handle):
            continue
        kept.append(line)
    result = "\n".join(kept)
    return re.sub(r"\n{3,}", "\n\n", result).strip()


def _is_source_channel_link(url: str, label: str, source_handle: str | None) -> bool:
    if not source_handle:
        return False
    low = f"{url} {label}".lower()
    return any(marker in low for marker in _source_handle_variants(source_handle))


def _link_allowed(url: str, label: str, source_handle: str | None, channel: dict | None = None) -> bool:
    url = _clean_url(url)
    if not url.lower().startswith(("http://", "https://")):
        return False
    if _is_source_channel_link(url, label, source_handle):
        return False
    if _is_telegram_url(url):
        return False
    if _is_marketplace_channel(channel):
        return _is_marketplace_product_url(url)
    return True


def _sanitize_reference_html_links(text: str, source_handle: str | None, channel: dict | None = None) -> str:
    def repl(match: re.Match) -> str:
        url = _clean_url(match.group(1))
        label = re.sub(r"<[^>]+>", "", match.group(2) or "").strip()
        if not _link_allowed(url, label, source_handle, channel):
            return ""
        safe_url = html_lib.escape(url, quote=True)
        safe_label = html_lib.escape(label or _reference_link_label(url))
        return f'<a href="{safe_url}">{safe_label}</a>'

    return _LINK_RE.sub(repl, text or "")


def cleanup_reference_text_before_rephrase(text: str, source_handle: str | None, channel: dict | None = None) -> str:
    """Remove donor/ad footer noise while preserving the body and allowed HTML links."""
    if not text:
        return ""
    cleaned = _sanitize_reference_html_links(str(text), source_handle, channel)
    lines = []
    for line in cleaned.splitlines():
        if _looks_like_reference_footer_line(line, source_handle, channel):
            continue
        lines.append(line.rstrip())
    result = "\n".join(lines)
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    return result


def cleanup_reference_text_after_rephrase(text: str, source_handle: str | None, channel: dict | None = None) -> str:
    """Post-LLM cleanup: remove meta, donor links, generated Markdown/plain links and footer noise."""
    if not text:
        return ""
    try:
        from ai_client import _clean_post_output
        text = _clean_post_output(text)
    except Exception:
        text = (text or "").strip()
    text = _MARKDOWN_LINK_RE.sub(r"\1", text)
    text = _LABEL_URL_RE.sub(r"\1", text)
    text = cleanup_reference_text_before_rephrase(text, source_handle, channel)
    anchors = []

    def keep_anchor(match: re.Match) -> str:
        anchors.append(match.group(0))
        return f"__REF_ANCHOR_{len(anchors) - 1}__"

    text = _LINK_RE.sub(keep_anchor, text)
    text = _URL_RE.sub("", text)
    for i, anchor in enumerate(anchors):
        text = text.replace(f"__REF_ANCHOR_{i}__", anchor)
    lines = []
    for line in text.splitlines():
        clean = re.sub(r"\s{2,}", " ", line).strip()
        if not clean:
            continue
        if _GENERIC_LINK_LABEL_RE.fullmatch(clean):
            continue
        line = _TRAILING_LINK_PHRASE_RE.sub("", line.rstrip())
        if not re.search(r"https?://|<a\s+|href=", line, re.IGNORECASE):
            line = _LINK_PLACEHOLDER_SUFFIX_RE.sub("", line).rstrip()
        if line.strip():
            lines.append(line)
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _strip_link_placeholders_from_body(text: str) -> str:
    if not text:
        return ""
    lines = []
    for line in str(text).splitlines():
        clean = re.sub(r"\s{2,}", " ", line).strip()
        if not clean or _GENERIC_LINK_LABEL_RE.fullmatch(clean):
            continue
        if not re.search(r"https?://|<a\s+|href=", clean, re.IGNORECASE):
            without_placeholder = _LINK_PLACEHOLDER_SUFFIX_RE.sub("", clean).strip()
            if without_placeholder and not _GENERIC_LINK_LABEL_RE.fullmatch(without_placeholder):
                clean = without_placeholder
            else:
                continue
        lines.append(clean)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _looks_like_standalone_marketplace_cta(line: str) -> bool:
    """True for bare CTA lines like "🛒 Купить на AliExpress" without a real URL."""
    if not line or re.search(r"https?://|<a\s+|href=", line, re.IGNORECASE):
        return False
    clean = html_lib.unescape(re.sub(r"<[^>]+>", " ", line)).lower()
    clean = clean.replace("ё", "е")
    clean = re.sub(r"[^a-zа-я0-9]+", " ", clean, flags=re.IGNORECASE).strip()
    if not clean:
        return False
    has_marketplace = any(name.replace("ё", "е") in clean for name in _MARKETPLACE_CTA_NAMES)
    has_cta = any(re.search(rf"\b{re.escape(verb)}\b", clean, re.IGNORECASE) for verb in _MARKETPLACE_CTA_VERBS)
    if not has_marketplace or not has_cta:
        return False
    return len(clean.split()) <= 8


def _strip_marketplace_cta_placeholders_from_body(text: str) -> str:
    if not text:
        return ""
    lines = []
    for line in str(text).splitlines():
        clean = re.sub(r"\s{2,}", " ", line).strip()
        if not clean:
            continue
        if _looks_like_standalone_marketplace_cta(clean):
            continue
        if not re.search(r"https?://|<a\s+|href=", clean, re.IGNORECASE):
            clean = _TRAILING_MARKETPLACE_CTA_RE.sub("", clean).rstrip()
        if clean:
            lines.append(clean)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


_MEANINGFUL_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]{2,}")
_MEANINGFUL_LETTER_RE = re.compile(r"[A-Za-zА-Яа-яЁё]")


def has_meaningful_text(text: str) -> bool:
    """True only for real caption/body text worth sending to the rephrase model."""
    if not text:
        return False
    plain = html_lib.unescape(str(text))
    plain = _LINK_RE.sub(" ", plain)
    plain = re.sub(r"<[^>]+>", " ", plain)
    plain = _MARKDOWN_LINK_RE.sub(" ", plain)
    plain = _URL_RE.sub(" ", plain)
    plain = re.sub(r"[@#][A-Za-zА-Яа-яЁё0-9_]+", " ", plain)
    plain = re.sub(r"[\W_]+", " ", plain, flags=re.UNICODE).strip()
    words = _MEANINGFUL_WORD_RE.findall(plain)
    letters = _MEANINGFUL_LETTER_RE.findall(plain)
    return len(words) >= 2 and len(letters) >= 8


def _looks_like_meta_output(text: str) -> bool:
    if not text:
        return True
    try:
        from ai_client import _looks_like_refusal
        return _looks_like_refusal(text)
    except Exception:
        return False


def _ref_flag(ref_config: dict | None, key: str, default: bool) -> bool:
    if not isinstance(ref_config, dict):
        return default
    return bool(ref_config.get(key, default))


def _extract_links(text_html: str, source_handle: str | None = None, channel: dict | None = None) -> list[tuple[str, str]]:
    """
    Достаёт гиперссылки (url, видимый_текст) из HTML-текста донора. Только http(s),
    дедуп по url, без t.me-упоминаний самого донора (служебные ссылки). Нужно, чтобы
    при перефразе (он отдаёт plain-текст) не терять партнёрские/товарные ссылки.
    """
    if not text_html:
        return []
    out, seen = [], set()
    for url, label in _LINK_RE.findall(text_html):
        url = _clean_url(url)
        label = re.sub(r"<[^>]+>", "", label or "").strip()
        if not _link_allowed(url, label, source_handle, channel):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append((url, label))
    return out


def _extract_reference_links(
    text_html: str,
    raw_text: str,
    source_handle: str | None,
    channel: dict | None = None,
    explicit_links: list | None = None,
) -> list[tuple[str, str]]:
    out, seen = [], set()
    for url, label in _extract_links(text_html or "", source_handle=source_handle, channel=channel):
        if url not in seen:
            seen.add(url)
            out.append((url, label))
    for item in explicit_links or []:
        if isinstance(item, dict):
            url = _clean_url(str(item.get("url") or ""))
            label = str(item.get("label") or "")
        elif isinstance(item, (list, tuple)) and item:
            url = _clean_url(str(item[0] or ""))
            label = str(item[1] if len(item) > 1 else "")
        else:
            continue
        if url in seen:
            continue
        if not _link_allowed(url, label, source_handle, channel):
            continue
        seen.add(url)
        out.append((url, label or _reference_link_label(url)))
    for url in _URL_RE.findall(raw_text or ""):
        url = _clean_url(url)
        if url in seen:
            continue
        if not _link_allowed(url, "", source_handle, channel):
            continue
        seen.add(url)
        out.append((url, _reference_link_label(url)))
    return out


def _restore_allowed_links(content: str, links: list[tuple[str, str]], channel: dict | None = None) -> tuple[str, str | None]:
    if not links:
        return (content or "").strip(), None
    allowed = []
    seen = set()
    for url, label in links:
        url = _clean_url(url)
        if url in seen:
            continue
        if _is_marketplace_channel(channel) and not _is_marketplace_product_url(url):
            continue
        seen.add(url)
        allowed.append((url, _reference_link_label(url, label)))
    if not allowed:
        return (content or "").strip(), None

    body = _sanitize_reference_html_links(content or "", None, channel)
    body = _MARKDOWN_LINK_RE.sub(r"\1", body)
    body = _LABEL_URL_RE.sub(r"\1", body)
    allowed_urls = {_clean_url(url) for url, _ in allowed}

    def drop_existing_allowed_anchor(match: re.Match) -> str:
        url = _clean_url(match.group(1))
        return "" if url in allowed_urls else match.group(0)

    body = _LINK_RE.sub(drop_existing_allowed_anchor, body)
    for url, _label in allowed:
        body = body.replace(url, "")
    body = _URL_RE.sub("", body)
    body = _strip_link_placeholders_from_body(body)
    body = _strip_marketplace_cta_placeholders_from_body(body)
    if "<" not in body and ">" not in body:
        body = html_lib.escape(body)
    existing = set(_clean_url(u) for u, _ in _LINK_RE.findall(body))
    cta = "\n".join(
        f'<a href="{html_lib.escape(url, quote=True)}">{html_lib.escape(label)}</a>'
        for url, label in allowed
        if url not in existing
    )
    if not cta:
        return body, "HTML"
    return (body + ("\n\n" if body else "") + cta).strip(), "HTML"


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
                                p: dict, do_rephrase: bool, ref_config: dict | None = None):
    """
    Создаёт запись(и) в буфере для одного поста донора.

    Возвращает список msg_id для пересылки боту (медиа), пустой список для
    текстового поста, или None если пост пустой (не добавлен).
    """
    # Ключ — по ЭФФЕКТИВНОМУ источнику (что увидит бот в forward_from_*): если донор
    # репостит из другого канала, это оригинальный канал+id. Иначе — сам донор+id.
    include_text = _ref_flag(ref_config, "include_text", True)
    take_media = _ref_flag(ref_config, "take_media", True)
    generate_text_from_media = _ref_flag(ref_config, "generate_text_from_media", False)
    if not include_text and not take_media:
        logger.warning(
            f"Reference skipped [{channel_id}] {handle}/{p.get('id')}: text_and_media_disabled"
        )
        return None

    topic = ref_topic(p.get("match_user") or handle, p.get("match_id") or p["id"])
    raw = p.get("text", "")
    raw_clean = cleanup_reference_text_before_rephrase(raw, handle, channel)
    html_clean = cleanup_reference_text_before_rephrase(p.get("text_html") or "", handle, channel)
    raw_for_safety = raw_clean or re.sub(r"<[^>]+>", " ", html_clean or "")
    meaningful_text = has_meaningful_text(raw_for_safety)
    source_kind = p.get("media_kind")
    kind = source_kind if take_media else None
    source_has_media = bool(source_kind)
    allowed_links = _extract_reference_links(html_clean or "", raw_clean or "", handle, channel, p.get("links"))
    restore_links_allowed = include_text or bool(allowed_links)
    import_content = (html_clean or raw_clean) if include_text else ""
    if _is_marketplace_channel(channel) and allowed_links:
        import_content, _ = _restore_allowed_links(import_content, allowed_links, channel)
    elif _is_marketplace_channel(channel):
        logger.warning(
            f"Reference skipped [{channel_id}] {handle}/{p.get('id')}: missing_marketplace_product_link"
        )
        return None

    import_validation = validate_imported_post(
        channel,
        {
            "channel_id": channel_id,
            "content": import_content,
            "format": "reference",
            "topic": topic,
            "media_type": "album" if (take_media and p.get("group_id")) else kind,
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
    if include_text and meaningful_text:
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
    content = ""
    parse_mode = None
    if not include_text:
        if _is_marketplace_channel(channel):
            content, parse_mode = _restore_allowed_links("", allowed_links, channel)
        if generate_text_from_media and source_has_media:
            logger.info(
                f"Reference {handle}/{p.get('id')} [{channel_id}]: media_vision_unavailable"
            )
    elif not meaningful_text:
        if source_has_media and take_media:
            reason = "media_vision_unavailable" if generate_text_from_media else "no_meaningful_text"
            logger.debug(f"Reference {handle}/{p.get('id')} [{channel_id}]: {reason}, media-only")
        else:
            logger.warning(
                f"Reference skipped [{channel_id}] {handle}/{p.get('id')}: no_meaningful_text"
            )
            return None
    elif do_rephrase and raw_clean:
        from ai_client import rephrase_text  # ленивый импорт (тяжёлая зависимость)
        try:
            content = await rephrase_text(raw_clean, channel)
        except Exception as e:
            logger.warning(f"Перефраз {handle}/{p['id']} не удался: {e} — беру оригинал")
            content = raw_clean
        content = cleanup_reference_text_after_rephrase(content, handle, channel)
        if _looks_like_meta_output(content):
            logger.warning(
                f"Reference rephrase rejected [{channel_id}] {handle}/{p.get('id')}: meta_or_refusal_output"
            )
            content = raw_clean if has_meaningful_text(raw_clean) else ""
        if restore_links_allowed:
            content, restored_parse_mode = _restore_allowed_links(content, allowed_links, channel)
            parse_mode = restored_parse_mode or parse_mode
    else:
        content = html_clean or raw_clean
        parse_mode = "HTML"

    content = cleanup_reference_text_after_rephrase(content, handle, channel)
    if restore_links_allowed:
        content, restored_parse_mode = _restore_allowed_links(content, allowed_links, channel)
        parse_mode = restored_parse_mode or parse_mode
    content = _strip_filtered_sentences(content)  # вырезаем «MAX» и пр.

    if content and _looks_like_meta_output(content):
        logger.warning(
            f"Reference skipped [{channel_id}] {handle}/{p.get('id')}: meta_or_refusal_output"
        )
        return None
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
                    channel, channel_id, q["handle"], p, q["ref"].get("rephrase", True), q["ref"]
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
