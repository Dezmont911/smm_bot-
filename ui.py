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
  ui:ch_create:@handle        → экран создания поста
  ui:ch_generate:@handle      → запустить генерацию
  ui:ch_postnow:@handle       → опубликовать следующий пост
  ui:ch_review:@handle        → очередь постов канала
  ui:ch_set:@handle:field     → начать редактирование поля
  ui:status                   → общий статус буферов
  ui:generate_all             → сгенерировать для всех каналов
"""

import asyncio
import html
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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
import accounts
from boost_manager import (
    add_tracked_channel,
    add_tracked_channel_from_smm_channel,
    boost_configured,
    boost_real_orders_allowed,
    boost_status,
    delete_tracked_channel,
    find_tracked_channel_for_input,
    find_tracked_channel_for_smm_channel,
    get_boost_settings,
    get_tracked_channel,
    link_tracked_channel_to_smm_channel,
    list_boost_events,
    list_tracked_channels,
    normalize_channel_input,
    parse_boost_quantity,
    set_boost_enabled,
    set_tracked_channel_enabled,
    set_tracked_channel_quantity,
)


def _acting_uid(qm) -> int | None:
    """Telegram id того, кто инициировал экран (Message или CallbackQuery)."""
    u = getattr(qm, "from_user", None)
    return u.id if u else None


def _owns(user_id, ch: dict) -> bool:
    """🔒 Владелец канала или админ."""
    if user_id is None:
        return False
    if accounts.is_admin(user_id):
        return True
    return bool(ch) and ch.get("owner_id") == user_id


# ── Постоянная ReplyKeyboard с кнопкой «Меню» ─────────────────────────────

MENU_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("☰ Меню")]],
    resize_keyboard=True,
    is_persistent=True,
)

ADMIN_SETTINGS_PATH = Path(__file__).parent / "admin_settings.json"


def _load_admin_settings() -> dict:
    """Локальные настройки админ-панели без миграции БД."""
    try:
        if ADMIN_SETTINGS_PATH.exists():
            return json.loads(ADMIN_SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Не удалось прочитать admin_settings.json: {e}")
    return {}


def _save_admin_settings(data: dict):
    try:
        ADMIN_SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error(f"Не удалось сохранить admin_settings.json: {e}")


def admin_default_rsy_enabled() -> bool:
    """Включать ли РСЯ-перекрытие по умолчанию для новых каналов superadmin."""
    return bool(_load_admin_settings().get("default_rsy_override", False))


def _boost_mode_label() -> str:
    settings = get_boost_settings()
    if boost_real_orders_allowed(settings, cfg):
        return "реальные заказы"
    return "тестовый режим"


def _boost_real_order_hint(settings: dict) -> str:
    if boost_real_orders_allowed(settings, cfg):
        return "Реальные заказы включены. Новые публичные посты будут отправляться в TwiBoost."

    reasons = []
    if not settings.get("boost_enabled"):
        reasons.append("глобальный Boost выключен")
    if getattr(cfg, "BOOST_DRY_RUN", True):
        reasons.append("включен тестовый режим")
    if not getattr(cfg, "BOOST_REAL_ORDERS_ENABLED", False):
        reasons.append("реальные заказы не разрешены в настройках")
    if not boost_configured(cfg):
        reasons.append("TwiBoost API не настроен")
    if not _boost_service_configured(settings):
        reasons.append("ID сервиса не настроен")

    reason_text = "; ".join(reasons) if reasons else "не выполнены условия безопасности"
    return (
        "Реальные заказы сейчас НЕ отправляются. "
        f"Причина: {reason_text}. "
        "Бот только записывает тестовые или диагностические события."
    )


BOOST_PICKER_PAGE_SIZE = 8


def _clear_boost_pending(context: ContextTypes.DEFAULT_TYPE):
    for key in (
        "boost_add_channel",
        "boost_add_external_channel",
        "boost_add_smm_channel_id",
        "boost_set_quantity_for",
    ):
        context.user_data.pop(key, None)


def _boost_service_configured(settings: dict | None = None) -> bool:
    settings = settings or get_boost_settings()
    return bool(settings.get("default_service_id") or getattr(cfg, "TWIBOOST_VIEWS_SERVICE_ID", None))


def _boost_onoff_label(value: bool) -> str:
    return "вкл" if value else "выкл"


def _boost_yesno_label(value: bool) -> str:
    return "да" if value else "нет"


def _boost_config_label(value: bool) -> str:
    return "настроено" if value else "не настроено"


def _boost_none_label(value) -> str:
    return str(value) if value not in (None, "") else "нет"


def _boost_link_label(value) -> str:
    if value in (None, ""):
        return "внешний/ручной"
    if str(value) in ("external", "external/manual"):
        return "внешний/ручной"
    return str(value)


def _boost_status_label(status: str | None) -> str:
    return {
        "disabled": "выключен",
        "dry_run": "тестовый заказ",
        "enabled": "включен",
        "ordered": "заказ создан",
        "ignored": "пропущено",
        "duplicate": "дубликат",
        "failed": "ошибка",
    }.get(status or "", status or "нет")


def _boost_reason_label(reason: str | None) -> str:
    return {
        "boost_disabled": "Boost выключен",
        "boost_global_disabled": "Boost выключен глобально",
        "channel_disabled": "Boost для канала выключен",
        "boost_channel_disabled": "Boost для канала выключен",
        "no_chat": "нет данных чата",
        "not_tracked": "канал не отслеживается",
        "no_message_id": "нет ID сообщения",
        "no_event_key": "не удалось собрать ключ события",
        "already_has_event": "событие уже создано",
        "no_public_post_url": "нет публичной ссылки на пост",
        "public_username": "публичный username найден",
        "missing_channel_or_message_id": "нет канала или ID сообщения",
        "twiboost_not_configured": "TwiBoost не настроен",
        "missing_service_id": "не указан ID сервиса",
        "provider_error": "ошибка провайдера",
        "quantity_must_be_integer": "количество должно быть числом",
        "quantity_must_be_positive": "количество должно быть больше нуля",
        "quantity_too_large": "количество слишком большое",
        "quantity_invalid": "некорректное количество",
        "quantity_range_reversed": "диапазон указан наоборот",
    }.get(reason or "", f"неизвестная причина: {reason}" if reason else "нет")


def _boost_event_type_label(event_type: str | None) -> str:
    return {
        "text": "текст",
        "photo": "фото",
        "video": "видео",
        "media_group": "альбом",
        "post": "пост",
    }.get(event_type or "", event_type or "пост")


def _boost_smm_state_label(existing: dict | None) -> str:
    if not existing:
        return "не добавлен"
    return "включен" if existing.get("enabled") else "выключен"


def _boost_quantity_display(ch: dict | None) -> str:
    ch = ch or {}
    if ch.get("quantity_display"):
        return str(ch["quantity_display"])
    qmin = ch.get("quantity_min")
    qmax = ch.get("quantity_max")
    if qmin is not None and qmax is not None:
        return str(qmin) if int(qmin) == int(qmax) else f"{qmin}–{qmax}"
    return str(ch.get("quantity") or cfg.BOOST_DEFAULT_QUANTITY)


def _boost_event_time_label(value) -> str:
    if not value:
        return "нет"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M МСК")
    except Exception:
        return str(value)


# ── Вспомогательные функции ────────────────────────────────────────────────

def _is_tester_channel(ch: dict) -> bool:
    """Канал принадлежит тестеру: есть owner_id и это НЕ админ. Каналы без owner_id
    (легаси/«домовые») и каналы, созданные админом, тестерскими не считаются."""
    oid = ch.get("owner_id")
    return oid is not None and not accounts.is_admin(oid)


def _load_channels(include_inactive: bool = False, owner_id: int | None = None,
                   scope: str | None = None) -> list[dict]:
    """Загружает карточки каналов из channels/. Тестер (owner_id, не админ) видит ТОЛЬКО
    свои каналы. Для админа `scope` разделяет общее пространство:
      None      — все каналы (легаси-поведение);
      'mine'    — только свои/«домовые» (без owner_id или owner=админ) — БЕЗ тестерских;
      'testers' — только тестерские (owner_id есть и это не админ).
    Для тестеров `scope` игнорируется (они и так видят лишь свои каналы)."""
    channels_dir = Path(__file__).parent / "channels"
    only_owner = owner_id is not None and not accounts.is_admin(owner_id)
    admin_view = owner_id is not None and accounts.is_admin(owner_id)
    channels = []
    for f in channels_dir.glob("*.json"):
        if f.name.startswith("example_"):
            continue
        try:
            ch = json.loads(f.read_text(encoding="utf-8"))
            if not (include_inactive or ch.get("active", True)):
                continue
            if only_owner and ch.get("owner_id") != owner_id:
                continue
            if scope and admin_view:
                tester = _is_tester_channel(ch)
                if scope == "mine" and tester:
                    continue
                if scope == "testers" and not tester:
                    continue
            channels.append(ch)
        except Exception as e:
            logger.error(f"Ошибка чтения {f.name}: {e}")
    return channels


def _folders(owner_id: int | None) -> list[str]:
    """Список папок (непустые `folder`) среди каналов владельца, отсортирован."""
    seen = set()
    for c in _load_channels(include_inactive=True, owner_id=owner_id, scope="mine"):
        f = (c.get("folder") or "").strip()
        if f:
            seen.add(f)
    return sorted(seen)


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


def _schedule_entry_to_utc_min(value) -> int | None:
    if isinstance(value, int) and 0 <= value <= 23:
        return value * 60
    if isinstance(value, str):
        m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", value.strip())
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2) or 0)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour * 60 + minute
    return None


def _schedule_utc_minutes(entries) -> list[int]:
    minutes = {
        m for m in (_schedule_entry_to_utc_min(v) for v in (entries or []))
        if m is not None
    }
    return sorted(minutes)


def _schedule_entries_from_utc_minutes(minutes) -> list:
    result = []
    for minute_of_day in sorted({int(m) % 1440 for m in minutes}):
        hour, minute = divmod(minute_of_day, 60)
        result.append(hour if minute == 0 else f"{hour:02d}:{minute:02d}")
    return result


def _schedule_msk_minutes(entries) -> list[int]:
    return sorted((m + 180) % 1440 for m in _schedule_utc_minutes(entries))


def _format_schedule_minutes(minutes, sep: str = " · ") -> str:
    return sep.join(f"{m // 60:02d}:{m % 60:02d}" for m in sorted(minutes))


def _parse_schedule_msk_text(text: str) -> list[int]:
    raw = re.split(r"[\s,]+", (text or "").strip())
    tokens = [t for t in raw if t]
    if not tokens:
        raise ValueError
    minutes = []
    for token in tokens:
        m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", token)
        if not m:
            raise ValueError
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
        minutes.append(hour * 60 + minute)
    return sorted(set(minutes))


def _msk_minutes_to_utc_entries(minutes) -> list:
    return _schedule_entries_from_utc_minutes((int(m) - 180) % 1440 for m in minutes)


def _utc_to_msk(hours: list[int]) -> list[int]:
    return sorted((h + 3) % 24 for h in hours if isinstance(h, int))


_SCHEDULE_DAY_LABELS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_SCHEDULE_ALL_DAYS = list(range(7))


def _schedule_days(ch: dict) -> list[int]:
    days = ch.get("schedule_days")
    if not isinstance(days, list):
        return _SCHEDULE_ALL_DAYS[:]
    clean = sorted({int(d) for d in days if isinstance(d, int) and 0 <= d <= 6})
    return clean or _SCHEDULE_ALL_DAYS[:]


def _schedule_days_label(days: list[int]) -> str:
    days = sorted(set(days))
    if days == _SCHEDULE_ALL_DAYS:
        return "каждый день"
    if days == [0, 1, 2, 3, 4]:
        return "будни"
    if days == [5, 6]:
        return "выходные"
    return " · ".join(_SCHEDULE_DAY_LABELS[d] for d in days)


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
        msg = query_or_message.message
        # Фото/медиа-карточка (нет текста для edit_message_text) — удаляем и шлём новым.
        # Иначе «К каналу» с фото-карточки молча падал (нет текста для редактирования).
        if msg is not None and not msg.text:
            try:
                await msg.delete()
            except Exception:
                pass
            await msg.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
            return
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
    channels = _load_channels(owner_id=_acting_uid(qm), scope="mine")
    active = len(channels)
    total_posts = sum(buffer.get_level(ch["channel_id"]) for ch in channels)

    # Иконки уровня буфера (порог здоровья = целевой уровень добора)
    _buf_ok = getattr(cfg, "BUFFER_TARGET", cfg.BUFFER_MIN)
    buf_icon = "✅" if total_posts >= _buf_ok * active else (
        "⚠️" if total_posts > 0 else "🔴"
    )

    text = (
        "🤖 <b>Content Factory</b>\n\n"
        f"📋 Каналов: <b>{active}</b>\n"
        f"📬 Постов в очереди: <b>{total_posts}</b> {buf_icon}"
    )

    rows = [
        [
            InlineKeyboardButton("📋 Мои каналы", callback_data="ui:channels"),
            InlineKeyboardButton("📊 Статус",     callback_data="ui:status"),
        ],
        [InlineKeyboardButton("📝 Очередь постов", callback_data="ui:queue")],
        [InlineKeyboardButton("➕ Добавить канал",  callback_data="add_start")],
        [InlineKeyboardButton("❓ Помощь",          callback_data="ui:help")],
    ]
    # Админ-панель — только для главного владельца (superadmin)
    if accounts.is_superadmin(_acting_uid(qm)):
        rows.append([InlineKeyboardButton("👑 Админ-панель", callback_data="ui:admin")])
    await _answer_or_send(qm, text, InlineKeyboardMarkup(rows))


CHANNELS_PAGE_SIZE = 10


def _toggle_icon(ch: dict) -> str:
    """Индикатор состояния канала в списке:
      🟢 — расписание активно (канал полноценно публикует);
      ⏸ — расписание на паузе, но РСЯ-перекрытие включено (реклама всё равно выходит);
      🔴 — и расписание остановлено, и РСЯ-перекрытие выключено (канал молчит).
    """
    schedule_active = not ch.get("schedule_disabled") and bool(ch.get("post_times_utc"))
    if schedule_active:
        return "🟢"
    if ch.get("rsy_override", False):
        return "⏸"
    return "🔴"


def _channel_button(ch: dict) -> InlineKeyboardButton:
    """Строка-кнопка канала с тумблером состояния."""
    name = ch.get("name") or ch["channel_id"]
    return InlineKeyboardButton(f"{_toggle_icon(ch)} {name}", callback_data=f"ui:ch:{ch['channel_id']}")


async def screen_channels(qm, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    """Список активных каналов: сверху действия, тумблеры состояния, пагинация по 10."""
    channels = [c for c in _load_channels(include_inactive=True, owner_id=_acting_uid(qm), scope="mine")
                if c.get("active", True)]

    # Фильтр по папке (из user_data): None/"__none__"=без папки, "__all__"=все, иначе имя папки.
    # Так каналы, разложенные по папкам, не захламляют главный экран «Мои каналы».
    folder = context.user_data.get("chfolder")
    if folder in (None, "__none__"):
        channels = [c for c in channels if not (c.get("folder") or "").strip()]
    elif folder == "__all__":
        pass
    elif folder:
        channels = [c for c in channels if (c.get("folder") or "").strip() == folder]

    # Верхние кнопки-действия
    top = [
        InlineKeyboardButton("➕ Добавить",   callback_data="add_start"),
        InlineKeyboardButton("🔍 Поиск",      callback_data="ui:ch_search"),
        InlineKeyboardButton("🗑 Удалённые",  callback_data="ui:ch_deleted:0"),
    ]
    folder_row = [InlineKeyboardButton("📁 Папки", callback_data="ui:folders")]
    if folder != "__all__":
        folder_row.append(InlineKeyboardButton("📋 Все каналы", callback_data="ui:chfold:all"))

    folder_title = (
        f" · 📋 все" if folder == "__all__" else
        (f" · 📁 {folder}" if folder and folder != "__none__" else " · 📂 без папки")
    )

    if not channels:
        kb = InlineKeyboardMarkup([top, folder_row, [InlineKeyboardButton("◀️ Меню", callback_data="ui:main")]])
        await _answer_or_send(qm, f"📋 <b>Мои каналы</b>{folder_title}\n\nКаналов нет.", kb)
        return

    total = len(channels)
    pages = (total + CHANNELS_PAGE_SIZE - 1) // CHANNELS_PAGE_SIZE
    page = max(0, min(page, pages - 1))
    chunk = channels[page * CHANNELS_PAGE_SIZE:(page + 1) * CHANNELS_PAGE_SIZE]

    buttons = [top, folder_row]
    for ch in chunk:
        buttons.append([_channel_button(ch)])

    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️", callback_data=f"ui:channels:{page - 1}"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"ui:channels:{page + 1}"))
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("◀️ Меню", callback_data="ui:main")])

    header = f"📋 <b>Мои каналы</b> ({total}){folder_title}"
    if pages > 1:
        header += f" · стр. {page + 1}/{pages}"
    legend = "🟢 публикует · ⏸ пауза (РСЯ вкл) · 🔴 остановлен"
    await _answer_or_send(qm, f"{header}\n\n{legend}", InlineKeyboardMarkup(buttons))


async def screen_channels_deleted(qm, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    """Удалённые (неактивные) каналы — с возможностью восстановить."""
    deleted = [c for c in _load_channels(include_inactive=True, owner_id=_acting_uid(qm), scope="mine")
               if not c.get("active", True)]

    if not deleted:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ К каналам", callback_data="ui:channels")]])
        await _answer_or_send(qm, "🗑 <b>Удалённые каналы</b>\n\nПусто.", kb)
        return

    total = len(deleted)
    pages = (total + CHANNELS_PAGE_SIZE - 1) // CHANNELS_PAGE_SIZE
    page = max(0, min(page, pages - 1))
    chunk = deleted[page * CHANNELS_PAGE_SIZE:(page + 1) * CHANNELS_PAGE_SIZE]

    buttons = []
    for ch in chunk:
        name = ch.get("name") or ch["channel_id"]
        buttons.append([InlineKeyboardButton(f"♻️ Восстановить «{name}»",
                                             callback_data=f"ui:ch_restore:{ch['channel_id']}")])

    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️", callback_data=f"ui:ch_deleted:{page - 1}"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"ui:ch_deleted:{page + 1}"))
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("◀️ К каналам", callback_data="ui:channels")])
    header = f"🗑 <b>Удалённые каналы</b> ({total})"
    if pages > 1:
        header += f" · стр. {page + 1}/{pages}"
    await _answer_or_send(qm, f"{header}\n\nНажми, чтобы восстановить:", InlineKeyboardMarkup(buttons))


async def action_channel_restore(qm, context: ContextTypes.DEFAULT_TYPE, handle: str):
    """Восстанавливает удалённый канал (active=True)."""
    ch = _load_channel(handle)
    if ch:
        ch["active"] = True
        _save_channel(ch)
        from telegram import CallbackQuery
        if isinstance(qm, CallbackQuery):
            await qm.answer(f"♻️ {handle} восстановлен")
        logger.info(f"Канал {handle} восстановлен")
    await screen_channels_deleted(qm, context, 0)


async def prompt_channel_search(qm, context: ContextTypes.DEFAULT_TYPE):
    """Просит прислать запрос для поиска канала."""
    context.user_data["channel_search"] = True
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ К каналам", callback_data="ui:channels")]])
    await _answer_or_send(
        qm,
        "🔍 <b>Поиск канала</b>\n\nПришли часть названия, @username или ссылку t.me — покажу совпадения.",
        kb,
    )


def _channel_search_terms(query: str) -> set[str]:
    q = (query or "").strip().lower()
    q = q.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    terms = {q} if q else set()

    m = re.search(r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(@?[a-z0-9_]{4,})", q)
    if m:
        username = m.group(1).lstrip("@")
        terms.update({username, f"@{username}", f"t.me/{username}", f"https://t.me/{username}"})
    elif q.startswith("@"):
        username = q.lstrip("@")
        terms.update({username, f"@{username}", f"t.me/{username}", f"https://t.me/{username}"})
    elif re.fullmatch(r"[a-z0-9_]{4,}", q):
        terms.update({q, f"@{q}", f"t.me/{q}", f"https://t.me/{q}"})

    return {t for t in terms if t}


def _channel_search_haystack(ch: dict) -> set[str]:
    values = {
        ch.get("channel_id"),
        ch.get("name"),
        ch.get("username"),
        ch.get("chat_username"),
        ch.get("handle"),
    }
    cid = (ch.get("channel_id") or "").strip().lower().lstrip("@")
    if cid:
        values.update({cid, f"@{cid}", f"t.me/{cid}", f"https://t.me/{cid}"})
    out = set()
    for value in values:
        text = (str(value or "")).strip().lower()
        if text:
            out.add(text)
    return out


async def screen_channels_search(qm, context: ContextTypes.DEFAULT_TYPE, query: str):
    """Показывает каналы, совпавшие с запросом (по названию, @username или t.me-ссылке)."""
    terms = _channel_search_terms(query)
    channels = [c for c in _load_channels(include_inactive=True, owner_id=_acting_uid(qm), scope="mine")
                if c.get("active", True)]
    matched = [
        c for c in channels
        if any(term in value for term in terms for value in _channel_search_haystack(c))
    ][:20]

    buttons = [[_channel_button(c)] for c in matched]
    buttons.append([InlineKeyboardButton("🔍 Искать ещё", callback_data="ui:ch_search")])
    buttons.append([InlineKeyboardButton("◀️ К каналам", callback_data="ui:channels")])

    if matched:
        text = f"🔍 Найдено по «{query}»: <b>{len(matched)}</b>"
    else:
        text = f"🔍 По «{query}» ничего не найдено."
    await _answer_or_send(qm, text, InlineKeyboardMarkup(buttons))


async def screen_folders(qm, context: ContextTypes.DEFAULT_TYPE):
    """Обзор папок: все каналы / по папкам / без папки. Выбор → фильтрует список."""
    uid = _acting_uid(qm)
    allch = [c for c in _load_channels(include_inactive=True, owner_id=uid, scope="mine") if c.get("active", True)]
    folders = _folders(uid)
    nofolder = sum(1 for c in allch if not (c.get("folder") or "").strip())

    rows = [[InlineKeyboardButton(f"📋 Все каналы ({len(allch)})", callback_data="ui:chfold:all")]]
    for i, f in enumerate(folders):
        cnt = sum(1 for c in allch if (c.get("folder") or "").strip() == f)
        rows.append([
            InlineKeyboardButton(f"📁 {f} ({cnt})", callback_data=f"ui:chfold:{i}"),
            InlineKeyboardButton("➕", callback_data=f"ui:fold_add:{i}:0"),
        ])
    if nofolder:
        rows.append([InlineKeyboardButton(f"📂 Без папки ({nofolder})", callback_data="ui:chfold:none")])
    rows.append([InlineKeyboardButton("➕ Новая папка", callback_data="ui:fold_new")])
    rows.append([InlineKeyboardButton("◀️ К каналам", callback_data="ui:channels")])

    hint = ("Тапни папку, чтобы открыть список. Кнопка «➕» рядом с папкой добавляет каналы массово."
            if folders else
            "Папок пока нет. Нажми «➕ Новая папка» и выбери каналы для неё.")
    await _answer_or_send(qm, f"📁 <b>Папки</b>\n\n{hint}", InlineKeyboardMarkup(rows))


def _folder_bulk_channels(uid: int | None) -> list[dict]:
    channels = [c for c in _load_channels(include_inactive=True, owner_id=uid, scope="mine") if c.get("active", True)]
    return sorted(channels, key=lambda c: ((c.get("name") or c.get("channel_id") or "").lower()))


async def screen_folder_bulk(qm, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    """Массовый выбор каналов для папки."""
    state = context.user_data.get("folder_bulk") or {}
    folder = (state.get("folder") or "").strip()
    if not folder:
        context.user_data.pop("folder_bulk", None)
        await screen_folders(qm, context)
        return

    uid = _acting_uid(qm)
    channels = _folder_bulk_channels(uid)
    selected = set(state.get("selected") or [])
    page_size = 8
    total = len(channels)
    max_page = max((total - 1) // page_size, 0)
    page = max(0, min(page, max_page))
    start = page * page_size
    chunk = channels[start:start + page_size]

    rows = []
    for idx, ch in enumerate(chunk, start=start):
        cid = ch["channel_id"]
        mark = "✅" if cid in selected else "⬜️"
        cur = (ch.get("folder") or "").strip()
        suffix = f" · {cur[:18]}" if cur and cur != folder else ""
        label = f"{mark} {(ch.get('name') or cid)[:28]}{suffix}"
        rows.append([InlineKeyboardButton(label, callback_data=f"ui:fold_pick:{page}:{idx}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("← Назад", callback_data=f"ui:fold_bulk:{page - 1}"))
    if start + page_size < total:
        nav.append(InlineKeyboardButton("Дальше →", callback_data=f"ui:fold_bulk:{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append([
        InlineKeyboardButton(f"✅ Добавить ({len(selected)})", callback_data="ui:fold_apply"),
        InlineKeyboardButton("Сброс", callback_data=f"ui:fold_reset:{page}"),
    ])
    rows.append([InlineKeyboardButton("◀️ Папки", callback_data="ui:folders")])

    text = (
        f"📁 <b>Добавить каналы в папку</b>\n"
        f"Папка: <b>{folder}</b>\n\n"
        f"Выбрано: <b>{len(selected)}</b>\n"
        f"Страница {page + 1}/{max_page + 1}\n\n"
        "Отмеченные каналы будут перенесены в эту папку."
    )
    await _answer_or_send(qm, text, InlineKeyboardMarkup(rows))


async def screen_set_folder(qm, context: ContextTypes.DEFAULT_TYPE, handle: str):
    """Назначение папки каналу: выбрать существующую / новая / убрать."""
    ch = _load_channel(handle)
    if not ch:
        return
    cur = (ch.get("folder") or "").strip()
    folders = _folders(_acting_uid(qm))
    rows = []
    for i, f in enumerate(folders):
        mark = "✅ " if f == cur else "📁 "
        rows.append([InlineKeyboardButton(f"{mark}{f}", callback_data=f"ui:ch_setfold:{handle}:{i}")])
    rows.append([InlineKeyboardButton("➕ Новая папка", callback_data=f"ui:ch_newfold:{handle}")])
    if cur:
        rows.append([InlineKeyboardButton("🚫 Убрать из папки", callback_data=f"ui:ch_setfold:{handle}:none")])
    rows.append([InlineKeyboardButton("◀️ К настройкам", callback_data=f"ui:ch_settings:{handle}")])
    text = (f"📁 <b>Папка канала</b> <code>{handle}</code>\n\n"
            f"Текущая: <b>{cur or 'нет'}</b>\n\nВыбери папку или создай новую.")
    await _answer_or_send(qm, text, InlineKeyboardMarkup(rows))


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
    msk_minutes = _schedule_msk_minutes(ch.get("post_times_utc", []))
    if paused:
        schedule_str = "⏸ остановлено"
    elif msk_minutes:
        schedule_str = _format_schedule_minutes(msk_minutes) + " МСК"
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
            InlineKeyboardButton("➕ Создать пост", callback_data=f"ui:ch_create:{channel_id}"),
            InlineKeyboardButton("📤 Постнуть",  callback_data=f"ui:ch_postnow:{channel_id}"),
        ],
        [
            InlineKeyboardButton("🔗 Референсы",  callback_data=f"ui:ch_refs:{channel_id}"),
            InlineKeyboardButton("✍️ Черновики",  callback_data=f"ui:ch_draft:{channel_id}"),
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


async def screen_create_post(qm, context: ContextTypes.DEFAULT_TYPE, handle: str):
    """Единый вход в создание поста: вручную, референсы или ИИ."""
    ch = _load_channel(handle)
    if not ch:
        await _answer_or_send(qm, f"❌ Канал {handle} не найден.", None)
        return

    channel_id = ch["channel_id"]
    name = ch.get("name") or channel_id
    refs = ch.get("reference_channels") or []
    drafts = buffer.count_drafts(channel_id)
    ready = buffer.get_level(channel_id)

    text = (
        f"➕ <b>Создать пост</b>\n"
        f"<b>{name}</b>  <code>{channel_id}</code>\n\n"
        f"📬 В очереди: <b>{ready}</b>\n"
        f"✍️ Черновиков: <b>{drafts}</b>\n"
        f"🔗 Доноров: <b>{len(refs)}</b>\n\n"
        f"Выбери, как подготовить новый пост."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✍️ Вручную", callback_data=f"ui:draft_new:{channel_id}")],
        [InlineKeyboardButton("🔗 Из референсов", callback_data=f"ui:ch_refs:{channel_id}")],
        [InlineKeyboardButton("🤖 Генерация ИИ", callback_data=f"ui:ch_generate:{channel_id}")],
        [InlineKeyboardButton("📝 Черновики", callback_data=f"ui:ch_draft:{channel_id}")],
        [InlineKeyboardButton("◀️ К каналу", callback_data=f"ui:ch:{channel_id}")],
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

    msk_minutes = _schedule_msk_minutes(ch.get("post_times_utc", []))
    if ch.get("schedule_disabled"):
        sched_str = "⏸ остановлено"
    elif msk_minutes:
        sched_str = _format_schedule_minutes(msk_minutes, sep=" ") + " МСК"
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
        [InlineKeyboardButton(f"🔄 Тема (подобрать заново): {topic[:25]}", callback_data=f"ui:ch_topic_redo:{channel_id}")],
        [InlineKeyboardButton(f"📁 Папка: {(ch.get('folder') or 'нет')[:25]}", callback_data=f"ui:ch_folder:{channel_id}")],
        [InlineKeyboardButton(f"📅 Расписание: {sched_str}", callback_data=f"ui:ch_schedule:{channel_id}")],
        [InlineKeyboardButton(f"📏 Длина поста: {post_len}",    callback_data=f"ui:ch_set:{channel_id}:post_length")],
        [InlineKeyboardButton(f"🖼 Картинки: {img_str}",        callback_data=f"ui:ch_images_toggle:{channel_id}")],
        [InlineKeyboardButton(f"📰 Источники тем: {src_mode_short}", callback_data=f"ui:ch_set:{channel_id}:rss")],
        [InlineKeyboardButton(f"🚫 Запрещённые темы: {forb_str}", callback_data=f"ui:ch_set:{channel_id}:forbidden")],
        [InlineKeyboardButton(f"{rsy_icon} Перекрытие рекламы РСЯ", callback_data=f"ui:rsy_toggle:{channel_id}")],
    ]

    if is_wb:
        rows.insert(3, [InlineKeyboardButton(f"📦 Категории WB: {wb_str}", callback_data=f"ui:ch_set:{channel_id}:wb_categories")])
    else:
        # Стиль (архетип) — только для контент-каналов. Источник тем — единая кнопка
        # «📰 Источники тем» (там же тумблер Авто/Ленты), отдельного дубля больше нет.
        rows.insert(2, [InlineKeyboardButton(f"🎭 Стиль: {arch_label}", callback_data=f"ui:ch_archetype:{channel_id}")])

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
    utc_minutes = _schedule_utc_minutes(ch.get("post_times_utc", []))
    msk_active = sorted((m + 180) % 1440 for m in utc_minutes)
    days = _schedule_days(ch)
    days_str = _schedule_days_label(days)

    # Популярные часы для быстрого выбора (МСК)
    POPULAR = [7, 9, 11, 13, 15, 17, 19, 21]

    # Строим сетку кнопок: 4 в строке
    time_buttons = []
    row = []
    for h in POPULAR:
        label = f"✅ {h:02d}:00" if h * 60 in msk_active else f"🕐 {h:02d}:00"
        row.append(InlineKeyboardButton(label, callback_data=f"ui:ch_sched_toggle:{channel_id}:{h}"))
        if len(row) == 4:
            time_buttons.append(row)
            row = []
    if row:
        time_buttons.append(row)

    active_str = _format_schedule_minutes(msk_active) or "не задано"

    sched_rows = [
        [InlineKeyboardButton(f"📆 Дни: {days_str}", callback_data=f"ui:ch_sched_days:{channel_id}")],
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
        f"Дни: <b>{days_str}</b>\n"
        f"Время: <b>{active_str}{' МСК' if msk_active else ''}</b>\n"
        f"{hint}\n\n"
        f"✅ — включено · 🕐 — выключено\n"
        f"Нажми на время чтобы включить или убрать:",
        kb,
    )


async def screen_schedule_days(qm, context, handle: str):
    """Выбор дней недели для обычного расписания публикаций (МСК)."""
    ch = _load_channel(handle)
    if not ch:
        await _answer_or_send(qm, f"❌ Канал {handle} не найден.", None)
        return

    channel_id = ch["channel_id"]
    days = set(_schedule_days(ch))
    rows = []
    row = []
    for i, label in enumerate(_SCHEDULE_DAY_LABELS):
        mark = "✅" if i in days else "⬜️"
        row.append(InlineKeyboardButton(f"{mark} {label}", callback_data=f"ui:ch_sched_day:{channel_id}:{i}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.extend([
        [
            InlineKeyboardButton("✅ Каждый день", callback_data=f"ui:ch_sched_daypreset:{channel_id}:all"),
            InlineKeyboardButton("💼 Будни", callback_data=f"ui:ch_sched_daypreset:{channel_id}:weekdays"),
        ],
        [InlineKeyboardButton("🏖 Выходные", callback_data=f"ui:ch_sched_daypreset:{channel_id}:weekend")],
        [InlineKeyboardButton("◀️ К расписанию", callback_data=f"ui:ch_schedule:{channel_id}")],
    ])
    await _answer_or_send(
        qm,
        f"📆 <b>Дни публикаций</b>  <code>{channel_id}</code>\n\n"
        f"Текущее: <b>{_schedule_days_label(sorted(days))}</b>\n"
        f"Часовой пояс: <b>МСК</b>\n\n"
        f"Нажми на день, чтобы включить или убрать.",
        InlineKeyboardMarkup(rows),
    )


async def action_schedule_day_toggle(qm, context, handle: str, day: int):
    ch = _load_channel(handle)
    if not ch or not 0 <= day <= 6:
        return
    days = set(_schedule_days(ch))
    if day in days:
        days.remove(day)
    else:
        days.add(day)
    ch["schedule_days"] = sorted(days) or _SCHEDULE_ALL_DAYS[:]
    _save_channel(ch)
    await screen_schedule_days(qm, context, handle)


async def action_schedule_day_preset(qm, context, handle: str, preset: str):
    ch = _load_channel(handle)
    if not ch:
        return
    if preset == "weekdays":
        ch["schedule_days"] = [0, 1, 2, 3, 4]
    elif preset == "weekend":
        ch["schedule_days"] = [5, 6]
    else:
        ch["schedule_days"] = _SCHEDULE_ALL_DAYS[:]
    _save_channel(ch)
    await screen_schedule_days(qm, context, handle)


async def action_schedule_toggle(qm, context, handle: str, hour_msk: int):
    """Добавляет или убирает час из расписания канала."""
    ch = _load_channel(handle)
    if not ch:
        return

    utc_minutes = set(_schedule_utc_minutes(ch.get("post_times_utc", [])))
    utc_minute = ((hour_msk - 3) % 24) * 60

    if utc_minute in utc_minutes:
        utc_minutes.discard(utc_minute)
    else:
        utc_minutes.add(utc_minute)

    ch["post_times_utc"] = _schedule_entries_from_utc_minutes(utc_minutes)
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
    channels = _load_channels(owner_id=_acting_uid(qm), scope="mine")
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
    channels = _load_channels(owner_id=_acting_uid(qm), scope="mine")
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
    if channels:
        buttons.append([InlineKeyboardButton(
            "📤 Опубликовать по 1 посту во всех каналах", callback_data="ui:queue_publish_all"
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
    """Запрашивает подтверждение очистки буфера каналов текущего пользователя."""
    from database import db
    channel_ids = [ch["channel_id"] for ch in _load_channels(owner_id=_acting_uid(qm), scope="mine")]
    if channel_ids:
        placeholders = ",".join("?" for _ in channel_ids)
        with db.connect() as conn:
            count = conn.execute(
                f"SELECT COUNT(*) FROM posts WHERE channel_id IN ({placeholders}) AND status IN ('ready','pending_review')",
                channel_ids,
            ).fetchone()[0]
    else:
        count = 0

    if count == 0:
        await _answer_or_send(
            qm,
            "📭 Буфер ваших каналов уже пуст.",
            InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="ui:queue")]]),
        )
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Да, очистить всё ({count})", callback_data="ui:queue_clear_all_ok")],
        [InlineKeyboardButton("◀️ Отмена", callback_data="ui:queue")],
    ])
    await _answer_or_send(
        qm,
        f"🧹 <b>Очистить буфер ваших каналов?</b>\n\n"
        f"Будет удалено <b>{count} постов</b> со статусом «готов» и «на проверке» "
        f"по вашим каналам разом.\nОпубликованные посты не затрагиваются.",
        kb,
    )


async def action_clear_all_ok(qm, context: ContextTypes.DEFAULT_TYPE):
    """Очищает буфер каналов текущего пользователя: ready/pending_review -> skipped."""
    from database import db
    channel_ids = [ch["channel_id"] for ch in _load_channels(owner_id=_acting_uid(qm), scope="mine")]
    if channel_ids:
        placeholders = ",".join("?" for _ in channel_ids)
        with db.connect() as conn:
            count = conn.execute(
                f"UPDATE posts SET status='skipped' WHERE channel_id IN ({placeholders}) AND status IN ('ready','pending_review')",
                channel_ids,
            ).rowcount
            conn.commit()
    else:
        count = 0

    logger.info(f"Буфер очищен по каналам пользователя, удалено {count} постов")
    await _answer_or_send(
        qm,
        f"🧹 Буфер очищен по вашим каналам — удалено <b>{count} постов</b>.",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ Назад", callback_data="ui:main")],
        ]),
    )


# ── Действия с каналом ────────────────────────────────────────────────────

async def action_queue_publish_all_confirm(qm, context: ContextTypes.DEFAULT_TYPE):
    """Запрашивает подтверждение срочной публикации по одному посту во все каналы."""
    channels = _load_channels(owner_id=_acting_uid(qm), scope="mine")
    if not channels:
        await _answer_or_send(
            qm,
            "📭 Активных каналов нет.",
            InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="ui:queue")]]),
        )
        return

    ready_now = sum(1 for ch in channels if buffer.get_ready_count(ch["channel_id"]) > 0)
    empty_now = len(channels) - ready_now
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Опубликовать по 1 посту ({len(channels)})", callback_data="ui:queue_publish_all_ok")],
        [InlineKeyboardButton("◀️ Отмена", callback_data="ui:queue")],
    ])
    await _answer_or_send(
        qm,
        "📤 <b>Срочная публикация во все каналы</b>\n\n"
        f"Каналов: <b>{len(channels)}</b>\n"
        f"С готовым постом в очереди: <b>{ready_now}</b>\n"
        f"Без готового поста прямо сейчас: <b>{empty_now}</b>\n\n"
        "После подтверждения бот опубликует по одному <b>готовому</b> посту в каждом канале. "
        "Если готового поста нет, сначала попробует взять один пост из референсов, затем сгенерировать один пост, "
        "и только после этого опубликовать.",
        kb,
    )


async def _ensure_one_ready_post_for_channel(channel: dict) -> tuple[str | None, str | None]:
    """Возвращает источник готового поста или причину, почему готового поста нет."""
    cid = channel["channel_id"]
    if buffer.get_ready_count(cid) > 0:
        return "буфер", None

    if channel.get("reference_channels"):
        try:
            from reference_importer import import_for_channel
            res = await import_for_channel(channel, count=1)
            if res.get("added", 0) > 0 and buffer.get_ready_count(cid) > 0:
                return "референс", None
        except Exception as e:
            logger.warning(f"Срочная публикация [{cid}]: референс не дал готовый пост: {e}")

    try:
        res = await generator.run_for_channel(channel, target_count=1, force=True)
        if res.get("generated", 0) > 0 and buffer.get_ready_count(cid) > 0:
            return "генерация", None
        reason = res.get("reason") or "не удалось создать готовый пост"
        return None, str(reason)
    except Exception as e:
        return None, str(e)


async def action_queue_publish_all_run(qm, context: ContextTypes.DEFAULT_TYPE):
    """Публикует по одному посту во все owner-scoped каналы: ready -> reference -> generation."""
    from telegram import CallbackQuery

    if context.user_data.get("queue_publish_all_running"):
        if isinstance(qm, CallbackQuery):
            await qm.answer("Публикация уже идет.", show_alert=True)
        return

    channels = _load_channels(owner_id=_acting_uid(qm), scope="mine")
    if not channels:
        await _answer_or_send(
            qm,
            "📭 Активных каналов нет.",
            InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="ui:queue")]]),
        )
        return

    context.user_data["queue_publish_all_running"] = True
    if isinstance(qm, CallbackQuery):
        await qm.answer("Запускаю срочную публикацию...")
        try:
            await qm.edit_message_text(
                f"⏳ <b>Публикую по одному посту во все каналы...</b>\n\nКаналов: <b>{len(channels)}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏳ Идет публикация...", callback_data="ui:noop")]]),
            )
        except Exception:
            pass

    ok_lines: list[str] = []
    fail_lines: list[str] = []
    source_counts = {"буфер": 0, "референс": 0, "генерация": 0}

    try:
        for ch in channels:
            cid = ch["channel_id"]
            source, reason = await _ensure_one_ready_post_for_channel(ch)
            if not source:
                fail_lines.append(f"❌ {html.escape(cid)} — {html.escape(reason or 'нет готового поста')}")
                continue

            result = await poster.post_now(cid)
            if result.get("success"):
                source_counts[source] = source_counts.get(source, 0) + 1
                ok_lines.append(f"✅ {html.escape(cid)} — {source}")
            else:
                fail_lines.append(f"❌ {html.escape(cid)} — {html.escape(result.get('error') or 'ошибка публикации')}")
    finally:
        context.user_data.pop("queue_publish_all_running", None)

    def _clip(lines: list[str], limit: int = 25) -> list[str]:
        if len(lines) <= limit:
            return lines
        return lines[:limit] + [f"... еще {len(lines) - limit}"]

    parts = [
        "📤 <b>Срочная публикация завершена</b>",
        "",
        f"Опубликовано: <b>{len(ok_lines)}</b>",
        f"Ошибок: <b>{len(fail_lines)}</b>",
        f"Источники: буфер {source_counts.get('буфер', 0)}, референсы {source_counts.get('референс', 0)}, генерация {source_counts.get('генерация', 0)}",
    ]
    if ok_lines:
        parts.extend(["", "<b>Опубликовано:</b>", *_clip(ok_lines)])
    if fail_lines:
        parts.extend(["", "<b>Не получилось:</b>", *_clip(fail_lines)])

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Очередь постов", callback_data="ui:queue")],
        [InlineKeyboardButton("◀️ В меню", callback_data="ui:main")],
    ])
    final_text = "\n".join(parts)
    if isinstance(qm, CallbackQuery):
        try:
            await qm.edit_message_text(final_text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            if getattr(qm, "message", None):
                await context.bot.send_message(
                    chat_id=qm.message.chat_id,
                    text=final_text,
                    reply_markup=kb,
                    parse_mode=ParseMode.HTML,
                )
    else:
        await _answer_or_send(qm, final_text, kb)


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
        ],
        [
            InlineKeyboardButton("5️⃣  5 постов", callback_data=f"ui:ch_gen_run:{handle}:5"),
            InlineKeyboardButton("🔟 10 постов", callback_data=f"ui:ch_gen_run:{handle}:10"),
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
        if generated == 0:
            # Объясняем «0», а не показываем сухой ноль без причины
            reason = result.get("reason") or (
                "все темы уже использованы, отсеяны фильтром релевантности/цензуры "
                "или тема канала запретная — поменяй тему/источники"
            )
            text = (
                f"⚠️ <b>Посты не созданы</b>\n\n"
                f"Канал: <b>{handle}</b>\n"
                f"Запрошено: {count} · Создано: <b>0</b>\n"
                f"Причина: {reason}."
            )
        else:
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
    rows.append([InlineKeyboardButton("◀️ К каналу", callback_data=f"ui:ch:{handle}")])

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
        "Это <b>всего</b> — поровну между всеми донорами (по кругу).\n"
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


# ── Черновик: ручные посты админа (текст/фото/видео), не в очереди ──────────

_NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
DRAFT_BATCH_LIMIT = 20
DRAFT_BATCH_WINDOW_SEC = 8
DRAFT_BATCH_SUMMARY_DELAY_SEC = 2.5
_MANUAL_DEDUP_STATUSES = ("draft", "ready", "pending_review", "awaiting_media", "published")
_DRAFT_MARKETPLACE_FORMATS = {"manual", "reference", "marketplace", "wb_product"}


def _draft_type_label(d: dict) -> str:
    """Тип черновика для списка: '📸 Фото + текст', '🎬 Видео', '📝 Текст' и т.п."""
    mt = d.get("media_type")
    has_text = bool((d.get("content") or "").strip())
    base = {
        "photo": "📸 Фото", "video": "🎬 Видео", "animation": "🎞 Гиф",
        "document": "📎 Документ", "album": "🖼 Альбом",
    }.get(mt)
    if not base:
        return "📝 Текст"
    return f"{base} + текст" if has_text else base


def _manual_dedup_text(content: str | None) -> str:
    text = html.unescape(content or "")
    text = re.sub(r"<a\b[^>]*>(.*?)</a>", r"\1", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"https?://\S+", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def _manual_post_duplicate(channel_id: str, content: str | None, media_type: str | None, tg_file_id: str | None) -> dict | None:
    """Ищет уже активный/опубликованный ручной дубль без новых колонок БД."""
    text_key = _manual_dedup_text(content)
    media_key = (tg_file_id or "").strip()
    if not text_key and not media_key:
        return None
    from database import db
    placeholders = ",".join("?" for _ in _MANUAL_DEDUP_STATUSES)
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT id, status, content, media_type, tg_file_id FROM posts "
            f"WHERE channel_id = ? AND status IN ({placeholders})",
            (channel_id, *_MANUAL_DEDUP_STATUSES),
        ).fetchall()
    for row in rows:
        old_media = (row["tg_file_id"] or "").strip()
        if media_key and old_media and media_key == old_media:
            return dict(row)
        if text_key and text_key == _manual_dedup_text(row["content"]):
            if not media_type or not row["media_type"] or media_type == row["media_type"]:
                return dict(row)
    return None


async def _warn_manual_duplicate(msg, duplicate: dict):
    status = duplicate.get("status") or "active"
    labels = {
        "draft": "уже есть в черновиках",
        "ready": "уже стоит в очереди",
        "pending_review": "уже ждёт проверки",
        "awaiting_media": "уже ждёт медиа",
        "published": "уже был опубликован",
    }
    await _reply_and_cleanup_user_msg(msg, f"⚠️ Такой пост {labels.get(status, 'уже есть')}. Повтор не добавляю.")


async def _delete_message_silent(message):
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"message cleanup skipped: {e}")


async def _reply_and_cleanup_user_msg(msg, text: str):
    await msg.reply_text(text)
    await _delete_message_silent(msg)


def _manual_import_rejection_message(validation: dict) -> str:
    reason = validation.get("reason_code") or "invalid_imported_post"
    if reason in {"missing_marketplace_link", "missing_marketplace_product_link"}:
        return "⚠️ Для marketplace-поста нужна активная товарная ссылка."
    if reason in {"import_ad_or_offtopic", "marketplace_offtopic_or_service_ad"}:
        return "⚠️ Похоже на рекламу или оффтоп. Такой пост не добавляю."
    if reason == "navigation_only_import":
        return "⚠️ В посте нет самостоятельного содержимого. Такой пост не добавляю."
    if reason == "blocked_imported_content":
        return "⚠️ Такой контент нельзя добавлять по политике бота."
    return f"⚠️ Пост не прошёл проверку: {reason}"


_DRAFT_LINK_RE = re.compile(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_DRAFT_URL_RE = re.compile(r'https?://[^\s<>"\]\)]+', re.IGNORECASE)
_DRAFT_MARKDOWN_LINK_RE = re.compile(r'\[([^\]]{1,120})\]\((https?://[^)\s]+)\)', re.IGNORECASE)
_DRAFT_LABEL_URL_RE = re.compile(r'\b([^\n()]{1,80}?)\s*\((https?://[^)\s]+)\)', re.IGNORECASE)
_DRAFT_STANDALONE_LINK_LABEL_RE = re.compile(
    r"^\s*(?:🔗\s*)?(?:ссылка(?:\s+на\s+товар)?|смотреть(?:\s+на\s+(?:wildberries|wb|ozon|aliexpress))?)\s*$",
    re.IGNORECASE,
)
_DRAFT_TRAILING_LINK_PHRASE_RE = re.compile(
    r"\s+(?:по|по этой|по товарной)\s+ссылке(?:\s+ниже)?\s*$",
    re.IGNORECASE,
)


def _draft_clean_url(url: str) -> str:
    return (url or "").strip().rstrip(".,;:!?)»”'")


def _draft_link_label(url: str, label: str | None = None) -> str:
    label = re.sub(r"<[^>]+>", "", label or "").strip()
    low = (url or "").lower()
    if label and not _DRAFT_STANDALONE_LINK_LABEL_RE.fullmatch(label):
        return label
    if "wildberries." in low:
        return "Смотреть на Wildberries"
    if "ozon." in low:
        return "Смотреть на Ozon"
    if "aliexpress." in low or "aliexpress.ru" in low:
        return "Смотреть на Aliexpress"
    return label or "Смотреть товар"


def _draft_html_links(text_html: str | None) -> list[tuple[str, str]]:
    out, seen = [], set()
    for url, label in _DRAFT_LINK_RE.findall(text_html or ""):
        url = _draft_clean_url(url)
        if not url.lower().startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        out.append((url, _draft_link_label(url, label)))
    return out


def _draft_links(text_html: str | None) -> list[tuple[str, str]]:
    out, seen = [], set()
    for url, label in _draft_html_links(text_html):
        seen.add(url)
        out.append((url, label))
    for url in _DRAFT_URL_RE.findall(text_html or ""):
        url = _draft_clean_url(url)
        if url and url not in seen:
            seen.add(url)
            out.append((url, _draft_link_label(url)))
    return out


def _draft_plain_text(text_html: str | None) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text_html or "", flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _strip_draft_generated_links(text: str) -> str:
    text = _DRAFT_LINK_RE.sub("", text or "")
    text = _DRAFT_MARKDOWN_LINK_RE.sub(r"\1", text)
    text = _DRAFT_LABEL_URL_RE.sub(r"\1", text)
    text = _DRAFT_URL_RE.sub("", text)
    lines = []
    for line in text.splitlines():
        clean = line.strip()
        clean = re.sub(r"\s{2,}", " ", clean)
        if _DRAFT_STANDALONE_LINK_LABEL_RE.fullmatch(clean):
            continue
        line = _DRAFT_TRAILING_LINK_PHRASE_RE.sub("", line.rstrip())
        if not line.strip():
            continue
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _merge_draft_polished_text(polished: str, original_html: str | None) -> tuple[str, str | None]:
    """Возвращает publish-ready текст и parse_mode, сохраняя реальные HTML-ссылки черновика."""
    links = _draft_links(original_html)
    body = _strip_draft_generated_links(polished)
    if not links:
        return body, None
    body_html = html.escape(body)
    cta = "\n".join(f'<a href="{url}">{html.escape(label)}</a>' for url, label in links)
    return (body_html + ("\n\n" if body_html else "") + cta).strip(), "HTML"


def _draft_requires_marketplace_link(ch: dict | None, post: dict | None) -> bool:
    if not ch or ch.get("channel_type") != "marketplace":
        return False
    fmt = ((post or {}).get("format") or "manual").strip().lower()
    return fmt in _DRAFT_MARKETPLACE_FORMATS


def _normalize_manual_draft_links_for_channel(ch: dict | None, post: dict) -> bool:
    """Нормализует plain marketplace URL в один HTML-link до валидации/сохранения."""
    if not _draft_requires_marketplace_link(ch, post):
        return False
    original = (post.get("content") or "").strip()
    if not original or _draft_html_links(original) or not _draft_links(original):
        return False
    body = _strip_draft_generated_links(_draft_plain_text(original) or original)
    content, parse_mode = _merge_draft_polished_text(body, original)
    if not content:
        return False
    post["content"] = content
    post["parse_mode"] = parse_mode
    return True


async def _warn_manual_import_rejected(msg, validation: dict):
    await _reply_and_cleanup_user_msg(msg, _manual_import_rejection_message(validation))


def _draft_batch_state(context: ContextTypes.DEFAULT_TYPE, handle: str, reset: bool = False) -> dict:
    state = context.user_data.get("draft_batch") or {}
    if reset or state.get("handle") != handle:
        task = (state.get("summary") or {}).get("task")
        if task and not task.done():
            task.cancel()
        state = {"handle": handle, "ids": [], "pending_albums": [], "overflow_warned": False}
        context.user_data["draft_batch"] = state
    return state


async def _disable_tracked_draft_cards(context: ContextTypes.DEFAULT_TYPE, handle: str):
    tracked = context.user_data.get("draft_card_messages") or {}
    items = tracked.pop(handle, [])
    context.user_data["draft_card_messages"] = tracked
    for item in items:
        chat_id = item["chat_id"]
        message_id = item["message_id"]
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=None,
            )
        except Exception as e:
            logger.debug(f"draft card cleanup skipped [{handle}]: {e}")
        try:
            await context.bot.edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption="Карточка черновика устарела.",
                reply_markup=None,
            )
        except Exception:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text="Карточка черновика устарела.",
                    reply_markup=None,
                )
            except Exception:
                pass


def _track_draft_card_message(context: ContextTypes.DEFAULT_TYPE | None, handle: str, sent):
    if context is None or not sent:
        return
    tracked = context.user_data.setdefault("draft_card_messages", {})
    items = tracked.setdefault(handle, [])
    items.append({"chat_id": sent.chat_id, "message_id": sent.message_id})
    del items[:-30]


def _draft_batch_count(context: ContextTypes.DEFAULT_TYPE, handle: str) -> int:
    state = _draft_batch_state(context, handle)
    return len(state.get("ids") or [])


def _draft_batch_used(context: ContextTypes.DEFAULT_TYPE, handle: str) -> int:
    state = _draft_batch_state(context, handle)
    return len(state.get("ids") or []) + len(state.get("pending_albums") or [])


def _draft_batch_burst(context: ContextTypes.DEFAULT_TYPE, handle: str) -> dict:
    state = _draft_batch_state(context, handle)
    burst = state.setdefault("burst", {"count": 0, "last_at": 0.0, "overflow_warned": False})
    return burst


def _draft_batch_burst_count(context: ContextTypes.DEFAULT_TYPE, handle: str) -> int:
    return int((_draft_batch_burst(context, handle).get("count") or 0))


def _draft_batch_summary_state(context: ContextTypes.DEFAULT_TYPE, handle: str) -> dict:
    state = _draft_batch_state(context, handle)
    summary = state.setdefault("summary", {
        "ids": [], "accepted": 0, "rejected": 0, "duplicates": 0, "overflow": 0, "last_reason": "", "chat_id": None,
    })
    return summary


def _draft_batch_add(context: ContextTypes.DEFAULT_TYPE, handle: str, post_id: str):
    state = _draft_batch_state(context, handle)
    ids = state.setdefault("ids", [])
    if post_id not in ids:
        ids.append(post_id)


def _draft_batch_remove(context: ContextTypes.DEFAULT_TYPE, post_id: str):
    state = context.user_data.get("draft_batch") or {}
    ids = state.get("ids") or []
    if post_id in ids:
        state["ids"] = [x for x in ids if x != post_id]
        context.user_data["draft_batch"] = state


def _draft_batch_track_album(context: ContextTypes.DEFAULT_TYPE, handle: str, gid: str):
    state = _draft_batch_state(context, handle)
    pending = state.setdefault("pending_albums", [])
    if gid not in pending:
        pending.append(gid)


def _draft_batch_untrack_album(context: ContextTypes.DEFAULT_TYPE, handle: str, gid: str):
    state = _draft_batch_state(context, handle)
    state["pending_albums"] = [x for x in state.get("pending_albums", []) if x != gid]


def _draft_batch_summary_kb(handle: str, batch_count: int) -> InlineKeyboardMarkup:
    rows = []
    if batch_count:
        rows.append([InlineKeyboardButton("👀 Показать превью", callback_data=f"ui:draft_preview_batch:{handle}")])
        rows.append([InlineKeyboardButton(f"📤 Все созданные в очередь ({batch_count})", callback_data=f"ui:draft_qbatch:{handle}")])
    rows.extend([
        [InlineKeyboardButton("✍️ Черновики", callback_data=f"ui:ch_draft:{handle}")],
        [InlineKeyboardButton("◀️ Создать пост", callback_data=f"ui:ch_create:{handle}")],
    ])
    return InlineKeyboardMarkup(rows)


def _draft_batch_summary_text(summary: dict, batch_count: int) -> str:
    lines = ["✅ <b>Пересыл обработан</b>"]
    accepted = int(summary.get("accepted") or 0)
    rejected = int(summary.get("rejected") or 0)
    duplicates = int(summary.get("duplicates") or 0)
    overflow = int(summary.get("overflow") or 0)
    if accepted:
        lines.append(f"Черновиков создано: <b>{accepted}</b>")
    if rejected:
        lines.append(f"Отклонено проверкой: <b>{rejected}</b>")
    if duplicates:
        lines.append(f"Дублей пропущено: <b>{duplicates}</b>")
    if overflow:
        lines.append(f"Лишних сверх лимита {DRAFT_BATCH_LIMIT}: <b>{overflow}</b>")
    if batch_count:
        lines.append(f"\nВ текущей пачке: <b>{batch_count}</b> черновиков.")
    else:
        lines.append("\nНовых черновиков не создано.")
    reason = (summary.get("last_reason") or "").strip()
    if reason and not accepted:
        lines.append(reason)
    return "\n".join(lines)


async def _flush_draft_batch_summary(context: ContextTypes.DEFAULT_TYPE, handle: str):
    try:
        await asyncio.sleep(DRAFT_BATCH_SUMMARY_DELAY_SEC)
    except asyncio.CancelledError:
        return
    state = context.user_data.get("draft_batch") or {}
    if state.get("handle") != handle:
        return
    summary = state.get("summary") or {}
    chat_id = summary.get("chat_id")
    if not chat_id:
        return
    batch_count = _draft_batch_count(context, handle)
    quiet_single = (
        int(summary.get("accepted") or 0) == 1
        and not int(summary.get("rejected") or 0)
        and not int(summary.get("duplicates") or 0)
        and not int(summary.get("overflow") or 0)
        and batch_count == 1
    )
    if quiet_single:
        post_id = (summary.get("ids") or [None])[-1]
        draft = next((d for d in buffer.get_drafts(handle) if d["id"] == post_id), None)
        if draft:
            await _disable_tracked_draft_cards(context, handle)
            cap = f"👀 <b>Превью черновика</b>\n{(draft.get('content') or '<i>(без подписи)</i>').strip()}"
            if len(cap) > 1024:
                cap = cap[:1020] + "…"
            kb = _draft_created_kb(draft, 1)
            sent = None
            mt, fid = draft.get("media_type"), draft.get("tg_file_id")
            try:
                if mt == "album" and fid:
                    data = json.loads(fid or "{}")
                    members, items = data.get("members", []), data.get("items", {})
                    first = items.get(str(members[0])) if members else None
                    if first:
                        if first.get("type") == "video":
                            sent = await context.bot.send_video(chat_id, first["file_id"], caption=cap, parse_mode=ParseMode.HTML, reply_markup=kb)
                        else:
                            sent = await context.bot.send_photo(chat_id, first["file_id"], caption=cap, parse_mode=ParseMode.HTML, reply_markup=kb)
                elif mt == "photo" and fid:
                    sent = await context.bot.send_photo(chat_id, fid, caption=cap, parse_mode=ParseMode.HTML, reply_markup=kb)
                elif mt == "video" and fid:
                    sent = await context.bot.send_video(chat_id, fid, caption=cap, parse_mode=ParseMode.HTML, reply_markup=kb)
                elif mt == "animation" and fid:
                    sent = await context.bot.send_animation(chat_id, fid, caption=cap, parse_mode=ParseMode.HTML, reply_markup=kb)
                elif mt == "document" and fid:
                    sent = await context.bot.send_document(chat_id, fid, caption=cap, parse_mode=ParseMode.HTML, reply_markup=kb)
                if sent is None:
                    sent = await context.bot.send_message(chat_id, cap, parse_mode=ParseMode.HTML, reply_markup=kb)
            except Exception as e:
                logger.debug(f"single draft preview failed [{handle}]: {e}")
                sent = await context.bot.send_message(chat_id, "✅ Черновик создан.", reply_markup=_draft_created_kb(draft, 1))
            _track_draft_card_message(context, handle, sent)
            state["summary"] = {
                "ids": [], "accepted": 0, "rejected": 0, "duplicates": 0, "overflow": 0, "last_reason": "", "chat_id": chat_id,
            }
            context.user_data["draft_batch"] = state
            return
    text = _draft_batch_summary_text(summary, batch_count)
    sent = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=_draft_batch_summary_kb(handle, batch_count),
    )
    _track_draft_card_message(context, handle, sent)
    state["summary"] = {
        "ids": [], "accepted": 0, "rejected": 0, "duplicates": 0, "overflow": 0, "last_reason": "", "chat_id": chat_id,
    }
    context.user_data["draft_batch"] = state


def _draft_batch_note(
    context: ContextTypes.DEFAULT_TYPE,
    handle: str,
    msg,
    *,
    post_id: str | None = None,
    rejected: bool = False,
    duplicate: bool = False,
    overflow: bool = False,
    reason: str = "",
):
    state = _draft_batch_state(context, handle)
    summary = _draft_batch_summary_state(context, handle)
    summary["chat_id"] = msg.chat_id
    if post_id:
        ids = summary.setdefault("ids", [])
        if post_id not in ids:
            ids.append(post_id)
        summary["accepted"] = int(summary.get("accepted") or 0) + 1
    if rejected:
        summary["rejected"] = int(summary.get("rejected") or 0) + 1
    if duplicate:
        summary["duplicates"] = int(summary.get("duplicates") or 0) + 1
    if overflow:
        summary["overflow"] = int(summary.get("overflow") or 0) + 1
    if reason:
        summary["last_reason"] = reason
    task = summary.get("task")
    if task and not task.done():
        task.cancel()
    summary["task"] = asyncio.create_task(_flush_draft_batch_summary(context, handle))
    state["summary"] = summary
    context.user_data["draft_batch"] = state


async def _draft_batch_limit_reached(msg, context: ContextTypes.DEFAULT_TYPE, handle: str) -> bool:
    state = _draft_batch_state(context, handle)
    now = time.monotonic()
    burst = _draft_batch_burst(context, handle)
    last_at = float(burst.get("last_at") or 0.0)
    if not last_at or now - last_at > DRAFT_BATCH_WINDOW_SEC:
        burst = {"count": 0, "last_at": now, "overflow_warned": False}
        state["burst"] = burst
    else:
        burst["last_at"] = now

    if int(burst.get("count") or 0) < DRAFT_BATCH_LIMIT:
        burst["count"] = int(burst.get("count") or 0) + 1
        context.user_data["draft_batch"] = state
        return False
    if not burst.get("overflow_warned"):
        burst["overflow_warned"] = True
        context.user_data["draft_batch"] = state
        _draft_batch_note(
            context, handle, msg,
            overflow=True,
            reason=f"⚠️ За один раз можно переслать не более {DRAFT_BATCH_LIMIT} постов. Лишние не добавляю.",
        )
        await _delete_message_silent(msg)
    else:
        _draft_batch_note(context, handle, msg, overflow=True)
        await _delete_message_silent(msg)
    return True


def _draft_created_kb(d: dict, batch_count: int = 0) -> InlineKeyboardMarkup:
    handle = d["channel_id"]
    rows = [
        [InlineKeyboardButton("📤 В очередь", callback_data=f"ui:draft_q:{d['id']}")],
        [InlineKeyboardButton("🤖 Улучшить текст", callback_data=f"ui:draft_ai:{d['id']}")],
    ]
    if batch_count > 1:
        rows.append([
            InlineKeyboardButton(
                f"📤 Все созданные в очередь ({batch_count})",
                callback_data=f"ui:draft_qbatch:{handle}",
            )
        ])
    rows.extend([
        [InlineKeyboardButton("✍️ В черновик", callback_data=f"ui:ch_draft:{handle}")],
        [InlineKeyboardButton("◀️ Назад", callback_data=f"ui:ch_create:{handle}")],
    ])
    return InlineKeyboardMarkup(rows)


def _draft_card_kb(d: dict) -> InlineKeyboardMarkup:
    handle = d["channel_id"]
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Текст", callback_data=f"ui:draft_edit:{d['id']}"),
            InlineKeyboardButton("🖼 Медиа", callback_data=f"ui:draft_media:{d['id']}"),
        ],
        [InlineKeyboardButton("🤖 Улучшить текст", callback_data=f"ui:draft_ai:{d['id']}")],
        [
            InlineKeyboardButton("📤 В очередь", callback_data=f"ui:draft_q:{d['id']}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"ui:draft_del:{d['id']}"),
        ],
        [InlineKeyboardButton("◀️ К черновикам", callback_data=f"ui:ch_draft:{handle}")],
    ])


def _draft_card_caption(d: dict, num: str) -> str:
    preview = (d.get("content") or "").strip()
    preview = preview if preview else "<i>(без подписи)</i>"
    cap = f"{num} <b>{_draft_type_label(d)}</b>\n{preview}"
    if len(cap) > 1024:
        cap = cap[:1020] + "…"
    return cap


async def _edit_draft_card_message(qm, d: dict, num: str) -> bool:
    cap = _draft_card_caption(d, num)
    kb = _draft_card_kb(d)
    try:
        msg = qm.message
        if getattr(msg, "photo", None) or getattr(msg, "video", None) or getattr(msg, "animation", None) or getattr(msg, "document", None):
            await qm.edit_message_caption(caption=cap, parse_mode=ParseMode.HTML, reply_markup=kb)
        else:
            await qm.edit_message_text(cap, parse_mode=ParseMode.HTML, reply_markup=kb)
        return True
    except Exception as e:
        logger.debug(f"draft card edit failed: {e}")
        return False


async def _reply_draft_card(
    msg_obj,
    d: dict,
    num: str,
    created: bool = False,
    batch_count: int = 0,
    context: ContextTypes.DEFAULT_TYPE | None = None,
):
    """Отправляет одну карточку черновика: реальное медиа (по file_id) + кнопки."""
    cap = _draft_card_caption(d, num)
    kb = _draft_created_kb(d, batch_count) if created else _draft_card_kb(d)
    mt, fid = d.get("media_type"), d.get("tg_file_id")
    sent = None
    try:
        if mt == "album" and fid:
            data = json.loads(fid or "{}")
            members, items = data.get("members", []), data.get("items", {})
            first = items.get(str(members[0])) if members else None
            if first:
                send = msg_obj.reply_video if first.get("type") == "video" else msg_obj.reply_photo
                sent = await send(first["file_id"], caption=cap, parse_mode=ParseMode.HTML, reply_markup=kb)
                _track_draft_card_message(context, d["channel_id"], sent)
                return
        elif fid and mt == "video":
            sent = await msg_obj.reply_video(fid, caption=cap, parse_mode=ParseMode.HTML, reply_markup=kb)
            _track_draft_card_message(context, d["channel_id"], sent); return
        elif fid and mt == "animation":
            sent = await msg_obj.reply_animation(fid, caption=cap, parse_mode=ParseMode.HTML, reply_markup=kb)
            _track_draft_card_message(context, d["channel_id"], sent); return
        elif fid and mt == "document":
            sent = await msg_obj.reply_document(fid, caption=cap, parse_mode=ParseMode.HTML, reply_markup=kb)
            _track_draft_card_message(context, d["channel_id"], sent); return
        elif fid:
            sent = await msg_obj.reply_photo(fid, caption=cap, parse_mode=ParseMode.HTML, reply_markup=kb)
            _track_draft_card_message(context, d["channel_id"], sent); return
    except Exception as e:
        logger.debug(f"draft card media fail: {e}")
    # текст или фолбэк
    sent = await msg_obj.reply_text(cap, parse_mode=ParseMode.HTML, reply_markup=kb)
    _track_draft_card_message(context, d["channel_id"], sent)


async def screen_drafts(qm, context: ContextTypes.DEFAULT_TYPE, handle: str):
    """Черновики канала: каждая — карточка с медиа + кнопки, снизу массовые действия."""
    from telegram import CallbackQuery
    ch = _load_channel(handle)
    if not ch:
        await _answer_or_send(qm, f"❌ Канал {handle} не найден.", None)
        return
    drafts = buffer.get_drafts(handle)
    name = ch.get("name", handle)

    if not drafts:
        if isinstance(qm, CallbackQuery):
            await _disable_tracked_draft_cards(context, handle)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Создать пост", callback_data=f"ui:ch_create:{handle}")],
            [InlineKeyboardButton("↩️ Назад к каналу", callback_data=f"ui:ch:{handle}")],
        ])
        await _answer_or_send(qm, f"🔥 <b>Черновики</b> — {name}\n\nПусто. «➕ Создать пост» → пришли текст, фото или видео.", kb)
        return

    msg_obj = qm.message if isinstance(qm, CallbackQuery) else qm
    if isinstance(qm, CallbackQuery):
        await qm.answer()
        await _disable_tracked_draft_cards(context, handle)
        try:
            await qm.edit_message_text(
                f"🔥 <b>Черновики</b> — {name}\n📌 Сейчас в черновиках: <b>{len(drafts)}</b>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            await msg_obj.reply_text(f"🔥 <b>Черновики</b> — {name}\n📌 Сейчас в черновиках: <b>{len(drafts)}</b>", parse_mode=ParseMode.HTML)

    # Карточки черновиков
    for i, d in enumerate(drafts):
        num = _NUM_EMOJI[i] if i < len(_NUM_EMOJI) else f"{i+1}."
        await _reply_draft_card(msg_obj, d, num, context=context)

    # Массовые действия снизу
    foot = []
    foot.append([InlineKeyboardButton(f"📤 Все в очередь ({len(drafts)})", callback_data=f"ui:draft_qall:{handle}")])
    foot.append([InlineKeyboardButton("➕ Создать пост", callback_data=f"ui:ch_create:{handle}")])
    foot.append([InlineKeyboardButton("🗑 Очистить все черновики", callback_data=f"ui:draft_clear:{handle}")])
    foot.append([InlineKeyboardButton("↩️ Назад к каналу", callback_data=f"ui:ch:{handle}")])
    sent = await msg_obj.reply_text("⬇️ Действия с черновиками:", reply_markup=InlineKeyboardMarkup(foot))
    _track_draft_card_message(context, handle, sent)


async def action_draft_edit_text(qm, context: ContextTypes.DEFAULT_TYPE, post_id: str):
    """Включает режим ввода нового текста/подписи для черновика."""
    handle = buffer.get_post_channel(post_id)
    context.user_data["draft_edit"] = post_id
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ К черновикам", callback_data=f"ui:ch_draft:{handle}")]])
    await _answer_or_send(qm, "✏️ Пришли <b>новый текст</b> (подпись) для этого черновика.", kb)


async def action_draft_edit_media(qm, context: ContextTypes.DEFAULT_TYPE, post_id: str):
    """Включает режим замены медиа черновика."""
    handle = buffer.get_post_channel(post_id)
    context.user_data["draft_media"] = post_id
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ К черновикам", callback_data=f"ui:ch_draft:{handle}")]])
    await _answer_or_send(qm, "🖼 Пришли <b>новое фото или видео</b> — заменю медиа черновика.", kb)


async def action_draft_ai_polish(qm, context: ContextTypes.DEFAULT_TYPE, post_id: str):
    """Бережно улучшает текст черновика через LLM, но не ставит его в очередь."""
    from telegram import CallbackQuery

    handle = buffer.get_post_channel(post_id)
    ch = _load_channel(handle) if handle else None
    draft = next((x for x in buffer.get_drafts(handle) if x["id"] == post_id), None) if handle else None
    if not ch or not draft:
        if isinstance(qm, CallbackQuery):
            await qm.answer("Черновик не найден.", show_alert=True)
        return

    original = (draft.get("content") or "").strip()
    plain = _draft_plain_text(original)
    links = _draft_links(original)
    html_links = _draft_html_links(original)
    plain_urls = [u for u in _DRAFT_URL_RE.findall(original or "") if _draft_clean_url(u)]
    llm_called = False
    logger.debug(
        "draft_ai_polish start | draft_id={} owner_id={} channel_id={} channel_type={} "
        "has_text={} has_media={} has_html_link={} has_plain_url={} extracted_links_count={}",
        post_id[:8],
        ch.get("owner_id"),
        handle,
        ch.get("channel_type"),
        bool(plain),
        bool(draft.get("media_type") or draft.get("tg_file_id")),
        bool(html_links),
        bool(plain_urls),
        len(links),
    )
    if not plain:
        if isinstance(qm, CallbackQuery):
            await qm.answer("В черновике нет текста для улучшения.", show_alert=True)
        return

    from content_safety import (
        build_content_brief,
        evaluate_topic_candidate,
        validate_generated_post,
        validate_imported_post,
    )

    validation_content = original
    if links and not html_links:
        validation_content, _ = _merge_draft_polished_text(_strip_draft_generated_links(plain), original)

    import_validation = validate_imported_post(ch, {
        "channel_id": handle,
        "content": validation_content,
        "format": draft.get("format") or "manual",
        "topic": draft.get("topic") or "manual draft",
        "media_type": draft.get("media_type"),
        "tg_file_id": draft.get("tg_file_id"),
    })
    if not import_validation.get("allowed"):
        logger.warning(
            "draft_ai_polish rejected before LLM | draft_id={} channel_id={} reason={} llm_called=False",
            post_id[:8], handle, import_validation.get("reason_code"),
        )
        if isinstance(qm, CallbackQuery):
            await qm.answer(_manual_import_rejection_message(import_validation), show_alert=True)
        return

    plain_for_llm = _strip_draft_generated_links(plain) or plain
    safety = evaluate_topic_candidate(ch, {"topic": plain_for_llm[:700], "source": "manual_draft_ai_polish"})
    if safety.get("decision") in ("blocked", "review") or not safety.get("safe_topic"):
        logger.warning(
            "draft_ai_polish topic rejected | draft_id={} channel_id={} reason={} llm_called=False",
            post_id[:8], handle, safety.get("reason_code"),
        )
        if isinstance(qm, CallbackQuery):
            await qm.answer("⚠️ Такой текст ИИ не будет переписывать по политике бота.", show_alert=True)
        return
    brief = build_content_brief(ch, safety, draft.get("format") or "manual")

    if isinstance(qm, CallbackQuery):
        await qm.answer("🤖 Улучшаю текст...")
        try:
            await qm.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏳ ИИ улучшает текст...", callback_data="ui:noop")
            ]]))
        except Exception:
            pass

    async def _send_result(text: str, kb: InlineKeyboardMarkup):
        if isinstance(qm, CallbackQuery):
            await qm.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        else:
            await _answer_or_send(qm, text, kb)

    try:
        from ai_client import rephrase_text
        llm_called = True
        polished_raw = await rephrase_text(plain_for_llm, ch)
        polished_raw = (polished_raw or "").strip()
    except Exception as e:
        logger.warning(f"draft_ai_polish failed {post_id[:8]}: {e}")
        polished_raw = ""

    if not polished_raw or polished_raw == plain_for_llm:
        logger.warning(
            "draft_ai_polish empty_or_same | draft_id={} channel_id={} llm_called={}",
            post_id[:8], handle, llm_called,
        )
        await _send_result(
            "😔 ИИ не смог улучшить текст. Черновик не изменён.",
            InlineKeyboardMarkup([[InlineKeyboardButton("◀️ К черновикам", callback_data=f"ui:ch_draft:{handle}")]]),
        )
        return

    content, parse_mode = _merge_draft_polished_text(polished_raw, original)
    validation = validate_generated_post(
        ch,
        {
            "channel_id": handle,
            "content": content,
            "format": draft.get("format") or "manual",
            "topic": safety.get("safe_topic") or draft.get("topic") or "manual draft",
            "media_type": draft.get("media_type"),
            "tg_file_id": draft.get("tg_file_id"),
        },
        safety,
        brief,
    )
    if not validation.get("allowed"):
        logger.warning(
            "draft_ai_polish validation skipped | draft_id={} channel_id={} validator_result={} failure_reason={} llm_called={}",
            post_id[:8], handle, validation.get("decision"), validation.get("reason_code"), llm_called,
        )
        await _send_result(
            f"⚠️ ИИ-текст не прошёл проверку: <code>{validation.get('reason_code')}</code>\nЧерновик не изменён.",
            InlineKeyboardMarkup([[InlineKeyboardButton("◀️ К черновикам", callback_data=f"ui:ch_draft:{handle}")]]),
        )
        return

    buffer.set_draft_content(post_id, content, parse_mode)
    logger.info(
        "draft_ai_polish updated | draft_id={} channel_id={} validator_result={} links={} llm_called={}",
        post_id[:8], handle, validation.get("reason_code"), len(links), llm_called,
    )
    updated = next((x for x in buffer.get_drafts(handle) if x["id"] == post_id), None)
    if updated and isinstance(qm, CallbackQuery):
        edited = await _edit_draft_card_message(qm, updated, "🤖 Обновлённый черновик")
        if not edited:
            await _reply_draft_card(qm.message, updated, "🤖 Обновлённый черновик", context=context)
    elif updated:
        await _reply_draft_card(qm, updated, "🤖 Обновлённый черновик", context=context)


async def action_draft_clear_confirm(qm, context: ContextTypes.DEFAULT_TYPE, handle: str):
    """Подтверждение очистки всех черновиков."""
    n = buffer.count_drafts(handle)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Да, удалить {n}", callback_data=f"ui:draft_clearok:{handle}")],
        [InlineKeyboardButton("◀️ Отмена", callback_data=f"ui:ch_draft:{handle}")],
    ])
    await _answer_or_send(qm, f"🗑 Удалить <b>все {n}</b> черновиков? Это необратимо.", kb)


async def action_draft_clear_ok(qm, context: ContextTypes.DEFAULT_TYPE, handle: str):
    """Удаляет все черновики канала."""
    n = buffer.delete_all_drafts(handle)
    from telegram import CallbackQuery
    if isinstance(qm, CallbackQuery):
        await qm.answer(f"🗑 Удалено: {n}")
        await _disable_tracked_draft_cards(context, handle)
    await screen_drafts(qm, context, handle)


async def action_draft_new(qm, context: ContextTypes.DEFAULT_TYPE, handle: str):
    """Включает режим приёма следующего сообщения как черновика."""
    if not _load_channel(handle):
        return
    context.user_data["draft_compose"] = handle
    _draft_batch_state(context, handle, reset=True)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data=f"ui:ch_create:{handle}")]])
    await _answer_or_send(
        qm,
        "✍️ <b>Создать пост вручную</b>\n\n"
        "Пришли одним сообщением:\n"
        "• <b>текст</b> — будет текстовый пост\n"
        "• <b>фото</b> (можно с подписью) — фото-пост\n"
        "• <b>видео</b> (можно с подписью) — видео-пост\n"
        f"• можно переслать пачкой до <b>{DRAFT_BATCH_LIMIT}</b> постов\n\n"
        "Я покажу превью. Если ничего не выбрать, пост останется в черновике.",
        kb,
    )


def _extract_msg_media(msg) -> tuple[str | None, str | None]:
    """file_id + тип из сообщения (порядок: animation до document)."""
    if msg.photo:
        return msg.photo[-1].file_id, "photo"
    if getattr(msg, "animation", None):
        return msg.animation.file_id, "animation"
    if msg.video:
        return msg.video.file_id, "video"
    if msg.document:
        return msg.document.file_id, "document"
    return None, None


def _message_html_content(msg) -> tuple[str, str | None]:
    """Текст/подпись с сохранением Telegram text_link/url entities как HTML."""
    raw = (msg.caption or msg.text or "").strip()
    html_text = None
    if msg.caption:
        html_text = getattr(msg, "caption_html", None)
    elif msg.text:
        html_text = getattr(msg, "text_html", None)
    if callable(html_text):
        html_text = html_text()
    html_text = (html_text or "").strip()
    if html_text:
        return html_text, "HTML"
    return raw, None


def _draft_done_kb(handle: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬆️ Сразу в очередь", callback_data=f"ui:draft_qlast:{handle}")],
        [InlineKeyboardButton("➕ Ещё пост",         callback_data=f"ui:draft_new:{handle}")],
        [InlineKeyboardButton("✍️ К черновикам",     callback_data=f"ui:ch_draft:{handle}")],
    ])


async def _flush_album_draft(context, gid: str, reply_msg):
    """Собирает накопленные кадры альбома в ОДИН черновик-альбом после паузы."""
    try:
        await asyncio.sleep(1.2)  # ждём, пока придут все кадры media_group
    except asyncio.CancelledError:
        return
    slots = context.user_data.get("_album_slots", {})
    slot = slots.pop(gid, None)
    if not slot or not slot["items"]:
        if slot:
            _draft_batch_untrack_album(context, slot.get("handle"), gid)
        return
    handle = slot["handle"]
    members = list(range(len(slot["items"])))
    items = {str(i): {"file_id": fid, "type": mt} for i, (fid, mt) in enumerate(slot["items"])}
    post = {
        "channel_id": handle,
        "content": slot["caption"],
        "format": "manual",
        "topic": "manual draft",
        "status": "draft",
        "media_type": "album",
        "parse_mode": slot.get("parse_mode"),
        "tg_file_id": json.dumps({"members": members, "items": items}),
    }
    _draft_batch_untrack_album(context, handle, gid)
    ch = _load_channel(handle)
    _normalize_manual_draft_links_for_channel(ch, post)
    if ch:
        from content_safety import validate_imported_post
        validation = validate_imported_post(ch, post)
        if not validation.get("allowed"):
            _draft_batch_note(
                context,
                handle,
                reply_msg,
                rejected=True,
                reason=_manual_import_rejection_message(validation),
            )
            await _delete_message_silent(reply_msg)
            return
    duplicate = _manual_post_duplicate(handle, post["content"], post["media_type"], post["tg_file_id"])
    if duplicate:
        _draft_batch_note(context, handle, reply_msg, duplicate=True)
        await _delete_message_silent(reply_msg)
        return
    post_id = buffer.add(post)
    post["id"] = post_id
    _draft_batch_add(context, handle, post_id)
    _draft_batch_note(context, handle, reply_msg, post_id=post_id)
    seen = set()
    for album_msg in slot.get("messages", []) or [reply_msg]:
        key = (getattr(album_msg, "chat_id", None), getattr(album_msg, "message_id", None))
        if key in seen:
            continue
        seen.add(key)
        await _delete_message_silent(album_msg)


async def create_draft_from_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Создаёт черновик из присланного/пересланного админом сообщения
    (текст/фото/видео/документ/альбом). Медиа храним по file_id (без скачивания).

    Режим compose «липкий»: после создания черновика остаётся включённым, чтобы
    можно было переслать СРАЗУ НЕСКОЛЬКО постов (каждый → свой черновик). Выход —
    любой кнопкой меню (ui_router сбрасывает draft_compose). Альбом (media_group)
    склеивается в ОДИН черновик-альбом через короткую паузу-дебаунс.
    Возвращает True, если сообщение обработано (поглощено режимом compose).
    """
    handle = context.user_data.get("draft_compose")
    if not handle:
        return False
    msg = update.message
    if not msg:
        return False

    caption, parse_mode = _message_html_content(msg)
    file_id, media_type = _extract_msg_media(msg)

    # --- Альбом: копим кадры, флашим одним черновиком после паузы ---
    gid = getattr(msg, "media_group_id", None)
    if gid and file_id:
        slots = context.user_data.setdefault("_album_slots", {})
        if str(gid) not in slots:
            if await _draft_batch_limit_reached(msg, context, handle):
                return True
            _draft_batch_track_album(context, handle, str(gid))
        slot = slots.setdefault(str(gid), {
            "items": [], "caption": "", "parse_mode": None, "handle": handle, "task": None, "messages": []
        })
        slot["items"].append((file_id, media_type))
        slot.setdefault("messages", []).append(msg)
        if caption and not slot["caption"]:
            slot["caption"] = caption
            slot["parse_mode"] = parse_mode
        if slot.get("task"):
            slot["task"].cancel()
        slot["task"] = asyncio.create_task(_flush_album_draft(context, str(gid), msg))
        return True

    if not file_id and not caption:
        await msg.reply_text("⚠️ Пусто. Пришли текст, фото, видео или перешли пост.")
        return True  # остаёмся в режиме compose

    if await _draft_batch_limit_reached(msg, context, handle):
        return True

    # Режим НЕ сбрасываем (липкий) — можно слать/пересылать ещё посты подряд.
    post = {
        "channel_id": handle,
        "content": caption,
        "format": "manual",
        "topic": "manual draft",
        "status": "draft",
        "parse_mode": parse_mode,
    }
    if file_id:
        post["tg_file_id"] = file_id
        post["media_type"] = media_type
    ch = _load_channel(handle)
    _normalize_manual_draft_links_for_channel(ch, post)
    if ch:
        from content_safety import validate_imported_post
        validation = validate_imported_post(ch, post)
        if not validation.get("allowed"):
            _draft_batch_note(
                context,
                handle,
                msg,
                rejected=True,
                reason=_manual_import_rejection_message(validation),
            )
            await _delete_message_silent(msg)
            return True
    duplicate = _manual_post_duplicate(handle, post["content"], post.get("media_type"), post.get("tg_file_id"))
    if duplicate:
        _draft_batch_note(context, handle, msg, duplicate=True)
        await _delete_message_silent(msg)
        return True
    post_id = buffer.add(post)
    post["id"] = post_id
    _draft_batch_add(context, handle, post_id)
    _draft_batch_note(context, handle, msg, post_id=post_id)
    await _delete_message_silent(msg)
    return True


async def apply_draft_edit_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Применяет правку черновика (текст или медиа), если включён режим. True если обработано."""
    msg = update.message
    if not msg:
        return False

    pid = context.user_data.get("draft_edit")
    if pid:
        text, parse_mode = _message_html_content(msg)
        if not text:
            await msg.reply_text("⚠️ Пришли текст.")
            return True
        context.user_data.pop("draft_edit", None)
        handle = buffer.get_post_channel(pid)
        ch = _load_channel(handle) if handle else None
        post = {"content": text, "format": "manual", "parse_mode": parse_mode}
        _normalize_manual_draft_links_for_channel(ch, post)
        buffer.set_draft_content(pid, post["content"], post.get("parse_mode"))
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ К черновикам", callback_data=f"ui:ch_draft:{handle}")]])
        await msg.reply_text("✅ Текст черновика обновлён.", reply_markup=kb)
        return True

    pid = context.user_data.get("draft_media")
    if pid:
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
            await msg.reply_text("⚠️ Пришли фото или видео.")
            return True
        context.user_data.pop("draft_media", None)
        handle = buffer.get_post_channel(pid)
        buffer.set_draft_media(pid, file_id, media_type)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ К черновикам", callback_data=f"ui:ch_draft:{handle}")]])
        await msg.reply_text("✅ Медиа черновика заменено.", reply_markup=kb)
        return True

    return False


def _validate_draft_for_queue(draft: dict) -> tuple[bool, str]:
    """Не выпускает marketplace manual/reference draft без товарной ссылки."""
    ch = _load_channel(draft.get("channel_id"))
    if not ch or ch.get("channel_type") != "marketplace":
        return True, ""
    normalized = dict(draft)
    normalized_links = _normalize_manual_draft_links_for_channel(ch, normalized)
    from content_safety import validate_generated_post
    validation = validate_generated_post(
        ch,
        {
            "content": normalized.get("content") or "",
            "format": normalized.get("format") or "manual",
            "topic": normalized.get("topic") or "manual draft",
        },
        {"decision": "allowed", "safe_topic": draft.get("topic") or "manual draft"},
        {},
    )
    if validation.get("allowed"):
        if normalized_links:
            buffer.set_draft_content(draft["id"], normalized["content"], normalized.get("parse_mode"))
            draft["content"] = normalized["content"]
            draft["parse_mode"] = normalized.get("parse_mode")
        return True, ""
    reason = validation.get("reason_code") or "invalid_marketplace_post"
    if reason in {"missing_marketplace_link", "missing_marketplace_product_link"}:
        return False, "⚠️ Для marketplace-поста нужна активная товарная ссылка."
    if reason == "marketplace_offtopic_or_service_ad":
        return False, "⚠️ Пост не похож на товарную карточку marketplace."
    return False, f"⚠️ Черновик не прошёл проверку: {reason}"


def _manual_queue_done_kb(handle: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Ещё пост", callback_data=f"ui:draft_new:{handle}")],
        [InlineKeyboardButton("✍️ Черновики", callback_data=f"ui:ch_draft:{handle}")],
        [InlineKeyboardButton("◀️ Создать пост", callback_data=f"ui:ch_create:{handle}")],
    ])


async def _send_manual_queue_done(qm, context: ContextTypes.DEFAULT_TYPE, handle: str, text: str):
    from telegram import CallbackQuery
    if isinstance(qm, CallbackQuery):
        await _delete_message_silent(qm.message)
        await context.bot.send_message(
            chat_id=qm.message.chat_id,
            text=text,
            reply_markup=_manual_queue_done_kb(handle),
        )


async def action_draft_preview_batch(qm, context: ContextTypes.DEFAULT_TYPE, handle: str):
    """Показывает превью черновиков, созданных в текущей ручной пачке, только по кнопке."""
    from telegram import CallbackQuery
    state = context.user_data.get("draft_batch") or {}
    ids = list(state.get("ids") or []) if state.get("handle") == handle else []
    drafts_by_id = {d["id"]: d for d in buffer.get_drafts(handle)}
    drafts = [drafts_by_id[pid] for pid in ids if pid in drafts_by_id]
    if isinstance(qm, CallbackQuery):
        if not drafts:
            await qm.answer("Нет созданных черновиков для показа.", show_alert=True)
            return
        await qm.answer("Показываю превью")
        try:
            await qm.edit_message_text(
                f"👀 <b>Превью созданных черновиков</b>\nПостов: <b>{len(drafts)}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=_draft_batch_summary_kb(handle, len(drafts)),
            )
        except Exception:
            pass
        for i, draft in enumerate(drafts, start=1):
            await _reply_draft_card(
                qm.message, draft, f"👀 Превью {i}/{len(drafts)}",
                created=True, batch_count=len(drafts), context=context,
            )


async def action_draft_queue(qm, context: ContextTypes.DEFAULT_TYPE, post_id: str):
    """Отправляет один черновик в очередь. Правит кнопки этой карточки на месте."""
    from telegram import CallbackQuery
    handle = buffer.get_post_channel(post_id)
    draft = next((x for x in buffer.get_drafts(handle) if x["id"] == post_id), None) if handle else None
    if draft:
        valid, message = _validate_draft_for_queue(draft)
        if not valid:
            if isinstance(qm, CallbackQuery):
                await qm.answer(message, show_alert=True)
            return
    ok = buffer.draft_to_ready(post_id)
    if isinstance(qm, CallbackQuery):
        await qm.answer("⬆️ В очереди" if ok else "Уже не черновик")
        if ok and handle:
            _draft_batch_remove(context, post_id)
            await _send_manual_queue_done(qm, context, handle, "✅ Пост добавлен в очередь.")
        else:
            try:
                await qm.edit_message_reply_markup(
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ В очереди", callback_data="ui:noop")]])
                )
            except Exception:
                pass


async def action_draft_queue_last(qm, context: ContextTypes.DEFAULT_TYPE, handle: str):
    """Отправляет в очередь самый свежий черновик (после создания)."""
    drafts = buffer.get_drafts(handle)
    from telegram import CallbackQuery
    target = None
    state = context.user_data.get("draft_batch") or {}
    if drafts:
        drafts_by_id = {draft["id"]: draft for draft in drafts}
        if state.get("handle") == handle:
            for post_id in reversed(state.get("ids") or []):
                if post_id in drafts_by_id:
                    target = drafts_by_id[post_id]
                    break
        target = target or drafts[-1]
    if target:
        valid, message = _validate_draft_for_queue(target)
        if not valid:
            if isinstance(qm, CallbackQuery):
                await qm.answer(message, show_alert=True)
            await screen_drafts(qm, context, handle)
            return
        buffer.draft_to_ready(target["id"])
        _draft_batch_remove(context, target["id"])
        if isinstance(qm, CallbackQuery):
            await qm.answer("⬆️ В очереди")
    await screen_drafts(qm, context, handle)


async def action_draft_queue_all(qm, context: ContextTypes.DEFAULT_TYPE, handle: str):
    """Отправляет ВСЕ черновики канала в очередь."""
    n = skipped = 0
    for draft in buffer.get_drafts(handle):
        valid, _ = _validate_draft_for_queue(draft)
        if valid and buffer.draft_to_ready(draft["id"]):
            n += 1
        else:
            skipped += 1
    from telegram import CallbackQuery
    if isinstance(qm, CallbackQuery):
        suffix = f", пропущено: {skipped}" if skipped else ""
        await qm.answer(f"⬆️ В очередь: {n}{suffix}")
    await screen_drafts(qm, context, handle)


async def action_draft_queue_batch(qm, context: ContextTypes.DEFAULT_TYPE, handle: str):
    """Отправляет в очередь только черновики, созданные в текущей ручной пачке."""
    state = context.user_data.get("draft_batch") or {}
    ids = list(state.get("ids") or []) if state.get("handle") == handle else []
    drafts = {d["id"]: d for d in buffer.get_drafts(handle)}
    n = skipped = 0
    remaining = []
    for post_id in ids:
        draft = drafts.get(post_id)
        if not draft:
            continue
        valid, _ = _validate_draft_for_queue(draft)
        if valid and buffer.draft_to_ready(post_id):
            n += 1
        else:
            skipped += 1
            remaining.append(post_id)
    if state.get("handle") == handle:
        state["ids"] = remaining
        context.user_data["draft_batch"] = state
    from telegram import CallbackQuery
    if isinstance(qm, CallbackQuery):
        suffix = f", пропущено: {skipped}" if skipped else ""
        await qm.answer(f"⬆️ В очередь: {n}{suffix}")
        await _send_manual_queue_done(qm, context, handle, f"✅ Созданные посты добавлены в очередь: {n}{suffix}.")
        return
    await screen_drafts(qm, context, handle)


async def action_draft_delete(qm, context: ContextTypes.DEFAULT_TYPE, post_id: str):
    """Удаляет черновик. Правит кнопки этой карточки на месте."""
    buffer.delete_draft(post_id)
    from telegram import CallbackQuery
    if isinstance(qm, CallbackQuery):
        await qm.answer("🗑 Удалён")
        try:
            await qm.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Удалён", callback_data="ui:noop")]])
            )
        except Exception:
            pass


async def action_draft_view(qm, context: ContextTypes.DEFAULT_TYPE, post_id: str):
    """Показывает черновик так, как он будет выглядеть (реальным сообщением)."""
    from telegram import CallbackQuery
    if isinstance(qm, CallbackQuery):
        await qm.answer()
    handle = buffer.get_post_channel(post_id)
    d = next((x for x in buffer.get_drafts(handle) if x["id"] == post_id), None) if handle else None
    if not d:
        return
    chat_id = qm.message.chat_id if isinstance(qm, CallbackQuery) else qm.chat_id
    cap = (d.get("content") or "") or None
    parse_mode = d.get("parse_mode")
    fid, mt = d.get("tg_file_id"), d.get("media_type")
    try:
        if mt == "photo":
            await context.bot.send_photo(chat_id, fid, caption=cap, parse_mode=parse_mode)
        elif mt == "video":
            await context.bot.send_video(chat_id, fid, caption=cap, parse_mode=parse_mode)
        elif mt == "animation":
            await context.bot.send_animation(chat_id, fid, caption=cap, parse_mode=parse_mode)
        elif mt == "document":
            await context.bot.send_document(chat_id, fid, caption=cap, parse_mode=parse_mode)
        else:
            await context.bot.send_message(chat_id, cap or "(пустой пост)", parse_mode=parse_mode)
    except Exception as e:
        await context.bot.send_message(chat_id, f"⚠️ Не показать превью: {e}")


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
# ВАЖНО: «topic» НАМЕРЕННО убран из ручного редактирования — тему нельзя вписывать
# руками (это был вектор обхода фильтров). Тема выводится ИИ из анализа канала
# (кнопка «🔄 Подобрать тему заново» → action_rederive_topic).
EDITABLE_FIELDS = {
    "tone":        ("🎨 Тон общения",         "Введи желаемый тон\nНапример: <i>дружелюбный, с юмором, без снобизма</i>", "text"),
    "schedule":    ("📅 Расписание",          "Введи время публикации МСК через пробел\nНапример: <code>08:14 09:20</code> или <code>9 12 16</code>",  "schedule"),
    "posts_count": ("🔢 Постов в день",       "Введи число постов в день (от 1 до 30)\nНапример: <code>10</code>",         "int"),
    "post_length": ("📏 Длина поста",         "Введи диапазон слов\nНапример: <code>100–200 слов</code>",                  "text"),
    "images":      ("🖼 Источник картинок",   None,  "images_menu"),
    "rss":         ("📰 Источники тем",        None, "rss_menu"),
    "forbidden":   ("🚫 Запрещённые темы",    "Глобальные запретки (политика, 18+, война, скам и др.) действуют всегда.\nЗдесь добавь СВОИ доп. темы через запятую.\nНапиши <b>нет</b> чтобы убрать доп. список.", "text_list"),
    "wb_categories":("📦 Категории WB",       "Перечисли категории через запятую\nНапример: <code>кроссовки, наушники</code>\nНапиши <b>все</b> чтобы убрать фильтр.", "text_list_or_clear"),
}


async def action_rederive_topic(qm, context, handle: str):
    """🔄 Подобрать тему заново: читает посты канала юзерботом и выводит тему через ИИ.
    Тему руками не задают — поэтому обойти фильтры через тему-фритекст нельзя."""
    from telegram import CallbackQuery
    ch = _load_channel(handle)
    if not ch:
        return
    back = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ К настройкам", callback_data=f"ui:ch_settings:{handle}")]])

    async def _show(text: str, kb=back):
        """Показ результата без повторного answer (qm уже отвечен)."""
        try:
            await qm.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            try:
                await qm.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            except Exception:
                pass

    if isinstance(qm, CallbackQuery):
        await qm.answer("🔎 Анализирую канал…")
    await _show(f"🔎 <b>Анализирую {handle}…</b>\n\nЧитаю посты и определяю тему. ~10–20 сек.", None)

    try:
        from userbot_reader import read_channel
        from channel_analyzer import analyzer, normalize_meta
        from ai_client import _contains_forbidden
        data = await read_channel(handle, limit=50)
    except Exception as e:
        await _show(f"❌ Не смог прочитать {handle}.\n\nКанал должен быть публичным. ({type(e).__name__})")
        return

    try:
        analysis = await analyzer.analyze_posts(
            data.get("title", handle), data.get("posts", []), about=data.get("about", "")
        )
    except ValueError:
        await _show(
            "📭 <b>В канале пока нет/мало постов</b> для определения темы.\n\n"
            "Тему бот определяет <b>только по реальным постам канала</b> "
            "(название не в счёт — оно может быть любым).\n\n"
            "Выложи в канал хотя бы 3 поста с текстом и нажми «🔄 Подобрать тему заново»."
        )
        return
    except Exception as e:
        await _show(f"❌ Ошибка анализа: {type(e).__name__}")
        return

    # Запрещённая тематика канала (по СМЫСЛУ постов, не по словам) — тему не присваиваем
    if analysis.get("forbidden"):
        reason = (analysis.get("forbidden_reason") or "контент нарушает правила").strip()
        ch.pop("topic", None)
        _save_channel(ch)
        await _show(
            f"🚫 <b>Канал на запрещённую тематику</b>\n\nПричина: {reason}.\n\n"
            "Бот не работает с таким контентом — тема не присвоена, автогенерация недоступна."
        )
        return

    new_topic = (analysis.get("topic") or "").strip()
    if not new_topic:
        await _show("⚠️ Не удалось определить тему по постам канала.")
        return

    ch["topic"] = new_topic
    if ch.get("channel_type") != "marketplace":
        arch, src = normalize_meta(analysis.get("archetype"), analysis.get("topic_source"))
        ch["archetype"] = arch
    _save_channel(ch)
    logger.info(f"Тема канала {handle} переопределена ИИ: {new_topic[:60]}")

    warn = ""
    if _contains_forbidden(new_topic):
        warn = "\n\n⚠️ Похоже, канал на запрещённую тематику — автогенерация будет недоступна."
    await _show(f"✅ <b>Тема обновлена по анализу канала:</b>\n\n📌 {new_topic}{warn}")


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

    field  = editing["field"]
    text   = update.message.text.strip()

    if field == "bulk_folder":
        name = re.sub(r"\s+", " ", text).strip()[:40]
        if not name or name.lower() in ("нет", "убрать", "очистить"):
            await update.message.reply_text("⚠️ Напиши название папки.")
            return True
        context.user_data.pop("editing", None)
        context.user_data["folder_bulk"] = {"folder": name, "selected": []}
        await screen_folder_bulk(update.message, context, 0)
        return True

    handle = editing["handle"]

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
        # Тему руками не задаём — её выводит ИИ из анализа канала (защита от обхода фильтров)
        await update.message.reply_text(
            "✋ Тему нельзя вписывать вручную. Открой настройки канала и нажми "
            "«🔄 Подобрать тему заново» — ИИ определит тему по постам канала."
        )
        return True

    elif field == "tone":
        ch["tone"] = text
        success_msg = f"✅ Тон обновлён: <i>{text}</i>"

    elif field == "schedule":
        try:
            minutes_msk = _parse_schedule_msk_text(text)
            ch["post_times_utc"] = _msk_minutes_to_utc_entries(minutes_msk)
            ch.pop("schedule_disabled", None)
            times_str = _format_schedule_minutes(minutes_msk, sep=" ")
            success_msg = f"✅ Расписание: <b>{times_str} МСК</b>"
        except Exception:
            await update.message.reply_text(
                "⚠️ Неверный формат. Вводи время в формате <code>HH:MM</code> через пробел.\n"
                "Например: <code>08:14 09:20</code> или <code>9 12 16</code>",
                parse_mode=ParseMode.HTML,
            )
            return True

    elif field == "schedule_add":
        try:
            new_minutes_msk = _parse_schedule_msk_text(text)
            existing_utc = set(_schedule_utc_minutes(ch.get("post_times_utc", [])))
            for minute_msk in new_minutes_msk:
                existing_utc.add((minute_msk - 180) % 1440)
            ch["post_times_utc"] = _schedule_entries_from_utc_minutes(existing_utc)
            ch.pop("schedule_disabled", None)
            all_msk = _schedule_msk_minutes(ch["post_times_utc"])
            times_str = _format_schedule_minutes(all_msk)
            success_msg = f"✅ Расписание обновлено: <b>{times_str} МСК</b>"
        except Exception:
            await update.message.reply_text(
                "⚠️ Неверный формат. Вводи время в формате <code>HH:MM</code> через пробел.\n"
                "Например: <code>08:14</code>, <code>09:20</code> или <code>8 14 22</code>",
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
        # Если задано число/диапазон (слова) — в пределах 10..300. Текстовые форматы пропускаем.
        from ai_client import POST_LENGTH_MIN_WORDS as _LMIN, POST_LENGTH_MAX_WORDS as _LMAX
        m = re.fullmatch(r"(\d+)\s*(?:[-–—]\s*(\d+))?\s*(?:слов\w*)?", text.strip(), re.IGNORECASE)
        if m:
            lo = int(m.group(1)); hi = int(m.group(2)) if m.group(2) else lo
            if max(lo, hi) < _LMIN:
                await update.message.reply_text(
                    f"⚠️ Слишком мало — минимум <b>{_LMIN} слов</b>.\n"
                    "Напр.: <code>100-200</code> или <code>150</code>.",
                    parse_mode=ParseMode.HTML,
                )
                return True
            if max(lo, hi) > _LMAX:
                await update.message.reply_text(
                    f"⚠️ Слишком много — максимум <b>{_LMAX} слов</b>.\n"
                    "Для постов с картинкой Telegram обрезает подпись (~1024 символа, "
                    "≈150 слов), так что длиннее ставить смысла мало.",
                    parse_mode=ParseMode.HTML,
                )
                return True
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

    elif field == "folder":
        name = re.sub(r"\s+", " ", text).strip()[:40]
        if not name or name.lower() in ("нет", "убрать", "очистить"):
            ch.pop("folder", None)
            success_msg = "✅ Канал убран из папки."
        else:
            ch["folder"] = name
            success_msg = f"✅ Канал в папке: <b>{name}</b>"

    if success_msg:
        _save_channel(ch)
        context.user_data.pop("editing", None)
        # Для расписания — возвращаем на экран расписания, для папки — на экран папки
        if field == "schedule_add":
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("📅 К расписанию", callback_data=f"ui:ch_schedule:{handle}"),
                InlineKeyboardButton("◀️ К каналу",     callback_data=f"ui:ch:{handle}"),
            ]])
        elif field == "folder":
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("📁 Папка канала", callback_data=f"ui:ch_folder:{handle}"),
                InlineKeyboardButton("⚙️ К настройкам", callback_data=f"ui:ch_settings:{handle}"),
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
        msk = _schedule_msk_minutes(ch.get("post_times_utc", [6, 9, 13, 17]))
        return _format_schedule_minutes(msk, sep=" ") + " МСК"
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
# СПРАВКА — встроенная инструкция по разделам (понятным языком)
# ══════════════════════════════════════════════════════════════════════════════

HELP_SECTIONS = [
    ("start", "🚀 С чего начать",
     "🚀 <b>С чего начать</b>\n\n"
     "1. <b>➕ Добавить канал</b> — пришли @username своего канала. Бот сам прочитает "
     "его и определит тему и стиль.\n"
     "2. <b>Сделай бота админом</b> канала (с правом публикации) — иначе он не сможет постить.\n"
     "3. <b>⚡ Сгенерировать</b> — бот напишет посты (до 10 за раз).\n"
     "4. <b>📝 Посты</b> — посмотри, поправь, опубликуй или удали.\n"
     "5. <b>📅 Расписание</b> — включи время, и бот будет постить сам.\n\n"
     "Бот сам придумывает темы, пишет тексты и подбирает картинки — ты только проверяешь."),

    ("add", "➕ Добавить канал",
     "➕ <b>Добавить канал</b>\n\n"
     "Способы:\n"
     "• <b>По @username</b> (рекомендую) — бот прочитает канал и сам заполнит тему/стиль/название.\n"
     "• <b>Списком</b> — несколько @username за раз.\n"
     "• <b>Вручную</b> — если канал закрытый: впишешь название сам.\n\n"
     "Тип канала:\n"
     "• <b>📝 Контент</b> — посты (новости/советы/факты/разборы).\n"
     "• <b>🛍 Маркетплейс</b> — товары WB (цена, фото, ссылка).\n\n"
     "⚠️ После добавления <b>сделай бота админом</b> канала."),

    ("queue", "📝 Очередь и публикация",
     "📝 <b>Очередь и публикация</b>\n\n"
     "«📝 Посты» — карточка одного поста со стрелками ◀ ▶:\n"
     "• <b>✏️ Текст</b> — поправить вручную или дать ИИ переписать (картинка сохранится).\n"
     "• <b>🖼 Картинка</b> — заменить, подобрать или сгенерировать.\n"
     "• <b>🔄 Перегенерировать</b> — новый пост на ту же тему.\n"
     "• <b>🗑 Удалить</b> · <b>📨 Опубликовать сейчас</b>.\n"
     "• <b>📋 Вся очередь</b> — список всех постов.\n\n"
     "Готовые посты публикуются сами по расписанию."),

    ("drafts", "✍️ Черновики",
     "✍️ <b>Черновики</b> — свои посты\n\n"
     "Канал → <b>✍️ Черновик</b> → <b>➕ Создать пост</b>:\n"
     "• Пришли текст, фото, видео или <b>перешли</b> пост из другого канала.\n"
     "• Можно слать несколько подряд — каждый станет черновиком (альбом склеится в один).\n\n"
     "Черновик можно поправить (текст/медиа) и отправить <b>в очередь</b> — по одному "
     "или все сразу. Пока не отправишь — в публикацию не пойдёт."),

    ("refs", "🔗 Референсы",
     "🔗 <b>Референсы</b> — каналы-доноры\n\n"
     "Канал → ⚙️ Настройки → <b>🔗 Референс-каналы</b> → добавь @username "
     "<b>публичного</b> канала-донора.\n\n"
     "Бот раз в день (или по кнопке <b>📥 Взять</b>) забирает свежие посты донора в твою "
     "очередь: медиа — как есть, текст — можно перефразировать или оставить.\n\n"
     "Реклама в постах донора отфильтровывается."),

    ("schedule", "📅 Расписание",
     "📅 <b>Расписание</b>\n\n"
     "Канал → ⚙️ Настройки → <b>📅 Расписание</b> → впиши часы по МСК "
     "(напр. <code>9 12 16 20</code>).\n\n"
     "Бот публикует из очереди в эти часы. <b>Пустое расписание = автопостинга нет.</b>\n\n"
     "Между постами пауза 40 мин — не будет двух подряд."),

    ("folders", "📁 Папки",
     "📁 <b>Папки</b>\n\n"
     "Чтобы группировать каналы (Факты, Анеки и т.д.):\n"
     "• Канал → ⚙️ Настройки → <b>📁 Папка</b> → выбери или создай папку.\n"
     "• «Мои каналы» → <b>📁 Папки</b> → тапни папку, чтобы видеть только её каналы.\n"
     "• <b>📋 Все каналы</b> — сбросить фильтр."),

    ("settings", "⚙️ Настройки канала",
     "⚙️ <b>Настройки канала</b>\n\n"
     "• <b>🔄 Тема</b> — определяется ИИ по постам канала (вручную не вписывается). "
     "Кнопка «подобрать заново».\n"
     "• <b>📏 Длина поста</b>, <b>🖼 Картинки</b> (вкл/выкл).\n"
     "• <b>📰 Источники тем</b> — Авто (веб-поиск) или по RSS-лентам.\n"
     "• <b>🚫 Запрещённые темы</b> — свои доп. запретки (глобальные действуют всегда).\n"
     "• <b>Перекрытие рекламы РСЯ</b> — бот «перекрывает» чужую рекламу своим постом."),

    ("icons", "🟢 Что значат кружки",
     "🟢 <b>Индикаторы каналов</b>\n\n"
     "В списке «Мои каналы»:\n"
     "• 🟢 — <b>публикует</b> (расписание активно).\n"
     "• ⏸ — <b>пауза</b> по расписанию, но перекрытие рекламы РСЯ включено.\n"
     "• 🔴 — <b>остановлен</b> (и расписание, и РСЯ выключены)."),
]

_ADMIN_HELP = (
    "admin", "👑 Админ-панель",
    "👑 <b>Админ-панель</b> (только владелец)\n\n"
    "• <b>🎟 Инвайт</b> — создаёт ссылку для тестера (одноразовая, на N человек). "
    "Отправляй её лично.\n"
    "• <b>👥 Пользователи</b> — список тестеров; выдать PRO или закрыть доступ.\n\n"
    "Те же действия командами: /gen_invite, /users, /grant, /revoke."
)


async def screen_help(qm, context: ContextTypes.DEFAULT_TYPE):
    """Главный экран справки — разделы кнопками."""
    sections = list(HELP_SECTIONS)
    if accounts.is_superadmin(_acting_uid(qm)):
        sections = sections + [_ADMIN_HELP]
    rows, row = [], []
    for key, label, _ in sections:
        row.append(InlineKeyboardButton(label, callback_data=f"ui:help:{key}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("◀️ В меню", callback_data="ui:main")])
    text = (
        "❓ <b>Помощь</b>\n\n"
        "Бот сам придумывает темы, пишет посты и подбирает картинки — ты их проверяешь "
        "и публикуешь (вручную или по расписанию).\n\n"
        "Выбери раздел:"
    )
    await _answer_or_send(qm, text, InlineKeyboardMarkup(rows))


async def screen_help_section(qm, context: ContextTypes.DEFAULT_TYPE, key: str):
    """Текст конкретного раздела справки."""
    allsec = list(HELP_SECTIONS) + [_ADMIN_HELP]
    text = next((t for k, _, t in allsec if k == key), None)
    if not text:
        await screen_help(qm, context)
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ К разделам", callback_data="ui:help")]])
    await _answer_or_send(qm, text, kb)


# ══════════════════════════════════════════════════════════════════════════════
# АДМИН-ПАНЕЛЬ (только superadmin) — инвайты и пользователи по кнопкам
# ══════════════════════════════════════════════════════════════════════════════

async def screen_boost_admin(qm, context: ContextTypes.DEFAULT_TYPE):
    uid = _acting_uid(qm)
    if not accounts.is_superadmin(uid):
        await qm.answer("Только для владельца.", show_alert=True)
        return

    settings = get_boost_settings()
    channels = list_tracked_channels()
    enabled_count = sum(1 for ch in channels if ch.get("enabled"))
    global_state = _boost_onoff_label(bool(settings.get("boost_enabled")))
    api_state = _boost_config_label(boost_configured(cfg))
    service_state = _boost_config_label(_boost_service_configured(settings))
    real_allowed = _boost_yesno_label(boost_real_orders_allowed(settings, cfg))
    global_hint = "\n\nБуст выключен глобально. Новые посты не обрабатываются." if not settings.get("boost_enabled") else ""
    real_order_hint = _boost_real_order_hint(settings)

    text = (
        "🚀 <b>Настройки Boost</b>\n\n"
        f"Boost: <b>{global_state}</b>\n"
        f"Статус: <b>{_boost_status_label(boost_status(settings, cfg))}</b>\n"
        f"Режим: <b>{_boost_mode_label()}</b>\n"
        f"Реальные заказы разрешены: <b>{real_allowed}</b>\n"
        f"TwiBoost API: <b>{api_state}</b>\n"
        f"ID сервиса: <b>{service_state}</b>\n"
        f"Количество по умолчанию: <b>{settings.get('default_quantity')}</b>\n"
        f"Отслеживаемые каналы: <b>{len(channels)}</b>\n"
        f"Включено каналов: <b>{enabled_count}</b>\n"
        f"Последняя ошибка: <b>{html.escape(_boost_reason_label(settings.get('last_error')))}</b>\n\n"
        f"{real_order_hint}"
        f"{global_hint}"
    )
    rows = [
        [InlineKeyboardButton(f"Глобальный Boost: {global_state}", callback_data="ui:boost_toggle")],
        [InlineKeyboardButton("➕ Добавить канал", callback_data="ui:boost_add")],
        [InlineKeyboardButton("📋 Список каналов", callback_data="ui:boost_channels")],
        [InlineKeyboardButton("🧾 Журнал событий", callback_data="ui:boost_events")],
        [InlineKeyboardButton("◀️ В админ-панель", callback_data="ui:admin")],
    ]
    await _answer_or_send(qm, text, InlineKeyboardMarkup(rows))


async def screen_boost_channels(qm, context: ContextTypes.DEFAULT_TYPE):
    uid = _acting_uid(qm)
    if not accounts.is_superadmin(uid):
        await qm.answer("Только для владельца.", show_alert=True)
        return

    channels = list_tracked_channels()
    rows = [
        [InlineKeyboardButton(_boost_channel_button_label(ch), callback_data=f"ui:boost_ch:{ch['id']}")]
        for ch in channels[:25]
    ]
    rows.append([InlineKeyboardButton("🧾 Журнал событий", callback_data="ui:boost_events")])
    rows.append([InlineKeyboardButton("◀️ Настройки Boost", callback_data="ui:boost")])
    text = (
        "🚀 <b>Каналы Boost</b>\n\n"
        f"Отслеживается: <b>{len(channels)}</b>\n"
        "Это отдельный список. Обычные каналы генератора сюда не попадают автоматически."
    )
    await _answer_or_send(qm, text, InlineKeyboardMarkup(rows))


def _boost_event_channel_name(event: dict) -> str:
    if event.get("channel_username"):
        return f"@{event['channel_username']}"
    if event.get("channel_tg_chat_id"):
        return str(event["channel_tg_chat_id"])
    if event.get("channel_title"):
        return str(event["channel_title"])
    return f"Boost #{event.get('boost_channel_id')}"


def _boost_event_line(event: dict) -> str:
    reason = event.get("reason_code") or event.get("error")
    reason_label = _boost_reason_label(reason)
    range_display = event.get("channel_quantity_display")
    quantity_line = f"{event.get('quantity')} просмотров"
    if range_display and str(range_display) != str(event.get("quantity")):
        quantity_line = f"{range_display} просмотров, выбрано: {event.get('quantity')}"
    status_line = f"Статус: {_boost_status_label(event.get('status'))}"
    if event.get("status") in ("ignored", "failed") and reason:
        status_line = f"Статус: {_boost_status_label(event.get('status'))}\nПричина: {html.escape(reason_label)}"
    link_line = f"Ссылка: {html.escape(str(event.get('post_url')))}" if event.get("post_url") else ""
    return (
        f"#{event.get('id')} · <b>{html.escape(_boost_event_channel_name(event))}</b>\n"
        f"{_boost_event_type_label(event.get('event_type')).capitalize()} · сообщение <code>{event.get('message_id')}</code> · "
        f"{html.escape(quantity_line)}\n"
        f"{status_line}\n"
        f"{link_line + chr(10) if link_line else ''}"
        f"<code>{html.escape(_boost_event_time_label(event.get('created_at')))}</code>"
    )


async def screen_boost_events(qm, context: ContextTypes.DEFAULT_TYPE, channel_id: int | None = None):
    uid = _acting_uid(qm)
    if not accounts.is_superadmin(uid):
        await qm.answer("Только для владельца.", show_alert=True)
        return

    events = list_boost_events(limit=10, boost_channel_id=channel_id)
    if events:
        body = "\n\n".join(_boost_event_line(event) for event in events)
    else:
        body = "Событий пока нет."
    title = "🧾 <b>Журнал Boost</b>"
    if channel_id is not None:
        title += f" · канал <code>{channel_id}</code>"
    rows = []
    if channel_id is not None:
        rows.append([InlineKeyboardButton("◀️ К карточке канала", callback_data=f"ui:boost_ch:{channel_id}")])
    rows.append([InlineKeyboardButton("◀️ Настройки Boost", callback_data="ui:boost")])
    await _answer_or_send(qm, f"{title}\n\n{body}", InlineKeyboardMarkup(rows))


async def screen_boost_add_prompt(qm, context: ContextTypes.DEFAULT_TYPE):
    uid = _acting_uid(qm)
    if not accounts.is_superadmin(uid):
        await qm.answer("Только для владельца.", show_alert=True)
        return
    _clear_boost_pending(context)
    text = (
        "➕ <b>Добавить канал Boost</b>\n\n"
        "Основной путь — выбрать канал из уже добавленных каналов smm_bot. "
        "Так Boost сохранит связь с карточкой канала и не смешает его с обычными настройками генератора.\n\n"
        "Внешний канал оставлен только как запасной ручной режим."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Из моих каналов", callback_data="ui:boost_add_mine:0")],
        [InlineKeyboardButton("➕ Внешний канал", callback_data="ui:boost_add_ext")],
        [InlineKeyboardButton("◀️ Настройки Boost", callback_data="ui:boost")],
    ])
    await _answer_or_send(qm, text, kb)


async def screen_boost_add_external_prompt(qm, context: ContextTypes.DEFAULT_TYPE):
    uid = _acting_uid(qm)
    if not accounts.is_superadmin(uid):
        await qm.answer("Только для владельца.", show_alert=True)
        return
    _clear_boost_pending(context)
    context.user_data["boost_add_channel"] = True
    text = (
        "➕ <b>Добавить внешний канал Boost</b>\n\n"
        "Пришли @username, t.me ссылку или chat_id. Если канал уже есть в Boost, я открою существующую карточку.\n\n"
        "Этот режим не связывает запись с карточкой smm_bot и будет показан как внешний/ручной."
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="ui:boost_add")]])
    await _answer_or_send(qm, text, kb)


def _boost_smm_candidates(uid: int | None) -> list[dict]:
    channels = [
        ch for ch in _load_channels(include_inactive=True, owner_id=uid, scope="mine")
        if ch.get("active", True)
    ]
    channels.sort(key=lambda c: ((c.get("name") or c.get("channel_id") or "").lower()))
    return channels


def _boost_smm_name(ch: dict) -> str:
    return ch.get("name") or ch.get("channel_id") or "канал"


def _boost_smm_identity(ch: dict) -> str:
    username = ch.get("username") or ch.get("channel_id")
    if username:
        return str(username)
    if ch.get("chat_id_num"):
        return str(ch["chat_id_num"])
    return "нет id"


def _boost_smm_button_label(ch: dict) -> str:
    existing = find_tracked_channel_for_smm_channel(ch)
    state = _boost_smm_state_label(existing)
    label = f"{_boost_smm_name(ch)} | {state}"
    return label[:60]


async def screen_boost_smm_picker(qm, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    uid = _acting_uid(qm)
    if not accounts.is_superadmin(uid):
        await qm.answer("Только для владельца.", show_alert=True)
        return
    channels = _boost_smm_candidates(uid)
    total = len(channels)
    if not channels:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Внешний канал", callback_data="ui:boost_add_ext")],
            [InlineKeyboardButton("◀️ Назад", callback_data="ui:boost_add")],
        ])
        await _answer_or_send(qm, "📋 <b>Из моих каналов</b>\n\nАктивных каналов smm_bot для выбора нет.", kb)
        return

    pages = (total + BOOST_PICKER_PAGE_SIZE - 1) // BOOST_PICKER_PAGE_SIZE
    page = max(0, min(int(page), pages - 1))
    start = page * BOOST_PICKER_PAGE_SIZE
    chunk = channels[start:start + BOOST_PICKER_PAGE_SIZE]

    lines = [f"📋 <b>Из моих каналов</b> ({total}) · стр. {page + 1}/{pages}\n"]
    rows = []
    for idx, ch in enumerate(chunk):
        existing = find_tracked_channel_for_smm_channel(ch)
        status = _boost_smm_state_label(existing)
        lines.append(
            f"{idx + 1}. <b>{html.escape(_boost_smm_name(ch))}</b>\n"
            f"   <code>{html.escape(_boost_smm_identity(ch))}</code> · Boost: <b>{status}</b>"
        )
        rows.append([InlineKeyboardButton(f"{idx + 1}. {_boost_smm_button_label(ch)}", callback_data=f"ui:boost_pick:{page}:{idx}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"ui:boost_add_mine:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"ui:boost_add_mine:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="ui:boost_add")])
    await _answer_or_send(qm, "\n".join(lines), InlineKeyboardMarkup(rows))


async def action_boost_pick_smm_channel(qm, context: ContextTypes.DEFAULT_TYPE, page: int, index: int):
    uid = _acting_uid(qm)
    if not accounts.is_superadmin(uid):
        await qm.answer("Только для владельца.", show_alert=True)
        return
    channels = _boost_smm_candidates(uid)
    offset = int(page) * BOOST_PICKER_PAGE_SIZE + int(index)
    if offset < 0 or offset >= len(channels):
        await qm.answer("Канал не найден.", show_alert=True)
        await screen_boost_smm_picker(qm, context, page)
        return

    ch = channels[offset]
    existing = find_tracked_channel_for_smm_channel(ch)
    if existing:
        linked = link_tracked_channel_to_smm_channel(existing["id"], ch, owner_id=ch.get("owner_id"))
        await qm.answer("Уже добавлен в Boost.")
        await screen_boost_channel_detail(qm, context, int((linked or existing)["id"]))
        return

    _clear_boost_pending(context)
    context.user_data["boost_add_smm_channel_id"] = ch.get("channel_id")
    settings = get_boost_settings()
    await qm.answer()
    await qm.edit_message_text(
        (
            f"📋 <b>{html.escape(_boost_smm_name(ch))}</b>\n\n"
            f"Канал: <code>{html.escape(_boost_smm_identity(ch))}</code>\n"
            f"Количество по умолчанию: <b>{settings.get('default_quantity')}</b>\n\n"
            "Введите количество просмотров: например 500 или диапазон 500-550. Канал будет сохранен выключенным по умолчанию."
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=f"ui:boost_add_mine:{page}")]]),
    )


def _boost_channel_name(ch: dict) -> str:
    if ch.get("username"):
        return f"@{ch['username']}"
    if ch.get("tg_chat_id"):
        return str(ch["tg_chat_id"])
    return ch.get("channel_key") or f"#{ch.get('id')}"


def _boost_channel_button_label(ch: dict) -> str:
    state = _boost_onoff_label(bool(ch.get("enabled")))
    qty = _boost_quantity_display(ch)
    linked = _boost_link_label(ch.get("smm_channel_id"))
    return f"{_boost_channel_name(ch)} | {linked} | {state} | {qty}"[:60]


async def screen_boost_channel_detail(qm, context: ContextTypes.DEFAULT_TYPE, channel_id: int):
    uid = _acting_uid(qm)
    if not accounts.is_superadmin(uid):
        await qm.answer("Только для владельца.", show_alert=True)
        return
    ch = get_tracked_channel(channel_id)
    if not ch:
        await qm.answer("Канал Boost не найден.", show_alert=True)
        await screen_boost_channels(qm, context)
        return

    linked = _boost_link_label(ch.get("smm_channel_id"))
    service_state = _boost_config_label(bool(ch.get("service_id") or _boost_service_configured()))
    public_state = "есть" if ch.get("username") else "нет"
    disabled_hint = "\n\n⚠️ Канал выключен. Новые посты не обрабатываются." if not ch.get("enabled") else ""
    text = (
        f"🚀 <b>Канал Boost</b> <code>{ch['id']}</code>\n\n"
        f"Канал: <b>{html.escape(_boost_channel_name(ch))}</b>\n"
        f"Связь с smm_bot: <b>{html.escape(str(linked))}</b>\n"
        f"Состояние: <b>{_boost_onoff_label(bool(ch.get('enabled')))}</b>\n"
        f"Количество: <b>{_boost_quantity_display(ch)}</b>\n"
        f"ID сервиса: <b>{service_state}</b>\n"
        f"Публичная ссылка: <b>{public_state}</b>\n"
        f"Название: <b>{html.escape(_boost_none_label(ch.get('title')))}</b>\n"
        f"Последний пост: <b>{_boost_none_label(ch.get('last_seen_message_id'))}</b>\n"
        f"Последний заказ: <b>{html.escape(_boost_none_label(ch.get('last_order_id')))}</b>\n"
        f"Последняя причина: <b>{html.escape(_boost_reason_label(ch.get('last_error')))}</b>"
        f"{disabled_hint}"
    )
    next_state = _boost_onoff_label(not bool(ch.get("enabled")))
    rows = [
        [InlineKeyboardButton(f"Состояние: {next_state}", callback_data=f"ui:boost_ch_tgl:{ch['id']}")],
        [InlineKeyboardButton("✏️ Изменить количество", callback_data=f"ui:boost_ch_qty:{ch['id']}")],
        [InlineKeyboardButton("🧾 Журнал канала", callback_data=f"ui:boost_ch_events:{ch['id']}")],
        [InlineKeyboardButton("Удалить", callback_data=f"ui:boost_ch_del:{ch['id']}")],
        [InlineKeyboardButton("◀️ К списку", callback_data="ui:boost_channels")],
    ]
    await _answer_or_send(qm, text, InlineKeyboardMarkup(rows))


async def screen_boost_delete_confirm(qm, context: ContextTypes.DEFAULT_TYPE, channel_id: int):
    uid = _acting_uid(qm)
    if not accounts.is_superadmin(uid):
        await qm.answer("Только для владельца.", show_alert=True)
        return
    ch = get_tracked_channel(channel_id)
    if not ch:
        await qm.answer("Канал Boost не найден.", show_alert=True)
        await screen_boost_channels(qm, context)
        return
    text = (
        f"Удалить канал Boost <b>{html.escape(_boost_channel_name(ch))}</b>?\n\n"
        "Это удалит только запись Boost. Карточка smm_bot и обычные настройки канала не трогаются."
    )
    rows = [
        [InlineKeyboardButton("Удалить из Boost", callback_data=f"ui:boost_ch_del_ok:{ch['id']}")],
        [InlineKeyboardButton("◀️ Назад", callback_data=f"ui:boost_ch:{ch['id']}")],
    ]
    await _answer_or_send(qm, text, InlineKeyboardMarkup(rows))


async def handle_boost_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if user is None or not accounts.is_superadmin(user.id):
        return False

    smm_channel_id = context.user_data.get("boost_add_smm_channel_id")
    if smm_channel_id:
        raw = (update.message.text or "").strip()
        try:
            quantity = parse_boost_quantity(raw)["quantity_display"]
        except ValueError:
            await update.message.reply_text("❌ Минимальное количество просмотров — 500. Используйте число 500 или диапазон 500-550.")
            return True

        channels = _boost_smm_candidates(user.id)
        smm_ch = next((c for c in channels if c.get("channel_id") == smm_channel_id), None)
        if not smm_ch:
            _clear_boost_pending(context)
            await update.message.reply_text("❌ Канал больше не доступен в твоём scope.")
            return True

        boost_owner_id = smm_ch.get("owner_id")
        ch, created = add_tracked_channel_from_smm_channel(
            smm_ch,
            owner_id=boost_owner_id,
            quantity=quantity,
            enabled=False,
        )
        _clear_boost_pending(context)
        await update.message.reply_text(
            (
                f"{'✅ Канал Boost добавлен' if created else 'ℹ️ Этот канал уже добавлен в Boost'}: "
                f"<b>{html.escape(_boost_channel_name(ch))}</b>\n"
                "Сейчас он выключен. Включи его в карточке канала Boost."
            ),
            parse_mode=ParseMode.HTML,
        )
        await screen_boost_channel_detail(update.message, context, int(ch["id"]))
        return True

    external_raw = context.user_data.get("boost_add_external_channel")
    if external_raw:
        raw = (update.message.text or "").strip()
        try:
            quantity = parse_boost_quantity(raw)["quantity_display"]
            ch = add_tracked_channel(external_raw, owner_id=user.id, quantity=quantity, enabled=False)
        except ValueError:
            await update.message.reply_text("❌ Минимальное количество просмотров — 500. Используйте число 500 или диапазон 500-550.")
            return True
        _clear_boost_pending(context)
        await update.message.reply_text(
            f"✅ Канал Boost добавлен: <b>{html.escape(_boost_channel_name(ch))}</b>\n"
            "Сейчас он выключен. Включи его в списке каналов.",
            parse_mode=ParseMode.HTML,
        )
        await screen_boost_channel_detail(update.message, context, int(ch["id"]))
        return True

    if context.user_data.get("boost_add_channel"):
        raw = (update.message.text or "").strip()
        try:
            normalize_channel_input(raw)
        except ValueError:
            await update.message.reply_text("❌ Не понял канал. Пришли @username, t.me/channel или numeric chat_id.")
            return True
        existing = find_tracked_channel_for_input(raw)
        if existing:
            _clear_boost_pending(context)
            await update.message.reply_text(
                f"ℹ️ Этот канал уже добавлен в Boost: <b>{html.escape(_boost_channel_name(existing))}</b>",
                parse_mode=ParseMode.HTML,
            )
            await screen_boost_channel_detail(update.message, context, int(existing["id"]))
            return True
        context.user_data.pop("boost_add_channel", None)
        context.user_data["boost_add_external_channel"] = raw
        settings = get_boost_settings()
        await update.message.reply_text(
            (
                "Введите количество просмотров: например 500 или диапазон 500-550.\n"
                f"Количество по умолчанию: {settings.get('default_quantity')}"
            )
        )
        return True

    qty_channel_id = context.user_data.get("boost_set_quantity_for")
    if qty_channel_id is not None:
        raw = (update.message.text or "").strip()
        try:
            quantity = parse_boost_quantity(raw)["quantity_display"]
            ch = set_tracked_channel_quantity(int(qty_channel_id), quantity)
        except (TypeError, ValueError):
            await update.message.reply_text("❌ Минимальное количество просмотров — 500. Используйте число 500 или диапазон 500-550.")
            return True
        context.user_data.pop("boost_set_quantity_for", None)
        await update.message.reply_text(
            f"✅ Количество обновлено: <b>{_boost_quantity_display(ch)}</b>",
            parse_mode=ParseMode.HTML,
        )
        await screen_boost_channel_detail(update.message, context, int(qty_channel_id))
        return True

    return False


async def screen_admin(qm, context: ContextTypes.DEFAULT_TYPE):
    """Меню админа: создать инвайт, список тестеров, каналы пользователей."""
    uid = _acting_uid(qm)
    users = accounts.list_users()
    tester_chans = _load_channels(include_inactive=True, owner_id=uid, scope="testers")
    my_chans = [c for c in _load_channels(include_inactive=True, owner_id=uid, scope="mine")
                if c.get("active", True) and c.get("owner_id") in (None, uid)]
    rsy_state = "вкл ✅" if admin_default_rsy_enabled() else "выкл ⬜️"
    text = (
        "👑 <b>Админ-панель</b>\n\n"
        f"Зарегистрировано тестеров: <b>{len(users)}</b>\n"
        f"Каналов у тестеров: <b>{len(tester_chans)}</b>\n\n"
        f"Моих активных каналов: <b>{len(my_chans)}</b>\n"
        f"РСЯ по умолчанию для новых моих каналов: <b>{rsy_state}</b>\n\n"
        "🎟 Инвайт-ссылка <b>одноразовая</b> (на N человек): кто первый откроет — "
        "тот и зарегистрируется. Отправляй её тестеру <b>лично</b>."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎟 Инвайт (1 чел · 30 дней)", callback_data="ui:adm_inv:1:30")],
        [InlineKeyboardButton("🎟 Инвайт (5 чел · 30 дней)", callback_data="ui:adm_inv:5:30")],
        [InlineKeyboardButton(f"👥 Пользователи ({len(users)})", callback_data="ui:adm_users")],
        [InlineKeyboardButton(f"📡 Каналы пользователей ({len(tester_chans)})", callback_data="ui:adm_chans:0")],
        [InlineKeyboardButton(f"📢 РСЯ по умолчанию: {rsy_state}", callback_data="ui:adm_rsy_default")],
        [InlineKeyboardButton(f"🗑 Удалить мои каналы ({len(my_chans)})", callback_data="ui:adm_del_my_chans")],
        [InlineKeyboardButton("🚀 Boost", callback_data="ui:boost")],
        [InlineKeyboardButton("💰 Расходы", callback_data="ui:adm_cost:today")],
        [InlineKeyboardButton("⚡ Генерить для всех", callback_data="ui:generate_all")],
        [InlineKeyboardButton("◀️ В меню", callback_data="ui:main")],
    ])
    await _answer_or_send(qm, text, kb)


async def action_admin_invite(qm, context, uses: int, days: int):
    """Создаёт инвайт-код и показывает готовую ссылку."""
    code = accounts.gen_invite(plan="trial", days=days, max_uses=uses, created_by=_acting_uid(qm))
    try:
        bot_username = (await context.bot.get_me()).username
        link = f"https://t.me/{bot_username}?start={code}"
    except Exception:
        link = f"(?start={code})"
    text = (
        "🎟 <b>Инвайт создан</b>\n\n"
        f"План: <b>trial</b> · {days} дней · использований: <b>{uses}</b>\n\n"
        f"Ссылка (нажми, чтобы скопировать):\n<code>{link}</code>\n\n"
        f"⚠️ Зарегистрируются первые <b>{uses}</b>, кто откроет ссылку. Отправь её лично."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎟 Ещё инвайт", callback_data="ui:admin")],
        [InlineKeyboardButton("◀️ Админ-панель", callback_data="ui:admin")],
    ])
    await _answer_or_send(qm, text, kb)


async def action_admin_rsy_default_toggle(qm, context):
    """Переключает РСЯ-перекрытие по умолчанию для новых каналов superadmin."""
    data = _load_admin_settings()
    data["default_rsy_override"] = not bool(data.get("default_rsy_override", False))
    _save_admin_settings(data)
    state = "включено ✅" if data["default_rsy_override"] else "выключено ⬜️"
    await qm.answer(f"РСЯ по умолчанию {state}")
    await screen_admin(qm, context)


async def action_admin_delete_my_channels_confirm(qm, context):
    """Подтверждение массовой деактивации только админских каналов superadmin."""
    uid = _acting_uid(qm)
    chans = [c for c in _load_channels(include_inactive=True, owner_id=uid, scope="mine")
             if c.get("active", True) and c.get("owner_id") in (None, uid)]
    if not chans:
        await _answer_or_send(
            qm,
            "🗑 <b>Мои каналы</b>\n\nАктивных каналов для удаления нет.",
            InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Админ-панель", callback_data="ui:admin")]]),
        )
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Да, удалить мои каналы ({len(chans)})", callback_data="ui:adm_del_my_chans_ok")],
        [InlineKeyboardButton("◀️ Отмена", callback_data="ui:admin")],
    ])
    await _answer_or_send(
        qm,
        f"🗑 <b>Удалить мои каналы?</b>\n\n"
        f"Будет деактивировано <b>{len(chans)}</b> каналов superadmin. "
        f"Каналы тестеров не затрагиваются. Посты в БД не удаляются.",
        kb,
    )


async def action_admin_delete_my_channels_ok(qm, context):
    """Деактивирует все активные каналы superadmin, не трогая каналы тестеров."""
    uid = _acting_uid(qm)
    chans = [c for c in _load_channels(include_inactive=True, owner_id=uid, scope="mine")
             if c.get("active", True) and c.get("owner_id") in (None, uid)]
    count = 0
    for ch in chans:
        ch["active"] = False
        _save_channel(ch)
        count += 1
    logger.info(f"Админские каналы superadmin деактивированы: {count}")
    await _answer_or_send(
        qm,
        f"🗑 Деактивировано моих каналов: <b>{count}</b>.",
        InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Админ-панель", callback_data="ui:admin")]]),
    )


async def _user_label(context, uid: int) -> str | None:
    """Юзернейм/имя пользователя через Bot API (работает для всех, кто писал боту)."""
    try:
        chat = await context.bot.get_chat(uid)
        if getattr(chat, "username", None):
            return f"@{chat.username}"
        name = " ".join(filter(None, [getattr(chat, "first_name", None),
                                      getattr(chat, "last_name", None)]))
        return name or None
    except Exception:
        return None


async def screen_admin_users(qm, context: ContextTypes.DEFAULT_TYPE):
    """Список тестеров: юзернейм/имя, план, остаток триала, кнопки PRO / закрыть доступ."""
    import html as _html
    users = accounts.list_users()
    if not users:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Админ-панель", callback_data="ui:admin")]])
        await _answer_or_send(qm, "👥 <b>Тестеры</b>\n\nПока никто не зарегистрировался.", kb)
        return
    lines = ["👥 <b>Тестеры</b>\n"]
    rows = []
    for u in users[:20]:
        uid = u["user_id"]
        plan = accounts.effective_plan(uid)
        left = accounts.trial_days_left(uid)
        left_s = f" · осталось {left}д" if left is not None else ""
        label = await _user_label(context, uid)
        who = _html.escape(label) if label else "—"
        lines.append(f"• <b>{who}</b> · <code>{uid}</code> — <b>{plan}</b>{left_s}")
        short = (label or str(uid))[:16]
        rows.append([
            InlineKeyboardButton(f"🔼 PRO · {short}", callback_data=f"ui:adm_pro:{uid}"),
            InlineKeyboardButton("⛔ Доступ", callback_data=f"ui:adm_revoke:{uid}"),
        ])
    rows.append([InlineKeyboardButton("◀️ Админ-панель", callback_data="ui:admin")])
    await _answer_or_send(qm, "\n".join(lines), InlineKeyboardMarkup(rows))


async def screen_admin_user_channels(qm, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    """Каналы тестеров (для админа) — отдельно от собственных. С пометкой владельца."""
    chans = [c for c in _load_channels(include_inactive=True, owner_id=_acting_uid(qm), scope="testers")
             if c.get("active", True)]
    if not chans:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Админ-панель", callback_data="ui:admin")]])
        await _answer_or_send(qm, "📡 <b>Каналы пользователей</b>\n\nУ тестеров пока нет каналов.", kb)
        return

    # Группируем по владельцу для читаемости
    chans.sort(key=lambda c: (c.get("owner_id") or 0, c.get("name") or c["channel_id"]))
    total = len(chans)
    pages = (total + CHANNELS_PAGE_SIZE - 1) // CHANNELS_PAGE_SIZE
    page = max(0, min(page, pages - 1))
    chunk = chans[page * CHANNELS_PAGE_SIZE:(page + 1) * CHANNELS_PAGE_SIZE]

    # Подписи владельцев (кэшируем в рамках экрана, чтобы не дёргать get_chat по разу на канал)
    owner_labels: dict[int, str] = {}
    rows = []
    for ch in chunk:
        oid = ch.get("owner_id")
        if oid and oid not in owner_labels:
            owner_labels[oid] = (await _user_label(context, oid)) or str(oid)
        suffix = f" · {owner_labels[oid]}" if oid else ""
        name = ch.get("name") or ch["channel_id"]
        rows.append([InlineKeyboardButton(
            f"{_toggle_icon(ch)} {name}{suffix}", callback_data=f"ui:ch:{ch['channel_id']}")])

    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️", callback_data=f"ui:adm_chans:{page - 1}"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"ui:adm_chans:{page + 1}"))
        rows.append(nav)
    rows.append([InlineKeyboardButton("◀️ Админ-панель", callback_data="ui:admin")])

    header = f"📡 <b>Каналы пользователей</b> ({total})"
    if pages > 1:
        header += f" · стр. {page + 1}/{pages}"
    legend = "🟢 публикует · ⏸ пауза (РСЯ вкл) · 🔴 остановлен"
    await _answer_or_send(qm, f"{header}\n\n{legend}", InlineKeyboardMarkup(rows))


def _costs_text(title: str, since: str | None) -> str:
    """Рендер сводки расходов за период."""
    import cost_tracker
    s = cost_tracker.summary(since)
    cl, fl = s["claude"], s["fal"]
    return (
        f"💰 <b>Расходы</b> · {title}\n\n"
        f"🤖 <b>Claude</b>: ${cl['cost']:.2f}\n"
        f"    {cl['calls']} вызовов · {cl['in_tok']:,} вход / {cl['out_tok']:,} выход токенов\n\n"
        f"🎨 <b>fal.ai (FLUX)</b>: ${fl['cost']:.2f}\n"
        f"    {fl['units']} картинок\n\n"
        f"━━━━━━━━━━━━━\n"
        f"<b>Итого: ${s['total']:.2f}</b>"
    ).replace(",", " ")


def _costs_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Сегодня", callback_data="ui:adm_cost:today"),
            InlineKeyboardButton("7 дней",  callback_data="ui:adm_cost:7"),
            InlineKeyboardButton("30 дней", callback_data="ui:adm_cost:30"),
        ],
        [
            InlineKeyboardButton("Всё время",   callback_data="ui:adm_cost:all"),
            InlineKeyboardButton("📅 Свой период", callback_data="ui:adm_cost:custom"),
        ],
        [InlineKeyboardButton("◀️ Админ-панель", callback_data="ui:admin")],
    ])


async def screen_admin_costs(qm, context: ContextTypes.DEFAULT_TYPE, period: str = "today"):
    """Расходы на сервисы за период: сегодня / 7 / 30 дней / всё время / свой период."""
    import cost_tracker
    if period == "custom":
        context.user_data["awaiting_cost_days"] = True
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="ui:adm_cost:today")]])
        await _answer_or_send(
            qm, "📅 <b>Свой период</b>\n\nПришли число дней (например <code>14</code>) — "
                "посчитаю расходы за последние N дней.", kb)
        return

    if period == "today":
        title, since = "сегодня", cost_tracker.since_today_msk()
    elif period == "all":
        title, since = "всё время", None
    elif period.isdigit():
        n = int(period)
        title, since = f"{n} дней", cost_tracker.since_days(n)
    else:
        title, since = "сегодня", cost_tracker.since_today_msk()

    await _answer_or_send(qm, _costs_text(title, since), _costs_keyboard())


async def screen_admin_costs_custom(qm, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Обработка ввода числа дней для произвольного периода расходов."""
    import cost_tracker
    digits = "".join(ch for ch in (text or "") if ch.isdigit())
    if not digits:
        context.user_data["awaiting_cost_days"] = True
        await _answer_or_send(
            qm, "❌ Нужно число дней. Пришли, например, <code>14</code>.",
            InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="ui:adm_cost:today")]]))
        return
    n = max(1, min(int(digits), 3650))
    await _answer_or_send(qm, _costs_text(f"{n} дней", cost_tracker.since_days(n)), _costs_keyboard())


async def action_admin_revoke(qm, context, uid: str):
    try:
        accounts.revoke_user(int(uid))
    except Exception:
        pass
    await screen_admin_users(qm, context)


async def action_admin_pro(qm, context, uid: str):
    try:
        accounts.set_plan(int(uid), "pro", days=30)
    except Exception:
        pass
    await screen_admin_users(qm, context)


# ══════════════════════════════════════════════════════════════════════════════
# ГЛАВНЫЙ РОУТЕР — обрабатывает все ui:* callback
# ══════════════════════════════════════════════════════════════════════════════

async def ui_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Центральный обработчик всех callback_data начинающихся с 'ui:'.
    Парсит callback_data и вызывает нужный экран/действие.
    """
    query = update.callback_query
    uid = query.from_user.id

    if not accounts.has_access(uid):
        await query.answer("Доступ по приглашению — пришли /start.")
        return

    data = query.data  # например: "ui:ch:@hagenezykas"
    parts = data.split(":")  # ["ui", "ch", "@hagenezykas"]
    action = parts[1] if len(parts) > 1 else ""

    if action == "noop":
        await query.answer()
        return

    if any(key.startswith("boost_") for key in context.user_data):
        _clear_boost_pending(context)

    # 👑 Админ-действия — только для главного владельца (superadmin)
    if action in ("admin", "adm_inv", "adm_users", "adm_revoke", "adm_pro", "adm_chans", "adm_cost",
                  "boost", "boost_toggle", "boost_add", "boost_add_mine", "boost_add_ext", "boost_pick",
                  "boost_channels", "boost_events", "boost_ch", "boost_ch_events", "boost_ch_tgl", "boost_ch_qty",
                  "boost_ch_del", "boost_ch_del_ok",
                  "adm_rsy_default", "adm_del_my_chans", "adm_del_my_chans_ok"):
        if not accounts.is_superadmin(uid):
            await query.answer("Только для владельца.", show_alert=True)
            return
        if action == "admin":
            await screen_admin(query, context)
        elif action == "adm_inv" and len(parts) >= 4:
            await action_admin_invite(query, context, int(parts[2]), int(parts[3]))
        elif action == "adm_users":
            await screen_admin_users(query, context)
        elif action == "adm_chans":
            page = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
            await screen_admin_user_channels(query, context, page)
        elif action == "adm_cost":
            period = parts[2] if len(parts) >= 3 and parts[2] else "today"
            await screen_admin_costs(query, context, period)
        elif action == "boost":
            await screen_boost_admin(query, context)
        elif action == "boost_toggle":
            current = get_boost_settings().get("boost_enabled", False)
            set_boost_enabled(not current)
            await query.answer(f"Глобальный Boost: {_boost_onoff_label(not current)}")
            await screen_boost_admin(query, context)
        elif action == "boost_add":
            await screen_boost_add_prompt(query, context)
        elif action == "boost_add_mine":
            page = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
            await screen_boost_smm_picker(query, context, page)
        elif action == "boost_add_ext":
            await screen_boost_add_external_prompt(query, context)
        elif action == "boost_pick" and len(parts) >= 4 and parts[2].isdigit() and parts[3].isdigit():
            await action_boost_pick_smm_channel(query, context, int(parts[2]), int(parts[3]))
        elif action == "boost_channels":
            await screen_boost_channels(query, context)
        elif action == "boost_events":
            await screen_boost_events(query, context)
        elif action == "boost_ch" and len(parts) >= 3 and parts[2].isdigit():
            await screen_boost_channel_detail(query, context, int(parts[2]))
        elif action == "boost_ch_events" and len(parts) >= 3 and parts[2].isdigit():
            await screen_boost_events(query, context, int(parts[2]))
        elif action == "boost_ch_tgl" and len(parts) >= 3 and parts[2].isdigit():
            ch = get_tracked_channel(int(parts[2]))
            if not ch:
                await query.answer("Канал Boost не найден.", show_alert=True)
                return
            set_tracked_channel_enabled(int(parts[2]), not bool(ch.get("enabled")))
            await query.answer("Обновлено.")
            await screen_boost_channel_detail(query, context, int(parts[2]))
        elif action == "boost_ch_qty" and len(parts) >= 3 and parts[2].isdigit():
            context.user_data["boost_set_quantity_for"] = int(parts[2])
            await query.answer()
            await query.edit_message_text(
                "Введите количество просмотров: например 500 или диапазон 500-550.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=f"ui:boost_ch:{parts[2]}")]]),
            )
        elif action == "boost_ch_del" and len(parts) >= 3 and parts[2].isdigit():
            await screen_boost_delete_confirm(query, context, int(parts[2]))
        elif action == "boost_ch_del_ok" and len(parts) >= 3 and parts[2].isdigit():
            delete_tracked_channel(int(parts[2]))
            await query.answer("Удалено.")
            await screen_boost_channels(query, context)
        elif action.startswith("boost"):
            await query.answer("Некорректное действие Boost.", show_alert=True)
        elif action == "adm_rsy_default":
            await action_admin_rsy_default_toggle(query, context)
        elif action == "adm_del_my_chans":
            await action_admin_delete_my_channels_confirm(query, context)
        elif action == "adm_del_my_chans_ok":
            await action_admin_delete_my_channels_ok(query, context)
        elif action == "adm_revoke" and len(parts) >= 3:
            await action_admin_revoke(query, context, parts[2])
        elif action == "adm_pro" and len(parts) >= 3:
            await action_admin_pro(query, context, parts[2])
        return

    # 🔒 Гард владельца (линчпин изоляции): если действие адресует конкретный канал
    # (parts[2] = @handle) или пост (draft-действия по post_id) — проверяем владение.
    # Пропуск хотя бы одного канало-зависимого действия = утечка в чужой канал.
    #
    # Основной предохранитель — это `_p2.startswith("@")` ниже: хэндлы каналов всегда
    # начинаются с «@», поэтому ЛЮБОЕ действие с handle в parts[2] гардится автоматически.
    # `_CHANNEL_ACTIONS` — явный список «на всякий случай» (читаемость + страховка, если
    # вдруг появится канало-действие с идентификатором БЕЗ «@»). НЕ полагайся на него как
    # на единственную защиту: забыть дописать сюда новый ch_* не страшно, пока handle с «@».
    # Действия с ДВУМЯ каналами (напр. ch_sched_copy_ok: dst в parts[2], src в parts[3])
    # этот общий гард прикрывает только по parts[2] — второй канал проверяй вручную (_owns).
    _POST_ACTIONS = {"draft_edit", "draft_media", "draft_ai", "draft_q", "draft_del", "draft_view"}
    _CHANNEL_ACTIONS = {
        "ch", "ch_settings", "ch_topic_redo", "ch_pause", "ch_delete", "ch_delete_ok",
        "ch_clear", "ch_clear_ok", "ch_create", "ch_generate", "ch_gen_run", "ch_postnow",
        "ch_review", "ch_schedule", "ch_sched_toggle", "ch_sched_clear",
        "ch_sched_days", "ch_sched_day", "ch_sched_daypreset",
        "ch_sched_custom", "ch_sched_copy", "ch_sched_copy_ok", "ch_images_toggle",
        "ch_history", "ch_set", "ch_set_img", "ch_folder", "ch_setfold",
        "ch_newfold", "ch_restore", "ch_draft", "draft_new", "draft_qlast",
        "draft_qall", "draft_qbatch", "draft_preview_batch", "draft_clear", "draft_clearok", "rsy_toggle",
        "ch_archetype", "ch_set_arche", "ch_source_toggle", "ch_src_mode",
        "ch_refs", "ref_tgl", "ref_del", "ref_add", "ref_take", "ref_go",
        "ref_import", "rss_del", "rss_clear", "rss_add", "rss_ai", "rss_ai_ok",
    }
    if len(parts) >= 3 and parts[2]:
        _p2 = parts[2]
        _ch = None
        if action in _POST_ACTIONS:
            _cid = buffer.get_post_channel(_p2)
            _ch = _load_channel(_cid) if _cid else None
        elif action in _CHANNEL_ACTIONS or _p2.startswith("@"):
            _ch = _load_channel(_p2)
        if _ch is not None and not _owns(uid, _ch):
            await query.answer("⛔ Это не твой канал.", show_alert=True)
            return

    # Любой клик по кнопке = навигация → сбрасываем «залипшие» режимы ожидания текста
    # (правка черновика/настроек/поиск). Нужный режим экран-обработчик ниже поставит
    # заново (он выполняется ПОСЛЕ этой очистки). Иначе старый режим перехватывал
    # чужой ввод (длина поста, добавление референса) — баги #7/#10/#11.
    for _k in ("draft_compose", "draft_edit", "draft_media", "channel_search", "editing"):
        context.user_data.pop(_k, None)

    if action == "main":
        context.user_data.pop("folder_bulk", None)
        await screen_main(query, context)

    elif action == "channels":
        context.user_data.pop("folder_bulk", None)
        # Вход из меню (без страницы) сбрасывает фильтр папки; пагинация — сохраняет
        if len(parts) < 3:
            context.user_data.pop("chfolder", None)
        page = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
        await screen_channels(query, context, page)

    elif action == "help":
        if len(parts) >= 3 and parts[2]:
            await screen_help_section(query, context, parts[2])
        else:
            await screen_help(query, context)

    elif action == "folders":
        context.user_data.pop("folder_bulk", None)
        await screen_folders(query, context)

    elif action == "fold_new":
        context.user_data["editing"] = {"field": "bulk_folder"}
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Папки", callback_data="ui:folders")]])
        await _answer_or_send(
            query,
            "➕ <b>Новая папка</b>\n\nНапиши название папки. Потом выберешь каналы, которые нужно туда добавить.",
            kb,
        )

    elif action == "fold_add" and len(parts) >= 3:
        folders = _folders(uid)
        idx = int(parts[2]) if parts[2].isdigit() else -1
        page = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else 0
        if 0 <= idx < len(folders):
            context.user_data["folder_bulk"] = {"folder": folders[idx], "selected": []}
            await screen_folder_bulk(query, context, page)
        else:
            await query.answer("Папка не найдена.", show_alert=True)

    elif action == "fold_bulk" and len(parts) >= 3:
        page = int(parts[2]) if parts[2].isdigit() else 0
        await screen_folder_bulk(query, context, page)

    elif action == "fold_pick" and len(parts) >= 4:
        page = int(parts[2]) if parts[2].isdigit() else 0
        idx = int(parts[3]) if parts[3].isdigit() else -1
        state = context.user_data.get("folder_bulk") or {}
        channels = _folder_bulk_channels(uid)
        if state.get("folder") and 0 <= idx < len(channels):
            cid = channels[idx]["channel_id"]
            selected = set(state.get("selected") or [])
            if cid in selected:
                selected.remove(cid)
            else:
                selected.add(cid)
            state["selected"] = sorted(selected)
            context.user_data["folder_bulk"] = state
        await screen_folder_bulk(query, context, page)

    elif action == "fold_reset":
        page = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
        state = context.user_data.get("folder_bulk") or {}
        if state:
            state["selected"] = []
            context.user_data["folder_bulk"] = state
        await screen_folder_bulk(query, context, page)

    elif action == "fold_apply":
        state = context.user_data.get("folder_bulk") or {}
        folder = (state.get("folder") or "").strip()
        selected = set(state.get("selected") or [])
        changed = 0
        if folder and selected:
            allowed = {c["channel_id"] for c in _folder_bulk_channels(uid)}
            for cid in sorted(selected & allowed):
                ch = _load_channel(cid)
                if not ch or not _owns(uid, ch):
                    continue
                ch["folder"] = folder
                _save_channel(ch)
                changed += 1
        context.user_data.pop("folder_bulk", None)
        await screen_folders(query, context)

    elif action == "chfold" and len(parts) >= 3:
        sel = parts[2]
        if sel == "all":
            context.user_data["chfolder"] = "__all__"
        elif sel == "none":
            context.user_data["chfolder"] = "__none__"
        else:
            folders = _folders(uid)
            idx = int(sel) if sel.isdigit() else -1
            if 0 <= idx < len(folders):
                context.user_data["chfolder"] = folders[idx]
            else:
                context.user_data.pop("chfolder", None)
        await screen_channels(query, context, 0)

    elif action == "ch_folder" and len(parts) >= 3:
        await screen_set_folder(query, context, parts[2])

    elif action == "ch_setfold" and len(parts) >= 4:
        handle = parts[2]
        ch = _load_channel(handle)
        if ch:
            sel = parts[3]
            if sel == "none":
                ch.pop("folder", None)
            else:
                folders = _folders(uid)
                idx = int(sel) if sel.isdigit() else -1
                if 0 <= idx < len(folders):
                    ch["folder"] = folders[idx]
            _save_channel(ch)
        await screen_set_folder(query, context, handle)

    elif action == "ch_newfold" and len(parts) >= 3:
        handle = parts[2]
        context.user_data["editing"] = {"handle": handle, "field": "folder"}
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data=f"ui:ch_folder:{handle}")]])
        await _answer_or_send(
            query,
            "➕ <b>Новая папка</b>\n\nНапиши название папки (напр. <i>Факты</i>, <i>Анеки</i>):",
            kb,
        )

    elif action == "ch_search":
        await prompt_channel_search(query, context)

    elif action == "ch_deleted":
        page = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
        await screen_channels_deleted(query, context, page)

    elif action == "ch_restore" and len(parts) >= 3:
        await action_channel_restore(query, context, parts[2])

    # ── Черновик ──
    elif action == "ch_draft" and len(parts) >= 3:
        await screen_drafts(query, context, parts[2])

    elif action == "draft_new" and len(parts) >= 3:
        await action_draft_new(query, context, parts[2])

    elif action == "draft_q" and len(parts) >= 3:
        await action_draft_queue(query, context, parts[2])

    elif action == "draft_qlast" and len(parts) >= 3:
        await action_draft_queue_last(query, context, parts[2])

    elif action == "draft_qall" and len(parts) >= 3:
        await action_draft_queue_all(query, context, parts[2])

    elif action == "draft_qbatch" and len(parts) >= 3:
        await action_draft_queue_batch(query, context, parts[2])

    elif action == "draft_preview_batch" and len(parts) >= 3:
        await action_draft_preview_batch(query, context, parts[2])

    elif action == "draft_clear" and len(parts) >= 3:
        await action_draft_clear_confirm(query, context, parts[2])

    elif action == "draft_clearok" and len(parts) >= 3:
        await action_draft_clear_ok(query, context, parts[2])

    elif action == "draft_del" and len(parts) >= 3:
        await action_draft_delete(query, context, parts[2])

    elif action == "draft_edit" and len(parts) >= 3:
        await action_draft_edit_text(query, context, parts[2])

    elif action == "draft_media" and len(parts) >= 3:
        await action_draft_edit_media(query, context, parts[2])

    elif action == "draft_ai" and len(parts) >= 3:
        await action_draft_ai_polish(query, context, parts[2])

    elif action == "draft_view" and len(parts) >= 3:
        await action_draft_view(query, context, parts[2])

    elif action == "status":
        await screen_status(query, context)

    elif action == "queue":
        await screen_queue(query, context)

    elif action == "queue_publish_all":
        await action_queue_publish_all_confirm(query, context)

    elif action == "queue_publish_all_ok":
        await action_queue_publish_all_run(query, context)

    elif action == "queue_clear_all":
        await action_clear_all_confirm(query, context)

    elif action == "queue_clear_all_ok":
        await action_clear_all_ok(query, context)

    elif action == "generate_all":
        if not accounts.is_superadmin(uid):
            await query.answer("Только для владельца.", show_alert=True)
            return
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

    elif action == "ch_create" and len(parts) >= 3:
        handle = parts[2]
        await screen_create_post(query, context, handle)

    elif action == "ch_settings" and len(parts) >= 3:
        handle = parts[2]
        await screen_channel_settings(query, context, handle)

    elif action == "ch_topic_redo" and len(parts) >= 3:
        handle = parts[2]
        await action_rederive_topic(query, context, handle)

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
        count = min(int(parts[3]), 10)  # максимум 10 за раз (15-20 — уже много)
        await action_generate_run(query, context, handle, count)

    elif action == "ch_postnow" and len(parts) >= 3:
        handle = parts[2]
        await action_postnow(query, context, handle)

    elif action == "ch_review" and len(parts) >= 3:
        handle = parts[2]
        context.user_data["review_channel"] = handle
        # Фокус-режим: показываем ОДИН пост (карточку 1/N) вместо вываливания списка.
        from bot import _send_post_card
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            await query.edit_message_reply_markup(reply_markup=None)
        await _send_post_card(query.message, handle, 0, context)

    elif action == "ch_schedule" and len(parts) >= 3:
        handle = parts[2]
        await screen_schedule(query, context, handle)

    elif action == "ch_sched_toggle" and len(parts) >= 4:
        handle   = parts[2]
        hour_msk = int(parts[3])
        await action_schedule_toggle(query, context, handle, hour_msk)

    elif action == "ch_sched_days" and len(parts) >= 3:
        handle = parts[2]
        await screen_schedule_days(query, context, handle)

    elif action == "ch_sched_day" and len(parts) >= 4:
        handle = parts[2]
        day = int(parts[3])
        await action_schedule_day_toggle(query, context, handle, day)

    elif action == "ch_sched_daypreset" and len(parts) >= 4:
        handle = parts[2]
        await action_schedule_day_preset(query, context, handle, parts[3])

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
            f"Напиши время или несколько слотов через пробел (МСК):\n"
            f"Например: <code>08:14</code>, <code>09:20</code> или <code>8 14 22</code>",
            reply_markup=kb, parse_mode=ParseMode.HTML,
        )

    elif action == "ch_sched_copy" and len(parts) >= 3:
        handle = parts[2]
        channels = _load_channels(owner_id=uid)
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
        if not ch_dst or not ch_src or not _owns(uid, ch_dst) or not _owns(uid, ch_src):
            await query.answer("⛔ Нет доступа к одному из каналов.", show_alert=True)
            return
        ch_dst["post_times_utc"] = ch_src.get("post_times_utc", [6, 9, 13, 17])
        ch_dst["schedule_days"] = _schedule_days(ch_src)
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
