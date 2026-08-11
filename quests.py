"""Quests: assignment, submission, review and history.

Two kinds, both from pets_quest_catalog.py, both proved the same way -- post a photo in
the chat with the quest's own hashtag (`#quest-nmm`), and a moderator accepts or rejects
it from the Mini App. Only an accepted submission pays.

  PAINTING CHALLENGES are DEALT. One per player per day, "paint one small thing in this
  technique", and the reroll below is how you trade the one you were given.

  КВЕСТЫ В РЕАЛЕ are TAKEN. Tidy the bench, walk six kilometres, buy a loupe. They sit in
  a list the player picks from and can hold several of at once, which is deliberate: a
  second dealt slot would stand visibly empty on every day nobody had one. Adding a real
  quest is a row in the catalogue and nothing else.

THREE RULES THAT SHAPE EVERYTHING HERE

  A quest is not replaced until it is FINISHED. "One per day" is a rate limit, not an
  expiry: an unfinished quest carries over indefinitely, because a technique worth a day
  of painting cannot be a thing that silently vanishes at midnight while you are still
  working on it. `daily_quest` therefore only ever hands out a new one when there is no
  live assignment at all.

  A REJECTION does not end the quest either -- it clears the submission and lets the
  player try again on the same technique. Rejecting is feedback, not a punishment, and
  the alternative (burning the day's quest on a bad photo) makes moderators reluctant to
  reject anything.

  REWARDS ARE PAID EXACTLY ONCE, at acceptance, keyed on the submission id. Review is a
  human pressing a button in a web page, which is precisely the kind of thing that gets
  double-tapped on a slow connection.

Storage is the usual one JSON file per chat, in stats._stats_dir() next to pets' and
economy's own, and gold is granted through economy.py rather than reimplemented -- the
same contract pets.py documents at length.

The reward table is data, not code: REWARDS_BY_DIFFICULTY is the default, and a chat's
moderators can edit any of it from the admin tab (see set_reward). Overrides are stored
per chat, so tuning one chat's economy never touches another's.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import secrets
import threading
from datetime import datetime, timedelta

import economy
import pets
import pets_quest_catalog as catalog
import stats
from app_time import now as app_now

STORE_VERSION = 1
_lock = threading.RLock()

# Two rerolls per quest, as commissioned. Deliberately per ASSIGNMENT rather than per day:
# rerolling is for "I cannot paint that this week", and a player who rerolls twice and then
# takes three days over the result should not come back to a fresh pair of rerolls on a
# quest they have already started.
REROLLS_PER_QUEST = 2
# Every difficulty a painting challenge can sit at, for reroll's climb.
DIFFICULTIES = catalog.DIFFICULTIES

# What an accepted quest pays, by difficulty 1-5.
#
# Calibrated against what already exists rather than invented: a won arena fight pays
# 15-30 gold and 100 pet XP, a six-hour farm shift pays 14-33 gold, the daily bonus tops
# out at 100, and posting a #япокрасил pays 500 all by itself. A quest is a real evening
# (or week) at the desk AND a moderator's time, so it sits above all of those -- but the
# paint itself is still what earns the 500, and this is on top of it.
#
# The drop chance is the outlier on purpose: an arena win rolls 15% and a farm shift 3%,
# because those happen many times a day. A quest happens once, so it has to be worth
# opening -- by difficulty 5 a find is likelier than not.
REWARDS_BY_DIFFICULTY = {
    1: {"gold": 150, "xp": 60,  "tickets": 1, "drop_chance": 0.20},
    2: {"gold": 250, "xp": 100, "tickets": 1, "drop_chance": 0.30},
    3: {"gold": 400, "xp": 160, "tickets": 1, "drop_chance": 0.42},
    4: {"gold": 600, "xp": 240, "tickets": 1, "drop_chance": 0.58},
    5: {"gold": 900, "xp": 360, "tickets": 1, "drop_chance": 0.75},
}
REWARD_FIELDS = ("gold", "xp", "tickets", "drop_chance")
# Ceilings for the admin editor. Not paranoia about moderators -- a mistyped 5000 in a
# text field is a chat's economy gone, and there is no undo for coins already spent.
REWARD_LIMITS = {
    "gold": (0, 5_000), "xp": (0, 5_000), "tickets": (0, 10), "drop_chance": (0.0, 1.0),
}

HISTORY_LIMIT = 400
SUBMISSION_LIMIT = 400

# `#quest-nmm`, anywhere in a caption, case-insensitively. The trailing boundary keeps
# "#quest-nmm-v2" from being read as "#quest-nmm".
_HASHTAG_RE = re.compile(r"#quest-([a-z0-9-]+)", re.IGNORECASE)


def parse_hashtag(text: str) -> str | None:
    """The quest code in a caption, or None. Unknown codes are not codes."""
    for match in _HASHTAG_RE.finditer(str(text or "")):
        code = match.group(1).lower().rstrip("-")
        if catalog.find_quest(code) is not None:
            return code
    return None


# --- storage --------------------------------------------------------------------------


def _path(entry: str):
    return stats._stats_dir() / f"{stats._cache_key(entry)}_quests.json"


def _empty() -> dict:
    return {
        "version": STORE_VERSION,
        "assignments": {},     # user_id -> the one live assignment
        "submissions": [],     # newest last; a rolling audit of every photo sent in
        "history": [],         # newest last; one row per finished quest
        "rewards": {},         # difficulty -> partial override of REWARDS_BY_DIFFICULTY
        "taken": {},           # user_id -> real quests they have picked up
        "disabled": [],        # quest codes a moderator has taken out of rotation
    }


def _load(entry: str) -> dict:
    try:
        data = json.loads(_path(entry).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    base = _empty()
    for key, blank in base.items():
        if key == "version":
            continue
        value = data.get(key)
        base[key] = value if isinstance(value, type(blank)) else blank
    base["assignments"] = {
        str(uid): row for uid, row in base["assignments"].items() if isinstance(row, dict)
    }
    base["taken"] = {
        str(uid): [row for row in rows if isinstance(row, dict)]
        for uid, rows in base["taken"].items() if isinstance(rows, list)
    }
    base["submissions"] = [row for row in base["submissions"] if isinstance(row, dict)]
    base["history"] = [row for row in base["history"] if isinstance(row, dict)]
    base["disabled"] = [str(code) for code in base["disabled"]]
    return base


def _save(entry: str, data: dict) -> None:
    data["version"] = STORE_VERSION
    data["submissions"] = data["submissions"][-SUBMISSION_LIMIT:]
    data["history"] = data["history"][-HISTORY_LIMIT:]
    stats._write_json_atomic(_path(entry), data)


# --- the reward table -----------------------------------------------------------------


def _clamp_reward(field: str, value):
    low, high = REWARD_LIMITS[field]
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    number = min(max(number, low), high)
    return number if field == "drop_chance" else int(round(number))


def rewards_for(entry: str, difficulty: int, data: dict | None = None) -> dict:
    """What difficulty `difficulty` pays in this chat, defaults plus any override."""
    level = min(max(1, int(difficulty or 1)), 5)
    reward = dict(REWARDS_BY_DIFFICULTY[level])
    stored = (data or _load(entry)).get("rewards", {}).get(str(level))
    if isinstance(stored, dict):
        for field in REWARD_FIELDS:
            if field in stored:
                fixed = _clamp_reward(field, stored[field])
                if fixed is not None:
                    reward[field] = fixed
    return reward


def reward_table(entry: str) -> list[dict]:
    """Every difficulty's current payout, for the admin editor and the quest card."""
    data = _load(entry)
    return [
        {"difficulty": level, "default": dict(REWARDS_BY_DIFFICULTY[level]),
         **rewards_for(entry, level, data)}
        for level in sorted(REWARDS_BY_DIFFICULTY)
    ]


def set_reward(entry: str, difficulty, field: str, value) -> tuple[bool, str]:
    """Edit one number in the reward table. Moderators only -- the caller enforces that."""
    if field not in REWARD_FIELDS:
        return False, "Неизвестное поле награды."
    try:
        level = int(difficulty)
    except (TypeError, ValueError):
        return False, "Неизвестная сложность."
    if level not in REWARDS_BY_DIFFICULTY:
        return False, "Сложность бывает от 1 до 5."
    fixed = _clamp_reward(field, value)
    if fixed is None:
        return False, "Это не число."
    with _lock:
        data = _load(entry)
        data.setdefault("rewards", {}).setdefault(str(level), {})[field] = fixed
        _save(entry, data)
    return True, f"Сложность {level}: {field} = {fixed}."


def set_quest_enabled(entry: str, code: str, enabled: bool) -> tuple[bool, str]:
    """Take one quest out of the daily rotation, or put it back.

    Live assignments are left alone: somebody halfway through painting a stone texture
    should not have it disappear because a moderator retired the quest this morning.
    """
    quest = catalog.find_quest(code)
    if quest is None:
        return False, "Такого квеста нет."
    with _lock:
        data = _load(entry)
        disabled = set(data.get("disabled", []))
        if enabled:
            disabled.discard(quest.code)
        else:
            disabled.add(quest.code)
        # Never disable the whole board: an empty pool means nobody can be given a quest
        # at all, and the failure would show up a day later as silence.
        if len(disabled) >= len(catalog.QUESTS):
            return False, "Нельзя выключить все квесты сразу."
        data["disabled"] = sorted(disabled)
        _save(entry, data)
    return True, ("Квест снова в ротации." if enabled else "Квест убран из ротации.")


def available_quests(entry: str, data: dict | None = None) -> tuple:
    """Painting challenges still in rotation. Real quests are never dealt, only taken."""
    data = data if data is not None else _load(entry)
    disabled = set(data.get("disabled", []))
    pool = tuple(quest for quest in catalog.PAINT_QUESTS if quest.code not in disabled)
    return pool or catalog.PAINT_QUESTS


# --- assignment -----------------------------------------------------------------------


def _quest_payload(entry: str, quest, data: dict) -> dict:
    return {
        "code": quest.code,
        "hashtag": catalog.hashtag(quest.code),
        "title": quest.title,
        "subject": quest.subject,
        "technique": quest.technique,
        "hint": quest.hint,
        "tool": quest.tool,
        "difficulty": quest.difficulty,
        "reward": rewards_for(entry, quest.difficulty, data),
    }


def _pick(entry: str, user_id, data: dict, exclude: set[str], difficulty: int | None = None):
    """A random painting challenge this player is not already looking at.

    Weighted by nothing: every technique in rotation is equally likely, and difficulty is
    what the reward scales on rather than what the odds do. Falls back to the full pool if
    the exclusions would empty it -- a player who has rerolled everything still gets a
    quest rather than an error.

    `difficulty` pins the rung, which is what makes a reroll cost something (see reroll).
    If a moderator has disabled every quest at that level, it widens rather than failing:
    a harder quest than asked for is still a quest, an exception is a broken button.
    """
    pool = [quest for quest in available_quests(entry, data) if quest.code not in exclude]
    if difficulty is not None:
        at_level = [quest for quest in pool if quest.difficulty == difficulty]
        if at_level:
            return random.choice(at_level)
        harder = [quest for quest in pool if quest.difficulty >= difficulty]
        if harder:
            return random.choice(harder)
    if not pool:
        pool = list(available_quests(entry, data))
    return random.choice(pool)


def _live_assignment(data: dict, user_id) -> dict | None:
    row = data.get("assignments", {}).get(str(user_id))
    if not isinstance(row, dict) or row.get("status") == "done":
        return None
    return row if catalog.find_quest(row.get("code")) else None


def daily_quest(entry: str, user_id, now: datetime | None = None) -> dict:
    """This player's live quest, assigning one if they have none.

    Deliberately NOT "today's quest": an unfinished quest is never replaced, so somebody
    who takes four days over a hard technique keeps it for four days. The day stamp is
    only a rate limit on being handed a NEW one -- finish today's and the next arrives
    tomorrow, not immediately, which is what stops a fast painter from clearing the
    board in an afternoon.
    """
    moment = now or app_now()
    today = moment.date().isoformat()
    with _lock:
        data = _load(entry)
        live = _live_assignment(data, user_id)
        if live is None:
            last_done = data.get("assignments", {}).get(str(user_id)) or {}
            if last_done.get("status") == "done" and str(last_done.get("finished_day")) == today:
                # Finished one already today. Show it, and say when the next is due.
                return {
                    "quest": None, "status": "resting", "next_day": "завтра",
                    "rerolls_left": 0, "submission": None,
                    "last": _quest_payload(entry, catalog.find_quest(last_done["code"]), data)
                    if catalog.find_quest(last_done.get("code")) else None,
                }
            quest = _pick(entry, user_id, data, exclude=set())
            live = {
                "code": quest.code,
                "day": today,
                "assigned_at": moment.isoformat(),
                "rerolls_used": 0,
                "status": "open",
                "submission_id": None,
            }
            data.setdefault("assignments", {})[str(user_id)] = live
            _save(entry, data)
        quest = catalog.find_quest(live["code"])
        submission = _find_submission(data, live.get("submission_id"))
        return {
            "quest": _quest_payload(entry, quest, data),
            # Two of the four reward legs need a creature to land in (see _pay). Said
            # here, on the card, rather than discovered at payout: somebody deciding
            # whether to spend an evening on this deserves to know beforehand.
            "has_pet": pets.get_pet(entry, user_id) is not None,
            "status": live.get("status", "open"),
            "day": live.get("day"),
            "rerolls_left": max(0, REROLLS_PER_QUEST - int(live.get("rerolls_used", 0) or 0)),
            "submission": _public_submission(submission) if submission else None,
        }


def reroll(entry: str, user_id, now: datetime | None = None) -> tuple[bool, str]:
    """Swap the daily challenge for a HARDER one, twice per quest.

    A reroll costs something. Without that it is just a free "spin until I get an easy
    one", and the reward table -- which pays by difficulty -- would be handing out the
    top payouts for the least work. Each reroll therefore climbs one rung, so two of them
    turn a level-1 challenge into a level-3 one, with the money to match.

    At difficulty 5 there is nowhere higher to go, so a reroll there simply deals another
    5. That is deliberate rather than a refusal: somebody who cannot paint THIS hard
    technique should still be able to trade it for a different hard one.
    """
    moment = now or app_now()
    with _lock:
        data = _load(entry)
        live = _live_assignment(data, user_id)
        if live is None:
            return False, "Сейчас нет активного квеста."
        if live.get("status") == "review":
            return False, "Работа уже на проверке — дождись ответа."
        used = int(live.get("rerolls_used", 0) or 0)
        if used >= REROLLS_PER_QUEST:
            return False, "Реролов больше нет."
        current = catalog.find_quest(live["code"])
        harder = min(max(1, int(getattr(current, "difficulty", 1) or 1)) + 1, max(DIFFICULTIES))
        quest = _pick(entry, user_id, data, exclude={live["code"]}, difficulty=harder)
        live["code"] = quest.code
        live["rerolls_used"] = used + 1
        live["assigned_at"] = moment.isoformat()
        live["submission_id"] = None
        live["status"] = "open"
        _save(entry, data)
    left = REROLLS_PER_QUEST - (used + 1)
    return True, (
        f"Новый квест: «{quest.title}» (сложность {quest.difficulty}). "
        f"Реролов осталось: {left}."
    )


# --- submissions ----------------------------------------------------------------------


# --- Квесты в реале -------------------------------------------------------------------
#
# Listed, not dealt. The daily painting challenge is a slot that always holds something;
# these are a shelf a player takes from, which is why there is no second slot standing
# empty on the days nobody has a real quest -- and why adding one is a row in the
# catalogue and nothing else.


def _moment_like(value, reference: datetime) -> datetime | None:
    """Parse a stored timestamp so it can be compared with `reference`.

    Stored times are written by app_now() and are timezone-aware; a caller (a test, a
    replayed fixture) may hand in a naive one. Python refuses to compare the two, so the
    parsed value is matched to whatever the reference is -- the same accommodation
    pets._checkpoint_at makes, and for the same reason: a cooldown must never raise.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None and reference.tzinfo is not None:
        return moment.replace(tzinfo=reference.tzinfo)
    if moment.tzinfo is not None and reference.tzinfo is None:
        return moment.replace(tzinfo=None)
    return moment


def _taken_rows(data: dict, user_id) -> list[dict]:
    rows = data.setdefault("taken", {})
    if not isinstance(rows, dict):
        rows = data["taken"] = {}
    mine = rows.setdefault(str(user_id), [])
    if not isinstance(mine, list):
        mine = rows[str(user_id)] = []
    return [row for row in mine if isinstance(row, dict)]


def _live_taken(data: dict, user_id, code: str) -> dict | None:
    for row in _taken_rows(data, user_id):
        if row.get("code") == code and row.get("status") in ("open", "review"):
            return row
    return None


def _cooldown_until(quest, data: dict, user_id, reference: datetime) -> datetime | str | None:
    """When this player may take `quest` again.

    None means now, "never" means it is a once-ever quest already done, and a datetime is
    the moment the cooldown lifts. A cooldown of 0 IS once-ever: buying a loupe cannot be
    repeated, while tidying the bench is worth doing again in a fortnight.

    Counted from THIS PLAYER'S own completion, which is also what spreads a fortnightly
    quest across the chat instead of handing it to everybody on the 1st and the 15th:
    finish on the 3rd and it returns on the 17th, finish on the 9th and it returns on the
    23rd. No separate staggering machinery, and -- more importantly -- one source of
    truth, so the list and `take_quest` can never disagree about whether it is available.
    """
    finished = [
        row for row in _taken_rows(data, user_id)
        if row.get("code") == quest.code and row.get("status") == "done"
    ]
    if not finished:
        return None
    if quest.cooldown_days <= 0:
        return "never"
    stamps = [_moment_like(row.get("finished_at"), reference) for row in finished]
    last = max((stamp for stamp in stamps if stamp is not None), default=None)
    return last + timedelta(days=quest.cooldown_days) if last else None


def real_quests(entry: str, user_id, now: datetime | None = None) -> list[dict]:
    """The shelf: every real quest, with whether this player can take it right now."""
    moment = now or app_now()
    data = _load(entry)
    disabled = set(data.get("disabled", []))
    rows = []
    for quest in catalog.REAL_QUESTS:
        if quest.code in disabled:
            continue
        taken = _live_taken(data, user_id, quest.code)
        until = _cooldown_until(quest, data, user_id, moment)
        available, reason = True, ""
        if taken is not None:
            available, reason = False, ("на проверке" if taken.get("status") == "review" else "взят")
        elif until == "never":
            available, reason = False, "уже сдан"
        elif isinstance(until, datetime) and moment < until:
            available, reason = False, f"снова с {until.date().isoformat()}"
        rows.append({
            **_quest_payload(entry, quest, data),
            "proof": quest.proof,
            "badge": quest.badge,
            "cooldown_days": quest.cooldown_days,
            "available": available,
            "reason": reason,
            "status": (taken or {}).get("status", ""),
        })
    rows.sort(key=lambda row: (not row["available"], row["difficulty"]))
    return rows


def take_quest(entry: str, user_id, code: str, now: datetime | None = None) -> tuple[bool, str]:
    """Put one real quest on this player's list. Painting challenges are not takeable."""
    moment = now or app_now()
    quest = catalog.find_quest(code)
    if quest is None or quest.kind != "real":
        return False, "Такого квеста в реале нет."
    with _lock:
        data = _load(entry)
        if quest.code in set(data.get("disabled", [])):
            return False, "Этот квест сейчас выключен."
        if _live_taken(data, user_id, quest.code) is not None:
            return False, "Этот квест уже взят."
        until = _cooldown_until(quest, data, user_id, moment)
        if until == "never":
            return False, "Этот квест уже сдан — его проходят один раз."
        if isinstance(until, datetime) and moment < until:
            return False, f"Снова можно будет взять {until.date().isoformat()}."
        data.setdefault("taken", {}).setdefault(str(user_id), []).append({
            "code": quest.code,
            "taken_at": moment.isoformat(),
            "status": "open",
            "submission_id": None,
        })
        _save(entry, data)
    return True, f"Квест взят: «{quest.title}». Хештег — {catalog.hashtag(quest.code)}."


def drop_quest(entry: str, user_id, code: str) -> tuple[bool, str]:
    """Put a taken quest back on the shelf, as long as nothing is under review for it."""
    with _lock:
        data = _load(entry)
        row = _live_taken(data, user_id, code)
        if row is None:
            return False, "Этот квест не взят."
        if row.get("status") == "review":
            return False, "Работа уже на проверке — дождись ответа."
        data["taken"][str(user_id)] = [
            other for other in _taken_rows(data, user_id) if other is not row
        ]
        _save(entry, data)
    quest = catalog.find_quest(code)
    return True, f"Квест отложен: «{quest.title if quest else code}»."


def _find_submission(data: dict, submission_id) -> dict | None:
    if not submission_id:
        return None
    for row in data.get("submissions", []):
        if str(row.get("id")) == str(submission_id):
            return row
    return None


def _public_submission(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "code": row.get("code"),
        "status": row.get("status"),
        "at": row.get("ts"),
        "note": row.get("note") or "",
        "reviewed_by_name": row.get("reviewed_by_name") or "",
    }


def submit(
    entry: str, user_id, code: str, *, chat_id=None, message_id=None, photo_file_id=None,
    author_name: str = "", author_username: str = "", now: datetime | None = None,
) -> tuple[bool, str]:
    """Record a photo posted with a quest hashtag, for a moderator to look at.

    Refuses a hashtag that is not the player's OWN live quest. Otherwise the hashtag would
    be the whole game: post `#quest-nmm` under anything and collect, with the daily
    assignment reduced to a suggestion.
    """
    moment = now or app_now()
    quest = catalog.find_quest(code)
    if quest is None:
        return False, "Такого квеста нет."
    with _lock:
        data = _load(entry)
        # A real quest is proved against the row the player TOOK; a painting challenge
        # against the one slot they were dealt. Either way the hashtag has to match
        # something they actually hold -- otherwise the tag alone would be the whole game.
        if quest.kind == "real":
            live = _live_taken(data, user_id, quest.code)
            if live is None:
                return False, (
                    f"Этот квест не взят. Открой «Квесты в реале» и возьми "
                    f"«{quest.title}»."
                )
        else:
            live = _live_assignment(data, user_id)
            if live is None:
                return False, "У тебя нет активного квеста — открой «Квесты» в /arena."
            if live["code"] != quest.code:
                active = catalog.find_quest(live["code"])
                return False, (
                    f"Сейчас у тебя другой челлендж: «{active.title}». "
                    f"Его хештег — {catalog.hashtag(active.code)}."
                )
        if live.get("status") == "review":
            return False, "Работа по этому квесту уже на проверке."
        row = {
            "id": f"{moment.strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}",
            "kind": quest.kind,
            "proof": quest.proof,
            "user_id": str(user_id),
            "author_name": str(author_name or ""),
            "author_username": str(author_username or "").lstrip("@"),
            "code": quest.code,
            "difficulty": quest.difficulty,
            "chat_id": chat_id,
            "message_id": message_id,
            "photo_file_id": photo_file_id,
            "ts": moment.isoformat(),
            "status": "pending",
            "reviewed_by": None,
            "reviewed_by_name": "",
            "reviewed_at": None,
            "note": "",
            "paid": None,
        }
        data.setdefault("submissions", []).append(row)
        live["submission_id"] = row["id"]
        live["status"] = "review"
        _save(entry, data)
    return True, f"Работа по квесту «{quest.title}» отправлена на проверку."


def pending(entry: str) -> list[dict]:
    """Everything waiting for a moderator, oldest first -- a review queue, not a feed."""
    data = _load(entry)
    rows = [dict(row) for row in data.get("submissions", []) if row.get("status") == "pending"]
    for row in rows:
        quest = catalog.find_quest(row.get("code"))
        row["title"] = quest.title if quest else row.get("code")
        row["subject"] = quest.subject if quest else ""
        row["reward"] = rewards_for(entry, row.get("difficulty", 1), data)
    return rows


def submissions(entry: str, user_id=None, limit: int = 50) -> list[dict]:
    """The audit trail, newest first: every photo sent in and what became of it."""
    data = _load(entry)
    rows = []
    for row in reversed(data.get("submissions", [])):
        if user_id is not None and str(row.get("user_id")) != str(user_id):
            continue
        quest = catalog.find_quest(row.get("code"))
        rows.append({
            **_public_submission(row),
            "user_id": row.get("user_id"),
            "author_name": row.get("author_name"),
            "title": quest.title if quest else row.get("code"),
            "difficulty": row.get("difficulty"),
        })
        if len(rows) >= max(0, int(limit)):
            break
    return rows


def history(entry: str, user_id=None, limit: int = 50) -> list[dict]:
    """Finished quests, newest first, with what each one actually paid."""
    data = _load(entry)
    rows = []
    for row in reversed(data.get("history", [])):
        if user_id is not None and str(row.get("user_id")) != str(user_id):
            continue
        quest = catalog.find_quest(row.get("code"))
        rows.append({
            **row,
            "title": quest.title if quest else row.get("code"),
            "subject": quest.subject if quest else "",
        })
        if len(rows) >= max(0, int(limit)):
            break
    return rows


# --- review ---------------------------------------------------------------------------


def review(
    entry: str, submission_id, reviewer_id, accept: bool, *, reviewer_name: str = "",
    note: str = "", now: datetime | None = None,
) -> tuple[bool, str, dict]:
    """Accept or reject one submission. Returns (ok, message, receipt).

    Everything that is paid is paid HERE and only here, and only on the transition out of
    "pending" -- a second press finds the row already decided and changes nothing, which
    matters because this is a web button a moderator will double-tap.

    No chat XP is threaded through, unlike every SPEND in this codebase: a quest only ever
    credits, and economy.grant_once needs no balance read to do that.
    """
    moment = now or app_now()
    receipt: dict = {}
    with _lock:
        data = _load(entry)
        row = _find_submission(data, submission_id)
        if row is None:
            return False, "Заявка не найдена.", receipt
        if row.get("status") != "pending":
            return False, "Эту заявку уже рассмотрели.", receipt
        quest = catalog.find_quest(row.get("code"))
        if quest is None:
            return False, "Квест больше не существует.", receipt

        row["status"] = "accepted" if accept else "rejected"
        row["reviewed_by"] = str(reviewer_id)
        row["reviewed_by_name"] = str(reviewer_name or "")
        row["reviewed_at"] = moment.isoformat()
        row["note"] = str(note or "")[:300]

        live = (
            _live_taken(data, row["user_id"], quest.code) if quest.kind == "real"
            else data.get("assignments", {}).get(str(row["user_id"]))
        )
        if accept:
            reward = rewards_for(entry, quest.difficulty, data)
            receipt = {
                "user_id": row["user_id"], "code": quest.code, "title": quest.title,
                "difficulty": quest.difficulty, "kind": quest.kind,
                "badge": quest.badge, "author_name": row.get("author_name", ""),
                **reward,
            }
            row["paid"] = dict(reward)
            if isinstance(live, dict) and live.get("submission_id") == row["id"]:
                live["status"] = "done"
                live["finished_day"] = moment.date().isoformat()
                live["finished_at"] = moment.isoformat()
            data.setdefault("history", []).append({
                "user_id": row["user_id"], "author_name": row.get("author_name", ""),
                "code": quest.code, "difficulty": quest.difficulty,
                "submission_id": row["id"], "finished_at": moment.isoformat(),
                "day": moment.date().isoformat(), "outcome": "accepted", **reward,
            })
        elif isinstance(live, dict) and live.get("submission_id") == row["id"]:
            # Back to work on the SAME quest. A rejection is feedback, not a lost day.
            live["status"] = "open"
            live["submission_id"] = None
        _save(entry, data)

    if not accept:
        return True, "Заявка отклонена.", receipt

    # Paid outside the lock: these are three independent stores (economy, pets, pets'
    # ticket wallet), each with its own locking, and holding this one across them is how
    # a deadlock gets built. The submission is already marked accepted, so a crash here
    # cannot pay twice -- it can only fail to pay, which the receipt makes visible.
    paid = _pay(entry, receipt, submission_id)
    receipt.update(paid)
    if receipt.get("badge"):
        receipt["badge_given"] = _award_badge(
            entry, receipt["user_id"], receipt["badge"], receipt.get("author_name", ""),
        )
    message = f"Принято. Начислено: {paid['gold']} монет"
    if paid["xp"]:
        message += f", {paid['xp']} опыта"
    if not paid["has_pet"]:
        message += " (опыт и находка не начислены — у игрока нет существа)"
    return True, message + ".", receipt


# A quest badge is the bot's own, like the founder badge stats.py already keeps: created
# on first award with a FIXED id derived from the quest code, so it survives restarts,
# never duplicates, and is exempt from a chat's custom-badge budget -- somebody who tidied
# their bench must not miss out because the moderators had used up their badge slots.
QUEST_BADGE_EMOJI = "🧽"


def _award_badge(entry: str, user_id, name: str, display_name: str) -> bool:
    """Create this quest's badge if needed and give it. True if it was newly awarded."""
    badge_id = "quest-" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    try:
        data = stats._load_custom_badge_data(entry)
        if badge_id not in data["badges"]:
            data["badges"][badge_id] = {
                "id": badge_id,
                "emoji": QUEST_BADGE_EMOJI,
                "name": name,
                "created_at": app_now().isoformat(),
                "created_by_id": "bot",
                "created_by_name": "ЕПХ-бот",
            }
            stats._save_custom_badge_data(entry, data)
        _badge, newly = stats.give_custom_badge(
            entry, badge_id, user_id, display_name or str(user_id), "bot", "ЕПХ-бот",
        )
        return newly
    except (OSError, ValueError, KeyError):
        # A badge is the garnish on a reward that has already been paid. Losing it must
        # never turn an accepted quest into an error the moderator has to retry.
        return False


def _pay(entry: str, receipt: dict, submission_id) -> dict:
    """Hand over the reward, and report what ACTUALLY landed.

    Two of the four legs need a tamed creature to land in, and quests deliberately do not
    require one: a quest is a painting task, and the chat is full of painters who have
    never bought a cage -- the same reasoning that puts the farm-ticket wallet at the top
    of pets' store rather than on the pet record. Gold and tickets therefore pay either
    way, while XP and the drop simply have nowhere to go.

    What must NOT happen is telling somebody they were paid experience that went nowhere,
    which is exactly what reporting the nominal reward here used to do: pets.award_xp
    silently returns (1, 0) for a user with no pet, and grant_random_drop declines for the
    same reason, so neither leg reports its own failure. Zeroed and flagged here instead,
    so the receipt and every message built from it describe reality.
    """
    user_id = receipt["user_id"]
    has_pet = pets.get_pet(entry, user_id) is not None
    gold = int(receipt.get("gold", 0) or 0)
    if gold:
        economy.grant_once(entry, user_id, gold, f"quest:{submission_id}")
    xp = int(receipt.get("xp", 0) or 0) if has_pet else 0
    if xp:
        pets.award_xp(entry, user_id, xp)
    # A ticket needs no creature: the wallet holding it does not live on the pet record.
    tickets = int(receipt.get("tickets", 0) or 0)
    for index in range(tickets):
        pets.grant_farm_ticket(entry, user_id, f"quest:{submission_id}:{index}")
    dropped = pets.grant_random_drop(
        entry, user_id, float(receipt.get("drop_chance", 0.0) or 0.0),
        seed=f"quest:{submission_id}",
    ) if has_pet else None
    return {
        "gold": gold, "xp": xp, "tickets": tickets, "has_pet": has_pet,
        "item": dropped.get("code") if dropped else None,
        "item_name": dropped.get("name") if dropped else None,
        "item_rarity": dropped.get("rarity") if dropped else None,
        "auto_equipped": bool(dropped.get("auto_equipped")) if dropped else False,
    }


def mail_events(entry: str, user_id, limit: int = 30) -> list[dict]:
    """Quest verdicts, shaped like a pets.mail event so the mailbox can carry them.

    Without this a player's coins simply appear: the moderator presses accept in a web
    page they cannot see, and the only trace is a balance that went up. A verdict is
    exactly the kind of thing the mailbox exists to tell somebody about -- and building it
    as an event here, rather than as a notification, means a rejection is waiting for them
    whenever they next look instead of depending on a DM having been delivered.

    Returned to the CALLER to hand to pets.mail rather than pushed there directly: this
    module imports pets, so the arrow cannot point back.
    """
    rows = []
    for row in reversed(_load(entry).get("submissions", [])):
        if str(row.get("user_id")) != str(user_id) or row.get("status") == "pending":
            continue
        quest = catalog.find_quest(row.get("code"))
        paid = row.get("paid") or {}
        accepted = row.get("status") == "accepted"
        rows.append({
            "kind": "quest_ok" if accepted else "quest_no",
            "outcome": "win" if accepted else "loss",
            "ts": row.get("reviewed_at") or row.get("ts"),
            "coins": int(paid.get("gold", 0) or 0),
            "xp": int(paid.get("xp", 0) or 0),
            "tickets": int(paid.get("tickets", 0) or 0),
            "pet_name": quest.title if quest else row.get("code"),
            "owner_name": row.get("reviewed_by_name") or "",
            "note": row.get("note") or "",
            "item": None, "item_name": None, "item_rarity": None, "auto_equipped": False,
        })
        if len(rows) >= max(0, int(limit)):
            break
    return rows


def stats_for(entry: str, user_id) -> dict:
    """How many this player has finished, and the hardest one they have cleared."""
    rows = [row for row in _load(entry).get("history", [])
            if str(row.get("user_id")) == str(user_id) and row.get("outcome") == "accepted"]
    return {
        "done": len(rows),
        "best_difficulty": max((int(row.get("difficulty", 1) or 1) for row in rows), default=0),
        "gold": sum(int(row.get("gold", 0) or 0) for row in rows),
    }
