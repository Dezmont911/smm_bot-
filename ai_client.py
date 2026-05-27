"""
ai_client.py — Генерация постов через Anthropic Claude API

Этот модуль отвечает за одну задачу: принять карточку канала
и инфоповод → вернуть готовый текст поста.

Использование из других модулей:
    from ai_client import generate_post
    post = await generate_post(channel_card, topic="новость о ставке ЦБ")
"""

import json
import random
from pathlib import Path

import anthropic
from loguru import logger

from config import cfg


# ============================================================
# Клиент Claude API (создаётся один раз при импорте модуля)
# ============================================================
client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)


# ============================================================
# Форматы постов — система ротации
# Соответствует handbook: форматы чередуются, не повторяются 2 раза подряд
# ============================================================
POST_FORMATS = {
    "совет": "Напиши пост в формате СОВЕТ/ИНСТРУКЦИЯ — как сделать что-то конкретное. Начни с глагола или вопроса. Дай 1–3 практических шага.",
    "факт": "Напиши пост в формате ФАКТ/СТАТИСТИКА — удивительная цифра или малоизвестный факт по теме. Объясни почему это важно читателю.",
    "вопрос": "Напиши пост в формате ВОПРОС АУДИТОРИИ — задай вовлекающий вопрос, коротко обозначь проблему, пригласи поделиться мнением в комментариях.",
    "разбор": "Напиши пост в формате МИНИ-РАЗБОР — объясни сложную тему простыми словами за 3–5 предложений. Используй аналогию из обычной жизни.",
    "инфоповод": "Напиши пост в формате ИНФОПОВОД — возьми новость и добавь взгляд редакции канала: что это значит для читателя лично.",
}


def _build_system_prompt(channel: dict, used_topics: list[str] | None = None) -> str:
    """
    Строит системный промпт из карточки канала.
    Это 'личность' редактора — Claude будет писать от её лица.

    used_topics — список тем уже опубликованных постов для дедупликации.
    """
    forbidden = ", ".join(channel.get("forbidden_topics", []))
    examples = channel.get("example_posts", [])
    examples_text = ""
    if examples:
        examples_text = "\n\nПримеры постов этого канала (придерживайся такого же стиля):\n"
        for i, ex in enumerate(examples[:2], 1):
            examples_text += f"{i}. {ex}\n"

    # Блок дедупликации — показываем Claude что уже было
    dedup_text = ""
    if used_topics:
        topics_list = "\n".join(f"- {t}" for t in used_topics[:20])
        dedup_text = f"""

УЖЕ ИСПОЛЬЗОВАННЫЕ ТЕМЫ (не повторяй их и не пиши похожее):
{topics_list}"""

    return f"""Ты — редактор Telegram-канала "{channel['name']}".

Тема канала: {channel['topic']}
Аудитория: {channel['audience']}
Тон общения: {channel['tone']}
Длина поста: {channel.get('post_length', '100–200 слов')}
Использовать эмодзи: {'да' if channel.get('use_emoji', True) else 'нет'}
Запрещено упоминать: {forbidden if forbidden else 'ничего конкретного'}
{examples_text}{dedup_text}

ПРАВИЛА:
- Пиши только текст поста — никаких вступлений типа "Вот пост:" или "Конечно!"
- Не добавляй хэштеги, если не попросят
- Не упоминай конкурентов и запрещённые темы
- Текст должен быть готов к публикации — без правок"""


def _build_user_prompt(format_name: str, topic: str) -> str:
    """
    Строит запрос пользователя: формат + тема/инфоповод.
    """
    format_instruction = POST_FORMATS.get(format_name, POST_FORMATS["совет"])
    return f"""{format_instruction}

Тема/инфоповод: {topic}

Напиши пост."""


async def generate_post(
    channel: dict,
    topic: str,
    format_name: str | None = None,
    used_topics: list[str] | None = None,
) -> dict:
    """
    Генерирует один пост для канала.

    Аргументы:
        channel     — карточка канала (словарь из JSON)
        topic       — тема или инфоповод для поста
        format_name — формат поста (если None — выбирается случайно)

    Возвращает словарь:
        {
            "content": "текст поста",
            "format": "совет",
            "channel_id": "@mychannel",
            "topic": "тема",
        }
    """
    # Если формат не задан — выбираем случайный из доступных для канала
    available_formats = channel.get("post_formats", list(POST_FORMATS.keys()))
    if format_name is None:
        # Маппинг русских названий из карточки канала на ключи POST_FORMATS
        format_map = {
            "совет дня": "совет",
            "факт/статистика": "факт",
            "вопрос аудитории": "вопрос",
            "мини-разбор": "разбор",
            "инфоповод": "инфоповод",
        }
        mapped = [format_map.get(f, f) for f in available_formats]
        format_name = random.choice([f for f in mapped if f in POST_FORMATS])

    system_prompt = _build_system_prompt(channel, used_topics=used_topics)
    user_prompt = _build_user_prompt(format_name, topic)

    logger.info(
        f"Генерирую пост | канал: {channel['channel_id']} | "
        f"формат: {format_name} | тема: {topic[:50]}..."
    )

    # Вызов Claude API
    message = client.messages.create(
        model=cfg.CLAUDE_MODEL,
        max_tokens=1024,
        messages=[
            {"role": "user", "content": user_prompt}
        ],
        system=system_prompt,
    )

    content = message.content[0].text.strip()

    logger.success(
        f"Пост сгенерирован | канал: {channel['channel_id']} | "
        f"символов: {len(content)}"
    )

    return {
        "content": content,
        "format": format_name,
        "channel_id": channel["channel_id"],
        "topic": topic,
    }


async def generate_batch(
    channel: dict,
    topics: list[str],
    count: int = 4,
) -> list[dict]:
    """
    Генерирует пакет постов для одного канала.
    Используется при утреннем пополнении буфера.

    Аргументы:
        channel — карточка канала
        topics  — список тем/инфоповодов (из RSS или вечнозелёных)
        count   — сколько постов сгенерировать

    Возвращает список постов.
    """
    posts = []
    formats = list(POST_FORMATS.keys())
    last_format = None  # Правило из handbook: нельзя 2 раза подряд один формат

    for i, topic in enumerate(topics[:count]):
        # Выбираем формат — не тот же, что был последним
        available = [f for f in formats if f != last_format]
        format_name = available[i % len(available)]
        last_format = format_name

        post = await generate_post(channel, topic, format_name)
        posts.append(post)

    return posts


def load_channel(channel_file: str) -> dict:
    """
    Загружает карточку канала из JSON файла.

    Пример:
        channel = load_channel("channels/example_channel.json")
    """
    path = Path(channel_file)
    if not path.exists():
        raise FileNotFoundError(f"Карточка канала не найдена: {channel_file}")

    with open(path, encoding="utf-8") as f:
        return json.load(f)


async def suggest_rss_sources(topic: str, channel_name: str) -> list[str]:
    """
    Просит Claude подобрать RSS-источники под тему канала,
    затем проверяет каждый URL реальным запросом (feedparser).
    Возвращает только рабочие ленты.
    """
    import feedparser
    import asyncio

    prompt = f"""Подбери 6-8 RSS-лент для Telegram-канала на тему: "{topic}".
Название канала: {channel_name}

Требования:
- Только реально существующие URL которые ты точно знаешь
- Приоритет: русскоязычные источники, если нет — английские
- Актуальные, регулярно обновляемые
- Стандартные пути RSS: /rss, /feed, /rss.xml, /atom.xml

Верни ТОЛЬКО список URL, по одному на строке, без пояснений и нумерации."""

    message = client.messages.create(
        model=cfg.CLAUDE_MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    candidates = [
        line.strip()
        for line in raw.splitlines()
        if line.strip().startswith("http")
    ]
    logger.info(f"Claude предложил {len(candidates)} кандидатов RSS для темы: {topic}")

    # Проверяем каждый URL — реально ли он отдаёт RSS-фид
    async def check_rss(url: str) -> str | None:
        try:
            loop = asyncio.get_event_loop()
            # feedparser синхронный — запускаем в executor чтобы не блокировать
            feed = await loop.run_in_executor(None, lambda: feedparser.parse(url))
            if feed.bozo and not feed.entries:
                return None  # сломанный фид
            if len(feed.entries) == 0:
                return None  # пустой фид
            return url
        except Exception:
            return None

    # Проверяем все параллельно с таймаутом 8 сек на весь батч
    tasks = [check_rss(url) for url in candidates]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    working = [
        url for url, res in zip(candidates, results)
        if res and not isinstance(res, Exception)
    ]

    logger.info(
        f"RSS проверка: {len(candidates)} кандидатов → {len(working)} рабочих: {working}"
    )
    return working[:5]


async def suggest_evergreen_topics(topic: str, count: int = 10) -> list[str]:
    """
    Генерирует вечнозелёные темы для канала через Claude.
    Используется при добавлении нового канала через /add.

    Возвращает список тем которые всегда актуальны.
    """
    prompt = f"""Придумай {count} вечнозелёных тем для постов в Telegram-канале на тему: "{topic}".

Вечнозелёные темы — это темы которые актуальны всегда, не привязаны к конкретным событиям.

Верни ТОЛЬКО список тем, по одной на строке, без нумерации и пояснений."""

    message = client.messages.create(
        model=cfg.CLAUDE_MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    topics = [line.strip("–—•* ").strip() for line in raw.splitlines() if line.strip()]
    return topics[:count]


# ============================================================
# ТЕСТ — запускается напрямую: python ai_client.py
# ============================================================
if __name__ == "__main__":
    import asyncio

    async def test():
        print("🚀 Тест генерации поста через Claude API\n")
        print("=" * 50)

        # Загружаем тестовую карточку канала
        channel = load_channel("channels/example_channel.json")
        print(f"📋 Канал: {channel['name']}")
        print(f"   Тема:  {channel['topic']}")
        print(f"   Тон:   {channel['tone']}\n")

        # Тестовый инфоповод
        test_topic = "ЦБ РФ сохранил ключевую ставку на уровне 21% годовых"

        # Генерируем пост
        print(f"📰 Инфоповод: {test_topic}\n")
        print("⏳ Генерирую пост...\n")

        result = await generate_post(channel, test_topic)

        print("=" * 50)
        print(f"✅ Формат: {result['format']}")
        print(f"📝 Пост ({len(result['content'])} символов):\n")
        print(result["content"])
        print("=" * 50)

        # Тест пакетной генерации
        print("\n\n🔄 Тест пакетной генерации (3 поста)...\n")
        topics = [
            "Инфляция в России составила 9.2% по итогам года",
            "Как правильно составить личный бюджет — советы экономистов",
            "Россияне стали чаще открывать вклады в банках",
        ]

        posts = await generate_batch(channel, topics, count=3)

        for i, post in enumerate(posts, 1):
            print(f"--- Пост {i} [{post['format']}] ---")
            print(post["content"][:200] + "...\n")

    asyncio.run(test())
