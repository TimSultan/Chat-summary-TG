"""The support-the-project collection: the roll of honour, and who asked to chip in.

Two lists that look similar and must not be confused.

  DONORS is the roll of honour shown to everybody. It is maintained BY HAND -- money
  arrives outside this bot, through whatever the owner and the donor agreed between
  themselves, and nothing here can or should try to verify that it did. Somebody appears
  on it because the owner put them on it.

  PLEDGES is the queue behind it: "I would like to give N dollars, talk to me". Written by
  the game when a player fills the form in, read by the owner, and never shown to anybody
  else -- it is a list of people's names next to sums of money they have not yet paid.

Nothing in this module touches payment. No card details, no wallets, no links out to a
processor: a pledge records an intention and a way to reach the person, and the owner
takes it from there. That is deliberate, and it is why this file can exist at all.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime

import stats
from app_time import now as app_now

DONATIONS_STORE_VERSION = 1
# The roll of honour is a short list on a game screen, not a ledger. Anything past this
# stops being an honour and starts being a directory.
MAX_DONORS_SHOWN = 10
# How many pending pledges are retained. They are a to-do list for one person; older ones
# have either been answered or gone cold.
PLEDGE_LIMIT = 400
# What the collection is for, in one place so the pitch and any admin view agree.
MONTHLY_GOAL_USD = 100


# The pitch, written once and rendered by both interfaces so the two can never drift into
# telling people different things about where their money goes. Plain paragraphs with no
# markup, so Telegram HTML and the web page can each wrap them however they like.
#
# Honest by construction: it says what the money buys, what it does not buy, and what the
# person gets back. Nothing here promises the game will change if they pay -- the rewards
# are cosmetic and a name on a list, and that is the whole point.
PITCH_TITLE = "Поддержать проект"

PITCH_PARAGRAPHS = (
    "Эту игру делает один человек на чистом энтузиазме — вместе с ИИ-агентами, "
    "которые пишут код.",

    "Агенты стоят не так уж дорого, но лимиты у них жёсткие и заканчиваются быстро. "
    "Основное я трачу на работу, поэтому на игру достаётся то, что остаётся — отсюда и "
    "паузы между обновлениями.",

    f"Если вам нравится то, что здесь происходит — квесты, покрасы, подземелье, арена — "
    f"и хочется, чтобы это росло быстрее, я открываю сбор на версию агентов помощнее "
    f"(${MONTHLY_GOAL_USD} в месяц). Это напрямую превращается в скорость разработки: "
    "больше правок, больше нового, меньше ожидания.",
)

# What a supporter gets. Kept apart from the paragraphs because both interfaces draw it as
# a list rather than as prose.
PITCH_PERKS = (
    "имя в топе поддержавших",
    "уникальный значок",
    "своё оружие в игре — придумаете сами",
)

PITCH_FOOTER = (
    "Ничего из этого не даёт преимущества в боях: только внешность и благодарность."
)

# Shown when somebody taps «Задонатить» -- the deliberate speed bump between an impulse
# and a message about money. The "просто смотрел" way out is offered first-class, not as
# an apology, because a player poking at a new button must be able to leave without
# feeling they have committed to anything.
CONFIRM_QUESTION = (
    "Спасибо за интерес и за то, что дочитали.\n\n"
    "Это не оплата — ни карту, ни реквизиты здесь вводить не нужно. "
    "Вы просто оставляете заявку, и я свяжусь с вами лично, чтобы обо всём договориться.\n\n"
    "Хотите оставить заявку?"
)

AMOUNT_PROMPT = (
    "Напишите сумму в долларах — просто числом, например: 5\n\n"
    "Это ориентир для разговора, а не счёт: точную сумму и способ обсудим лично."
)

THANKS = (
    "Спасибо. Заявка сохранена — я свяжусь с вами в личных сообщениях, "
    "чтобы обо всём договориться.\n\n"
    "Даже если в итоге ничего не сложится, само желание поддержать уже много значит."
)


def _path(entry: str):
    return stats._stats_dir() / f"{stats._cache_key(entry)}_donations.json"


def _empty() -> dict:
    return {"version": DONATIONS_STORE_VERSION, "donors": [], "pledges": []}


def _load(entry: str) -> dict:
    path = _path(entry)
    if not path.exists():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    data.setdefault("version", DONATIONS_STORE_VERSION)
    if not isinstance(data.get("donors"), list):
        data["donors"] = []
    if not isinstance(data.get("pledges"), list):
        data["pledges"] = []
    data["donors"] = [row for row in data["donors"] if isinstance(row, dict) and row.get("name")]
    data["pledges"] = [row for row in data["pledges"] if isinstance(row, dict)][-PLEDGE_LIMIT:]
    return data


def _save(entry: str, data: dict) -> None:
    stats._write_json_atomic(_path(entry), data)


def _amount(value) -> int:
    try:
        return max(0, int(float(str(value).strip().replace(",", ".").lstrip("$"))))
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- roll of honour


def donors(entry: str, limit: int = MAX_DONORS_SHOWN) -> list[dict]:
    """The published list, biggest first. Safe to show anybody."""
    rows = sorted(
        _load(entry)["donors"],
        key=lambda row: (-_amount(row.get("amount")), str(row.get("name", "")).casefold()),
    )
    return [
        {
            "name": str(row.get("name") or "").strip(),
            "amount": _amount(row.get("amount")),
            # Whatever the owner promised this person: a title, a weapon they invented,
            # a badge. Free text on purpose -- the rewards are negotiated, not a schema.
            "note": str(row.get("note") or "").strip(),
        }
        for row in rows[:max(0, int(limit))]
    ]


def add_donor(entry: str, name: str, amount, note: str = "") -> dict:
    """Put somebody on the roll of honour, or top up what they already gave."""
    name = str(name or "").strip()
    if not name:
        raise ValueError("Нужно имя.")
    data = _load(entry)
    for row in data["donors"]:
        if str(row.get("name", "")).strip().casefold() == name.casefold():
            row["amount"] = _amount(row.get("amount")) + _amount(amount)
            if note:
                row["note"] = str(note).strip()
            _save(entry, data)
            return dict(row)
    row = {"name": name, "amount": _amount(amount), "note": str(note or "").strip(),
           "added_at": app_now().isoformat()}
    data["donors"].append(row)
    _save(entry, data)
    return dict(row)


def remove_donor(entry: str, name: str) -> bool:
    data = _load(entry)
    needle = str(name or "").strip().casefold()
    kept = [row for row in data["donors"] if str(row.get("name", "")).strip().casefold() != needle]
    if len(kept) == len(data["donors"]):
        return False
    data["donors"] = kept
    _save(entry, data)
    return True


def total_raised(entry: str) -> int:
    return sum(_amount(row.get("amount")) for row in _load(entry)["donors"])


# ------------------------------------------------------------------------------- pledges


def record_pledge(entry: str, user_id, amount, *, name: str = "", username: str = "") -> dict:
    """Somebody said they would like to give `amount`. Returns the stored row.

    Deliberately not idempotent on the player: somebody may pledge twice, months apart,
    and collapsing that into one row would lose the second conversation. Each pledge gets
    its own id so the owner can refer to one.
    """
    data = _load(entry)
    row = {
        "id": secrets.token_hex(6),
        "user_id": str(user_id),
        "name": str(name or "").strip(),
        "username": str(username or "").strip().lstrip("@"),
        "amount": _amount(amount),
        "at": app_now().isoformat(),
    }
    data["pledges"].append(row)
    data["pledges"] = data["pledges"][-PLEDGE_LIMIT:]
    _save(entry, data)
    return dict(row)


def pledges(entry: str, limit: int = 50) -> list[dict]:
    """Newest first. For the owner only -- see the module docstring."""
    return [dict(row) for row in reversed(_load(entry)["pledges"])][:max(0, int(limit))]


def pledge_summary(row: dict) -> str:
    """One pledge as the message the owner receives. Plain text, no markup."""
    who = row.get("name") or "Игрок"
    handle = f" @{row['username']}" if row.get("username") else ""
    when = str(row.get("at") or "")
    try:
        when = datetime.fromisoformat(when).strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        pass
    return (
        "💜 Новая поддержка проекта\n\n"
        f"Кто: {who}{handle}\n"
        f"ID: {row.get('user_id')}\n"
        f"Сумма: ${row.get('amount', 0)}\n"
        f"Когда: {when}\n"
        f"Заявка: {row.get('id')}\n\n"
        "Свяжитесь с человеком, чтобы договориться о переводе."
    )
