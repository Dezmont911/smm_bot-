"""
wb_parser.py — Парсер товаров Wildberries для marketplace-каналов

Стратегия (v2, без 429):
  1. Артикулы берутся из кеша cards/wb_ids_cache.json
     (собираются через браузер, обновлять раз в 1-2 недели)
  2. По каждому артикулу запрашиваем card.wb.ru/cards/v2/detail
     → этот эндпоинт не блокируется с VPS (нет PoW, нет 429)
  3. Картинки — с CDN wbbasket.ru (тоже не блокируется)

Чтобы обновить кеш артикулов: команда /wb_refresh в боте
(бот откроет инструкцию по обновлению через Chrome)
"""

import asyncio
import json
import random
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from loguru import logger

import aiohttp

from config import cfg


# ============================================================
# ПУТЬ К КЕШУ АРТИКУЛОВ
# ============================================================
CACHE_PATH = Path(__file__).parent / "cards" / "wb_ids_cache.json"

# Маппинг ключевых слов категорий канала → ключи в кеше
CATEGORY_MAP = {
    "кроссовки": "кроссовки",
    "обувь": "кроссовки",
    "косметика": "косметика",
    "красота": "косметика",
    "уход": "косметика",
    "наушники": "наушники беспроводные",
    "электроника": "наушники беспроводные",
    "сумка": "сумка женская",
    "аксессуары": "сумка женская",
    "кружка": "термокружка",
    "посуда": "термокружка",
    "кухня": "термокружка",
    "платье": "платье женское",
    "одежда": "платье женское",
    "игры": "настольные игры",
    "игрушки": "настольные игры",
}


def _load_cache() -> dict[str, list[int]]:
    """Загружает кеш артикулов из JSON файла."""
    if not CACHE_PATH.exists():
        logger.warning(f"WB кеш не найден: {CACHE_PATH}")
        return {}
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        categories = data.get("categories", {})
        total = sum(len(v) for v in categories.values())
        logger.debug(f"WB кеш загружен: {len(categories)} категорий, {total} артикулов")
        return categories
    except Exception as e:
        logger.error(f"WB кеш: ошибка чтения: {e}")
        return {}


class WBParser:
    """
    Генерирует посты из товаров Wildberries.
    Данные: кеш артикулов + card.wb.ru API (без 429).
    """

    CARD_API = "https://card.wb.ru/cards/v4/detail"
    # Динамический поиск товаров по ключевому слову. ВАЖНО: с нового VPS (Contabo)
    # этот эндпоинт работает НАПРЯМУЮ (без прокси), а через наши прокси отдаёт 429
    # (их IP зафлажены WB). Поэтому search ходит direct, а карточки — card.wb.ru.
    SEARCH_API = "https://search.wb.ru/exactmatch/ru/common/{ver}/search"
    SEARCH_VERSIONS = ("v5", "v4")  # классическая структура {"products":[...]}
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Origin": "https://www.wildberries.ru",
        "Referer": "https://www.wildberries.ru/",
    }

    def __init__(self):
        self._cache: dict[str, list[int]] = {}
        self._cache_loaded = False

    def _ensure_cache(self):
        """Ленивая загрузка кеша при первом обращении."""
        if not self._cache_loaded:
            self._cache = _load_cache()
            self._cache_loaded = True

    def _pick_articles(self, channel: dict, count: int) -> list[int]:
        """
        Выбирает нужное количество случайных артикулов из кеша.
        Учитывает wb_categories канала, если заданы.
        """
        self._ensure_cache()
        if not self._cache:
            return []

        # Определяем какие категории кеша подходят для канала
        channel_cats = channel.get("wb_categories", [])
        matching_cache_keys = set()

        if channel_cats:
            for cat in channel_cats:
                cat_lower = cat.lower()
                # Прямое совпадение с ключом кеша
                if cat_lower in self._cache:
                    matching_cache_keys.add(cat_lower)
                    continue
                # Поиск через маппинг
                for keyword, cache_key in CATEGORY_MAP.items():
                    if keyword in cat_lower:
                        matching_cache_keys.add(cache_key)
                        break

        # Если ничего не нашли по теме — берём из всех категорий
        if not matching_cache_keys:
            matching_cache_keys = set(self._cache.keys())

        # Собираем пул артикулов из подходящих категорий
        pool: list[int] = []
        for key in matching_cache_keys:
            pool.extend(self._cache.get(key, []))

        if not pool:
            pool = [art for ids in self._cache.values() for art in ids]

        # Перемешиваем и берём нужное количество (с запасом ×5 для фильтрации)
        # Запас нужен: часть артикулов может быть недоступна или вернуть ошибку
        random.shuffle(pool)
        return pool[:min(count * 5, len(pool))]

    async def search_articles(self, keyword: str, pages: int = 1) -> list[int]:
        """
        Динамический поиск артикулов по ключевому слову через search.wb.ru.
        Ходит НАПРЯМУЮ (без прокси) — с нового VPS не блокируется. Перебирает
        версии эндпоинта (WB их меняет). Возвращает список id товаров.
        """
        if not keyword or not keyword.strip():
            return []

        ids: list[int] = []
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(
            headers=self.HEADERS, connector=connector, trust_env=False,
        ) as session:
            for ver in self.SEARCH_VERSIONS:
                url = self.SEARCH_API.format(ver=ver)
                ok = False
                for page in range(1, max(1, pages) + 1):
                    params = {
                        "ab_testing": "false", "appType": "1", "curr": "rub",
                        "dest": "-1257786", "query": keyword.strip(),
                        "resultset": "catalog", "sort": "popular", "spp": "30",
                        "suppressSpellcheck": "false", "page": str(page),
                    }
                    # search.wb.ru жёстко троттлит — на 429 ретраим с backoff
                    data = None
                    bad_version = False
                    for attempt in range(3):
                        try:
                            async with session.get(
                                url, params=params,
                                timeout=aiohttp.ClientTimeout(total=15),
                            ) as resp:
                                if resp.status == 429:
                                    await asyncio.sleep(2 * (attempt + 1))
                                    continue
                                if resp.status != 200:
                                    bad_version = True
                                    break  # версия не та — пробуем следующую
                                data = await resp.json(content_type=None)
                                break
                        except Exception as e:
                            logger.debug(f"WB search [{ver}] '{keyword}': {e}")
                            break
                    if bad_version:
                        break
                    if data is None:
                        break  # 429 не отступил — отдаём что есть (или пусто)

                    products = (data.get("products")
                                or data.get("data", {}).get("products", []))
                    page_ids = [int(p["id"]) for p in products if p.get("id")]
                    if not page_ids:
                        break
                    ids.extend(page_ids)
                    ok = True
                    await asyncio.sleep(random.uniform(0.5, 1.0))
                if ok:
                    break  # версия сработала — другие не нужны

        # дедуп с сохранением порядка
        seen: set[int] = set()
        uniq = [a for a in ids if not (a in seen or seen.add(a))]
        logger.info(f"WB search '{keyword}': найдено {len(uniq)} артикулов")
        return uniq

    async def _discover_articles(self, channel: dict, count: int) -> list[int]:
        """
        Live-подбор артикулов по wb_categories канала через search.wb.ru.
        Round-robin по категориям, дедуп, перемешивание. Запас ×5 на фильтрацию.
        Пусто (нет категорий / WB упал) → вызывающий уйдёт на кеш-фолбэк.
        """
        cats = [c for c in (channel.get("wb_categories") or []) if c and c.strip()]
        if not cats:
            return []

        # Один успешный запрос отдаёт ~100 артикулов — этого с запасом хватает на
        # count*5. Чтобы не злить троттлинг search.wb.ru, дёргаем категории ПО ОДНОЙ
        # (в случайном порядке) и останавливаемся, как только набрали достаточно.
        random.shuffle(cats)
        pool: list[int] = []
        seen: set[int] = set()
        enough = count * 3
        for cat in cats:
            try:
                found = await self.search_articles(cat, pages=1)
            except Exception as e:
                logger.warning(f"WB discover '{cat}': {e}")
                found = []
            for a in found:
                if a not in seen:
                    seen.add(a)
                    pool.append(a)
            if len(pool) >= enough:
                break  # набрали — лишний раз WB не дёргаем
            if not found:
                await asyncio.sleep(2.0)  # троттлинг — пауза перед след. категорией

        random.shuffle(pool)
        return pool[:min(count * 5, len(pool))]

    async def generate_posts(self, channel: dict, count: int = 10) -> list[dict]:
        """
        Главный метод. Генерирует count постов.
        Гибрид: сначала live-поиск товаров (search.wb.ru по wb_categories), при
        неудаче — статический кеш артикулов. Детали/цены/картинки — card.wb.ru.
        """
        # 1) Динамический поиск (свежие товары, без ручного обновления кеша)
        article_ids = await self._discover_articles(channel, count)
        source = "search"

        # 2) Фолбэк на статический кеш (если поиск пуст / WB вернул 429)
        if not article_ids:
            article_ids = self._pick_articles(channel, count)
            source = "cache"

        if not article_ids:
            logger.error(
                "WB-парсер: ни поиск, ни кеш не дали артикулов "
                f"[{channel.get('channel_id', '?')}]. Проверь wb_categories / кеш."
            )
            return []

        logger.info(
            f"WB-парсер [{channel.get('channel_id', '?')}]: источник артикулов="
            f"{source}, кандидатов={len(article_ids)}, запрос {count} постов"
        )

        posts = await self._fetch_posts(article_ids, count)
        if source == "search" and not posts:
            cache_ids = self._pick_articles(channel, count)
            if cache_ids:
                logger.warning(
                    f"WB-парсер [{channel.get('channel_id', '?')}]: search дал артикулы, "
                    "но card API собрал 0 постов — пробую кеш"
                )
                source = "cache_after_empty_cards"
                posts = await self._fetch_posts(cache_ids, count)
        random.shuffle(posts)
        final = posts[:count]
        logger.info(f"WB-парсер: собрано {len(final)} из {count} запрошенных")
        return final

    async def _fetch_posts(self, article_ids: list[int], need: int) -> list[dict]:
        """
        Запрашивает данные по артикулам через card.wb.ru.
        Батчами по 20 штук — WB отдаёт до 20 за раз.
        Если задан WB_PROXY_URL — используем его (нужен для datacenter IP).
        """
        posts = []
        batch_size = 20

        # Собираем список прокси: сначала WB_PROXY_URLS (список), потом WB_PROXY_URL
        proxy_list = list(cfg.WB_PROXY_URLS) if cfg.WB_PROXY_URLS else []
        if not proxy_list and cfg.WB_PROXY_URL:
            proxy_list = [cfg.WB_PROXY_URL]

        if proxy_list:
            logger.debug(f"WB card API: {len(proxy_list)} прокси для ротации")
        else:
            logger.warning("WB card API: прокси не заданы — datacenter IP может быть заблокирован")

        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(
            headers=self.HEADERS,
            connector=connector,
            trust_env=False,
        ) as session:
            for i in range(0, len(article_ids), batch_size):
                if len(posts) >= need:
                    break

                # Случайный прокси для каждого батча
                proxy = random.choice(proxy_list) if proxy_list else None
                batch = article_ids[i:i + batch_size]
                batch_posts = await self._fetch_batch(session, batch, proxy=proxy)
                posts.extend(batch_posts)

                if i + batch_size < len(article_ids):
                    await asyncio.sleep(random.uniform(1.0, 2.0))

        return posts

    async def _fetch_batch(
        self,
        session: aiohttp.ClientSession,
        article_ids: list[int],
        proxy: str | None = None,
    ) -> list[dict]:
        """Запрашивает данные по пачке артикулов через card.wb.ru."""
        return await asyncio.to_thread(self._fetch_batch_sync, article_ids, proxy)

    def _fetch_batch_sync(
        self,
        article_ids: list[int],
        proxy: str | None = None,
    ) -> list[dict]:
        """Синхронный card.wb.ru запрос.

        WB начал резать aiohttp-клиенты на card API 403, даже с браузерными
        headers. urllib проходит как обычный HTTPS-клиент; вызываем его через
        asyncio.to_thread(), чтобы не блокировать event loop.
        """
        nm_param = ";".join(str(a) for a in article_ids)
        params = {
            "appType": "1",
            "curr": "rub",
            "dest": "-1257786",
            "nm": nm_param,
        }
        url = f"{self.CARD_API}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers=self.HEADERS)
        opener = (
            urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
            if proxy
            else urllib.request.build_opener()
        )

        for attempt in range(3):
            try:
                with opener.open(request, timeout=15) as resp:
                    status = getattr(resp, "status", resp.getcode())
                    if status == 429:
                        wait = 10 * (attempt + 1)
                        logger.warning(f"WB card API 429, жду {wait}с")
                        import time
                        time.sleep(wait)
                        continue
                    if status != 200:
                        logger.warning(f"WB card API → HTTP {status}")
                        return []
                    raw = resp.read()
                    data = json.loads(raw.decode("utf-8", "replace"))

                # v4 API: {"products": [...]}  (v2 было: {"data": {"products": [...]}})
                products = data.get("products") or data.get("data", {}).get("products", [])
                posts = []
                for product in products:
                    post = self._format_post(product)
                    if post:
                        posts.append(post)

                logger.debug(f"WB card API: батч {len(article_ids)} → {len(posts)} постов")
                return posts

            except TimeoutError:
                logger.warning(f"WB card API: таймаут (попытка {attempt+1}/3)")
                import time
                time.sleep(3)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = 10 * (attempt + 1)
                    logger.warning(f"WB card API 429, жду {wait}с")
                    import time
                    time.sleep(wait)
                    continue
                logger.warning(f"WB card API → HTTP {e.code}")
                return []
            except Exception as e:
                logger.error(f"WB card API: {e}")
                return []

        return []

    def _format_post(self, product: dict) -> dict | None:
        """Форматирует данные товара в пост для Telegram."""
        try:
            article = product.get("id")
            if not article:
                return None

            name = product.get("name", "Товар без названия")
            brand = product.get("brand", "")
            rating = product.get("reviewRating", 0.0)
            feedbacks = product.get("feedbacks", 0)

            # ---- Цена (в копейках) ----
            sizes = product.get("sizes", [])
            price = 0
            original_price = 0

            # Сначала ищем в sizes[].price (работает в v2 и v4)
            for size in sizes:
                price_data = size.get("price", {})
                p = price_data.get("product", 0)
                b = price_data.get("basic", 0)
                if p > 0:
                    price = p // 100
                    original_price = b // 100
                    break

            # Fallback: поля priceU / salePriceU (некоторые версии API)
            if price == 0:
                sale = product.get("salePriceU", 0)
                basic = product.get("priceU", 0)
                if sale > 0:
                    price = sale // 100
                    original_price = basic // 100 if basic > sale else 0

            if price == 0:
                return None

            # ---- Скидка ----
            discount_line = ""
            if original_price > price and original_price > 0:
                pct = round((1 - price / original_price) * 100)
                if pct >= 15:
                    discount_line = f"🔥 <b>Скидка {pct}%</b> (было {original_price:,} ₽)\n".replace(",", " ")

            # ---- Рейтинг ----
            rating_line = ""
            if rating > 0:
                stars = "⭐" * min(5, round(rating))
                reviews_text = f"{feedbacks:,}".replace(",", " ")
                rating_line = f"{stars} {rating:.1f} · {reviews_text} отзывов\n"

            # ---- Заголовок ----
            title = f"🛍 <b>{brand} — {name}</b>" if brand else f"🛍 <b>{name}</b>"

            # ---- Ссылка ----
            wb_link = f"https://www.wildberries.ru/catalog/{article}/detail.aspx"

            # ---- Текст поста ----
            text = (
                f"{title}\n\n"
                f"{discount_line}"
                f"💰 <b>{price:,} ₽</b>\n".replace(",", " ") +
                rating_line +
                f"\n📦 Арт. {article}\n"
                f'🔗 <a href="{wb_link}">Смотреть на Wildberries</a>'
            )

            return {
                "content": text,
                "image_url": self._get_image_url(int(article)),
                "parse_mode": "HTML",
                "source": "wb_parser",
                "wb_article": str(article),
                "wb_category": product.get("subjectName") or product.get("subject") or "",
            }

        except Exception as e:
            logger.warning(f"WB format error: {e} | id={product.get('id')}")
            return None

    def _get_image_url(self, article: int) -> str:
        """Строит URL картинки с WB CDN по артикулу."""
        vol = article // 100000
        part = article // 1000
        basket = self._get_basket(vol)
        return (
            f"https://basket-{basket:02d}.wbbasket.ru"
            f"/vol{vol}/part{part}/{article}/images/big/1.webp"
        )

    def _get_basket(self, vol: int) -> int:
        """Номер CDN-корзины по vol.

        Baskets 1-19: точная таблица.
        Baskets 20+: кусочно-линейная формула из проверенных точек (2026-05):
          (4622, 26), (7620, 35), (7901, 36), (9017, 39), (10243, 41)
        """
        if   vol <=  143: return 1
        elif vol <=  287: return 2
        elif vol <=  431: return 3
        elif vol <=  719: return 4
        elif vol <= 1007: return 5
        elif vol <= 1061: return 6
        elif vol <= 1115: return 7
        elif vol <= 1169: return 8
        elif vol <= 1313: return 9
        elif vol <= 1601: return 10
        elif vol <= 1655: return 11
        elif vol <= 1919: return 12
        elif vol <= 2045: return 13
        elif vol <= 2189: return 14
        elif vol <= 2405: return 15
        elif vol <= 2621: return 16
        elif vol <= 2837: return 17
        elif vol <= 3053: return 18
        elif vol <= 3269: return 19
        # Baskets 20+: кусочно-линейные сегменты (step разный по сегментам)
        elif vol <= 4622: return 20 + (vol - 3270) // 225   # step≈225 (подтв.: 4622→26)
        elif vol <= 7620: return 26 + (vol - 4351) // 333   # step≈333 (подтв.: 4660→26,6242→31,7118→34,7427→35,7620→35)
        elif vol <  7901: return 35                          # узкий сегмент (подтв.: 7620→35)
        elif vol <= 9017: return 36 + (vol - 7568) // 372   # step≈372 (подтв.: 8537→38, 9017→39)
        elif vol <= 10243: return 39 + (vol - 9017) // 613  # step≈613 (подтв.: 10243→41)
        else:              return 41 + (vol - 10243) // 613  # экстраполяция

    async def fetch_single(self, article: int) -> dict | None:
        """Получает данные одного товара по артикулу (для /add_product)."""
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(headers=self.HEADERS, connector=connector, trust_env=False) as session:
            posts = await self._fetch_batch(session, [article])
            return posts[0] if posts else None

    def reload_cache(self):
        """Принудительно перечитывает кеш с диска (вызывается после обновления)."""
        self._cache_loaded = False
        self._ensure_cache()
        return len(self._cache)


# ============================================================
# ЕДИНСТВЕННЫЙ ЭКЗЕМПЛЯР
# ============================================================
wb_parser = WBParser()


# ============================================================
# БЫСТРЫЙ ТЕСТ
# ============================================================
if __name__ == "__main__":
    import asyncio

    async def test():
        channel = {"channel_id": "test", "wb_categories": ["кроссовки"]}
        posts = await wb_parser.generate_posts(channel, count=3)
        print(f"\nПолучено постов: {len(posts)}")
        for i, p in enumerate(posts, 1):
            print(f"\n--- Пост {i} ---")
            print(p["content"][:200])
            print(f"Картинка: {p['image_url']}")

    asyncio.run(test())
