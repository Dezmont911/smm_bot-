"""Safe Boost subsystem foundation.

Boost is intentionally separate from ordinary generator channel cards. The
first slice supports admin-managed tracked channels and dry-run order events.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from config import cfg
from database import db


BOOST_PROVIDER = "twiboost"
BOOST_STATUS_DISABLED = "disabled"
BOOST_STATUS_DRY_RUN = "dry_run"
BOOST_STATUS_ENABLED = "enabled"
BOOST_STATUS_IGNORED = "ignored"
BOOST_EVENT_TYPE_MEDIA_GROUP = "media_group"
BOOST_EVENT_TYPE_POST = "post"
BOOST_EVENT_TYPE_TEXT = "text"
BOOST_EVENT_TYPE_PHOTO = "photo"
BOOST_EVENT_TYPE_VIDEO = "video"
BOOST_REASON_NO_PUBLIC_POST_URL = "no_public_post_url"
BOOST_REASON_PUBLIC_USERNAME = "public_username"
BOOST_REASON_GLOBAL_DISABLED = "boost_global_disabled"
BOOST_REASON_CHANNEL_DISABLED = "boost_channel_disabled"

REQUIRED_ENV_VARS = (
    "TWIBOOST_API_KEY",
    "TWIBOOST_API_URL",
    "TWIBOOST_VIEWS_SERVICE_ID",
    "BOOST_DEFAULT_QUANTITY",
    "BOOST_DRY_RUN",
    "BOOST_REAL_ORDERS_ENABLED",
)


@contextmanager
def _connect(database=db):
    conn = database.connect()
    try:
        yield conn
    finally:
        conn.close()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def required_env_vars() -> list[str]:
    return list(REQUIRED_ENV_VARS)


def ensure_boost_schema(database=db):
    with _connect(database) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS boost_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                boost_enabled INTEGER NOT NULL DEFAULT 0,
                boost_dry_run INTEGER NOT NULL DEFAULT 1,
                real_orders_enabled INTEGER NOT NULL DEFAULT 0,
                default_quantity INTEGER NOT NULL DEFAULT 500,
                default_service_id TEXT,
                provider TEXT NOT NULL DEFAULT 'twiboost',
                last_error TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS boost_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_key TEXT UNIQUE NOT NULL,
                smm_channel_id TEXT,
                owner_id INTEGER,
                tg_chat_id TEXT,
                username TEXT,
                title TEXT,
                enabled INTEGER NOT NULL DEFAULT 0,
                quantity INTEGER,
                service_id TEXT,
                note TEXT,
                last_seen_message_id INTEGER,
                last_order_id TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS boost_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                boost_channel_id INTEGER NOT NULL,
                tg_chat_id TEXT,
                message_id INTEGER NOT NULL,
                event_key TEXT,
                media_group_id TEXT,
                canonical_message_id INTEGER,
                event_type TEXT NOT NULL DEFAULT 'post',
                post_url TEXT,
                quantity INTEGER NOT NULL,
                service_id TEXT,
                provider_order_id TEXT,
                status TEXT NOT NULL,
                dry_run INTEGER NOT NULL DEFAULT 1,
                reason_code TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (boost_channel_id) REFERENCES boost_channels(id)
            );

            CREATE INDEX IF NOT EXISTS idx_boost_channels_enabled
                ON boost_channels(enabled);
            CREATE INDEX IF NOT EXISTS idx_boost_orders_channel
                ON boost_orders(boost_channel_id, message_id);
            """
        )
        _ensure_boost_order_columns(conn)
        conn.commit()


def _ensure_boost_order_columns(conn):
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(boost_orders)").fetchall()}
    required = {
        "event_key": "TEXT",
        "media_group_id": "TEXT",
        "canonical_message_id": "INTEGER",
        "event_type": "TEXT NOT NULL DEFAULT 'post'",
        "reason_code": "TEXT",
    }
    for name, definition in required.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE boost_orders ADD COLUMN {name} {definition}")
    conn.execute(
        """
        UPDATE boost_orders
        SET
            canonical_message_id = COALESCE(canonical_message_id, message_id),
            event_type = COALESCE(NULLIF(event_type, ''), 'post'),
            event_key = COALESCE(NULLIF(event_key, ''), 'msg:' || message_id)
        WHERE canonical_message_id IS NULL
           OR event_type IS NULL
           OR event_type = ''
           OR event_key IS NULL
           OR event_key = ''
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_boost_orders_unique_event
            ON boost_orders(boost_channel_id, event_key, COALESCE(service_id, ''))
        """
    )
    channel_columns = {row["name"] for row in conn.execute("PRAGMA table_info(boost_channels)").fetchall()}
    if "smm_channel_id" not in channel_columns:
        conn.execute("ALTER TABLE boost_channels ADD COLUMN smm_channel_id TEXT")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_boost_channels_smm_channel
            ON boost_channels(smm_channel_id)
        """
    )


def _row_to_dict(row) -> dict | None:
    return dict(row) if row is not None else None


def _bool_int(value: Any) -> int:
    return 1 if bool(value) else 0


def _default_settings(config=cfg) -> dict:
    return {
        "boost_enabled": False,
        "boost_dry_run": bool(getattr(config, "BOOST_DRY_RUN", True)),
        "real_orders_enabled": bool(getattr(config, "BOOST_REAL_ORDERS_ENABLED", False)),
        "default_quantity": int(getattr(config, "BOOST_DEFAULT_QUANTITY", 500) or 500),
        "default_service_id": str(getattr(config, "TWIBOOST_VIEWS_SERVICE_ID", "") or "") or None,
        "provider": BOOST_PROVIDER,
        "last_error": None,
        "updated_at": None,
    }


def _settings_from_row(row, config=cfg) -> dict:
    if row is None:
        return _default_settings(config)
    data = dict(row)
    data["boost_enabled"] = bool(data.get("boost_enabled"))
    data["boost_dry_run"] = bool(data.get("boost_dry_run"))
    data["real_orders_enabled"] = bool(data.get("real_orders_enabled"))
    data["default_quantity"] = int(data.get("default_quantity") or getattr(config, "BOOST_DEFAULT_QUANTITY", 500) or 500)
    data["provider"] = data.get("provider") or BOOST_PROVIDER
    return data


def get_boost_settings(database=db, config=cfg) -> dict:
    ensure_boost_schema(database)
    with _connect(database) as conn:
        row = conn.execute("SELECT * FROM boost_settings WHERE id = 1").fetchone()
    return _settings_from_row(row, config)


def save_boost_settings(updates: dict, database=db, config=cfg) -> dict:
    ensure_boost_schema(database)
    current = get_boost_settings(database, config)
    data = {**current, **updates}
    data["updated_at"] = utc_now()
    with _connect(database) as conn:
        conn.execute(
            """
            INSERT INTO boost_settings (
                id, boost_enabled, boost_dry_run, real_orders_enabled,
                default_quantity, default_service_id, provider, last_error, updated_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                boost_enabled = excluded.boost_enabled,
                boost_dry_run = excluded.boost_dry_run,
                real_orders_enabled = excluded.real_orders_enabled,
                default_quantity = excluded.default_quantity,
                default_service_id = excluded.default_service_id,
                provider = excluded.provider,
                last_error = excluded.last_error,
                updated_at = excluded.updated_at
            """,
            (
                _bool_int(data["boost_enabled"]),
                _bool_int(data["boost_dry_run"]),
                _bool_int(data["real_orders_enabled"]),
                int(data["default_quantity"] or 500),
                data.get("default_service_id"),
                data.get("provider") or BOOST_PROVIDER,
                data.get("last_error"),
                data["updated_at"],
            ),
        )
        conn.commit()
    return get_boost_settings(database, config)


def set_boost_enabled(enabled: bool, database=db, config=cfg) -> dict:
    return save_boost_settings({"boost_enabled": bool(enabled), "last_error": None}, database, config)


def boost_configured(config=cfg) -> bool:
    return bool(
        getattr(config, "TWIBOOST_API_KEY", "")
        and getattr(config, "TWIBOOST_API_URL", "")
        and int(getattr(config, "TWIBOOST_VIEWS_SERVICE_ID", 0) or 0) > 0
    )


def boost_real_orders_allowed(settings: dict | None = None, config=cfg) -> bool:
    settings = settings or get_boost_settings(config=config)
    return (
        bool(settings.get("boost_enabled"))
        and not bool(settings.get("boost_dry_run", True))
        and bool(settings.get("real_orders_enabled", False))
        and boost_configured(config)
    )


def boost_status(settings: dict | None = None, config=cfg) -> str:
    settings = settings or get_boost_settings(config=config)
    if not settings.get("boost_enabled"):
        return BOOST_STATUS_DISABLED
    if boost_real_orders_allowed(settings, config):
        return BOOST_STATUS_ENABLED
    return BOOST_STATUS_DRY_RUN


def normalize_channel_input(raw: str) -> dict:
    value = str(raw or "").strip()
    if not value:
        raise ValueError("empty_channel")

    value = value.replace("https://", "").replace("http://", "")
    if value.startswith("t.me/"):
        value = value.split("t.me/", 1)[1]
    value = value.strip().strip("/")
    value = value.split("?", 1)[0]
    value = value.split("/", 1)[0]

    if re.fullmatch(r"-?\d+", value):
        return {"channel_key": f"chat:{value}", "tg_chat_id": value, "username": None}

    value = value.lstrip("@").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{4,64}", value):
        raise ValueError("invalid_channel")
    username = value.lower()
    return {"channel_key": f"user:{username}", "tg_chat_id": None, "username": username}


def _normalize_username_value(value: str | None) -> str | None:
    if not value:
        return None
    value = str(value).strip().lstrip("@").lower()
    if not re.fullmatch(r"[a-z0-9_]{4,64}", value):
        return None
    return value


def _normalize_chat_id_value(value: int | str | None) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    if not re.fullmatch(r"-?\d+", value):
        return None
    return value


def validate_boost_quantity(value: int | str, config=cfg) -> int:
    try:
        quantity = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("quantity_must_be_integer")
    max_quantity = int(getattr(config, "BOOST_MAX_QUANTITY", 100000) or 100000)
    if quantity <= 0:
        raise ValueError("quantity_must_be_positive")
    if quantity > max_quantity:
        raise ValueError("quantity_too_large")
    return quantity


def _smm_channel_identifier(channel: dict) -> str:
    chat_id = channel.get("chat_id_num")
    if chat_id:
        return str(chat_id)
    username = channel.get("username") or channel.get("channel_id")
    if username:
        return str(username)
    raise ValueError("smm_channel_missing_identifier")


def _smm_channel_username(channel: dict) -> str | None:
    value = channel.get("username") or channel.get("channel_id")
    return _normalize_username_value(value)


def _smm_channel_chat_id(channel: dict) -> str | None:
    chat_id = channel.get("chat_id_num")
    return _normalize_chat_id_value(chat_id)


def _find_existing_for_identity(
    conn,
    channel_key: str | None = None,
    smm_channel_id: str | None = None,
    tg_chat_id: int | str | None = None,
    username: str | None = None,
):
    if smm_channel_id:
        row = conn.execute(
            "SELECT * FROM boost_channels WHERE smm_channel_id = ?",
            (str(smm_channel_id),),
        ).fetchone()
        if row:
            return row

    if channel_key:
        row = conn.execute("SELECT * FROM boost_channels WHERE channel_key = ?", (channel_key,)).fetchone()
        if row:
            return row

    normalized_chat_id = _normalize_chat_id_value(tg_chat_id)
    if normalized_chat_id:
        for query, value in (
            ("SELECT * FROM boost_channels WHERE tg_chat_id = ?", normalized_chat_id),
            ("SELECT * FROM boost_channels WHERE channel_key = ?", f"chat:{normalized_chat_id}"),
        ):
            row = conn.execute(query, (value,)).fetchone()
            if row:
                return row

    normalized_username = _normalize_username_value(username)
    if normalized_username:
        for query, value in (
            ("SELECT * FROM boost_channels WHERE username = ?", normalized_username),
            ("SELECT * FROM boost_channels WHERE channel_key = ?", f"user:{normalized_username}"),
        ):
            row = conn.execute(query, (value,)).fetchone()
            if row:
                return row

    return None


def _find_existing_for_smm_channel(conn, channel: dict):
    smm_channel_id = channel.get("channel_id")
    chat_id = _smm_channel_chat_id(channel)
    username = _smm_channel_username(channel)
    primary_key = f"chat:{chat_id}" if chat_id else f"user:{username}" if username else None
    return _find_existing_for_identity(
        conn,
        channel_key=primary_key,
        smm_channel_id=str(smm_channel_id) if smm_channel_id else None,
        tg_chat_id=chat_id,
        username=username,
    )


def find_tracked_channel_for_smm_channel(smm_channel: dict, database=db) -> dict | None:
    ensure_boost_schema(database)
    with _connect(database) as conn:
        row = _find_existing_for_smm_channel(conn, smm_channel)
    return _row_to_dict(row)


def find_tracked_channel_for_input(
    raw_channel: str,
    smm_channel_id: str | None = None,
    snapshot_username: str | None = None,
    snapshot_tg_chat_id: int | str | None = None,
    database=db,
) -> dict | None:
    ensure_boost_schema(database)
    normalized = normalize_channel_input(raw_channel)
    username = _normalize_username_value(snapshot_username or normalized["username"])
    tg_chat_id = _normalize_chat_id_value(snapshot_tg_chat_id if snapshot_tg_chat_id is not None else normalized["tg_chat_id"])
    with _connect(database) as conn:
        row = _find_existing_for_identity(
            conn,
            channel_key=normalized["channel_key"],
            smm_channel_id=smm_channel_id,
            tg_chat_id=tg_chat_id,
            username=username,
        )
    return _row_to_dict(row)


def link_tracked_channel_to_smm_channel(
    boost_channel_id: int,
    smm_channel: dict,
    owner_id: int | None,
    database=db,
) -> dict | None:
    ensure_boost_schema(database)
    now = utc_now()
    with _connect(database) as conn:
        conn.execute(
            """
            UPDATE boost_channels SET
                smm_channel_id = COALESCE(smm_channel_id, ?),
                owner_id = COALESCE(owner_id, ?),
                tg_chat_id = COALESCE(?, tg_chat_id),
                username = COALESCE(?, username),
                title = COALESCE(?, title),
                updated_at = ?
            WHERE id = ?
            """,
            (
                smm_channel.get("channel_id"),
                owner_id,
                _smm_channel_chat_id(smm_channel),
                _smm_channel_username(smm_channel),
                smm_channel.get("name") or smm_channel.get("title"),
                now,
                int(boost_channel_id),
            ),
        )
        conn.commit()
    return get_tracked_channel(boost_channel_id, database)


def add_tracked_channel(
    raw_channel: str,
    owner_id: int | None,
    quantity: int | None = None,
    service_id: int | str | None = None,
    title: str | None = None,
    note: str | None = None,
    enabled: bool = False,
    smm_channel_id: str | None = None,
    snapshot_username: str | None = None,
    snapshot_tg_chat_id: int | str | None = None,
    database=db,
    config=cfg,
) -> dict:
    ensure_boost_schema(database)
    normalized = normalize_channel_input(raw_channel)
    now = utc_now()
    qty = validate_boost_quantity(quantity if quantity is not None else getattr(config, "BOOST_DEFAULT_QUANTITY", 500) or 500, config)
    svc = str(service_id or getattr(config, "TWIBOOST_VIEWS_SERVICE_ID", "") or "") or None
    tg_chat_id = _normalize_chat_id_value(snapshot_tg_chat_id if snapshot_tg_chat_id is not None else normalized["tg_chat_id"])
    username = _normalize_username_value(snapshot_username or normalized["username"])
    with _connect(database) as conn:
        existing = _find_existing_for_identity(
            conn,
            channel_key=normalized["channel_key"],
            smm_channel_id=smm_channel_id,
            tg_chat_id=tg_chat_id,
            username=username,
        )
        if existing:
            conn.execute(
                """
                UPDATE boost_channels SET
                    smm_channel_id = COALESCE(?, smm_channel_id),
                    owner_id = COALESCE(owner_id, ?),
                    tg_chat_id = COALESCE(?, tg_chat_id),
                    username = COALESCE(?, username),
                    title = COALESCE(?, title),
                    quantity = CASE WHEN ? THEN ? ELSE quantity END,
                    service_id = COALESCE(?, service_id),
                    note = COALESCE(?, note),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    smm_channel_id,
                    owner_id,
                    tg_chat_id,
                    username,
                    title,
                    1 if quantity is not None else 0,
                    qty,
                    svc,
                    note,
                    now,
                    int(existing["id"]),
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO boost_channels (
                    channel_key, smm_channel_id, owner_id, tg_chat_id, username, title, enabled,
                    quantity, service_id, note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized["channel_key"],
                    smm_channel_id,
                    owner_id,
                    tg_chat_id,
                    username,
                    title,
                    _bool_int(enabled),
                    qty,
                    svc,
                    note,
                    now,
                    now,
                ),
            )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM boost_channels WHERE id = ?",
            (int(existing["id"]) if existing else int(conn.execute("SELECT last_insert_rowid()").fetchone()[0]),),
        ).fetchone()
    return _row_to_dict(row)


def add_tracked_channel_from_smm_channel(
    smm_channel: dict,
    owner_id: int | None,
    quantity: int | str,
    service_id: int | str | None = None,
    enabled: bool = False,
    database=db,
    config=cfg,
) -> tuple[dict, bool]:
    ensure_boost_schema(database)
    quantity = validate_boost_quantity(quantity, config)
    with _connect(database) as conn:
        existing = _find_existing_for_smm_channel(conn, smm_channel)
    if existing:
        linked = link_tracked_channel_to_smm_channel(existing["id"], smm_channel, owner_id, database)
        return linked or _row_to_dict(existing), False

    raw_channel = _smm_channel_identifier(smm_channel)
    channel = add_tracked_channel(
        raw_channel,
        owner_id=owner_id,
        quantity=quantity,
        service_id=service_id,
        title=smm_channel.get("name") or smm_channel.get("title"),
        note="linked_smm_channel",
        enabled=enabled,
        smm_channel_id=smm_channel.get("channel_id"),
        snapshot_username=_smm_channel_username(smm_channel),
        snapshot_tg_chat_id=_smm_channel_chat_id(smm_channel),
        database=database,
        config=config,
    )
    return channel, True


def list_tracked_channels(database=db, include_disabled: bool = True) -> list[dict]:
    ensure_boost_schema(database)
    where = "" if include_disabled else "WHERE enabled = 1"
    with _connect(database) as conn:
        rows = conn.execute(
            f"SELECT * FROM boost_channels {where} ORDER BY id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_tracked_channel(channel_id: int, database=db) -> dict | None:
    ensure_boost_schema(database)
    with _connect(database) as conn:
        row = conn.execute("SELECT * FROM boost_channels WHERE id = ?", (int(channel_id),)).fetchone()
    return _row_to_dict(row)


def find_tracked_channel(chat_id: int | str | None = None, username: str | None = None, database=db) -> dict | None:
    ensure_boost_schema(database)
    username = _normalize_username_value(username)
    chat_id = _normalize_chat_id_value(chat_id)
    if not username and not chat_id:
        return None
    with _connect(database) as conn:
        row = _find_existing_for_identity(
            conn,
            channel_key=f"user:{username}" if username else f"chat:{chat_id}" if chat_id else None,
            tg_chat_id=chat_id,
            username=username,
        )
        if row:
            return dict(row)
    return None


def set_tracked_channel_enabled(channel_id: int, enabled: bool, database=db) -> dict | None:
    ensure_boost_schema(database)
    with _connect(database) as conn:
        conn.execute(
            "UPDATE boost_channels SET enabled = ?, updated_at = ? WHERE id = ?",
            (_bool_int(enabled), utc_now(), int(channel_id)),
        )
        conn.commit()
    return get_tracked_channel(channel_id, database)


def set_tracked_channel_quantity(channel_id: int, quantity: int, database=db) -> dict | None:
    quantity = validate_boost_quantity(quantity)
    ensure_boost_schema(database)
    with _connect(database) as conn:
        conn.execute(
            "UPDATE boost_channels SET quantity = ?, updated_at = ? WHERE id = ?",
            (quantity, utc_now(), int(channel_id)),
        )
        conn.commit()
    return get_tracked_channel(channel_id, database)


def delete_tracked_channel(channel_id: int, database=db) -> bool:
    ensure_boost_schema(database)
    with _connect(database) as conn:
        count = conn.execute("DELETE FROM boost_channels WHERE id = ?", (int(channel_id),)).rowcount
        conn.commit()
    return count > 0


def _message_value(message, attr: str, default=None):
    return getattr(message, attr, default)


def build_boost_event_key(message) -> str | None:
    message_id = int(_message_value(message, "message_id", 0) or 0)
    if not message_id:
        return None
    media_group_id = _message_value(message, "media_group_id", None)
    if media_group_id:
        return f"mg:{media_group_id}"
    return f"msg:{message_id}"


def infer_boost_event_type(message) -> str:
    if _message_value(message, "media_group_id", None):
        return BOOST_EVENT_TYPE_MEDIA_GROUP
    if _message_value(message, "video", None):
        return BOOST_EVENT_TYPE_VIDEO
    if _message_value(message, "photo", None):
        return BOOST_EVENT_TYPE_PHOTO
    return BOOST_EVENT_TYPE_TEXT


def build_telegram_post_url(channel: dict | None, message) -> dict:
    message_id = int(_message_value(message, "message_id", 0) or 0)
    chat = _message_value(message, "chat", None)
    chat_id = _message_value(chat, "id", None) if chat is not None else None
    username = _message_value(chat, "username", None) if chat is not None else None

    if channel:
        chat_id = chat_id if chat_id is not None else channel.get("tg_chat_id")
        username = username or channel.get("username")

    username = str(username).lstrip("@").strip() if username else ""
    result = {
        "ok": False,
        "post_url": None,
        "reason_code": BOOST_REASON_NO_PUBLIC_POST_URL,
        "canonical_message_id": message_id or None,
        "chat_id": str(chat_id) if chat_id is not None else None,
        "username": username or None,
        "is_public": False,
    }

    if not channel or not message_id:
        result["reason_code"] = "missing_channel_or_message_id"
        return result

    if username:
        result.update(
            {
                "ok": True,
                "post_url": f"https://t.me/{username}/{message_id}",
                "reason_code": BOOST_REASON_PUBLIC_USERNAME,
                "is_public": True,
            }
        )
    return result


def get_boost_event_by_key(
    boost_channel_id: int,
    event_key: str,
    service_id: int | str | None,
    database=db,
) -> dict | None:
    ensure_boost_schema(database)
    with _connect(database) as conn:
        row = conn.execute(
            """
            SELECT * FROM boost_orders
            WHERE boost_channel_id = ?
              AND event_key = ?
              AND COALESCE(service_id, '') = COALESCE(?, '')
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(boost_channel_id), event_key, str(service_id) if service_id is not None else None),
        ).fetchone()
    return _row_to_dict(row)


def list_boost_events(
    limit: int = 10,
    boost_channel_id: int | None = None,
    database=db,
) -> list[dict]:
    ensure_boost_schema(database)
    limit = max(1, min(int(limit or 10), 100))
    params: list[Any] = []
    where = ""
    if boost_channel_id is not None:
        where = "WHERE o.boost_channel_id = ?"
        params.append(int(boost_channel_id))
    params.append(limit)
    with _connect(database) as conn:
        rows = conn.execute(
            f"""
            SELECT
                o.*,
                c.channel_key,
                c.smm_channel_id,
                c.owner_id,
                c.username AS channel_username,
                c.tg_chat_id AS channel_tg_chat_id,
                c.title AS channel_title,
                c.enabled AS channel_enabled
            FROM boost_orders o
            LEFT JOIN boost_channels c ON c.id = o.boost_channel_id
            {where}
            ORDER BY o.id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def _log_boost_result(
    status: str,
    reason: str | None,
    message=None,
    channel: dict | None = None,
    event: dict | None = None,
    event_key: str | None = None,
    event_type: str | None = None,
    settings: dict | None = None,
):
    chat = getattr(message, "chat", None) if message is not None else None
    logger.info(
        "Boost event | status={} reason={} boost_channel_id={} smm_channel_id={} "
        "username={} chat_id={} message_id={} media_group_id={} event_key={} event_type={} "
        "enabled={} global_enabled={} dry_run={} event_id={}",
        status,
        reason,
        channel.get("id") if channel else None,
        channel.get("smm_channel_id") if channel else None,
        (channel.get("username") if channel else None) or (getattr(chat, "username", None) if chat else None),
        (channel.get("tg_chat_id") if channel else None) or (getattr(chat, "id", None) if chat else None),
        getattr(message, "message_id", None) if message is not None else None,
        getattr(message, "media_group_id", None) if message is not None else None,
        event_key,
        event_type,
        bool(channel.get("enabled")) if channel else None,
        bool(settings.get("boost_enabled")) if settings else None,
        bool(settings.get("boost_dry_run", True)) if settings else True,
        event.get("id") if event else None,
    )


def create_boost_event(
    boost_channel: dict,
    message_id: int,
    post_url: str | None,
    quantity: int,
    service_id: int | str | None,
    status: str,
    dry_run: bool,
    event_key: str | None = None,
    media_group_id: str | None = None,
    canonical_message_id: int | None = None,
    event_type: str | None = None,
    reason_code: str | None = None,
    provider_order_id: int | str | None = None,
    error: str | None = None,
    database=db,
) -> dict:
    ensure_boost_schema(database)
    now = utc_now()
    with _connect(database) as conn:
        cur = conn.execute(
            """
            INSERT INTO boost_orders (
                boost_channel_id, tg_chat_id, message_id, event_key, media_group_id,
                canonical_message_id, event_type, post_url, quantity, service_id,
                provider_order_id, status, dry_run, reason_code, error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(boost_channel["id"]),
                boost_channel.get("tg_chat_id"),
                int(message_id),
                event_key or f"msg:{int(message_id)}",
                str(media_group_id) if media_group_id is not None else None,
                int(canonical_message_id or message_id),
                event_type or BOOST_EVENT_TYPE_POST,
                post_url,
                int(quantity),
                str(service_id) if service_id is not None else None,
                str(provider_order_id) if provider_order_id is not None else None,
                status,
                _bool_int(dry_run),
                reason_code,
                error,
                now,
                now,
            ),
        )
        conn.execute(
            """
            UPDATE boost_channels SET
                last_seen_message_id = ?,
                last_order_id = COALESCE(?, last_order_id),
                last_error = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                int(message_id),
                str(provider_order_id) if provider_order_id is not None else None,
                error,
                now,
                int(boost_channel["id"]),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM boost_orders WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


async def handle_boost_channel_post_dry_run(message, database=db, config=cfg, client=None) -> dict:
    """Prepare a dry-run event for a tracked channel post; never sends real orders."""
    chat = getattr(message, "chat", None)
    if chat is None:
        _log_boost_result("ignored", "no_chat", message=message)
        return {"status": "ignored", "reason": "no_chat"}

    channel = find_tracked_channel(
        chat_id=getattr(chat, "id", None),
        username=getattr(chat, "username", None),
        database=database,
    )
    if not channel:
        _log_boost_result("ignored", "not_tracked", message=message)
        return {"status": "ignored", "reason": "not_tracked"}

    message_id = int(getattr(message, "message_id", 0) or 0)
    if not message_id:
        _log_boost_result("ignored", "no_message_id", message=message, channel=channel)
        return {"status": "ignored", "reason": "no_message_id", "channel": channel}

    settings = get_boost_settings(database, config)
    quantity = int(channel.get("quantity") or settings.get("default_quantity") or getattr(config, "BOOST_DEFAULT_QUANTITY", 500) or 500)
    service_id = channel.get("service_id") or settings.get("default_service_id") or getattr(config, "TWIBOOST_VIEWS_SERVICE_ID", None)
    event_key = build_boost_event_key(message)
    event_type = infer_boost_event_type(message)
    media_group_id = getattr(message, "media_group_id", None)
    if not event_key:
        _log_boost_result("ignored", "no_event_key", message=message, channel=channel, event_type=event_type, settings=settings)
        return {"status": "ignored", "reason": "no_event_key", "channel": channel}

    existing = get_boost_event_by_key(channel["id"], event_key, service_id, database)
    if existing:
        _log_boost_result("duplicate", "already_has_event", message=message, channel=channel, event=existing, event_key=event_key, event_type=event_type, settings=settings)
        return {"status": "duplicate", "reason": "already_has_event", "channel": channel, "event": existing}

    if not settings.get("boost_enabled"):
        event = create_boost_event(
            channel,
            message_id,
            None,
            quantity,
            service_id,
            status=BOOST_STATUS_IGNORED,
            dry_run=True,
            event_key=event_key,
            media_group_id=str(media_group_id) if media_group_id is not None else None,
            canonical_message_id=message_id,
            event_type=event_type,
            reason_code=BOOST_REASON_GLOBAL_DISABLED,
            error=BOOST_REASON_GLOBAL_DISABLED,
            database=database,
        )
        _log_boost_result(BOOST_STATUS_IGNORED, BOOST_REASON_GLOBAL_DISABLED, message=message, channel=channel, event=event, event_key=event_key, event_type=event_type, settings=settings)
        return {"status": BOOST_STATUS_IGNORED, "reason": BOOST_REASON_GLOBAL_DISABLED, "channel": channel, "event": event}

    if not bool(channel.get("enabled")):
        event = create_boost_event(
            channel,
            message_id,
            None,
            quantity,
            service_id,
            status=BOOST_STATUS_IGNORED,
            dry_run=True,
            event_key=event_key,
            media_group_id=str(media_group_id) if media_group_id is not None else None,
            canonical_message_id=message_id,
            event_type=event_type,
            reason_code=BOOST_REASON_CHANNEL_DISABLED,
            error=BOOST_REASON_CHANNEL_DISABLED,
            database=database,
        )
        _log_boost_result(BOOST_STATUS_IGNORED, BOOST_REASON_CHANNEL_DISABLED, message=message, channel=channel, event=event, event_key=event_key, event_type=event_type, settings=settings)
        return {"status": BOOST_STATUS_IGNORED, "reason": BOOST_REASON_CHANNEL_DISABLED, "channel": channel, "event": event}

    post_url_result = build_telegram_post_url(channel, message)

    if not post_url_result["ok"]:
        event = create_boost_event(
            channel,
            message_id,
            post_url_result["post_url"],
            quantity,
            service_id,
            status=BOOST_STATUS_DRY_RUN,
            dry_run=True,
            event_key=event_key,
            media_group_id=str(media_group_id) if media_group_id is not None else None,
            canonical_message_id=post_url_result["canonical_message_id"] or message_id,
            event_type=event_type,
            reason_code=post_url_result["reason_code"],
            error=post_url_result["reason_code"],
            database=database,
        )
        _log_boost_result(BOOST_STATUS_DRY_RUN, post_url_result["reason_code"], message=message, channel=channel, event=event, event_key=event_key, event_type=event_type, settings=settings)
        return {
            "status": BOOST_STATUS_DRY_RUN,
            "reason": post_url_result["reason_code"],
            "event": event,
            "url": post_url_result,
        }

    wrapper = client or TwiBoostClientWrapper(config=config)
    result = await wrapper.create_views_order(post_url_result["post_url"] or "", quantity, service_id, dry_run=True)
    event = create_boost_event(
        channel,
        message_id,
        post_url_result["post_url"],
        quantity,
        service_id,
        status=BOOST_STATUS_DRY_RUN,
        dry_run=True,
        event_key=event_key,
        media_group_id=str(media_group_id) if media_group_id is not None else None,
        canonical_message_id=post_url_result["canonical_message_id"] or message_id,
        event_type=event_type,
        reason_code=post_url_result["reason_code"],
        error=None if result.get("would_create_order") else result.get("error"),
        database=database,
    )
    _log_boost_result(BOOST_STATUS_DRY_RUN, post_url_result["reason_code"], message=message, channel=channel, event=event, event_key=event_key, event_type=event_type, settings=settings)
    return {"status": BOOST_STATUS_DRY_RUN, "event": event, "request": result, "url": post_url_result}


@dataclass
class TwiBoostClientWrapper:
    api_key: str = ""
    api_url: str = ""
    service_id: int = 0
    timeout: int = 30

    def __init__(
        self,
        api_key: str | None = None,
        api_url: str | None = None,
        service_id: int | None = None,
        timeout: int = 30,
        config=cfg,
    ):
        self.config = config
        self.api_key = api_key if api_key is not None else getattr(config, "TWIBOOST_API_KEY", "")
        self.api_url = api_url if api_url is not None else getattr(config, "TWIBOOST_API_URL", "")
        self.service_id = int(service_id if service_id is not None else getattr(config, "TWIBOOST_VIEWS_SERVICE_ID", 0) or 0)
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_url and self.service_id > 0)

    def _request_sync(self, params: dict[str, Any]) -> dict:
        if not self.api_key or not self.api_url:
            return {"error": "twiboost_not_configured", "configured": False}
        try:
            import requests

            payload = dict(params)
            payload["key"] = self.api_key
            response = requests.get(self.api_url, params=payload, timeout=self.timeout)
            try:
                data = response.json()
            except Exception:
                return {"error": f"invalid_json_response: {response.text[:200]}"}
            if not response.ok and isinstance(data, dict) and "error" not in data:
                data["error"] = f"HTTP {response.status_code}: {data}"
            return data if isinstance(data, dict) else {"error": "invalid_response", "response": data}
        except Exception as exc:
            return {"error": str(exc)}

    async def _request(self, params: dict[str, Any]) -> dict:
        return await asyncio.to_thread(self._request_sync, params)

    async def get_balance(self) -> dict:
        return await self._request({"action": "balance"})

    async def create_views_order(
        self,
        post_url: str,
        quantity: int | None = None,
        service_id: int | str | None = None,
        dry_run: bool = True,
    ) -> dict:
        qty = int(quantity or getattr(self.config, "BOOST_DEFAULT_QUANTITY", 500) or 500)
        svc = int(service_id or self.service_id or 0)
        payload = {"action": "add", "service": svc, "link": post_url, "quantity": qty}

        if dry_run:
            return {
                "dry_run": True,
                "would_create_order": True,
                "configured": self.configured,
                "request": payload,
            }
        if not self.configured or svc <= 0:
            return {"error": "twiboost_not_configured", "configured": False}
        return await self._request(payload)

    async def get_order_status(self, order_id: int | str) -> dict:
        return await self._request({"action": "status", "order": order_id})

    async def cancel_order(self, order_id: int | str) -> dict:
        return await self._request({"action": "cancel", "order": order_id})
