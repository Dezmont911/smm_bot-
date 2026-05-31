"""
ui.py — Inline-меню бота (UI слой)

Архитектура: каждый «экран» — отдельная async функция screen_XXX().
Навигация: callback_data начинается с "ui:" и описывает экран + параметры.
Постоянная кнопка «☰ Меню» (ReplyKeyboard) всегда видна внизу экрана.

Схема callback_data:
  ui:main                     → главное меню
  ui:channels                 → список каналов
  ui:ch:@handle               → карточка канала
  ui:ch_settings:@handle      → настройки канала
  ui:ch_pause:@handle         → переключить паузу
  ui:ch_delete:@handle        → запрос подтверждения удаления
  ui:ch_delete_ok:@handle     → подтверждённое удаление
  ui:ch_generate:@handle      → запустить генерацию
  ui:ch_postnow:@handle       → опубликовать следующий пост
  ui:ch_review:@handle        → очередь постов канала
  ui:ch_set:@handle:field     → начать редактирование поля
  ui:status                   → общий статус буферов
  ui:generate_all             → сгенерировать для всех каналов
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from loguru import logger

from buffer_manager import buffer
from config import cfg
from content_generator import generator
from poster import poster


# ── Постоянная ReplyKeyboard с кнопкой «Меню» ─────────────────────────────

MENU_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("☰ Меню")]],
    resize_keyboard=True,
    is_persistent=True,
)


# ── Вспомогательные функции ────────────────────────────────────────────────

def _load_channels(include_inactive: bool = False) -> list[dict]:
    """Загружает все карточки каналов из папки channels/."""
    channels_dir = Path(__file__).parent / "channels"
    channels = []
    for f in channels_dir.glob("*.json"):
        if f.name.startswith("example_"):
            continue
        try:
            ch = json.loads(f.read_text(encoding="utf-8"))
            if include_inactive or ch.get("active", True):
                channels.append(ch)
        except Exception as e:
            logger.error(f"Ошибка чтения {f.name}: {e}")
    return channels


def _safe_slug(channel_id: str) -> str:
    """Безопасное имя файла из handle (защита от path traversal)."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", channel_id.lstrip("@"))
    if not cleaned:
        raise ValueError(f"Недопустимый handle канала: {channel_id!r}")
    return cleaned


def _load_channel(handle: str) -> dict | None:
    """Загружает карточку одного канала по handle."""
    slug = _safe_slug(handle)
    path = Path(__file__).parent / "channels" / f"{slug}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_channel(ch: dict):
    """Сохраняет карточку канала (с обрезкой полей по лимитам — защита от инъекций)."""
    from ai_client import sanitize_field, FIELD_LIMITS

    for key in ("name", "topic", "audience", "tone", "post_length"):
        if ch.get(key):
            ch[key] = sanitize_field(ch[key], FIELD_LIMITS.get(key, 300))
    if isinstance(ch.get("forbidden_topics"), list):
        ch["forbidden_topics"] = [
            sanitize_field(t, FIELD_LIMITS["forbidden"]) for t in ch["forbidden_topics"][:15]
        ]
    if isinstance(ch.get("example_posts"), list):
        ch["example_posts"] = [
            sanitize_field(t, FIELD_LIMITS["example"]) for t in ch["example_posts"][:5]
        ]

    slug = _safe_slug(ch["channel_id"])
    path = Path(__file__).parent / "channels" / f"{slug}.json"
    path.write_text(json.dumps(ch, ensure_ascii=False, indent=2), encoding="utf-8")


def _utc_to_msk(hours: list[int]) -> list[int]:
    return sorted((h + 3) % 24 for h in hours)


def _images_enabled(ch: dict) -> bool:
    """Включены ли картинки у канала (единый критерий)."""
    src = ch.get("image_source", "auto")
    if src in ("none", "off"):
        return False
    return ch.get("use_images", True) is not False


def _images_status_label(ch: dict) -> str:
    """Человекочитаемый статус картинок для карточки/настроек."""
    if ch.get("channel_type") == "marketplace":
        return "из карточек товара"
    return "включены" if _images_enabled(ch) else "выключены"


def _channel_status_icon(ch: dict) -> str:
    """Иконка состояния канала для списка."""
    if ch.get("schedule_disabled"):
        return "⏸"
    level = buffer.get_level(ch["channel_id"])
    if level == 0:
        return "🔴"
    if level <= cfg.BUFFER_CRITICAL:
        return "🟡"
    return "🟢"


async def _answer_or_send(query_or_message, text: str, reply_markup, parse_mode=ParseMode.HTML):
    """
    Универсальная отправка: если пришёл callback_query — редактируем
    существующее сообщение, иначе отправляем новое.
    """
    from telegram import Message, CallbackQuery
    if isinstance(query_or_message, CallbackQuery):
        await query_or_message.answer()
        try:
            await query_or_message.edit_message_text(
                text, reply_markup=reply_markup, parse_mode=parse_mode
            )
        except Exception:
            # Текст не изменился — игнорируем ошибку
            pass
    else:
        await query_or_message.reply_text(
            text, reply_markup=reply_markup, parse_mode=parse_mode
        )


# ══════════════════════════════════════════════════════════════════════════════
# ЭКРАНЫ
# ══════════════════════════════════════════════════════════════════════════════

async def screen_main(qm, context: ContextTypes.DEFAULT_TYPE):
    """Главное меню."""
    channels = _load_channels()
    active = len(channels)
    total_posts = sum(buffer.get_level(ch["channel_id"]) for ch in channels)

    # Иконки уровня буфера
    buf_icon = "✅" if total_posts >= cfg.BUFFER_MIN * active else (
        "⚠️" if total_posts > 0 else "🔴"
    )

    text = (
        "🤖 <b>Content Factory</b>\n\n"
        f"📋 Каналов: <b>{active}</b>\n"
        f"📬 Постов в очереди: <b>{total_posts}</b> {buf_icon}"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 Мои каналы", callback_data="ui:channels"),
            InlineKeyboardButton("📊 Статус",     callback_data="ui:status"),
        ],
        [InlineKeyboardButton("📝 Очередь постов", callback_data="ui:queue")],
        [InlineKeyboardButton("➕ Добавить канал",  callback_data="add_start")],
        [InlineKeyboardButton("🎨 Тест генерации картинок", callback_data="ui:img_test")],
    ])
    await _answer_or_send(qm, text, kb)


async def screen_channels(qm, context: ContextTypes.DEFAULT_TYPE):
    """Список каналов с иконками статуса."""
    channels = _load_channels(include_inactive=True)

    if not channels:
        text = "📋 <b>Мои каналы</b>\n\nКаналов пока нет."
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Добавить канал", callback_data="add_start")],
            [InlineKeyboardButton("◀️ Назад",          callback_data="ui:main")],
        ])
        await _answer_or_send(qm, text, kb)
        return

    buttons = []
    for ch in channels:
        icon = _channel_status_icon(ch)
        lvl  = buffer.get_level(ch["channel_id"])
        name = ch.get("name") or ch["channel_id"]
        label = f"{icon} {name} · {lvl} постов"
        buttons.append([InlineKeyboardButton(label, callback_data=f"ui:ch:{ch['channel_id']}")])

    buttons.append([InlineKeyboardButton("➕ Добавить канал", callback_data="add_start")])
    buttons.append([InlineKeyboardButton("◀️ Назад",          callback_data="ui:main")])

    await _answer_or_send(
        qm,
        "📋 <b>Мои каналы</b>\n\nВыбери канал для управления:",
        InlineKeyboardMarkup(buttons),
    )


async def screen_channel_card(qm, context: ContextTypes.DEFAULT_TYPE, handle: str):
    """Карточка одного канала."""
    ch = _load_channel(handle)
    if not ch:
        await _answer_or_send(qm, f"❌ Канал {handle} не найден.", None)
        return

    channel_id = ch["channel_id"]
    name       = ch.get("name") or channel_id
    topic      = ch.get("topic", "—")
    ch_type    = "🛍 Маркетплейс" if ch.get("channel_type") == "marketplace" else "📝 Контент"
    paused     = ch.get("schedule_disabled", False)

    # Буфер
    lvl    = buffer.get_level(channel_id)
    status = buffer.check_status(channel_id)
    buf_icon = {"ok": "✅", "emergency": "⚠️", "critical": "🚨"}.get(status, "🔴")

    # Расписание (без дефолта: пустое = «не задано»)
    msk_hours = _utc_to_msk(sorted(ch.get("post_times_utc", [])))
    if paused:
        schedule_str = "⏸ остановлено"
    elif msk_hours:
        schedule_str = " · ".join(f"{h:02d}:00" for h in msk_hours) + " МСК"
    else:
        schedule_str = "не задано"

    # Картинки — простой статус вкл/выкл (движок сам: сток → AI-дорисовка)
    img_str = _images_status_label(ch)

    # Статус паузы. Пауза глушит ТОЛЬКО расписание; перекрытие РСЯ (если включено)
    # продолжает работать — показываем это явно, чтобы не было ощущения «пауза не пашет».
    rsy_on = ch.get("rsy_override", False)
    if paused:
        pause_line = "\n⏸ <b>Расписание остановлено</b> — посты по таймеру не выходят"
        if rsy_on:
            pause_line += "\n📢 <i>но перекрытие РСЯ активно — пост выйдет при рекламе</i>"
    else:
        pause_line = ""

    # Источник тем — без жаргона «RSS»
    if ch.get("channel_type") == "marketplace":
        src_line = ""
    else:
        src_mode = "🌐 Авто (веб-поиск)" if ch.get("topic_source") == "search" else "📡 по лентам"
        src_line = f"\n📰 Источник тем: {src_mode}"

    text = (
        f"<b>{name}</b>  <code>{channel_id}</code>\n"
        f"{ch_type} · {topic}{pause_line}\n\n"
        f"📬 Буфер: <b>{lvl} постов</b> {buf_icon}\n"
        f"⏰ Расписание: {schedule_str}\n"
        f"🖼 Картинки: {img_str}"
        f"{src_line}"
    )

    pause_btn = (
        InlineKeyboardButton("▶️ Включить расписание", callback_data=f"ui:ch_pause:{channel_id}")
        if paused else
        InlineKeyboardButton("⏸ Стоп расписание",  callback_data=f"ui:ch_pause:{channel_id}")
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Обновить карточку", callback_data=f"ui:ch:{channel_id}")],
        [
            InlineKeyboardButton("📝 Посты",     callback_data=f"ui:ch_review:{channel_id}"),
            InlineKeyboardButton("⚙️ Настройки", callback_data=f"ui:ch_settings:{channel_id}"),
        ],
        [
            InlineKeyboardButton("⚡ Генерить",  callback_data=f"ui:ch_generate:{channel_id}"),
            InlineKeyboardButton("📤 Постнуть",  callback_data=f"ui:ch_postnow:{channel_id}"),
        ],
        [InlineKeyboardButton("📜 История публикаций", callback_data=f"ui:ch_history:{channel_id}")],
        [InlineKeyboardButton("🧹 Очистить буфер",    callback_data=f"ui:ch_clear:{channel_id}")],
        [
            pause_btn,
            InlineKeyboardButton("🗑 Удалить",   callback_data=f"ui:ch_delete:{channel_id}"),
        ],
        [InlineKeyboardButton("◀️ К списку каналов", callback_data="ui:channels")],
    ])

    await _answer_or_send(qm, text, kb)


async def screen_channel_settings(qm, context: ContextTypes.DEFAULT_TYPE, handle: str):
    """Экран настроек канала — каждый параметр кнопкой."""
    ch = _load_channel(handle)
    if not ch:
        await _answer_or_send(qm, f"❌ Канал {handle} не найден.", None)
        return

    channel_id = ch["channel_id"]
    topic      = ch.get("topic", "—")
    tone       = ch.get("tone", "—")
    posts_day  = ch.get("daily_posts_count", "?")
    post_len   = ch.get("post_length", "100–200 слов")

    msk_hours  = _utc_to_msk(sorted(ch.get("post_times_utc", [])))
    if ch.get("schedule_disabled"):
        sched_str = "⏸ остановлено"
    elif msk_hours:
        sched_str = " ".join(f"{h:02d}" for h in msk_hours) + " МСК"
    else:
        sched_str = "не задано"

    img_str = "вкл ✅" if _images_enabled(ch) else "выкл ⬜️"
    if ch.get("channel_type") == "marketplace":
        img_str = "из карточек товара"

    forbidden = ch.get("forbidden_topics", [])
    forb_str  = f"{len(forbidden)} тем" if forbidden else "не задано"

    rss_count = len(ch.get("rss_sources", []))
    wb_cats   = ch.get("wb_categories", [])
    wb_str    = ", ".join(wb_cats[:2]) + ("..." if len(wb_cats) > 2 else "") if wb_cats else "все категории"

    is_wb = ch.get("channel_type") == "marketplace"
    rsy_on = ch.get("rsy_override", False)
    rsy_icon = "✅" if rsy_on else "⬜️"

    from archetypes import ARCHETYPE_LABELS
    arch_label = ARCHETYPE_LABELS.get(ch.get("archetype", "default"), ch.get("archetype", "default"))
    src_label = "🌐 Веб-поиск" if ch.get("topic_source") == "search" else "📡 RSS"

    src_mode_short = "Авто (веб-поиск)" if ch.get("topic_source") == "search" else "по лентам"
    rows = [
        [InlineKeyboardButton(f"📌 Тема: {topic[:35]}", callback_data=f"ui:ch_set:{channel_id}:topic")],
        [InlineKeyboardButton(f"📅 Расписание: {sched_str}", callback_data=f"ui:ch_schedule:{channel_id}")],
        [InlineKeyboardButton(f"🔢 Постов в день: {posts_day}", callback_data=f"ui:ch_set:{channel_id}:posts_count")],
        [InlineKeyboardButton(f"📏 Длина поста: {post_len}",    callback_data=f"ui:ch_set:{channel_id}:post_length")],
        [InlineKeyboardButton(f"🖼 Картинки: {img_str}",        callback_data=f"ui:ch_images_toggle:{channel_id}")],
        [InlineKeyboardButton(f"📰 Источники тем: {src_mode_short}", callback_data=f"ui:ch_set:{channel_id}:rss")],
        [InlineKeyboardButton(f"🔗 Референс-каналы ({len(ch.get('reference_channels', []))})", callback_data=f"ui:ch_refs:{channel_id}")],
        [InlineKeyboardButton(f"🚫 Запрещённые темы: {forb_str}", callback_data=f"ui:ch_set:{channel_id}:forbidden")],
        [InlineKeyboardButton(f"{rsy_icon} Перекрытие рекламы РСЯ", callback_data=f"ui:rsy_toggle:{channel_id}")],
    ]

    if is_wb:
        rows.insert(4, [InlineKeyboardButton(f"📦 Категории WB: {wb_str}", callback_data=f"ui:ch_set:{channel_id}:wb_categories")])
    else:
        # Стиль (архетип) и источник тем — только для контент-каналов
        rows.insert(2, [InlineKeyboardButton(f"🎭 Стиль: {arch_label}", callback_data=f"ui:ch_archetype:{channel_id}")])
        rows.insert(3, [InlineKeyboardButton(f"🔎 Источник тем: {src_label}", callback_data=f"ui:ch_source_toggle:{channel_id}")])

    rows.append([InlineKeyboardButton("◀️ Назад к каналу", callback_data=f"ui:ch:{channel_id}")])

    await _answer_or_send(
        qm,
        f"⚙️ <b>Настройки</b>  <code>{channel_id}</code>\n\nНажми параметр чтобы изменить:",
        InlineKeyboardMarkup(rows),
    )


async def screen_archetype_picker(qm, context: ContextTypes.DEFAULT_TYPE, handle: str):
    """Экран выбора архетипа (стиля/личности) канала."""
    from archetypes import ARCHETYPE_LABELS
    ch = _load_channel(handle)
    if not ch:
        await _answer_or_send(qm, f"❌ Канал {handle} не найден.", None)
        return

    channel_id = ch["channel_id"]
    current = ch.get("archetype", "default")

    rows = []
    for key, label in ARCHETYPE_LABELS.items():
        mark = "✅ " if key == current else ""
        rows.append([InlineKeyboardButton(
            f"{mark}{label}", callback_data=f"ui:ch_set_arche:{channel_id}:{key}"
        )])
    rows.append([InlineKeyboardButton("◀️ Назад к настройкам", callback_data=f"ui:ch_settings:{channel_id}")])

    await _answer_or_send(
        qm,
        f"🎭 <b>Стиль канала</b>  <code>{channel_id}</code>\n\n"
        f"Архетип задаёт «голос»: лексику, эмодзи, форматы, температуру.\n"
        f"Текущий: <b>{ARCHETYPE_LABELS.get(current, current)}</b>\n\nВыбери:",
        InlineKeyboardMarkup(rows),
    )


async def screen_schedule(qm, context, handle: str):
    """
    Экран настройки расписания.
    Текущие слоты показаны как кнопки ✅ (нажать — убрать).
    Остальные часы — как кнопки без галочки (нажать — добавить).
    Плюс кнопка «+ Своё время» для ручного ввода.
    """
    ch = _load_channel(handle)
    if not ch:
        await _answer_or_send(qm, f"❌ Канал {handle} не найден.", None)
        return

    channel_id = ch["channel_id"]
    utc_hours  = sorted(ch.get("post_times_utc", []))
    msk_active = sorted((h + 3) % 24 for h in utc_hours)

    # Популярные часы для быстрого выбора (МСК)
    POPULAR = [7, 9, 11, 13, 15, 17, 19, 21]

    # Строим сетку кнопок: 4 в строке
    time_buttons = []
    row = []
    for h in POPULAR:
        label = f"✅ {h:02d}:00" if h in msk_active else f"🕐 {h:02d}:00"
        row.append(InlineKeyboardButton(label, callback_data=f"ui:ch_sched_toggle:{channel_id}:{h}"))
        if len(row) == 4:
            time_buttons.append(row)
            row = []
    if row:
        time_buttons.append(row)

    active_str = " · ".join(f"{h:02d}:00" for h in msk_active) or "не задано"

    sched_rows = [
        *time_buttons,
        [InlineKeyboardButton("➕ Добавить своё время", callback_data=f"ui:ch_sched_custom:{channel_id}")],
        [InlineKeyboardButton("📋 Скопировать с другого канала", callback_data=f"ui:ch_sched_copy:{channel_id}")],
    ]
    if msk_active:
        sched_rows.append([InlineKeyboardButton(
            "🧹 Очистить всё расписание", callback_data=f"ui:ch_sched_clear:{channel_id}"
        )])
    sched_rows.append([InlineKeyboardButton("◀️ Назад к настройкам", callback_data=f"ui:ch_settings:{channel_id}")])
    kb = InlineKeyboardMarkup(sched_rows)

    hint = (
        "🟢 Автопубликация по расписанию включена."
        if msk_active else
        "⏸ Расписание пустое — автопубликации нет (включи любое время)."
    )
    await _answer_or_send(
        qm,
        f"📅 <b>Расписание</b>  <code>{channel_id}</code>\n\n"
        f"Текущее: <b>{active_str}{' МСК' if msk_active else ''}</b>\n"
        f"{hint}\n\n"
        f"✅ — включено · 🕐 — выключено\n"
        f"Нажми на время чтобы включить или убрать:",
        kb,
    )


async def action_schedule_toggle(qm, context, handle: str, hour_msk: int):
    """Добавляет или убирает час из расписания канала."""
    ch = _load_channel(handle)
    if not ch:
        return

    utc_hours = set(ch.get("post_times_utc", []))
    hour_utc  = (hour_msk - 3) % 24

    if hour_utc in utc_hours:
        utc_hours.discard(hour_utc)
    else:
        utc_hours.add(hour_utc)

    ch["post_times_utc"] = sorted(utc_hours)
    # Пустое расписание = автопубликация выключена (пауза); есть время = снимаем паузу
    if ch["post_times_utc"]:
        ch.pop("schedule_disabled", None)
    else:
        ch["schedule_disabled"] = True
    _save_channel(ch)
    await screen_schedule(qm, context, handle)


async def action_schedule_clear(qm, context, handle: str):
    """Полностью очищает расписание канала (автопубликация выключается)."""
    ch = _load_channel(handle)
    if not ch:
        return
    ch["post_times_utc"] = []
    ch["schedule_disabled"] = True
    _save_channel(ch)
    from telegram import CallbackQuery
    if isinstance(qm, CallbackQuery):
        await qm.answer("🧹 Расписание очищено — автопубликация выключена")
    await screen_schedule(qm, context, handle)


async def screen_status(qm, context: ContextTypes.DEFAULT_TYPE):
    """Общий статус всех буферов."""
    channels = _load_channels()
    if not channels:
        await _answer_or_send(qm, "📊 Каналов нет.", InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Назад", callback_data="ui:main")
        ]]))
        return

    lines = ["📊 <b>Статус буферов</b>\n"]
    for ch in channels:
        cid    = ch["channel_id"]
        lvl    = buffer.get_level(cid)
        status = buffer.check_status(cid)
        icon   = {"ok": "✅", "emergency": "⚠️", "critical": "🚨"}.get(status, "🔴")
        paused = " ⏸" if ch.get("schedule_disabled") else ""
        lines.append(f"{icon} <b>{cid}</b> — {lvl} постов{paused}")

    last_gen = _last_generation_time()
    if last_gen:
        lines.append(f"\n🕐 Последняя генерация: {last_gen}")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ Назад", callback_data="ui:main")],
    ])
    await _answer_or_send(qm, "\n".join(lines), kb)


async def screen_queue(qm, context: ContextTypes.DEFAULT_TYPE):
    """Выбор канала для просмотра очереди постов."""
    channels = _load_channels()
    buttons = []
    total = 0
    for ch in channels:
        lvl  = buffer.get_level(ch["channel_id"])
        total += lvl
        icon = "📭" if lvl == 0 else "📋"
        buttons.append([InlineKeyboardButton(
            f"{icon} {ch['channel_id']} · {lvl}",
            callback_data=f"ui:ch_review:{ch['channel_id']}"
        )])
    # Очистка буфера сразу по всем каналам
    if total > 0:
        buttons.append([InlineKeyboardButton(
            f"🧹 Очистить весь буфер ({total})", callback_data="ui:queue_clear_all"
        )])
    buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="ui:main")])

    await _answer_or_send(
        qm,
        "📝 <b>Очередь постов</b>\n\nВыбери канал:",
        InlineKeyboardMarkup(buttons),
    )


async def action_clear_all_confirm(qm, context: ContextTypes.DEFAULT_TYPE):
    """Запрашивает подтверждение очистки буфера ВСЕХ каналов."""
    from database import db
    with db.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE status IN ('ready','pending_review')"
        ).fetchone()[0]

    if count == 0:
        await _answer_or_send(
            qm,
            "📭 Буфер всех каналов уже пуст.",
            InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="ui:queue")]]),
        )
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Да, очистить всё ({count})", callback_data="ui:queue_clear_all_ok")],
        [InlineKeyboardButton("◀️ Отмена", callback_data="ui:queue")],
    ])
    await _answer_or_send(
        qm,
        f"🧹 <b>Очистить буфер ВСЕХ каналов?</b>\n\n"
        f"Будет удалено <b>{count} постов</b> со статусом «готов» и «на проверке» "
        f"по всем каналам разом.\nОпубликованные посты не затрагиваются.",
        kb,
    )


async def action_clear_all_ok(qm, context: ContextTypes.DEFAULT_TYPE):
    """Очищает буфер всех каналов: ready/pending_review → skipped."""
    from database import db
    with db.connect() as conn:
        count = conn.execute(
            "UPDATE posts SET status='skipped' WHERE status IN ('ready','pending_review')"
        ).rowcount
        conn.commit()

    logger.info(f"Буфер очищен по ВСЕМ каналам, удалено {count} постов")
    await _answer_or_send(
        qm,
        f"🧹 Буфер очищен по всем каналам — удалено <b>{count} постов</b>.",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("⚡ Генерить для всех", callback_data="ui:generate_all")],
            [InlineKeyboardButton("◀️ Назад", callback_data="ui:main")],
        ]),
    )


# ── Действия с каналом ────────────────────────────────────────────────────

async def action_pause_toggle(qm, context, handle: str):
    """Переключает паузу канала."""
    ch = _load_channel(handle)
    if not ch:
        return
    if ch.get("schedule_disabled"):
        ch.pop("schedule_disabled", None)
        msg = f"▶️ Расписание {handle} включено."
    else:
        ch["schedule_disabled"] = True
        msg = f"⏸ Расписание {handle} остановлено (РСЯ-перекрытие, если включено, продолжит работать)."
    _save_channel(ch)
    logger.info(msg)
    # Возвращаемся к карточке канала с обновлёнными данными
    await screen_channel_card(qm, context, handle)


async def action_delete_confirm(qm, context, handle: str):
    """Запрашивает подтверждение удаления."""
    ch = _load_channel(handle)
    name = ch.get("name", handle) if ch else handle
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Да, удалить",  callback_data=f"ui:ch_delete_ok:{handle}"),
            InlineKeyboardButton("◀️ Отмена",       callback_data=f"ui:ch:{handle}"),
        ]
    ])
    await _answer_or_send(
        qm,
        f"🗑 Удалить канал <b>{name}</b>?\n\nПосты в буфере тоже будут удалены.",
        kb,
    )


async def action_delete_ok(qm, context, handle: str):
    """Выполняет удаление канала."""
    slug = _safe_slug(handle)
    path = Path(__file__).parent / "channels" / f"{slug}.json"
    if path.exists():
        ch = json.loads(path.read_text(encoding="utf-8"))
        ch["active"] = False
        path.write_text(json.dumps(ch, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Канал {handle} деактивирован.")
    await _answer_or_send(
        qm,
        f"🗑 Канал <b>{handle}</b> удалён.",
        InlineKeyboardMarkup([[InlineKeyboardButton("◀️ К списку", callback_data="ui:channels")]]),
    )


async def action_clear_buffer_confirm(qm, context, handle: str):
    """Запрашивает подтверждение очистки буфера канала."""
    from database import db
    with db.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE channel_id=? AND status IN ('ready','pending_review')",
            (handle,)
        ).fetchone()[0]

    ch = _load_channel(handle.lstrip("@"))
    name = ch.get("name", handle) if ch else handle

    if count == 0:
        await _answer_or_send(
            qm,
            f"📭 Буфер канала <b>{name}</b> уже пуст.",
            InlineKeyboardMarkup([[InlineKeyboardButton("◀️ К каналу", callback_data=f"ui:ch:{handle}")]]),
        )
        return

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"✅ Да, удалить {count} постов", callback_data=f"ui:ch_clear_ok:{handle}"),
        ],
        [InlineKeyboardButton("◀️ Отмена", callback_data=f"ui:ch:{handle}")],
    ])
    await _answer_or_send(
        qm,
        f"🧹 <b>Очистить буфер канала {name}?</b>\n\n"
        f"Будет удалено <b>{count} постов</b> со статусом «готов» и «на проверке».\n"
        f"Опубликованные посты не затрагиваются.",
        kb,
    )


async def action_clear_buffer_ok(qm, context, handle: str):
    """Выполняет очистку буфера: помечает все ready/pending_review посты как skipped."""
    from database import db
    with db.connect() as conn:
        count = conn.execute(
            "UPDATE posts SET status='skipped' WHERE channel_id=? AND status IN ('ready','pending_review')",
            (handle,)
        ).rowcount
        conn.commit()

    logger.info(f"Буфер очищен: {handle}, удалено {count} постов")
    await _answer_or_send(
        qm,
        f"🧹 Буфер очищен — удалено <b>{count} постов</b>.\n\n"
        f"Нажми ⚡ Генерить чтобы наполнить буфер заново.",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("⚡ Генерить", callback_data=f"ui:ch_generate:{handle}")],
            [InlineKeyboardButton("◀️ К каналу", callback_data=f"ui:ch:{handle}")],
        ]),
    )


async def action_generate(qm, context, handle: str):
    """Показывает выбор количества постов для генерации."""
    from database import db
    # Текущий уровень буфера
    with db.connect() as conn:
        current = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE channel_id=? AND status IN ('ready','pending_review')",
            (handle,)
        ).fetchone()[0]

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1️⃣  1 пост",   callback_data=f"ui:ch_gen_run:{handle}:1"),
            InlineKeyboardButton("3️⃣  3 поста",  callback_data=f"ui:ch_gen_run:{handle}:3"),
            InlineKeyboardButton("5️⃣  5 постов", callback_data=f"ui:ch_gen_run:{handle}:5"),
        ],
        [
            InlineKeyboardButton("🔟 10 постов", callback_data=f"ui:ch_gen_run:{handle}:10"),
            InlineKeyboardButton("💼 15 постов", callback_data=f"ui:ch_gen_run:{handle}:15"),
            InlineKeyboardButton("📦 20 постов", callback_data=f"ui:ch_gen_run:{handle}:20"),
        ],
        [InlineKeyboardButton("◀️ Отмена", callback_data=f"ui:ch:{handle}")],
    ])
    await _answer_or_send(
        qm,
        f"⚡ <b>Генерация постов</b>\n\n"
        f"Канал: <b>{handle}</b>\n"
        f"Сейчас в буфере: <b>{current}</b> постов\n\n"
        f"Сколько постов сгенерировать?",
        kb,
    )


async def action_generate_run(qm, context, handle: str, count: int):
    """Запускает генерацию заданного количества постов."""
    from telegram import CallbackQuery
    if isinstance(qm, CallbackQuery):
        await qm.answer(f"⚡ Генерирую {count} постов...")

    # Показываем прогресс
    if isinstance(qm, CallbackQuery):
        try:
            await qm.edit_message_text(
                f"⏳ <b>Генерирую {count} постов для {handle}...</b>\n\nЭто займёт до {count * 5} секунд.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⏳ Генерация...", callback_data="noop")
                ]]),
            )
        except Exception:
            pass

    try:
        from content_generator import generator as _gen
        channel = _gen._load_channel_by_id(handle)
        result = await _gen.run_for_channel(channel, target_count=count)
        generated = result.get("generated", 0)
        text = (
            f"✅ <b>Готово!</b>\n\n"
            f"Канал: <b>{handle}</b>\n"
            f"Запрошено: {count} · Сгенерировано: <b>{generated}</b>"
        )
    except Exception as e:
        text = f"❌ Ошибка генерации для {handle}:\n{e}"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Посмотреть посты", callback_data=f"ui:ch_review:{handle}")],
        [InlineKeyboardButton("◀️ К каналу",         callback_data=f"ui:ch:{handle}")],
    ])
    if isinstance(qm, CallbackQuery):
        await qm.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await qm.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def action_postnow(qm, context, handle: str):
    """Публикует следующий пост из буфера немедленно."""
    from telegram import CallbackQuery
    if isinstance(qm, CallbackQuery):
        await qm.answer("📤 Публикую...")

    try:
        from poster import poster as _poster
        result = await _poster.post_now(handle)
        if result["success"]:
            text = f"✅ Пост опубликован в <b>{handle}</b>!"
        else:
            text = f"❌ Не удалось: {result.get('error', 'Неизвестная ошибка')}"
    except Exception as e:
        text = f"❌ Ошибка: {e}"

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("◀️ Назад к каналу", callback_data=f"ui:ch:{handle}")
    ]])
    if isinstance(qm, CallbackQuery):
        await qm.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await qm.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


# ── RSS менеджер ──────────────────────────────────────────────────────────

async def screen_rss_manager(qm, context: ContextTypes.DEFAULT_TYPE, handle: str):
    """Экран управления RSS-источниками канала."""
    ch = _load_channel(handle)
    if not ch:
        return

    sources = ch.get("rss_sources", [])
    channel_id = ch["channel_id"]
    mode = ch.get("topic_source", "rss")
    mode_label = "🌐 Авто (бот сам ищет в интернете)" if mode == "search" else "📰 По лентам (сайты-источники)"

    lines = [
        f"📰 <b>Источники тем</b>  <code>{channel_id}</code>\n",
        f"Откуда брать темы для постов:\n<b>{mode_label}</b>\n",
    ]
    if mode == "search":
        lines.append("В режиме «Авто» список лент не нужен — бот находит свежие темы сам.")
    elif sources:
        lines.append(f"Подключено лент: <b>{len(sources)}</b>")
        for i, url in enumerate(sources):
            domain = url.split("/")[2] if url.startswith("http") else url
            lines.append(f"{i+1}. <code>{domain}</code>")
    else:
        lines.append("Лент пока нет — добавь вручную или нажми «Подобрать ИИ».")

    text = "\n".join(lines)

    # Переключатель режима источника тем
    toggle_label = "🌐 Переключить на Авто (веб-поиск)" if mode != "search" else "📰 Переключить на ленты"
    action_buttons = [[InlineKeyboardButton(toggle_label, callback_data=f"ui:ch_src_mode:{handle}")]]

    # Кнопки удаления лент — только в режиме «по лентам»
    if mode != "search":
        for i, url in enumerate(sources):
            domain = url.split("/")[2] if url.startswith("http") else url[:30]
            action_buttons.append([InlineKeyboardButton(
                f"🗑 {i+1}. {domain}", callback_data=f"ui:rss_del:{handle}:{i}"
            )])
        action_buttons.append([InlineKeyboardButton("➕ Добавить ленту", callback_data=f"ui:rss_add:{handle}")])
        action_buttons.append([InlineKeyboardButton("✨ Подобрать ИИ", callback_data=f"ui:rss_ai:{handle}")])
        if sources:
            action_buttons.append([InlineKeyboardButton("🧹 Очистить все ленты", callback_data=f"ui:rss_clear:{handle}")])

    action_buttons.append([InlineKeyboardButton("◀️ К настройкам", callback_data=f"ui:ch_settings:{handle}")])

    await _answer_or_send(qm, text, InlineKeyboardMarkup(action_buttons))


async def action_rss_delete(qm, context: ContextTypes.DEFAULT_TYPE, handle: str, idx: int):
    """Удаляет один RSS-источник по индексу."""
    ch = _load_channel(handle)
    if not ch:
        return
    sources = ch.get("rss_sources", [])
    if 0 <= idx < len(sources):
        removed = sources.pop(idx)
        ch["rss_sources"] = sources
        _save_channel(ch)
        domain = removed.split("/")[2] if removed.startswith("http") else removed[:40]
        await qm.answer(f"🗑 Удалено: {domain}", show_alert=False)
    await screen_rss_manager(qm, context, handle)


async def action_rss_clear(qm, context: ContextTypes.DEFAULT_TYPE, handle: str):
    """Очищает все RSS-источники."""
    ch = _load_channel(handle)
    if not ch:
        return
    ch["rss_sources"] = []
    _save_channel(ch)
    await qm.answer("🧹 Все источники удалены")
    await screen_rss_manager(qm, context, handle)


# ── Референс-каналы (доноры контента) ─────────────────────────────────────

async def screen_references(qm, context: ContextTypes.DEFAULT_TYPE, handle: str):
    """Экран управления каналами-донорами (референсами)."""
    ch = _load_channel(handle)
    if not ch:
        return
    cid = ch["channel_id"]
    refs = ch.get("reference_channels", [])

    lines = [f"🔗 <b>Референс-каналы</b>  <code>{cid}</code>\n"]
    if refs:
        lines.append("Бот раз в день забирает новые посты доноров в очередь "
                     "(медиа как есть, текст — по настройке).\n")
    else:
        lines.append("Доноров пока нет.\n\nДобавь канал-донор — бот будет брать его "
                     "свежие посты: медиа как есть, текст можно перефразировать или "
                     "оставить как есть. Первый импорт — последние 10 постов.")

    rows = []
    for i, r in enumerate(refs):
        rp = "вкл" if r.get("rephrase", True) else "выкл"
        md = "вкл" if r.get("take_media", True) else "выкл"
        ad = "вкл" if r.get("skip_ads", True) else "выкл"
        lines.append(f"\n{i+1}. <code>{r.get('handle')}</code>")
        rows.append([InlineKeyboardButton(f"{i+1}. {r.get('handle')}", callback_data=f"ui:ch_refs:{handle}")])
        rows.append([
            InlineKeyboardButton(f"✍️ перефраз: {rp}", callback_data=f"ui:ref_tgl:{handle}:{i}:rephrase"),
            InlineKeyboardButton(f"🖼 медиа: {md}",    callback_data=f"ui:ref_tgl:{handle}:{i}:take_media"),
        ])
        rows.append([
            InlineKeyboardButton(f"🚫 фильтр рекламы: {ad}", callback_data=f"ui:ref_tgl:{handle}:{i}:skip_ads"),
            InlineKeyboardButton("🗑 удалить",              callback_data=f"ui:ref_del:{handle}:{i}"),
        ])

    rows.append([InlineKeyboardButton("➕ Добавить донор", callback_data=f"ui:ref_add:{handle}")])
    if refs:
        rows.append([InlineKeyboardButton("📥 Взять референсы", callback_data=f"ui:ref_take:{handle}")])
    rows.append([InlineKeyboardButton("◀️ К настройкам", callback_data=f"ui:ch_settings:{handle}")])

    await _answer_or_send(qm, "\n".join(lines), InlineKeyboardMarkup(rows))


async def action_ref_toggle(qm, context, handle: str, idx: int, flag: str):
    """Переключает флаг референса (rephrase / take_media / skip_ads)."""
    ch = _load_channel(handle)
    if not ch:
        return
    refs = ch.get("reference_channels", [])
    if 0 <= idx < len(refs) and flag in ("rephrase", "take_media", "skip_ads"):
        cur = refs[idx].get(flag, True)
        refs[idx][flag] = not cur
        _save_channel(ch)
    await screen_references(qm, context, handle)


async def action_ref_delete(qm, context, handle: str, idx: int):
    """Удаляет референс по индексу."""
    ch = _load_channel(handle)
    if not ch:
        return
    refs = ch.get("reference_channels", [])
    if 0 <= idx < len(refs):
        removed = refs.pop(idx)
        ch["reference_channels"] = refs
        _save_channel(ch)
        await qm.answer(f"🗑 Удалён {removed.get('handle')}")
    await screen_references(qm, context, handle)


async def action_ref_add(qm, context, handle: str):
    """Запрашивает @username донора текстом."""
    from telegram import CallbackQuery
    context.user_data["editing"] = {"handle": handle, "field": "ref_add"}
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data=f"ui:ch_refs:{handle}")]])
    text = (
        f"➕ <b>Добавить канал-донор</b>\n\n"
        f"Пришли @username или ссылку на публичный канал, из которого брать посты.\n"
        f"Например: <code>@durov</code>"
    )
    if isinstance(qm, CallbackQuery):
        await qm.answer()
        await qm.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await qm.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


REF_TAKE_MAX = 50  # потолок: больше не берём за раз (защита от «500»)


async def screen_ref_take_count(qm, context, handle: str):
    """Спрашивает, сколько постов взять: кнопки 5/10/20/50 или ввод числа (макс 50)."""
    ch = _load_channel(handle)
    if not ch or not ch.get("reference_channels"):
        await qm.answer("Нет референсов")
        return
    # Готовим приём числа текстом (если пользователь напишет цифру)
    context.user_data["editing"] = {"handle": handle, "field": "ref_count"}
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("5",  callback_data=f"ui:ref_go:{handle}:5"),
            InlineKeyboardButton("10", callback_data=f"ui:ref_go:{handle}:10"),
            InlineKeyboardButton("20", callback_data=f"ui:ref_go:{handle}:20"),
            InlineKeyboardButton("50", callback_data=f"ui:ref_go:{handle}:50"),
        ],
        [InlineKeyboardButton("◀️ Отмена", callback_data=f"ui:ch_refs:{handle}")],
    ])
    await _answer_or_send(
        qm,
        "📥 <b>Сколько постов взять?</b>\n\n"
        "Выбери кнопкой или пришли число (например <code>12</code>).\n"
        f"Максимум — <b>{REF_TAKE_MAX}</b> за раз.",
        kb,
    )


async def action_ref_import(qm, context, handle: str, count: int = 10):
    """
    Собирает ещё `count` постов доноров (relay-режим): сначала новые, если мало —
    добирает старые из архива. Медиа пересылается юзерботом в ЛС бота (без скачивания);
    медиа-посты появятся в очереди как только подтянется file_id.
    """
    from telegram import CallbackQuery
    context.user_data.pop("editing", None)  # ввод числа больше не ждём
    count = max(1, min(int(count), REF_TAKE_MAX))  # кап: 1..50

    ch = _load_channel(handle)
    if not ch or not ch.get("reference_channels"):
        if isinstance(qm, CallbackQuery):
            await qm.answer("Нет референсов")
        return
    if isinstance(qm, CallbackQuery):
        await qm.answer(f"⏳ Беру {count}…")
    else:
        await qm.reply_text(f"⏳ Беру {count}…")
    from reference_importer import import_for_channel
    try:
        res = await import_for_channel(ch, count=count)
        dups = res.get("skipped_dups", 0)
        lim = res.get("skipped_limits", 0)
        notes = ""
        if dups:
            notes += f"\n🔁 Пропущено дублей: <b>{dups}</b>"
        if lim:
            notes += f"\n📏 Пропущено по лимиту видео/размера: <b>{lim}</b>"
        msg = (
            f"✅ Собрано: добавлено <b>{res['added']}</b> постов из {res['refs']} донор(ов).{notes}\n\n"
            f"<i>Медиа-посты подтянутся в очередь по мере пересылки файлов ботом.</i>"
        )
        if res['added'] == 0 and not dups and not lim:
            msg = "ℹ️ Новых постов нет, и архив на этом уровне исчерпан."
    except Exception as e:
        logger.error(f"Импорт референсов вручную [{handle}]: {e}")
        msg = f"❌ Ошибка импорта: {e}"
    await _answer_or_send(
        qm, msg,
        InlineKeyboardMarkup([[InlineKeyboardButton("🔗 К референсам", callback_data=f"ui:ch_refs:{handle}")]]),
    )


async def action_rss_add_prompt(qm, context: ContextTypes.DEFAULT_TYPE, handle: str):
    """Просит прислать URL для добавления."""
    context.user_data["editing"] = {"handle": handle, "field": "rss"}
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("◀️ Отмена", callback_data=f"ui:ch_set:{handle}:rss")
    ]])
    await _answer_or_send(
        qm,
        "📰 <b>Добавить RSS-источники</b>\n\n"
        "Пришли URL через запятую или каждый с новой строки:\n\n"
        "<code>https://site.com/feed\nhttps://other.com/rss</code>",
        kb,
    )


async def action_rss_ai_suggest(qm, context: ContextTypes.DEFAULT_TYPE, handle: str):
    """ИИ подбирает RSS-источники по теме канала."""
    ch = _load_channel(handle)
    if not ch:
        return

    await _answer_or_send(
        qm,
        "✨ <b>Подбираю RSS-источники...</b>\n\n"
        f"Анализирую тему: <i>{ch.get('topic', '')[:80]}</i>\n\n"
        "⏳ Подождите несколько секунд...",
        InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data=f"ui:ch_set:{handle}:rss")]]),
    )

    try:
        from claude_helper import claude_text

        topic = ch.get("topic", "")
        channel_name = ch.get("name", handle)
        existing = ch.get("rss_sources", [])
        existing_domains = [u.split("/")[2] for u in existing if u.startswith("http")]

        system = (
            "You are an expert at finding RSS feeds for content creators. "
            "Return ONLY a JSON array of RSS feed URLs, no explanation. "
            "Only include feeds that actually exist and are commonly used. "
            "Prefer feeds that publish frequently (daily or weekly)."
        )
        user_msg = (
            f"Find 5-8 RSS feed URLs for a Telegram channel about: '{topic}'\n"
            f"Channel name: {channel_name}\n"
            + (f"Already has: {', '.join(existing_domains)}\n" if existing_domains else "")
            + "Return JSON array of URLs only. Example: [\"https://site.com/feed\", ...]"
        )

        raw = await claude_text(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        import json as _json, re as _re
        # Извлекаем JSON-массив из ответа
        match = _re.search(r'\[.*?\]', raw, _re.DOTALL)
        if not match:
            raise ValueError("Нет JSON в ответе")
        suggested = _json.loads(match.group())
        # Фильтруем уже добавленные
        new_urls = [u for u in suggested if isinstance(u, str) and u.startswith("http") and u not in existing]

    except Exception as e:
        logger.error(f"RSS AI suggest error: {e}")
        new_urls = []

    if not new_urls:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ К RSS", callback_data=f"ui:ch_set:{handle}:rss")
        ]])
        await _answer_or_send(qm, "😞 Не удалось подобрать источники. Добавь вручную.", kb)
        return

    # Сохраняем предложенные в user_data для подтверждения
    context.user_data["rss_ai_suggested"] = {"handle": handle, "urls": new_urls}

    lines = [f"✨ <b>ИИ подобрал {len(new_urls)} источников:</b>\n"]
    for i, url in enumerate(new_urls):
        domain = url.split("/")[2] if "/" in url else url
        lines.append(f"{i+1}. <code>{domain}</code>")
    lines.append("\nДобавить все эти источники?")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Добавить все",   callback_data=f"ui:rss_ai_ok:{handle}")],
        [InlineKeyboardButton("◀️ Отмена",         callback_data=f"ui:ch_set:{handle}:rss")],
    ])
    await _answer_or_send(qm, "\n".join(lines), kb)


async def action_rss_ai_confirm(qm, context: ContextTypes.DEFAULT_TYPE, handle: str):
    """Добавляет AI-подобранные RSS-источники."""
    data = context.user_data.pop("rss_ai_suggested", None)
    if not data or data.get("handle") != handle:
        await screen_rss_manager(qm, context, handle)
        return

    ch = _load_channel(handle)
    if not ch:
        return

    new_urls = data["urls"]
    existing = ch.get("rss_sources", [])
    ch["rss_sources"] = list(dict.fromkeys(existing + new_urls))
    _save_channel(ch)

    await qm.answer(f"✅ Добавлено {len(new_urls)} источников!")
    await screen_rss_manager(qm, context, handle)


# ── Редактирование настроек ───────────────────────────────────────────────

# Конфиг редактируемых полей: field_key → (заголовок, подсказка, тип)
EDITABLE_FIELDS = {
    "topic":       ("📌 Тема канала",        "Введи новую тему канала\nНапример: <i>Майнкрафт советы и новости</i>",      "text"),
    "tone":        ("🎨 Тон общения",         "Введи желаемый тон\nНапример: <i>дружелюбный, с юмором, без снобизма</i>", "text"),
    "schedule":    ("📅 Расписание",          "Введи часы публикации МСК через пробел\nНапример: <code>9 12 16 20</code>",  "schedule"),
    "posts_count": ("🔢 Постов в день",       "Введи число постов в день (от 1 до 30)\nНапример: <code>10</code>",         "int"),
    "post_length": ("📏 Длина поста",         "Введи диапазон слов\nНапример: <code>100–200 слов</code>",                  "text"),
    "images":      ("🖼 Источник картинок",   None,  "images_menu"),
    "rss":         ("📰 Источники тем",        None, "rss_menu"),
    "forbidden":   ("🚫 Запрещённые темы",    "Перечисли запрещённые темы через запятую.\nНапиши <b>нет</b> чтобы убрать все.", "text_list"),
    "wb_categories":("📦 Категории WB",       "Перечисли категории через запятую\nНапример: <code>кроссовки, наушники</code>\nНапиши <b>все</b> чтобы убрать фильтр.", "text_list_or_clear"),
}


async def screen_edit_field(qm, context, handle: str, field: str):
    """Начинает редактирование конкретного поля настроек."""
    from telegram import CallbackQuery

    cfg_field = EDITABLE_FIELDS.get(field)
    if not cfg_field:
        return

    title, prompt, field_type = cfg_field

    if field_type == "images_menu":
        # Для картинок — показываем меню выбора источника
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎮 Reddit (тематические)", callback_data=f"ui:ch_set_img:{handle}:reddit")],
            [InlineKeyboardButton("📸 Pexels/Unsplash",       callback_data=f"ui:ch_set_img:{handle}:stock")],
            [InlineKeyboardButton("🚫 Без картинок",          callback_data=f"ui:ch_set_img:{handle}:none")],
            [InlineKeyboardButton("◀️ Отмена",                callback_data=f"ui:ch_settings:{handle}")],
        ])
        await _answer_or_send(
            qm,
            f"🖼 <b>Источник картинок для {handle}</b>\n\nВыбери откуда брать изображения:",
            kb,
        )
        return

    if field_type == "rss_menu":
        await screen_rss_manager(qm, context, handle)
        return

    # Для остальных — ждём текстовый ввод
    context.user_data["editing"] = {"handle": handle, "field": field}

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("◀️ Отмена", callback_data=f"ui:ch_settings:{handle}")
    ]])

    ch = _load_channel(handle)
    current = _get_field_display(ch, field) if ch else "—"

    text = (
        f"✏️ <b>{title}</b>\n"
        f"Канал: <code>{handle}</code>\n\n"
        f"Сейчас: <i>{current}</i>\n\n"
        f"{prompt}"
    )

    if isinstance(qm, CallbackQuery):
        await qm.answer()
        await qm.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await qm.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def action_images_toggle(qm, context, handle: str):
    """Простой тумблер картинок: вкл (auto: сток→AI-дорисовка) / выкл."""
    ch = _load_channel(handle)
    if not ch:
        return
    if _images_enabled(ch):
        ch["image_source"] = "none"
        ch["use_images"] = False
        msg = "🚫 Картинки выключены"
    else:
        ch["image_source"] = "auto"
        ch["use_images"] = True
        msg = "🖼 Картинки включены"
    _save_channel(ch)
    from telegram import CallbackQuery
    if isinstance(qm, CallbackQuery):
        await qm.answer(msg)
    await screen_channel_settings(qm, context, handle)


async def screen_set_image_source(qm, context, handle: str, source: str):
    """Обрабатывает выбор источника картинок из меню."""
    ch = _load_channel(handle)
    if not ch:
        return

    if source == "none":
        ch["use_images"] = False
        ch.pop("reddit_image_subreddits", None)
        msg = "🚫 Картинки отключены."
    elif source == "stock":
        ch["use_images"] = True
        ch["image_source"] = "stock"
        ch.pop("reddit_image_subreddits", None)
        if not ch.get("image_keywords"):
            ch["image_keywords"] = [t.strip() for t in ch.get("topic", "").split(",")][:3]
        msg = "📸 Источник: Pexels/Unsplash. Ключевые слова взяты из темы канала."
    elif source == "reddit":
        # Запрашиваем сабреддиты текстом
        context.user_data["editing"] = {"handle": handle, "field": "reddit_subs"}
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Отмена", callback_data=f"ui:ch_settings:{handle}")
        ]])
        from telegram import CallbackQuery
        text = (
            f"🎮 <b>Reddit-картинки для {handle}</b>\n\n"
            f"Напиши сабреддиты через запятую (без r/):\n\n"
            f"• Майнкрафт: <code>Minecraft, MCPE, feedthebeast</code>\n"
            f"• КС2: <code>GlobalOffensive, csgo</code>\n"
            f"• Аниме: <code>anime, Animemes</code>"
        )
        if isinstance(qm, CallbackQuery):
            await qm.answer()
            await qm.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        else:
            await qm.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return
    else:
        return

    _save_channel(ch)
    await screen_channel_settings(qm, context, handle)


async def handle_settings_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Обрабатывает текстовый ввод для редактирования настроек.
    Возвращает True если ввод был обработан (чтобы bot.py мог пропустить дальше).
    """
    editing = context.user_data.get("editing")
    if not editing:
        return False

    handle = editing["handle"]
    field  = editing["field"]
    text   = update.message.text.strip()

    ch = _load_channel(handle)
    if not ch:
        context.user_data.pop("editing", None)
        return False

    success_msg = None

    # Ввод количества для «📥 Взять референсы» — не сохраняем карточку, а сразу импортируем
    if field == "ref_count":
        context.user_data.pop("editing", None)
        digits = "".join(c for c in text if c.isdigit())
        if not digits:
            context.user_data["editing"] = {"handle": handle, "field": "ref_count"}
            await update.message.reply_text(
                f"⚠️ Пришли число, например <code>12</code> (максимум {REF_TAKE_MAX}).",
                parse_mode=ParseMode.HTML,
            )
            return True
        n = max(1, min(int(digits), REF_TAKE_MAX))
        await action_ref_import(update.message, context, handle, n)
        return True

    if field == "topic":
        ch["topic"] = text
        success_msg = f"✅ Тема обновлена: <i>{text}</i>"

    elif field == "tone":
        ch["tone"] = text
        success_msg = f"✅ Тон обновлён: <i>{text}</i>"

    elif field == "schedule":
        try:
            hours_msk = sorted(set(int(h) for h in text.split() if h.isdigit() and 0 <= int(h) <= 23))
            if not hours_msk:
                raise ValueError
            ch["post_times_utc"] = sorted((h - 3) % 24 for h in hours_msk)
            ch.pop("schedule_disabled", None)
            times_str = " ".join(f"{h:02d}:00" for h in hours_msk)
            success_msg = f"✅ Расписание: <b>{times_str} МСК</b>"
        except Exception:
            await update.message.reply_text(
                "⚠️ Неверный формат. Напиши часы через пробел, например: <code>9 12 16 20</code>",
                parse_mode=ParseMode.HTML,
            )
            return True

    elif field == "schedule_add":
        try:
            new_hours_msk = [int(h) for h in text.split() if h.isdigit() and 0 <= int(h) <= 23]
            if not new_hours_msk:
                raise ValueError
            existing_utc = set(ch.get("post_times_utc", [6, 9, 13, 17]))
            for h in new_hours_msk:
                existing_utc.add((h - 3) % 24)
            ch["post_times_utc"] = sorted(existing_utc)
            ch.pop("schedule_disabled", None)
            all_msk = sorted((h + 3) % 24 for h in ch["post_times_utc"])
            times_str = " · ".join(f"{h:02d}:00" for h in all_msk)
            success_msg = f"✅ Расписание обновлено: <b>{times_str} МСК</b>"
        except Exception:
            await update.message.reply_text(
                "⚠️ Неверный формат. Напиши часы через пробел, например: <code>8</code> или <code>8 14 22</code>",
                parse_mode=ParseMode.HTML,
            )
            return True

    elif field == "posts_count":
        try:
            n = int(text)
            if n < 1 or n > 30:
                raise ValueError
            ch["daily_posts_count"] = n
            success_msg = f"✅ Постов в день: <b>{n}</b>"
        except Exception:
            await update.message.reply_text("⚠️ Введи число от 1 до 30.")
            return True

    elif field == "post_length":
        ch["post_length"] = text
        success_msg = f"✅ Длина поста: <i>{text}</i>"

    elif field == "rss":
        urls = [u.strip() for u in text.replace("\n", ",").split(",") if u.strip().startswith("http")]
        if urls:
            ch["rss_sources"] = list(dict.fromkeys(ch.get("rss_sources", []) + urls))
            _save_channel(ch)
            context.user_data.pop("editing", None)
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("📰 К RSS-источникам", callback_data=f"ui:ch_set:{handle}:rss"),
            ]])
            await update.message.reply_text(
                f"✅ Добавлено <b>{len(urls)}</b> источников. Всего: {len(ch['rss_sources'])}",
                reply_markup=kb, parse_mode=ParseMode.HTML,
            )
            return True
        else:
            await update.message.reply_text("⚠️ Не нашёл URL. Пришли ссылки начинающиеся с http.")
            return True

    elif field == "forbidden":
        if text.lower() in ("нет", "нет.", "убрать", "очистить"):
            ch["forbidden_topics"] = []
            success_msg = "✅ Запрещённые темы убраны."
        else:
            topics = [t.strip() for t in text.replace("\n", ",").split(",") if t.strip()]
            ch["forbidden_topics"] = topics
            success_msg = f"✅ Запрещённые темы: {', '.join(topics)}"

    elif field == "wb_categories":
        if text.lower() in ("все", "все.", "убрать", "очистить"):
            ch.pop("wb_categories", None)
            success_msg = "✅ Категории сброшены — будут использоваться все."
        else:
            cats = [c.strip() for c in text.replace("\n", ",").split(",") if c.strip()]
            ch["wb_categories"] = cats
            success_msg = f"✅ Категории: {', '.join(cats)}"

    elif field == "ref_add":
        from userbot_reader import normalize_handle
        donor = normalize_handle(text)
        if not donor or donor == "@":
            await update.message.reply_text("⚠️ Пришли @username или ссылку на канал-донор.")
            return True
        refs = ch.get("reference_channels", [])
        if any(r.get("handle", "").lower() == donor.lower() for r in refs):
            await update.message.reply_text(f"⚠️ {donor} уже в референсах.")
            return True
        # Дефолтный пресет: медиа как есть, перефраз вкл, фильтр рекламы вкл
        refs.append({"handle": donor, "rephrase": True, "take_media": True,
                     "skip_ads": True, "last_id": 0})
        ch["reference_channels"] = refs
        _save_channel(ch)
        context.user_data.pop("editing", None)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔗 К референсам", callback_data=f"ui:ch_refs:{handle}"),
        ]])
        await update.message.reply_text(
            f"✅ Донор {donor} добавлен. Нажми «Импортировать сейчас», "
            f"чтобы сразу забрать последние 10 постов, или жди ежедневного импорта.",
            reply_markup=kb, parse_mode=ParseMode.HTML,
        )
        return True

    elif field == "reddit_subs":
        subs = [s.strip().lstrip("r/") for s in text.replace("\n", ",").split(",") if s.strip()]
        if subs:
            ch["reddit_image_subreddits"] = subs
            ch["use_images"] = True
            ch["image_source"] = "reddit"
            subs_str = ", ".join(f"r/{s}" for s in subs)
            success_msg = f"✅ Сабреддиты: <b>{subs_str}</b>"
        else:
            await update.message.reply_text("⚠️ Не понял. Напиши названия через запятую.")
            return True

    if success_msg:
        _save_channel(ch)
        context.user_data.pop("editing", None)
        # Для расписания — возвращаем на экран расписания, иначе на настройки
        if field == "schedule_add":
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("📅 К расписанию", callback_data=f"ui:ch_schedule:{handle}"),
                InlineKeyboardButton("◀️ К каналу",     callback_data=f"ui:ch:{handle}"),
            ]])
        else:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("⚙️ К настройкам", callback_data=f"ui:ch_settings:{handle}"),
                InlineKeyboardButton("◀️ К каналу",     callback_data=f"ui:ch:{handle}"),
            ]])
        await update.message.reply_text(success_msg, reply_markup=kb, parse_mode=ParseMode.HTML)
        return True

    return False


# ── Вспомогательные ───────────────────────────────────────────────────────

def _get_field_display(ch: dict, field: str) -> str:
    """Возвращает текущее значение поля для отображения в подсказке."""
    if field == "topic":       return ch.get("topic", "—")
    if field == "tone":        return ch.get("tone", "—")
    if field == "posts_count": return str(ch.get("daily_posts_count", "?"))
    if field == "post_length": return ch.get("post_length", "—")
    if field == "schedule":
        msk = _utc_to_msk(ch.get("post_times_utc", [6, 9, 13, 17]))
        return " ".join(str(h) for h in msk) + " МСК"
    if field == "rss":
        n = len(ch.get("rss_sources", []))
        return f"{n} источников"
    if field == "forbidden":
        t = ch.get("forbidden_topics", [])
        return ", ".join(t) if t else "нет"
    if field == "wb_categories":
        cats = ch.get("wb_categories", [])
        return ", ".join(cats) if cats else "все категории"
    return "—"


def _last_generation_time() -> str | None:
    """Возвращает время последней генерации из БД (если доступно)."""
    try:
        from database import db
        result = db.execute_one(
            "SELECT MAX(created_at) FROM posts WHERE status != 'draft'"
        )
        if result and result[0]:
            return result[0][:16]
    except Exception:
        pass
    return None


# ══════════════════════════════════════════════════════════════════════════════
async def screen_history(qm, context: ContextTypes.DEFAULT_TYPE, handle: str, offset: int = 0):
    """История опубликованных постов канала — по 5 штук."""
    from database import db

    PAGE = 5

    with db.connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE channel_id = ? AND status = 'published'",
            (handle,),
        ).fetchone()[0]

        rows = conn.execute(
            """
            SELECT content, format, topic, published_at, image_url
            FROM posts
            WHERE channel_id = ? AND status = 'published'
            ORDER BY published_at DESC
            LIMIT ? OFFSET ?
            """,
            (handle, PAGE, offset),
        ).fetchall()

    if total == 0:
        text = f"📜 <b>История публикаций</b>\n{handle}\n\nПубликаций пока нет."
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ К каналу", callback_data=f"ui:ch:{handle}")],
        ])
        await _answer_or_send(qm, text, kb)
        return

    lines = [f"📜 <b>История публикаций</b>  {handle}\nВсего: {total} постов\n"]

    for i, row in enumerate(rows, start=offset + 1):
        content, fmt, topic, pub_at, image_url = row

        # Форматируем дату
        try:
            dt = datetime.fromisoformat(pub_at).strftime("%d.%m %H:%M")
        except Exception:
            dt = pub_at[:16] if pub_at else "?"

        # Превью текста — первые 80 символов без HTML-тегов
        import re
        preview = re.sub(r"<[^>]+>", "", content or "")[:80].strip()
        if len(content or "") > 80:
            preview += "…"

        img_icon = "🖼" if image_url else "📝"
        lines.append(f"<b>{i}.</b> {dt} · {fmt} {img_icon}\n<i>{preview}</i>\n")

    text = "\n".join(lines)

    # Навигация
    nav_buttons = []
    if offset > 0:
        nav_buttons.append(
            InlineKeyboardButton("⬅️ Назад", callback_data=f"ui:ch_history:{handle}:{max(0, offset - PAGE)}")
        )
    if offset + PAGE < total:
        nav_buttons.append(
            InlineKeyboardButton("➡️ Вперёд", callback_data=f"ui:ch_history:{handle}:{offset + PAGE}")
        )

    kb_rows = []
    if nav_buttons:
        kb_rows.append(nav_buttons)
    kb_rows.append([InlineKeyboardButton("◀️ К каналу", callback_data=f"ui:ch:{handle}")])

    await _answer_or_send(qm, text, InlineKeyboardMarkup(kb_rows))


# ══════════════════════════════════════════════════════════════════════════════
# ТЕСТ ГЕНЕРАЦИИ КАРТИНОК
# ══════════════════════════════════════════════════════════════════════════════

async def _do_img_test_generate(qm, context: ContextTypes.DEFAULT_TYPE, description: str):
    """Генерирует картинку по описанию и показывает результат."""
    from image_generator import generate_image
    from telegram.constants import ParseMode as PM

    # Определяем chat_id и удаляем старое сообщение
    if hasattr(qm, "message"):
        # CallbackQuery
        chat_id = qm.message.chat_id
        try:
            await qm.message.delete()
        except Exception:
            pass
    else:
        # Message
        chat_id = qm.chat_id
        try:
            await qm.delete()
        except Exception:
            pass

    # Отправляем новое сообщение-прогресс
    progress_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🎨 <b>Генерирую картинку...</b>\n\n"
            f"📝 <i>{description[:100]}</i>\n\n"
            "⏳ Обычно 3–10 секунд"
        ),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Отмена", callback_data="ui:img_test_cancel")
        ]]),
        parse_mode=PM.HTML,
    )

    try:
        url = await generate_image(topic=description, channel_topic="", channel_name="")
    except Exception as e:
        url = None
        logger.error(f"img_test error: {e}")

    kb_result = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Ещё раз",         callback_data="ui:img_test_retry")],
        [InlineKeyboardButton("✏️ Новое описание",   callback_data="ui:img_test")],
        [InlineKeyboardButton("◀️ В меню",           callback_data="ui:main")],
    ])

    # Удаляем прогресс-сообщение
    try:
        await progress_msg.delete()
    except Exception:
        pass

    if url:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=url,
            caption=f"✅ <b>Готово!</b>\n\n📝 <i>{description[:120]}</i>",
            reply_markup=kb_result,
            parse_mode=PM.HTML,
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text="😞 <b>Не удалось сгенерировать картинку.</b>\n\nПопробуй другое описание.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Попробовать снова", callback_data="ui:img_test_retry")],
                [InlineKeyboardButton("◀️ В меню",            callback_data="ui:main")],
            ]),
            parse_mode=PM.HTML,
        )


async def handle_img_test_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Вызывается из handle_image_url в bot.py.
    Возвращает True если сообщение было обработано как описание для теста.
    """
    if not context.user_data.get("waiting_img_test"):
        return False

    description = (update.message.text or "").strip()
    if not description:
        return False

    context.user_data.pop("waiting_img_test", None)
    context.user_data["last_img_test_desc"] = description

    await _do_img_test_generate(update.message, context, description)
    return True


# ══════════════════════════════════════════════════════════════════════════════
# ГЛАВНЫЙ РОУТЕР — обрабатывает все ui:* callback
# ══════════════════════════════════════════════════════════════════════════════

async def ui_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Центральный обработчик всех callback_data начинающихся с 'ui:'.
    Парсит callback_data и вызывает нужный экран/действие.
    """
    from config import cfg as _cfg
    query = update.callback_query

    if query.from_user.id not in _cfg.ADMIN_CHAT_IDS:
        await query.answer("Нет доступа.")
        return

    data = query.data  # например: "ui:ch:@hagenezykas"
    parts = data.split(":")  # ["ui", "ch", "@hagenezykas"]
    action = parts[1] if len(parts) > 1 else ""

    if action == "main":
        await screen_main(query, context)

    elif action == "channels":
        await screen_channels(query, context)

    elif action == "status":
        await screen_status(query, context)

    elif action == "queue":
        await screen_queue(query, context)

    elif action == "queue_clear_all":
        await action_clear_all_confirm(query, context)

    elif action == "queue_clear_all_ok":
        await action_clear_all_ok(query, context)

    elif action == "generate_all":
        await query.answer("⚡ Запускаю генерацию для всех каналов...")
        try:
            from content_generator import generator as gen
            result = await gen.run_for_all_channels()
            total = sum(r.get("generated", 0) for r in result.values()) if isinstance(result, dict) else 0
            text = f"✅ Генерация завершена!\nДобавлено постов: <b>{total}</b>"
        except Exception as e:
            text = f"❌ Ошибка генерации: {e}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="ui:main")]])
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)

    elif action == "ch" and len(parts) >= 3:
        handle = parts[2]
        await screen_channel_card(query, context, handle)

    elif action == "ch_settings" and len(parts) >= 3:
        handle = parts[2]
        await screen_channel_settings(query, context, handle)

    elif action == "ch_pause" and len(parts) >= 3:
        handle = parts[2]
        await action_pause_toggle(query, context, handle)

    elif action == "ch_delete" and len(parts) >= 3:
        handle = parts[2]
        await action_delete_confirm(query, context, handle)

    elif action == "ch_delete_ok" and len(parts) >= 3:
        handle = parts[2]
        await action_delete_ok(query, context, handle)

    elif action == "ch_clear" and len(parts) >= 3:
        handle = parts[2]
        await action_clear_buffer_confirm(query, context, handle)

    elif action == "ch_clear_ok" and len(parts) >= 3:
        handle = parts[2]
        await action_clear_buffer_ok(query, context, handle)

    elif action == "ch_generate" and len(parts) >= 3:
        handle = parts[2]
        await action_generate(query, context, handle)

    elif action == "ch_gen_run" and len(parts) >= 4:
        handle = parts[2]
        count = min(int(parts[3]), 20)  # максимум 20
        await action_generate_run(query, context, handle, count)

    elif action == "ch_postnow" and len(parts) >= 3:
        handle = parts[2]
        await action_postnow(query, context, handle)

    elif action == "ch_review" and len(parts) >= 3:
        handle = parts[2]
        # Делегируем в существующий _send_review_page из bot.py
        context.user_data["review_channel"] = handle
        # Импортируем функцию из bot.py
        from bot import _send_review_page
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=None)
        await _send_review_page(query.message, handle, offset=0)

    elif action == "ch_schedule" and len(parts) >= 3:
        handle = parts[2]
        await screen_schedule(query, context, handle)

    elif action == "ch_sched_toggle" and len(parts) >= 4:
        handle   = parts[2]
        hour_msk = int(parts[3])
        await action_schedule_toggle(query, context, handle, hour_msk)

    elif action == "ch_sched_clear" and len(parts) >= 3:
        handle = parts[2]
        await action_schedule_clear(query, context, handle)

    elif action == "ch_images_toggle" and len(parts) >= 3:
        handle = parts[2]
        await action_images_toggle(query, context, handle)

    elif action == "ch_sched_custom" and len(parts) >= 3:
        handle = parts[2]
        context.user_data["editing"] = {"handle": handle, "field": "schedule_add"}
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Отмена", callback_data=f"ui:ch_schedule:{handle}")
        ]])
        await query.answer()
        await query.edit_message_text(
            f"➕ <b>Добавить своё время</b>\n\n"
            f"Напиши час или несколько через пробел (МСК):\n"
            f"Например: <code>8</code> или <code>8 14 22</code>",
            reply_markup=kb, parse_mode=ParseMode.HTML,
        )

    elif action == "ch_sched_copy" and len(parts) >= 3:
        handle = parts[2]
        channels = _load_channels()
        others = [c for c in channels if c["channel_id"] != handle]
        if not others:
            await query.answer("Нет других каналов для копирования.", show_alert=True)
            return
        btns = [[InlineKeyboardButton(
            c.get("name", c["channel_id"]),
            callback_data=f"ui:ch_sched_copy_ok:{handle}:{c['channel_id']}"
        )] for c in others]
        btns.append([InlineKeyboardButton("◀️ Отмена", callback_data=f"ui:ch_schedule:{handle}")])
        await query.answer()
        await query.edit_message_text(
            "📋 <b>Скопировать расписание с:</b>",
            reply_markup=InlineKeyboardMarkup(btns), parse_mode=ParseMode.HTML,
        )

    elif action == "ch_sched_copy_ok" and len(parts) >= 4:
        handle      = parts[2]
        src_handle  = parts[3]
        ch_dst = _load_channel(handle)
        ch_src = _load_channel(src_handle)
        if ch_dst and ch_src:
            ch_dst["post_times_utc"] = ch_src.get("post_times_utc", [6, 9, 13, 17])
            _save_channel(ch_dst)
            await query.answer("✅ Расписание скопировано!")
        await screen_schedule(query, context, handle)

    elif action == "ch_history" and len(parts) >= 3:
        handle = parts[2]
        offset = int(parts[3]) if len(parts) >= 4 else 0
        await screen_history(query, context, handle, offset)

    elif action == "ch_set" and len(parts) >= 4:
        handle = parts[2]
        field  = parts[3]
        await screen_edit_field(query, context, handle, field)

    elif action == "ch_set_img" and len(parts) >= 4:
        handle = parts[2]
        source = parts[3]
        await screen_set_image_source(query, context, handle, source)

    elif action == "rsy_toggle" and len(parts) >= 3:
        handle = parts[2]
        ch = _load_channel(handle)
        if ch:
            ch["rsy_override"] = not ch.get("rsy_override", False)
            _save_channel(ch)
            state = "включено ✅" if ch["rsy_override"] else "выключено ⬜️"
            await query.answer(f"Перекрытие РСЯ {state}")
        await screen_channel_settings(query, context, handle)

    elif action == "ch_archetype" and len(parts) >= 3:
        handle = parts[2]
        await screen_archetype_picker(query, context, handle)

    elif action == "ch_set_arche" and len(parts) >= 4:
        handle = parts[2]
        new_arche = parts[3]
        ch = _load_channel(handle)
        if ch:
            ch["archetype"] = new_arche
            _save_channel(ch)
            await query.answer(f"Стиль обновлён: {new_arche}")
        await screen_channel_settings(query, context, handle)

    elif action == "ch_source_toggle" and len(parts) >= 3:
        handle = parts[2]
        ch = _load_channel(handle)
        if ch:
            ch["topic_source"] = "rss" if ch.get("topic_source") == "search" else "search"
            _save_channel(ch)
            label = "веб-поиск 🌐" if ch["topic_source"] == "search" else "RSS 📡"
            await query.answer(f"Источник тем: {label}")
        await screen_channel_settings(query, context, handle)

    elif action == "ch_src_mode" and len(parts) >= 3:
        handle = parts[2]
        ch = _load_channel(handle)
        if ch:
            ch["topic_source"] = "rss" if ch.get("topic_source") == "search" else "search"
            _save_channel(ch)
            await query.answer(
                "🌐 Режим: Авто (веб-поиск)" if ch["topic_source"] == "search"
                else "📰 Режим: по лентам"
            )
        await screen_rss_manager(query, context, handle)

    elif action == "ch_refs" and len(parts) >= 3:
        await screen_references(query, context, parts[2])

    elif action == "ref_tgl" and len(parts) >= 5:
        await action_ref_toggle(query, context, parts[2], int(parts[3]), parts[4])

    elif action == "ref_del" and len(parts) >= 4:
        await action_ref_delete(query, context, parts[2], int(parts[3]))

    elif action == "ref_add" and len(parts) >= 3:
        await action_ref_add(query, context, parts[2])

    elif action == "ref_take" and len(parts) >= 3:
        await screen_ref_take_count(query, context, parts[2])

    elif action == "ref_go" and len(parts) >= 4:
        await action_ref_import(query, context, parts[2], int(parts[3]))

    elif action == "ref_import" and len(parts) >= 3:
        await action_ref_import(query, context, parts[2])

    elif action == "rss_del" and len(parts) >= 4:
        handle = parts[2]
        idx = int(parts[3])
        await action_rss_delete(query, context, handle, idx)

    elif action == "rss_clear" and len(parts) >= 3:
        handle = parts[2]
        await action_rss_clear(query, context, handle)

    elif action == "rss_add" and len(parts) >= 3:
        handle = parts[2]
        await action_rss_add_prompt(query, context, handle)

    elif action == "rss_ai" and len(parts) >= 3:
        handle = parts[2]
        await query.answer("✨ Анализирую тему канала...")
        await action_rss_ai_suggest(query, context, handle)

    elif action == "rss_ai_ok" and len(parts) >= 3:
        handle = parts[2]
        await action_rss_ai_confirm(query, context, handle)

    elif action == "img_test":
        await query.answer()
        context.user_data["waiting_img_test"] = True
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Отмена", callback_data="ui:img_test_cancel")
        ]])
        await query.edit_message_text(
            "🎨 <b>Тест генерации картинок</b>\n\n"
            "Опиши что хочешь увидеть на картинке (на русском или английском).\n\n"
            "Например:\n"
            "<i>Майнкрафт, ночной лес с факелами и замком вдали</i>\n"
            "<i>Красивый закат над горами, фотореализм</i>",
            reply_markup=kb, parse_mode=ParseMode.HTML,
        )

    elif action == "img_test_cancel":
        context.user_data.pop("waiting_img_test", None)
        await screen_main(query, context)

    elif action == "img_test_retry":
        # Повторная генерация с тем же описанием
        await query.answer("🔄 Генерирую снова...")
        description = context.user_data.get("last_img_test_desc", "")
        if not description:
            await screen_main(query, context)
            return
        await _do_img_test_generate(query, context, description)

    elif action == "noop":
        await query.answer()

    else:
        await query.answer(f"Неизвестное действие: {data}")
