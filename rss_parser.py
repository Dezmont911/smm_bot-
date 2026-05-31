"""
rss_parser.py — Парсинг RSS-лент для получения инфоповодов

Что делает:
  1. Читает список RSS-лент из карточки канала
  2. Парсит каждую ленту и извлекает статьи
  3. Ищет картинки в статье (3 способа — от простого к сложному)
  4. Оценивает каждую статью по новизне и релевантности (скоринг)
  5. Возвращает топ-N лучших статей для генерации постов

Источники картинок (в порядке приоритета):
  1. media:content или enclosure в самом RSS → самый надёжный
  2. og:image в HTML статьи → почти всегда есть
  3. Первая <img> в тексте статьи → запасной вариант

Использование:
    from rss_parser import rss
    articles = await rss.fetch_for_channel(channel_card, limit=5)
    # articles = [{"title": "...", "summary": "...", "image_url": "...", ...}]
"""

import re
import time
import asyncio
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse

import feedparser
import aiohttp
from bs4 import BeautifulSoup
from loguru import logger


# Таймаут на загрузку одной RSS-ленты (секунды)
RSS_TIMEOUT = 15

# Максимальный возраст статьи для включения в подборку (дни)
MAX_AGE_DAYS = 3

# User-Agent чтобы сайты не блокировали наши запросы
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


class RSSParser:
    """Парсер RSS-лент с извлечением картинок и скорингом статей."""

    # --------------------------------------------------------
    # Основной метод — получить статьи для канала
    # --------------------------------------------------------

    async def fetch_for_channel(
        self,
        channel: dict,
        limit: int = 5,
    ) -> list[dict]:
        """
        Получает топ-N статей для канала из всех его RSS-источников.

        Аргументы:
            channel — карточка канала (словарь из JSON)
            limit   — сколько лучших статей вернуть

        Возвращает список словарей:
            {
                "title":     "Заголовок статьи",
                "summary":   "Краткое описание",
                "link":      "https://...",
                "image_url": "https://..." или None,
                "published": datetime,
                "source":    "rbc.ru",
                "score":     8.5,
            }
        """
        rss_sources = channel.get("rss_sources", [])
        if not rss_sources:
            logger.warning(f"Нет RSS-источников для канала {channel['channel_id']}")
            return []

        # Парсим все ленты параллельно
        tasks = [self._fetch_feed(url) for url in rss_sources]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Собираем все статьи в один список
        all_articles = []
        for url, result in zip(rss_sources, results):
            if isinstance(result, Exception):
                logger.error(f"Ошибка RSS [{url}]: {result}")
                continue
            all_articles.extend(result)

        if not all_articles:
            logger.warning(f"Не удалось получить статьи для {channel['channel_id']}")
            return []

        # Убираем дубликаты по URL
        seen_urls = set()
        unique_articles = []
        for article in all_articles:
            if article["link"] not in seen_urls:
                seen_urls.add(article["link"])
                unique_articles.append(article)

        # Скоринг — сортируем по качеству
        scored = self._score_articles(unique_articles, channel)

        logger.info(
            f"RSS для {channel['channel_id']}: "
            f"найдено {len(unique_articles)} статей, "
            f"возвращаю топ-{min(limit, len(scored))}"
        )

        return scored[:limit]

    # --------------------------------------------------------
    # Парсинг одной RSS-ленты
    # --------------------------------------------------------

    async def _fetch_feed(self, url: str) -> list[dict]:
        """
        Парсит одну RSS-ленту и возвращает список статей.
        feedparser работает синхронно, запускаем в отдельном потоке.
        """
        logger.debug(f"Парсю RSS: {url}")

        loop = asyncio.get_event_loop()
        try:
            # feedparser синхронный — запускаем чтобы не блокировать
            feed = await loop.run_in_executor(
                None,
                lambda: feedparser.parse(url, request_headers=HEADERS)
            )
        except Exception as e:
            logger.error(f"Ошибка парсинга RSS {url}: {e}")
            return []

        if not feed.entries:
            logger.warning(f"Пустая лента (0 записей): {url} | bozo={feed.bozo}")
            return []

        if feed.bozo:
            logger.debug(f"Лента с XML-предупреждением (но записи есть): {url}")

        articles = []
        source_domain = urlparse(url).netloc.replace("www.", "")

        for entry in feed.entries:
            article = self._parse_entry(entry, source_domain)
            if article:
                articles.append(article)

        logger.debug(f"Получено {len(articles)} статей из {source_domain}")
        return articles

    def _parse_entry(self, entry, source: str) -> dict | None:
        """
        Разбирает одну запись из RSS и возвращает словарь с данными.
        Возвращает None если статья слишком старая.
        """
        # Заголовок
        title = getattr(entry, "title", "").strip()
        if not title:
            return None

        # Фильтруем мусорные записи — служебные сообщения Reddit и пустые заглушки
        _JUNK_PHRASES = [
            "this post contains content not supported",
            "content not supported on",
            "reddit video",
            "[removed]",
            "[deleted]",
            "&#x200b;",  # zero-width space — типичный мусор Reddit
        ]
        if any(p in title.lower() for p in _JUNK_PHRASES):
            return None

        # Ссылка
        link = getattr(entry, "link", "").strip()
        if not link:
            return None

        # Пропускаем записи которые ведут на Reddit — это пользовательские посты,
        # не профессиональный контент. Для игровых каналов такие темы дают
        # нерелевантные картинки и "сырой" Reddit-стиль в тексте.
        if "reddit.com/r/" in link or "redd.it/" in link:
            return None

        # Краткое описание — убираем HTML теги
        summary_raw = (
            getattr(entry, "summary", "")
            or getattr(entry, "description", "")
        )
        summary = self._clean_html(summary_raw)[:500]  # первые 500 символов

        # Дата публикации
        published = self._parse_date(entry)

        # Пропускаем старые статьи
        if published:
            age = datetime.now(timezone.utc) - published
            if age > timedelta(days=MAX_AGE_DAYS):
                return None

        # Картинка — ищем тремя способами
        image_url = (
            self._get_image_from_media(entry)     # способ 1: media:content
            or self._get_image_from_enclosure(entry)  # способ 2: enclosure
            or self._get_image_from_summary(summary_raw)  # способ 3: <img> в HTML
        )

        return {
            "title": title,
            "summary": summary,
            "link": link,
            "image_url": image_url,
            "published": published,
            "source": source,
            "score": 0.0,  # заполняется в _score_articles
        }

    # --------------------------------------------------------
    # Извлечение картинок — 3 способа
    # --------------------------------------------------------

    def _get_image_from_media(self, entry) -> str | None:
        """
        Способ 1: ищем в тегах media:content и media:thumbnail.
        Это самый надёжный способ — картинка явно указана в RSS.
        """
        # media:content (например, РБК, VC.ru)
        media_content = getattr(entry, "media_content", [])
        for media in media_content:
            url = media.get("url", "")
            if url and self._is_image_url(url):
                return url

        # media:thumbnail (YouTube, некоторые новостные)
        media_thumbnail = getattr(entry, "media_thumbnail", [])
        for thumb in media_thumbnail:
            url = thumb.get("url", "")
            if url and self._is_image_url(url):
                return url

        return None

    def _get_image_from_enclosure(self, entry) -> str | None:
        """
        Способ 2: ищем в enclosure (вложение в RSS, стандарт RSS 2.0).
        Часто используется подкастами и некоторыми новостными сайтами.
        """
        enclosures = getattr(entry, "enclosures", [])
        for enc in enclosures:
            enc_type = enc.get("type", "")
            url = enc.get("url", "")
            if url and enc_type.startswith("image/"):
                return url
        return None

    def _get_image_from_summary(self, html: str) -> str | None:
        """
        Способ 3: ищем первый <img> тег в HTML описании статьи.
        Запасной вариант — работает хуже всего, но лучше чем ничего.
        """
        if not html:
            return None
        try:
            soup = BeautifulSoup(html, "lxml")
            img = soup.find("img")
            if img:
                src = img.get("src", "")
                if src and self._is_image_url(src):
                    return src
        except Exception:
            pass
        return None

    # --------------------------------------------------------
    # Скоринг статей (из handbook: новизна + релевантность)
    # --------------------------------------------------------

    def _score_articles(self, articles: list[dict], channel: dict) -> list[dict]:
        """
        Оценивает каждую статью по двум критериям:
          - Новизна: чем свежее — тем выше балл (max 5)
          - Релевантность: совпадение с темой канала (max 5)

        Итоговый score = новизна + релевантность (max 10).
        Статьи с картинкой получают +1 бонус.
        """
        now = datetime.now(timezone.utc)
        topic_keywords = self._extract_keywords(channel.get("topic", ""))

        for article in articles:
            score = 0.0

            # --- Скор по новизне (0–5 баллов) ---
            if article["published"]:
                age_hours = (now - article["published"]).total_seconds() / 3600
                if age_hours < 6:
                    score += 5.0
                elif age_hours < 12:
                    score += 4.0
                elif age_hours < 24:
                    score += 3.0
                elif age_hours < 48:
                    score += 2.0
                else:
                    score += 1.0
            else:
                score += 1.0  # нет даты — считаем старой

            # --- Скор по релевантности (0–5 баллов) ---
            article_text = f"{article['title']} {article['summary']}".lower()
            matches = sum(1 for kw in topic_keywords if kw in article_text)
            relevance = min(matches / max(len(topic_keywords), 1) * 5, 5.0)
            score += relevance

            # --- Бонус за картинку (+1) ---
            if article["image_url"]:
                score += 1.0

            article["score"] = round(score, 2)

        # Сортируем по убыванию score
        return sorted(articles, key=lambda x: x["score"], reverse=True)

    # --------------------------------------------------------
    # Вспомогательные методы
    # --------------------------------------------------------

    def _clean_html(self, html: str) -> str:
        """Убирает HTML теги, оставляет чистый текст."""
        if not html:
            return ""
        try:
            soup = BeautifulSoup(html, "lxml")
            return soup.get_text(separator=" ").strip()
        except Exception:
            # Запасной вариант — простая regex замена
            return re.sub(r"<[^>]+>", " ", html).strip()

    def _parse_date(self, entry) -> datetime | None:
        """Извлекает дату публикации из записи RSS."""
        published_parsed = getattr(entry, "published_parsed", None)
        if published_parsed:
            try:
                ts = time.mktime(published_parsed)
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except Exception:
                pass

        updated_parsed = getattr(entry, "updated_parsed", None)
        if updated_parsed:
            try:
                ts = time.mktime(updated_parsed)
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except Exception:
                pass

        return None

    def _is_image_url(self, url: str) -> bool:
        """Проверяет, похож ли URL на картинку."""
        if not url or not url.startswith("http"):
            return False
        ext = url.lower().split("?")[0].split(".")[-1]
        return ext in {"jpg", "jpeg", "png", "gif", "webp", "svg"}

    def _extract_keywords(self, topic: str) -> list[str]:
        """Извлекает ключевые слова из поля topic карточки канала."""
        # Убираем знаки препинания, разбиваем по запятым и пробелам
        words = re.split(r"[,\s]+", topic.lower())
        # Фильтруем короткие слова (предлоги и т.п.)
        return [w.strip() for w in words if len(w.strip()) > 3]


# ============================================================
# ЕДИНСТВЕННЫЙ ЭКЗЕМПЛЯР
# ============================================================
rss = RSSParser()


# ============================================================
# ТЕСТ — запускается напрямую: python rss_parser.py
# ============================================================
if __name__ == "__main__":
    import json
    from pathlib import Path

    async def test():
        print("📡 Тест RSS парсера\n")

        # Загружаем карточку канала
        with open("channels/example_channel.json", encoding="utf-8") as f:
            channel = json.load(f)

        print(f"📋 Канал: {channel['name']}")
        print(f"📰 RSS-источники: {channel['rss_sources']}\n")

        print("⏳ Парсю RSS-ленты...\n")
        articles = await rss.fetch_for_channel(channel, limit=5)

        if not articles:
            print("❌ Статьи не найдены. Проверь RSS-ссылки в карточке канала.")
            return

        print(f"✅ Найдено топ-{len(articles)} статей:\n")
        print("=" * 60)

        for i, article in enumerate(articles, 1):
            age = ""
            if article["published"]:
                hours_ago = (
                    datetime.now(timezone.utc) - article["published"]
                ).total_seconds() / 3600
                age = f"{hours_ago:.0f}ч назад"

            has_image = "🖼️" if article["image_url"] else "📄"

            print(f"{i}. {has_image} [{article['score']}] {article['title'][:60]}")
            print(f"   Источник: {article['source']} | {age}")
            print(f"   Описание: {article['summary'][:100]}...")
            if article["image_url"]:
                print(f"   Картинка: {article['image_url'][:70]}...")
            print()

        # Сводка
        with_images = sum(1 for a in articles if a["image_url"])
        print("=" * 60)
        print(f"📊 Итого: {len(articles)} статей, из них с картинкой: {with_images}")

    asyncio.run(test())
