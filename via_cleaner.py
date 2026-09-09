"""ViaCleaner: sweeps inline-bot posts ("via @gif") out of the chat after a set delay.

WHAT IT IS FOR. An inline bot result is useful for about as long as it takes to look at
it. A dozen of them in an evening pushes the actual conversation off the screen, and a
week of them makes the chat unreadable to anybody scrolling back. So the message is left
alone long enough to be seen and reacted to, and then removed.

WHY A DELAY AND NOT AN IMMEDIATE DELETE. Deleting instantly reads as censorship: the
person who sent it never sees their own result, and neither does anybody who was not
looking at that exact second. The delay is the whole feature -- "сразу" is offered in the
menu, but it is not the default.

WHY A FILE. The delay outlives the process. A five-minute timer held only in memory is
lost to every deploy, and a deploy is exactly when a batch of pending deletions is most
likely to be waiting -- the result being via-messages that survive for ever purely because
they were lucky about their timing. Pending deletions and per-chat settings therefore live
on the persistent volume next to everything else (stats._stats_dir), and a restart picks
up whatever was owed.

WHO DOES WHAT. Detection happens wherever a via-message is seen: listener.py's Telethon
session sees every one of them, and bot_listener.py sees them too whenever the bot's
privacy mode is off. Both just call `remember`, which de-duplicates by (chat, message id),
so having two observers costs nothing and means one of them being down does not stop the
cleaning. The deletion itself is bot_listener.py's alone -- removing somebody else's
message needs the "delete messages" admin right, which the bot account is the one that
holds (see [[feedback-bot-account-only]] -- the personal account never acts in the chat).

WHAT IT DELIBERATELY DOES NOT DO. It does not touch ordinary messages, forwards, replies,
or anything the bot itself posted; it has no opinion about which inline bot is acceptable;
and it never deletes anything in a chat that is not named in LISTENER_ALLOWED_CHATS. It is
off until an administrator turns it on.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import weakref

import stats

VIA_CLEANER_STORE_VERSION = 1

# Off until somebody asks for it. A bot that silently starts deleting messages the day it
# is deployed is a bot that gets removed from the chat.
DEFAULT_ENABLED = False
DEFAULT_DELAY_SECONDS = 5 * 60

# What the settings menu offers. Ordered, and used verbatim to build the keyboard, so this
# tuple is the single definition of "which delays exist".
DELAY_CHOICES = (0, 60, 5 * 60, 15 * 60, 60 * 60, 3 * 60 * 60, 24 * 60 * 60)

# Telegram refuses to let a bot delete a message older than roughly 48 hours. Anything we
# are still holding past that is undeletable, so it is dropped rather than retried for
# ever -- otherwise a long outage would leave a permanent backlog that fails on every
# single sweep. Measured from when the message was SEEN, which is what Telegram's own
# limit is measured from, not from when it became due.
PENDING_EXPIRY_SECONDS = 47 * 60 * 60

# A hard ceiling on the backlog. Only reachable if the bot has been unable to delete for a
# very long time (no rights, chat unresolvable) while an inline bot is being hammered; at
# that point the oldest entries are the least useful ones to keep, and an unbounded list
# would be a store that grows for ever.
PENDING_LIMIT = 5000

# How long the sweeper is willing to sit idle before looking at the store again. Nothing
# depends on it -- `wake()` is called the moment anything is remembered -- so this is
# purely the belt to that braces: a nudge lost to a cancelled wait costs at most this long.
IDLE_WAIT_SECONDS = 300

# How many deletions one sweep pass performs before going back round. Telegram rate-limits
# bulk deletes, and a backlog released by a restart could otherwise be several hundred
# calls fired back to back.
SWEEP_BATCH = 50

CALLBACK_PREFIX = "viaclean"
COMMANDS = ("/viacleaner", "/via")

BACK_BUTTON_TEXT = "◀️ Назад"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
#
# Every read-modify-write below happens inside one synchronous function with no `await`
# in it, and the whole application runs on a single event loop, so two writers can never
# interleave. That is the entire concurrency story -- no lock is needed and adding one
# would only be a claim that something here yields when it does not.


def _path():
    return stats._stats_dir() / "via_cleaner.json"


def _key(entry: str | None) -> str:
    """One chat's settings key. Normalised the way LISTENER_ALLOWED_CHATS entries are
    compared everywhere else, so "@Chat" and "chat" are the same chat and not two."""
    return (entry or "").strip().lstrip("@").lower()


def _blank() -> dict:
    return {"version": VIA_CLEANER_STORE_VERSION, "chats": {}, "pending": []}


def _read() -> dict:
    path = _path()
    if not path.exists():
        return _blank()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A damaged store must not start deleting messages on default settings, and must
        # not crash the sweeper either. Blank means "off everywhere", which is the safe
        # direction for a feature whose failure mode is removing somebody's message.
        return _blank()
    if not isinstance(data, dict):
        return _blank()
    chats = data.get("chats")
    pending = data.get("pending")
    return {
        "version": VIA_CLEANER_STORE_VERSION,
        "chats": chats if isinstance(chats, dict) else {},
        "pending": [item for item in pending if isinstance(item, dict)] if isinstance(pending, list) else [],
    }


def _write(store: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written beside the real file and moved into place: a process killed mid-write (a
    # deploy, which is exactly when pending deletions exist) would otherwise leave
    # truncated JSON, and _read would then quietly reset every chat's settings to off.
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _chat_record(store: dict, entry: str) -> dict:
    record = store["chats"].get(_key(entry))
    if not isinstance(record, dict):
        record = {}
    return {
        "entry": record.get("entry") or (entry or ""),
        "enabled": bool(record.get("enabled", DEFAULT_ENABLED)),
        "delay_seconds": _clean_delay(record.get("delay_seconds", DEFAULT_DELAY_SECONDS)),
        "deleted": max(0, int(record.get("deleted") or 0)),
        "updated_by": str(record.get("updated_by") or ""),
    }


def _clean_delay(value) -> int:
    """Any stored delay, coerced back into something offerable in the menu.

    Bounded rather than rejected: a value edited by hand into the file, or left behind by
    an older DELAY_CHOICES, must still produce a working setting instead of an exception
    on a path that runs for every message.
    """
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return DEFAULT_DELAY_SECONDS
    if seconds in DELAY_CHOICES:
        return seconds
    seconds = max(0, min(seconds, max(DELAY_CHOICES)))
    return min(DELAY_CHOICES, key=lambda choice: abs(choice - seconds))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def settings(entry: str | None) -> dict:
    """This chat's ViaCleaner settings, plus the two live counters the menu shows."""
    store = _read()
    record = _chat_record(store, entry or "")
    record["pending"] = sum(1 for item in store["pending"] if item.get("entry_key") == _key(entry))
    return record


def is_enabled(entry: str | None) -> bool:
    return settings(entry)["enabled"]


def set_enabled(entry: str, enabled: bool, by: str = "") -> dict:
    """Turn the cleaner on or off for one chat.

    Turning it OFF also forgets what was already scheduled: somebody switching this off is
    asking for messages to stop disappearing, and honouring a queue built under the old
    setting would delete a handful more of them minutes after they said stop.
    """
    store = _read()
    record = _chat_record(store, entry)
    record["enabled"] = bool(enabled)
    record["updated_by"] = by or record["updated_by"]
    store["chats"][_key(entry)] = record
    if not enabled:
        store["pending"] = [item for item in store["pending"] if item.get("entry_key") != _key(entry)]
    _write(store)
    result = dict(record)
    result["pending"] = sum(1 for item in store["pending"] if item.get("entry_key") == _key(entry))
    return result


def set_delay(entry: str, seconds, by: str = "") -> dict:
    """Change how long a via-message is left standing.

    Already-scheduled deletions are re-timed against the new delay rather than left on the
    old clock. Shortening the delay from a day to a minute and then watching yesterday's
    backlog sit there for another day would read as the setting simply not working.
    """
    store = _read()
    record = _chat_record(store, entry)
    record["delay_seconds"] = _clean_delay(seconds)
    record["updated_by"] = by or record["updated_by"]
    store["chats"][_key(entry)] = record
    for item in store["pending"]:
        if item.get("entry_key") == _key(entry):
            item["delete_at"] = float(item.get("seen_at") or 0.0) + record["delay_seconds"]
    _write(store)
    # A shortened delay can make a whole backlog due in the past. Without this the sweeper
    # would sit out its idle timeout first, and the setting would look like it did nothing.
    wake()
    result = dict(record)
    result["pending"] = sum(1 for item in store["pending"] if item.get("entry_key") == _key(entry))
    return result


# ---------------------------------------------------------------------------
# The pending queue
# ---------------------------------------------------------------------------


def remember(entry: str | None, message_id: int, via: str = "", now: float | None = None) -> float | None:
    """Schedule one via-message for deletion. Returns when it will go, or None.

    None means "nothing to do" and is the ordinary answer, not a failure: the chat has the
    cleaner switched off, or this exact message is already scheduled. The de-duplication
    is what lets both observers (the Telethon session and the bot's own updates) call this
    for the same message without a second entry, and what makes a redelivered update after
    a reconnect harmless.
    """
    if not entry or not message_id:
        return None
    now = time.time() if now is None else now
    store = _read()
    record = _chat_record(store, entry)
    if not record["enabled"]:
        return None
    key = _key(entry)
    for item in store["pending"]:
        if item.get("entry_key") == key and item.get("message_id") == message_id:
            return float(item.get("delete_at") or now)
    delete_at = now + record["delay_seconds"]
    store["pending"].append({
        "entry": entry,
        "entry_key": key,
        "message_id": int(message_id),
        "via": str(via or ""),
        "seen_at": now,
        "delete_at": delete_at,
    })
    if len(store["pending"]) > PENDING_LIMIT:
        store["pending"].sort(key=lambda item: float(item.get("delete_at") or 0.0))
        del store["pending"][: len(store["pending"]) - PENDING_LIMIT]
    _write(store)
    wake()
    return delete_at


def _expired(item: dict, now: float) -> bool:
    return now - float(item.get("seen_at") or 0.0) > PENDING_EXPIRY_SECONDS


def due(now: float | None = None, limit: int = SWEEP_BATCH) -> list[dict]:
    """The messages whose time is up, oldest first, at most `limit` of them.

    Read-only: nothing is dropped here. The caller deletes what it got and then hands the
    same items to `settle`, so a crash in between costs a retry (a delete of an
    already-deleted message fails harmlessly) rather than a message left standing for ever.
    """
    now = time.time() if now is None else now
    ready = [
        item for item in _read()["pending"]
        if float(item.get("delete_at") or 0.0) <= now and not _expired(item, now)
    ]
    ready.sort(key=lambda item: float(item.get("delete_at") or 0.0))
    return ready[:limit]


def settle(items, now: float | None = None) -> int:
    """Drop the handled `items` from the queue and count them as deleted.

    Also drops everything Telegram will no longer let us delete (PENDING_EXPIRY_SECONDS),
    which is why the sweeper calls this even after a pass that handled nothing: it is the
    one place the backlog is allowed to shrink on its own.
    """
    now = time.time() if now is None else now
    handled = {(item.get("entry_key"), item.get("message_id")) for item in items}
    store = _read()
    kept = []
    counts: dict[str, int] = {}
    for item in store["pending"]:
        identity = (item.get("entry_key"), item.get("message_id"))
        if identity in handled:
            counts[item.get("entry_key") or ""] = counts.get(item.get("entry_key") or "", 0) + 1
            continue
        if _expired(item, now):
            continue
        kept.append(item)
    if len(kept) == len(store["pending"]) and not counts:
        # Nothing was handled and nothing aged out. The sweeper calls this after every
        # pass, including the idle ones, and rewriting an unchanged file every few minutes
        # for the life of the process is pure disk churn.
        return len(kept)
    store["pending"] = kept
    for key, count in counts.items():
        record = store["chats"].get(key)
        if not isinstance(record, dict):
            # Settings deleted (or the store reset) while a deletion was in flight. The
            # message still went, so the count still belongs somewhere.
            record = {"entry": key, "enabled": DEFAULT_ENABLED, "delay_seconds": DEFAULT_DELAY_SECONDS}
        record["deleted"] = max(0, int(record.get("deleted") or 0)) + count
        store["chats"][key] = record
    _write(store)
    return len(kept)


def next_due_at(now: float | None = None) -> float | None:
    """When the earliest still-live pending deletion comes due, or None if the queue is
    empty. Expired entries are ignored: they are never deleted, so waking for them would
    be waking for nothing."""
    now = time.time() if now is None else now
    times = [
        float(item.get("delete_at") or 0.0)
        for item in _read()["pending"]
        if not _expired(item, now)
    ]
    return min(times) if times else None


def seconds_until_next(now: float | None = None) -> float:
    """How long the sweeper may sleep: until the next deletion is due, capped at
    IDLE_WAIT_SECONDS and never negative."""
    now = time.time() if now is None else now
    upcoming = next_due_at(now)
    if upcoming is None:
        return float(IDLE_WAIT_SECONDS)
    return max(0.0, min(float(IDLE_WAIT_SECONDS), upcoming - now))


# ---------------------------------------------------------------------------
# Waking the sweeper
# ---------------------------------------------------------------------------
#
# The store is the hand-off between the half that SEES a via-message and the half that
# deletes it, so unlike the other cross-half features here there is no data for a queue to
# carry -- only "look again now", which matters because a chat set to "сразу" would
# otherwise wait out the sweeper's idle timeout.
#
# Kept per event loop rather than as one module-level Event. An asyncio.Event binds itself
# to the first loop that awaits it and then refuses every other one, which in a process
# that only ever runs one loop is a distinction without a difference -- right up until
# something runs a second one and gets "bound to a different event loop" out of a feature
# that has nothing to do with loops. The map is weak so a finished loop takes its event
# with it, and nothing is created at import time.

_wake_events: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _event() -> "asyncio.Event | None":
    """This loop's wake flag, or None when called with no loop running -- in which case
    there is no sweeper waiting and nothing to wake."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    event = _wake_events.get(loop)
    if event is None:
        event = asyncio.Event()
        _wake_events[loop] = event
    return event


def wake() -> None:
    """Tell a waiting sweeper there is something new."""
    event = _event()
    if event is not None:
        event.set()


async def wait_for_work(timeout: float) -> None:
    """Sleep until `wake()` or `timeout`, whichever comes first, and clear the flag.

    A timeout of 0 is the ordinary way of saying "there is a backlog, look now": it must
    return, not hang, which is why the flag is cleared outside the wait.
    """
    event = _event()
    if event is None:  # pragma: no cover -- only reachable without a running loop
        return
    try:
        await asyncio.wait_for(event.wait(), timeout=max(0.0, timeout))
    except asyncio.TimeoutError:
        pass
    event.clear()


# ---------------------------------------------------------------------------
# The settings menu
# ---------------------------------------------------------------------------


def format_delay(seconds: int) -> str:
    """A delay as an administrator reads it, not as a number of seconds."""
    seconds = int(seconds)
    if seconds <= 0:
        return "сразу"
    if seconds < 60 * 60:
        minutes = seconds // 60
        if minutes == 1:
            return "1 минута"
        return f"{minutes} минут" + ("ы" if 2 <= minutes <= 4 else "")
    hours = seconds // (60 * 60)
    if hours == 24:
        return "24 часа"
    if hours == 1:
        return "1 час"
    return f"{hours} час" + ("а" if 2 <= hours <= 4 else "ов")


def callback_data(action: str, argument: str | int | None = None) -> str:
    parts = [CALLBACK_PREFIX, action]
    if argument is not None:
        parts.append(str(argument))
    return ":".join(parts)


def parse_callback(data: str) -> tuple[str, str] | None:
    """(action, argument) for one of this menu's buttons, or None if it isn't one."""
    parts = (data or "").split(":")
    if len(parts) < 2 or parts[0] != CALLBACK_PREFIX:
        return None
    return parts[1], parts[2] if len(parts) > 2 else ""


def menu_text(entry: str | None) -> str:
    """The root screen. States what the feature does before what it is set to: this menu
    is opened by whoever is about to switch on message deletion in a chat of live people,
    and "what will disappear" is the question they need answered first."""
    current = settings(entry)
    lines = [
        "🧹 <b>ViaCleaner</b>",
        "",
        "Удаляет сообщения, отправленные через инлайн-ботов — те, что помечены «via …». "
        "Обычные сообщения, ответы и пересылки не трогает.",
        "",
        f"Чат: <b>{_escape(entry or '—')}</b>",
        "Сейчас: <b>" + ("включён" if current["enabled"] else "выключен") + "</b>",
        f"Удаляет через: <b>{format_delay(current['delay_seconds'])}</b>",
    ]
    if current["pending"]:
        lines.append(f"Ждут удаления: {current['pending']}")
    if current["deleted"]:
        lines.append(f"Удалено всего: {current['deleted']}")
    if current["enabled"]:
        lines.append("")
        lines.append(
            "Боту нужно право «Удаление сообщений» в этом чате — без него он "
            "уберёт только свои собственные сообщения."
        )
    return "\n".join(lines)


def menu_keyboard(entry: str | None) -> dict:
    current = settings(entry)
    return {
        "inline_keyboard": [
            [{
                "text": "⏹ Выключить" if current["enabled"] else "▶️ Включить",
                "callback_data": callback_data("toggle"),
            }],
            [{
                "text": f"⏱ Задержка: {format_delay(current['delay_seconds'])}",
                "callback_data": callback_data("delays"),
            }],
        ]
    }


def delay_text(entry: str | None) -> str:
    current = settings(entry)
    return (
        "⏱ <b>Через сколько удалять via-сообщения</b>\n\n"
        f"Сейчас: <b>{format_delay(current['delay_seconds'])}</b>\n\n"
        "«Сразу» удаляет, как только сообщение появилось — его успеют увидеть только те, "
        "кто в этот момент смотрел в чат."
    )


def delay_keyboard(entry: str | None) -> dict:
    """Two choices per row, with the current one marked -- an administrator opening this
    screen should be able to see what is set without reading the text above it."""
    current = settings(entry)["delay_seconds"]
    buttons = [
        {
            "text": ("✅ " if choice == current else "") + format_delay(choice),
            "callback_data": callback_data("delay", choice),
        }
        for choice in DELAY_CHOICES
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([{"text": BACK_BUTTON_TEXT, "callback_data": callback_data("menu")}])
    return {"inline_keyboard": rows}


def _escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
