"""
bot.py — Главный файл: Telegram бот администратора

Что делает этот бот:
  1. Запускает генерацию контента для каналов
  2. Публикует посты из буфера в каналы
  3. Отправляет алерты (буфер низкий, пост не перекрыт и т.д.)
  4. Управляет списком каналов

Команды:
  /start    — приветствие
  /status   — состояние буферов всех каналов
  /list     — список каналов
  /add      — добавить канал
  /generate — запустить генерацию для всех каналов
  /preview  — сгенерировать и показать пост (без сохранения)
  /post_now — опубликовать следующий пост из буфера немедленно

Запуск:
  python bot.py
"""

import asyncio
import json
from pathlib import Path
from datetime import datetime, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)
from telegram.constants import ParseMode
from loguru import logger

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import cfg
from database import db
from buffer_manager import buffer
from content_generator import generator
from poster import poster


# ============================================================
# Состояния ConversationHandler
# ============================================================
WAITING_EDITED_TEXT = 1

# Состояния для /add (добавление канала — пошаговый диалог)
ADD_HANDLE, ADD_NAME, ADD_TOPIC, ADD_TONE, ADD_FORBIDDEN, ADD_RSS_CONFIRM, ADD_POSTS_COUNT = range(10, 17)
# Новые состояния: выбор метода + экспорт-флоу + channel_type
ADD_CHOOSE_METHOD, ADD_WAITING_EXPORT, ADD_EXPORT_HANDLE, ADD_EXPORT_CONFIRM, ADD_CHANNEL_TYPE = range(17, 22)


# ============================================================
# Вспомогательные функции
# ============================================================

def is_admin(user_id: int) -> bool:
    """Проверяет что команду отправил администратор."""
    return user_id == cfg.ADMIN_CHAT_ID


def load_all_channels() -> list[dict]:
    """Загружает все активные карточки каналов из папки channels/."""
    channels_dir = Path(__file__).parent / "channels"
    channels = []
    for json_file in channels_dir.glob("*.json"):
        if json_file.name.startswith("example_"):
            continue
        try:
            with open(json_file, encoding="utf-8") as f:
                ch = json.load(f)
            if ch.get("active", True):
                channels.append(ch)
        except Exception as e:
            logger.error(f"Ошибка загрузки карточки {json_file}: {e}")
    return channels


def format_post_message(post: dict, index: int = 0, total: int = 0) -> str:
    """Форматирует сообщение с постом для просмотра админом."""
    channel_id = post.get("channel_id", "?")
    fmt = post.get("format", "?")
    topic = post.get("topic", "")[:60]
    content = post.get("content", "")
    has_image = "🖼 Есть картинка" if post.get("image_url") else "📄 Без картинки"
    counter = f"Пост {index} из {total} · " if total > 0 else ""

    return (
        f"📋 {counter}<b>{channel_id}</b>\n"
        f"🎨 Формат: {fmt} · {has_image}\n"
        f"💡 Тема: {topic}\n"
        f"{'─' * 30}\n\n"
        f"{content}"
    )


def review_keyboard(post_id: str) -> InlineKeyboardMarkup:
    """
    Кнопки для просмотра/редактирования поста в очереди.
    Пост уже готов к публикации — кнопки только для правок.
    """
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Изменить текст", callback_data=f"edit:{post_id}"),
            InlineKeyboardButton("🖼 Картинку",        callback_data=f"image:{post_id}"),
        ],
        [
            InlineKeyboardButton("🔄 Перегенерировать", callback_data=f"regen:{post_id}"),
            InlineKeyboardButton("🗑 Удалить",           callback_data=f"delete:{post_id}"),
        ],
        [
            InlineKeyboardButton("📤 Опубликовать сейчас", callback_data=f"postnow:{post_id}"),
        ],
    ])


# ============================================================
# Управление каналами — сохранение/удаление
# ============================================================

def save_channel_card(channel: dict):
    """
    Сохраняет карточку канала в JSON файл и регистрирует в БД.
    Имя файла = handle без @ (например finance_channel.json).
    """
    channels_dir = Path(__file__).parent / "channels"
    handle_clean = channel["channel_id"].lstrip("@")
    file_path = channels_dir / f"{handle_clean}.json"

    # Сохраняем JSON
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(channel, f, ensure_ascii=False, indent=2)

    # Регистрируем в БД
    with db.connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO channels
               (tg_handle, name, topic, tone, config_json, active)
               VALUES (?, ?, ?, ?, ?, 1)""",
            (channel["channel_id"], channel["name"],
             channel["topic"], channel["tone"],
             json.dumps(channel, ensure_ascii=False)),
        )

    logger.info(f"Канал сохранён: {channel['channel_id']}")


def deactivate_channel(channel_id: str):
    """Деактивирует канал — ставит active=false в JSON и БД."""
    channels_dir = Path(__file__).parent / "channels"
    handle_clean = channel_id.lstrip("@")
    file_path = channels_dir / f"{handle_clean}.json"

    if file_path.exists():
        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)
        data["active"] = False
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    with db.connect() as conn:
        conn.execute(
            "UPDATE channels SET active = 0 WHERE tg_handle = ?",
            (channel_id,),
        )

    logger.info(f"Канал деактивирован: {channel_id}")


# ============================================================
# Команды бота
# ============================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приветствие при первом запуске."""
    if not is_admin(update.effective_user.id):
        return

    await update.message.reply_text(
        "👋 <b>Content Factory Bot</b>\n\n"
        "Я управляю автопостингом в твоих Telegram-каналах.\n\n"
        "<b>Управление каналами:</b>\n"
        "/list — список всех каналов\n"
        "/add — добавить канал\n\n"
        "<b>Контент:</b>\n"
        "/status — состояние буферов\n"
        "/generate — запустить генерацию\n"
        "/review — посмотреть посты в очереди\n"
        "/preview — превью поста\n"
        "/post_now — опубликовать сейчас\n",
        parse_mode=ParseMode.HTML,
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает список всех каналов с кнопками управления."""
    if not is_admin(update.effective_user.id):
        return

    channels = load_all_channels()
    if not channels:
        await update.message.reply_text(
            "Каналов пока нет.\nДобавь первый канал командой /add"
        )
        return

    for ch in channels:
        level = buffer.get_level(ch["channel_id"])
        status = buffer.check_status(ch["channel_id"])
        icon = {"ok": "✅", "low": "⚠️", "emergency": "🔴", "critical": "🚨"}.get(status, "❓")

        text = (
            f"{icon} <b>{ch['name']}</b>\n"
            f"Handle: {ch['channel_id']}\n"
            f"Тема: {ch['topic'][:60]}\n"
            f"Тон: {ch['tone'][:40]}\n"
            f"Буфер: {level} постов"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✏️ Изменить тему", callback_data=f"settopic:{ch['channel_id']}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"removech:{ch['channel_id']}"),
        ]])
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def cmd_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начинает диалог добавления нового канала — предлагает выбор метода."""
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    context.user_data.clear()
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📂 Загрузить экспорт Telegram", callback_data="addmethod:export"),
        InlineKeyboardButton("✏️ Описать вручную",           callback_data="addmethod:manual"),
    ]])
    await update.message.reply_text(
        "➕ <b>Добавление канала</b>\n\n"
        "Как хочешь настроить канал?\n\n"
        "📂 <b>Экспорт Telegram</b> — скинь <code>result.json</code> из Telegram Desktop, "
        "и бот сам определит тему, тон, тип канала и вечнозелёные темы.\n\n"
        "✏️ <b>Вручную</b> — пошаговый диалог с вопросами.\n\n"
        "/cancel — отменить",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    return ADD_CHOOSE_METHOD


async def cmd_add_handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получает handle канала. Принимает любой формат:
       @channel, https://t.me/channel, t.me/channel, просто channel
    """
    text = update.message.text.strip()

    # Вытаскиваем handle из любого формата
    if "t.me/" in text:
        # https://t.me/channel или t.me/channel
        handle = "@" + text.split("t.me/")[-1].strip("/").split("?")[0]
    elif text.startswith("@"):
        handle = text
    else:
        handle = f"@{text}"

    # Убираем лишние символы которые не могут быть в handle
    handle = handle.split()[0]  # берём только первое слово

    # Проверяем нет ли уже такого канала
    existing = load_all_channels()
    if any(ch["channel_id"] == handle for ch in existing):
        await update.message.reply_text(
            f"❌ Канал {handle} уже добавлен.\n/cancel — отменить"
        )
        return ADD_HANDLE

    context.user_data["new_channel"] = {"channel_id": handle}
    await update.message.reply_text(
        f"✅ Handle: <b>{handle}</b>\n\n"
        f"Шаг 2/5: Как называется канал?\n"
        f"Например: <i>Финансы для людей</i>",
        parse_mode=ParseMode.HTML,
    )
    return ADD_NAME


async def cmd_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получает название канала."""
    context.user_data["new_channel"]["name"] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ Название сохранено.\n\n"
        f"Шаг 3/5: <b>Тема канала</b> — о чём пишем?\n"
        f"Например: <i>личные финансы, инвестиции, сбережения</i>",
        parse_mode=ParseMode.HTML,
    )
    return ADD_TOPIC


async def cmd_add_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получает тему канала, затем спрашивает тип канала."""
    context.user_data["new_channel"]["topic"] = update.message.text.strip()
    await update.message.reply_text(
        "✅ Тема сохранена.\n\n"
        "Шаг 3.5/5: <b>Тип канала</b>\n\n"
        "📝 <b>Контент</b> — пишем посты: новости, советы, факты, разборы\n"
        "🛍 <b>Маркетплейс</b> — постим товары с WB/Ozon (цена, фото, ссылка)",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📝 Контент-канал",     callback_data="channeltype:content"),
            InlineKeyboardButton("🛍 Маркетплейс WB/Ozon", callback_data="channeltype:marketplace"),
        ]]),
    )
    return ADD_CHANNEL_TYPE


async def cmd_add_tone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получает тон общения."""
    context.user_data["new_channel"]["tone"] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ Тон сохранён.\n\n"
        f"Шаг 5/5: <b>Запрещённые темы</b> — что нельзя упоминать?\n"
        f"Перечисли через запятую или напиши <i>нет</i>",
        parse_mode=ParseMode.HTML,
    )
    return ADD_FORBIDDEN


async def cmd_add_forbidden(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получает запрещённые темы, затем автоматически подбирает RSS через Claude."""
    from ai_client import suggest_rss_sources, suggest_evergreen_topics

    text = update.message.text.strip()
    forbidden = [] if text.lower() == "нет" else [t.strip() for t in text.split(",")]
    context.user_data["new_channel"]["forbidden_topics"] = forbidden

    ch = context.user_data["new_channel"]

    msg = await update.message.reply_text(
        "⏳ Подбираю RSS-источники и вечнозелёные темы под тематику канала...",
    )

    # Claude подбирает RSS и вечнозелёные темы
    rss_urls = await suggest_rss_sources(ch["topic"], ch["name"])
    evergreen = await suggest_evergreen_topics(ch["topic"], count=8)

    context.user_data["new_channel"]["rss_sources"] = rss_urls
    context.user_data["new_channel"]["evergreen_topics"] = evergreen

    rss_text = "\n".join(f"  • {url}" for url in rss_urls) if rss_urls else "  (не найдено)"

    await msg.edit_text(
        f"📡 <b>Claude подобрал RSS-источники для темы «{ch['topic']}»:</b>\n\n"
        f"{rss_text}\n\n"
        f"Подтверди или пришли свои URL через запятую.\n"
        f"Напиши <b>ок</b> чтобы принять предложенные.",
        parse_mode=ParseMode.HTML,
    )
    return ADD_RSS_CONFIRM


async def cmd_add_rss_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждает или заменяет RSS-источники, затем спрашивает кол-во постов."""
    text = update.message.text.strip()
    ch = context.user_data["new_channel"]
    CONFIRM_WORDS = ("ок", "ok", "да", "yes", "принять", "подтвердить")

    if text.lower() in CONFIRM_WORDS:
        # Принимаем предложенные Claude источники
        pass
    else:
        # Проверяем что в тексте есть хоть один валидный URL
        custom_urls = [
            u.strip()
            for u in text.replace(",", "\n").splitlines()
            if u.strip().startswith("http")
        ]
        if custom_urls:
            # Объединяем — добавляем свои к предложенным Claude, без дублей
            existing = ch.get("rss_sources", [])
            combined = existing + [u for u in custom_urls if u not in existing]
            ch["rss_sources"] = combined
            await update.message.reply_text(
                f"➕ Добавлено {len(custom_urls)} URL к {len(existing)} предложенным Claude.\n"
                f"Итого источников: {len(combined)}\n\n"
                f"Напиши <b>ок</b> чтобы подтвердить, или пришли ещё URL.",
                parse_mode=ParseMode.HTML,
            )
            return ADD_RSS_CONFIRM
        else:
            # Ни "ок", ни URL — просим повторить
            rss_text = "\n".join(f"  • {url}" for url in ch.get("rss_sources", []))
            await update.message.reply_text(
                f"⚠️ Не понял ответ.\n\n"
                f"Напиши <b>ок</b> чтобы принять эти источники:\n{rss_text}\n\n"
                f"Или пришли свои URL (каждый с новой строки или через запятую).",
                parse_mode=ParseMode.HTML,
            )
            return ADD_RSS_CONFIRM  # остаёмся на том же шаге

    # Спрашиваем сколько постов генерировать в день
    await update.message.reply_text(
        "✅ RSS-источники сохранены.\n\n"
        "Сколько постов генерировать в день для этого канала?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("4 поста", callback_data="postscount:4"),
            InlineKeyboardButton("10 постов", callback_data="postscount:10"),
            InlineKeyboardButton("20 постов", callback_data="postscount:20"),
        ]]),
    )
    return ADD_POSTS_COUNT


async def cmd_add_posts_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получает выбранное количество постов и завершает создание канала."""
    query = update.callback_query
    await query.answer()

    count = int(query.data.split(":")[1])
    ch = context.user_data["new_channel"]
    ch["daily_posts_count"] = count

    channel_type = ch.get("channel_type", "content")

    # Финальная сборка карточки
    base_defaults = {
        "audience": "широкая аудитория",
        "post_length": "100–200 слов",
        "use_emoji": True,
        "active": True,
        "example_posts": [],
        "use_images": False,
    }

    if channel_type == "marketplace":
        base_defaults["post_formats"] = ["wb_product"]
        base_defaults["image_keywords"] = []
        base_defaults.setdefault("tone", "продающий, дружелюбный")
    else:
        base_defaults["post_formats"] = ["совет дня", "факт/статистика", "вопрос аудитории", "мини-разбор", "инфоповод"]
        base_defaults["image_keywords"] = [t.strip() for t in ch.get("topic", "").split(",")][:3]

    ch.update(base_defaults)

    save_channel_card(ch)

    # Добавляем вечнозелёные темы в БД (для контент-каналов)
    buffer.add_evergreen_topics(ch["channel_id"], ch.get("evergreen_topics", []))

    rss_count = len(ch.get("rss_sources", []))
    eg_count = len(ch.get("evergreen_topics", []))
    ch_type_label = "🛍 Маркетплейс" if channel_type == "marketplace" else "📝 Контент"

    await query.edit_message_text(
        f"🎉 <b>Канал добавлен!</b>\n\n"
        f"Handle: {ch['channel_id']}\n"
        f"Название: {ch['name']}\n"
        f"Тип: {ch_type_label}\n"
        f"Тема: {ch.get('topic', '—')}\n"
        f"Постов в день: {count}\n"
        + (f"RSS-источников: {rss_count}\nВечнозелёных тем: {eg_count}\n" if channel_type == "content" else "") +
        f"\n<b>Следующие шаги:</b>\n"
        f"1. Добавь бота администратором в канал {ch['channel_id']}\n"
        f"2. Запусти /generate чтобы создать первые посты",
        parse_mode=ParseMode.HTML,
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Универсальная отмена любого диалога."""
    context.user_data.clear()
    await update.message.reply_text("❌ Отменено.")
    return ConversationHandler.END


async def cmd_add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отменяет добавление канала."""
    context.user_data.clear()
    await update.message.reply_text("❌ Добавление канала отменено.")
    return ConversationHandler.END


# ============================================================
# /add — выбор метода: экспорт или вручную
# ============================================================

async def handle_add_method_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает выбор метода добавления канала."""
    query = update.callback_query
    await query.answer()

    method = query.data.split(":")[1]  # "export" или "manual"

    if method == "export":
        await query.edit_message_text(
            "📂 <b>Загрузка экспорта</b>\n\n"
            "Как экспортировать канал:\n"
            "1. Открой Telegram Desktop\n"
            "2. Зайди в канал → ⋮ меню → <b>Экспорт истории чата</b>\n"
            "3. Формат: <b>JSON</b>, галочки на медиа убрать\n"
            "4. Пришли файл <code>result.json</code> сюда\n\n"
            "/cancel — отменить",
            parse_mode=ParseMode.HTML,
        )
        return ADD_WAITING_EXPORT

    else:  # manual
        await query.edit_message_text(
            "✏️ <b>Ручное добавление</b>\n\n"
            "Шаг 1: Пришли <b>handle</b> канала.\n"
            "Например: <code>@my_finance_channel</code>\n\n"
            "/cancel — отменить",
            parse_mode=ParseMode.HTML,
        )
        return ADD_HANDLE


# ============================================================
# /add — экспорт-флоу
# ============================================================

async def handle_export_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принимает JSON-файл экспорта, анализирует через Claude."""
    from channel_analyzer import analyzer

    doc = update.message.document
    if not doc:
        await update.message.reply_text(
            "⚠️ Пришли JSON-файл экспорта.\n/cancel — отменить"
        )
        return ADD_WAITING_EXPORT

    # Проверяем что это JSON
    if not (doc.file_name or "").endswith(".json") and doc.mime_type != "application/json":
        await update.message.reply_text(
            "⚠️ Нужен файл <code>result.json</code> в формате JSON.\n"
            "/cancel — отменить",
            parse_mode=ParseMode.HTML,
        )
        return ADD_WAITING_EXPORT

    # Проверяем размер (не больше 50 МБ)
    if doc.file_size and doc.file_size > 50 * 1024 * 1024:
        await update.message.reply_text(
            "⚠️ Файл слишком большой (>50 МБ). Попробуй сделать экспорт "
            "только последних нескольких месяцев.\n/cancel — отменить"
        )
        return ADD_WAITING_EXPORT

    msg = await update.message.reply_text("⏳ Читаю файл и анализирую канал...")

    try:
        # Скачиваем файл
        tg_file = await doc.get_file()
        file_bytes = await tg_file.download_as_bytearray()

        # Анализируем через Claude
        analysis = await analyzer.analyze_from_bytes(bytes(file_bytes), doc.file_name)

        # Сохраняем анализ в user_data
        context.user_data["export_analysis"] = analysis
        context.user_data["new_channel"] = {}

        # Форматируем превью
        ch_type_label = (
            "🛍 Маркетплейс (WB/Ozon)" if analysis.get("channel_type") == "marketplace"
            else "📝 Контент-канал"
        )
        confidence_pct = int(analysis.get("confidence", 0.8) * 100)
        evergreen_preview = "\n".join(
            f"  • {t}" for t in analysis.get("evergreen_topics", [])[:5]
        )

        await msg.edit_text(
            f"✅ <b>Анализ завершён</b> (уверенность: {confidence_pct}%)\n\n"
            f"📌 <b>Канал:</b> {analysis.get('export_channel_name', '?')}\n"
            f"🏷 <b>Тип:</b> {ch_type_label}\n"
            f"📝 <b>Тема:</b> {analysis.get('topic', '?')}\n"
            f"🎤 <b>Тон:</b> {analysis.get('tone', '?')}\n"
            f"📊 <b>Частота:</b> ~{analysis.get('post_frequency', 3)} поста/день\n"
            f"💡 <b>Вечнозелёные темы ({len(analysis.get('evergreen_topics', []))}):</b>\n"
            f"{evergreen_preview}\n\n"
            f"🔍 <i>{analysis.get('analysis_notes', '')}</i>\n\n"
            f"Теперь укажи <b>@handle</b> канала в Telegram:\n"
            f"(например <code>@my_channel</code>)\n\n"
            f"/cancel — отменить",
            parse_mode=ParseMode.HTML,
        )
        return ADD_EXPORT_HANDLE

    except ValueError as e:
        await msg.edit_text(
            f"❌ <b>Ошибка анализа:</b> {e}\n\n"
            "Убедись что файл — это <code>result.json</code> от Telegram.\n"
            "/cancel — отменить",
            parse_mode=ParseMode.HTML,
        )
        return ADD_WAITING_EXPORT

    except Exception as e:
        logger.error(f"Ошибка анализа экспорта: {e}")
        await msg.edit_text(
            "❌ Не удалось проанализировать файл. Проверь логи.\n"
            "/cancel — отменить"
        )
        return ADD_WAITING_EXPORT


async def handle_export_handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получает @handle после экспорт-анализа, показывает итоговую карточку."""
    text = update.message.text.strip()

    # Нормализуем handle
    if "t.me/" in text:
        handle = "@" + text.split("t.me/")[-1].strip("/").split("?")[0]
    elif text.startswith("@"):
        handle = text
    else:
        handle = f"@{text}"
    handle = handle.split()[0]

    # Проверяем дублирование
    existing = load_all_channels()
    if any(ch["channel_id"] == handle for ch in existing):
        await update.message.reply_text(
            f"❌ Канал {handle} уже добавлен.\nПришли другой handle или /cancel"
        )
        return ADD_EXPORT_HANDLE

    analysis = context.user_data.get("export_analysis", {})
    context.user_data["new_channel"]["channel_id"] = handle

    # Показываем итоговую карточку
    ch_type_label = (
        "🛍 Маркетплейс" if analysis.get("channel_type") == "marketplace"
        else "📝 Контент"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Создать канал", callback_data="exportconfirm:yes"),
        InlineKeyboardButton("❌ Отмена",        callback_data="exportconfirm:no"),
    ]])

    await update.message.reply_text(
        f"📋 <b>Карточка канала</b>\n\n"
        f"🔗 Handle: <code>{handle}</code>\n"
        f"📌 Название: {analysis.get('export_channel_name', handle)}\n"
        f"🏷 Тип: {ch_type_label}\n"
        f"📝 Тема: {analysis.get('topic', '—')}\n"
        f"🎤 Тон: {analysis.get('tone', '—')}\n"
        f"📊 Постов в день: {analysis.get('post_frequency', 3)}\n\n"
        f"Всё верно? Создаём канал?",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    return ADD_EXPORT_CONFIRM


async def handle_export_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение создания канала из экспорта."""
    from ai_client import suggest_rss_sources

    query = update.callback_query
    await query.answer()

    action = query.data.split(":")[1]

    if action == "no":
        context.user_data.clear()
        await query.edit_message_text("❌ Добавление канала отменено.")
        return ConversationHandler.END

    # action == "yes" — создаём канал
    analysis = context.user_data.get("export_analysis", {})
    ch = context.user_data["new_channel"]

    channel_id = ch["channel_id"]
    channel_type = analysis.get("channel_type", "content")

    # Для контент-каналов подтягиваем RSS если есть rss_keywords
    rss_urls = []
    if channel_type == "content":
        kw_list = analysis.get("rss_keywords", [])
        if kw_list:
            try:
                topic_for_rss = analysis.get("topic", " ".join(kw_list[:3]))
                rss_urls = await suggest_rss_sources(
                    topic_for_rss, analysis.get("export_channel_name", "")
                )
            except Exception as e:
                logger.warning(f"RSS suggest ошибка: {e}")

    # Собираем карточку
    channel_card = {
        "channel_id": channel_id,
        "name": analysis.get("export_channel_name", channel_id),
        "topic": analysis.get("topic", ""),
        "tone": analysis.get("tone", "информационный"),
        "channel_type": channel_type,
        "daily_posts_count": analysis.get("post_frequency", 4),
        "rss_sources": rss_urls,
        "evergreen_topics": analysis.get("evergreen_topics", []),
        "forbidden_topics": [],
        "audience": "широкая аудитория",
        "post_length": "100–200 слов",
        "use_emoji": True,
        "active": True,
        "post_formats": ["совет дня", "факт/статистика", "вопрос аудитории", "мини-разбор", "инфоповод"],
        "example_posts": [],
        "use_images": False,
        "image_keywords": [],
    }

    # Marketplace-специфика
    if channel_type == "marketplace":
        channel_card["post_formats"] = ["wb_product"]

    save_channel_card(channel_card)
    buffer.add_evergreen_topics(channel_id, channel_card.get("evergreen_topics", []))

    ch_type_label = "🛍 Маркетплейс" if channel_type == "marketplace" else "📝 Контент"
    await query.edit_message_text(
        f"🎉 <b>Канал добавлен!</b>\n\n"
        f"Handle: {channel_id}\n"
        f"Тип: {ch_type_label}\n"
        f"Тема: {channel_card['topic'][:80]}\n"
        f"Постов в день: {channel_card['daily_posts_count']}\n"
        f"RSS-источников: {len(rss_urls)}\n"
        f"Вечнозелёных тем: {len(channel_card['evergreen_topics'])}\n\n"
        f"<b>Следующие шаги:</b>\n"
        f"1. Добавь бота администратором в <code>{channel_id}</code>\n"
        f"2. Запусти /generate для первых постов",
        parse_mode=ParseMode.HTML,
    )
    context.user_data.clear()
    return ConversationHandler.END


# ============================================================
# /add — ручной флоу: выбор типа канала (после темы)
# ============================================================

async def handle_add_channel_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает выбор типа канала (контент / маркетплейс)."""
    query = update.callback_query
    await query.answer()

    ch_type = query.data.split(":")[1]  # "content" или "marketplace"
    context.user_data["new_channel"]["channel_type"] = ch_type

    if ch_type == "marketplace":
        # Для маркетплейса RSS/тон не нужны — сразу спрашиваем кол-во постов
        context.user_data["new_channel"]["tone"] = "продающий, дружелюбный"
        await query.edit_message_text(
            "🛍 <b>Маркетплейс-канал</b>\n\n"
            "Бот будет автоматически находить товары из разных категорий WB "
            "и публиковать карточки с ценами и ссылками.\n\n"
            "Сколько постов генерировать в день?",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("4 поста",  callback_data="postscount:4"),
                InlineKeyboardButton("10 постов", callback_data="postscount:10"),
                InlineKeyboardButton("20 постов", callback_data="postscount:20"),
            ]]),
        )
        return ADD_POSTS_COUNT

    else:
        # Контент — продолжаем обычный флоу
        await query.edit_message_text(
            "✅ Тип сохранён.\n\n"
            "Шаг 4/5: <b>Тон общения</b> с аудиторией?\n"
            "Например: <i>дружелюбный эксперт, без снобизма, с юмором</i>",
            parse_mode=ParseMode.HTML,
        )
        return ADD_TONE


async def handle_channel_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает кнопки управления каналами (удалить, изменить тему)."""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    action, channel_id = query.data.split(":", 1)

    if action == "removech":
        # Показываем подтверждение
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Да, удалить", callback_data=f"confirmremove:{channel_id}"),
                InlineKeyboardButton("◀️ Отмена", callback_data=f"cancelremove:{channel_id}"),
            ]])
        )

    elif action == "confirmremove":
        deactivate_channel(channel_id)
        await query.edit_message_text(
            f"🗑 Канал <b>{channel_id}</b> удалён.",
            parse_mode=ParseMode.HTML,
        )

    elif action == "cancelremove":
        # Восстанавливаем исходные кнопки
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✏️ Изменить тему", callback_data=f"settopic:{channel_id}"),
                InlineKeyboardButton("🗑 Удалить", callback_data=f"removech:{channel_id}"),
            ]])
        )

    elif action == "settopic":
        context.user_data["settopic_channel"] = channel_id
        await query.message.reply_text(
            f"✏️ Пришли новую тему для канала <b>{channel_id}</b>:",
            parse_mode=ParseMode.HTML,
        )


async def handle_set_topic_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получает новую тему канала от администратора."""
    if not is_admin(update.effective_user.id):
        return

    channel_id = context.user_data.get("settopic_channel")
    if not channel_id:
        return

    new_topic = update.message.text.strip()
    channels_dir = Path(__file__).parent / "channels"
    handle_clean = channel_id.lstrip("@")
    file_path = channels_dir / f"{handle_clean}.json"

    if file_path.exists():
        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)
        data["topic"] = new_topic
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        with db.connect() as conn:
            conn.execute(
                "UPDATE channels SET topic = ? WHERE tg_handle = ?",
                (new_topic, channel_id),
            )

        await update.message.reply_text(
            f"✅ Тема канала <b>{channel_id}</b> обновлена:\n{new_topic}",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text("❌ Файл карточки канала не найден.")

    context.user_data.pop("settopic_channel", None)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает состояние буферов всех каналов."""
    if not is_admin(update.effective_user.id):
        return

    channels = load_all_channels()
    if not channels:
        await update.message.reply_text("Нет активных каналов. Добавь карточки в папку channels/")
        return

    lines = ["📊 <b>Состояние буферов</b>\n"]
    need_generation = []  # каналы где нужна генерация

    for ch in channels:
        ch_id = ch["channel_id"]
        level = buffer.get_ready_count(ch_id)
        status = buffer.check_status(ch_id)

        icon = {"ok": "✅", "low": "⚠️", "emergency": "🔴", "critical": "🚨"}.get(status, "❓")

        lines.append(
            f"{icon} <b>{ch['name']}</b> ({ch_id})\n"
            f"   В очереди: {level} постов\n"
        )

        if status in ("emergency", "critical", "low"):
            need_generation.append(ch_id)

    # Кнопки генерации для каналов с малым буфером
    keyboard = None
    if need_generation:
        lines.append("——\n⚡ <i>Нажми чтобы пополнить буфер:</i>")
        buttons = [
            [InlineKeyboardButton(f"⚡ Генерировать {ch_id}", callback_data=f"gen_channel:{ch_id}")]
            for ch_id in need_generation
        ]
        keyboard = InlineKeyboardMarkup(buttons)

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def cmd_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Показывает посты в очереди на публикацию.
    Посты уже готовы (статус ready) — просмотр опциональный.
    Можно изменить текст, добавить картинку или удалить пост.

    Использование:
        /review          — первые 5 постов всех каналов
        /review @channel — посты конкретного канала
    """
    if not is_admin(update.effective_user.id):
        return

    # Проверяем аргументы — конкретный канал или все
    args = context.args
    channel_filter = None
    if args:
        channel_filter = args[0] if args[0].startswith("@") else f"@{args[0]}"

    # Берём ready посты из БД
    with db.connect() as conn:
        if channel_filter:
            rows = conn.execute(
                """SELECT * FROM posts
                   WHERE channel_id = ? AND status = 'ready'
                   ORDER BY generated_at ASC""",
                (channel_filter,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM posts
                   WHERE status = 'ready'
                   ORDER BY channel_id, generated_at ASC"""
            ).fetchall()

    posts = [dict(r) for r in rows]

    if not posts:
        tip = f"канала {channel_filter}" if channel_filter else "всех каналов"
        await update.message.reply_text(
            f"📭 Очередь {tip} пуста.\n\n"
            f"Запусти /generate чтобы создать посты."
        )
        context.user_data.pop("review_offset", None)
        context.user_data.pop("review_filter", None)
        return

    total = len(posts)
    PAGE = 5

    # Сбрасываем offset если сменился фильтр канала
    prev_filter = context.user_data.get("review_filter")
    if prev_filter != channel_filter:
        context.user_data["review_offset"] = 0
        context.user_data["review_filter"] = channel_filter

    offset = context.user_data.get("review_offset", 0)

    # Если дошли до конца — начинаем сначала
    if offset >= total:
        offset = 0
        context.user_data["review_offset"] = 0

    page_posts = posts[offset:offset + PAGE]
    shown_end = offset + len(page_posts)

    await update.message.reply_text(
        f"📋 <b>Постов в очереди: {total}</b>\n"
        f"Показываю {offset + 1}–{shown_end}. Редактируй если нужно — "
        f"или просто оставь, постер опубликует по расписанию.",
        parse_mode=ParseMode.HTML,
    )

    for i, post in enumerate(page_posts, start=offset + 1):
        msg_text = format_post_message(post, index=i, total=total)
        keyboard = review_keyboard(post["id"])

        # Если у поста есть картинка — показываем с ней
        if post.get("image_url"):
            try:
                await update.message.reply_photo(
                    photo=post["image_url"],
                    caption=msg_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
                continue
            except Exception:
                pass  # картинка битая — покажем без неё

        await update.message.reply_text(
            msg_text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

    # Обновляем offset для следующего вызова
    context.user_data["review_offset"] = shown_end if shown_end < total else 0

    if total > PAGE:
        remaining = total - shown_end
        if remaining > 0:
            await update.message.reply_text(
                f"👆 Показано {offset + 1}–{shown_end} из {total}.\n"
                f"Вызови /review ещё раз чтобы увидеть следующие {min(PAGE, remaining)}."
            )
        else:
            await update.message.reply_text(
                f"✅ Показаны все {total} постов. /review снова — с начала."
            )


async def handle_gen_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопки '⚡ Генерировать @channel' из /status."""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    channel_id = query.data.split(":", 1)[1]
    await query.edit_message_reply_markup(reply_markup=None)  # убираем кнопки
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=(
            f"⏳ <b>Генерация для {channel_id} запущена в фоне</b>\n"
            f"Пришлю результат когда готово."
        ),
        parse_mode=ParseMode.HTML,
    )
    asyncio.create_task(
        _run_generation_background(context.bot, query.message.chat_id, force=True, channel_id=channel_id)
    )


async def cmd_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запускает генерацию контента в фоне — бот не зависает."""
    if not is_admin(update.effective_user.id):
        return

    await update.message.reply_text(
        "⏳ <b>Генерация запущена в фоне</b>\n\n"
        "Это займёт 1–2 минуты (зависит от числа каналов).\n"
        "Пришлю сообщение когда готово — можешь пока делать другие дела.",
        parse_mode=ParseMode.HTML,
    )

    # Запускаем в фоне — не блокируем бота
    # force=True: генерируем поверх существующего буфера (ручной запуск)
    asyncio.create_task(
        _run_generation_background(context.bot, update.effective_chat.id, force=True)
    )


async def _regen_one_post(bot, chat_id: int, channel_id: str):
    """
    Генерирует ровно 1 новый пост для конкретного канала.
    Используется кнопкой 🔄 Перегенерировать в /review.
    """
    try:
        channels = load_all_channels()
        channel = next((c for c in channels if c["channel_id"] == channel_id), None)
        if not channel:
            await bot.send_message(chat_id=chat_id, text=f"❌ Канал {channel_id} не найден.")
            return

        result = await generator.run_for_channel(channel, target_count=1, force=True)
        generated = result.get("generated", 0)

        if generated > 0:
            await bot.send_message(
                chat_id=chat_id,
                text=f"✅ Новый пост для {channel_id} добавлен в очередь.",
            )
        else:
            reason = result.get("reason", "нет тем")
            await bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ Не удалось сгенерировать пост для {channel_id}: {reason}",
            )
    except Exception as e:
        logger.error(f"Ошибка перегенерации поста [{channel_id}]: {e}")
        await bot.send_message(chat_id=chat_id, text=f"❌ Ошибка: {e}")


async def _run_generation_background(bot, chat_id: int, force: bool = False, channel_id: str = None):
    """
    Фоновая задача генерации — отправляет результат когда готово.
    channel_id: если указан — генерируем только для этого канала.
    """
    try:
        if channel_id:
            # Генерация для одного конкретного канала
            channels = load_all_channels()
            channel = next((c for c in channels if c["channel_id"] == channel_id), None)
            if not channel:
                await bot.send_message(chat_id=chat_id, text=f"❌ Канал {channel_id} не найден.")
                return
            result = await generator.run_for_channel(channel, force=force)
            generated = result.get("generated", 0)
            sources = ", ".join(result.get("sources_used", [])) or "нет тем"
            text = (
                f"✅ <b>Генерация для {channel_id} завершена!</b>\n\n"
                f"Постов создано: {generated}\n"
                f"Источники: {sources}\n\n"
                f"Посты добавлены в очередь и будут публиковаться по расписанию."
            )
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
            return

        # Генерация для всех каналов
        result = await generator.run_morning_batch(force=force)

        # Формируем детальный отчёт по каналам
        channels_info = ""
        for ch in result.get("channels", []):
            status_icon = "✅" if ch["generated"] > 0 else "⚠️"
            sources = ", ".join(ch.get("sources_used", [])) or "нет тем"
            channels_info += (
                f"\n{status_icon} {ch['channel_id']}: "
                f"+{ch['generated']} постов ({sources})"
            )

        text = (
            f"✅ <b>Генерация завершена!</b>\n\n"
            f"Каналов: {result['channels_processed']}\n"
            f"Постов создано: {result['total_generated']}\n"
            f"Пропущено: {result['total_skipped']}\n"
            f"Время: {result['elapsed_seconds']:.0f}с"
            f"{channels_info}\n\n"
            f"Посты добавлены в очередь и будут публиковаться по расписанию."
        )
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)

    except Exception as e:
        logger.error(f"Ошибка фоновой генерации: {e}")
        await bot.send_message(
            chat_id=chat_id,
            text=f"❌ <b>Ошибка генерации:</b> {e}",
            parse_mode=ParseMode.HTML,
        )


async def cmd_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Генерирует превью поста и показывает кнопки:
    «В очередь» или «Опубликовать сейчас».

    Использование:
      /preview          — первый канал из списка
      /preview @channel — конкретный канал
    """
    if not is_admin(update.effective_user.id):
        return

    channels = load_all_channels()
    if not channels:
        await update.message.reply_text("Нет активных каналов.")
        return

    # Выбираем канал: из аргумента или первый по списку
    args = context.args
    channel = channels[0]
    if args:
        handle = args[0] if args[0].startswith("@") else f"@{args[0]}"
        matched = [c for c in channels if c["channel_id"] == handle]
        if not matched:
            await update.message.reply_text(f"Канал {handle} не найден.")
            return
        channel = matched[0]

    await update.message.reply_text(
        f"⏳ Генерирую превью для <b>{channel.get('name', channel['channel_id'])}</b>...",
        parse_mode=ParseMode.HTML,
    )

    asyncio.create_task(
        _run_preview_background(context.bot, update.effective_chat.id, channel)
    )


async def _run_preview_background(bot, chat_id: int, channel: dict):
    """
    Фоновая генерация превью.
    После генерации показывает пост с кнопками действий.
    """
    import uuid, random
    try:
        from ai_client import generate_post

        # Берём случайную тему из вечнозелёных или используем общую
        evergreen = channel.get("evergreen_topics", [])
        topic = random.choice(evergreen) if evergreen else channel.get("topic", "полезный совет")

        post = await generate_post(channel, topic)

        # Сохраняем пост во временное хранилище (module-level dict, не в БД)
        preview_id = str(uuid.uuid4())
        _preview_store[preview_id] = {
            "channel_id": channel["channel_id"],
            "content":    post["content"],
            "format":     post.get("format", ""),
            "topic":      topic,
        }

        # Обрезаем текст для превью (HTML-теги могут сломать разметку)
        content_safe = post["content"].replace("<", "&lt;").replace(">", "&gt;")
        preview_text = (
            f"👁 <b>Превью</b> · {channel['channel_id']}\n"
            f"🎨 Формат: {post.get('format', '?')} · 💡 {topic}\n"
            f"{'─' * 32}\n\n"
            f"{content_safe}"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📥 В очередь",          callback_data=f"preview_queue:{preview_id}"),
                InlineKeyboardButton("📤 Опубликовать сейчас", callback_data=f"preview_now:{preview_id}"),
            ],
            [
                InlineKeyboardButton("🔄 Перегенерировать",   callback_data=f"preview_regen:{channel['channel_id']}"),
                InlineKeyboardButton("🗑 Выбросить",           callback_data=f"preview_discard:{preview_id}"),
            ],
        ])

        await bot.send_message(
            chat_id=chat_id,
            text=preview_text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error(f"Ошибка генерации превью: {e}")
        await bot.send_message(chat_id=chat_id, text=f"❌ Ошибка превью: {e}")


# Временное хранилище превью (живёт пока работает бот, не в БД)
_preview_store: dict = {}


async def handle_preview_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает кнопки после превью поста."""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    data = query.data  # "preview_queue:uuid" / "preview_now:uuid" / etc.
    action, payload = data.split(":", 1)

    # --- Перегенерировать ---
    if action == "preview_regen":
        channel_id = payload
        channels = load_all_channels()
        channel = next((c for c in channels if c["channel_id"] == channel_id), None)
        if not channel:
            await query.edit_message_text("❌ Канал не найден.")
            return
        await query.edit_message_text(
            f"🔄 Перегенерирую превью для {channel_id}...",
            parse_mode=ParseMode.HTML,
        )
        asyncio.create_task(
            _run_preview_background(context.bot, query.message.chat_id, channel)
        )
        return

    # --- Выбросить ---
    if action == "preview_discard":
        _preview_store.pop(payload, None)
        await query.edit_message_text("🗑 Превью удалено.")
        return

    # --- Достаём пост из временного хранилища ---
    post_data = _preview_store.get(payload)
    if not post_data:
        await query.edit_message_text("❌ Превью устарело, перегенерируй заново.")
        return

    # --- В очередь ---
    if action == "preview_queue":
        post_id = buffer.add({
            "channel_id": post_data["channel_id"],
            "content":    post_data["content"],
            "topic":      post_data["topic"],
            "format":     post_data["format"],
        })
        _preview_store.pop(payload, None)
        level = buffer.get_level(post_data["channel_id"])
        await query.edit_message_text(
            f"✅ Пост добавлен в очередь!\n"
            f"Канал: {post_data['channel_id']}\n"
            f"Постов в очереди: {level}"
        )

    # --- Опубликовать сейчас ---
    elif action == "preview_now":
        # Сначала сохраняем в буфер, потом сразу публикуем
        post_id = buffer.add({
            "channel_id": post_data["channel_id"],
            "content":    post_data["content"],
            "topic":      post_data["topic"],
            "format":     post_data["format"],
        })
        _preview_store.pop(payload, None)

        await query.edit_message_text(
            f"📤 Публикую в {post_data['channel_id']}..."
        )

        result = await poster.post_now(post_data["channel_id"])
        if result["success"]:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"✅ Опубликовано в {post_data['channel_id']}!",
            )
        else:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"❌ Ошибка публикации: {result['error']}",
            )


async def cmd_post_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Немедленно публикует следующий пост из буфера.
    Использование: /post_now или /post_now @channel
    """
    if not is_admin(update.effective_user.id):
        return

    channels = load_all_channels()
    if not channels:
        await update.message.reply_text("Нет активных каналов.")
        return

    # Определяем канал: из аргумента или первый по списку
    args = context.args
    if args:
        channel_id = args[0] if args[0].startswith("@") else f"@{args[0]}"
        if not any(c["channel_id"] == channel_id for c in channels):
            await update.message.reply_text(f"❌ Канал {channel_id} не найден.")
            return
    else:
        channel_id = channels[0]["channel_id"]

    await update.message.reply_text(f"⏳ Публикую в {channel_id}...")

    result = await poster.post_now(channel_id)

    if result["success"]:
        post = result["post"]
        await update.message.reply_text(
            f"✅ <b>Опубликовано в {channel_id}!</b>\n\n"
            f"Формат: {post.get('format', '?')}\n"
            f"Тема: {post.get('topic', '')[:80]}",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            f"❌ Не удалось опубликовать: {result['error']}\n\n"
            f"Запусти /generate чтобы создать новые посты."
        )


# ============================================================
# Удаление постов (/delete_posts)
# ============================================================

async def cmd_delete_posts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Удаляет все готовые посты из буфера с подтверждением.

    Использование:
      /delete_posts              — удалить все posты со всех каналов
      /delete_posts @channel     — удалить все посты конкретного канала
    """
    if not is_admin(update.effective_user.id):
        return

    args = context.args or []

    # Определяем: все каналы или конкретный
    if args:
        handle = args[0] if args[0].startswith("@") else "@" + args[0]
        channels = load_all_channels()
        if not any(c["channel_id"] == handle for c in channels):
            await update.message.reply_text(
                f"❌ Канал <code>{handle}</code> не найден. Список: /list",
                parse_mode=ParseMode.HTML,
            )
            return
        filter_channel = handle
    else:
        filter_channel = None  # все каналы

    # Считаем сколько постов будет удалено
    with db.connect() as conn:
        if filter_channel:
            count = conn.execute(
                "SELECT COUNT(*) FROM posts WHERE channel_id = ? AND status = 'ready'",
                (filter_channel,),
            ).fetchone()[0]
        else:
            count = conn.execute(
                "SELECT COUNT(*) FROM posts WHERE status = 'ready'"
            ).fetchone()[0]

    if count == 0:
        target = f"канала <b>{filter_channel}</b>" if filter_channel else "всех каналов"
        await update.message.reply_text(
            f"📭 В буфере {target} нет готовых постов — нечего удалять.",
            parse_mode=ParseMode.HTML,
        )
        return

    # Кодируем фильтр в callback_data (пустая строка = все каналы)
    encoded = filter_channel or "ALL"
    target_label = f"<b>{filter_channel}</b>" if filter_channel else "<b>всех каналов</b>"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Да, удалить", callback_data=f"delete_all_confirm:{encoded}"),
            InlineKeyboardButton("❌ Отмена",      callback_data="delete_all_cancel"),
        ]
    ])
    await update.message.reply_text(
        f"⚠️ <b>Удалить посты?</b>\n\n"
        f"Канал: {target_label}\n"
        f"Постов к удалению: <b>{count}</b>\n\n"
        f"Это действие <b>необратимо</b> — посты из очереди исчезнут.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def handle_delete_posts_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопок подтверждения/отмены удаления постов."""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    data = query.data  # "delete_all_confirm:@channel" или "delete_all_cancel"

    if data == "delete_all_cancel":
        await query.edit_message_text("❌ Удаление отменено.")
        return

    # Подтверждение
    encoded = data.split(":", 1)[1]
    filter_channel = None if encoded == "ALL" else encoded

    with db.connect() as conn:
        if filter_channel:
            result = conn.execute(
                "DELETE FROM posts WHERE channel_id = ? AND status = 'ready'",
                (filter_channel,),
            )
        else:
            result = conn.execute(
                "DELETE FROM posts WHERE status = 'ready'"
            )
        deleted = result.rowcount

    target_label = f"канала {filter_channel}" if filter_channel else "всех каналов"
    logger.info(f"Удалено {deleted} постов из буфера ({target_label})")
    await query.edit_message_text(
        f"🗑 <b>Удалено {deleted} постов</b> из буфера {target_label}.\n\n"
        f"Запусти /generate чтобы заполнить буфер заново.",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# Обработчики кнопок (одобрение постов)
# ============================================================

async def handle_post_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает кнопки редактирования постов в очереди.
    Пост уже в статусе ready — кнопка Одобрить не нужна.
    """
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    action, post_id = query.data.split(":", 1)

    if action == "delete":
        buffer.mark_skipped(post_id)
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑 Удалён из очереди", callback_data="done")
            ]])
        )
        logger.info(f"Пост удалён из очереди: {post_id[:8]}")

    elif action == "regen":
        # Удаляем старый пост и запускаем генерацию нового
        with db.connect() as conn:
            row = conn.execute(
                "SELECT channel_id FROM posts WHERE id = ?", (post_id,)
            ).fetchone()
        if row:
            buffer.mark_skipped(post_id)
            channel_id = row["channel_id"]
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Отправлен на перегенерацию", callback_data="done")
                ]])
            )
            asyncio.create_task(
                _regen_one_post(context.bot, cfg.ADMIN_CHAT_ID, channel_id)
            )
            logger.info(f"Пост отправлен на перегенерацию: {post_id[:8]}")

    elif action == "image":
        context.user_data["awaiting_image_for"] = post_id
        # Сбрасываем состояние редактирования текста чтобы не было конфликта
        context.user_data.pop("editing_post_id", None)
        await query.message.reply_text(
            "🖼 <b>Добавить картинку к посту</b>\n\n"
            "Два способа:\n"
            "• <b>Отправь фото</b> прямо сюда (из галереи или скачай с гугла)\n"
            "• <b>Пришли URL</b> — прямую ссылку на .jpg/.png/.webp\n\n"
            "/cancel — отменить",
            parse_mode=ParseMode.HTML,
        )

    elif action == "edit":
        # Показываем текущий текст поста перед редактированием
        with db.connect() as conn:
            row = conn.execute(
                "SELECT content FROM posts WHERE id = ?", (post_id,)
            ).fetchone()
        current_text = row["content"] if row else "(не найден)"

        context.user_data["editing_post_id"] = post_id
        context.user_data.pop("awaiting_image_for", None)
        await query.message.reply_text(
            f"✏️ <b>Текущий текст поста:</b>\n\n"
            f"{current_text}\n\n"
            f"——\n"
            f"Пришли новый текст целиком.\n"
            f"/cancel — отменить",
            parse_mode=ParseMode.HTML,
        )
        return WAITING_EDITED_TEXT

    elif action == "postnow":
        # Публикуем этот конкретный пост немедленно
        with db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM posts WHERE id = ? AND status = 'ready'",
                (post_id,),
            ).fetchone()
        if not row:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("❌ Пост не найден или уже опубликован.")
            return

        post_data = dict(row)
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⏳ Публикую...", callback_data="done")
        ]]))

        pub = await poster._publish(post_data)
        if pub["success"]:
            buffer.mark_published(post_id)
            had_image = bool(post_data.get("image_url"))
            if had_image and not pub["used_image"]:
                label = "✅ Опубликовано (без картинки — URL недоступен)"
            else:
                label = "✅ Опубликовано!"
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(label, callback_data="done")
            ]]))
            logger.info(f"Пост опубликован вручную из /review: {post_id[:8]}")
        else:
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Ошибка публикации", callback_data="done")
            ]]))

    elif action == "done":
        pass  # уже обработано, ничего не делаем


async def handle_edited_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получает отредактированный текст поста от администратора."""
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    post_id = context.user_data.get("editing_post_id")
    if not post_id:
        return ConversationHandler.END

    new_text = update.message.text
    # Обновляем текст — статус остаётся ready (пост уже в очереди)
    with db.connect() as conn:
        conn.execute(
            "UPDATE posts SET content = ? WHERE id = ?",
            (new_text, post_id),
        )
    logger.info(f"Текст поста обновлён админом: {post_id[:8]}")

    preview = new_text[:120] + ("..." if len(new_text) > 120 else "")
    await update.message.reply_text(
        f"✅ Текст обновлён! Пост остаётся в очереди.\n\n"
        f"<i>{preview}</i>",
        parse_mode=ParseMode.HTML,
    )

    context.user_data.pop("editing_post_id", None)
    return ConversationHandler.END


async def handle_image_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Получает картинку от администратора — фото из чата или URL.
    Вызывается когда в user_data установлен 'awaiting_image_for'.
    """
    if not is_admin(update.effective_user.id):
        return

    post_id = context.user_data.get("awaiting_image_for")
    if not post_id:
        return

    image_ref = None  # будет file_id (фото) или URL (текст)

    # --- Вариант 1: пользователь отправил фото ---
    if update.message.photo:
        # Берём наибольшее разрешение (последний элемент списка)
        photo = update.message.photo[-1]
        image_ref = photo.file_id
        logger.info(f"Получено фото для поста {post_id[:8]}: file_id={image_ref[:16]}...")

    # --- Вариант 2: пользователь прислал текст (URL) ---
    elif update.message.text:
        text = update.message.text.strip()

        # Предупреждение если это ссылка Google Images (не прямой URL)
        if "google.com/imgres" in text or "google.com/search" in text:
            await update.message.reply_text(
                "⚠️ Это ссылка на <b>страницу</b> Google Images, а не на саму картинку.\n"
                "Telegram не сможет её загрузить.\n\n"
                "<b>Лучший способ</b> — просто отправь картинку прямо в этот чат!\n\n"
                "<b>Или прямой URL:</b> правая кнопка на картинке → "
                "<i>«Копировать адрес изображения»</i> (.jpg/.png/.webp)\n\n"
                "/cancel — отменить",
                parse_mode=ParseMode.HTML,
            )
            return  # ждём нормальный ввод

        if not text.startswith("http"):
            await update.message.reply_text(
                "❌ Не понял. Отправь фото или прямую ссылку на картинку (http...).\n"
                "/cancel — отменить"
            )
            return

        image_ref = text

    else:
        # Что-то другое (стикер, документ и т.д.) — игнорируем
        return

    # Сохраняем в БД
    with db.connect() as conn:
        conn.execute(
            "UPDATE posts SET image_url = ? WHERE id = ?",
            (image_ref, post_id),
        )
    logger.info(f"Картинка привязана к посту {post_id[:8]}")
    await update.message.reply_text(
        "✅ Картинка добавлена! Пост опубликуется с ней по расписанию."
    )
    context.user_data.pop("awaiting_image_for", None)


async def handle_photo_for_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Отдельный хендлер для фото-сообщений — делегирует в handle_image_url.
    Нужен потому что MessageHandler(filters.PHOTO) регистрируется отдельно.
    """
    await handle_image_url(update, context)


# ============================================================
# Детектор рекламы Яндекс РСЯ
# ============================================================

# Признаки рекламного поста Яндекс РСЯ в тексте/сущностях
RSY_MARKERS = [
    "ya.cc",
    "yabs.yandex.ru",
    "yandex.ru/adv",
    "erid:",           # маркировка рекламы по закону
]

def is_rsy_ad(message) -> bool:
    """
    Определяет, является ли сообщение рекламой Яндекс РСЯ.

    Проверяет:
    - Текст содержит характерные ссылки (ya.cc, yabs.yandex.ru и т.д.)
    - Сущности (entities) содержат URL с этими доменами
    - Текст содержит маркировку "erid:" (обязательна по закону РФ)
    """
    text = (message.text or message.caption or "").lower()

    # Проверяем текст напрямую
    for marker in RSY_MARKERS:
        if marker in text:
            return True

    # Проверяем entities (кликабельные ссылки могут быть скрыты)
    entities = message.entities or message.caption_entities or []
    for entity in entities:
        if entity.url:
            url = entity.url.lower()
            for marker in RSY_MARKERS:
                if marker in url:
                    return True

    return False


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обработчик новых постов в каналах.

    Когда в канале появляется новый пост — проверяем, реклама ли это РСЯ.
    Если да — планируем публикацию нашего поста через 5–15 минут.
    """
    message = update.channel_post
    if not message:
        return

    channel_id = f"@{message.chat.username}" if message.chat.username else str(message.chat.id)

    if not is_rsy_ad(message):
        return  # обычный пост — ничего не делаем

    logger.info(f"📢 Обнаружена реклама РСЯ в {channel_id} | message_id: {message.message_id}")

    # Проверяем что у нас есть посты для этого канала
    ready_count = buffer.get_ready_count(channel_id)
    if ready_count == 0:
        logger.warning(f"Реклама в {channel_id}, но буфер пуст — запускаю экстренную генерацию!")
        await context.bot.send_message(
            chat_id=cfg.ADMIN_CHAT_ID,
            text=(
                f"📢 <b>Реклама РСЯ в {channel_id}</b>\n"
                f"⚠️ Буфер пуст — запускаю генерацию и публикую автоматически..."
            ),
            parse_mode=ParseMode.HTML,
        )
        # Генерируем пост и сразу публикуем — не ждём пользователя
        asyncio.create_task(
            _generate_and_post_after_ad(context.bot, channel_id)
        )
        return

    # Случайная задержка 5–15 минут — имитируем живого редактора
    import random
    delay_seconds = random.randint(cfg.POST_DELAY_MIN, cfg.POST_DELAY_MAX)
    delay_minutes = delay_seconds // 60

    logger.info(f"Публикую ответный пост в {channel_id} через {delay_minutes} мин.")

    # Планируем отложенную публикацию
    asyncio.create_task(
        _post_after_ad(context.bot, channel_id, delay_seconds)
    )


async def _generate_and_post_after_ad(bot, channel_id: str):
    """
    Когда реклама РСЯ пришла а буфер пуст:
    1. Генерируем 1 пост через Claude
    2. Сразу публикуем его
    3. Отправляем отчёт администратору
    """
    try:
        # Генерируем 1 пост для канала
        from content_generator import generator
        channels = load_all_channels()
        channel = next((c for c in channels if c["channel_id"] == channel_id), None)
        if not channel:
            await bot.send_message(
                chat_id=cfg.ADMIN_CHAT_ID,
                text=f"❌ Канал {channel_id} не найден в карточках — не могу сгенерировать пост.",
                parse_mode=ParseMode.HTML,
            )
            return

        result = await generator.run_for_channel(channel, target_count=1, force=True)
        generated = result.get("generated", 0)

        if generated == 0:
            reason = result.get("reason", "нет тем")
            logger.error(f"Экстренная генерация для {channel_id} не дала постов: {reason}")
            await bot.send_message(
                chat_id=cfg.ADMIN_CHAT_ID,
                text=(
                    f"❌ <b>Не смог перекрыть рекламу в {channel_id}</b>\n"
                    f"Генерация не дала постов: {reason}\n"
                    f"Запусти /generate {channel_id} вручную."
                ),
                parse_mode=ParseMode.HTML,
            )
            return

        # Пост сгенерирован — публикуем сразу
        pub_result = await poster.post_now(channel_id)
        if pub_result["success"]:
            post = pub_result["post"]
            logger.success(f"✅ Экстренный пост опубликован в {channel_id} после рекламы РСЯ")
            await bot.send_message(
                chat_id=cfg.ADMIN_CHAT_ID,
                text=(
                    f"✅ <b>Реклама РСЯ перекрыта!</b>\n"
                    f"Канал: {channel_id}\n"
                    f"Формат: {post.get('format', '?')}\n"
                    f"Буфер был пуст — сгенерировал и опубликовал автоматически.\n\n"
                    f"💡 Рекомендую запустить /generate {channel_id} чтобы пополнить буфер."
                ),
                parse_mode=ParseMode.HTML,
            )
        else:
            await bot.send_message(
                chat_id=cfg.ADMIN_CHAT_ID,
                text=(
                    f"⚠️ <b>Пост сгенерирован, но не опубликован в {channel_id}</b>\n"
                    f"Причина: {pub_result['error']}\n"
                    f"Пост сохранён в буфере — попробуй /post_now {channel_id}"
                ),
                parse_mode=ParseMode.HTML,
            )

    except Exception as e:
        logger.error(f"Ошибка экстренной генерации+публикации [{channel_id}]: {e}")
        await bot.send_message(
            chat_id=cfg.ADMIN_CHAT_ID,
            text=f"❌ Ошибка при экстренной генерации для {channel_id}: {e}",
            parse_mode=ParseMode.HTML,
        )


async def _post_after_ad(bot, channel_id: str, delay_seconds: int):
    """Ждёт нужное время и публикует пост после рекламы."""
    import asyncio as _asyncio
    await _asyncio.sleep(delay_seconds)

    result = await poster.post_now(channel_id)

    if result["success"]:
        post = result["post"]
        logger.success(
            f"✅ Пост опубликован после рекламы РСЯ в {channel_id} | "
            f"формат: {post.get('format', '?')}"
        )
    else:
        logger.error(f"❌ Не удалось опубликовать после рекламы в {channel_id}: {result['error']}")
        await bot.send_message(
            chat_id=cfg.ADMIN_CHAT_ID,
            text=(
                f"❌ <b>Не смог перекрыть рекламу в {channel_id}</b>\n"
                f"Причина: {result['error']}"
            ),
            parse_mode=ParseMode.HTML,
        )


# ============================================================
# Публикация поста в канал
# ============================================================

async def publish_post(bot, post: dict) -> bool:
    """
    Публикует пост в Telegram-канал.
    Возвращает True если успешно, False если ошибка.
    """
    channel_id = post["channel_id"]
    content = post["content"]
    image_url = post.get("image_url")

    try:
        if image_url:
            # Пост с картинкой
            await bot.send_photo(
                chat_id=channel_id,
                photo=image_url,
                caption=content,
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            # Текстовый пост
            await bot.send_message(
                chat_id=channel_id,
                text=content,
                parse_mode=ParseMode.MARKDOWN,
            )

        logger.success(f"Пост опубликован в {channel_id}")
        return True

    except Exception as e:
        logger.error(f"Ошибка публикации в {channel_id}: {e}")
        return False


# ============================================================
# Управление расписанием (/schedule)
# ============================================================

async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /schedule — просмотр и изменение расписания публикаций.

    Использование:
      /schedule                        — расписание всех каналов
      /schedule @channel               — расписание конкретного канала
      /schedule @channel 09 12 16 20   — установить часы (МСК)
      /schedule @channel off           — только РСЯ (без таймера)
      /schedule @channel on            — вернуть расписание
    """
    if not is_admin(update.effective_user.id):
        return

    args = context.args or []
    channels = load_all_channels()

    def utc_to_msk(hours: list) -> list:
        return sorted([(h + 3) % 24 for h in hours])

    def msk_to_utc(hours: list) -> list:
        return sorted([(h - 3) % 24 for h in hours])

    DEFAULT_UTC = [6, 9, 13, 17]  # 09, 12, 16, 20 МСК

    # --- Без аргументов: расписание всех каналов ---
    if not args:
        if not channels:
            await update.message.reply_text("Нет добавленных каналов.")
            return
        lines = ["📅 <b>Расписание публикаций</b>\n"]
        for ch in channels:
            cid = ch["channel_id"]
            if ch.get("schedule_disabled", False):
                lines.append(f"• {cid} — <b>⏸ только РСЯ</b>")
            else:
                msk = utc_to_msk(ch.get("post_times_utc", DEFAULT_UTC))
                lines.append(f"• {cid} — {', '.join(f'{h:02d}:00' for h in msk)} МСК")
        lines.append("\n<i>/schedule @channel 09 12 16 20 — изменить</i>")
        lines.append("<i>/schedule @channel off — только РСЯ</i>")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        return

    # --- Первый аргумент — канал ---
    handle = args[0] if args[0].startswith("@") else "@" + args[0]
    channel = next((ch for ch in channels if ch["channel_id"] == handle), None)
    if channel is None:
        await update.message.reply_text(
            f"❌ Канал <code>{handle}</code> не найден. Список: /list",
            parse_mode=ParseMode.HTML,
        )
        return

    # --- Только канал без параметров: показать текущее ---
    if len(args) == 1:
        if channel.get("schedule_disabled", False):
            text = (
                f"📅 <b>{handle}</b>\n"
                f"Режим: ⏸ только РСЯ\n\n"
                f"<i>Включить расписание: /schedule {handle} on</i>"
            )
        else:
            msk = utc_to_msk(channel.get("post_times_utc", DEFAULT_UTC))
            times = ", ".join(f"{h:02d}:00" for h in msk)
            text = (
                f"📅 <b>{handle}</b>\n"
                f"Расписание: {times} МСК\n\n"
                f"<i>Изменить: /schedule {handle} 09 12 16 20</i>\n"
                f"<i>Отключить: /schedule {handle} off</i>"
            )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        return

    action = args[1].lower()

    # --- Включить расписание ---
    if action == "on":
        channel.pop("schedule_disabled", None)
        save_channel_card(channel)
        msk = utc_to_msk(channel.get("post_times_utc", DEFAULT_UTC))
        times = ", ".join(f"{h:02d}:00" for h in msk)
        await update.message.reply_text(
            f"✅ Расписание включено для <b>{handle}</b>\n"
            f"Публикации в: {times} МСК",
            parse_mode=ParseMode.HTML,
        )
        return

    # --- Отключить расписание (только РСЯ) ---
    if action == "off":
        channel["schedule_disabled"] = True
        save_channel_card(channel)
        await update.message.reply_text(
            f"⏸ Расписание отключено для <b>{handle}</b>\n"
            f"Бот публикует только при обнаружении рекламы РСЯ.",
            parse_mode=ParseMode.HTML,
        )
        return

    # --- Установить часы публикаций (МСК) ---
    try:
        hours_msk = []
        for arg in args[1:]:
            h = int(arg)
            if not 0 <= h <= 23:
                raise ValueError(f"Час вне диапазона: {h}")
            hours_msk.append(h)
        hours_msk = sorted(set(hours_msk))
        channel["post_times_utc"] = msk_to_utc(hours_msk)
        channel.pop("schedule_disabled", None)
        save_channel_card(channel)
        times = ", ".join(f"{h:02d}:00" for h in hours_msk)
        await update.message.reply_text(
            f"✅ Расписание обновлено для <b>{handle}</b>\n"
            f"Публикации в: <b>{times} МСК</b>",
            parse_mode=ParseMode.HTML,
        )
    except ValueError as e:
        await update.message.reply_text(
            f"❌ Ошибка: {e}\n\n"
            f"Примеры:\n"
            f"  /schedule {handle} 09 12 16 20\n"
            f"  /schedule {handle} off\n"
            f"  /schedule {handle} on",
        )


# ============================================================
# Алерты (отправка сообщений администратору)
# ============================================================

async def send_alert(bot, message: str):
    """Отправляет алерт администратору в Telegram."""
    try:
        await bot.send_message(
            chat_id=cfg.ADMIN_CHAT_ID,
            text=message,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"Не удалось отправить алерт: {e}")


# ============================================================
# Запуск бота
# ============================================================

def main():
    """Запускает Telegram бота вместе с планировщиком постов."""

    # Инициализируем БД
    db.init()

    logger.info("Запускаю Content Factory Bot...")

    import os
    proxy_url = os.getenv("PROXY_URL", "")
    builder = Application.builder().token(cfg.BOT_TOKEN)
    if proxy_url:
        builder = builder.proxy_url(proxy_url).get_updates_proxy_url(proxy_url)
        logger.info(f"Используется прокси: {proxy_url}")
    app = builder.build()

    # --- Диалог добавления канала (/add) ---
    add_channel_conv = ConversationHandler(
        entry_points=[CommandHandler("add", cmd_add_start)],
        states={
            # Шаг 0: выбор метода (кнопки)
            ADD_CHOOSE_METHOD:  [CallbackQueryHandler(handle_add_method_choice, pattern="^addmethod:")],

            # Экспорт-флоу
            ADD_WAITING_EXPORT: [MessageHandler(filters.Document.ALL, handle_export_upload)],
            ADD_EXPORT_HANDLE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_export_handle)],
            ADD_EXPORT_CONFIRM: [CallbackQueryHandler(handle_export_confirm, pattern="^exportconfirm:")],

            # Ручной флоу
            ADD_HANDLE:         [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_add_handle)],
            ADD_NAME:           [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_add_name)],
            ADD_TOPIC:          [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_add_topic)],
            ADD_CHANNEL_TYPE:   [CallbackQueryHandler(handle_add_channel_type, pattern="^channeltype:")],
            ADD_TONE:           [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_add_tone)],
            ADD_FORBIDDEN:      [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_add_forbidden)],
            ADD_RSS_CONFIRM:    [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_add_rss_confirm)],
            ADD_POSTS_COUNT:    [CallbackQueryHandler(cmd_add_posts_count, pattern="^postscount:")],
        },
        per_message=False,
        fallbacks=[CommandHandler("cancel", cmd_add_cancel)],
    )
    app.add_handler(add_channel_conv)

    # --- Диалог редактирования поста ---
    edit_conversation = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_post_actions, pattern="^edit:")],
        states={
            WAITING_EDITED_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edited_text)
            ],
        },
        per_message=False,
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )
    app.add_handler(edit_conversation)

    # --- Команды ---
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("generate", cmd_generate))
    app.add_handler(CommandHandler("review", cmd_review))
    app.add_handler(CommandHandler("preview", cmd_preview))
    app.add_handler(CommandHandler("post_now", cmd_post_now))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler("delete_posts", cmd_delete_posts))

    # --- Кнопки: удаление всех постов ---
    app.add_handler(CallbackQueryHandler(
        handle_delete_posts_confirm,
        pattern="^(delete_all_confirm:|delete_all_cancel)",
    ))

    # --- Кнопки: генерация для конкретного канала из /status ---
    app.add_handler(CallbackQueryHandler(
        handle_gen_channel,
        pattern="^gen_channel:",
    ))

    # --- Кнопки: редактирование постов ---
    app.add_handler(CallbackQueryHandler(handle_post_actions, pattern="^(delete|image|regen|done|postnow):"))

    # --- Кнопки: превью поста ---
    app.add_handler(CallbackQueryHandler(handle_preview_actions, pattern="^preview_(queue|now|regen|discard):"))

    # --- Кнопки: управление каналами ---
    app.add_handler(CallbackQueryHandler(handle_channel_actions, pattern="^(removech|confirmremove|cancelremove|settopic):"))

    # --- Текстовые сообщения от админа (URL картинки / новая тема канала) ---
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_image_url,
    ))

    # --- Фото от админа (картинка для поста) ---
    app.add_handler(MessageHandler(
        filters.PHOTO & filters.ChatType.PRIVATE,
        handle_photo_for_post,
    ))

    # --- Посты в каналах (детектор рекламы РСЯ) ---
    app.add_handler(MessageHandler(
        filters.ChatType.CHANNEL,
        handle_channel_post,
    ))

    # --------------------------------------------------------
    # Планировщик постов
    # --------------------------------------------------------

    async def on_startup(application):
        """Запускается после старта бота — инициализируем постер и планировщик."""
        poster.set_bot(application.bot)

        scheduler = AsyncIOScheduler(timezone="UTC")

        # Постер — каждый час проверяет все каналы
        scheduler.add_job(
            poster.tick,
            CronTrigger(minute=0),
            id="poster_tick",
            name="Публикация постов",
            misfire_grace_time=300,
        )

        # Утренняя генерация — каждый день в 06:00 UTC (09:00 МСК)
        scheduler.add_job(
            generator.run_morning_batch,
            CronTrigger(hour=cfg.GENERATION_HOUR, minute=cfg.GENERATION_MINUTE),
            id="morning_generation",
            name="Утренняя генерация",
            misfire_grace_time=600,
        )

        scheduler.start()
        application.bot_data["scheduler"] = scheduler

        logger.success(
            f"Планировщик запущен:\n"
            f"  • Постер: каждый час\n"
            f"  • Генерация: {cfg.GENERATION_HOUR:02d}:{cfg.GENERATION_MINUTE:02d} UTC ежедневно"
        )

    async def on_shutdown(application):
        """Останавливаем планировщик при завершении."""
        scheduler = application.bot_data.get("scheduler")
        if scheduler:
            scheduler.shutdown(wait=False)
            logger.info("Планировщик остановлен")

    app.post_init = on_startup
    app.post_shutdown = on_shutdown

    logger.success("Бот запущен! Нажми Ctrl+C для остановки.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
