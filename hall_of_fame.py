"""The Hall of Fame -- «Доска почёта»: every contest /vote has closed, kept for good, and a
page per artist built from them. The website is hall_web.py.

A CONTEST is one closed v1 poll: the weekly #итогинедели vote, or a thematic contest
collected under its own hashtag (voting.poll_id_for). It is recorded the moment its vote
is closed (record_poll, called from bot_listener) with every admitted work, its place and
its votes, so the chronology shows the whole field and not only the podium.

THE PHOTOS ARE COPIED, the same decision nominations.py made for the same reason: v1's
"очистить" archives a poll and deletes its photos, and a hall that pointed at v1's media
would lose every picture the first time anybody started a new week. Each work's first
photo also gets a square thumbnail, framed the way the administrator framed it for the
board picture (voting.Poll.crops) or filling the square when nobody did -- a gallery of
full-size phone photos would be megabytes per screen.

History from before the hall existed comes in through import_history: the announced
results records under voting/results survive a clear, and so does every closed poll still
on disk. A week whose photos were cleared before the hall existed comes in without them --
the names, places and votes are all still true.

Storage is DATA_DIR/hall_of_fame/<chat key>/: one JSON file per contest under contests/,
the pictures under media/<contest id>/. Reads go through a cache keyed on the files' own
names, sizes and mtimes, so a page view costs a directory listing rather than a parse of
every contest. Every function here is synchronous and touches the disk: the web layer runs
them in a worker thread, never on the event loop.
"""

import hashlib
import json
import math
import os
import re
import shutil
import threading
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import voting

DATA_DIR = Path(os.getenv("DATA_DIR", "."))
# Its own tree, beside voting/ and nominations/ rather than inside either: the hall must
# outlive both systems' clears.
HALL_DIR = DATA_DIR / "hall_of_fame"

# A square cover per work: three across a phone at 3x density is about this many pixels.
THUMB_SIDE = 480
THUMB_PREFIX = "t_"
THUMB_QUALITY = 82
# Behind a letterboxed crop -- the board picture's thumbnail colour (vote_image.THUMB_BG).
THUMB_BACKGROUND = (26, 37, 50)
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")

# The badge the weekly contest's winner earns. A thematic contest's winner earns the
# contest's own badge, or voting.DEFAULT_THEME_BADGE when it was given none.
WEEKLY_BADGE = "🏆"


def _hall_dir() -> Path:
    """Indirection purely for tests, like voting._voting_dir."""
    return HALL_DIR


def _key(entry: str) -> str:
    normalized = unicodedata.normalize("NFKC", entry or "").strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def contests_dir(entry: str) -> Path:
    return _hall_dir() / _key(entry) / "contests"


def contest_path(entry: str, contest_id: str) -> Path:
    return contests_dir(entry) / f"{contest_id}.json"


def media_dir(entry: str, contest_id: str) -> Path:
    return _hall_dir() / _key(entry) / "media" / contest_id


def _int_or_none(value) -> int | None:
    try:
        return None if value is None or value == "" else int(value)
    except (TypeError, ValueError):
        return None


def author_key(author_id, author_name: str) -> str:
    """Who a work is by, as one token for a URL: the Telegram id when there is one (a name
    changes, an id does not), else a hash of the name for a post whose sender was hidden."""
    if _int_or_none(author_id) is not None:
        return str(_int_or_none(author_id))
    name = unicodedata.normalize("NFKC", author_name or "").strip().lower()
    return "n" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:10]


# ------------------------------------------------------------------------------ records


@dataclass
class Work:
    entry_id: str
    place: int
    votes: int
    author_id: int | None
    author_name: str
    author_username: str | None = None
    text: str = ""
    posted_at: str = ""
    message_id: int = 0
    photos: list[str] = field(default_factory=list)   # under media_dir(entry, contest_id)
    thumb: str | None = None                          # the first photo's square cover

    @property
    def key(self) -> str:
        return author_key(self.author_id, self.author_name)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "Work":
        return cls(
            entry_id=str(raw.get("entry_id") or ""),
            place=int(raw.get("place") or 0),
            votes=int(raw.get("votes") or 0),
            author_id=_int_or_none(raw.get("author_id")),
            author_name=str(raw.get("author_name") or "Unknown"),
            author_username=raw.get("author_username") or None,
            text=str(raw.get("text") or ""),
            posted_at=str(raw.get("posted_at") or ""),
            message_id=int(raw.get("message_id") or 0),
            photos=[str(name) for name in raw.get("photos") or [] if _SAFE_NAME.match(str(name))],
            thumb=(str(raw["thumb"]) if raw.get("thumb") and _SAFE_NAME.match(str(raw["thumb"])) else None),
        )


@dataclass
class Contest:
    contest_id: str          # the poll id it was recorded from
    entry: str
    hashtag: str = voting.CONTEST_HASHTAG
    title: str = ""
    badge: str = ""
    created_at: str = ""
    closed_at: str = ""
    voters: int = 0
    works: list[Work] = field(default_factory=list)   # in place order

    @property
    def is_weekly(self) -> bool:
        return voting.is_weekly_hashtag(self.hashtag)

    def label(self) -> str:
        return voting.contest_title(self.hashtag, self.title)

    def winner_badge(self) -> str:
        if self.is_weekly:
            return WEEKLY_BADGE
        return self.badge or voting.DEFAULT_THEME_BADGE

    def winner(self) -> Work | None:
        """First place, provided anybody voted for it -- a contest nobody voted in has a
        field but no winner, the same rule voting.close_and_announce applies."""
        if self.works and self.works[0].votes > 0:
            return self.works[0]
        return None

    def podium(self) -> list[Work]:
        return [work for work in self.works[:3] if work.votes > 0]

    def on_podium(self, work: Work) -> bool:
        return any(placed is work for placed in self.podium())

    def week(self) -> str:
        """The ISO week the poll id names ("2026-W41"), whatever slug follows it."""
        match = re.match(r"^\d{4}-W\d{2}", self.contest_id)
        return match.group(0) if match else ""

    def when(self) -> str:
        return self.closed_at or self.created_at

    def to_dict(self) -> dict:
        return {
            "contest_id": self.contest_id,
            "entry": self.entry,
            "hashtag": self.hashtag,
            "title": self.title,
            "badge": self.badge,
            "created_at": self.created_at,
            "closed_at": self.closed_at,
            "voters": self.voters,
            "works": [work.to_dict() for work in self.works],
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Contest":
        works = [Work.from_dict(item) for item in raw.get("works") or [] if isinstance(item, dict)]
        works.sort(key=lambda work: work.place)
        return cls(
            contest_id=str(raw.get("contest_id") or ""),
            entry=str(raw.get("entry") or ""),
            hashtag=voting.normalize_hashtag(raw.get("hashtag")) or voting.CONTEST_HASHTAG,
            title=str(raw.get("title") or ""),
            badge=str(raw.get("badge") or ""),
            created_at=str(raw.get("created_at") or ""),
            closed_at=str(raw.get("closed_at") or ""),
            voters=int(raw.get("voters") or 0),
            works=works,
        )


# One writer at a time: recording runs in worker threads (the close of a vote, an import),
# and two of them writing the same contest must not interleave their photo copies.
_write_lock = threading.Lock()


def save_contest(contest: Contest) -> Path:
    path = contest_path(contest.entry, contest.contest_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(contest.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def load_contest(entry: str, contest_id: str) -> Contest | None:
    if not _SAFE_NAME.match(contest_id or ""):
        return None
    path = contest_path(entry, contest_id)
    if not path.exists():
        return None
    try:
        return Contest.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None


# ------------------------------------------------------------------------ photos


def _copy_photos(names: list[str], source: Path, target: Path) -> list[str]:
    """The names that are now in `target`: copied from `source`, or already there from an
    earlier recording. A photo that is in neither is simply not part of the record."""
    present = []
    for name in names:
        if not _SAFE_NAME.match(name or ""):
            continue
        destination = target / name
        if not destination.exists():
            origin = source / name
            if not origin.is_file():
                continue
            try:
                target.mkdir(parents=True, exist_ok=True)
                shutil.copy2(origin, destination)
            except OSError:
                continue  # one unreadable photo costs that work a picture, not the record
        present.append(name)
    return present


def _square(image, crop: dict | None, side: int):
    """`image` as a side x side square: through the administrator's crop (a square in the
    photo's own pixels that may hang off its edges -- see voting.Poll.crops) when there is
    one, otherwise filling the square from the centre."""
    from PIL import Image, ImageOps

    if not crop:
        return ImageOps.fit(image, (side, side), Image.LANCZOS)
    x, y, size = float(crop["x"]), float(crop["y"]), float(crop["size"])
    scale = side / size
    square = Image.new("RGB", (side, side), THUMB_BACKGROUND)
    left, top = max(0, math.floor(x)), max(0, math.floor(y))
    right, bottom = min(image.width, math.ceil(x + size)), min(image.height, math.ceil(y + size))
    if right <= left or bottom <= top:
        return square
    region = image.crop((left, top, right, bottom))
    region = region.resize((max(1, round(region.width * scale)), max(1, round(region.height * scale))),
                           Image.LANCZOS)
    square.paste(region, (round((left - x) * scale), round((top - y) * scale)))
    return square


def _make_thumb(directory: Path, name: str, crop: dict | None) -> str | None:
    """Writes the square cover for photo `name` and returns its file name, or None when the
    photo cannot be read -- the page then shows the photo itself."""
    try:
        from PIL import Image, ImageOps

        with Image.open(directory / name) as opened:
            # Rotation first, as the browser and the board renderer both do: a crop is in
            # the coordinates of the photo the way it is SHOWN.
            rotated = (ImageOps.exif_transpose(opened) or opened).convert("RGB")
            thumb = _square(rotated, crop, THUMB_SIDE)
        thumb_name = THUMB_PREFIX + name
        temporary = directory / (thumb_name + ".tmp")
        thumb.save(temporary, "JPEG", quality=THUMB_QUALITY, optimize=True)
        temporary.replace(directory / thumb_name)
        return thumb_name
    except Exception:  # noqa: BLE001 -- a broken photo must not cost the contest its record
        return None


# --------------------------------------------------------------------------- recording


def _record(
    entry: str, contest_id: str, standings: list, *, hashtag: str, title: str, badge: str,
    created_at: str, closed_at: str, voters: int, source: Path, crops: dict,
) -> Contest:
    target = media_dir(entry, contest_id)
    works = []
    for place, (work, votes) in enumerate(standings, start=1):
        photos = _copy_photos(list(work.media), source, target)
        thumb = _make_thumb(target, photos[0], crops.get(work.entry_id)) if photos else None
        works.append(Work(
            entry_id=str(work.entry_id), place=place, votes=int(votes),
            author_id=work.author_id, author_name=work.author_name,
            author_username=work.author_username, text=work.text, posted_at=work.posted_at,
            message_id=int(work.message_id or 0), photos=photos, thumb=thumb,
        ))
    contest = Contest(
        contest_id=contest_id, entry=entry, hashtag=hashtag, title=title, badge=badge,
        created_at=created_at, closed_at=closed_at, voters=voters, works=works,
    )
    save_contest(contest)
    return contest


def record_poll(poll: "voting.Poll", standings: list | None = None, closed_at: str | None = None) -> Contest:
    """Writes `poll` into the hall as a finished contest and returns the record.

    `standings` is poll.tally() -- every admitted work, best first -- and is taken as given
    so the hall shows exactly the board that was announced. Recording the same poll again
    (a vote re-closed after an admission changed) replaces its record: the newer close is
    the truth, as it is for voting.save_results. Photos already copied are kept, so a
    re-record after "очистить" does not lose them."""
    standings = poll.tally() if standings is None else standings
    with _write_lock:
        return _record(
            poll.entry, poll.poll_id, standings,
            hashtag=poll.hashtag, title=poll.title, badge=poll.badge,
            created_at=poll.created_at,
            closed_at=closed_at or datetime.now(timezone.utc).isoformat(),
            voters=len(poll.votes),
            source=voting.media_path(poll.entry, poll.poll_id), crops=poll.crops,
        )


def import_history(entry: str) -> dict:
    """Brings every contest the hall does not have yet in from /vote's own records, and
    says how many: {"added", "known", "without_photos"}.

    Two sources. The announced results records (voting.save_results), which a clear keeps;
    and closed polls still on disk that were never announced through the bot. A contest
    already in the hall is never overwritten from here -- its record is newer than any
    file this reads.

    Photos come from the poll's media directory while it still exists. A week cleared
    before the hall existed has lost them and comes in without pictures, counted in
    `without_photos` so whoever ran the import is told rather than left to find blank
    cards."""
    added = known = without_photos = 0
    with _write_lock:
        recorded = {path.stem for path in contests_dir(entry).glob("*.json")} if contests_dir(entry).exists() else set()
        for poll_id in voting.results_poll_ids(entry):
            if poll_id in recorded:
                known += 1
                continue
            record = voting.load_results(entry, poll_id)
            if not record or not _SAFE_NAME.match(poll_id):
                continue
            poll = voting.load_poll(entry, poll_id) or voting.load_archived_poll(entry, poll_id)
            by_id = {work.entry_id: work for work in poll.entries} if poll else {}
            standings = []
            for row in record.get("standings") or []:
                if not isinstance(row, dict):
                    continue
                entry_id = str(row.get("entry_id") or "")
                live = by_id.get(entry_id)
                standings.append((voting.Entry(
                    entry_id=entry_id,
                    message_id=int(row.get("message_id") or (live.message_id if live else 0) or 0),
                    author_id=_int_or_none(row.get("author_id")),
                    author_name=row.get("author_name") or "Unknown",
                    author_username=row.get("author_username"),
                    text=row.get("text") or "",
                    media=list(row.get("media") or []),
                    posted_at=row.get("posted_at") or (live.posted_at if live else ""),
                ), int(row.get("votes") or 0)))
            if not standings:
                continue
            contest = _record(
                entry, poll_id, standings,
                hashtag=(voting.normalize_hashtag(record.get("hashtag"))
                         or (poll.hashtag if poll else voting.CONTEST_HASHTAG)),
                title=record.get("title") or (poll.title if poll else ""),
                badge=record.get("badge") or (poll.badge if poll else ""),
                created_at=record.get("created_at") or (poll.created_at if poll else ""),
                closed_at=record.get("announced_at") or "",
                voters=int(record.get("voters") or 0),
                source=voting.media_path(entry, poll_id), crops=poll.crops if poll else {},
            )
            recorded.add(poll_id)
            added += 1
            if not any(work.photos for work in contest.works):
                without_photos += 1
        for poll_id in voting.poll_ids(entry):
            if poll_id in recorded:
                continue
            poll = voting.load_poll(entry, poll_id)
            if poll is None or poll.open or not poll.winner_entry_id or not _SAFE_NAME.match(poll_id):
                continue
            _record(
                entry, poll_id, poll.tally(), hashtag=poll.hashtag, title=poll.title,
                badge=poll.badge, created_at=poll.created_at, closed_at=poll.created_at,
                voters=len(poll.votes), source=voting.media_path(entry, poll_id), crops=poll.crops,
            )
            recorded.add(poll_id)
            added += 1
    return {"added": added, "known": known, "without_photos": without_photos}


# ------------------------------------------------------------------------------ reading


@dataclass
class Artist:
    key: str
    author_id: int | None
    name: str
    username: str | None
    # (contest, work), newest contest first.
    entries: list[tuple[Contest, Work]] = field(default_factory=list)

    @property
    def wins(self) -> list[Contest]:
        return [contest for contest, work in self.entries if contest.winner() is work]

    @property
    def podiums(self) -> int:
        return sum(1 for contest, work in self.entries if contest.on_podium(work))

    @property
    def total_votes(self) -> int:
        return sum(work.votes for _, work in self.entries)

    def best_place(self) -> int | None:
        places = [work.place for contest, work in self.entries if contest.on_podium(work)]
        return min(places) if places else None


@dataclass
class Hall:
    contests: list[Contest]            # newest first
    artists: list[Artist]              # the leaderboard's order
    by_key: dict[str, Artist]
    by_id: dict[str, Contest]


def _rank(artist: Artist) -> tuple:
    return (-len(artist.wins), -artist.podiums, -artist.total_votes, -len(artist.entries),
            artist.name.lower())


def build_hall(contests: list[Contest]) -> Hall:
    """Everything the site shows, from the contests alone. Pure, so it can be tested
    without a disk; snapshot() caches its result."""
    ordered = sorted(contests, key=lambda c: (c.when(), c.contest_id), reverse=True)
    by_key: dict[str, Artist] = {}
    for contest in ordered:
        for work in contest.works:
            artist = by_key.get(work.key)
            if artist is None:
                # Newest contest first, so the name and @tag shown are the current ones.
                artist = by_key[work.key] = Artist(
                    key=work.key, author_id=work.author_id, name=work.author_name,
                    username=work.author_username,
                )
            artist.entries.append((contest, work))
    artists = sorted(by_key.values(), key=_rank)
    return Hall(contests=ordered, artists=artists, by_key=by_key,
                by_id={contest.contest_id: contest for contest in ordered})


_cache: dict[str, tuple[tuple, Hall]] = {}
_cache_lock = threading.Lock()


def snapshot(entry: str) -> Hall:
    """The hall as it is on disk now, parsed only when a contest file has changed since
    the last call. Callers must treat the result as read-only: it is shared."""
    directory = contests_dir(entry)
    files = []
    if directory.exists():
        for path in directory.glob("*.json"):
            try:
                stat = path.stat()
            except OSError:
                continue  # replaced between the listing and the stat; the next call sees it
            files.append((path.name, stat.st_mtime_ns, stat.st_size))
    signature = (str(directory), tuple(sorted(files)))
    with _cache_lock:
        cached = _cache.get(entry)
        if cached is not None and cached[0] == signature:
            return cached[1]
    contests = []
    for name, _, _ in signature[1]:
        try:
            contests.append(Contest.from_dict(json.loads((directory / name).read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            continue  # one corrupt record must not take the whole hall down
    hall = build_hall(contests)
    with _cache_lock:
        _cache[entry] = (signature, hall)
    return hall


def media_file(entry: str, contest_id: str, name: str) -> Path | None:
    """The picture file for a URL, or None -- strict names AND a containment check, the
    same two-step guard as vote_web.handle_media."""
    if not _SAFE_NAME.match(contest_id or "") or not _SAFE_NAME.match(name or ""):
        return None
    directory = media_dir(entry, contest_id)
    path = (directory / name).resolve()
    if not str(path).startswith(str(directory.resolve()) + os.sep) or not path.is_file():
        return None
    return path
