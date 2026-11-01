"""«Лучшая работа месяца»: the month's three votes -- bronze, silver and gold -- between
the weekly contests' medallists.

Each round is an ordinary /vote poll (voting.Poll) under voting.MONTHLY_HASHTAG: the bronze
round puts every week's third place to the vote, the silver round the second places, the
gold round the winners, and the round's winner takes that round's medal (Poll.tier, which
the Hall of Fame records as Contest.tier). The works -- photos, the administrator's
framing, authors, captions -- come from the weeks' own records in the Hall of Fame, which
keeps a copy of every closed week. Nothing is read from the chat again.

WHICH WEEKS. A week belongs to the month of the Sunday its vote was held on (week_sunday):
October's rounds are the weeks of 4, 11, 18 and 25 October. A week of the month before
comes in too when it was still running when that month's rounds were put together:
September's rounds were run on 22 September, so the week of 27 September never had a
monthly round and goes into October's. The administrator sees the list and can tick a
week in or out before anything is created (plan_month, then create_rounds).

ONE ROUND AT A TIME. The voting page shows one poll, voting.latest_poll's, so the three
rounds are created together and run as a queue -- bronze, then silver, then gold. Bronze
is created newest, so it is the one the page opens; when a round is closed,
start_next_round makes the next one the newest, so the page moves on to it, and the last
one closed stays on the page with its result.

Everything here is synchronous and touches the disk: the bot runs it in a worker thread.
"""

import hashlib
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import hall_of_fame
import voting

TITLE = "Лучшая работа месяца"
# As the archived rounds' titles spell them: "Лучшая работа месяца · июль · бронза".
MONTH_NAMES = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
               "сентябрь", "октябрь", "ноябрь", "декабрь")
# "неделя сентября"
MONTH_NAMES_GENITIVE = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
                        "сентября", "октября", "ноября", "декабря")
# The channel's own monthly polls took one answer each, so a round does too. The
# moderation screen can still change it.
MAX_CHOICES = 1
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class Round:
    key: str      # in the poll id: "2026-10-bronze"
    tier: int     # the medal its winner takes: 1 gold, 2 silver, 3 bronze
    name: str     # in the title
    badge: str


# The order the page runs them in. Every week's place `tier` goes into that round.
ROUNDS = (
    Round("bronze", 3, "бронза", "🥉"),
    Round("silver", 2, "серебро", "🥈"),
    Round("gold", 1, "золото", "🥇"),
)
_ROUND_ID = re.compile(r"^(\d{4})-(\d{2})-(bronze|silver|gold)$")


def month_id(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def round_poll_id(year: int, month: int, round_: Round) -> str:
    """The same ids as the archived rounds ("2026-07-gold"), so the hall reads alike."""
    return f"{month_id(year, month)}-{round_.key}"


def round_title(month: int, round_: Round) -> str:
    return f"{TITLE} · {MONTH_NAMES[month - 1]} · {round_.name}"


def parse_round_id(poll_id: str) -> tuple[int, int, Round] | None:
    match = _ROUND_ID.match(poll_id or "")
    if match is None:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return year, month, next(r for r in ROUNDS if r.key == match.group(3))


def previous_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _local_date(stamp: str, tz) -> date | None:
    try:
        moment = datetime.fromisoformat(stamp or "")
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz).date()


def nearest_sunday(day: date) -> date:
    """The Sunday closest to `day`: back to it from Monday to Wednesday, on to the next
    one from Thursday to Saturday."""
    since_sunday = (day.weekday() + 1) % 7
    if since_sunday <= 3:
        return day - timedelta(days=since_sunday)
    return day + timedelta(days=7 - since_sunday)


def week_sunday(contest: hall_of_fame.Contest, tz) -> date | None:
    """The Sunday a weekly contest's vote was held on.

    Read from when the vote was put together, not from its id: the id is the ISO week of
    the collect, and a vote collected on the Monday after its week ends carries the NEXT
    week's number. The vote is run around the turn of the week, so the Sunday nearest to
    its start is the one it belongs to. The id's own Sunday is the fallback for a record
    without a usable time."""
    held = _local_date(contest.created_at, tz)
    if held is not None:
        return nearest_sunday(held)
    week = re.match(r"^(\d{4})-W(\d{2})", contest.contest_id or "")
    if week is not None:
        try:
            return date.fromisocalendar(int(week.group(1)), int(week.group(2)), 7)
        except ValueError:
            pass
    closed = _local_date(contest.closed_at, tz)
    return nearest_sunday(closed) if closed is not None else None


# ------------------------------------------------------------------------- the plan


@dataclass
class Week:
    """One weekly contest that may go into the month: its medallists by medal."""
    contest: hall_of_fame.Contest
    sunday: date
    medals: dict[int, hall_of_fame.Work]
    # Ticked unless the administrator says otherwise.
    selected: bool = True
    # A week of the month before that was still running when that month's rounds began.
    carried: bool = False


@dataclass
class RoundState:
    round: Round
    poll_id: str
    poll: voting.Poll | None = None                 # the live poll, if there is one
    recorded: hall_of_fame.Contest | None = None    # in the Hall of Fame, once closed

    @property
    def status(self) -> str:
        """'missing', 'pending' (open, nobody has voted), 'voting', 'closed' -- or
        'recorded' for a round in the hall whose poll has since been cleared."""
        if self.poll is None:
            return "recorded" if self.recorded is not None else "missing"
        if not self.poll.open:
            return "closed"
        return "voting" if self.poll.votes else "pending"

    @property
    def can_build(self) -> bool:
        """Whether create_rounds may (re)build it: never over a vote somebody has cast in,
        nor over a round that has been decided."""
        return self.status in ("missing", "pending")


@dataclass
class MonthPlan:
    year: int
    month: int
    weeks: list[Week]                 # oldest first
    running: list[date]               # this month's weekly votes with no result yet
    rounds: list[RoundState]          # in ROUNDS order
    previous_started: date | None     # when the month before's rounds were created
    shown: voting.Poll | None = None  # what the voting page opens right now

    def default_mask(self) -> int:
        return sum(1 << index for index, week in enumerate(self.weeks) if week.selected)

    def selected(self, mask: int | None = None) -> list[Week]:
        mask = self.default_mask() if mask is None else mask
        return [week for index, week in enumerate(self.weeks) if mask >> index & 1]

    def fingerprint(self) -> str:
        """Four hex characters naming the list of weeks a mask was made for, so a tick
        made before another week closed is never applied to a different list."""
        ids = ",".join(week.contest.contest_id for week in self.weeks)
        return hashlib.sha256(ids.encode("utf-8")).hexdigest()[:4]

    def label(self) -> str:
        return f"{MONTH_NAMES[self.month - 1]} {self.year}"


def _rounds_started(entry: str, hall: hall_of_fame.Hall, year: int, month: int, tz) -> date | None:
    """The day a month's rounds were first created, live or recorded -- None if it never
    had any."""
    days = []
    for round_ in ROUNDS:
        poll_id = round_poll_id(year, month, round_)
        for record in (hall.by_id.get(poll_id), voting.load_poll(entry, poll_id)):
            day = _local_date(record.created_at, tz) if record is not None else None
            if day is not None:
                days.append(day)
    return min(days) if days else None


def _running_weeks(entry: str, hall: hall_of_fame.Hall, year: int, month: int, tz) -> list[date]:
    """This month's weekly votes that are still on: their podium is not known yet."""
    sundays = set()
    for poll_id in voting.poll_ids(entry):
        if poll_id in hall.by_id:
            continue
        poll = voting.load_poll(entry, poll_id)
        if poll is None or not poll.is_weekly or not poll.open or not poll.approved:
            continue
        held = _local_date(poll.created_at, tz)
        sunday = nearest_sunday(held) if held is not None else None
        if sunday is not None and (sunday.year, sunday.month) == (year, month):
            sundays.add(sunday)
    return sorted(sundays)


def plan_month(entry: str, year: int, month: int, tz) -> MonthPlan:
    """Which weeks go into `month`'s rounds, who their medallists are, and how far each
    round has got. Reads the hall and the live polls; writes nothing."""
    hall = hall_of_fame.snapshot(entry)
    previous = previous_month(year, month)
    previous_started = _rounds_started(entry, hall, *previous, tz)
    weeks = []
    for contest in hall.contests:
        if not contest.is_weekly:
            continue
        sunday = week_sunday(contest, tz)
        if sunday is None:
            continue
        if (sunday.year, sunday.month) == (year, month):
            selected, carried = True, False
        elif (sunday.year, sunday.month) == previous and (
            previous_started is None or sunday >= previous_started
        ):
            # Past the month before's rounds: it never had a monthly round, so it is
            # this month's. When the month before had no rounds at all it is offered, not
            # ticked -- those weeks are that month's to run.
            carried = previous_started is not None
            selected = carried
        else:
            continue
        medals = {contest.medal_for(work): work for work in contest.podium()}
        if not medals:
            continue  # nobody voted that week: there is nobody to bring
        weeks.append(Week(contest=contest, sunday=sunday, medals=medals,
                          selected=selected, carried=carried))
    weeks.sort(key=lambda week: (week.sunday, week.contest.contest_id))
    rounds = []
    for round_ in ROUNDS:
        poll_id = round_poll_id(year, month, round_)
        rounds.append(RoundState(round=round_, poll_id=poll_id, poll=voting.load_poll(entry, poll_id),
                                 recorded=hall.by_id.get(poll_id)))
    return MonthPlan(
        year=year, month=month, weeks=weeks,
        running=_running_weeks(entry, hall, year, month, tz),
        rounds=rounds, previous_started=previous_started, shown=voting.latest_poll(entry),
    )


# ------------------------------------------------------------------------ the rounds


def _entry_id(contest: hall_of_fame.Contest, work: hall_of_fame.Work) -> str:
    """A number, as every v1 entry id is (the browser page's ballot link writes them in
    base 36): the post's message id, which is what a collected work's id already is. An
    archived work without one gets a stable number of its own."""
    if work.message_id:
        return str(work.message_id)
    if work.entry_id.isdigit():
        return work.entry_id
    digest = hashlib.sha256(f"{contest.contest_id}:{work.entry_id}".encode("utf-8")).hexdigest()
    return str(int(digest[:10], 16))


def _copy_photos(names: list[str], sources: list[Path], target: Path, prefix: str) -> list[str]:
    """`names` copied out of the first of `sources` that has each, into `target` as
    "<prefix>_<n><suffix>": two weeks' photos may share a file name, and one entry id
    always lands on the same names, so a rebuild finds its own files already there. A
    hardlink where the disk allows one, as the hall does: a photo is never rewritten."""
    copied = []
    for index, name in enumerate(names):
        if not _SAFE_NAME.match(name or ""):
            continue
        destination = target / f"{prefix}_{index}{Path(name).suffix or '.jpg'}"
        if not destination.is_file():
            origin = next((source / name for source in sources if (source / name).is_file()), None)
            if origin is None:
                continue
            try:
                target.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(origin, destination)
                except OSError:
                    shutil.copy2(origin, destination)
            except OSError:
                continue  # one unreadable photo costs that work a picture, not the round
        copied.append(destination.name)
    return copied


@dataclass
class RoundResult:
    round: Round
    poll_id: str
    # 'created', 'rebuilt', 'kept' (somebody voted, or it is decided), 'empty' (no week
    # had this place).
    outcome: str
    works: int = 0
    without_photos: list[str] = field(default_factory=list)   # "Имя (неделя 04.10)"


def _round_entries(entry: str, poll_id: str, round_: Round, weeks: list[Week]):
    target = voting.media_path(entry, poll_id)
    entries, crops, seen, without_photos = [], {}, set(), []
    for week in weeks:
        work = week.medals.get(round_.tier)
        if work is None:
            continue
        entry_id = _entry_id(week.contest, work)
        if entry_id in seen:
            continue  # the same post placed in two weeks is one card
        sources = [hall_of_fame.media_dir(entry, week.contest.contest_id),
                   *voting.photo_dirs(entry, week.contest.contest_id)]
        photos = _copy_photos(work.photos, sources, target, entry_id)
        if not photos:
            without_photos.append(f"{work.author_name} (неделя {week.sunday:%d.%m})")
            continue  # a card with no picture has nothing to vote on
        seen.add(entry_id)
        entries.append(voting.Entry(
            entry_id=entry_id, message_id=int(work.message_id or 0), author_id=work.author_id,
            author_name=work.author_name, author_username=work.author_username,
            text=work.text, media=photos, posted_at=work.posted_at,
        ))
        if work.crop:
            crops[entry_id] = dict(work.crop)
    return entries, crops, without_photos


def create_rounds(entry: str, plan: MonthPlan, weeks: list[Week], now: datetime | None = None) -> list[RoundResult]:
    """Creates the month's three rounds from `weeks` (plan.selected()), every work
    admitted and the vote open, and returns what became of each.

    A round somebody has voted in, or one already decided, is left exactly as it is. One
    nobody has voted in yet is rebuilt from `weeks` -- that is how a week that closed after
    the rounds were made gets into them -- keeping its place in the queue and its ballot
    settings.

    Callers hold voting.poll_lock: a round already on the page takes ballots."""
    now = now or datetime.now(timezone.utc)
    results = []
    for position, state in enumerate(plan.rounds):
        round_ = state.round
        if not state.can_build:
            results.append(RoundResult(round_, state.poll_id, "kept",
                                       works=len(state.poll.approved) if state.poll else 0))
            continue
        # Read again under the caller's lock: the plan was drawn up before it was taken.
        existing = voting.load_poll(entry, state.poll_id)
        if existing is not None and (existing.votes or not existing.open):
            results.append(RoundResult(round_, state.poll_id, "kept", works=len(existing.approved)))
            continue
        entries, crops, without_photos = _round_entries(entry, state.poll_id, round_, weeks)
        if not entries:
            results.append(RoundResult(round_, state.poll_id, "empty", without_photos=without_photos))
            continue
        # The queue is the creation order: the bronze round newest, so the page opens it,
        # then silver, then gold (see start_next_round).
        created_at = existing.created_at if existing else (now - timedelta(seconds=position)).isoformat()
        poll = voting.Poll(
            poll_id=state.poll_id, entry=entry, created_at=created_at, entries=entries,
            approved=[e.entry_id for e in entries], open=True,
            max_choices=existing.max_choices if existing else MAX_CHOICES,
            allow_revote=existing.allow_revote if existing else True,
            crops=crops, hashtag=voting.MONTHLY_HASHTAG, title=round_title(plan.month, round_),
            badge=round_.badge, tier=round_.tier,
        )
        voting.save_poll(poll)
        results.append(RoundResult(round_, state.poll_id, "rebuilt" if existing else "created",
                                   works=len(entries), without_photos=without_photos))
    return results


def start_next_round(entry: str, closed: voting.Poll) -> voting.Poll | None:
    """After `closed` -- a monthly round -- has been decided, hands the page to the next
    round of the same month that is still open, and returns it (None when there is none).

    Making it the newest is all it takes: of the polls people can vote in the page opens
    the newest (voting.latest_poll). Without it the gold round, created oldest, would end
    the month with the page showing bronze's result. Callers hold voting.poll_lock."""
    parsed = parse_round_id(closed.poll_id)
    if parsed is None or not closed.is_monthly:
        return None
    year, month, round_ = parsed
    for later in ROUNDS[ROUNDS.index(round_) + 1:]:
        poll = voting.load_poll(entry, round_poll_id(year, month, later))
        if poll is not None and poll.open and poll.approved:
            voting.make_current(poll)
            voting.save_poll(poll)
            return poll
    return None
