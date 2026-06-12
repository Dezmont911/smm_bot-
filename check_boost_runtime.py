"""Read-only Boost runtime diagnostic.

This script checks local .env flags and SQLite Boost state. It does not call
TwiBoost and never creates orders.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
ENV_PATHS = [
    BASE_DIR / ".env",
    BASE_DIR / ".env.boost_tester",
]
DB_PATH = BASE_DIR / "data" / "content_factory.db"


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _read_env(paths: list[Path]) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in paths:
        values.update(_read_env_file(path))
    return values


def _env(values: dict[str, str], key: str, default: str = "") -> str:
    return values.get(key) or os.getenv(key, default)


def _env_bool(values: dict[str, str], key: str, default: bool) -> bool:
    raw = _env(values, key, "true" if default else "false").strip().lower()
    return raw in {"1", "true", "yes", "y", "on", "да"}


def _env_int(values: dict[str, str], key: str, default: int = 0) -> int:
    raw = _env(values, key, str(default)).strip()
    return int(raw) if raw.lstrip("-").isdigit() else default


def _tester_ids(values: dict[str, str]) -> list[int]:
    result: list[int] = []
    for item in _env(values, "BOOST_TESTER_IDS").split(","):
        item = item.strip()
        if item.lstrip("-").isdigit():
            result.append(int(item))
    return result


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return bool(row)


def _one(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


def _all(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def _yes(value: Any) -> str:
    return "yes" if bool(value) else "no"


def _on(value: Any) -> str:
    return "on" if bool(value) else "off"


def _effective_service(channel: dict[str, Any], default_service: int) -> str:
    return str(channel.get("service_id") or default_service or "")


def _channel_blockers(
    channel: dict[str, Any],
    *,
    global_enabled: bool,
    dry_run: bool,
    real_orders_enabled: bool,
    tester_key_configured: bool,
    default_views_service: int,
    default_reactions_service: int,
) -> list[str]:
    blockers: list[str] = []
    if not global_enabled:
        blockers.append("global Boost off")
    if dry_run:
        blockers.append("BOOST_DRY_RUN=true")
    if not real_orders_enabled:
        blockers.append("BOOST_REAL_ORDERS_ENABLED=false")
    if not tester_key_configured:
        blockers.append("tester API key missing")
    if not channel.get("enabled"):
        blockers.append("channel off")
    if not channel.get("username"):
        blockers.append("no public username/post url")
    if not _effective_service(channel, default_views_service):
        blockers.append("views service id missing")
    if channel.get("reactions_enabled"):
        if not str(channel.get("reactions_quantity_min") or channel.get("reactions_quantity") or "").strip():
            blockers.append("reactions qty missing")
        if not str(channel.get("reactions_service_id") or default_reactions_service or "").strip():
            blockers.append("reactions service id missing")
    return blockers


def _print_events(conn: sqlite3.Connection, owner_id: int, limit: int = 10) -> None:
    rows = _all(
        conn,
        """
        SELECT
            o.id, o.boost_channel_id, o.message_id, o.order_kind, o.status,
            o.dry_run, o.reason_code, o.error, o.post_url, o.quantity,
            o.service_id, o.provider_order_id, o.created_at,
            c.username, c.tg_chat_id
        FROM boost_orders o
        LEFT JOIN boost_channels c ON c.id = o.boost_channel_id
        WHERE c.owner_id = ?
        ORDER BY o.id DESC
        LIMIT ?
        """,
        (owner_id, limit),
    )
    print(f"\nRecent tester events for {owner_id}: {len(rows)}")
    for row in rows:
        channel = f"@{row['username']}" if row.get("username") else str(row.get("tg_chat_id") or "?")
        status = row.get("status")
        dry = "dry" if row.get("dry_run") else "real"
        reason = row.get("reason_code") or row.get("error") or "-"
        order = row.get("provider_order_id") or "-"
        print(
            f"  #{row['id']} {channel} msg={row['message_id']} "
            f"{row['order_kind']} {status}/{dry} qty={row['quantity']} "
            f"service={row.get('service_id') or '-'} provider_order={order} reason={reason}"
        )
        if row.get("post_url"):
            print(f"     url={row['post_url']}")


def _print_conflicts(conn: sqlite3.Connection, channel: dict[str, Any]) -> None:
    clauses: list[str] = []
    params: list[Any] = []
    if channel.get("username"):
        clauses.append("username = ?")
        params.append(channel["username"])
    if channel.get("tg_chat_id"):
        clauses.append("tg_chat_id = ?")
        params.append(channel["tg_chat_id"])
    if not clauses:
        return
    params.append(int(channel["id"]))
    rows = _all(
        conn,
        f"""
        SELECT id, owner_id, username, tg_chat_id, enabled, smm_channel_id
        FROM boost_channels
        WHERE ({" OR ".join(clauses)}) AND id != ?
        ORDER BY id
        """,
        tuple(params),
    )
    if not rows:
        return
    print("    possible duplicate/conflict rows:")
    for row in rows:
        print(
            f"      id={row['id']} owner_id={row.get('owner_id')} "
            f"enabled={_on(row.get('enabled'))} username={row.get('username') or '-'} "
            f"chat_id={row.get('tg_chat_id') or '-'} smm={row.get('smm_channel_id') or '-'}"
        )


def main() -> int:
    env_values = _read_env(ENV_PATHS)
    tester_ids = _tester_ids(env_values)
    dry_run = _env_bool(env_values, "BOOST_DRY_RUN", True)
    real_orders_enabled = _env_bool(env_values, "BOOST_REAL_ORDERS_ENABLED", False)
    tester_key_configured = bool(_env(env_values, "TWIBOOST_TESTER_API_KEY"))
    default_views_service = _env_int(env_values, "TWIBOOST_TESTER_VIEWS_SERVICE_ID")
    default_reactions_service = _env_int(env_values, "TWIBOOST_TESTER_REACTIONS_SERVICE_ID")

    print("Boost runtime check")
    print("env files:")
    for path in ENV_PATHS:
        print(f"  {path} exists={path.exists()}")
    print(f"db file: {DB_PATH}")
    print("read-only: yes")
    print(f"BOOST_TESTER_IDS: {tester_ids or 'not configured'}")
    print(f"BOOST_DRY_RUN: {_yes(dry_run)}")
    print(f"BOOST_REAL_ORDERS_ENABLED: {_yes(real_orders_enabled)}")
    print(f"tester API key configured: {_yes(tester_key_configured)}")
    print(f"tester views service id: {default_views_service or 'not configured'}")
    print(f"tester reactions service id: {default_reactions_service or 'not configured'}")

    if not DB_PATH.exists():
        print("\nERROR: database file not found")
        return 2

    with _connect(DB_PATH) as conn:
        required_tables = ["boost_settings", "boost_channels", "boost_orders"]
        missing = [name for name in required_tables if not _table_exists(conn, name)]
        if missing:
            print(f"\nERROR: missing tables: {', '.join(missing)}")
            return 2

        settings = _one(conn, "SELECT * FROM boost_settings WHERE id = 1") or {}
        global_enabled = bool(settings.get("boost_enabled"))
        print("\nGlobal DB settings")
        print(f"  boost_enabled: {_on(global_enabled)}")
        print(f"  db boost_dry_run: {_yes(settings.get('boost_dry_run'))}")
        print(f"  db real_orders_enabled: {_yes(settings.get('real_orders_enabled'))}")
        print(f"  default_quantity: {settings.get('default_quantity') or '-'}")
        print(f"  default_service_id: {settings.get('default_service_id') or '-'}")
        print(f"  last_error: {settings.get('last_error') or '-'}")

        if not tester_ids:
            print("\nNo tester ids configured; nothing to inspect.")
            return 1

        for owner_id in tester_ids:
            channels = _all(
                conn,
                """
                SELECT *
                FROM boost_channels
                WHERE owner_id = ?
                ORDER BY id
                """,
                (owner_id,),
            )
            print(f"\nTester {owner_id} boost channels: {len(channels)}")
            if not channels:
                print("  No boost_channels rows for this tester.")
            for channel in channels:
                name = f"@{channel['username']}" if channel.get("username") else channel.get("tg_chat_id") or channel.get("channel_key")
                views_service = _effective_service(channel, default_views_service) or "-"
                reactions_service = str(channel.get("reactions_service_id") or default_reactions_service or "-")
                blockers = _channel_blockers(
                    channel,
                    global_enabled=global_enabled,
                    dry_run=dry_run,
                    real_orders_enabled=real_orders_enabled,
                    tester_key_configured=tester_key_configured,
                    default_views_service=default_views_service,
                    default_reactions_service=default_reactions_service,
                )
                print(
                    f"  id={channel['id']} {name} enabled={_on(channel.get('enabled'))} "
                    f"smm={channel.get('smm_channel_id') or '-'} chat_id={channel.get('tg_chat_id') or '-'}"
                )
                print(
                    f"    views qty={channel.get('quantity_display') or channel.get('quantity') or '-'} "
                    f"service={views_service} | reactions={_on(channel.get('reactions_enabled'))} "
                    f"qty={channel.get('reactions_quantity_display') or channel.get('reactions_quantity') or '-'} "
                    f"service={reactions_service}"
                )
                print(
                    f"    last_seen={channel.get('last_seen_message_id') or '-'} "
                    f"last_order={channel.get('last_order_id') or '-'} "
                    f"last_error={channel.get('last_error') or '-'}"
                )
                if blockers:
                    print(f"    WILL NOT ORDER: {', '.join(blockers)}")
                else:
                    print("    READY: next public channel post should create real views order")
                    if channel.get("reactions_enabled"):
                        print("    READY: reactions order should also be created")
                _print_conflicts(conn, channel)
            _print_events(conn, owner_id)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
