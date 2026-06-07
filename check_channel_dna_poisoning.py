"""Read-only scanner for incompatible channel_dna in channel cards.

Usage:
    python check_channel_dna_poisoning.py

The script prints one JSON object per channel with DNA status. It does not edit
channel cards.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from channel_dna import channel_dna_compatibility


CHANNELS_DIR = Path(__file__).parent / "channels"


def iter_channels(channels_dir: Path):
    for path in sorted(channels_dir.glob("*.json")):
        try:
            channel = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            yield path, None, {"status": "read_error", "reason": str(exc)}
            continue
        yield path, channel, channel_dna_compatibility(channel)


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan channel cards for incompatible channel_dna.")
    parser.add_argument("--channels-dir", default=str(CHANNELS_DIR))
    parser.add_argument("--only-problems", action="store_true")
    args = parser.parse_args()

    root = Path(args.channels_dir)
    total = problems = 0
    for path, channel, status in iter_channels(root):
        total += 1
        is_problem = status.get("status") in {"ignored_incompatible", "read_error"}
        if is_problem:
            problems += 1
        if args.only_problems and not is_problem:
            continue
        row = {
            "file": path.name,
            "channel_id": (channel or {}).get("channel_id"),
            "name": (channel or {}).get("name"),
            "channel_type": (channel or {}).get("channel_type"),
            "archetype": (channel or {}).get("archetype"),
            "dna_status": status.get("status"),
            "dna_reason": status.get("reason"),
            "suspicious_fields": status.get("suspicious_fields") or [],
        }
        print(json.dumps(row, ensure_ascii=False))

    print(json.dumps({"summary": {"total": total, "problems": problems}}, ensure_ascii=False))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
