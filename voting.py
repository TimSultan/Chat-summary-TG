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

import hashlib
import hmac
import json
import os
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl

DATA_DIR = Path(os.getenv("DATA_DIR", "."))
VOTING_DIR = DATA_DIR / "voting"

# The hashtag that nominates a post. Kept in sync with stats.WEEKLY_CONTEST_HASHTAG,
# imported lazily in collect_entries so this module stays importable on its own.
CONTEST_HASHTAG = "#итогинедели"

# How many calendar days back a new poll looks for nominations, counting today. Two, per
# the request: today and yesterday.
DEFAULT_LOOKBACK_DAYS = 2

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
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    hashtag: str = CONTEST_HASHTAG,
    log=print,
) -> list[Entry]:
    """Reads the last `lookback_days` calendar days of `chat_ref` and returns one Entry per
    nominated post, downloading every attached photo into `media_dir`.

    Uses the Telethon session directly rather than telegram_fetch's cache: that cache
    stores plain text dicts, and this needs the media and the grouped_id, neither of which
    survives that conversion.
    """
    from telegram_fetch import resolve_chat, sender_display_name

    entity = chat_ref if not isinstance(chat_ref, str) else await resolve_chat(client, chat_ref)

    now_local = datetime.now(tz)
    start_local = (now_local - timedelta(days=lookback_days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start_utc = start_local.astimezone(timezone.utc)

    messages = []
    async for message in client.iter_messages(entity, reverse=False):
        if message.date < start_utc:
            break
        if message.action is not None:
            continue  # service message (join/leave/pin)
        messages.append(message)

    groups = group_into_entries(messages, hashtag)
    log(f"[voting] {len(messages)} message(s) since {start_local.date()} -> {len(groups)} nomination(s)")

    media_dir.mkdir(parents=True, exist_ok=True)
    entries: list[Entry] = []
    for group in groups:
        head = group[0]
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
    open: bool = True
    # Set once by close_and_announce, kept alongside the poll so a reloaded page (or a
    # second look days later) can still show who won without recomputing it from votes
    # that may since have shifted (an un-admit after closing, say).
    winner_entry_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "poll_id": self.poll_id,
            "entry": self.entry,
            "created_at": self.created_at,
            "entries": [e.to_dict() for e in self.entries],
            "approved": list(self.approved),
            "votes": {k: list(v) for k, v in self.votes.items()},
            "open": self.open,
            "winner_entry_id": self.winner_entry_id,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Poll":
        return cls(
            poll_id=str(raw.get("poll_id") or ""),
            entry=raw.get("entry") or "",
            created_at=raw.get("created_at") or "",
            entries=[Entry.from_dict(e) for e in raw.get("entries") or []],
            approved=[str(e) for e in raw.get("approved") or []],
            votes={str(k): [str(e) for e in v] for k, v in (raw.get("votes") or {}).items()},
            open=bool(raw.get("open", True)),
            winner_entry_id=(str(raw["winner_entry_id"]) if raw.get("winner_entry_id") else None),
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


def save_poll(poll: Poll) -> None:
    path = poll_path(poll.entry, poll.poll_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written to a sibling then moved: a redeploy or crash mid-write would otherwise
    # leave a truncated file, and unlike a cache this cannot simply be re-fetched.
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(poll.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_poll(entry: str, poll_id: str) -> Poll | None:
    path = poll_path(entry, poll_id)
    if not path.exists():
        return None
    try:
        return Poll.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None


def latest_poll(entry: str) -> Poll | None:
    """The most recently created poll for this chat -- what /vote and the page open."""
    directory = _voting_dir()
    if not directory.exists():
        return None
    prefix = f"{_poll_key(entry)}_"
    polls = []
    for path in directory.glob(f"{prefix}*.json"):
        try:
            polls.append(Poll.from_dict(json.loads(path.read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            continue
    if not polls:
        return None
    polls.sort(key=lambda p: p.created_at, reverse=True)
    return polls[0]


def build_poll(entry: str, poll_id: str, entries: list[Entry], existing: Poll | None = None) -> Poll:
    """A poll for `entries`, carrying over the moderation and votes of `existing`.

    Re-collecting is how an administrator picks up nominations posted since the last run,
    so it must not undo the admitting they have already done or throw away votes already
    cast. Anything that is no longer among the entries drops out of both -- a deleted post
    cannot stay admitted or keep its votes.
    """
    now = datetime.now(timezone.utc).isoformat()
    poll = Poll(
        poll_id=poll_id,
        entry=entry,
        created_at=(existing.created_at if existing else now),
        entries=entries,
    )
    if existing is None:
        return poll

    known = {e.entry_id for e in entries}
    poll.approved = [entry_id for entry_id in existing.approved if entry_id in known]
    poll.votes = {
        user_id: [e for e in choices if e in known]
        for user_id, choices in existing.votes.items()
    }
    poll.open = existing.open
    if existing.winner_entry_id in known:
        poll.winner_entry_id = existing.winner_entry_id
    return poll


def set_approved(poll: Poll, entry_ids: list[str]) -> Poll:
    """Replaces the admitted set wholesale -- the moderation screen always submits the
    complete picture, so this cannot drift from what the administrator saw."""
    known = {e.entry_id for e in poll.entries}
    poll.approved = [entry_id for entry_id in dict.fromkeys(entry_ids) if entry_id in known]
    return poll


def record_vote(poll: Poll, user_id: int | str, entry_ids: list[str]) -> Poll:
    """One ballot per user, replacing whatever they chose before -- voting again is
    changing your mind, not stuffing the box. Choices outside the admitted set are dropped
    rather than rejecting the whole ballot, so a page left open across a moderation change
    still records the choices that are still valid."""
    allowed = set(poll.approved)
    poll.votes[str(user_id)] = [e for e in dict.fromkeys(entry_ids) if e in allowed]
    return poll


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
