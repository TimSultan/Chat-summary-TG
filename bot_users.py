"""Who has the bot open in their DM -- the audience of a /vote mailing, and the list an
administrator opens from the /vote panel ("👥 Кто активировал бота").

Telegram gives a bot no list of the people who pressed Start, and a bot cannot write to
anybody first, so this keeps one of its own, from four sources:

- anything somebody does in the DM (a message, a button press): they have the bot
  started, so they are ACTIVE;
- my_chat_member updates for a private chat: Telegram sends one when a person blocks
  the bot ("kicked" -> BLOCKED) and when they unblock or restart it ("member" -> ACTIVE);
- what came back from a message the bot sent them: delivered is ACTIVE, "bot was blocked
  by the user" is BLOCKED, "user is deactivated" is GONE (a deleted account);
- the check ("🔍 Проверить"), which asks Telegram about everybody the bot has seen in the
  chat. It is how people who started the bot before this list existed get onto it.

A person who never pressed Start is not written down at all: they are not "blocked", the
bot simply has no conversation with them.

Muting is invisible to a bot. Telegram tells it nothing when somebody silences the chat,
and a muted chat accepts messages exactly like an unmuted one -- so the list cannot have
a "muted" column, and says so instead of guessing.

The store is one JSON file, read once and answered from memory after that, written only
when something changed. Thread-safe, because callers on the event loop hand the disk work
to a worker thread (asyncio.to_thread), the same as vote_eligibility.JoinLog.
"""

import asyncio
import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "."))
BOT_USERS_DIR = DATA_DIR / "bot_users"

ACTIVE = "active"
BLOCKED = "blocked"
GONE = "gone"
# An outcome only, never stored: Telegram says the bot has no conversation with this
# person (never pressed Start, or a bot). Somebody on the list who comes back like this is
# taken off it.
NEVER = "never"
STATUSES = (ACTIVE, BLOCKED, GONE)

# 20 messages a second: under Telegram's ~30/s for messages to different people, with room
# for the bot's ordinary replies going out at the same time.
SEND_INTERVAL_SECONDS = 0.05
# How many times one person is retried after Telegram says "Too Many Requests", and the
# longest wait it may ask for before that person is given up on for this run.
RATE_LIMIT_RETRIES = 3
MAX_RETRY_AFTER_SECONDS = 60

_BLOCKED_ERRORS = re.compile(r"blocked by the user", re.I)
_GONE_ERRORS = re.compile(r"user is deactivated", re.I)
_NEVER_ERRORS = re.compile(
    r"can't initiate conversation|bot can't send messages to bots|chat not found|user not found"
    r"|PEER_ID_INVALID",
    re.I,
)
_RETRY_AFTER = re.compile(r"retry after (\d+)", re.I)


def _dir() -> Path:
    """Indirection purely for tests (patch this, not the module-level constant)."""
    return BOT_USERS_DIR


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def display_name(user: dict | None) -> str:
    user = user or {}
    return " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p).strip()


def person_label(name: str | None, username: str | None, user_id) -> str:
    """"Имя (@ник)", or whichever of the two is known, or the bare id."""
    name = (name or "").strip()
    handle = f"@{username.lstrip('@')}" if username else ""
    if name and handle:
        return f"{name} ({handle})"
    return name or handle or f"id {user_id}"


@dataclass(frozen=True)
class Person:
    user_id: str
    name: str
    username: str
    status: str
    changed_at: str
    seen: str

    def label(self) -> str:
        return person_label(self.name, self.username, self.user_id)


class Registry:
    def __init__(self, path: Path, now=_utcnow):
        self._path = Path(path)
        self._now = now
        self._lock = threading.Lock()
        self._users: dict[str, dict] | None = None
        self._checked_at: str | None = None
        self.reads = 0
        self.writes = 0

    # ---------------------------------------------------------------- disk

    def _loaded(self) -> dict[str, dict]:
        if self._users is None:
            self.reads += 1
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {}
            raw = raw if isinstance(raw, dict) else {}
            users = {}
            for user_id, record in (raw.get("users") or {}).items():
                if isinstance(record, dict) and record.get("status") in STATUSES:
                    users[str(user_id)] = dict(record)
            self._users = users
            self._checked_at = raw.get("checked_at") if isinstance(raw.get("checked_at"), str) else None
        return self._users

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Named per process and thread: two writers sharing one temporary name could
        # move each other's half-written file into place.
        temporary = self._path.with_name(
            f"{self._path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        temporary.write_text(
            json.dumps(
                {"version": 1, "checked_at": self._checked_at, "users": self._users},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        temporary.replace(self._path)
        self.writes += 1

    # ---------------------------------------------------------------- writes

    def _set(self, users: dict, user_id: str, status: str, name: str = "", username: str = "",
             seen: bool = False) -> bool:
        """One person's new state, in memory. Returns whether anything changed. A name or
        handle that is not given keeps what was stored."""
        record = users.get(user_id)
        now = self._now()
        if record is None:
            record = {"name": "", "username": "", "status": "", "changed_at": "", "seen": ""}
        updated = dict(record)
        if name:
            updated["name"] = name
        if username:
            updated["username"] = username
        if updated["status"] != status:
            updated["status"] = status
            updated["changed_at"] = now.isoformat(timespec="seconds")
        if seen:
            # By the day: somebody chatting with the bot all afternoon is one write, not one
            # per message.
            updated["seen"] = now.date().isoformat()
        if updated == record and user_id in users:
            return False
        users[user_id] = updated
        return True

    def note_seen(self, user: dict) -> bool:
        """Somebody did something in the DM, so they have the bot started. Returns
        whether this changed the store (and so was written)."""
        if not user or user.get("is_bot") or user.get("id") is None:
            return False
        with self._lock:
            users = self._loaded()
            changed = self._set(
                users, str(user["id"]), ACTIVE,
                name=display_name(user), username=user.get("username") or "", seen=True,
            )
            if changed:
                self._save()
        return changed

    def is_current(self, user: dict) -> bool:
        """Whether note_seen would change nothing -- answered from memory alone, without
        the lock or the disk, so the event loop can skip handing it to a thread for every
        message somebody sends in the DM. False until the store has been read once."""
        users = self._users
        if users is None or not user or user.get("id") is None:
            return False
        record = users.get(str(user["id"]))
        return bool(
            record
            and record.get("status") == ACTIVE
            and record.get("seen") == self._now().date().isoformat()
            and (not display_name(user) or record.get("name") == display_name(user))
            and (not user.get("username") or record.get("username") == user.get("username"))
        )

    def note_member_update(self, update: dict) -> str | None:
        """One my_chat_member update. In a private chat it is the person blocking the bot
        ("kicked") or unblocking/restarting it ("member"). Returns the new status when it
        changed the store, None otherwise (a group's update, or nothing new)."""
        chat = update.get("chat") or {}
        if chat.get("type") != "private":
            return None
        new = (update.get("new_chat_member") or {}).get("status")
        status = BLOCKED if new == "kicked" else ACTIVE if new == "member" else None
        user = update.get("from") or {}
        if status is None or user.get("id") is None or user.get("is_bot"):
            return None
        with self._lock:
            users = self._loaded()
            changed = self._set(
                users, str(user["id"]), status,
                name=display_name(user), username=user.get("username") or "",
            )
            if changed:
                self._save()
        return status if changed else None

    def record_outcomes(self, outcomes: dict, names: dict | None = None,
                        checked: bool = False) -> int:
        """What a mailing or a check found out, all at once: one read and one write for
        the whole run, however many people it reached. `outcomes` maps a user id to
        ACTIVE, BLOCKED, GONE or NEVER (anything else -- a timeout, say -- is no news and
        is not passed in). `names` may carry {"name", "username"} per id for people seen
        for the first time. `checked` stamps the time of the check. Returns how many
        people changed."""
        names = names or {}
        changed = 0
        with self._lock:
            users = self._loaded()
            for user_id, outcome in outcomes.items():
                user_id = str(user_id)
                if outcome == NEVER:
                    if users.pop(user_id, None) is not None:
                        changed += 1
                    continue
                if outcome not in STATUSES:
                    continue
                # A name from elsewhere (the chat's stats) only fills a blank: the one a
                # person's own DM brought is newer and stays.
                known = names.get(user_id) or {}
                stored = users.get(user_id) or {}
                if self._set(users, user_id, outcome,
                             name="" if stored.get("name") else known.get("name") or "",
                             username="" if stored.get("username") else known.get("username") or ""):
                    changed += 1
            if checked:
                self._checked_at = self._now().isoformat(timespec="seconds")
            if changed or checked:
                self._save()
        return changed

    # ---------------------------------------------------------------- reads

    def people(self, status: str | None = None) -> list[Person]:
        """Everybody on the list (or only those with `status`), sorted by name."""
        with self._lock:
            users = self._loaded()
            rows = [
                Person(
                    user_id=user_id, name=record.get("name") or "",
                    username=record.get("username") or "", status=record["status"],
                    changed_at=record.get("changed_at") or "", seen=record.get("seen") or "",
                )
                for user_id, record in users.items()
                if status is None or record["status"] == status
            ]
        return sorted(rows, key=lambda p: (p.label().casefold(), p.user_id))

    def counts(self) -> dict[str, int]:
        with self._lock:
            users = self._loaded()
            result = {status: 0 for status in STATUSES}
            for record in users.values():
                result[record["status"]] += 1
        return result

    def checked_at(self) -> datetime | None:
        with self._lock:
            self._loaded()
            stamp = self._checked_at
        try:
            return datetime.fromisoformat(stamp) if stamp else None
        except ValueError:
            return None


# One Registry per storage place, for the life of the process: it reads its file once and
# answers from memory after that.
_REGISTRIES: dict[Path, Registry] = {}


def registry() -> Registry:
    path = _dir() / "users.json"
    found = _REGISTRIES.get(path)
    if found is None:
        found = _REGISTRIES[path] = Registry(path)
    return found


# -------------------------------------------------------------------- delivery


def outcome_for_error(error: BaseException) -> str | None:
    """What a failed send says about the person: BLOCKED, GONE, NEVER -- or None when it
    says nothing about them (a network error, a rate limit, a bad request)."""
    text = str(error)
    if _BLOCKED_ERRORS.search(text):
        return BLOCKED
    if _GONE_ERRORS.search(text):
        return GONE
    if _NEVER_ERRORS.search(text):
        return NEVER
    return None


def retry_after(error: BaseException) -> int | None:
    """Telegram's "Too Many Requests: retry after N", as N -- from the structured field
    bot_api attaches when it can, else from the text."""
    value = getattr(error, "retry_after", None)
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    match = _RETRY_AFTER.search(str(error))
    return int(match.group(1)) if match else None


@dataclass
class Delivery:
    """What one run of deliver() did, person by person."""
    total: int = 0
    delivered: int = 0
    outcomes: dict = None
    errors: dict = None

    def __post_init__(self):
        self.outcomes = {} if self.outcomes is None else self.outcomes
        self.errors = {} if self.errors is None else self.errors

    def count(self, outcome: str) -> int:
        return sum(1 for value in self.outcomes.values() if value == outcome)


async def deliver(user_ids, send, *, interval: float | None = None, progress=None,
                  sleep=None) -> Delivery:
    """Calls `send(user_id)` for each person in turn, paced so Telegram's flood limit is
    never reached, and sorts out what came back.

    A "Too Many Requests" waits as long as Telegram asks and tries the same person again
    (up to RATE_LIMIT_RETRIES). An error that names the person's state (blocked, deleted,
    never started) becomes their outcome; any other error is kept in `errors` and says
    nothing about them. `progress(done, total)` is awaited after every person, if given.
    Never raises for one person's failure: one bad address must not stop the run."""
    interval = SEND_INTERVAL_SECONDS if interval is None else interval
    sleep = sleep or asyncio.sleep
    user_ids = list(dict.fromkeys(str(u) for u in user_ids))
    result = Delivery(total=len(user_ids))
    for index, user_id in enumerate(user_ids):
        if index:
            await sleep(interval)
        attempt = 0
        while True:
            try:
                await send(user_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 -- sorted below, never fatal for the run
                wait = retry_after(e)
                if wait is not None and attempt < RATE_LIMIT_RETRIES and wait <= MAX_RETRY_AFTER_SECONDS:
                    attempt += 1
                    await sleep(wait + 1)
                    continue
                outcome = outcome_for_error(e)
                if outcome is not None:
                    result.outcomes[user_id] = outcome
                else:
                    result.errors[user_id] = str(e)
            else:
                result.outcomes[user_id] = ACTIVE
                result.delivered += 1
            break
        if progress is not None:
            await progress(index + 1, len(user_ids))
    return result
