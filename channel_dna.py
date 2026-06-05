"""Rule-based channel DNA builder for analyzed channel cards."""

from __future__ import annotations

import re
from typing import Any

from content_safety import KIDS_EDU_ARCHETYPES


UNKNOWN_FACTS_DEFAULT = [
    "price",
    "free_trial",
    "discount",
    "exact_dates",
    "address",
    "schedule",
    "age_range",
    "guaranteed_results",
    "concrete_time_to_result",
]

KIDS_EDU_PAIN_POINTS = [
    "ребенок много сидит в телефоне",
    "родитель не знает, с какого возраста начинать",
    "хочется полезное занятие",
    "нужно развивать логику, внимание и самостоятельность",
]

KIDS_EDU_ALLOWED_TOPIC_TYPES = [
    "польза занятий",
    "ответы на вопросы родителей",
    "развитие логики",
    "программирование для детей",
    "робототехника",
    "пробное занятие",
]

KIDS_EDU_FORBIDDEN_ANGLES = [
    "игровые новости",
    "релизы Nintendo/Steam/консолей",
    "взрослая IT-карьера",
    "абстрактные AI-новости",
    "сложные технические посты для программистов",
    "обещания гарантированного результата",
]

KIDS_EDU_MARKERS = (
    "робототех", "программирован", "дет", "ребен", "ребён", "школь",
    "занят", "круж", "секци", "лагер", "логик", "edtech",
)


def _clean_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].rstrip()


def _low(value: Any) -> str:
    return _clean_text(value, 3000).lower()


def _as_list(value: Any, limit: int = 12) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [_clean_text(v, 180) for v in value[:limit] if _clean_text(v, 180)]


def _joined_text(analysis: dict, posts_sample: list[str] | None, about: str) -> str:
    parts = [
        analysis.get("topic", ""),
        analysis.get("tone", ""),
        analysis.get("archetype", ""),
        analysis.get("analysis_notes", ""),
        analysis.get("export_channel_name", ""),
        about,
        " ".join(_as_list(analysis.get("evergreen_topics"), 10)),
    ]
    if posts_sample:
        parts.extend(_clean_text(p, 500) for p in posts_sample[:12])
    return "\n".join(p for p in parts if p)


def _is_kids_education_analysis(analysis: dict, text: str) -> bool:
    archetype = _clean_text(analysis.get("archetype", ""), 80)
    if archetype in KIDS_EDU_ARCHETYPES:
        return True
    low = _low(text)
    return sum(1 for marker in KIDS_EDU_MARKERS if marker in low) >= 2


def _find_known_facts(text: str) -> dict:
    low = _low(text)
    known: dict[str, Any] = {}

    age = re.search(r"(?:от\s*)?\d{1,2}\s*(?:[-–]\s*\d{1,2})?\s*(?:лет|года|год)", low)
    if age:
        known["age_range"] = age.group(0)

    price = re.search(r"\b\d[\d\s]*(?:₽|руб\.?|р\.)\b", low)
    if price:
        known["price"] = price.group(0)

    contact = re.search(r"(?:\+?\d[\d\s().-]{7,}\d|whatsapp|ватсап|wa\.me|t\.me/|@[a-z0-9_]{4,})", low)
    if contact:
        known["contact"] = contact.group(0)

    if "бесплат" in low and any(marker in low for marker in ("пробн", "урок", "занят")):
        known["free_trial"] = True
    if any(marker in low for marker in ("скидк", "акци")) or "%" in low:
        known["discount"] = True
    if "пробн" in low and any(marker in low for marker in ("урок", "занят", "консультац")):
        known["trial_lesson"] = True

    address = re.search(r"(?:ул\.|улиц|проспект|пр-т|переул|шоссе|бульвар|офис|кабинет|тц\s+)[^.\n,;]{3,80}", low)
    if address:
        known["address"] = address.group(0)

    schedule = re.search(r"(?:пн|вт|ср|чт|пт|сб|вс|понедельник|суббот|воскрес).*?\d{1,2}[:.]\d{2}", low)
    if schedule:
        known["schedule"] = schedule.group(0)

    date = re.search(r"\b\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\b", low)
    if date:
        known["exact_dates"] = date.group(0)

    directions = []
    for marker, label in (
        ("робототех", "робототехника"),
        ("программирован", "программирование"),
        ("логик", "логика"),
        ("конструкт", "конструирование"),
    ):
        if marker in low:
            directions.append(label)
    if directions:
        known["directions"] = sorted(set(directions))

    return known


def _unknown_facts(known_facts: dict) -> list[str]:
    unknown = []
    for fact in UNKNOWN_FACTS_DEFAULT:
        if fact == "free_trial":
            if known_facts.get("free_trial") is not True:
                unknown.append(fact)
        elif fact not in known_facts:
            unknown.append(fact)
    return unknown


def _needs_admin_questions(unknown_facts: list[str]) -> list[str]:
    questions = []
    if "price" in unknown_facts:
        questions.append("Есть ли цены или их нельзя упоминать?")
    if "free_trial" in unknown_facts:
        questions.append("Пробное занятие бесплатное или просто пробное?")
    if "address" in unknown_facts:
        questions.append("Можно ли указывать адрес или город?")
    if "schedule" in unknown_facts or "exact_dates" in unknown_facts:
        questions.append("Есть ли расписание, даты старта или смены?")
    return questions


def build_channel_dna(analysis: dict, posts_sample: list[str] | None = None, about: str = "") -> dict:
    """Build optional channel_dna from already analyzed channel data.

    The builder is conservative: it stores only explicit facts and leaves missing
    business details in unknown_facts so prompts and validators can avoid them.
    """
    text = _joined_text(analysis, posts_sample, about)
    safe_profile = analysis.get("safe_profile") if isinstance(analysis.get("safe_profile"), dict) else {}
    known_facts = _find_known_facts(text)
    unknown_facts = _unknown_facts(known_facts)
    kids_edu = _is_kids_education_analysis(analysis, text)

    if kids_edu:
        offer = ""
        profile_text = _low(text)
        if "робототех" in profile_text and "программирован" in profile_text:
            offer = "школа робототехники и программирования для детей"
        elif "робототех" in profile_text:
            offer = "занятия по робототехнике для детей"
        elif "программирован" in profile_text:
            offer = "занятия по программированию для детей"

        allowed = list(dict.fromkeys(
            KIDS_EDU_ALLOWED_TOPIC_TYPES
            + _as_list(safe_profile.get("allowed_topic_types"), 8)
        ))
        if "лагер" in profile_text:
            allowed.append("летний лагерь / секция")

        forbidden = list(dict.fromkeys(
            KIDS_EDU_FORBIDDEN_ANGLES
            + _as_list(safe_profile.get("forbidden_angles"), 8)
        ))

        return {
            "audience": "родители детей",
            "goal": "запись на пробное занятие / консультацию / подбор направления",
            "offer": offer,
            "locality": None,
            "tone": "теплый, понятный, уверенный, без взрослого IT-жаргона",
            "pain_points": KIDS_EDU_PAIN_POINTS,
            "allowed_topic_types": allowed,
            "forbidden_angles": forbidden,
            "cta": "напишите, подберем направление по возрасту",
            "known_facts": known_facts,
            "unknown_facts": unknown_facts,
            "confidence": "medium" if offer else "low",
            "needs_admin_questions": _needs_admin_questions(unknown_facts),
        }

    return {
        "audience": _clean_text(safe_profile.get("audience") or analysis.get("audience") or "аудитория канала", 220),
        "goal": "дать полезный пост по теме канала",
        "offer": "",
        "locality": None,
        "tone": _clean_text(analysis.get("tone", ""), 180),
        "pain_points": [],
        "allowed_topic_types": _as_list(safe_profile.get("allowed_topic_types"), 8),
        "forbidden_angles": _as_list(safe_profile.get("forbidden_angles"), 8),
        "cta": "",
        "known_facts": known_facts,
        "unknown_facts": unknown_facts,
        "confidence": "low",
        "needs_admin_questions": _needs_admin_questions(unknown_facts),
    }


def attach_channel_dna(analysis: dict, posts_sample: list[str] | None = None, about: str = "") -> dict:
    """Attach channel_dna to analysis when safe, preserving existing manual DNA."""
    if isinstance(analysis.get("channel_dna"), dict) and analysis["channel_dna"]:
        return analysis

    profile = analysis.get("safe_profile")
    if not isinstance(profile, dict):
        return analysis
    if profile.get("supported") is not True or profile.get("risk_level") == "blocked":
        return analysis

    analysis["channel_dna"] = build_channel_dna(analysis, posts_sample, about)
    return analysis
