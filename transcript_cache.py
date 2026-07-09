"""Caches the raw fetched transcript (as plain message dicts) for a single calendar day
per chat, so that different questions about the same day -- "summary of today", "what
did Anzhelika talk about", asked five minutes apart by different people -- only pay for
one Telegram fetch. Each question still gets its own fresh OpenAI call against that
shared transcript; only the fetch is deduplicated.

A day strictly before today (in the relevant timezone) is closed and can't gain new
messages, so it's cached indefinitely. Today itself is cached with a short TTL: reused
if fetched less than TODAY_TTL_SECONDS ago, re-fetched (and the file updated) once stale.
"""

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

# DATA_DIR defaults to the current directory (local use). On a host with no persistent
# disk by default (Railway, etc.), set DATA_DIR to a mounted Volume's path so the cache
# survives restarts/redeploys instead of resetting each time -- entirely optional, the
# app works fine without it, just re-fetching from Telegram after every restart.
DATA_DIR = Path(os.getenv("DATA_DIR", "."))
CACHE_DIR = DATA_DIR / "cache" / "transcripts"
TODAY_TTL_SECONDS = 30 * 60


def day_is_final(day: date, tz) -> bool:
    """True once `day` is strictly before today in `tz` -- closed days are cached
    forever; the still-open current day is cached on a TTL instead."""
    return day < datetime.now(tz).date()


def _path(chat_id: int, day: date) -> Path:
    return CACHE_DIR / f"{chat_id}_{day.isoformat()}.json"


def load(chat_id: int, day: date, is_final: bool) -> list[dict] | None:
    """Returns the cached list of message dicts for this (chat, day), or None if
    there's no cache entry, or (for a non-final day) it's older than the TTL."""
    path = _path(chat_id, day)
    if not path.exists():
        return None

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not is_final:
        fetched_at = datetime.fromisoformat(payload["fetched_at"])
        age_seconds = (datetime.now(timezone.utc) - fetched_at).total_seconds()
        if age_seconds > TODAY_TTL_SECONDS:
            return None
    return payload["messages"]


def save(chat_id: int, day: date, messages: list[dict]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "messages": messages,
    }
    _path(chat_id, day).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
