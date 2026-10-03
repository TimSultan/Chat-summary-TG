"""Per-user activity stats and a gamified leaderboard for a chat -- message/character/
media/reply counts, active days, and an hourly activity histogram, computed once per
calendar day from the SAME per-day transcript cache telegram_fetch.py already maintains
(see finalize_and_record, called by listener.py's midnight rollover job). Powers "/top
day|week|month|year|all" (an XP leaderboard) and "/stat [username]" (one
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
the LISTENER_ALLOWED_CHATS entry string. Chosen because it sidesteps Telegram's two
different chat-id numbering schemes (Telethon's own vs. the Bot API's) entirely, since
both listener.py and bot_listener.py can always recover the *entry* for an incoming
message via their own matched_allowed_chat/_match_allowed_chat helpers, regardless of
which account is handling the request. A day file's existence IS the "already recorded"
check record_day/finalize_and_record need to stay idempotent -- rerunning the rollover
job for a day it already processed (e.g. a restart landing near midnight) is then a cheap
no-op, not a double-count.

XP scoring:
    Message XP, PER DAY, is one of two things depending on whether that day predates
    word-tracking (see _has_word_data / UserStats.legacy_message_points vs. .words):
        +1 per message (flat), for any day recorded before this feature shipped -- kept
            forever exactly as it always scored, never reinterpreted.
        +(word count / words_per_point), for any day from after -- NOT a flat rate. A
            flat +1/message rewarded spamming lots of short messages just as much as
            writing one that says something; word count divided by the chat's own
            average words/message (see words_per_point) keeps a "typical" message worth
            about 1 XP while a one-word "ok" is worth a fraction and a long one worth
            more.
    +1 per message containing a photo or video
    +1 per message that's a reply (see the is_reply note on UserStats.replies below)
    +5 per distinct calendar day the person posted at least once
    +200 per message tagged #япокрасил that has an actual photo OR video attached (a
        "figurine painted" post -- see FIGURINE_HASHTAG; a hashtag with no media doesn't
        qualify). Also surfaced as its own raw count, "Покрашено фигурок", in /stat.
XP is never stored -- always recomputed on demand from the raw per-day counters for
whatever window (day/week/month/year, or -- for a bare /stat lookup -- every recorded
day) is asked about, so changing the point values later doesn't require re-processing any
history -- and per the split above, a day's message-scoring rule never changes after the
fact either, regardless of what happens to word-tracking later. words_per_point is the
one number here that isn't a fixed constant in code -- it's calibrated per chat, from
that chat's own real activity (see MIN_CALIBRATION_MESSAGES) -- but it's still fixed in
effect: calibrated exactly once, then cached to disk and reused forever, never
automatically recomputed (see words_per_point's own docstring). So a message's XP
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
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path

import telegram_fetch
from app_time import cache_namespace, now as app_now, resolve_timezone

DATA_DIR = Path(os.getenv("DATA_DIR", "."))
STATS_DIR = DATA_DIR / "cache" / "stats"


def _stats_dir() -> Path:
    return STATS_DIR / cache_namespace(resolve_timezone())

XP_PER_MEDIA_MESSAGE = 1
XP_PER_REPLY = 1
XP_PER_ACTIVE_DAY = 5
XP_PER_FIGURINE = 200
# Halved from 10. Coins are DERIVED from XP (see economy.balance), never stored, so this
# is not only a better rate going forward -- it retroactively doubles what everybody has
# already earned, in one step, with no migration. That is the intended effect: the arena
# is not most members' income, chat activity is, and at 10 XP per coin a normal chatter
# earned about 5 coins a day against stat upgrades priced in the thousands.
XP_PER_COIN = 5

# Anti-farming limits, applied when a day is COMPUTED (compute_day_stats) rather than
# when it is scored, so they can never reach back and reprice a day that was already
# recorded -- the same immutability rule legacy_message_points exists to protect. Word
# scoring already makes a one-word "ок" nearly worthless, but nothing previously stopped
# someone from farming XP with a burst of long messages, and coins are about to be worth
# something real (see economy.py).
#
# Messages inside the cooldown still count as messages -- they happened, and /stat's
# message count, active days and hour histogram must keep telling the truth. Only the
# three SCORED counters skip them.
#
# SHIPPED DISABLED (0), deliberately. The mechanism below is complete and turning it on
# is a one-line change, but measuring the standard 30-60s advice against this chat's own
# 62k cached messages showed it does not fit here at all:
#
#     cooldown | messages suppressed | words | media | replies
#         15s  |        24.3%        | 12.7% | 41.8% |   7.9%
#         30s  |        36.0%        | 21.7% | 46.5% |  17.7%
#         45s  |        43.6%        | 29.0% | 50.3% |  25.9%
#
# Half of every photo in the chat arrives within 45s of that person's previous message,
# because painters post several angles of one miniature back to back -- precisely the
# behavior the chat exists to encourage. A cooldown here is not an anti-farming measure,
# it is an across-the-board XP cut aimed at the most engaged members. The daily caps
# below do the actual job: they bite 2-4% of person-days, all of them genuine outliers
# (the worst real day seen was 11,025 words from one person).
XP_MESSAGE_COOLDOWN_SECONDS = 0
# Daily ceilings on the scored counters -- the limits that actually bound farming. Sized
# off the chat's own distribution: over 1,579 measured person-days these caught 30 days
# over the word cap, 67 over the media cap and 48 over the reply cap, leaving ordinary
# days completely untouched.
XP_DAILY_WORD_CAP = 1_500
XP_DAILY_MEDIA_CAP = 25
XP_DAILY_REPLY_CAP = 100

# The painter rank: one name per figurine milestone, gated on figurines alone (see
# painter_rank). Ordered lowest first. These seven names used to be the chat's only
# ladder, gated on XP *and* figurines at once, which froze both a constant talker who
# never painted and a constant painter who rarely posted at the bottom forever. Talking
# now has its own track (chat_level), so the old XP half of each step is gone.
PAINTER_RANKS = (
    (0, "🩶", "Серый новичок"),
    (3, "⚪", "Ученик грунта"),
    (5, "🖌️", "Подмастерье кисти"),
    (10, "💨", "Укротитель аэрографа"),
    (20, "💧", "Повелитель проливок"),
    (35, "🏛️", "Мастер витрины"),
    (50, "👑", "Легенда покраса"),
)

# --- chat level -------------------------------------------------------------------
#
# The activity track: XP only, no figurine gate, so it always moves for anybody who
# talks. Many small steps rather than a few enormous ones.
#
# Scored on ALL-TIME XP and never reset. It was briefly scored on a calendar-quarter
# "season" instead, and the first quarter boundary (1 October 2026) dropped every member
# back to level 1-3 overnight with nothing in the chat to say why -- members saw their
# level as broken, and they were right: a level that silently falls is not progress.
#
# The curve is the one the seasonal ladder was calibrated on (the p95 member earns ~103
# XP/day and reaches level 40 in about three months), so the first forty levels cost
# exactly what they did then and nobody's level went down when the season was dropped.
# There is deliberately no top: with all-time XP a cap would freeze the most active
# members within months, which is the "nothing moves any more" problem the season was
# trying to solve. Each level costs a little more than the one before it, so the climb
# slows down on its own.
CHAT_LEVEL_CURVE_BASE = 25
CHAT_LEVEL_CURVE_EXPONENT = 1.6

# One name per five levels, so the number moves often while the title still means
# something. Index 0 covers levels 1-5, index 1 covers 6-10, and so on; the last name
# carries on above level 40, where the number alone keeps counting.
CHAT_LEVEL_TIERS = (
    ("🌱", "Новенький"),
    ("💬", "Болтун"),
    ("🗣️", "Голос чата"),
    ("📣", "Заводила"),
    ("🎙️", "Старожил"),
    ("🔥", "Душа чата"),
    ("⚡", "Легенда общения"),
    ("🌟", "Хранитель чата"),
)

# --- automatic badges ---------------------------------------------------------------
#
# Exactly two, each with levels that never run out. There used to be a dozen (painting
# steps, message steps, streaks, night shifts, gambling, a gallery, two hashtags), and
# after a few months nearly every regular held most of them, so the block took half of
# /stat while telling nobody anything. The painting steps also repeated the painter rank
# shown two lines above them. Day files still record every counter the old badges read,
# so bringing one back is a display change, not a re-scan.
ACTIVE_DAYS_PER_BADGE_LEVEL = 30
MESSAGES_PER_BADGE_LEVEL = 1_000
ACTIVE_DAYS_BADGE = ("active_days", "📅", "Завсегдатай")
MESSAGES_BADGE = ("messages", "💬", "Собеседник")

# --- reputation -------------------------------------------------------------------
#
# Mostly the anti-grind track: the three peer-granted components below cannot be moved by
# posting at all. Every point of them comes from somebody else choosing to give it, which
# is the one thing a farming script cannot do.
REPUTATION_PER_CONTEST_WIN = 10
REPUTATION_PER_BADGE_RECEIVED = 5
# Coins RECEIVED from other members (see economy.transfer), divided down so a single
# wealthy friend cannot mint somebody a reputation.
REPUTATION_PER_COINS_RECEIVED = 20
# The one self-earned component: a point per automatic badge LEVEL held (see
# medal_levels). It IS grindable, unlike the three above -- deliberately, so that a
# member with no peers handing them anything still has a reputation that moves. The
# badges themselves have no top, so the points are capped one short of two contest wins:
# grinding can never outrank being valued by the chat.
REPUTATION_PER_MEDAL_LEVEL = 1
REPUTATION_MEDAL_LEVEL_CAP = 2 * REPUTATION_PER_CONTEST_WIN - 1

REPUTATION_TIERS = (
    (100, "🏅", "Легенда сообщества"),
    (50, "🤝", "Опора чата"),
    (25, "👏", "Уважаемый"),
    (10, "🌿", "Замеченный"),
    (0, "·", "Пока тихо"),
)

# Custom badges are deliberately bounded because Telegram inline keyboards allow at
# most 100 buttons. Fifty leaves comfortable room for navigation/menu buttons later.
MAX_CUSTOM_BADGES = 50
CUSTOM_BADGE_NAME_MAX_CHARS = 40
CUSTOM_BADGE_STORE_VERSION = 1
# 2 added the showcase-post refs (see BEST_WORK_HASHTAGS/WORKPLACE_HASHTAGS). Bumping
# this is what makes _backfill_day_badge_stats revisit already-recorded days and fill the
# new fields in from the transcript cache -- without a bump, every existing day file
# would already look current and the two tags would only ever be seen going forward.
BADGE_STATS_SCHEMA_VERSION = 2
WEEKLY_CONTEST_STORE_VERSION = 1
# Deep-link payload behind /stat's cabinet link: t.me/<bot>?start=cabinet makes
# Telegram show a START button that sends "/start cabinet" to the bot.
CABINET_START_PAYLOAD = "cabinet"
WORK_NAME_STORE_VERSION = 1
BADGE_MANAGER_STORE_VERSION = 1
XP_GRANT_STORE_VERSION = 1
# Long enough for "Космодесантник Ультрамаринов", short enough that thirty of them still
# fit in one Telegram message alongside their links.
WORK_NAME_MAX_CHARS = 32
# 2 replaced the single figurine-gated ladder with two independently announced tracks
# (chat level and painter rank). A stored version below this is discarded rather than
# read: its "minimum_xp" watermark describes a ladder that no longer exists, and
# comparing new track positions against it would announce a promotion for essentially
# every member at once. Discarding re-baselines everybody silently on the next
# observation, which is exactly what happened when levels first shipped. Dropping the
# season needed no bump: every caller always observed ALL-TIME XP, so the stored chat
# level already meant what it means now.
LEVEL_STATE_VERSION = 2
DELETED_FIGURINE_STORE_VERSION = 1
# Two calendar weeks ensure the immediately preceding weekly contest is covered no
# matter which weekday the upgraded process first starts. Only already-recorded days
# outside the normal STATS_CATCHUP_DAYS window are considered (see listener.py).
#
# Widened from 14 to 30 for the showcase tags: #моялучшая was a one-day themed event
# rather than a rolling habit, so a window that merely "covers the last contest" can miss
# the single day that carries almost every post -- 14 days would already have been cutting
# it close, and any delay between writing this and deploying it would silently drop the
# event entirely. 30 days covers both tags' full history with margin. The ongoing cost is
# unchanged (a day already on the current schema is one local JSON read and returns
# early); the one-off cost is a transcript re-fetch for any day 15-30 back that is
# recorded but no longer cached, and final days are cached indefinitely.
HASHTAG_BADGE_BACKFILL_DAYS = 30

NOT_GAY_HASHTAG = "#янепидор"
WEEKLY_CONTEST_HASHTAG = "#итогинедели"

# Showcase hashtags. Unlike NOT_GAY_HASHTAG/WEEKLY_CONTEST_HASHTAG above -- which only
# ever needed a COUNT, so that's all their day files store -- /stat links straight to
# these posts, and a bare counter can't produce a link. They're therefore tracked the
# same way FIGURINE_HASHTAG posts are: as [ts, message_id] refs (see _merge_post_refs).
BEST_WORK_HASHTAGS = ("#моялучшая",)
# Both spellings are in real use in the chat. Telegram treats "_" as part of a hashtag,
# so "#рабочееместо" and "#рабочее_место" are two genuinely different tags to Telegram
# (and to _has_hashtag's \w-boundary match) -- neither matches the other, so both have to
# be listed rather than normalized into one.
WORKPLACE_HASHTAGS = ("#рабочееместо", "#рабочее_место")

# The flat rate every message scored under before word-based points existed. ONLY applied
# (via UserStats.legacy_message_points) to days recorded before word-tracking existed --
# those days keep exactly the score they always had, forever, rather than being
# reinterpreted by a formula that didn't exist when they happened. See _has_word_data.
LEGACY_XP_PER_MESSAGE = 1

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


@dataclass(frozen=True)
class Badge:
    badge_id: str
    emoji: str
    name: str
    description: str = ""
    custom: bool = False

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.name}"


@dataclass(frozen=True)
class PainterRank:
    minimum_figurines: int
    emoji: str
    name: str

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.name}"


def coins_for_xp(xp: int) -> int:
    """One earned coin for every complete XP_PER_COIN XP. Coins are derived rather than
    stored (see economy.balance), so this can change without migrating old stats."""
    return max(0, xp) // XP_PER_COIN


@dataclass(frozen=True)
class ChatLevel:
    number: int
    emoji: str
    tier_name: str
    current_threshold: int
    next_threshold: int

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.tier_name} {self.number}"


def chat_level_threshold(level_number: int) -> int:
    """XP needed to reach `level_number`. Level 1 starts at 0 -- everybody is level 1."""
    if level_number <= 1:
        return 0
    return int(CHAT_LEVEL_CURVE_BASE * (level_number ** CHAT_LEVEL_CURVE_EXPONENT))


def chat_level(xp: int) -> ChatLevel:
    """The activity level for all-time `xp` alone -- no figurine requirement, by design.

    Solved from the curve rather than walked up level by level, because the ladder has no
    top: a hand-granted XP total in the millions would otherwise mean thousands of
    iterations per member on every /top-sized read. The nudges afterwards absorb the
    rounding int() applies inside chat_level_threshold.
    """
    xp = max(0, int(xp))
    number = max(1, int((xp / CHAT_LEVEL_CURVE_BASE) ** (1 / CHAT_LEVEL_CURVE_EXPONENT)))
    while number > 1 and chat_level_threshold(number) > xp:
        number -= 1
    while chat_level_threshold(number + 1) <= xp:
        number += 1
    emoji, tier_name = CHAT_LEVEL_TIERS[_chat_tier_index(number)]
    return ChatLevel(
        number, emoji, tier_name, chat_level_threshold(number), chat_level_threshold(number + 1)
    )


def chat_level_progress(xp: int) -> int:
    """Percentage into the current chat level, 0-100.

    /stat renders this as a bar WITHOUT printing the target number, keeping the existing
    "don't reveal what's missing for the next level" rule while still giving people the
    visible near-goal progress that makes a level worth chasing at all."""
    level = chat_level(xp)
    span = level.next_threshold - level.current_threshold
    if span <= 0:
        return 100
    return max(0, min(100, int((xp - level.current_threshold) * 100 / span)))


def progress_bar(percent: int, width: int = 10) -> str:
    """Floors rather than rounds, so only a genuinely complete level shows a full bar --
    rounding let 98% render as ten filled blocks, which reads as "why haven't I levelled
    up yet?" precisely when somebody is closest to caring."""
    filled = max(0, min(width, int(percent * width / 100)))
    return "▓" * filled + "░" * (width - filled)


def painter_rank(figurines_painted: int) -> tuple[PainterRank, PainterRank | None]:
    """(current rank, next rank or None at the top) for this many painted figurines."""
    ranks = [PainterRank(*definition) for definition in PAINTER_RANKS]
    current_index = 0
    for index, rank in enumerate(ranks):
        if figurines_painted < rank.minimum_figurines:
            break
        current_index = index
    current = ranks[current_index]
    next_rank = ranks[current_index + 1] if current_index + 1 < len(ranks) else None
    return current, next_rank


def active_days_badge_level(user: "UserStats") -> int:
    """One level per ACTIVE_DAYS_PER_BADGE_LEVEL days with at least one message."""
    return max(0, int(user.active_days)) // ACTIVE_DAYS_PER_BADGE_LEVEL


def messages_badge_level(user: "UserStats") -> int:
    """One level per MESSAGES_PER_BADGE_LEVEL messages, all time."""
    return max(0, int(user.messages)) // MESSAGES_PER_BADGE_LEVEL


def _levelled_badge(definition: tuple, level: int, description: str) -> Badge | None:
    if level < 1:
        return None
    badge_id, emoji, name = definition
    return Badge(badge_id, emoji, f"{name} {level}", description)


def earned_badges(user: "UserStats") -> list[Badge]:
    """The two automatic badges, each showing its level and only once it has one.

    Numbered like the levels they are: "Завсегдатай 3" is ninety active days, and the
    number keeps climbing for as long as the member does.
    """
    days_level = active_days_badge_level(user)
    messages_level = messages_badge_level(user)
    return [
        badge
        for badge in (
            _levelled_badge(
                ACTIVE_DAYS_BADGE, days_level,
                f"{days_level * ACTIVE_DAYS_PER_BADGE_LEVEL}+ активных дней, "
                f"новый уровень каждые {ACTIVE_DAYS_PER_BADGE_LEVEL}",
            ),
            _levelled_badge(
                MESSAGES_BADGE, messages_level,
                f"{_thousands(messages_level * MESSAGES_PER_BADGE_LEVEL)}+ сообщений, "
                f"новый уровень каждые {_thousands(MESSAGES_PER_BADGE_LEVEL)}",
            ),
        )
        if badge is not None
    ]


def medal_levels(user: "UserStats") -> int:
    """The self-earned half of reputation: one point per automatic badge level, capped.

    Counts exactly what earned_badges puts in /stat's "🏅 Значки" line and nothing else,
    so nobody scores for a medal they cannot see. Hand-made badges and weekly wins are
    left to their own, larger rates -- a point on top would pay twice for one medal.
    Capped at REPUTATION_MEDAL_LEVEL_CAP because the badges themselves never run out.
    """
    levels = active_days_badge_level(user) + messages_badge_level(user)
    return min(levels, REPUTATION_MEDAL_LEVEL_CAP)


def reputation_score(
    contest_wins: int, badges_received: int, coins_received: int, medals: int = 0
) -> int:
    """Standing: three peer-granted components nobody can move by posting, plus the
    earned-badge levels from medal_levels. `medals` defaults to 0 so a caller with no
    UserStats to hand (and every pre-existing test) still scores the peer-granted half."""
    return (
        max(0, contest_wins) * REPUTATION_PER_CONTEST_WIN
        + max(0, badges_received) * REPUTATION_PER_BADGE_RECEIVED
        + max(0, coins_received) // REPUTATION_PER_COINS_RECEIVED
        + max(0, medals) * REPUTATION_PER_MEDAL_LEVEL
    )


def reputation_tier(score: int) -> tuple[str, str]:
    """(emoji, name) for a reputation score. REPUTATION_TIERS is ordered highest-first."""
    for threshold, emoji, name in REPUTATION_TIERS:
        if score >= threshold:
            return emoji, name
    return REPUTATION_TIERS[-1][1], REPUTATION_TIERS[-1][2]


def badge_collection_progress(
    user: "UserStats",
    xp: int,
    custom_badges: list[Badge] | None = None,
    chat_custom_badge_total: int = 0,
) -> tuple[int, int]:
    """(unlocked, total) across everything collectable that has an end: chat-level
    names, painter ranks, the two automatic badges and the chat's hand-made badges.

    The automatic badges count once each, as soon as they have a first level -- their
    levels never run out, so counting levels would leave the total undefined. The chat
    level counts by NAME (one per five levels) for the same reason. `chat_custom_badge_
    total` is how many custom badges this chat has DEFINED, so admin-made badges count
    towards the denominator instead of being an unbounded unknown. `xp` is the all-time
    XP the chat level is scored on.
    """
    unlocked = 0
    total = 0

    total += len(CHAT_LEVEL_TIERS)
    unlocked += _chat_tier_index(chat_level(xp).number) + 1

    total += len(PAINTER_RANKS)
    unlocked += sum(1 for minimum, *_ in PAINTER_RANKS if user.figurines_painted >= minimum)

    total += 2
    unlocked += len(earned_badges(user))

    total += max(chat_custom_badge_total, len(custom_badges or []))
    unlocked += len(custom_badges or [])
    return unlocked, total


def _thousands(value: int) -> str:
    """Dot-grouped thousands, the way /stat already prints XP and messages."""
    return f"{int(value):,}".replace(",", ".")


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

# "Топ покрастинаторов": sent automatically every PROCRASTINATOR_DIGEST_INTERVAL_DAYS
# days at PROCRASTINATOR_DIGEST_HOUR local (app-timezone) time -- see run_stats_rollover's
# digest loop and should_send_procrastinator_digest -- calling out exactly
# PROCRASTINATOR_LIST_SIZE people (fewer only if there simply aren't that many
# candidates), walking down the last-30-days scorers (the same window /top month uses,
# NOT all-time -- see format_procrastinators) from the top, SKIPPING (not counting
# towards the list) anyone who's posted a #япокрасил+photo/video within the last
# PROCRASTINATOR_INACTIVE_DAYS days -- so the list is always full-size instead of
# shrinking on a day when most of the top scorers happen to be caught up.
# Ten, down from 21. This is a public call-out list, and past about ten names it stops
# reading as a nudge and starts reading as a wall. Shared by BOTH the on-demand
# "/top pokras" and the automatic digest -- they are the same list, and letting them
# differ would mean the same command answers differently depending on who asked for it.
PROCRASTINATOR_LIST_SIZE = 10
PROCRASTINATOR_INACTIVE_DAYS = 14
PROCRASTINATOR_DIGEST_HOUR = 19
PROCRASTINATOR_DIGEST_INTERVAL_DAYS = 2

# The ЕПХ-tree morning post. Pinned to Moscow rather than the app timezone: the chat
# asked for a Moscow morning, and the deployment's own timezone is a hosting detail that
# could move without anybody deciding to move the greeting.
TREE_DIGEST_HOUR = 10
TREE_DIGEST_TIMEZONE = "Europe/Moscow"


def tree_digest_tz():
    """Europe/Moscow, falling back to the app timezone if the zoneinfo database is
    missing on the host -- a wrong hour is better than no morning post at all."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(TREE_DIGEST_TIMEZONE)
    except Exception:
        return resolve_timezone()

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


def _custom_badges_path(entry: str) -> Path:
    return _stats_dir() / f"{_cache_key(entry)}_custom_badges.json"


def _weekly_contest_path(entry: str) -> Path:
    return _stats_dir() / f"{_cache_key(entry)}_weekly_contest.json"


def _level_state_path(entry: str) -> Path:
    return _stats_dir() / f"{_cache_key(entry)}_level_state.json"


def _xp_grants_path(entry: str) -> Path:
    return _stats_dir() / f"{_cache_key(entry)}_xp_grants.json"


def _load_xp_grants(entry: str) -> dict:
    try:
        data = json.loads(_xp_grants_path(entry).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": XP_GRANT_STORE_VERSION, "users": {}}
    if not isinstance(data, dict) or data.get("version") != XP_GRANT_STORE_VERSION:
        return {"version": XP_GRANT_STORE_VERSION, "users": {}}
    users = data.get("users")
    return {"version": XP_GRANT_STORE_VERSION, "users": users if isinstance(users, dict) else {}}


def grant_xp_once(
    entry: str, user_id, amount: int, key: str, *,
    username: str | None = None, display_name: str | None = None,
) -> bool:
    """Persist one idempotent XP adjustment that participates in normal coin derivation.

    For CHAT reasons only -- correcting a total that recorded activity got wrong. Never
    use it to pay somebody: XP is what /top and /stat rank and level people by, so money
    handed out as XP rewrites the chat's leaderboard and cannot be told apart from XP that
    was earned by writing. Coins go through economy.grant_once (see admin_xp.py).
    """
    amount = int(amount)
    key = str(key or "").strip()
    if amount <= 0 or not key:
        return False
    data = _load_xp_grants(entry)
    users = data["users"]
    user = users.get(str(user_id))
    if not isinstance(user, dict):
        user = {"grants": {}}
        users[str(user_id)] = user
    grants = user.setdefault("grants", {})
    if not isinstance(grants, dict):
        grants = {}
        user["grants"] = grants
    if key in grants:
        return False
    grants[key] = {"amount": amount, "granted_at": app_now().date().isoformat()}
    if username:
        user["username"] = str(username).lstrip("@")
    if display_name:
        user["display_name"] = str(display_name)
    _write_json_atomic(_xp_grants_path(entry), data)
    return True


def _grant_amount(grant) -> int:
    """One stored grant's XP, SIGNED -- the running admin adjustment can be negative, and
    _apply_xp_grants counts it that way, so every reader has to as well."""
    if not isinstance(grant, dict):
        return 0
    try:
        return int(grant.get("amount", 0) or 0)
    except (TypeError, ValueError):
        return 0


def xp_grants_for(entry: str, user_id) -> dict:
    """This member's named XP adjustments: {key: {"amount", "granted_at"}}.

    Read-only, and the only way to see what an XP grant actually did after the fact --
    bonus_xp is summed into the totals and is otherwise indistinguishable from XP somebody
    earned by writing in the chat. Amounts are signed: a negative admin adjustment used
    to read as 0 here while still being subtracted from the real total.
    """
    row = _load_xp_grants(entry)["users"].get(str(user_id))
    grants = (row or {}).get("grants")
    if not isinstance(grants, dict):
        return {}
    return {
        str(key): {
            "amount": _grant_amount(grant),
            "granted_at": str(grant.get("granted_at") or ""),
        }
        for key, grant in grants.items()
        if isinstance(grant, dict)
    }


ADMIN_XP_ADJUST_KEY = "admin_adjust"


def adjust_bonus_xp(entry: str, user_id, delta: int, *, by: str = "") -> int:
    """Move somebody's chat XP up or down by hand. Returns the delta actually applied.

    One running adjustment per person under a fixed key, deliberately unlike grant_xp_once
    (which is a pile of separate idempotent awards). An administrator nudging a number is
    not awarding anything; they are correcting a total, and correcting it twice should
    read as one number rather than as a growing list of half-corrections.

    Clamped so TOTAL XP can never go below zero: earned XP is derived from real recorded
    activity and cannot be reduced, so the floor for the adjustment is -earned. Somebody
    who asks to remove more than exists gets everything removed, and the return value says
    so, rather than the leaderboard growing a negative entry.
    """
    delta = int(delta)
    if not delta:
        return 0
    rows = aggregate_all_time(entry)
    user = rows.get(str(user_id))
    baseline = _load_words_per_point(entry) or DEFAULT_WORDS_PER_POINT
    total = user.xp(baseline) if user is not None else 0
    if delta < 0:
        delta = -min(-delta, max(0, total))
        if not delta:
            return 0

    data = _load_xp_grants(entry)
    row = data["users"].setdefault(str(user_id), {})
    grants = row.setdefault("grants", {})
    if not isinstance(grants, dict):
        grants = row["grants"] = {}
    current = grants.get(ADMIN_XP_ADJUST_KEY)
    running = int((current or {}).get("amount", 0) or 0) if isinstance(current, dict) else 0
    grants[ADMIN_XP_ADJUST_KEY] = {
        "amount": running + delta,
        "granted_at": app_now().date().isoformat(),
        "by": str(by or ""),
    }
    _write_json_atomic(_xp_grants_path(entry), data)
    return delta


def xp_breakdown(entry: str, user_id) -> dict:
    """Total XP split into what was earned and what was handed out, with the earned half
    itemised. One function so the CLI and the admin page cannot disagree about it.

    The question it exists to answer: somebody at the top of /top is either very active or
    was given a number, and those look identical from outside.
    """
    rows = aggregate_all_time(entry)
    baseline = _load_words_per_point(entry) or DEFAULT_WORDS_PER_POINT
    user = rows.get(str(user_id))
    if user is None:
        return {"found": False, "total": 0, "granted": 0, "earned": 0, "parts": [], "grants": {}}
    total = user.xp(baseline)
    granted = int(user.bonus_xp)
    parts = [
        ("сообщения", int(user.legacy_message_points)),
        ("слова", round(user.words / baseline)),
        ("медиа", user.media * XP_PER_MEDIA_MESSAGE),
        ("ответы", user.replies * XP_PER_REPLY),
        ("активные дни", user.active_days * XP_PER_ACTIVE_DAY),
        ("покрасы", user.figurines_painted * XP_PER_FIGURINE),
    ]
    return {
        "found": True,
        "display_name": user.display_name,
        "username": user.username,
        "total": total,
        "granted": granted,
        "earned": total - granted,
        "coins_from_granted": granted // XP_PER_COIN,
        "parts": [{"label": label, "xp": value} for label, value in parts if value],
        "grants": xp_grants_for(entry, user_id),
    }


def revoke_xp_grants(entry: str, user_id, key: str | None = None) -> int:
    """Take back one XP grant, or all of them. Returns the XP actually removed, signed.

    The counterpart grant_xp_once never had, which is why an XP grant used to be a
    one-way door. It matters because XP is the WRONG lever for handing somebody money:
    coins are derived from it (see economy.balance), so a grant meant to top up a wallet
    also lands in /top and /stat and quietly rewrites the chat's leaderboard.

    Removing the XP therefore removes the coins it was standing in for. Whoever calls this
    is expected to put those coins back through economy.grant_once, which is the lever
    that should have been used in the first place -- see admin_xp.py, which does both.

    Signed because the running admin adjustment can be negative: clearing a +10M grant
    together with the -10M adjustment that already undid it removes 0 XP, and paying
    compensation for 10M there would hand out the money twice.
    """
    data = _load_xp_grants(entry)
    row = data["users"].get(str(user_id))
    grants = (row or {}).get("grants")
    if not isinstance(grants, dict):
        return 0
    if key is None:
        removed = sum(_grant_amount(grant) for grant in grants.values())
        row["grants"] = {}
    else:
        if str(key) not in grants:
            return 0
        removed = _grant_amount(grants.pop(str(key)))
    _write_json_atomic(_xp_grants_path(entry), data)
    return removed


def xp_grant_in_effect(entry: str, user_id, key: str) -> tuple[int, int]:
    """(granted, still in effect) for one positive XP grant; (0, 0) if there is none.

    "In effect" nets the grant against a NEGATIVE running admin adjustment, because that
    is how an administrator undoes a grant from the resource panel: a +10M grant beside a
    -10M adjustment adds nothing to the total any more, and the coins it once implied
    are already gone. retire_xp_grant removes exactly this much.
    """
    grants = xp_grants_for(entry, user_id)
    granted = grants.get(str(key), {}).get("amount", 0)
    if granted <= 0:
        return 0, 0
    adjustment = grants.get(ADMIN_XP_ADJUST_KEY, {}).get("amount", 0)
    return granted, granted - min(granted, max(0, -adjustment))


def retire_xp_grant(entry: str, user_id, key: str) -> int:
    """Remove one positive XP grant, cancelling it against any negative admin adjustment
    first, in ONE write. Returns the XP that actually left the member's total -- the
    "still in effect" half of xp_grant_in_effect, which is what any coin compensation
    must be based on. The total can never go below what the member earned.
    """
    data = _load_xp_grants(entry)
    row = data["users"].get(str(user_id))
    grants = (row or {}).get("grants")
    if not isinstance(grants, dict) or str(key) not in grants:
        return 0
    granted = _grant_amount(grants[str(key)])
    if granted <= 0:
        return 0
    adjustment = grants.get(ADMIN_XP_ADJUST_KEY)
    cancelled = min(granted, max(0, -_grant_amount(adjustment)))
    del grants[str(key)]
    if cancelled:
        remaining = _grant_amount(adjustment) + cancelled
        if remaining:
            adjustment["amount"] = remaining
        else:
            del grants[ADMIN_XP_ADJUST_KEY]
    _write_json_atomic(_xp_grants_path(entry), data)
    return granted - cancelled


def _deleted_figurines_path(entry: str) -> Path:
    return _stats_dir() / f"{_cache_key(entry)}_deleted_figurines.json"


def _work_names_path(entry: str) -> Path:
    return _stats_dir() / f"{_cache_key(entry)}_work_names.json"


def _badge_managers_path(entry: str) -> Path:
    return _stats_dir() / f"{_cache_key(entry)}_badge_managers.json"


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _load_deleted_figurines(entry: str) -> dict:
    path = _deleted_figurines_path(entry)
    if not path.exists():
        return {"version": DELETED_FIGURINE_STORE_VERSION, "posts": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": DELETED_FIGURINE_STORE_VERSION, "posts": {}}
    if not isinstance(data, dict):
        return {"version": DELETED_FIGURINE_STORE_VERSION, "posts": {}}
    data.setdefault("posts", {})
    return data


def delete_figurine_submission(
    entry: str,
    user_id: int | str,
    message_id: int,
    deleted_by_id: int | str,
    deleted_by_name: str,
) -> bool:
    """Persistently exclude one deleted #япокрасил post from stats.

    This is a tombstone instead of an edit to a single day file because today's
    transcript cache may still contain a Telegram message for a while after it was
    deleted. Applying the tombstone at aggregation time removes the work link, one
    figurine, its XP, and any badge/level progress derived from that figurine even if a
    stale transcript or immutable historical snapshot still contains the old post.
    Returns False when the same post was already excluded.
    """
    data = _load_deleted_figurines(entry)
    key = str(message_id)
    if key in data["posts"]:
        return False
    data["posts"][key] = {
        "user_id": str(user_id),
        "deleted_at": app_now().isoformat(),
        "deleted_by_id": str(deleted_by_id),
        "deleted_by_name": deleted_by_name,
    }
    data["version"] = DELETED_FIGURINE_STORE_VERSION
    _write_json_atomic(_deleted_figurines_path(entry), data)
    return True


def _load_work_names(entry: str) -> dict:
    path = _work_names_path(entry)
    if not path.exists():
        return {"version": WORK_NAME_STORE_VERSION, "users": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": WORK_NAME_STORE_VERSION, "users": {}}
    if not isinstance(data, dict):
        return {"version": WORK_NAME_STORE_VERSION, "users": {}}
    data.setdefault("users", {})
    return data


def work_names_for_user(entry: str, user_id: int | str) -> dict:
    """{message_id (str): name} for one member's renamed works.

    Keyed by message_id rather than by the position shown in /stat, because those
    positions shift: deleting a work compacts the numbering (see delete_figurine_
    submission), and a rename must follow the work rather than the slot it happened to
    occupy. Corruption costs names, never the works themselves."""
    return (_load_work_names(entry).get("users") or {}).get(str(user_id)) or {}


def numbered_figurine_posts(user: "UserStats") -> list:
    """The works that get a number, newest first: every figurine post that has a message
    id to link to. A post recorded live without one cannot be linked, so it takes no
    number -- and every screen that numbers works (/stat, /работы, the cabinet,
    /deletepokras) must skip it the same way, or the same number names two works."""
    return [
        post for post in user.recent_figurine_posts
        if len(post) >= 2 and post[1] is not None
    ]


def work_name_list(entry: str, user: "UserStats") -> list:
    """Names aligned position-for-position with figurine_message_links, None where a work
    has not been named. One helper so /stat, the cabinet's stats screen and the group
    reply all label works identically instead of each doing the message_id lookup."""
    names = work_names_for_user(entry, user.user_id)
    return [names.get(str(post[1])) for post in numbered_figurine_posts(user)]


def set_work_name(entry: str, user_id: int | str, message_id: int | str, name: str) -> str:
    """Name (or, with an empty `name`, un-name) one work. Returns the stored name."""
    data = _load_work_names(entry)
    names = data["users"].setdefault(str(user_id), {})
    clean = " ".join((name or "").split())[:WORK_NAME_MAX_CHARS]
    if clean:
        names[str(message_id)] = clean
    else:
        names.pop(str(message_id), None)
    data["version"] = WORK_NAME_STORE_VERSION
    _write_json_atomic(_work_names_path(entry), data)
    return clean


def _load_badge_managers(entry: str) -> dict:
    path = _badge_managers_path(entry)
    if not path.exists():
        return {"version": BADGE_MANAGER_STORE_VERSION, "managers": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": BADGE_MANAGER_STORE_VERSION, "managers": {}}
    if not isinstance(data, dict):
        return {"version": BADGE_MANAGER_STORE_VERSION, "managers": {}}
    data.setdefault("managers", {})
    return data


def is_badge_manager(entry: str, user_id: int | str) -> bool:
    """Whether this member was delegated badge management for `entry`.

    Read on every cabinet render, so it must never raise: a corrupt store costs a
    delegate their extra button, it must not break the menu for everybody."""
    try:
        return str(user_id) in _load_badge_managers(entry).get("managers", {})
    except (OSError, ValueError):
        return False


def list_badge_managers(entry: str) -> list[dict]:
    managers = _load_badge_managers(entry).get("managers", {})
    return [dict(record, user_id=user_id) for user_id, record in sorted(managers.items())]


def grant_badge_manager(
    entry: str,
    user_id: int | str,
    username: str | None,
    display_name: str,
    granted_by_id: int | str,
    granted_by_name: str,
) -> bool:
    """Delegate badge management. False when they already had it (idempotent, like
    give_custom_badge), so the caller can say "already" rather than "done" twice."""
    data = _load_badge_managers(entry)
    key = str(user_id)
    if key in data["managers"]:
        return False
    data["managers"][key] = {
        "username": (username or "").lstrip("@") or None,
        "display_name": display_name,
        "granted_at": app_now().isoformat(),
        "granted_by_id": str(granted_by_id),
        "granted_by_name": granted_by_name,
    }
    data["version"] = BADGE_MANAGER_STORE_VERSION
    _write_json_atomic(_badge_managers_path(entry), data)
    return True


def revoke_badge_manager(entry: str, user_id: int | str) -> bool:
    """Take the delegation back. False when they did not have it."""
    data = _load_badge_managers(entry)
    if data["managers"].pop(str(user_id), None) is None:
        return False
    data["version"] = BADGE_MANAGER_STORE_VERSION
    _write_json_atomic(_badge_managers_path(entry), data)
    return True


def _empty_custom_badge_data() -> dict:
    return {"version": CUSTOM_BADGE_STORE_VERSION, "badges": {}, "assignments": {}}


def _load_custom_badge_data(entry: str) -> dict:
    path = _custom_badges_path(entry)
    if not path.exists():
        return _empty_custom_badge_data()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("custom badge file must contain a JSON object")
    data.setdefault("badges", {})
    data.setdefault("assignments", {})
    return data


def _save_custom_badge_data(entry: str, data: dict) -> None:
    """Atomic replace keeps /stat readers from observing a half-written JSON file."""
    path = _custom_badges_path(entry)
    data["version"] = CUSTOM_BADGE_STORE_VERSION
    _write_json_atomic(path, data)


# Emoji that are punctuation or digits by Unicode category and so are invisible to the
# "So" test below: the keycap mark that turns "1" into 1️⃣, the variation selector that
# turns a plain sign into its emoji form (↔️, ⌨️), and a handful of legacy symbols.
_EMOJI_CODEPOINTS = frozenset({
    0x20E3,  # combining enclosing keycap
    0xFE0F,  # variation selector-16 -- "render the previous character as an emoji"
    0x203C, 0x2049, 0x3030, 0x303D,
})


def _contains_emoji(text: str) -> bool:
    """Whether `text` holds at least one character Telegram would render as an emoji.

    Asks Unicode for the character's CATEGORY rather than matching a hand-written list of
    blocks. "So" (symbol, other) is where the pictographs live -- 🎯 and ⭐ alike -- and
    the block list this replaces kept missing whole neighbourhoods of it: the star is
    U+2B50 and the hourglass U+231B, both outside every range it named, so "⭐ Майор" was
    refused with "отправьте эмодзи и название". Reported from the chat, 2026-08-10.

    Deliberately does NOT accept "Sm" (mathematical symbols) on its own -- that would make
    "+ Майор" a valid badge. An arrow only counts when it carries U+FE0F, which is the
    author saying "this one is an emoji".
    """
    return any(
        unicodedata.category(char) == "So"
        or 0x1F000 <= ord(char) <= 0x1FAFF
        or ord(char) in _EMOJI_CODEPOINTS
        for char in text
    )


def _is_only_emoji(text: str) -> bool:
    """Whether `text` is a picture and nothing else -- no letters, digits or punctuation.

    Variation selectors, zero-width joiners and skin-tone modifiers count as part of the
    picture: a single emoji is routinely several code points, and splitting one down the
    middle would read as "there is text here" when there is not.
    """
    stripped = "".join(str(text or "").split())
    if not stripped:
        return False
    return all(
        char in "‍️︎"
        or unicodedata.category(char) in ("So", "Sk", "Mn", "Cf")
        or 0x1F000 <= ord(char) <= 0x1FAFF
        or 0x1F3FB <= ord(char) <= 0x1F3FF
        or ord(char) in _EMOJI_CODEPOINTS
        for char in stripped
    )


def parse_custom_badge_spec(text: str) -> tuple[str, str]:
    """Parses "<emoji> <name>" from the create-badge conversation."""
    parts = (text or "").strip().split(maxsplit=1)
    if len(parts) != 2 or not _contains_emoji(parts[0]):
        raise ValueError("Отправьте эмодзи и название через пробел, например: 🎯 Меткий глаз")
    emoji, name = parts[0], _normalise_custom_badge_name(parts[1])
    if len(emoji) > 16:
        raise ValueError("Эмодзи слишком длинный.")
    return emoji, name


def _normalise_custom_badge_name(name: str) -> str:
    """One name contract for creation and the admin rename flow."""
    name = " ".join(str(name or "").split())
    if not name:
        raise ValueError("У значка должно быть название.")
    if len(name) > CUSTOM_BADGE_NAME_MAX_CHARS:
        raise ValueError(f"Название должно быть не длиннее {CUSTOM_BADGE_NAME_MAX_CHARS} символов.")
    return name


def custom_badge_holder_count(entry: str, badge_id: str) -> int:
    """How many members currently hold `badge_id` -- what a delete confirmation needs to
    say out loud, since deleting a definition takes it away from all of them."""
    try:
        data = _load_custom_badge_data(entry)
    except (json.JSONDecodeError, OSError, ValueError):
        return 0
    return sum(1 for assigned in data["assignments"].values() if badge_id in assigned)


def delete_custom_badge(entry: str, badge_id: str) -> Badge | None:
    """Remove a custom badge definition AND every assignment of it.

    The assignments are cleared rather than left dangling: custom_badges_for_user already
    skips an assignment whose definition is gone, so a leftover would be invisible but
    would still count towards somebody's collection total and would come back to life if
    a new badge were ever created with the same id. Returns the deleted badge, or None if
    it was already gone."""
    try:
        data = _load_custom_badge_data(entry)
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    record = data["badges"].pop(badge_id, None)
    if record is None:
        return None
    for assigned in data["assignments"].values():
        assigned.pop(badge_id, None)
    _save_custom_badge_data(entry, data)
    return Badge(
        badge_id=record["id"], emoji=record["emoji"], name=record["name"],
        description="выдан администратором", custom=True,
    )


def split_custom_badge_label(text: str, current_emoji: str) -> tuple[str, str]:
    """One typed line into the (emoji, name) a badge is stored as.

    Three spellings, and all three are what somebody would reasonably type:

      "🎖 Майор запаса" -- the whole label, which becomes the whole label;
      "Майор запаса"    -- the name alone, keeping the emoji the badge already has;
      "🎖"              -- the emoji alone, keeping the name it already has.

    The last one exists because without it a lone emoji would be stored as the NAME and
    the badge would come out reading "⭐ 🎖" -- a state nobody typed and nobody wants.
    """
    stripped = " ".join(str(text or "").split())
    if not stripped:
        raise ValueError("У значка должно быть название.")
    if _is_only_emoji(stripped):
        # Nothing but emoji: this is a new picture, not a new name. "" says so.
        if len(stripped) > 16:
            raise ValueError("Эмодзи слишком длинный.")
        return stripped, ""
    parts = stripped.split(maxsplit=1)
    if len(parts) == 2 and _contains_emoji(parts[0]) and len(parts[0]) <= 16:
        return parts[0], _normalise_custom_badge_name(parts[1])
    return str(current_emoji or ""), _normalise_custom_badge_name(stripped)


def rename_custom_badge(entry: str, badge_id: str, text: str) -> Badge | None:
    """Rename a definition without replacing its id or any holder assignments.

    Takes the badge's WHOLE label, emoji included: what an admin types is what the badge
    reads afterwards. It used to take the name on its own and keep the old emoji, which
    made typing what you could see on the screen the one thing that did not work --
    "🏆 Чемпион" was stored as the NAME, and the badge came out "⭐ 🏆 Чемпион", with the
    old picture still stuck to the front of it. Sending the name alone still keeps the
    current emoji, so nobody's habit breaks.
    """
    data = _load_custom_badge_data(entry)
    record = data["badges"].get(str(badge_id))
    if record is None:
        return None
    emoji, name = split_custom_badge_label(text, record.get("emoji") or "")
    if not name:
        name = str(record.get("name") or "")
    duplicate = next(
        (
            other for other_id, other in data["badges"].items()
            if other_id != str(badge_id)
            and other.get("emoji") == emoji
            and str(other.get("name") or "").casefold() == name.casefold()
        ),
        None,
    )
    if duplicate is not None:
        raise ValueError("Значок с таким эмодзи и названием уже существует.")
    record["emoji"] = emoji
    record["name"] = name
    _save_custom_badge_data(entry, data)
    return Badge(
        badge_id=record["id"], emoji=record["emoji"], name=record["name"],
        description="выдан администратором", custom=True,
    )


def revoke_custom_badge(entry: str, badge_id: str, user_id: int | str) -> Badge | None:
    """Take one badge away from one member, leaving the definition in place. Returns the
    badge, or None when that member did not have it."""
    try:
        data = _load_custom_badge_data(entry)
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    assigned = data["assignments"].get(str(user_id)) or {}
    held = assigned.get(badge_id)
    if held is None:
        return None
    # One press takes ONE off the stack, which is the exact inverse of one press of
    # «Выдать значок». Revoking a ×3 down to nothing in a single tap would make an
    # accidental extra award unfixable without re-granting twice.
    count = _badge_stack_count(held) - 1
    if count >= 1 and isinstance(held, dict):
        held["count"] = count
    else:
        assigned.pop(badge_id, None)
    record = data["badges"].get(badge_id)
    _save_custom_badge_data(entry, data)
    if record is None:
        return None
    return Badge(
        badge_id=record["id"], emoji=record["emoji"], name=record["name"],
        description="выдан администратором", custom=True,
    )


def list_custom_badges(entry: str) -> list[Badge]:
    data = _load_custom_badge_data(entry)
    records = sorted(data["badges"].values(), key=lambda item: item.get("created_at", ""))
    return [
        Badge(
            badge_id=record["id"],
            emoji=record["emoji"],
            name=record["name"],
            description="выдан администратором",
            custom=True,
        )
        for record in records
    ]


def create_custom_badge(
    entry: str,
    emoji: str,
    name: str,
    creator_id: int | str,
    creator_name: str,
) -> Badge:
    data = _load_custom_badge_data(entry)
    if len(data["badges"]) >= MAX_CUSTOM_BADGES:
        raise ValueError(f"В одном чате можно создать не больше {MAX_CUSTOM_BADGES} значков.")
    duplicate = next(
        (
            record
            for record in data["badges"].values()
            if record.get("emoji") == emoji and record.get("name", "").casefold() == name.casefold()
        ),
        None,
    )
    if duplicate:
        raise ValueError("Такой значок уже существует.")
    badge_id = uuid.uuid4().hex[:10]
    record = {
        "id": badge_id,
        "emoji": emoji,
        "name": name,
        "created_at": app_now().isoformat(),
        "created_by_id": str(creator_id),
        "created_by_name": creator_name,
    }
    data["badges"][badge_id] = record
    _save_custom_badge_data(entry, data)
    return Badge(badge_id=badge_id, emoji=emoji, name=name, description="выдан администратором", custom=True)


# The badge everyone who took part in the planting keeps. It lives in the custom-badge
# store rather than among the automatic badges because nothing about it can be recomputed
# from a member's stats -- it records a single afternoon, and after that afternoon there
# is no way to earn it again. Being a custom badge also puts it on the "✨ Уникальные
# значки" line of /stat, which is exactly where a thing you cannot earn belongs.
FOUNDER_BADGE_ID = "founder"
FOUNDER_BADGE_EMOJI = "🌱"
FOUNDER_BADGE_NAME = "Основатель"


def ensure_founder_badge(entry: str) -> Badge:
    """Create the founder badge if it isn't there yet, with a FIXED id so the ceremony
    can hand it out without an administrator having to create it first.

    Deliberately exempt from MAX_CUSTOM_BADGES: this is the bot's own badge, and a chat
    that had already filled its badge budget would otherwise plant its tree with nobody
    getting anything to show for it.
    """
    data = _load_custom_badge_data(entry)
    record = data["badges"].get(FOUNDER_BADGE_ID)
    if record is None:
        record = {
            "id": FOUNDER_BADGE_ID,
            "emoji": FOUNDER_BADGE_EMOJI,
            "name": FOUNDER_BADGE_NAME,
            "created_at": app_now().isoformat(),
            "created_by_id": "bot",
            "created_by_name": "ЕПХ-бот",
        }
        data["badges"][FOUNDER_BADGE_ID] = record
        _save_custom_badge_data(entry, data)
    return Badge(
        badge_id=FOUNDER_BADGE_ID,
        emoji=record["emoji"],
        name=record["name"],
        description="участвовал в посадке дерева ЕПХ",
        custom=True,
    )


def award_founder_badges(entry: str) -> int:
    """Give the founder badge to everyone on the open ceremony's guest list. Returns how
    many members newly received it. Idempotent, so a retried 10:00 post cannot double-
    award or fail on somebody who already has it."""
    state = planting_state(entry)
    if state is None or not state["planters"]:
        return 0
    ensure_founder_badge(entry)
    awarded = 0
    for planter in state["planters"]:
        try:
            # No stacking, for the reason in this function's docstring: the 10:00 post is
            # retried, and a retry must not hand the guest list a founder badge x2.
            _, newly, _count = give_custom_badge(
                entry, FOUNDER_BADGE_ID, planter["user_id"],
                planter.get("display_name") or "", "bot", "ЕПХ-бот",
            )
        except ValueError:
            continue
        awarded += int(newly)
    return awarded


def _badge_stack_count(record) -> int:
    """How many times a member holds one custom badge. Missing means one.

    Assignments written before badges could stack are a bare metadata dict with no count
    in them, and every one of those is a single award -- so the default has to be 1, not 0.
    """
    if not isinstance(record, dict):
        return 1
    try:
        return max(1, int(record.get("count", 1) or 1))
    except (TypeError, ValueError):
        return 1


def stacked_badge_name(name: str, count: int) -> str:
    """The badge's name with its multiplier, and only when there IS a multiplier.

    One of something is not "×1". The suffix appears the moment a second one is granted
    and never before, which is the whole visible point of stacking: a player who has won
    twice should be able to see that they won twice.
    """
    return f"{name} ×{count}" if count > 1 else name


def give_custom_badge(
    entry: str,
    badge_id: str,
    user_id: int | str,
    user_display_name: str,
    giver_id: int | str,
    giver_name: str,
    stack: bool = False,
) -> tuple[Badge, bool, int]:
    """Give one custom badge. Returns (badge, changed_anything, holder's count after).

    `stack` is off by default, and that default is load-bearing. Two callers award badges
    automatically and must stay idempotent: a repeatable quest would otherwise inflate its
    badge every time it is completed, and `award_founder_badges` re-runs on every retried
    ceremony post, which would hand the whole guest list a founder badge ×5. Only a human
    pressing «Выдать значок» means "another one", so only that flow passes stack=True.

    The returned Badge keeps its PLAIN name. The count comes back separately rather than
    baked into it, because the two places that use this want different things: the chat
    announcement says what the badge is, and the reply to the admin says how many.
    """
    data = _load_custom_badge_data(entry)
    record = data["badges"].get(badge_id)
    if record is None:
        raise ValueError("Этот значок больше не существует.")
    user_assignments = data["assignments"].setdefault(str(user_id), {})
    held = user_assignments.get(badge_id)
    count = _badge_stack_count(held) if held is not None else 0
    changed = held is None or stack
    if changed:
        count += 1
        user_assignments[badge_id] = {
            **(held if isinstance(held, dict) else {}),
            "given_at": app_now().isoformat(),
            "given_by_id": str(giver_id),
            "given_by_name": giver_name,
            "user_display_name": user_display_name,
            "count": count,
        }
        _save_custom_badge_data(entry, data)
    badge = Badge(
        badge_id=record["id"],
        emoji=record["emoji"],
        name=record["name"],
        description="выдан администратором",
        custom=True,
    )
    return badge, changed, count


def custom_badges_for_user(entry: str, user_id: int | str) -> list[Badge]:
    """Custom badge corruption must never prevent the core /stat response."""
    try:
        data = _load_custom_badge_data(entry)
    except (json.JSONDecodeError, OSError, ValueError):
        return []
    assigned_ids = data["assignments"].get(str(user_id), {})
    badges = []
    for badge_id, assignment in assigned_ids.items():
        record = data["badges"].get(badge_id)
        if record:
            count = _badge_stack_count(assignment)
            badges.append(
                Badge(
                    badge_id=record["id"],
                    emoji=record["emoji"],
                    name=stacked_badge_name(record["name"], count),
                    description=(
                        "выдан администратором" if count < 2
                        else f"выдан администратором, раз: {count}"
                    ),
                    custom=True,
                )
            )
    return badges


def _load_weekly_contest_data(entry: str) -> dict:
    path = _weekly_contest_path(entry)
    if not path.exists():
        return {"version": WEEKLY_CONTEST_STORE_VERSION, "winners": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("weekly contest file must contain a JSON object")
    data.setdefault("winners", {})
    return data


def record_weekly_contest_winner(
    entry: str,
    contest_week: int,
    user_id: int | str,
    user_display_name: str,
    giver_id: int | str,
    giver_name: str,
) -> tuple[str, int, str | None]:
    """Records exactly one winner for a numbered contest week.

    Returns (status, this user's total wins, existing winner name). Status is one of
    "awarded", "already", or "taken". Repeating the same award is idempotent; assigning
    an already-claimed week to someone else is refused rather than silently overwritten.
    """
    if contest_week < 1:
        raise ValueError("Номер недели должен быть положительным числом.")
    data = _load_weekly_contest_data(entry)
    week_key = str(contest_week)
    existing = data["winners"].get(week_key)
    if existing:
        user_wins = sum(
            1 for winner in data["winners"].values() if str(winner.get("user_id")) == str(user_id)
        )
        if str(existing.get("user_id")) == str(user_id):
            return "already", user_wins, existing.get("user_display_name")
        return "taken", user_wins, existing.get("user_display_name")

    data["winners"][week_key] = {
        "contest_week": contest_week,
        "user_id": str(user_id),
        "user_display_name": user_display_name,
        "given_at": app_now().isoformat(),
        "given_by_id": str(giver_id),
        "given_by_name": giver_name,
    }
    data["version"] = WEEKLY_CONTEST_STORE_VERSION
    _write_json_atomic(_weekly_contest_path(entry), data)
    user_wins = sum(
        1 for winner in data["winners"].values() if str(winner.get("user_id")) == str(user_id)
    )
    return "awarded", user_wins, None


def weekly_wins_for_user(entry: str, user_id: int | str) -> int:
    """How many numbered weekly contests this person has won -- the peer-granted half of
    reputation_score. Same corruption tolerance as the badge readers: a broken store
    costs somebody reputation points, it must never break /stat."""
    try:
        data = _load_weekly_contest_data(entry)
    except (json.JSONDecodeError, OSError, ValueError):
        return 0
    return sum(
        1 for winner in data["winners"].values() if str(winner.get("user_id")) == str(user_id)
    )


def weekly_winner_badges_for_user(entry: str, user_id: int | str) -> list[Badge]:
    try:
        data = _load_weekly_contest_data(entry)
    except (json.JSONDecodeError, OSError, ValueError):
        return []
    won_weeks = sorted(
        int(week)
        for week, winner in data["winners"].items()
        if str(winner.get("user_id")) == str(user_id)
    )
    if not won_weeks:
        return []
    count = len(won_weeks)
    return [
        Badge(
            "weekly_contest_winner",
            "🏆",
            # Shares stacked_badge_name with custom badges so the two read identically --
            # and so a single win stops announcing itself as "×1".
            stacked_badge_name("Победитель Недельного Конкурса", count),
            "победы в неделях: " + ", ".join(f"№{week}" for week in won_weeks),
        )
    ]


def _chat_tier_index(level_number: int) -> int:
    """Which CHAT_LEVEL_TIERS band a level falls in. Levels 1-5 are band 0, 6-10 band 1,
    and so on; a level of 0 (never observed) sits below every band."""
    if level_number < 1:
        return -1
    return min((level_number - 1) // 5, len(CHAT_LEVEL_TIERS) - 1)


def _load_level_state(entry: str) -> dict:
    path = _level_state_path(entry)
    if not path.exists():
        return {"version": LEVEL_STATE_VERSION, "users": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("level state file must contain a JSON object")
    if int(data.get("version", 1)) < LEVEL_STATE_VERSION:
        # Watermarks from the retired single-ladder scheme cannot be compared against the
        # new tracks -- see LEVEL_STATE_VERSION. Start clean so the next observation
        # baselines everyone silently instead of announcing a chat-wide promotion storm.
        return {"version": LEVEL_STATE_VERSION, "users": {}}
    data.setdefault("users", {})
    return data


def _level_announcement_name(user: "UserStats") -> str:
    return f"@{user.username}" if user.username else user.display_name


def record_level_observations(
    entry: str,
    observations: list[tuple["UserStats", int]],
) -> list[str]:
    """Persists observed levels and returns one announcement per actual promotion.

    A user's first observation silently establishes a baseline, preventing a deployment
    from announcing every historical level in a busy chat. Stored progress never moves
    backward if stats are temporarily incomplete or level rules are later adjusted.
    """
    try:
        data = _load_level_state(entry)
    except (json.JSONDecodeError, OSError, ValueError):
        return []
    announcements = []
    dirty = False
    for user, xp in observations:
        level = chat_level(xp)
        rank, _ = painter_rank(user.figurines_painted)
        user_key = str(user.user_id)
        previous = data["users"].get(user_key)
        observed = {
            "chat_level": level.number,
            "chat_level_name": level.tier_name,
            "painter_figurines": rank.minimum_figurines,
            "painter_rank_name": rank.name,
            "observed_at": app_now().isoformat(),
        }
        if previous is None:
            data["users"][user_key] = observed
            dirty = True
            continue
        # Each track is compared against its own watermark, so progress on one never
        # suppresses an announcement on the other, and neither can move backward if
        # stats are momentarily incomplete.
        previous_level = int(previous.get("chat_level", 0))
        previous_figurines = int(previous.get("painter_figurines", 0))
        # Only a TIER change is watched, not every single level: an active member climbs
        # dozens of levels in their first months, and a promotion message per level would
        # put several a day into the chat from the same few people. Tiers (every five
        # levels) are the milestones worth interrupting the room for -- the exact level
        # is always visible in /stat.
        promoted_chat = _chat_tier_index(level.number) > _chat_tier_index(previous_level)
        promoted_painter = rank.minimum_figurines > previous_figurines
        if not promoted_chat and not promoted_painter:
            continue
        observed["chat_level"] = max(level.number, previous_level)
        observed["painter_figurines"] = max(rank.minimum_figurines, previous_figurines)
        data["users"][user_key] = observed
        dirty = True
        name = _level_announcement_name(user)
        # Chat-level promotions are tracked but NOT announced: they are frequent, they
        # come from the same handful of people, and the level is always visible in /stat
        # and the cabinet. The watermark above is still maintained, so announcing them
        # again is restoring two lines, not rebuilding the state.
        if promoted_painter:
            announcements.append(
                f"{name} получил новое звание «{rank.label}»! 🎉🎊🥳"
            )
    if dirty:
        data["version"] = LEVEL_STATE_VERSION
        _write_json_atomic(_level_state_path(entry), data)
    return announcements


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


def _tree_planted_path(entry: str) -> Path:
    return _stats_dir() / f"{_cache_key(entry)}_tree_planted_on"


def tree_planted_on(entry: str) -> date | None:
    """The day this chat's tree was planted, or None before the first morning post."""
    path = _tree_planted_path(entry)
    if not path.exists():
        return None
    try:
        return date.fromisoformat(path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def mark_tree_planted(entry: str, day: date) -> None:
    """Records the planting day, once. Never overwritten: the tree's whole height is
    measured from here, so moving this would silently resize the tree. Use replant_tree
    for the deliberate, administrator-initiated version."""
    if tree_planted_on(entry) is not None:
        return
    _stats_dir().mkdir(parents=True, exist_ok=True)
    _tree_planted_path(entry).write_text(day.isoformat(), encoding="utf-8")


def replant_tree(entry: str, day: date) -> None:
    """Start the tree over from `day`, overwriting an existing planting date.

    The only way to reach these markers on a deployed host, where the stats directory is
    a volume this codebase cannot otherwise touch.

    Marks `day` as already greeted, because the planting announcement the caller has just
    posted IS that day's post. Leaving the marker unset would have the 10:00 loop follow
    the announcement with an ordinary digest reading "выросло на 0 мм, Семечко — 0 мм",
    since the tree was planted moments earlier and has nothing to report yet."""
    _stats_dir().mkdir(parents=True, exist_ok=True)
    _tree_planted_path(entry).write_text(day.isoformat(), encoding="utf-8")
    mark_tree_digest_sent(entry, day)


# --- Посадка семечка ------------------------------------------------------------------
#
# The opening ceremony. An admin posts the invitation, members press the button under it
# for as long as it stays open, and the next 10:00 post names all of them and plants the
# tree. The two halves live in different processes -- the button presses arrive at
# bot_listener.py, the 10:00 post is built by listener.py -- so the guest list goes
# through the stats directory, the one thing both of them can already reach.


def _planting_path(entry: str) -> Path:
    return _stats_dir() / f"{_cache_key(entry)}_planting.json"


def planting_state(entry: str) -> dict | None:
    """The open ceremony, or None when there isn't one.

    {"message_id": int, "chat_id": int, "opened_on": "YYYY-MM-DD", "planters": [...]}
    A corrupt file reads as "no ceremony" rather than raising: this is consulted from the
    10:00 loop, and a broken guest list must not be able to stop the morning post.
    """
    path = _planting_path(entry)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    if not isinstance(data, dict) or "message_id" not in data:
        return None
    data.setdefault("planters", [])
    return data


def planting_is_open(entry: str) -> bool:
    return planting_state(entry) is not None


def open_planting(entry: str, chat_id: int, message_id: int, day: date) -> None:
    """Start collecting presses on the invitation just posted."""
    _stats_dir().mkdir(parents=True, exist_ok=True)
    _write_json_atomic(_planting_path(entry), {
        "chat_id": int(chat_id),
        "message_id": int(message_id),
        "opened_on": day.isoformat(),
        "planters": [],
    })


def add_planter(entry: str, user_id: int | str, display_name: str, username: str | None) -> bool:
    """Sign one member up. True when this is the first time they pressed.

    The name is stored at press time rather than looked up at 10:00 on purpose: the roll
    call has to name people who may have said nothing at all in the chat, and those are
    exactly the members the stats files know nothing about.
    """
    state = planting_state(entry)
    if state is None:
        return False
    key = str(user_id)
    if any(str(planter.get("user_id")) == key for planter in state["planters"]):
        return False
    state["planters"].append({
        "user_id": key,
        "display_name": display_name,
        "username": (username or "").lstrip("@") or None,
    })
    _write_json_atomic(_planting_path(entry), state)
    return True


def planters(entry: str) -> list[tuple[str, str | None]]:
    """[(display_name, username)] in the order they pressed -- whoever was first stays
    first in the roll call."""
    state = planting_state(entry)
    if state is None:
        return []
    return [
        (planter.get("display_name") or f"id{planter.get('user_id')}", planter.get("username"))
        for planter in state["planters"]
    ]


def close_planting(entry: str) -> None:
    """Stop collecting. The invitation's button starts answering "посадка уже закрыта"
    from here on, which is why the file is removed rather than flagged."""
    _planting_path(entry).unlink(missing_ok=True)


# --- Тест inline-кнопки ---------------------------------------------------------------
#
# /preview test_button is intentionally separate from the planting ceremony: it proves
# that callback queries arrive and records their senders without opening a ceremony,
# awarding badges, or changing the tree. The state is persisted because the bot may
# restart while the test post is still visible.


def _preview_button_test_path(entry: str) -> Path:
    return _stats_dir() / f"{_cache_key(entry)}_preview_button_test.json"


def preview_button_test_state(entry: str) -> dict | None:
    path = _preview_button_test_path(entry)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    if not isinstance(data, dict) or "chat_id" not in data or "message_id" not in data:
        return None
    data.setdefault("testers", [])
    return data


def open_preview_button_test(entry: str, chat_id: int, message_id: int) -> None:
    """Start a fresh list for the newly posted test, replacing any older test."""
    _stats_dir().mkdir(parents=True, exist_ok=True)
    _write_json_atomic(_preview_button_test_path(entry), {
        "chat_id": int(chat_id),
        "message_id": int(message_id),
        "testers": [],
    })


def add_preview_button_tester(
    entry: str,
    chat_id: int,
    message_id: int,
    user_id: int | str,
    display_name: str,
    username: str | None,
) -> bool | None:
    """Record one test press.

    True means a new participant, False a repeated press, and None an old/closed test.
    """
    state = preview_button_test_state(entry)
    if (
        state is None
        or int(state["chat_id"]) != int(chat_id)
        or int(state["message_id"]) != int(message_id)
    ):
        return None
    key = str(user_id)
    if any(str(tester.get("user_id")) == key for tester in state["testers"]):
        return False
    state["testers"].append({
        "user_id": key,
        "display_name": display_name,
        "username": (username or "").lstrip("@") or None,
    })
    _write_json_atomic(_preview_button_test_path(entry), state)
    return True


def preview_button_testers(
    entry: str, chat_id: int, message_id: int
) -> list[tuple[str, str | None]] | None:
    """The active test's participants, or None when these ids name an old test."""
    state = preview_button_test_state(entry)
    if (
        state is None
        or int(state["chat_id"]) != int(chat_id)
        or int(state["message_id"]) != int(message_id)
    ):
        return None
    return [
        (tester.get("display_name") or f"id{tester.get('user_id')}", tester.get("username"))
        for tester in state["testers"]
    ]


def close_preview_button_test(entry: str, chat_id: int, message_id: int) -> bool:
    """Close this test without accidentally clearing a newer one."""
    state = preview_button_test_state(entry)
    if (
        state is None
        or int(state["chat_id"]) != int(chat_id)
        or int(state["message_id"]) != int(message_id)
    ):
        return False
    _preview_button_test_path(entry).unlink(missing_ok=True)
    return True


# --- Посты с настраиваемыми кнопками и счётчиками ------------------------------------
#
# /buttons publishes posts whose counters must survive a bot restart. Multiple posts may
# remain active at once, so unlike the one-off preview test they share a keyed store.


def _button_posts_path(entry: str) -> Path:
    return _stats_dir() / f"{_cache_key(entry)}_button_posts.json"


def _button_posts_store(entry: str) -> dict:
    path = _button_posts_path(entry)
    if not path.exists():
        return {"version": 1, "posts": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return {"version": 1, "posts": {}}
    if not isinstance(data, dict) or not isinstance(data.get("posts"), dict):
        return {"version": 1, "posts": {}}
    data["version"] = 1
    return data


def create_button_post(
    entry: str,
    post_id: str,
    chat_id: int,
    message_id: int,
    message_text: str,
    button_texts: list[str],
    created_by_id: int | str,
    dm_chat_id: int,
    photo_file_id: str | None = None,
) -> dict:
    """Persist a newly published counter post and return its normalized state."""
    if not post_id or not button_texts or len(button_texts) > 5:
        raise ValueError("A button post needs an id and one to five buttons.")
    store = _button_posts_store(entry)
    post = {
        "post_id": post_id,
        "chat_id": int(chat_id),
        "message_id": int(message_id),
        "message_text": message_text,
        "buttons": [{"text": text, "count": 0} for text in button_texts],
        "voters": {},
        "created_by_id": str(created_by_id),
        "dm_chat_id": int(dm_chat_id),
        "photo_file_id": photo_file_id or None,
    }
    store["posts"][post_id] = post
    _write_json_atomic(_button_posts_path(entry), store)
    return post


def button_post(entry: str, post_id: str) -> dict | None:
    post = _button_posts_store(entry)["posts"].get(post_id)
    if not isinstance(post, dict):
        return None
    if not isinstance(post.get("buttons"), list) or not post.get("buttons"):
        return None
    return post


def active_button_posts(entry: str) -> list[dict]:
    """Every published counter post, in stable id order, for the refresh loop."""
    store = _button_posts_store(entry)
    return [
        post
        for post_id, post in sorted(store["posts"].items())
        if isinstance(post, dict)
        and post.get("post_id") == post_id
        and isinstance(post.get("buttons"), list)
        and post.get("buttons")
    ]


def record_button_post_vote(
    entry: str,
    post_id: str,
    chat_id: int,
    message_id: int,
    button_index: int,
    user_id: int | str,
) -> tuple[str, int] | None:
    """Record one member's only choice on a post.

    ("added", new button count) means a new vote. ("already", original button index)
    means this member has already chosen either the same or another button. None means
    the callback belongs to an old/invalid post.
    """
    store = _button_posts_store(entry)
    post = store["posts"].get(post_id)
    if (
        not isinstance(post, dict)
        or int(post.get("chat_id", 0)) != int(chat_id)
        or int(post.get("message_id", 0)) != int(message_id)
        or not isinstance(post.get("buttons"), list)
        or button_index < 0
        or button_index >= len(post["buttons"])
    ):
        return None
    voters = post.setdefault("voters", {})
    user_key = str(user_id)
    if user_key in voters:
        return "already", int(voters[user_key])
    button = post["buttons"][button_index]
    button["count"] = int(button.get("count", 0)) + 1
    voters[user_key] = int(button_index)
    _write_json_atomic(_button_posts_path(entry), store)
    return "added", button["count"]


def delete_button_post(entry: str, post_id: str, chat_id: int, message_id: int) -> dict | None:
    """Forget exactly this post without allowing an old control to remove a newer one."""
    store = _button_posts_store(entry)
    post = store["posts"].get(post_id)
    if (
        not isinstance(post, dict)
        or int(post.get("chat_id", 0)) != int(chat_id)
        or int(post.get("message_id", 0)) != int(message_id)
    ):
        return None
    store["posts"].pop(post_id, None)
    _write_json_atomic(_button_posts_path(entry), store)
    return post


def _tree_digest_last_sent_path(entry: str) -> Path:
    return _stats_dir() / f"{_cache_key(entry)}_tree_digest_last_sent"


def tree_digest_last_sent(entry: str) -> date | None:
    path = _tree_digest_last_sent_path(entry)
    if not path.exists():
        return None
    try:
        return date.fromisoformat(path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def mark_tree_digest_sent(entry: str, day: date) -> None:
    _stats_dir().mkdir(parents=True, exist_ok=True)
    _tree_digest_last_sent_path(entry).write_text(day.isoformat(), encoding="utf-8")


def should_send_tree_digest(entry: str, today: date) -> bool:
    """Once per calendar day, per chat. Marker-based rather than "did the loop fire",
    because the loop also checks on startup: without this, every restart between 10:00
    and midnight would post another good morning."""
    last = tree_digest_last_sent(entry)
    return last is None or last < today


def is_figurine_caption(text: str) -> bool:
    """Whether `text` (a raw caption/message text, NOT the "[Photo] "/"[Video] " tagged
    form compute_day_stats works with) carries the #япокрасил hashtag. Callers still have
    to check for an attached photo OR video themselves -- what that looks like differs by
    API (Telethon's `msg.photo`/`msg.video` vs. the Bot API's "photo"/"video" key), so
    there's no one shared check for that half."""
    return FIGURINE_HASHTAG in (text or "").lower()


def _has_hashtag(text: str, hashtag: str) -> bool:
    """Case-insensitive whole-hashtag match; longer lookalike tags do not qualify."""
    return re.search(rf"(?<!\w){re.escape(hashtag)}(?!\w)", text or "", re.IGNORECASE) is not None


def _has_any_hashtag(text: str, hashtags) -> bool:
    """True if `text` carries any one of `hashtags` -- for a tag people spell more than
    one way (see WORKPLACE_HASHTAGS), where each spelling is its own distinct tag."""
    return any(_has_hashtag(text, hashtag) for hashtag in hashtags)


def _iso_week_key(moment: datetime) -> str:
    iso_year, iso_week, _ = moment.date().isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


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
    `message_id` (if given) is appended to this user's figurine-posts list (deduped,
    kept newest-first, and never truncated), for /stat's links to every tracked work
    (see figurine_message_links) -- Telegram has no deep link for a
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
    existing_posts = [tuple(p) for p in u.get("recent_posts", [])]
    # Listener deliveries are normally exactly once, but reconnects can replay an
    # update.  A message id is the stable event identity, so a replay must neither add
    # another figurine nor another temporary arena-buff source.
    already_counted = message_id is not None and any(
        len(post) >= 2 and str(post[1]) == str(message_id) for post in existing_posts
    )
    if not already_counted:
        u["count"] += 1
        u["recent_posts"] = _merge_post_refs(
            existing_posts, [(app_now().isoformat(), message_id)]
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


def recent_figurine_fight_bonus_count(
    entry: str, user_id: int | str, today: date, window_days: int,
) -> int:
    """Number of valid recent #япокрасил posts for the temporary arena-fight buff.

    Finalised days come from ``aggregate`` so an administrator's figurine tombstone is
    applied exactly as it is for XP and the cabinet.  Today is read from the live ledger,
    which makes the +2 fights visible immediately after the listener sees the message.
    The live ledger's post ids, rather than its mutable counter, are authoritative when
    present; this keeps replayed updates idempotent even for data written before the
    duplicate-delivery guard above existed.
    """
    days = max(1, int(window_days))
    start = today - timedelta(days=days - 1)
    key = str(user_id)
    deleted = set((_load_deleted_figurines(entry).get("posts") or {}).keys())
    count = 0
    day = start
    while day <= today:
        # A finalised day is authoritative and already knows how to subtract deleted
        # paintings. If midnight processing is late (or the process restarted), its
        # separate live ledger is still accepted instead -- one source per day, never
        # both, so the same post cannot be double counted during hand-over.
        if _load_day(entry, day) is not None:
            user = aggregate(entry, day, day).get(key)
            count += max(0, int(user.figurines_painted)) if user is not None else 0
        else:
            live = _load_live_figurines(entry, day).get(key) or {}
            posts = live.get("recent_posts") or []
            if posts:
                count += sum(
                    1 for post in posts
                    if len(post) >= 2 and str(post[1]) not in deleted
                )
            else:
                # Old live files predate recent_posts. Their count remains a safe
                # fallback; all new events carry Telegram's message id above.
                count += max(0, int(live.get("count", 0) or 0))
        day += timedelta(days=1)
    return count


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


def _current_streak(active_day_dates: set, today: date, frozen_days: set | None = None) -> int:
    """How many CONSECUTIVE days, counting backward, this person has posted at least
    once -- the number shown next to /stat's fire emoji. Starts counting from today if
    they've already posted today, otherwise from yesterday: the streak isn't considered
    broken just because today isn't over yet and they haven't posted YET (matching how
    "streak" counters commonly work elsewhere, e.g. Duolingo) -- it only actually breaks
    once a full day passes with no post at all. Walks backward through
    UserStats.active_day_dates (the actual dates behind the active_days count) until it
    hits a gap.

    `frozen_days` are days covered by a bought streak freeze (economy.consume_streak_
    freeze). A frozen day bridges the gap -- the walk continues through it -- but does
    NOT itself add to the count: the freeze protects a streak, it doesn't fabricate
    activity that never happened."""
    frozen = frozen_days or set()
    day = today if today.isoformat() in active_day_dates else today - timedelta(days=1)
    streak = 0
    while True:
        key = day.isoformat()
        if key in active_day_dates:
            streak += 1
        elif key in frozen:
            pass  # bridged by a freeze, contributes nothing itself
        else:
            break
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
    separately store a caption-only string.

    Two anti-farming limits apply to the SCORED counters only (`words`, `media`,
    `replies`): a per-sender cooldown (XP_MESSAGE_COOLDOWN_SECONDS) and per-day ceilings
    (XP_DAILY_*_CAP). `messages`, `chars`, `hours`, `last_message_at` and active-day
    membership deliberately still count every message, because those describe what
    actually happened in the chat and are what /stat and the message-count badges read.
    Messages are walked in timestamp order for the cooldown's benefit; every other
    counter here is order-independent."""
    users: dict[str, dict] = {}
    # Per-sender timestamp of the last message that was allowed to score, which is what
    # the cooldown is measured from -- NOT the last message seen. Otherwise a long burst
    # would keep pushing the window forward and suppress everything after it.
    last_scored_at: dict[str, datetime] = {}
    for m in sorted(messages, key=lambda message: message.dt_local):
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
                "not_gay_hashtag_uses": 0,
                "weekly_contest_weeks": [],
                # [ts, message_id] pairs, one per qualifying message this day. The full
                # history stays available so /stat can link to every tracked work.
                "figurine_posts": [],
                # Same [ts, message_id] shape, for the two showcase tags /stat links to
                # (see BEST_WORK_HASHTAGS/WORKPLACE_HASHTAGS).
                "best_work_posts": [],
                "workplace_posts": [],
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
        previous_scored = last_scored_at.get(key)
        scores = (
            not XP_MESSAGE_COOLDOWN_SECONDS
            or previous_scored is None
            or (m.dt_local - previous_scored).total_seconds() >= XP_MESSAGE_COOLDOWN_SECONDS
        )
        if scores:
            last_scored_at[key] = m.dt_local
            u["words"] = min(XP_DAILY_WORD_CAP, u["words"] + len(m.text.split()))
            if m.text.startswith(MEDIA_TAG_PREFIXES):
                u["media"] = min(XP_DAILY_MEDIA_CAP, u["media"] + 1)
            if m.is_reply:
                u["replies"] = min(XP_DAILY_REPLY_CAP, u["replies"] + 1)
        ts = m.dt_local.isoformat()
        if m.text.startswith(MEDIA_TAG_PREFIXES) and is_figurine_caption(m.text):
            u["figurines"] += 1
            u["figurine_posts"].append([ts, m.message_id])
        # Showcase tags, media-gated for the same reason FIGURINE_HASHTAG is: /stat
        # advertises these as a link to the person's own photo, so a text-only message
        # that merely mentions the tag must not become that link. This is not
        # hypothetical -- the day #моялучшая launched, the one text-only use of it in the
        # whole chat was the organizer's announcement ("Сегодня показываем самую лучшую
        # вашу работу"), which would otherwise have been linked as their best work.
        if m.text.startswith(MEDIA_TAG_PREFIXES):
            if _has_any_hashtag(m.text, BEST_WORK_HASHTAGS):
                u["best_work_posts"].append([ts, m.message_id])
            if _has_any_hashtag(m.text, WORKPLACE_HASHTAGS):
                u["workplace_posts"].append([ts, m.message_id])
        if _has_hashtag(m.text, NOT_GAY_HASHTAG):
            u["not_gay_hashtag_uses"] += 1
        if _has_hashtag(m.text, WEEKLY_CONTEST_HASHTAG):
            week_key = _iso_week_key(m.dt_local)
            if week_key not in u["weekly_contest_weeks"]:
                u["weekly_contest_weeks"].append(week_key)
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
        "badge_stats_schema_version": BADGE_STATS_SCHEMA_VERSION,
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


def _backfill_day_badge_stats(entry: str, day: date, payload: dict, messages: list, log=print) -> bool:
    """Adds only the new hashtag-derived fields to an existing immutable day.

    XP counters are left byte-for-byte unchanged, so introducing badges cannot reprice
    old activity. This is used once for recent already-recorded days whose raw transcript
    is still available through the normal transcript cache.
    """
    if payload.get("badge_stats_schema_version", 0) >= BADGE_STATS_SCHEMA_VERSION:
        return False
    recomputed = compute_day_stats(messages)
    for user_id, existing_user in payload.get("users", {}).items():
        source = recomputed.get(user_id, {})
        existing_user["not_gay_hashtag_uses"] = source.get("not_gay_hashtag_uses", 0)
        existing_user["weekly_contest_weeks"] = source.get("weekly_contest_weeks", [])
        existing_user["best_work_posts"] = source.get("best_work_posts", [])
        existing_user["workplace_posts"] = source.get("workplace_posts", [])
    payload["badge_stats_schema_version"] = BADGE_STATS_SCHEMA_VERSION
    payload["badge_stats_backfilled_at"] = app_now().isoformat()
    _write_json_atomic(_path(entry, day), payload)
    log(f"[stats] backfilled hashtag badges for '{entry}' on {day}")
    return True


async def finalize_and_record(client, chat_ref, entry: str, day: date, tz, log=print) -> bool:
    """The whole "close out a day" step listener.py's midnight rollover (and its startup
    catch-up) calls once per (entry, day): makes sure `day`'s transcript cache is
    complete (see telegram_fetch.ensure_day_finalized) then records that day's per-user
    stats from it (see record_day). A recently recorded day from before hashtag badges
    existed is read once from the transcript cache and augmented with only those badge
    counters; its XP fields are never recomputed."""
    existing = _load_day(entry, day)
    if existing and existing.get("badge_stats_schema_version", 0) >= BADGE_STATS_SCHEMA_VERSION:
        return False
    await telegram_fetch.ensure_day_finalized(client, chat_ref, day, tz, log=log)
    _, messages = await telegram_fetch.fetch_range_messages_cached(
        client=client, chat_ref=chat_ref, start_day=day, end_day=day, tz=tz, log=log,
    )
    if existing:
        return _backfill_day_badge_stats(entry, day, existing, messages, log=log)
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
    # each such day contributes its original flat messages*LEGACY_XP_PER_MESSAGE value
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
    not_gay_hashtag_uses: int = 0
    # ISO week keys, e.g. "2026-W30". A set makes several #итогинедели posts in the
    # same week count only once, including when that week spans two merged day files.
    weekly_contest_weeks: set = field(default_factory=set)
    # ALL of this person's [ts, message_id] figurine posts ever, newest first, never
    # truncated -- /stat links to the complete tracked set, while
    # format_procrastinators uses the first item as the true most recent post.
    recent_figurine_posts: list = field(default_factory=list)
    # Same [ts, message_id] newest-first shape as recent_figurine_posts, for the two
    # showcase tags. /stat renders only the newest of each as one link (both tags
    # describe a CURRENT state -- "my best work", "my workplace" -- so an older post is
    # superseded rather than accumulated), but the full history is still kept here so
    # that display choice stays reversible without a re-scan.
    best_work_posts: list = field(default_factory=list)
    workplace_posts: list = field(default_factory=list)
    active_days: int = 0
    # ISO date strings ("YYYY-MM-DD") for every day this person posted at least once --
    # the actual DATES behind the `active_days` count above, needed to walk backward day
    # by day for a "current streak" (see _current_streak). Populated by _merge_day from
    # each merged day's own "day" key (present on every recorded day-file and on every
    # synthetic live-today payload built for this purpose -- see aggregate_live etc.).
    active_day_dates: set = field(default_factory=set)
    hours: dict = field(default_factory=dict)
    last_message_at: str | None = None
    # Explicit administrative adjustments live outside message/day caches. They are
    # still XP, so coins and levels consume them through the same methods as earned XP.
    bonus_xp: int = 0

    def xp(self, words_per_point: float) -> int:
        """`words_per_point` -- see the function of that name -- is this chat's frozen
        words/message baseline. It's a required argument rather than a module constant
        because, unlike the other XP_PER_* values, it's calibrated per chat (from
        that chat's own real activity) rather than picked as one universal number (see
        the module docstring's Scoring section) -- but once calibrated, it's fixed, same
        as the others. `legacy_message_points` (days from before word-tracking existed)
        and `words / words_per_point` (days from after) are two DIFFERENT ways of
        scoring a message, added side by side rather than one replacing the other --
        see UserStats.legacy_message_points."""
        return round(
            self.legacy_message_points
            + self.words / words_per_point
            + self.media * XP_PER_MEDIA_MESSAGE
            + self.replies * XP_PER_REPLY
            + self.active_days * XP_PER_ACTIVE_DAY
            + self.figurines_painted * XP_PER_FIGURINE
            + self.bonus_xp
        )

    def score(self, words_per_point: float) -> int:
        """Backward-compatible alias for integrations that imported the old name."""
        return self.xp(words_per_point)


def day_xp_ranking(entry: str, day: date, words_per_point: float) -> list[tuple[str, UserStats, int]]:
    """Everyone active on `day` as (user_id, stats, XP earned that day), highest first.

    One implementation for "who moved the needle yesterday", shared by the ЕПХ tree's
    morning digest and the daily chatter prize so the two can never rank the same day
    differently. Deleted figurines are subtracted first, exactly as the tree expects.

    Ties break on user id so two runs of the same day agree -- the prize pays real coins
    off this order, and a coin flip between two people on equal XP is not something a
    restart should be able to re-decide.
    """
    users = aggregate(entry, day, day)
    _apply_deleted_figurines(entry, users)
    ranked = [
        (str(user_id), user, user.xp(words_per_point)) for user_id, user in users.items()
    ]
    ranked.sort(key=lambda row: (-row[2], row[0]))
    return ranked


def _apply_deleted_figurines(entry: str, users: dict[str, UserStats]) -> None:
    """Remove tombstoned posts and their one-per-post figurine credit in-place."""
    records = _load_deleted_figurines(entry).get("posts", {})
    if not records:
        return
    deleted_by_user: dict[str, set[str]] = {}
    for message_id, record in records.items():
        user_id = str((record or {}).get("user_id", ""))
        if user_id:
            deleted_by_user.setdefault(user_id, set()).add(str(message_id))
    for user_id, user in users.items():
        deleted_ids = deleted_by_user.get(str(user_id))
        if not deleted_ids:
            continue
        kept_posts = []
        removed = 0
        for post in user.recent_figurine_posts:
            if len(post) >= 2 and str(post[1]) in deleted_ids:
                removed += 1
            else:
                kept_posts.append(post)
        if removed:
            user.recent_figurine_posts = kept_posts
            user.figurines_painted = max(0, user.figurines_painted - removed)


def _merge_post_refs(existing: list, new_posts) -> list:
    """Combines `existing` [ts, message_id] pairs with `new_posts` (any iterable of the
    same shape) and sorts newest-first -- deliberately NEVER truncates: every qualifying
    post is kept forever, so /stat can link to every tracked work and
    format_procrastinators can always find the TRUE most recent post. The one place this
    merge+dedup happens, used by both _merge_day (across recorded days) and
    record_figurine_live/_live_today_users (today's live counter).

    Tag-agnostic despite the figurine-shaped history above: the same merge backs
    UserStats.best_work_posts and .workplace_posts (see BEST_WORK_HASHTAGS/
    WORKPLACE_HASHTAGS), which need identical newest-first dedup semantics.

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
            s.legacy_message_points += u.get("messages", 0) * LEGACY_XP_PER_MESSAGE
        s.media += u.get("media", 0)
        s.replies += u.get("replies", 0)
        s.figurines_painted += u.get("figurines", 0)
        s.not_gay_hashtag_uses += u.get("not_gay_hashtag_uses", 0)
        s.weekly_contest_weeks.update(u.get("weekly_contest_weeks", []))
        if u.get("figurine_posts"):
            s.recent_figurine_posts = _merge_post_refs(s.recent_figurine_posts, u["figurine_posts"])
        if u.get("best_work_posts"):
            s.best_work_posts = _merge_post_refs(s.best_work_posts, u["best_work_posts"])
        if u.get("workplace_posts"):
            s.workplace_posts = _merge_post_refs(s.workplace_posts, u["workplace_posts"])
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
    _apply_deleted_figurines(entry, combined)
    return combined


def _apply_xp_grants(entry: str, combined: dict[str, UserStats]) -> None:
    """Merge persistent adjustments into all-time totals, never daily rankings."""
    for user_id, row in _load_xp_grants(entry)["users"].items():
        if not isinstance(row, dict):
            continue
        user = combined.setdefault(str(user_id), UserStats(user_id=str(user_id)))
        if row.get("username") and not user.username:
            user.username = str(row["username"])
        if row.get("display_name") and user.display_name == "Unknown":
            user.display_name = str(row["display_name"])
        grants = row.get("grants")
        if not isinstance(grants, dict):
            continue
        for grant in grants.values():
            if not isinstance(grant, dict):
                continue
            try:
                # Signed, not clamped at zero: an administrator has to be able to take XP
                # back as well as give it (see adjust_bonus_xp). A stored negative used to
                # be silently discarded here, which made a correction look like it had
                # worked while changing nothing at all.
                user.bonus_xp += int(grant.get("amount", 0))
            except (TypeError, ValueError):
                continue


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
    _apply_xp_grants(entry, combined)
    _apply_deleted_figurines(entry, combined)
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


def parse_top_argument(arg: str) -> str:
    """Period from whatever followed "/top", defaulting to "today".

    Takes the ARGUMENT rather than the whole command line, so that "/top all" and the
    "/topall" menu alias resolve identically. The alias exists because Telegram only
    accepts [a-z0-9_] in a registered command name -- "/top all" cannot be a menu entry,
    so the space has to go somewhere."""
    return _normalize_period(arg) or "today"


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
                "not_gay_hashtag_uses": 0, "weekly_contest_weeks": [],
                "figurine_posts": [], "best_work_posts": [], "workplace_posts": [],
                "hours": {}, "last_message_at": None,
            },
        )
        u["figurines"] = max(u.get("figurines", 0), live.get("count", 0))
        if live.get("recent_posts"):
            u["figurine_posts"] = _merge_post_refs(u["figurine_posts"], live["recent_posts"])
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
        _apply_deleted_figurines(entry, combined)
    return combined


async def aggregate_all_time_live(client, chat_ref, entry: str, tz, log=print) -> dict[str, UserStats]:
    """Like aggregate_all_time(), plus today's live snapshot merged on top -- see
    aggregate_live's same reasoning. Used by /stat, and by resolve_stat_target so someone
    who has only ever posted today (no recorded day yet at all) is still found."""
    combined = aggregate_all_time(entry)
    today = datetime.now(tz).date()
    live_users = await _live_today_users(client, chat_ref, entry, tz, log=log)
    _merge_day(combined, {"day": today.isoformat(), "users": live_users})
    _apply_deleted_figurines(entry, combined)
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


async def collect_level_up_announcements(
    client,
    chat_ref,
    entry: str,
    tz,
    log=print,
) -> list[str]:
    """Observes every tracked user's current level, normally once at daily rollover."""
    users = await aggregate_all_time_live(client, chat_ref, entry, tz, log=log)
    if not users:
        return []
    wpp = await words_per_point(client, chat_ref, entry, tz, log=log)
    observations = [(user, user.xp(wpp)) for user in users.values()]
    return record_level_observations(entry, observations)


def most_improved_user(
    current: dict[str, UserStats],
    previous: dict[str, UserStats],
    words_per_point_value: float,
) -> tuple[UserStats, int] | None:
    """Largest positive XP change between two equal windows, including newcomers."""
    candidates = []
    for user_id, user in current.items():
        current_xp = user.xp(words_per_point_value)
        previous_user = previous.get(user_id)
        previous_xp = previous_user.xp(words_per_point_value) if previous_user else 0
        delta = current_xp - previous_xp
        if delta > 0:
            candidates.append((delta, current_xp, user))
    if not candidates:
        return None
    delta, _, user = max(candidates, key=lambda item: (item[0], item[1]))
    return user, delta


async def format_top(client, chat_ref, entry: str, period: str, tz, top_n: int, log=print) -> str:
    if period == "all":
        combined = await aggregate_all_time_live(client, chat_ref, entry, tz, log=log)
        start = None
    else:
        start, end = resolve_period_window(period, tz)
        combined = await aggregate_live(client, chat_ref, entry, start, end, tz, log=log)
    wpp = await words_per_point(client, chat_ref, entry, tz, log=log)
    # Same order resolve_stat_target ranks by, ties included, so /top and /stat agree.
    ranked = sorted(combined.values(), key=lambda s: (-s.xp(wpp), s.user_id))[:top_n]
    if not ranked:
        return "Пока нет данных за этот период."
    lines = ["🏆 Топ по XP:", ""]
    for i, s in enumerate(ranked, start=1):
        lines.append(f"{i}. {s.display_name} — {s.xp(wpp)} XP")
    if period == "week" and start is not None:
        previous_end = start - timedelta(days=1)
        previous_start = previous_end - timedelta(days=PERIOD_LOOKBACK_DAYS["week"])
        previous = aggregate(entry, previous_start, previous_end)
        improved = most_improved_user(combined, previous, wpp)
        if improved:
            user, delta = improved
            lines.extend(["", f"🚀 Прорыв недели: {user.display_name} (+{delta} XP к прошлой неделе)"])
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
    ranked = sorted(pool.values(), key=lambda s: s.xp(wpp), reverse=True)

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
        last_at = posts[0][0]  # newest-first, see _merge_post_refs
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


def post_message_links(chat_username: str | None, chat_id: int | None, posts: list) -> list[str]:
    """Return direct links for a [ts, message_id] ref list, preserving its newest-first
    order (see _merge_post_refs).

    Skips a post rather than emitting a broken link when the chat has neither a public
    username nor a resolvable marked id; see figurine_message_link for those cases.
    """
    links = []
    for _, message_id in posts:
        link = figurine_message_link(chat_username, chat_id, message_id)
        if link:
            links.append(link)
    return links


def figurine_message_links(chat_username: str | None, chat_id: int | None, user: UserStats) -> list[str]:
    """Direct links for every numbered figurine post of `user`, newest first."""
    return post_message_links(chat_username, chat_id, numbered_figurine_posts(user))


def showcase_message_links(
    chat_username: str | None, chat_id: int | None, user: UserStats
) -> tuple[str | None, str | None]:
    """(best-work link, workplace link) for `user` -- the NEWEST post of each showcase
    tag, or None where there is none to link. Only the newest is returned because both
    tags describe a current state that a later post supersedes; see
    UserStats.best_work_posts for why the older ones are still stored."""
    best = post_message_links(chat_username, chat_id, user.best_work_posts)
    workplace = post_message_links(chat_username, chat_id, user.workplace_posts)
    return (best[0] if best else None, workplace[0] if workplace else None)


# /stat links only the newest works; /работы lists every one of them. The full history
# is never trimmed (see _merge_post_refs) -- this is purely how much one reply shows.
STAT_WORKS_SHOWN = 10
# Typed in the chat as "/работы", which reads better there but is not a command Telegram
# recognises ([a-z0-9_] only), so it can never be highlighted or put in the menu --
# "/works" is the spelling that always reaches the bot. Same pairing as PLANT_COMMANDS.
WORKS_COMMANDS = ("/работы", "/works")
# Telegram's limit is 4096 characters of VISIBLE text per message (links' URLs do not
# count). Kept well under it so the header and separators always fit too.
WORKS_MESSAGE_CHAR_BUDGET = 3_500


def parse_works_command(text: str) -> str | None:
    """The argument after "/работы" or "/works" ("" for none), or None when `text` is not
    that command. Expects the "@botname" suffix already stripped (strip_command_bot_
    mention). Matched as a whole word, so "/worksheet" is not "/works"."""
    stripped = (text or "").strip()
    for spelling in WORKS_COMMANDS:
        if re.match(rf"{re.escape(spelling)}(?:\s|$)", stripped, re.IGNORECASE):
            return stripped[len(spelling):].strip()
    return None


def _work_caption(position: int, name: str | None) -> str:
    """The visible label of one work link: its number, plus its name when it has one. The
    number always stays: /deletepokras and the cabinet's rename take it as their argument,
    so replacing it with a name would leave nothing to point at."""
    return f"{position}. {name}" if name else str(position)


def _work_link(position: int, link: str, name: str | None) -> str:
    return f'<a href="{escape(link, quote=True)}">{escape(_work_caption(position, name))}</a>'


def works_handle(user: UserStats) -> str:
    """How to name this member after /работы so the lookup finds them again."""
    return f"@{user.username}" if user.username else user.display_name


def format_stat(
    user: UserStats,
    rank: int,
    total: int,
    xp: int,
    streak: int,
    figurine_links: list[str] | None = None,
    custom_badges: list[Badge] | None = None,
    best_work_link: str | None = None,
    workplace_link: str | None = None,
    coins: int | None = None,
    reputation: int = 0,
    custom_title: str | None = None,
    bot_username: str | None = None,
    work_names: list | None = None,
) -> str:
    """Build an HTML-formatted `/stat` message.

    `xp` and `streak` are computed by the caller (resolve_stat_target) rather than
    read/derived off `user` directly -- UserStats.xp needs the chat's current
    words_per_point, and the streak needs "today" (see _current_streak), neither of which
    this function has any way to get itself (it's sync, with no client/chat_ref/tz).
    User-controlled fields are escaped so callers can safely send the result with
    Telegram's HTML parse mode; that enables compact clickable work numbers.

    `coins`, `reputation` and `custom_title` come from economy.py the same way and for
    the same reason -- this module must not import economy (economy imports stats), so
    the caller reads them and passes them down. `coins` falling back to the derived
    coins_for_xp keeps every existing caller and test rendering a correct balance for
    somebody who has never spent anything.

    Kept short on purpose: one line per fact, related facts sharing a line, badges on one
    line each, and only the newest STAT_WORKS_SHOWN works -- the rest are one /работы
    away. It is posted into a busy group chat, where a reply that fills the screen is a
    reply that pushes the conversation away.
    """
    avg = user.messages / user.active_days if user.active_days else 0.0
    level = chat_level(xp)
    rank_level, _ = painter_rank(user.figurines_painted)
    reputation_emoji, reputation_name = reputation_tier(reputation)
    bar = progress_bar(chat_level_progress(xp))

    # The name lives in the header rather than on its own "Имя:" line. A bought title
    # (see economy.set_title) goes right below it in quotes, kept clearly separate so it
    # can never be mistaken for the person's actual name.
    lines = [f"📊 Статистика {escape(user.display_name)}:"]
    if custom_title:
        lines.append(f"«{escape(custom_title)}»")
    # Three independent tracks. The chat level always moves for anybody who talks, the
    # painter rank only for figurines, and reputation only when somebody else grants it
    # -- so nobody is ever looking at a screen where nothing can progress. The bar shows
    # position within the current chat level WITHOUT printing the target, preserving the
    # existing "don't reveal the next requirement" rule.
    coins_str = _thousands(coins if coins is not None else coins_for_xp(xp))
    lines += [
        "",
        f"⭐️ XP: {_thousands(xp)} · 🪙 Монеты: {coins_str}",
        f"📈 Место в рейтинге: {rank} из {total}",
        f"🧩 Уровень: {escape(level.label)}  {bar}",
        f"🎨 Звание: {escape(rank_level.label)}",
        f"{reputation_emoji} Репутация: {reputation} ({escape(reputation_name)})",
        "",
    ]

    # The two showcase posts ride on the figurine line, each as a one-tap link named for
    # what it is, and vanish when the person has no such post.
    figurine_line = f"🖼️ Фигурок: {user.figurines_painted} ({FIGURINE_HASHTAG})"
    if workplace_link:
        figurine_line += f' · <a href="{escape(workplace_link, quote=True)}">🛠️ Рабочее место</a>'
    if best_work_link:
        figurine_line += f' · <a href="{escape(best_work_link, quote=True)}">💎 Моя лучшая</a>'
    activity_line = f"📅 Активных дней: {user.active_days}"
    if streak > 0:
        activity_line += f" · 🔥 Серия: {_ru_days(streak)}"
    messages_line = f"💬 Сообщений: {_thousands(user.messages)} ({avg:.1f} в день)"
    if user.hours:
        messages_line += f" · 🕘 {_favorite_hour_label(user.hours)}"
    lines += [figurine_line, activity_line, messages_line]

    # Hand-made badges lead, on their own line: they are the only ones somebody chose to
    # give this person. Split on Badge.custom, which is what that flag is for -- a weekly-
    # contest win is assigned by an administrator but is still earned, so it sits with
    # the two automatic badges.
    unique = [badge for badge in (custom_badges or []) if badge.custom]
    earned = earned_badges(user) + [badge for badge in (custom_badges or []) if not badge.custom]
    lines.append("")
    if unique:
        lines.append("✨ Уникальные значки: " + " · ".join(escape(badge.label) for badge in unique))
    if earned:
        lines.append("🏅 Значки: " + " · ".join(escape(badge.label) for badge in earned))
    elif not unique:
        lines.append("🏅 Значки: пока нет")

    if figurine_links:
        names = list(work_names or [])
        shown = [
            _work_link(index, link, names[index - 1] if index - 1 < len(names) else None)
            for index, link in enumerate(figurine_links[:STAT_WORKS_SHOWN], start=1)
        ]
        heading = "🎨 Последние работы" if len(figurine_links) > STAT_WORKS_SHOWN else "🎨 Работы"
        lines += ["", f"{heading}: " + " · ".join(shown)]
        if len(figurine_links) > STAT_WORKS_SHOWN:
            lines.append(
                f"📂 Все {len(figurine_links)}: {WORKS_COMMANDS[0]} {escape(works_handle(user))}"
            )

    # Last line, so it reads as "and there's more over there" rather than competing with
    # the numbers above. The ?start= payload means one tap opens the cabinet instead of
    # dropping somebody into an empty DM where they still have to know a command.
    # Omitted when there is no bot to link to.
    if bot_username:
        lines += [
            "",
            f'<a href="https://t.me/{escape(bot_username, quote=True)}'
            f'?start={CABINET_START_PAYLOAD}">👤 Открыть личный кабинет</a>',
        ]
    return "\n".join(lines)


def format_works(
    user: UserStats, figurine_links: list[str], work_names: list | None = None,
) -> list[str]:
    """Every tracked work of `user` as numbered links, split into as many messages as
    Telegram's length limit needs. /stat shows the newest STAT_WORKS_SHOWN; this is the
    rest. Numbered exactly like /stat, the cabinet and /deletepokras (newest first)."""
    header = f"🎨 Работы {escape(user.display_name)} — {len(figurine_links)} ({FIGURINE_HASHTAG})"
    if not figurine_links:
        return [f"{header}\n\nПока ни одной — выложи работу с {FIGURINE_HASHTAG}."]
    names = list(work_names or [])
    messages: list[str] = []
    current: list[str] = []
    visible = len(header)
    for index, link in enumerate(figurine_links, start=1):
        name = names[index - 1] if index - 1 < len(names) else None
        cost = len(_work_caption(index, name)) + 3  # " · "
        if current and visible + cost > WORKS_MESSAGE_CHAR_BUDGET:
            messages.append(" · ".join(current))
            current, visible = [], 0
        current.append(_work_link(index, link, name))
        visible += cost
    messages.append(" · ".join(current))
    messages[0] = f"{header}\n\n{messages[0]}"
    return messages


def _find_user(users: dict[str, UserStats], name_or_username: str) -> UserStats | None:
    """Case-insensitive match against a tracked user's @username (exact), then their
    display name (exact), then a substring of it -- same idea as telegram_fetch.
    sender_matches, but against an already-aggregated {user_id: UserStats} dict instead
    of a live transcript. The exact display-name pass exists so "Саша" finds Саша even
    when "Саша Иванов" happens to come first."""
    needle = name_or_username.strip().lstrip("@").lower()
    if not needle:
        return None
    for s in users.values():
        if s.username and needle == s.username.lower():
            return s
    for s in users.values():
        if needle == s.display_name.strip().lower():
            return s
    for s in users.values():
        if needle in s.display_name.lower():
            return s
    return None


async def chat_tree_totals(
    client, chat_ref, entry: str, day: date, tz, log=print, live_total: bool = False
):
    """(tree XP, that day's chat XP, that day's contributors) for the ЕПХ tree.

    The tree's total is counted from the PLANTING DAY forward, not from the chat's whole
    history. That is what makes "сегодня мы посадили семечко" true: this chat had months
    of tracked activity before the tree existed, and counting it would plant a seed that
    is already a metre tall. It also means the three-year horizon starts when the tree
    does. Before planting (the very first run) the total is 0 by definition.

    Contributors are [(display_name, username, xp)] sorted highest-first for exactly
    `day` -- the morning post names who moved the tree yesterday, so this reads one
    recorded day rather than a window.

    `live_total` includes today so far. /tree wants it ("how are we doing right now"); the
    morning post does not, because it reports a closed day and a total that had jumped by
    an unexplained amount would contradict the growth figure right above it.
    """
    wpp = await words_per_point(client, chat_ref, entry, tz, log=log)
    planted = tree_planted_on(entry)
    today = datetime.now(tz).date()

    if planted is None:
        total_xp = 0
    elif live_total:
        grown = await aggregate_live(client, chat_ref, entry, planted, today, tz, log=log)
        total_xp = sum(user.xp(wpp) for user in grown.values())
    else:
        grown = aggregate(entry, planted, today - timedelta(days=1))
        _apply_deleted_figurines(entry, grown)
        total_xp = sum(user.xp(wpp) for user in grown.values())

    ranked = day_xp_ranking(entry, day, wpp)
    contributors = [(user.display_name, user.username, xp) for _, user, xp in ranked]
    return total_xp, sum(item[2] for item in contributors), contributors


async def resolve_stat_target(
    client, chat_ref, entry: str, arg: str, requester_username: str | None, requester_display_name: str, tz,
    log=print, frozen_days_for=None, requester_id=None,
) -> tuple[UserStats | None, int | None, int, int | None, int | None]:
    """Resolves who a /stat command is asking about: an explicit argument (@username or
    a name fragment) if given, otherwise the requester's own tracked stats -- by their
    Telegram id when the caller knows it, then by @username (exact), then by display
    name. Fetches the all-time-plus-today-live aggregate exactly once regardless of how
    many of those lookups it takes, rather than once per attempt.

    The id comes first because a name is not an identity: a member without a username
    whose display name happened to be part of somebody else's ("Саша" inside "Саша
    Иванов") used to be shown that other person's XP, level and works as their own.

    Returns (user, rank, total, xp, streak): `rank` is the person's 1-based position by
    all-time XP among everyone ever tracked for this chat (ties broken by user id, so two
    calls agree), and `total` is how many people that's out of. `xp` and `streak` are
    returned alongside `user` (rather than left for the caller to derive from `user`
    itself) since both need context this function already has and format_stat doesn't --
    words_per_point for XP (see UserStats.xp) and today's date for streak (see
    _current_streak). The chat level is scored on that same all-time `xp`.
    `rank`/`xp`/`streak` are None (with user) if no match was found; `total` is still
    meaningful in that case.

    `frozen_days_for`, if given, is called with the resolved user_id and returns the days
    covered by bought streak freezes (economy.apply_streak_freezes). It is injected as a
    callback rather than imported because economy imports this module, and it can only be
    called once the target is known -- which is here, not in the caller."""
    all_time = await aggregate_all_time_live(client, chat_ref, entry, tz, log=log)
    total = len(all_time)
    if arg:
        user = _find_user(all_time, arg)
    else:
        user = all_time.get(str(requester_id)) if requester_id is not None else None
        if user is None and requester_username:
            user = _find_user(all_time, requester_username)
        if user is None:
            user = _find_user(all_time, requester_display_name)
    if user is None:
        return None, None, total, None, None
    wpp = await words_per_point(client, chat_ref, entry, tz, log=log)
    ranked = sorted(all_time.values(), key=lambda s: (-s.xp(wpp), s.user_id))
    rank = next(i for i, s in enumerate(ranked, start=1) if s.user_id == user.user_id)
    today = datetime.now(tz).date()
    frozen = None
    if frozen_days_for is not None:
        try:
            frozen = frozen_days_for(user.user_id, user.active_day_dates, today)
        except Exception:
            log("[stats] streak freeze lookup failed; falling back to an unfrozen streak")
            frozen = None
    streak = _current_streak(user.active_day_dates, today, frozen)
    return user, rank, total, user.xp(wpp), streak
