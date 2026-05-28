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


def _load_channel(handle: str) -> dict | None:
    """Загружает карточку одного канала по handle."""
    slug = handle.lstrip("@")
    path = Path(__file__).parent / "channels" / f"{slug}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_channel(ch: dict):
    """Сохраняет карточку канала."""
    slug = ch["channel_id"].lstrip("@")
    path = Path(__file__).parent / "channels" / f"{slug}.json"
    path.write_text(json.dumps(ch, ensure_ascii=False, indent=2), encoding="utf-8")


def _utc_to_msk(hours: list[int]) -> list[int]:
    return sorted((h + 3) % 24 for h in hours)


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
        [InlineKeyboardButton("➕ Добавить канал", callback_data="add_start")],
        [
            InlineKeyboardButton("📋 Мои каналы", callback_data="ui:channels"),
            InlineKeyboardButton("📊 Статус",     callback_data="ui:status"),
        ],
        [
            InlineKeyboardButton("📝 Очередь постов", callback_data="ui:queue"),
        ],
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

    # Расписание
    post_hours_utc = ch.get("post_times_utc", [6, 9, 13, 17])
    msk_hours = _utc_to_msk(post_hours_utc)
    schedule_str = " · ".join(f"{h:02d}:00" for h in msk_hours) + " МСК"

    # Картинки
    if ch.get("reddit_image_subreddits"):
        subs = ", ".join(f"r/{s}" for s in ch["reddit_image_subreddits"][:3])
        img_str = f"Reddit ({subs})"
    elif ch.get("use_images"):
        img_str = "Pexels/Unsplash"
    else:
        img_str = "Без картинок"

    # Статус паузы
    pause_line = "\n⏸ <b>На паузе</b> — публикации остановлены" if paused else ""

    text = (
        f"<b>{name}</b>  <code>{channel_id}</code>\n"
        f"{ch_type} · {topic}{pause_line}\n\n"
        f"📬 Буфер: <b>{lvl} постов</b> {buf_icon}\n"
        f"⏰ Расписание: {schedule_str}\n"
        f"🖼 Картинки: {img_str}\n"
        f"📰 RSS: {len(ch.get('rss_sources', []))} источников"
    )

    pause_btn = (
        InlineKeyboardButton("▶️ Возобновить", callback_data=f"ui:ch_pause:{channel_id}")
        if paused else
        InlineKeyboardButton("⏸ Остановить",  callback_data=f"ui:ch_pause:{channel_id}")
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

    msk_hours  = _utc_to_msk(ch.get("post_times_utc", [6, 9, 13, 17]))
    sched_str  = " ".join(f"{h:02d}" for h in msk_hours)

    if ch.get("reddit_image_subreddits"):
        img_str = "Reddit: " + ", ".join(ch["reddit_image_subreddits"][:2])
    elif ch.get("use_images"):
        img_str = "Pexels/Unsplash"
    else:
        img_str = "Без картинок"

    forbidden = ch.get("forbidden_topics", [])
    forb_str  = f"{len(forbidden)} тем" if forbidden else "не задано"

    rss_count = len(ch.get("rss_sources", []))
    wb_cats   = ch.get("wb_categories", [])
    wb_str    = ", ".join(wb_cats[:2]) + ("..." if len(wb_cats) > 2 else "") if wb_cats else "все категории"

    is_wb = ch.get("channel_type") == "marketplace"

    rows = [
        [InlineKeyboardButton(f"📌 Тема: {topic[:35]}", callback_data=f"ui:ch_set:{channel_id}:topic")],
        [InlineKeyboardButton(f"🎨 Тон: {tone[:35]}",   callback_data=f"ui:ch_set:{channel_id}:tone")],
        [InlineKeyboardButton(f"📅 Расписание: {sched_str} МСК", callback_data=f"ui:ch_set:{channel_id}:schedule")],
        [InlineKeyboardButton(f"🔢 Постов в день: {posts_day}", callback_data=f"ui:ch_set:{channel_id}:posts_count")],
        [InlineKeyboardButton(f"📏 Длина поста: {post_len}",    callback_data=f"ui:ch_set:{channel_id}:post_length")],
        [InlineKeyboardButton(f"🖼 Картинки: {img_str}",        callback_data=f"ui:ch_set:{channel_id}:images")],
        [InlineKeyboardButton(f"📰 RSS-источники ({rss_count})",callback_data=f"ui:ch_set:{channel_id}:rss")],
        [InlineKeyboardButton(f"🚫 Запрещённые темы: {forb_str}", callback_data=f"ui:ch_set:{channel_id}:forbidden")],
    ]

    if is_wb:
        rows.insert(4, [InlineKeyboardButton(f"📦 Категории WB: {wb_str}", callback_data=f"ui:ch_set:{channel_id}:wb_categories")])

    rows.append([InlineKeyboardButton("◀️ Назад к каналу", callback_data=f"ui:ch:{channel_id}")])

    await _answer_or_send(
        qm,
        f"⚙️ <b>Настройки</b>  <code>{channel_id}</code>\n\nНажми параметр чтобы изменить:",
        InlineKeyboardMarkup(rows),
    )


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
        [InlineKeyboardButton("⚡ Сгенерировать для всех", callback_data="ui:generate_all")],
        [InlineKeyboardButton("◀️ Назад",                  callback_data="ui:main")],
    ])
    await _answer_or_send(qm, "\n".join(lines), kb)


async def screen_queue(qm, context: ContextTypes.DEFAULT_TYPE):
    """Выбор канала для просмотра очереди постов."""
    channels = _load_channels()
    buttons = []
    for ch in channels:
        lvl  = buffer.get_level(ch["channel_id"])
        icon = "📭" if lvl == 0 else "📋"
        buttons.append([InlineKeyboardButton(
            f"{icon} {ch['channel_id']} · {lvl}",
            callback_data=f"ui:ch_review:{ch['channel_id']}"
        )])
    buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="ui:main")])

    await _answer_or_send(
        qm,
        "📝 <b>Очередь постов</b>\n\nВыбери канал:",
        InlineKeyboardMarkup(buttons),
    )


# ── Действия с каналом ────────────────────────────────────────────────────

async def action_pause_toggle(qm, context, handle: str):
    """Переключает паузу канала."""
    ch = _load_channel(handle)
    if not ch:
        return
    if ch.get("schedule_disabled"):
        ch.pop("schedule_disabled", None)
        msg = f"▶️ Канал {handle} возобновлён."
    else:
        ch["schedule_disabled"] = True
        msg = f"⏸ Канал {handle} поставлен на паузу."
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
    slug = handle.lstrip("@")
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


async def action_generate(qm, context, handle: str):
    """Запускает экстренную генерацию для одного канала."""
    from telegram import CallbackQuery
    if isinstance(qm, CallbackQuery):
        await qm.answer("⚡ Запускаю генерацию...")

    # Показываем прогресс
    progress_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⏳ Генерация...", callback_data="noop")
    ]])

    if isinstance(qm, CallbackQuery):
        try:
            await qm.edit_message_reply_markup(reply_markup=progress_kb)
        except Exception:
            pass

    try:
        from content_generator import generator as _gen
        result = await _gen.run_emergency(handle)
        generated = result.get("generated", 0)
        text = (
            f"✅ Генерация завершена для <b>{handle}</b>\n"
            f"Добавлено постов: <b>{generated}</b>"
        )
    except Exception as e:
        text = f"❌ Ошибка генерации для {handle}:\n{e}"

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("◀️ Назад к каналу", callback_data=f"ui:ch:{handle}")
    ]])
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


# ── Редактирование настроек ───────────────────────────────────────────────

# Конфиг редактируемых полей: field_key → (заголовок, подсказка, тип)
EDITABLE_FIELDS = {
    "topic":       ("📌 Тема канала",        "Введи новую тему канала\nНапример: <i>Майнкрафт советы и новости</i>",      "text"),
    "tone":        ("🎨 Тон общения",         "Введи желаемый тон\nНапример: <i>дружелюбный, с юмором, без снобизма</i>", "text"),
    "schedule":    ("📅 Расписание",          "Введи часы публикации МСК через пробел\nНапример: <code>9 12 16 20</code>",  "schedule"),
    "posts_count": ("🔢 Постов в день",       "Введи число постов в день (от 1 до 30)\nНапример: <code>10</code>",         "int"),
    "post_length": ("📏 Длина поста",         "Введи диапазон слов\nНапример: <code>100–200 слов</code>",                  "text"),
    "images":      ("🖼 Источник картинок",   None,  "images_menu"),
    "rss":         ("📰 RSS-источники",       "Пришли URL через запятую или с новой строки.\nНапиши <b>очистить</b> чтобы убрать все.", "rss"),
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
        if text.lower() == "очистить":
            ch["rss_sources"] = []
            success_msg = "✅ RSS-источники очищены."
        else:
            urls = [u.strip() for u in text.replace("\n", ",").split(",") if u.strip().startswith("http")]
            if urls:
                ch["rss_sources"] = list(dict.fromkeys(ch.get("rss_sources", []) + urls))
                success_msg = f"✅ Добавлено {len(urls)} RSS-источников. Всего: {len(ch['rss_sources'])}"
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
        # Показываем успех + кнопку вернуться к настройкам
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
# ГЛАВНЫЙ РОУТЕР — обрабатывает все ui:* callback
# ══════════════════════════════════════════════════════════════════════════════

async def ui_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Центральный обработчик всех callback_data начинающихся с 'ui:'.
    Парсит callback_data и вызывает нужный экран/действие.
    """
    from config import cfg as _cfg
    query = update.callback_query

    if query.from_user.id != _cfg.ADMIN_CHAT_ID:
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

    elif action == "ch_generate" and len(parts) >= 3:
        handle = parts[2]
        await action_generate(query, context, handle)

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

    elif action == "ch_set" and len(parts) >= 4:
        handle = parts[2]
        field  = parts[3]
        await screen_edit_field(query, context, handle, field)

    elif action == "ch_set_img" and len(parts) >= 4:
        handle = parts[2]
        source = parts[3]
        await screen_set_image_source(query, context, handle, source)

    elif action == "noop":
        await query.answer()

    else:
        await query.answer(f"Неизвестное действие: {data}")
