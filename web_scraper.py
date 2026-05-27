"""
web_scraper.py — Умный подбор RSS-источников для канала

Когда RSS-ленты канала дают мало тем, этот модуль:
  1. Просит Claude подобрать Reddit-сабреддиты и Medium-теги под тему канала
  2. Конвертирует их в стандартные RSS-URL
  3. Проверяет что ленты живые через feedparser
  4. Сохраняет в карточку канала как web_sources
  5. Возвращает статьи из этих лент

Reddit и Medium всегда имеют RSS — никакого скрапинга, никаких 403/404.

Использование:
    from web_scraper import scraper
    topics = await scraper.scrape_for_channel(channel, limit=5)
"""

import asyncio
import json
from pathlib import Path

import anthropic
import feedparser
from loguru import logger

from config import cfg


# ============================================================
# Настройки
# ============================================================

CHANNELS_DIR = Path(__file__).parent / "channels"


class WebScraper:
    """Находит Reddit/Medium RSS-ленты под тему канала и читает их."""

    def __init__(self):
        self._claude = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)

    # ----------------------------------------------------------
    # Публичный метод — вызывается из content_generator
    # ----------------------------------------------------------

    async def scrape_for_channel(
        self,
        channel: dict,
        limit: int = 5,
    ) -> list[dict]:
        """
        Возвращает список тем для канала из Reddit/Medium RSS.

        Если web_sources нет — Claude подбирает их один раз и сохраняет.
        Следующие вызовы используют уже сохранённые источники.

        Возвращает список словарей:
            {"topic": "...", "source": "url", "image_url": None}
        """
        channel_id  = channel["channel_id"]
        web_sources = channel.get("web_sources", [])

        if not web_sources:
            logger.info(
                f"web_sources нет для {channel_id} — "
                f"подбираю Reddit/Medium RSS через Claude"
            )
            web_sources = await self._discover_and_save(channel)

        if not web_sources:
            logger.warning(f"Не удалось подобрать web_sources для {channel_id}")
            return []

        # Читаем все ленты и собираем статьи
        all_articles: list[dict] = []
        for url in web_sources:
            articles = await self._fetch_feed(url)
            all_articles.extend(articles)
            if articles:
                logger.debug(f"web_sources [{channel_id}]: {url} → {len(articles)} статей")

        logger.info(
            f"web_scraper [{channel_id}]: "
            f"{len(web_sources)} лент → {len(all_articles)} статей"
        )
        return all_articles[:limit]

    # ----------------------------------------------------------
    # Подбор источников через Claude
    # ----------------------------------------------------------

    async def _discover_and_save(self, channel: dict) -> list[str]:
        """
        Claude предлагает Reddit-сабреддиты и Medium-теги под тему.
        Конвертирует в RSS-URL, проверяет что живые, сохраняет.
        """
        topic = channel.get("topic", "")
        name  = channel.get("name", "")

        prompt = f"""Подбери 3-5 Reddit сабреддитов и/или Medium тегов для Telegram-канала.

Канал: "{name}"
Тема: {topic}

Требования:
- Reddit: популярные сабреддиты с активным постингом (r/название)
- Medium: теги где публикуют статьи по теме (tag/название)
- Только реально существующие, с большой аудиторией

Верни ТОЛЬКО список в формате, по одному на строке:
reddit:название_сабреддита
medium:название_тега

Пример:
reddit:Minecraft
reddit:feedthebeast
medium:gaming"""

        try:
            message = self._claude.messages.create(
                model=cfg.CLAUDE_MODEL,
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            raw   = message.content[0].text.strip()
            lines = [l.strip() for l in raw.splitlines() if l.strip()]

            # Конвертируем в RSS URL
            candidates = []
            for line in lines:
                url = self._to_rss_url(line)
                if url:
                    candidates.append(url)

            logger.info(
                f"Claude предложил для {channel['channel_id']}: {candidates}"
            )

            # Проверяем что ленты живые
            working = await self._verify_feeds(candidates)
            if not working:
                return []

            # Сохраняем в карточку
            self._save_to_card(channel, working)
            return working

        except Exception as e:
            logger.error(f"Ошибка подбора web_sources: {e}")
            return []

    @staticmethod
    def _to_rss_url(line: str) -> str | None:
        """Конвертирует 'reddit:Minecraft' → RSS URL."""
        line = line.lower().strip()
        if line.startswith("reddit:"):
            sub = line.split(":", 1)[1].strip()
            return f"https://www.reddit.com/r/{sub}/.rss"
        if line.startswith("medium:"):
            tag = line.split(":", 1)[1].strip()
            return f"https://medium.com/feed/tag/{tag}"
        return None

    async def _verify_feeds(self, urls: list[str]) -> list[str]:
        """Проверяет каждый URL через feedparser — возвращает только рабочие."""
        async def check(url: str) -> str | None:
            try:
                loop = asyncio.get_event_loop()
                feed = await loop.run_in_executor(
                    None, lambda: feedparser.parse(url)
                )
                if feed.entries:
                    return url
                return None
            except Exception:
                return None

        results = await asyncio.gather(*[check(u) for u in urls])
        working = [u for u in results if u]
        logger.info(
            f"Проверка лент: {len(urls)} кандидатов → {len(working)} рабочих"
        )
        return working

    def _save_to_card(self, channel: dict, urls: list[str]) -> None:
        """Сохраняет web_sources в JSON-карточку канала."""
        channel_id = channel["channel_id"].lstrip("@")
        card_path  = CHANNELS_DIR / f"{channel_id}.json"

        if not card_path.exists():
            logger.warning(f"Карточка {card_path.name} не найдена")
            return

        try:
            with open(card_path, encoding="utf-8") as f:
                data = json.load(f)

            existing = data.get("web_sources", [])
            combined = existing + [u for u in urls if u not in existing]
            data["web_sources"] = combined

            with open(card_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            logger.info(f"web_sources → {card_path.name}: {combined}")
        except Exception as e:
            logger.error(f"Ошибка сохранения web_sources: {e}")

    # ----------------------------------------------------------
    # Чтение RSS-ленты
    # ----------------------------------------------------------

    @staticmethod
    def _strip_html(text: str) -> str:
        """Убирает HTML-теги из текста (Reddit кладёт в summary таблицы)."""
        import re
        clean = re.sub(r"<[^>]+>", " ", text)
        clean = re.sub(r"\s+", " ", clean).strip()
        return clean

    async def _fetch_feed(self, url: str) -> list[dict]:
        """Читает RSS-ленту и возвращает список тем."""
        try:
            loop = asyncio.get_event_loop()
            feed = await loop.run_in_executor(
                None, lambda: feedparser.parse(url)
            )
            articles = []
            for entry in feed.entries[:8]:
                title   = entry.get("title", "").strip()
                # Убираем HTML-теги из summary (Reddit кладёт туда таблицы)
                raw_summary = entry.get("summary", "")
                summary = self._strip_html(raw_summary)[:200].strip()
                if not title or len(title) < 5:
                    continue
                topic = f"{title}. {summary}" if summary else title
                articles.append({
                    "topic":     topic,
                    "source":    url,
                    "image_url": None,
                })
            return articles
        except Exception as e:
            logger.warning(f"Ошибка чтения ленты {url}: {e}")
            return []


# ============================================================
# Единственный экземпляр
# ============================================================
scraper = WebScraper()


# ============================================================
# ТЕСТ — python web_scraper.py
# ============================================================
if __name__ == "__main__":
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    async def test():
        print("=== Тест web_scraper (Reddit/Medium RSS) ===\n")

        test_cases = [
            {
                "channel_id": "@test_minecraft",
                "name": "Майнкрафт Новости",
                "topic": "майнкрафт, игры, моды, обновления",
                "audience": "игроки в майнкрафт",
            },
            {
                "channel_id": "@test_finance",
                "name": "Финансы и инвестиции",
                "topic": "личные финансы, инвестиции, экономика",
                "audience": "люди интересующиеся деньгами",
            },
        ]

        for channel in test_cases:
            print(f"Канал: {channel['name']} | Тема: {channel['topic']}")
            topics = await scraper.scrape_for_channel(channel, limit=4)

            if topics:
                print(f"  Найдено тем: {len(topics)}")
                for t in topics:
                    src = "reddit" if "reddit" in t["source"] else "medium"
                    print(f"  [{src}] {t['topic'][:80]}")
            else:
                print("  Тем не найдено")
            print()

    asyncio.run(test())
