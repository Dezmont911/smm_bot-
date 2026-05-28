"""
wb_partner_parser.py — Парсер товаров WB через официальный API

Работает с любого IP (VPS/datacenter), не требует PoW и не блокируется.
В отличие от wb_parser.py использует API-ключ для аутентификации.

─────────────────────────────────────────────────────────────
КАК ПОЛУЧИТЬ API-КЛЮЧ (выбери один из двух вариантов):
─────────────────────────────────────────────────────────────

ВАРИАНТ A — Если ты ПРОДАВЕЦ на WB (seller.wildberries.ru):
  1. seller.wildberries.ru → Главное меню → Настройки (шестерёнка) → Доступ к API
  2. Нажми "Создать новый ключ"
  3. Выбери права: ✅ Контент (Content)  — для получения карточек товаров
                   ✅ Цены и скидки       — для актуальных цен
  4. Скопируй ключ (он длинный, начинается с eyJ...)
  5. Добавь в .env файл на сервере: WB_API_KEY=eyJ...

ВАРИАНТ B — Если ты АФФИЛИАТ / ПАРТНЁР (partners.wb.ru):
  1. partners.wb.ru → войди в кабинет
  2. Найди раздел "API" или "Настройки" → "Доступ к API"
  3. Получи API-токен
  4. Добавь в .env: WB_API_KEY=...
  5. Также добавь: WB_AFFILIATE_ID=твой_партнёрский_код (если есть)

─────────────────────────────────────────────────────────────
ЧТО ПУБЛИКУЕТ БОТ:
─────────────────────────────────────────────────────────────
  - ВАРИАНТ A (продавец): твои собственные товары со WB
  - ВАРИАНТ B (аффилиат): товары со скидками из разных категорий
    + партнёрская ссылка для комиссии

Если WB_API_KEY не задан — автоматически падает на wb_parser.py
(публичный API, может блокироваться с VPS).
"""

import asyncio
import random
from loguru import logger

from config import cfg

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    logger.warning("aiohttp не установлен. Запустите: pip install aiohttp")


# ============================================================
# ENDPOINTS WB API
# ============================================================

# Seller Content API — получение карточек товаров продавца
SELLER_CARDS_URL = "https://content-api.wildberries.ru/content/v2/get/cards/list"

# Seller Prices API — текущие цены и скидки
SELLER_PRICES_URL = "https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter"

# Seller Statistics — топ товаров по продажам (для выбора лучших для постов)
SELLER_STATS_URL = "https://statistics-api.wildberries.ru/api/v1/supplier/reportDetailByPeriod"

# WB Search API — работает С ключом (обходит PoW через авторизацию)
# Тот же endpoint что в wb_parser, но с Authorization header
SEARCH_URL = "https://search.wb.ru/exactmatch/ru/common/v7/search"


# ============================================================
# ГЛАВНЫЙ КЛАСС
# ============================================================

class WBPartnerParser:
    """
    Генерирует посты через официальный WB API.
    Интерфейс совместим с WBParser — content_generator работает без изменений.
    """

    def _api_headers(self) -> dict:
        """Заголовки с авторизацией для WB API."""
        return {
            "Authorization": cfg.WB_API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        }

    def _search_headers(self) -> dict:
        """Заголовки для поискового API (браузерный стиль)."""
        return {
            "Authorization": cfg.WB_API_KEY,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Origin": "https://www.wildberries.ru",
            "Referer": "https://www.wildberries.ru/",
        }

    # --------------------------------------------------------
    # Главный метод (совместим с WBParser)
    # --------------------------------------------------------

    async def generate_posts(self, channel: dict, count: int = 10) -> list[dict]:
        """
        Главный метод. Совместим с WBParser.generate_posts().
        Автоматически выбирает режим: seller API или поиск с ключом.
        """
        # Если ключ не задан — используем публичный парсер
        if not cfg.WB_API_KEY:
            logger.warning("WB_API_KEY не задан — использую публичный wb_parser (может блокироваться)")
            from wb_parser import wb_parser
            return await wb_parser.generate_posts(channel, count)

        if not AIOHTTP_AVAILABLE:
            logger.error("aiohttp не установлен")
            return []

        channel_id = channel.get("channel_id", "?")
        logger.info(f"WB Partner API [{channel_id}]: запрос {count} постов")

        # Определяем режим по настройке канала (или пробуем автоматически)
        mode = channel.get("wb_api_mode", "auto")  # "seller" | "search" | "auto"

        posts = []

        if mode in ("seller", "auto"):
            # Пробуем Seller Content API
            try:
                posts = await self._fetch_seller_posts(channel, count)
            except Exception as e:
                logger.warning(f"WB Seller API недоступен: {e}")
                posts = []

        if not posts and mode in ("search", "auto"):
            # Пробуем поисковый API с авторизацией
            try:
                posts = await self._fetch_search_posts(channel, count)
            except Exception as e:
                logger.warning(f"WB Search API недоступен: {e}")
                posts = []

        if not posts:
            logger.error(f"WB Partner API [{channel_id}]: оба метода не дали результатов")
            return []

        logger.info(f"WB Partner API [{channel_id}]: готово {len(posts)} постов")
        return posts

    # --------------------------------------------------------
    # Режим 1: Seller API — товары продавца
    # --------------------------------------------------------

    async def _fetch_seller_posts(self, channel: dict, count: int) -> list[dict]:
        """
        Получает карточки товаров продавца через Seller Content API.
        Работает только если WB_API_KEY — ключ продавца с правами 'Контент'.
        """
        # Шаг 1: получаем список карточек (без цен)
        cards = await self._get_seller_cards(count * 2)
        if not cards:
            return []

        # Шаг 2: получаем цены для этих карточек
        nm_ids = [c.get("nmID") or c.get("nmId") for c in cards if c.get("nmID") or c.get("nmId")]
        prices_map = await self._get_seller_prices(nm_ids) if nm_ids else {}

        # Шаг 3: форматируем посты
        random.shuffle(cards)
        posts = []
        for card in cards:
            if len(posts) >= count:
                break
            nm_id = card.get("nmID") or card.get("nmId")
            price_info = prices_map.get(nm_id, {})
            post = self._format_seller_post(card, price_info)
            if post:
                posts.append(post)

        return posts

    async def _get_seller_cards(self, limit: int) -> list[dict]:
        """Запрашивает карточки товаров продавца."""
        payload = {
            "settings": {
                "cursor": {
                    "limit": min(limit, 100)
                },
                "filter": {
                    "withPhoto": 1  # только товары с фото
                }
            }
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                SELLER_CARDS_URL,
                headers=self._api_headers(),
                json=payload,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 401:
                    logger.error("WB Seller API: неверный API-ключ (401 Unauthorized)")
                    logger.info("💡 Проверь: ключ должен быть из seller.wildberries.ru с правами 'Контент'")
                    return []
                if resp.status == 403:
                    logger.error("WB Seller API: нет прав на Контент (403 Forbidden)")
                    logger.info("💡 Добавь право 'Контент' при создании ключа")
                    return []
                if resp.status != 200:
                    logger.error(f"WB Seller API: HTTP {resp.status}")
                    return []
                data = await resp.json(content_type=None)

        cards = data.get("data", {}).get("cards", [])
        logger.debug(f"WB Seller Cards: получено {len(cards)} карточек")
        return cards

    async def _get_seller_prices(self, nm_ids: list[int]) -> dict[int, dict]:
        """
        Получает текущие цены для списка артикулов.
        Возвращает {nm_id: {price, discount_price, discount_pct}}.
        """
        if not nm_ids:
            return {}

        # WB Prices API поддерживает до 1000 артикулов за запрос
        params = {
            "limit": len(nm_ids),
            "offset": 0,
            "filterNmIds": ",".join(str(n) for n in nm_ids[:1000]),
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    SELLER_PRICES_URL,
                    headers=self._api_headers(),
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        logger.debug(f"WB Prices API: HTTP {resp.status} — цены недоступны")
                        return {}
                    data = await resp.json(content_type=None)

            result = {}
            goods = data.get("data", {}).get("listGoods", [])
            for item in goods:
                nm_id = item.get("nmID") or item.get("nmId")
                if not nm_id:
                    continue
                sizes = item.get("sizes", [{}])
                if sizes:
                    size = sizes[0]
                    price = size.get("price", 0)
                    disc_price = size.get("discountedPrice", price)
                    disc_pct = item.get("discount", 0)
                    result[nm_id] = {
                        "price": price,
                        "discounted_price": disc_price,
                        "discount_pct": disc_pct,
                    }

            logger.debug(f"WB Prices API: получены цены для {len(result)} товаров")
            return result

        except Exception as e:
            logger.warning(f"WB Prices API недоступен: {e}")
            return {}

    def _format_seller_post(self, card: dict, price_info: dict) -> dict | None:
        """Форматирует карточку продавца в пост для Telegram."""
        try:
            nm_id = card.get("nmID") or card.get("nmId")
            if not nm_id:
                return None

            title = card.get("title", "Товар без названия")
            brand = card.get("brand", "")
            # Артикул продавца (vendorCode) или WB артикул
            vendor_code = card.get("vendorCode", "")

            # ---- Цена ----
            price = price_info.get("price", 0)
            disc_price = price_info.get("discounted_price", price)
            disc_pct = price_info.get("discount_pct", 0)

            price_line = ""
            if disc_price and disc_price > 0:
                if disc_pct >= 15 and price > disc_price:
                    price_line = (
                        f"🔥 <b>Скидка {disc_pct}%</b> "
                        f"(было {price:,} ₽)\n"
                        f"💰 <b>{disc_price:,} ₽</b>\n"
                    ).replace(",", " ")
                else:
                    price_line = f"💰 <b>{disc_price:,} ₽</b>\n".replace(",", " ")
            elif price > 0:
                price_line = f"💰 <b>{price:,} ₽</b>\n".replace(",", " ")

            # ---- Заголовок ----
            if brand:
                title_text = f"🛍 <b>{brand} — {title}</b>"
            else:
                title_text = f"🛍 <b>{title}</b>"

            # ---- Описание (первые 150 символов) ----
            description = card.get("description", "")
            desc_line = ""
            if description and len(description) > 20:
                short_desc = description[:150].rstrip()
                if len(description) > 150:
                    short_desc += "..."
                desc_line = f"\n{short_desc}\n"

            # ---- Партнёрская ссылка ----
            wb_link = f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx"
            affiliate_id = getattr(cfg, "WB_AFFILIATE_ID", "") or ""
            if affiliate_id:
                wb_link += f"?rnext={affiliate_id}"

            # ---- Текст поста ----
            text = (
                f"{title_text}\n"
                f"{desc_line}\n"
                f"{price_line}"
                f"📦 Арт. {nm_id}\n"
                f'🔗 <a href="{wb_link}">Смотреть на Wildberries</a>'
            )

            # ---- Картинка ----
            image_url = self._extract_image_url(card, int(nm_id))

            return {
                "content": text,
                "image_url": image_url,
                "parse_mode": "HTML",
                "source": "wb_partner",
                "wb_article": str(nm_id),
                "wb_category": card.get("subjectName", ""),
            }

        except Exception as e:
            logger.warning(f"WB Partner format error: {e} | nmID={card.get('nmID')}")
            return None

    # --------------------------------------------------------
    # Режим 2: Search API с авторизацией
    # --------------------------------------------------------

    # Категории по умолчанию (если не заданы в карточке канала)
    DEFAULT_CATEGORIES = [
        "кроссовки", "косметика", "наушники беспроводные",
        "сумка женская", "термокружка", "платье женское", "настольные игры"
    ]

    async def _fetch_search_posts(self, channel: dict, count: int) -> list[dict]:
        """
        Получает товары через поисковый API WB с авторизацией.
        Авторизация обходит PoW-блокировку для VPS.
        """
        categories = channel.get("wb_categories", self.DEFAULT_CATEGORIES)
        n_cats = max(1, min(count, 3))  # не больше 3 категорий за раз
        selected_cats = random.sample(categories, min(len(categories), n_cats))
        per_cat = max(1, (count + n_cats - 1) // n_cats)

        logger.info(
            f"WB Search+Auth: {count} постов из {n_cats} категорий: {selected_cats}"
        )

        affiliate_id = getattr(cfg, "WB_AFFILIATE_ID", "") or ""

        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            posts = []
            for cat in selected_cats:
                try:
                    cat_posts = await self._search_category(
                        cat, per_cat + 1, session, affiliate_id
                    )
                    posts.extend(cat_posts)
                    # Небольшая пауза между запросами
                    await asyncio.sleep(random.uniform(1.0, 2.5))
                except Exception as e:
                    logger.error(f"WB Search '{cat}': {e}")

        random.shuffle(posts)
        return posts[:count]

    async def _search_category(
        self,
        category: str,
        limit: int,
        session: "aiohttp.ClientSession",
        affiliate_id: str = "",
    ) -> list[dict]:
        """Ищет товары по категории через WB Search API."""
        params = {
            "appType": "1",
            "curr": "rub",
            "dest": "-1257786",
            "query": category,
            "resultset": "catalog",
            "sort": "popular",
            "spp": "30",
            "page": str(random.randint(1, 3)),
        }

        for attempt in range(3):
            try:
                async with session.get(
                    SEARCH_URL,
                    headers=self._search_headers(),
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 429:
                        wait = 8 * (attempt + 1)
                        logger.warning(f"WB Search+Auth '{category}' → 429, жду {wait}с")
                        await asyncio.sleep(wait)
                        continue
                    if resp.status != 200:
                        logger.warning(f"WB Search+Auth '{category}' → HTTP {resp.status}")
                        return []
                    data = await resp.json(content_type=None)

                products = data.get("data", {}).get("products", [])
                if not products:
                    return []

                pool = random.sample(products, min(limit * 3, len(products)))
                posts = []
                for product in pool:
                    if len(posts) >= limit:
                        break
                    post = self._format_search_post(product, category, affiliate_id)
                    if post:
                        posts.append(post)

                logger.debug(f"WB Search+Auth '{category}': {len(posts)} товаров")
                return posts

            except asyncio.TimeoutError:
                logger.warning(f"WB Search+Auth '{category}': таймаут (попытка {attempt+1}/3)")
                await asyncio.sleep(5)
                continue
            except Exception as e:
                logger.error(f"WB Search+Auth '{category}': {e}")
                return []

        return []

    def _format_search_post(
        self, product: dict, category: str, affiliate_id: str = ""
    ) -> dict | None:
        """Форматирует товар из поискового API в пост (с партнёрской ссылкой)."""
        try:
            article = product.get("id")
            if not article:
                return None

            name = product.get("name", "Товар без названия")
            brand = product.get("brand", "")
            rating = product.get("reviewRating", 0.0)
            feedbacks = product.get("feedbacks", 0)

            # Цена (WB возвращает в копейках)
            sizes = product.get("sizes", [])
            price = 0
            original_price = 0
            for size in sizes:
                price_data = size.get("price", {})
                p = price_data.get("product", 0)
                b = price_data.get("basic", 0)
                if p > 0:
                    price = p // 100
                    original_price = b // 100
                    break

            if price == 0:
                return None

            # Скидка
            discount_line = ""
            if original_price > price and original_price > 0:
                pct = round((1 - price / original_price) * 100)
                if pct >= 15:
                    discount_line = (
                        f"🔥 <b>Скидка {pct}%</b> "
                        f"(было {original_price:,} ₽)\n"
                    ).replace(",", " ")

            # Рейтинг
            rating_line = ""
            if rating > 0:
                stars = "⭐" * min(5, round(rating))
                reviews_text = f"{feedbacks:,}".replace(",", " ")
                rating_line = f"{stars} {rating:.1f} · {reviews_text} отзывов\n"

            # Заголовок
            if brand:
                title = f"🛍 <b>{brand} — {name}</b>"
            else:
                title = f"🛍 <b>{name}</b>"

            # Партнёрская ссылка
            wb_link = f"https://www.wildberries.ru/catalog/{article}/detail.aspx"
            if affiliate_id:
                wb_link += f"?rnext={affiliate_id}"

            text = (
                f"{title}\n\n"
                f"{discount_line}"
                f"💰 <b>{price:,} ₽</b>\n".replace(",", " ") +
                rating_line +
                f"\n📦 Арт. {article}\n"
                f'🔗 <a href="{wb_link}">Смотреть на Wildberries</a>'
            )

            image_url = self._get_image_url(int(article))

            return {
                "content": text,
                "image_url": image_url,
                "parse_mode": "HTML",
                "source": "wb_partner",
                "wb_article": str(article),
                "wb_category": category,
            }

        except Exception as e:
            logger.warning(f"WB Partner search format error: {e}")
            return None

    # --------------------------------------------------------
    # Работа с изображениями
    # --------------------------------------------------------

    def _extract_image_url(self, card: dict, article: int) -> str:
        """
        Получает URL картинки.
        Сначала из массива photos карточки, потом через CDN-формулу.
        """
        photos = card.get("photos", [])
        if photos:
            # WB возвращает список URL или объектов с полем tm/big
            first = photos[0]
            if isinstance(first, str):
                return first
            if isinstance(first, dict):
                # Пробуем разные форматы ответа WB API
                for key in ("tm", "big", "c246x328", "1"):
                    if url := first.get(key):
                        return url

        # Fallback: строим URL по формуле CDN
        return self._get_image_url(article)

    def _get_image_url(self, article: int) -> str:
        """Строит URL картинки через WB CDN (формула без API)."""
        vol = article // 100000
        part = article // 1000
        basket = self._get_basket(vol)
        return (
            f"https://basket-{basket:02d}.wbbasket.ru"
            f"/vol{vol}/part{part}/{article}/images/big/1.webp"
        )

    def _get_basket(self, vol: int) -> int:
        """Номер CDN-сервера по vol (таблица WB RU 2024-2025)."""
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
        else:             return 20

    # --------------------------------------------------------
    # Дополнительно: получить один товар по артикулу
    # --------------------------------------------------------

    async def fetch_single(self, article: int) -> dict | None:
        """
        Получает данные одного товара по артикулу.
        Используется для команды /add_product.
        """
        if not cfg.WB_API_KEY:
            from wb_parser import wb_parser
            return await wb_parser.fetch_single(article)

        url = f"https://card.wb.ru/cards/v2/detail?nm={article}"
        headers = self._api_headers()
        headers.pop("Content-Type", None)  # GET запрос

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json(content_type=None)

            products = data.get("data", {}).get("products", [])
            if not products:
                return None

            # Переиспользуем форматтер поискового режима
            return self._format_search_post(products[0], "manual")

        except Exception as e:
            logger.error(f"WB Partner fetch_single {article}: {e}")
            return None


# ============================================================
# ЕДИНСТВЕННЫЙ ЭКЗЕМПЛЯР
# ============================================================
wb_partner_parser = WBPartnerParser()


# ============================================================
# ТЕСТ — python wb_partner_parser.py
# ============================================================
if __name__ == "__main__":
    import asyncio
    import os

    async def test():
        api_key = os.getenv("WB_API_KEY", "")
        if not api_key:
            print("❌ WB_API_KEY не задан в .env")
            print("   Добавь: WB_API_KEY=eyJ...")
            return

        print("🔑 API ключ найден, тестирую...")
        print("=" * 60)

        channel = {
            "channel_id": "@test",
            "wb_categories": ["косметика", "бижутерия"],
        }

        posts = await wb_partner_parser.generate_posts(channel, count=3)

        if posts:
            print(f"✅ Получено {len(posts)} постов:\n")
            for i, post in enumerate(posts, 1):
                print(f"Пост {i}:")
                print(post["content"][:200])
                print(f"Картинка: {post.get('image_url', '—')[:60]}")
                print("—" * 40)
        else:
            print("❌ Постов не получено")
            print("   Проверь тип ключа (seller/partner) и права доступа")

    asyncio.run(test())
