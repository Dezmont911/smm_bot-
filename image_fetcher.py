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
    Извлекает 2-3 ключевых слова из темы и переводит их на английский.

    Сначала пробует перевести через Claude (точно).
    Если Claude недоступен — возвращает транслитерацию/фолбэк.
    """
    # Извлекаем ключевые слова из темы
    clean = re.sub(r"[^\w\s]", " ", topic.lower())
    words = clean.split()
    keywords = [w for w in words if w not in _RU_STOP and len(w) > 2]
    ru_query = " ".join(keywords[:3])

    if not ru_query and channel_topic:
        ru_query = channel_topic.split(",")[0].strip()

    if not ru_query:
        return "lifestyle technology"

    # Если запрос уже на латинице — не переводим
    if re.match(r"^[a-zA-Z0-9 ]+$", ru_query):
        return ru_query

    # Переводим через Claude
    try:
        translated = await _translate_with_claude(ru_query)
        if translated:
            return translated
    except Exception as e:
        logger.debug(f"Claude перевод не удался: {e}")

    # Фолбэк: возвращаем латинскую транслитерацию темы канала
    if channel_topic:
        channel_en = channel_topic.split(",")[0].strip()
        # Простые замены для самых частых тем
        fallbacks = {
            "майнкрафт": "minecraft gaming",
            "minecraft": "minecraft gaming",
            "финансы": "personal finance",
            "инвестиции": "investing money",
            "автомобил": "cars automotive",
            "авто": "cars automotive",
            "здоровье": "health fitness",
            "спорт": "sport fitness",
            "технологии": "technology",
            "бизнес": "business",
            "маркетинг": "marketing",
        }
        for ru, en in fallbacks.items():
            if ru in channel_topic.lower():
                return en

    return "lifestyle"


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
