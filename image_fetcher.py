"""
image_fetcher.py — Автоматический поиск картинок для постов

Поддерживает два источника (в порядке приоритета):
  1. Pexels — 200 запросов/час, бесплатно, работает с РФ
     Регистрация: https://www.pexels.com/api/
     Нужен ключ: PEXELS_API_KEY в .env

  2. Unsplash — 50 запросов/час, бесплатно
     Регистрация: https://unsplash.com/developers → New Application
     Нужен ключ: UNSPLASH_ACCESS_KEY в .env

Оба API принимают ТОЛЬКО английские запросы — модуль автоматически
переводит русские ключевые слова через Claude перед поиском.

Если ключей нет — возвращает None, пост публикуется без картинки.

Использование:
    from image_fetcher import fetch_image_url
    url = await fetch_image_url(topic="Как откладывать деньги", channel_topic="личные финансы")
"""

import asyncio
import json
import re
import urllib.request
import urllib.parse
import urllib.error
import aiohttp
from loguru import logger

from config import cfg


# ============================================================
# API endpoints
# ============================================================
UNSPLASH_SEARCH = "https://api.unsplash.com/search/photos"
PEXELS_SEARCH   = "https://api.pexels.com/v1/search"

# Стоп-слова русского языка — не несут смысла для поиска картинок
_RU_STOP = {
    "как", "что", "для", "при", "или", "это", "так", "уже", "все", "всё",
    "от", "до", "по", "из", "на", "в", "и", "с", "о", "а", "но", "не",
    "то", "бы", "же", "ли", "за", "под", "над", "без", "через", "перед",
    "после", "между", "если", "когда", "чтобы", "можно", "нужно", "надо",
    "топ", "лучших", "лучшие", "самых", "самые", "новый", "новые",
}


# ============================================================
# Публичный API
# ============================================================

async def fetch_image_url(
    topic: str,
    channel_topic: str = "",
) -> str | None:
    """
    Ищет подходящую картинку для поста.

    Переводит русскую тему в английские ключевые слова через Claude,
    затем ищет в Pexels (приоритет), потом в Unsplash.

    Args:
        topic        — тема/инфоповод конкретного поста
        channel_topic — общая тема канала (помогает при коротких темах)

    Returns:
        Прямой URL картинки (пригоден для Telegram send_photo) или None
    """
    if not cfg.PEXELS_API_KEY and not cfg.UNSPLASH_ACCESS_KEY:
        logger.debug("Нет ключей Pexels/Unsplash — пост без картинки")
        return None

    # Переводим русские ключевые слова в английские для поиска
    query = await _build_english_query(topic, channel_topic)
    logger.debug(f"Ищу картинку | запрос: '{query}'")

    # Pexels — первый приоритет (выше лимит, лучше доступность из РФ)
    if cfg.PEXELS_API_KEY:
        url = await _search_pexels(query)
        if url:
            return url

    # Unsplash — резервный
    if cfg.UNSPLASH_ACCESS_KEY:
        url = await _search_unsplash(query)
        if url:
            return url

    return None


# ============================================================
# Построение запроса: русский → английский
# ============================================================

async def _build_english_query(topic: str, channel_topic: str = "") -> str:
    """
    Строит английский поисковый запрос из темы поста и тематики канала.

    Логика:
    - Определяет контекст канала (gaming, finance, etc.)
    - Если тема на английском — фильтрует стоп-слова, берёт 1-2 ключевых слова
    - Если тема на русском — переводит через Claude
    - Всегда добавляет контекст канала к запросу
    """
    # Словарь тематик каналов → английские ключевые слова
    CHANNEL_MAP = {
        "майнкрафт": "minecraft", "minecraft": "minecraft",
        "игр": "gaming", "игры": "gaming", "steam": "gaming pc",
        "финансы": "finance money", "инвестиц": "investing",
        "здоровье": "health wellness", "спорт": "sport fitness",
        "технологи": "technology", "бизнес": "business",
        "маркетинг": "marketing", "авто": "cars automotive",
        "путешеств": "travel", "кулинар": "food cooking",
        "красот": "beauty", "мода": "fashion style",
        "криптовалют": "cryptocurrency", "программирован": "programming",
    }

    # Английские стоп-слова для фильтрации
    EN_STOP = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been",
        "have", "has", "had", "do", "does", "did", "will", "would",
        "could", "should", "may", "might", "can", "this", "that",
        "these", "those", "it", "its", "for", "on", "in", "at", "to",
        "of", "and", "or", "but", "with", "from", "by", "as", "all",
        "out", "up", "how", "what", "when", "here", "there", "their",
        "you", "your", "our", "we", "they", "them", "now", "just",
        "get", "got", "new", "big", "top", "best", "more", "about",
    }

    # Определяем контекст канала
    channel_context = ""
    for ru_key, en_val in CHANNEL_MAP.items():
        if ru_key in channel_topic.lower():
            channel_context = en_val
            break
    # Если канал уже на английском
    if not channel_context and channel_topic:
        ch_first = channel_topic.split(",")[0].strip()
        if re.match(r"^[a-zA-Z0-9\s]+$", ch_first):
            channel_context = ch_first.lower().split()[0]

    # Берём только заголовок (до первой точки)
    title = topic.split(".")[0].strip()[:120]

    # Определяем язык заголовка
    latin_ratio = len(re.findall(r"[a-zA-Z]", title)) / max(len(title), 1)
    is_english = latin_ratio > 0.6

    if is_english:
        # Заголовок на английском — фильтруем стоп-слова
        words = re.findall(r"[a-zA-Z]+", title.lower())
        keywords = [w for w in words if w not in EN_STOP and len(w) > 3]
        title_part = " ".join(keywords[:2])
        if channel_context and title_part:
            return f"{channel_context} {title_part}"
        return title_part or channel_context or "gaming"
    else:
        # Заголовок на русском — переводим через Claude
        clean = re.sub(r"[^\w\s]", " ", title.lower())
        words = clean.split()
        keywords = [w for w in words if w not in _RU_STOP and len(w) > 2]
        ru_query = " ".join(keywords[:3]) or channel_topic.split(",")[0].strip()

        if not ru_query:
            return channel_context or "lifestyle"

        try:
            translated = await _translate_with_claude(ru_query)
            if translated:
                if channel_context and channel_context not in translated:
                    return f"{channel_context} {translated}"
                return translated
        except Exception as e:
            logger.debug(f"Claude перевод не удался: {e}")

        return channel_context or "lifestyle technology"


async def _translate_with_claude(ru_text: str) -> str | None:
    """
    Переводит 2-3 русских слова в английские через Claude.
    Использует минимальный prompt — быстро и дёшево (Haiku).
    """
    import anthropic
    client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)

    prompt = (
        f"Translate these Russian words to 2-3 English keywords for stock photo search. "
        f"Return ONLY a single short phrase (max 4 words), no explanations, no newlines: {ru_text}"
    )

    message = client.messages.create(
        model=cfg.CLAUDE_MODEL,
        max_tokens=30,
        messages=[{"role": "user", "content": prompt}],
    )

    result = message.content[0].text.strip().lower()
    # Берём только первую строку (на случай многострочного ответа)
    result = result.splitlines()[0]
    # Убираем лишние символы
    result = re.sub(r"[\"'.,:;\n]", "", result).strip()
    logger.debug(f"Перевод запроса: '{ru_text}' → '{result}'")
    return result if result else None


# ============================================================
# Поиск на Pexels
# ============================================================

async def _search_pexels(query: str) -> str | None:
    """
    Запрашивает Pexels через urllib (Cloudflare блокирует aiohttp).
    Запускается в executor чтобы не блокировать event loop.
    Возвращает `src.large` — ~1280px, оптимально для Telegram.
    """
    def _sync_request() -> str | None:
        params  = urllib.parse.urlencode({"query": query, "per_page": 5, "orientation": "landscape"})
        url     = f"{PEXELS_SEARCH}?{params}"
        req     = urllib.request.Request(url, headers={
            "Authorization": cfg.PEXELS_API_KEY,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data   = json.loads(resp.read())
                photos = data.get("photos", [])
                if not photos:
                    return None
                return photos[0]["src"]["large"]
        except urllib.error.HTTPError as e:
            code = e.code
            if code == 401:
                logger.warning("Pexels: неверный API ключ (PEXELS_API_KEY)")
            elif code == 429:
                logger.warning("Pexels: превышен лимит запросов (200/час)")
            else:
                logger.warning(f"Pexels HTTP {code} для запроса: '{query}'")
            return None
        except Exception as e:
            logger.warning(f"Pexels ошибка: {type(e).__name__}: {e}")
            return None

    try:
        loop   = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _sync_request)
        if result:
            logger.info(f"Pexels OK | '{query}' → {result[:70]}...")
        else:
            logger.debug(f"Pexels: нет результатов для '{query}'")
        return result
    except Exception as e:
        logger.warning(f"Pexels executor ошибка: {e}")
        return None


# ============================================================
# Поиск на Unsplash
# ============================================================

async def _search_unsplash(query: str) -> str | None:
    """
    Запрашивает Unsplash и возвращает URL первой подходящей картинки.
    Возвращает `urls.regular` — ~1080px.
    """
    try:
        params  = {
            "query": query, "per_page": 5,
            "orientation": "landscape", "client_id": cfg.UNSPLASH_ACCESS_KEY,
        }
        timeout = aiohttp.ClientTimeout(total=10)

        async with aiohttp.ClientSession() as session:
            async with session.get(
                UNSPLASH_SEARCH, params=params, timeout=timeout
            ) as resp:
                if resp.status == 401:
                    logger.warning("Unsplash: неверный API ключ (UNSPLASH_ACCESS_KEY)")
                    return None
                if resp.status == 403:
                    logger.warning("Unsplash: превышен лимит запросов (50/час)")
                    return None
                if resp.status != 200:
                    logger.warning(f"Unsplash вернул {resp.status} для запроса: '{query}'")
                    return None

                data    = await resp.json()
                results = data.get("results", [])

                if not results:
                    logger.debug(f"Unsplash: нет результатов для '{query}'")
                    return None

                url = results[0]["urls"]["regular"]
                logger.info(f"Unsplash OK | '{query}' → {url[:70]}...")
                return url

    except aiohttp.ClientError as e:
        logger.warning(f"Unsplash сетевая ошибка: {type(e).__name__}: {e}")
        return None
    except Exception as e:
        logger.warning(f"Unsplash неожиданная ошибка: {type(e).__name__}: {e}")
        return None


# ============================================================
# ТЕСТ — python image_fetcher.py
# ============================================================
if __name__ == "__main__":
    import asyncio
    import sys
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    async def test():
        print("=== Тест поиска картинок ===\n")

        test_cases = [
            ("Топ-5 модов для выживания в Minecraft", "майнкрафт, игры"),
            ("Как правильно откладывать деньги", "личные финансы"),
            ("ЦБ РФ сохранил ключевую ставку на 21%", "экономика, финансы"),
            ("Лучшие автомобили 2024 года", "авто, машины"),
        ]

        for topic, channel_topic in test_cases:
            print(f"Тема: {topic}")
            url = await fetch_image_url(topic, channel_topic)
            if url:
                print(f"  OK  {url[:90]}...")
            else:
                print(f"  --  Картинка не найдена")
            print()

    asyncio.run(test())
