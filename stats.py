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
    Message points, PER DAY, are one of two things depending on whether that day predates
    word-tracking (see _has_word_data / UserStats.legacy_message_points vs. .words):
        +1 per message (flat), for any day recorded before this feature shipped -- kept
            forever exactly as it always scored, never reinterpreted.
        +(word count / words_per_point), for any day from after -- NOT a flat rate. A
            flat +1/message rewarded spamming lots of short messages just as much as
            writing one that says something; word count divided by the chat's own
            average words/message (see words_per_point) keeps a "typical" message worth
            about 1 point while a one-word "ok" is worth a fraction and a long one worth
            more.
    +1 per message containing a photo or video
    +1 per message that's a reply (see the is_reply note on UserStats.replies below)
    +5 per distinct calendar day the person posted at least once
    +150 per message tagged #япокрасил that has an actual photo OR video attached (a
        "figurine painted" post -- see FIGURINE_HASHTAG; a hashtag with no media doesn't
        qualify). Also surfaced as its own raw count, "Покрашено фигурок", in /stat.
Points are never stored -- always recomputed on demand from the raw per-day counters for
whatever window (day/week/month/year, or -- for a bare /stat lookup -- every recorded
day) is asked about, so changing the point values later doesn't require re-processing any
history -- and per the split above, a day's message-scoring RULE never changes after the
fact either, regardless of what happens to word-tracking later. words_per_point is the
one number here that isn't a fixed constant in code -- it's calibrated per chat, from
that chat's own real activity (see MIN_CALIBRATION_MESSAGES) -- but it's still fixed in
effect: calibrated exactly once, then cached to disk and reused forever, never
automatically recomputed (see words_per_point's own docstring). So a message's point
value, once it has one, stays stable over time -- the only thing that's ever freshly
recomputed per call, same as before this feature, is "today" itself (not yet finalized --
see record_day), not the scoring rule or conversion rate applied to any already-recorded
day.
"""

import hashlib
import json
import os
import random
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

POINTS_PER_MEDIA_MESSAGE = 1
POINTS_PER_REPLY = 1
POINTS_PER_ACTIVE_DAY = 5
POINTS_PER_FIGURINE = 200
# The flat rate every message scored under before word-based points existed. ONLY applied
# (via UserStats.legacy_message_points) to days recorded before word-tracking existed --
# those days keep exactly the score they always had, forever, rather than being
# reinterpreted by a formula that didn't exist when they happened. See _has_word_data.
LEGACY_POINTS_PER_MESSAGE = 1

# From the day this feature shipped onward, a message is worth its word count divided by
# the chat's OWN average words/message, rather than a flat +1/message -- so a typical-
# length message is still worth about 1 point, a long one worth more, and a one-word "ok"
# worth a fraction of one. That average is calibrated ONCE per chat, from the trailing
# WORDS_PER_POINT_LOOKBACK_DAYS days, then frozen -- see words_per_point and the module
# docstring's Scoring section.
WORDS_PER_POINT_LOOKBACK_DAYS = 3
# Fallback words-per-point when there isn't yet enough real data to calibrate from (see
# MIN_CALIBRATION_MESSAGES) -- avoids a division by zero / a degenerate "any message is
# worth infinite points" result.
DEFAULT_WORDS_PER_POINT = 5.0
# words_per_point won't freeze a calibration based on fewer than this many real
# (post-migration) messages -- a tiny sample right after this feature ships (or in a
# quiet chat) could easily be skewed by one or two messages, and unlike everything else
# here, a bad calibration doesn't get a chance to self-correct once cached.
MIN_CALIBRATION_MESSAGES = 30
# Bumped whenever the calibration algorithm changes in a way that could make an
# already-cached value wrong -- a cache file written under an older version is ignored
# and recalibrated fresh next call, rather than requiring anyone to manually find and
# delete it on the deployed Railway volume (which this codebase has no way to reach).
WORDS_PER_POINT_CACHE_VERSION = 2

# Telegram_fetch.describe_media prepends one of these bracketed tags to a media message's
# cached text (e.g. "[Photo] nice caption"). Narrowed to photo/video only, per spec --
# stickers/voice notes/documents/etc. aren't counted as "media" here even though
# describe_media tags those too.
MEDIA_TAG_PREFIXES = ("[Photo]", "[Video]")


def is_zero_content_message(text: str) -> bool:
    """A message that's JUST a sticker or JUST a GIF, with nothing else -- these don't
    count towards points at all (not even the base +1/message), so sticker/GIF spam
    can't inflate someone's message count, score, or leaderboard rank. Stickers never
    carry accompanying text in Telegram (no caption support there), so any
    "[Sticker ...]"-tagged message already qualifies by prefix alone. A GIF DOES support
    a caption, so only an exact bare "[GIF]" (no caption at all) qualifies -- "[GIF] nice
    one" still counts in full, since that's real authored content alongside the media."""
    return text.startswith("[Sticker") or text == "[GIF]"

# A "figurine painted" post: the #япокрасил hashtag, anywhere in the caption, on a
# message that has an actual photo OR video attached (a hashtag-only text message
# doesn't count -- per spec it "has to contain media"). Matched case-insensitively since
# people don't reliably type Cyrillic hashtags in one consistent case.
FIGURINE_HASHTAG = "#япокрасил"

# How many of a person's most recent figurine posts /stat LINKS TO (see
# figurine_message_links, the only place this cap is actually applied). Every qualifying
# post is saved and kept forever -- UserStats.recent_figurine_posts is deduped and sorted
# newest-first wherever it's built or merged (_merge_recent_figurine_posts) but never
# truncated there, precisely so format_procrastinators can still find the true most
# recent post even for someone who's painted far more than 3 total; only the /stat
# DISPLAY trims to this many.
RECENT_FIGURINE_LINKS = 3

# "Топ покрастинаторов": sent automatically every PROCRASTINATOR_DIGEST_INTERVAL_DAYS
# days at PROCRASTINATOR_DIGEST_HOUR local (app-timezone) time -- see run_stats_rollover's
# digest loop and should_send_procrastinator_digest -- calling out exactly
# PROCRASTINATOR_LIST_SIZE people (fewer only if there simply aren't that many
# candidates), walking down the last-30-days scorers (the same window /top month uses,
# NOT all-time -- see format_procrastinators) from the top, SKIPPING (not counting
# towards the list) anyone who's posted a #япокрасил+photo/video within the last
# PROCRASTINATOR_INACTIVE_DAYS days -- so the list is always full-size instead of
# shrinking on a day when most of the top scorers happen to be caught up.
PROCRASTINATOR_LIST_SIZE = 21
PROCRASTINATOR_INACTIVE_DAYS = 14
PROCRASTINATOR_DIGEST_HOUR = 19
PROCRASTINATOR_DIGEST_INTERVAL_DAYS = 2

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


def _procrastinator_last_sent_path(entry: str) -> Path:
    return _stats_dir() / f"{_cache_key(entry)}_procrastinator_last_sent"


def procrastinator_last_sent(entry: str) -> date | None:
    """The calendar day (app timezone) the automatic "Топ покрастинаторов" digest was
    last sent for `entry`, or None if it's never gone out -- a plain date string in a
    marker file (not a full day-file: nothing else about the send needs remembering).
    Used by should_send_procrastinator_digest to enforce the every-other-day cadence
    across restarts."""
    path = _procrastinator_last_sent_path(entry)
    if not path.exists():
        return None
    try:
        return date.fromisoformat(path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def mark_procrastinator_sent(entry: str, day: date) -> None:
    _stats_dir().mkdir(parents=True, exist_ok=True)
    _procrastinator_last_sent_path(entry).write_text(day.isoformat(), encoding="utf-8")


def should_send_procrastinator_digest(
    entry: str, today: date, interval_days: int = PROCRASTINATOR_DIGEST_INTERVAL_DAYS
) -> bool:
    """Whether today's PROCRASTINATOR_DIGEST_HOUR check-in (see run_stats_rollover's
    digest loop) is a "send" day for `entry`'s every-other-day cadence: true the very
    first time (never sent before) or once `interval_days` or more have passed since the
    last send. Using elapsed days rather than e.g. an odd/even day-of-year means a missed
    check-in (downtime spanning the send hour) still catches up on the next one instead of
    permanently drifting the cadence."""
    last = procrastinator_last_sent(entry)
    return last is None or (today - last).days >= interval_days


def is_figurine_caption(text: str) -> bool:
    """Whether `text` (a raw caption/message text, NOT the "[Photo] "/"[Video] " tagged
    form compute_day_stats works with) carries the #япокрасил hashtag. Callers still have
    to check for an attached photo OR video themselves -- what that looks like differs by
    API (Telethon's `msg.photo`/`msg.video` vs. the Bot API's "photo"/"video" key), so
    there's no one shared check for that half."""
    return FIGURINE_HASHTAG in (text or "").lower()


def _live_figurines_path(entry: str, day: date) -> Path:
    return _stats_dir() / f"{_cache_key(entry)}_{day.isoformat()}_live_figurines.json"


def record_figurine_live(
    entry: str, day: date, user_id, username: str | None, display_name: str,
    message_id: int | None = None, log=print,
) -> int:
    """Bumps one user's figurine-painted count for `day` the instant a qualifying
    message is seen live (listener.py's on_message, which sees every message as it
    arrives) -- a plain local read-modify-write, no Telegram call involved, so /stat and
    /top reflect it immediately rather than waiting on the transcript cache's own TTL
    (see _live_today_users, which overlays this on top of that cache for "today").
    `message_id` (if given) is appended to this user's recent-posts list (deduped and
    kept newest-first, but never truncated -- see RECENT_FIGURINE_LINKS), for /stat's
    links to them (see figurine_message_links) -- Telegram has no deep link for a
    filtered/scoped search, only a link to one specific message. Returns the user's new
    total for `day`, for logging.

    Kept in a file separate from the per-day file `_path` writes (record_day's finalized,
    immutable snapshot of a CLOSED day) -- writing here must never be mistaken by
    is_recorded for that day already being finalized. Cleared by record_day once `day`
    actually closes, since the finalized file then carries the authoritative count."""
    _stats_dir().mkdir(parents=True, exist_ok=True)
    path = _live_figurines_path(entry, day)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (json.JSONDecodeError, OSError):
        data = {}
    key = str(user_id)
    u = data.setdefault(key, {"username": None, "display_name": display_name, "count": 0, "recent_posts": []})
    if username:
        u["username"] = username
    if display_name:
        u["display_name"] = display_name
    u["count"] += 1
    u["recent_posts"] = _merge_recent_figurine_posts(
        [tuple(p) for p in u.get("recent_posts", [])], [(app_now().isoformat(), message_id)]
    )
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    log(f"[stats] figurine recorded live for '{entry}' user {key}: {u['count']} today")
    return u["count"]


def _load_live_figurines(entry: str, day: date) -> dict:
    path = _live_figurines_path(entry, day)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _clear_live_figurines(entry: str, day: date) -> None:
    try:
        _live_figurines_path(entry, day).unlink(missing_ok=True)
    except OSError:
        pass


def _ru_days(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return f"{n} дня"
    return f"{n} дней"


def _current_streak(active_day_dates: set, today: date) -> int:
    """How many CONSECUTIVE days, counting backward, this person has posted at least
    once -- the number shown next to /stat's fire emoji. Starts counting from today if
    they've already posted today, otherwise from yesterday: the streak isn't considered
    broken just because today isn't over yet and they haven't posted YET (matching how
    "streak" counters commonly work elsewhere, e.g. Duolingo) -- it only actually breaks
    once a full day passes with no post at all. Walks backward through
    UserStats.active_day_dates (the actual dates behind the active_days count) until it
    hits a gap."""
    day = today if today.isoformat() in active_day_dates else today - timedelta(days=1)
    streak = 0
    while day.isoformat() in active_day_dates:
        streak += 1
        day -= timedelta(days=1)
    return streak


def compute_day_stats(messages: list) -> dict:
    """Returns {user_id_str: {...counters...}} for one day's messages (a full day's
    telegram_fetch.ChatMessage list). Messages with no resolvable sender_id (rare --
    sender resolution failed) are skipped, since there's no stable key to attribute them
    to -- same for a zero-content sticker/GIF-only message (see is_zero_content_message):
    both are excluded entirely, not just from scoring, so they don't even nudge
    active_days or last_message_at. `chars` counts the full cached text including any
    media-tag prefix (e.g. "[Photo] "), not just a caption -- a minor, deliberate
    over-count for media messages given chars isn't scored and the cache doesn't
    separately store a caption-only string."""
    users: dict[str, dict] = {}
    for m in messages:
        if m.sender_id is None or is_zero_content_message(m.text):
            continue
        key = str(m.sender_id)
        u = users.setdefault(
            key,
            {
                "username": None,
                "display_name": m.sender_name,
                "messages": 0,
                "chars": 0,
                "words": 0,
                "media": 0,
                "replies": 0,
                "figurines": 0,
                # [ts, message_id] pairs, one per qualifying message this day -- NOT yet
                # trimmed to RECENT_FIGURINE_LINKS (that happens once, at merge time in
                # _merge_day, across the whole history rather than per day).
                "figurine_posts": [],
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
        u["words"] += len(m.text.split())
        if m.text.startswith(MEDIA_TAG_PREFIXES):
            u["media"] += 1
        ts = m.dt_local.isoformat()
        if m.text.startswith(MEDIA_TAG_PREFIXES) and is_figurine_caption(m.text):
            u["figurines"] += 1
            u["figurine_posts"].append([ts, m.message_id])
        if m.is_reply:
            u["replies"] += 1
        hour_key = str(m.dt_local.hour)
        u["hours"][hour_key] = u["hours"].get(hour_key, 0) + 1
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
    # The just-finalized payload above now carries the authoritative figurine count for
    # `day` (recomputed from the full transcript) -- the live counter's job (see
    # record_figurine_live) was only ever to cover today before that existed.
    _clear_live_figurines(entry, day)
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
    # Only from days with real word-tracking (see _has_word_data) -- the words/points
    # formula (see score) applies exclusively to those. `legacy_message_points` below is
    # the counterpart for days recorded before word-tracking existed.
    words: int = 0
    # Message points already locked in from days recorded BEFORE word-tracking existed --
    # each such day contributes its original flat messages*LEGACY_POINTS_PER_MESSAGE value
    # (see _merge_day), computed once at merge time and simply summed here, so an old day's
    # score can never change again no matter what happens to words_per_point later. `words`
    # above is the separate, live counterpart for days that DO have real word data.
    legacy_message_points: int = 0
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
    # ALL of this person's [ts, message_id] figurine posts ever, newest first, never
    # truncated (see RECENT_FIGURINE_LINKS) -- so /stat can link straight to their most
    # recent few (see figurine_message_links, which slices to RECENT_FIGURINE_LINKS at
    # DISPLAY time) while format_procrastinators can still find the true most recent post
    # for anyone regardless of their total count.
    recent_figurine_posts: list = field(default_factory=list)
    active_days: int = 0
    # ISO date strings ("YYYY-MM-DD") for every day this person posted at least once --
    # the actual DATES behind the `active_days` count above, needed to walk backward day
    # by day for a "current streak" (see _current_streak). Populated by _merge_day from
    # each merged day's own "day" key (present on every recorded day-file and on every
    # synthetic live-today payload built for this purpose -- see aggregate_live etc.).
    active_day_dates: set = field(default_factory=set)
    hours: dict = field(default_factory=dict)
    last_message_at: str | None = None

    def score(self, words_per_point: float) -> int:
        """`words_per_point` -- see the function of that name -- is this chat's frozen
        words/message baseline. It's a required argument rather than a module constant
        because, unlike the other POINTS_PER_* values, it's calibrated per chat (from
        that chat's own real activity) rather than picked as one universal number (see
        the module docstring's Scoring section) -- but once calibrated, it's fixed, same
        as the others. `legacy_message_points` (days from before word-tracking existed)
        and `words / words_per_point` (days from after) are two DIFFERENT ways of
        scoring a message, added side by side rather than one replacing the other --
        see UserStats.legacy_message_points."""
        return round(
            self.legacy_message_points
            + self.words / words_per_point
            + self.media * POINTS_PER_MEDIA_MESSAGE
            + self.replies * POINTS_PER_REPLY
            + self.active_days * POINTS_PER_ACTIVE_DAY
            + self.figurines_painted * POINTS_PER_FIGURINE
        )


def _merge_recent_figurine_posts(existing: list, new_posts) -> list:
    """Combines `existing` [ts, message_id] pairs with `new_posts` (any iterable of the
    same shape) and sorts newest-first -- deliberately NEVER truncates (see
    RECENT_FIGURINE_LINKS's own docstring): every qualifying post is kept forever, so
    format_procrastinators can always find the TRUE most recent post regardless of how
    many someone has painted in total. Callers that only want a bounded list for display
    (figurine_message_links) slice it themselves. The one place this merge+dedup happens,
    used by both _merge_day (across recorded days) and record_figurine_live/
    _live_today_users (today's live counter).

    De-dupes by message_id first: the SAME message can legitimately reach this from two
    independent sources with two different timestamps -- record_figurine_live's live
    counter (stamped the instant the message was seen) and, once the transcript cache
    catches up, compute_day_stats independently re-deriving that same day's posts from
    the actual cached messages (stamped from the message's own dt_local). Without this,
    that one post would show up twice in "Последние N работы" -- a real bug caught by
    the user ("duplicated 1 and 2") once the transcript cache had caught up with a
    same-day live post."""
    combined = existing + list(new_posts)
    by_message_id: dict = {}
    for ts, message_id in combined:
        current = by_message_id.get(message_id)
        if current is None or ts > current[0]:
            by_message_id[message_id] = (ts, message_id)
    deduped = list(by_message_id.values())
    deduped.sort(key=lambda p: p[0], reverse=True)
    return deduped


def _has_word_data(payload: dict) -> bool:
    """False for a recorded day-file from before the words-per-message feature shipped:
    old files never wrote a "words" key on any user record, while compute_day_stats has
    written it (even as an explicit 0) for every user since. Two callers rely on this:

    - _merge_day uses it to decide, per day, which of the two message-scoring paths that
      day's messages go through -- UserStats.legacy_message_points (the original flat
      +1/message, for days from before this existed) or UserStats.words (the word-count
      formula, for days from after) -- so an old day keeps exactly the score it always
      had, forever, rather than being silently reinterpreted by a formula that didn't
      exist when it happened.
    - words_per_point's calibration uses it to skip pre-migration days entirely when
      averaging, rather than treating their real, un-tracked word counts as zero (which
      would understate the chat's true words/message and freeze a permanently-too-
      generous rate -- the actual cause of a real production bug: right after this
      feature shipped, the lookback window was mostly old zero-word days, so the
      one-time calibration froze on an artificially tiny baseline, letting one quiet
      person's single long message outscore actually chatty regulars).

    A day with no messages at all (empty `users`) contributes nothing either way and is
    treated as unusable too, just to keep this check simple."""
    users = payload.get("users") or {}
    return bool(users) and all("words" in u for u in users.values())


def _merge_day(combined: dict[str, UserStats], payload: dict) -> None:
    word_scored_day = _has_word_data(payload)
    day_str = payload.get("day")
    for user_id, u in payload.get("users", {}).items():
        s = combined.setdefault(user_id, UserStats(user_id=user_id))
        if u.get("username"):
            s.username = u["username"]
        if u.get("display_name"):
            s.display_name = u["display_name"]
        s.messages += u.get("messages", 0)
        s.chars += u.get("chars", 0)
        if word_scored_day:
            s.words += u.get("words", 0)
        else:
            s.legacy_message_points += u.get("messages", 0) * LEGACY_POINTS_PER_MESSAGE
        s.media += u.get("media", 0)
        s.replies += u.get("replies", 0)
        s.figurines_painted += u.get("figurines", 0)
        if u.get("figurine_posts"):
            s.recent_figurine_posts = _merge_recent_figurine_posts(s.recent_figurine_posts, u["figurine_posts"])
        if u.get("messages", 0) > 0:
            s.active_days += 1
            if day_str:
                s.active_day_dates.add(day_str)
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
    whole tracked history, not a fixed window. The glob is deliberately narrowed to the
    exact "<prefix>_YYYY-MM-DD.json" shape `_path` writes, NOT a loose "<prefix>_*.json"
    -- the stats dir also holds other <prefix>_-prefixed auxiliary files for this entry
    (the live figurine counter, `_live_figurines_path`; the procrastinator-digest
    bootstrap marker) that must never be mistaken for a recorded day."""
    combined: dict[str, UserStats] = {}
    stats_dir = _stats_dir()
    if not stats_dir.exists():
        return combined
    prefix = _cache_key(entry)
    for path in sorted(stats_dir.glob(f"{prefix}_????-??-??.json")):
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


# "pokras" (Latin, as typed) and "покрас" (its natural Cyrillic spelling, added as a
# free alias) both call up the "Топ покрастинаторов" list on demand -- see
# is_procrastinator_command/format_procrastinators.
PROCRASTINATOR_KEYWORDS = ("pokras", "покрас")


def is_procrastinator_command(arg: str) -> bool:
    """Recognizes a "/stat pokras" call -- shows the same "Топ покрастинаторов" call-out
    format_procrastinators sends automatically (see PROCRASTINATOR_DIGEST_HOUR/
    PROCRASTINATOR_DIGEST_INTERVAL_DAYS), on demand instead of waiting for the next
    scheduled send. This on-demand reply still self-deletes like every other /stat reply
    (STATS_DELETE_AFTER, see listener.py/bot_listener.py) -- unlike the automatic digest,
    which is deliberately left in the chat. Same single-bare-word matching rule as
    parse_stat_period, so a real username that happens to resemble one of
    PROCRASTINATOR_KEYWORDS isn't shadowed (checked first by callers regardless, since
    these two keyword sets don't overlap)."""
    arg = arg.strip().lower()
    return arg in PROCRASTINATOR_KEYWORDS


async def _live_today_users(client, chat_ref, entry: str, tz, log=print) -> dict:
    """Computes (but does NOT persist -- see record_day) today's per-user stats fresh, by
    fetching today's current transcript same as /summary would (reusing the same
    30-minute-TTL per-day cache, so this doesn't add Telegram load beyond what querying
    /summary for today already costs). Merged into every query below so "/top
    today"/"week"/"month" and "/stat" reflect today's activity as it happens, rather than
    only ever showing data through yesterday -- today itself only gets permanently
    recorded once, by the midnight rollover, once it's actually over (see record_day);
    until then, every query recomputes it fresh instead of reading a persisted file.

    Figurine counts are the one exception to "fresh from the transcript cache": that
    cache can lag up to transcript_cache.TODAY_TTL_SECONDS behind, but record_figurine_live
    updates the instant a qualifying message is seen, so its count for today is overlaid
    here (taking the max of the two -- the live count could itself be momentarily behind
    right after a restart, if the transcript cache already picked up a qualifying message
    from while this process was down) rather than waiting on the next transcript refresh."""
    today = datetime.now(tz).date()
    _, messages = await telegram_fetch.fetch_range_messages_cached(
        client=client, chat_ref=chat_ref, start_day=today, end_day=today, tz=tz, log=log,
    )
    users = compute_day_stats(messages)
    for key, live in _load_live_figurines(entry, today).items():
        u = users.setdefault(
            key,
            {
                "username": None, "display_name": live.get("display_name", "Unknown"),
                "messages": 0, "chars": 0, "words": 0, "media": 0, "replies": 0, "figurines": 0,
                "figurine_posts": [],
                "hours": {}, "last_message_at": None,
            },
        )
        u["figurines"] = max(u.get("figurines", 0), live.get("count", 0))
        if live.get("recent_posts"):
            u["figurine_posts"] = _merge_recent_figurine_posts(u["figurine_posts"], live["recent_posts"])
        if live.get("username"):
            u["username"] = live["username"]
        if live.get("display_name"):
            u["display_name"] = live["display_name"]
    return users


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
        live_users = await _live_today_users(client, chat_ref, entry, tz, log=log)
        _merge_day(combined, {"day": today.isoformat(), "users": live_users})
    return combined


async def aggregate_all_time_live(client, chat_ref, entry: str, tz, log=print) -> dict[str, UserStats]:
    """Like aggregate_all_time(), plus today's live snapshot merged on top -- see
    aggregate_live's same reasoning. Used by /stat, and by resolve_stat_target so someone
    who has only ever posted today (no recorded day yet at all) is still found."""
    combined = aggregate_all_time(entry)
    today = datetime.now(tz).date()
    live_users = await _live_today_users(client, chat_ref, entry, tz, log=log)
    _merge_day(combined, {"day": today.isoformat(), "users": live_users})
    return combined


def _words_per_point_path(entry: str) -> Path:
    return _stats_dir() / f"{_cache_key(entry)}_words_per_point.json"


def _load_words_per_point(entry: str) -> float | None:
    path = _words_per_point_path(entry)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != WORDS_PER_POINT_CACHE_VERSION:
            return None
        return float(data["words_per_point"])
    except (json.JSONDecodeError, OSError, KeyError, ValueError, TypeError):
        return None


def _save_words_per_point(entry: str, value: float) -> None:
    _stats_dir().mkdir(parents=True, exist_ok=True)
    _words_per_point_path(entry).write_text(
        json.dumps(
            {
                "words_per_point": value,
                "calibrated_at": app_now().isoformat(),
                "version": WORDS_PER_POINT_CACHE_VERSION,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


async def words_per_point(client, chat_ref, entry: str, tz, log=print) -> float:
    """The chat-wide average words/message this chat's scoring treats as "1 point" (see
    UserStats.score). Calibrated ONCE -- the first time this chat has at least
    MIN_CALIBRATION_MESSAGES real (post-migration) messages to calibrate from -- out of
    the trailing WORDS_PER_POINT_LOOKBACK_DAYS recorded days (skipping any that predate
    word-tracking, see _has_word_data) plus today's live snapshot (always real, since
    it's computed fresh rather than read from a persisted file). Once calibrated, it's
    cached to disk (see _words_per_point_path) and simply read back on every later call,
    never recomputed: unlike almost everything else in this module, a message posted a
    month ago must keep the same point value it always has, not silently reprice itself
    every time someone happens to check /top. To force a one-time recalibration (e.g. the
    chat's typical message length has genuinely shifted), delete this entry's cache file
    -- nothing does that automatically."""
    cached = _load_words_per_point(entry)
    if cached is not None:
        return cached
    today = datetime.now(tz).date()
    total_words = 0
    total_messages = 0
    day = today - timedelta(days=WORDS_PER_POINT_LOOKBACK_DAYS - 1)
    while day < today:
        payload = _load_day(entry, day)
        if payload and _has_word_data(payload):
            for u in payload["users"].values():
                total_words += u.get("words", 0)
                total_messages += u.get("messages", 0)
        day += timedelta(days=1)
    for u in (await _live_today_users(client, chat_ref, entry, tz, log=log)).values():
        total_words += u.get("words", 0)
        total_messages += u.get("messages", 0)
    if total_messages < MIN_CALIBRATION_MESSAGES:
        log(
            f"[stats] not enough post-migration messages yet to calibrate words_per_point "
            f"for '{entry}' ({total_messages}/{MIN_CALIBRATION_MESSAGES}) -- using the "
            f"default of {DEFAULT_WORDS_PER_POINT} for now, uncached, will retry next call"
        )
        return DEFAULT_WORDS_PER_POINT
    value = total_words / total_messages
    _save_words_per_point(entry, value)
    log(f"[stats] calibrated words_per_point for '{entry}': {value:.2f} words/message (frozen, won't auto-update)")
    return value


async def format_top(client, chat_ref, entry: str, period: str, tz, top_n: int, log=print) -> str:
    if period == "all":
        combined = await aggregate_all_time_live(client, chat_ref, entry, tz, log=log)
    else:
        start, end = resolve_period_window(period, tz)
        combined = await aggregate_live(client, chat_ref, entry, start, end, tz, log=log)
    wpp = await words_per_point(client, chat_ref, entry, tz, log=log)
    ranked = sorted(combined.values(), key=lambda s: s.score(wpp), reverse=True)[:top_n]
    if not ranked:
        return "Пока нет данных за этот период."
    lines = ["🏆 Топ активистов:", ""]
    for i, s in enumerate(ranked, start=1):
        lines.append(f"{i}. {s.display_name} — {s.score(wpp)} очков")
    return "\n".join(lines)


PROCRASTINATOR_REMINDER = "Скидывайте свою последнюю или новую работу с хэштегом #япокрасил"
PROCRASTINATOR_TAUNT = "Языком чесать - не кистями работать."
# Purely decorative -- one random pick per line (see format_procrastinators), just to
# make the call-out list less of a wall of plain text. Themed around slowness/napping to
# match the existing 🐌 header, but there's no other meaning attached to WHICH one a
# given person gets -- re-rolled fresh every send, not tied to the person.
PROCRASTINATOR_NAME_EMOJI = ("🐢", "🦥", "🐌", "😴", "🛌", "⏰", "🙈", "💤", "🫠", "🥱")


async def format_procrastinators(
    client, chat_ref, entry: str, tz,
    list_size: int = PROCRASTINATOR_LIST_SIZE, inactive_days: int = PROCRASTINATOR_INACTIVE_DAYS, log=print,
) -> str | None:
    """The "Топ покрастинаторов" call-out, sent automatically every
    PROCRASTINATOR_DIGEST_INTERVAL_DAYS days (see run_stats_rollover's digest loop) and
    available on demand via "/stat pokras": always exactly `list_size` people (fewer only
    if there simply aren't that many candidates at all), walking DOWN the last-30-days
    scorers for `entry` (same rolling window /top month uses -- "currently active", NOT
    all-time, so someone who was huge months ago but has since gone quiet isn't
    considered just because of old history) from the top, SKIPPING -- not counting
    towards `list_size` -- anyone who's posted a #япокрасил+photo/video within the last
    `inactive_days` days. Unlike a fixed top-N pool filtered afterwards (the old design),
    this keeps walking as far down the ranking as it takes, so the list is always full
    whenever enough overdue people exist at all, instead of shrinking on a day when most
    of the top scorers happen to already be caught up. Includes people who have NEVER
    posted one at all, not just people who used to and stopped -- both count as "hasn't
    sent new work recently" -- shown with a distinct line since there's no "last time" to
    count days from for them.

    The ranking (who's "active", and what order candidates get considered in) and the
    recency check (when did they last post) deliberately use two different windows:
    ranking is by score over the last 30 days, but "last posted" is looked up against
    their WHOLE history (aggregate_all_time) -- otherwise someone overdue by more than a
    month would wrongly look like they've never posted at all, just because that post
    fell outside the 30-day ranking window. Because the recency check goes through
    `all_time` (which holds an entry for every tracked user, independent of whether
    they're ever reached/shown here), someone's "last posted" timer is always correctly
    updated by a fresh post even on a day they're skipped for already being caught up, or
    never reached at all because `list_size` filled up before the ranking got to them.

    `client`/`chat_ref` are required (unlike the old, purely file-based design) so today
    can be re-derived via _live_today_users -- the SAME fresh-transcript-plus-live-file
    merge /stat and /top already rely on -- fetched ONCE and reused for both the ranking
    and the recency lookup below. This matters: a version of this function that trusted
    ONLY the local live-figurine-counter file (record_figurine_live's write, which only
    ever happens from listener.py's on_message seeing a message live, in real time) was
    found in production to disagree with /stat -- someone whose post /stat correctly
    showed (via its own fresh transcript re-derivation) still showed up here as "never
    posted", because the live-counter file alone doesn't cover every way a today-post can
    become known (e.g. a process restart between the post and the check-in means
    on_message never ran for it, yet a fresh fetch still finds it in the actual message
    history). Using the same _live_today_users merge as /stat closes that gap.

    Returns None if there's nobody to call out at all (nobody ranked, or everyone ranked
    has already posted within the window) -- callers should simply not send anything."""
    today = datetime.now(tz).date()
    live_today = await _live_today_users(client, chat_ref, entry, tz, log=log)

    month_start, month_end = resolve_period_window("month", tz)
    historical_end = min(month_end, today - timedelta(days=1))
    pool = aggregate(entry, month_start, historical_end) if month_start <= historical_end else {}
    _merge_day(pool, {"day": today.isoformat(), "users": live_today})
    wpp = await words_per_point(client, chat_ref, entry, tz, log=log)
    ranked = sorted(pool.values(), key=lambda s: s.score(wpp), reverse=True)

    all_time = aggregate_all_time(entry)
    _merge_day(all_time, {"day": today.isoformat(), "users": live_today})

    # (sort_key, line) pairs -- sort_key is days-since-last-post, with a large sentinel
    # for "never posted" so those sort to the top (the most overdue, in spirit) without
    # needing a fabricated day count.
    entries: list[tuple[int, str]] = []
    for s in ranked:
        if len(entries) >= list_size:
            break
        # Deliberately the display name, not an @username mention -- per explicit user
        # request, this call-out shouldn't ping people (it repeats every
        # PROCRASTINATOR_DIGEST_INTERVAL_DAYS days, unlike a one-off notification). A
        # random decorative emoji is prepended per line (see PROCRASTINATOR_NAME_EMOJI).
        who = f"{random.choice(PROCRASTINATOR_NAME_EMOJI)} {s.display_name}"
        history = all_time.get(s.user_id)
        posts = history.recent_figurine_posts if history else []
        if not posts:
            entries.append((10**9, f"{who} — ещё ни разу не скидывал(а) работы"))
            continue
        last_at = posts[0][0]  # newest-first, see _merge_recent_figurine_posts
        days_since = (today - datetime.fromisoformat(last_at).date()).days
        if days_since >= inactive_days:
            entries.append((days_since, f"{who} — не скидывал работы {_ru_days(days_since)}"))
        # else: posted recently -- skip without counting towards list_size, keep walking
    if not entries:
        return None
    entries.sort(key=lambda pair: pair[0], reverse=True)
    lines = [line for _, line in entries]
    header = "🐌 Список Покрастинаторов\n2 недели не скидывали свои покрасы:\n\n"
    return header + "\n".join(lines) + f"\n\n{PROCRASTINATOR_TAUNT}\n{PROCRASTINATOR_REMINDER}"


def _favorite_hour_label(hours: dict) -> str:
    if not hours:
        return "нет данных"
    best_hour = int(max(hours.items(), key=lambda kv: kv[1])[0])
    return f"{best_hour:02d}:00–{(best_hour + 1) % 24:02d}:00"


def figurine_message_link(chat_username: str | None, chat_id: int | None, message_id: int | None) -> str | None:
    """Best-effort t.me link straight to someone's single most recent #япокрасил+photo/
    video post -- the closest thing to "show me all of theirs" that Telegram actually exposes
    as a URL: there is no documented deep link for a sender+hashtag-filtered in-chat
    search (only the in-app search UI supports that combination, entered by hand), just a
    link to one specific message (see message_id, tracked by compute_day_stats/
    record_figurine_live). `chat_id` must be the "marked" id (event.chat_id in
    listener.py, chat_id straight from the Bot API -- both use the same numbering, see
    bot_listener._resolve_chat_id), NOT Telethon's raw entity.id.

    Prefers the public-username form (works for anyone); falls back to the "-100"-prefixed
    marked-id form (t.me/c/..., only resolvable by an existing chat member) for a private
    supergroup/channel with no username. None if there's nothing to link yet, or the chat
    is a small basic group (never upgraded to a supergroup), which has no stable t.me/c/
    numbering at all."""
    if message_id is None:
        return None
    if chat_username:
        return f"https://t.me/{chat_username}/{message_id}"
    if chat_id is not None:
        marked = str(chat_id)
        if marked.startswith("-100"):
            return f"https://t.me/c/{marked[4:]}/{message_id}"
    return None


def figurine_message_links(chat_username: str | None, chat_id: int | None, user: UserStats) -> list[str]:
    """figurine_message_link, applied across the `RECENT_FIGURINE_LINKS` most recent of
    `user.recent_figurine_posts` (which itself holds the person's WHOLE history, newest
    first, never truncated -- see RECENT_FIGURINE_LINKS's own docstring) -- what /stat's
    "Последние N работы" section links to. This is the one place that cap is actually
    applied. Skips (rather than emitting a broken link for) any post that can't be
    linked, e.g. a chat with neither a public username nor a resolvable marked id -- see
    figurine_message_link's own docstring for when that happens -- so the result can have
    fewer than RECENT_FIGURINE_LINKS links, never more."""
    links = []
    for _, message_id in user.recent_figurine_posts[:RECENT_FIGURINE_LINKS]:
        link = figurine_message_link(chat_username, chat_id, message_id)
        if link:
            links.append(link)
    return links


def format_stat(
    user: UserStats, rank: int, total: int, score: int, streak: int, figurine_links: list[str] | None = None
) -> str:
    """`score` and `streak` are computed by the caller (resolve_stat_target) rather than
    read/derived off `user` directly -- UserStats.score needs the chat's current
    words_per_point, and the streak needs "today" (see _current_streak), neither of which
    this function has any way to get itself (it's sync, with no client/chat_ref/tz)."""
    avg = user.messages / user.active_days if user.active_days else 0.0
    last_seen = "нет данных"
    if user.last_message_at:
        last_seen = datetime.fromisoformat(user.last_message_at).strftime("%Y-%m-%d %H:%M")
    score_str = f"{score:,}".replace(",", ".")
    activity_line = f"Активность: {_ru_days(user.active_days)}"
    if streak > 0:
        activity_line += f" 🔥{_ru_days(streak)}"
    text = (
        "📊 Статистика пользователя:\n\n"
        f"Имя: {user.display_name}\n"
        f"🏆 Очки: 🪙{score_str}\n"
        f"📈 Место в рейтинге: {rank} из {total}\n"
        f"Сообщений: {user.messages}\n"
        f"Среднее сообщений в день: {avg:.1f}\n"
        f"{activity_line}\n"
        f"Покрашено фигурок: {user.figurines_painted} (+{POINTS_PER_FIGURINE} за фигурку {FIGURINE_HASHTAG})\n"
        f"Любимое время: {_favorite_hour_label(user.hours)}\n"
        f"Последняя активность: {last_seen}"
    )
    if figurine_links:
        works = "\n".join(f"{i}. {link}" for i, link in enumerate(figurine_links, start=1))
        text += f"\n\nПоследние 3 работы:\n{works}"
    return text


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
) -> tuple[UserStats | None, int | None, int, int | None, int | None]:
    """Resolves who a /stat command is asking about: an explicit argument (@username or
    a name fragment) if given, otherwise the requester's own tracked stats -- tried first
    by @username (exact), falling back to their display name (substring). Fetches the
    all-time-plus-today-live aggregate exactly once regardless of how many of those three
    lookups it takes, rather than once per attempt.

    Returns (user, rank, total, score, streak): `rank` is the person's 1-based position by
    score among everyone ever tracked for this chat (ties broken by dict iteration order,
    which is stable but arbitrary -- fine for a gamified leaderboard, not meant to be
    exact), and `total` is how many people that's out of. `score` and `streak` are
    returned alongside `user` (rather than left for the caller to derive from `user`
    itself) since both need context this function already has and format_stat doesn't --
    words_per_point for score (see UserStats.score) and today's date for streak (see
    _current_streak). `rank`/`score`/`streak` are None (with user) if no match was found;
    `total` is still meaningful in that case (could be used for a "N people tracked"
    message even without a match, though callers currently don't)."""
    all_time = await aggregate_all_time_live(client, chat_ref, entry, tz, log=log)
    total = len(all_time)
    if arg:
        user = _find_user(all_time, arg)
    else:
        user = _find_user(all_time, requester_username) if requester_username else None
        if user is None:
            user = _find_user(all_time, requester_display_name)
    if user is None:
        return None, None, total, None, None
    wpp = await words_per_point(client, chat_ref, entry, tz, log=log)
    ranked = sorted(all_time.values(), key=lambda s: s.score(wpp), reverse=True)
    rank = next(i for i, s in enumerate(ranked, start=1) if s.user_id == user.user_id)
    today = datetime.now(tz).date()
    streak = _current_streak(user.active_day_dates, today)
    return user, rank, total, user.score(wpp), streak
