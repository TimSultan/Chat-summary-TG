"""Quests: assignment, submission, review and history.

Two kinds, both from pets_quest_catalog.py, both proved the same way -- post a photo in
the chat with the quest's own hashtag (`#quest_nmm`), and a moderator accepts or rejects
it from the Mini App. Only an accepted submission pays.

  PAINTING CHALLENGES -- one small thing painted in one named technique.

  КВЕСТЫ В РЕАЛЕ -- tidy the bench, walk six kilometres, buy a loupe.

Painting challenges are dealt as three cards and stay until the painter completes or
manually rerolls them; a paint deadline only punishes slow, careful work. The real-life
board still lasts 24 hours. Each of the three groups can be rerolled as a whole once every
12 hours and follows the same submission and review path.

THREE RULES THAT SHAPE EVERYTHING HERE

  A PAINT BOARD has no automatic deadline. Its cards only change through completion and
  the player's explicit group reroll, so Telegram and the Mini App always describe the
  same set.

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
import pets_config as C
import pets_quest_catalog as catalog
import stats
from app_time import now as app_now

STORE_VERSION = 1
_lock = threading.RLock()

# One explicit refresh per group every twelve hours. Kept server-side and shared by the
# web/Telegram clients so reopening the page or pressing an old callback cannot bypass it.
REROLLS_PER_QUEST = 1  # compatibility name for older clients; rerolls are group-wide now
REROLL_COOLDOWN = timedelta(hours=12)
# Every difficulty a painting challenge can sit at, for reroll's climb.
DIFFICULTIES = catalog.DIFFICULTIES

# What an accepted quest pays, by difficulty 1-5.
#
# Calibrated against what already exists rather than invented: a won arena fight pays
# 15-30 gold and 100 pet XP, a six-hour farm shift pays 14-33, the daily bonus tops out
# at 100, and posting a #япокрасил pays 500 all by itself.
#
# The top of this table used to be 900, which was wrong twice over. It put one accepted
# quest at nearly two #япокрасил posts -- and the paint post is meant to be the thing
# that actually pays, with a quest as the bonus on top of it. And it was priced when a
# player could only hold ONE quest; there are two live slots now (a painting challenge
# and a Квест в реале), so the same numbers would have quietly doubled the daily take.
#
# 450 at the top instead: below a single paint post, and both slots cleared in one day
# by a lucky player still lands under it. The curve is compressed as well as lowered, so
# the escalating reroll stays worth taking without the top rung being the only one worth
# painting.
#
# The drop chance came down with it for the same reason: two quests a day at 75% was a
# faster item stream than the arena's own 15% per win.
REWARDS_BY_DIFFICULTY = {
    1: {"gold": 80,  "xp": 40,  "tickets": 1, "drop_chance": 0.12},
    2: {"gold": 130, "xp": 70,  "tickets": 1, "drop_chance": 0.18},
    3: {"gold": 200, "xp": 110, "tickets": 1, "drop_chance": 0.26},
    4: {"gold": 300, "xp": 160, "tickets": 1, "drop_chance": 0.36},
    5: {"gold": 450, "xp": 240, "tickets": 1, "drop_chance": 0.50},
}
REWARD_FIELDS = ("gold", "xp", "tickets", "drop_chance")
# Ceilings for the admin editor. Not paranoia about moderators -- a mistyped 5000 in a
# text field is a chat's economy gone, and there is no undo for coins already spent.
REWARD_LIMITS = {
    "gold": (0, 5_000), "xp": (0, 5_000), "tickets": (0, 10), "drop_chance": (0.0, 1.0),
}

HISTORY_LIMIT = 400
SUBMISSION_LIMIT = 400

# `#quest_nmm` anywhere in a caption, case-insensitively.
#
# BOTH separators are accepted after "quest" and inside the code. The tags are written
# with underscores now, because Telegram ends a hashtag at the first character outside
# letters/digits/underscore -- `#quest-nmm` posted as the tag `#quest` plus loose text,
# which neither highlighted nor grouped in search. Hyphens stay readable here so a
# caption written before that change, or copied from an old message, still counts.
_HASHTAG_RE = re.compile(r"#quest[_-]([a-z0-9_-]+)", re.IGNORECASE)


def parse_hashtag(text: str) -> str | None:
    """The quest code in a caption, or None. Unknown codes are not codes."""
    for match in _HASHTAG_RE.finditer(str(text or "")):
        code = catalog.normalise_code(match.group(1).rstrip("-_"))
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
        "real_assignments": {},  # user_id -> the one live Квест в реале
        "rune_assignments": {},  # user_id -> five elemental/tool rune challenges
        # Its own slot. _load rebuilds this store from _empty()'s keys, so a board that
        # is not declared here is silently discarded on every read -- which reads as a
        # shelf that re-deals itself on every open.
        "gear_assignments": {},  # user_id -> five arena-upgrade paint challenges
        "done": {},            # user_id -> {quest code: when it was last finished}
        "moderators": {},      # user_id -> who may review, delegated by an admin
        "disabled": [],        # quest codes a moderator has taken out of rotation
        "overrides": {},       # quest code -> per-chat editable text fields
        "ideas": [],           # player-suggested quest ideas, newest last
        "reroll_cooldowns": {},  # user_id -> kind -> last successful group reroll
        # Bot API photo ids may arrive just before Telethon records the matching quest
        # submission. Keep that tiny race durable so an accepted personal-paint reward
        # never loses the artwork it is meant to carry.
        "submission_photos": {},  # "chat_id:message_id" -> {file_id, ts}
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
    base["real_assignments"] = {
        str(uid): row for uid, row in base["real_assignments"].items() if isinstance(row, dict)
    }
    base["rune_assignments"] = {
        str(uid): row for uid, row in base["rune_assignments"].items() if isinstance(row, dict)
    }
    base["gear_assignments"] = {
        str(uid): row for uid, row in base["gear_assignments"].items() if isinstance(row, dict)
    }
    # Codes are canonicalised on READ rather than migrated on disk -- the same thing
    # pets._load does for legacy item codes. Quest codes used hyphens until Telegram's
    # hashtag parser turned out to end a tag at one (see catalog.normalise_code), and a
    # stored `done` key that no longer matched its quest would silently reopen a
    # once-ever quest and lose every cooldown.
    base["done"] = {
        str(uid): {catalog.normalise_code(code): str(when) for code, when in rows.items()}
        for uid, rows in base["done"].items() if isinstance(rows, dict)
    }
    for row in (list(base["assignments"].values()) + list(base["real_assignments"].values())
                + list(base["rune_assignments"].values())):
        if row.get("code"):
            row["code"] = catalog.normalise_code(row["code"])
        for card in row.get("quests", []) if isinstance(row.get("quests"), list) else []:
            if isinstance(card, dict) and card.get("code"):
                card["code"] = catalog.normalise_code(card["code"])
    base["submissions"] = [row for row in base["submissions"] if isinstance(row, dict)]
    base["history"] = [row for row in base["history"] if isinstance(row, dict)]
    base["moderators"] = {
        str(uid): row for uid, row in base["moderators"].items() if isinstance(row, dict)
    }
    base["disabled"] = [catalog.normalise_code(code) for code in base["disabled"]]
    base["reroll_cooldowns"] = {
        str(uid): {str(kind): str(when) for kind, when in rows.items()}
        for uid, rows in base["reroll_cooldowns"].items() if isinstance(rows, dict)
    }
    base["submission_photos"] = {
        str(key): dict(row) for key, row in base["submission_photos"].items()
        if isinstance(row, dict) and row.get("file_id")
    }
    return base


def _save(entry: str, data: dict) -> None:
    data["version"] = STORE_VERSION
    data["submissions"] = data["submissions"][-SUBMISSION_LIMIT:]
    data["history"] = data["history"][-HISTORY_LIMIT:]
    stats._write_json_atomic(_path(entry), data)


def _submission_photo_key(chat_id, message_id) -> str:
    return f"{chat_id}:{message_id}"


def attach_submission_photo(
    entry: str, chat_id, message_id, photo_file_id: str,
    now: datetime | None = None,
) -> bool:
    """Attach the Bot API image id to a quest post, regardless of listener order.

    Telegram's Bot API and the Telethon listener observe the same group post on separate
    update streams. Either one can win the race: if the submission exists we enrich it
    immediately; otherwise ``submit`` consumes this small pending record moments later.
    """
    file_id = str(photo_file_id or "").strip()
    if not file_id or chat_id is None or message_id is None:
        return False
    moment = now or app_now()
    key = _submission_photo_key(chat_id, message_id)
    with _lock:
        data = _load(entry)
        for row in reversed(data.get("submissions", [])):
            if (_submission_photo_key(row.get("chat_id"), row.get("message_id")) == key):
                row["photo_file_id"] = file_id
                data.setdefault("submission_photos", {}).pop(key, None)
                _save(entry, data)
                return True
        pending = data.setdefault("submission_photos", {})
        pending[key] = {"file_id": file_id, "ts": moment.isoformat()}
        # A normal match is consumed within one event-loop turn. The cap only protects
        # against unrelated photos carrying a quest-looking caption forever.
        while len(pending) > 500:
            pending.pop(next(iter(pending)))
        _save(entry, data)
    return True


# --- who may review -------------------------------------------------------------------
#
# A quest moderator is NOT a chat administrator and not a badge manager. It is its own
# small delegation, because the two jobs need different trust: reviewing a painting is
# "does this look like NMM", which any experienced painter in the chat can do, while
# chat admin carries the ban button and /badgeadmin hands out badges. Keeping the list
# separate means an admin can hand out review duty without handing out anything else.
#
# The list lives in the quest store rather than in stats.py for the same reason: it is a
# quest-shaped permission, and it should disappear with the quest data if this feature
# ever does.


def _moderator_rows(data: dict) -> dict:
    rows = data.setdefault("moderators", {})
    if not isinstance(rows, dict):
        rows = data["moderators"] = {}
    return rows


def is_moderator(entry: str, user_id=None, username: str | None = None) -> bool:
    """Whether this person has been delegated quest review in this chat.

    Matched on id OR username. The id is the reliable key, but somebody can be appointed
    from a Telegram username before they have ever opened the Mini App, and the signed
    initData the page verifies carries a username too -- so accepting either is what makes
    "add @vasya" work immediately rather than after Vasya's next visit.
    """
    rows = _moderator_rows(_load(entry))
    if user_id is not None and str(user_id) in rows:
        return True
    handle = str(username or "").strip().lstrip("@").lower()
    if not handle:
        return False
    return any(
        str(row.get("username") or "").lower() == handle
        for row in rows.values() if isinstance(row, dict)
    )


def moderators(entry: str) -> list[dict]:
    """Everyone delegated quest review here, newest first."""
    rows = _moderator_rows(_load(entry))
    listed = [
        {"user_id": uid, **row} for uid, row in rows.items() if isinstance(row, dict)
    ]
    listed.sort(key=lambda row: str(row.get("added_at") or ""), reverse=True)
    return listed


def add_moderator(
    entry: str, user_id, username: str | None, display_name: str,
    added_by_id=None, added_by_name: str = "",
) -> tuple[bool, str]:
    """Delegate quest review. False when they already had it, so callers can say so."""
    if not str(user_id or "").strip():
        return False, "Не понял, кого добавлять."
    with _lock:
        data = _load(entry)
        rows = _moderator_rows(data)
        if str(user_id) in rows:
            return False, f"{display_name} и так может проверять квесты."
        rows[str(user_id)] = {
            "username": (username or "").lstrip("@") or None,
            "display_name": display_name,
            "added_at": app_now().isoformat(),
            "added_by_id": str(added_by_id or ""),
            "added_by_name": added_by_name,
        }
        _save(entry, data)
    return True, f"{display_name} теперь может проверять квесты."


def remove_moderator(entry: str, user_id) -> tuple[bool, str]:
    with _lock:
        data = _load(entry)
        rows = _moderator_rows(data)
        row = rows.pop(str(user_id), None)
        if row is None:
            return False, "Этот человек и так не модератор квестов."
        _save(entry, data)
    return True, f"{row.get('display_name') or user_id} больше не проверяет квесты."


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


def rewards_for_player(
    entry: str, user_id, difficulty: int, data: dict | None = None,
) -> dict:
    """Quest reward with its gold scaled to the player's current hero level."""
    reward = rewards_for(entry, difficulty, data)
    pet = pets.get_pet(entry, user_id)
    hero_level = max(1, int((pet or {}).get("level", 1) or 1))
    gold_base = max(0, int(reward.get("gold", 0) or 0))
    reward.update({
        "gold_base": gold_base,
        "gold_multiplier": C.hero_gold_multiplier(hero_level, "quest"),
        "hero_level": hero_level,
        "gold": C.gold_for_hero(gold_base, hero_level, "quest"),
    })
    return reward


def _reward_payload(
    entry: str, difficulty: int, data: dict | None = None, *, user_id=None,
) -> dict:
    """Player-facing quest rewards, including the fixed rare-scroll roll.

    Gold, XP, tickets and item chance remain moderator-editable. Scroll acquisition is
    deliberately a separate global balance table, so a quest moderator cannot
    accidentally turn a rare permanent ability into a guaranteed routine payout.
    """
    reward = (
        rewards_for_player(entry, user_id, difficulty, data)
        if user_id is not None else rewards_for(entry, difficulty, data)
    )
    scroll_chance = pets.HARD_QUEST_SCROLL_CHANCES.get(int(difficulty or 0))
    if scroll_chance is not None:
        reward.update({
            "scroll_chance": scroll_chance,
            "scroll_pity": pets.HARD_QUEST_SCROLL_PITY,
        })
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
        pool = catalog.PAINT_QUESTS if quest.kind == "paint" else catalog.REAL_QUESTS
        if all(item.code in disabled for item in pool):
            return False, "Нельзя выключить все квесты этого вида сразу."
        data["disabled"] = sorted(disabled)
        _save(entry, data)
    return True, ("Квест снова в ротации." if enabled else "Квест убран из ротации.")


QUEST_TEXT_FIELDS = ("title", "subject", "technique", "hint", "proof")
QUEST_TEXT_LIMITS = {
    "title": 160, "subject": 500, "technique": 1_000, "hint": 1_000, "proof": 500,
}


def _quest_text(quest, data: dict) -> dict:
    """The catalogue's text with this chat's moderator edits applied."""
    changed = data.get("overrides", {}).get(quest.code, {})
    return {
        field: str(changed.get(field) or getattr(quest, field))
        for field in QUEST_TEXT_FIELDS
    }


def set_quest_text(entry: str, code: str, text: dict) -> tuple[bool, str]:
    """Save a per-chat edit of a quest brief; the global catalogue stays untouched."""
    quest = catalog.find_quest(code)
    if quest is None:
        return False, "Такого квеста нет."
    if not isinstance(text, dict):
        return False, "Текст квеста передан неверно."
    updated = {}
    for field in QUEST_TEXT_FIELDS:
        if field not in text:
            continue
        value = str(text.get(field) or "").strip()
        if not value:
            return False, "Все поля квеста должны быть заполнены."
        if len(value) > QUEST_TEXT_LIMITS[field]:
            return False, f"Поле «{field}» слишком длинное."
        updated[field] = value
    if not updated:
        return False, "Нет изменений текста квеста."
    with _lock:
        data = _load(entry)
        row = data.setdefault("overrides", {}).setdefault(quest.code, {})
        row.update(updated)
        _save(entry, data)
    return True, "Текст квеста сохранён."


def catalog_entries(entry: str) -> list[dict]:
    """The complete editable catalogue for the moderation page."""
    data = _load(entry)
    disabled = set(data.get("disabled", []))
    rows = []
    for quest in catalog.QUESTS:
        rows.append({
            "code": quest.code, "difficulty": quest.difficulty, "tool": quest.tool,
            "kind": quest.kind, "hashtag": catalog.hashtag(quest.code),
            "enabled": quest.code not in disabled,
            **_quest_text(quest, data),
        })
    return rows


def available_quests(entry: str, data: dict | None = None, kind: str = "paint") -> tuple:
    """Quests of one kind still in a moderator's rotation."""
    data = data if data is not None else _load(entry)
    disabled = set(data.get("disabled", []))
    everything = (catalog.PAINT_QUESTS if kind == "paint" else catalog.REAL_QUESTS
                  if kind == "real" else catalog.GEAR_PAINT_QUESTS if kind == "gear"
                  else catalog.RUNE_QUESTS)
    pool = tuple(quest for quest in everything if quest.code not in disabled)
    return pool or everything


# --- assignment -----------------------------------------------------------------------


def _quest_payload(entry: str, quest, data: dict, *, user_id=None) -> dict:
    text = _quest_text(quest, data)
    reward = _reward_payload(entry, quest.difficulty, data, user_id=user_id)
    if quest.kind == "rune":
        reward["rubies"] = 2
        personal_target = pets.personal_paint_target_for_quest(quest.code)
        tool_masterwork = pets.RUNE_TOOL_MASTERWORKS.get(quest.code)
        if personal_target:
            reward.update({
                "personal_paint_target": personal_target,
                "personal_paint_multiplier": pets.PERSONAL_PAINT_STAT_MULTIPLIER,
            })
        elif tool_masterwork in pets.WORKPLACE_FIGURINES:
            # A figurine is not a tool: it multiplies nothing, it buys the right to leave
            # somebody at a station. Sending tool_efficiency here would promise +50%.
            reward.update({
                "workplace_figurine": tool_masterwork,
                "tool_masterwork": tool_masterwork,
                "figurine_xp_bonus": C.FIGURINE_XP_BONUS,
            })
        elif tool_masterwork:
            reward.update({"tool_masterwork": tool_masterwork, "tool_efficiency": 1.5})
        else:
            reward.update({"magic_guaranteed": True, "random_runes": 1})
    return {
        "code": quest.code,
        "hashtag": catalog.hashtag(quest.code),
        **text,
        "tool": quest.tool,
        "difficulty": quest.difficulty,
        "kind": quest.kind,
        # A painting challenge is always proved by a photo of the painted thing; a real
        # quest is proved by something else entirely, and the card has to say which.
        "proof": quest.proof,
        "badge": quest.badge,
        "cooldown_days": quest.cooldown_days,
        "reward": reward,
    }


def _pick(
    entry: str, user_id, data: dict, exclude: set[str], difficulty: int | None = None,
    kind: str = "paint", moment: datetime | None = None,
):
    """A random quest of one kind that this player can actually be given right now.

    Weighted by nothing: every quest in rotation is equally likely, and difficulty is
    what the reward scales on rather than what the odds do.

    `difficulty` pins the rung, which is what makes a reroll cost something (see reroll).
    If nothing is left at that level it widens upward rather than failing: a harder quest
    than asked for is still a quest, an exception is a broken button.

    Returns None only when the whole eligible pool is empty, which a painting challenge
    can never be but a real quest can -- every real quest carries a cooldown, and somebody
    who has cleared the board has to be told so rather than handed a repeat.
    """
    moment = moment or app_now()
    pool = [quest for quest in available_quests(entry, data, kind) if quest.code not in exclude]
    if kind == "real":
        # A cooldown is what keeps a real quest from being farmed. Applied at the DEAL
        # now rather than at a shelf, so the slot simply never offers one that is resting.
        pool = [quest for quest in pool if _is_offerable(quest, data, user_id, moment)]
    if difficulty is not None:
        at_level = [quest for quest in pool if quest.difficulty == difficulty]
        if at_level:
            return random.choice(at_level)
        harder = [quest for quest in pool if quest.difficulty >= difficulty]
        if harder:
            return random.choice(harder)
    if not pool:
        # Widening past the exclusion is safe for painting challenges (there are 60 and
        # none of them expire), but never past a cooldown -- see above.
        wider = [
            quest for quest in available_quests(entry, data, kind)
            if kind != "real" or _is_offerable(quest, data, user_id, moment)
        ]
        if not wider:
            return None
        pool = wider
    return random.choice(pool)


def _is_offerable(quest, data: dict, user_id, moment: datetime) -> bool:
    """Whether a real quest's cooldown has lapsed for this player."""
    until = _cooldown_until(quest, data, user_id, moment)
    if until is None:
        return True
    if until == "never":
        return False
    return moment >= until


def _cooldown_notice(quest, data: dict, user_id, moment: datetime) -> str:
    """Why a quest on cooldown was turned down, and when it comes back.

    A date, not a bare refusal: the quest IS the player's to do again eventually, and
    "не сейчас" without a when is the kind of answer that gets the tag posted five more
    times (see the #quest_zenithal report).
    """
    until = _cooldown_until(quest, data, user_id, moment)
    if until == "never":
        return f"«{quest.title}» проходят один раз — этот уже сделан."
    if isinstance(until, datetime):
        days = max(1, (until.date() - moment.date()).days)
        return (
            f"«{quest.title}» уже сдан. Его можно повторить "
            f"{until.strftime('%d.%m.%Y')} — через {_plural_days(days)}."
        )
    return f"«{quest.title}» пока недоступен."


def _plural_days(days: int) -> str:
    tail = days % 100
    if 11 <= tail <= 14:
        return f"{days} дней"
    return f"{days} " + {1: "день", 2: "дня", 3: "дня", 4: "дня"}.get(days % 10, "дней")


def _pending_submission(data: dict, user_id, code: str) -> dict | None:
    """This player's still-unreviewed submission for this quest, if any.

    Asked of the submission list rather than of the board, because a quest that was never
    dealt has no board row to carry a "review" flag -- and one painted model must still not
    become several rows in the moderator's queue.
    """
    for row in data.get("submissions", []):
        if (
            row.get("status") == "pending"
            and str(row.get("user_id")) == str(user_id)
            and catalog.normalise_code(row.get("code")) == catalog.normalise_code(code)
        ):
            return row
    return None


# Independent storage for the three-card painting board and one-card real-life board.
SLOTS = {"paint": "assignments", "real": "real_assignments", "rune": "rune_assignments",
         # Its own storage, so splitting the shelf never re-deals somebody's live rune
         # board or loses a submission already sitting on it.
         "gear": "gear_assignments"}
QUESTS_PER_BOARD = {"paint": 3, "real": 1, "rune": 5, "gear": 5}
BOARD_LIFETIME = timedelta(hours=24)
EMPTY_BOARD_REFRESH = timedelta(hours=8)
AUTO_REFRESH_KINDS = frozenset({"real"})
# Painting boards are kept until the player rerolls them, which is the point -- a card
# somebody is halfway through must not vanish overnight. The cost of that is that a board
# dealt before the catalogue changed keeps its old cards for ever, and there is no amount
# of waiting that fixes it: quests added, retired or moved to another kind simply never
# reach anybody already holding a board.
#
# Bumping this retires every stored board at once. The next time a player looks, they are
# dealt from the catalogue as it stands now -- and anything they have already sent to a
# moderator is carried across rather than thrown away with the rest.
BOARD_BUNDLE_VERSION = 3


def _assignment_row(quest, moment: datetime) -> dict:
    return {
        "code": quest.code, "day": moment.date().isoformat(),
        "assigned_at": moment.isoformat(), "rerolls_used": 0,
        "status": "open", "submission_id": None,
    }


def _board_rows(board: dict) -> list[dict]:
    rows = board.get("quests") if isinstance(board, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _board_refresh_at(board: dict, moment: datetime, kind: str) -> datetime | None:
    if kind not in AUTO_REFRESH_KINDS:
        return None
    issued = _moment_like(board.get("issued_at"), moment) or moment
    expires = _moment_like(board.get("expires_at"), moment) or (issued + BOARD_LIFETIME)
    rows = _board_rows(board)
    if not rows or all(row.get("status") == "done" for row in rows):
        empty = _moment_like(board.get("empty_since"), moment) or moment
        return min(expires, empty + EMPTY_BOARD_REFRESH)
    return expires


def _kind_pool(kind: str) -> tuple:
    """Every quest of one kind in the catalogue, disabled ones included."""
    return (catalog.PAINT_QUESTS if kind == "paint" else catalog.REAL_QUESTS
            if kind == "real" else catalog.GEAR_PAINT_QUESTS if kind == "gear"
            else catalog.RUNE_QUESTS)


def _board_pool_codes(kind: str) -> set[str]:
    """Codes this board may hold: a quest that was retired or moved kind is not one.

    Read off the catalogue rather than the moderator's rotation on purpose. Switching a
    quest off stops it being DEALT; it does not reach into the board of somebody already
    working on it and take the card away.
    """
    return {quest.code for quest in _kind_pool(kind)}


def _make_board(
    entry: str, user_id, data: dict, kind: str, moment: datetime,
    avoid: set[str] | None = None, keep: list[dict] | None = None,
) -> dict:
    # Anything already with a moderator is carried across. Replacing a card somebody is
    # waiting on an answer for would strand the submission and lose them the reward.
    rows = [dict(row) for row in (keep or ())]
    excluded = set(avoid or ()) | {str(row.get("code") or "") for row in rows}
    while len(rows) < QUESTS_PER_BOARD[kind]:
        quest = _pick(entry, user_id, data, excluded, kind=kind, moment=moment)
        if quest is None or quest.code in excluded:
            break
        rows.append(_assignment_row(quest, moment))
        excluded.add(quest.code)
    board = {
        "bundle_version": BOARD_BUNDLE_VERSION,
        "kind": kind, "issued_at": moment.isoformat(),
        "expires_at": (moment + BOARD_LIFETIME).isoformat(), "quests": rows,
        "empty_since": moment.isoformat() if not rows else None,
    }
    data.setdefault(SLOTS[kind], {})[str(user_id)] = board
    return board


def _ensure_board(entry: str, user_id, data: dict, kind: str, moment: datetime) -> tuple[dict, bool]:
    stored = data.setdefault(SLOTS[kind], {}).get(str(user_id))
    changed = False
    if isinstance(stored, dict) and stored.get("code"):
        # Preserve the old live quest as card one, then deal the missing cards.
        issued = _moment_like(stored.get("assigned_at"), moment) or moment
        stored = {
            "bundle_version": BOARD_BUNDLE_VERSION,
            "kind": kind, "issued_at": issued.isoformat(),
            "expires_at": (issued + BOARD_LIFETIME).isoformat(),
            "quests": [stored], "empty_since": None,
        }
        data[SLOTS[kind]][str(user_id)] = stored
        excluded = {stored["quests"][0].get("code")}
        while len(stored["quests"]) < QUESTS_PER_BOARD[kind]:
            quest = _pick(entry, user_id, data, excluded, kind=kind, moment=moment)
            if quest is None or quest.code in excluded:
                break
            stored["quests"].append(_assignment_row(quest, moment))
            excluded.add(quest.code)
        changed = True
    if not isinstance(stored, dict) or "quests" not in stored:
        return _make_board(entry, user_id, data, kind, moment), True
    # A board dealt before the catalogue moved is retired here rather than waiting for a
    # reroll nobody knows they need. What is under review survives the swap.
    if int(stored.get("bundle_version", 0) or 0) < BOARD_BUNDLE_VERSION:
        return _make_board(
            entry, user_id, data, kind, moment,
            keep=[row for row in _board_rows(stored) if row.get("status") == "review"],
        ), True
    # A quest that has been retired, or moved to another kind, is no longer this board's
    # to offer -- matching on "does the code exist anywhere" left cards sitting on the
    # wrong board after the rune and gear quests were split out of painting.
    pool = _board_pool_codes(kind)
    cleaned = [
        row for row in _board_rows(stored)
        if str(row.get("code") or "") in pool or row.get("status") == "review"
    ]
    if len(cleaned) != len(_board_rows(stored)):
        stored["quests"] = cleaned
        changed = True
    refresh_at = _board_refresh_at(stored, moment, kind)
    if refresh_at is not None and moment >= refresh_at:
        return _make_board(
            entry, user_id, data, kind, moment,
            avoid={row.get("code") for row in _board_rows(stored)},
        ), True
    return stored, changed


def _live_assignment(
    data: dict, user_id, kind: str = "paint", *, code: str | None = None,
    submission_id: str | None = None,
) -> dict | None:
    stored = data.get(SLOTS[kind], {}).get(str(user_id))
    rows = _board_rows(stored)
    if not rows and isinstance(stored, dict) and stored.get("code"):
        rows = [stored]
    wanted = catalog.normalise_code(code) if code is not None else None
    for row in rows:
        if wanted is not None and row.get("code") != wanted:
            continue
        if submission_id is not None and str(row.get("submission_id")) != str(submission_id):
            continue
        if row.get("status") != "done" and catalog.find_quest(row.get("code")):
            return row
    return None


def quest_board(entry: str, user_id, kind: str = "paint", now: datetime | None = None) -> dict:
    """A persistent paint selection or the 24-hour real-life selection."""
    moment = now or app_now()
    with _lock:
        data = _load(entry)
        board, changed = _ensure_board(entry, user_id, data, kind, moment)
        if changed:
            _save(entry, data)
        cards = []
        for live in _board_rows(board):
            quest = catalog.find_quest(live.get("code"))
            if quest is None:
                continue
            submission = _find_submission(data, live.get("submission_id"))
            cards.append({
                **_quest_payload(entry, quest, data, user_id=user_id),
                "status": live.get("status", "open"),
                "rerolls_left": max(
                    0, REROLLS_PER_QUEST - int(live.get("rerolls_used", 0) or 0)
                ),
                "submission": _public_submission(submission) if submission else None,
            })
        open_cards = [card for card in cards if card["status"] == "open"]
        reviewing = [card for card in cards if card["status"] == "review"]
        refresh_at = _board_refresh_at(board, moment, kind)
        reroll_at = _group_reroll_at(data, user_id, kind, moment)
        cooldown_ready = reroll_at is None or moment >= reroll_at
        reroll_available = cooldown_ready and any(
            live.get("status") != "review" for live in _board_rows(board)
        )
        for card in cards:
            card["rerolls_left"] = 1 if reroll_available else 0
        status = "open" if open_cards else (
            "review" if reviewing else ("resting" if cards else "exhausted")
        )
        # A just-submitted card remains the compatibility headline while it is under
        # review; the new UIs use the full `quests` list and are not constrained by it.
        primary = (reviewing or open_cards or cards or [None])[0]
        return {
            "quest": primary,  # compatibility for integrations that show one headline
            "quests": cards, "kind": kind,
            "has_pet": pets.get_pet(entry, user_id) is not None,
            "status": status,
            "rerolls_left": 1 if reroll_available else 0,
            "submission": primary.get("submission") if primary else None,
            "available_count": len(open_cards), "attention": bool(open_cards),
            "auto_refresh": refresh_at is not None,
            "refresh_at": refresh_at.isoformat() if refresh_at is not None else None,
            "seconds_until_refresh": (
                max(0, int((refresh_at - moment).total_seconds()))
                if refresh_at is not None else None
            ),
            "reroll_available": reroll_available,
            "reroll_at": reroll_at.isoformat() if reroll_at is not None else None,
            "reroll_at_label": reroll_at.strftime("%H:%M") if reroll_at is not None else "",
            "seconds_until_reroll": (
                max(0, int((reroll_at - moment).total_seconds()))
                if reroll_at is not None else 0
            ),
        }


def daily_quest(entry: str, user_id, now: datetime | None = None) -> dict:
    """The three-card painting board."""
    return quest_board(entry, user_id, "paint", now)


def real_quest(entry: str, user_id, now: datetime | None = None) -> dict:
    """The one-card real-life board."""
    return quest_board(entry, user_id, "real", now)


def rune_quest(entry: str, user_id, now: datetime | None = None) -> dict:
    """The five-card elemental and tool rune board."""
    return quest_board(entry, user_id, "rune", now)


def gear_quest(entry: str, user_id, now: datetime | None = None) -> dict:
    """The five-card board of paint quests that upgrade arena gear."""
    return quest_board(entry, user_id, "gear", now)


# Public compatibility name retained for callers that used the old one-slot API.
quest_slot = quest_board


def has_available_quests(entry: str, user_id, now: datetime | None = None) -> bool:
    """Whether either board currently has a quest the player can act on."""
    moment = now or app_now()
    return bool(
        daily_quest(entry, user_id, moment).get("attention")
        or real_quest(entry, user_id, moment).get("attention")
        or rune_quest(entry, user_id, moment).get("attention")
        or gear_quest(entry, user_id, moment).get("attention")
    )


def _group_reroll_at(data: dict, user_id, kind: str, reference: datetime) -> datetime | None:
    rows = data.setdefault("reroll_cooldowns", {}).get(str(user_id), {})
    last = _moment_like(rows.get(kind), reference) if isinstance(rows, dict) else None
    return last + REROLL_COOLDOWN if last is not None else None


def reroll(
    entry: str, user_id, now: datetime | None = None, kind: str = "paint",
    code: str | None = None,
) -> tuple[bool, str]:
    """Replace one quest group, at most once per twelve hours.

    ``code`` is accepted only so callbacks sent before the group-reroll release remain
    safe; it no longer narrows the operation to one card. A submitted card is never dealt
    away while moderators are reviewing it.
    """
    moment = now or app_now()
    with _lock:
        data = _load(entry)
        board, _changed = _ensure_board(entry, user_id, data, kind, moment)
        next_at = _group_reroll_at(data, user_id, kind, moment)
        if next_at is not None and moment < next_at:
            return False, f"Следующий реролл в {next_at.strftime('%H:%M')} по Москве."

        old_rows = _board_rows(board)
        protected = [row for row in old_rows if row.get("status") == "review"]
        if len(protected) >= QUESTS_PER_BOARD[kind]:
            return False, "Вся группа уже на проверке — дождись ответа модераторов."
        old_codes = {str(row.get("code") or "") for row in old_rows}
        fresh = list(protected)
        chosen = {str(row.get("code") or "") for row in protected}
        while len(fresh) < QUESTS_PER_BOARD[kind]:
            candidates = [
                quest for quest in available_quests(entry, data, kind)
                if quest.code not in chosen
                and (kind != "real" or _is_offerable(quest, data, user_id, moment))
            ]
            if not candidates:
                break
            preferred = [quest for quest in candidates if quest.code not in old_codes]
            quest = random.choice(preferred or candidates)
            fresh.append(_assignment_row(quest, moment))
            chosen.add(quest.code)
        if len(fresh) == len(protected):
            return False, "Больше нечего предложить — все квесты этого вида на отдыхе."
        board.update({
            "issued_at": moment.isoformat(),
            "expires_at": (moment + BOARD_LIFETIME).isoformat(),
            "quests": fresh,
            "empty_since": None,
        })
        cooldowns = data.setdefault("reroll_cooldowns", {}).setdefault(str(user_id), {})
        cooldowns[kind] = moment.isoformat()
        _save(entry, data)
    return True, (
        f"Группа квестов обновлена. Следующий реролл в "
        f"{(moment + REROLL_COOLDOWN).strftime('%H:%M')} по Москве."
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


def _done_map(data: dict, user_id) -> dict:
    """code -> when this player last finished it.

    A compact per-user map rather than a scan of `history`: history is capped chat-wide
    (HISTORY_LIMIT), and a once-ever quest whose completion had scrolled off the end of
    it would quietly become available again. At most one row per quest per player, so it
    stays small no matter how long somebody plays.
    """
    done = data.setdefault("done", {})
    if not isinstance(done, dict):
        done = data["done"] = {}
    mine = done.setdefault(str(user_id), {})
    if not isinstance(mine, dict):
        mine = done[str(user_id)] = {}
    return mine


def _cooldown_until(quest, data: dict, user_id, reference: datetime) -> datetime | str | None:
    """When this player may be dealt `quest` again.

    None means now, "never" means it is a once-ever quest already done, and a datetime is
    the moment the cooldown lifts. A cooldown of 0 IS once-ever: buying a loupe cannot be
    repeated, while tidying the bench is worth doing again in a fortnight.

    Counted from THIS PLAYER'S own completion, which is also what spreads a fortnightly
    quest across the chat instead of handing it to everybody on the 1st and the 15th:
    finish on the 3rd and it comes back on the 17th, finish on the 9th and it comes back
    on the 23rd. No separate staggering machinery, and only one source of truth, so the
    deal and the reroll can never disagree about what is offerable.
    """
    finished = _moment_like(_done_map(data, user_id).get(quest.code), reference)
    if finished is None:
        return None
    if quest.cooldown_days <= 0:
        return "never"
    return finished + timedelta(days=quest.cooldown_days)


def _find_submission(data: dict, submission_id) -> dict | None:
    if not submission_id:
        return None
    for row in data.get("submissions", []):
        if str(row.get("id")) == str(submission_id):
            return row
    return None


def record_notifications(entry: str, submission_id, sent: list[tuple]) -> None:
    """Remember which moderator DMs announced this submission, as (chat_id, message_id).

    Kept so a verdict can go back and mark those messages decided. Without it a moderator
    who reviewed in the Mini App would leave every OTHER moderator looking at a live
    "Принять/Отклонить" card for work that was settled ten minutes ago -- and tapping it
    is how two people end up reviewing the same photo.
    """
    rows = [
        {"chat_id": chat_id, "message_id": int(message_id)}
        for chat_id, message_id in sent
        if message_id is not None
    ]
    if not rows:
        return
    with _lock:
        data = _load(entry)
        row = _find_submission(data, submission_id)
        if row is None:
            return
        row["notifications"] = rows
        _save(entry, data)


def notifications_for(entry: str, submission_id) -> list[dict]:
    data = _load(entry)
    row = _find_submission(data, submission_id)
    stored = (row or {}).get("notifications")
    return [dict(item) for item in stored] if isinstance(stored, list) else []


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

    ANY quest may be submitted, whether or not it was ever dealt to this player. The board
    suggests; it does not restrict. People read each other's cards and remember ones they
    liked from weeks ago, and painting a technique because you saw somebody else do it is
    the behaviour this game is for -- refusing that taught nobody anything and mostly
    produced silent confusion (see the #quest_zenithal report).

    Two things still hold, and neither is about the board:

      A quest already waiting on a moderator cannot be submitted again, or one painted
      model becomes several entries in the review queue.

      A COOLDOWN is still enforced, and is now enforced here rather than only at the deal.
      It is the whole of what stops a repeatable quest being farmed, and the deal used to
      be the only way in. Every real-life quest carries one (14 or 30 days); no painting
      challenge does, which is exactly right -- repeating a technique is the point of them.
    """
    moment = now or app_now()
    quest = catalog.find_quest(code)
    if quest is None:
        return False, "Такого квеста нет."
    with _lock:
        data = _load(entry)
        # Still dealt, so the board stays live and a submitted card can be marked done --
        # it is just no longer a gate on what may be handed in.
        _board, _changed = _ensure_board(entry, user_id, data, quest.kind, moment)
        live = _live_assignment(data, user_id, quest.kind, code=quest.code)
        if _pending_submission(data, user_id, quest.code) is not None:
            return False, "Работа по этому квесту уже на проверке."
        if not _is_offerable(quest, data, user_id, moment):
            return False, _cooldown_notice(quest, data, user_id, moment)
        photo_key = _submission_photo_key(chat_id, message_id)
        cached_photo = data.setdefault("submission_photos", {}).pop(photo_key, None)
        resolved_photo_file_id = str(
            photo_file_id or ((cached_photo or {}).get("file_id") if isinstance(cached_photo, dict) else "")
            or ""
        ).strip() or None
        promised_reward = rewards_for_player(entry, user_id, quest.difficulty, data)
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
            "photo_file_id": resolved_photo_file_id,
            "ts": moment.isoformat(),
            "status": "pending",
            "reviewed_by": None,
            "reviewed_by_name": "",
            "reviewed_at": None,
            "note": "",
            "paid": None,
            # Freeze the displayed level-scaled gold when the work is submitted. A slow
            # moderator review must not silently change what the accepted card promised.
            "gold_base": promised_reward["gold_base"],
            "gold_promised": promised_reward["gold"],
            "gold_multiplier": promised_reward["gold_multiplier"],
            "hero_level": promised_reward["hero_level"],
        }
        data.setdefault("submissions", []).append(row)
        # Only when this quest happens to be on the player's board. Submitting one they
        # were never dealt leaves the board untouched -- review() already handles a
        # submission with no assignment behind it, and pays it just the same.
        if isinstance(live, dict):
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
        text = _quest_text(quest, data) if quest else {}
        row["title"] = text.get("title", row.get("code"))
        row["subject"] = text.get("subject", "")
        row["technique"] = text.get("technique", "")
        row["hint"] = text.get("hint", "")
        row["proof"] = text.get("proof", "")
        row["hashtag"] = catalog.hashtag(row.get("code"))
        reward = _reward_payload(
            entry, row.get("difficulty", 1), data, user_id=row.get("user_id"),
        )
        if row.get("gold_promised") is not None:
            reward.update({
                "gold": int(row.get("gold_promised", 0) or 0),
                "gold_base": int(row.get("gold_base", reward.get("gold_base", 0)) or 0),
                "gold_multiplier": float(row.get("gold_multiplier", 1.0) or 1.0),
                "hero_level": int(row.get("hero_level", 1) or 1),
            })
        row["reward"] = reward
    return rows


def pending_count(entry: str) -> int:
    """How many works still need a moderator's verdict."""
    return sum(1 for row in _load(entry).get("submissions", [])
               if row.get("status") == "pending")


def suggest_idea(
    entry: str, user_id, text: str, *, author_name: str = "", author_username: str = "",
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Keep a player's quest idea for moderators to read later.

    Ideas deliberately have no expiry and no public feed: they are an inbox for people
    who curate the catalogue, not another chat the whole group has to keep up with.
    """
    idea = str(text or "").strip()
    if not idea:
        return False, "Напиши текст идеи."
    if len(idea) > 1_000:
        return False, "Идея слишком длинная — до 1000 символов."
    moment = now or app_now()
    with _lock:
        data = _load(entry)
        data.setdefault("ideas", []).append({
            "id": f"{moment.strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}",
            "user_id": str(user_id),
            "author_name": str(author_name or ""),
            "author_username": str(author_username or "").lstrip("@"),
            "text": idea,
            "ts": moment.isoformat(),
        })
        _save(entry, data)
    return True, "Идея сохранена. Спасибо!"


def ideas(entry: str, limit: int = 500) -> list[dict]:
    """All suggested ideas, newest first, for the quest moderation screen."""
    rows = [dict(row) for row in reversed(_load(entry).get("ideas", []))]
    return rows[:max(0, int(limit))]


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
    note = str(note or "").strip()
    if not accept and not note:
        return False, "Укажи причину отклонения.", receipt
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
        if accept and pets.personal_paint_target_for_quest(quest.code) \
                and not str(row.get("photo_file_id") or "").strip():
            return False, (
                "У заявки нет доступной фотографии для аватарки руны. "
                "Попроси игрока пересдать квест именно фотографией."
            ), receipt

        row["status"] = "accepted" if accept else "rejected"
        row["reviewed_by"] = str(reviewer_id)
        row["reviewed_by_name"] = str(reviewer_name or "")
        row["reviewed_at"] = moment.isoformat()
        row["note"] = note[:300]

        live = _live_assignment(
            data, row["user_id"], quest.kind, submission_id=str(row["id"]),
        )
        if accept:
            reward = rewards_for_player(entry, row["user_id"], quest.difficulty, data)
            if row.get("gold_promised") is not None:
                reward.update({
                    "gold": int(row.get("gold_promised", 0) or 0),
                    "gold_base": int(row.get("gold_base", reward.get("gold_base", 0)) or 0),
                    "gold_multiplier": float(row.get("gold_multiplier", 1.0) or 1.0),
                    "hero_level": int(row.get("hero_level", 1) or 1),
                })
            receipt = {
                "user_id": row["user_id"], "code": quest.code, "title": quest.title,
                "difficulty": quest.difficulty, "kind": quest.kind,
                "badge": quest.badge, "author_name": row.get("author_name", ""),
                # The personal-paint rune keeps this Telegram file id as its artwork.
                # It is copied from the immutable submitted proof, never from a client
                # review request.
                "photo_file_id": row.get("photo_file_id"),
                **reward,
            }
            row["paid"] = dict(reward)
            if isinstance(live, dict) and live.get("submission_id") == row["id"]:
                live["status"] = "done"
                live["finished_day"] = moment.date().isoformat()
                live["finished_at"] = moment.isoformat()
                board = data.get(SLOTS[quest.kind], {}).get(str(row["user_id"]))
                if _board_rows(board) and all(
                    item.get("status") == "done" for item in _board_rows(board)
                ):
                    board["empty_since"] = moment.isoformat()
            # The durable completion stamp a cooldown is measured from. Written for BOTH
            # kinds -- only real quests have cooldowns today, but the slot above is
            # overwritten by the next deal, so this is the only lasting record of it.
            _done_map(data, row["user_id"])[quest.code] = moment.isoformat()
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
    # The review row is the durable source for mailbox events.  Payment deliberately
    # happens outside the quest lock, so record the realised drop/scroll receipt in a
    # short second transaction rather than claiming a random reward that never landed.
    with _lock:
        data = _load(entry)
        row = _find_submission(data, submission_id)
        if row is not None and row.get("status") == "accepted":
            row["paid"] = {**dict(row.get("paid") or {}), **paid}
            _save(entry, data)
    if receipt.get("badge"):
        receipt["badge_given"] = _award_badge(
            entry, receipt["user_id"], receipt["badge"], receipt.get("author_name", ""),
        )
    message = f"Принято. Начислено: {paid['gold']} монет"
    if paid["xp"]:
        message += f", {paid['xp']} опыта"
    if not paid["has_pet"]:
        message += " (опыт и предмет снаряжения не начислены — у игрока нет существа)"
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
        # No stacking: a quest badge marks that the quest was done, and a repeatable
        # quest would otherwise inflate it every single completion.
        _badge, newly, _count = stats.give_custom_badge(
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
    kind = receipt.get("kind")
    rubies = 10 if kind == "real" else (2 if kind == "rune" else 5)
    pets.grant_rubies_once(entry, user_id, rubies, f"quest:{submission_id}")
    # Pickaxe and shovel rune-paint quests are direct permanent tool upgrades; every
    # other specialist paint quest may mint exactly one owner-bound artwork rune.
    tool_masterwork = pets.unlock_tool_for_rune_quest(entry, user_id, receipt.get("code"))
    pickaxe_unlocked = tool_masterwork == "pickaxe"
    personal_paint_rune = pets.grant_personal_paint_rune(
        entry, user_id, receipt.get("code"), submission_id, receipt.get("photo_file_id"),
    )
    specialist_paint = bool(
        pets.personal_paint_target_for_quest(receipt.get("code"))
        or pets.RUNE_TOOL_MASTERWORKS.get(str(receipt.get("code") or ""))
    )
    rune = {"granted": 0}
    if kind == "rune" and not specialist_paint:
        element = random.Random(f"dungeon-quest-rune:{submission_id}").choice(pets.RUNE_ELEMENTS)
        rune = pets.grant_runes(entry, user_id, element, 1, f"dungeon-quest:{submission_id}")
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
    # Scrolls are their own rare, permanent reward stream.  Only accepted difficulty-4
    # and difficulty-5 quests enter it; the submission id makes the roll deterministic
    # and idempotent even if a worker ever retries this reward receipt.
    scroll = (
        {"granted": False, "reason": "specialist_paint_reward"}
        if kind == "rune" and specialist_paint else
        pets.grant_scroll_reward(
            entry, user_id, source=f"dungeon-quest:{submission_id}", kind="dungeon",
            chance=1.0, pity_after=1, seed=f"dungeon-quest:{submission_id}",
        ) if kind == "rune" else pets.grant_scroll_for_hard_quest(
            entry, user_id, submission_id, receipt.get("difficulty", 0),
        )
    )
    return {
        "gold": gold, "xp": xp, "tickets": tickets, "rubies": rubies, "rune": rune,
        "pickaxe_unlocked": pickaxe_unlocked,
        "tool_masterwork": tool_masterwork,
        "personal_paint_rune": personal_paint_rune,
        "has_pet": has_pet,
        "item": dropped.get("code") if dropped else None,
        "item_name": dropped.get("name") if dropped else None,
        "item_rarity": dropped.get("rarity") if dropped else None,
        "auto_equipped": bool(dropped.get("auto_equipped")) if dropped else False,
        "scroll": scroll.get("code") if scroll and scroll.get("granted") else None,
        "scroll_name": scroll.get("name") if scroll and scroll.get("granted") else None,
        "scroll_icon": scroll.get("icon") if scroll and scroll.get("granted") else None,
        "scroll_ultimate": bool(scroll.get("ultimate")) if scroll and scroll.get("granted") else False,
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
            "item": paid.get("item"), "item_name": paid.get("item_name"),
            "item_rarity": paid.get("item_rarity"), "auto_equipped": bool(paid.get("auto_equipped")),
            "scroll": paid.get("scroll"), "scroll_name": paid.get("scroll_name"),
            "scroll_icon": paid.get("scroll_icon"), "scroll_ultimate": bool(paid.get("scroll_ultimate")),
            "scroll_source": f"quest:{row.get('id')}" if paid.get("scroll") else None,
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
