"""Pulls every message from a single sender out of the cached transcripts
(cache/transcripts/*.json) and writes them to a flat text file, oldest first.

Run: python extract_user_messages.py <username_or_id> [--out FILE]
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

TRANSCRIPTS_DIR = Path("cache") / "transcripts"


def matches(m: dict, needle: str) -> bool:
    needle = needle.lower().lstrip("@")
    return (
        needle == str(m.get("sender_id", "")).lower()
        or needle == (m.get("sender_username") or "").lower()
        or needle == (m.get("sender_name") or "").lower()
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("who", help="username, display name, or numeric sender id")
    parser.add_argument("--out", default=None, help="output .txt path")
    args = parser.parse_args()

    out_path = Path(args.out) if args.out else Path(f"{args.who.lstrip('@')}_messages.txt")

    rows = []  # (dt_local, day, sender_name, text)
    for src in sorted(TRANSCRIPTS_DIR.glob("*.json")):
        payload = json.loads(src.read_text(encoding="utf-8"))
        chat_id, _, day = src.stem.partition("_")
        for m in payload.get("messages", []):
            if matches(m, args.who):
                rows.append((m.get("dt_local"), day, m.get("sender_name"), m.get("text") or ""))

    rows.sort(key=lambda r: (r[0] or "", r[1]))

    lines = []
    for dt_local, day, sender_name, text in rows:
        try:
            ts = datetime.fromisoformat(dt_local).strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            ts = f"{day} ??:??"
        lines.append(f"[{ts}] {sender_name}: {text}")

    out_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    print(f"Wrote {len(rows)} messages to {out_path}")


if __name__ == "__main__":
    main()
