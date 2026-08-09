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

# A collection covers the CONTEST WEEK -- Monday 00:00 local through the moment of
# collecting -- rather than a rolling number of days. Collecting happens on Sunday, and a
# rolling window either misses the Monday-to-Friday posts or, run a day late, reaches back
# into the previous week and pulls its works into the new poll.
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
    progress=None,
    log=print,
) -> list[Entry]:
    """Reads the CURRENT CONTEST WEEK of `chat_ref` -- Monday 00:00 local through now --
    and returns one Entry per NEWLY found nominated post, downloading every attached photo
    into `media_dir`.

    The window is the week rather than a rolling span of days on purpose: collecting runs
    on Sunday, so a rolling window is simultaneously too short (it misses everything posted
    Monday through Friday) and, if collecting slips past midnight into Monday, too long --
    it reaches back into the previous week and pulls its works into the new poll.

    `skip_entry_ids` -- entry ids (message ids, as strings) already known from a previous
    collection -- do two things. They are never resolved further (no get_sender() round
    trip, no photo download, no Entry built), and, more importantly, THE SCAN STOPS at the
    first one it meets. The listing is newest-first, so everything past a work that was
    already collected was already collected too; a re-collect that only wants today's
    additions has no reason to read back to Monday. A first collection (no skip ids) still
    reads the whole week.

    The caller is responsible for keeping those already-known entries around (see
    bot_listener.handle_vote_command) -- this function only ever reports what's new.

    The cost of stopping early: a post that gained the hashtag AFTER it was first passed
    over -- edited days later -- sits below the newest known work and is not picked up.
    Clearing and collecting again finds it, and that is rarer than adding a few late
    entries to a poll, which is the case this is for.

    Uses the Telethon session directly rather than telegram_fetch's cache: that cache
    stores plain text dicts, and this needs the media and the grouped_id, neither of which
    survives that conversion.
    """
    from telegram_fetch import resolve_chat, sender_display_name

    entity = chat_ref if not isinstance(chat_ref, str) else await resolve_chat(client, chat_ref)

    now_local = datetime.now(tz)
    start_local = contest_week_start(now_local)
    start_utc = start_local.astimezone(timezone.utc)

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
    await report("scan", 0, 0)
    async for message in client.iter_messages(entity, reverse=False):
        if message.date < start_utc:
            break
        if message.action is not None:
            continue  # service message (join/leave/pin)
        messages.append(message)
        # Reaching a nomination that was already collected means everything below it was
        # too: the listing is newest-first, so a re-collect only has to walk back as far
        # as the first thing it recognises. Without this, adding one late entry re-read
        # the whole week every time.
        #
        # The known message is kept rather than dropped, and the break happens after
        # appending it: in an album the entry id is the FIRST message's, which arrives
        # last here, so stopping before it would leave the album's other messages behind
        # as a headless group -- which reads as a brand-new nomination and gets collected
        # a second time.
        if skip_entry_ids and str(message.id) in skip_entry_ids:
            stopped_at_known = True
            break
        if len(messages) % 250 == 0:
            await report("scan", len(messages), 0)

    groups = group_into_entries(messages, hashtag)
    log(
        f"[voting] {len(messages)} message(s) "
        f"{'back to the first already-collected work' if stopped_at_known else f'since {start_local.date()}'}"
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
            "max_choices": self.max_choices,
            "allow_revote": self.allow_revote,
            "crops": {k: dict(v) for k, v in self.crops.items()},
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
            max_choices=(int(raw["max_choices"]) if raw.get("max_choices") else None),
            allow_revote=bool(raw.get("allow_revote", True)),
            crops=_clean_crops(raw.get("crops")),
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


def latest_poll(entry: str) -> Poll | None:
    """The most recently created poll for this chat -- what /vote and the page open."""
    polls = _all_polls(entry)
    return polls[0] if polls else None


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
    poll.max_choices = existing.max_choices
    poll.allow_revote = existing.allow_revote
    # Framing survives a re-collect for the same reason admitting does: it is work the
    # administrator did by hand, and a poll refreshed to pick up two new nominations must
    # not silently un-crop the dozen that were already framed.
    poll.crops = {k: dict(v) for k, v in existing.crops.items() if k in known}
    return poll


# Works no longer roll over between weeks. A poll contains exactly what was nominated in
# its own Monday-to-Sunday window, so the only way a work appears in a vote is that
# somebody posted it that week. The carry-over that used to re-seed a new poll with last
# week's runners-up was removed: it re-filled a poll the moderator had just cleared, which
# made "clear, then collect" impossible to express.


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


def format_results_text(standings: list[tuple[Entry, int]], places: int | None = None) -> str:
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
    """
    lines = []
    for index, (entry, votes) in enumerate(standings if places is None else standings[:places], start=1):
        lines.append(f"{index}. {who(entry)} — {votes_label(votes)}")
    # A board of nothing but noughts is a worse read than saying it outright, so the whole
    # list collapses to one line when the poll closed without a single vote cast.
    if not any(votes > 0 for _, votes in standings):
        lines = []
    body = "\n".join(lines) if lines else _RESULTS_NOBODY
    return f"{_RESULTS_HEADER}\n{body}\n\n{_RESULTS_FOOTER}"


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
        "standings": [
            {
                "place": place,
                "entry_id": e.entry_id,
                "author_id": e.author_id,
                "author_name": e.author_name,
                "author_username": e.author_username,
                "votes": votes,
                "text": e.text,
                "media": list(e.media),
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
