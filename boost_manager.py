"""Safe TwiBoost integration helpers.

This module is intentionally not wired into publishing. It only stores settings,
computes effective boost state, and exposes a dry-run-first TwiBoost client.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from config import cfg


BOOST_OVERRIDE_INHERIT = "inherit"
BOOST_OVERRIDE_ON = "on"
BOOST_OVERRIDE_OFF = "off"
BOOST_OVERRIDE_VALUES = {BOOST_OVERRIDE_INHERIT, BOOST_OVERRIDE_ON, BOOST_OVERRIDE_OFF}

BOOST_STATUS_DISABLED = "disabled"
BOOST_STATUS_DRY_RUN = "dry_run"
BOOST_STATUS_ENABLED = "enabled"

BOOST_SETTING_KEYS = (
    "boost_global_enabled",
    "boost_scope",
    "boost_default_mode",
    "boost_status",
    "boost_last_run",
    "boost_error",
)

REQUIRED_ENV_VARS = (
    "TWIBOOST_API_KEY",
    "TWIBOOST_API_URL",
    "TWIBOOST_VIEWS_SERVICE_ID",
    "BOOST_DEFAULT_QUANTITY",
    "BOOST_DRY_RUN",
    "BOOST_REAL_ORDERS_ENABLED",
)


def required_env_vars() -> list[str]:
    return list(REQUIRED_ENV_VARS)


def normalize_boost_override(value: Any) -> str:
    value = str(value or "").strip().lower()
    if value in BOOST_OVERRIDE_VALUES:
        return value
    return BOOST_OVERRIDE_INHERIT


def _admin_ids(config=cfg) -> set[int]:
    return {int(x) for x in getattr(config, "ADMIN_CHAT_IDS", []) if str(x).lstrip("-").isdigit()}


def _owner_id(channel: dict | None) -> int | None:
    if not channel:
        return None
    owner = channel.get("owner_id")
    if owner is None:
        return None
    try:
        return int(owner)
    except (TypeError, ValueError):
        return None


def channel_can_use_boost(channel: dict | None, config=cfg) -> bool:
    """Allow boost only for main/admin-owned channels, never tester-owned ones."""
    owner = _owner_id(channel)
    return owner is None or owner in _admin_ids(config)


def boost_configured(config=cfg) -> bool:
    return bool(
        getattr(config, "TWIBOOST_API_KEY", "")
        and getattr(config, "TWIBOOST_API_URL", "")
        and int(getattr(config, "TWIBOOST_VIEWS_SERVICE_ID", 0) or 0) > 0
    )


def boost_real_orders_allowed(config=cfg) -> bool:
    return (
        not bool(getattr(config, "BOOST_DRY_RUN", True))
        and bool(getattr(config, "BOOST_REAL_ORDERS_ENABLED", False))
        and boost_configured(config)
    )


def _effective_status(enabled: bool, config=cfg) -> str:
    if not enabled:
        return BOOST_STATUS_DISABLED
    if boost_real_orders_allowed(config):
        return BOOST_STATUS_ENABLED
    return BOOST_STATUS_DRY_RUN


def load_global_boost_settings(admin_settings: dict | None = None, config=cfg) -> dict:
    data = admin_settings if isinstance(admin_settings, dict) else {}
    nested = data.get("boost") if isinstance(data.get("boost"), dict) else {}

    def read(key: str, default=None):
        return data.get(key, nested.get(key, default))

    enabled = bool(read("boost_global_enabled", False))
    return {
        "boost_global_enabled": enabled,
        "boost_scope": read("boost_scope", "superadmin_channels") or "superadmin_channels",
        "boost_default_mode": normalize_boost_override(read("boost_default_mode", BOOST_OVERRIDE_INHERIT)),
        "boost_status": _effective_status(enabled, config),
        "boost_last_run": read("boost_last_run"),
        "boost_error": read("boost_error"),
    }


def write_global_boost_settings(admin_settings: dict | None, enabled: bool, config=cfg) -> dict:
    data = dict(admin_settings or {})
    data["boost_global_enabled"] = bool(enabled)
    data["boost_scope"] = "superadmin_channels"
    data["boost_default_mode"] = BOOST_OVERRIDE_INHERIT
    data["boost_status"] = _effective_status(bool(enabled), config)
    data.setdefault("boost_last_run", None)
    data["boost_error"] = None
    return data


def channel_boost_override(channel: dict | None) -> str:
    return normalize_boost_override((channel or {}).get("boost_override"))


def set_channel_boost_override(channel: dict, override: str) -> dict:
    channel["boost_override"] = normalize_boost_override(override)
    channel.setdefault("boost_status", None)
    channel.setdefault("boost_error", None)
    return channel


def effective_boost_enabled(
    channel: dict | None,
    global_settings: dict | None,
    actor_context: dict | None = None,
    config=cfg,
) -> bool:
    del actor_context
    if not channel_can_use_boost(channel, config):
        return False

    override = channel_boost_override(channel)
    if override == BOOST_OVERRIDE_OFF:
        return False
    if override == BOOST_OVERRIDE_ON:
        return True

    settings = load_global_boost_settings(global_settings, config)
    return bool(settings.get("boost_global_enabled"))


def build_telegram_post_url(channel: dict | None, message_id: int | str | None) -> str | None:
    if not channel or not message_id:
        return None
    handle = (
        channel.get("channel_id")
        or channel.get("username")
        or channel.get("handle")
        or channel.get("name")
    )
    if not handle:
        return None
    handle = str(handle).strip()
    if not handle or handle.startswith("-100"):
        return None
    handle = handle[1:] if handle.startswith("@") else handle
    return f"https://t.me/{handle}/{message_id}"


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
            response.raise_for_status()
            data = response.json()
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
        service_id: int | None = None,
        dry_run: bool = True,
    ) -> dict:
        qty = int(quantity or getattr(cfg, "BOOST_DEFAULT_QUANTITY", 500) or 500)
        svc = int(service_id or self.service_id or 0)
        payload = {"action": "add", "service": svc, "link": post_url, "quantity": qty}

        if dry_run:
            return {"dry_run": True, "configured": self.configured, "request": payload}
        if not self.configured or svc <= 0:
            return {"error": "twiboost_not_configured", "configured": False}
        return await self._request(payload)

    async def get_order_status(self, order_id: int | str) -> dict:
        return await self._request({"action": "status", "order": order_id})

    async def cancel_order(self, order_id: int | str) -> dict:
        return await self._request({"action": "cancel", "order": order_id})
