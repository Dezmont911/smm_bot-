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

from config import cfg
from database import db


BOOST_PROVIDER = "twiboost"
BOOST_STATUS_DISABLED = "disabled"
BOOST_STATUS_DRY_RUN = "dry_run"
BOOST_STATUS_ENABLED = "enabled"

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
                post_url TEXT,
                quantity INTEGER NOT NULL,
                service_id TEXT,
                provider_order_id TEXT,
                status TEXT NOT NULL,
                dry_run INTEGER NOT NULL DEFAULT 1,
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
        conn.commit()


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


def add_tracked_channel(
    raw_channel: str,
    owner_id: int | None,
    quantity: int | None = None,
    service_id: int | str | None = None,
    title: str | None = None,
    note: str | None = None,
    enabled: bool = False,
    database=db,
    config=cfg,
) -> dict:
    ensure_boost_schema(database)
    normalized = normalize_channel_input(raw_channel)
    now = utc_now()
    qty = int(quantity or getattr(config, "BOOST_DEFAULT_QUANTITY", 500) or 500)
    svc = str(service_id or getattr(config, "TWIBOOST_VIEWS_SERVICE_ID", "") or "") or None
    with _connect(database) as conn:
        existing = conn.execute(
            "SELECT * FROM boost_channels WHERE channel_key = ?",
            (normalized["channel_key"],),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE boost_channels SET
                    owner_id = ?, tg_chat_id = ?, username = ?, title = COALESCE(?, title),
                    quantity = ?, service_id = ?, note = COALESCE(?, note),
                    updated_at = ?
                WHERE channel_key = ?
                """,
                (
                    owner_id,
                    normalized["tg_chat_id"],
                    normalized["username"],
                    title,
                    qty,
                    svc,
                    note,
                    now,
                    normalized["channel_key"],
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO boost_channels (
                    channel_key, owner_id, tg_chat_id, username, title, enabled,
                    quantity, service_id, note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized["channel_key"],
                    owner_id,
                    normalized["tg_chat_id"],
                    normalized["username"],
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
            "SELECT * FROM boost_channels WHERE channel_key = ?",
            (normalized["channel_key"],),
        ).fetchone()
    return _row_to_dict(row)


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
    keys = []
    if username:
        keys.append(f"user:{str(username).lstrip('@').lower()}")
    if chat_id is not None:
        keys.append(f"chat:{chat_id}")
    if not keys:
        return None
    with _connect(database) as conn:
        for key in keys:
            row = conn.execute("SELECT * FROM boost_channels WHERE channel_key = ?", (key,)).fetchone()
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
    if int(quantity) <= 0:
        raise ValueError("quantity_must_be_positive")
    ensure_boost_schema(database)
    with _connect(database) as conn:
        conn.execute(
            "UPDATE boost_channels SET quantity = ?, updated_at = ? WHERE id = ?",
            (int(quantity), utc_now(), int(channel_id)),
        )
        conn.commit()
    return get_tracked_channel(channel_id, database)


def delete_tracked_channel(channel_id: int, database=db) -> bool:
    ensure_boost_schema(database)
    with _connect(database) as conn:
        count = conn.execute("DELETE FROM boost_channels WHERE id = ?", (int(channel_id),)).rowcount
        conn.commit()
    return count > 0


def build_telegram_post_url(channel: dict | None, message_id: int | str | None) -> str | None:
    if not channel or not message_id:
        return None
    username = channel.get("username")
    if username:
        return f"https://t.me/{str(username).lstrip('@')}/{message_id}"
    chat_id = str(channel.get("tg_chat_id") or "")
    if chat_id.startswith("-100") and len(chat_id) > 4:
        return f"https://t.me/c/{chat_id[4:]}/{message_id}"
    return None


def create_boost_event(
    boost_channel: dict,
    message_id: int,
    post_url: str | None,
    quantity: int,
    service_id: int | str | None,
    status: str,
    dry_run: bool,
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
                boost_channel_id, tg_chat_id, message_id, post_url, quantity,
                service_id, provider_order_id, status, dry_run, error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(boost_channel["id"]),
                boost_channel.get("tg_chat_id"),
                int(message_id),
                post_url,
                int(quantity),
                str(service_id) if service_id is not None else None,
                str(provider_order_id) if provider_order_id is not None else None,
                status,
                _bool_int(dry_run),
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
    settings = get_boost_settings(database, config)
    if not settings.get("boost_enabled"):
        return {"status": "ignored", "reason": "boost_disabled"}

    chat = getattr(message, "chat", None)
    if chat is None:
        return {"status": "ignored", "reason": "no_chat"}
    channel = find_tracked_channel(
        chat_id=getattr(chat, "id", None),
        username=getattr(chat, "username", None),
        database=database,
    )
    if not channel:
        return {"status": "ignored", "reason": "not_tracked"}
    if not bool(channel.get("enabled")):
        return {"status": "ignored", "reason": "channel_disabled", "channel": channel}

    message_id = int(getattr(message, "message_id", 0) or 0)
    if not message_id:
        return {"status": "ignored", "reason": "no_message_id", "channel": channel}
    last_seen = int(channel.get("last_seen_message_id") or 0)
    if last_seen and message_id <= last_seen:
        return {"status": "ignored", "reason": "already_seen", "channel": channel}

    quantity = int(channel.get("quantity") or settings.get("default_quantity") or getattr(config, "BOOST_DEFAULT_QUANTITY", 500) or 500)
    service_id = channel.get("service_id") or settings.get("default_service_id") or getattr(config, "TWIBOOST_VIEWS_SERVICE_ID", None)
    post_url = build_telegram_post_url(channel, message_id)
    wrapper = client or TwiBoostClientWrapper(config=config)
    result = await wrapper.create_views_order(post_url or "", quantity, service_id, dry_run=True)
    event = create_boost_event(
        channel,
        message_id,
        post_url,
        quantity,
        service_id,
        status=BOOST_STATUS_DRY_RUN,
        dry_run=True,
        error=None if result.get("would_create_order") else result.get("error"),
        database=database,
    )
    return {"status": BOOST_STATUS_DRY_RUN, "event": event, "request": result}


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
