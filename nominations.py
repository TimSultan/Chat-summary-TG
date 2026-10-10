"""Vote v3: the week's works voted on in NOMINATIONS -- named categories ("Аниме",
"Фэнтези") that an administrator makes up, each its own small ballot, shown as tabs in one
Mini App (nominations_web.py, opened by /vote3).

A third system beside /vote (voting.py) and /vote2 (arena.py). It VOTES apart from v1 --
its own storage tree, its own lock, its own command and routes -- but it does not COLLECT
apart from it: the works it offers are the works /vote has collected (sync_from_v1), so
there is one collection and nobody has to gather the week twice. That link only ever
reads: nothing here writes a poll or a v1 photo, and nothing in voting.py reads anything
here, so v1 cannot tell that this exists.

THE MODEL. One live contest per chat: a POOL of works and a list of nominations. The pool
mirrors /vote's collection, and is the administrator's raw material -- never shown to
voters as such; a voter sees each nomination's works and nothing else. A nomination is a
name, the ids of the pool works playing in it, and its own ballots. The same work may play
in several nominations, and a ballot in one is not a ballot in another.

The photos are COPIED into v3's own directory rather than served out of v1's, which is
the one thing the two do not share: v1's "очистить" moves its photos into v1's archive,
and a nominations vote still running at that moment must not lose its pictures.

One contest rather than one per ISO week (a poll's and a tournament's key): nominations are
built by hand, and a week key would hide all of them behind a fresh empty week the first
time somebody collected on a Monday. Starting over is "очистить", which archives the file.

Every write goes through update_contest, which holds a THREADING lock around the whole
load -> mutate -> save. The web handlers run it through asyncio.to_thread so the disk work
stays off the event loop that also serves v1's ballots, and a threading lock is the one
that still serialises writers once they are on worker threads.
"""

import hashlib
import json
import os
import shutil
import threading
import unicodedata
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

import archive_store
import voting

DATA_DIR = Path(os.getenv("DATA_DIR", "."))
# A tree of its own, not a subdirectory of v1's: voting.py globs "<key>_*.json" out of its
# own directory, and the cheapest way to never be in that glob is to live somewhere else.
NOMINATIONS_DIR = DATA_DIR / "nominations"

# A name is a tab label on a phone: past this it stops fitting and starts wrapping.
NAME_MAX_LENGTH = 40
# More tabs than this is a scrolling strip nobody reads to the end of.
MAX_NOMINATIONS = 20


class ContestError(Exception):
    """A request this contest refuses, with the HTTP status the web layer should answer.

    Raised from inside update_contest's mutation, so a refused change is never saved."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class Nomination:
    nomination_id: str
    name: str
    # Pool entry ids playing in this nomination. A work removed from the pool (a clear, a
    # re-collect that lost it) simply stops resolving -- see Contest.members.
    entry_ids: list[str] = field(default_factory=list)
    # voter id (a string, since JSON keys are) -> the entry ids they chose HERE.
    votes: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "nomination_id": self.nomination_id,
            "name": self.name,
            "entry_ids": list(self.entry_ids),
            "votes": {k: list(v) for k, v in self.votes.items()},
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Nomination":
        return cls(
            nomination_id=str(raw.get("nomination_id") or ""),
            name=str(raw.get("name") or ""),
            entry_ids=[str(e) for e in raw.get("entry_ids") or []],
            votes={str(k): [str(e) for e in v] for k, v in (raw.get("votes") or {}).items()},
        )


@dataclass
class Contest:
    entry: str                       # the LISTENER_ALLOWED_CHATS entry it belongs to
    created_at: str
    entries: list[voting.Entry] = field(default_factory=list)   # the pool
    nominations: list[Nomination] = field(default_factory=list)
    open: bool = True
    # How many works one ballot may name, in EVERY nomination. None is unlimited, which is
    # v1's default too -- a voter used to /vote should not find this one stricter by surprise.
    max_choices: int | None = None

    def to_dict(self) -> dict:
        return {
            "entry": self.entry,
            "created_at": self.created_at,
            "entries": [e.to_dict() for e in self.entries],
            "nominations": [n.to_dict() for n in self.nominations],
            "open": self.open,
            "max_choices": self.max_choices,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Contest":
        return cls(
            entry=raw.get("entry") or "",
            created_at=raw.get("created_at") or "",
            entries=[voting.Entry.from_dict(e) for e in raw.get("entries") or []],
            nominations=[Nomination.from_dict(n) for n in raw.get("nominations") or []],
            open=bool(raw.get("open", True)),
            max_choices=(int(raw["max_choices"]) if raw.get("max_choices") else None),
        )

    def entry_map(self) -> dict[str, voting.Entry]:
        return {e.entry_id: e for e in self.entries}

    def nomination(self, nomination_id: str) -> Nomination | None:
        return next((n for n in self.nominations if n.nomination_id == nomination_id), None)

    def members(self, nomination: Nomination) -> list[str]:
        """The nomination's works that are still in the pool, in the order it lists them."""
        known = self.entry_map()
        return [entry_id for entry_id in nomination.entry_ids if entry_id in known]

    def ballot(self, nomination: Nomination, user_id) -> list[str]:
        """What this voter has chosen in this nomination, as it counts NOW.

        A choice of a work since taken out of the nomination is kept on file but not shown
        or counted -- the same rule v1 applies to un-admitting -- so putting the work back
        returns the vote rather than having silently lost it."""
        members = set(self.members(nomination))
        return [e for e in nomination.votes.get(str(user_id), []) if e in members]

    def voter_count(self, nomination: Nomination) -> int:
        """People whose ballot here still names at least one of its works."""
        members = set(self.members(nomination))
        return sum(1 for choices in nomination.votes.values() if members.intersection(choices))

    def tally(self, nomination: Nomination) -> list[tuple[voting.Entry, int]]:
        """The nomination's works with their votes, most first. A tie keeps the order the
        nomination lists them in rather than an id order nobody chose."""
        known = self.entry_map()
        members = self.members(nomination)
        counts = dict.fromkeys(members, 0)
        for choices in nomination.votes.values():
            for entry_id in set(choices):
                if entry_id in counts:
                    counts[entry_id] += 1
        position = {entry_id: index for index, entry_id in enumerate(members)}
        ranked = sorted(members, key=lambda e: (-counts[e], position[e]))
        return [(known[entry_id], counts[entry_id]) for entry_id in ranked]


# ------------------------------------------------------------------------------ storage


def _nominations_dir() -> Path:
    """Indirection purely for tests (patch this, not the constant) -- voting._voting_dir's
    convention."""
    return NOMINATIONS_DIR


def _key(entry: str) -> str:
    normalized = unicodedata.normalize("NFKC", entry or "").strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def contest_path(entry: str) -> Path:
    return _nominations_dir() / f"{_key(entry)}.json"


def media_path(entry: str) -> Path:
    """v3's OWN photo directory. Never v1's: clearing either system must not be able to
    delete the other's pictures, which is also why importing from v1 copies them."""
    return _nominations_dir() / "media" / _key(entry)


def archive_dir() -> Path:
    return _nominations_dir() / "archive"


def save_contest(contest: Contest) -> None:
    path = contest_path(contest.entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    # tmp-then-replace, like voting.save_poll: a crash mid-write must not leave a
    # truncated file, and a lost contest is lost ballots, not a lost cache.
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(contest.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_contest(entry: str) -> Contest | None:
    """The contest, or None if there is none -- or if its file no longer parses, which to
    a reader means the same thing. Writers check for that case themselves (update_contest)
    so a corrupt file is reported rather than overwritten."""
    path = contest_path(entry)
    if not path.exists():
        return None
    try:
        return Contest.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None


# One writer at a time, across threads: see the module docstring.
_write_lock = threading.Lock()


def update_contest(entry: str, mutate, create: bool = False):
    """Load the contest, apply `mutate(contest)`, save it -- as one step no other writer
    can interleave with -- and return (contest, whatever mutate returned).

    `create` makes an empty contest when there is none; otherwise there being none is a
    404. A file that exists but does not parse is refused rather than replaced: starting a
    fresh contest over it would erase every ballot it held. A ContestError raised by
    `mutate` propagates and nothing is saved.
    """
    with _write_lock:
        contest = load_contest(entry)
        if contest is None:
            if contest_path(entry).exists():
                raise ContestError("файл номинаций повреждён -- смотри логи сервера", status=500)
            if not create:
                raise ContestError("номинации ещё не созданы", status=404)
            contest = Contest(entry=entry, created_at=datetime.now(timezone.utc).isoformat())
        result = mutate(contest)
        save_contest(contest)
        return contest, result


def archive_contest(entry: str) -> bool:
    """Starts over: the contest leaves the live set.

    Nothing is destroyed: the file -- its ballots are the only record of how the
    nominations went -- and its photos move into archive_dir() together
    (archive_store.move_to_archive). The photos used to be deleted here. Returns whether
    there was anything to clear."""
    with _write_lock:
        path = contest_path(entry)
        existed = path.exists()
        archive_store.move_to_archive(path, media_path(entry), archive_dir())
        return existed


# --------------------------------------------------------------------------- the pool


def add_entries(contest: Contest, entries: list[voting.Entry]) -> int:
    """Adds works the pool does not have yet, and returns how many. Known ones are left as
    they are, so adding twice is harmless. (The pool's normal source is sync_from_v1.)"""
    known = {e.entry_id for e in contest.entries}
    incoming = [e for e in entries if e.entry_id not in known]
    # Newest post first, matching how collect_entries hands them over and how v1 lists them.
    contest.entries = incoming + contest.entries
    return len(incoming)


def v1_collection(entry: str) -> tuple[list[tuple[voting.Entry, "voting.Poll"]], set[str]]:
    """What /vote has collected right now: every work in every LIVE poll (cleared weeks are
    archived and no longer count), newest poll first, each with the poll it came from; and
    the ids /vote has admitted to its own ballot.

    Every live poll rather than only latest_poll: v1 can hold two weeks at once -- the one
    being voted in and a newer one collected but not yet moderated -- and both are works
    somebody posted for the contest. Read-only, like everything here that touches v1.
    """
    polls = [poll for poll in (voting.load_poll(entry, poll_id) for poll_id in voting.poll_ids(entry))
             if poll is not None]
    polls.sort(key=lambda poll: poll.created_at, reverse=True)
    works, seen, admitted = [], set(), set()
    for poll in polls:
        admitted.update(poll.approved)
        for work in poll.entries:
            if work.entry_id not in seen:
                seen.add(work.entry_id)
                works.append((work, poll))
    return works, admitted


def _copy_photos(work: voting.Entry, poll: "voting.Poll", target: Path) -> voting.Entry | None:
    """`work` with its photos copied out of v1's media directory into v3's, or None if not
    one of them could be -- a card with no picture has nothing to vote on. No lock: these
    are picture files, which nothing else in v3 writes."""
    source = voting.media_path(poll.entry, poll.poll_id)
    copied = []
    for name in work.media:
        origin, destination = source / name, target / name
        if not destination.exists():
            if not origin.is_file():
                continue
            try:
                target.mkdir(parents=True, exist_ok=True)
                shutil.copy2(origin, destination)
            except OSError:
                continue  # one unreadable photo costs that card a picture, not the sync
        copied.append(name)
    return replace(work, media=copied) if copied else None


def _mirrored(pool: list[voting.Entry], live: list[voting.Entry], used: set[str]) -> list[voting.Entry]:
    """The pool /vote's collection implies: its works in its order, then whatever has left
    /vote but still plays in a nomination. A work /vote dropped (a cleared week, a deleted
    post) leaves the pool unless a nomination is using it -- pulling a candidate out from
    under a running vote is what must never happen."""
    live_ids = {work.entry_id for work in live}
    return live + [work for work in pool if work.entry_id not in live_ids and work.entry_id in used]


def sync_from_v1(entry: str) -> tuple[Contest | None, int, set[str]]:
    """Brings the pool in line with what /vote has collected, and returns (contest, how
    many works are new to the pool, the ids /vote has admitted).

    Run whenever an administrator looks at v3, so the pool is never older than the screen
    showing it and there is no "import" to remember. Writes only when the pool would
    actually change, so opening the editing view twice does not save twice; and creates the
    contest if there is none yet but /vote has works, so the first look already has them.
    A work already in the pool keeps the copy it has: only new works are copied.
    """
    collected, admitted = v1_collection(entry)
    contest = load_contest(entry)
    pool = contest.entries if contest else []
    known = {work.entry_id for work in pool}
    target = media_path(entry)
    fresh = {}
    for work, poll in collected:
        if work.entry_id not in known:
            copy = _copy_photos(work, poll, target)
            if copy is not None:
                fresh[copy.entry_id] = copy

    def live_from(current: dict[str, voting.Entry]) -> list[voting.Entry]:
        return [current.get(work.entry_id) or fresh[work.entry_id]
                for work, _ in collected if work.entry_id in current or work.entry_id in fresh]

    def used_by(contest_: Contest | None) -> set[str]:
        return {e for n in (contest_.nominations if contest_ else []) for e in n.entry_ids}

    planned = _mirrored(pool, live_from({w.entry_id: w for w in pool}), used_by(contest))
    if [w.entry_id for w in planned] == [w.entry_id for w in pool]:
        return contest, 0, admitted

    def mutate(contest_: Contest) -> int:
        # Recomputed under the lock from what is on disk NOW, not from the read above: an
        # administrator may have put a work into a nomination in between.
        current = contest_.entry_map()
        contest_.entries = _mirrored(contest_.entries, live_from(current), used_by(contest_))
        return sum(1 for work in contest_.entries if work.entry_id not in current)

    contest, added = update_contest(entry, mutate, create=True)
    return contest, added, admitted


# ------------------------------------------------------------------------- nominations


def clean_name(raw) -> str:
    """A nomination name as it will be stored: whitespace collapsed, length checked."""
    if not isinstance(raw, str):
        raise ContestError("название должно быть текстом")
    name = " ".join(raw.split())
    if not name:
        raise ContestError("название не может быть пустым")
    if len(name) > NAME_MAX_LENGTH:
        raise ContestError(f"название длиннее {NAME_MAX_LENGTH} символов")
    return name


def _require(contest: Contest, nomination_id) -> Nomination:
    nomination = contest.nomination(str(nomination_id or ""))
    if nomination is None:
        raise ContestError("такой номинации нет -- обнови страницу", status=404)
    return nomination


def _refuse_duplicate(contest: Contest, name: str, keep: Nomination | None = None) -> None:
    # Two tabs reading the same is two ballots the voter cannot tell apart.
    folded = name.casefold()
    if any(n is not keep and n.name.casefold() == folded for n in contest.nominations):
        raise ContestError(f"номинация «{name}» уже есть", status=409)


def add_nomination(contest: Contest, raw_name) -> Nomination:
    name = clean_name(raw_name)
    if len(contest.nominations) >= MAX_NOMINATIONS:
        raise ContestError(f"номинаций не может быть больше {MAX_NOMINATIONS}", status=409)
    _refuse_duplicate(contest, name)
    taken = {n.nomination_id for n in contest.nominations}
    nomination_id = uuid.uuid4().hex[:8]
    while nomination_id in taken:  # pragma: no cover -- 8 hex digits, 20 nominations
        nomination_id = uuid.uuid4().hex[:8]
    nomination = Nomination(nomination_id=nomination_id, name=name)
    contest.nominations.append(nomination)
    return nomination


def rename_nomination(contest: Contest, nomination_id, raw_name) -> Nomination:
    nomination = _require(contest, nomination_id)
    name = clean_name(raw_name)
    _refuse_duplicate(contest, name, keep=nomination)
    nomination.name = name
    return nomination


def set_nomination_entries(contest: Contest, nomination_id, entry_ids) -> Nomination:
    """Replaces which works play in a nomination, wholesale -- the page always sends the
    complete set it is showing, so this cannot drift from what the administrator saw.

    Stored in POOL order whatever order they arrive in, so the voter's grid matches the
    administrator's. Ballots are not touched: a work taken out stops counting (see
    Contest.ballot) and counts again if it is put back."""
    nomination = _require(contest, nomination_id)
    if not isinstance(entry_ids, list) or not all(isinstance(e, str) for e in entry_ids):
        raise ContestError("entry_ids must be a list of entry ids")
    wanted = set(entry_ids)
    nomination.entry_ids = [e.entry_id for e in contest.entries if e.entry_id in wanted]
    return nomination


def delete_nomination(contest: Contest, nomination_id) -> Nomination:
    nomination = _require(contest, nomination_id)
    contest.nominations.remove(nomination)
    return nomination


def set_settings(contest: Contest, body: dict) -> None:
    """Whichever of `open` and `max_choices` the body carries; absent keys are untouched."""
    if "max_choices" in body:
        max_choices = body["max_choices"]
        if max_choices is not None and (
            isinstance(max_choices, bool) or not isinstance(max_choices, int) or max_choices < 1
        ):
            raise ContestError("max_choices must be a positive integer or null")
        contest.max_choices = max_choices
    if "open" in body:
        contest.open = bool(body["open"])


def record_vote(contest: Contest, nomination_id, user_id, choices) -> list[str]:
    """One ballot per voter per nomination, replacing their previous one there -- voting
    again is changing your mind. Returns the ballot as recorded.

    Choices outside the nomination are dropped rather than refusing the ballot, so a page
    left open across an edit still records what is still valid. Going OVER the cap is
    refused outright instead: quietly keeping the first N would be choosing for the voter.
    An empty ballot withdraws the voter from this nomination entirely."""
    if not contest.open:
        raise ContestError("голосование закрыто", status=409)
    nomination = _require(contest, nomination_id)
    if not isinstance(choices, list) or not all(isinstance(c, str) for c in choices):
        raise ContestError("choices must be a list of entry ids")
    members = set(contest.members(nomination))
    picked = [c for c in dict.fromkeys(choices) if c in members]
    if contest.max_choices and len(picked) > contest.max_choices:
        raise ContestError(f"в номинации можно выбрать не более {contest.max_choices}")
    voter_id = str(user_id)
    if picked:
        nomination.votes[voter_id] = picked
    else:
        nomination.votes.pop(voter_id, None)
    return picked


# ------------------------------------------------------------------------------ reports


def results_text(contest: Contest) -> str:
    """Every nomination's standings, for "/vote3 итоги" -- plain text an administrator can
    read in the DM or paste into the chat as it is. Emoji-free like v1's announcement."""
    if not contest.nominations:
        return "Номинаций пока нет."
    lines = [f"Номинации — {'голосование открыто' if contest.open else 'голосование закрыто'}"]
    for nomination in contest.nominations:
        lines.append("")
        lines.append(f"{nomination.name} (проголосовало: {contest.voter_count(nomination)})")
        standings = contest.tally(nomination)
        if not standings:
            lines.append("работ в номинации нет")
            continue
        if not any(votes for _, votes in standings):
            lines.append("голосов пока нет")
            continue
        for place, (work, votes) in enumerate(standings, start=1):
            lines.append(f"{place}. {voting.who(work)} — {voting.votes_label(votes)}")
    return "\n".join(lines)
