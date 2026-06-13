"""
topic_search.py — Поиск свежих тем для постов через веб-поиск Claude.

Идея: каналу достаточно МИНИМАЛЬНОГО описания (например «CS2: новости, патчи,
киберспорт»), а Claude сам находит в интернете актуальные инфоповоды с помощью
встроенного инструмента web_search и возвращает список тем. Это убирает
зависимость от RSS-лент и конфликт дедупликации с повторяющимися заголовками.

Включается через поле карточки канала:
    "topic_source": "search"

Модель: по умолчанию cfg.CLAUDE_MODEL (Haiku). Если Haiku вернул пусто —
автоматический фолбэк на Sonnet (он лучше в tool-use).

Документация инструмента:
https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool
Стоимость: $10 / 1000 поисков + токены. На наших объёмах — копейки.
"""

import json
import re
from datetime import datetime, timezone

import anthropic
from loguru import logger

from claude_helper import (
    aclient,
    _is_anthropic_billing_error,
    _openai_available,
    openai_web_search_text,
)
from config import cfg
from database import db


# Модель-фолбэк для шага поиска, если основная (Haiku) не справилась
SEARCH_FALLBACK_MODEL = "claude-sonnet-4-5"

# Базовое определение серверного инструмента веб-поиска
_WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}


def _parse_topics(text: str) -> list[str]:
    """Достаёт список тем из ответа Claude (сначала JSON-массив, потом построчно).
    На выходе отсекает заголовки/мета/отказы/запретку через ai_client.clean_topics."""
    from ai_client import clean_topics
    if not text:
        return []
    # 1) пытаемся найти JSON-массив
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        try:
            arr = json.loads(m.group())
            topics = clean_topics([str(t) for t in arr])
            if topics:
                return topics
        except Exception:
            pass
    # 2) фолбэк — построчно, чистим маркеры списков/нумерацию/кавычки
    lines = []
    for line in text.splitlines():
        cleaned = re.sub(r'^[\-\*\d\.\)\s"«»]+', "", line).strip().strip('"«»').strip()
        lines.append(cleaned)
    return clean_topics(lines)


def _build_prompt(channel: dict, count: int, used_topics: list[str]) -> str:
    """Промпт для поиска свежих тем."""
    name = channel.get("name", "")
    topic = channel.get("topic", "")
    audience = channel.get("audience", "")

    dedup = ""
    if used_topics:
        items = "\n".join(f"- {t[:100]}" for t in used_topics[:20])
        dedup = f"\n\nНЕ предлагай эти уже использованные темы (и похожие на них):\n{items}"

    audience_line = f"Аудитория: {audience}\n" if audience else ""

    return f"""Ты — редактор Telegram-канала «{name}».
Тема канала: {topic}
{audience_line}
Найди в интернете {count} СВЕЖИХ и интересных инфоповодов по теме канала —
актуальные новости, события, обновления, релизы за последние дни/недели.
Обязательно используй веб-поиск, не выдумывай.

Требования к темам:
- разные между собой, конкретные (не общие рубрики)
- реально существующие, подтверждённые поиском
- интересные аудитории канала{dedup}

Верни ТОЛЬКО JSON-массив строк с темами, без пояснений и без markdown.
Пример: ["Valve выпустила обновление X для CS2", "Команда Y выиграла мажор Z"]"""


async def _search_once(prompt: str, model: str, max_uses: int,
                       allowed_domains: list[str] | None) -> str:
    """
    Один проход веб-поиска. Возвращает финальный текст ответа Claude.
    Обрабатывает stop_reason='pause_turn' (серверный цикл инструмента).
    """
    tool = dict(_WEB_SEARCH_TOOL)
    tool["max_uses"] = max_uses
    if allowed_domains:
        tool["allowed_domains"] = allowed_domains

    messages = [{"role": "user", "content": prompt}]

    for _ in range(6):  # защита от бесконечного цикла pause_turn
        resp = await aclient.messages.create(
            model=model,
            max_tokens=1024,
            messages=messages,
            tools=[tool],
        )
        if resp.stop_reason == "pause_turn":
            # Сервер просит продолжить — возвращаем накопленный контент назад
            messages.append({"role": "assistant", "content": resp.content})
            continue
        # Финальный ответ — склеиваем все текстовые блоки
        return "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ).strip()

    logger.warning("web_search: превышен лимит итераций pause_turn")
    return ""


async def _search_openai_fallback(
    channel: dict,
    count: int,
    used_topics: list[str],
    allowed_domains: list[str] | None = None,
    purpose: str = "topic_search_openai_web_search_fallback",
    reason: str = "Anthropic недоступен",
) -> list[str]:
    if not _openai_available():
        return []
    channel_id = channel.get("channel_id", "?")
    prompt = _build_prompt(channel, count, used_topics)
    try:
        text = await openai_web_search_text(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=900,
            system=(
                "Ты помогаешь редактору Telegram-канала искать свежие темы. "
                "Обязательно используй веб-поиск. Верни только JSON-массив строк."
            ),
            temperature=0.5,
            retries=1,
            purpose=purpose,
            allowed_domains=allowed_domains,
        )
        topics = _parse_topics(text)
        if topics:
            logger.info(
                f"web_search [{channel_id}]: {reason}, "
                f"OpenAI web_search fallback дал {len(topics)} тем"
            )
            return topics[:count]
    except Exception as e:
        logger.warning(
            f"web_search [{channel_id}]: OpenAI web_search fallback не помог: "
            f"{type(e).__name__}: {e}"
        )
    return []


async def discover_topics(
    channel: dict,
    count: int = 10,
    used_topics: list[str] | None = None,
    max_uses: int = 5,
) -> list[str]:
    """
    Возвращает список свежих тем для канала через веб-поиск.

    Сначала пробует основную модель (cfg.CLAUDE_MODEL = Haiku).
    Если она вернула 0 тем — повторяет на SEARCH_FALLBACK_MODEL (Sonnet).
    При ошибке (веб-поиск не включён в Console, сеть и т.п.) — возвращает [],
    и вызывающий код спокойно откатывается на RSS/вечнозелёные темы.
    """
    channel_id = channel.get("channel_id", "?")
    used_topics = used_topics or []
    allowed_domains = channel.get("search_domains") or None
    prompt = _build_prompt(channel, count, used_topics)

    if (cfg.LLM_PROVIDER or "").strip().lower() == "openai":
        topics = await _search_openai_fallback(
            channel,
            count,
            used_topics,
            allowed_domains,
            purpose="topic_search_openai_web_search_primary",
            reason="LLM_PROVIDER=openai",
        )
        if topics:
            return topics
        logger.warning(
            f"web_search [{channel_id}]: OpenAI primary returned no topics, trying Anthropic"
        )

    for model in (cfg.CLAUDE_MODEL, SEARCH_FALLBACK_MODEL):
        try:
            text = await _search_once(prompt, model, max_uses, allowed_domains)
            topics = _parse_topics(text)
            if topics:
                logger.info(
                    f"web_search [{channel_id}] ({model}): найдено {len(topics)} тем"
                )
                return topics[:count]
            logger.warning(
                f"web_search [{channel_id}] ({model}): тем не найдено, "
                f"{'пробую Sonnet' if model == cfg.CLAUDE_MODEL else 'фолбэк не помог'}"
            )
        except anthropic.APIError as e:
            if _is_anthropic_billing_error(e):
                topics = await _search_openai_fallback(
                    channel, count, used_topics, allowed_domains
                )
                if topics:
                    return topics
            logger.warning(
                f"web_search [{channel_id}] ({model}): ошибка API: "
                f"{type(e).__name__}: {e}. "
                f"Проверь, включён ли веб-поиск в Console (Settings → Privacy)."
            )
            # На ошибке API нет смысла пробовать другую модель — выходим в фолбэк
            return []
        except Exception as e:
            logger.warning(f"web_search [{channel_id}] ({model}): {type(e).__name__}: {e}")

    return []


# ============================================================
# Кэш тем — чтобы не искать в вебе на каждый прогон
# ============================================================

def _cache_get_unused(channel_id: str, ttl_hours: int) -> list[tuple[int, str]]:
    """Свободные (used=0) свежие темы из кэша. Возвращает [(id, topic), ...]."""
    try:
        with db.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, topic FROM topic_cache
                WHERE channel_id = ? AND used = 0
                  AND created_at > datetime('now', ?)
                ORDER BY created_at DESC
                """,
                (channel_id, f"-{int(ttl_hours)} hours"),
            ).fetchall()
        return [(r["id"], r["topic"]) for r in rows]
    except Exception as e:
        logger.warning(f"topic_cache get ошибка [{channel_id}]: {e}")
        return []


def _cache_store(channel_id: str, topics: list[str]):
    """Складывает темы в кэш как свободные."""
    if not topics:
        return
    now = datetime.now(timezone.utc).isoformat()
    try:
        with db.connect() as conn:
            conn.executemany(
                "INSERT INTO topic_cache (channel_id, topic, created_at, used) VALUES (?, ?, ?, 0)",
                [(channel_id, t, now) for t in topics],
            )
    except Exception as e:
        logger.warning(f"topic_cache store ошибка [{channel_id}]: {e}")


def _cache_mark_used(ids: list[int]):
    """Помечает темы как использованные + чистит протухшие записи."""
    if not ids:
        return
    try:
        with db.connect() as conn:
            conn.executemany("UPDATE topic_cache SET used = 1 WHERE id = ?", [(i,) for i in ids])
            # попутная уборка старья (TTL*3, чтобы не копилось)
            conn.execute(
                "DELETE FROM topic_cache WHERE created_at < datetime('now', ?)",
                (f"-{cfg.TOPIC_CACHE_TTL_HOURS * 3} hours",),
            )
    except Exception as e:
        logger.warning(f"topic_cache mark ошибка: {e}")


async def get_topics(channel: dict, count: int, used_topics: list[str] | None = None) -> list[str]:
    """
    Возвращает `count` тем для канала, экономя веб-поиск через кэш.

    Логика:
      1) Берём свободные свежие темы из кэша (в пределах TTL).
      2) Если их хватает — используем, помечаем used, БЕЗ обращения к поиску.
      3) Если мало — делаем ОДИН веб-поиск с запасом (TOPIC_SEARCH_BATCH),
         лишнее кладём в кэш, недостающее добираем из свежего поиска.
    """
    channel_id = channel.get("channel_id", "?")
    used_topics = used_topics or []
    ttl = cfg.TOPIC_CACHE_TTL_HOURS

    # 1) Кэш
    cached = _cache_get_unused(channel_id, ttl)
    picked_ids: list[int] = []
    picked: list[str] = []
    for cid, topic in cached:
        if len(picked) >= count:
            break
        picked_ids.append(cid)
        picked.append(topic)

    if len(picked) >= count:
        _cache_mark_used(picked_ids)
        logger.info(f"Темы из кэша [{channel_id}]: {len(picked)} (без веб-поиска)")
        return picked[:count]

    # 2) Не хватило — ищем с запасом
    need = count - len(picked)
    batch = max(cfg.TOPIC_SEARCH_BATCH, need)
    # в дедуп для поиска отдаём и уже использованные, и взятые из кэша
    fresh = await discover_topics(
        channel, count=batch, used_topics=used_topics + picked
    )

    if fresh:
        take = fresh[:need]
        rest = fresh[need:]
        _cache_store(channel_id, rest)  # излишек — в кэш на будущее
        _cache_mark_used(picked_ids)    # то что взяли из кэша — пометить
        result = picked + take
        logger.info(
            f"Темы [{channel_id}]: кэш={len(picked)} + поиск={len(take)} "
            f"(в кэш отложено {len(rest)})"
        )
        return result[:count]

    # поиск ничего не дал — возвращаем что есть из кэша
    if picked:
        _cache_mark_used(picked_ids)
    return picked[:count]
