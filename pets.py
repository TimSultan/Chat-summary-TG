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

import hashlib
import json
import random
import secrets
import threading
from datetime import date, datetime, timedelta

import economy
import pets_config as C
import stats
from app_time import now as app_now

PETS_STORE_VERSION = 3
# Rolling fight log, capped independently of C.HISTORY_LIMIT (that constant bounds what
# ONE player is shown per /history call, not how many chat-wide entries are kept on disk)
# -- mirrors economy.py's LOG_LIMIT convention for the same reason: trimming this can
# never change anybody's stats, wins or gold, all of which live on the per-pet record.
FIGHT_LOG_LIMIT = 2_000
# Store-level marker: this chat's one-off cage-upgrade refund has already been paid out.
# See refund_cage_upgrades for why the per-user lock alone would keep paying forever.
CAGE_UPGRADE_REFUND_FLAG = "cage_upgrade_refund_202608"
UNIQUE_WEAPONS_MIGRATION_FLAG = "unique_weapons_202608"
REMOVED_MOP_CODE = "w003"
REMOVED_MOP_COMPENSATION = 100

_NAME_MAX_LEN = 24
# The poller and a button press can settle the same finished run in one process.  The
# run id also keys the economy grant, so a process restart cannot mint a second payout.
_farm_settlement_lock = threading.RLock()


# --- storage -----------------------------------------------------------------------


def _pets_path(entry: str):
    return stats._stats_dir() / f"{stats._cache_key(entry)}_pets.json"


def _empty() -> dict:
    return {
        "version": PETS_STORE_VERSION, "pets": {}, "fights": [], "duels": {},
        "gift_history": [], "economy_metrics": _new_economy_metrics(),
    }


def _new_economy_metrics() -> dict:
    """Aggregate-only game-economy observability; never a second balance ledger."""
    return {
        "passive_gold_minted": 0,
        "farm_gold_minted": 0,
        "farm_runs": 0,
        "item_sale_gold": 0,
        "gifts": 0,
        "arena_reward_gold": 0,
        "guardian_interventions": 0,
        "drops_by_rarity": {
            rarity: 0 for rarity in ("cursed", "common", "uncommon", "rare", "legendary")
        },
    }


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
    data.setdefault("gift_history", [])
    _economy_metrics(data)
    # Older saves used an append-only list.  Accept their duplicates while reading,
    # then expose a canonical unique inventory to every game operation.
    for record in data["pets"].values():
        if not isinstance(record, dict):
            continue
        inventory = record.get("inventory")
        if not isinstance(inventory, list):
            inventory = []
        seen = set()
        unique = []
        for code in inventory:
            code = C.LEGACY_ITEM_CODES.get(code, code)
            if isinstance(code, str) and code not in seen:
                seen.add(code)
                unique.append(code)
        record["inventory"] = unique
        equipped = record.get("equipped")
        if not isinstance(equipped, dict):
            equipped = {}
            record["equipped"] = equipped
        for slot in C.SLOT_KEYS:
            equipped.setdefault(slot, None)
            code = C.LEGACY_ITEM_CODES.get(equipped.get(slot), equipped.get(slot))
            equipped[slot] = code
            if code and code not in unique:
                # A historic equipped object is still owned; preserve it rather than
                # stripping it as a side effect of a duplicate-data migration.
                unique.append(code)
        # Discovery is permanent: selling or gifting something should not erase it
        # from the collection book.  Older saves had no such field, so their current
        # bag and worn equipment are the complete historic collection we can infer.
        discovered = record.get("discovered")
        if not isinstance(discovered, list):
            discovered = []
        discovered_unique = []
        discovered_seen = set()
        for code in discovered + unique:
            code = C.LEGACY_ITEM_CODES.get(code, code)
            if isinstance(code, str) and C.find_item(code) is not None and code not in discovered_seen:
                discovered_seen.add(code)
                discovered_unique.append(code)
        record["discovered"] = discovered_unique
        # A lock is a personal safety switch, not an item attribute.  Keep only locks
        # for gear that is still in the player's bag; a transferred item must never
        # arrive locked for its new owner.
        locked = record.get("locked_items")
        if not isinstance(locked, list):
            locked = []
        record["locked_items"] = [
            code for code in dict.fromkeys(C.LEGACY_ITEM_CODES.get(code, code) for code in locked)
            if code in unique
        ]
        pending = record.get("pending_item_actions")
        # Confirmation secrets are only a short-lived UX/security handshake.  Bad or
        # historic values are dropped instead of letting malformed saves block gear.
        record["pending_item_actions"] = pending if isinstance(pending, dict) else {}
        # A farm job is intentionally stored on the pet, not in a transient scheduler:
        # bot restarts must not bring a pet home early or lose its reward.
        farm_run = record.get("farm_run")
        record["farm_run"] = farm_run if isinstance(farm_run, dict) else None
        notifications = record.get("farm_notifications")
        record["farm_notifications"] = (
            [row for row in notifications if isinstance(row, dict)][-50:]
            if isinstance(notifications, list) else []
        )
    return data


def _economy_metrics(data: dict) -> dict:
    """Return a repaired aggregate metrics record without retaining member profiles."""
    metrics = data.setdefault("economy_metrics", {})
    defaults = _new_economy_metrics()
    for key, default in defaults.items():
        if key == "drops_by_rarity":
            drops = metrics.setdefault(key, {})
            if not isinstance(drops, dict):
                drops = metrics[key] = {}
            for rarity in default:
                drops[rarity] = max(0, int(drops.get(rarity, 0) or 0))
        else:
            metrics[key] = max(0, int(metrics.get(key, default) or 0))
    return metrics


def _metric_add(data: dict, key: str, amount: int = 1, *, rarity: str | None = None) -> None:
    metrics = _economy_metrics(data)
    if rarity is not None:
        drops = metrics["drops_by_rarity"]
        drops[rarity] = drops.get(rarity, 0) + max(0, int(amount))
    else:
        metrics[key] = metrics.get(key, 0) + max(0, int(amount))


def economy_telemetry(entry: str) -> dict:
    """Read-only aggregate counters for balancing; no names, messages, or wallets."""
    metrics = _economy_metrics(_load(entry))
    return {
        key: (dict(value) if isinstance(value, dict) else value)
        for key, value in metrics.items()
    }


def gift_history(entry: str) -> list[dict]:
    """Newest-first audit of item handoffs, containing only IDs, code and timestamp."""
    data = _load(entry)
    rows = [row for row in data.get("gift_history", []) if isinstance(row, dict)]
    return [dict(row) for row in reversed(rows[-C.GIFT_AUDIT_LIMIT:])]


def discovered_weapon_collection(entry: str) -> list[dict]:
    """Chat-wide discovered weapons with their current owner.

    Discovery survives sale, so a weapon remains visible even when nobody currently
    carries it. Ownership is derived from live inventories rather than the audit log;
    Every catalogue weapon is a single chat-wide object, so at most one current owner
    is expected after the startup migration.
    """
    data = _load(entry)
    discovered: set[str] = set()
    owners: dict[str, list[dict]] = {}
    for user_id, record in data.get("pets", {}).items():
        if not isinstance(record, dict) or not record.get("name"):
            continue
        for code in record.get("discovered", []):
            item = C.find_item(code)
            if item is not None and item.slot == "weapon":
                discovered.add(item.code)
        owner = {
            "user_id": str(user_id),
            "name": record.get("owner_name") or "кто-то",
            "username": (record.get("owner_username") or "").lstrip("@") or None,
        }
        for code in record.get("inventory", []):
            item = C.find_item(code)
            if item is None or item.slot != "weapon":
                continue
            discovered.add(item.code)
            owners.setdefault(item.code, []).append(owner)
    return [
        {"code": item.code, "owners": tuple(owners.get(item.code, ()))}
        for item in sorted(C.items_for_slot("weapon"), key=lambda candidate: candidate.code)
        if item.code in discovered
    ]


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
        "owner_username": None,
        "cage_price_paid": C.CAGE_PRICE,
        "cage_level": 1,
        "stats": {key: C.STAT_MIN_LEVEL for key in C.STAT_KEYS},
        "equipped": {slot: None for slot in C.SLOT_KEYS},
        "inventory": [],
        "discovered": [],
        "locked_items": [],
        "pending_item_actions": {},
        "level": 1,
        "xp": 0,
        "fights": 0,
        "wins": 0,
        "created_at": app_now().isoformat(),
        "fights_today": 0,
        "fights_day": app_now().date().isoformat(),
        "farm_level": 0,
        "farm_features": {},
        "farm_run": None,
        "farm_notifications": [],
    }


def _tamed_record(data: dict, user_id) -> dict | None:
    record = data["pets"].get(str(user_id))
    return record if record and record.get("name") else None


def _owned_weapon_codes(data: dict) -> set[str]:
    """All weapon objects currently owned anywhere in one chat."""
    owned = set()
    for record in data.get("pets", {}).values():
        if not isinstance(record, dict):
            continue
        for code in record.get("inventory", []):
            item = C.find_item(code)
            if item is not None and item.slot == "weapon":
                owned.add(item.code)
    return owned


def _weapon_owner_ids(data: dict, code: str) -> list[str]:
    return [
        str(user_id)
        for user_id, record in data.get("pets", {}).items()
        if isinstance(record, dict) and code in record.get("inventory", [])
    ]


def _daily_storefront_weapons(data: dict, entry: str, day: date | None = None):
    return C.daily_storefront_weapons(
        entry, day or today(), excluded_codes=_owned_weapon_codes(data),
    )


def daily_storefront_weapons(entry: str, day: date | None = None):
    """The shared daily stock, excluding every weapon already owned in this chat."""
    return _daily_storefront_weapons(_load(entry), entry, day)


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


def hamsterator_level(entry, user_id) -> int:
    """Level of the passive facility; zero means it has not been built."""
    record = _load(entry)["pets"].get(str(user_id))
    return record.get("hamsterator_level", 0) if record else 0


def _hamsterator_terms(level: int) -> tuple[int, int]:
    level = min(max(0, int(level)), C.HAMSTERATOR_MAX_LEVEL)
    return C.HAMSTERATOR_GOLD_PER_HOUR[level], C.HAMSTERATOR_STORAGE_CAP[level]


def settle_passive_income(entry, user_id, now: datetime | None = None) -> dict:
    rate, cap = _hamsterator_terms(hamsterator_level(entry, user_id))
    result = economy.settle_passive_income(entry, user_id, rate, cap, now=now)
    # The underlying ledger advances its checkpoint atomically with the credit. A
    # retry therefore reports zero and cannot inflate this aggregate counter either.
    credited = max(0, int(result.get("credited", 0) or 0))
    if credited:
        data = _load(entry)
        _metric_add(data, "passive_gold_minted", credited)
        _save(entry, data)
    return result


def passive_income_status(entry, user_id, now: datetime | None = None) -> dict:
    level = hamsterator_level(entry, user_id)
    rate, cap = _hamsterator_terms(level)
    return {
        **economy.passive_income_status(entry, user_id, rate, cap, now=now),
        "level": level, "rate": rate, "cap": cap,
    }


def balance_for(entry, user_id, xp) -> int:
    """Thin pass-through so pets_ui never has to import economy directly."""
    # All arena balance views collect complete hours. The ledger's checkpoint and bonus
    # are written together, so a redraw/retry is idempotent.
    if has_cage(entry, user_id):
        settle_passive_income(entry, user_id)
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


def refund_legacy_cages(entries) -> int:
    """Refund the old cage price once for every cage created before the price increase."""
    refunded = 0
    for entry in entries:
        data = _load(entry)
        changed = False
        for user_id, record in data["pets"].items():
            if record.get("cage_price_paid") is not None:
                continue
            if economy.refund_once(
                entry, user_id, 0, C.LEGACY_CAGE_PRICE, "pet_cage_price_202608",
            ):
                refunded += 1
            record["cage_price_paid"] = C.LEGACY_CAGE_PRICE
            changed = True
        if changed:
            _save(entry, data)
    return refunded


def refund_cage_upgrades(entries) -> int:
    """Pay C.CAGE_UPGRADE_REFUND once to everyone who bought a cage upgrade at the old,
    escalating price -- called at boot, like refund_legacy_cages.

    `cage_level > 1` is the signal rather than a `buy:pet_cage_upgrade` row in the economy
    log: that log is capped (economy.LOG_LIMIT), so an early upgrade may already have been
    trimmed out of it, while the level on the pet record never decreases.

    Two locks, because one is not enough here. Per user, economy.grant_once keys on the
    migration name, so a crash halfway through a chat cannot pay anybody twice. Per chat,
    CAGE_UPGRADE_REFUND_FLAG closes the eligibility window at the first run: without it
    every FUTURE upgrader would also collect 350, which at the new flat CAGE_UPGRADE_COSTS
    of 100 turns "buy an upgrade" into a 250-coin profit on the next restart."""
    refunded = 0
    for entry in entries:
        data = _load(entry)
        if data.get(CAGE_UPGRADE_REFUND_FLAG):
            continue
        for user_id, record in data["pets"].items():
            if record.get("cage_level", 1) <= 1:
                continue
            if economy.grant_once(
                entry, user_id, C.CAGE_UPGRADE_REFUND, "pet_cage_upgrade_202608",
            ):
                refunded += 1
        data[CAGE_UPGRADE_REFUND_FLAG] = True
        _save(entry, data)
    return refunded


def _remove_weapon_from_record(record: dict, code: str) -> None:
    record["inventory"] = [owned for owned in record.get("inventory", []) if owned != code]
    for slot, equipped_code in (record.get("equipped") or {}).items():
        if equipped_code == code:
            record["equipped"][slot] = None
    record["locked_items"] = [
        locked for locked in record.get("locked_items", []) if locked != code
    ]
    pending = record.get("pending_item_actions") or {}
    record["pending_item_actions"] = {
        key: token for key, token in pending.items() if not key.endswith(f":{code}")
    }


# --- farm --------------------------------------------------------------------------


def farm_level(entry, user_id) -> int:
    """Current farm level, with zero meaning that it has not been built yet."""
    record = _tamed_record(_load(entry), user_id)
    if record is None:
        return 0
    return min(max(0, int(record.get("farm_level", 0) or 0)), C.FARM_MAX_LEVEL)


def _farm_features(record: dict) -> dict[str, bool]:
    raw = record.get("farm_features")
    raw = raw if isinstance(raw, dict) else {}
    return {key: bool(raw.get(key)) for key in C.FARM_FEATURES}


def _farm_multipliers(record: dict) -> tuple[float, float, float]:
    """Gold, pet-XP and item-find terms for one farm run."""
    gold, pet_xp, drop = 1.0, 1.0, C.FARM_DROP_CHANCE
    for key, owned in _farm_features(record).items():
        if not owned:
            continue
        feature = C.FARM_FEATURES[key]
        gold *= float(feature.get("gold_multiplier", 1.0))
        pet_xp *= float(feature.get("xp_multiplier", 1.0))
        drop += float(feature.get("drop_bonus", 0.0))
    return gold, pet_xp, min(1.0, drop)


def _farm_item_for(data: dict, record: dict, rng: random.Random, chance: float):
    """Pick a deterministic personal accessory find, never a chat-unique weapon.

    A farm locks the pet out of arena drops, but equipment may still be gifted while it
    is away.  Restricting finds to accessories keeps the reservation-free farm job from
    racing the chat-wide one-copy weapon catalogue.
    """
    if rng.random() >= chance:
        return None
    owned = set(record.get("inventory", []))
    pool = [
        item for item in C.ITEMS
        if item.source == "drop" and item.slot != "weapon" and item.code not in owned
    ]
    if not pool:
        return None
    weighted = [
        item for item in pool for _ in range(max(0, int(getattr(item, "drop_weight", 1))))
    ]
    return rng.choice(weighted) if weighted else None


def _farm_reward(data: dict, record: dict, run_id: str) -> dict:
    level = min(max(1, int(record.get("farm_level", 0))), C.FARM_MAX_LEVEL)
    gold_multiplier, xp_multiplier, drop_chance = _farm_multipliers(record)
    # The run id is persisted before the pet leaves.  Thus an interrupted poll/retry
    # cannot reroll either rewards or the found item.
    rng = random.Random(run_id)
    found = _farm_item_for(data, record, rng, drop_chance)
    return {
        "gold": max(1, round(C.FARM_GOLD_PER_RUN[level] * gold_multiplier)),
        "xp": max(1, round(C.FARM_XP_PER_RUN[level] * xp_multiplier)),
        "item_code": found.code if found is not None else None,
    }


def _farm_run_ready(run: dict, moment: datetime) -> bool:
    try:
        return moment >= datetime.fromisoformat(str(run.get("ready_at")))
    except (TypeError, ValueError):
        # Corrupt historic state must never hold a pet hostage indefinitely.
        return True


def _repair_farm_run_id(user_id: str, run: dict) -> str:
    """Give a malformed historic run a stable id before any payout is attempted.

    A missing run id used to make a due job invisible forever.  This id is derived only
    from persisted fields, so every retry chooses the same `grant_once` key.  A damaged
    reward is conservatively repaired to zero rather than inventing a payout.
    """
    material = "|".join((str(user_id), str(run.get("started_at") or ""), str(run.get("ready_at") or "")))
    run["run_id"] = "recovered-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    reward = run.get("reward")
    if not isinstance(reward, dict):
        reward = {}
    run["reward"] = {
        "gold": max(0, int(reward.get("gold", 0) or 0)),
        "xp": max(0, int(reward.get("xp", 0) or 0)),
        "item_code": reward.get("item_code") if C.find_item(reward.get("item_code")) else None,
    }
    return run["run_id"]


def _is_farming_record(record: dict | None, moment: datetime | None = None) -> bool:
    if not isinstance(record, dict):
        return False
    run = record.get("farm_run")
    return isinstance(run, dict) and not _farm_run_ready(run, moment or app_now())


def is_farming(entry, user_id, now: datetime | None = None) -> bool:
    """Whether the pet is still inside its exact six-hour farm shift."""
    return _is_farming_record(_tamed_record(_load(entry), user_id), now)


def farm_status(entry, user_id, now: datetime | None = None) -> dict:
    """Read-only UI status for the Farm button and scheduler.

    ``running`` means the six-hour lock is active. ``ready`` means a completed job is
    awaiting settlement; callers should invoke :func:`settle_completed_farms` instead
    of treating it as a second collect button.
    """
    moment = now or app_now()
    record = _tamed_record(_load(entry), user_id)
    if record is None:
        return {"available": False, "level": 0, "running": False, "ready": False}
    level = min(max(0, int(record.get("farm_level", 0) or 0)), C.FARM_MAX_LEVEL)
    run = record.get("farm_run") if isinstance(record.get("farm_run"), dict) else None
    ready = bool(run) and _farm_run_ready(run, moment)
    ready_at = run.get("ready_at") if run else None
    seconds_left = 0
    if run and not ready:
        try:
            seconds_left = max(0, int((datetime.fromisoformat(ready_at) - moment).total_seconds()))
        except (TypeError, ValueError):
            seconds_left = 0
    features = _farm_features(record)
    gold_multiplier, xp_multiplier, drop_chance = _farm_multipliers(record)
    estimate_level = max(1, level)
    feature_status = {
        key: {
            "level": int(owned), "max_level": 1,
            "cost": int(C.FARM_FEATURES[key]["cost"]),
            "next_cost": None if owned else int(C.FARM_FEATURES[key]["cost"]),
            "effect": (
                "+25% монет" if key == "well" else
                "+25% опыта" if key == "sprinkler" else
                "шанс вещи 3% → 8%" if key == "beds" else
                "+20% монет и опыта"
            ),
        }
        for key, owned in features.items()
    }
    return {
        "available": True,
        "level": level,
        "max_level": C.FARM_MAX_LEVEL,
        "duration_hours": C.FARM_DURATION_HOURS,
        "running": bool(run) and not ready,
        # ``active`` is retained for the Telegram view's simple boolean contract.
        "active": bool(run) and not ready,
        "ready": ready,
        "can_start": level > 0 and not run,
        "started_at": run.get("started_at") if run else None,
        "ready_at": ready_at,
        "seconds_left": seconds_left,
        "reward": dict(run.get("reward") or {}) if run else None,
        "features": feature_status,
        "feature_costs": {key: int(spec["cost"]) for key, spec in C.FARM_FEATURES.items()},
        "next_level_cost": C.FARM_UPGRADE_COSTS[level] if level < C.FARM_MAX_LEVEL else None,
        "next_level_bonus": (
            f"{C.FARM_GOLD_PER_RUN[level + 1]} монет · {C.FARM_XP_PER_RUN[level + 1]} опыта за рейс"
            if level < C.FARM_MAX_LEVEL else None
        ),
        "estimated_gold": max(1, round(C.FARM_GOLD_PER_RUN[estimate_level] * gold_multiplier)),
        "estimated_xp": max(1, round(C.FARM_XP_PER_RUN[estimate_level] * xp_multiplier)),
        "drop_chance": drop_chance,
    }


def start_farm(entry, user_id, now: datetime | None = None) -> tuple[bool, str]:
    """Send a pet to the farm for exactly six hours, with reward fixed up front."""
    moment = now or app_now()
    # Finish a due run first so a user who opens the menu after six hours is not stuck
    # waiting for the background poller before starting the next one.
    settle_completed_farms(entry, now=moment)
    data = _load(entry)
    record = _tamed_record(data, user_id)
    if record is None:
        return False, "Сначала приручи существо."
    level = min(max(0, int(record.get("farm_level", 0) or 0)), C.FARM_MAX_LEVEL)
    if level <= 0:
        return False, "Сначала построй ферму: прокачай её до 1 уровня."
    if isinstance(record.get("farm_run"), dict):
        return False, "Питомец уже работает на ферме."
    run_id = secrets.token_hex(16)
    ready_at = moment + timedelta(hours=C.FARM_DURATION_HOURS)
    record["farm_run"] = {
        "run_id": run_id,
        "started_at": moment.isoformat(),
        "ready_at": ready_at.isoformat(),
        "reward": _farm_reward(data, record, run_id),
    }
    _save(entry, data)
    return True, "Питомец отправлен на ферму на 6 часов."


def upgrade_farm(entry, user_id, xp) -> tuple[bool, str]:
    """Build/upgrade the farm one level, charging the shared coin wallet once."""
    data = _load(entry)
    record = _tamed_record(data, user_id)
    if record is None:
        return False, "Сначала приручи существо."
    level = min(max(0, int(record.get("farm_level", 0) or 0)), C.FARM_MAX_LEVEL)
    if level >= C.FARM_MAX_LEVEL:
        return False, f"Ферма уже максимального уровня ({C.FARM_MAX_LEVEL})."
    cost = C.FARM_UPGRADE_COSTS[level]
    ok, balance = economy.spend(entry, user_id, xp, cost, "buy:pet_farm_upgrade")
    if not ok:
        return False, f"Нужно {cost} монет на ферму, у тебя {balance}."
    record["farm_level"] = level + 1
    _save(entry, data)
    return True, f"Ферма прокачана до {level + 1} уровня за {cost} монет."


def upgrade_farm_feature(entry, user_id, xp, feature: str) -> tuple[bool, str]:
    """Buy one permanent farm feature (well, sprinkler, beds or tractor)."""
    key = str(feature or "").strip().lower()
    spec = C.FARM_FEATURES.get(key)
    if spec is None:
        return False, "Такого улучшения фермы нет."
    data = _load(entry)
    record = _tamed_record(data, user_id)
    if record is None:
        return False, "Сначала приручи существо."
    if int(record.get("farm_level", 0) or 0) <= 0:
        return False, "Сначала построй ферму."
    features = record.setdefault("farm_features", {})
    if features.get(key):
        return False, f"«{spec['name']}» уже установлена."
    cost = int(spec["cost"])
    ok, balance = economy.spend(entry, user_id, xp, cost, f"buy:pet_farm_feature:{key}")
    if not ok:
        return False, f"Нужно {cost} монет на «{spec['name']}», у тебя {balance}."
    features[key] = True
    _save(entry, data)
    return True, f"На ферме появилась «{spec['name']}» за {cost} монет."


def _farm_receipt(user_id: str, record: dict, run: dict, moment: datetime, levels_gained: int,
                  item_code: str | None, auto_equipped: bool) -> dict:
    reward = run.get("reward") if isinstance(run.get("reward"), dict) else {}
    return {
        "user_id": str(user_id),
        "run_id": str(run.get("run_id") or ""),
        "pet_name": record.get("name") or "Питомец",
        "gold": max(0, int(reward.get("gold", 0) or 0)),
        "xp": max(0, int(reward.get("xp", 0) or 0)),
        "levels_gained": max(0, int(levels_gained)),
        "level": int(record.get("level", 1) or 1),
        "item_code": item_code,
        "item": item_code,
        "auto_equipped": bool(auto_equipped),
        "settled_at": moment.isoformat(),
        "notified_at": None,
    }


def settle_completed_farms(entry, now: datetime | None = None) -> list[dict]:
    """Idempotently settle every due farm run and return only newly completed receipts.

    Gold is guarded by ``economy.grant_once(..., run_id)``.  The same-process lock
    covers concurrent poll ticks/button requests, while a crash between the two stores
    retries from the persisted reward and merely observes the existing ledger grant.
    """
    moment = now or app_now()
    receipts = []
    with _farm_settlement_lock:
        initial = _load(entry)
        due = []
        repaired = False
        for user_id, record in initial.get("pets", {}).items():
            if not isinstance(record, dict) or not record.get("name"):
                continue
            run = record.get("farm_run")
            if not isinstance(run, dict) or not _farm_run_ready(run, moment):
                continue
            if not str(run.get("run_id") or ""):
                _repair_farm_run_id(str(user_id), run)
                repaired = True
            due.append((str(user_id), dict(run)))
        if repaired:
            # Persist the deterministic recovery before `grant_once`; a crash after it
            # is therefore retried under exactly the same payout key.
            _save(entry, initial)
        for user_id, snapshot_run in due:
            run_id = str(snapshot_run.get("run_id") or "")
            reward = snapshot_run.get("reward") if isinstance(snapshot_run.get("reward"), dict) else {}
            gold = max(0, int(reward.get("gold", 0) or 0))
            if gold:
                economy.grant_once(entry, user_id, gold, f"pet:farm:{run_id}")
            # Reload after the independent economy write.  Do not overwrite a change
            # made by arena/UI code while this particular job was being paid.
            data = _load(entry)
            record = _tamed_record(data, user_id)
            run = record.get("farm_run") if record else None
            if not isinstance(run, dict) or str(run.get("run_id") or "") != run_id:
                continue
            reward = run.get("reward") if isinstance(run.get("reward"), dict) else {}
            _, levels_gained = _apply_xp(record, max(0, int(reward.get("xp", 0) or 0)))
            item_code = reward.get("item_code")
            item = C.find_item(item_code) if item_code else None
            auto_equipped = False
            if item is not None and item.code not in record.setdefault("inventory", []):
                record["inventory"].append(item.code)
                _discover(record, item.code)
                equipped = record.setdefault("equipped", {})
                current = C.find_item(equipped.get(item.slot))
                if current is None or C.equipment_score(item) > C.equipment_score(current):
                    equipped[item.slot] = item.code
                    auto_equipped = True
                _metric_add(data, "drops_by_rarity", rarity=item.rarity)
            else:
                item_code = None
            receipt = _farm_receipt(
                user_id, record, run, moment, levels_gained, item_code, auto_equipped,
            )
            record.setdefault("farm_notifications", []).append(receipt)
            record["farm_notifications"] = record["farm_notifications"][-50:]
            record["farm_run"] = None
            _metric_add(data, "farm_gold_minted", gold)
            _metric_add(data, "farm_runs")
            _save(entry, data)
            receipts.append(dict(receipt))
    return receipts


def pending_farm_notifications(entry) -> list[dict]:
    """Receipts whose direct-message delivery still needs a retry."""
    data = _load(entry)
    pending = []
    for user_id, record in data.get("pets", {}).items():
        if not isinstance(record, dict):
            continue
        for receipt in record.get("farm_notifications", []):
            if isinstance(receipt, dict) and not receipt.get("notified_at"):
                row = dict(receipt)
                row["user_id"] = str(user_id)
                pending.append(row)
    return pending


def mark_farm_notified(entry, user_id, run_id, now: datetime | None = None) -> bool:
    """Mark one receipt delivered only after a successful Telegram private message."""
    with _farm_settlement_lock:
        data = _load(entry)
        record = _tamed_record(data, user_id)
        if record is None:
            return False
        for receipt in record.get("farm_notifications", []):
            if str(receipt.get("run_id") or "") != str(run_id):
                continue
            if receipt.get("notified_at"):
                return False
            receipt["notified_at"] = (now or app_now()).isoformat()
            _save(entry, data)
            return True
    return False


def enforce_unique_weapons(entries) -> dict:
    """One-time cleanup that turns catalogue codes into chat-wide unique objects.

    ``w003`` is deliberately removed from every old owner and pays the requested fixed
    100 coins. For other historic duplicates, one copy remains (an equipped copy wins
    the deterministic tie-break); removed shop copies receive their purchase price and
    removed drops receive their normal resale value. Every grant is independently
    idempotent, so a crash before the pets save cannot pay anyone twice on restart.
    """
    report = {
        "removed_mops": 0,
        "mop_grants": 0,
        "deduplicated": 0,
        "duplicate_refunds": 0,
        "duplicate_refund_gold": 0,
    }
    for entry in entries:
        data = _load(entry)
        if data.get(UNIQUE_WEAPONS_MIGRATION_FLAG):
            continue

        for user_id, record in data.get("pets", {}).items():
            if not isinstance(record, dict) or REMOVED_MOP_CODE not in record.get("inventory", []):
                continue
            if economy.grant_once(
                entry, user_id, REMOVED_MOP_COMPENSATION, "pet_w003_removal_202608",
            ):
                report["mop_grants"] += 1
            _remove_weapon_from_record(record, REMOVED_MOP_CODE)
            report["removed_mops"] += 1

        claims: dict[str, list[tuple[str, dict]]] = {}
        for user_id, record in data.get("pets", {}).items():
            if not isinstance(record, dict):
                continue
            for code in record.get("inventory", []):
                item = C.find_item(code)
                if item is not None and item.slot == "weapon":
                    claims.setdefault(item.code, []).append((str(user_id), record))

        for code, owners in claims.items():
            if len(owners) <= 1:
                continue
            # Prefer somebody actively using the object, then use a stable id tie-break.
            owners.sort(key=lambda pair: (
                code not in (pair[1].get("equipped") or {}).values(), pair[0],
            ))
            item = C.find_item(code)
            refund = (
                C.PRE_REBALANCE_WEAPON_BUY_PRICES.get(code, item.price)
                if item.source == "shop"
                else C.resale_value(item)
            )
            for user_id, record in owners[1:]:
                reason = f"pet_weapon_duplicate_202608:{code}"
                if refund > 0 and economy.grant_once(entry, user_id, refund, reason):
                    report["duplicate_refunds"] += 1
                    report["duplicate_refund_gold"] += refund
                _remove_weapon_from_record(record, code)
                report["deduplicated"] += 1

        data[UNIQUE_WEAPONS_MIGRATION_FLAG] = True
        _save(entry, data)
    return report


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


def upgrade_hamsterator(entry, user_id, xp, now: datetime | None = None) -> tuple[bool, str]:
    """Upgrade passive income, first banking elapsed time at the old level's rate."""
    data = _load(entry)
    record = data["pets"].get(str(user_id))
    if not record:
        return False, "Сначала купи клетку."
    level = min(max(0, int(record.get("hamsterator_level", 0))), C.HAMSTERATOR_MAX_LEVEL)
    if level >= C.HAMSTERATOR_MAX_LEVEL:
        return False, f"Хомяколатор уже максимального уровня ({C.HAMSTERATOR_MAX_LEVEL})."
    settle_passive_income(entry, user_id, now=now)
    # Settling may have written telemetry, so never overwrite that fresh store with the
    # pre-settlement snapshot kept above.
    data = _load(entry)
    record = data["pets"][str(user_id)]
    cost = C.HAMSTERATOR_UPGRADE_COSTS[level]
    ok, balance = economy.spend(entry, user_id, xp, cost, "buy:pet_hamsterator")
    if not ok:
        return False, f"Нужно {cost} монет на Хомяколатор, у тебя {balance}."
    record["hamsterator_level"] = level + 1
    _save(entry, data)
    rate, _ = _hamsterator_terms(level + 1)
    return True, f"Хомяколатор прокачан до {level + 1} уровня: +{rate} монет/ч."


def tame(
    entry, user_id, xp, name, photo_file_id, owner_name, owner_username: str | None = None,
) -> tuple[bool, str]:
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
    record["owner_username"] = (owner_username or "").lstrip("@") or None
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


def equipped_combat_effects(entry, user_id) -> tuple[dict, ...]:
    """Immutable item-effect snapshots for the pure combat engine.

    Effects never participate in normal stat arithmetic or the power-rating shortcut;
    combat receives the catalogue metadata explicitly with its fighter snapshot instead.
    Returning copies prevents a caller from mutating the global item catalogue.
    """
    record = _tamed_record(_load(entry), user_id)
    if record is None:
        return ()
    effects = []
    for code in (record.get("equipped") or {}).values():
        item = C.find_item(code) if code else None
        effect = getattr(item, "effect", None) if item else None
        if isinstance(effect, dict) and effect.get("code"):
            effects.append(dict(effect))
    return tuple(effects)


def _equipped_effect(record: dict, effect_code: str) -> dict | None:
    """Read one equipped passive without reloading the chat during fight settlement."""
    for code in (record.get("equipped") or {}).values():
        item = C.find_item(code) if code else None
        effect = getattr(item, "effect", None) if item else None
        if isinstance(effect, dict) and effect.get("code") == effect_code:
            return effect
    return None


def _effect_fraction(effect: dict | None) -> float:
    if not effect:
        return 0.0
    try:
        return max(0.0, float(effect.get("value", 0))) / 100
    except (TypeError, ValueError):
        return 0.0


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


def pet_leaderboard(entry: str) -> list[dict]:
    """All tamed creatures in a chat, strongest first.

    The returned records are copies so callers can render them without mutating persisted
    game state. A stable user-id tie-breaker keeps the order predictable for equal power.
    """
    data = _load(entry)
    rows = [
        {
            "user_id": user_id,
            "name": record["name"],
            "owner_name": record.get("owner_name") or "кто-то",
            "owner_username": record.get("owner_username"),
            "power": _power_rating_for(record),
        }
        for user_id, record in data["pets"].items()
        if record.get("name")
    ]
    return sorted(rows, key=lambda row: (-row["power"], row["user_id"]))


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
    if item.slot == "weapon":
        owners = _weapon_owner_ids(data, item.code)
        if str(user_id) in owners:
            return False, f"«{item.name}» у тебя уже есть."
        if owners:
            return False, f"«{item.name}» уже принадлежит другому игроку."
    if item.slot == "weapon" and item.code not in {
        offered.code for offered in _daily_storefront_weapons(data, entry, today())
    }:
        return False, "Этого оружия сегодня нет на витрине. Загляни завтра."
    if item.code in record["inventory"]:
        return False, f"«{item.name}» у тебя уже есть."
    ok, balance = economy.spend(entry, user_id, xp, item.price, f"buy:pet_item:{item.code}")
    if not ok:
        return False, f"Нужно {item.price} монет, у тебя {balance}."
    record["inventory"].append(item.code)
    _discover(record, item.code)
    _save(entry, data)
    return True, f"Куплено: «{item.name}» за {item.price} монет."


def _discover(record: dict, code: str) -> None:
    """Record a catalogue item once in the permanent collection book."""
    if C.find_item(code) is None:
        return
    discovered = record.setdefault("discovered", [])
    if code not in discovered:
        discovered.append(code)


def is_item_locked(record: dict, code: str) -> bool:
    return code in (record.get("locked_items") or [])


def toggle_item_lock(entry, user_id, code) -> tuple[bool, str, bool]:
    """Lock/unlock an owned item; locked gear cannot be sold or gifted."""
    data = _load(entry)
    record = _tamed_record(data, user_id)
    item = C.find_item(code)
    if record is None:
        return False, "Сначала приручи существо.", False
    if item is None or item.code not in record.get("inventory", []):
        return False, "Этого предмета нет в сумке.", False
    locked = record.setdefault("locked_items", [])
    if item.code in locked:
        locked.remove(item.code)
        value = False
        note = f"Снята защита: «{item.name}»."
    else:
        locked.append(item.code)
        value = True
        note = f"Предмет защищён: «{item.name}»."
    _save(entry, data)
    return True, note, value


def begin_item_confirmation(entry, user_id, action: str, code: str) -> tuple[bool, str, str]:
    """Create a one-time server-side token for a rare sale or gift."""
    if action not in {"sell", "gift"}:
        return False, "Неизвестное действие.", ""
    data = _load(entry)
    record = _tamed_record(data, user_id)
    item = C.find_item(code)
    if record is None or item is None or item.code not in record.get("inventory", []):
        return False, "Этого предмета уже нет в сумке.", ""
    if item.rarity not in {"rare", "legendary"}:
        return False, "Подтверждение этому предмету не нужно.", ""
    token = secrets.token_urlsafe(5).replace("-", "a").replace("_", "b")
    record.setdefault("pending_item_actions", {})[f"{action}:{item.code}"] = token
    _save(entry, data)
    return True, "", token


def _consume_item_confirmation(record: dict, action: str, code: str, token: str | None) -> bool:
    """Consume, rather than merely check, a confirmation to prevent replay."""
    pending = record.setdefault("pending_item_actions", {})
    key = f"{action}:{code}"
    if not token or pending.get(key) != token:
        return False
    pending.pop(key, None)
    return True


def sell_item(entry, user_id, code, confirmation_token: str | None = None) -> tuple[bool, str, int]:
    """Sell one unequipped item for its deliberately modest stated resale value."""
    data = _load(entry)
    record = _tamed_record(data, user_id)
    item = C.find_item(code)
    if record is None:
        return False, "Сначала приручи существо.", 0
    if item is None or item.code not in record.get("inventory", []):
        return False, "Этого предмета нет в сумке.", 0
    if item.code in (record.get("equipped") or {}).values():
        return False, "Сначала сними этот предмет.", 0
    if is_item_locked(record, item.code):
        return False, "Предмет защищён. Сначала сними 🔒 в инвентаре.", 0
    if item.rarity in {"rare", "legendary"} and not _consume_item_confirmation(
        record, "sell", item.code, confirmation_token
    ):
        return False, "Редкий предмет нужно подтвердить отдельной кнопкой.", 0
    value = C.resale_value(item)
    record["inventory"].remove(item.code)
    _metric_add(data, "item_sale_gold", value)
    _save(entry, data)
    economy.grant(entry, user_id, value, f"sell:pet_item:{item.code}")
    return True, f"Продано: «{item.name}» за {value} монет.", value


def _gift_cooldown_message(seconds: float) -> str:
    remaining = max(1, int(seconds))
    hours, remainder = divmod(remaining, 3600)
    minutes = (remainder + 59) // 60
    if hours:
        return f"Следующий подарок можно отправить через {hours} ч. {minutes} мин."
    return f"Следующий подарок можно отправить через {minutes} мин."


def gift_item(
    entry, giver_id, receiver_id, code, confirmation_token: str | None = None,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Atomically transfer an unequipped unique item between tamed pets."""
    data = _load(entry)
    giver = _tamed_record(data, giver_id)
    receiver = _tamed_record(data, receiver_id)
    item = C.find_item(code)
    if str(giver_id) == str(receiver_id):
        return False, "Себе подарить нельзя."
    if giver is None:
        return False, "У тебя нет приручённого существа."
    if receiver is None:
        return False, "У получателя нет приручённого существа."
    if giver.get("level", 1) < C.GIFT_MIN_PET_LEVEL:
        return False, f"Дарить можно с {C.GIFT_MIN_PET_LEVEL} уровня питомца."
    if item is None or item.code not in giver.get("inventory", []):
        return False, "Этого предмета нет в сумке."
    if item.code in (giver.get("equipped") or {}).values():
        return False, "Сначала сними этот предмет."
    if is_item_locked(giver, item.code):
        return False, "Предмет защищён. Сначала сними 🔒 в инвентаре."
    # Apply cooldown before consuming a confirmation token. A rejected attempt must not
    # make the player confirm the same rare item again.
    moment = now or app_now()
    last_gift = giver.get("gift_last_at")
    if last_gift:
        try:
            elapsed = (moment - datetime.fromisoformat(last_gift)).total_seconds()
        except (TypeError, ValueError):
            elapsed = C.GIFT_COOLDOWN_SECONDS
        if elapsed < C.GIFT_COOLDOWN_SECONDS:
            return False, _gift_cooldown_message(C.GIFT_COOLDOWN_SECONDS - elapsed)
    if item.rarity in {"rare", "legendary"} and not _consume_item_confirmation(
        giver, "gift", item.code, confirmation_token
    ):
        return False, "Редкий предмет нужно подтвердить отдельной кнопкой."
    if item.code in receiver.get("inventory", []):
        return False, "У получателя уже есть такой предмет."
    giver["inventory"].remove(item.code)
    if item.code in giver.get("locked_items", []):
        giver["locked_items"].remove(item.code)
    receiver.setdefault("inventory", []).append(item.code)
    _discover(receiver, item.code)
    giver["gift_last_at"] = moment.isoformat()
    audit = data.setdefault("gift_history", [])
    audit.append({
        "ts": moment.isoformat(), "giver_id": str(giver_id),
        "receiver_id": str(receiver_id), "item_code": item.code,
    })
    if len(audit) > C.GIFT_AUDIT_LIMIT:
        del audit[:-C.GIFT_AUDIT_LIMIT]
    _metric_add(data, "gifts")
    _save(entry, data)
    return True, f"Подарено: «{item.name}»."


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
    return fight_allowance_breakdown(entry, user_id, today)["allowance"]


def fight_allowance_breakdown(entry, user_id, today) -> dict:
    """Public, display-ready components of today's fixed arena-fight allowance.

    The paint component is derived from stats rather than saved on the pet.  That makes
    a qualifying post grant its bonus immediately, lets it expire with the rolling
    seven-day window, and ensures a deleted post disappears from the allowance too.
    """
    data = _load(entry)
    record = _tamed_record(data, user_id)
    if record is None:
        return {
            "allowance": 0, "base": 0, "cage_bonus": 0, "farm_bonus": 0,
            "paint_bonus": 0, "recent_figurines": 0,
        }
    cage = min(max(int(record.get("cage_level", 1) or 1), 1), C.CAGE_MAX_LEVEL)
    farm = max(0, int(record.get("farm_level", 0) or 0))
    recent_figurines = stats.recent_figurine_fight_bonus_count(
        entry, user_id, today, C.RECENT_FIGURINE_FIGHT_BUFF_DAYS,
    )
    cage_bonus = C.CAGE_BONUS_FIGHTS[cage - 1]
    farm_bonus = farm // C.FARM_LEVELS_PER_FIGHT
    paint_bonus = recent_figurines * C.FIGHTS_PER_RECENT_FIGURINE
    return {
        "allowance": C.daily_fight_allowance(cage, farm, recent_figurines),
        "base": C.BASE_DAILY_FIGHTS,
        "cage_bonus": cage_bonus,
        "farm_bonus": farm_bonus,
        "paint_bonus": paint_bonus,
        "recent_figurines": recent_figurines,
    }


def fights_left(entry, user_id, today) -> int:
    data = _load(entry)
    record = _tamed_record(data, user_id)
    if record is None:
        return 0
    _reset_if_new_day(record, today)  # in-memory only; nothing to persist on a pure read
    allowance = fight_allowance_breakdown(entry, user_id, today)["allowance"]
    return max(0, allowance - record.get("fights_today", 0))


def claim_duel(entry, user_id, opponent_id, now=None) -> tuple[bool, str]:
    """Atomically reserve one public duel, including its once-per-target daily limit."""
    now = now or app_now()
    data = _load(entry)
    uid, opponent_uid = str(user_id), str(opponent_id)
    if _is_farming_record(_tamed_record(data, uid), now):
        return False, "Питомец сейчас работает на ферме и не может драться."
    if _is_farming_record(_tamed_record(data, opponent_uid), now):
        return False, "Этот питомец сейчас работает на ферме."
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
    data = _load(entry)
    if _is_farming_record(_tamed_record(data, attacker_id)):
        return False
    if _is_farming_record(_tamed_record(data, defender_id)):
        return False
    return (
        sum(
            1
            for fight in data.get("fights", [])
            if fight.get("date") == day.isoformat()
            and fight.get("attacker_id") == str(attacker_id)
            and fight.get("defender_id") == str(defender_id)
        )
        < C.ARENA_SAME_OPPONENT_DAILY_LIMIT
    )


def find_opponent(
    entry, user_id, rng=None, exclude_ids=None, attackable_only: bool = False,
) -> str | None:
    """Choose one attackable opponent uniformly at random.

    ``exclude_ids`` is a soft exclusion: it prevents the card currently on screen from
    being dealt again when another eligible opponent exists, but a one-person arena can
    still show its only opponent.  ``attackable_only`` is used by the arena UI so a
    search never displays somebody the player has already reached the daily limit
    against.
    """
    # The normal arena draw is fresh OS-backed randomness.  Tests and simulations can
    # still inject a seeded RNG to make a particular draw reproducible.
    rng = rng or secrets.SystemRandom()
    data = _load(entry)
    seeker = _tamed_record(data, user_id)
    if seeker is None:
        return None
    uid = str(user_id)
    excluded = {str(other_id) for other_id in (exclude_ids or ())}
    candidates = [
        other_id for other_id, record in data["pets"].items()
        if other_id != uid
        and record.get("name")
        and not _is_farming_record(record)
        and (not attackable_only or can_attack_in_arena(entry, uid, other_id))
    ]
    if not candidates:
        return None

    # A reroll should deal a different card whenever there is one.  The arena deliberately
    # has no power or level window: a choice is made uniformly from every eligible pet.
    alternatives = [other_id for other_id in candidates if other_id not in excluded]
    if alternatives:
        candidates = alternatives

    return rng.choice(candidates)


def record_guardian_intervention(
    entry, attacker_id, defender_id, today,
) -> dict:
    """Record the level-gap safety rule without running combat or touching either wallet.

    The attacker did choose and spend a fight, while the defender did not fight at all.
    Keeping a normal-looking history row (with an explicit marker) also makes the arena
    per-target limit and audit trail agree with what the player saw.
    """
    data = _load(entry)
    attacker_uid, defender_uid = str(attacker_id), str(defender_id)
    attacker = data["pets"][attacker_uid]
    defender = data["pets"][defender_uid]
    if _is_farming_record(attacker) or _is_farming_record(defender):
        raise ValueError("Питомец на ферме и не может участвовать в бою.")
    _reset_if_new_day(attacker, today)
    attacker["fights_today"] = attacker.get("fights_today", 0) + 1
    attacker["fights"] = attacker.get("fights", 0) + 1
    _, levels_gained = _apply_xp(attacker, C.GUARDIAN_XP)
    _metric_add(data, "guardian_interventions")
    data["fights"].append({
        "ts": app_now().isoformat(),
        "date": today.isoformat(),
        "attacker_id": attacker_uid,
        "defender_id": defender_uid,
        "winner_id": None,
        "loser_id": None,
        "draw": False,
        "guardian_intervention": True,
        "attacker_name": attacker.get("name"),
        "defender_name": defender.get("name"),
        "attacker_owner": attacker.get("owner_name"),
        "defender_owner": defender.get("owner_name"),
        "gold": 0,
        "loss_gold": 0,
        "dropped_item": None,
        "xp": C.GUARDIAN_XP,
        "combat_seed": None,
        "total_damage": {},
        "combat_snapshot": None,
    })
    _save(entry, data)
    return {
        "guardian_intervention": True,
        "draw": False,
        "gold": 0,
        "loss_gold": 0,
        "xp": C.GUARDIAN_XP,
        "levels_gained": levels_gained,
        "level": attacker.get("level", 1),
        "dropped_item": None,
        "opponent_gold": 0,
        "opponent_loss_gold": 0,
        "opponent_xp": 0,
        "opponent_levels_gained": 0,
        "opponent_level": defender.get("level", 1),
        "opponent_dropped_item": None,
    }


def legendary_pity_progress(entry: str, user_id) -> dict:
    """Progress toward the next guaranteed legendary drop for this pet.

    Only wins while an unowned drop-only legendary still exists are eligible.  Once all
    five are held, the counter is reset rather than promising an impossible duplicate.
    """
    data = _load(entry)
    record = _tamed_record(data, user_id)
    threshold = C.LEGENDARY_PITY_ELIGIBLE_WINS
    if record is None:
        return {
            "eligible": False, "wins_without_legend": 0,
            "remaining_wins": threshold, "threshold": threshold,
        }
    owned = _owned_weapon_codes(data)
    eligible = any(
        item.slot == "weapon" and item.source == "drop"
        and item.rarity == "legendary" and item.code not in owned
        for item in C.ITEMS
    )
    wins = max(0, int(record.get("legendary_pity_wins", 0) or 0)) if eligible else 0
    return {
        "eligible": eligible,
        "wins_without_legend": wins,
        "remaining_wins": max(0, threshold - wins),
        "threshold": threshold,
    }


def record_fight(
    entry, attacker_id, defender_id, result, today, attacker_xp=None, combat_snapshot=None,
) -> dict:
    data = _load(entry)
    attacker_uid, defender_uid = str(attacker_id), str(defender_id)
    attacker = data["pets"][attacker_uid]
    defender = data["pets"][defender_uid]
    if _is_farming_record(attacker) or _is_farming_record(defender):
        raise ValueError("Питомец на ферме и не может участвовать в бою.")

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
            "opponent_level": defender.get("level", 1),
        }

    winner_uid = str(result.winner)
    loser_uid = str(result.loser)
    winner = data["pets"][winner_uid]
    loser = data["pets"][loser_uid]
    winner["wins"] = winner.get("wins", 0) + 1

    winner_cage_level = winner.get("cage_level", 1)
    bonus_pct = C.CAGE_GOLD_BONUS_PCT[winner_cage_level - 1]
    reward_multiplier = C.arena_level_reward_multiplier(
        winner.get("level", 1), loser.get("level", 1)
    )
    gold = round(
        random.randint(C.WIN_GOLD_MIN, C.WIN_GOLD_MAX)
        * (1 + bonus_pct / 100)
        * reward_multiplier
    )
    economy.grant(entry, winner_uid, gold, "pet_fight_win")
    _metric_add(data, "arena_reward_gold", gold)

    # The loser pays half of that. Charged as a spend rather than a negative grant so it
    # lands in the same ledger column as every other purchase -- and clamped to what they
    # actually hold, because economy.balance floors at zero and a debt nobody can see the
    # cause of is worse than a bill that got rounded down. `loser_xp` is the loser's live
    # chat XP, which the caller cannot supply for a defender who is not the one playing,
    # so it is read from the same aggregate economy.balance itself uses.
    penalty = C.loss_gold_for(gold)
    # «Последний чек» keeps part of the loser's coins. It changes only the amount paid;
    # the winner's already-calculated reward is never reduced by somebody else's gear.
    survivor_share = _effect_fraction(_equipped_effect(loser, "survivor"))
    if survivor_share:
        penalty = max(0, round(penalty * (1 - min(1.0, survivor_share))))
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
    # An item code represents one unique object. A full duplicate-proof pool also
    # means a lucky winner can still receive a different drop. The pity counter is
    # deliberately tied to wins, not merely to successful 8% drop rolls: the normal
    # rate is about 0.088% for a legendary, so a 500-win ceiling removes a frustrating
    # extreme tail while leaving almost every drop to the ordinary weighted table.
    # Weapons are unique chat-wide; accessories are unique inside the winner's own bag.
    # Excluding both sets prevents a successful 8% roll from reporting a duplicate that
    # `_load` would silently collapse on the next read.
    owned_codes = _owned_weapon_codes(data) | set(winner.get("inventory", []))
    drop_pool = [
        item for item in C.ITEMS
        if item.source == "drop" and item.code not in owned_codes
    ]
    # The 500-win pity contract is specifically for the five legendary weapons. New
    # legendary amulets/boots/gloves remain exciting normal rolls, not substitutes for
    # the promised weapon.
    legendary_pool = [
        item for item in drop_pool if item.slot == "weapon" and item.rarity == "legendary"
    ]
    pity_before = max(0, int(winner.get("legendary_pity_wins", 0) or 0))
    force_legendary = bool(legendary_pool) and (
        pity_before + 1 >= C.LEGENDARY_PITY_ELIGIBLE_WINS
    )
    dropped = None
    auto_equipped = False
    collector_bonus = _effect_fraction(_equipped_effect(winner, "collector"))
    drop_chance = min(1.0, C.DROP_CHANCE * (1 + collector_bonus))
    if force_legendary:
        dropped = random.choice(legendary_pool)
    elif drop_pool and random.random() < drop_chance:
        # Keep the selection inspectable/testable with random.choice while still
        # honoring rarity weights. The catalogue's small integer weights keep this
        # expanded pool tiny (and only build it on an actual drop).
        weighted_pool = [
            item for item in drop_pool
            for _ in range(max(0, getattr(item, "drop_weight", 1)))
        ]
        dropped = random.choice(weighted_pool)
    if dropped is not None:
        winner.setdefault("inventory", []).append(dropped.code)
        _discover(winner, dropped.code)
        dropped_code = dropped.code
        equipped = winner.setdefault("equipped", {})
        current = C.find_item(equipped.get(dropped.slot))
        if current is None or C.equipment_score(dropped) > C.equipment_score(current):
            equipped[dropped.slot] = dropped.code
            auto_equipped = True
        _metric_add(data, "drops_by_rarity", rarity=dropped.rarity)
    if not legendary_pool:
        # Do not carry an unreachable promise after the last legendary is collected.
        winner["legendary_pity_wins"] = 0
    else:
        winner["legendary_pity_wins"] = (
            0 if dropped is not None and dropped.rarity == "legendary" else pity_before + 1
        )

    winner_xp = max(1, round(C.WIN_XP * reward_multiplier))
    _, winner_levels_gained = _apply_xp(winner, winner_xp)
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
        "auto_equipped": auto_equipped,
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
        "xp": winner_xp if attacker_won else C.LOSS_XP,
        "levels_gained": winner_levels_gained if attacker_won else loser_levels_gained,
        "level": attacker.get("level", 1),
        "dropped_item": dropped_code if attacker_won else None,
        "auto_equipped": auto_equipped if attacker_won else False,
        "opponent_gold": gold if not attacker_won else 0,
        "opponent_loss_gold": paid if attacker_won else 0,
        "opponent_xp": C.LOSS_XP if attacker_won else winner_xp,
        "opponent_levels_gained": loser_levels_gained if attacker_won else winner_levels_gained,
        "opponent_level": defender.get("level", 1),
        "opponent_dropped_item": dropped_code if not attacker_won else None,
        "opponent_auto_equipped": auto_equipped if not attacker_won else False,
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
