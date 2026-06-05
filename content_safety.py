"""
content_safety.py — lightweight v1 safety layer for topic -> brief -> post.

This module is intentionally rule-based: no LLM calls, no Telegram publishing,
no database writes. It keeps raw source text out of the writer prompt by turning
candidate topics into safe topics/angles first.
"""

from __future__ import annotations

import re
from typing import Any


BLOCKED_TERMS = (
    "порно", "порнограф", "эроти", "наркотик", "казино", "ставк на спорт",
    "мошенн", "скам", "террор", "теракт", "экстрем", "оружие", "дрон",
    "ракета", "обстрел", "война", "украин", "лгбт",
)

REFUSAL_START_MARKERS = (
    "я не могу написать", "я не могу помочь", "я не могу создать",
    "извините, но я не могу", "извини, но я не могу",
    "к сожалению, я не могу", "как ии", "as an ai",
    "i cannot", "i can't", "i am unable", "i'm unable",
)

META_REFUSAL_MARKERS = (
    "выберите другую тему", "выбери другую тему", "этот запрос нарушает",
    "не могу создать такой контент", "не могу помочь с этой темой",
    "не могу написать такой пост", "не могу написать на эту тему",
)

GAME_NEWS_MARKERS = (
    "nintendo", "direct", "steam", "playstation", "xbox", "console",
    "консол", "релиз игр", "релизы игр", "новые игры", "игровая новость",
    "игровые новости", "топ игр", "game pass",
)

ADULT_TECH_NEWS_MARKERS = (
    "ии научился", "ai научился", "искусственный интеллект научился",
    "лучше программистов", "заменит программистов", "зарплат", "войти в it",
    "карьера в it", "рынок it", "нейросеть написала код",
)

KIDS_EDU_ARCHETYPES = {"kids_education", "local_service", "parent_marketing", "hobby_school", "edtech"}

KIDS_EDU_PROFILE_MARKERS = (
    "робототех", "программирован", "дет", "ребен", "ребён", "школь",
    "занят", "круж", "секци", "лагер", "логик", "самостоятельн",
)

KIDS_EDU_FIT_MARKERS = (
    "робототех", "программирован", "ребен", "ребён", "дет", "родител",
    "обуч", "занят", "круж", "секци", "логик", "мышлен", "самостоятельн",
    "пробн", "школ", "лагер", "проект", "созда", "конструкт",
)


def _clean_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].rstrip()


def _low(value: Any) -> str:
    return _clean_text(value, 1000).lower()


def _as_list(value: Any, limit: int = 12) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [_clean_text(v, 160) for v in value[:limit] if _clean_text(v, 160)]


def _channel_dna(channel: dict) -> dict:
    dna = channel.get("channel_dna") or {}
    return dna if isinstance(dna, dict) else {}


def _profile_text(channel: dict) -> str:
    dna = _channel_dna(channel)
    parts = [
        channel.get("topic", ""),
        channel.get("audience", ""),
        dna.get("audience", ""),
        dna.get("goal", ""),
        dna.get("offer", ""),
        " ".join(_as_list(dna.get("pain_points"))),
        " ".join(_as_list(dna.get("allowed_topic_types"))),
    ]
    return _low(" ".join(p for p in parts if p))


def is_kids_education_channel(channel: dict) -> bool:
    dna = _channel_dna(channel)
    if channel.get("archetype") in KIDS_EDU_ARCHETYPES:
        return True
    if dna and any(_low(dna.get(k)) for k in ("goal", "offer", "audience")):
        profile = _profile_text(channel)
        return any(marker in profile for marker in KIDS_EDU_PROFILE_MARKERS)
    profile = _profile_text(channel)
    return sum(1 for marker in KIDS_EDU_PROFILE_MARKERS if marker in profile) >= 2


def _looks_like_refusal(text: str) -> bool:
    low = _low(text)
    head = low[:700].lstrip(" \"'«»“”")
    if any(head.startswith(marker) for marker in REFUSAL_START_MARKERS):
        return True
    return any(marker in head for marker in META_REFUSAL_MARKERS)


def _blocked_content(text: str) -> bool:
    low = _low(text)
    return any(term in low for term in BLOCKED_TERMS)


def _intent_for(text: str) -> str:
    low = _low(text)
    if _blocked_content(low):
        return "illegal_facilitation"
    if any(w in low for w in ("урок", "обуч", "как ", "почему", "развива", "навык")):
        return "education"
    if any(w in low for w in ("запис", "пробн", "скидк", "акци", "whatsapp", "dm")):
        return "marketing"
    if any(w in low for w in ("новост", "релиз", "анонс", "direct")):
        return "news"
    if any(w in low for w in ("игр", "gaming", "nintendo", "steam")):
        return "entertainment"
    return "other"


def _kids_edu_reframe(raw_topic: str) -> tuple[str, str] | None:
    low = _low(raw_topic)
    if any(marker in low for marker in GAME_NEWS_MARKERS):
        safe = "Как интерес ребенка к играм направить в создание собственных проектов"
        angle = "объяснить родителям, как игры могут стать мостиком к робототехнике и программированию"
        return safe, angle
    if any(marker in low for marker in ADULT_TECH_NEWS_MARKERS):
        safe = "Почему детям полезно изучать программирование через понятные проекты"
        angle = "перевести взрослую AI/IT новость в пользу раннего развития логики, внимания и самостоятельности"
        return safe, angle
    return None


def _fits_kids_education(raw_topic: str) -> bool:
    low = _low(raw_topic)
    return any(marker in low for marker in KIDS_EDU_FIT_MARKERS)


def evaluate_topic_candidate(channel: dict, topic_data: dict) -> dict:
    """Safety Gate + Channel Fit Check for one raw candidate topic."""
    raw_topic = _clean_text(topic_data.get("topic", ""), 700)
    source = _clean_text(topic_data.get("source", "unknown"), 80)
    normalized = _clean_text(raw_topic, 500)
    intent = _intent_for(raw_topic)

    result = {
        "decision": "allowed",
        "intent": intent,
        "risk_level": "low",
        "raw_topic": raw_topic,
        "normalized_topic": normalized or None,
        "safe_topic": normalized or None,
        "safe_angle": None,
        "reason_code": "allowed",
        "notes": f"source={source}",
        "source": source,
    }

    if not normalized:
        result.update({
            "decision": "blocked",
            "risk_level": "medium",
            "safe_topic": None,
            "reason_code": "empty_topic",
            "notes": f"source={source}; empty candidate",
        })
        return result

    if _looks_like_refusal(raw_topic):
        result.update({
            "decision": "blocked",
            "risk_level": "medium",
            "safe_topic": None,
            "reason_code": "meta_or_refusal_topic",
            "notes": f"source={source}; candidate looks like model refusal/meta answer",
        })
        return result

    if _blocked_content(raw_topic):
        result.update({
            "decision": "blocked",
            "risk_level": "high",
            "safe_topic": None,
            "reason_code": "blocked_content",
            "notes": f"source={source}; candidate contains restricted content",
        })
        return result

    if is_kids_education_channel(channel):
        reframed = _kids_edu_reframe(raw_topic)
        if reframed:
            safe_topic, safe_angle = reframed
            result.update({
                "decision": "reframe",
                "risk_level": "low",
                "safe_topic": safe_topic,
                "safe_angle": safe_angle,
                "reason_code": "kids_education_reframe",
                "notes": f"source={source}; raw topic reframed for parent/education angle",
            })
            return result
        if not _fits_kids_education(raw_topic):
            result.update({
                "decision": "review",
                "risk_level": "medium",
                "safe_topic": None,
                "reason_code": "channel_fit_unclear",
                "notes": f"source={source}; unclear fit for kids education/local service channel",
            })
            return result
        result.update({
            "decision": "allowed_safe",
            "safe_angle": "связать тему с пользой для ребенка и вопросами родителей",
            "reason_code": "kids_education_fit",
        })
        return result

    return result


def build_content_brief(channel: dict, safety: dict, format_name: str | None = None) -> dict:
    """Rule-based Content Brief. Optional channel_dna enriches the old flow."""
    dna = _channel_dna(channel)
    audience = _clean_text(dna.get("audience") or channel.get("audience") or "аудитория канала", 220)
    goal = _clean_text(dna.get("goal") or "дать полезный пост по теме канала", 220)
    offer = _clean_text(dna.get("offer"), 220)
    cta = _clean_text(dna.get("cta"), 220)
    tone = _clean_text(dna.get("tone") or channel.get("tone") or "", 180)
    pain_points = _as_list(dna.get("pain_points"), 8)
    forbidden_angles = _as_list(dna.get("forbidden_angles"), 10)

    must_include = []
    if offer:
        must_include.append(f"оффер: {offer}")
    if pain_points:
        must_include.append("связать с болью аудитории: " + "; ".join(pain_points[:3]))
    if is_kids_education_channel(channel):
        must_include.append("показать пользу для ребенка или ответить на вопрос родителя")

    must_avoid = list(forbidden_angles)
    if is_kids_education_channel(channel):
        must_avoid.extend([
            "писать игровую новость как новость",
            "уходить во взрослую IT-карьеру без связи с детьми",
            "абстрактный AI/news тон без пользы для родителей",
        ])

    return {
        "topic": safety.get("safe_topic"),
        "angle": safety.get("safe_angle") or "раскрыть тему через пользу для аудитории канала",
        "target_reader": audience,
        "post_goal": goal,
        "must_include": must_include,
        "must_avoid": must_avoid,
        "cta": cta,
        "tone": tone,
        "format_constraints": [f"format={format_name}"] if format_name else [],
        "safety_reason_code": safety.get("reason_code"),
    }


def validate_generated_post(channel: dict, post: dict, safety: dict, brief: dict) -> dict:
    """Output Validator before buffer.add()."""
    content = _clean_text(post.get("content", ""), 5000)
    result = {
        "allowed": True,
        "decision": "allowed",
        "reason_code": "valid_post",
        "notes": "",
    }

    if not content:
        result.update({"allowed": False, "decision": "blocked", "reason_code": "empty_output"})
        return result

    if _looks_like_refusal(content):
        result.update({"allowed": False, "decision": "blocked", "reason_code": "meta_or_refusal_output"})
        return result

    if _blocked_content(content):
        result.update({"allowed": False, "decision": "blocked", "reason_code": "blocked_output_content"})
        return result

    if is_kids_education_channel(channel):
        low = _low(content)
        has_parent_or_child_context = any(
            marker in low for marker in ("ребен", "ребён", "дет", "родител", "занят", "обуч", "круж", "школ")
        )
        if not has_parent_or_child_context:
            result.update({
                "allowed": False,
                "decision": "review",
                "reason_code": "missing_kids_education_context",
                "notes": "no clear child/parent/education context in generated post",
            })
            return result

        cta = _low(brief.get("cta"))
        if cta:
            has_cta = any(marker in low for marker in (
                "напиш", "запис", "пробн", "whatsapp", "ватсап", "dm", "директ", "сообщ"
            ))
            if not has_cta:
                result.update({
                    "allowed": False,
                    "decision": "review",
                    "reason_code": "missing_required_cta",
                    "notes": "channel_dna CTA exists, but generated post has no visible CTA",
                })
                return result

        if any(marker in low for marker in GAME_NEWS_MARKERS) and not any(
            marker in low for marker in ("ребен", "ребён", "родител", "обуч", "созда")
        ):
            result.update({
                "allowed": False,
                "decision": "review",
                "reason_code": "gaming_news_drift",
                "notes": "post looks like generic gaming news for kids education channel",
            })
            return result

    return result


def dry_run_topic(channel: dict, raw_topic: str, source: str = "dry_run") -> dict:
    """Safe local dry-run helper: no LLM, no DB, no Telegram."""
    safety = evaluate_topic_candidate(channel, {"topic": raw_topic, "source": source})
    brief = None
    if safety.get("decision") not in {"blocked", "review"}:
        brief = build_content_brief(channel, safety)
    return {"safety": safety, "content_brief": brief}


def build_safe_channel_profile(analysis: dict) -> dict:
    """Normalize channel_analyzer output into a safe profile stored in channel cards."""
    topic = _clean_text(analysis.get("topic", ""), 600)
    archetype = _clean_text(analysis.get("archetype", "default"), 80) or "default"
    audience = _clean_text(analysis.get("audience", ""), 220)
    notes = _clean_text(analysis.get("analysis_notes", ""), 400)

    if analysis.get("forbidden") or _blocked_content(topic):
        return {
            "safe_topic": None,
            "archetype": archetype,
            "audience": audience,
            "allowed_topic_types": [],
            "forbidden_angles": [],
            "risk_level": "blocked",
            "supported": False,
            "notes": analysis.get("forbidden_reason") or "blocked by safe channel profile",
        }

    if not topic or _looks_like_refusal(topic):
        return {
            "safe_topic": None,
            "archetype": archetype,
            "audience": audience,
            "allowed_topic_types": [],
            "forbidden_angles": [],
            "risk_level": "review",
            "supported": False,
            "notes": "channel topic is empty or looks like meta/refusal output",
        }

    allowed_topic_types: list[str] = []
    forbidden_angles: list[str] = []
    profile = _low(" ".join([topic, archetype, audience, notes]))
    if archetype in KIDS_EDU_ARCHETYPES or any(m in profile for m in KIDS_EDU_PROFILE_MARKERS):
        allowed_topic_types = [
            "польза для ребенка",
            "обучение и развитие логики",
            "вопросы родителей",
            "пробное занятие и локальный оффер",
        ]
        forbidden_angles = [
            "игровые новости как новости",
            "взрослая IT-карьера без связи с детьми",
            "абстрактные AI/tech новости без пользы для родителей",
        ]

    return {
        "safe_topic": topic,
        "archetype": archetype,
        "audience": audience,
        "allowed_topic_types": allowed_topic_types,
        "forbidden_angles": forbidden_angles,
        "risk_level": "safe",
        "supported": True,
        "notes": notes,
    }
