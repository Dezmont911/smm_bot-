"""
content_router.py — Выбор стратегии генерации под конкретный канал.

Вместо «один промпт на всех» роутер собирает стратегию канала:
  пресет архетипа  +  переопределения из карточки канала
→ возвращает эффективный стиль, веса форматов, temperature и набор хуков.

Так два канала одной ниши всё равно могут отличаться (через overrides в карточке),
а 40 каналов не нужно настраивать вручную — достаточно проставить archetype.

Использование:
    from content_router import resolve, pick_format, pick_hook
    strat = resolve(channel)
    fmt = pick_format(strat, last_format)
    hook = pick_hook(strat)
"""

import random

from archetypes import get_archetype


# Доступные форматы (ключи как в ai_client.POST_FORMATS)
FORMATS = ("совет", "факт", "вопрос", "разбор", "инфоповод")


def resolve(channel: dict) -> dict:
    """
    Собирает стратегию генерации для канала.

    Приоритет: значения из карточки канала > пресет архетипа > дефолт.
    Возвращает словарь:
        {
          "archetype": "gaming_esports",
          "style": {...},              # объединённый стиль
          "format_bias": {fmt: weight},
          "temperature": float,
          "hooks": [...],
        }
    """
    arche_name = channel.get("archetype") or "default"
    preset = get_archetype(arche_name)

    # style: пресет, поверх — overrides из карточки
    style = dict(preset["style"])
    style.update(channel.get("style") or {})

    # format_bias: из карточки или из пресета
    format_bias = channel.get("format_bias") or dict(preset["format_bias"])

    # Если у канала задан явный список post_formats — оставляем веса только для них
    allowed = channel.get("post_formats")
    if allowed:
        mapped = {_map_format(f) for f in allowed}
        format_bias = {f: w for f, w in format_bias.items() if f in mapped}
        if not format_bias:  # на всякий случай не оставить пусто
            format_bias = {f: 1 for f in mapped if f in FORMATS}

    temperature = channel.get("temperature", preset.get("temperature", 0.9))
    hooks = channel.get("hooks") or preset.get("hooks") or []

    return {
        "archetype": arche_name,
        "style": style,
        "format_bias": format_bias,
        "temperature": temperature,
        "hooks": hooks,
    }


_FORMAT_MAP = {
    "совет дня": "совет",
    "факт/статистика": "факт",
    "вопрос аудитории": "вопрос",
    "мини-разбор": "разбор",
    "инфоповод": "инфоповод",
}


def _map_format(f: str) -> str:
    """Маппит русские названия форматов из карточки в канонические ключи."""
    return _FORMAT_MAP.get(f, f)


def pick_format(strategy: dict, last_format: str | None = None) -> str:
    """
    Выбирает формат поста по весам format_bias, не повторяя последний.
    Веса 0 исключаются полностью.
    """
    bias = {f: w for f, w in strategy["format_bias"].items() if w > 0 and f in FORMATS}
    if not bias:
        bias = {f: 1 for f in FORMATS}

    # Убираем последний использованный формат, если есть из чего выбирать
    if last_format and len(bias) > 1 and last_format in bias:
        bias = {f: w for f, w in bias.items() if f != last_format}

    formats = list(bias.keys())
    weights = list(bias.values())
    return random.choices(formats, weights=weights, k=1)[0]


def pick_hook(strategy: dict) -> str | None:
    """Случайный структурный хук (как начать пост) для разнообразия."""
    hooks = strategy.get("hooks") or []
    return random.choice(hooks) if hooks else None
