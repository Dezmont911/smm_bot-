"""
channel_analyzer.py — Анализ Telegram-канала по экспорту чата

Пользователь делает экспорт канала через Telegram Desktop:
  Настройки → Экспорт данных чата → JSON → result.json

Этот модуль:
  1. Читает result.json
  2. Извлекает текстовые посты (убирает стикеры, видео, системные)
  3. Берёт выборку до 30 постов (новых + случайных из архива)
  4. Отправляет в Claude с просьбой определить параметры канала
  5. Возвращает готовую карточку канала

Результат:
  {
    "name": "Крутые находки WB",
    "topic": "Товары с WB и Ozon — находки, скидки, новинки",
    "tone": "дружелюбный, позитивный",
    "channel_type": "marketplace",  # или "content"
    "evergreen_topics": ["Топ-5 товаров до 500 ₽", ...],
    "post_frequency": 3,  # постов в день
    "confidence": 0.9,
    "analysis_notes": "Канал публикует карточки товаров WB..."
  }
"""

import json
import random
from pathlib import Path

from loguru import logger

from ai_client import claude_client
from config import cfg


# ============================================================
# ПРОМПТ ДЛЯ АНАЛИЗА
# ============================================================

ANALYSIS_PROMPT = """Ты анализируешь Telegram-канал для настройки SMM-бота.

Я дам тебе выборку постов из канала. Твоя задача — определить параметры канала.

ВАЖНЫЕ ОПРЕДЕЛЕНИЯ:
- channel_type = "marketplace" — если канал публикует товары с маркетплейсов (WB, Ozon, AliExpress): цены, артикулы, ссылки на товары. Даже если иногда есть другой контент.
- channel_type = "content" — все остальные: новости, факты, образование, развлечения, обзоры, игры, авто, рыбалка, кулинария, лайфхаки и т.д.

ПОСТЫ ИЗ КАНАЛА:
{posts_text}

Ответь ТОЛЬКО валидным JSON (без markdown, без пояснений):
{{
  "name": "название канала на основе контента",
  "topic": "краткое описание темы канала в 1-2 предложения, что он публикует",
  "tone": "тон общения (например: информационный, дружелюбный, экспертный, развлекательный, продающий)",
  "channel_type": "marketplace" или "content",
  "evergreen_topics": ["вечнозелёная тема 1", "вечнозелёная тема 2", "...до 10 тем"],
  "post_frequency": число постов в день (целое число 1-10),
  "rss_keywords": ["ключевое слово для RSS 1", "ключевое слово 2", "...до 5 слов"],
  "confidence": число от 0.5 до 1.0 (насколько ты уверен в анализе),
  "analysis_notes": "1-2 предложения: что за канал, что постит"
}}

Для "evergreen_topics" придумай темы которые ПОДХОДЯТ для этого канала и никогда не устаревают.
Для "rss_keywords" подбери слова для поиска RSS-лент по теме канала.
"""


# ============================================================
# ОСНОВНОЙ КЛАСС
# ============================================================

class ChannelAnalyzer:
    """Анализирует Telegram-канал по JSON-экспорту."""

    # Сколько постов берём для анализа
    SAMPLE_SIZE = 30
    # Минимальная длина поста для анализа (отсеиваем "." и короткие подписи)
    MIN_POST_LENGTH = 30

    async def analyze_export(self, json_path: str | Path) -> dict:
        """
        Главный метод. Принимает путь к result.json.
        Возвращает карточку канала или выбрасывает исключение.
        """
        json_path = Path(json_path)
        if not json_path.exists():
            raise FileNotFoundError(f"Файл не найден: {json_path}")

        # Читаем экспорт
        logger.info(f"Читаю экспорт канала: {json_path}")
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)

        channel_name = data.get("name", "Неизвестный канал")
        messages = data.get("messages", [])

        if not messages:
            raise ValueError("В экспорте нет сообщений")

        logger.info(f"Экспорт '{channel_name}': {len(messages)} сообщений")

        # Извлекаем текстовые посты
        posts = self._extract_posts(messages)
        if len(posts) < 3:
            raise ValueError(
                f"Слишком мало текстовых постов для анализа ({len(posts)}). "
                "Нужно минимум 3 поста с текстом."
            )

        # Формируем выборку
        sample = self._make_sample(posts)
        logger.info(f"Выборка для анализа: {len(sample)} постов")

        # Анализируем через Claude
        analysis = await self._analyze_with_claude(sample, channel_name)

        # Дополняем результат метаданными из экспорта
        analysis["export_channel_name"] = channel_name
        analysis["total_messages"] = len(messages)
        analysis["analyzed_posts"] = len(sample)
        analysis["post_frequency"] = self._estimate_frequency(messages)

        logger.success(
            f"Анализ завершён: type={analysis.get('channel_type')}, "
            f"confidence={analysis.get('confidence')}"
        )
        return analysis

    async def analyze_from_bytes(self, file_bytes: bytes, filename: str = "result.json") -> dict:
        """
        Анализирует экспорт из байтов (для Telegram бота — документ в памяти).
        """
        import tempfile
        import os

        # Пишем во временный файл
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=".json", delete=False
        ) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name

        try:
            return await self.analyze_export(tmp_path)
        finally:
            os.unlink(tmp_path)

    # --------------------------------------------------------
    # Извлечение постов
    # --------------------------------------------------------

    def _extract_posts(self, messages: list) -> list[str]:
        """
        Извлекает читаемый текст из сообщений.
        Пропускает: системные, стикеры, пустые, очень короткие.
        """
        posts = []
        for msg in messages:
            # Только обычные сообщения
            if msg.get("type") != "message":
                continue

            # Извлекаем текст (может быть строкой или списком фрагментов)
            text = self._extract_text(msg.get("text", ""))

            if len(text) < self.MIN_POST_LENGTH:
                continue

            posts.append(text)

        return posts

    def _extract_text(self, text_field) -> str:
        """
        Telegram экспорт: text может быть строкой или списком dict/str.
        Например: ["привет ", {"type": "bold", "text": "мир"}, "!"]
        """
        if isinstance(text_field, str):
            return text_field.strip()

        if isinstance(text_field, list):
            parts = []
            for item in text_field:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    # Берём текст любого типа форматирования
                    parts.append(item.get("text", ""))
            return "".join(parts).strip()

        return ""

    # --------------------------------------------------------
    # Выборка постов для анализа
    # --------------------------------------------------------

    def _make_sample(self, posts: list[str]) -> list[str]:
        """
        Формирует выборку для отправки в Claude.
        Берёт последние 10 + случайные из архива = до SAMPLE_SIZE постов.
        """
        if len(posts) <= self.SAMPLE_SIZE:
            return posts

        # Берём последние 10 (самые актуальные)
        recent = posts[-10:]

        # И случайные из остальных
        rest = posts[:-10]
        n_random = min(self.SAMPLE_SIZE - len(recent), len(rest))
        sampled = random.sample(rest, n_random) if n_random > 0 else []

        # Перемешиваем чтобы Claude не видел порядок
        combined = recent + sampled
        random.shuffle(combined)
        return combined

    # --------------------------------------------------------
    # Анализ через Claude
    # --------------------------------------------------------

    async def _analyze_with_claude(self, posts: list[str], channel_name: str) -> dict:
        """Отправляет выборку постов в Claude и получает анализ."""

        # Формируем текст постов (нумерованный список, обрезаем длинные)
        posts_text_parts = []
        for i, post in enumerate(posts, 1):
            truncated = post[:400] if len(post) > 400 else post
            posts_text_parts.append(f"[{i}] {truncated}")
        posts_text = "\n\n".join(posts_text_parts)

        # Добавляем имя канала из экспорта как подсказку
        header = f"Имя канала из экспорта: «{channel_name}»\n\n"
        prompt = ANALYSIS_PROMPT.format(posts_text=header + posts_text)

        try:
            import anthropic
            client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)

            response = client.messages.create(
                model=cfg.CLAUDE_MODEL,
                max_tokens=1000,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()

            # Убираем возможный markdown-блок
            if raw.startswith("```"):
                lines = raw.split("\n")
                raw = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

            result = json.loads(raw)
            return result

        except json.JSONDecodeError as e:
            logger.error(f"Claude вернул невалидный JSON: {e}")
            # Возвращаем базовые параметры как fallback
            return self._fallback_analysis(posts, channel_name)

        except Exception as e:
            logger.error(f"Ошибка анализа через Claude: {e}")
            return self._fallback_analysis(posts, channel_name)

    def _fallback_analysis(self, posts: list[str], channel_name: str) -> dict:
        """Базовый анализ без Claude — простая эвристика по ключевым словам."""
        all_text = " ".join(posts).lower()

        # Определяем тип по ключевым словам
        marketplace_keywords = ["wb", "wildberries", "ozon", "озон", "артикул", "₽", "скидка", "цена", "руб.", "рублей"]
        marketplace_score = sum(1 for kw in marketplace_keywords if kw in all_text)
        channel_type = "marketplace" if marketplace_score >= 3 else "content"

        return {
            "name": channel_name,
            "topic": f"Контент канала «{channel_name}»",
            "tone": "информационный",
            "channel_type": channel_type,
            "evergreen_topics": [],
            "post_frequency": 3,
            "rss_keywords": [],
            "confidence": 0.5,
            "analysis_notes": "Анализ выполнен без Claude (ошибка API).",
        }

    # --------------------------------------------------------
    # Частота постинга
    # --------------------------------------------------------

    def _estimate_frequency(self, messages: list) -> int:
        """
        Оценивает среднюю частоту постинга в день
        по последним 30 дням экспорта.
        """
        from datetime import datetime, timezone, timedelta

        # Берём только обычные сообщения
        timestamps = []
        for msg in messages:
            if msg.get("type") != "message":
                continue
            ts = msg.get("date_unixtime") or msg.get("date")
            if ts and str(ts).isdigit():
                timestamps.append(int(ts))

        if len(timestamps) < 2:
            return 3  # дефолт

        # Анализируем последние 30 дней
        now = datetime.now(timezone.utc).timestamp()
        cutoff = now - 30 * 24 * 3600
        recent = [t for t in timestamps if t >= cutoff]

        if not recent:
            # Если нет постов за 30 дней — берём весь период
            span_days = max(1, (max(timestamps) - min(timestamps)) / 86400)
            avg = len(timestamps) / span_days
        else:
            avg = len(recent) / 30

        # Округляем до разумных значений
        freq = round(avg)
        return max(1, min(10, freq))  # от 1 до 10 постов в день


# ============================================================
# ЕДИНСТВЕННЫЙ ЭКЗЕМПЛЯР
# ============================================================
analyzer = ChannelAnalyzer()
