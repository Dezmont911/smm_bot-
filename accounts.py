"""
accounts.py — SaaS-слой: пользователи (тестеры), инвайт-коды, планы, доступ.

Мягкая мультитенантность поверх одной БД. Изоляция данных держится на `owner_id`
в карточках каналов + `assert_owns` в каждом обработчике (см. bot.py/ui.py).

Роли:
  • superadmin — cfg.ADMIN_CHAT_ID (первый id): инвайты, «Расходы», /users.
  • admin      — любой из cfg.ADMIN_CHAT_IDS: полный доступ, без лимитов.
  • tester     — зарегистрирован по инвайту; план trial/free/pro.

Этот модуль НЕ решает enforcement лимитов (это Фаза 2, entitlements.py) — только
регистрацию, планы и доступ «пускать ли в бота».
"""

import secrets
from datetime import datetime, timezone, timedelta

from loguru import logger

from database import db
from config import cfg


# ============================================================
# Роли
# ============================================================

def is_admin(user_id: int) -> bool:
    """Полный админ (любой из ADMIN_CHAT_IDS) — без лимитов, видит свои каналы."""
    return user_id in cfg.ADMIN_CHAT_IDS


def is_superadmin(user_id: int) -> bool:
    """Главный владелец (первый id) — инвайты, расходы, глобальная статистика."""
    return user_id == cfg.ADMIN_CHAT_ID


# ============================================================
# Пользователи
# ============================================================

def get_user(user_id: int) -> dict | None:
    """Возвращает запись пользователя или None, если не зарегистрирован."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return dict(row) if row else None


def is_registered(user_id: int) -> bool:
    """Админ ИЛИ есть запись в users."""
    return is_admin(user_id) or get_user(user_id) is not None


def has_access(user_id: int) -> bool:
    """Пускать ли пользователя в бота вообще (админ или зарегистрированный тестер)."""
    return is_registered(user_id)


def effective_plan(user_id: int) -> str:
    """
    Действующий план с учётом истечения триала «на лету»:
      • админ → 'admin';
      • plan=='trial' и now > trial_until → 'free' (мягкий даунгрейд);
      • иначе — сохранённый план.
    """
    if is_admin(user_id):
        return "admin"
    u = get_user(user_id)
    if not u:
        return "none"
    plan = u.get("plan") or "trial"
    if plan == "trial" and u.get("trial_until"):
        try:
            if datetime.now(timezone.utc) > datetime.fromisoformat(u["trial_until"]):
                return "free"
        except Exception:
            pass
    return plan


def trial_days_left(user_id: int) -> int | None:
    """Сколько дней триала осталось (None, если не trial / нет даты)."""
    u = get_user(user_id)
    if not u or (u.get("plan") != "trial") or not u.get("trial_until"):
        return None
    try:
        delta = datetime.fromisoformat(u["trial_until"]) - datetime.now(timezone.utc)
        return max(0, delta.days)
    except Exception:
        return None


def register_user(user_id: int, plan: str, days: int, invited_by: str) -> dict:
    """Создаёт/обновляет запись пользователя (идемпотентно по user_id)."""
    now = datetime.now(timezone.utc)
    trial_until = (now + timedelta(days=days)).isoformat()
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO users (user_id, plan, trial_until, invited_by, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   plan=excluded.plan,
                   trial_until=excluded.trial_until,
                   invited_by=excluded.invited_by""",
            (user_id, plan, trial_until, invited_by, now.isoformat()),
        )
    logger.info(f"Пользователь {user_id} зарегистрирован: план={plan}, дней={days}, by={invited_by}")
    return get_user(user_id)


def set_plan(user_id: int, plan: str, days: int = 30) -> dict:
    """Ручная выдача плана (админ-команда /grant)."""
    return register_user(user_id, plan, days, invited_by="grant")


def revoke_user(user_id: int) -> bool:
    """Удаляет пользователя (доступ закрывается). Каналы НЕ трогаем."""
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    return cur.rowcount > 0


def list_users() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# ============================================================
# Инвайт-коды
# ============================================================

def gen_invite(plan: str = "trial", days: int = 30, max_uses: int = 1,
               created_by: int = 0) -> str:
    """Создаёт инвайт-код и возвращает его строку."""
    code = secrets.token_hex(4).upper()  # 8 hex-символов, напр. 'A3F9C201'
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO invite_codes (code, plan, days, max_uses, used_count,
                                         active, created_by, created_at)
               VALUES (?, ?, ?, ?, 0, 1, ?, ?)""",
            (code, plan, days, max_uses, created_by, now),
        )
    logger.info(f"Инвайт создан: {code} план={plan} дней={days} uses={max_uses}")
    return code


def redeem_invite(code: str, user_id: int) -> tuple[bool, str]:
    """
    Активирует код для пользователя. Возвращает (успех, сообщение).
    Атомарно: проверка лимита и инкремент used_count в одной транзакции.
    """
    code = (code or "").strip().upper()
    if not code:
        return False, "Пустой код."
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM invite_codes WHERE code = ?", (code,)
        ).fetchone()
        if not row:
            return False, "Код не найден."
        inv = dict(row)
        if not inv.get("active"):
            return False, "Код неактивен."
        if inv["used_count"] >= inv["max_uses"]:
            return False, "Код уже исчерпан."
        conn.execute(
            "UPDATE invite_codes SET used_count = used_count + 1 WHERE code = ?",
            (code,),
        )
    register_user(user_id, inv["plan"], inv["days"], invited_by=code)
    return True, f"Доступ открыт: план {inv['plan']} на {inv['days']} дней."
