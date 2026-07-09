"""CLI: summarize a Telegram chat (or one participant's messages in it) over a date range.

Usage:
    python main.py --chat "My Group" --date 2026-07-08
    python main.py --chat @somechannel --date yesterday
    python main.py --chat "My Group" --date 2026-07-01:2026-07-08
    python main.py --chat "My Group" --date last7days --user @some_user
"""

import argparse
import asyncio
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from telethon import TelegramClient

from config import build_session, load_config
from errors import ChatSummaryError
from summarizer import summarize_transcript
from telegram_fetch import fetch_range_messages_cached, format_transcript_lines, sender_matches


def parse_date_range(value: str) -> tuple[date, date]:
    if not value or not value.strip():
        raise ChatSummaryError("--date cannot be empty.")

    v = value.strip().lower()
    today = date.today()
    if v == "today":
        return today, today
    if v == "yesterday":
        d = today - timedelta(days=1)
        return d, d
    if v == "last7days":
        return today - timedelta(days=6), today
    if v == "last30days":
        return today - timedelta(days=29), today
    if ":" in value:
        start_s, end_s = value.split(":", 1)
        try:
            start = datetime.strptime(start_s.strip(), "%Y-%m-%d").date()
            end = datetime.strptime(end_s.strip(), "%Y-%m-%d").date()
        except ValueError:
            raise ChatSummaryError(f"Invalid --date range '{value}'. Use YYYY-MM-DD:YYYY-MM-DD.")
        if end < start:
            start, end = end, start
        return start, end
    try:
        d = datetime.strptime(value.strip(), "%Y-%m-%d").date()
        return d, d
    except ValueError:
        raise ChatSummaryError(
            f"Invalid --date '{value}'. Use YYYY-MM-DD, a 'YYYY-MM-DD:YYYY-MM-DD' range, "
            "'today', 'yesterday', 'last7days', or 'last30days'."
        )


def resolve_tz(tz_name: str | None):
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except Exception as e:
            raise ChatSummaryError(f"Unknown timezone '{tz_name}': {e}") from e
    try:
        from tzlocal import get_localzone

        return get_localzone()
    except Exception:
        return datetime.now().astimezone().tzinfo


def safe_filename(name: str) -> str:
    name = re.sub(r"[^\w\-. ]+", "_", name).strip().strip(".")
    return name or "chat"


def period_label(start: date, end: date) -> str:
    return start.isoformat() if start == end else f"{start.isoformat()} to {end.isoformat()}"


async def run(args) -> Path:
    if not args.chat or not args.chat.strip():
        raise ChatSummaryError("--chat cannot be empty.")

    cfg = load_config()
    tz = resolve_tz(args.tz)
    start_day, end_day = parse_date_range(args.date)
    label = period_label(start_day, end_day)
    user = args.user or None

    client = TelegramClient(build_session(cfg), cfg.api_id, cfg.api_hash)
    async with client:
        print(f"Fetching messages from '{args.chat}' for {label} ({tz})...")
        chat_title, messages = await fetch_range_messages_cached(
            client, args.chat, start_day, end_day, tz, log=print, force_refresh=args.force
        )

    if user:
        matched = sum(1 for m in messages if sender_matches(m, user))
        print(f"Fetched {len(messages)} messages from '{chat_title}' ({matched} from {user}).")
    else:
        print(f"Fetched {len(messages)} messages from '{chat_title}'.")

    lines = format_transcript_lines(messages, include_date=(start_day != end_day))

    model = args.model or cfg.openai_model
    print(f"Summarizing with {model}...")
    summary_md = summarize_transcript(
        api_key=cfg.openai_api_key,
        model=model,
        chat_title=chat_title,
        period_label=label,
        lines=lines,
        focus_user=user,
        style="file",
    )

    participants = len({m.sender_name for m in messages})
    generated_at = datetime.now(tz).strftime("%Y-%m-%d %H:%M %Z")
    title_suffix = f" — {user}" if user else ""
    header = (
        f"# {chat_title}{title_suffix} — Summary for {label}\n\n"
        f"*Generated {generated_at}. {len(messages)} messages from {participants} participants.*\n\n"
    )
    full_md = header + summary_md + "\n"

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    user_suffix = f"_{safe_filename(user)}" if user else ""
    date_part = start_day.isoformat() if start_day == end_day else f"{start_day.isoformat()}_{end_day.isoformat()}"
    out_path = out_dir / f"{safe_filename(chat_title)}{user_suffix}_{date_part}.md"
    out_path.write_text(full_md, encoding="utf-8")
    return out_path


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chat", required=True, help="Chat username (@name), numeric ID, or title substring")
    parser.add_argument(
        "--date",
        default="today",
        help="YYYY-MM-DD, a 'YYYY-MM-DD:YYYY-MM-DD' range, 'today', 'yesterday', 'last7days', "
        "or 'last30days' (default: today)",
    )
    parser.add_argument(
        "--user",
        default=None,
        help="Restrict the summary to what this participant discussed (username or display name substring)",
    )
    parser.add_argument("--tz", default=None, help="IANA timezone, e.g. Europe/Istanbul (default: system local)")
    parser.add_argument(
        "--model",
        default=None,
        help="Override OPENAI_MODEL from .env (e.g. gpt-5.4-mini, gpt-5.5, gpt-5.4-nano)",
    )
    parser.add_argument("--output-dir", default="output", help="Directory to write the markdown summary to")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore the cached transcript and re-fetch every day fresh from Telegram",
    )
    args = parser.parse_args()

    try:
        out_path = asyncio.run(run(args))
    except ChatSummaryError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        sys.exit(130)

    print(f"Summary saved to {out_path}")


if __name__ == "__main__":
    main()
