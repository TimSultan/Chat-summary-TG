"""Per-user activity stats and a gamified leaderboard for a chat -- message/character/
media/reply counts, active days, and an hourly activity histogram, computed once per
calendar day from the SAME per-day transcript cache telegram_fetch.py already maintains
(see finalize_and_record, called by listener.py's midnight rollover job). Powers "/top
day|week|month|year|all" (a simple points leaderboard) and "/stat [username]" (one
person's tracked history). "/stat" accepts those same period keywords too (e.g. "/stat
all", "/stat year") -- given one, bare, with nothing else, it shows the same leaderboard
as the equivalent "/top" instead of searching for a user literally named "all" or "year"
(see parse_stat_period).

The daily rollover only ever permanently records a day once it's actually over (closed
days are immutable, see record_day) -- but a query for "today" run *during* today
obviously can't wait for that. Every query-facing function (format_top,
resolve_stat_target, and the aggregate_live/aggregate_all_time_live they call) therefore
merges in a freshly-computed, never-persisted snapshot of today on top of whatever's
already recorded for earlier days, so "/top today" and "/stat" both reflect activity as
it happens rather than only ever showing yesterday-and-earlier.

Storage: one JSON file per (chat, day) under DATA_DIR/cache/stats/<timezone>/, keyed by a hash of
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
    +100 per message tagged #япокрасил that has an actual photo attached (a "figurine
        painted" post -- see FIGURINE_HASHTAG; a video or a hashtag with no photo doesn't
        qualify). Also surfaced as its own raw count, "Покрашено фигурок", in /stat.
Points are never stored -- always recomputed on demand from the raw per-day counters for
whatever window (day/week/month/year, or -- for a bare /stat lookup -- every recorded
day) is asked about, so changing the point values later doesn't require re-processing
any history.
"""

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import telegram_fetch
from app_time import cache_namespace, now as app_now, resolve_timezone

DATA_DIR = Path(os.getenv("DATA_DIR", "."))
STATS_DIR = DATA_DIR / "cache" / "stats"


def _stats_dir() -> Path:
    return STATS_DIR / cache_namespace(resolve_timezone())

POINTS_PER_MESSAGE = 1
POINTS_PER_MEDIA_MESSAGE = 1
POINTS_PER_REPLY = 1
POINTS_PER_ACTIVE_DAY = 5
POINTS_PER_FIGURINE = 100

# Telegram_fetch.describe_media prepends one of these bracketed tags to a media message's
# cached text (e.g. "[Photo] nice caption"). Narrowed to photo/video only, per spec --
# stickers/voice notes/documents/etc. aren't counted as "media" here even though
# describe_media tags those too.
MEDIA_TAG_PREFIXES = ("[Photo]", "[Video]")

# A "figurine painted" post: the #япокрасил hashtag, anywhere in the caption, on a
# message that has an actual photo attached (a video or a hashtag-only text message
# doesn't count -- per spec it "has to contain image"). Matched case-insensitively since
# people don't reliably type Cyrillic hashtags in one consistent case.
FIGURINE_HASHTAG = "#япокрасил"

VALID_PERIODS = ("today", "week", "month", "year", "all")
# "day" isn't a distinct window -- it's just the word people actually type for "today".
# Normalized away by _normalize_period before anything looks at VALID_PERIODS.
PERIOD_ALIASES = {"day": "today"}
# Days back from today (inclusive of today) each bounded period covers -- rolling
# windows, not calendar-aligned (a "week" is always the last 7 days, not necessarily
# Mon-Sun, and a "year" the last 365 days, not Jan 1-Dec 31) so /top week/month/year are
# never thin just because it happens to be early in a calendar week/month/year. "all"
# isn't a bounded window at all -- format_top special-cases it to
# aggregate_all_time_live instead of a start/end range, so it has no entry here.
PERIOD_LOOKBACK_DAYS = {"today": 0, "week": 6, "month": 29, "year": 364}


def _cache_key(entry: str) -> str:
    return hashlib.sha1(entry.strip().lower().encode("utf-8")).hexdigest()[:16]


def _path(entry: str, day: date) -> Path:
    return _stats_dir() / f"{_cache_key(entry)}_{day.isoformat()}.json"


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
                "figurines": 0,
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
        if m.text.startswith("[Photo]") and FIGURINE_HASHTAG in m.text.lower():
            u["figurines"] += 1
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
    _stats_dir().mkdir(parents=True, exist_ok=True)
    users = compute_day_stats(messages)
    payload = {
        "entry": entry,
        "day": day.isoformat(),
        "recorded_at": app_now().isoformat(),
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
    figurines_painted: int = 0
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
            + self.figurines_painted * POINTS_PER_FIGURINE
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
        s.figurines_painted += u.get("figurines", 0)
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
    stats_dir = _stats_dir()
    if not stats_dir.exists():
        return combined
    prefix = _cache_key(entry)
    for path in sorted(stats_dir.glob(f"{prefix}_*.json")):
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


def strip_command_bot_mention(text: str, bot_username: str | None) -> str:
    """Telegram's own convention: "/command@botusername" with NO space before the "@" is
    how a command explicitly targets one bot -- typically auto-appended by the Telegram
    client when several bots in the same group share a command name -- the "@botusername"
    part is never meant as the command's actual argument. Strips exactly that (case-
    insensitive, and only if it matches THIS bot's own username) off the front of `text`,
    leaving whatever follows (the real argument, if there is one) untouched -- so
    "/stat@Trash_Modelist" alone becomes bare "/stat" (self-lookup), while
    "/stat@Trash_Modelist Someone" becomes "/stat Someone". A "/stat @Someone" with a
    space, or "/stat@Someone" where Someone isn't this bot, are both left alone entirely,
    since those are legitimate lookups for a different person, not a bot mention."""
    if not bot_username:
        return text
    pattern = re.compile(r"^(/\w+)@" + re.escape(bot_username) + r"\b", re.IGNORECASE)
    return pattern.sub(r"\1", text, count=1)


def _normalize_period(word: str) -> str | None:
    """Case-folds `word` and resolves the "day" -> "today" alias, then returns it only if
    it names one of VALID_PERIODS -- None for anything else (a username, a typo, ...)."""
    word = word.strip().lower()
    word = PERIOD_ALIASES.get(word, word)
    return word if word in VALID_PERIODS else None


def parse_top_command(text: str) -> str:
    """Extracts the period keyword from a "/top ..." command. Defaults to "today" if
    none is given, or it's not one of VALID_PERIODS (after alias normalization), rather
    than rejecting the request."""
    parts = text.strip().split()
    if len(parts) > 1:
        normalized = _normalize_period(parts[1])
        if normalized:
            return normalized
    return "today"


def parse_stat_period(arg: str) -> str | None:
    """Recognizes a "/stat <period>" call -- e.g. "/stat all" or "/stat year" -- so it
    shows the same leaderboard as the equivalent "/top <period>" instead of
    resolve_stat_target searching for a user literally named "all" or "year". Only
    matches when `arg` is a single bare word naming a period (after alias
    normalization): a leading "@" or any extra word makes it an unambiguous username/name
    lookup instead, and is left alone (returns None)."""
    arg = arg.strip()
    if not arg or arg.startswith("@") or len(arg.split()) != 1:
        return None
    return _normalize_period(arg)


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
    if period == "all":
        combined = await aggregate_all_time_live(client, chat_ref, entry, tz, log=log)
    else:
        start, end = resolve_period_window(period, tz)
        combined = await aggregate_live(client, chat_ref, entry, start, end, tz, log=log)
    ranked = sorted(combined.values(), key=lambda s: s.score, reverse=True)[:top_n]
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


def format_stat(user: UserStats, rank: int, total: int) -> str:
    avg = user.messages / user.active_days if user.active_days else 0.0
    last_seen = "нет данных"
    if user.last_message_at:
        last_seen = datetime.fromisoformat(user.last_message_at).strftime("%Y-%m-%d %H:%M")
    return (
        "📊 Статистика пользователя:\n\n"
        f"Имя: {user.display_name}\n"
        f"🏆 Очки: {user.score}\n"
        f"📈 Место в рейтинге: {rank} из {total}\n"
        f"Сообщений: {user.messages}\n"
        f"Среднее сообщений в день: {avg:.1f}\n"
        f"Активность: {_ru_days(user.active_days)}\n"
        f"Покрашено фигурок: {user.figurines_painted}\n"
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
) -> tuple[UserStats | None, int | None, int]:
    """Resolves who a /stat command is asking about: an explicit argument (@username or
    a name fragment) if given, otherwise the requester's own tracked stats -- tried first
    by @username (exact), falling back to their display name (substring). Fetches the
    all-time-plus-today-live aggregate exactly once regardless of how many of those three
    lookups it takes, rather than once per attempt.

    Returns (user, rank, total): `rank` is the person's 1-based position by score among
    everyone ever tracked for this chat (ties broken by dict iteration order, which is
    stable but arbitrary -- fine for a gamified leaderboard, not meant to be exact), and
    `total` is how many people that's out of. `rank` is None (with user) if no match was
    found; `total` is still meaningful in that case (could be used for a "N people
    tracked" message even without a match, though callers currently don't)."""
    all_time = await aggregate_all_time_live(client, chat_ref, entry, tz, log=log)
    total = len(all_time)
    if arg:
        user = _find_user(all_time, arg)
    else:
        user = _find_user(all_time, requester_username) if requester_username else None
        if user is None:
            user = _find_user(all_time, requester_display_name)
    if user is None:
        return None, None, total
    ranked = sorted(all_time.values(), key=lambda s: s.score, reverse=True)
    rank = next(i for i, s in enumerate(ranked, start=1) if s.user_id == user.user_id)
    return user, rank, total
