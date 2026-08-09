"""The arena: head-to-head voting as a SECOND, independent system beside the grid ballot
in voting.py.

Nothing here reads or writes v1's data. Separate directory, separate files, separate
media, separate moderation, separate commands -- the two can be run in the same week
without either noticing the other. What they do share is code that has nothing to do with
either one's rules: voting.Entry (a nominated post is a nominated post) and
voting.collect_entries (one implementation of "read #итогинедели out of the chat" is
enough, and it takes the media directory to download into as an argument).

The session rules are the ones import/CLAUDE.md says must not regress, with one
substitution: it identifies a voter by an invite code, and here Telegram already says who
somebody is, so the Telegram user id IS the code. Everything else stands --

    One voter, one ballot, for ever.  A finished ballot never reopens.
    Resume, don't restart.           Coming back mid-session returns the same pairs.
    Ranking is order-independent.    See arena_core.compute_standings.

Storage is one JSON file per tournament under DATA_DIR/arena, with the photos beside it,
mirroring voting.py's layout so both systems fail and survive a redeploy the same way.
"""

import asyncio
import hashlib
import json
import os
import shutil
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import arena_core
from arena_core import TIE
from voting import Entry

DATA_DIR = Path(os.getenv("DATA_DIR", "."))
ARENA_DIR = DATA_DIR / "arena"

# How many head-to-heads one voter is asked for. Ten is the reference default and about
# thirty seconds of tapping; CLAUDE.md's sizing note says 40 voters x 10 pairs is the floor
# for a 20-work field to separate at the top.
DEFAULT_PAIRS_PER_VOTER = 10
PAIRING_MODES = ("random", "adaptive")

# Refit the table at most this often: a single vote cannot move a pairing decision, and
# adaptive pairing asks for standings on every session start.
STANDINGS_CACHE_SECONDS = 30
STANDINGS_CACHE_VOTES = 10


class ArenaError(Exception):
    """Something the voter or admin should be told about, in their own words. `code` is
    the machine-readable half, mirroring import/voting-service.js's VotingError."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Ballot:
    """One voter's session: the pairs they were dealt and what they said about them."""

    user_id: str
    name: str = ""                 # display name, so the admin view can name a voter
    pairs: list = field(default_factory=list)   # [[entry_id, entry_id], ...]
    picks: list = field(default_factory=list)   # entry_id | TIE, one per answered pair
    status: str = "active"         # active | done
    started_at: str = ""
    done_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "Ballot":
        return cls(
            user_id=str(raw.get("user_id") or ""),
            name=raw.get("name") or "",
            # Tuples would round-trip as lists through JSON anyway; kept as lists so an
            # in-memory ballot and a reloaded one compare equal.
            pairs=[list(pair) for pair in raw.get("pairs") or []],
            picks=[str(pick) for pick in raw.get("picks") or []],
            status=raw.get("status") or "active",
            started_at=raw.get("started_at") or "",
            done_at=raw.get("done_at"),
        )

    @property
    def position(self) -> int:
        """Which pair this voter is on -- always exactly how many they have answered."""
        return len(self.picks)


@dataclass
class Tournament:
    tournament_id: str
    entry: str                     # the LISTENER_ALLOWED_CHATS entry it belongs to
    created_at: str
    entries: list = field(default_factory=list)          # Entry
    approved: list = field(default_factory=list)         # entry_ids admitted to the arena
    ballots: dict = field(default_factory=dict)          # user_id -> Ballot
    pairs_per_voter: int = DEFAULT_PAIRS_PER_VOTER
    pairing: str = "random"
    open: bool = True

    def to_dict(self) -> dict:
        return {
            "tournament_id": self.tournament_id,
            "entry": self.entry,
            "created_at": self.created_at,
            "entries": [e.to_dict() for e in self.entries],
            "approved": list(self.approved),
            "ballots": {k: b.to_dict() for k, b in self.ballots.items()},
            "pairs_per_voter": self.pairs_per_voter,
            "pairing": self.pairing,
            "open": self.open,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Tournament":
        return cls(
            tournament_id=str(raw.get("tournament_id") or ""),
            entry=raw.get("entry") or "",
            created_at=raw.get("created_at") or "",
            entries=[Entry.from_dict(e) for e in raw.get("entries") or []],
            approved=[str(e) for e in raw.get("approved") or []],
            ballots={
                str(k): Ballot.from_dict(v) for k, v in (raw.get("ballots") or {}).items()
            },
            pairs_per_voter=int(raw.get("pairs_per_voter") or DEFAULT_PAIRS_PER_VOTER),
            pairing=(raw.get("pairing") if raw.get("pairing") in PAIRING_MODES else "random"),
            open=bool(raw.get("open", True)),
        )

    def approved_entries(self) -> list:
        allowed = set(self.approved)
        return [e for e in self.entries if e.entry_id in allowed]

    def entry_by_id(self, entry_id: str):
        return next((e for e in self.entries if e.entry_id == entry_id), None)

    def standings(self) -> dict:
        """The fitted table over the ADMITTED works only. Votes for a work that was later
        un-admitted are ignored rather than counted for nobody -- the same rule
        voting.Poll.tally follows."""
        return arena_core.compute_standings(self.approved_entries(), list(self.ballots.values()))

    def progress(self) -> dict:
        done = sum(1 for b in self.ballots.values() if b.status == "done")
        active = sum(1 for b in self.ballots.values() if b.status == "active")
        judgements = sum(len(b.picks) for b in self.ballots.values())
        return {
            "voters": len(self.ballots),
            "completed": done,
            "in_progress": active,
            "judgements": judgements,
            "coverage": arena_core.coverage(len(self.approved), judgements),
            "open": self.open,
            "works": len(self.approved),
        }


# ------------------------------------------------------------------------------ storage


def _arena_dir() -> Path:
    """Indirection for tests to patch, matching voting._voting_dir's convention."""
    return ARENA_DIR


def _key(entry: str) -> str:
    normalized = unicodedata.normalize("NFKC", entry or "").strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def tournament_path(entry: str, tournament_id: str) -> Path:
    return _arena_dir() / f"{_key(entry)}_{tournament_id}.json"


def media_path(entry: str, tournament_id: str) -> Path:
    """The arena's OWN photo directory. Deliberately not v1's: clearing one system must
    never delete the other's pictures, and both address a photo as
    <tournament id>/<file name> inside their own tree."""
    return _arena_dir() / "media" / f"{_key(entry)}_{tournament_id}"


def save_tournament(tournament: Tournament) -> None:
    path = tournament_path(tournament.entry, tournament.tournament_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # tmp-then-replace, like voting.save_poll: a crash mid-write would otherwise leave a
    # truncated file, and a lost tournament is lost votes rather than a lost cache.
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(tournament.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_tournament(entry: str, tournament_id: str) -> Tournament | None:
    path = tournament_path(entry, tournament_id)
    if not path.exists():
        return None
    try:
        return Tournament.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None


def latest_tournament(entry: str) -> Tournament | None:
    directory = _arena_dir()
    if not directory.exists():
        return None
    found = []
    for path in directory.glob(f"{_key(entry)}_*.json"):
        try:
            found.append(Tournament.from_dict(json.loads(path.read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            continue
    if not found:
        return None
    found.sort(key=lambda t: t.created_at, reverse=True)
    return found[0]


def delete_tournament(entry: str, tournament_id: str) -> bool:
    path = tournament_path(entry, tournament_id)
    existed = path.exists()
    if existed:
        path.unlink()
    directory = media_path(entry, tournament_id)
    if directory.exists():
        shutil.rmtree(directory)
    return existed


def archive_dir() -> Path:
    """Where cleared tournaments are kept. A SUBDIRECTORY on purpose: latest_tournament
    globs the arena directory itself and does not recurse, so an archived week is
    invisible to the arena while its file still exists."""
    return _arena_dir() / "archive"


def tournament_ids(entry: str) -> list[str]:
    """Every tournament id on disk for this chat, read from filenames so a week whose
    JSON no longer parses is still listed."""
    directory = _arena_dir()
    if not directory.exists():
        return []
    prefix = f"{_key(entry)}_"
    return sorted(path.stem[len(prefix):] for path in directory.glob(f"{prefix}*.json"))


def archive_all_tournaments(entry: str) -> int:
    """Clears the arena: every tournament leaves the live set, its photos are deleted.

    Clearing one week at a time made "очистить" look broken -- it removed whatever
    latest_tournament pointed at, so a second tap silently ate the week BEFORE the one the
    admin meant. Starting over clears the lot. Returns how many were cleared.

    The tournament file is MOVED into archive_dir(), not unlinked. Unlike v1 the arena
    keeps no separate results record: its entries, ballots and standings all live in that
    one file, so deleting it really would erase the week's statistics.
    """
    cleared = 0
    destination_dir = archive_dir()
    for tournament_id in tournament_ids(entry):
        path = tournament_path(entry, tournament_id)
        destination = destination_dir / path.name
        try:
            destination_dir.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
                destination = destination_dir / f"{path.stem}_{stamp}{path.suffix}"
            path.replace(destination)
            cleared += 1
        except OSError:
            continue
        media_directory = media_path(entry, tournament_id)
        if media_directory.exists():
            shutil.rmtree(media_directory, ignore_errors=True)
        invalidate_standings(tournament_id)
    return cleared


def build_tournament(
    entry: str, tournament_id: str, entries: list, existing: Tournament | None = None
) -> Tournament:
    """A tournament for `entries`, keeping the moderation, ballots and settings of
    `existing` -- re-collecting must not undo admitting or throw away votes already cast.
    Anything no longer among the entries drops out of the admitted set; ballots keep their
    pairs untouched, because a ballot has to stay interpretable (compute_standings simply
    skips a pick naming a work that has gone)."""
    now = datetime.now(timezone.utc).isoformat()
    tournament = Tournament(
        tournament_id=tournament_id,
        entry=entry,
        created_at=(existing.created_at if existing else now),
        entries=entries,
    )
    if existing is None:
        return tournament
    known = {e.entry_id for e in entries}
    tournament.approved = [e for e in existing.approved if e in known]
    tournament.ballots = existing.ballots
    tournament.pairs_per_voter = existing.pairs_per_voter
    tournament.pairing = existing.pairing
    tournament.open = existing.open
    return tournament


def set_approved(tournament: Tournament, entry_ids: list) -> Tournament:
    """Replaces the admitted set wholesale -- the moderation screen always submits the
    whole picture, so this cannot drift from what the moderator saw."""
    known = {e.entry_id for e in tournament.entries}
    tournament.approved = [e for e in dict.fromkeys(entry_ids) if e in known]
    return tournament


def import_entries_from_poll(tournament: Tournament, poll, copy_media=True) -> int:
    """Takes the works ADMITTED to a v1 poll into this tournament, photos and all, and
    returns how many were added.

    This is the one bridge between the two systems, and it runs one way, on demand, by
    copying: v1's poll is not touched, not read again afterwards, and its media directory
    is left alone. Anything already here is skipped, so pressing it twice is harmless.

    Only v1's ADMITTED works come across -- a nomination a moderator rejected there should
    not need rejecting again here. They still arrive UNadmitted in the arena: this system
    has its own moderation, and inheriting an admit decision made for a different vote is
    exactly the kind of quiet coupling that makes two systems one.
    """
    import voting

    known = {e.entry_id for e in tournament.entries}
    incoming = [e for e in poll.approved_entries() if e.entry_id not in known]
    if not incoming:
        return 0

    if copy_media:
        source = voting.media_path(poll.entry, poll.poll_id)
        target = media_path(tournament.entry, tournament.tournament_id)
        target.mkdir(parents=True, exist_ok=True)
        for item in incoming:
            for name in item.media:
                origin, destination = source / name, target / name
                if destination.exists() or not origin.is_file():
                    continue
                try:
                    shutil.copy2(origin, destination)
                except OSError:
                    continue  # one unreadable photo costs that card its picture, not the import

    tournament.entries = tournament.entries + incoming
    return len(incoming)


# ------------------------------------------------------------------------ session rules

# Serialises read-modify-write of a tournament, exactly as voting.poll_lock does for a
# poll and for the same reason: every pick is load -> mutate -> save on one event loop, and
# two arriving together would otherwise lose one of them.
arena_lock = asyncio.Lock()

# tournament_id -> {"at": monotonic, "votes_since": int, "standings": dict}
_standings_cache: dict = {}


def standings_cached(tournament: Tournament, force: bool = False) -> dict:
    """The fitted table, refitted at most every STANDINGS_CACHE_SECONDS or every
    STANDINGS_CACHE_VOTES picks. Adaptive pairing needs a table on every session start, and
    a full refit per vote is both wasteful and pointless -- one vote cannot move a pairing
    decision."""
    import time

    cached = _standings_cache.get(tournament.tournament_id)
    if (
        not force
        and cached
        and time.monotonic() - cached["at"] < STANDINGS_CACHE_SECONDS
        and cached["votes_since"] < STANDINGS_CACHE_VOTES
    ):
        return cached["standings"]
    standings = tournament.standings()
    _standings_cache[tournament.tournament_id] = {
        "at": time.monotonic(), "votes_since": 0, "standings": standings,
    }
    return standings


def _note_vote(tournament_id: str) -> None:
    cached = _standings_cache.get(tournament_id)
    if cached:
        cached["votes_since"] = cached.get("votes_since", 0) + 1


def invalidate_standings(tournament_id: str) -> None:
    """Drops the cached table outright -- for changes that make it wrong rather than
    merely stale (admitting or un-admitting a work, or clearing the tournament)."""
    _standings_cache.pop(tournament_id, None)


def start_session(tournament: Tournament, user_id, name: str = "") -> Ballot:
    """Open, or resume, this voter's ballot. One voter, one ballot, for ever.

    A ballot already in progress comes back with its pairs and picks intact, so a refresh
    (or a phone that went to sleep mid-vote) continues instead of dealing a new set --
    re-dealing would let somebody keep re-rolling until they got a matchup they liked.
    A FINISHED ballot is refused outright: reopening one is worse than losing a vote.
    """
    key = str(user_id)
    existing = tournament.ballots.get(key)
    if existing and existing.status == "done":
        raise ArenaError("ALREADY_VOTED", "Ты уже проголосовал в этой арене.")
    if existing:
        return existing
    if not tournament.open:
        raise ArenaError("VOTING_CLOSED", "Арена закрыта.")

    entry_ids = [e.entry_id for e in tournament.approved_entries()]
    if len(entry_ids) < 2:
        raise ArenaError("NOT_ENOUGH_WORKS", "В арене меньше двух работ -- сравнивать нечего.")

    if tournament.pairing == "adaptive":
        finished = sum(1 for b in tournament.ballots.values() if b.status == "done")
        pairs = arena_core.build_pairs_adaptive(
            entry_ids, tournament.pairs_per_voter,
            standings=standings_cached(tournament), ballots_so_far=finished,
        )
    else:
        pairs = arena_core.build_pairs_random(entry_ids, tournament.pairs_per_voter)

    ballot = Ballot(
        user_id=key,
        name=name,
        pairs=[list(pair) for pair in pairs],
        picks=[],
        status="active",
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    tournament.ballots[key] = ballot
    return ballot


def record_pick(tournament: Tournament, user_id, position: int, pick: str) -> Ballot:
    """Record one judgement.

    `position` is the pair the client believes it is answering. A submit for anywhere else
    is returned unchanged rather than applied: a double tap, a retry on a flaky connection
    and a stale tab all land here, and any of them counting twice would be a fabricated
    vote. The client resyncs from the ballot it gets back.
    """
    ballot = tournament.ballots.get(str(user_id))
    if ballot is None:
        raise ArenaError("NO_SESSION", "Сессия не найдена -- открой арену заново.")
    if ballot.status == "done":
        raise ArenaError("BALLOT_COMPLETE", "Твой бюллетень уже закрыт.")
    if not tournament.open:
        raise ArenaError("VOTING_CLOSED", "Арена закрыта.")

    if position != ballot.position:
        return ballot  # duplicate or out of order; nothing recorded

    if position >= len(ballot.pairs):
        raise ArenaError("BALLOT_COMPLETE", "Пар больше нет.")
    pair = ballot.pairs[position]
    if pick != TIE and pick not in pair:
        raise ArenaError("BAD_PICK", "Этот вариант не из этой пары.")

    ballot.picks = ballot.picks + [pick]
    if len(ballot.picks) >= len(ballot.pairs):
        ballot.status = "done"
        ballot.done_at = datetime.now(timezone.utc).isoformat()
    _note_vote(tournament.tournament_id)
    return ballot


def undo_pick(tournament: Tournament, user_id) -> Ballot:
    """Take back the last judgement, so a mistap can be corrected.

    Repeatable all the way back to the first pair: the picks are a plain list, `position`
    is derived from its length, and nothing downstream remembers the order votes arrived in
    (compute_standings refits from the whole table every time), so dropping the tail IS the
    undo -- there is no incremental state to unwind.

    A DONE ballot is refused, exactly as start_session refuses to resume one. "One voter,
    one ballot, for ever" is the rule the arena is built on, and an undo that reopened a
    closed ballot would be a reopen under another name. In practice that means every pair
    can be taken back except the one that finished the ballot.

    The cached table is dropped rather than left stale: it still counts a vote that no
    longer exists, and adaptive pairing would go on seeding pairs from it.
    """
    ballot = tournament.ballots.get(str(user_id))
    if ballot is None:
        raise ArenaError("NO_SESSION", "Сессия не найдена -- открой арену заново.")
    if ballot.status == "done":
        raise ArenaError("BALLOT_COMPLETE", "Бюллетень уже закрыт -- вернуться нельзя.")
    if not tournament.open:
        raise ArenaError("VOTING_CLOSED", "Арена закрыта.")
    if not ballot.picks:
        raise ArenaError("NOTHING_TO_UNDO", "Это первая пара -- назад некуда.")

    ballot.picks = ballot.picks[:-1]
    invalidate_standings(tournament.tournament_id)
    return ballot
