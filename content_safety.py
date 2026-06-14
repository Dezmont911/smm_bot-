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
    "порно", "порнограф", "эроти", "хентай", "hentai", "нюдес", "нюдс",
    "nude", "nudes", "фетиш", "fetish", "наркотик", "казино", "ставк на спорт",
    "мошенн", "скам", "террор", "теракт", "экстрем", "оружие", "дрон",
    "ракета", "обстрел", "война", "украин", "лгбт",
    "военн", "войск", "всу", "минобор", "мобилизац", "фронт",
    "штурм", "солдат", "оборон", "красноармейск", "покровск", "запорож",
    "новоселов", "марочко", "госдум", "сенатор", "депутат", "законопроект",
    "санкц", "правозащит", "цензур",
)

BLOCKED_CONTENT_RE = re.compile(
    r"(?iu)(\bрф\b|\bвс\s+росси[ия]\b|\bмо\s+рф\b|\bсво\b|\bвсу\b)"
)

NON_RELAXABLE_BLOCKED_TERMS = (
    "порно", "порнограф", "эроти", "хентай", "hentai", "нюдес", "нюдс",
    "nude", "nudes", "фетиш", "fetish", "наркотик", "казино", "ставк на спорт",
    "мошенн", "скам", "террор", "теракт", "экстрем", "лгбт",
    "украин", "зеленск", "путин", "обстрел", "минобор", "мобилизац",
    "красноармейск", "покровск", "запорож", "новоселов", "марочко",
    "госдум", "сенатор", "депутат", "законопроект", "санкц",
    "правозащит", "цензур",
)

ENTERTAINMENT_ARCHETYPES = {
    "gaming", "gaming_casual", "gaming_esports", "anime", "memes",
    "movie", "movies", "cinema", "film", "films", "serials",
}

ENTERTAINMENT_PROFILE_MARKERS = (
    "кино", "фильм", "фильмы", "сериал", "сериалы", "киноклевер", "kinoclever",
    "movie", "movies", "film", "films", "cinema", "serial", "netflix",
    "аниме", "тайтл", "игр", "gaming", "steam", "playstation", "xbox",
)

ENTERTAINMENT_TEXT_MARKERS = (
    "фильм", "фильме", "фильма", "фильмы", "кино", "кинокартин", "сериал",
    "сериале", "сериала", "сезон", "серия", "сюжет", "персонаж", "герой",
    "героин", "сцена", "трейлер", "премьера", "режиссер", "режиссёр",
    "актер", "актёр", "актрис", "роль", "жанр", "франшиз", "боевик",
    "триллер", "хоррор", "фантастик", "комед", "драма", "аниме", "тайтл",
    "игра", "игре", "игры", "геймплей", "steam", "playstation", "xbox",
    "терминатор", "война миров", "звездные войны", "звёздные войны", "star wars",
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
    "пояснение:", "я оставил", "если нужна более развёрнут",
    "если нужна более развернут", "если нужна версия", "если нужна другая версия",
    "дай исходный текст", "если нужен исходный",
)

REFERENCE_META_OUTPUT_MARKERS = (
    "готов помочь переписать",
    "пришли полный текст",
    "пришли текст",
    "отправь текст",
    "вижу только эмодзи",
    "не вижу текста",
    "могу переписать",
    "я перепишу",
    "если пришлёшь",
    "дай исходный текст",
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

EXPLICIT_NON_KIDS_ARCHETYPES = {
    "gaming",
    "gaming_casual",
    "gaming_esports",
    "news",
    "tech_news",
    "auto",
    "music",
    "finance",
    "celeb_drama",
    "anime",
    "memes",
    "cats",
    "pets",
    "marketplace",
    "wb_product",
}

CELEB_DRAMA_ARCHETYPES = {"celeb_drama"}

CELEB_DRAMA_PROFILE_MARKERS = (
    "блогер", "блогерск", "инфлюенсер", "интернет-персон", "селеб", "звезд", "звёзд",
    "ютуб", "youtube", "тикток", "tiktok", "стример", "стрим", "соцсет", "telegram",
    "инстаграм", "instagram", "фанат", "подписчик", "роман", "расставан", "скандал",
    "коллаб", "карьер", "образ", "шоу-бизнес", "пев", "актёр", "актер", "артист",
)

CELEB_DRAMA_OFFTOPIC_MARKERS = (
    "нато", "главком", "военн", "армия", "военнослуж", "танк", "ввс", "днр", "лнр",
    "пригранич", "фронт", "европа", "запад", "совбез", "медведев", "госдум", "сенатор",
    "депутат", "законопроект", "судейство", "гимнаст", "студотряд", "всм", "самолет",
    "самолёт", "крушени", "убийств", "заподозри", "силовик", "полици", "маф", "рабств",
    "протез", "стоматолог", "зуб", "операци", "клиник", "правозащит", "цензур",
    "иноагент", "минюст", "колони", "удо", "уголовн", "арест", "задерж", "следств",
    "приговор", "тюрьм", "сизо", "прокурат", "суд ", "суда", "суде", "судеб",
    "штраф", "онколог", "химиотерап", "болезн", "диагноз", "госпитал",
)

CELEB_DRAMA_GENERIC_TOPIC_MARKERS = (
    "канал публикует",
    "новости о жизни российских блогеров",
    "новости о жизни блогеров",
    "личная жизнь блогеров",
    "личной жизни инфлюенсеров",
    "мифы вокруг",
    "блогеры тоже люди",
    "все блогеры",
    "жизнь инфлюенсеров",
    "инфлюенсеры в соцсетях",
    "контент-крейторы",
    "стоит ли блогерам",
    "как блогерам",
    "почему блогеры",
    "личное должно оставаться приватным",
    "интернет-издание",
    "публикующее новости",
)

CELEB_DRAMA_EVENT_MARKERS = (
    "попал", "попала", "прошёл", "прошла", "вошёл", "вошла", "forbes", "рейтинг",
    "ограб", "расстал", "рассталась", "развод", "роман", "свад", "отношени",
    "сменил", "сменила", "образ", "запустил", "запустила", "выпустил", "выпустила",
    "объявил", "объявила", "показал", "показала", "переехал", "переехала",
    "скандал", "конфликт", "обвинил", "обвинила", "извинил", "извинилась",
    "интервью", "шоу", "клип", "проект", "коллаб", "реакци", "фанат", "подписчик",
    "марафон", "курс", "бренд", "распрод", "вещ", "аккаунт", "взлом", "хакер",
    "потерял доступ", "потеряла доступ",
)

KIDS_EDU_PROFILE_MARKERS = (
    "робототех", "программирован", "дет", "ребен", "ребён", "школь",
    "занят", "круж", "секци", "лагер", "логик", "самостоятельн",
)

KIDS_EDU_FIT_MARKERS = (
    "робототех", "программирован", "ребен", "ребён", "дет", "родител",
    "обуч", "занят", "круж", "секци", "логик", "мышлен", "самостоятельн",
    "пробн", "школ", "лагер", "проект", "созда", "конструкт",
)

FREE_TRIAL_MARKERS = (
    "бесплатный урок", "бесплатное пробное", "первый урок бесплатно",
    "первое занятие бесплатно", "пробное бесплатно", "бесплатное занятие",
    "бесплатная консультация",
)

DISCOUNT_MARKERS = (
    "со скидкой", "скидка", "скидки", "акция", "акции", "%",
)

TIME_TO_RESULT_MARKERS = (
    "за месяц", "через месяц", "за несколько месяцев", "через несколько месяцев",
    "за пару занятий", "за несколько занятий", "в первый же день",
)

GUARANTEED_RESULT_MARKERS = (
    "гарантированно", "точно научится", "забудет о телефоне", "забывают про экран",
    "перестанет сидеть в телефоне", "обязательно научится",
)

ADDRESS_MARKERS = (
    "ул.", "улица", "проспект", "пр-т", "переулок", "шоссе", "бульвар", "офис", "кабинет",
)

PRICE_MARKERS = (
    "₽", "руб", "стоимость", "цена", "от 990", "от 1500",
)

MARKETPLACE_PRODUCT_LINK_MARKERS = (
    "wildberries.ru", "wb.ru", "ozon.ru", "ozon.onelink.me",
    "aliexpress", "ali.click", "market.yandex", "yandex.ru/cc",
    "reelsmarket.ru", "takprdm.ru", "dplnk.ru", "megamarket",
    "lamoda.ru", "kazanexpress.ru",
)

MARKETPLACE_OFFTOPIC_MARKERS = (
    "пост не совсем по нашей теме", "не совсем по нашей теме",
    "финансовая рекомендация", "комиссия для продавцов", "комиссию для продавцов",
    "для продавцов", "налоги", "логистика", "упаковка", "бензин",
    "цены на всё", "окно возможностей", "загляните на маркетплейсы",
)

MARKETPLACE_SERVICE_AD_MARKERS = (
    "стоматолог", "клиник", "имплант", "лечение зуб", "трансфер",
    "проживание", "путевка", "путёвка", "бесплатная линия",
    "ссылка на чат в whatsapp", "telegram / whatsapp",
)

IMPORT_AD_MARKERS = (
    "по вопросам рекламы", "erid", "ерид", "#ad", "промокод",
    "партнерский материал", "партнёрский материал", "рекламная интеграция",
    "бесплатная линия", "telegram / whatsapp", "ссылка на чат в whatsapp",
    "подпишись", "подписывайся", "наш канал", "больше в",
)

IMPORT_AD_WORD_RE = re.compile(
    r"(?iu)\b(max|розыгрыш\w*|разыгр\w*|конкурс\w*)\b"
)

NAV_ONLY_MARKERS = (
    "смотреть тут", "читать тут", "серия тут", "продолжение тут",
    "тут", "здесь", "по ссылке", "ссылка ниже",
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
    try:
        from channel_dna import get_effective_channel_dna
        return get_effective_channel_dna(channel) or {}
    except Exception as e:
        # Safe layer must fail closed on DNA: no optional DNA is better than poisoned DNA.
        return {}


def _unknown_facts(channel: dict) -> set[str]:
    dna = _channel_dna(channel)
    facts = dna.get("unknown_facts")
    if not isinstance(facts, (list, tuple)):
        return set()
    return {_clean_text(f, 80) for f in facts if _clean_text(f, 80)}


def _known_facts(channel: dict) -> dict:
    dna = _channel_dna(channel)
    facts = dna.get("known_facts")
    return facts if isinstance(facts, dict) else {}


def _known_age_groups_text(known: dict) -> str:
    groups = known.get("age_groups")
    if not isinstance(groups, list):
        return ""
    rendered = []
    for item in groups[:6]:
        if isinstance(item, dict):
            age = _clean_text(item.get("age"), 40)
            dirs = _as_list(item.get("directions"), 8)
            if age and dirs:
                rendered.append(f"{age}: {', '.join(dirs)}")
    return "; ".join(rendered)


def _known_directions(known: dict) -> list[str]:
    directions = _as_list(known.get("directions"), 20)
    for item in known.get("age_groups") or []:
        if isinstance(item, dict):
            directions.extend(_as_list(item.get("directions"), 8))
    result = []
    seen = set()
    for direction in directions:
        key = _low(direction)
        if key and key not in seen:
            seen.add(key)
            result.append(direction)
    return result[:20]


PROGRAM_FACT_INTENT_MARKERS = (
    "возраст", "с какого", "с каких", "когда начинать", "куда отдать",
    "выбрать", "выбор", "подобрать", "подбор", "подойдет", "подойдёт",
    "направлен", "группа", "уровень", "стартовать", "что выбрать",
    "lego", "wedo", "scratch", "ev3", "mindstorms",
)


def _should_include_program_facts(safety: dict, format_name: str | None = None) -> bool:
    text = _low(" ".join([
        safety.get("safe_topic") or "",
        safety.get("safe_angle") or "",
        format_name or "",
    ]))
    return any(marker in text for marker in PROGRAM_FACT_INTENT_MARKERS)


SPECIFIC_DIRECTION_MARKERS = (
    "lego wedo",
    "wedo",
    "wedo 2.0",
    "mindstorms",
    "ev3",
    "scratch",
    "roblox",
    "minecraft",
    "python",
    "unity",
    "разработка игр",
)


def _unsupported_direction_claim(known: dict, low_content: str) -> str | None:
    known_directions = _known_directions(known)
    if not known_directions:
        return None
    known_low = " | ".join(_low(direction) for direction in known_directions)
    for marker in SPECIFIC_DIRECTION_MARKERS:
        if marker in low_content and marker not in known_low:
            return marker
    return None


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


def _is_marketplace_channel(channel: dict) -> bool:
    return _low(channel.get("channel_type")) == "marketplace"


def is_kids_education_channel(channel: dict) -> bool:
    if _is_marketplace_channel(channel):
        return False
    if _clean_text(channel.get("archetype"), 80) in EXPLICIT_NON_KIDS_ARCHETYPES:
        return False
    dna = _channel_dna(channel)
    if channel.get("archetype") in KIDS_EDU_ARCHETYPES:
        return True
    if dna and any(_low(dna.get(k)) for k in ("goal", "offer", "audience")):
        profile = _profile_text(channel)
        return any(marker in profile for marker in KIDS_EDU_PROFILE_MARKERS)
    profile = _profile_text(channel)
    return sum(1 for marker in KIDS_EDU_PROFILE_MARKERS if marker in profile) >= 2


_FRAGMENTED_GENERATION_RE = re.compile(
    r"^\s*(?:please|sure|okay)[\s.!:,-]+(?:---+\s*)?\d{1,2}\s*/\s*\d{1,2}\s*[\.)]",
    re.IGNORECASE,
)
_NUMBERED_VARIANT_RE = re.compile(r"^\s*\d{1,2}\s*/\s*\d{1,2}\s*[\.)]\s+\S")


def _looks_like_fragmented_generation(text: str) -> bool:
    if not text:
        return False
    head = text[:300].strip()
    return bool(_FRAGMENTED_GENERATION_RE.search(head) or _NUMBERED_VARIANT_RE.search(head))


def _looks_like_refusal(text: str) -> bool:
    low = _low(text)
    head = low[:700].lstrip(" \"'«»“”")
    if any(head.startswith(marker) for marker in REFUSAL_START_MARKERS):
        return True
    if _looks_like_fragmented_generation(text):
        return True
    return any(marker in head for marker in META_REFUSAL_MARKERS + REFERENCE_META_OUTPUT_MARKERS)


def _blocked_content(text: str) -> bool:
    low = _low(text)
    return any(term in low for term in BLOCKED_TERMS) or bool(BLOCKED_CONTENT_RE.search(low))


def _is_entertainment_channel(channel: dict) -> bool:
    archetype = _low(channel.get("archetype"))
    if archetype in ENTERTAINMENT_ARCHETYPES:
        return True
    profile = _low(" ".join([
        str(channel.get("channel_id") or ""),
        str(channel.get("name") or ""),
        str(channel.get("topic") or ""),
        str(channel.get("tone") or ""),
    ]))
    return any(marker in profile for marker in ENTERTAINMENT_PROFILE_MARKERS)


def _looks_like_entertainment_context(text: str) -> bool:
    low = _low(text)
    return any(marker in low for marker in ENTERTAINMENT_TEXT_MARKERS)


def _blocked_content_for_channel(channel: dict, text: str) -> bool:
    low = _low(text)
    if not low:
        return False
    if any(term in low for term in NON_RELAXABLE_BLOCKED_TERMS) or bool(BLOCKED_CONTENT_RE.search(low)):
        return True
    if not any(term in low for term in BLOCKED_TERMS):
        return False
    if _is_entertainment_channel(channel) and _looks_like_entertainment_context(low):
        return False
    return True


def _intent_for(text: str, channel: dict | None = None) -> str:
    low = _low(text)
    if _blocked_content_for_channel(channel or {}, low):
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


def _is_celeb_drama_channel(channel: dict) -> bool:
    archetype = _low(channel.get("archetype"))
    if archetype in CELEB_DRAMA_ARCHETYPES:
        return True
    profile = _low(" ".join([
        str(channel.get("topic") or ""),
        str(channel.get("name") or ""),
    ]))
    return any(marker in profile for marker in CELEB_DRAMA_PROFILE_MARKERS)


def is_celeb_drama_channel(channel: dict) -> bool:
    """Public helper for source-routing decisions outside the safety module."""
    return _is_celeb_drama_channel(channel)


def _has_person_like_mention(text: str) -> bool:
    raw = _clean_text(text, 700)
    if re.search(r"@[a-zA-Z0-9_]{4,}", raw):
        return True
    return bool(re.search(r"\b[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]{2,}\b", raw))


def _is_generic_celeb_drama_topic(raw_topic: str) -> bool:
    low = _low(raw_topic)
    return any(marker in low for marker in CELEB_DRAMA_GENERIC_TOPIC_MARKERS)


def _fits_celeb_drama(raw_topic: str) -> bool:
    low = _low(raw_topic)
    if _has_celeb_drama_offtopic(raw_topic):
        return False
    if _is_generic_celeb_drama_topic(raw_topic):
        return False

    has_profile_marker = any(marker in low for marker in CELEB_DRAMA_PROFILE_MARKERS)
    has_event_marker = any(marker in low for marker in CELEB_DRAMA_EVENT_MARKERS)
    has_person = _has_person_like_mention(raw_topic)

    # Для новостей блогеров нужна конкретика: персонаж/ник + событие.
    # Иначе генератор превращает тему в абстрактный пост "про блогеров вообще".
    if has_profile_marker and (has_event_marker or has_person):
        return True
    if has_person and has_event_marker:
        return True
    return False


def _has_celeb_drama_offtopic(text: str) -> bool:
    low = _low(text)
    return any(marker in low for marker in CELEB_DRAMA_OFFTOPIC_MARKERS)


def evaluate_topic_candidate(channel: dict, topic_data: dict) -> dict:
    """Safety Gate + Channel Fit Check for one raw candidate topic."""
    raw_topic = _clean_text(topic_data.get("topic", ""), 700)
    source = _clean_text(topic_data.get("source", "unknown"), 80)
    normalized = _clean_text(raw_topic, 500)
    intent = _intent_for(raw_topic, channel)

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

    if _blocked_content_for_channel(channel, raw_topic):
        result.update({
            "decision": "blocked",
            "risk_level": "high",
            "safe_topic": None,
            "reason_code": "blocked_content",
            "notes": f"source={source}; candidate contains restricted content",
        })
        return result

    if _is_marketplace_channel(channel):
        result.update({
            "decision": "allowed_safe",
            "safe_angle": "сохранить товарную пользу и реальную ссылку на товар",
            "reason_code": "marketplace_product_fit",
            "notes": f"source={source}; marketplace channel",
        })
        return result

    if _is_celeb_drama_channel(channel):
        if not _fits_celeb_drama(raw_topic):
            result.update({
                "decision": "review",
                "risk_level": "medium",
                "safe_topic": None,
                "reason_code": "celeb_drama_fit_unclear",
                "notes": f"source={source}; unclear fit for blogger/celebrity news channel",
            })
            return result
        result.update({
            "decision": "allowed_safe",
            "safe_angle": "оставить только блогерский/селебрити-контекст без политики, криминала, медицины и общих новостей",
            "reason_code": "celeb_drama_fit",
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
    unknown_facts = _unknown_facts(channel)
    known_facts = _known_facts(channel)
    known_age_groups = _known_age_groups_text(known_facts)
    known_directions = _known_directions(known_facts)
    include_program_facts = _should_include_program_facts(safety, format_name)
    program_fact_labels = []

    must_include = []
    if offer:
        must_include.append(f"оффер: {offer}")
    if pain_points:
        must_include.append("связать с болью аудитории: " + "; ".join(pain_points[:3]))
    if is_kids_education_channel(channel):
        must_include.append("показать пользу для ребенка или ответить на вопрос родителя")
    if known_age_groups:
        if include_program_facts:
            must_include.append("подтвержденные возрастные группы: " + known_age_groups)
        else:
            program_fact_labels.append("возрастные группы")
    if known_directions:
        if include_program_facts:
            must_include.append("подтвержденные направления: " + ", ".join(known_directions))
        else:
            program_fact_labels.append("направления")

    must_avoid = list(forbidden_angles)
    if program_fact_labels:
        must_avoid.append(
            "не перечислять " + " и ".join(program_fact_labels) +
            " из анкеты без прямой необходимости; использовать их только если пост прямо про возраст, "
            "выбор направления или подбор программы"
        )
    if dna:
        must_avoid.append(
            "не указывать цены, бесплатность, скидки, адреса, даты, расписание, сроки и "
            "гарантированные результаты, если этого нет в known_facts"
        )
    if "free_trial" in unknown_facts:
        must_avoid.append("не писать, что пробное занятие или первый урок бесплатные")
    if "discount" in unknown_facts:
        must_avoid.append("не упоминать скидки, акции и проценты")
    if "concrete_time_to_result" in unknown_facts:
        must_avoid.append("не обещать результат за месяц, за несколько месяцев или за пару занятий")
    if "guaranteed_results" in unknown_facts:
        must_avoid.append("не обещать гарантированный результат или что ребенок точно забудет о телефоне")
    if "price" in unknown_facts:
        must_avoid.append("не указывать цены или скидки")
    if {"price", "free_trial", "discount"} & unknown_facts:
        must_avoid.append(
            "не упоминать бесплатность, скидки, акции и цены; вместо этого писать: "
            "актуальные условия подскажем в WhatsApp или подберем направление по возрасту"
        )
    if "age_range" in unknown_facts and not known_age_groups:
        must_avoid.append("не указывать конкретные возрастные диапазоны, если age_range нет в known_facts")
    if "address" in unknown_facts:
        must_avoid.append("не указывать адрес")
    if "schedule" in unknown_facts or "exact_dates" in unknown_facts:
        must_avoid.append("не указывать расписание, даты старта или точные даты")
    if "родител" in _low(audience):
        must_avoid.append(
            "обращайся к родителям на 'вы'; не используй повелительные формы в стиле "
            "'ищи', 'определи', 'выбирай' как обращение к ребенку/подростку"
        )
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


def _unsupported_fact_claim(channel: dict, content: str) -> str | None:
    if not _channel_dna(channel):
        return None

    known = _known_facts(channel)
    low = _low(content)
    unsupported_direction = _unsupported_direction_claim(known, low)
    if unsupported_direction:
        return "directions"

    unknown = _unknown_facts(channel)
    if not unknown:
        return None

    if "free_trial" in unknown and not known.get("free_trial") and any(marker in low for marker in FREE_TRIAL_MARKERS):
        return "free_trial"
    if "discount" in unknown and not known.get("discount") and any(marker in low for marker in DISCOUNT_MARKERS):
        return "discount"
    if "concrete_time_to_result" in unknown and any(marker in low for marker in TIME_TO_RESULT_MARKERS):
        return "concrete_time_to_result"
    if "guaranteed_results" in unknown and any(marker in low for marker in GUARANTEED_RESULT_MARKERS):
        return "guaranteed_results"
    if "price" in unknown and not known.get("price") and (
        any(marker in low for marker in PRICE_MARKERS)
        or re.search(r"\b(?:от\s*)?\d[\d\s]*(?:₽|руб\.?|р\.)\b", low)
    ):
        return "price"
    if "exact_dates" in unknown and re.search(
        r"\b\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\b",
        low,
    ):
        return "exact_dates"
    if "schedule" in unknown and re.search(
        r"(?:пн|вт|ср|чт|пт|сб|вс|понедельник|суббот|воскрес).*?\d{1,2}[:.]\d{2}",
        low,
    ):
        return "schedule"
    if "age_range" in unknown and not known.get("age_range") and not known.get("age_groups") and re.search(
        r"(?:\b\d{1,2}\s*[-–]\s*\d{1,2}\s*(?:лет|года|год)\b|\b(?:после|с|от)\s*\d{1,2}\s*(?:лет|года|год)?\b)",
        low,
    ):
        return "age_range"
    if "address" in unknown and not known.get("address") and any(marker in low for marker in ADDRESS_MARKERS):
        return "address"
    return None


def _requires_marketplace_link(channel: dict, post: dict) -> bool:
    fmt = _low(post.get("format"))
    return (
        fmt == "wb_product"
        or (
            channel.get("channel_type") == "marketplace"
            and fmt in {"manual", "reference", "marketplace", "wb_product"}
        )
    )


def _html_links(content: str) -> list[str]:
    return re.findall(r'<a\s+href=["\'](https?://[^"\']+)["\']', content or "", re.IGNORECASE)


def _is_marketplace_product_url(url: str) -> bool:
    low = (url or "").lower()
    return any(marker in low for marker in MARKETPLACE_PRODUCT_LINK_MARKERS)


def _is_forbidden_marketplace_reference_link(url: str) -> bool:
    low = (url or "").lower()
    if _is_marketplace_product_url(low):
        return False
    return True


def _plain_from_html(content: str) -> str:
    text = re.sub(r"<a\b[^>]*>(.*?)</a>", r"\1", content or "", flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"https?://\S+", " ", text, flags=re.IGNORECASE)
    return _clean_text(text, 5000)


def _has_html_link(content: str) -> bool:
    return bool(_html_links(content))


def _has_marketplace_product_link(content: str) -> bool:
    links = _html_links(content)
    if not links:
        return False
    return any(_is_marketplace_product_url(url) for url in links)


def _has_forbidden_marketplace_reference_link(content: str) -> bool:
    links = _html_links(content)
    return any(_is_forbidden_marketplace_reference_link(url) for url in links)


def _looks_like_marketplace_offtopic(content: str) -> bool:
    low = _low(content)
    return any(marker in low for marker in MARKETPLACE_OFFTOPIC_MARKERS + MARKETPLACE_SERVICE_AD_MARKERS)


def _looks_like_import_ad_or_offtopic(content: str) -> bool:
    low = _plain_from_html(content).lower()
    return bool(IMPORT_AD_WORD_RE.search(low)) or any(
        marker in low
        for marker in IMPORT_AD_MARKERS + MARKETPLACE_OFFTOPIC_MARKERS + MARKETPLACE_SERVICE_AD_MARKERS
    )


def _looks_like_navigation_only(content: str, has_media: bool) -> bool:
    plain = _plain_from_html(content).lower().strip(" .,:;!؟?—-–«»\"'")
    if not plain:
        return False
    if plain in NAV_ONLY_MARKERS:
        return True
    if not has_media and len(plain) < 25 and any(marker in plain for marker in NAV_ONLY_MARKERS):
        return True
    return False


def validate_imported_post(channel: dict, post: dict) -> dict:
    """Lightweight guard for manual forwards and reference imports before buffer.add()."""
    content = post.get("content") or ""
    media_type = post.get("media_type")
    has_media = bool(media_type or post.get("tg_file_id") or post.get("media_path"))
    result = {"allowed": True, "decision": "allowed", "reason_code": "valid_imported_post", "notes": ""}

    if not content and not has_media:
        result.update({"allowed": False, "decision": "blocked", "reason_code": "empty_imported_post"})
        return result

    if post.get("format") == "reference" and has_media and not content and not post.get("allow_media_only"):
        result.update({
            "allowed": False,
            "decision": "review",
            "reason_code": "media_only_reference_no_text",
            "notes": "reference media has no text, so text-only safety cannot classify it",
        })
        return result

    if content and _looks_like_refusal(content):
        result.update({"allowed": False, "decision": "blocked", "reason_code": "meta_or_refusal_output"})
        return result

    if content and _blocked_content_for_channel(channel, content):
        result.update({"allowed": False, "decision": "blocked", "reason_code": "blocked_imported_content"})
        return result

    if content and _looks_like_navigation_only(content, has_media):
        result.update({"allowed": False, "decision": "review", "reason_code": "navigation_only_import"})
        return result

    if content and _looks_like_import_ad_or_offtopic(content):
        result.update({"allowed": False, "decision": "review", "reason_code": "import_ad_or_offtopic"})
        return result

    if _requires_marketplace_link(channel, post) and not _has_html_link(content):
        result.update({
            "allowed": False,
            "decision": "review",
            "reason_code": "missing_marketplace_link",
            "notes": "marketplace imported post has no real <a href> link",
        })
        return result

    if _requires_marketplace_link(channel, post) and not _has_marketplace_product_link(content):
        result.update({
            "allowed": False,
            "decision": "review",
            "reason_code": "missing_marketplace_product_link",
            "notes": "marketplace imported post has links, but no recognized product/marketplace link",
        })
        return result

    if _requires_marketplace_link(channel, post) and _has_forbidden_marketplace_reference_link(content):
        result.update({
            "allowed": False,
            "decision": "review",
            "reason_code": "forbidden_marketplace_reference_link",
            "notes": "marketplace imported post contains non-product/ad/donor link",
        })
        return result

    if channel.get("channel_type") == "marketplace" and content and _looks_like_marketplace_offtopic(content):
        result.update({
            "allowed": False,
            "decision": "review",
            "reason_code": "marketplace_offtopic_or_service_ad",
        })
        return result

    return result


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

    if _blocked_content_for_channel(channel, content):
        result.update({"allowed": False, "decision": "blocked", "reason_code": "blocked_output_content"})
        return result

    if _looks_like_import_ad_or_offtopic(content):
        result.update({"allowed": False, "decision": "review", "reason_code": "ad_or_offtopic_output"})
        return result

    if _is_celeb_drama_channel(channel) and _has_celeb_drama_offtopic(content):
        result.update({"allowed": False, "decision": "review", "reason_code": "celeb_drama_offtopic_output"})
        return result

    if _requires_marketplace_link(channel, post) and not _has_html_link(content):
        result.update({
            "allowed": False,
            "decision": "review",
            "reason_code": "missing_marketplace_link",
            "notes": "marketplace/reference post has no real <a href> link",
        })
        return result

    if _requires_marketplace_link(channel, post) and not _has_marketplace_product_link(content):
        result.update({
            "allowed": False,
            "decision": "review",
            "reason_code": "missing_marketplace_product_link",
            "notes": "marketplace post has links, but no recognized product/marketplace link",
        })
        return result

    if _requires_marketplace_link(channel, post) and _has_forbidden_marketplace_reference_link(content):
        result.update({
            "allowed": False,
            "decision": "review",
            "reason_code": "forbidden_marketplace_reference_link",
            "notes": "marketplace/reference post contains non-product/ad/donor link",
        })
        return result

    if channel.get("channel_type") == "marketplace" and _looks_like_marketplace_offtopic(content):
        result.update({
            "allowed": False,
            "decision": "review",
            "reason_code": "marketplace_offtopic_or_service_ad",
            "notes": "marketplace post looks like service ad or advisory content, not a product card",
        })
        return result

    if not _requires_marketplace_link(channel, post):
        unsupported_fact = _unsupported_fact_claim(channel, content)
    else:
        unsupported_fact = None
    if unsupported_fact:
        result.update({
            "allowed": False,
            "decision": "review",
            "reason_code": "unsupported_claim_or_unknown_fact",
            "notes": f"generated post mentions unknown fact: {unsupported_fact}",
        })
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

    if analysis.get("forbidden") or _blocked_content_for_channel(analysis, topic):
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
