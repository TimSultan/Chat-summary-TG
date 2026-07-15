"""Caches the raw fetched transcript (as plain message dicts) for a single calendar day
per chat, so that different questions about the same day -- "summary of today", "what
did Anzhelika talk about", asked five minutes apart by different people -- only pay for
one Telegram fetch. Each question still gets its own fresh OpenAI call against that
shared transcript; only the fetch is deduplicated.

A day strictly before today (in the relevant timezone) is closed and can't gain new
messages, so it's cached indefinitely. Today itself is cached with a short TTL: reused
if fetched less than TODAY_TTL_SECONDS ago. Once stale, the caller (see
telegram_fetch.fetch_range_messages_cached) doesn't discard and re-fetch it wholesale --
it fetches only what's new since and appends -- so `load` always hands back whatever
messages exist on disk, tagged with whether they're still within the TTL, rather than
returning None outright once they're not.
"""

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from app_time import cache_namespace, now as app_now

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


def _path(chat_id: int, day: date, tz) -> Path:
    return CACHE_DIR / cache_namespace(tz) / f"{chat_id}_{day.isoformat()}.json"


@dataclass
class CachedDay:
    messages: list[dict]
    is_fresh: bool  # always True for a final day; for today, whether it's within the TTL


def load(chat_id: int, day: date, is_final: bool, tz) -> CachedDay | None:
    """Returns the cached messages for this (chat, day) plus whether they're still
    fresh, or None if there's no cache entry at all. Unlike a straight cache miss (None),
    a *stale* entry still returns its messages -- see the module docstring -- so the
    caller can extend them instead of discarding and re-fetching everything. The
    timezone namespace deliberately leaves legacy, un-namespaced cache files alone."""
    path = _path(chat_id, day, tz)
    if not path.exists():
        return None

    payload = json.loads(path.read_text(encoding="utf-8"))
    is_fresh = True
    if not is_final:
        fetched_at = datetime.fromisoformat(payload["fetched_at"])
        age_seconds = (datetime.now(timezone.utc) - fetched_at).total_seconds()
        is_fresh = age_seconds <= TODAY_TTL_SECONDS
    return CachedDay(messages=payload["messages"], is_fresh=is_fresh)


def save(chat_id: int, day: date, messages: list[dict], tz) -> None:
    path = _path(chat_id, day, tz)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": app_now(tz).isoformat(),
        "messages": messages,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
