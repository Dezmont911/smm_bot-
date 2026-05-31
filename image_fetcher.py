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
from claude_helper import claude_text


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
    subreddits: list[str] | None = None,
    channel_name: str = "",
    image_keywords: list[str] | None = None,
) -> str | None:
    """
    Ищет подходящую картинку для поста.

    Порядок приоритетов:
      1. Reddit (явно заданные или авто-определённые сабреддиты) — реальные скриншоты/арт
      2. Pexels — стоковые фото (запрос формирует Claude на основе темы поста + контекста канала)
      3. Unsplash — резервный

    Авто-определение сабреддитов:
      Если subreddits не заданы вручную, но тема нишевая (игры, аниме, авто и т.д.) —
      Claude предложит подходящие сабреддиты, они сохранятся в JSON канала на будущее.

    Args:
        topic          — тема/инфоповод конкретного поста
        channel_topic  — общая тема канала (например "майнкрафт, игры")
        subreddits     — список сабреддитов из карточки канала (None = ещё не задано)
        channel_name   — название канала
        image_keywords — ключевые слова из карточки канала
        channel_id     — @handle канала (для сохранения авто-сабреддитов в JSON)

    Returns:
        Прямой URL картинки (пригоден для Telegram send_photo) или None
    """
    # Строим полный контекст канала для Claude
    full_channel_context = channel_topic
    if channel_name:
        full_channel_context = f"{channel_name}: {full_channel_context}"
    if image_keywords:
        full_channel_context += ", " + ", ".join(image_keywords)

    # 1. Reddit — только если сабреддиты явно заданы в карточке канала
    if subreddits:
        url = await _fetch_reddit_image(subreddits)
        if url:
            return url
        logger.debug("Reddit не дал картинки, пробуем Pexels/Unsplash")

    if not cfg.PEXELS_API_KEY and not cfg.UNSPLASH_ACCESS_KEY:
        logger.debug("Нет ключей Pexels/Unsplash — пост без картинки")
        return None

    # 3. Pexels / Unsplash — фоллбэк для универсальных тем
    query = await _build_english_query(topic, full_channel_context)

    # Якорим запрос к теме канала коротким ключом, чтобы картинка не «уплывала»
    # (например тема CS2 → не случайный геймпад). Берём первый image_keyword
    # или первое слово темы канала.
    anchor = ""
    if image_keywords:
        anchor = image_keywords[0].strip()
    elif channel_topic:
        first = channel_topic.split(",")[0].strip()
        # Якорь — только если это короткий ключ, а не описание-предложение
        # («Практические советы и лайфхаки…» якорем только портит запрос)
        if len(first.split()) <= 3:
            anchor = first[:30]
    if anchor and anchor.lower() not in query.lower():
        query = f"{anchor} {query}"

    logger.info(f"Картинка (сток): пост «{(topic or '')[:60]}…» → запрос '{query}'")

    if cfg.PEXELS_API_KEY:
        url = await _search_pexels(query)
        if url:
            return url

    if cfg.UNSPLASH_ACCESS_KEY:
        url = await _search_unsplash(query)
        if url:
            return url

    return None



# ============================================================
# Reddit — реальные скриншоты из сообщества
# ============================================================

async def _fetch_reddit_image(subreddits: list[str]) -> str | None:
    """
    Берёт случайное изображение из топ-постов указанных сабреддитов за неделю.

    Reddit отдаёт публичный JSON без API-ключа.
    Фильтруем только посты с прямыми ссылками на картинки.
    """
    import random

    # Перемешиваем сабреддиты — каждый раз разный источник
    shuffled = subreddits[:]
    random.shuffle(shuffled)

    for subreddit in shuffled:
        url = await _reddit_top_image(subreddit)
        if url:
            return url

    return None


async def _reddit_top_image(subreddit: str) -> str | None:
    """Запрашивает топ-посты за неделю из сабреддита и возвращает URL картинки."""
    import random

    # old.reddit.com менее агрессивно блокирует серверные IP чем www.reddit.com
    api_url = f"https://old.reddit.com/r/{subreddit}/top.json?t=week&limit=50"
    headers = {
        # Имитируем браузерный запрос — снижает вероятность 403
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/html, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, headers=headers, timeout=timeout) as resp:
                if resp.status != 200:
                    logger.debug(f"Reddit r/{subreddit}: статус {resp.status}")
                    return None

                data = await resp.json()
                posts = data.get("data", {}).get("children", [])

                # Фильтруем только посты с прямыми ссылками на изображения
                image_urls = []
                for post in posts:
                    pd = post.get("data", {})
                    # Пропускаем NSFW и удалённые посты
                    if pd.get("over_18") or pd.get("removed_by_category"):
                        continue
                    post_url = pd.get("url", "")
                    # Прямые ссылки на картинки
                    if any(post_url.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")):
                        image_urls.append(post_url)
                    # Reddit-hosted images (i.redd.it)
                    elif "i.redd.it" in post_url:
                        image_urls.append(post_url)
                    # Reddit gallery — берём первую картинку
                    elif pd.get("post_hint") == "image" and pd.get("url"):
                        image_urls.append(pd["url"])

                if not image_urls:
                    logger.debug(f"Reddit r/{subreddit}: нет подходящих картинок")
                    return None

                chosen = random.choice(image_urls)
                logger.info(f"Reddit r/{subreddit} OK | {chosen[:70]}...")
                return chosen

    except aiohttp.ClientError as e:
        logger.warning(f"Reddit r/{subreddit} сетевая ошибка: {e}")
        return None
    except Exception as e:
        logger.warning(f"Reddit r/{subreddit} ошибка: {type(e).__name__}: {e}")
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

    # ВЕСЬ текст поста — основа. title (первое предложение) оставляем только
    # для фоллбэка, если Claude недоступен.
    content = (topic or "").strip()
    title = content.split(".")[0].strip()[:120]

    # Определяем язык заголовка
    latin_ratio = len(re.findall(r"[a-zA-Z]", title)) / max(len(title), 1)
    is_english = latin_ratio > 0.6

    # Для игровых каналов — не пытаемся угадать скриншот по теме поста,
    # Pexels не знает Minecraft. Берём случайный вариант из списка чтобы
    # не было одной картинки на все посты.
    import random
    GAMING_QUERIES = {
        "minecraft": [
            "minecraft blocks landscape",
            "sandbox game building blocks",
            "video game adventure exploration",
            "game world fantasy landscape",
            "pixel art game",
            "open world game environment",
            "game building construction",
            "survival game forest",
            "indie game colorful",
            "game crafting workshop",
        ],
        "gaming pc": [
            "gaming setup rgb",
            "gaming pc desk",
            "esports gamer",
            "gaming monitor keyboard",
            "streamer setup",
            "gaming chair rgb",
        ],
        "gaming": [
            "video game screen",
            "gamer playing game",
            "gaming headset",
            "esports competition",
            "game console playing",
            "retro game console",
        ],
    }
    # ── ПЕРВИЧНО: определяем ГЛАВНЫЙ визуальный субъект по ВСЕМУ посту ──
    # Раньше брали только первое предложение (title) — а с живым тоном это
    # обычно эмоциональный крючок без визуального субъекта, отсюда нерелевант.
    # Claude читает весь пост (любой язык), игнорирует «воду» и даёт визуальные
    # ключи. Старая логика по first sentence — только фоллбэк.
    try:
        visual_query = await _extract_visual_keywords(content, channel_context)
        if visual_query:
            # Для игровых каналов принудительно держим название игры в запросе
            # ("rate my house" → "minecraft house", а не "house interior").
            if channel_context in GAMING_QUERIES and channel_context not in visual_query.lower():
                visual_query = f"{channel_context} {visual_query}"
            return visual_query
    except Exception as e:
        logger.debug(f"Claude visual extraction не удался: {e}")

    # ── ФОЛЛБЭК (Claude недоступен): старая логика по первому предложению ──
    if channel_context in GAMING_QUERIES:
        return random.choice(GAMING_QUERIES[channel_context])

    if is_english:
        words = re.findall(r"[a-zA-Z]+", title.lower())
        keywords = [w for w in words if w not in EN_STOP and len(w) > 3]
        title_part = " ".join(keywords[:2])
        if channel_context and title_part:
            return f"{channel_context} {title_part}"
        return title_part or channel_context or "lifestyle"
    else:
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


async def _extract_visual_keywords(post_text: str, channel_context: str = "") -> str | None:
    """
    Определяет ГЛАВНЫЙ визуальный субъект по ВСЕМУ тексту поста через Claude и
    возвращает 2-4 английских ключевых слова для поиска/генерации картинки.

    Работает с любым языком и игнорирует «воду» живого тона (приветствия, эмоции,
    мнения, призывы), беря то, что реально можно сфотографировать/нарисовать.

    Например:
      "Честно, я не верил… но этот мод на выживание в майнкрафте меняет всё"
      + "minecraft" → "minecraft survival gameplay"
    """
    context_hint = f'The channel theme is "{channel_context}". ' if channel_context else ""
    prompt = (
        f"{context_hint}"
        "Below is a social-media post. It may be in Russian and contain conversational "
        "filler — greetings, emotions, opinions, jokes, calls to action. IGNORE the filler "
        "and identify the SINGLE main concrete subject of the post that can be photographed "
        "or rendered. Return ONLY 2-4 English keywords for an image search — specific and "
        "visual, no punctuation, no explanations.\n\nPost:\n"
        f"{post_text[:600]}"
    )

    raw = await claude_text(
        model="claude-haiku-4-5-20251001",  # Haiku — быстро и дёшево
        max_tokens=30,
        messages=[{"role": "user", "content": prompt}],
    )
    if not raw:
        return None

    result = raw.lower().splitlines()[0]
    result = re.sub(r"[\"'.,:;\n]", "", result).strip()
    return result if result else None


async def _translate_with_claude(ru_text: str) -> str | None:
    """
    Переводит 2-3 русских слова в английские через Claude.
    Использует минимальный prompt — быстро и дёшево (Haiku).
    """
    prompt = (
        f"Translate these Russian words to 2-3 English keywords for stock photo search. "
        f"Return ONLY a single short phrase (max 4 words), no explanations, no newlines: {ru_text}"
    )

    raw = await claude_text(
        model="claude-haiku-4-5-20251001",  # Haiku — быстро и дёшево для перевода
        max_tokens=30,
        messages=[{"role": "user", "content": prompt}],
    )
    if not raw:
        return None

    result = raw.lower()
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
        params  = urllib.parse.urlencode({"query": query, "per_page": 15, "orientation": "landscape"})
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
                import random
                return random.choice(photos)["src"]["large"]
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
            "query": query, "per_page": 15,
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

                import random
                url = random.choice(results)["urls"]["regular"]
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
