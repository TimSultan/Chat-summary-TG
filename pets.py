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
import pets_combat
import pets_config as C
import pets_mobs as M
import pets_scroll_catalog as SCROLLS
import pets_sprite as SPRITE
import stats
from app_time import now as app_now

import pets_dungeon as D
PETS_STORE_VERSION = 7
# Rolling fight log, capped independently of C.HISTORY_LIMIT (that constant bounds what
# ONE player is shown per /history call, not how many chat-wide entries are kept on disk)
# -- mirrors economy.py's LOG_LIMIT convention for the same reason: trimming this can
# never change anybody's stats, wins or gold, all of which live on the per-pet record.
FIGHT_LOG_LIMIT = 2_000
# Store-level marker: this chat's one-off cage-upgrade refund has already been paid out.
# See refund_cage_upgrades for why the per-user lock alone would keep paying forever.
CAGE_UPGRADE_REFUND_FLAG = "cage_upgrade_refund_202608"
# Store-level marker: this chat's scroll collections have already been emptied, so
# everybody earns all forty through drops. A new dated name rather than a cleared old
# one, deliberately: the first wipe left a starter set behind, and a chat that already
# ran it still has those four to lose -- see reset_scroll_collections.
SCROLL_RESET_FLAG = "scroll_wipe_all_202608"
HAMSTERATOR_RETIREMENT_REASON = "pet_hamsterator_retirement_202608"
# Same two-lock shape as the cage refund above, for the farm's 75 -> 10 build price.
FARM_BUILD_REFUND_FLAG = "farm_build_refund_202608"
# One free common weapon for anybody who never got one. Also two-locked: the per-chat flag
# is what stops a player who sells their only weapon from being handed another on the next
# restart, forever.
STARTER_WEAPON_GIFT_FLAG = "starter_weapon_gift_202608"
# Зеркало души. Named here rather than looked up by effect code because two call sites
# need the ITEM (equip it, check it is owned) and only combat needs the effect.
MIRROR_AMULET_CODE = "amulet_soul_mirror"
# PVE counterparts to the mirror: when owned, they are temporarily worn for a mob fight
# and the player's normal loadout is restored immediately afterwards.
MOB_GEAR_CODES = {"weapon": "w009", "amulet": "amulet_mob_ward"}

_NAME_MAX_LEN = 24
# How many recent grant keys a ticket wallet remembers, purely to swallow a replayed
# listener update (see grant_farm_ticket). Seconds matter here, not weeks.
FARM_TICKET_GRANT_MEMORY = 50
# Scrolls are earned abilities.  A few basic ones are deliberately available from the
# moment a creature is tamed; everything else is a rare permanent unlock rather than a
# consumable item or a shop purchase.
SCROLL_REWARD_MEMORY = 1_000
PAINT_SCROLL_CHANCE = 0.025
PAINT_SCROLL_PITY = 20
HARD_QUEST_SCROLL_CHANCES = {4: 0.12, 5: 0.20}
HARD_QUEST_SCROLL_PITY = 6
ULTIMATE_SCROLL_SHARE = 0.12
# The poller and a button press can settle the same finished run in one process.  The
# run id also keys the economy grant, so a process restart cannot mint a second payout.
# It also guards the ticket wallet and the shift a ticket shortens, both of which are
# read-modify-writes against a run this same lock is settling.
_farm_settlement_lock = threading.RLock()


# --- storage -----------------------------------------------------------------------


def _pets_path(entry: str):
    return stats._stats_dir() / f"{stats._cache_key(entry)}_pets.json"


def _empty() -> dict:
    return {
        "version": PETS_STORE_VERSION, "pets": {}, "fights": [], "duels": {},
        "gift_history": [], "farm_tickets": {}, "rubies": {},
        "scroll_wallets": {}, "scroll_notifications": [],
        "storefront_sales": {},
        "economy_metrics": _new_economy_metrics(),
    }


def _new_economy_metrics() -> dict:
    """Aggregate-only game-economy observability; never a second balance ledger."""
    return {
        "passive_gold_minted": 0,
        "farm_gold_minted": 0,
        "farm_runs": 0,
        "item_sale_gold": 0,
        "gifts": 0,
        "rubies_minted": 0,
        "pve_gold_minted": 0,
        "pve_fights": 0,
        "arena_reward_gold": 0,
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
    # A store written before tickets existed simply has none; nothing to migrate.
    if not isinstance(data.setdefault("farm_tickets", {}), dict):
        data["farm_tickets"] = {}
    if not isinstance(data.setdefault("rubies", {}), dict):
        data["rubies"] = {}
    if not isinstance(data.setdefault("scroll_wallets", {}), dict):
        data["scroll_wallets"] = {}
    notices = data.setdefault("scroll_notifications", [])
    data["scroll_notifications"] = [row for row in notices if isinstance(row, dict)][-400:] \
        if isinstance(notices, list) else []
    sales = data.setdefault("storefront_sales", {})
    if not isinstance(sales, dict):
        data["storefront_sales"] = {}
    else:
        try:
            window = int(sales.get("window"))
        except (TypeError, ValueError):
            window = None
        codes = sales.get("codes")
        data["storefront_sales"] = (
            {"window": window, "codes": list(dict.fromkeys(
                code for code in codes if isinstance(code, str)
            ))}
            if window is not None and isinstance(codes, list) else {}
        )
    _economy_metrics(data)
    legal_scrolls = {row["code"] for row in SCROLLS.SCROLLS}
    # Older saves used an append-only list.  Accept their duplicates while reading,
    # then expose a canonical unique inventory to every game operation.
    for record in data["pets"].values():
        if not isinstance(record, dict):
            continue
        purchased_stats = record.get("stats")
        if not isinstance(purchased_stats, dict):
            purchased_stats = {}
            record["stats"] = purchased_stats
        for key in C.STAT_KEYS:
            purchased_stats.setdefault(key, C.STAT_MIN_LEVEL)
        try:
            record["stat_points"] = max(0, int(record.get("stat_points", 0) or 0))
        except (TypeError, ValueError):
            record["stat_points"] = 0
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
        record["fight_result_notifications"] = bool(
            record.get("fight_result_notifications", True)
        )
        dungeon = record.get("dungeon_run")
        record["dungeon_run"] = dungeon if isinstance(dungeon, dict) else None
        record["dungeon_deepest"] = max(
            1, _safe_nonnegative_int(record.get("dungeon_deepest"), 1),
        )
        # Scrolls are abilities, not inventory objects, and the four slots holding them
        # may each be empty. A malformed hand-edited loadout falls back to four empty
        # slots atomically, rather than entering combat half-valid.
        try:
            record["skill_slots"] = list(SCROLLS.validate_loadout(record.get("skill_slots")))
        except ValueError:
            record["skill_slots"] = list(SCROLLS.EMPTY_LOADOUT)
        # Equipped codes are folded into the owned list as a repair for saves written
        # before ownership was tracked per creature. Nothing else is added: there is no
        # starter set, so a creature owns exactly what it has earned.
        owned_scrolls = record.get("owned_scrolls")
        if not isinstance(owned_scrolls, list):
            owned_scrolls = []
        record["owned_scrolls"] = list(dict.fromkeys(
            code for code in [*SCROLLS.equipped_codes(record["skill_slots"]), *owned_scrolls]
            if isinstance(code, str) and code in legal_scrolls
        ))
    # A player may paint and earn a scroll before taming a creature.  This top-level
    # wallet is deliberately shaped like farm tickets: it survives that gap, then gets
    # merged into the pet's owned list the first time the creature is read.
    for user_id, wallet in list(data["scroll_wallets"].items()):
        if not isinstance(wallet, dict):
            wallet = data["scroll_wallets"][user_id] = {}
        unlocked = wallet.get("unlocked")
        if not isinstance(unlocked, list):
            unlocked = []
        wallet["unlocked"] = list(dict.fromkeys(
            code for code in unlocked if isinstance(code, str) and code in legal_scrolls
        ))
        wallet["reward_log"] = wallet.get("reward_log") if isinstance(wallet.get("reward_log"), dict) else {}
        wallet_pity = wallet.get("pity") if isinstance(wallet.get("pity"), dict) else {}
        wallet["pity"] = {
            "paint": _safe_nonnegative_int(wallet_pity.get("paint")),
            "hard_quest": _safe_nonnegative_int(wallet_pity.get("hard_quest")),
        }
        record = data["pets"].get(str(user_id))
        if isinstance(record, dict):
            record["owned_scrolls"] = list(dict.fromkeys([
                *record.get("owned_scrolls", []), *wallet["unlocked"],
            ]))
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
        "stat_points": 0,
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
        # New pets enter the arena with a full basic bank.  The checkpoint is per pet,
        # rather than a midnight-wide reset, so partial hours survive restarts.
        "fight_bank": C.BASE_FIGHT_BANK_CAPACITY,
        "fight_bank_cap": C.BASE_FIGHT_BANK_CAPACITY,
        "fight_bank_checkpoint": app_now().isoformat(),
        "farm_level": 0,
        "farm_features": {},
        "farm_run": None,
        "farm_notifications": [],
        "fight_result_notifications": True,
        "skill_slots": list(SCROLLS.EMPTY_LOADOUT),
        "owned_scrolls": [],
        "dungeon_run": None,
        "dungeon_deepest": 1,
    }


def _tamed_record(data: dict, user_id) -> dict | None:
    record = data["pets"].get(str(user_id))
    return record if record and record.get("name") else None


def _daily_storefront_items(
    data: dict, entry: str, user_id, slot: str, day: date | datetime | str | None = None,
):
    moment = day or app_now()
    record = _tamed_record(data, user_id) or {}
    return C.daily_storefront_items(
        entry, slot, moment, excluded_codes=set(record.get("inventory", [])), user_id=user_id,
    )


def _daily_storefront_weapons(
    data: dict, entry: str, user_id, day: date | datetime | str | None = None,
):
    return _daily_storefront_items(data, entry, user_id, "weapon", day)


def daily_storefront_items(
    entry: str, slot: str, day: date | datetime | str | None = None, *, user_id=None,
):
    """One player's five ordinary and one rare offers for this slot and 12-hour window."""
    return _daily_storefront_items(_load(entry), entry, user_id, slot, day)


def daily_storefront_weapons(
    entry: str, day: date | datetime | str | None = None, *, user_id=None,
):
    """One player's five ordinary and one rare weapon offers for this 12-hour window."""
    return daily_storefront_items(entry, "weapon", day, user_id=user_id)


def _name_taken(data: dict, name: str, exclude_uid: str | None = None) -> bool:
    needle = name.strip().lower()
    for uid, record in data["pets"].items():
        if uid == exclude_uid:
            continue
        existing = record.get("name")
        if existing and existing.strip().lower() == needle:
            return True
    return False


def _safe_nonnegative_int(value, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _checkpoint_at(value, now: datetime) -> datetime | None:
    """Parse a persisted fight-bank checkpoint without trusting malformed saves."""
    if not isinstance(value, str):
        return None
    try:
        checkpoint = datetime.fromisoformat(value)
    except ValueError:
        return None
    # Historic/manual saves can contain a naive ISO timestamp.  Treat it in the same
    # local timezone as ``now`` rather than raising on an aware/naive subtraction.
    if checkpoint.tzinfo is None and now.tzinfo is not None:
        checkpoint = checkpoint.replace(tzinfo=now.tzinfo)
    elif checkpoint.tzinfo is not None and now.tzinfo is None:
        checkpoint = checkpoint.replace(tzinfo=None)
    return checkpoint


def _legacy_fights_remaining(record: dict, capacity: int, now: datetime) -> int:
    """Safely turn a pre-v4 daily counter into one initial bank balance.

    A prior calendar day had already earned its normal reset, so it starts full under
    the new rules.  For the current day, preserve only unspent legacy fights and clamp
    them to the smaller new capacity.  No old elapsed time is converted into hourly
    credits: migration's checkpoint is always ``now``.
    """
    if record.get("fights_day") != now.date().isoformat():
        return capacity
    cage = min(max(_safe_nonnegative_int(record.get("cage_level", 1), 1), 1), C.CAGE_MAX_LEVEL)
    farm = _safe_nonnegative_int(record.get("farm_level", 0))
    cage_bonus = C.CAGE_BONUS_FIGHTS[cage - 1]
    farm_bonus = farm // C.FARM_LEVELS_PER_FIGHT
    # The new capacity contains one slot per active paint, so its live paint count can
    # be recovered after subtracting the base/cage/farm terms. The retired daily system
    # granted two attempts per paint; include those when preserving today's remainder.
    recent_paints = max(0, capacity - C.BASE_FIGHT_BANK_CAPACITY - cage_bonus - farm_bonus)
    legacy_allowance = 10 + cage_bonus + farm_bonus + recent_paints * 2
    remaining = max(0, legacy_allowance - _safe_nonnegative_int(record.get("fights_today")))
    return min(capacity, remaining)


def _settle_fight_bank(record: dict, capacity: int, now: datetime) -> tuple[int, datetime, bool]:
    """Settle whole elapsed recharge hours in-place and return bank/checkpoint/changed.

    The previous stored cap is deliberately used while settling.  Therefore buying an
    upgrade never turns time before that purchase into extra fights.  Conversely, once a
    bank reaches either cap, overflow and the old fractional remainder are discarded;
    spending later cannot instantly refill from an old timestamp.
    """
    capacity = max(0, int(capacity))
    changed = False
    if "fight_bank" not in record:
        bank = _legacy_fights_remaining(record, capacity, now)
        checkpoint = now
        record["fight_bank"] = bank
        record["fight_bank_checkpoint"] = checkpoint.isoformat()
        record["fight_bank_cap"] = capacity
        record.pop("fights_today", None)
        record.pop("fights_day", None)
        return bank, checkpoint, True

    raw_bank = record.get("fight_bank")
    raw_cap = record.get("fight_bank_cap")
    try:
        bank = int(raw_bank)
        old_cap = int(raw_cap)
        numeric_state_valid = bank >= 0 and old_cap > 0
    except (TypeError, ValueError):
        bank, old_cap, numeric_state_valid = 0, capacity, False
    if not numeric_state_valid:
        # A damaged bank must not turn an ancient otherwise-valid checkpoint into a
        # windfall. Repair conservatively and start its recharge clock now.
        bank = min(max(0, bank), capacity)
        old_cap = capacity
        checkpoint = now
        changed = True
    else:
        checkpoint = _checkpoint_at(record.get("fight_bank_checkpoint"), now)
    # A missing/malformed cap is trusted no more than today's current capacity.  This
    # avoids a corrupt value becoming an unbounded historical-recharge multiplier.
    if old_cap <= 0:
        old_cap = capacity
    bank = min(bank, old_cap)
    if checkpoint is None or checkpoint > now:
        checkpoint = now
        changed = True
    else:
        elapsed = max(0.0, (now - checkpoint).total_seconds())
        completed = int(elapsed // C.FIGHT_BANK_RECHARGE_SECONDS)
        if bank >= old_cap:
            # The bank was already full: elapsed time cannot be stored as credit.
            checkpoint = now
            changed = True
        elif completed:
            bank = min(old_cap, bank + completed)
            if bank >= old_cap:
                checkpoint = now
            else:
                checkpoint += timedelta(seconds=completed * C.FIGHT_BANK_RECHARGE_SECONDS)
            changed = True

    # Now apply capacity changes.  Increasing capacity creates room only; it never
    # creates a fight.  Losing a temporary/upgrade bonus trims an overfull bank.
    if bank > capacity:
        bank = capacity
        checkpoint = now
        changed = True
    if record.get("fight_bank") != bank:
        record["fight_bank"] = bank
        changed = True
    if record.get("fight_bank_cap") != capacity:
        record["fight_bank_cap"] = capacity
        changed = True
    checkpoint_text = checkpoint.isoformat()
    if record.get("fight_bank_checkpoint") != checkpoint_text:
        record["fight_bank_checkpoint"] = checkpoint_text
        changed = True
    return bank, checkpoint, changed


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
    """Application-local calendar date, retained for history and duel limits."""
    return app_now().date()


def fight_refresh_seconds(now: datetime | None = None) -> int:
    """Whole seconds until the next wall-clock hour (legacy display helper).

    A pet's exact recharge can differ by its persisted fractional checkpoint; callers
    rendering an arena card should use ``fight_allowance_breakdown()['seconds_until_next']``.
    """
    moment = now or app_now()
    next_hour = moment.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return max(0, round((next_hour - moment).total_seconds()))


def get_pet(entry, user_id) -> dict | None:
    return _tamed_record(_load(entry), user_id)


def fight_result_notifications_enabled(entry, user_id) -> bool:
    """Whether this player wants private arena fight receipts."""
    pet = get_pet(entry, user_id)
    return bool(pet and pet.get("fight_result_notifications", True))


def toggle_fight_result_notifications(entry, user_id) -> bool:
    """Flip private arena fight receipts and return the new enabled state."""
    data = _load(entry)
    record = _tamed_record(data, user_id)
    if record is None:
        return False
    record["fight_result_notifications"] = not bool(
        record.get("fight_result_notifications", True)
    )
    _save(entry, data)
    return record["fight_result_notifications"]


def has_cage(entry, user_id) -> bool:
    return str(user_id) in _load(entry)["pets"]


def cage_level(entry, user_id) -> int:
    """0 when there is no cage at all, else the real 1..C.CAGE_MAX_LEVEL -- distinct from
    has_cage() so the UI can render a level number without a second bool check."""
    record = _load(entry)["pets"].get(str(user_id))
    return record.get("cage_level", 0) if record else 0


def _farm_passive_terms(record: dict | None) -> tuple[int, int, int]:
    level = min(
        max(0, int((record or {}).get("farm_level", 0) or 0)), C.FARM_MAX_LEVEL,
    )
    return (
        level,
        C.FARM_PASSIVE_GOLD_PER_HOUR[level],
        C.FARM_PASSIVE_STORAGE_CAP[level],
    )


def settle_passive_income(entry, user_id, now: datetime | None = None) -> dict:
    level, rate, cap = _farm_passive_terms(_tamed_record(_load(entry), user_id))
    result = economy.settle_passive_income(entry, user_id, rate, cap, now=now)
    # The underlying ledger advances its checkpoint atomically with the credit. A
    # retry therefore reports zero and cannot inflate this aggregate counter either.
    credited = max(0, int(result.get("credited", 0) or 0))
    if credited:
        data = _load(entry)
        _metric_add(data, "passive_gold_minted", credited)
        _save(entry, data)
    return {**result, "level": level, "rate": rate, "cap": cap}


def passive_income_status(entry, user_id, now: datetime | None = None) -> dict:
    level, rate, cap = _farm_passive_terms(_tamed_record(_load(entry), user_id))
    return {
        **economy.passive_income_status(entry, user_id, rate, cap, now=now),
        "level": level, "rate": rate, "cap": cap,
    }


def balance_for(entry, user_id, xp) -> int:
    """Thin pass-through so a balance read also settles the pet's passive income.

    It is no longer true that pets_ui never touches economy: the daily bonus screen calls
    economy directly, because that faucet deliberately works for members with no pet at
    all and routing it through this module would invent a dependency it does not have.
    What this wrapper still owns is the pet-specific part below.
    """
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


def refund_farm_builds(entries) -> int:
    """Pay back C.FARM_BUILD_REFUND once to everybody who built a farm at the old 75.

    Same two locks and the same reasoning as refund_cage_upgrades: economy.grant_once
    keys per user so a crash mid-chat cannot pay twice, and FARM_BUILD_REFUND_FLAG closes
    the eligibility window at the first run -- otherwise every player who builds a farm
    AFTER this deploy would also collect 65 on the next restart, turning a 10-coin
    building into a 55-coin profit.

    `farm_level >= 1` is the signal rather than a `buy:pet_farm_upgrade` row in the
    economy log, because that log is capped (economy.LOG_LIMIT) and an early build may
    already have been trimmed out of it, while the level on the pet record never
    decreases. At the moment this ships, having a farm at all means having paid 75 for it.
    """
    refunded = 0
    for entry in entries:
        data = _load(entry)
        if data.get(FARM_BUILD_REFUND_FLAG):
            continue
        for user_id, record in data.get("pets", {}).items():
            if not isinstance(record, dict):
                continue
            if int(record.get("farm_level", 0) or 0) < 1:
                continue
            if economy.grant_once(
                entry, user_id, C.FARM_BUILD_REFUND, "pet_farm_build_202608",
            ):
                refunded += 1
        data[FARM_BUILD_REFUND_FLAG] = True
        _save(entry, data)
    return refunded


def grant_starter_weapons(entries) -> int:
    """Hand one free common weapon to every pet that owns no weapon at all.

    The choice is seeded per chat+player so an interrupted run re-picks the same weapon
    rather than wandering through the catalogue on each retry. Weapon designs are shared
    between players; only a player's own bag must remain duplicate-free.

    Locked per chat rather than per user: "owns no weapon" is a condition a player can
    re-enter by selling or gifting, and without the flag every restart would refill them.
    """
    granted = 0
    for entry in entries:
        data = _load(entry)
        if data.get(STARTER_WEAPON_GIFT_FLAG):
            continue
        pool = sorted(
            (item for item in C.ITEMS if item.slot == "weapon" and item.rarity == "common"),
            key=lambda item: item.code,
        )
        for user_id, record in sorted(data.get("pets", {}).items()):
            if not isinstance(record, dict) or not record.get("name"):
                continue
            owned = record.setdefault("inventory", [])
            if any(
                (item := C.find_item(code)) is not None and item.slot == "weapon"
                for code in owned
            ):
                continue
            gift = random.Random(f"{entry}:{user_id}:starter-weapon").choice(pool)
            owned.append(gift.code)
            _discover(record, gift.code)
            # Their weapon slot is empty by definition, so there is nothing to compare
            # against and nothing to displace.
            record.setdefault("equipped", {})["weapon"] = gift.code
            granted += 1
        data[STARTER_WEAPON_GIFT_FLAG] = True
        _save(entry, data)
    return granted


def retire_hamsterators(entries) -> dict:
    """Remove the retired building and refund every historic upgrade exactly once."""
    players = 0
    refunded_gold = 0
    for entry in entries:
        data = _load(entry)
        changed = False
        for user_id, record in data.get("pets", {}).items():
            if not isinstance(record, dict) or "hamsterator_level" not in record:
                continue
            level = min(
                max(0, int(record.get("hamsterator_level", 0) or 0)),
                len(C.LEGACY_HAMSTERATOR_UPGRADE_COSTS),
            )
            refund = sum(C.LEGACY_HAMSTERATOR_UPGRADE_COSTS[:level])
            if refund and economy.grant_once(
                entry, user_id, refund, HAMSTERATOR_RETIREMENT_REASON,
            ):
                players += 1
                refunded_gold += refund
            record.pop("hamsterator_level", None)
            changed = True
        if changed:
            _save(entry, data)
    return {"players": players, "gold": refunded_gold}


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


def _farm_multipliers(features: dict, hours: int, luck: int = 0) -> tuple[float, float, float]:
    """Gold, pet-XP and item-find terms for one farm run of the given length.

    `features` is a plain {key: bool} mapping -- either the record's LIVE farm_features
    (for a not-yet-started preview) or the SNAPSHOT stored on an active run (so an upgrade
    bought while the pet is away affects only the next trip, not the one in progress).
    `luck` is snapshotted the same way and for the same reason.

    Luck multiplies the finished chance, beds included: a player who has bought both the
    beds and the luck deserves them to compound, and multiplying only the base would make
    the building progressively less worth owning the luckier the pet got.
    """
    gold, pet_xp = 1.0, 1.0
    drop = C.FARM_DROP_CHANCE_BY_HOURS[max(0, min(C.FARM_MAX_HOURS, int(hours)))]
    for key, owned in (features or {}).items():
        if not owned:
            continue
        feature = C.FARM_FEATURES.get(key)
        if not feature:
            continue
        gold *= float(feature.get("gold_multiplier", 1.0))
        pet_xp *= float(feature.get("xp_multiplier", 1.0))
        drop += float(feature.get("drop_bonus", 0.0))
    return gold, pet_xp, min(1.0, drop * C.luck_drop_multiplier(luck))


# Rarity is checked from richest to plainest so a fallback always lands on something more
# common, never something rarer than what was actually rolled.
_FARM_RARITY_FALLBACK_ORDER = ("legendary", "rare", "common")


def _farm_item_for(
    data: dict, record: dict, rng: random.Random, hours: int, chance: float,
):
    """Roll a farm find: rarity first (hours-scaled), then an eligible item of that rarity.

    Loot is rolled at settlement time. Every item design may belong to more than one
    player, while a winner's own inventory still prevents duplicate copies.
    """
    if rng.random() >= chance:
        return None
    weights = C.FARM_LOOT_RARITY_WEIGHTS.get(int(hours))
    if not weights:
        return None
    order = [rarity for rarity in _FARM_RARITY_FALLBACK_ORDER if rarity in weights]
    if not order:
        return None
    picked = rng.choices(order, weights=[weights[rarity] for rarity in order], k=1)[0]
    owned = set(record.get("inventory", []))
    start = _FARM_RARITY_FALLBACK_ORDER.index(picked)
    for rarity in _FARM_RARITY_FALLBACK_ORDER[start:]:
        pool = [
            item for item in C.ITEMS
            if item.source == "drop" and item.rarity == rarity and item.code not in owned
        ]
        if not pool:
            continue
        weighted = [
            item for item in pool for _ in range(max(0, int(getattr(item, "drop_weight", 1))))
        ]
        if weighted:
            return rng.choice(weighted)
    return None


def _farm_run_hours(run: dict) -> int:
    """The shift length this run pays for.

    This is the field cancel_farm overwrites with the hours actually worked, so it is NOT
    always the originally chosen length. It is missing entirely only on a run persisted
    before this feature shipped, and every one of those was a fixed six-hour shift -- so a
    missing field means exactly that, never "unknown".
    """
    try:
        hours = int(run.get("hours", C.FARM_DURATION_HOURS))
    except (TypeError, ValueError):
        hours = C.FARM_DURATION_HOURS
    return max(0, min(C.FARM_MAX_HOURS, hours))


def _farm_reward(data: dict, record: dict, run: dict) -> dict:
    """Roll gold/xp/a find for one completed (or cancelled) shift.

    `level` and `features` are read from the RUN, not the live record: start_farm snapshots
    both when the pet leaves, so an upgrade bought while it is away changes only the next
    trip. The RNG is seeded from (run_id, hours) rather than run_id alone -- cancel_farm
    overwrites `hours` with whatever was actually worked, and that shorter shift must get
    its OWN reproducible roll rather than replaying what the full planned shift would have
    paid. Seeding this way (rather than storing the rolled reward up front, as before) is
    what makes retrying a crashed settlement safe: reload the same run, get the same seed,
    get the same gold/xp/item every time.
    """
    hours = _farm_run_hours(run)
    if hours <= 0:
        # Cancelling before a single whole hour has elapsed is allowed, but pays nothing --
        # there is deliberately no partial-hour credit, and no loot roll to go with it.
        return {"gold": 0, "xp": 0, "item_code": None}
    run_id = str(run.get("run_id") or "")
    level = min(max(1, int(run.get("level", 1) or 1)), C.FARM_MAX_LEVEL)
    features = run.get("features") if isinstance(run.get("features"), dict) else {}
    # A run started before luck affected drops has no snapshot; zero reproduces exactly
    # the chance it was promised when the pet left.
    luck = max(0, int(run.get("luck", 0) or 0))
    gold_multiplier, xp_multiplier, drop_chance = _farm_multipliers(features, hours, luck)
    rng = random.Random(f"{run_id}:{hours}")
    found = _farm_item_for(data, record, rng, hours, drop_chance)
    return {
        "gold": C.farm_gold_for(level, hours, gold_multiplier),
        "xp": C.farm_xp_for(level, hours, xp_multiplier),
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
    from persisted fields, so every retry chooses the same `grant_once` key.

    What happens to the payout depends on what else survived. A pre-rebalance run always
    carried a pre-rolled `reward` dict -- sanitise it (a damaged one is conservatively
    repaired to zero rather than inventing a payout) and settle_completed_farms will pay it
    verbatim, untouched by any later rebalance of the hour-scaled formula. A post-rebalance
    run that merely lost its id still has its `hours`/`level`/`features` snapshot intact, so
    it is left alone and rolls its reward normally at settlement. Only a run with NEITHER --
    genuinely blank, not just missing an id -- falls back to a zeroed, six-hour-shaped stub.
    """
    material = "|".join((str(user_id), str(run.get("started_at") or ""), str(run.get("ready_at") or "")))
    run["run_id"] = "recovered-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    reward = run.get("reward")
    if isinstance(reward, dict):
        run["reward"] = {
            "gold": max(0, int(reward.get("gold", 0) or 0)),
            "xp": max(0, int(reward.get("xp", 0) or 0)),
            "item_code": reward.get("item_code") if C.find_item(reward.get("item_code")) else None,
        }
    elif "hours" not in run:
        run["hours"] = C.FARM_DURATION_HOURS
        run["reward"] = {"gold": 0, "xp": 0, "item_code": None}
    return run["run_id"]


def _is_farming_record(record: dict | None, moment: datetime | None = None) -> bool:
    if not isinstance(record, dict):
        return False
    run = record.get("farm_run")
    return isinstance(run, dict) and not _farm_run_ready(run, moment or app_now())


def is_farming(entry, user_id, now: datetime | None = None) -> bool:
    """Whether the pet is still inside its current farm shift (any chosen length)."""
    return _is_farming_record(_tamed_record(_load(entry), user_id), now)


def farm_status(entry, user_id, now: datetime | None = None) -> dict:
    """Read-only UI status for the Farm button and scheduler.

    ``running`` means the pet's current shift is still locked in. ``ready`` means a
    completed job is awaiting settlement; callers should invoke
    :func:`settle_completed_farms` instead of treating it as a second collect button.
    """
    moment = now or app_now()
    data = _load(entry)
    record = _tamed_record(data, user_id)
    if record is None:
        # Tickets are still reported: they are earned by painting, so somebody can hold
        # several before they ever own a pet to spend them on.
        return {
            "available": False, "level": 0, "running": False, "ready": False,
            "tickets": _ticket_row(data, user_id)["count"], "can_ticket": False,
        }
    level = min(max(0, int(record.get("farm_level", 0) or 0)), C.FARM_MAX_LEVEL)
    run = record.get("farm_run") if isinstance(record.get("farm_run"), dict) else None
    ready = bool(run) and _farm_run_ready(run, moment)
    ready_at = run.get("ready_at") if run else None
    seconds_left = 0
    planned_hours = None
    worked_hours = 0
    if run:
        planned_hours = _farm_run_hours(run)
        if not ready:
            try:
                seconds_left = max(0, int((datetime.fromisoformat(ready_at) - moment).total_seconds()))
            except (TypeError, ValueError):
                seconds_left = 0
            try:
                started_at = datetime.fromisoformat(str(run.get("started_at")))
                worked_hours = max(0, min(
                    planned_hours, int((moment - started_at).total_seconds() // 3600),
                ))
            except (TypeError, ValueError):
                worked_hours = 0
    features = _farm_features(record)
    estimate_level = max(1, level)
    feature_status = {
        key: {
            "level": int(owned), "max_level": 1,
            "cost": int(C.FARM_FEATURES[key]["cost"]),
            "next_cost": None if owned else int(C.FARM_FEATURES[key]["cost"]),
            "effect": (
                "+25% монет" if key == "well" else
                "+25% опыта" if key == "sprinkler" else
                "+5 п.п. к шансу вещи" if key == "beds" else
                "+20% монет и опыта"
            ),
        }
        for key, owned in features.items()
    }
    # One row per selectable duration, priced at CURRENT level/features/luck -- nothing is
    # committed by looking, so unlike the frozen `reward` below this always reflects what
    # tapping that button right now would actually pay.
    live_luck = int((record.get("stats") or {}).get("luck", C.STAT_MIN_LEVEL) or 0)
    hour_previews = []
    for hours in C.FARM_HOUR_CHOICES:
        gold_multiplier, xp_multiplier, drop_chance = _farm_multipliers(features, hours, live_luck)
        hour_previews.append({
            "hours": hours,
            "gold": C.farm_gold_for(estimate_level, hours, gold_multiplier),
            "xp": C.farm_xp_for(estimate_level, hours, xp_multiplier),
            "drop_chance": drop_chance,
        })
    six_hour_preview = next(row for row in hour_previews if row["hours"] == C.FARM_DURATION_HOURS)
    # The active run's payout is projected from its OWN frozen level/features snapshot
    # (or, for a legacy pre-rebalance run, the reward it pre-rolled up front) -- never from
    # the live values above, for the same "upgrades affect only the next trip" reason.
    projected_reward = None
    if run:
        legacy_reward = run.get("reward")
        projected_reward = (
            dict(legacy_reward) if isinstance(legacy_reward, dict) else _farm_reward(data, record, run)
        )
    return {
        "available": True,
        "level": level,
        "max_level": C.FARM_MAX_LEVEL,
        "duration_hours": C.FARM_DURATION_HOURS,
        "min_hours": C.FARM_MIN_HOURS,
        "max_hours": C.FARM_MAX_HOURS,
        "running": bool(run) and not ready,
        # ``active`` is retained for the Telegram view's simple boolean contract.
        "active": bool(run) and not ready,
        "ready": ready,
        "can_start": level > 0 and not run,
        "can_cancel": bool(run) and not ready,
        "tickets": _ticket_row(data, user_id)["count"],
        # The button is offered only when it would actually do something: a ticket in hand,
        # a shift running, and more than a minute of it left to cut.
        "can_ticket": (
            bool(run) and not ready
            and _ticket_row(data, user_id)["count"] > 0
            and seconds_left > C.FARM_TICKET_SECONDS
        ),
        "ticket_seconds": C.FARM_TICKET_SECONDS,
        "started_at": run.get("started_at") if run else None,
        "ready_at": ready_at,
        "seconds_left": seconds_left,
        "planned_hours": planned_hours,
        "worked_hours": worked_hours,
        "reward": projected_reward,
        "features": feature_status,
        "feature_costs": {key: int(spec["cost"]) for key, spec in C.FARM_FEATURES.items()},
        "next_level_cost": C.FARM_UPGRADE_COSTS[level] if level < C.FARM_MAX_LEVEL else None,
        "next_level_bonus": (
            f"{C.FARM_GOLD_PER_RUN[level + 1]} монет · {C.FARM_XP_PER_RUN[level + 1]} опыта"
            f" за {C.FARM_DURATION_HOURS}-часовую смену"
            if level < C.FARM_MAX_LEVEL else None
        ),
        "hour_previews": hour_previews,
        # Kept for backward compatibility with anything still reading a single number:
        # the six-hour anchor row of hour_previews, byte-for-byte what these three fields
        # meant before durations existed.
        "estimated_gold": six_hour_preview["gold"],
        "estimated_xp": six_hour_preview["xp"],
        "drop_chance": six_hour_preview["drop_chance"],
    }


def start_farm(
    entry, user_id, hours: int = C.FARM_DURATION_HOURS, now: datetime | None = None,
) -> tuple[bool, str]:
    """Send a pet to the farm for a player-chosen 1-8 hour shift.

    The run snapshots the CURRENT farm level and features onto itself, not the reward --
    the reward is deliberately rolled later, at settlement, from this snapshot and however
    many hours turn out to have actually been worked (see _farm_reward). Freezing level and
    features here rather than reading them live at settlement is what still guarantees an
    upgrade bought while the pet is away only affects the NEXT trip.
    """
    moment = now or app_now()
    # Finish a due run first so a user who opens the menu after their shift ends is not
    # stuck waiting for the background poller before starting the next one.
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
    try:
        hours = int(hours)
    except (TypeError, ValueError):
        hours = C.FARM_DURATION_HOURS
    if hours not in C.FARM_HOUR_CHOICES:
        return False, f"Выбери смену от {C.FARM_MIN_HOURS} до {C.FARM_MAX_HOURS} часов."
    run_id = secrets.token_hex(16)
    ready_at = moment + timedelta(hours=hours)
    record["farm_run"] = {
        "run_id": run_id,
        "started_at": moment.isoformat(),
        "ready_at": ready_at.isoformat(),
        "hours": hours,
        "level": level,
        "features": _farm_features(record),
        "luck": int((record.get("stats") or {}).get("luck", C.STAT_MIN_LEVEL) or 0),
    }
    _save(entry, data)
    return True, f"Питомец отправлен на ферму на {hours} ч."


def _ruby_row(data: dict) -> dict:
    wallet = data.setdefault("rubies", {})
    if not isinstance(wallet, dict):
        wallet = data["rubies"] = {}
    return wallet


def ruby_balance(entry, user_id) -> int:
    """How many Руби this member holds.

    A second currency, deliberately NOT in economy.py: coins are the chat's ledger, shared
    with /stat and /shop and earned by talking, while rubies come only off mobs and the
    occasional farm shift. Keeping them apart means nothing that spends coins can ever
    accidentally spend these, and the day rubies get a sink they can be priced on their
    own supply rather than against a ledger they never entered.
    """
    return max(0, int(_ruby_row(_load(entry)).get(str(user_id), 0) or 0))


def grant_rubies(entry, user_id, amount: int) -> int:
    """Add rubies and return the new balance. Nothing spends them yet, by design."""
    amount = max(0, int(amount or 0))
    if not amount:
        return ruby_balance(entry, user_id)
    with _farm_settlement_lock:
        data = _load(entry)
        wallet = _ruby_row(data)
        total = max(0, int(wallet.get(str(user_id), 0) or 0)) + amount
        wallet[str(user_id)] = total
        _metric_add(data, "rubies_minted", amount)
        _save(entry, data)
    return total


def farm_tickets(entry, user_id) -> int:
    return _ticket_row(_load(entry), user_id)["count"]


def _ticket_row(data: dict, user_id) -> dict:
    """This player's ticket wallet, repaired in place.

    Kept at the top level of the store rather than on the pet record, because a ticket is
    earned by painting a figurine -- something a member can do long before they buy a cage,
    and the reward for it must not quietly evaporate because they had no pet yet.
    """
    wallet = data.setdefault("farm_tickets", {})
    if not isinstance(wallet, dict):
        wallet = data["farm_tickets"] = {}
    row = wallet.setdefault(str(user_id), {})
    if not isinstance(row, dict):
        row = wallet[str(user_id)] = {}
    row["count"] = _safe_nonnegative_int(row.get("count"))
    granted = row.get("granted")
    row["granted"] = [str(key) for key in granted][-FARM_TICKET_GRANT_MEMORY:] if isinstance(granted, list) else []
    return row


def grant_farm_ticket(entry, user_id, reason: str = "") -> bool:
    """Hand one farm ticket to a member. True if this call is what added it.

    `reason` is an idempotency key, and the caller is expected to supply the identity of
    the event being rewarded (a message id, for a #япокрасил post). Listener deliveries
    are normally exactly once, but a reconnect can replay an update -- stats.
    record_figurine_live already refuses to count the same message twice for exactly this
    reason, and a ticket is worth considerably more than a figurine in the day's tally.

    Only the last few keys are remembered: a replay arrives seconds later, not weeks, and
    an unbounded list of every paint anybody has ever posted is a store that only grows.
    """
    with _farm_settlement_lock:
        data = _load(entry)
        row = _ticket_row(data, user_id)
        key = str(reason or "")
        if key and key in row["granted"]:
            return False
        row["count"] += 1
        if key:
            row["granted"] = (row["granted"] + [key])[-FARM_TICKET_GRANT_MEMORY:]
        _save(entry, data)
        return True


def use_farm_ticket(entry, user_id, now: datetime | None = None) -> tuple[bool, str]:
    """Spend a ticket to cut the running shift down to one minute.

    Only `ready_at` moves. `hours` -- the field the payout is computed from, and the field
    cancel_farm overwrites with the hours actually worked -- is deliberately left alone, so
    an eight-hour shift redeemed at minute three still pays for eight hours. That IS the
    ticket: it buys the waiting, not the work. Nothing is granted here either; the ordinary
    settlement path pays the run a minute later, through the same single grant key it
    always would have.
    """
    moment = now or app_now()
    with _farm_settlement_lock:
        data = _load(entry)
        record = _tamed_record(data, user_id)
        if record is None:
            return False, "Сначала приручи существо."
        run = record.get("farm_run")
        if not isinstance(run, dict):
            return False, "Питомец сейчас не работает на ферме."
        if _farm_run_ready(run, moment):
            return False, "Смена уже закончилась — награда вот-вот придёт."
        row = _ticket_row(data, user_id)
        if row["count"] <= 0:
            return False, "Билетов нет. Их дают за новый покрас."
        ready_at = moment + timedelta(seconds=C.FARM_TICKET_SECONDS)
        try:
            # Refuse rather than burn a ticket on a shift that was about to end anyway --
            # this can only ever move the finish line closer.
            if datetime.fromisoformat(str(run.get("ready_at"))) <= ready_at:
                return False, "Смена и так закончится раньше — билет не нужен."
        except (TypeError, ValueError):
            pass
        row["count"] -= 1
        run["ready_at"] = ready_at.isoformat()
        _save(entry, data)
    hours = _farm_run_hours(run)
    return True, (
        f"Билет использован: смена на {hours} ч закончится через минуту. "
        f"Награду заплатят как за полные {hours} ч."
    )


def cancel_farm(entry, user_id, now: datetime | None = None) -> tuple[bool, str]:
    """Recall a farming pet early, paying only for whole hours actually worked.

    This deliberately does NOT compute or grant a payout itself. It stamps the worked hour
    count onto the run (overwriting the originally planned length) and marks the run ready
    right now, then hands off to the ordinary settle_completed_farms path -- the same single
    grant key, the same receipt shape, the same one place gold is ever minted for a farm
    run. A cancelled run pays the SHORT-shift rate for those hours, not a prorated share of
    the long shift that was originally chosen: quitting early genuinely costs the better
    per-hour rate and rarity odds a longer stay would have earned (see FARM_DURATION_BONUS
    and FARM_LOOT_RARITY_WEIGHTS), it does not just truncate them.
    """
    moment = now or app_now()
    data = _load(entry)
    record = _tamed_record(data, user_id)
    if record is None:
        return False, "Сначала приручи существо."
    run = record.get("farm_run")
    if not isinstance(run, dict):
        return False, "Питомец сейчас не работает на ферме."
    if _farm_run_ready(run, moment):
        return False, "Смена уже закончилась — открой ферму, чтобы получить награду."
    try:
        started_at = datetime.fromisoformat(str(run.get("started_at")))
    except (TypeError, ValueError):
        started_at = moment
    planned_hours = _farm_run_hours(run)
    worked_hours = max(0, min(planned_hours, int((moment - started_at).total_seconds() // 3600)))
    run["hours"] = worked_hours
    # Only a run started before this feature shipped can be missing these -- fall back to
    # the pet's current level/features for it, since no true start-of-shift snapshot exists.
    run.setdefault("level", min(max(1, int(record.get("farm_level", 0) or 0)), C.FARM_MAX_LEVEL))
    run.setdefault("features", _farm_features(record))
    run.setdefault("luck", int((record.get("stats") or {}).get("luck", C.STAT_MIN_LEVEL) or 0))
    run.pop("reward", None)  # force a fresh, hours-scaled roll instead of a stale legacy one
    run["ready_at"] = moment.isoformat()
    _save(entry, data)
    settle_completed_farms(entry, now=moment)
    if worked_hours <= 0:
        return True, "Питомец вернулся с фермы раньше времени: меньше часа работы награды не даёт."
    return True, f"Питомец досрочно вернулся с фермы: засчитано {worked_hours} из {planned_hours} ч."


def upgrade_farm(entry, user_id, xp, now: datetime | None = None) -> tuple[bool, str]:
    """Upgrade the farm after banking passive gold at the old level's rate."""
    moment = now or app_now()
    data = _load(entry)
    record = _tamed_record(data, user_id)
    if record is None:
        return False, "Сначала приручи существо."
    level = min(max(0, int(record.get("farm_level", 0) or 0)), C.FARM_MAX_LEVEL)
    if level >= C.FARM_MAX_LEVEL:
        return False, f"Ферма уже максимального уровня ({C.FARM_MAX_LEVEL})."
    # Settle the arena bank against the PRE-upgrade cap before this farm opens a new
    # capacity slot.  The new slot is room, not retroactive hourly credit.
    old_capacity, *_ = _fight_bank_components(entry, user_id, record, moment)
    _, _, bank_changed = _settle_fight_bank(record, old_capacity, moment)
    if bank_changed:
        _save(entry, data)
    settle_passive_income(entry, user_id, now=moment)
    # Settlement writes economy and telemetry independently. Reload so the farm upgrade
    # cannot overwrite a freshly updated metrics record with the stale snapshot above.
    data = _load(entry)
    record = _tamed_record(data, user_id)
    level = min(max(0, int(record.get("farm_level", 0) or 0)), C.FARM_MAX_LEVEL)
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


def _farm_receipt(
    user_id: str, record: dict, run_id: str, hours: int, moment: datetime, levels_gained: int,
    reward: dict, item_code: str | None, auto_equipped: bool,
) -> dict:
    return {
        "user_id": str(user_id),
        "run_id": str(run_id or ""),
        "pet_name": record.get("name") or "Питомец",
        "hours": max(0, int(hours or 0)),
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

    Gold is guarded by ``economy.grant_once(..., run_id)``. The same-process lock (held for
    the whole function) covers concurrent poll ticks/button requests. Each due run's payout
    is rolled exactly ONCE per call, here in the first pass over `initial` -- before
    anything is mutated or saved -- so a crash between the grant and the final save simply
    reloads the same still-untouched on-disk run on the next retry and reproduces the
    identical numbers (see _farm_reward's seeding). A run that still carries a pre-rebalance
    `reward` dict -- anything started before this feature shipped, or a corrupt run
    `_repair_farm_run_id` just sanitised -- pays that dict verbatim instead of rolling a new
    one, so an in-flight shift is never retroactively repriced by a later rebalance.
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
            legacy_reward = run.get("reward")
            if isinstance(legacy_reward, dict):
                reward = {
                    "gold": max(0, int(legacy_reward.get("gold", 0) or 0)),
                    "xp": max(0, int(legacy_reward.get("xp", 0) or 0)),
                    "item_code": (
                        legacy_reward.get("item_code")
                        if C.find_item(legacy_reward.get("item_code")) else None
                    ),
                }
            else:
                reward = _farm_reward(initial, record, run)
            found = C.find_item(reward.get("item_code")) if reward.get("item_code") else None
            due.append((str(user_id), dict(run), reward))
        if repaired:
            # Persist the deterministic recovery before `grant_once`; a crash after it
            # is therefore retried under exactly the same payout key.
            _save(entry, initial)
        for user_id, snapshot_run, reward in due:
            run_id = str(snapshot_run.get("run_id") or "")
            hours = _farm_run_hours(snapshot_run)
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
            # An occasional ruby off a shift. Seeded on the run id like the rest of the
            # payout, so a retried settlement pays the same one rather than rolling again
            # -- the same reproducibility contract _farm_reward documents.
            ruby_rng = random.Random(f"{run_id}:ruby")
            ruby = (
                ruby_rng.randint(C.FARM_RUBY_MIN, C.FARM_RUBY_MAX)
                if ruby_rng.random() < C.FARM_RUBY_CHANCE else 0
            )
            receipt = _farm_receipt(
                user_id, record, run_id, hours, moment, levels_gained, reward, item_code, auto_equipped,
            )
            receipt["rubies"] = ruby
            record.setdefault("farm_notifications", []).append(receipt)
            record["farm_notifications"] = record["farm_notifications"][-50:]
            record["farm_run"] = None
            _metric_add(data, "farm_gold_minted", gold)
            _metric_add(data, "farm_runs")
            _save(entry, data)
            if ruby:
                grant_rubies(entry, user_id, ruby)
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


def reset_scroll_collections(entries) -> dict:
    """Empty every scroll collection and every slot, once per chat.

    A clean zero, not a floor: the four slots may each be empty, so "owns nothing" is a
    state a creature can actually be in, and all forty scrolls are earned through paint
    and hard-quest drops from here.

    Ownership lives in three places that all have to be cleared together, because each
    one is merged back into the others on the next read: the creature's ``owned_scrolls``,
    the equipped ``skill_slots`` (folded into the owned list by ``_owned_scroll_codes_for``,
    so leaving them would hand back precisely the scrolls a player was using), and the
    per-user wallet that survives an untamed gap. ``reward_log`` is deliberately kept --
    it is the receipt ledger that stops an old paint or quest being replayed for a second
    roll, and clearing it would reopen every past event to farming.
    """
    report = {"players": 0, "scrolls": 0}
    for entry in entries:
        data = _load(entry)
        if data.get(SCROLL_RESET_FLAG):
            continue
        pets_by_user = data.get("pets", {})
        wallets = data.get("scroll_wallets", {})
        for user_id in dict.fromkeys([*pets_by_user, *wallets]):
            record = pets_by_user.get(user_id)
            wallet = wallets.get(user_id)
            held = set()
            if isinstance(record, dict):
                for key in ("owned_scrolls", "skill_slots"):
                    held.update(
                        code for code in record.get(key) or [] if isinstance(code, str)
                    )
            if isinstance(wallet, dict):
                held.update(
                    code for code in wallet.get("unlocked") or [] if isinstance(code, str)
                )
            if held:
                report["players"] += 1
                report["scrolls"] += len(held)
            if isinstance(record, dict):
                record["owned_scrolls"] = []
                record["skill_slots"] = list(SCROLLS.EMPTY_LOADOUT)
            if isinstance(wallet, dict):
                wallet["unlocked"] = []
                # The collection starts over, so the counters pacing it start over too --
                # nobody carries 19 unlucky paints into a catalogue they no longer own.
                wallet["pity"] = {"paint": 0, "hard_quest": 0}
        # The mailbox replays these forever; left alone it would go on congratulating
        # players on scrolls the same migration just took away.
        data["scroll_notifications"] = []
        data[SCROLL_RESET_FLAG] = True
        _save(entry, data)
    return report


def upgrade_cage(entry, user_id, xp, now: datetime | None = None) -> tuple[bool, str]:
    data = _load(entry)
    record = data["pets"].get(str(user_id))
    if not record:
        return False, "Сначала купи клетку."
    level = record.get("cage_level", 1)
    if level >= C.CAGE_MAX_LEVEL:
        return False, f"Клетка уже максимального уровня ({C.CAGE_MAX_LEVEL})."
    moment = now or app_now()
    old_capacity, *_ = _fight_bank_components(entry, user_id, record, moment)
    _, _, bank_changed = _settle_fight_bank(record, old_capacity, moment)
    if bank_changed:
        _save(entry, data)
    cost = C.CAGE_UPGRADE_COSTS[level]
    ok, balance = economy.spend(entry, user_id, xp, cost, "buy:pet_cage_upgrade")
    if not ok:
        return False, f"Нужно {cost} монет на апгрейд клетки, у тебя {balance}."
    record["cage_level"] = level + 1
    _save(entry, data)
    return True, f"Клетка прокачана до {level + 1} уровня за {cost} монет."


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
    # A new picture is a new composition, so the old framing cannot survive it -- the
    # square that centred somebody's last figurine would land anywhere on this one.
    record.pop("portrait_crop", None)
    _save(entry, data)
    return True, "Фото обновлено."


def set_portrait_crop(entry, user_id, crop: dict | None) -> tuple[bool, str]:
    """How the pet's photo is framed into a square: {x, y, size} in the PHOTO'S OWN pixels.

    The rectangle is stored rather than the cut pixels, the same way voting.py stores an
    entry's framing (see vote_image._crop_to_square): the picture itself lives on
    Telegram's servers and is re-rendered from a file_id every time, so a framing baked
    into pixels could never be adjusted afterwards, and every place that draws the pet
    would have to agree on which of two images was the real one.

    `None` clears it, which means "fit the whole photo" -- the same default an untouched
    pet has always had.
    """
    data = _load(entry)
    record = _tamed_record(data, user_id)
    if record is None:
        return False, "Сначала приручи существо."
    if crop is None:
        record.pop("portrait_crop", None)
        _save(entry, data)
        return True, "Кадрирование сброшено."
    try:
        x, y, size = float(crop["x"]), float(crop["y"]), float(crop["size"])
    except (KeyError, TypeError, ValueError):
        return False, "Не понял кадрирование."
    if not (size > 0) or size > 100_000 or abs(x) > 100_000 or abs(y) > 100_000:
        return False, "Кадрирование вне допустимых границ."
    record["portrait_crop"] = {"x": x, "y": y, "size": size}
    _save(entry, data)
    return True, "Кадр сохранён."


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


def available_stat_points(record: dict | None) -> int:
    """Unspent respec points attached to one pet record."""
    try:
        return max(0, int((record or {}).get("stat_points", 0) or 0))
    except (TypeError, ValueError):
        return 0


def respec_stats(entry, user_id) -> tuple[bool, str, int]:
    """Reset purchased stats and turn their invested levels into free stat points."""
    with _farm_settlement_lock:
        data = _load(entry)
        record = _tamed_record(data, user_id)
        if record is None:
            return False, "Сначала приручи существо.", 0
        refundable = sum(
            max(0, int(record["stats"].get(key, C.STAT_MIN_LEVEL)) - C.STAT_MIN_LEVEL)
            for key in C.STAT_KEYS
        )
        if refundable <= 0:
            return False, "Сбрасывать пока нечего.", 0
        wallet = _ruby_row(data)
        rubies = max(0, int(wallet.get(str(user_id), 0) or 0))
        if rubies < C.STAT_RESPEC_RUBY_COST:
            return False, f"Нужно {C.STAT_RESPEC_RUBY_COST} 💎, у тебя {rubies}.", 0
        wallet[str(user_id)] = rubies - C.STAT_RESPEC_RUBY_COST
        record["stats"] = {key: C.STAT_MIN_LEVEL for key in C.STAT_KEYS}
        record["stat_points"] = available_stat_points(record) + refundable
        _save(entry, data)
    return True, f"Статы сброшены. Свободных очков: {record['stat_points']}.", refundable


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

    # Costs climb with level (see pets_config.stat_upgrade_cost), so N levels is NOT
    # N * cost -- walk the individual steps and stop the moment the running total would
    # exceed what is actually in the wallet, rather than refusing the whole batch or
    # letting the balance go negative.
    current_balance = economy.balance(entry, user_id, xp)
    bought = 0
    total_cost = 0
    reached_level = level
    free_points = available_stat_points(record)
    used_points = 0
    while bought < times:
        if used_points < free_points:
            used_points += 1
        else:
            step_cost = C.stat_upgrade_cost(reached_level)
            if total_cost + step_cost > current_balance:
                break
            total_cost += step_cost
        reached_level += 1
        bought += 1

    if bought == 0:
        needed = C.stat_upgrade_cost(level)
        return False, f"Нужно {needed} монет на следующий уровень {name}, у тебя {current_balance}.", 0

    ok = total_cost == 0 or economy.spend(
        entry, user_id, xp, total_cost, f"buy:pet_stat:{stat}", ref=str(reached_level),
    )[0]
    if not ok:
        # The balance moved between the check above and the debit (e.g. a second command
        # landed in between) -- refuse cleanly on stale numbers rather than overspend.
        return False, "Баланс изменился, попробуй ещё раз.", 0

    record["stats"][stat] = reached_level
    record["stat_points"] = free_points - used_points
    _save(entry, data)

    point_note = f" Использовано очков: {used_points}." if used_points else ""
    if bought == times:
        message = f"{name} прокачан(а) до {reached_level} уровня. Потрачено {total_cost} монет.{point_note}"
    else:
        message = (
            f"Хватило золота только на {bought} из {times} уровней {name} "
            f"(до {reached_level}), потрачено {total_cost} монет.{point_note}"
        )
    return True, message, total_cost


def effective_stats(entry, user_id) -> dict:
    return _effective_stats_for(_tamed_record(_load(entry), user_id) or {})


def _skill_loadout_for(record: dict | None) -> tuple:
    try:
        return SCROLLS.validate_loadout((record or {}).get("skill_slots"))
    except ValueError:
        return SCROLLS.EMPTY_LOADOUT


def _owned_scroll_codes_for(record: dict | None) -> tuple[str, ...]:
    """Canonical permanent scroll collection for one creature.

    Starts empty and grows only through drops. Equipped codes are folded in as a
    backwards-compatibility safeguard: an old valid loadout can never become unusable
    merely because a save was read during the ownership migration.
    """
    legal = {row["code"] for row in SCROLLS.SCROLLS}
    values = (record or {}).get("owned_scrolls")
    if not isinstance(values, list):
        values = []
    values = [*SCROLLS.equipped_codes(_skill_loadout_for(record)), *values]
    return tuple(dict.fromkeys(code for code in values if isinstance(code, str) and code in legal))


def owned_scrolls(entry: str, user_id, *, ultimate: bool | None = None) -> tuple[str, ...]:
    """Permanent abilities this creature may equip, ordered by discovery.

    ``ultimate`` narrows the collection for a particular slot without exposing a second
    mutable inventory model to Telegram and the Mini App.
    """
    data = _load(entry)
    record = _tamed_record(data, user_id)
    values = tuple(dict.fromkeys([
        *_owned_scroll_codes_for(record), *_scroll_wallet(data, user_id)["unlocked"],
    ]))
    if ultimate is None:
        return values
    return tuple(code for code in values if bool(SCROLLS.scroll(code).get("ultimate")) == bool(ultimate))


def _scroll_wallet(data: dict, user_id) -> dict:
    """A durable per-user reward wallet, also valid before the first pet is tamed."""
    wallets = data.setdefault("scroll_wallets", {})
    if not isinstance(wallets, dict):
        wallets = data["scroll_wallets"] = {}
    row = wallets.setdefault(str(user_id), {})
    if not isinstance(row, dict):
        row = wallets[str(user_id)] = {}
    legal = {spell["code"] for spell in SCROLLS.SCROLLS}
    unlocked = row.get("unlocked")
    if not isinstance(unlocked, list):
        unlocked = []
    row["unlocked"] = list(dict.fromkeys(
        code for code in unlocked if isinstance(code, str) and code in legal
    ))
    _scroll_reward_row(row)
    return row


def _scroll_reward_row(row: dict) -> tuple[dict, dict]:
    """Repair a reward wallet's bounded replay log and pity counters."""
    log = row.setdefault("reward_log", {})
    if not isinstance(log, dict):
        log = row["reward_log"] = {}
    pity = row.setdefault("pity", {})
    if not isinstance(pity, dict):
        pity = row["pity"] = {}
    for kind in ("paint", "hard_quest", "dungeon"):
        pity[kind] = _safe_nonnegative_int(pity.get(kind))
    return log, pity


def _remember_scroll_reward(log: dict, source: str, receipt: dict) -> None:
    """Remember both misses and wins: a replay must not get a fresh dice roll."""
    log[source] = dict(receipt)
    if len(log) > SCROLL_REWARD_MEMORY:
        # Dict insertion order is durable in current Python.  Keeping the newest keys is
        # enough for transport retries; quest acceptance itself has a separate permanent
        # submission-state guard, and a figurine replay is already swallowed by tickets.
        for stale in list(log)[:len(log) - SCROLL_REWARD_MEMORY]:
            log.pop(stale, None)


def _remember_scroll_notification(data: dict, user_id, receipt: dict) -> None:
    """A rare unlock must be visible even when it came from a group paint post."""
    if not receipt.get("granted"):
        return
    rows = data.setdefault("scroll_notifications", [])
    if not isinstance(rows, list):
        rows = data["scroll_notifications"] = []
    source = str(receipt.get("source") or "")
    if any(str(row.get("source") or "") == source and str(row.get("user_id")) == str(user_id)
           for row in rows):
        return
    rows.append({
        "user_id": str(user_id), "source": source, "ts": app_now().isoformat(),
        "code": receipt.get("code"), "name": receipt.get("name"), "icon": receipt.get("icon"),
        "ultimate": bool(receipt.get("ultimate")), "kind": receipt.get("kind"),
    })
    data["scroll_notifications"] = rows[-400:]


def grant_scroll_reward(
    entry: str, user_id, *, source: str, kind: str, chance: float, pity_after: int,
    seed: str | None = None,
) -> dict:
    """Try to unlock one previously unknown scroll, exactly once for an earned event.

    The caller supplies a stable source identity (a figurine's message id or a quest
    submission id).  We persist a miss too, so retries cannot farm randomness.  A pity
    counter only advances on eligible, unique events and resets solely after an actual
    unlock.  Ultimates are selected from the same permanent collection but only have a
    12% share while ordinary scrolls remain, making them meaningfully rarer without a
    second loot table to tune.
    """
    source = str(source or "").strip()
    if kind not in ("paint", "hard_quest", "dungeon") or not source:
        return {"granted": False, "reason": "invalid_source"}
    chance = max(0.0, min(1.0, float(chance or 0.0)))
    pity_after = max(1, int(pity_after or 1))
    with _farm_settlement_lock:
        data = _load(entry)
        record = _tamed_record(data, user_id)
        wallet = _scroll_wallet(data, user_id)
        log, pity = _scroll_reward_row(wallet)
        prior = log.get(source)
        if isinstance(prior, dict):
            return dict(prior)

        owned = set(_owned_scroll_codes_for(record)) | set(wallet["unlocked"])
        regular = [row for row in SCROLLS.REGULAR_SCROLLS if row["code"] not in owned]
        ultimates = [row for row in SCROLLS.ULTIMATE_SCROLLS if row["code"] not in owned]
        if not regular and not ultimates:
            receipt = {"granted": False, "reason": "catalogue_complete", "source": source, "kind": kind}
            _remember_scroll_reward(log, source, receipt)
            _save(entry, data)
            return receipt

        entropy = f"{entry}|{user_id}|{kind}|{source}|{seed or source}"
        rng = random.Random(hashlib.sha256(entropy.encode("utf-8")).hexdigest())
        forced = pity[kind] + 1 >= pity_after
        won = forced or rng.random() < chance
        if not won:
            pity[kind] += 1
            receipt = {
                "granted": False, "reason": "miss", "source": source, "kind": kind,
                "pity": pity[kind], "pity_after": pity_after,
            }
            _remember_scroll_reward(log, source, receipt)
            _save(entry, data)
            return receipt

        # No ordinary candidates means an ultimate is the only possible useful drop;
        # otherwise its low fixed share makes ultimate unlocks genuinely special.
        pool = ultimates if not regular or (ultimates and rng.random() < ULTIMATE_SCROLL_SHARE) else regular
        spell = pool[rng.randrange(len(pool))]
        wallet["unlocked"].append(spell["code"])
        if record is not None:
            record["owned_scrolls"] = list(dict.fromkeys([
                *_owned_scroll_codes_for(record), *wallet["unlocked"],
            ]))
        pity[kind] = 0
        receipt = {
            "granted": True, "source": source, "kind": kind, "code": spell["code"],
            "name": spell["name"], "icon": spell["icon"], "ultimate": bool(spell["ultimate"]),
            "forced": forced,
        }
        _remember_scroll_reward(log, source, receipt)
        _remember_scroll_notification(data, user_id, receipt)
        _save(entry, data)
        return receipt


def grant_scroll_for_painting(entry: str, user_id, message_id) -> dict:
    """One rare scroll attempt for a genuinely new #япокрасил ticket event."""
    return grant_scroll_reward(
        entry, user_id, source=f"paint:{message_id}", kind="paint", chance=PAINT_SCROLL_CHANCE,
        pity_after=PAINT_SCROLL_PITY, seed=f"paint:{message_id}",
    )


def grant_scroll_for_hard_quest(entry: str, user_id, submission_id, difficulty) -> dict:
    """Reward accepted difficulty-4/5 quests; easier cards never enter this loot roll."""
    try:
        difficulty = int(difficulty)
    except (TypeError, ValueError):
        difficulty = 0
    chance = HARD_QUEST_SCROLL_CHANCES.get(difficulty)
    if chance is None:
        return {"granted": False, "reason": "not_hard", "kind": "hard_quest"}
    return grant_scroll_reward(
        entry, user_id, source=f"quest:{submission_id}", kind="hard_quest", chance=chance,
        pity_after=HARD_QUEST_SCROLL_PITY, seed=f"quest:{submission_id}",
    )


def skill_loadout(entry, user_id) -> tuple:
    """The pet's four scroll slots, in order. An empty slot reads as None."""
    return _skill_loadout_for(_tamed_record(_load(entry), user_id))


def clear_skill_slot(entry, user_id, slot: int) -> tuple[bool, str]:
    """Take the scroll out of one slot and leave it empty.

    Emptying is an ordinary move rather than an undo: a creature is not required to
    field four scrolls, so a slot with nothing in it is a position a player can choose.
    """
    return set_skill_slot(entry, user_id, slot, None)


def set_skill_slot(entry, user_id, slot: int, code: str | None) -> tuple[bool, str]:
    """Equip one permanently unlocked scroll, or empty the slot with a falsy code.

    Abilities are never consumed: equipping moves a scroll the creature already owns
    into a slot, and clearing puts it back in the collection untouched.
    """
    data = _load(entry)
    record = _tamed_record(data, user_id)
    if record is None:
        return False, "Сначала приручи существо."
    try:
        index = int(slot) - 1
    except (TypeError, ValueError):
        index = -1
    if index not in range(4):
        return False, "Неизвестный слот."
    loadout = list(_skill_loadout_for(record))

    if not code:
        if loadout[index] is None:
            return False, f"Слот {slot} и так пустой."
        loadout[index] = None
        record["skill_slots"] = list(SCROLLS.validate_loadout(loadout))
        _save(entry, data)
        return True, f"Слот {slot} свободен."

    spell = SCROLLS.scroll(code)
    if spell is None:
        return False, "Неизвестный свиток."
    if bool(spell.get("ultimate")) != (index == 3):
        return False, "В четвёртом слоте должен быть ультимейт, в первых трёх — обычные свитки."
    if code not in _owned_scroll_codes_for(record):
        return False, "Этот свиток ещё не открыт. Его можно найти за покрас или сложный квест."
    if code in loadout and loadout[index] != code:
        return False, "Один свиток нельзя поставить сразу в два слота."
    loadout[index] = code
    try:
        record["skill_slots"] = list(SCROLLS.validate_loadout(loadout))
    except ValueError as error:
        return False, str(error)
    _save(entry, data)
    return True, f"Слот {slot}: «{spell['name']}»."


def _combat_shield_for(record: dict | None) -> dict | None:
    code = ((record or {}).get("equipped") or {}).get("shield")
    item = C.find_item(code) if code else None
    if item is None or item.slot != "shield":
        return None
    effect = dict(getattr(item, "effect", {}) or {})
    return {
        "code": item.code, "name": item.name,
        "guard": effect.get("guard", .40),
        "defend_effects": tuple(
            dict(row) for row in effect.get("defend_effects", ()) if isinstance(row, dict)
        ),
    }


def combat_shield(entry, user_id) -> dict | None:
    """Snapshot of the worn shield's Defend hook for deterministic combat/replay."""
    return _combat_shield_for(_tamed_record(_load(entry), user_id))


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


def _dungeon_active(record: dict | None) -> bool:
    return bool(isinstance(record, dict) and record.get("dungeon_run"))


def dungeon_status(entry: str, user_id) -> dict:
    """Public state reconstructed from the server-owned dungeon run."""
    record = _tamed_record(_load(entry), user_id)
    if record is None:
        return {
            "active": False, "available": D.DUNGEON_OPEN,
            "closed_notice": D.DUNGEON_CLOSED_NOTICE, "min_power": D.MIN_POWER,
        }
    run = record.get("dungeon_run")
    state = {
        "active": bool(run), "available": D.DUNGEON_OPEN,
        "closed_notice": D.DUNGEON_CLOSED_NOTICE, "min_power": D.MIN_POWER,
        "power": _power_rating_for(record), "deepest": int(record.get("dungeon_deepest", 1)),
        "entry_cost": D.ENTRY_RUBY_COST,
        "escalator_cost": D.ESCALATOR_RUBY_COST,
    }
    if not isinstance(run, dict):
        return state
    floor = max(1, int(run.get("floor", 1) or 1))
    max_hp = max(1, int(run.get("max_hp", 1) or 1))
    cleared = {int(value) for value in run.get("cleared", []) if str(value).isdigit()}
    encounters = []
    for row in D.encounters_for_floor(floor):
        copy = dict(row)
        copy["cleared"] = copy["index"] in cleared
        encounters.append(copy)
    state.update({
        "floor": floor, "theme": D.floor_name(floor), "hp": max(0, int(run.get("hp", max_hp))),
        "max_hp": max_hp, "cleared": sorted(cleared), "encounters": encounters,
        "can_rest": len(cleared) == len(encounters),
        "heal_cost": D.shop_heal_cost(floor), "boss_lives": int(run.get("boss_lives", 0) or 0),
    })
    return state


def _dungeon_fighter(record: dict, key: str, *, damage_multiplier: float = 1.0) -> pets_combat.Fighter:
    effective = _effective_stats_for(record)
    effects = []
    for code in (record.get("equipped") or {}).values():
        item = C.find_item(code) if code else None
        effect = getattr(item, "effect", None) if item else None
        if isinstance(effect, dict) and effect.get("code"):
            effects.append(dict(effect))
    return pets_combat.Fighter(
        key=str(key), name=record.get("name") or "Существо",
        strength=effective["strength"], health=effective["health"],
        agility=effective["agility"], luck=effective["luck"], armor=effective.get("armor", 0),
        effects=tuple(effects), level=int(record.get("level", 1)),
        skills=_skill_loadout_for(record), shield=_combat_shield_for(record),
        damage_multiplier=damage_multiplier,
    )


def _dungeon_has_healing(record: dict) -> bool:
    healing_effects = {"vampiric", "second_wind", "dodge_heal", "regen", "medkit", "bite", "blood_pact"}
    for code in _skill_loadout_for(record):
        scroll = SCROLLS.scroll(code) if code else None
        if scroll and any(row.get("op") in {"heal", "regen"} for row in scroll.get("effects", ())):
            return True
    return any(
        (getattr(C.find_item(code), "effect", {}) or {}).get("code") in healing_effects
        for code in (record.get("equipped") or {}).values() if code
    )


def _dungeon_has_element(record: dict, element: str) -> bool:
    return any((SCROLLS.scroll(code) or {}).get("element") == element
               for code in _skill_loadout_for(record) if code)


def _dungeon_has_fire_damage(record: dict) -> bool:
    if _dungeon_has_element(record, "fire"):
        return True
    return any(
        (getattr(C.find_item(code), "effect", {}) or {}).get("code") == "burn"
        for code in (record.get("equipped") or {}).values() if code
    )


def enter_dungeon(entry: str, user_id, *, escalator: bool = False) -> tuple[bool, str]:
    if not D.DUNGEON_OPEN:
        return False, D.DUNGEON_CLOSED_NOTICE
    with _farm_settlement_lock:
        data = _load(entry)
        record = _tamed_record(data, user_id)
        if record is None:
            return False, "Сначала приручи существо."
        if _is_farming_record(record, app_now()):
            return False, "Питомец на ферме."
        if _dungeon_active(record):
            return False, "Ты уже в подземелье."
        if _power_rating_for(record) < D.MIN_POWER:
            return False, f"Для подземелья нужно {D.MIN_POWER} силы."
        wallet = _ruby_row(data)
        rubies = max(0, int(wallet.get(str(user_id), 0) or 0))
        if rubies < D.ENTRY_RUBY_COST:
            return False, f"Вход стоит {D.ENTRY_RUBY_COST} рубинов."
        wallet[str(user_id)] = rubies - D.ENTRY_RUBY_COST
        floor = 1
        if escalator:
            floor = max(1, int(record.get("dungeon_deepest", 1)))
            if floor <= 1:
                return False, "Эскалатор откроется, когда доберёшься глубже."
            rubies = max(0, int(wallet.get(str(user_id), 0) or 0))
            if rubies < D.ESCALATOR_RUBY_COST:
                return False, f"Нужно {D.ESCALATOR_RUBY_COST} рубинов."
            wallet[str(user_id)] = rubies - D.ESCALATOR_RUBY_COST
        hero = _dungeon_fighter(record, str(user_id))
        max_hp = round(pets_combat.derive(hero, hero)["max_hp"])
        record["dungeon_run"] = {"floor": floor, "hp": max_hp, "max_hp": max_hp, "cleared": []}
        _save(entry, data)
    return True, f"{'Эскалатор доставил' if escalator else 'Ты вошёл'} на этаж {floor}."


def dungeon_fight(entry: str, user_id, index: int) -> tuple[bool, str, dict | None]:
    """Fight a fixed encounter, recording persistent damage before rewards leave the store."""
    if not D.DUNGEON_OPEN:
        return False, D.DUNGEON_CLOSED_NOTICE, None
    reward = None
    with _farm_settlement_lock:
        data = _load(entry)
        record = _tamed_record(data, user_id)
        run = record.get("dungeon_run") if record else None
        if not isinstance(run, dict):
            return False, "Сначала войди в подземелье.", None
        floor = max(1, int(run.get("floor", 1) or 1))
        row = D.encounter(floor, index)
        cleared = {int(value) for value in run.get("cleared", []) if str(value).isdigit()}
        if row["index"] in cleared:
            return False, "Этот противник уже побеждён.", None
        if row["gimmick"] == "spells_only" and not any(_skill_loadout_for(record)):
            return False, "Призрака можно ранить только свитками.", None
        if row["gimmick"] == "healing_pass" and _dungeon_has_healing(record):
            cleared.add(row["index"])
            run["cleared"] = sorted(cleared)
            reward, result = dict(row["reward"]), None
            message = "Лечение успокоило плачущее дерево. Ты проходишь дальше."
        else:
            fire_bonus = row["gimmick"] == "fire_only" and _dungeon_has_fire_damage(record)
            hero = _dungeon_fighter(record, str(user_id), damage_multiplier=5 if fire_bonus else 1)
            enemy = pets_combat.Fighter(
                key=f"dungeon:{row['code']}", name=row["name"], armor=row["armor"],
                level=row["level"],
                effects=(({"code": "thorns", "value": 50},) if row["gimmick"] == "healing_pass" else ()),
                **row["stats"],
            )
            result = pets_combat.simulate(hero, enemy, seed=secrets.randbits(63))
            player_hp = 0
            for turn in reversed(result.rounds):
                if turn.attacker == str(user_id):
                    player_hp = turn.attacker_hp
                    break
                if turn.defender_hp >= 0:
                    player_hp = turn.defender_hp
                    break
            run["hp"] = min(max(0, int(run.get("hp", 0))), max(0, int(player_hp)))
            if result.winner != str(user_id):
                record["dungeon_run"] = None
                _save(entry, data)
                return False, f"{row['name']} победил. Забег окончен на этаже {floor}.", {"encounter": row, "result": result, "hero": hero, "enemy": enemy}
            if row["gimmick"] == "reincarnate" and not int(run.get("boss_lives", 0) or 0):
                run["boss_lives"] = 1
                _save(entry, data)
                return True, f"{row['name']} возрождается. Добей его ещё раз.", {"encounter": row, "result": result, "hero": hero, "enemy": enemy, "reincarnated": True}
            cleared.add(row["index"])
            run["cleared"] = sorted(cleared)
            reward, message = dict(row["reward"]), f"Побеждён: {row['name']}."
        _apply_xp(record, int(reward["xp"]))
        _save(entry, data)

    economy.grant(entry, user_id, int(reward["gold"]), "pet_dungeon_win")
    dropped = grant_random_drop(entry, user_id, float(reward["item_chance"]), seed=f"dungeon:{floor}:{index}")
    scroll = None
    if reward["scroll_chance"]:
        scroll = grant_scroll_reward(entry, user_id, source=f"dungeon:{floor}:{index}", kind="dungeon", chance=float(reward["scroll_chance"]), pity_after=8)
    return True, message, {"encounter": row, "result": result, "hero": hero if result else None, "enemy": enemy if result else None, "reward": reward, "dropped": dropped, "scroll": scroll}


def dungeon_rest(entry: str, user_id, xp: int) -> tuple[bool, str]:
    if not D.DUNGEON_OPEN:
        return False, D.DUNGEON_CLOSED_NOTICE
    data = _load(entry)
    record = _tamed_record(data, user_id)
    run = record.get("dungeon_run") if record else None
    if not isinstance(run, dict):
        return False, "Сначала войди в подземелье."
    floor = int(run.get("floor", 1) or 1)
    if len(run.get("cleared", [])) < len(D.encounters_for_floor(floor)):
        return False, "Сначала очисти этаж."
    cost = D.shop_heal_cost(floor)
    paid, _balance = economy.spend(entry, user_id, xp, cost, "pet_dungeon_heal")
    if not paid:
        return False, f"На лечение нужно {cost} монет."
    run["hp"] = int(run.get("max_hp", 1))
    _save(entry, data)
    return True, "Лавка подземелья полностью восстановила здоровье."


def dungeon_descend(entry: str, user_id) -> tuple[bool, str]:
    if not D.DUNGEON_OPEN:
        return False, D.DUNGEON_CLOSED_NOTICE
    data = _load(entry)
    record = _tamed_record(data, user_id)
    run = record.get("dungeon_run") if record else None
    if not isinstance(run, dict):
        return False, "Сначала войди в подземелье."
    floor = int(run.get("floor", 1) or 1)
    if len(run.get("cleared", [])) < len(D.encounters_for_floor(floor)):
        return False, "Сначала очисти этаж."
    run["floor"], run["cleared"] = floor + 1, []
    run.pop("boss_lives", None)
    record["dungeon_deepest"] = max(int(record.get("dungeon_deepest", 1)), floor + 1)
    _save(entry, data)
    return True, f"Ты спускаешься на этаж {floor + 1}."


def quit_dungeon(entry: str, user_id) -> tuple[bool, str]:
    data = _load(entry)
    record = _tamed_record(data, user_id)
    if record is None or not _dungeon_active(record):
        return False, "Ты не в подземелье."
    record["dungeon_run"] = None
    _save(entry, data)
    return True, "Ты покинул подземелье."


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

    # A granted debuff scales the finished numbers rather than any one of their parts, so
    # it cannot be dodged by re-equipping and applies identically to a bare pet and a
    # fully geared one. This is the ONLY place it is applied: everything downstream --
    # the fight, the power rating, both pet cards, the leaderboard -- reads its stats
    # through here, so none of them needs to know that debuffs exist.
    scale = debuff_scale(record)
    result = {
        key: max(1, round(
            (stat_levels.get(key, C.STAT_MIN_LEVEL)
             + pet_level * C.PET_LEVEL_STAT_BONUS + bonuses[key]) * scale
        ))
        for key in C.STAT_KEYS
    }
    result["armor"] = max(0, round(armor * scale))
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
            # The power beside it is already the reduced one, so the ranking would
            # otherwise be quietly misreporting a marked creature as simply weaker.
            "debuff": debuff_for(record),
        }
        for user_id, record in data["pets"].items()
        if record.get("name")
    ]
    return sorted(rows, key=lambda row: (-row["power"], row["user_id"]))


# --- inventory & equipment -------------------------------------------------------------


def buy_item(entry, user_id, xp, code) -> tuple[bool, str]:
    data = _load(entry)
    moment = app_now()
    record = _tamed_record(data, user_id)
    if record is None:
        return False, "Сначала приручи существо."
    item = C.find_item(code)
    if item is None:
        return False, "Такого предмета не существует."
    offers = _daily_storefront_items(data, entry, user_id, item.slot, moment)
    offered = next((offer for offer in offers if offer.code == item.code), None)
    if offered is None:
        return False, "Этого предмета сейчас нет на витрине. Она меняется каждые 12 часов."
    if item.code in record["inventory"]:
        return False, f"«{item.name}» у тебя уже есть."
    ok, balance = economy.spend(entry, user_id, xp, offered.price, f"buy:pet_item:{item.code}")
    if not ok:
        return False, f"Нужно {offered.price} монет, у тебя {balance}."
    record["inventory"].append(item.code)
    _discover(record, item.code)
    _save(entry, data)
    return True, f"Куплено: «{item.name}» за {offered.price} монет."


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


FORGE_NEXT_RARITY = {"cursed": "rare", "common": "rare", "rare": "legendary"}
FORGE_REQUIREMENTS = {"cursed": 6, "common": 5, "rare": 7}


def _forge_ingredients(record: dict, rarity: str) -> list:
    equipped = set((record.get("equipped") or {}).values())
    locked = set(record.get("locked_items") or [])
    items = [
        item for item in (C.find_item(code) for code in record.get("inventory", []))
        if item is not None and item.rarity == rarity
        and item.code not in equipped and item.code not in locked
    ]
    # Consume the least valuable candidates first.  The preview shows these exact items,
    # so a strong favourite never disappears merely because it shares a rarity.
    return sorted(items, key=lambda item: (
        C.equipment_score(item), C.resale_value(item), item.code,
    ))


def forge_status(entry: str, user_id) -> dict:
    record = _tamed_record(_load(entry), user_id)
    if record is None:
        return {"recipes": []}
    recipes = []
    for rarity, result_rarity in FORGE_NEXT_RARITY.items():
        ingredients = _forge_ingredients(record, rarity)
        required = FORGE_REQUIREMENTS[rarity]
        recipes.append({
            "rarity": rarity,
            "result_rarity": result_rarity,
            "available": len(ingredients),
            "required": required,
            "ingredients": [item.code for item in ingredients[:required]],
            "can_forge": len(ingredients) >= required,
        })
    return {"recipes": recipes}


def reforge_items(entry: str, user_id, rarity: str, rng=None) -> tuple[bool, str, str | None]:
    """Turn unlocked, unequipped ingredients into one random next-rarity drop."""
    rarity = "common" if rarity == "uncommon" else str(rarity or "")
    result_rarity = FORGE_NEXT_RARITY.get(rarity)
    if result_rarity is None:
        return False, "Эту редкость перековать нельзя.", None
    chooser = rng or random
    with _farm_settlement_lock:
        data = _load(entry)
        record = _tamed_record(data, user_id)
        if record is None:
            return False, "Сначала приручи существо.", None
        ingredients = _forge_ingredients(record, rarity)
        required = FORGE_REQUIREMENTS[rarity]
        if len(ingredients) < required:
            return False, f"Нужно {required} свободных предметов этой редкости. Надетые и защищённые не считаются.", None
        consumed = ingredients[:required]
        owned = set(record.get("inventory", []))
        pool = ([C.find_item("cursed_relic")] if rarity == "cursed" else [
            item for item in C.ITEMS
            if item.source == "drop" and item.rarity == result_rarity
            and item.code not in owned
        ])
        pool = [item for item in pool if item is not None and item.code not in owned]
        if not pool:
            return False, "Подходящие новые предметы этой редкости закончились.", None
        result = chooser.choice(pool)
        inventory = record.setdefault("inventory", [])
        for item in consumed:
            inventory.remove(item.code)
        inventory.append(result.code)
        _discover(record, result.code)
        _metric_add(data, "forges")
        _save(entry, data)
    names = ", ".join(f"«{item.name}»" for item in consumed)
    return True, f"Перековано: {names}. Получено: «{result.name}»!", result.code


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
    if _dungeon_active(record):
        return False, "Снаряжение можно менять только между этажами подземелья."
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
    if _dungeon_active(record):
        return False, "Снаряжение можно менять только между этажами подземелья."
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


def daily_allowance(entry, user_id, today=None) -> int:
    """Compatibility accessor for the maximum accumulated-fight capacity."""
    return fight_allowance_breakdown(entry, user_id, today)["capacity"]


def _fight_bank_components(entry, user_id, record: dict, now: datetime) -> tuple[int, int, int, int, int]:
    """Return current (capacity, cage, farm, paint, recent painting count)."""
    cage = min(max(int(record.get("cage_level", 1) or 1), 1), C.CAGE_MAX_LEVEL)
    farm = max(0, int(record.get("farm_level", 0) or 0))
    recent_figurines = stats.recent_figurine_fight_bonus_count(
        entry, user_id, now.date(), C.RECENT_FIGURINE_FIGHT_BUFF_DAYS,
    )
    cage_bonus = C.CAGE_BONUS_FIGHTS[cage - 1]
    farm_bonus = farm // C.FARM_LEVELS_PER_FIGHT
    paint_bonus = recent_figurines * C.FIGHTS_PER_RECENT_FIGURINE
    return (
        C.daily_fight_allowance(cage, farm, recent_figurines),
        cage_bonus, farm_bonus, paint_bonus, recent_figurines,
    )


def fight_allowance_breakdown(entry, user_id, today=None, now: datetime | None = None) -> dict:
    """Public, display-ready state of the accumulated arena-fight bank.

    ``today`` is accepted for old callers but does not influence the bank.  The paint
    component remains live-derived, so posts grant capacity immediately and expiry only
    reduces capacity (never yields a duplicate credit).
    """
    now = now or app_now()
    data = _load(entry)
    record = _tamed_record(data, user_id)
    if record is None:
        return {
            "allowance": 0, "capacity": 0, "available": 0, "fights_left": 0, "bank": 0,
            "base": 0, "cage_bonus": 0, "farm_bonus": 0, "paint_bonus": 0,
            "recent_figurines": 0, "seconds_until_next": None, "next_fight_at": None,
        }
    capacity, cage_bonus, farm_bonus, paint_bonus, recent_figurines = _fight_bank_components(
        entry, user_id, record, now,
    )
    bank, checkpoint, changed = _settle_fight_bank(record, capacity, now)
    if changed:
        _save(entry, data)
    if bank >= capacity:
        seconds_until_next = None
        next_fight_at = None
    else:
        elapsed = max(0.0, (now - checkpoint).total_seconds())
        remainder = elapsed % C.FIGHT_BANK_RECHARGE_SECONDS
        seconds_until_next = max(1, int(C.FIGHT_BANK_RECHARGE_SECONDS - remainder + 0.999999))
        next_fight_at = (checkpoint + timedelta(seconds=C.FIGHT_BANK_RECHARGE_SECONDS)).isoformat()
    return {
        # `allowance` stays as a UI-friendly alias while old callers move to capacity.
        "allowance": capacity, "capacity": capacity, "available": bank,
        "fights_left": bank, "bank": bank,
        "base": C.BASE_FIGHT_BANK_CAPACITY,
        "cage_bonus": cage_bonus,
        "farm_bonus": farm_bonus,
        "paint_bonus": paint_bonus,
        "recent_figurines": recent_figurines,
        "seconds_until_next": seconds_until_next,
        "next_fight_at": next_fight_at,
    }


def fights_left(entry, user_id, today=None, now: datetime | None = None) -> int:
    """Current whole fights in the member's settled bank."""
    return fight_allowance_breakdown(entry, user_id, today, now)["available"]


def _spend_arena_fight(record: dict, capacity: int, now: datetime) -> None:
    """Settle and reserve one fight inside the same saved arena transaction."""
    bank, _, _ = _settle_fight_bank(record, capacity, now)
    if bank <= 0:
        raise ValueError("No accumulated arena fights available.")
    record["fight_bank"] = bank - 1


def claim_duel(entry, user_id, opponent_id, now=None) -> tuple[bool, str]:
    """Atomically reserve one public duel, including its once-per-target daily limit.

    Only the CHALLENGER's own farm status is a block: a farming pet still cannot start a
    fight (see the module-level note on _is_farming_record call sites), but it is a normal,
    attackable target for somebody else's duel like it is for the arena.
    """
    now = now or app_now()
    data = _load(entry)
    uid, opponent_uid = str(user_id), str(opponent_id)
    if _is_farming_record(_tamed_record(data, uid), now):
        return False, "Питомец сейчас работает на ферме и не может драться."
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
    """Whether `attacker_id` may spend an arena fight on `defender_id` today.

    Only the ATTACKER's farm status gates this: a farming pet cannot start a fight, but is
    an ordinary, attackable defender -- the reason weapons used to be excluded from farm
    drops was a reservation race, not a claim that farming should be a hiding place.
    """
    day = day or today()
    data = _load(entry)
    if _is_farming_record(_tamed_record(data, attacker_id)):
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


def auto_equip_mirror(entry, attacker_id, defender_id) -> str | None:
    """Put Зеркало души on before punching a long way down. Returns the code if it went on.

    Commissioned as automatic, and it has to be: the whole item exists so a strong pet can
    fight a weak one fairly, and an amulet you must remember to swap in beforehand would
    be worn by the people who least need reminding. Anybody who owns it and attacks
    somebody MIRROR_LEVEL_GAP or more levels below gets it equipped for them.

    Whatever was in the amulet slot is remembered on the record and put back by
    `restore_after_mirror` once the fight is recorded -- an automatic swap that silently
    kept the player's real amulet off would be a bug that only shows up as lost fights
    later. Nothing happens at all for a player who does not own the mirror.
    """
    with _farm_settlement_lock:
        data = _load(entry)
        attacker = _tamed_record(data, attacker_id)
        defender = _tamed_record(data, defender_id)
        if attacker is None or defender is None:
            return None
        gap = int(defender.get("level", 1) or 1) - int(attacker.get("level", 1) or 1)
        if gap > -C.MIRROR_LEVEL_GAP:
            return None
        if MIRROR_AMULET_CODE not in attacker.get("inventory", []):
            return None
        equipped = attacker.setdefault("equipped", {})
        if equipped.get("amulet") == MIRROR_AMULET_CODE:
            return None
        attacker["mirror_restore"] = equipped.get("amulet")
        equipped["amulet"] = MIRROR_AMULET_CODE
        _save(entry, data)
    return MIRROR_AMULET_CODE


def restore_after_mirror(entry, user_id) -> bool:
    """Put back whatever the automatic mirror swap displaced. True if something moved."""
    with _farm_settlement_lock:
        data = _load(entry)
        record = _tamed_record(data, user_id)
        if record is None or "mirror_restore" not in record:
            return False
        previous = record.pop("mirror_restore")
        equipped = record.setdefault("equipped", {})
        if equipped.get("amulet") == MIRROR_AMULET_CODE:
            # Only if it is still ours to put back -- a player who changed their own
            # amulet mid-fight gets to keep that choice.
            equipped["amulet"] = previous if previous in record.get("inventory", []) else None
        _save(entry, data)
    return True


def auto_equip_mob_gear(entry, user_id) -> tuple[str, ...]:
    """Temporarily wear every owned anti-mob item before a PVE fight.

    This deliberately mirrors :func:`auto_equip_mirror`: PVE utility should work for a
    player who bought the item, without making them remember an inventory swap, but it
    must not silently replace their normal arena setup after the result is shown.
    """
    with _farm_settlement_lock:
        data = _load(entry)
        record = _tamed_record(data, user_id)
        if record is None:
            return ()
        equipped = record.setdefault("equipped", {})
        restore = {}
        equipped_codes = []
        for slot, code in MOB_GEAR_CODES.items():
            if code not in record.get("inventory", []) or equipped.get(slot) == code:
                continue
            restore[slot] = equipped.get(slot)
            equipped[slot] = code
            equipped_codes.append(code)
        if not restore:
            return ()
        record["mob_gear_restore"] = restore
        _save(entry, data)
    return tuple(equipped_codes)


def restore_after_mob_gear(entry, user_id) -> bool:
    """Restore the exact weapon/amulet displaced by :func:`auto_equip_mob_gear`."""
    with _farm_settlement_lock:
        data = _load(entry)
        record = _tamed_record(data, user_id)
        if record is None or "mob_gear_restore" not in record:
            return False
        restore = record.pop("mob_gear_restore")
        equipped = record.setdefault("equipped", {})
        restored = False
        if isinstance(restore, dict):
            for slot, previous in restore.items():
                code = MOB_GEAR_CODES.get(slot)
                # Do not overwrite a deliberate change made while the result was being
                # sent; the auto swap restores only the item it itself still wears.
                if code and equipped.get(slot) == code:
                    equipped[slot] = previous if previous in record.get("inventory", []) else None
                    restored = True
        _save(entry, data)
    return restored


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
        # A farming pet is a normal defender: it cannot start a fight itself (that is what
        # `attackable_only`'s can_attack_in_arena check enforces for the SEEKER), but it is
        # not removed from the pool of pets somebody else can be shown as an opponent.
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


def legendary_pity_progress(entry: str, user_id) -> dict:
    """Progress toward the next guaranteed legendary drop for this pet.

    Only wins while this player still has an unowned drop-only legendary design are
    eligible. Other players owning the same design no longer exhaust the guarantee.
    """
    data = _load(entry)
    record = _tamed_record(data, user_id)
    threshold = C.LEGENDARY_PITY_ELIGIBLE_WINS
    if record is None:
        return {
            "eligible": False, "wins_without_legend": 0,
            "remaining_wins": threshold, "threshold": threshold,
        }
    owned = set(record.get("inventory", []))
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
    now: datetime | None = None,
) -> dict:
    moment = now or app_now()
    data = _load(entry)
    attacker_uid, defender_uid = str(attacker_id), str(defender_id)
    attacker = data["pets"][attacker_uid]
    defender = data["pets"][defender_uid]
    # Only the ATTACKER's farm status is a hard stop here. This is the last-line safety net
    # behind can_attack_in_arena/claim_duel's own attacker-only check (a stale UI tap could
    # otherwise slip through); the defender being away farming is not a reason to block --
    # it is a normal, attackable target now, same as everywhere else in the arena.
    if _is_farming_record(attacker):
        raise ValueError("Питомец на ферме и не может участвовать в бою.")

    # Only the attacker spends an accumulated fight.  This happens inside the same
    # state mutation as rewards/history, so an exhausted stale callback cannot mint a
    # result even if it passed an earlier UI check.
    capacity, *_ = _fight_bank_components(entry, attacker_uid, attacker, moment)
    _spend_arena_fight(attacker, capacity, moment)
    attacker["fights"] = attacker.get("fights", 0) + 1
    defender["fights"] = defender.get("fights", 0) + 1

    is_draw = bool(getattr(result, "is_draw", False))
    if is_draw:
        _, attacker_levels_gained = _apply_xp(attacker, C.DRAW_XP)
        _, defender_levels_gained = _apply_xp(defender, C.DRAW_XP)
        data["fights"].append({
            "ts": moment.isoformat(),
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
            "consolation_gold": 0,
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
            "consolation_gold": 0,
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
    # Зеркало души: "не уменьшает добычу за победу". The wearer has already given up
    # their stats to make the fight fair, so the arena stops docking them for the level
    # gap as well -- that double penalty is the exact reason nobody punches downward.
    # Clamped at 1.0 rather than replaced by it: the mirror removes a penalty, it does
    # not also hand out the bonus for punching UP.
    if _equipped_effect(winner, "mirror_soul"):
        reward_multiplier = max(1.0, reward_multiplier)
    gold = round(
        random.randint(C.WIN_GOLD_MIN, C.WIN_GOLD_MAX)
        * (1 + bonus_pct / 100)
        * reward_multiplier
    )
    # The coin-rake fantasy is "coins fly out on every hit", but directly charging an
    # offline defender would break the arena's no-loss guarantee and invite farming one
    # victim. The arena therefore adds the capped amount to the winner's purse. Only
    # ordinary landed attacks count; effect log rows and dodges do not.
    coin_rake = _equipped_effect(winner, "coin_rake")
    if coin_rake:
        landed = sum(
            1 for blow in getattr(result, "rounds", ())
            if str(getattr(blow, "attacker", "")) == winner_uid
            and int(getattr(blow, "number", 0) or 0) > 0
            and int(getattr(blow, "damage", 0) or 0) > 0
            and not str(getattr(blow, "event", "")).startswith("amulet_")
        )
        try:
            per_hit = max(0, int(coin_rake.get("value", 1) or 0))
            cap = max(0, int(coin_rake.get("cap", 5) or 0))
        except (TypeError, ValueError):
            per_hit, cap = 0, 0
        gold += min(cap, landed * per_hit)
    economy.grant(entry, winner_uid, gold, "pet_fight_win")
    _metric_add(data, "arena_reward_gold", gold)

    # Only the ATTACKER pays a penalty for losing -- they are the one who pressed "напасть".
    # A losing DEFENDER never chose this fight (opponents are dealt out of the power window,
    # and a farming pet is now a valid target too), so they pay nothing and instead receive
    # a small consolation minted onto their balance. See LOSS_GOLD_SHARE and
    # DEFENDER_CONSOLATION_SHARE in pets_config.py for why the two are different numbers.
    attacker_lost = loser_uid == attacker_uid
    paid = 0
    consolation = 0
    if attacker_lost:
        # Charged as a spend rather than a negative grant so it lands in the same ledger
        # column as every other purchase -- and clamped to what they actually hold, because
        # economy.balance floors at zero and a debt nobody can see the cause of is worse
        # than a bill that got rounded down.
        penalty = C.loss_gold_for(gold)
        # «Последний чек» keeps part of the loser's coins. It changes only the amount paid;
        # the winner's already-calculated reward is never reduced by somebody else's gear.
        survivor_share = _effect_fraction(_equipped_effect(loser, "survivor"))
        if survivor_share:
            penalty = max(0, round(penalty * (1 - min(1.0, survivor_share))))
        if penalty > 0:
            # Prefer the caller's own figure: it is the live, today-inclusive XP
            # economy.balance wants, whereas _chat_xp_for can only see closed days. Only
            # the attacker can reach this branch (loser_uid == attacker_uid was just
            # checked above), so attacker_xp is always the right source when supplied; the
            # fallback covers a caller that omitted it (most tests do).
            loser_xp = attacker_xp if attacker_xp is not None else _chat_xp_for(entry, loser_uid)
            affordable = min(penalty, economy.balance(entry, loser_uid, loser_xp))
            if affordable > 0:
                ok, _ = economy.spend(
                    entry, loser_uid, loser_xp, affordable, "pet_fight_loss", ref=winner_uid
                )
                paid = affordable if ok else 0
    else:
        # The defender branch needs no XP lookup at all: economy.grant only credits a
        # balance, unlike economy.spend/economy.balance it never has to read one first.
        # That is also why this never needed the `_chat_xp_for` fallback the old shared
        # penalty path carried for a defender it could not get live XP for.
        consolation = C.defender_consolation_for(gold)
        if consolation > 0:
            economy.grant(entry, loser_uid, consolation, "pet_fight_defender_consolation")

    dropped_code = None
    # A player's bag never receives the same code twice, but item designs are shared
    # between players. The pity counter is tied to wins, not merely successful rolls.
    owned_codes = set(winner.get("inventory", []))
    drop_pool = [
        item for item in C.ITEMS
        if item.source == "drop" and item.code not in owned_codes
    ]
    # The 500-win pity contract is specifically for legendary weapons. New
    # legendary amulets/boots/gloves remain exciting normal rolls, not substitutes for
    # the promised weapon.
    legendary_pool = [
        item for item in drop_pool if item.slot == "weapon" and item.rarity == "legendary"
    ]
    pity_before = max(0, int(winner.get("legendary_pity_wins", 0) or 0))
    item_pity_before = max(0, int(winner.get("item_pity_wins", 0) or 0))
    force_legendary = bool(legendary_pool) and (
        pity_before + 1 >= C.LEGENDARY_PITY_ELIGIBLE_WINS
    )
    force_item = bool(drop_pool) and item_pity_before + 1 >= C.ITEM_PITY_ELIGIBLE_WINS
    dropped = None
    auto_equipped = False
    collector_bonus = _effect_fraction(_equipped_effect(winner, "collector"))
    compass_bonus = _effect_fraction(_equipped_effect(winner, "trophy_compass")) \
        if int(winner.get("level", 1) or 1) < int(loser.get("level", 1) or 1) else 0.0
    # Only the winner rolls, so it is the winner's luck that pays -- the same pet whose
    # luck already bought the crits that probably won the fight.
    luck_bonus = C.luck_drop_multiplier(
        (winner.get("stats") or {}).get("luck", C.STAT_MIN_LEVEL)
    )
    drop_chance = min(1.0, C.DROP_CHANCE * (1 + collector_bonus + compass_bonus) * luck_bonus)
    if force_legendary:
        dropped = random.choice(legendary_pool)
    elif drop_pool and (force_item or random.random() < drop_chance):
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
            0 if dropped is not None and dropped.slot == "weapon"
            and dropped.rarity == "legendary" else pity_before + 1
        )
    winner["item_pity_wins"] = 0 if dropped is not None or not drop_pool else item_pity_before + 1

    winner_xp = max(1, round(C.WIN_XP * reward_multiplier))
    _, winner_levels_gained = _apply_xp(winner, winner_xp)
    _, loser_levels_gained = _apply_xp(loser, C.LOSS_XP)

    # Names/owners are snapshotted INTO the log entry rather than looked up when
    # history() is read, so a later rename does not rewrite what already happened.
    data["fights"].append({
        "ts": moment.isoformat(),
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
        # number rather than recomputing an amount that was never charged. Exactly one of
        # loss_gold/consolation_gold can be non-zero: an attacker-loser pays, a
        # defender-loser is paid, never both on the same fight.
        "loss_gold": paid,
        "consolation_gold": consolation,
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
        # An attacker never gets a consolation -- only a losing defender does, below. Kept
        # as an explicit field (not a sign-flipped loss_gold) so a reader can tell "paid a
        # penalty" and "was minted a consolation" apart instead of inferring it from sign.
        "consolation_gold": 0,
        "xp": winner_xp if attacker_won else C.LOSS_XP,
        "levels_gained": winner_levels_gained if attacker_won else loser_levels_gained,
        "level": attacker.get("level", 1),
        "dropped_item": dropped_code if attacker_won else None,
        "auto_equipped": auto_equipped if attacker_won else False,
        "opponent_gold": gold if not attacker_won else 0,
        # A losing defender never pays anymore -- see opponent_consolation_gold for what
        # they receive instead. Left in place (rather than dropped) so any reader still
        # asking "did the opponent lose money" gets a correct zero, not a missing key.
        "opponent_loss_gold": 0,
        "opponent_consolation_gold": consolation if attacker_won else 0,
        "opponent_xp": C.LOSS_XP if attacker_won else winner_xp,
        "opponent_levels_gained": loser_levels_gained if attacker_won else winner_levels_gained,
        "opponent_level": defender.get("level", 1),
        "opponent_dropped_item": dropped_code if not attacker_won else None,
        "opponent_auto_equipped": auto_equipped if not attacker_won else False,
    }


# --- PVE ------------------------------------------------------------------------------


def _pve_window(moment: datetime) -> int:
    """Which fixed 8-hour block of the chat's clock `moment` falls in.

    Derived from the wall clock rather than counted from each player's first fight, which
    is what makes the reset simultaneous for everybody: the block boundaries are 00:00,
    08:00 and 16:00 local for every member at once. Anchored on the local DATE so a
    timezone with a non-zero UTC offset still breaks at midnight rather than at 03:00.
    """
    return (moment.date().toordinal() * 24 + moment.hour) // C.PVE_WINDOW_HOURS


def _pve_window_end(moment: datetime) -> datetime:
    """The moment this window closes and everybody's PVE attacks come back."""
    block_start_hour = (moment.hour // C.PVE_WINDOW_HOURS) * C.PVE_WINDOW_HOURS
    start = moment.replace(hour=block_start_hour, minute=0, second=0, microsecond=0)
    return start + timedelta(hours=C.PVE_WINDOW_HOURS)


# ------------------------------------------------------------------------------- sprites
# Which kind of thing the creature's photograph actually shows -- a dog, a robot, a ghost.
# The battle screen animates the photo, and the idle it plays has to match the subject or
# the whole effect reads as a picture wobbling rather than a creature standing there.
#
# Derived once per PHOTOGRAPH, never per fight: it costs a vision call, and the answer
# cannot change while the picture does not. The row records which photo it came from, so
# a new picture reads as a cache miss rather than as a stale answer about the old one
# (see pets_sprite.cached_archetype). Storage here, the decision itself in pets_sprite.


def sprite_archetype(entry, user_id) -> str | None:
    """The remembered archetype for this creature's CURRENT photo, or None if unknown."""
    return SPRITE.cached_archetype(_tamed_record(_load(entry), str(user_id)))


def remember_sprite(entry, user_id, archetype_code: str) -> str:
    """Store an archetype against the picture it was derived from.

    Re-reads the record inside the lock rather than trusting the caller's snapshot: the
    classification happens off the event loop and takes seconds, and the player may well
    have changed their picture in the meantime. Writing the new code against a photo it
    was not derived from would cache a wrong answer that nothing would ever invalidate.
    """
    data = _load(entry)
    record = _tamed_record(data, str(user_id))
    if record is None:
        return SPRITE.DEFAULT_ARCHETYPE
    record[SPRITE.SPRITE_KEY] = SPRITE.sprite_row(
        archetype_code, record.get("photo_file_id"), app_now().isoformat(),
    )
    _save(entry, data)
    return str(record[SPRITE.SPRITE_KEY]["archetype"])


# ------------------------------------------------------------------------------ debuffs
# A mark an admin hands out by name (see C.DEBUFFS). It lives on the CREATURE'S record
# rather than in a chat-level row, which is what lets `_effective_stats_for` apply it
# without any caller passing the store around -- and therefore what makes it show up in
# the fight, the power rating and every card at once.
#
# Whether it is still in force is DERIVED, never stored: the row keeps the picture the
# creature wore when the mark was given, and the mark is simply not active once the
# picture is a different one. Nothing has to notice the change, so no upload path can
# forget to lift it and no cron has to run. The row stays behind afterwards as a record
# of what happened; only `_debuff_active` decides whether it bites.
DEBUFF_KEY = "debuff"


def _debuff_row(record: dict | None) -> dict:
    row = (record or {}).get(DEBUFF_KEY)
    return row if isinstance(row, dict) else {}


def _debuff_active(record: dict | None) -> bool:
    row = _debuff_row(record)
    spec = C.debuff_spec(row.get("code"))
    if spec is None:
        return False
    if spec.get("clears_on") == "photo":
        # `None` on both sides is a creature that had no picture and still has none --
        # squarely the case the mark is about, so that counts as unchanged.
        return row.get("photo_file_id") == (record or {}).get("photo_file_id")
    return True


def debuff_scale(record: dict | None) -> float:
    """The multiplier this creature's stats are under. 1.0 for almost everybody."""
    if not _debuff_active(record):
        return 1.0
    spec = C.debuff_spec(_debuff_row(record).get("code")) or {}
    try:
        scale = float(spec.get("scale", 1.0))
    except (TypeError, ValueError):
        return 1.0
    return max(C.DEBUFF_STAT_SCALE_FLOOR, min(1.0, scale))


def debuff_for(record: dict | None) -> dict | None:
    """One creature's active mark as display data, or None. Never touches the store.

    Returns the catalogue copy alongside the row, because every screen that shows the
    mark has to show what it is and how to get rid of it -- a bare "🎭 Самозванец" with a
    stat penalty behind it and no explanation is indistinguishable from a bug.
    """
    if not _debuff_active(record):
        return None
    row = _debuff_row(record)
    spec = C.debuff_spec(row.get("code")) or {}
    return {
        "code": row.get("code"),
        "emoji": spec.get("emoji", "•"),
        "title": spec.get("title", ""),
        "line": spec.get("line", ""),
        "description": spec.get("description", ""),
        "hint": spec.get("hint", ""),
        "percent": round((1 - debuff_scale(record)) * 100),
        "set_at": row.get("set_at"),
        "set_by": row.get("set_by"),
    }


def debuff(entry, user_id) -> dict | None:
    """The active mark on one player's creature, or None."""
    return debuff_for(_tamed_record(_load(entry), str(user_id)))


def set_debuff(entry, user_id, code, *, set_by=None) -> dict:
    """Hand a mark to one player. Re-granting the same code re-arms it on today's picture.

    Re-arming is deliberate: an admin who gives the same mark twice means "this picture is
    still not good enough", and without re-snapshotting, the second grant would land
    already-expired against the picture the first one recorded.
    """
    user_id = str(user_id or "").strip()
    spec = C.debuff_spec(code)
    if not user_id:
        raise ValueError("Не выбран игрок.")
    if spec is None:
        raise ValueError("Такого эффекта нет.")
    data = _load(entry)
    record = _tamed_record(data, user_id)
    if record is None:
        raise ValueError("У этого игрока нет существа.")
    record[DEBUFF_KEY] = {
        "code": str(code),
        "photo_file_id": record.get("photo_file_id"),
        "set_by": str(set_by or ""),
        "set_at": app_now().isoformat(),
    }
    _save(entry, data)
    return debuff_for(record) or {}


def clear_debuff(entry, user_id) -> bool:
    """Take a mark off by hand. Returns whether there was one on the creature."""
    data = _load(entry)
    record = _tamed_record(data, str(user_id))
    if record is None or not _debuff_row(record):
        return False
    record.pop(DEBUFF_KEY, None)
    _save(entry, data)
    return True


def debuff_holders(entry) -> list[dict]:
    """Everyone currently carrying an active mark, for the admin screen."""
    data = _load(entry)
    rows = []
    for user_id, record in (data.get("pets") or {}).items():
        mark = debuff_for(record) if isinstance(record, dict) else None
        if mark is None:
            continue
        rows.append({
            "user_id": str(user_id),
            "owner_name": record.get("owner_name") or "кто-то",
            "owner_username": record.get("owner_username"),
            "pet_name": record.get("name"),
            **mark,
        })
    return sorted(rows, key=lambda row: str(row["owner_name"]).lower())


# ---------------------------------------------------------------------------- birthdays
# One person a day sits at the top of the arena with a Поздравить button instead of an
# attack one. Stored with the DATE it is for, never as a bare switch: an admin sets it and
# forgets it, and a dated row retires itself at midnight instead of paying a stale
# celebrant for a fortnight.
BIRTHDAY_KEY = "birthday"


def _birthday_row(data: dict) -> dict:
    row = data.setdefault(BIRTHDAY_KEY, {})
    if not isinstance(row, dict):
        row = data[BIRTHDAY_KEY] = {}
    if not isinstance(row.get("greeted"), dict):
        row["greeted"] = {}
    return row


def _birthday_active(row: dict, day: date | None = None) -> bool:
    return bool(str(row.get("user_id") or "")) \
        and str(row.get("date") or "") == (day or today()).isoformat()


def birthday(entry, day: date | None = None, viewer=None) -> dict | None:
    """Today's celebrant, or None. Reading never mutates the store.

    `viewer` adds the two facts every screen needs and neither should work out for
    itself: whether the person looking IS the celebrant, and whether they have already
    sent their greeting. Both clients render from these rather than re-deriving them.
    """
    data = _load(entry)
    row = _birthday_row(data)
    if not _birthday_active(row, day):
        return None
    celebrant = str(row["user_id"])
    record = _tamed_record(data, celebrant)
    greeted = row.get("greeted") or {}
    return {
        "user_id": celebrant,
        "date": row.get("date"),
        "greeted_count": len(greeted),
        "greeted_by": sorted(str(key) for key in greeted),
        "owner_name": (record or {}).get("owner_name") or row.get("owner_name") or "именинник",
        "owner_username": (record or {}).get("owner_username") or row.get("owner_username"),
        "pet_name": (record or {}).get("name"),
        "has_pet": record is not None,
        "is_me": viewer is not None and str(viewer) == celebrant,
        "greeted": viewer is not None and str(viewer) in greeted,
    }


def set_birthday(entry, user_id, *, day: date | None = None, set_by=None,
                 owner_name: str | None = None, owner_username: str | None = None) -> dict:
    """Put somebody at the top of the arena for one day.

    Re-setting the SAME person on the same day keeps the greeting log, so an admin who
    fixes a typo in the name does not reopen the day for everyone who already paid their
    respects. Naming anybody else -- or another date -- starts a fresh log, because it is
    a different celebration.
    """
    user_id = str(user_id or "").strip()
    if not user_id:
        raise ValueError("Не выбран именинник.")
    day = day or today()
    data = _load(entry)
    row = _birthday_row(data)
    same = str(row.get("user_id") or "") == user_id and str(row.get("date") or "") == day.isoformat()
    record = _tamed_record(data, user_id)
    data[BIRTHDAY_KEY] = {
        "user_id": user_id,
        "date": day.isoformat(),
        "set_by": str(set_by or ""),
        "set_at": app_now().isoformat(),
        "owner_name": owner_name or (record or {}).get("owner_name") or "именинник",
        "owner_username": owner_username or (record or {}).get("owner_username"),
        "greeted": dict(row.get("greeted") or {}) if same else {},
    }
    _save(entry, data)
    return birthday(entry, day) or {}


def clear_birthday(entry) -> bool:
    """Take the celebration down early. Returns whether there was one to take down."""
    data = _load(entry)
    row = _birthday_row(data)
    if not str(row.get("user_id") or ""):
        return False
    data[BIRTHDAY_KEY] = {"greeted": {}}
    _save(entry, data)
    return True


def congratulate(entry, user_id, day: date | None = None) -> dict:
    """Congratulate today's celebrant. Pays both of them a win's worth, once per person.

    Idempotent on the greeter: a second tap returns the first receipt instead of paying
    again, so a double-click or a retried request cannot mint a second purse. The reward
    is a win's gold and xp for BOTH sides, exactly as commissioned -- the greeter is paid
    for showing up and the celebrant is paid per well-wisher.

    Costs no arena fight. A birthday is not a duel, and making it compete with the fight
    bank would mean choosing between congratulating somebody and playing.
    """
    user_id = str(user_id)
    day = day or today()
    with _farm_settlement_lock:
        data = _load(entry)
        row = _birthday_row(data)
        if not _birthday_active(row, day):
            raise ValueError("Сегодня никто не празднует.")
        celebrant = str(row["user_id"])
        if celebrant == user_id:
            raise ValueError("Себя поздравлять не нужно — это все остальные к тебе.")
        prior = (row.get("greeted") or {}).get(user_id)
        if isinstance(prior, dict):
            return {**prior, "already": True}

        greeter_record = _tamed_record(data, user_id)
        celebrant_record = _tamed_record(data, celebrant)
        # Each side rolls its own purse, the same way two arena wins would.
        gold = random.randint(C.WIN_GOLD_MIN, C.WIN_GOLD_MAX)
        celebrant_gold = random.randint(C.WIN_GOLD_MIN, C.WIN_GOLD_MAX)
        economy.grant(entry, user_id, gold, "pet_birthday_greeting")
        economy.grant(entry, celebrant, celebrant_gold, "pet_birthday_greeted")
        _metric_add(data, "arena_reward_gold", gold + celebrant_gold)
        # Gold lands on the person; xp lands on the creature, so somebody who has not
        # tamed one yet still gets paid rather than being turned away at the button.
        xp = C.WIN_XP if greeter_record is not None else 0
        celebrant_xp = C.WIN_XP if celebrant_record is not None else 0
        if greeter_record is not None:
            _apply_xp(greeter_record, xp)
        if celebrant_record is not None:
            _apply_xp(celebrant_record, celebrant_xp)

        receipt = {
            "already": False,
            "celebrant": celebrant,
            "celebrant_name": row.get("owner_name") or "именинник",
            "greeter": user_id,
            "greeter_name": (greeter_record or {}).get("owner_name") or "кто-то",
            "gold": gold, "xp": xp,
            "celebrant_gold": celebrant_gold, "celebrant_xp": celebrant_xp,
            "ts": app_now().isoformat(),
        }
        row["greeted"][user_id] = receipt
        _remember_birthday_notification(data, celebrant, receipt)
        _save(entry, data)
        return receipt


def _remember_birthday_notification(data: dict, celebrant: str, receipt: dict) -> None:
    """The celebrant's copy, kept in the store so a closed DM cannot lose the news."""
    rows = data.setdefault("birthday_notifications", [])
    if not isinstance(rows, list):
        rows = data["birthday_notifications"] = []
    rows.append({
        "user_id": str(celebrant),
        "greeter": receipt.get("greeter"),
        "greeter_name": receipt.get("greeter_name"),
        "gold": receipt.get("celebrant_gold"),
        "xp": receipt.get("celebrant_xp"),
        "ts": receipt.get("ts"),
    })
    data["birthday_notifications"] = rows[-400:]


def pve_allowance(entry, user_id, now: datetime | None = None) -> dict:
    """PVE attacks left, and when the whole server gets them back.

    Deliberately NOT the arena's fight bank. The arena trickles one fight back an hour per
    player; this is a flat ten that everybody loses and regains together, so PVE is
    something you sit down to rather than something you top up between messages.
    """
    moment = now or app_now()
    record = _tamed_record(_load(entry), user_id)
    used = 0
    if record is not None and int(record.get("pve_window", -1) or -1) == _pve_window(moment):
        used = max(0, int(record.get("pve_used", 0) or 0))
    ends = _pve_window_end(moment)
    return {
        "available": max(0, C.PVE_ATTACKS_PER_WINDOW - used),
        "capacity": C.PVE_ATTACKS_PER_WINDOW,
        "used": used,
        "resets_at": ends.isoformat(),
        "seconds_until_reset": max(0, int((ends - moment).total_seconds())),
        "window_hours": C.PVE_WINDOW_HOURS,
    }


def _spend_pve_fight(record: dict, moment: datetime) -> None:
    """Consume one PVE attack, rolling the counter over into a new window if needed."""
    window = _pve_window(moment)
    if int(record.get("pve_window", -1) or -1) != window:
        record["pve_window"] = window
        record["pve_used"] = 0
    record["pve_used"] = max(0, int(record.get("pve_used", 0) or 0)) + 1


def roll_mob(entry, user_id, rng=None) -> dict | None:
    """Deal one mob at one tier, scaled against THIS player. None without a pet.

    Both the mob and its tier are drawn at random, as commissioned -- there is no picking
    a soft target off a list. The stat block is generated from the seeker's own effective
    stats (see pets_mobs.TIER_SCALING), so a mob is a real fight at level 3 and still a
    real fight at level 40 instead of being outgrown.

    Nothing is stored. The returned block is handed straight to `fight_mob`, which rolls
    its own fresh one -- a mob a player could re-roll and then bank would be a mob they
    could shop for.
    """
    rng = rng or secrets.SystemRandom()
    record = _tamed_record(_load(entry), user_id)
    if record is None:
        return None
    mob = rng.choice(list(M.MOBS))
    tier = rng.choice(list(M.TIERS))
    return _mob_block(entry, user_id, record, mob, tier, rng)


def mob_block(entry, user_id, code: str, tier: str, rng=None) -> dict | None:
    """Rebuild a named mob at a named tier, server-side.

    The client is handed a mob and hands one back; this is what makes that safe. Only the
    CODE and the TIER survive the round trip -- every stat is generated here from the
    player's own numbers, so a hand-edited block is just a request for a different fight,
    not a request for an easier one.
    """
    rng = rng or secrets.SystemRandom()
    mob = M.find_mob(code)
    record = _tamed_record(_load(entry), user_id)
    if mob is None or record is None or tier not in M.TIERS:
        return None
    return _mob_block(entry, user_id, record, mob, tier, rng)


def _mob_block(entry, user_id, record: dict, mob, tier: str, rng) -> dict:
    scale, spread = M.TIER_SCALING[tier]
    mine = effective_stats(entry, user_id)
    # Scale the *whole combat profile* once.  The old implementation gave every stat
    # its own high/low roll, so a hard mob could luck into a full set of peaks at once.
    # Power has a fixed base, therefore only its variable part is scaled; otherwise the
    # base would make low-level mobs look much tougher than their tier promises.
    profile_jitter = 1.0 + rng.uniform(-spread, spread)
    mine_power = _power_from(mine, mine.get("armor", 0))
    variable_power = max(1, mine_power - C.POWER_RATING_BASE)
    target_variable_power = max(0, round(variable_power * scale * profile_jitter))
    profile_scale = target_variable_power / variable_power
    stats_out = {}
    for key in C.STAT_KEYS:
        stats_out[key] = max(0, round(mine.get(key, 0) * profile_scale))
    armor = max(0, round(mine.get("armor", 0) * profile_scale))
    return {
        "code": mob.code, "name": mob.name, "flavour": mob.flavour, "taunt": mob.taunt,
        "tier": tier, "tier_name": M.TIER_NAMES[tier],
        "stats": stats_out,
        "armor": armor,
        "level": int(record.get("level", 1) or 1),
        "power": _power_from(stats_out, armor),
    }


def _power_from(stats: dict, armor: int) -> int:
    """A mob's power on the SAME scale the leaderboard shows, so the two are comparable."""
    return C.POWER_RATING_BASE + sum(
        {**stats, "armor": armor}.get(key, 0) * C.POWER_RATING_WEIGHTS[key]
        for key in (*C.STAT_KEYS, "armor")
    )


def mob_fighter(block: dict):
    """The mob as a pets_combat.Fighter. Key is the mob code, which is never a user id."""
    stats = block.get("stats") or {}
    return pets_combat.Fighter(
        key=f"mob:{block.get('code')}",
        name=block.get("name") or "Моб",
        strength=stats.get("strength", 1), health=stats.get("health", 1),
        agility=stats.get("agility", 1), luck=stats.get("luck", 1),
        armor=block.get("armor", 0),
        effects=(), level=int(block.get("level", 1) or 1),
    )


def record_mob_fight(entry, user_id, block: dict, result, now: datetime | None = None) -> dict:
    """Bank a PVE result: spend the fight, pay the purse, roll loot and rubies.

    Uses the SAME fight bank as a duel, as commissioned, so PVE is an alternative to the
    arena rather than a second income running beside it. There is no defender: nobody
    loses coins, nobody gains XP on the other side, and nothing is written to the duel
    log -- a mob has no history to keep and no name to snapshot.
    """
    moment = now or app_now()
    mob = M.find_mob(block.get("code"))
    if mob is None:
        raise ValueError("Такого моба нет.")
    with _farm_settlement_lock:
        data = _load(entry)
        record = _tamed_record(data, user_id)
        if record is None:
            raise ValueError("Сначала приручи существо.")
        if _is_farming_record(record, moment):
            raise ValueError("Питомец на ферме и не может участвовать в бою.")
        # PVE's OWN counter -- the arena bank is untouched by a mob fight and vice versa.
        window = _pve_window(moment)
        used = (
            max(0, int(record.get("pve_used", 0) or 0))
            if int(record.get("pve_window", -1) or -1) == window else 0
        )
        if used >= C.PVE_ATTACKS_PER_WINDOW:
            ends = _pve_window_end(moment)
            raise ValueError(
                f"Атаки на мобов кончились. Новые придут в "
                f"{ends.strftime('%H:%M')} — сразу у всех."
            )
        _spend_pve_fight(record, moment)
        record["fights"] = record.get("fights", 0) + 1

        won = str(result.winner or "") == str(user_id)
        tier = block.get("tier", "medium")
        gold = xp = 0
        if won:
            record["wins"] = record.get("wins", 0) + 1
            # Half an arena purse before the mob's own multiplier -- see pets_mobs.
            base = random.randint(C.WIN_GOLD_MIN, C.WIN_GOLD_MAX) * C.PVE_GOLD_SHARE
            gold = max(1, round(base * M.TIER_REWARD[tier] * mob.gold))
            xp = max(1, round(C.WIN_XP * C.PVE_XP_SHARE * M.TIER_REWARD[tier]))
        else:
            xp = C.LOSS_XP
        _, levels_gained = _apply_xp(record, xp)
        _metric_add(data, "pve_fights")
        if gold:
            _metric_add(data, "pve_gold_minted", gold)
        _save(entry, data)

    if gold:
        economy.grant(entry, user_id, gold, "pet_mob_win")
    dropped = None
    ruby = 0
    if won:
        dropped = grant_random_drop(entry, user_id, C.PVE_DROP_CHANCE * mob.loot)
        if random.random() < min(1.0, M.TIER_RUBY_CHANCE[tier] * mob.ruby):
            ruby = random.randint(C.PVE_RUBY_MIN, C.PVE_RUBY_MAX)
            grant_rubies(entry, user_id, ruby)
    return {
        "won": won, "draw": bool(getattr(result, "is_draw", False)),
        "gold": gold, "xp": xp, "levels_gained": levels_gained,
        "level": get_pet(entry, user_id).get("level", 1) if get_pet(entry, user_id) else 1,
        "rubies": ruby, "ruby_total": ruby_balance(entry, user_id),
        "dropped_item": dropped.get("code") if dropped else None,
        "dropped_name": dropped.get("name") if dropped else None,
        "dropped_rarity": dropped.get("rarity") if dropped else None,
        "auto_equipped": bool(dropped.get("auto_equipped")) if dropped else False,
        "mob": {"code": mob.code, "name": mob.name, "tier": tier,
                "tier_name": M.TIER_NAMES[tier]},
        "at": moment.isoformat(),
    }


def history(entry, user_id) -> list[dict]:
    data = _load(entry)
    uid = str(user_id)
    mine = []
    for fight in data.get("fights", []):
        if fight.get("attacker_id") != uid and fight.get("defender_id") != uid:
            continue
        row = dict(fight)
        # Money columns are rewritten from the READER's side: "gold" is what the winner
        # received, "loss_gold" what an attacker-loser paid and "consolation_gold" what a
        # defender-loser was paid instead -- a fight has exactly one winner and one loser
        # (or a draw), so at most one of the three is non-zero on any one person's line.
        won = fight.get("winner_id") == uid
        row["gold"] = fight.get("gold", 0) if won else 0
        row["loss_gold"] = 0 if won else fight.get("loss_gold", 0)
        row["consolation_gold"] = 0 if won else fight.get("consolation_gold", 0)
        mine.append(row)
    mine.reverse()  # stored oldest -> newest, so reverse for "newest first"
    return mine[:C.HISTORY_LIMIT]


def fight_id(fight: dict) -> str:
    """A URL-safe id for one recorded fight.

    The timestamp IS the identity: a fight row has never carried an id of its own, the
    log is trimmed from the front (FIGHT_LOG_LIMIT) so a position would point at a
    different fight after the next two thousand, and `ts` comes from app_now() with
    microseconds. Only the timezone's "+" is a problem -- in a query string a raw plus
    decodes to a space, which would turn "forgot to percent-encode this" into a silent
    miss instead of an error. "~" is unreserved in a URL and needs no encoding anywhere,
    and the swap is undone on lookup, so what is stored stays a plain ISO timestamp.
    """
    return str((fight or {}).get("ts") or "").replace("+", "~")


def grant_random_drop(entry, user_id, chance: float, seed: str | None = None) -> dict | None:
    """Roll one loot drop outside a fight, and give it if it lands.

    For rewards that are not arena wins -- a completed quest today, whatever else later.
    The pool rules are the arena's, not a second set: every code is unique inside its
    owner's bag, while item designs may belong to multiple players. The roll is
    rarity-weighted from the same catalogue.

    Seeded when a caller passes `seed`, so the same reward paid twice by a retried
    settlement rolls the same item instead of two -- the same reproducibility trick
    _farm_reward uses.
    """
    if chance <= 0:
        return None
    with _farm_settlement_lock:
        data = _load(entry)
        record = _tamed_record(data, user_id)
        if record is None:
            # No pet, nowhere to put an item. Gold and XP still land; this simply does not.
            return None
        rng = random.Random(seed) if seed else random
        if rng.random() >= min(1.0, float(chance)):
            return None
        owned = set(record.get("inventory", []))
        pool = [item for item in C.ITEMS if item.source == "drop" and item.code not in owned]
        if not pool:
            return None
        weighted = [item for item in pool for _ in range(max(1, getattr(item, "drop_weight", 1)))]
        dropped = rng.choice(weighted)
        record.setdefault("inventory", []).append(dropped.code)
        _discover(record, dropped.code)
        equipped = record.setdefault("equipped", {})
        current = C.find_item(equipped.get(dropped.slot))
        auto_equipped = current is None or C.equipment_score(dropped) > C.equipment_score(current)
        if auto_equipped:
            equipped[dropped.slot] = dropped.code
        _metric_add(data, "drops_by_rarity", rarity=dropped.rarity)
        _save(entry, data)
    return {
        "code": dropped.code, "name": dropped.name, "rarity": dropped.rarity,
        "slot": dropped.slot, "auto_equipped": auto_equipped,
    }


def find_fight(entry, user_id, wire_id) -> dict | None:
    """One recorded fight this player took part in, by the id `fight_id` gave out.

    Participation is checked HERE rather than by the caller, because this is the only
    function that can: a fight belongs to exactly two people, and nobody else gets to
    read one back out of a chat-wide log by guessing a timestamp.
    """
    uid = str(user_id)
    wanted = str(wire_id or "").replace("~", "+")
    if not wanted:
        return None
    for fight in _load(entry).get("fights", []):
        if not isinstance(fight, dict) or str(fight.get("ts") or "") != wanted:
            continue
        if uid not in (str(fight.get("attacker_id")), str(fight.get("defender_id"))):
            continue
        return dict(fight)
    return None


# --- mailbox -------------------------------------------------------------------------


def _mail_moment(value, tz) -> datetime | None:
    """Parse a stored timestamp for sorting, tolerating naive and damaged values."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment.replace(tzinfo=tz) if moment.tzinfo is None else moment


def _mail_item(code) -> dict:
    """The three fields every mail line needs about a find, or three Nones."""
    item = C.find_item(code) if code else None
    if item is None:
        return {"item": None, "item_name": None, "item_rarity": None}
    return {"item": item.code, "item_name": item.name, "item_rarity": item.rarity}


def _mail_pet(data: dict, user_id) -> tuple[str | None, str | None]:
    record = data.get("pets", {}).get(str(user_id))
    if not isinstance(record, dict):
        return None, None
    return record.get("name"), record.get("owner_name")


def mail(entry, user_id, limit: int | None = None, extra: list[dict] | None = None) -> list[dict]:
    """One merged feed with the newest kept and displayed at the bottom.

    Read-side only: no new store, nothing written, no migration. The three things worth
    telling somebody about are already persisted by the code that performs them -- fights
    in ``data["fights"]``, farm shifts in the pet's own ``farm_notifications``, gifts in
    ``data["gift_history"]`` -- so the mailbox is a merge of records that already exist.
    That is also why it is retroactive: the first player to open it sees their real
    history rather than an empty box waiting for the next event.

    A find is a property OF the fight or shift that produced it, so it rides on that
    event instead of getting a line of its own -- "победа, +28, нашёл X" is one thing
    that happened, and splitting it would double the length of a good day.

    Every event carries ``coins`` signed from the reader's side (what their balance did),
    ``at`` as HH.MM and ``day`` as an ISO date, both already in the chat's timezone: the
    bot and the Mini App must not disagree about what time something happened, and the
    page has no idea which timezone the chat lives in.

    `extra` is for events this module cannot read for itself -- quest verdicts, which live
    in quests.py, and quests.py imports THIS module, so the dependency can only point one
    way. The caller supplies them; the merging, the ordering and the cap all still happen
    here, in the one place that knows what a mailbox is.
    """
    cap = C.MAIL_LIMIT if limit is None else max(0, int(limit))
    data = _load(entry)
    uid = str(user_id)
    tz = app_now().tzinfo
    events: list[tuple[datetime, dict]] = []

    def add(ts, event: dict) -> None:
        moment = _mail_moment(ts, tz)
        if moment is None:
            return
        event["ts"] = moment.isoformat()
        event["at"] = moment.strftime("%H.%M")
        event["day"] = moment.date().isoformat()
        events.append((moment, event))

    for fight in data.get("fights", []):
        if not isinstance(fight, dict):
            continue
        attacked = str(fight.get("attacker_id")) == uid
        if not attacked and str(fight.get("defender_id")) != uid:
            continue
        draw = bool(fight.get("draw"))
        won = str(fight.get("winner_id") or "") == uid
        if won:
            coins = int(fight.get("gold", 0) or 0)
        elif draw:
            coins = 0
        elif int(fight.get("loss_gold", 0) or 0):
            # An attacker who lost paid this; a defender never does (see record_fight).
            coins = -int(fight.get("loss_gold", 0) or 0)
        else:
            coins = int(fight.get("consolation_gold", 0) or 0)
        event = {
            "kind": "attack" if attacked else "defense",
            "outcome": "draw" if draw else ("win" if won else "loss"),
            "coins": coins,
            # Derived from the RAW stored timestamp -- not from the normalised `ts` add()
            # writes below, which may have gained a timezone that find_fight would then
            # fail to match. Only a fight carrying a combat snapshot can be replayed; the
            # rest are from before snapshots were kept.
            "fight_id": fight_id(fight),
            "replayable": bool(fight.get("combat_snapshot")),
            # Snapshotted at fight time, so a later rename does not rewrite the past.
            "pet_name": fight.get("defender_name") if attacked else fight.get("attacker_name"),
            "owner_name": fight.get("defender_owner") if attacked else fight.get("attacker_owner"),
            # The drop belongs to the winner, always -- so a loser's line never claims one.
            **_mail_item(fight.get("dropped_item") if won else None),
            "auto_equipped": bool(fight.get("auto_equipped")) if won else False,
        }
        add(fight.get("ts"), event)

    record = data.get("pets", {}).get(uid)
    if isinstance(record, dict):
        for receipt in record.get("farm_notifications", []):
            if not isinstance(receipt, dict):
                continue
            add(receipt.get("settled_at"), {
                "kind": "farm",
                "outcome": "",
                "coins": int(receipt.get("gold", 0) or 0),
                "xp": int(receipt.get("xp", 0) or 0),
                "hours": int(receipt.get("hours", 0) or 0),
                "levels_gained": int(receipt.get("levels_gained", 0) or 0),
                "pet_name": receipt.get("pet_name"),
                "owner_name": None,
                **_mail_item(receipt.get("item_code")),
                "auto_equipped": bool(receipt.get("auto_equipped")),
            })

    for row in data.get("gift_history", []):
        if not isinstance(row, dict):
            continue
        sent = str(row.get("giver_id")) == uid
        if not sent and str(row.get("receiver_id")) != uid:
            continue
        # Gifts are audited by id only, so the names here are read live rather than
        # snapshotted. A renamed pet therefore shows its current name -- acceptable, and
        # better than showing an id, which is what the audit row actually holds.
        pet_name, owner_name = _mail_pet(data, row.get("receiver_id") if sent else row.get("giver_id"))
        add(row.get("ts"), {
            "kind": "gift_out" if sent else "gift_in",
            "outcome": "",
            "coins": 0,
            "pet_name": pet_name,
            "owner_name": owner_name,
            **_mail_item(row.get("item_code")),
            "auto_equipped": False,
        })

    # A quest verdict already carries its scroll on the same mailbox line. Keep the
    # standalone notification as a crash-safe fallback in storage, but do not show the
    # same unlock twice when the quest receipt was persisted normally.
    covered_scroll_sources = {
        str(event.get("scroll_source")) for event in (extra or [])
        if isinstance(event, dict) and event.get("scroll_name") and event.get("scroll_source")
    }
    for notice in data.get("scroll_notifications", []):
        if not isinstance(notice, dict) or str(notice.get("user_id")) != uid:
            continue
        if str(notice.get("source") or "") in covered_scroll_sources:
            continue
        add(notice.get("ts"), {
            "kind": "scroll", "outcome": "win", "coins": 0,
            "scroll": notice.get("code"), "scroll_name": notice.get("name"),
            "scroll_icon": notice.get("icon"), "scroll_ultimate": bool(notice.get("ultimate")),
            "scroll_source": notice.get("kind"), "pet_name": None, "owner_name": None,
            "item": None, "item_name": None, "item_rarity": None, "auto_equipped": False,
        })

    for event in extra or []:
        if isinstance(event, dict):
            add(event.get("ts"), dict(event))

    events.sort(key=lambda pair: pair[0])
    kept = events[-cap:] if cap else []
    return [event for _moment, event in kept]


def award_xp(entry, user_id, amount) -> tuple[int, int]:
    data = _load(entry)
    record = data["pets"].get(str(user_id))
    if record is None:
        return 1, 0
    new_level, levels_gained = _apply_xp(record, amount)
    _save(entry, data)
    return new_level, levels_gained
