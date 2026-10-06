"""Weekly-contest voting: collects #итогинедели posts into entries, lets an administrator
choose which ones are admitted, and records one vote per Telegram user.

Three things live here, all of them pure logic or plain file I/O so they can be tested
without a network: collecting entries from a chat, the on-disk poll, and verifying the
signed identity Telegram hands a Mini App. The HTTP surface is vote_web.py; the bot
command that creates a poll is in bot_listener.py.

An entry is ONE POST, not one message. Several photos sent together are an album --
Telegram delivers those as separate messages sharing a grouped_id, and only one of them
carries the caption (so only one carries the hashtag). Collecting per-message would turn a
five-photo post into one entry with the text and four blank ones; entries are therefore
grouped by grouped_id first and the hashtag is looked for anywhere in the group.

Storage is one JSON file per poll under DATA_DIR, with the photos next to it. DATA_DIR
defaults to the current directory, which on a host with no persistent disk means a poll
does not survive a redeploy -- see transcript_cache.py's identical note. For voting that
matters more than it does for a cache: a lost poll is lost votes, not a re-fetch.
"""

import asyncio
import hashlib
import hmac
import json
import math
import os
import re
import shutil
import threading
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl

DATA_DIR = Path(os.getenv("DATA_DIR", "."))
VOTING_DIR = DATA_DIR / "voting"
# Announced results live in their own subtree rather than beside the polls: latest_poll
# globs "<key>_*.json" straight out of the voting dir, and a results file named on the
# same key would be picked up by that glob and fail to parse as a Poll.
RESULTS_DIR = VOTING_DIR / "results"

# The hashtag that nominates a post. Kept in sync with stats.WEEKLY_CONTEST_HASHTAG,
# imported lazily in collect_entries so this module stays importable on its own.
CONTEST_HASHTAG = "#итогинедели"
# What the weekly contest is called wherever a contest needs a name (the Hall of Fame, the
# status panel). A thematic contest is called by the title its administrator gave it.
WEEKLY_CONTEST_TITLE = "Итоги недели"

# A collection covers whole CONTEST WEEKS -- from Monday 00:00 local through the moment of
# collecting -- rather than a rolling number of days. A rolling window's reach depends on
# the day it is run, so the same button would find a different set of works on Sunday
# than on Monday; a calendar boundary finds the same ones whenever it is pressed.
CONTEST_WEEK_STARTS_ON = 0  # Monday, matching datetime.weekday()


def contest_week_start(now_local: datetime) -> datetime:
    """Midnight on the Monday of the week `now_local` falls in.

    Uses the plain weekday rather than isocalendar so the result is a real local datetime
    that keeps `now_local`'s timezone -- the poll id is keyed on the ISO week, and the two
    agree because both treat Monday as the first day.
    """
    days_since_monday = (now_local.weekday() - CONTEST_WEEK_STARTS_ON) % 7
    return (now_local - timedelta(days=days_since_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

# A Mini App's initData is signed but replayable forever, so it also carries the time it
# was issued. Anything older than this is refused -- it means a stale page (or a copied
# URL), not a live session.
INIT_DATA_MAX_AGE_SECONDS = 24 * 60 * 60

# Telegram caps an album at 10 items.
MAX_ALBUM_ITEMS = 10


def _has_hashtag(text: str, hashtag: str) -> bool:
    """Case-insensitive whole-hashtag match; longer lookalike tags do not qualify. Same
    rule as stats._has_hashtag -- duplicated rather than imported so this module has no
    dependency on stats.py's much heavier import graph."""
    return re.search(rf"(?<!\w){re.escape(hashtag)}(?!\w)", text or "", re.IGNORECASE) is not None


# ------------------------------------------------------------------ which hashtag to collect
#
# /vote collects #итогинедели by default, and a THEMATIC contest ("Лучший аниме-покрас",
# "Лучшая миниатюра 32 мм") collects a hashtag of its own. Each hashtag gets its own poll per
# week (poll_id_for), so a thematic contest collected while the weekly vote is running can
# never touch that vote's works, ballots, results record or board picture -- they are all
# keyed by poll id.

# Longer than this stops being a hashtag anybody types and starts being a sentence.
HASHTAG_MAX_LENGTH = 40
# A theme's title is a heading on a phone screen and a badge on an artist's profile.
THEME_TITLE_MAX_LENGTH = 60
# The badge is one emoji; a ZWJ sequence (👩‍🎨) is several code points, so this is generous.
THEME_BADGE_MAX_LENGTH = 16
DEFAULT_THEME_BADGE = "🏅"


def normalize_hashtag(raw) -> str | None:
    """'#Аниме', 'аниме' and ' #аниме ' are all '#аниме'; None when it is not a hashtag.

    What Telegram itself makes tappable: '#' and then letters of any alphabet, digits and
    '_'. Lower-cased because matching is case-insensitive anyway, and one spelling per tag
    is what lets a tag name its own poll and its own theme."""
    text = unicodedata.normalize("NFKC", str(raw or "")).strip()
    if not text:
        return None
    if not text.startswith("#"):
        text = "#" + text
    text = text.lower()
    if len(text) > HASHTAG_MAX_LENGTH or not re.fullmatch(r"#\w+", text):
        return None
    return text


def is_weekly_hashtag(hashtag) -> bool:
    return (normalize_hashtag(hashtag) or CONTEST_HASHTAG) == CONTEST_HASHTAG


def hashtag_slug(hashtag) -> str:
    """'' for the weekly hashtag, six hex characters for any other.

    The slug goes into a poll id, and a poll id goes into file names and photo URLs, which
    allow only [A-Za-z0-9_.-] -- so a hash of the tag rather than the tag itself. A hash and
    not a counter: the same tag always lands in the same poll, with nothing to look up."""
    tag = normalize_hashtag(hashtag) or CONTEST_HASHTAG
    if tag == CONTEST_HASHTAG:
        return ""
    return hashlib.sha256(tag.encode("utf-8")).hexdigest()[:6]


def poll_id_for(week_id: str, hashtag=CONTEST_HASHTAG) -> str:
    """The poll a collect of `hashtag` in `week_id` writes to. The weekly hashtag keeps the
    bare week ("2026-W41") every poll before this feature has, so nothing on disk moves."""
    slug = hashtag_slug(hashtag)
    return f"{week_id}-{slug}" if slug else week_id


def contest_title(hashtag, title: str = "") -> str:
    """What a contest is called: its own title, "Итоги недели" for the weekly one, and the
    bare hashtag for a thematic contest nobody named."""
    if title:
        return title
    if is_weekly_hashtag(hashtag):
        return WEEKLY_CONTEST_TITLE
    return normalize_hashtag(hashtag) or str(hashtag or "")


def parse_theme_text(text: str) -> tuple[str, str, str] | None:
    """'#аниме 🌸 Лучший аниме-покрас' -> ('#аниме', 'Лучший аниме-покрас', '🌸').

    The first word is the hashtag; an optional word with no letters or digits in it is the
    winner's badge; the rest is the theme's title. Both may be left out. The '#' may be
    left out too, but only when the tag is all there is -- 'просто текст' is a sentence,
    not the tag #просто titled 'текст'. None when it is not a hashtag."""
    words = (text or "").split()
    if not words or (len(words) > 1 and not words[0].startswith("#")):
        return None
    hashtag = normalize_hashtag(words[0])
    if hashtag is None:
        return None
    rest = words[1:]
    badge = ""
    if rest and len(rest[0]) <= THEME_BADGE_MAX_LENGTH and not any(ch.isalnum() for ch in rest[0]):
        badge, rest = rest[0], rest[1:]
    title = " ".join(rest)[:THEME_TITLE_MAX_LENGTH].strip()
    return hashtag, title, badge


@dataclass
class Theme:
    """A hashtag /vote has been asked to collect, with what its contest is called and the
    badge its winner earns. Remembered so the next collect can offer it as a button."""

    hashtag: str
    title: str = ""
    badge: str = ""
    used_at: str = ""

    @property
    def slug(self) -> str:
        return hashtag_slug(self.hashtag)

    def label(self) -> str:
        return contest_title(self.hashtag, self.title)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "Theme | None":
        hashtag = normalize_hashtag((raw or {}).get("hashtag"))
        if hashtag is None:
            return None
        return cls(
            hashtag=hashtag,
            title=str(raw.get("title") or "")[:THEME_TITLE_MAX_LENGTH],
            badge=str(raw.get("badge") or "")[:THEME_BADGE_MAX_LENGTH],
            used_at=str(raw.get("used_at") or ""),
        )


# remember_theme is a read-modify-write of one small file. Its callers are admin commands,
# which run on the event loop, but it is cheap enough to also be safe from a worker thread.
_themes_lock = threading.Lock()


def themes_path(entry: str) -> Path:
    """One file per chat, in a subdirectory: latest_poll globs "<key>_*.json" straight out
    of the voting directory, and a themes file there would be read as a poll."""
    return _voting_dir() / "themes" / f"{_poll_key(entry)}.json"


def load_themes(entry: str) -> list[Theme]:
    """Every remembered theme, most recently used first. The weekly hashtag is never
    stored here -- it is always available and has nothing to remember."""
    path = themes_path(entry)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    themes = [Theme.from_dict(item) for item in (raw.get("themes") or []) if isinstance(item, dict)]
    themes = [theme for theme in themes if theme is not None and not is_weekly_hashtag(theme.hashtag)]
    return sorted(themes, key=lambda theme: theme.used_at, reverse=True)


def find_theme(entry: str, slug_or_hashtag: str) -> Theme | None:
    """A remembered theme by its slug or its hashtag; the weekly theme for '' or the
    weekly hashtag; None for anything unknown."""
    key = str(slug_or_hashtag or "").strip()
    tag = normalize_hashtag(key) if key.startswith("#") else None
    if key in ("", "-") or tag == CONTEST_HASHTAG:
        return Theme(hashtag=CONTEST_HASHTAG)
    for theme in load_themes(entry):
        if theme.slug == key or theme.hashtag == tag:
            return theme
    return None


def theme_for(entry: str, hashtag) -> Theme:
    """The remembered theme for `hashtag`, or a bare one if it was never remembered."""
    tag = normalize_hashtag(hashtag) or CONTEST_HASHTAG
    return find_theme(entry, tag) or Theme(hashtag=tag)


def remember_theme(entry: str, hashtag, title: str | None = None, badge: str | None = None) -> Theme:
    """Records that `hashtag` is being collected, and returns its theme. A title or badge
    of None keeps whatever was remembered before, so re-picking a theme from its button
    does not wipe the name it was given when it was typed. The weekly hashtag is returned
    as it is and never written."""
    tag = normalize_hashtag(hashtag) or CONTEST_HASHTAG
    if tag == CONTEST_HASHTAG:
        return Theme(hashtag=CONTEST_HASHTAG)
    with _themes_lock:
        themes = load_themes(entry)
        theme = next((t for t in themes if t.hashtag == tag), None)
        if theme is None:
            theme = Theme(hashtag=tag)
            themes.append(theme)
        if title is not None:
            theme.title = title[:THEME_TITLE_MAX_LENGTH].strip()
        if badge is not None:
            theme.badge = badge[:THEME_BADGE_MAX_LENGTH].strip()
        theme.used_at = datetime.now(timezone.utc).isoformat()
        path = themes_path(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps({"themes": [t.to_dict() for t in themes]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
    return theme


@dataclass
class Entry:
    """One nominated post: its author, its text, and every photo attached to it."""

    entry_id: str          # the album's first message id, as a string
    message_id: int
    author_id: int | None
    author_name: str
    author_username: str | None
    text: str
    media: list[str] = field(default_factory=list)  # file names under the poll's media dir
    posted_at: str = ""    # ISO 8601, local time

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "Entry":
        return cls(
            entry_id=str(raw.get("entry_id")),
            message_id=int(raw.get("message_id") or 0),
            author_id=raw.get("author_id"),
            author_name=raw.get("author_name") or "Unknown",
            author_username=raw.get("author_username"),
            text=raw.get("text") or "",
            media=list(raw.get("media") or []),
            posted_at=raw.get("posted_at") or "",
        )


# --------------------------------------------------------------------------- collection


def group_into_entries(messages: list, hashtag: str = CONTEST_HASHTAG) -> list[list]:
    """Groups raw Telethon messages into nominated posts, newest post first.

    Messages sharing a grouped_id are one post. A group qualifies if ANY of its messages
    carries the hashtag, since in an album only the captioned one does. Returned groups
    are each sorted by message id, so the first item is the one whose photo the caption
    belongs to -- that is the photo shown large.
    """
    groups: dict[object, list] = {}
    for message in messages:
        key = message.grouped_id if getattr(message, "grouped_id", None) else ("single", message.id)
        groups.setdefault(key, []).append(message)

    nominated = []
    for group in groups.values():
        group.sort(key=lambda m: m.id)
        if any(_has_hashtag(getattr(m, "text", "") or "", hashtag) for m in group):
            nominated.append(group[:MAX_ALBUM_ITEMS])
    # Newest post first: the most recent nomination is the one people are looking for.
    nominated.sort(key=lambda g: g[0].id, reverse=True)
    return nominated


def _clean_caption(group: list, hashtag: str = CONTEST_HASHTAG) -> str:
    """The post's own words, with the nominating hashtag itself taken out -- it is how the
    post got here, not something the voter needs to read on every card."""
    texts = [(getattr(m, "text", "") or "").strip() for m in group]
    text = next((t for t in texts if t), "")
    text = re.sub(rf"(?<!\w){re.escape(hashtag)}(?!\w)", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


async def collect_entries(
    client,
    chat_ref,
    tz,
    media_dir: Path,
    hashtag: str = CONTEST_HASHTAG,
    skip_entry_ids=frozenset(),
    weeks: int = 1,
    stop_at_known: bool = True,
    progress=None,
    log=print,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[Entry]:
    """Reads the last `weeks` CONTEST WEEKS of `chat_ref` and returns one Entry per NEWLY
    found nominated post, downloading every attached photo into `media_dir`.

    `since`/`until` (timezone-aware, `until` exclusive) replace the week window with dates
    an administrator picked: the scan then starts at `until` rather than at the newest
    message, and stops at `since`. See bot_listener's date picker for "/vote собрать".

    `weeks` counts calendar weeks ending with the one in progress: 1 is Monday 00:00 local
    through now, 2 reaches back to the Monday before that. /vote collects two -- the vote
    is run around the turn of the week, so on a Monday the week in progress is a few hours
    old and every work worth voting on sits in the week just ended, while on a Sunday it is
    the other way round. Both weeks in one pass means the moderator never has to know which
    of the two a work was posted in; they see all of it and admit what belongs.

    The window is whole weeks rather than a rolling span of days on purpose: a rolling
    window's reach depends on the day it is run, so collecting a day late would quietly
    drop the oldest day's works instead of finding the same set.

    `skip_entry_ids` -- entry ids (message ids, as strings) already known from a previous
    collection -- are never resolved further: no get_sender() round trip, no photo
    download, no Entry built. With `stop_at_known` (the default) THE SCAN ALSO STOPS at the
    first one it meets. The listing is newest-first, so everything past a work that was
    already collected was collected too -- as long as the earlier collection read the same
    window. A re-collect that only wants today's additions has no reason to read back to
    Monday. A first collection (no skip ids) reads the whole window either way.

    That "as long as" is why stopping is optional. It is wrong whenever the earlier
    collection read LESS than this one: a poll collected for the week in progress holds
    only this week's works, so a two-week collect that stops at the newest of them never
    reaches the week before -- which is exactly how the first two-week collect in
    production (2026-09-27) found two works from this week and none from last. It also
    misses a post that gained the hashtag after it was first passed over (edited days
    later). `stop_at_known=False` reads the whole window regardless and still skips the
    known works, so it costs only the scan, never a second download.

    The caller is responsible for keeping those already-known entries around (see
    bot_listener.handle_vote_command) -- this function only ever reports what's new.

    Uses the Telethon session directly rather than telegram_fetch's cache: that cache
    stores plain text dicts, and this needs the media and the grouped_id, neither of which
    survives that conversion.
    """
    from telegram_fetch import resolve_chat, sender_display_name

    entity = chat_ref if not isinstance(chat_ref, str) else await resolve_chat(client, chat_ref)

    now_local = datetime.now(tz)
    if since is not None:
        start_local = since
    else:
        # Shift "now" back whole weeks and take THAT week's Monday, rather than subtracting
        # days from the current Monday -- the two agree, and this one keeps working when
        # the shift crosses a DST change or a year boundary.
        start_local = contest_week_start(now_local - timedelta(weeks=max(1, weeks) - 1))
    start_utc = start_local.astimezone(timezone.utc)
    until_utc = until.astimezone(timezone.utc) if until is not None else None
    # Telegram lists newest first, so a range that ends in the past starts the listing at
    # its end (offset_date) instead of reading everything posted since only to discard it.
    listing = {"reverse": False}
    if until_utc is not None:
        listing["offset_date"] = until_utc

    async def report(stage: str, done: int, total: int) -> None:
        """Tell the caller how far along this is, without letting that stop the scan.

        A whole week of a busy chat is thousands of messages and every nomination's photos
        on top, which takes minutes -- long enough that a silent bot reads as a hung one.
        """
        if progress is None:
            return
        try:
            await progress(stage, done, total)
        except Exception as e:
            log(f"[voting] progress report failed: {e}")

    messages = []
    stopped_at_known = False
    scanned = 0
    await report("scan", 0, 0)
    async for message in client.iter_messages(entity, **listing):
        scanned += 1
        # Counted on every message read, not on every message kept: two weeks of a busy
        # chat is thousands of messages, most of them not nominations, and progress that
        # sat still for that whole stretch reads as a hung collection -- which is what the
        # reporter is for.
        if scanned % 250 == 0:
            await report("scan", scanned, 0)
        if message.date < start_utc:
            break
        if until_utc is not None and message.date >= until_utc:
            continue  # offset_date already skips these; this holds even where it doesn't
        if message.action is not None:
            continue  # service message (join/leave/pin)
        messages.append(message)
        # Reaching a nomination that was already collected means everything below it was
        # too: the listing is newest-first, so a re-collect only has to walk back as far
        # as the first thing it recognises. Without this, adding one late entry re-read
        # the whole window every time. Only when asked -- see stop_at_known.
        #
        # The known message is kept rather than dropped, and the break happens after
        # appending it: in an album the entry id is the FIRST message's, which arrives
        # last here, so stopping before it would leave the album's other messages behind
        # as a headless group -- which reads as a brand-new nomination and gets collected
        # a second time.
        if stop_at_known and skip_entry_ids and str(message.id) in skip_entry_ids:
            stopped_at_known = True
            break

    groups = group_into_entries(messages, hashtag)
    window = f"since {start_local.date()}" + (f" until {until.date()}" if until is not None else "")
    log(
        f"[voting] {len(messages)} message(s) of {scanned} read "
        f"{'back to the first already-collected work' if stopped_at_known else window}"
        f" -> {len(groups)} nomination(s)"
    )

    media_dir.mkdir(parents=True, exist_ok=True)
    entries: list[Entry] = []
    skipped = 0
    for position, group in enumerate(groups, start=1):
        await report("download", position, len(groups))
        head = group[0]
        if str(head.id) in skip_entry_ids:
            skipped += 1
            continue
        sender = await head.get_sender()
        files: list[str] = []
        for index, message in enumerate(group):
            if not message.photo:
                continue  # a video/document in the album is not shown on the page
            name = f"{head.id}_{index}.jpg"
            path = media_dir / name
            if not path.exists():
                try:
                    await client.download_media(message, file=str(path))
                except Exception as e:  # one unreadable photo must not lose the whole entry
                    log(f"[voting] could not download photo {message.id}: {e}")
                    continue
            files.append(name)

        if not files:
            continue  # a nomination with no picture has nothing to vote on

        entries.append(
            Entry(
                entry_id=str(head.id),
                message_id=head.id,
                author_id=getattr(sender, "id", None),
                author_name=sender_display_name(sender),
                author_username=getattr(sender, "username", None),
                text=_clean_caption(group, hashtag),
                media=files,
                posted_at=head.date.astimezone(tz).isoformat(),
            )
        )
    log(f"[voting] {len(entries)} new entr{'y' if len(entries) == 1 else 'ies'}, {skipped} already known -- skipped")
    return entries


# ------------------------------------------------------------------------------ the poll


def _voting_dir() -> Path:
    """Indirection purely for tests (patch this, not the module-level constant) --
    matches stats._stats_dir's convention."""
    return VOTING_DIR


def _poll_key(entry: str) -> str:
    """Filesystem-safe stable key for a chat name, matching stats._cache_key's intent."""
    normalized = unicodedata.normalize("NFKC", entry or "").strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def poll_path(entry: str, poll_id: str) -> Path:
    return _voting_dir() / f"{_poll_key(entry)}_{poll_id}.json"


def media_path(entry: str, poll_id: str) -> Path:
    return _voting_dir() / "media" / f"{_poll_key(entry)}_{poll_id}"


def _clean_crops(raw) -> dict[str, dict]:
    """Whatever was in the JSON, reduced to the crops that are actually usable: three
    finite numbers with a positive size, keyed by entry id. Anything else is dropped
    rather than raising -- a nonsense crop must cost that one work its framing, not make
    the whole poll unloadable (same tolerance load_poll has for the file as a whole)."""
    crops: dict[str, dict] = {}
    for entry_id, crop in (raw or {}).items():
        if not isinstance(crop, dict):
            continue
        try:
            x, y, size = float(crop["x"]), float(crop["y"]), float(crop["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if not all(map(math.isfinite, (x, y, size))) or size <= 0:
            continue
        crops[str(entry_id)] = {"x": x, "y": y, "size": size}
    return crops


def set_crops(poll: "Poll", crops: dict) -> "Poll":
    """Replaces the framing wholesale -- the cropping page always submits every card it is
    showing, so this cannot drift from what the editor saw. Crops for entries that are no
    longer in the poll are dropped, same rule set_approved follows."""
    known = {e.entry_id for e in poll.entries}
    poll.crops = {k: v for k, v in _clean_crops(crops).items() if k in known}
    return poll


def export_dir() -> Path:
    """Where rendered board pictures live. Its own directory for the same reason
    results_path has one: latest_poll globs "<key>_*.json" out of the voting dir itself,
    and anything sharing that name shape belongs somewhere else."""
    return _voting_dir() / "exports"


def export_image_path(entry: str, poll_id: str, columns: int = 3) -> Path:
    """Where the rendered board picture (vote_image.py) is saved -- same
    `<poll key>_<poll id>` naming as poll_path.

    A non-default column count gets its own file rather than overwriting: exporting the
    week four-across and then three-across are two different pictures somebody may well
    want both of, and one of them silently replacing the other is the kind of thing you
    only notice after sending the wrong one. (3 is vote_image.COLUMNS, hardcoded here
    because importing vote_image from this module would be a cycle -- vote_image imports
    voting.)
    """
    variant = "" if columns == 3 else f"_c{columns}"
    return export_dir() / f"{_poll_key(entry)}_{poll_id}{variant}.jpg"


@dataclass
class Poll:
    poll_id: str
    entry: str                                   # the LISTENER_ALLOWED_CHATS entry it belongs to
    created_at: str
    entries: list[Entry] = field(default_factory=list)
    # Moderation. `approved` is what voters see. It starts EMPTY rather than holding
    # everything: admitting is a deliberate act, so a poll nobody has moderated yet shows
    # voters nothing instead of showing them posts an administrator has not looked at.
    approved: list[str] = field(default_factory=list)
    # user_id (as a string, since JSON keys are strings) -> list of entry_ids.
    votes: dict[str, list[str]] = field(default_factory=dict)
    # Whether the voter was subscribed when their latest ballot was accepted.  Keep the
    # snapshot instead of inferring it later: the weekly figures need historical status.
    subscriber_votes: dict[str, bool] = field(default_factory=dict)
    open: bool = True
    # Set once by close_and_announce, kept alongside the poll so a reloaded page (or a
    # second look days later) can still show who won without recomputing it from votes
    # that may since have shifted (an un-admit after closing, say).
    winner_entry_id: str | None = None
    # Admin-configurable, set from the moderation screen. None means unlimited -- a voter
    # may admit as many approved entries as they like, the original behavior.
    max_choices: int | None = None
    # Whether re-submitting a ballot replaces the previous one. False locks a voter's
    # FIRST ballot in permanently -- enforced by vote_web.handle_ballot, not here (see its
    # docstring): this field is just the setting, not the enforcement.
    allow_revote: bool = True
    # entry_id -> {"x": float, "y": float, "size": float}: how that entry's FIRST photo is
    # framed in the exported board picture (vote_image.py), set on the cropping page. A
    # square in the photo's own pixel coordinates, taken AFTER the EXIF rotation both the
    # browser and Pillow apply, so the page and the render mean the same square.
    #
    # It may hang off the edge of the photo (negative x/y, or a size past the photo's own):
    # that is how "fit the whole thing, letterboxed" is expressed as a crop rather than as
    # a separate mode -- one representation for both, so the renderer has one path.
    # An entry with no entry here is drawn fitted, exactly as before any cropping existed.
    crops: dict[str, dict] = field(default_factory=dict)
    # The hashtag this poll's works were collected by. Every poll written before thematic
    # contests existed collected #итогинедели, which is what a missing value means.
    hashtag: str = CONTEST_HASHTAG
    # A thematic contest's name and its winner's badge, copied from the theme when the poll
    # is collected so the poll (and the Hall of Fame record made from it) says what it was
    # without a lookup. Empty for the weekly contest.
    title: str = ""
    badge: str = ""

    @property
    def is_weekly(self) -> bool:
        return is_weekly_hashtag(self.hashtag)

    def label(self) -> str:
        """"Итоги недели", or the thematic contest's title (its hashtag if untitled)."""
        return contest_title(self.hashtag, self.title)

    def to_dict(self) -> dict:
        return {
            "poll_id": self.poll_id,
            "entry": self.entry,
            "created_at": self.created_at,
            "entries": [e.to_dict() for e in self.entries],
            "approved": list(self.approved),
            "votes": {k: list(v) for k, v in self.votes.items()},
            "subscriber_votes": {k: bool(v) for k, v in self.subscriber_votes.items()},
            "open": self.open,
            "winner_entry_id": self.winner_entry_id,
            "max_choices": self.max_choices,
            "allow_revote": self.allow_revote,
            "crops": {k: dict(v) for k, v in self.crops.items()},
            "hashtag": self.hashtag,
            "title": self.title,
            "badge": self.badge,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Poll":
        votes = {str(k): [str(e) for e in v] for k, v in (raw.get("votes") or {}).items()}
        raw_subscribers = raw.get("subscriber_votes")
        # Before non-subscribers were allowed to vote, the web handler rejected them.
        # Therefore every old recorded ballot was necessarily from a subscriber at the
        # time; preserve that fact when adding the new snapshot field retroactively.
        subscriber_votes = (
            {str(k): bool(v) for k, v in raw_subscribers.items()}
            if isinstance(raw_subscribers, dict)
            else {voter_id: True for voter_id in votes}
        )
        return cls(
            poll_id=str(raw.get("poll_id") or ""),
            entry=raw.get("entry") or "",
            created_at=raw.get("created_at") or "",
            entries=[Entry.from_dict(e) for e in raw.get("entries") or []],
            approved=[str(e) for e in raw.get("approved") or []],
            votes=votes,
            subscriber_votes=subscriber_votes,
            open=bool(raw.get("open", True)),
            winner_entry_id=(str(raw["winner_entry_id"]) if raw.get("winner_entry_id") else None),
            max_choices=(int(raw["max_choices"]) if raw.get("max_choices") else None),
            allow_revote=bool(raw.get("allow_revote", True)),
            crops=_clean_crops(raw.get("crops")),
            hashtag=normalize_hashtag(raw.get("hashtag")) or CONTEST_HASHTAG,
            title=str(raw.get("title") or "")[:THEME_TITLE_MAX_LENGTH],
            badge=str(raw.get("badge") or "")[:THEME_BADGE_MAX_LENGTH],
        )

    def approved_entries(self) -> list[Entry]:
        allowed = set(self.approved)
        return [e for e in self.entries if e.entry_id in allowed]

    def tally(self) -> list[tuple[Entry, int]]:
        """Approved entries with their vote counts, most votes first. Votes for an entry
        that was later un-admitted are ignored rather than counted for nobody."""
        counts: dict[str, int] = {}
        allowed = set(self.approved)
        for choices in self.votes.values():
            for entry_id in choices:
                if entry_id in allowed:
                    counts[entry_id] = counts.get(entry_id, 0) + 1
        ranked = [(e, counts.get(e.entry_id, 0)) for e in self.approved_entries()]
        ranked.sort(key=lambda pair: (-pair[1], pair[0].entry_id))
        return ranked

    def winner(self) -> Entry | None:
        """The entry recorded by close_and_announce, or None if nothing has been
        announced yet -- looked up fresh each time rather than cached as an Entry, since
        the poll's own entries list is the single source of truth for entry data."""
        if not self.winner_entry_id:
            return None
        return next((e for e in self.entries if e.entry_id == self.winner_entry_id), None)


def close_and_announce(poll: Poll) -> tuple[Entry, int] | None:
    """Closes voting and records the winner: the top of `tally()`, provided it actually
    has at least one vote. Returns (entry, vote_count), or None -- and leaves the poll
    untouched -- if there is nothing to announce (no admitted entries yet, or admitted
    entries that nobody has voted for). Idempotent: announcing an already-closed poll
    just recomputes and re-records the same winner rather than refusing."""
    ranked = poll.tally()
    if not ranked or ranked[0][1] <= 0:
        return None
    winner_entry, votes = ranked[0]
    poll.open = False
    poll.winner_entry_id = winner_entry.entry_id
    return winner_entry, votes


# Serialises every read-modify-write of a poll. The voting page's handlers run
# concurrently on one event loop against one file, and each ballot does load -> mutate ->
# save with an await (the membership check) in the middle. Without this, two people voting
# at the same moment can each load the poll, add their own choice and save, and whoever
# writes second silently erases the other's ballot. Held around the whole load/mutate/save,
# not just the save: locking only the write would still let the second writer save state
# built from a stale read. Voting traffic is a handful of requests a second at most, so the
# contention this costs is irrelevant next to losing a vote.
poll_lock = asyncio.Lock()


def save_poll(poll: Poll) -> None:
    path = poll_path(poll.entry, poll.poll_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written to a sibling then moved: a redeploy or crash mid-write would otherwise
    # leave a truncated file, and unlike a cache this cannot simply be re-fetched.
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(poll.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def delete_poll(entry: str, poll_id: str) -> bool:
    """Deletes a poll's JSON file and its downloaded media outright, rather than just
    resetting its fields in place -- "start over" means the next /vote собрать builds a
    genuinely fresh poll (its own created_at), not a same-poll reset that would still
    carry the old identity. Returns whether there was anything to delete."""
    path = poll_path(entry, poll_id)
    existed = path.exists()
    if existed:
        path.unlink()
    media_dir = media_path(entry, poll_id)
    if media_dir.exists():
        shutil.rmtree(media_dir)
    return existed


def poll_ids(entry: str) -> list[str]:
    """Every poll id this chat has on disk, oldest id first.

    Read from the filenames rather than by parsing each poll, so a week whose JSON no
    longer loads is still listed -- clearing and archiving both need to see it.
    """
    directory = _voting_dir()
    if not directory.exists():
        return []
    prefix = f"{_poll_key(entry)}_"
    return sorted(path.stem[len(prefix):] for path in directory.glob(f"{prefix}*.json"))


def archive_dir() -> Path:
    """Where cleared polls are kept. A SUBDIRECTORY on purpose: _all_polls globs the
    voting directory itself and does not recurse, so an archived week is invisible to
    latest_poll and the page while its file still exists."""
    return _voting_dir() / "archive"


def archive_all_polls(entry: str) -> int:
    """Clears the contest: every poll leaves the live set, and its photos are deleted.

    "Очистить" means the contest starts over, so it cannot leave last week's poll behind
    to become `latest_poll` the moment this week's is gone -- clearing once and finding
    the previous week in its place is indistinguishable from the clear not having worked.
    Returns how many polls were cleared.

    NOTHING RECORDED IS DESTROYED. The poll file is MOVED into archive_dir() rather than
    unlinked, and the announced results (results_path) and rendered boards
    (export_image_path) are left where they are -- clearing is "let me collect a new
    vote", never "erase what the contest has already decided". Only the collected photos
    go, because they are the bulk on disk and the boards have already been rendered from
    them (see bot_listener._archive_vote_boards).
    """
    cleared = 0
    destination_dir = archive_dir()
    for poll_id in poll_ids(entry):
        path = poll_path(entry, poll_id)
        destination = destination_dir / path.name
        try:
            destination_dir.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                # Cleared twice with a re-collect in between: keep both rather than let
                # the second clear silently overwrite the first week's record.
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
                destination = destination_dir / f"{path.stem}_{stamp}{path.suffix}"
            path.replace(destination)
            cleared += 1
        except OSError:
            continue
        media_directory = media_path(entry, poll_id)
        if media_directory.exists():
            shutil.rmtree(media_directory, ignore_errors=True)
    return cleared


def load_poll(entry: str, poll_id: str) -> Poll | None:
    path = poll_path(entry, poll_id)
    if not path.exists():
        return None
    try:
        return Poll.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None


def _all_polls(entry: str) -> list[Poll]:
    """Every readable poll for this chat, newest first. An unreadable file is skipped
    rather than raising -- same tolerance load_poll has, for the same reason: one corrupt
    week must not make the current one unopenable."""
    directory = _voting_dir()
    if not directory.exists():
        return []
    prefix = f"{_poll_key(entry)}_"
    polls = []
    for path in directory.glob(f"{prefix}*.json"):
        try:
            polls.append(Poll.from_dict(json.loads(path.read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            continue
    polls.sort(key=lambda p: p.created_at, reverse=True)
    return polls


def _ballot_rank(poll: Poll) -> int:
    """How much of a ballot a poll is -- 0 is the most, and wins. See latest_poll."""
    if poll.approved and poll.open:
        return 0  # works admitted: people can vote in this one, and may be doing so now
    if poll.entries:
        # Collected, nothing admitted yet: waiting on a moderator. Or CLOSED: the vote is
        # over and only its result is left to look at. The two share a rank, so the newer
        # wins -- a contest collected after a vote closed is the one being worked on now,
        # and a closed vote still shows its result until something newer is collected.
        return 1
    return 2      # empty: a week nobody nominated anything in


def latest_poll(entry: str) -> Poll | None:
    """What /vote and the page open: the most recent poll that is actually a BALLOT --
    one with admitted works -- falling back to the most recent collected-but-unmoderated
    week, and only then to the most recent poll outright.

    Recency alone is not enough, and both weaker weeks get written routinely. An EMPTY
    poll is saved even for a week nobody nominated anything in, which on a Monday is the
    normal outcome for the week just begun. And a week that has been collected but not yet
    moderated has no ballot in it either: every work in it is pending, so opening it shows
    a voter nothing to vote for.

    Either would otherwise be the newest file on disk and would take the page away from
    the week people are voting in -- which is exactly what happened in production on
    2026-08-10: last week's poll was open with 15 admitted works and 34 ballots cast, a
    routine "собрать за эту неделю" found one new nomination for the week in progress, and
    that one pending work moved the ballot to a poll with nothing admitted in it. Nobody
    lost a vote (they are all in their own week's file), but the ballot showed no
    candidates until this ordering was fixed.

    A CLOSED vote is no longer a ballot. It keeps the page while it is the newest thing
    there, so voters can still see its result, but a contest collected after it -- next
    week's, or a thematic contest's own hashtag -- takes over without anybody having to
    clear the finished one first (clearing archives every poll, the new one included).
    """
    polls = _all_polls(entry)
    if not polls:
        return None
    # min() returns the FIRST of an equally-ranked group and _all_polls is newest-first, so
    # the tie-break within a rank stays "most recently made current" (see make_current).
    return min(polls, key=_ballot_rank)


def make_current(poll: Poll) -> Poll:
    """Marks `poll` as the newest, which is how latest_poll breaks a tie in rank.

    `created_at` is what polls are ordered on, and "newest created" only means "the week
    being worked on" while weeks are collected in order. A re-collect into a poll created
    days ago breaks that -- another week's poll written in between would outrank it -- and
    so did the old "collect the previous week" button, whose polls may still be on disk.

    This only settles a tie. It cannot promote a week PAST a live ballot: a poll with no
    admitted works ranks below one that has them however recently it was collected (see
    _ballot_rank), so collecting mid-vote can no longer take the page away from the week
    people are voting in.
    """
    poll.created_at = datetime.now(timezone.utc).isoformat()
    return poll


def build_poll(
    entry: str, poll_id: str, entries: list[Entry], existing: Poll | None = None,
    theme: Theme | None = None,
) -> Poll:
    """A poll for `entries`, carrying over the moderation and votes of `existing`.

    Re-collecting is how an administrator picks up nominations posted since the last run,
    so it must not undo the admitting they have already done or throw away votes already
    cast. Anything that is no longer among the entries drops out of both -- a deleted post
    cannot stay admitted or keep its votes.

    `theme` is what the collect was for -- its hashtag, title and badge are stamped on the
    poll. Without one the poll keeps `existing`'s, or is the weekly contest.
    """
    now = datetime.now(timezone.utc).isoformat()
    poll = Poll(
        poll_id=poll_id,
        entry=entry,
        created_at=(existing.created_at if existing else now),
        entries=entries,
    )
    source = theme or existing
    if source is not None:
        poll.hashtag = normalize_hashtag(source.hashtag) or CONTEST_HASHTAG
        poll.title, poll.badge = source.title, source.badge
    if existing is None:
        return poll

    known = {e.entry_id for e in entries}
    poll.approved = [entry_id for entry_id in existing.approved if entry_id in known]
    poll.votes = {
        user_id: [e for e in choices if e in known]
        for user_id, choices in existing.votes.items()
    }
    poll.subscriber_votes = {
        user_id: subscribed
        for user_id, subscribed in existing.subscriber_votes.items()
        if user_id in poll.votes
    }
    poll.open = existing.open
    if existing.winner_entry_id in known:
        poll.winner_entry_id = existing.winner_entry_id
    poll.max_choices = existing.max_choices
    poll.allow_revote = existing.allow_revote
    # Framing survives a re-collect for the same reason admitting does: it is work the
    # administrator did by hand, and a poll refreshed to pick up two new nominations must
    # not silently un-crop the dozen that were already framed.
    poll.crops = {k: dict(v) for k, v in existing.crops.items() if k in known}
    return poll


def posted_within(entry: Entry, since: datetime, until: datetime) -> bool:
    """Whether `entry` was posted in [since, until) -- the dates a collect was given.

    A work whose posting time cannot be read is counted as inside: a collect that narrows
    the window removes what it can SHOW lies outside it, and never a work on a guess."""
    try:
        posted = datetime.fromisoformat(entry.posted_at)
    except (TypeError, ValueError):
        return True
    if posted.tzinfo is None:
        return True
    return since <= posted < until


# Works are not carried over from another poll. A poll contains exactly what the chat
# scan found in its collection window (the previous and the current contest week, see
# collect_entries), so the only way a work appears in a vote is that somebody posted it
# with the hashtag in that window -- nothing is copied out of an older poll's file. The
# carry-over that used to re-seed a new poll with last week's runners-up was removed.


def set_approved(poll: Poll, entry_ids: list[str]) -> Poll:
    """Replaces the admitted set wholesale -- the moderation screen always submits the
    complete picture, so this cannot drift from what the administrator saw."""
    known = {e.entry_id for e in poll.entries}
    poll.approved = [entry_id for entry_id in dict.fromkeys(entry_ids) if entry_id in known]
    return poll


def record_vote(
    poll: Poll, user_id: int | str, entry_ids: list[str], subscriber: bool | None = None,
) -> Poll:
    """One ballot per user, replacing whatever they chose before -- voting again is
    changing your mind, not stuffing the box. Choices outside the admitted set are dropped
    rather than rejecting the whole ballot, so a page left open across a moderation change
    still records the choices that are still valid."""
    allowed = set(poll.approved)
    voter_id = str(user_id)
    poll.votes[voter_id] = [e for e in dict.fromkeys(entry_ids) if e in allowed]
    if subscriber is not None:
        poll.subscriber_votes[voter_id] = bool(subscriber)
    return poll


# ------------------------------------------------------------- voting by a link to the bot
#
# The browser page (vote_web.BROWSER_HTML) is for people whose Telegram will not open the
# Mini App. A web page cannot tell who is looking at it, so it does not vote: it builds a
# t.me/<bot>?start=<payload> link carrying the chosen works, and the ballot is cast by the
# Telegram account that sends that /start to the bot -- the same identity the Mini App's
# signed initData gives, so one person still has one ballot.
#
# The payload is "vote-" and each entry id (a message id) in base 36, dash-separated:
# start parameters allow only [A-Za-z0-9_-], are capped at 64 characters, and bot_listener
# lowercases them -- base 36 is lowercase and about two thirds the length of decimal.
BALLOT_LINK_PREFIX = "vote-"
BALLOT_LINK_MAX = 64


def encode_ballot_link(entry_ids: list[str]) -> str:
    """The start payload for these choices. Mirrored by ballotPayload() in the page's
    script -- the two are pinned against each other by a test."""
    return BALLOT_LINK_PREFIX + "-".join(_base36(int(entry_id)) for entry_id in entry_ids)


def _base36(number: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    text = ""
    while True:
        number, remainder = divmod(number, 36)
        text = digits[remainder] + text
        if not number:
            return text


def decode_ballot_link(payload: str) -> list[str] | None:
    """Entry ids from a start payload, in order and without repeats; None if it is not a
    ballot link or any part of it is not a base-36 number."""
    payload = (payload or "").strip().lower()
    if not payload.startswith(BALLOT_LINK_PREFIX):
        return None
    parts = payload[len(BALLOT_LINK_PREFIX):].split("-")
    if not parts or not all(re.fullmatch(r"[0-9a-z]{1,12}", part) for part in parts):
        return None
    return list(dict.fromkeys(str(int(part, 36)) for part in parts))


def weekly_vote_records(entry: str) -> list[dict]:
    """Private voter-id sets for the weekly vote-report renderer, oldest first.

    Clearing a vote archives its JSON, so the reporting view reads both the live and
    archived files. Polls created before the subscription prompt are restored as
    subscriber ballots because the old API rejected anybody else.
    """
    directory = _voting_dir()
    if not directory.exists():
        return []
    prefix = f"{_poll_key(entry)}_"
    paths = list(directory.glob(f"{prefix}*.json"))
    archive = archive_dir()
    if archive.exists():
        paths.extend(archive.glob(f"{prefix}*.json"))

    weeks: list[dict] = []
    for path in paths:
        try:
            poll = Poll.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            continue
        voter_ids = set(poll.votes)
        weeks.append({
            "week": poll.poll_id,
            "voter_ids": voter_ids,
            "subscriber_ids": {
                voter_id for voter_id in voter_ids if poll.subscriber_votes.get(voter_id) is True
            },
            "non_subscriber_ids": {
                voter_id for voter_id in voter_ids if poll.subscriber_votes.get(voter_id) is False
            },
        })
    return sorted(weeks, key=lambda row: row["week"])


def weekly_vote_stats(
    entry: str, current_subscriber_ids: set[str] | None = None,
    checked_voter_ids: set[str] | None = None, records: list[dict] | None = None,
) -> list[dict]:
    """Public weekly vote totals, without exposing voter identities.

    ``subscribers`` is the subscription snapshot at vote time. ``subscribed_after`` and
    ``not_subscribed`` use a fresh channel-membership lookup only for people who voted
    while not subscribed, so they show conversion without reclassifying past ballots.
    """
    records = weekly_vote_records(entry) if records is None else records
    weeks = []
    for record in records:
        non_subscriber_ids = record["non_subscriber_ids"]
        if current_subscriber_ids is None:
            subscribed_after = 0
            not_subscribed = len(non_subscriber_ids)
        else:
            subscribed_after = len(non_subscriber_ids & current_subscriber_ids)
            checked_ids = non_subscriber_ids if checked_voter_ids is None else checked_voter_ids
            not_subscribed = len((non_subscriber_ids & checked_ids) - current_subscriber_ids)
        weeks.append({
            "week": record["week"],
            "voters": len(record["voter_ids"]),
            "subscribers": len(record["subscriber_ids"]),
            # Kept for API compatibility with the first subscription-split view.
            "non_subscribers": len(non_subscriber_ids),
            "subscribed_after": subscribed_after,
            "not_subscribed": not_subscribed,
        })
    return weeks


# -------------------------------------------------------------------- announced results


def results_path(entry: str, poll_id: str) -> Path:
    """Where this poll's results record lives -- same `<poll key>_<poll id>` naming as
    poll_path, one directory down. Built from _voting_dir() rather than the RESULTS_DIR
    constant so a test that patches _voting_dir redirects results too."""
    return _voting_dir() / "results" / f"{_poll_key(entry)}_{poll_id}.json"


def who(entry: Entry) -> str:
    """How an entry's author is named in prose: the display name, plus the @handle when
    there is one so the winner is actually pingable. Lives here rather than in
    bot_listener because the announcement text is built here and the listener only
    delivers it."""
    return f"{entry.author_name} (@{entry.author_username})" if entry.author_username else entry.author_name


def votes_label(count: int) -> str:
    """"1 голос" / "2 голоса" / "5 голосов", with the 11-14 exception Russian grammar
    makes for the teens -- 11 is "голосов" even though it ends in 1."""
    tail_two = abs(count) % 100
    tail_one = abs(count) % 10
    if 11 <= tail_two <= 14 or tail_one == 0 or tail_one >= 5:
        word = "голосов"
    elif tail_one == 1:
        word = "голос"
    else:
        word = "голоса"
    return f"{count} {word}"


# The announcement's closing lines, fixed wording dictated by the user. Deliberately
# emoji-free: medals read as decoration the chat did not ask for, and bot prose in this
# project stays plain (the stat/leaderboard displays are the only exception).
_RESULTS_HEADER = "Результаты недельного голосования:"
_RESULTS_FOOTER = "Всем спасибо за участие.\nКрасим дальше."
# What replaces the list of places when the poll closed with no votes at all. Announcing
# an empty top 3 would be worse than saying so: a header followed by nothing reads like
# the message got truncated, so it says plainly that nobody scored.
_RESULTS_NOBODY = "В этот раз голосов не набрал никто."


def results_header(poll: "Poll | None" = None) -> str:
    """The weekly contest's dictated header, or the thematic contest's own name."""
    if poll is None or poll.is_weekly:
        return _RESULTS_HEADER
    return f"Результаты конкурса «{poll.label()}»:"


def format_results_text(
    standings: list[tuple[Entry, int]], places: int | None = None, header: str | None = None,
) -> str:
    """The announcement message for a finished poll.

        Результаты недельного голосования:
        1. Имя (@username) — 17 голосов
        2. Имя — 14 голосов
        3. Имя (@username) — 12 голосов

        Всем спасибо за участие.
        Красим дальше.

    Every entrant is listed, not just a podium: the announcement is the only place the
    chat ever sees the score -- the poll is closed by then and the Mini App shows nothing
    to anyone who did not vote -- so cutting it at three would hide most of the week's
    work. Entries nobody voted for are listed too, with their nought, since being in the
    contest is the thing being acknowledged. `places` caps the list when a caller wants
    one; None (the default) means all of them.

    `standings` is a tally() result: already ordered, every admitted entry included.
    Positions are positional -- a tie shares no number, the tally's own ordering decides,
    because the chat needs one unambiguous winner to hand the prize to.

    `header` replaces the first line -- results_header(poll) names a thematic contest.
    """
    lines = []
    for index, (entry, votes) in enumerate(standings if places is None else standings[:places], start=1):
        lines.append(f"{index}. {who(entry)} — {votes_label(votes)}")
    # A board of nothing but noughts is a worse read than saying it outright, so the whole
    # list collapses to one line when the poll closed without a single vote cast.
    if not any(votes > 0 for _, votes in standings):
        lines = []
    body = "\n".join(lines) if lines else _RESULTS_NOBODY
    return f"{header or _RESULTS_HEADER}\n{body}\n\n{_RESULTS_FOOTER}"


def save_results(poll: Poll, standings: list[tuple[Entry, int]], text: str) -> Path:
    """Writes the announced result of `poll` as a self-contained JSON record and returns
    its path.

    Self-contained on purpose: the poll file it came from keeps being rewritten (entries
    re-collected, admitting changed, votes still arriving on a page someone left open),
    so a record that only stored entry ids would quietly describe something other than
    what was announced. Every ranked entry is copied in with the votes it had at the
    moment of announcing -- all of them, since the announcement itself names every entrant
    and any later "what happened that week" lookup wants the full board.

    Overwrites an existing record for the same poll: announcing is idempotent here (see
    close_and_announce), and a re-announcement is the newer truth, not a second event.
    """
    path = results_path(poll.entry, poll.poll_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "poll_id": poll.poll_id,
        "entry": poll.entry,
        "created_at": poll.created_at,
        "announced_at": datetime.now(timezone.utc).isoformat(),
        "voters": len(poll.votes),
        "text": text,
        # Which contest this was: the Hall of Fame (hall_of_fame.import_history) reads
        # these back for a week whose poll file has since been archived.
        "hashtag": poll.hashtag,
        "title": poll.title,
        "badge": poll.badge,
        "standings": [
            {
                "place": place,
                "entry_id": e.entry_id,
                "message_id": e.message_id,
                "author_id": e.author_id,
                "author_name": e.author_name,
                "author_username": e.author_username,
                "votes": votes,
                "text": e.text,
                "media": list(e.media),
                "posted_at": e.posted_at,
            }
            for place, (e, votes) in enumerate(standings, start=1)
        ],
    }
    # Same tmp-then-replace as save_poll: a crash mid-write must not leave a half-written
    # record, which here would be a week's result lost for good rather than re-fetchable.
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def results_poll_ids(entry: str) -> list[str]:
    """The poll ids this chat has an announced results record for, oldest id first --
    including weeks whose poll has since been cleared, since clearing keeps the results."""
    directory = _voting_dir() / "results"
    if not directory.exists():
        return []
    prefix = f"{_poll_key(entry)}_"
    return sorted(path.stem[len(prefix):] for path in directory.glob(f"{prefix}*.json"))


def load_archived_poll(entry: str, poll_id: str) -> Poll | None:
    """A cleared poll out of archive_dir(), or None. A week cleared twice has a time stamp
    on the end of its file name (see archive_all_polls); the newest of those is returned."""
    directory = archive_dir()
    if not directory.exists():
        return None
    exact = directory / poll_path(entry, poll_id).name
    candidates = [exact] if exact.exists() else sorted(
        directory.glob(f"{_poll_key(entry)}_{poll_id}_*.json"), reverse=True,
    )
    for path in candidates:
        try:
            return Poll.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            continue
    return None


def load_results(entry: str, poll_id: str) -> dict | None:
    """The record save_results wrote, or None if there is none or it is unreadable --
    same tolerance as load_poll: a corrupt file means "nothing announced yet" to every
    caller, which is recoverable, rather than an exception on a page render."""
    path = results_path(entry, poll_id)
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


# ------------------------------------------------------ Telegram Mini App identity check


class InitDataError(Exception):
    """The initData a Mini App sent is missing, malformed, expired, or not signed by us."""


def verify_init_data(
    init_data: str, bot_token: str, max_age_seconds: int = INIT_DATA_MAX_AGE_SECONDS
) -> dict:
    """Validates the signed payload Telegram gives a Mini App and returns its `user` dict.

    This is the whole of the authentication: everything else on the voting API trusts the
    user id this returns, so it must never fall back to trusting unsigned input. The
    scheme (core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app):
    every field except `hash` joined as "key=value" lines in key order, HMAC-SHA256'd with
    a key that is itself HMAC-SHA256("WebAppData", bot_token).

    Raises InitDataError on anything short of a valid, current signature.
    """
    if not init_data:
        raise InitDataError("no initData -- open the vote from the button in Telegram")
    if not bot_token:
        raise InitDataError("the bot token is not configured on the server")

    # keep_blank_values: a present-but-empty field is still part of what was signed, and
    # dropping it would change the check string and fail every signature that has one.
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = fields.pop("hash", "")
    if not received_hash:
        raise InitDataError("initData carries no hash")

    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    # compare_digest, not ==: a plain comparison leaks how much of the hash matched.
    if not hmac.compare_digest(expected, received_hash):
        raise InitDataError("initData signature does not match -- not issued by this bot")

    try:
        auth_date = int(fields.get("auth_date", "0"))
    except ValueError:
        raise InitDataError("initData has an unreadable auth_date")
    age = datetime.now(timezone.utc).timestamp() - auth_date
    if auth_date <= 0 or age > max_age_seconds:
        raise InitDataError("this page has been open too long -- reopen the vote")

    try:
        user = json.loads(fields.get("user") or "{}")
    except json.JSONDecodeError:
        raise InitDataError("initData has an unreadable user")
    if not isinstance(user, dict) or not user.get("id"):
        raise InitDataError("initData identifies no user")
    return user


def display_name(user: dict) -> str:
    parts = [(user or {}).get("first_name"), (user or {}).get("last_name")]
    name = " ".join(p for p in parts if p)
    return name or (user or {}).get("username") or f"id{(user or {}).get('id')}"
