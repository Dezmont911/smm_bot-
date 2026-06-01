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
import re
import warnings
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Заглушаем информационное предупреждение PTB про per_message=False
# (не влияет на работу — наши ConversationHandler-ы работают корректно)
warnings.filterwarnings("ignore", message=".*per_message=False.*")

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
from ui import (
    ui_router,
    screen_main,
    handle_settings_text_input,
    handle_img_test_input,
    MENU_KEYBOARD,
)


# ============================================================
# Состояния ConversationHandler
# ============================================================
WAITING_EDITED_TEXT = 1

# Состояния для /add (добавление канала — пошаговый диалог)
ADD_HANDLE, ADD_NAME, ADD_TOPIC, ADD_TONE, ADD_FORBIDDEN, ADD_RSS_CONFIRM, ADD_POSTS_COUNT = range(10, 17)
# Новые состояния: выбор метода + экспорт-флоу + channel_type
ADD_CHOOSE_METHOD, ADD_WAITING_EXPORT, ADD_EXPORT_HANDLE, ADD_EXPORT_CONFIRM, ADD_CHANNEL_TYPE = range(17, 22)
# Шаги настройки картинок и WB-категорий
ADD_IMAGE_SOURCE, ADD_REDDIT_SUBS, ADD_WB_CATEGORIES = range(22, 25)
# Добавление по @username через Telethon-юзербота (авто-анализ)
ADD_USERNAME, ADD_USERNAME_CONFIRM = range(25, 27)
# Массовое добавление: список @username за раз (тот же авто-анализ для каждого)
ADD_BULK = 27


# ============================================================
# Вспомогательные функции
# ============================================================

def is_admin(user_id: int) -> bool:
    """Проверяет что команду отправил администратор."""
    return user_id in cfg.ADMIN_CHAT_IDS


def safe_slug(channel_id: str) -> str:
    """
    Превращает handle канала в безопасное имя файла.
    Убирает @ и всё, что не буква/цифра/_/-/. — защита от path traversal
    (например '../../etc/passwd' → 'etcpasswd').
    Telegram-хендлы и так состоят только из [A-Za-z0-9_], так что
    нормальные имена не страдают.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", channel_id.lstrip("@"))
    if not cleaned:
        raise ValueError(f"Недопустимый handle канала: {channel_id!r}")
    return cleaned


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

def clamp_channel_fields(channel: dict) -> dict:
    """
    Обрезает и чистит текстовые поля карточки канала по лимитам FIELD_LIMITS.
    Защита от раздувания контекста и промт-инъекций через поля канала.
    Меняет словарь на месте и возвращает его.
    """
    from ai_client import sanitize_field, FIELD_LIMITS

    for key in ("name", "topic", "audience", "tone", "post_length"):
        if channel.get(key):
            limit = FIELD_LIMITS.get(key, 300)
            channel[key] = sanitize_field(channel[key], limit)

    if isinstance(channel.get("forbidden_topics"), list):
        channel["forbidden_topics"] = [
            sanitize_field(t, FIELD_LIMITS["forbidden"])
            for t in channel["forbidden_topics"][:15]
        ]
    if isinstance(channel.get("example_posts"), list):
        channel["example_posts"] = [
            sanitize_field(t, FIELD_LIMITS["example"])
            for t in channel["example_posts"][:5]
        ]
    return channel


def save_channel_card(channel: dict):
    """
    Сохраняет карточку канала в JSON файл и регистрирует в БД.
    Имя файла = handle без @ (например finance_channel.json).
    """
    clamp_channel_fields(channel)  # защита: обрезаем поля до лимитов
    channels_dir = Path(__file__).parent / "channels"
    handle_clean = safe_slug(channel["channel_id"])
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
    handle_clean = safe_slug(channel_id)
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
    """Открывает главное меню и показывает постоянную кнопку Меню."""
    if not is_admin(update.effective_user.id):
        return

    # Сначала отправляем ReplyKeyboard — она «прилипнет» внизу навсегда
    await update.message.reply_text(
        "☰",
        reply_markup=MENU_KEYBOARD,
    )
    # Затем показываем главное меню как inline
    await screen_main(update.message, context)


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
    """Начинает диалог добавления нового канала — предлагает выбор метода.
    Работает и как команда /add, и как callback от кнопки 'Добавить канал'.
    """
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    context.user_data.clear()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 По ссылке / @username (авто)", callback_data="addmethod:username")],
        [InlineKeyboardButton("📋 Списком (много сразу)",         callback_data="addmethod:bulk")],
        [InlineKeyboardButton("✏️ Описать вручную",              callback_data="addmethod:manual")],
        [InlineKeyboardButton("📂 Загрузить экспорт Telegram",   callback_data="addmethod:export")],
        [InlineKeyboardButton("◀️ В меню",                       callback_data="add_to_menu")],
    ])
    text = (
        "➕ <b>Добавление канала</b>\n\n"
        "Как хочешь настроить канал?\n\n"
        "🔍 <b>По ссылке / @username</b> — просто пришли ссылку или @username "
        "публичного канала. Бот сам прочитает его и определит тему, стиль, тон "
        "и источники. <b>Рекомендуется.</b>\n\n"
        "✏️ <b>Вручную</b> — пошаговый диалог с вопросами.\n\n"
        "📂 <b>Экспорт Telegram</b> — файл <code>result.json</code> из Telegram Desktop.\n\n"
        "/cancel — отменить"
    )

    # Поддержка и команды /add, и нажатия inline-кнопки
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
        )
    else:
        await update.message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
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
        f"Шаг 2/3: Как называется канал?\n"
        f"Например: <i>Финансы для людей</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=_add_cancel_kb(),
    )
    return ADD_NAME


async def cmd_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получает название канала."""
    context.user_data["new_channel"]["name"] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ Название сохранено.\n\n"
        f"Шаг 3/3: <b>Тема канала</b> — о чём пишем?\n"
        f"Например: <i>личные финансы, инвестиции, сбережения</i>\n\n"
        f"<i>Тон, источники тем и картинки настроятся автоматически.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=_add_cancel_kb(),
    )
    return ADD_TOPIC


async def cmd_add_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получает тему канала, затем спрашивает тип канала."""
    context.user_data["new_channel"]["topic"] = update.message.text.strip()
    await update.message.reply_text(
        "✅ Тема сохранена.\n\n"
        "Последний шаг: <b>Тип канала</b>\n\n"
        "📝 <b>Контент</b> — пишем посты: новости, советы, факты, разборы\n"
        "🛍 <b>Маркетплейс</b> — постим товары с WB/Ozon (цена, фото, ссылка)",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📝 Контент-канал",        callback_data="channeltype:content"),
                InlineKeyboardButton("🛍 Маркетплейс WB/Ozon",  callback_data="channeltype:marketplace"),
            ],
            [InlineKeyboardButton("❌ Отменить", callback_data="add_cancel_inline")],
        ]),
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
        reply_markup=_add_cancel_kb(),
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
        reply_markup=_add_cancel_kb(),
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

    # Спрашиваем источник картинок для постов
    await update.message.reply_text(
        "✅ RSS-источники сохранены.\n\n"
        "📸 <b>Картинки к постам</b>\n\n"
        "Откуда брать изображения для постов?\n\n"
        "• <b>Reddit</b> — тематические скриншоты из сабреддитов (лучше для игр, аниме, хобби)\n"
        "• <b>Pexels/Unsplash</b> — стоковые фото по ключевым словам (хорошо для бизнеса, лайфстайл)\n"
        "• <b>Без картинок</b> — только текст",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🎮 Reddit",              callback_data="imgsource:reddit"),
                InlineKeyboardButton("📸 Pexels/Unsplash",     callback_data="imgsource:stock"),
                InlineKeyboardButton("🚫 Без картинок",        callback_data="imgsource:none"),
            ],
            [InlineKeyboardButton("❌ Отменить", callback_data="add_cancel_inline")],
        ]),
    )
    return ADD_IMAGE_SOURCE


def _derive_image_keywords(topic: str, name: str = "") -> list[str]:
    """Короткие ключевые слова для поиска картинок (анкор к теме канала).

    Берёт только КОРОТКИЕ фрагменты темы (≤3 слов, ≤30 симв). Если тема —
    это описание-предложение ("Канал публикует ..."), такие длинные части
    отбрасываются и возвращается [], чтобы не засорять запрос к Pexels:
    тогда image_fetcher сам построит чистый запрос через Claude.
    """
    parts = [p.strip() for p in (topic or "").split(",") if p.strip()]
    keywords = [p for p in parts if len(p.split()) <= 3 and len(p) <= 30]
    return keywords[:3]


async def handle_add_image_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает выбор источника картинок (reddit / stock / none)."""
    query = update.callback_query
    await query.answer()

    source = query.data.split(":")[1]  # "reddit", "stock", "none"
    ch = context.user_data["new_channel"]

    if source == "none":
        ch["use_images"] = False
        await query.edit_message_text(
            "🚫 Картинки отключены — посты будут только текстовые.\n\n"
            "Сколько постов генерировать в день?",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("4 поста",    callback_data="postscount:4"),
                    InlineKeyboardButton("10 постов",  callback_data="postscount:10"),
                    InlineKeyboardButton("20 постов",  callback_data="postscount:20"),
                ],
                [InlineKeyboardButton("❌ Отменить", callback_data="add_cancel_inline")],
            ]),
        )
        return ADD_POSTS_COUNT

    elif source == "stock":
        ch["use_images"] = True
        ch["image_source"] = "stock"
        # Ключевые слова — короткий анкор из темы (без описаний-предложений)
        ch["image_keywords"] = _derive_image_keywords(ch.get("topic", ""), ch.get("name", ""))
        kw_label = ", ".join(ch["image_keywords"]) if ch["image_keywords"] else "(по теме автоматически)"
        await query.edit_message_text(
            f"📸 Pexels/Unsplash выбран.\n"
            f"Ключевые слова для поиска: <b>{kw_label}</b>\n"
            f"(можно поменять позже через карточку канала)\n\n"
            f"Сколько постов генерировать в день?",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("4 поста", callback_data="postscount:4"),
                InlineKeyboardButton("10 постов", callback_data="postscount:10"),
                InlineKeyboardButton("20 постов", callback_data="postscount:20"),
            ]]),
        )
        return ADD_POSTS_COUNT

    else:  # reddit
        ch["use_images"] = True
        ch["image_source"] = "reddit"
        await query.edit_message_text(
            "🎮 <b>Reddit-картинки</b>\n\n"
            "Напиши сабреддиты через запятую (только название без r/).\n\n"
            "<b>Примеры:</b>\n"
            "• Майнкрафт: <code>Minecraft, MCPE, feedthebeast</code>\n"
            "• КС2: <code>GlobalOffensive, csgo</code>\n"
            "• Аниме: <code>anime, Animemes, manga</code>\n"
            "• Общее: <code>gaming, pcgaming</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=_add_cancel_kb(),
        )
        return ADD_REDDIT_SUBS


async def handle_add_reddit_subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получает список сабреддитов и переходит к выбору кол-ва постов."""
    text = update.message.text.strip()
    ch = context.user_data["new_channel"]

    # Парсим: "Minecraft, MCPE, feedthebeast" → ["Minecraft", "MCPE", "feedthebeast"]
    subs = [s.strip().lstrip("r/") for s in text.replace("\n", ",").split(",") if s.strip()]

    if not subs:
        await update.message.reply_text(
            "⚠️ Не понял. Напиши названия сабреддитов через запятую.\n"
            "Например: <code>Minecraft, MCPE</code>",
            parse_mode=ParseMode.HTML,
        )
        return ADD_REDDIT_SUBS

    ch["reddit_image_subreddits"] = subs

    subs_str = ", ".join(f"r/{s}" for s in subs)
    await update.message.reply_text(
        f"✅ Сабреддиты сохранены: <b>{subs_str}</b>\n\n"
        f"Сколько постов генерировать в день?",
        parse_mode=ParseMode.HTML,
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
    await _finalize_new_channel(query, context, count)
    return ConversationHandler.END


async def _finalize_new_channel(query, context: ContextTypes.DEFAULT_TYPE, count: int):
    """Собирает карточку канала с дефолтами, авто-стилем и сохраняет."""
    ch = context.user_data["new_channel"]
    ch["daily_posts_count"] = count

    channel_type = ch.get("channel_type", "content")

    # Финальная сборка карточки
    # Примечание: use_images, image_keywords, reddit_image_subreddits, wb_categories
    # уже могут быть заполнены предыдущими шагами — не перезаписываем их
    base_defaults = {
        "audience": "широкая аудитория",
        "post_length": "100–200 слов",
        "use_emoji": True,
        "active": True,
        "example_posts": [],
        # Новый канал НЕ постит по умолчанию — расписание включается вручную
        # через /schedule @channel on (или установкой часов). Так нет «дефолтных»
        # 09/12/16/20, которые раньше включались сами.
        "schedule_disabled": True,
    }

    if channel_type == "marketplace":
        base_defaults["post_formats"] = ["wb_product"]
        base_defaults.setdefault("tone", "продающий, дружелюбный")
        # use_images для WB не нужен
        base_defaults["use_images"] = False
    else:
        base_defaults["post_formats"] = ["совет дня", "факт/статистика", "вопрос аудитории", "мини-разбор", "инфоповод"]
        # use_images и image_keywords уже выставлены на шаге ADD_IMAGE_SOURCE
        # Но добавим fallback на случай если шаг был пропущен
        if "use_images" not in ch:
            base_defaults["use_images"] = True
        if "image_keywords" not in ch:
            base_defaults["image_keywords"] = _derive_image_keywords(
                ch.get("topic", ""), ch.get("name", "")
            )

    # update() не перезапишет уже установленные ключи в ch — используем reversed merge
    for k, v in base_defaults.items():
        ch.setdefault(k, v)

    # Авто-определение архетипа (стиль) и источника тем по описанию — для контента
    if channel_type == "content" and "archetype" not in ch:
        from channel_analyzer import classify_channel
        meta = await classify_channel(ch.get("name", ""), ch.get("topic", ""))
        ch["archetype"] = meta["archetype"]
        # topic_source ставим только если уверенность приличная; иначе оставляем rss
        if meta["confidence"] >= 0.6:
            ch.setdefault("topic_source", meta["topic_source"])
        ch.setdefault("topic_source", "rss")

    save_channel_card(ch)

    # Добавляем вечнозелёные темы в БД (для контент-каналов)
    buffer.add_evergreen_topics(ch["channel_id"], ch.get("evergreen_topics", []))

    rss_count = len(ch.get("rss_sources", []))
    eg_count = len(ch.get("evergreen_topics", []))
    ch_type_label = "🛍 Маркетплейс" if channel_type == "marketplace" else "📝 Контент"

    # Строка про авто-определённый стиль и источник тем (для контент-каналов)
    meta_line = ""
    if channel_type == "content":
        from archetypes import ARCHETYPE_LABELS
        arch_label = ARCHETYPE_LABELS.get(ch.get("archetype", "default"), ch.get("archetype", "default"))
        src_label = "🌐 веб-поиск" if ch.get("topic_source") == "search" else "📡 RSS"
        meta_line = f"Стиль: {arch_label}\nИсточник тем: {src_label}\n"

    channel_id_added = ch['channel_id']
    await query.edit_message_text(
        f"🎉 <b>Канал добавлен!</b>\n\n"
        f"Handle: {channel_id_added}\n"
        f"Название: {ch['name']}\n"
        f"Тип: {ch_type_label}\n"
        f"Тема: {ch.get('topic', '—')}\n"
        f"Постов в день: {count}\n"
        + meta_line
        + (f"RSS-источников: {rss_count}\nВечнозелёных тем: {eg_count}\n" if channel_type == "content" else "") +
        f"\n⏸ Автопубликация <b>выключена</b> — включи расписание:\n"
        f"<code>/schedule {channel_id_added} 09 12 16 20</code>\n"
        f"\n⚠️ Добавь бота администратором в <code>{channel_id_added}</code>",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⚡ Сгенерировать первые посты", callback_data=f"ui:ch_generate:{channel_id_added}")],
            [InlineKeyboardButton("⚙️ Настройки канала",           callback_data=f"ui:ch_settings:{channel_id_added}")],
            [InlineKeyboardButton("◀️ В меню",                     callback_data="ui:main")],
        ]),
        parse_mode=ParseMode.HTML,
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Универсальная отмена любого диалога."""
    context.user_data.clear()
    await update.message.reply_text("❌ Отменено.")
    return ConversationHandler.END


def _add_cancel_kb() -> InlineKeyboardMarkup:
    """Кнопки отмены / выхода в меню для каждого шага диалога добавления канала."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Отменить",  callback_data="add_cancel_inline"),
        InlineKeyboardButton("◀️ В меню",    callback_data="add_to_menu"),
    ]])


async def cmd_add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отменяет добавление канала (команда /cancel)."""
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Добавление канала отменено.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ В меню", callback_data="ui:main"),
        ]]),
    )
    return ConversationHandler.END


async def cmd_add_cancel_inline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отменяет добавление канала (inline-кнопка)."""
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text(
        "❌ Добавление канала отменено.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ В меню", callback_data="ui:main"),
        ]]),
    )
    return ConversationHandler.END


async def cmd_add_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Закрывает диалог добавления канала и сразу открывает главное меню."""
    from ui import screen_main
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await screen_main(query, context)
    return ConversationHandler.END


# ============================================================
# /add — выбор метода: экспорт или вручную
# ============================================================

async def handle_add_method_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает выбор метода добавления канала."""
    query = update.callback_query
    await query.answer()

    method = query.data.split(":")[1]  # "username" / "export" / "manual"

    if method == "username":
        await query.edit_message_text(
            "🔍 <b>Добавление по ссылке / @username</b>\n\n"
            "Пришли ссылку или @username <b>публичного</b> канала.\n"
            "Например: <code>@durov</code> или <code>https://t.me/durov</code>\n\n"
            "Бот прочитает канал через юзербота и сам всё определит.\n\n"
            "/cancel — отменить",
            parse_mode=ParseMode.HTML,
        )
        return ADD_USERNAME

    if method == "bulk":
        await query.edit_message_text(
            "📋 <b>Массовое добавление</b>\n\n"
            "Пришли список публичных каналов — по одному в строке "
            "(или через запятую). До <b>20</b> за раз.\n\n"
            "Например:\n"
            "<code>@channel1\n@channel2\nhttps://t.me/channel3</code>\n\n"
            "Бот прочитает каждый юзерботом, сам определит тему/стиль/источники "
            "и создаст карточки. Автопубликация у всех будет выключена.\n\n"
            "/cancel — отменить",
            parse_mode=ParseMode.HTML,
        )
        return ADD_BULK

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
        import traceback
        logger.error(f"Ошибка анализа экспорта: {e}\n{traceback.format_exc()}")
        await msg.edit_text(
            f"❌ Не удалось проанализировать файл.\n\n"
            f"<code>{type(e).__name__}: {str(e)[:200]}</code>\n\n"
            "/cancel — отменить",
            parse_mode=ParseMode.HTML,
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
        "use_images": True,
        "image_keywords": [],
        # Новый канал не постит по умолчанию — расписание включается через /schedule
        "schedule_disabled": True,
    }

    # Marketplace-специфика
    if channel_type == "marketplace":
        channel_card["post_formats"] = ["wb_product"]
        channel_card["use_images"] = False
    else:
        # Архетип (стиль) и источник тем — из анализа постов (нормализуем)
        from channel_analyzer import normalize_meta
        arch, src = normalize_meta(analysis.get("archetype"), analysis.get("topic_source"))
        channel_card["archetype"] = arch
        channel_card["topic_source"] = src

    save_channel_card(channel_card)
    buffer.add_evergreen_topics(channel_id, channel_card.get("evergreen_topics", []))

    ch_type_label = "🛍 Маркетплейс" if channel_type == "marketplace" else "📝 Контент"
    meta_line = ""
    if channel_type == "content":
        from archetypes import ARCHETYPE_LABELS
        arch_label = ARCHETYPE_LABELS.get(channel_card.get("archetype", "default"),
                                          channel_card.get("archetype", "default"))
        src_label = "🌐 веб-поиск" if channel_card.get("topic_source") == "search" else "📡 RSS"
        meta_line = f"Стиль: {arch_label}\nИсточник тем: {src_label}\n"
    await query.edit_message_text(
        f"🎉 <b>Канал добавлен!</b>\n\n"
        f"Handle: {channel_id}\n"
        f"Тип: {ch_type_label}\n"
        f"Тема: {channel_card['topic'][:80]}\n"
        f"Постов в день: {channel_card['daily_posts_count']}\n"
        + meta_line +
        f"RSS-источников: {len(rss_urls)}\n"
        f"Вечнозелёных тем: {len(channel_card['evergreen_topics'])}\n\n"
        f"⏸ Автопубликация <b>выключена</b> — включи расписание:\n"
        f"<code>/schedule {channel_id} 09 12 16 20</code>\n\n"
        f"⚠️ Добавь бота администратором в <code>{channel_id}</code>",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⚡ Сгенерировать первые посты", callback_data=f"ui:ch_generate:{channel_id}")],
            [InlineKeyboardButton("⚙️ Настройки канала",           callback_data=f"ui:ch_settings:{channel_id}")],
            [InlineKeyboardButton("◀️ В меню",                     callback_data="ui:main")],
        ]),
        parse_mode=ParseMode.HTML,
    )
    context.user_data.clear()
    return ConversationHandler.END


# ============================================================
# /add — авто-добавление по @username через Telethon-юзербота
# ============================================================

async def _create_channel_from_analysis(analysis: dict, channel_id: str, display_name: str) -> tuple[dict, list]:
    """Собирает и сохраняет карточку канала из результата анализа.
    Общая логика для экспорт- и username-пути. Возвращает (карточка, rss_urls)."""
    from ai_client import suggest_rss_sources
    from channel_analyzer import normalize_meta

    channel_type = analysis.get("channel_type", "content")
    rss_urls = []
    if channel_type == "content":
        kw_list = analysis.get("rss_keywords", [])
        if kw_list:
            try:
                topic_for_rss = analysis.get("topic", " ".join(kw_list[:3]))
                rss_urls = await suggest_rss_sources(topic_for_rss, display_name)
            except Exception as e:
                logger.warning(f"RSS suggest ошибка: {e}")

    card = {
        "channel_id": channel_id,
        "name": display_name or channel_id,
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
        "use_images": True,
        "image_keywords": [],
        # Новый канал не постит по умолчанию — расписание включается через /schedule
        "schedule_disabled": True,
    }
    if channel_type == "marketplace":
        card["post_formats"] = ["wb_product"]
        card["use_images"] = False
    else:
        arch, src = normalize_meta(analysis.get("archetype"), analysis.get("topic_source"))
        card["archetype"] = arch
        card["topic_source"] = src

    save_channel_card(card)
    buffer.add_evergreen_topics(channel_id, card.get("evergreen_topics", []))
    return card, rss_urls


async def handle_add_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принимает @username/ссылку, читает канал юзерботом и анализирует."""
    from userbot_reader import read_channel, normalize_handle, UserbotNotAuthorized
    from channel_analyzer import analyzer

    handle = normalize_handle(update.message.text or "")
    if not handle or handle == "@":
        await update.message.reply_text(
            "⚠️ Не похоже на username. Пришли, например, <code>@durov</code>.\n/cancel — отменить",
            parse_mode=ParseMode.HTML,
        )
        return ADD_USERNAME

    # Уже добавлен?
    if any(ch["channel_id"].lower() == handle.lower() for ch in load_all_channels()):
        await update.message.reply_text(
            f"❌ Канал {handle} уже добавлен.\n/cancel — отменить"
        )
        return ADD_USERNAME

    msg = await update.message.reply_text("🔎 Анализирую канал…")

    try:
        data = await read_channel(handle, limit=50)
    except UserbotNotAuthorized:
        await msg.edit_text(
            "❌ Юзербот не авторизован — авто-чтение недоступно.\n"
            "Добавь канал вручную (✏️) или через экспорт (📂).\n/cancel — отменить"
        )
        return ADD_USERNAME
    except ValueError as e:
        await msg.edit_text(f"❌ {e}\n\nПопробуй другой username или /cancel.")
        return ADD_USERNAME
    except Exception as e:
        logger.error(f"Чтение канала {handle} не удалось: {e}")
        await msg.edit_text(
            f"❌ Не удалось прочитать канал: {e}\n\nПопробуй ещё раз или /cancel."
        )
        return ADD_USERNAME

    if data["post_count"] < 3:
        await msg.edit_text(
            f"❌ В канале {handle} мало текстовых постов ({data['post_count']}) — "
            f"не хватает для анализа.\nДобавь вручную (✏️) или пришли другой канал.\n/cancel"
        )
        return ADD_USERNAME

    try:
        analysis = await analyzer.analyze_posts(
            data["title"], data["posts"], about=data["about"]
        )
    except Exception as e:
        logger.error(f"Анализ {handle} не удался: {e}")
        await msg.edit_text(f"❌ Ошибка анализа: {e}\n/cancel — отменить")
        return ADD_USERNAME

    # Сохраняем в user_data для подтверждения
    real_handle = data["handle"]
    context.user_data["uname_analysis"] = analysis
    context.user_data["uname_handle"] = real_handle
    context.user_data["uname_title"] = data["title"]

    ch_type_label = (
        "🛍 Маркетплейс (WB/Ozon)" if analysis.get("channel_type") == "marketplace"
        else "📝 Контент-канал"
    )
    from archetypes import ARCHETYPE_LABELS
    arch_label = ARCHETYPE_LABELS.get(analysis.get("archetype", "default"), analysis.get("archetype", "default"))
    src_label = "🌐 веб-поиск" if analysis.get("topic_source") == "search" else "📡 по лентам"
    conf_pct = int(analysis.get("confidence", 0.8) * 100)
    topics_preview = "\n".join(f"  • {t}" for t in analysis.get("evergreen_topics", [])[:5])

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Добавить канал", callback_data="usernameconfirm:yes")],
        [InlineKeyboardButton("❌ Отмена",          callback_data="usernameconfirm:no")],
    ])
    await msg.edit_text(
        f"✅ <b>Анализ канала готов</b> (изучено постов: {analysis.get('analyzed_posts', '?')}, "
        f"уверенность: {conf_pct}%)\n\n"
        f"📌 <b>Канал:</b> {data['title']}  <code>{real_handle}</code>\n"
        f"🏷 <b>Тип:</b> {ch_type_label}\n"
        f"📝 <b>Тема:</b> {analysis.get('topic', '?')}\n"
        f"🎭 <b>Стиль:</b> {arch_label}\n"
        f"📰 <b>Источник тем:</b> {src_label}\n"
        f"📊 <b>Частота:</b> ~{analysis.get('post_frequency', 4)} поста/день\n"
        f"💡 <b>Постоянные темы:</b>\n{topics_preview}\n\n"
        f"Добавить с этими настройками? (тему и расписание можно изменить позже)",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )
    return ADD_USERNAME_CONFIRM


async def handle_username_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение создания канала, добавленного по username."""
    query = update.callback_query
    await query.answer()

    if query.data.split(":")[1] == "no":
        context.user_data.clear()
        await query.edit_message_text("❌ Добавление канала отменено.")
        return ConversationHandler.END

    analysis = context.user_data.get("uname_analysis", {})
    handle = context.user_data.get("uname_handle")
    title = context.user_data.get("uname_title", handle)
    if not analysis or not handle:
        await query.edit_message_text("❌ Данные устарели, начни заново: /add")
        return ConversationHandler.END

    card, rss_urls = await _create_channel_from_analysis(analysis, handle, title)

    ch_type_label = "🛍 Маркетплейс" if card.get("channel_type") == "marketplace" else "📝 Контент"
    meta_line = ""
    if card.get("channel_type") == "content":
        from archetypes import ARCHETYPE_LABELS
        arch_label = ARCHETYPE_LABELS.get(card.get("archetype", "default"), card.get("archetype", "default"))
        src_label = "🌐 веб-поиск" if card.get("topic_source") == "search" else "📡 по лентам"
        meta_line = f"Стиль: {arch_label}\nИсточник тем: {src_label}\n"

    await query.edit_message_text(
        f"🎉 <b>Канал добавлен!</b>\n\n"
        f"Handle: {handle}\n"
        f"Название: {title}\n"
        f"Тип: {ch_type_label}\n"
        f"Тема: {card.get('topic', '—')[:80]}\n"
        f"Постов в день: {card.get('daily_posts_count')}\n"
        + meta_line
        + (f"Лент подобрано: {len(rss_urls)}\n" if card.get("channel_type") == "content" else "") +
        f"\n⏸ Автопубликация <b>выключена</b> — включи расписание:\n"
        f"<code>/schedule {handle} 09 12 16 20</code>\n"
        f"\n⚠️ Чтобы бот мог публиковать, добавь его администратором в <code>{handle}</code>",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⚡ Сгенерировать первые посты", callback_data=f"ui:ch_generate:{handle}")],
            [InlineKeyboardButton("⚙️ Настройки канала",           callback_data=f"ui:ch_settings:{handle}")],
            [InlineKeyboardButton("◀️ В меню",                     callback_data="ui:main")],
        ]),
        parse_mode=ParseMode.HTML,
    )
    context.user_data.clear()
    return ConversationHandler.END


async def handle_add_bulk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Массовое добавление: список @username за раз. Для каждого — тот же авто-пайплайн
    (юзербот читает канал → анализ → карточка). Ошибки по каналу не рвут весь процесс.
    """
    from userbot_reader import read_channel, normalize_handle, UserbotNotAuthorized
    from channel_analyzer import analyzer

    raw = update.message.text or ""
    # режем по строкам/запятым/пробелам, нормализуем, убираем дубли
    tokens = raw.replace(",", "\n").split()
    handles, seen = [], set()
    for t in tokens:
        h = normalize_handle(t)
        if h and h != "@" and h.lower() not in seen:
            seen.add(h.lower())
            handles.append(h)

    if not handles:
        await update.message.reply_text(
            "⚠️ Не нашёл ни одного @username. Пришли списком, по одному в строке.\n/cancel",
        )
        return ADD_BULK

    MAX = 20
    if len(handles) > MAX:
        await update.message.reply_text(
            f"⚠️ Прислано {len(handles)} — это много. Беру первые {MAX}, остальные пришли отдельно."
        )
        handles = handles[:MAX]

    existing = {c["channel_id"].lower() for c in load_all_channels()}
    progress = await update.message.reply_text(f"⏳ Обрабатываю {len(handles)} каналов…")

    added, skipped = [], []
    for i, handle in enumerate(handles, 1):
        try:
            if handle.lower() in existing:
                skipped.append((handle, "уже добавлен"))
            else:
                data = await read_channel(handle, limit=50)
                if data["post_count"] < 3:
                    skipped.append((handle, f"мало постов ({data['post_count']})"))
                else:
                    analysis = await analyzer.analyze_posts(
                        data["title"], data["posts"], about=data["about"]
                    )
                    await _create_channel_from_analysis(analysis, data["handle"], data["title"])
                    existing.add(data["handle"].lower())
                    added.append(data["handle"])
        except UserbotNotAuthorized:
            skipped.append((handle, "юзербот не авторизован — стоп"))
            break  # без юзербота дальше смысла нет
        except ValueError as e:
            skipped.append((handle, str(e)[:60]))
        except Exception as e:
            logger.error(f"Массовое добавление {handle}: {e}")
            skipped.append((handle, "ошибка чтения/анализа"))

        try:
            await progress.edit_text(
                f"⏳ {i}/{len(handles)} … ✅ {len(added)}  ⏭ {len(skipped)}"
            )
        except Exception:
            pass

    lines = [f"📋 <b>Массовое добавление завершено</b>\n", f"✅ Добавлено: <b>{len(added)}</b>"]
    if added:
        lines.append("\n".join(f"  • {h}" for h in added))
    if skipped:
        lines.append(f"\n⏭ Пропущено: <b>{len(skipped)}</b>")
        lines.append("\n".join(f"  • {h} — {r}" for h, r in skipped))
    lines.append(
        "\n⏸ Автопубликация у всех выключена — включи через /schedule.\n"
        "⚠️ Добавь бота админом в каждый канал, чтобы он мог публиковать."
    )
    text = "\n".join(lines)
    await progress.edit_text(
        text[:4000],
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ В меню", callback_data="ui:main")]]),
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
        # Для маркетплейса RSS/тон не нужны — сразу спрашиваем категории
        context.user_data["new_channel"]["tone"] = "продающий, дружелюбный"
        await query.edit_message_text(
            "🛍 <b>Маркетплейс-канал</b>\n\n"
            "Бот будет находить товары WB и публиковать карточки с ценами.\n\n"
            "📦 <b>Категории товаров</b> (необязательно)\n\n"
            "Напиши категории через запятую — бот будет искать товары именно в них.\n"
            "Например: <code>кроссовки, наушники, косметика</code>\n\n"
            "Или нажми <b>Все категории</b> — парсер сам подберёт из кеша.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📦 Все категории", callback_data="wbcats:all"),
            ]]),
        )
        return ADD_WB_CATEGORIES

    else:
        # Контент — БОЛЬШЕ НЕ СПРАШИВАЕМ тон/запретки/RSS/картинки/кол-во.
        # Всё ставится автоматически: тон единый (_HUMAN_VOICE), запреток нет,
        # источники тем и стиль определяет Claude по теме, картинки = auto.
        ch = context.user_data["new_channel"]
        ch.setdefault("forbidden_topics", [])
        ch.setdefault("image_source", "auto")   # единое правило: RSS→сток→FLUX
        ch.setdefault("use_images", True)

        await query.edit_message_text(
            "⏳ Настраиваю канал: подбираю источники тем, вечнозелёные темы и стиль…"
        )

        from ai_client import suggest_rss_sources, suggest_evergreen_topics
        try:
            ch["rss_sources"] = await suggest_rss_sources(ch["topic"], ch.get("name", ""))
        except Exception as e:
            logger.warning(f"Авто-RSS не удалось [{ch['channel_id']}]: {e}")
            ch.setdefault("rss_sources", [])
        try:
            ch["evergreen_topics"] = await suggest_evergreen_topics(ch["topic"], count=8)
        except Exception as e:
            logger.warning(f"Авто-evergreen не удалось [{ch['channel_id']}]: {e}")
            ch.setdefault("evergreen_topics", [])

        # Кол-во постов в день по умолчанию (меняется в карточке канала)
        await _finalize_new_channel(query, context, count=10)
        return ConversationHandler.END


async def handle_add_wb_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Шаг WB-канала: получает категории товаров (текстом или кнопкой 'Все').
    Затем спрашивает кол-во постов в день.
    """
    ch = context.user_data["new_channel"]

    # Кнопка "Все категории"
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        # Категории не задаём — парсер возьмёт из общего кеша
        ch.pop("wb_categories", None)
        msg_obj = query.message
        await query.edit_message_text(
            "✅ Будут использоваться все доступные категории из кеша.\n\n"
            "Сколько постов генерировать в день?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("4 поста",  callback_data="postscount:4"),
                InlineKeyboardButton("8 постов",  callback_data="postscount:8"),
                InlineKeyboardButton("20 постов", callback_data="postscount:20"),
            ]]),
        )
    else:
        # Пришёл текст с категориями
        text = update.message.text.strip()
        cats = [c.strip() for c in text.replace("\n", ",").split(",") if c.strip()]
        if not cats:
            await update.message.reply_text(
                "⚠️ Не понял. Напиши категории через запятую, например:\n"
                "<code>кроссовки, наушники, косметика</code>\n\n"
                "Или нажми кнопку ниже:",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📦 Все категории", callback_data="wbcats:all"),
                ]]),
            )
            return ADD_WB_CATEGORIES

        ch["wb_categories"] = cats
        cats_str = ", ".join(cats)
        await update.message.reply_text(
            f"✅ Категории: <b>{cats_str}</b>\n\n"
            f"Сколько постов генерировать в день?",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("4 поста",  callback_data="postscount:4"),
                InlineKeyboardButton("8 постов",  callback_data="postscount:8"),
                InlineKeyboardButton("20 постов", callback_data="postscount:20"),
            ]]),
        )

    return ADD_POSTS_COUNT


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

    from ai_client import sanitize_field, FIELD_LIMITS
    new_topic = sanitize_field(update.message.text, FIELD_LIMITS["topic"])
    channels_dir = Path(__file__).parent / "channels"
    handle_clean = safe_slug(channel_id)
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

    Использование:
        /review          — выбор канала через inline-кнопки
        /review @channel — посты конкретного канала сразу
    """
    if not is_admin(update.effective_user.id):
        return

    args = context.args or []

    if args:
        # Прямой переход к постам конкретного канала
        channel_id = args[0] if args[0].startswith("@") else f"@{args[0]}"
        await _send_review_page(update.message, channel_id, offset=0)
    else:
        # Показываем выбор канала через inline-кнопки
        channels = load_all_channels()
        if not channels:
            await update.message.reply_text("Нет активных каналов. Добавь через /add")
            return

        buttons = []
        row = []
        for ch in channels:
            count = buffer.get_ready_count(ch["channel_id"])
            icon = "📭" if count == 0 else "📋"
            label = f"{icon} {ch['channel_id']} · {count}"
            row.append(InlineKeyboardButton(label, callback_data=f"review_ch:{ch['channel_id']}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

        await update.message.reply_text(
            "📋 <b>Очередь постов</b>\n\nВыбери канал для просмотра:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )


async def handle_review_channel_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопки выбора канала из /review (review_ch:@channel)."""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    channel_id = query.data.split(":", 1)[1]
    await query.edit_message_reply_markup(reply_markup=None)  # убираем клавиатуру выбора
    await _send_review_page(query.message, channel_id, offset=0)


async def handle_review_next_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопки пагинации «Следующие N →» / «← Сначала»."""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    # format: review_page:@channel:offset
    parts = query.data.split(":", 2)
    channel_id = parts[1]
    offset = int(parts[2])

    # Убираем кнопку пагинации у предыдущей страницы
    await query.edit_message_reply_markup(reply_markup=None)
    await _send_review_page(query.message, channel_id, offset=offset)


async def _send_review_page(message, channel_id: str, offset: int):
    """
    Вспомогательная функция: показывает страницу постов канала.
    Работает как с message (из команды), так и с query.message (из кнопки).
    """
    PAGE = 5

    with db.connect() as conn:
        rows = conn.execute(
            """SELECT * FROM posts
               WHERE channel_id = ? AND status = 'ready'
               ORDER BY generated_at ASC""",
            (channel_id,),
        ).fetchall()

    posts = [dict(r) for r in rows]
    total = len(posts)

    if total == 0:
        await message.reply_text(
            f"📭 Очередь <b>{channel_id}</b> пуста.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚡ Сгенерировать посты", callback_data=f"ui:ch_generate:{channel_id}")],
                [InlineKeyboardButton("◀️ К каналу",           callback_data=f"ui:ch:{channel_id}")],
            ]),
        )
        return

    offset = min(offset, total - 1)  # защита от выхода за границы
    page_posts = posts[offset:offset + PAGE]
    shown_end = offset + len(page_posts)
    page_num = (offset // PAGE) + 1
    total_pages = (total + PAGE - 1) // PAGE

    # Заголовок страницы
    await message.reply_text(
        f"📋 <b>{channel_id}</b> · {total} постов\n"
        f"Страница {page_num}/{total_pages} · показываю {offset + 1}–{shown_end}.\n"
        f"Редактируй или оставь — постер опубликует по расписанию.",
        parse_mode=ParseMode.HTML,
    )

    # Показываем посты страницы
    for i, post in enumerate(page_posts, start=offset + 1):
        msg_text = format_post_message(post, index=i, total=total)
        keyboard = review_keyboard(post["id"])

        if post.get("image_url"):
            try:
                await message.reply_photo(
                    photo=post["image_url"],
                    caption=msg_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
                continue
            except Exception:
                # Картинка по ссылке не открылась — честно сообщаем об этом
                msg_text = msg_text.replace(
                    "🖼 Есть картинка",
                    "⚠️ Картинка недоступна (перегенерируется при публикации)",
                )

        await message.reply_text(
            msg_text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

    # Кнопки навигации
    nav_buttons = []

    if offset > 0:
        nav_buttons.append(
            InlineKeyboardButton("← Сначала", callback_data=f"review_page:{channel_id}:0")
        )

    if shown_end < total:
        remaining = total - shown_end
        next_label = f"Следующие {min(PAGE, remaining)} →"
        nav_buttons.append(
            InlineKeyboardButton(next_label, callback_data=f"review_page:{channel_id}:{shown_end}")
        )

    # Навигационная строка: пагинация + кнопка «Назад к каналу»
    back_btn = InlineKeyboardButton("◀️ К каналу", callback_data=f"ui:ch:{channel_id}")

    if nav_buttons:
        await message.reply_text(
            f"👆 {offset + 1}–{shown_end} из {total}",
            reply_markup=InlineKeyboardMarkup([nav_buttons, [back_btn]]),
        )
    else:
        # Последняя (или единственная) страница
        label = f"✅ Все посты показаны ({total} шт.)" if total > PAGE else f"📋 {total} постов в очереди"
        await message.reply_text(
            label,
            reply_markup=InlineKeyboardMarkup([[back_btn]]),
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
    """Запускает генерацию для конкретного канала.

    Использование:
      /generate @channel — генерация для одного канала
    """
    if not is_admin(update.effective_user.id):
        return

    args = context.args or []

    if not args:
        channels = load_all_channels()
        if not channels:
            await update.message.reply_text("Каналов нет. Добавь канал через меню.")
            return
        lines = ["Укажи канал: <code>/generate @handle</code>\n\nДоступные каналы:"]
        for ch in channels:
            lines.append(f"  • <code>/generate {ch['channel_id']}</code>")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        return

    raw = args[0]
    channel_id = raw if raw.startswith("@") else f"@{raw}"

    channels = load_all_channels()
    if not any(c["channel_id"] == channel_id for c in channels):
        await update.message.reply_text(
            f"❌ Канал <code>{channel_id}</code> не найден.",
            parse_mode=ParseMode.HTML,
        )
        return

    await update.message.reply_text(
        f"⏳ <b>Генерация для {channel_id} запущена в фоне</b>\n\nПришлю результат когда готово.",
        parse_mode=ParseMode.HTML,
    )
    asyncio.create_task(
        _run_generation_background(context.bot, update.effective_chat.id, force=True, channel_id=channel_id)
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

async def handle_img_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает выбор способа добавления картинки к посту:
      upload   — пользователь отправит фото сам
      auto     — бот подбирает новую картинку автоматически
      generate — заглушка для будущей генерации через API
      cancel   — отмена
    """
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    _, action, post_id = query.data.split(":", 2)

    if action == "upload":
        # Просим пользователя прислать фото
        context.user_data["awaiting_image_for"] = post_id
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Отмена", callback_data=f"img_action:cancel:{post_id}")
        ]])
        await query.edit_message_text(
            "📤 <b>Отправь фото</b>\n\n"
            "Пришли фото прямо в этот чат — из галереи или скачай с интернета.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )

    elif action == "auto":
        # Ищем новую картинку автоматически через image_fetcher
        await query.edit_message_text("🔄 Подбираю новую картинку...")
        try:
            with db.connect() as conn:
                row = conn.execute(
                    "SELECT topic, channel_id FROM posts WHERE id = ?", (post_id,)
                ).fetchone()
            if not row:
                raise ValueError("Пост не найден")

            from image_fetcher import fetch_image_url

            channels = load_all_channels()
            channel = next((c for c in channels if c["channel_id"] == row["channel_id"]), {})
            new_url = await fetch_image_url(
                topic=row["topic"],
                channel_topic=channel.get("topic", "") if channel else "",
                subreddits=channel.get("reddit_image_subreddits") if channel else None,
                channel_name=channel.get("name", "") if channel else "",
                image_keywords=channel.get("image_keywords") if channel else None,
            )

            if new_url:
                with db.connect() as conn:
                    conn.execute(
                        "UPDATE posts SET image_url = ? WHERE id = ?", (new_url, post_id)
                    )
                channel_id = row["channel_id"]
                await query.edit_message_text(
                    "✅ <b>Новая картинка подобрана!</b>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("◀️ К очереди постов", callback_data=f"ui:ch_review:{channel_id}")
                    ]]),
                )
            else:
                await query.edit_message_text(
                    "😔 Не удалось найти подходящую картинку.\nПопробуй отправить своё фото.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("📤 Отправить своё фото", callback_data=f"img_action:upload:{post_id}"),
                        InlineKeyboardButton("◀️ Отмена", callback_data=f"img_action:cancel:{post_id}"),
                    ]]),
                )
        except Exception as e:
            logger.error(f"Ошибка автоподбора картинки: {e}")
            await query.edit_message_text(
                f"❌ Ошибка: {e}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Отмена", callback_data=f"img_action:cancel:{post_id}")
                ]]),
            )

    elif action == "generate":
        from image_generator import generate_image
        if not cfg.FAL_API_KEY:
            await query.edit_message_text(
                "🎨 <b>Генерация изображений</b>\n\n"
                "❌ FAL_API_KEY не задан в .env\n"
                "Зарегистрируйся на fal.ai и добавь ключ.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Назад", callback_data=f"img_action:back:{post_id}")
                ]]),
            )
            return

        # Показываем прогресс
        await query.edit_message_text(
            "🎨 <b>Генерирую картинку...</b>\n\n"
            "⚡ FLUX AI работает, обычно 3-10 секунд.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏳ Генерация...", callback_data="noop")
            ]]),
        )

        try:
            with db.connect() as conn:
                row = conn.execute(
                    "SELECT topic, channel_id FROM posts WHERE id=?", (post_id,)
                ).fetchone()

            channels = load_all_channels()
            channel = next((c for c in channels if c["channel_id"] == row["channel_id"]), {})

            new_url = await generate_image(
                topic=row["topic"],
                channel_topic=channel.get("topic", ""),
                channel_name=channel.get("name", ""),
            )

            if new_url:
                with db.connect() as conn:
                    conn.execute(
                        "UPDATE posts SET image_url=?, has_image=1 WHERE id=?",
                        (new_url, post_id)
                    )
                    conn.commit()
                await query.edit_message_text(
                    "✅ <b>Картинка сгенерирована!</b>\n\nОткрой пост чтобы увидеть результат.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "📋 К очереди постов",
                            callback_data=f"ui:ch_review:{row['channel_id']}"
                        )
                    ]]),
                )
            else:
                await query.edit_message_text(
                    "😔 Не удалось сгенерировать картинку.\nПопробуй ещё раз или загрузи своё фото.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔄 Попробовать снова", callback_data=f"img_action:generate:{post_id}"),
                        InlineKeyboardButton("📤 Своё фото", callback_data=f"img_action:upload:{post_id}"),
                    ]]),
                )
        except Exception as e:
            logger.error(f"Ошибка генерации картинки: {e}")
            await query.edit_message_text(
                f"❌ Ошибка: {e}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Назад", callback_data=f"img_action:back:{post_id}")
                ]]),
            )

    elif action in ("cancel", "back"):
        # Убираем состояние ожидания фото
        context.user_data.pop("awaiting_image_for", None)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.delete()


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
        logger.info(f"Пост удалён из очереди: {post_id[:8]}")
        # Удаляем само сообщение с постом — не висит в ленте
        try:
            await query.message.delete()
        except Exception:
            # Если удалить не удалось — хотя бы убираем кнопки
            await query.edit_message_reply_markup(reply_markup=None)

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
        context.user_data.pop("editing_post_id", None)

        # Получаем тему поста для автоподбора
        with db.connect() as conn:
            row = conn.execute(
                "SELECT topic, channel_id FROM posts WHERE id = ?", (post_id,)
            ).fetchone()
        topic     = row["topic"]     if row else ""
        chan_id   = row["channel_id"] if row else ""

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Отправить своё фото", callback_data=f"img_action:upload:{post_id}")],
            [InlineKeyboardButton("🔄 Подобрать новую автоматически", callback_data=f"img_action:auto:{post_id}")],
            [InlineKeyboardButton("🎨 Сгенерировать изображение", callback_data=f"img_action:generate:{post_id}")],
            [InlineKeyboardButton("◀️ Отмена", callback_data=f"img_action:cancel:{post_id}")],
        ])
        await query.message.reply_text(
            "🖼 <b>Картинка для поста</b>\n\n"
            "Выбери способ:",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
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
                # Картинка была, но URL не сработал — чистим из базы
                with db.connect() as conn:
                    conn.execute(
                        "UPDATE posts SET image_url = NULL WHERE id = ?",
                        (post_id,),
                    )
                label = "✅ Опубликовано (без картинки — URL недоступен)"
            else:
                label = "✅ Опубликовано!"
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(label, callback_data="done")
            ]]))
            logger.info(f"Пост опубликован вручную из /review: {post_id[:8]}")
            # Удаляем карточку поста из чата через 3 секунды
            await asyncio.sleep(3)
            try:
                await query.message.delete()
            except Exception:
                pass  # Если не удалось удалить — не страшно
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

    Сначала проверяет не находится ли пользователь в режиме
    редактирования настроек канала (ui.py editing).
    """
    if not is_admin(update.effective_user.id):
        return

    # Поиск канала (из «Мои каналы → 🔍 Поиск»)
    if context.user_data.pop("channel_search", False):
        from ui import screen_channels_search
        await screen_channels_search(update.message, context, update.message.text or "")
        return

    # Сначала пробуем обработать как ввод настроек (ui.py)
    if context.user_data.get("editing"):
        handled = await handle_settings_text_input(update, context)
        if handled:
            return

    # Проверка «Меню» — отдельный handler, но на всякий случай
    text_msg = update.message.text or ""
    if text_msg.strip() in ("☰ Меню", "☰Меню", "Меню"):
        await screen_main(update.message, context)
        return

    # Проверяем тест генерации картинок
    if await handle_img_test_input(update, context):
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

    # Это наш канал? (бот видит channel_post только по каналам, где он админ)
    channels = load_all_channels()
    channel = next((c for c in channels if c["channel_id"] == channel_id), None)
    if not channel:
        return  # не наш канал — игнор

    if not is_rsy_ad(message):
        # Обычный (ручной) пост админа — это публикация. Бот свои посты обратно
        # как апдейт НЕ получает, значит это ручной пост → фиксируем время, чтобы
        # ближайший плановый слот (в пределах MIN_GAP) не выдал дубль.
        poster.record_published(channel_id)
        logger.info(f"Ручной пост в {channel_id} — обновил last_published (слот рядом пропустится)")
        return

    # Реклама РСЯ. Перекрываем только если включено для канала.
    if not channel.get("rsy_override", False):
        return  # перекрытие выключено — плановый слот сам прикроет рекламу

    logger.info(f"📢 Обнаружена реклама РСЯ в {channel_id} | message_id: {message.message_id}")

    # Случайная задержка 5–15 минут — имитируем живого редактора.
    # ПЕРСИСТЕНТНО: запись в БД, а не asyncio.Task — переживает рестарт сервиса.
    # Публикацию выполнит планировщик (process_due_ads) когда придёт due_at.
    import random
    delay_seconds = random.randint(cfg.POST_DELAY_MIN, cfg.POST_DELAY_MAX)
    due_at = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat()

    if buffer.record_pending_ad(channel_id, message.message_id, due_at):
        logger.info(
            f"РСЯ-перекрытие {channel_id} запланировано через ~{delay_seconds // 60} мин "
            f"(в БД, переживёт рестарт)"
        )
    else:
        logger.debug(f"РСЯ {channel_id}/{message.message_id} уже обрабатывается — пропускаю")


async def process_due_ads(bot):
    """
    Публикует «дозревшие» РСЯ-перекрытия (вызывается планировщиком раз в минуту).
    Переживает рестарт: задачи лежат в БД (processed_ads), а не в памяти.
    """
    now = datetime.now(timezone.utc)
    due = buffer.get_due_ads(now.isoformat())
    if not due:
        return

    for ad in due:
        cid = ad["channel_id"]
        ad_id = ad["id"]

        # Если просрочено сильно (бот лежал > 2ч) — поздно перекрывать, помечаем expired
        try:
            due_dt = datetime.fromisoformat(ad["due_at"])
            if due_dt.tzinfo is None:
                due_dt = due_dt.replace(tzinfo=timezone.utc)
            if (now - due_dt).total_seconds() > 2 * 3600:
                buffer.mark_ad_failed(ad_id, "expired")
                logger.warning(f"РСЯ-перекрытие {cid} просрочено (>2ч) — пропускаю")
                continue
        except Exception:
            pass

        # Перекрытие публикуем ВСЕГДА, когда дозрело (реклама важнее; у неё свои
        # окна и она не выходит два раза подряд). MIN_GAP к перекрытию не применяем.

        try:
            # Буфер пуст — экстренно генерируем 1 пост
            if buffer.get_ready_count(cid) == 0:
                channels = load_all_channels()
                channel = next((c for c in channels if c["channel_id"] == cid), None)
                if channel:
                    from content_generator import generator
                    logger.info(f"РСЯ {cid}: буфер пуст — экстренная генерация")
                    await generator.run_for_channel(channel, target_count=1, force=True)

            result = await poster.post_now(cid)
            if result.get("success"):
                post = result.get("post", {})
                buffer.mark_ad_published(ad_id, post.get("id"))
                logger.success(f"✅ Реклама РСЯ перекрыта в {cid}")
                await bot.send_message(
                    chat_id=cfg.ADMIN_CHAT_ID,
                    text=f"✅ <b>Реклама РСЯ перекрыта</b> в {cid}\nФормат: {post.get('format', '?')}",
                    parse_mode=ParseMode.HTML,
                )
            else:
                buffer.mark_ad_failed(ad_id)
                logger.error(f"❌ Не перекрыл рекламу в {cid}: {result.get('error')}")
                await bot.send_message(
                    chat_id=cfg.ADMIN_CHAT_ID,
                    text=(
                        f"❌ <b>Не смог перекрыть рекламу в {cid}</b>\n"
                        f"Причина: {result.get('error')}\nПопробуй /post_now {cid}"
                    ),
                    parse_mode=ParseMode.HTML,
                )
        except Exception as e:
            buffer.mark_ad_failed(ad_id)
            logger.error(f"Ошибка перекрытия РСЯ [{cid}]: {e}")
            try:
                await bot.send_message(
                    chat_id=cfg.ADMIN_CHAT_ID,
                    text=f"❌ Ошибка перекрытия РСЯ для {cid}: {e}",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass


# ============================================================
# Relay-референсы: ловим медиа, пересланное юзерботом в ЛС бота
# ============================================================

async def handle_userbot_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Юзербот пересылает медиа-посты доноров в ЛС бота (обычный форвард). Здесь
    достаём file_id и привязываем к ожидающей записи буфера (status=awaiting_media)
    по ключу topic = 'ref:донор:msg_id'. Файл на диск НЕ пишем — храним file_id.
    """
    from buffer_manager import buffer
    from reference_importer import ref_topic

    msg = update.effective_message
    if not msg:
        return

    # Должно быть переслано из канала-донора (forward_from_* в PTB 20.7)
    src_chat = getattr(msg, "forward_from_chat", None)
    src_id = getattr(msg, "forward_from_message_id", None)
    donor = getattr(src_chat, "username", None) if src_chat else None
    if not donor or not src_id:
        return  # не форвард из канала с username — матчить нечем

    # Достаём file_id и тип (порядок важен: animation проверяем до document)
    file_id = media_type = None
    if msg.photo:
        file_id, media_type = msg.photo[-1].file_id, "photo"
    elif getattr(msg, "animation", None):
        file_id, media_type = msg.animation.file_id, "animation"
    elif msg.video:
        file_id, media_type = msg.video.file_id, "video"
    elif msg.document:
        file_id, media_type = msg.document.file_id, "document"
    if not file_id:
        return

    # Сначала пробуем как кадр альбома (media_group), иначе — одиночное медиа
    topic_prefix = f"ref:{donor.lower()}:"
    matched = False
    if buffer.attach_album_member(topic_prefix, src_id, file_id, media_type):
        logger.info(f"Relay: кадр альбома {donor}/{src_id} привязан")
        matched = True
    else:
        topic = ref_topic(donor, src_id)
        if buffer.attach_reference_media(topic, file_id, media_type):
            logger.info(f"Relay: привязал {media_type} к {topic} → ready")
            matched = True
        else:
            logger.debug(f"Relay: нет awaiting_media для {topic} (уже привязано/чужой форвард)")

    # Чистим ЛС бота: file_id уже сохранён и остаётся валидным после удаления,
    # поэтому удаляем пересланное сообщение, чтобы не засорять чат с ботом.
    if matched:
        try:
            await msg.delete()
        except Exception as e:
            logger.debug(f"Relay: не смог удалить служебное сообщение из ЛС: {e}")


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
# Глобальный обработчик ошибок
# ============================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """
    Ловит все необработанные исключения в хендлерах.
    Без него ошибка просто молча падала в лог, а пользователь ничего не получал.
    Логирует traceback и уведомляет администратора.
    """
    logger.exception(f"Необработанная ошибка в хендлере: {context.error}")

    # Пытаемся уведомить администратора (коротко, без traceback в чат)
    try:
        err_type = type(context.error).__name__
        await context.bot.send_message(
            chat_id=cfg.ADMIN_CHAT_ID,
            text=(
                f"⚠️ <b>Ошибка в боте</b>\n"
                f"<code>{err_type}: {str(context.error)[:300]}</code>\n"
                f"Подробности в логах."
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Не удалось отправить алерт об ошибке: {e}")


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
    # Обрабатываем апдейты параллельно: один медленный хендлер (напр. импорт
    # референсов через Telethon) не должен морозить всю очередь команд.
    builder = builder.concurrent_updates(True)
    if proxy_url:
        builder = builder.proxy_url(proxy_url).get_updates_proxy_url(proxy_url)
        logger.info(f"Используется прокси: {proxy_url}")
    app = builder.build()

    # Глобальный обработчик ошибок — ловит всё, что не поймали хендлеры
    app.add_error_handler(error_handler)

    # --- Диалог добавления канала (/add) ---
    add_channel_conv = ConversationHandler(
        entry_points=[
            CommandHandler("add", cmd_add_start),
            CallbackQueryHandler(cmd_add_start, pattern="^add_start$"),
        ],
        states={
            # Шаг 0: выбор метода (кнопки)
            ADD_CHOOSE_METHOD:  [CallbackQueryHandler(handle_add_method_choice, pattern="^addmethod:")],

            # Авто-добавление по @username (Telethon-юзербот)
            ADD_USERNAME:         [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_add_username)],
            ADD_USERNAME_CONFIRM: [CallbackQueryHandler(handle_username_confirm, pattern="^usernameconfirm:")],
            ADD_BULK:             [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_add_bulk)],

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
            # Шаги выбора картинок (контент-каналы)
            ADD_IMAGE_SOURCE:   [CallbackQueryHandler(handle_add_image_source, pattern="^imgsource:")],
            ADD_REDDIT_SUBS:    [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_add_reddit_subs)],
            # Шаг категорий WB (маркетплейс)
            ADD_WB_CATEGORIES:  [
                CallbackQueryHandler(handle_add_wb_categories, pattern="^wbcats:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_add_wb_categories),
            ],
            ADD_POSTS_COUNT:    [CallbackQueryHandler(cmd_add_posts_count, pattern="^postscount:")],
        },
        per_message=False,
        fallbacks=[
            CommandHandler("cancel", cmd_add_cancel),
            CallbackQueryHandler(cmd_add_cancel_inline, pattern="^add_cancel_inline$"),
            CallbackQueryHandler(cmd_add_to_menu, pattern="^add_to_menu$"),
        ],
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

    # --- UI: главный роутер inline-меню (все callback начинающиеся с "ui:") ---
    app.add_handler(CallbackQueryHandler(ui_router, pattern="^ui:"))

    # --- UI: кнопка «Меню» и «add_start» (запуск /add из меню) ---
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex(r"^☰\s*[Мм]еню?$") & filters.ChatType.PRIVATE,
        lambda u, c: screen_main(u.message, c),
    ))
    # add_start теперь entry_point в add_channel_conv выше

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

    # --- Кнопки: /review — выбор канала и пагинация ---
    app.add_handler(CallbackQueryHandler(handle_review_channel_select, pattern="^review_ch:"))
    app.add_handler(CallbackQueryHandler(handle_review_next_page, pattern="^review_page:"))

    # --- Кнопки: редактирование постов ---
    app.add_handler(CallbackQueryHandler(handle_post_actions, pattern="^(delete|image|regen|done|postnow):"))

    # --- Кнопки: действия с картинкой поста ---
    app.add_handler(CallbackQueryHandler(handle_img_action, pattern="^img_action:"))

    # --- Кнопки: превью поста ---
    app.add_handler(CallbackQueryHandler(handle_preview_actions, pattern="^preview_(queue|now|regen|discard):"))

    # --- Кнопки: управление каналами ---
    app.add_handler(CallbackQueryHandler(handle_channel_actions, pattern="^(removech|confirmremove|cancelremove|settopic):"))

    # --- Relay-референсы: медиа, пересланное юзерботом в ЛС бота ---
    # ВАЖНО: регистрируем ДО handle_photo_for_post, иначе форварднутое фото
    # перехватит обработчик «картинка для поста».
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.FORWARDED,
        handle_userbot_forward,
    ))

    # --- Текстовые сообщения от админа (URL картинки / новая тема канала) ---
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_image_url,
    ))

    # --- Фото от админа (картинка для поста) ---
    app.add_handler(MessageHandler(
        filters.PHOTO & filters.ChatType.PRIVATE & ~filters.FORWARDED,
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

        # job_defaults применяются ко ВСЕМ задачам — защита от рестартов/простоя:
        #   coalesce=True       — после простоя пропущенные запуски схлопываются в один
        #                         (нет burst-догонки, напр. постер не выстрелит 5 раз подряд)
        #   max_instances=1     — задача не наложится сама на себя (нет дублей/гонок)
        scheduler = AsyncIOScheduler(
            timezone="UTC",
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
        )

        # Постер — каждый час проверяет все каналы
        scheduler.add_job(
            poster.tick,
            CronTrigger(minute=0),
            id="poster_tick",
            name="Публикация постов",
            misfire_grace_time=300,
        )

        # Ступенчатая генерация — каждый час (в :30, со сдвигом от постера),
        # подливает понемногу только каналы с просевшим буфером. Распределяет
        # нагрузку по дню вместо одного большого батча ночью.
        scheduler.add_job(
            generator.run_top_up_cycle,
            CronTrigger(minute=30),
            id="topup_generation",
            name="Ступенчатая генерация",
            misfire_grace_time=600,
            max_instances=1,  # не запускать второй цикл, пока идёт первый
        )

        # Импорт референсов — раз в день (08:00 UTC = 11:00 МСК): забираем
        # новые посты каналов-доноров в буфер.
        from reference_importer import import_all as import_references_all
        scheduler.add_job(
            import_references_all,
            CronTrigger(hour=8, minute=0),
            id="reference_import",
            name="Импорт референсов",
            misfire_grace_time=3600,
            max_instances=1,
        )

        # Чистка зависших relay-референсов: записи awaiting_media, к которым так
        # и не пришло медиа от юзербота (удаляем, чтобы пост можно было взять заново).
        async def _cleanup_awaiting():
            from buffer_manager import buffer
            buffer.cleanup_awaiting(older_than_minutes=60)

        scheduler.add_job(
            _cleanup_awaiting,
            CronTrigger(minute=15),
            id="cleanup_awaiting",
            name="Чистка зависших awaiting_media",
            misfire_grace_time=300,
        )

        # РСЯ-перекрытия: раз в минуту проверяем «дозревшие» (персистентно в БД).
        # Переживает рестарт: после перезапуска задача подхватит отложенные перекрытия.
        async def _process_rsy_overlays():
            await process_due_ads(application.bot)

        scheduler.add_job(
            _process_rsy_overlays,
            CronTrigger(second=0),   # каждую минуту
            id="rsy_overlays",
            name="Публикация РСЯ-перекрытий",
            misfire_grace_time=120,
            max_instances=1,
        )

        scheduler.start()
        application.bot_data["scheduler"] = scheduler

        # Реконсиляция на старте: ВСЕ awaiting_media на момент старта — сироты
        # (форвард медиа был в уже умершем процессе, а backlog апдейтов дропается
        # при старте), media к ним уже не придёт. Чистим все → их можно взять заново.
        try:
            from buffer_manager import buffer as _buf
            n_cleaned = _buf.cleanup_awaiting(older_than_minutes=0)
            if n_cleaned:
                logger.info(f"Старт: подчищено зависших awaiting_media: {n_cleaned}")
        except Exception as e:
            logger.warning(f"Старт: реконсиляция awaiting_media не удалась: {e}")

        logger.success(
            "Планировщик запущен:\n"
            "  • Постер: каждый час (:00)\n"
            "  • Ступенчатая генерация: каждый час (:30), только просевшие каналы\n"
            "  • Импорт референсов: раз в день (08:00 UTC)\n"
            "  • Чистка awaiting_media: каждый час (:15)\n"
            "  • РСЯ-перекрытия: каждую минуту (персистентно)"
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
