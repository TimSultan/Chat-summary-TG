"""The receipt a group /summary leaves behind, and its "Отправить мне тоже" button.

The answer itself goes to the requester's DM. The group gets one line saying so, with a
button that sends the same answer to whoever presses it and a counter of how many people
did. The receipt stays in the chat for good, but the answer it can re-send is kept for
SHARE_TTL only: a summary is a catch-up on one stretch of chat, nobody needs it a day
later, and keeping every answer forever would grow the store without bound. After that
the text is dropped from disk and a press is told the summary has gone stale.

Stored on the volume beside the other stores rather than in memory, so a deploy in the
middle of the day does not turn every receipt posted that morning into a dead button.
"""

import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import stats

CALLBACK_PREFIX = "sumshare"
# The deep link a press falls back to when the presser has never opened the bot's DM:
# t.me/<bot>?start=sum-<token>. /start payloads arrive lowercased, so tokens are hex.
START_PREFIX = "sum-"
SHARE_TTL = timedelta(hours=24)
READ_EMOJI = "👀"
BUTTON_TEXT = "Отправить мне тоже"
STALE_TEXT = "Это саммари устарело. Запроси свежее: /summary"


def receipt_text(count: int) -> str:
    return f"Саммари отправлено в личку бота ({READ_EMOJI} {count})"


def receipt_keyboard(token: str) -> dict:
    return {"inline_keyboard": [[{"text": BUTTON_TEXT, "callback_data": f"{CALLBACK_PREFIX}:{token}"}]]}


def start_link(bot_username: str, token: str) -> str:
    return f"https://t.me/{bot_username}?start={START_PREFIX}{token}"


def _path() -> Path:
    return stats._stats_dir() / "summary_shares.json"


def _load() -> dict:
    path = _path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(store: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _is_stale(entry: dict, now: datetime) -> bool:
    try:
        created = datetime.fromisoformat(entry["created_at"])
    except (KeyError, TypeError, ValueError):
        return True
    return now - created >= SHARE_TTL


def _purge(store: dict, now: datetime) -> None:
    for token in [token for token, entry in store.items() if _is_stale(entry, now)]:
        del store[token]


def create(text: str, requester_id, now: datetime | None = None) -> str:
    """Keep `text` for SHARE_TTL and return the token its button carries. The requester
    already has the answer, so their own press is never counted."""
    now = now or datetime.now(timezone.utc)
    store = _load()
    _purge(store, now)
    token = secrets.token_hex(5)
    store[token] = {
        "created_at": now.isoformat(),
        "text": text,
        "recipients": [str(requester_id)],
        "count": 0,
        "chat_id": None,
        "message_id": None,
    }
    _save(store)
    return token


def attach_message(token: str, chat_id, message_id: int) -> None:
    """Remember where the receipt landed, so a deep-link delivery can update its counter."""
    store = _load()
    if token in store:
        store[token]["chat_id"] = chat_id
        store[token]["message_id"] = message_id
        _save(store)


def claim(token: str, user_id, now: datetime | None = None) -> tuple[str, dict | None]:
    """Record `user_id` as a recipient before anything is sent.

    Returns ("expired", None), ("already", entry) or ("claimed", entry) with the counter
    already raised. Claiming first is what keeps a double tap from sending twice: the
    second press finds the first one's claim. A delivery that then fails must call
    release, or the counter would include somebody who never got the text.
    """
    now = now or datetime.now(timezone.utc)
    store = _load()
    entry = store.get(token)
    if entry is None or _is_stale(entry, now):
        _purge(store, now)
        _save(store)
        return "expired", None
    if str(user_id) in entry["recipients"]:
        return "already", entry
    entry["recipients"].append(str(user_id))
    entry["count"] += 1
    _save(store)
    return "claimed", entry


def release(token: str, user_id) -> dict | None:
    """Undo a claim whose delivery failed. Returns the entry as it now stands."""
    store = _load()
    entry = store.get(token)
    if entry is None or str(user_id) not in entry["recipients"]:
        return entry
    entry["recipients"].remove(str(user_id))
    entry["count"] = max(0, entry["count"] - 1)
    _save(store)
    return entry
