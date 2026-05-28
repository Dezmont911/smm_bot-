"""
wb_parser.py — Парсер товаров Wildberries для marketplace-каналов

Как работает:
  1. При запросе N постов — выбирает несколько случайных категорий
  2. Из каждой категории берёт 2-3 случайных популярных товара
  3. Форматирует в красивый пост: фото + название + цена + рейтинг + ссылка
  4. Картинка берётся напрямую с WB CDN (без API-ключей)

Подключение канала: добавить в cards/channel.json поле "channel_type": "marketplace"
Можно уточнить категории полем "wb_categories": ["косметика", "украшения"]
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
    logger.warning("aiohttp не установлен — WB-парсер недоступен. Запустите: pip install aiohttp")


# ============================================================
# КАТЕГОРИИ ТОВАРОВ (без 18+ / адалт)
# ============================================================

WB_CATEGORIES = [
    # Красота и здоровье
    "косметика", "парфюм", "уход за лицом", "шампунь", "маска для волос",
    # Украшения и аксессуары
    "бижутерия", "серьги", "кольца", "браслеты", "часы женские",
    # Дом и интерьер
    "для дома", "подушка декоративная", "ваза", "свеча ароматическая", "органайзер",
    # Кухня
    "посуда", "термокружка", "форма для выпечки", "нож кухонный", "сковорода",
    # Одежда и обувь (без интима)
    "футболка", "худи", "кроссовки", "сумка женская", "рюкзак",
    # Спорт
    "фитнес", "коврик для йоги", "гантели", "бутылка для воды", "спортивная одежда",
    # Автотовары
    "автомобильный органайзер", "ароматизатор для авто", "автозарядка",
    # Детские товары
    "игрушки для детей", "развивающие игрушки", "конструктор", "пластилин",
    # Книги и хобби
    "книги", "пазлы", "настольные игры", "раскраски антистресс",
    # Электроника (бытовая)
    "наушники беспроводные", "power bank", "умная колонка", "led лента",
    # Животные
    "товары для кошек", "товары для собак", "аквариум",
    # Текстиль
    "постельное бельё", "полотенце", "плед",
]

# Сколько категорий выбирать в зависимости от количества постов
def _categories_count(n: int) -> int:
    if n <= 3:  return 2
    if n <= 6:  return 3
    if n <= 10: return 4
    return 5


# ============================================================
# ОСНОВНОЙ КЛАСС
# ============================================================

class WBParser:
    """Генерирует посты из случайных категорий Wildberries."""

    SEARCH_URL = "https://search.wb.ru/exactmatch/ru/common/v7/search"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Origin": "https://www.wildberries.ru",
        "Referer": "https://www.wildberries.ru/catalog/0/search.aspx",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
        "Connection": "keep-alive",
    }

    async def generate_posts(self, channel: dict, count: int = 10) -> list[dict]:
        """
        Главный метод. Генерирует count постов из разных категорий.
        channel — карточка канала из JSON.
        """
        if not AIOHTTP_AVAILABLE:
            logger.error("WB-парсер: aiohttp не установлен")
            return []

        # Берём категории из настроек канала или из дефолтного списка
        categories = channel.get("wb_categories", WB_CATEGORIES)

        # Выбираем несколько случайных категорий
        n_cats = _categories_count(count)
        selected_cats = random.sample(categories, min(len(categories), n_cats))
        per_cat = max(1, (count + n_cats - 1) // n_cats)  # округление вверх

        logger.info(
            f"WB-парсер [{channel.get('channel_id', '?')}]: "
            f"запрос {count} постов из {n_cats} категорий: {selected_cats}"
        )

        # Прокси (если задан WB_PROXY_URL в .env — обходит PoW блокировку VPS)
        proxy = cfg.WB_PROXY_URL or None
        if proxy:
            logger.debug(f"WB-парсер: используем прокси {proxy.split('@')[-1]}")

        # Создаём одну сессию на всё — WB видит цепочку запросов с cookies
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(headers=self.HEADERS, connector=connector) as session:
            # Прогреваем сессию — получаем cookies с главной страницы
            try:
                await session.get(
                    "https://www.wildberries.ru/",
                    proxy=proxy,
                    timeout=aiohttp.ClientTimeout(total=10),
                )
                await asyncio.sleep(random.uniform(1.0, 2.0))
                logger.debug("WB-парсер: сессия прогрета (cookies получены)")
            except Exception as e:
                logger.warning(f"WB-парсер: не удалось прогреть сессию: {e}")

            # Запрашиваем категории последовательно
            posts = []
            for cat in selected_cats:
                try:
                    cat_posts = await self._fetch_category_posts(cat, per_cat + 1, session, proxy)
                    posts.extend(cat_posts)
                except Exception as e:
                    logger.error(f"WB-парсер: ошибка для категории '{cat}': {e}")

        # Перемешиваем и обрезаем до нужного количества
        random.shuffle(posts)
        final = posts[:count]

        logger.info(f"WB-парсер: собрано {len(final)} постов из {count} запрошенных")
        return final

    async def _fetch_category_posts(
        self, category: str, limit: int, session: "aiohttp.ClientSession",
        proxy: str | None = None,
    ) -> list[dict]:
        """Берёт товары по поисковому запросу WB. Ретрай при 429."""
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

        # Пауза между запросами к разным категориям
        await asyncio.sleep(random.uniform(2.0, 4.0))

        for attempt in range(3):
            try:
                async with session.get(
                    self.SEARCH_URL, params=params,
                    proxy=proxy,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status == 429:
                        wait = 8 * (attempt + 1)  # 8с, 16с, 24с
                        logger.warning(f"WB search '{category}' → 429, жду {wait}с (попытка {attempt+1}/3)")
                        await asyncio.sleep(wait)
                        continue
                    if resp.status != 200:
                        logger.warning(f"WB search '{category}' → HTTP {resp.status}")
                        return []
                    data = await resp.json(content_type=None)

                products = data.get("data", {}).get("products", [])
                if not products:
                    logger.warning(f"WB search '{category}': нет результатов")
                    return []

                pool = random.sample(products, min(limit * 3, len(products)))
                posts = []
                for product in pool:
                    if len(posts) >= limit:
                        break
                    post = self._format_post(product, category)
                    if post:
                        posts.append(post)

                logger.debug(f"WB search '{category}': найдено {len(posts)} товаров")
                return posts

            except asyncio.TimeoutError:
                logger.warning(f"WB search '{category}': таймаут (попытка {attempt+1}/3)")
                await asyncio.sleep(5)
                continue
            except Exception as e:
                logger.error(f"WB search '{category}': {e}")
                return []

        logger.error(f"WB search '{category}': все попытки исчерпаны")
        return []

    def _format_post(self, product: dict, category: str) -> dict | None:
        """Форматирует данные товара в пост для Telegram."""
        try:
            article = product.get("id")
            if not article:
                return None

            name = product.get("name", "Товар без названия")
            brand = product.get("brand", "")
            rating = product.get("reviewRating", 0.0)
            feedbacks = product.get("feedbacks", 0)

            # ---- Цена ----
            # WB возвращает цену в копейках
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
                return None  # товар без цены — пропускаем

            # ---- Скидка ----
            discount_line = ""
            if original_price > price and original_price > 0:
                pct = round((1 - price / original_price) * 100)
                if pct >= 15:  # показываем только значимые скидки
                    discount_line = f"🔥 <b>Скидка {pct}%</b> (было {original_price:,} ₽)\n".replace(",", " ")

            # ---- Рейтинг ----
            rating_line = ""
            if rating > 0:
                stars = "⭐" * min(5, round(rating))
                reviews_text = f"{feedbacks:,}".replace(",", " ")
                rating_line = f"{stars} {rating:.1f} · {reviews_text} отзывов\n"

            # ---- Заголовок ----
            if brand:
                title = f"🛍 <b>{brand} — {name}</b>"
            else:
                title = f"🛍 <b>{name}</b>"

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

            # ---- Картинка ----
            image_url = self._get_image_url(int(article))

            return {
                "content": text,
                "image_url": image_url,
                "parse_mode": "HTML",
                "source": "wb_parser",
                "wb_article": str(article),
                "wb_category": category,
            }

        except Exception as e:
            logger.warning(f"WB format error: {e} | product: {product.get('id')}")
            return None

    def _get_image_url(self, article: int) -> str:
        """
        Строит URL первой картинки товара с WB CDN.
        Формула: basket-{NN}.wbbasket.ru/vol{VOL}/part{PART}/{ARTICLE}/images/big/1.webp
        """
        vol = article // 100000
        part = article // 1000
        basket = self._get_basket(vol)
        return (
            f"https://basket-{basket:02d}.wbbasket.ru"
            f"/vol{vol}/part{part}/{article}/images/big/1.webp"
        )

    def _get_basket(self, vol: int) -> int:
        """
        Определяет номер CDN-корзины по vol.
        Таблица актуальна для WB RU (2024-2025).
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
        else:             return 20

    async def fetch_single(self, article: int) -> dict | None:
        """
        Получает данные одного товара по артикулу.
        Используется для команды /add_product.
        """
        if not AIOHTTP_AVAILABLE:
            return None

        url = f"https://card.wb.ru/cards/v2/detail?nm={article}"
        try:
            async with aiohttp.ClientSession(headers=self.HEADERS) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json(content_type=None)

            products = data.get("data", {}).get("products", [])
            if not products:
                return None

            post = self._format_post(products[0], "manual")
            return post

        except Exception as e:
            logger.error(f"WB fetch_single {article}: {e}")
            return None


# ============================================================
# ЕДИНСТВЕННЫЙ ЭКЗЕМПЛЯР
# ============================================================
wb_parser = WBParser()
