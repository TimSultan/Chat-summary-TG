"""Renders the on-disk caches into human-readable Markdown under export/.

The caches (cache/transcripts, cache/stats) are machine-shaped JSON meant for reuse by
the fetcher and the stats engine, not for reading. This script leaves them untouched and
writes a parallel Markdown copy: one file per cached chat-day transcript, one per cached
stats day, plus an index.

Run: python export_cache.py [--out export]
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

TRANSCRIPTS_DIR = Path("cache") / "transcripts"
STATS_DIR = Path("cache") / "stats"


def _hhmm(iso: str) -> str:
    """'2026-07-15T00:01:14+01:00' -> '00:01'; anything unparseable passes through."""
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except (ValueError, TypeError):
        return str(iso)


def _indent_continuations(text: str) -> str:
    """Keeps multi-line messages visually attached to their '[hh:mm] Name:' line."""
    return (text or "").replace("\n", "\n    ")


def render_transcript(path: Path) -> tuple[str, int]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    messages = payload.get("messages", [])
    chat_id, _, day = path.stem.partition("_")

    lines = [
        f"# Chat {chat_id} — {day}",
        "",
        f"*{len(messages)} messages · fetched {payload.get('fetched_at', 'unknown')}*",
        "",
        "```",
    ]
    for m in messages:
        reply_tag = " (reply)" if m.get("is_reply") else ""
        sender = m.get("sender_name") or m.get("sender_username") or m.get("sender_id")
        lines.append(
            f"[{_hhmm(m.get('dt_local'))}] {sender}{reply_tag}: "
            f"{_indent_continuations(m.get('text'))}"
        )
    lines.append("```")
    return "\n".join(lines) + "\n", len(messages)


def render_stats(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    users = payload.get("users", {})
    rows = sorted(users.items(), key=lambda kv: kv[1].get("messages", 0), reverse=True)

    lines = [
        f"# Stats — {payload.get('entry', path.stem)} — {payload.get('day', '?')}",
        "",
        f"*{len(users)} participants · recorded {payload.get('recorded_at', 'unknown')}*",
        "",
        "| User | Username | Messages | Chars | Media | Replies | Last message |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for user_id, u in rows:
        lines.append(
            f"| {u.get('display_name') or user_id} | {u.get('username') or '—'} "
            f"| {u.get('messages', 0)} | {u.get('chars', 0)} | {u.get('media', 0)} "
            f"| {u.get('replies', 0)} | {u.get('last_message_at') or '—'} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="export", help="output directory (default: export)")
    args = parser.parse_args()

    out_root = Path(args.out)
    transcript_out = out_root / "transcripts"
    stats_out = out_root / "stats"

    index = ["# Cache export", ""]

    transcripts = sorted(TRANSCRIPTS_DIR.glob("*.json"))
    if transcripts:
        transcript_out.mkdir(parents=True, exist_ok=True)
        total = 0
        index += ["## Transcripts", "", "| Day | Messages | File |", "| --- | ---: | --- |"]
        for src in transcripts:
            text, count = render_transcript(src)
            dest = transcript_out / f"{src.stem}.md"
            dest.write_text(text, encoding="utf-8")
            total += count
            index.append(f"| {src.stem} | {count} | [{dest.name}](transcripts/{dest.name}) |")
        index += ["", f"**{len(transcripts)} days · {total} messages**", ""]

    stats_files = sorted(STATS_DIR.glob("*.json"))
    if stats_files:
        stats_out.mkdir(parents=True, exist_ok=True)
        index += ["## Stats", ""]
        for src in stats_files:
            dest = stats_out / f"{src.stem}.md"
            dest.write_text(render_stats(src), encoding="utf-8")
            index.append(f"- [{dest.name}](stats/{dest.name})")
        index.append("")

    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "index.md").write_text("\n".join(index), encoding="utf-8")
    print(f"Wrote {len(transcripts)} transcripts and {len(stats_files)} stats files to {out_root}/")


if __name__ == "__main__":
    main()
