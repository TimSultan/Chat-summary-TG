"""Who may cast a ballot: a member of the home chat who joined it at least a day ago.

The rule is about organised voting. A burst of accounts that join the chat, vote for one
work and leave is what a rigged contest looks like (see vote_audit), and a day's wait
between joining and voting takes the point out of it while costing a real member nothing.
It applies to every vote the bot runs -- /vote in the Mini App and through its browser
link, the arena, the nominations -- and each of them asks it through bot_listener, which
owns the two Telegram clients the answer needs.

Two questions, two sources:

- Is this person in the chat NOW? The Bot API's getChatMember, as before. It is exact and
  current, but it carries no join date at all.
- Since WHEN? The userbot (Telethon) can read the chat's member list, and every record in
  it has the date that member joined. One read covers the whole chat, so it is kept
  (JoinDates) and read again only for somebody it does not yet list -- somebody who
  joined since -- and then no more often than once per REFRESH_SECONDS.

A check that cannot be completed refuses the ballot ("unverified") rather than letting it
through: a rule that quietly switches itself off whenever Telegram hiccups is not a rule.
Chat administrators and the creator are trusted and skip the join-date half.
"""

import asyncio
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# How long somebody must have been in the chat before their ballot counts.
MIN_MEMBERSHIP = timedelta(hours=24)

# The member list is read again at most this often, however many people it does not list
# ask in that time -- a wave of fresh joiners opening the page costs one read, not one each.
REFRESH_SECONDS = 30.0
# A userbot that cannot connect does not raise, it waits; a voter must not wait with it.
FETCH_TIMEOUT_SECONDS = 20.0

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
            return ("Голосовать могут только участники чата, которые состоят в нём "
                    "не меньше суток.")
        if self.reason == TOO_NEW:
            text = "Голосовать можно через сутки после вступления в чат."
            if self.eligible_at is not None:
                text += f" Ваш голос можно будет отдать {_hours_left(self.eligible_at, now or _utcnow())}."
            return text
        return "Не получилось проверить, как давно вы в чате. Попробуйте ещё раз чуть позже."


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


class JoinDates:
    """When each member of one chat joined it, from the userbot's member list.

    `fetch` is an async callable returning {user_id: joined_at} for the whole chat as it
    is right now. Somebody already listed is answered from memory -- a join date does not
    change while they stay, and whether they still do is getChatMember's question, not
    this one's. Somebody not listed means the list predates them, so it is read again --
    one read at a time, at most once per `refresh_seconds` -- and somebody missing from a
    list read a moment ago joined after it, which is reported as the moment it was read.

    The one thing a kept list cannot see is a member who left and came back: they keep
    their first date. That is somebody who has already spent a day in the chat once, which
    is not the account this rule is for.
    """

    def __init__(self, fetch, *, refresh_seconds: float = REFRESH_SECONDS,
                 timeout: float = FETCH_TIMEOUT_SECONDS, clock=time.monotonic, now=_utcnow,
                 log=print):
        self._fetch = fetch
        self._refresh_seconds = refresh_seconds
        self._timeout = timeout
        self._clock = clock
        self._now = now
        self._log = log
        self._dates: dict[int, datetime] = {}
        self._read_at: float | None = None
        self._read_wall: datetime | None = None
        self._lock = asyncio.Lock()

    async def joined_at(self, user_id: int) -> datetime:
        """This member's join date. Raises if the member list cannot be read."""
        user_id = int(user_id)
        known = self._dates.get(user_id)
        if known is not None:
            return known
        async with self._lock:
            # Whoever held the lock may have just read a list that has them.
            known = self._dates.get(user_id)
            if known is not None:
                return known
            if self._read_at is None or self._clock() - self._read_at >= self._refresh_seconds:
                dates = await asyncio.wait_for(self._fetch(), timeout=self._timeout)
                self._dates = {int(k): v for k, v in dates.items()}
                self._read_at = self._clock()
                self._read_wall = self._now()
                known = self._dates.get(user_id)
                if known is not None:
                    return known
            # Expected for somebody who has just joined. For long-standing members it
            # means the userbot cannot see the whole list (hidden members, say).
            self._log(f"[vote] {user_id} is in the chat but not in its member list read at "
                      f"{self._read_wall:%Y-%m-%d %H:%M} UTC -- counted as joined since")
            return self._read_wall


async def judge(member: dict | None, user_id: int, join_dates: JoinDates,
                now: datetime | None = None, log=print) -> Verdict:
    """The verdict for one person, from their getChatMember answer (None when that call
    failed -- the Bot API errors for somebody who never joined) and the chat's join dates."""
    if not in_chat(member):
        return Verdict(is_member=False, reason=NOT_A_MEMBER)
    if member.get("status") in _TRUSTED_STATUSES:
        return ALLOWED
    try:
        joined = await join_dates.joined_at(user_id)
    except Exception as e:  # noqa: BLE001 -- any failure to read the list is "unverified"
        log(f"[vote] could not read when {user_id} joined the chat: {type(e).__name__}: {e}")
        return Verdict(is_member=True, reason=UNVERIFIED)
    if joined.tzinfo is None:
        joined = joined.replace(tzinfo=timezone.utc)
    eligible_at = joined + MIN_MEMBERSHIP
    if eligible_at <= (now or _utcnow()):
        return ALLOWED
    return Verdict(is_member=True, reason=TOO_NEW, eligible_at=eligible_at)


def join_date_of(participant) -> datetime | None:
    """A Telethon member record's join date: `date` on every kind iter_participants yields
    but the creator's, which has none and gets FOUNDING. A restricted record that has left
    the chat is not a member, so None."""
    if participant is None or getattr(participant, "left", False):
        return None
    date = getattr(participant, "date", None)
    if date is None:
        return FOUNDING
    return date if date.tzinfo is not None else date.replace(tzinfo=timezone.utc)
