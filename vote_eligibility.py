"""Who may cast a ballot: a member of Папка художников who joined it at least a day ago.

The rule is about organised voting. A burst of accounts that join, vote for one work and
leave is what a rigged contest looks like (see vote_audit), and a day's wait between
joining and voting takes the point out of it while costing a real member nothing. It
applies to every vote the bot runs -- /vote in the Mini App and through its browser link,
the arena, the nominations -- and each of them asks it through bot_listener, which owns
the two Telegram clients the answer needs.

The community is Папка художников (`VOTE_MEMBER_CHAT`, `@papkahudojnicov` by default),
not the tracked chat the works are collected from: the people a contest is for are the
ones who follow it there.

Two questions, two sources:

- Is this person in it NOW? The Bot API's getChatMember. Exact and current, but it carries
  no join date at all.
- Since WHEN? The userbot (Telethon) can read the member list, and every record in it has
  the date that member joined. One read covers everybody in it, so it is kept (JoinDates)
  and read again only for somebody it does not list -- somebody who joined since -- and
  then no more often than once per REFRESH_SECONDS. A list Telegram cuts short (a
  channel's subscribers are not all handed out) cannot place somebody missing from it, so
  that person is looked up by name instead.

A check that cannot be completed refuses the ballot ("unverified") rather than letting it
through: a rule that quietly switches itself off whenever Telegram hiccups is not a rule.
Administrators and the creator are trusted and skip the join-date half.
"""

import asyncio
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# How long somebody must have been a member before their ballot counts.
MIN_MEMBERSHIP = timedelta(hours=24)

# Where somebody who is not a member is sent to join: every vote page's "Вступить" button
# and the browser link's reply. Opened inside Telegram, it is the chat's own screen with
# its own one-tap join -- no API joins a chat on a user's behalf.
JOIN_URL = "https://t.me/papkahudojnicov"

# The member list is read again at most this often, however many people it does not list
# ask in that time -- a wave of fresh joiners opening the page costs one read, not one each.
REFRESH_SECONDS = 30.0
# A userbot that cannot connect does not raise, it waits; a voter must not wait with it.
FETCH_TIMEOUT_SECONDS = 20.0
# Lookups by name run side by side up to this many: a crowd opening the page at once must
# not wait in single file, nor fire enough requests at once to earn a flood wait.
SEARCH_CONCURRENCY = 4

# Why a ballot is refused. None means it is not.
NOT_A_MEMBER = "not_member"
TOO_NEW = "too_new"
UNVERIFIED = "unverified"

# Statuses that are in the chat. "restricted" is decided by its own is_member flag.
_IN_CHAT_STATUSES = frozenset({"creator", "administrator", "member"})
_TRUSTED_STATUSES = frozenset({"creator", "administrator"})

# The join date given to a member record that has none -- only the creator's lacks one.
FOUNDING = datetime.min.replace(tzinfo=timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def in_chat(member: dict | None) -> bool:
    """Whether a getChatMember answer says this person is in the chat. "restricted" is
    special-cased because Telegram uses that one status for two situations: a member under
    restrictions (still in the chat) and somebody who was restricted and then left. Only
    the payload's is_member tells them apart."""
    if not isinstance(member, dict):
        return False
    status = member.get("status")
    if status == "restricted":
        return bool(member.get("is_member"))
    return status in _IN_CHAT_STATUSES


def _hours_left(eligible_at: datetime, now: datetime) -> str:
    seconds = (eligible_at - now).total_seconds()
    if seconds < 3600:
        return "меньше чем через час"
    return f"примерно через {math.ceil(seconds / 3600)} ч"


@dataclass(frozen=True)
class Verdict:
    """One person's standing for a ballot. `is_member` is also what a ballot records as
    its subscription snapshot, so a vote never needs a second membership lookup (None:
    nobody checked -- a page mounted without bot_listener)."""

    is_member: bool | None
    reason: str | None = None
    # For TOO_NEW: the earliest moment the ballot would count.
    eligible_at: datetime | None = None

    @property
    def can_vote(self) -> bool:
        return self.reason is None

    def message(self, now: datetime | None = None) -> str | None:
        """What the voter is told instead of their ballot being taken, or None."""
        if self.reason is None:
            return None
        if self.reason == NOT_A_MEMBER:
            return ("Голосовать могут только участники Папки художников, которые состоят "
                    "в ней не меньше суток.")
        if self.reason == TOO_NEW:
            text = "Голосовать можно через сутки после вступления в Папку художников."
            if self.eligible_at is not None:
                text += f" Ваш голос можно будет отдать {_hours_left(self.eligible_at, now or _utcnow())}."
            return text
        return ("Не получилось проверить, как давно вы в Папке художников. "
                "Попробуйте ещё раз чуть позже.")


ALLOWED = Verdict(is_member=True)


def membership_only(is_member):
    """A verdict from a plain membership answer, without the join-date half -- what a
    page mounted without bot_listener (a test, a local run) falls back to, since only
    bot_listener can read join dates."""
    async def verdict(user: dict) -> Verdict:
        if await is_member(user):
            return ALLOWED
        return Verdict(is_member=False, reason=NOT_A_MEMBER)
    return verdict


def search_queries(user: dict) -> list[str]:
    """What to look a voter up by in a member list Telegram will not hand over whole: the
    @username first (it names one account), then the full name, then the first name."""
    full_name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p)
    queries: list[str] = []
    for query in (user.get("username"), full_name, user.get("first_name")):
        query = (query or "").strip()
        if query and query not in queries:
            queries.append(query)
    return queries


@dataclass(frozen=True)
class MemberList:
    """One read of the member list: who was in it and since when. `complete` says the
    read saw everybody, which is what lets somebody missing from it be read as having
    joined since -- a group's list comes whole, a channel's is cut short by Telegram."""

    dates: dict = field(default_factory=dict)
    complete: bool = True


class JoinDates:
    """When each member joined, from the userbot's reads of the member list.

    `fetch` is an async callable returning a MemberList for the chat as it is right now;
    `search` takes a name and returns {user_id: joined_at} for the members it matches.
    Somebody already known is answered from memory -- a join date does not change while
    they stay, and whether they still do is getChatMember's question, not this one's.
    Somebody unknown makes the list be read again (one read at a time, at most once per
    `refresh_seconds`), then a search by their name. Missing from both: if the last read
    was complete they joined after it, reported as the moment it was read; if it was cut
    short there is no telling, and the lookup raises.

    The one thing a kept answer cannot see is a member who left and came back: they keep
    their first date. That is somebody who has already spent a day as a member once, which
    is not the account this rule is for.
    """

    def __init__(self, fetch, search=None, *, refresh_seconds: float = REFRESH_SECONDS,
                 timeout: float = FETCH_TIMEOUT_SECONDS, clock=time.monotonic, now=_utcnow,
                 log=print):
        self._fetch = fetch
        self._search = search
        self._refresh_seconds = refresh_seconds
        self._timeout = timeout
        self._clock = clock
        self._now = now
        self._log = log
        self._dates: dict[int, datetime] = {}
        # Answers a search found. Kept apart, so a fresh read of the list does not forget
        # the people it does not hold.
        self._found: dict[int, datetime] = {}
        self._complete = True
        self._read_at: float | None = None
        self._read_wall: datetime | None = None
        self._lock = asyncio.Lock()
        self._search_slots = asyncio.Semaphore(SEARCH_CONCURRENCY)

    def _known(self, user_id: int) -> datetime | None:
        known = self._dates.get(user_id)
        return known if known is not None else self._found.get(user_id)

    async def joined_at(self, user_id: int, queries=()) -> datetime:
        """This member's join date. `queries` are the names to search for them by (see
        search_queries). Raises if the list cannot be read, or cannot place them."""
        user_id = int(user_id)
        known = self._known(user_id)
        if known is not None:
            return known
        async with self._lock:
            # Whoever held the lock may have just read a list that has them.
            known = self._known(user_id)
            if known is not None:
                return known
            if self._read_at is None or self._clock() - self._read_at >= self._refresh_seconds:
                listing = await asyncio.wait_for(self._fetch(), timeout=self._timeout)
                self._dates = {int(k): v for k, v in listing.dates.items()}
                self._complete = listing.complete
                self._read_at = self._clock()
                self._read_wall = self._now()
                known = self._dates.get(user_id)
                if known is not None:
                    return known
            complete, read_wall = self._complete, self._read_wall
        found = await self._search_by_name(user_id, queries)
        if found is not None:
            return found
        if complete:
            # Expected for somebody who has just joined; for a long-standing member it
            # means the userbot cannot see the whole list (hidden members, say).
            self._log(f"[vote] {user_id} is a member but not in the member list read at "
                      f"{read_wall:%Y-%m-%d %H:%M} UTC -- counted as joined since")
            return read_wall
        raise LookupError(
            f"{user_id} is not in the part of the member list Telegram hands out, "
            f"and no search by name found them"
        )

    async def _search_by_name(self, user_id: int, queries) -> datetime | None:
        if self._search is None:
            return None
        for query in queries:
            try:
                async with self._search_slots:
                    found = await asyncio.wait_for(self._search(query), timeout=self._timeout)
            except Exception as e:  # noqa: BLE001 -- a failed search is a search that missed
                self._log(f"[vote] searching the member list for {query!r} failed: "
                          f"{type(e).__name__}: {e}")
                continue
            # Everybody a search turns up is somebody whose date is now known.
            self._found.update({int(k): v for k, v in found.items()})
            if user_id in self._found:
                return self._found[user_id]
        return None


async def judge(member: dict | None, user: dict, join_dates: JoinDates,
                now: datetime | None = None, log=print) -> Verdict:
    """The verdict for one person, from their getChatMember answer (None when Telegram
    said there is no such member) and the join dates."""
    if not in_chat(member):
        return Verdict(is_member=False, reason=NOT_A_MEMBER)
    if member.get("status") in _TRUSTED_STATUSES:
        return ALLOWED
    user_id = user["id"]
    try:
        joined = await join_dates.joined_at(user_id, search_queries(user))
    except Exception as e:  # noqa: BLE001 -- any failure to place them is "unverified"
        log(f"[vote] could not tell when {user_id} joined: {type(e).__name__}: {e}")
        return Verdict(is_member=True, reason=UNVERIFIED)
    if joined.tzinfo is None:
        joined = joined.replace(tzinfo=timezone.utc)
    eligible_at = joined + MIN_MEMBERSHIP
    if eligible_at <= (now or _utcnow()):
        return ALLOWED
    return Verdict(is_member=True, reason=TOO_NEW, eligible_at=eligible_at)


def join_date_of(participant) -> datetime | None:
    """A Telethon member record's join date: `date` on every kind a member list yields but
    the creator's, which has none and gets FOUNDING. A restricted record that has left is
    not a member, so None."""
    if participant is None or getattr(participant, "left", False):
        return None
    date = getattr(participant, "date", None)
    if date is None:
        return FOUNDING
    return date if date.tzinfo is not None else date.replace(tzinfo=timezone.utc)
