"""Channel DNA questionnaire helpers.

The module keeps raw questionnaire text out of generation prompts. UI code
stores only normalized facts returned from these helpers.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from content_safety import _blocked_content, _clean_text, _looks_like_refusal, is_kids_education_channel


SUPPORTED_QUESTIONNAIRE_ARCHETYPES = {
    "kids_education",
    "local_service",
    "parent_marketing",
    "edtech",
    "hobby_school",
}

QUESTIONNAIRE_TEMPLATE = """Возрастные группы:
4-6: Lego WeDo, Lego WeDo 2.0
с 7: Lego Mindstorms EV3, разработка игр

Город:
WhatsApp или телефон:
Адрес:
Пробное занятие: есть / нет / не знаю
Бесплатное пробное: да / нет / не знаю
Цены/скидки: не указывать / условия в WhatsApp / есть цены
CTA: написать в WhatsApp / позвонить / написать в Telegram
Нельзя обещать: гарантированный результат, результат за месяц, отучим от телефона"""


_FIELD_ALIASES = {
    "age_groups": ("возраст", "направлен", "групп"),
    "city": ("город",),
    "contact": ("whatsapp", "ватсап", "телефон", "контакт"),
    "address": ("адрес",),
    "trial_lesson": ("пробное занятие",),
    "free_trial": ("бесплатное пробное", "бесплатность", "бесплатно"),
    "price_policy": ("цены", "скидки", "стоимость"),
    "cta": ("cta", "призыв", "кнопка", "действие"),
    "forbidden_promises": ("нельзя обещать", "запрещено обещать", "обещания"),
}

_AGE_RE = re.compile(
    r"^(?P<age>(?:\d{1,2}\s*[-–—]\s*\d{1,2}|с\s*\d{1,2}|от\s*\d{1,2}|\d{1,2}\+))\s*[:—-]\s*(?P<directions>.+)$",
    re.IGNORECASE,
)

_SPECIFIC_DIRECTION_MARKERS = (
    "lego wedo",
    "wedo",
    "wedo 2.0",
    "mindstorms",
    "ev3",
    "scratch",
    "roblox",
    "minecraft",
    "python",
    "разработка игр",
    "unity",
)


def questionnaire_supported(channel: dict) -> bool:
    if (channel or {}).get("channel_type") == "marketplace":
        return False
    if (channel or {}).get("archetype") in SUPPORTED_QUESTIONNAIRE_ARCHETYPES:
        return True
    return is_kids_education_channel(channel or {})


def validate_questionnaire_input(field: str, value: Any, channel: dict | None = None) -> dict:
    field = (field or "").strip()
    raw_source = str(value or "").strip()
    raw = _clean_text(raw_source, 5000)
    if not raw:
        return _err("empty", "Поле пустое.")
    if len(raw) > 900:
        return _err("too_long", "Слишком длинно. Оставь только факты, без простыни текста.")
    if _blocked_content(raw) or _looks_like_refusal(raw):
        return _err("blocked", "Текст не похож на факты канала или содержит запрещенную тему.")

    if field == "age_groups":
        return _validate_age_groups(raw_source)
    if field == "city":
        city = re.sub(r"\s+", " ", raw).strip()
        if not re.fullmatch(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\s-]{1,39}", city):
            return _err("invalid_city", "Город должен быть обычным названием без ссылок и мусора.")
        return _ok(city)
    if field in {"contact", "phone", "whatsapp"}:
        return _validate_contact(raw)
    if field == "address":
        return _ok(raw[:220])
    if field == "trial_lesson":
        return _validate_enum(raw, {
            "есть": True,
            "да": True,
            "нет": False,
            "не знаю": None,
            "неизвестно": None,
        }, "Некорректно. Напиши: есть, нет или не знаю.")
    if field == "free_trial":
        return _validate_enum(raw, {
            "да": True,
            "есть": True,
            "бесплатно": True,
            "нет": False,
            "платно": False,
            "не знаю": None,
            "неизвестно": None,
        }, "Некорректно. Напиши: да, нет или не знаю.")
    if field == "price_policy":
        return _validate_enum(raw, {
            "не указывать": "do_not_mention",
            "условия в whatsapp": "conditions_in_whatsapp",
            "условия в ватсап": "conditions_in_whatsapp",
            "есть цены": "has_prices",
            "не знаю": None,
        }, "Некорректно. Напиши: не указывать, условия в WhatsApp, есть цены или не знаю.")
    if field == "cta":
        return _validate_enum(raw, {
            "написать в whatsapp": "write_whatsapp",
            "написать в ватсап": "write_whatsapp",
            "позвонить": "call",
            "написать в telegram": "write_telegram",
            "написать в телеграм": "write_telegram",
        }, "Некорректно. Выбери CTA из шаблона.")
    if field == "forbidden_promises":
        items = _split_items(raw, 12)
        if not items:
            return _err("invalid_list", "Нужно перечислить, что нельзя обещать.")
        return _ok(items)
    return _err("unknown_field", "Неизвестное поле анкеты.")


def parse_questionnaire_text(text: str, channel: dict | None = None) -> dict:
    answers: dict[str, Any] = {}
    errors: list[str] = []
    current: str | None = None
    bucket: list[str] = []

    def flush():
        nonlocal bucket, current
        if not current:
            bucket = []
            return
        value = "\n".join(x for x in bucket if x.strip()).strip()
        if value:
            result = validate_questionnaire_input(current, value, channel)
            if result["ok"]:
                answers[current] = result["normalized"]
            else:
                errors.append(f"{_field_label(current)}: {result['message']}")
        bucket = []

    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        key, value = _split_key_value(line)
        detected = _detect_field(key) if key else None
        if detected:
            flush()
            current = detected
            if value:
                bucket.append(value)
            continue
        if _AGE_RE.match(line):
            if current != "age_groups":
                flush()
                current = "age_groups"
            bucket.append(line)
            continue
        if current:
            bucket.append(line)
    flush()

    if not answers and not errors:
        result = validate_questionnaire_input("age_groups", text, channel)
        if result["ok"]:
            answers["age_groups"] = result["normalized"]
        else:
            errors.append(result["message"])

    return {"ok": bool(answers) and not errors, "answers": answers, "errors": errors}


def build_proposed_channel_dna(existing_dna: dict | None, questionnaire_answers: dict) -> dict:
    dna = copy.deepcopy(existing_dna or {})
    known = copy.deepcopy(dna.get("known_facts") if isinstance(dna.get("known_facts"), dict) else {})
    source = copy.deepcopy(dna.get("known_facts_source") if isinstance(dna.get("known_facts_source"), dict) else {})
    unknown = set(dna.get("unknown_facts") if isinstance(dna.get("unknown_facts"), list) else [])

    def set_fact(name: str, value: Any, remove_unknown: tuple[str, ...] = ()):
        if value is None or value == "" or value == []:
            return
        known[name] = value
        source[name] = "questionnaire"
        for key in remove_unknown:
            unknown.discard(key)

    age_groups = questionnaire_answers.get("age_groups")
    if isinstance(age_groups, list) and age_groups:
        directions: list[str] = []
        for item in age_groups:
            if isinstance(item, dict):
                directions.extend(item.get("directions") or [])
        set_fact("age_groups", age_groups, ("age_range", "age_groups"))
        set_fact("directions", _dedupe(directions), ("directions",))

    set_fact("city", questionnaire_answers.get("city"), ("city",))
    set_fact("contact", questionnaire_answers.get("contact"), ("contact",))
    set_fact("address", questionnaire_answers.get("address"), ("address",))

    if "trial_lesson" in questionnaire_answers and questionnaire_answers["trial_lesson"] is not None:
        set_fact("trial_lesson", questionnaire_answers["trial_lesson"], ("trial_lesson",))
    if "free_trial" in questionnaire_answers and questionnaire_answers["free_trial"] is not None:
        set_fact("free_trial", questionnaire_answers["free_trial"], ("free_trial",))
    if "price_policy" in questionnaire_answers and questionnaire_answers["price_policy"]:
        set_fact("price_policy", questionnaire_answers["price_policy"], ())

    cta = questionnaire_answers.get("cta")
    if cta:
        dna["cta"] = _cta_text(cta)
        source["cta"] = "questionnaire"

    forbidden = questionnaire_answers.get("forbidden_promises")
    if isinstance(forbidden, list) and forbidden:
        existing = dna.get("forbidden_angles") if isinstance(dna.get("forbidden_angles"), list) else []
        dna["forbidden_angles"] = _dedupe([*existing, *forbidden])[:20]
        source["forbidden_promises"] = "questionnaire"

    dna["known_facts"] = known
    dna["known_facts_source"] = source
    if unknown:
        dna["unknown_facts"] = sorted(unknown)
    else:
        dna["unknown_facts"] = []
    dna.setdefault("confidence", "high")
    return dna


def diagnostics_for_channel(channel: dict) -> dict:
    dna = channel.get("channel_dna") if isinstance(channel.get("channel_dna"), dict) else {}
    known = dna.get("known_facts") if isinstance(dna.get("known_facts"), dict) else {}
    unknown = dna.get("unknown_facts") if isinstance(dna.get("unknown_facts"), list) else []
    warnings = []
    if questionnaire_supported(channel):
        if not known.get("age_groups"):
            warnings.append("не заполнены возрастные группы")
        if not known.get("directions"):
            warnings.append("не заполнены направления")
        if not known.get("city"):
            warnings.append("не указан город")
        if not known.get("contact"):
            warnings.append("не указан WhatsApp/телефон")
    if unknown:
        warnings.append("есть неизвестные факты: " + ", ".join(str(x) for x in unknown[:6]))
    return {"dna": dna, "known_facts": known, "unknown_facts": unknown, "warnings": warnings}


def format_known_facts(known: dict, limit: int = 8) -> list[str]:
    lines: list[str] = []
    age_groups = known.get("age_groups")
    if isinstance(age_groups, list) and age_groups:
        rendered = []
        for item in age_groups[:4]:
            if isinstance(item, dict):
                rendered.append(f"{item.get('age')}: {', '.join(item.get('directions') or [])}")
        if rendered:
            lines.append("Возраст/направления: " + "; ".join(rendered))
    for key, label in (
        ("city", "Город"),
        ("contact", "Контакт"),
        ("address", "Адрес"),
        ("trial_lesson", "Пробное занятие"),
        ("free_trial", "Бесплатное пробное"),
        ("price_policy", "Цены/скидки"),
    ):
        if key in known:
            lines.append(f"{label}: {_human_value(known[key])}")
    return lines[:limit]


def _validate_age_groups(raw: str) -> dict:
    entries = []
    for part in re.split(r"[\n;]+", raw):
        line = part.strip()
        if not line:
            continue
        match = _AGE_RE.match(line)
        if not match:
            return _err("invalid_age_group", "Возрастные группы нужны в формате: 4-6: Lego WeDo, с 7: EV3.")
        age = _normalize_age(match.group("age"))
        directions = _split_items(match.group("directions"), 8)
        if not directions:
            return _err("empty_directions", "После возраста нужны направления.")
        entries.append({"age": age, "directions": directions})
    if not entries:
        return _err("empty", "Возрастные группы не найдены.")
    return _ok(entries)


def _validate_contact(raw: str) -> dict:
    if raw.lower().startswith(("http://", "https://")) and not any(x in raw.lower() for x in ("wa.me", "t.me")):
        return _err("invalid_contact", "Контакт должен быть телефоном, WhatsApp или Telegram.")
    digits = re.sub(r"\D+", "", raw)
    if digits and 10 <= len(digits) <= 15:
        if raw.strip().startswith("+"):
            return _ok("+" + digits)
        return _ok(digits)
    if re.search(r"(wa\.me|t\.me|telegram|whatsapp|ватсап)", raw, re.IGNORECASE):
        return _ok(raw[:120])
    return _err("invalid_contact", "Контакт должен быть телефоном, WhatsApp или Telegram.")


def _validate_enum(raw: str, mapping: dict[str, Any], message: str) -> dict:
    key = _clean_text(raw, 120).lower().replace("ё", "е")
    key = re.sub(r"\s+", " ", key).strip(" .")
    if key in mapping:
        return _ok(mapping[key])
    return _err("invalid_enum", message)


def _split_items(raw: str, limit: int) -> list[str]:
    items = []
    for part in re.split(r"[,;\n]+", raw):
        item = _clean_text(part, 80)
        if item:
            items.append(item)
    return _dedupe(items)[:limit]


def _split_key_value(line: str) -> tuple[str | None, str]:
    match = re.match(r"^([^:：]{2,50})[:：]\s*(.*)$", line)
    if not match:
        return None, line
    return match.group(1).strip(), match.group(2).strip()


def _detect_field(key: str | None) -> str | None:
    low = _clean_text(key, 80).lower().replace("ё", "е")
    for field, aliases in _FIELD_ALIASES.items():
        if any(alias in low for alias in aliases):
            return field
    return None


def _normalize_age(value: str) -> str:
    age = _clean_text(value, 40).lower().replace("—", "–").replace("-", "–")
    age = re.sub(r"\s*–\s*", "–", age)
    age = re.sub(r"\s+", " ", age)
    return age


def _dedupe(values: list[Any]) -> list[Any]:
    result = []
    seen = set()
    for value in values:
        key = repr(value).lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _cta_text(value: str) -> str:
    return {
        "write_whatsapp": "напишите в WhatsApp",
        "call": "позвоните",
        "write_telegram": "напишите в Telegram",
    }.get(value, str(value))


def _human_value(value: Any) -> str:
    if value is True:
        return "да"
    if value is False:
        return "нет"
    if value == "conditions_in_whatsapp":
        return "условия в WhatsApp"
    if value == "do_not_mention":
        return "не указывать"
    if value == "has_prices":
        return "цены указаны"
    return str(value)


def _field_label(field: str) -> str:
    return {
        "age_groups": "Возрастные группы",
        "city": "Город",
        "contact": "Контакт",
        "address": "Адрес",
        "trial_lesson": "Пробное занятие",
        "free_trial": "Бесплатное пробное",
        "price_policy": "Цены/скидки",
        "cta": "CTA",
        "forbidden_promises": "Нельзя обещать",
    }.get(field, field)


def _ok(normalized: Any) -> dict:
    return {"ok": True, "normalized": normalized, "reason_code": None, "message": ""}


def _err(reason_code: str, message: str) -> dict:
    return {"ok": False, "normalized": None, "reason_code": reason_code, "message": message}
