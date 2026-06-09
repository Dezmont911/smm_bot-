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
import re
from pathlib import Path

from loguru import logger

from config import cfg
from claude_helper import claude_text
from content_safety import build_safe_channel_profile
from channel_dna import attach_channel_dna


_MARKETPLACE_BRAND_RE = re.compile(
    r"\b(?:wb|ozon|aliexpress|ali|lamoda)\b|"
    r"wildberries|вайлдберриз|озон|алиэкспресс|яндекс\s*маркет|"
    r"wildberries\.ru|ozon\.ru|aliexpress\.|market\.yandex\.",
    re.IGNORECASE,
)
_MARKETPLACE_PRODUCT_RE = re.compile(
    r"артикул|sku|цена|скидк|промокод|распродаж|купить|заказать|"
    r"в\s+корзин|товар|находк[аи]|руб\.?|рублей|₽|\d+\s*(?:₽|руб\.?|рублей)",
    re.IGNORECASE,
)
_MARKETPLACE_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:wildberries\.ru|ozon\.ru|aliexpress\.|market\.yandex\.)",
    re.IGNORECASE,
)
_GENERIC_AD_RE = re.compile(
    r"реклама|сотрудничество|ответственност|рекламодател|прайс|закуп|размещени[ея]",
    re.IGNORECASE,
)


def _looks_like_marketplace_fallback(text: str) -> bool:
    """Conservative fallback: classify as marketplace only on product evidence."""
    if not text:
        return False

    product_urls = len(_MARKETPLACE_URL_RE.findall(text))
    brands = len(_MARKETPLACE_BRAND_RE.findall(text))
    product_signals = len(_MARKETPLACE_PRODUCT_RE.findall(text))

    if product_urls:
        return True
    if brands and product_signals >= 1:
        return True

    # Do not let generic ad footers turn a content channel into WB mode.
    generic_ads = len(_GENERIC_AD_RE.findall(text))
    if generic_ads and product_signals <= generic_ads:
        return False

    return False


# ============================================================
# ПРОМПТ ДЛЯ АНАЛИЗА
# ============================================================

ANALYSIS_PROMPT = """Ты анализируешь Telegram-канал для настройки SMM-бота.

Я дам тебе выборку постов из канала. Твоя задача — определить параметры канала.

🔴 ГЛАВНОЕ ПРАВИЛО: тему, тип, архетип и всё остальное определяй СТРОГО по СОДЕРЖАНИЮ
постов ниже. Название канала может быть каким угодно и НЕ отражать реальный контент —
названия у тебя нет, и не выдумывай тему «по смыслу названия». Только то, о чём
РЕАЛЬНО пишут посты. Если посты про рыбалку — тема рыбалка, как бы канал ни назывался.

ВАЖНЫЕ ОПРЕДЕЛЕНИЯ:
- channel_type = "marketplace" — если канал публикует товары с маркетплейсов (WB, Ozon, AliExpress): цены, артикулы, ссылки на товары. Даже если иногда есть другой контент.
- channel_type = "content" — все остальные: новости, факты, образование, развлечения, обзоры, игры, авто, рыбалка, кулинария, лайфхаки и т.д.

🚫 ЗАПРЕЩЁННАЯ ТЕМАТИКА: если ОСНОВНОЙ (профильный) контент канала — это 18+/порно/
эротика, сексуализация несовершеннолетних, наркотики, оружие/насилие/терроризм/война,
азартные игры/ставки, мошенничество/скам, политическая агитация или экстремизм —
поставь "forbidden": true и кратко укажи причину в "forbidden_reason". Смотри на СУТЬ
контента (в т.ч. эвфемизмы и иносказания, любой язык), а не только на явные слова.
Разовая сторонняя реклама запрещёнкой не считается — оценивай профильный контент.

ИГНОРИРУЙ РЕКЛАМУ: среди постов могут попадаться рекламные/спонсорские вставки,
не относящиеся к сути канала (сторонние товары, кредиты/займы, казино/ставки,
похудение, вакансии, реклама других каналов и т.п.). Определяй тему, тон, архетип
и постоянные темы ТОЛЬКО по основному (профильному) контенту канала, рекламные
посты не учитывай.

ПОСТЫ ИЗ КАНАЛА:
{posts_text}

АРХЕТИП (стиль/ниша канала) — выбери ОДИН из списка:
- gaming_esports — киберспорт, турниры, патчи, профессиональная сцена игр
- gaming_casual — игры казуально, мемы, забавные клипы, стримеры
- anime — аниме, мангa, фандом, релизы тайтлов
- news — общие/тематические новости, события
- auto — авто, техника, тест-драйвы
- celeb_drama — шоу-бизнес, знаменитости, светская хроника
- finance — финансы, крипта, рынки, инвестиции
- default — если ничего из перечисленного не подходит

ИСТОЧНИК ТЕМ:
- "search" — если канал про СВЕЖИЕ новости/события (киберспорт, новости, крипта,
  шоубиз, релизы аниме) — тогда бот будет искать темы в интернете.
- "rss" — если контент вечнозелёный/нишевый без привязки к срочным новостям.

Ответь ТОЛЬКО валидным JSON (без markdown, без пояснений):
{{
  "name": "название канала на основе контента",
  "topic": "краткое описание темы канала в 1-2 предложения, что он публикует",
  "forbidden": true или false (true — если профильный контент канала запрещён, см. выше),
  "forbidden_reason": "если forbidden=true — кратко почему (1 фраза), иначе пустая строка",
  "tone": "тон общения (например: информационный, дружелюбный, экспертный, развлекательный, продающий)",
  "channel_type": "marketplace" или "content",
  "archetype": "один из архетипов выше",
  "topic_source": "search" или "rss",
  "evergreen_topics": ["вечнозелёная тема 1", "вечнозелёная тема 2", "...до 10 тем"],
  "post_frequency": число постов в день (целое число 1-10),
  "rss_keywords": ["ключевое слово для RSS 1", "ключевое слово 2", "...до 5 слов"],
  "confidence": число от 0.5 до 1.0 (насколько ты уверен в анализе),
  "analysis_notes": "1-2 предложения простым языком: о чём канал и что публикует (без терминов вроде «вечнозелёный»)"
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

        analysis["safe_profile"] = build_safe_channel_profile(analysis)
        attach_channel_dna(analysis, sample)

        logger.success(
            f"Анализ завершён: type={analysis.get('channel_type')}, "
            f"confidence={analysis.get('confidence')}"
        )
        return analysis

    async def analyze_posts(
        self, channel_name: str, posts: list[str], about: str = ""
    ) -> dict:
        """
        Анализирует канал по уже извлечённым постам (например, прочитанным
        через Telethon-юзербота по @username — без файла экспорта).

        channel_name — название канала, posts — список текстов постов,
        about — описание канала (если есть). Возвращает ту же карточку,
        что и analyze_export.
        """
        clean = [p.strip() for p in posts if p and len(p.strip()) >= self.MIN_POST_LENGTH]
        if len(clean) < 3:
            raise ValueError(
                f"Слишком мало текстовых постов для анализа ({len(clean)}). "
                "Нужно минимум 3 поста с текстом."
            )

        sample = self._make_sample(clean)
        # Описание канала добавляем подсказкой к имени — помогает классификации
        name_hint = channel_name + (f" — {about}" if about else "")
        analysis = await self._analyze_with_claude(sample, name_hint)

        analysis["export_channel_name"] = channel_name
        analysis["analyzed_posts"] = len(sample)
        analysis.setdefault("post_frequency", 4)
        analysis.setdefault("forbidden", False)
        analysis["safe_profile"] = build_safe_channel_profile(analysis)
        attach_channel_dna(analysis, sample, about=about)
        logger.success(
            f"Анализ по постам завершён: type={analysis.get('channel_type')}, "
            f"archetype={analysis.get('archetype')}, conf={analysis.get('confidence')}"
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

        # Название канала в промпт НЕ передаём намеренно: оно может быть любым и
        # искажать тему. Анализ — строго по содержанию постов.
        prompt = ANALYSIS_PROMPT.format(posts_text=posts_text)

        try:
            raw = await claude_text(
                max_tokens=1000,
                messages=[{"role": "user", "content": prompt}],
            )

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

        channel_type = "marketplace" if _looks_like_marketplace_fallback(all_text) else "content"

        return {
            "name": channel_name,
            "topic": f"Контент канала «{channel_name}»",
            "tone": "информационный",
            "channel_type": channel_type,
            "archetype": "default",
            "topic_source": "rss",
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
# Авто-определение архетипа и источника по описанию (ручной /add)
# ============================================================

# Список валидных архетипов берём из пресетов, чтобы не рассинхронизироваться
from archetypes import ARCHETYPES

_VALID_ARCHETYPES = set(ARCHETYPES.keys())
_VALID_SOURCES = {"search", "rss"}


def normalize_meta(archetype: str | None, topic_source: str | None) -> tuple[str, str]:
    """Приводит архетип/источник к валидным значениям (с дефолтами)."""
    arch = (archetype or "").strip().lower()
    if arch not in _VALID_ARCHETYPES:
        arch = "default"
    src = (topic_source or "").strip().lower()
    if src not in _VALID_SOURCES:
        src = "rss"
    return arch, src


async def classify_channel(name: str, topic: str) -> dict:
    """
    Лёгкая классификация канала по краткому описанию (ручной путь /add).
    Возвращает {"archetype", "topic_source", "confidence"}.
    Не падает: при ошибке возвращает дефолт (default/rss).
    """
    arche_list = ", ".join(sorted(_VALID_ARCHETYPES))
    prompt = f"""Определи нишу Telegram-канала и источник тем.

Название: {name}
Описание: {topic}

Архетип (выбери ОДИН): {arche_list}
  gaming_esports=киберспорт/турниры/патчи, gaming_casual=игры/мемы/стримеры,
  anime=аниме/фандом, news=новости/события, auto=авто/техника,
  celeb_drama=шоубиз/знаменитости, finance=финансы/крипта, default=иначе.

Источник тем:
  "search" — если про свежие новости/события (киберспорт, новости, крипта, шоубиз, релизы),
  "rss" — если вечнозелёный/нишевый контент без срочности.

Ответь ТОЛЬКО JSON: {{"archetype": "...", "topic_source": "...", "confidence": 0.0-1.0}}"""

    try:
        raw = await claude_text(
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        import re as _re
        m = _re.search(r"\{.*\}", raw, _re.DOTALL)
        data = json.loads(m.group()) if m else {}
        arch, src = normalize_meta(data.get("archetype"), data.get("topic_source"))
        conf = data.get("confidence", 0.6)
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            conf = 0.6
        logger.info(f"classify_channel '{name}': архетип={arch}, источник={src}, conf={conf}")
        return {"archetype": arch, "topic_source": src, "confidence": conf}
    except Exception as e:
        logger.warning(f"classify_channel ошибка: {e} — дефолт default/rss")
        return {"archetype": "default", "topic_source": "rss", "confidence": 0.0}


# ============================================================
# ЕДИНСТВЕННЫЙ ЭКЗЕМПЛЯР
# ============================================================
analyzer = ChannelAnalyzer()
