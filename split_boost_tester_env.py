"""Move tester TwiBoost secrets from .env to .env.boost_tester.

The script creates a backup, does not print secret values, and is intended as a
one-time operational helper for VPS/local environments.
"""

from __future__ import annotations

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
MAIN_ENV = BASE_DIR / ".env"
TESTER_ENV = BASE_DIR / ".env.boost_tester"
BACKUP_ENV = BASE_DIR / ".env.before_boost_tester_split"

TESTER_KEYS = {
    "BOOST_TESTER_IDS",
    "TWIBOOST_TESTER_API_KEY",
    "TWIBOOST_TESTER_API_URL",
    "TWIBOOST_TESTER_VIEWS_SERVICE_ID",
    "TWIBOOST_TESTER_REACTIONS_SERVICE_ID",
}


def _parse_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip()


def _read_existing_tester_keys() -> set[str]:
    if not TESTER_ENV.exists():
        return set()
    found: set[str] = set()
    for line in TESTER_ENV.read_text(encoding="utf-8", errors="replace").splitlines():
        key = _parse_key(line)
        if key:
            found.add(key)
    return found


def main() -> int:
    if not MAIN_ENV.exists():
        print(f"ERROR: {MAIN_ENV} not found")
        return 2

    original_lines = MAIN_ENV.read_text(encoding="utf-8", errors="replace").splitlines()
    existing_tester_keys = _read_existing_tester_keys()
    moved_lines: list[str] = []
    kept_lines: list[str] = []
    moved_keys: list[str] = []

    for line in original_lines:
        key = _parse_key(line)
        if key in TESTER_KEYS:
            if key not in existing_tester_keys:
                moved_lines.append(line)
                moved_keys.append(key)
            kept_lines.append(f"# moved to .env.boost_tester: {key}=")
            continue
        kept_lines.append(line)

    if not moved_keys:
        print("Nothing to move: tester keys are absent or already present in .env.boost_tester.")
        print(f"main env: {MAIN_ENV}")
        print(f"tester env: {TESTER_ENV} exists={TESTER_ENV.exists()}")
        return 0

    if not BACKUP_ENV.exists():
        BACKUP_ENV.write_text("\n".join(original_lines) + "\n", encoding="utf-8")

    tester_prefix = []
    if not TESTER_ENV.exists():
        tester_prefix = [
            "# Split tester TwiBoost secrets.",
            "# Loaded after .env and intentionally kept out of git.",
            "",
        ]

    with TESTER_ENV.open("a", encoding="utf-8") as fh:
        if tester_prefix:
            fh.write("\n".join(tester_prefix))
            fh.write("\n")
        if TESTER_ENV.stat().st_size > 0:
            fh.write("\n")
        fh.write("# TWIBOOST_API_TESTER\n")
        for line in moved_lines:
            fh.write(line)
            fh.write("\n")

    MAIN_ENV.write_text("\n".join(kept_lines) + "\n", encoding="utf-8")

    print("Moved tester TwiBoost keys to .env.boost_tester.")
    print(f"moved keys: {', '.join(moved_keys)}")
    print(f"backup: {BACKUP_ENV}")
    print("secret values printed: no")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
