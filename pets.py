"""State, storage and economy plumbing for the pet arena (see PETS_CONTRACT.md).

One JSON file per chat, next to economy's and stats' own per-chat files, in the same
`stats._stats_dir()` / `stats._cache_key()` namespace so a chat's whole cache lives under
one hash. The wallet is deliberately NOT reimplemented here -- gold is the same coin
ledger /stat and /shop already show (economy.py), so taming a creature and losing an
arena fight both show up as one number to the player instead of two. Every call that
touches money threads the caller's live chat `xp` through to economy.balance/spend,
exactly the way economy.py's own docstring describes.

A single "pets" dict, keyed by user id, holds BOTH the cage and the creature that lives
in it, rather than two separate records. That is not an accident: a cage can exist
without a tamed creature (bought, not yet named), so the record is allocated at
`buy_cage` with `name=None` and only becomes a real pet once `tame` fills the name in.
Every function that needs an actual creature checks for a truthy `name` rather than mere
presence in the dict -- see `_tamed_record`. This means "have a cage but no pet yet" is a
first-class, storable state instead of something inferred from field absence.

Pet XP/levels are a SEPARATE ladder from the chat's XP (pets_config.PET_XP_BASE etc, fed
by WIN_XP/LOSS_XP) -- they only share a name with the chat-activity `xp` argument by
coincidence of vocabulary. Mixing them would let grinding the chat level up a creature's
combat stats for free, which is not what "у существ отдельный свой опыт" asked for.

`record_fight` reports its outcome from the ATTACKER's point of view (gold/xp/level-ups/
drop actually credited to whoever pressed the fight button), because the attacker is the
only side that "acted" this call -- the defender is a passive opponent who may not even
be online, and still shows up correctly in their own /history because the fight is
appended once and `history()` filters by either role.
"""

import json
import random
from datetime import date, datetime, timedelta

import economy
import pets_config as C
import stats
from app_time import now as app_now

PETS_STORE_VERSION = 1
# Rolling fight log, capped independently of C.HISTORY_LIMIT (that constant bounds what
# ONE player is shown per /history call, not how many chat-wide entries are kept on disk)
# -- mirrors economy.py's LOG_LIMIT convention for the same reason: trimming this can
# never change anybody's stats, wins or gold, all of which live on the per-pet record.
FIGHT_LOG_LIMIT = 2_000

_NAME_MAX_LEN = 24


# --- storage -----------------------------------------------------------------------


def _pets_path(entry: str):
    return stats._stats_dir() / f"{stats._cache_key(entry)}_pets.json"


def _empty() -> dict:
    return {"version": PETS_STORE_VERSION, "pets": {}, "fights": [], "duels": {}}


def _load(entry: str) -> dict:
    path = _pets_path(entry)
    if not path.exists():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    data.setdefault("pets", {})
    data.setdefault("fights", [])
    data.setdefault("duels", {})
    return data


def _save(entry: str, data: dict) -> None:
    data["version"] = PETS_STORE_VERSION
    if len(data.get("fights", [])) > FIGHT_LOG_LIMIT:
        data["fights"] = data["fights"][-FIGHT_LOG_LIMIT:]
    stats._write_json_atomic(_pets_path(entry), data)


def _new_record() -> dict:
    """A cage that has just been bought: real, chargeable, but no creature in it yet."""
    return {
        "name": None,
        "photo_file_id": None,
        "owner_name": None,
        "cage_level": 1,
        "stats": {key: C.STAT_MIN_LEVEL for key in C.STAT_KEYS},
        "equipped": {slot: None for slot in C.SLOT_KEYS},
        "inventory": [],
        "level": 1,
        "xp": 0,
        "fights": 0,
        "wins": 0,
        "created_at": app_now().isoformat(),
        "fights_today": 0,
        "fights_day": app_now().date().isoformat(),
    }


def _tamed_record(data: dict, user_id) -> dict | None:
    record = data["pets"].get(str(user_id))
    return record if record and record.get("name") else None


def _name_taken(data: dict, name: str, exclude_uid: str | None = None) -> bool:
    needle = name.strip().lower()
    for uid, record in data["pets"].items():
        if uid == exclude_uid:
            continue
        existing = record.get("name")
        if existing and existing.strip().lower() == needle:
            return True
    return False


def _reset_if_new_day(record: dict, today: date) -> None:
    if record.get("fights_day") != today.isoformat():
        record["fights_day"] = today.isoformat()
        record["fights_today"] = 0


def _apply_xp(record: dict, amount: int) -> tuple[int, int]:
    """Feed `amount` pet-xp into `record` in place. Returns (new_level, levels_gained)."""
    old_level = record.get("level", 1)
    record["xp"] = record.get("xp", 0) + amount
    level = old_level
    while level < C.PET_MAX_LEVEL:
        needed = C.pet_xp_for_next_level(level)
        if needed <= 0 or record["xp"] < needed:
            break
        record["xp"] -= needed
        level += 1
    record["level"] = level
    return level, level - old_level


# --- cage & taming -------------------------------------------------------------------


def today() -> date:
    """The one clock the whole game reads (`fights_left`, `record_fight`, ...), so the UI
    and this module can never disagree about what day it is."""
    return app_now().date()


def get_pet(entry, user_id) -> dict | None:
    return _tamed_record(_load(entry), user_id)


def has_cage(entry, user_id) -> bool:
    return str(user_id) in _load(entry)["pets"]


def cage_level(entry, user_id) -> int:
    """0 when there is no cage at all, else the real 1..C.CAGE_MAX_LEVEL -- distinct from
    has_cage() so the UI can render a level number without a second bool check."""
    record = _load(entry)["pets"].get(str(user_id))
    return record.get("cage_level", 0) if record else 0


def balance_for(entry, user_id, xp) -> int:
    """Thin pass-through so pets_ui never has to import economy directly."""
    return economy.balance(entry, user_id, xp)


def buy_cage(entry, user_id, xp) -> tuple[bool, str]:
    data = _load(entry)
    uid = str(user_id)
    if uid in data["pets"]:
        return False, "У тебя уже есть клетка."
    ok, balance = economy.spend(entry, user_id, xp, C.CAGE_PRICE, "buy:pet_cage")
    if not ok:
        return False, f"Нужно {C.CAGE_PRICE} монет на клетку, у тебя {balance}."
    data["pets"][uid] = _new_record()
    _save(entry, data)
    return True, f"Клетка куплена за {C.CAGE_PRICE} монет. Теперь найди, кого туда поселить."


def upgrade_cage(entry, user_id, xp) -> tuple[bool, str]:
    data = _load(entry)
    record = data["pets"].get(str(user_id))
    if not record:
        return False, "Сначала купи клетку."
    level = record.get("cage_level", 1)
    if level >= C.CAGE_MAX_LEVEL:
        return False, f"Клетка уже максимального уровня ({C.CAGE_MAX_LEVEL})."
    cost = C.CAGE_UPGRADE_COSTS[level]
    ok, balance = economy.spend(entry, user_id, xp, cost, "buy:pet_cage_upgrade")
    if not ok:
        return False, f"Нужно {cost} монет на апгрейд клетки, у тебя {balance}."
    record["cage_level"] = level + 1
    _save(entry, data)
    return True, f"Клетка прокачана до {level + 1} уровня за {cost} монет."


def tame(entry, user_id, xp, name, photo_file_id, owner_name) -> tuple[bool, str]:
    data = _load(entry)
    uid = str(user_id)
    record = data["pets"].get(uid)
    if record is None:
        return False, "Сначала купи клетку."
    if record.get("name"):
        return False, "У тебя уже есть существо."
    try:
        clean_name = validate_name(name)
    except ValueError as error:
        return False, str(error)
    if _name_taken(data, clean_name, exclude_uid=uid):
        return False, f"Имя «{clean_name}» уже занято в этом чате -- выбери другое."
    ok, balance = economy.spend(entry, user_id, xp, C.TAME_PRICE, "buy:pet_tame")
    if not ok:
        return False, f"Нужно {C.TAME_PRICE} монет, у тебя {balance}."
    record["name"] = clean_name
    record["photo_file_id"] = photo_file_id
    record["owner_name"] = owner_name
    record["level"] = 1
    record["xp"] = 0
    record["created_at"] = app_now().isoformat()
    _save(entry, data)
    return True, f"Готово! «{clean_name}» теперь твоё существо."


def rename(entry, user_id, name) -> tuple[bool, str]:
    data = _load(entry)
    uid = str(user_id)
    record = _tamed_record(data, uid)
    if record is None:
        return False, "Сначала приручи существо."
    try:
        clean_name = validate_name(name)
    except ValueError as error:
        return False, str(error)
    if _name_taken(data, clean_name, exclude_uid=uid):
        return False, f"Имя «{clean_name}» уже занято в этом чате -- выбери другое."
    record["name"] = clean_name
    _save(entry, data)
    return True, f"Теперь существо зовут «{clean_name}»."


def set_photo(entry, user_id, photo_file_id) -> tuple[bool, str]:
    data = _load(entry)
    record = _tamed_record(data, user_id)
    if record is None:
        return False, "Сначала приручи существо."
    record["photo_file_id"] = photo_file_id
    _save(entry, data)
    return True, "Фото обновлено."


def validate_name(name) -> str:
    """1-24 chars after collapsing whitespace (which also swallows newlines), no HTML
    angle brackets since the bot renders names into HTML messages. Raises ValueError with
    an already-Russian, already-printable reason -- callers just forward str(error)."""
    collapsed = " ".join((name or "").split())
    if not collapsed:
        raise ValueError("Имя не может быть пустым.")
    if len(collapsed) > _NAME_MAX_LEN:
        raise ValueError(f"Имя слишком длинное (максимум {_NAME_MAX_LEN} символов).")
    if "<" in collapsed or ">" in collapsed:
        raise ValueError("Имя не может содержать символы < или >.")
    return collapsed


# --- stats -----------------------------------------------------------------------------


def stat_level(entry, user_id, stat) -> int:
    record = _tamed_record(_load(entry), user_id)
    if record is None:
        return C.STAT_MIN_LEVEL
    return record.get("stats", {}).get(stat, C.STAT_MIN_LEVEL)


def upgrade_stat(entry, user_id, xp, stat, times=1) -> tuple[bool, str, int]:
    if stat not in C.STAT_KEYS:
        return False, "Неизвестная характеристика.", 0
    if times < 1:
        return False, "Количество уровней должно быть положительным.", 0

    data = _load(entry)
    record = _tamed_record(data, user_id)
    if record is None:
        return False, "Сначала приручи существо.", 0

    name = C.STAT_NAMES[stat]
    level = record["stats"].get(stat, C.STAT_MIN_LEVEL)
    if level >= C.STAT_MAX_LEVEL:
        return False, f"{name} уже прокачан(а) до максимума ({C.STAT_MAX_LEVEL}).", 0

    # Costs climb with level (see pets_config.stat_upgrade_cost), so N levels is NOT
    # N * cost -- walk the individual steps and stop the moment the running total would
    # exceed what is actually in the wallet, rather than refusing the whole batch or
    # letting the balance go negative.
    current_balance = economy.balance(entry, user_id, xp)
    bought = 0
    total_cost = 0
    reached_level = level
    while bought < times and reached_level < C.STAT_MAX_LEVEL:
        step_cost = C.stat_upgrade_cost(reached_level)
        if total_cost + step_cost > current_balance:
            break
        total_cost += step_cost
        reached_level += 1
        bought += 1

    if bought == 0:
        needed = C.stat_upgrade_cost(level)
        return False, f"Нужно {needed} монет на следующий уровень {name}, у тебя {current_balance}.", 0

    ok, _ = economy.spend(entry, user_id, xp, total_cost, f"buy:pet_stat:{stat}", ref=str(reached_level))
    if not ok:
        # The balance moved between the check above and the debit (e.g. a second command
        # landed in between) -- refuse cleanly on stale numbers rather than overspend.
        return False, "Баланс изменился, попробуй ещё раз.", 0

    record["stats"][stat] = reached_level
    _save(entry, data)

    if bought == times:
        message = f"{name} прокачан(а) до {reached_level} уровня. Потрачено {total_cost} монет."
    elif reached_level >= C.STAT_MAX_LEVEL:
        message = (
            f"{name} прокачан(а) до максимума ({reached_level}). "
            f"Куплено {bought} из {times} уровней, потрачено {total_cost} монет."
        )
    else:
        message = (
            f"Хватило золота только на {bought} из {times} уровней {name} "
            f"(до {reached_level}), потрачено {total_cost} монет."
        )
    return True, message, total_cost


def effective_stats(entry, user_id) -> dict:
    return _effective_stats_for(_tamed_record(_load(entry), user_id) or {})


def _effective_stats_for(record: dict) -> dict:
    stat_levels = record.get("stats") or {}
    pet_level = record.get("level", 1)
    equipped = record.get("equipped") or {}

    bonuses = {key: 0 for key in C.STAT_KEYS}
    armor = 0
    for code in equipped.values():
        item = C.find_item(code) if code else None
        if item is None:
            continue
        for stat_key, amount in item.bonuses.items():
            if stat_key == "armor":
                armor += amount
            elif stat_key in bonuses:
                bonuses[stat_key] += amount

    result = {
        key: max(1, stat_levels.get(key, C.STAT_MIN_LEVEL) + pet_level * C.PET_LEVEL_STAT_BONUS + bonuses[key])
        for key in C.STAT_KEYS
    }
    result["armor"] = max(0, armor)
    return result


def power_rating(entry, user_id) -> int:
    """A comparable combat score from the stats that actually enter a fight.

    It intentionally is not a win/loss Elo score: new gear or a pet level-up must affect
    matchmaking immediately instead of requiring several unfair calibration fights.
    """
    record = _tamed_record(_load(entry), user_id)
    if record is None:
        return 0
    return _power_rating_for(record)


def _power_rating_for(record: dict) -> int:
    stats = _effective_stats_for(record)
    return C.POWER_RATING_BASE + sum(
        stats.get(key, 0) * C.POWER_RATING_WEIGHTS[key]
        for key in (*C.STAT_KEYS, "armor")
    )


# --- inventory & equipment -------------------------------------------------------------


def buy_item(entry, user_id, xp, code) -> tuple[bool, str]:
    data = _load(entry)
    record = _tamed_record(data, user_id)
    if record is None:
        return False, "Сначала приручи существо."
    item = C.find_item(code)
    if item is None:
        return False, "Такого предмета не существует."
    if item.source != "shop":
        return False, f"«{item.name}» нельзя купить -- он выпадает только из боёв."
    if item.code in record["inventory"]:
        return False, f"«{item.name}» у тебя уже есть."
    ok, balance = economy.spend(entry, user_id, xp, item.price, f"buy:pet_item:{item.code}")
    if not ok:
        return False, f"Нужно {item.price} монет, у тебя {balance}."
    record["inventory"].append(item.code)
    _save(entry, data)
    return True, f"Куплено: «{item.name}» за {item.price} монет."


def equip(entry, user_id, code) -> tuple[bool, str]:
    data = _load(entry)
    record = _tamed_record(data, user_id)
    if record is None:
        return False, "Сначала приручи существо."
    item = C.find_item(code)
    if item is None:
        return False, "Такого предмета не существует."
    if item.code not in record.get("inventory", []):
        return False, f"«{item.name}» не найден(а) в инвентаре."
    # A second weapon replaces the first outright -- one item per slot, the old one just
    # goes back to sitting unequipped in the inventory instead of vanishing.
    record.setdefault("equipped", {})[item.slot] = item.code
    _save(entry, data)
    return True, f"Экипировано: «{item.name}» ({C.SLOT_NAMES[item.slot]})."


def unequip(entry, user_id, slot) -> tuple[bool, str]:
    if slot not in C.SLOT_KEYS:
        return False, "Неизвестный слот."
    data = _load(entry)
    record = _tamed_record(data, user_id)
    if record is None:
        return False, "Сначала приручи существо."
    equipped = record.setdefault("equipped", {})
    current = equipped.get(slot)
    if not current:
        return False, f"В слоте «{C.SLOT_NAMES[slot]}» и так пусто."
    item = C.find_item(current)
    equipped[slot] = None
    _save(entry, data)
    return True, f"Снято: «{item.name if item else current}»."


# --- the arena ---------------------------------------------------------------------


def _chat_xp_for(entry, user_id) -> int:
    """One member's chat XP, derived synchronously from what is already on disk.

    Needed because the loser of a fight may be the DEFENDER, who is not the person
    playing -- the caller has the attacker's xp in hand and cannot possibly have theirs.
    economy.balance derives the earned half of a wallet from live XP, so charging a
    defender anything at all means working their XP out here.

    Two deliberate approximations, both erring the same safe way:
    `stats.aggregate_all_time` covers recorded days only, so today's earnings are not
    counted, and `words_per_point` is read from its frozen on-disk calibration rather than
    recomputed. Both make the number a slight UNDER-estimate, so the worst case is a loser
    who is charged less than they could afford -- never one billed for money they do not
    have.
    """
    wpp = stats._load_words_per_point(entry) or stats.DEFAULT_WORDS_PER_POINT
    user = stats.aggregate_all_time(entry).get(str(user_id))
    return user.xp(wpp) if user is not None else 0


def yesterday_activity(entry, user_id, today) -> tuple[int, int]:
    """(messages, figurines) this member posted YESTERDAY, from the recorded day file.

    A closed day, deliberately -- see C.daily_fight_allowance. It is also the only reason
    this is cheap enough to call on every menu draw: yesterday is already finalised on
    disk, so this is one local JSON read and never a Telegram fetch, unlike anything that
    has to know about today.

    Returns (0, 0) when there is no file for yesterday at all -- a chat whose stats
    tracking started this morning, or a midnight rollover that did not run. Everybody then
    falls back to the base allowance, which is the right failure: fewer fights than earned,
    never more.
    """
    day = today - timedelta(days=1)
    users = stats.aggregate(entry, day, day)
    user = users.get(str(user_id))
    if user is None:
        return 0, 0
    return user.messages, user.figurines_painted


def daily_allowance(entry, user_id, today) -> int:
    """Today's fight budget for one member, before anything they have already spent."""
    data = _load(entry)
    record = _tamed_record(data, user_id)
    if record is None:
        return 0
    messages, figurines = yesterday_activity(entry, user_id, today)
    return C.daily_fight_allowance(messages, figurines, record.get("cage_level", 1))


def fights_left(entry, user_id, today) -> int:
    data = _load(entry)
    record = _tamed_record(data, user_id)
    if record is None:
        return 0
    _reset_if_new_day(record, today)  # in-memory only; nothing to persist on a pure read
    messages, figurines = yesterday_activity(entry, user_id, today)
    allowance = C.daily_fight_allowance(messages, figurines, record.get("cage_level", 1))
    return max(0, allowance - record.get("fights_today", 0))


def claim_duel(entry, user_id, opponent_id, now=None) -> tuple[bool, str]:
    """Atomically reserve one public duel, including its once-per-target daily limit."""
    now = now or app_now()
    data = _load(entry)
    uid, opponent_uid = str(user_id), str(opponent_id)
    record = data.setdefault("duels", {}).setdefault(uid, {})
    today_key = now.date().isoformat()
    if record.get("day") != today_key:
        record.update({"day": today_key, "uses": 0, "last_at": None, "targets": {}})
    targets = record.setdefault("targets", {})
    last_at = record.get("last_at")
    if last_at:
        try:
            elapsed = (now - datetime.fromisoformat(last_at)).total_seconds()
        except (TypeError, ValueError):
            elapsed = C.DUEL_COOLDOWN_SECONDS
        if elapsed < C.DUEL_COOLDOWN_SECONDS:
            left = max(1, round(C.DUEL_COOLDOWN_SECONDS - elapsed))
            return False, f"До следующего дуэля {left // 60}:{left % 60:02d}."
    if record.get("uses", 0) >= C.DUEL_DAILY_LIMIT:
        return False, f"На сегодня дуэли закончились ({C.DUEL_DAILY_LIMIT}/{C.DUEL_DAILY_LIMIT})."
    if targets.get(opponent_uid, 0) >= C.DUEL_SAME_OPPONENT_DAILY_LIMIT:
        return False, "С этим соперником на сегодня уже был дуэль."
    record["uses"] = record.get("uses", 0) + 1
    targets[opponent_uid] = targets.get(opponent_uid, 0) + 1
    record["last_at"] = now.isoformat()
    _save(entry, data)
    return True, f"Дуэлей осталось: {C.DUEL_DAILY_LIMIT - record['uses']}."


def arena_attacks_against(entry, attacker_id, defender_id, day: date) -> int:
    """How often this attacker has already selected this defender on `day`."""
    attacker_uid, defender_uid = str(attacker_id), str(defender_id)
    return sum(
        1
        for fight in _load(entry).get("fights", [])
        if fight.get("date") == day.isoformat()
        and fight.get("attacker_id") == attacker_uid
        and fight.get("defender_id") == defender_uid
    )


def can_attack_in_arena(entry, attacker_id, defender_id, day: date | None = None) -> bool:
    day = day or today()
    return (
        arena_attacks_against(entry, attacker_id, defender_id, day)
        < C.ARENA_SAME_OPPONENT_DAILY_LIMIT
    )


def find_opponent(entry, user_id, rng=None, exclude_ids=None) -> str | None:
    rng = rng or random
    data = _load(entry)
    seeker = _tamed_record(data, user_id)
    if seeker is None:
        return None
    uid = str(user_id)
    excluded = {str(other_id) for other_id in (exclude_ids or ())}
    candidates = [
        other_id for other_id, record in data["pets"].items()
        if other_id != uid and other_id not in excluded and record.get("name")
    ]
    if not candidates:
        return None

    seeker_power = _power_rating_for(seeker)
    differences = {
        other_id: abs(_power_rating_for(data["pets"][other_id]) - seeker_power)
        for other_id in candidates
    }
    in_window = [
        other_id for other_id in candidates
        if differences[other_id] <= C.OPPONENT_POWER_WINDOW
    ]
    if in_window:
        return rng.choice(in_window)

    # A small arena still needs a match. When no fair-range opponent exists, choose only
    # among the nearest candidates rather than widening to an arbitrary power gap.
    nearest_gap = min(differences.values())
    nearest = [other_id for other_id in candidates if differences[other_id] == nearest_gap]
    return rng.choice(nearest)


def opponent_cycle(entry, user_id, seed: int) -> list[str]:
    """A deterministic, power-first list for one opponent-search session.

    The listener uses positions 0 through 3 from this list, so reroll buttons can never
    repeat an earlier candidate while still working after a bot restart.
    """
    data = _load(entry)
    seeker = _tamed_record(data, user_id)
    if seeker is None:
        return []
    uid = str(user_id)
    candidates = [
        other_id for other_id, record in data["pets"].items()
        if other_id != uid
        and record.get("name")
        and can_attack_in_arena(entry, uid, other_id)
    ]
    seeker_power = _power_rating_for(seeker)
    differences = {
        other_id: abs(_power_rating_for(data["pets"][other_id]) - seeker_power)
        for other_id in candidates
    }
    picker = random.Random(seed)
    picker.shuffle(candidates)
    return sorted(
        candidates,
        key=lambda other_id: (
            differences[other_id] > C.OPPONENT_POWER_WINDOW,
            differences[other_id],
        ),
    )


def record_fight(
    entry, attacker_id, defender_id, result, today, attacker_xp=None, combat_snapshot=None,
) -> dict:
    data = _load(entry)
    attacker_uid, defender_uid = str(attacker_id), str(defender_id)
    attacker = data["pets"][attacker_uid]
    defender = data["pets"][defender_uid]

    # Only the attacker spends a daily fight. The defender did not choose this fight, so
    # it must not come out of the budget they earned by chatting -- the loss penalty below
    # is the only thing a defender can be made to pay.
    _reset_if_new_day(attacker, today)
    attacker["fights_today"] = attacker.get("fights_today", 0) + 1
    attacker["fights"] = attacker.get("fights", 0) + 1
    defender["fights"] = defender.get("fights", 0) + 1

    is_draw = bool(getattr(result, "is_draw", False))
    if is_draw:
        _, attacker_levels_gained = _apply_xp(attacker, C.DRAW_XP)
        _, defender_levels_gained = _apply_xp(defender, C.DRAW_XP)
        data["fights"].append({
            "ts": app_now().isoformat(),
            "date": today.isoformat(),
            "attacker_id": attacker_uid,
            "defender_id": defender_uid,
            "winner_id": None,
            "loser_id": None,
            "draw": True,
            "attacker_name": attacker.get("name"),
            "defender_name": defender.get("name"),
            "attacker_owner": attacker.get("owner_name"),
            "defender_owner": defender.get("owner_name"),
            "gold": 0,
            "loss_gold": 0,
            "dropped_item": None,
            "combat_seed": getattr(result, "seed", None),
            "total_damage": dict(getattr(result, "total_damage", {})),
            "combat_snapshot": combat_snapshot,
        })
        _save(entry, data)
        return {
            "draw": True,
            "gold": 0,
            "loss_gold": 0,
            "xp": C.DRAW_XP,
            "levels_gained": attacker_levels_gained,
            "level": attacker.get("level", 1),
            "dropped_item": None,
            "opponent_levels_gained": defender_levels_gained,
        }

    winner_uid = str(result.winner)
    loser_uid = str(result.loser)
    winner = data["pets"][winner_uid]
    loser = data["pets"][loser_uid]
    winner["wins"] = winner.get("wins", 0) + 1

    winner_cage_level = winner.get("cage_level", 1)
    bonus_pct = C.CAGE_GOLD_BONUS_PCT[winner_cage_level - 1]
    gold = round(random.randint(C.WIN_GOLD_MIN, C.WIN_GOLD_MAX) * (1 + bonus_pct / 100))
    economy.grant(entry, winner_uid, gold, "pet_fight_win")

    # The loser pays half of that. Charged as a spend rather than a negative grant so it
    # lands in the same ledger column as every other purchase -- and clamped to what they
    # actually hold, because economy.balance floors at zero and a debt nobody can see the
    # cause of is worse than a bill that got rounded down. `loser_xp` is the loser's live
    # chat XP, which the caller cannot supply for a defender who is not the one playing,
    # so it is read from the same aggregate economy.balance itself uses.
    penalty = C.loss_gold_for(gold)
    paid = 0
    if penalty > 0:
        # Prefer the caller's own figure for the attacker: it is the live, today-inclusive
        # XP economy.balance wants, whereas _chat_xp_for can only see closed days. The
        # fallback is for the defender, whose XP the caller has no way to know.
        if loser_uid == attacker_uid and attacker_xp is not None:
            loser_xp = attacker_xp
        else:
            loser_xp = _chat_xp_for(entry, loser_uid)
        affordable = min(penalty, economy.balance(entry, loser_uid, loser_xp))
        if affordable > 0:
            ok, _ = economy.spend(
                entry, loser_uid, loser_xp, affordable, "pet_fight_loss", ref=winner_uid
            )
            paid = affordable if ok else 0

    dropped_code = None
    if random.random() < C.DROP_CHANCE:
        drop_pool = [item for item in C.ITEMS if item.source == "drop"]
        if drop_pool:
            dropped = random.choice(drop_pool)
            winner.setdefault("inventory", []).append(dropped.code)
            dropped_code = dropped.code

    _, winner_levels_gained = _apply_xp(winner, C.WIN_XP)
    _, loser_levels_gained = _apply_xp(loser, C.LOSS_XP)

    # Names/owners are snapshotted INTO the log entry rather than looked up when
    # history() is read, so a later rename does not rewrite what already happened.
    data["fights"].append({
        "ts": app_now().isoformat(),
        "date": today.isoformat(),
        "attacker_id": attacker_uid,
        "defender_id": defender_uid,
        "winner_id": winner_uid,
        "loser_id": loser_uid,
        "draw": False,
        "attacker_name": attacker.get("name"),
        "defender_name": defender.get("name"),
        "attacker_owner": attacker.get("owner_name"),
        "defender_owner": defender.get("owner_name"),
        "gold": gold,
        # What the LOSER actually paid, which is not always C.loss_gold_for(gold): an
        # empty wallet pays what it has. Stored so a history line can show the real
        # number rather than recomputing an amount that was never charged.
        "loss_gold": paid,
        "dropped_item": dropped_code,
        "combat_seed": getattr(result, "seed", None),
        "total_damage": dict(getattr(result, "total_damage", {})),
        "combat_snapshot": combat_snapshot,
    })
    _save(entry, data)

    attacker_won = winner_uid == attacker_uid
    return {
        "draw": False,
        "gold": gold if attacker_won else 0,
        "loss_gold": 0 if attacker_won else paid,
        "xp": C.WIN_XP if attacker_won else C.LOSS_XP,
        "levels_gained": winner_levels_gained if attacker_won else loser_levels_gained,
        "level": attacker.get("level", 1),
        "dropped_item": dropped_code if attacker_won else None,
    }


def history(entry, user_id) -> list[dict]:
    data = _load(entry)
    uid = str(user_id)
    mine = []
    for fight in data.get("fights", []):
        if fight.get("attacker_id") != uid and fight.get("defender_id") != uid:
            continue
        row = dict(fight)
        # Both money columns are rewritten from the READER's side: "gold" is what the
        # winner received and "loss_gold" what the loser paid, so exactly one of them can
        # be non-zero on any one person's line.
        won = fight.get("winner_id") == uid
        row["gold"] = fight.get("gold", 0) if won else 0
        row["loss_gold"] = 0 if won else fight.get("loss_gold", 0)
        mine.append(row)
    mine.reverse()  # stored oldest -> newest, so reverse for "newest first"
    return mine[:C.HISTORY_LIMIT]


def award_xp(entry, user_id, amount) -> tuple[int, int]:
    data = _load(entry)
    record = data["pets"].get(str(user_id))
    if record is None:
        return 1, 0
    new_level, levels_gained = _apply_xp(record, amount)
    _save(entry, data)
    return new_level, levels_gained
