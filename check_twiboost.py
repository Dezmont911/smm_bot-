"""Safe TwiBoost configuration check.

This script never creates orders. It only checks account balance and services.
It intentionally does not print API keys.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ENV_PATH = Path(__file__).with_name(".env")
TIMEOUT_SECONDS = 25


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            values[key] = value
    return values


def _env(values: dict[str, str], key: str, default: str = "") -> str:
    return values.get(key) or os.getenv(key, default)


def _fingerprint(secret: str) -> str:
    if not secret:
        return "-"
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


def _request(api_url: str, api_key: str, action: str) -> tuple[int | None, str]:
    data = urllib.parse.urlencode({"key": api_key, "action": action}).encode("utf-8")
    request = urllib.request.Request(api_url, data=data, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    request.add_header("User-Agent", "smm-bot-twiboost-check/1.0")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return exc.code, body
    except Exception as exc:  # CLI diagnostic must not crash noisily.
        return None, f"{type(exc).__name__}: {exc}"


def _load_json(body: str) -> Any:
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def _scrub(text: str, api_key: str) -> str:
    if api_key:
        text = text.replace(api_key, "[hidden]")
    return text


def _print_balance(api_url: str, api_key: str) -> None:
    status, body = _request(api_url, api_key, "balance")
    print(f"  balance HTTP: {status}")
    parsed = _load_json(body)
    if isinstance(parsed, dict):
        if "error" in parsed:
            print(f"  balance error: {parsed.get('error')}")
            return
        shown = {k: parsed.get(k) for k in ("balance", "currency") if k in parsed}
        print(f"  balance result: {shown or 'ok'}")
        return
    print(f"  balance raw: {_scrub(body, api_key)[:300]}")


def _service_matches(service: dict[str, Any], service_id: str) -> bool:
    if not service_id:
        return False
    return str(service.get("service", "")).strip() == service_id


def _print_services(api_url: str, api_key: str, service_ids: list[tuple[str, str]]) -> None:
    status, body = _request(api_url, api_key, "services")
    print(f"  services HTTP: {status}")
    parsed = _load_json(body)
    if isinstance(parsed, dict) and "error" in parsed:
        print(f"  services error: {parsed.get('error')}")
        return
    if not isinstance(parsed, list):
        print(f"  services raw: {_scrub(body, api_key)[:500]}")
        return

    print(f"  services count: {len(parsed)}")
    for label, service_id in service_ids:
        if not service_id:
            print(f"  {label}: not configured")
            continue
        match = next((item for item in parsed if isinstance(item, dict) and _service_matches(item, service_id)), None)
        if not match:
            print(f"  {label}: service {service_id} NOT FOUND")
            continue
        name = str(match.get("name", "")).strip()
        min_qty = match.get("min")
        max_qty = match.get("max")
        print(f"  {label}: service {service_id} found | min={min_qty} max={max_qty} | {name[:90]}")


def _check_profile(
    env_values: dict[str, str],
    *,
    label: str,
    key_name: str,
    url_name: str,
    service_names: list[tuple[str, str]],
) -> None:
    api_key = _env(env_values, key_name)
    api_url = _env(env_values, url_name, "https://twiboost.com/api/v2").rstrip("/")
    services = [(service_label, _env(env_values, env_name)) for service_label, env_name in service_names]

    print(f"\n== {label} ==")
    print(f"  api url: {api_url or 'not configured'}")
    print(f"  api key configured: {bool(api_key)}")
    print(f"  api key length: {len(api_key)}")
    print(f"  api key fingerprint: {_fingerprint(api_key)}")
    for service_label, service_id in services:
        print(f"  {service_label} service id: {service_id or 'not configured'}")

    if not api_key or not api_url:
        print("  skip API check: missing api key or url")
        return

    _print_balance(api_url, api_key)
    _print_services(api_url, api_key, services)


def main() -> int:
    env_values = _read_env(ENV_PATH)
    print("TwiBoost safe check")
    print(f"env file: {ENV_PATH}")
    print("actions used: balance, services")
    print("orders created: no")

    _check_profile(
        env_values,
        label="MAIN",
        key_name="TWIBOOST_API_KEY",
        url_name="TWIBOOST_API_URL",
        service_names=[("views", "TWIBOOST_VIEWS_SERVICE_ID")],
    )
    _check_profile(
        env_values,
        label="TESTER",
        key_name="TWIBOOST_TESTER_API_KEY",
        url_name="TWIBOOST_TESTER_API_URL",
        service_names=[
            ("views", "TWIBOOST_TESTER_VIEWS_SERVICE_ID"),
            ("reactions", "TWIBOOST_TESTER_REACTIONS_SERVICE_ID"),
        ],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
