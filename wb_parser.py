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

        # Перемешиваем и берём нужное количество (с запасом ×2 для фильтрации)
        random.shuffle(pool)
        return pool[:min(count * 2, len(pool))]

    async def generate_posts(self, channel: dict, count: int = 10) -> list[dict]:
        """
        Главный метод. Генерирует count постов.
        Использует кеш артикулов + card.wb.ru (без 429).
        """
        article_ids = self._pick_articles(channel, count)

        if not article_ids:
            logger.error("WB-парсер: кеш пустой. Обнови cards/wb_ids_cache.json")
            return []

        logger.info(
            f"WB-парсер [{channel.get('channel_id', '?')}]: "
            f"запрос {count} постов из {len(article_ids)} артикулов"
        )

        posts = await self._fetch_posts(article_ids, count)
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
        nm_param = ";".join(str(a) for a in article_ids)
        params = {
            "appType": "1",
            "curr": "rub",
            "dest": "-1257786",
            "nm": nm_param,
        }

        for attempt in range(3):
            try:
                async with session.get(
                    self.CARD_API,
                    params=params,
                    proxy=proxy,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 429:
                        wait = 10 * (attempt + 1)
                        logger.warning(f"WB card API 429, жду {wait}с")
                        await asyncio.sleep(wait)
                        continue
                    if resp.status != 200:
                        logger.warning(f"WB card API → HTTP {resp.status}")
                        return []

                    data = await resp.json(content_type=None)

                # v4 API: {"products": [...]}  (v2 было: {"data": {"products": [...]}})
                products = data.get("products") or data.get("data", {}).get("products", [])
                posts = []
                for product in products:
                    post = self._format_post(product)
                    if post:
                        posts.append(post)

                logger.debug(f"WB card API: батч {len(article_ids)} → {len(posts)} постов")
                return posts

            except asyncio.TimeoutError:
                logger.warning(f"WB card API: таймаут (попытка {attempt+1}/3)")
                await asyncio.sleep(3)
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
        elif vol <= 7620: return 26 + (vol - 4622) // 333   # step≈333 (подтв.: 7620→35)
        elif vol <  7901: return 35                          # узкий сегмент (подтв.: 7620→35)
        elif vol <= 9017: return 36 + (vol - 7901) // 372   # step≈372 (подтв.: 7901→36, 9017→39)
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
