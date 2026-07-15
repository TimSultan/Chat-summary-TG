"""Per-user activity stats and a gamified leaderboard for a chat -- message/character/
media/reply counts, active days, and an hourly activity histogram, computed once per
calendar day from the SAME per-day transcript cache telegram_fetch.py already maintains
(see finalize_and_record, called by listener.py's midnight rollover job). Powers "/top
today|week|month" (a simple points leaderboard) and "/stat [username]" (one person's
tracked history).

The daily rollover only ever permanently records a day once it's actually over (closed
days are immutable, see record_day) -- but a query for "today" run *during* today
obviously can't wait for that. Every query-facing function (format_top,
resolve_stat_target, and the aggregate_live/aggregate_all_time_live they call) therefore
merges in a freshly-computed, never-persisted snapshot of today on top of whatever's
already recorded for earlier days, so "/top today" and "/stat" both reflect activity as
it happens rather than only ever showing yesterday-and-earlier.

Storage: one JSON file per (chat, day) under DATA_DIR/cache/stats/, keyed by a hash of
the LISTENER_ALLOWED_CHATS entry string -- same keying scheme chat_profile.py already
uses for its own per-chat cache, chosen for the same reason: it sidesteps Telegram's two
different chat-id numbering schemes (Telethon's own vs. the Bot API's) entirely, since
both listener.py and bot_listener.py can always recover the *entry* for an incoming
message via their own matched_allowed_chat/_match_allowed_chat helpers, regardless of
which account is handling the request. A day file's existence IS the "already recorded"
check record_day/finalize_and_record need to stay idempotent -- rerunning the rollover
job for a day it already processed (e.g. a restart landing near midnight) is then a cheap
no-op, not a double-count.

Scoring (used only by /top's leaderboard ranking -- /stat shows raw counts, not points):
    +1 per message
    +1 per message containing a photo or video
    +1 per message that's a reply (see the is_reply note on UserStats.replies below)
    +5 per distinct calendar day the person posted at least once
Points are never stored -- always recomputed on demand from the raw per-day counters for
whatever window (today/week/month, or -- for /stat -- every recorded day) is asked about,
so changing the point values later doesn't require re-processing any history.
"""

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import telegram_fetch

DATA_DIR = Path(os.getenv("DATA_DIR", "."))
STATS_DIR = DATA_DIR / "cache" / "stats"

POINTS_PER_MESSAGE = 1
POINTS_PER_MEDIA_MESSAGE = 1
POINTS_PER_REPLY = 1
POINTS_PER_ACTIVE_DAY = 5

# Telegram_fetch.describe_media prepends one of these bracketed tags to a media message's
# cached text (e.g. "[Photo] nice caption"). Narrowed to photo/video only, per spec --
# stickers/voice notes/documents/etc. aren't counted as "media" here even though
# describe_media tags those too.
MEDIA_TAG_PREFIXES = ("[Photo]", "[Video]")

VALID_PERIODS = ("today", "week", "month")
# Days back from today (inclusive of today) each period covers -- rolling windows, not
# calendar-aligned (a "week" is always the last 7 days, not necessarily Mon-Sun) so
# /top week and /top month are never thin just because it happens to be early in a
# calendar week/month.
PERIOD_LOOKBACK_DAYS = {"today": 0, "week": 6, "month": 29}


def _cache_key(entry: str) -> str:
    return hashlib.sha1(entry.strip().lower().encode("utf-8")).hexdigest()[:16]


def _path(entry: str, day: date) -> Path:
    return STATS_DIR / f"{_cache_key(entry)}_{day.isoformat()}.json"


def is_recorded(entry: str, day: date) -> bool:
    """Cheap and synchronous -- just a file existence check, no parsing. This is the
    idempotency guard record_day/finalize_and_record rely on."""
    return _path(entry, day).exists()


def _ru_days(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return f"{n} дня"
    return f"{n} дней"


def compute_day_stats(messages: list) -> dict:
    """Returns {user_id_str: {...counters...}} for one day's messages (a full day's
    telegram_fetch.ChatMessage list). Messages with no resolvable sender_id (rare --
    sender resolution failed) are skipped, since there's no stable key to attribute them
    to. `chars` counts the full cached text including any media-tag prefix (e.g.
    "[Photo] "), not just a caption -- a minor, deliberate over-count for media messages
    given chars isn't scored and the cache doesn't separately store a caption-only
    string."""
    users: dict[str, dict] = {}
    for m in messages:
        if m.sender_id is None:
            continue
        key = str(m.sender_id)
        u = users.setdefault(
            key,
            {
                "username": None,
                "display_name": m.sender_name,
                "messages": 0,
                "chars": 0,
                "media": 0,
                "replies": 0,
                "hours": {},
                "last_message_at": None,
            },
        )
        if m.sender_username:
            u["username"] = m.sender_username
        if m.sender_name:
            u["display_name"] = m.sender_name
        u["messages"] += 1
        u["chars"] += len(m.text)
        if m.text.startswith(MEDIA_TAG_PREFIXES):
            u["media"] += 1
        if m.is_reply:
            u["replies"] += 1
        hour_key = str(m.dt_local.hour)
        u["hours"][hour_key] = u["hours"].get(hour_key, 0) + 1
        ts = m.dt_local.isoformat()
        if u["last_message_at"] is None or ts > u["last_message_at"]:
            u["last_message_at"] = ts
    return users


def record_day(entry: str, day: date, messages: list, log=print) -> bool:
    """Computes and saves per-user stats for `day` from `messages` (that day's full
    transcript). Returns False without writing anything if this (entry, day) was already
    recorded -- callers must not double-count a day into the running totals by recording
    it twice. Returns True once it actually records the day."""
    if is_recorded(entry, day):
        return False
    STATS_DIR.mkdir(parents=True, exist_ok=True)
    users = compute_day_stats(messages)
    payload = {
        "entry": entry,
        "day": day.isoformat(),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "users": users,
    }
    _path(entry, day).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    log(f"[stats] recorded {day} for '{entry}': {len(messages)} message(s), {len(users)} user(s)")
    return True


async def finalize_and_record(client, chat_ref, entry: str, day: date, tz, log=print) -> bool:
    """The whole "close out a day" step listener.py's midnight rollover (and its startup
    catch-up) calls once per (entry, day): makes sure `day`'s transcript cache is
    complete (see telegram_fetch.ensure_day_finalized) then records that day's per-user
    stats from it (see record_day). Returns False, without any Telegram calls at all, if
    `day` was already recorded for `entry`."""
    if is_recorded(entry, day):
        return False
    await telegram_fetch.ensure_day_finalized(client, chat_ref, day, tz, log=log)
    _, messages = await telegram_fetch.fetch_range_messages_cached(
        client=client, chat_ref=chat_ref, start_day=day, end_day=day, tz=tz, log=log,
    )
    return record_day(entry, day, messages, log=log)


@dataclass
class UserStats:
    user_id: str
    username: str | None = None
    display_name: str = "Unknown"
    messages: int = 0
    chars: int = 0
    media: int = 0
    # Counts any message Telegram itself flags as a reply (ChatMessage.is_reply) --
    # including a reply to one's own earlier message, which the cache doesn't currently
    # distinguish from a reply to someone else. Self-replies are rare in group chat use,
    # so this is a deliberate, documented simplification rather than a bug: getting it
    # exactly right would mean caching reply_to_msg_id and cross-referencing the replied-
    # to message's sender, a real schema change for a small accuracy gain on a gamified
    # scoring stat.
    replies: int = 0
    active_days: int = 0
    hours: dict = field(default_factory=dict)
    last_message_at: str | None = None

    @property
    def score(self) -> int:
        return (
            self.messages * POINTS_PER_MESSAGE
            + self.media * POINTS_PER_MEDIA_MESSAGE
            + self.replies * POINTS_PER_REPLY
            + self.active_days * POINTS_PER_ACTIVE_DAY
        )


def _merge_day(combined: dict[str, UserStats], payload: dict) -> None:
    for user_id, u in payload.get("users", {}).items():
        s = combined.setdefault(user_id, UserStats(user_id=user_id))
        if u.get("username"):
            s.username = u["username"]
        if u.get("display_name"):
            s.display_name = u["display_name"]
        s.messages += u.get("messages", 0)
        s.chars += u.get("chars", 0)
        s.media += u.get("media", 0)
        s.replies += u.get("replies", 0)
        if u.get("messages", 0) > 0:
            s.active_days += 1
        for hour, count in u.get("hours", {}).items():
            s.hours[hour] = s.hours.get(hour, 0) + count
        last = u.get("last_message_at")
        if last and (s.last_message_at is None or last > s.last_message_at):
            s.last_message_at = last


def _load_day(entry: str, day: date) -> dict | None:
    path = _path(entry, day)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def aggregate(entry: str, start_day: date, end_day: date) -> dict[str, UserStats]:
    """Sums every recorded day in [start_day, end_day] (inclusive) into one UserStats per
    user. A day with no recorded file yet (not processed yet, or before tracking started
    for this chat) simply contributes nothing -- not an error."""
    combined: dict[str, UserStats] = {}
    day = start_day
    while day <= end_day:
        payload = _load_day(entry, day)
        if payload:
            _merge_day(combined, payload)
        day += timedelta(days=1)
    return combined


def aggregate_all_time(entry: str) -> dict[str, UserStats]:
    """Like aggregate, but over every day ever recorded for this chat (globs STATS_DIR
    rather than walking a bounded date range) -- used by /stat, which reports a person's
    whole tracked history, not a fixed window."""
    combined: dict[str, UserStats] = {}
    if not STATS_DIR.exists():
        return combined
    prefix = _cache_key(entry)
    for path in sorted(STATS_DIR.glob(f"{prefix}_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        _merge_day(combined, payload)
    return combined


def resolve_period_window(period: str, tz) -> tuple[date, date]:
    if period not in PERIOD_LOOKBACK_DAYS:
        raise ValueError(f"unknown period '{period}' (expected one of {VALID_PERIODS})")
    today = datetime.now(tz).date()
    return today - timedelta(days=PERIOD_LOOKBACK_DAYS[period]), today


def parse_top_command(text: str) -> str:
    """Extracts the period keyword from a "/top ..." command. Defaults to "today" if
    none is given, or it's not one of VALID_PERIODS, rather than rejecting the request."""
    parts = text.strip().split()
    if len(parts) > 1 and parts[1].lower() in VALID_PERIODS:
        return parts[1].lower()
    return "today"


async def _live_today_users(client, chat_ref, tz, log=print) -> dict:
    """Computes (but does NOT persist -- see record_day) today's per-user stats fresh, by
    fetching today's current transcript same as /summary would (reusing the same
    30-minute-TTL per-day cache, so this doesn't add Telegram load beyond what querying
    /summary for today already costs). Merged into every query below so "/top
    today"/"week"/"month" and "/stat" reflect today's activity as it happens, rather than
    only ever showing data through yesterday -- today itself only gets permanently
    recorded once, by the midnight rollover, once it's actually over (see record_day);
    until then, every query recomputes it fresh instead of reading a persisted file."""
    today = datetime.now(tz).date()
    _, messages = await telegram_fetch.fetch_range_messages_cached(
        client=client, chat_ref=chat_ref, start_day=today, end_day=today, tz=tz, log=log,
    )
    return compute_day_stats(messages)


async def aggregate_live(
    client, chat_ref, entry: str, start_day: date, end_day: date, tz, log=print
) -> dict[str, UserStats]:
    """Like aggregate(), but if `end_day` is today, also merges in today's live snapshot
    (see _live_today_users) on top of whatever's already recorded for earlier days in the
    range -- today is deliberately excluded from the aggregate() call itself (capped at
    yesterday) so it's never read from a persisted file and live-merged at the same time,
    which would double-count it."""
    today = datetime.now(tz).date()
    historical_end = min(end_day, today - timedelta(days=1))
    combined = aggregate(entry, start_day, historical_end) if start_day <= historical_end else {}
    if start_day <= today <= end_day:
        _merge_day(combined, {"users": await _live_today_users(client, chat_ref, tz, log=log)})
    return combined


async def aggregate_all_time_live(client, chat_ref, entry: str, tz, log=print) -> dict[str, UserStats]:
    """Like aggregate_all_time(), plus today's live snapshot merged on top -- see
    aggregate_live's same reasoning. Used by /stat, and by resolve_stat_target so someone
    who has only ever posted today (no recorded day yet at all) is still found."""
    combined = aggregate_all_time(entry)
    _merge_day(combined, {"users": await _live_today_users(client, chat_ref, tz, log=log)})
    return combined


async def format_top(client, chat_ref, entry: str, period: str, tz, top_n: int, log=print) -> str:
    start, end = resolve_period_window(period, tz)
    ranked = sorted(
        (await aggregate_live(client, chat_ref, entry, start, end, tz, log=log)).values(),
        key=lambda s: s.score,
        reverse=True,
    )[:top_n]
    if not ranked:
        return "Пока нет данных за этот период."
    lines = ["🏆 Топ активистов:", ""]
    for i, s in enumerate(ranked, start=1):
        lines.append(f"{i}. {s.display_name} — {s.score} очков")
    return "\n".join(lines)


def _favorite_hour_label(hours: dict) -> str:
    if not hours:
        return "нет данных"
    best_hour = int(max(hours.items(), key=lambda kv: kv[1])[0])
    return f"{best_hour:02d}:00–{(best_hour + 1) % 24:02d}:00"


def format_stat(user: UserStats) -> str:
    avg = user.messages / user.active_days if user.active_days else 0.0
    last_seen = "нет данных"
    if user.last_message_at:
        last_seen = datetime.fromisoformat(user.last_message_at).strftime("%Y-%m-%d %H:%M")
    return (
        "Статистика пользователя:\n\n"
        f"Имя: {user.display_name}\n"
        f"Сообщений: {user.messages}\n"
        f"Среднее сообщений в день: {avg:.1f}\n"
        f"Активность: {_ru_days(user.active_days)}\n"
        f"Любимое время: {_favorite_hour_label(user.hours)}\n"
        f"Последняя активность: {last_seen}"
    )


def _find_user(users: dict[str, UserStats], name_or_username: str) -> UserStats | None:
    """Case-insensitive match against a tracked user's @username (exact) or a substring
    of their display name -- same precedence as telegram_fetch.sender_matches, but
    against an already-aggregated {user_id: UserStats} dict instead of a live transcript."""
    needle = name_or_username.strip().lstrip("@").lower()
    if not needle:
        return None
    for s in users.values():
        if s.username and needle == s.username.lower():
            return s
    for s in users.values():
        if needle in s.display_name.lower():
            return s
    return None


async def resolve_stat_target(
    client, chat_ref, entry: str, arg: str, requester_username: str | None, requester_display_name: str, tz, log=print
) -> UserStats | None:
    """Resolves who a /stat command is asking about: an explicit argument (@username or
    a name fragment) if given, otherwise the requester's own tracked stats -- tried first
    by @username (exact), falling back to their display name (substring). Fetches the
    all-time-plus-today-live aggregate exactly once regardless of how many of those three
    lookups it takes, rather than once per attempt."""
    all_time = await aggregate_all_time_live(client, chat_ref, entry, tz, log=log)
    if arg:
        return _find_user(all_time, arg)
    if requester_username:
        found = _find_user(all_time, requester_username)
        if found is not None:
            return found
    return _find_user(all_time, requester_display_name)
