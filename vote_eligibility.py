"""Who may cast a ballot: a member of Папка художников who joined it at least a day ago.

The rule is about organised voting. A burst of accounts that join, vote for one work and
leave is what a rigged contest looks like (see vote_audit), and a day's wait between
joining and voting takes the point out of it while costing a real member nothing. It
applies to every vote the bot runs -- /vote in the Mini App and through its browser link,
the arena, the nominations -- and each of them asks it through bot_listener, which owns
the Bot API client the answer needs.

The community is Папка художников (`VOTE_MEMBER_CHAT`, `@papkahudojnicov` by default),
not the tracked chat the works are collected from: the people a contest is for are the
ones who follow it there.

Two questions, two sources, both the bot's own:

- Is this person in it NOW? The Bot API's getChatMember. Exact and current, but it carries
  no join date at all.
- Since WHEN? Telegram tells an administrator bot about every join and leave as it happens
  (`chat_member` updates, each with its date), and the bot -- an administrator of Папка
  художников, since it posts the vote announcements there -- writes the recent joins down
  (JoinLog). Somebody with no join written down was a member before the bot started
  watching, and may vote. Reading Telegram's member list instead was tried first: for a
  channel only its administrators may read it, and a userbot that is not one left every
  member unverifiable and nobody able to vote.

A membership that cannot be checked refuses the ballot ("unverified") rather than letting
it through. Administrators and the creator are trusted and skip the join-date half.
"""

import json
import math
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# How long somebody must have been a member before their ballot counts.
MIN_MEMBERSHIP = timedelta(hours=24)

# A written-down join older than this is dropped: past MIN_MEMBERSHIP it decides nothing
# that "no join written down" would not, and the day on top only spares the edge.
KEEP_JOINS = MIN_MEMBERSHIP + timedelta(days=1)

# Where somebody who is not a member is sent to join: every vote page's "Вступить" button
# and the browser link's reply. Opened inside Telegram, it is the chat's own screen with
# its own one-tap join -- no API joins a chat on a user's behalf.
JOIN_URL = "https://t.me/papkahudojnicov"

# Why a ballot is refused. None means it is not.
NOT_A_MEMBER = "not_member"
TOO_NEW = "too_new"
UNVERIFIED = "unverified"

# Statuses that are in the chat. "restricted" is decided by its own is_member flag.
_IN_CHAT_STATUSES = frozenset({"creator", "administrator", "member"})
_TRUSTED_STATUSES = frozenset({"creator", "administrator"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def in_chat(member: dict | None) -> bool:
    """Whether a getChatMember answer (or either side of a chat_member update) says this
    person is in the chat. "restricted" is special-cased because Telegram uses that one
    status for two situations: a member under restrictions (still in the chat) and
    somebody who was restricted and then left. Only the payload's is_member tells them
    apart. Wherever is_member is given as false it wins over the status: an owner who
    leaves stays the owner, and must not vote as a member."""
    if not isinstance(member, dict):
        return False
    if member.get("is_member") is False:
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
        return ("Не получилось проверить, состоите ли вы в Папке художников. "
                "Попробуйте ещё раз чуть позже.")


ALLOWED = Verdict(is_member=True)


def membership_only(is_member):
    """A verdict from a plain membership answer, without the join-date half -- what a
    page mounted without bot_listener (a test, a local run) falls back to, since only
    bot_listener sees who joins when."""
    async def verdict(user: dict) -> Verdict:
        if await is_member(user):
            return ALLOWED
        return Verdict(is_member=False, reason=NOT_A_MEMBER)
    return verdict


def judge(member: dict | None, joined_at: datetime | None,
          now: datetime | None = None) -> Verdict:
    """The verdict for one person, from their getChatMember answer (None when Telegram
    said there is no such member) and the join the JoinLog has for them (None when it has
    none: they were a member before the bot started watching)."""
    if not in_chat(member):
        return Verdict(is_member=False, reason=NOT_A_MEMBER)
    if member.get("status") in _TRUSTED_STATUSES or joined_at is None:
        return ALLOWED
    eligible_at = joined_at + MIN_MEMBERSHIP
    if eligible_at <= (now or _utcnow()):
        return ALLOWED
    return Verdict(is_member=True, reason=TOO_NEW, eligible_at=eligible_at)


def matches_chat(chat: dict | None, chat_ref: str | None, chat_id=None) -> bool:
    """Whether an update's chat is the one `chat_ref` names: an @username, a numeric id,
    or (for any other reference) the already-resolved `chat_id`."""
    if not isinstance(chat, dict) or not chat_ref:
        return False
    if chat_ref.startswith("@"):
        return (chat.get("username") or "").lower() == chat_ref[1:].lower()
    if chat_ref.lstrip("-").isdigit():
        return str(chat.get("id")) == chat_ref
    return chat_id is not None and chat.get("id") == chat_id


class JoinLog:
    """The recent joins of one chat, as the bot saw them happen -- {user_id: joined_at},
    kept on disk so a restart does not forget who joined an hour ago.

    Fed by note() with each chat_member update: a join writes the person down with the
    update's date, a leave rubs them out (so coming back starts the day afresh). Joins
    older than KEEP_JOINS are dropped on the way. Read once from disk, then answered from
    memory; written only when something changed. Thread-safe, since callers on the event
    loop hand the disk work to a worker thread.
    """

    def __init__(self, path: Path, now=_utcnow):
        self._path = Path(path)
        self._now = now
        self._lock = threading.Lock()
        self._joins: dict[str, datetime] | None = None
        self.reads = 0
        self.writes = 0

    def _loaded(self) -> dict[str, datetime]:
        if self._joins is None:
            self.reads += 1
            joins: dict[str, datetime] = {}
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {}
            for user_id, stamp in (raw if isinstance(raw, dict) else {}).items():
                try:
                    joins[str(user_id)] = datetime.fromisoformat(stamp)
                except (TypeError, ValueError):
                    continue
            self._joins = joins
        return self._joins

    def _prune(self, joins: dict[str, datetime]) -> bool:
        cutoff = self._now() - KEEP_JOINS
        stale = [user_id for user_id, joined in joins.items() if joined < cutoff]
        for user_id in stale:
            del joins[user_id]
        return bool(stale)

    def _save(self, joins: dict[str, datetime]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps({k: v.isoformat() for k, v in joins.items()}, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(self._path)
        self.writes += 1

    def joined_at(self, user_id) -> datetime | None:
        """When this person joined, if that was recent enough to be written down."""
        with self._lock:
            joined = self._loaded().get(str(user_id))
        if joined is None or joined < self._now() - KEEP_JOINS:
            return None
        return joined

    def note(self, update: dict) -> str | None:
        """Takes one chat_member update (ChatMemberUpdated). Returns "joined", "left", or
        None when it changed nothing this log keeps (a promotion, a restriction)."""
        old, new = update.get("old_chat_member"), update.get("new_chat_member")
        user = (new or {}).get("user") or {}
        if user.get("id") is None:
            return None
        was_in, is_in = in_chat(old), in_chat(new)
        if was_in == is_in:
            return None
        user_id = str(user["id"])
        stamp = update.get("date")
        with self._lock:
            joins = self._loaded()
            if is_in:
                joins[user_id] = (datetime.fromtimestamp(int(stamp), timezone.utc)
                                  if stamp else self._now())
                changed, change = True, "joined"
            else:
                changed, change = joins.pop(user_id, None) is not None, "left"
            if self._prune(joins) or changed:
                self._save(joins)
        return change
