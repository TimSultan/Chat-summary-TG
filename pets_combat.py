"""The pet fight, as a pure function: two `Fighter`s and an rng in, a `FightResult` out.

Kept free of I/O, storage and Telegram on purpose -- the UI layer (pets_ui.py) is the
only thing allowed to know a chat exists, and a pure simulate() is what makes the balance
testable at all: the same seed must replay to the same fight, and "run 200 seeded fights
and check the median round count" has to be a five-line test, not an integration test
against a bot. See pets_config.py's "combat" section for where every constant used below
comes from and why -- nothing here is tuned; it is all read out of that file.

Fighters carry EFFECTIVE stats (purchased level + pet level + equipment) already summed,
because combat has no business knowing how a number was assembled -- that is pets.py's
job. derive() turns those four raw stats into the five numbers a fight actually runs on
(max_hp, damage, dodge, crit, reduction) plus which stats triggered the dominance bonus,
and is exposed publicly because the UI prints it straight onto the pet card.

The per-blow pipeline (dodge, then crit, then jitter, then armor, then the damage floor)
is a fixed order rather than independent rolls combined after the fact, because that is
what makes one event win when several conditions are true at once: a dodge is total and
skips everything else, a crit that also gets mostly blocked is still reported as a crit
rather than a confusing double-message, and "low_damage" only ever replaces a plain "hit"
-- a crit or a blocked hit already explains why the number is small.

A "round" is a full exchange, not a single blow: the round's leader swings, and -- unless
that already ends the fight -- the other side swings straight back within the same round
number. Leadership alternates round to round from the rng-picked first mover, so nobody
is stuck permanently swinging second. `MAX_SKILL_ACTIONS_PER_FIGHTER` is a hard cap on
each fighter's actions, so there can be no more than that many blows from either side.
"""

import math
import random
from collections.abc import Mapping
from dataclasses import dataclass, replace

import pets_config as C
import pets_flavor
import pets_scroll_catalog as SCROLLS

_STATS = ("strength", "health", "agility", "luck")
_SIGNATURE_STATS = _STATS + ("armor",)
PHYSICAL = "physical"
MAGIC = "magic"
ELEMENTAL = "elemental"
ATTACK_TYPES = frozenset((PHYSICAL, MAGIC, ELEMENTAL))
MAGICAL_ATTACK_TYPES = frozenset((MAGIC, ELEMENTAL))
_DEFICIT_TEXT = {
    "strength": "🧱 {name} не держит корпус: слабая сила режет уклонение.",
    "health": "🩹 {name} выходит без запаса прочности: максимум HP ниже.",
    "agility": "🐌 {name} не успевает уйти с линии удара: входящий урон выше.",
    "luck": "🧲 {name} роняет подкову: атаки легче прочитать и обойти.",
}


@dataclass(frozen=True)
class Fighter:
    key: str            # opaque id (a user id as str) -- combat never interprets it
    name: str            # pet name, already display-ready
    strength: int        # EFFECTIVE values: purchased level + pet level + equipment
    health: int
    agility: int
    luck: int
    armor: int           # from equipment only, 0 for a bare pet
    # Combat-only item metadata.  It is deliberately carried with the snapshot rather
    # than looked up here: replaying a saved fight must not change after a shop rotation.
    # Each entry may be ``"vampiric"`` or ``{"code": "vampiric", "value": .12}``.
    effects: tuple = ()
    level: int = 1       # pet level snapshot; used by Giant Slayer
    # Active combat loadout. Empty means a historic/classic fighter and preserves the
    # old simulator byte-for-byte; current pets carry 3 regular scrolls + one ultimate.
    skills: tuple = ()
    # Codes of scrolls carrying a personal paint rune.  The engine scales only their
    # numerical power fields, never duration or probability; kept on the fighter so an
    # audit/replay is independent of later inventory changes.
    personal_enchanted_scrolls: tuple = ()
    # Snapshot of the equipped live shield's Defend hook (or None for base Defend).
    shield: dict | None = None
    damage_multiplier: float = 1.0
    physical_damage_taken_multiplier: float = 1.0
    # True turns an unruned weapon's swing into healing rather than merely wasting it
    # (see the dungeon's spells_only boss). Kept as its own bool instead of folding into
    # physical_damage_taken_multiplier because "does nothing" and "actively heals" are
    # different player-facing claims, and a future gimmick may want partial resistance
    # (a nonzero multiplier) combined with a heal on top of it.
    physical_damage_heals: bool = False
    magic_reflect_multiplier: float = 0.0
    enchant_reflect_multiplier: float = 0.0
    weapon_enchanted: bool = False
    starting_hp: int | None = None


@dataclass(frozen=True)
class Round:
    number: int
    attacker: str        # Fighter.key
    event: str            # one of pets_flavor's combat events
    damage: int
    attacker_hp: int     # AFTER the blow
    defender_hp: int
    text: str             # the flavour line, ready to print
    # Elemental damage carries both ELEMENTAL and MAGIC: elemental is a magical subtype.
    attack_types: tuple[str, ...] = (PHYSICAL,)
    # Complete post-event combat state.  Kept on the immutable transcript row so an
    # audit never has to infer a stun, guard or proc charge from player-facing prose.
    state: dict | None = None
    # False for a consequence attached to somebody else's action: guard absorption,
    # burn tick, healing reaction, counterattack and similar transcript detail.  These
    # rows are valuable evidence but must never look as though they spent another turn.
    is_action: bool = True


@dataclass(frozen=True)
class FightResult:
    winner: str | None    # Fighter.key, or None for a draw
    loser: str | None
    rounds: tuple
    opening: str          # flavour line
    closing: str          # flavour line
    total_damage: dict
    stopped_early: bool   # attack cap hit, awarded by damage among living fighters
    is_draw: bool
    seed: int | None
    accident: str | None
    final_hp: dict | None = None
    fight_id: str | None = None


# Amulets use these stable machine codes; their catalogue descriptions are player-facing
# copy, while this table is the deliberately small rules contract.  Values in item
# metadata override the default where that makes sense.  Unknown codes are harmless so a
# partially deployed catalogue can never make a fight fail.
_EFFECT_ALIASES = {
    "start_shield": "opening_shield", "shield": "opening_shield",
    "ambush": "opening_blast", "opening_attack": "opening_blast",
    "fury": "battle_cry", "initiative": "first_strike",
    "health_up": "vitality", "damage_up": "ferocity", "dodge_up": "nimble",
    "crit_up": "lucky", "armor_up": "plating", "accuracy": "precision",
    "rage": "berserker", "execute": "executioner", "lifesteal": "vampiric",
    "armor_pierce": "piercing", "double_hit": "combo", "venom": "poison",
    "wind": "second_wind", "cheat_death": "last_stand", "reflect": "thorns",
    "rejuvenation": "regen", "underdog": "giant_slayer",
}

_EFFECT_DEFAULTS = {
    # Catalogue percentages are written as whole numbers (12 == 12%), which keeps
    # player copy and data reviewable.  `_fraction` converts them at the hook.
    "opening_shield": 3, "opening_blast": 4, "battle_cry": 12,
    "first_strike": 18, "vitality": 14, "ferocity": 3, "nimble": 5,
    "lucky": 5, "plating": 3, "precision": 8, "berserker": 12,
    "executioner": 16, "vampiric": 9, "piercing": 12, "combo": 5,
    # Percent of the owner's swing, like every other damage-over-time number now.
    "poison": 6, "thorns": 7, "second_wind": 18, "last_stand": 1,
    "dodge_heal": 7, "crit_guard": 30, "retaliation": 6, "regen": 1,
    "focused": 20, "momentum": 2, "gambler": 18, "safeguard": 35,
    "giant_slayer": 18, "mob_hunter": 15, "mob_ward": 15, "collector": 25,
    "survivor": 30, "mirror_soul": 20,
    "bite": 50, "armor_burst": 75, "late_strike": 35, "medkit": 20,
    "countercrit": 20, "trophy_compass": 35, "stun": 1, "cocoon": 100,
    "glass_crit": 60, "blood_pact": 35, "chill": 40, "tesla": 15,
    "death_shield": 20, "acid": 25, "spring": 100, "candle": 40,
    "armor_shred": 6, "wound": 1, "burn": 6, "venom_blade": 18,
    "coin_rake": 1, "bleed": 4, "shield_breaker": 100, "heavy_combo": 50,
    "phantom_step": 1, "afterimage": 45, "rewind": 25,
    "echo_strike": 50, "crushing_grip": 10, "perfect_parry": 35,
    # Legendary-only mechanics. Every legendary weapon used to carry a bigger number of a
    # passive a rare weapon already had -- twelve flagships and not one rule the tier owned
    # by itself. These six are the legendary weapon's own vocabulary; nothing below the tier
    # carries them.
    "chain_crit": 100, "double_strike": 65, "shatter": 10, "reap": 20,
    "pressure": 8, "tax": 4,
    # Cursed legendaries: a genuinely large upside welded to a genuinely large cost, in one
    # rule. These are the only passives in the catalogue that can lose a fight on their own,
    # which is the entire point of the shelf.
    "charge_crit": 400, "wild_swing": 200, "blind_fury": 3, "glass_body": 90,
    "blood_price": 80, "hunger": 14, "soul_debt": 40, "recoil": 150,
}

_EFFECT_TEXT = {
    "opening_shield": "поднимает щит амулета: +{amount} щита.",
    "opening_blast": "выпускает стартовый разряд: {amount} урона.",
    "battle_cry": "включает боевой клич — первые удары сильнее.",
    "first_strike": "перехватывает инициативу амулетом.",
    "vampiric": "вытягивает из удара {amount} HP.",
    "poison": "оставляет на сопернике едкий след: {amount} урона.",
    "thorns": "отвечает шипами: {amount} урона.",
    "second_wind": "ловит второе дыхание: +{amount} HP.",
    "last_stand": "цепляется за амулет и остаётся на 1 HP.",
    "dodge_heal": "уворачивается и восстанавливает {amount} HP.",
    "retaliation": "отвечает контрударом: {amount} урона.",
    "regen": "восстанавливает {amount} HP перед ударом.",
    "crit_guard": "гасит один критический удар.",
    "bite": "кусает ещё раз: {amount} урона и немного здоровья обратно.",
    "armor_burst": "раскрывает бронекапсулу и гасит {amount} урона.",
    "late_strike": "раскачивает маятник и бьёт сильнее.",
    "medkit": "раскрывает аптечку: +{amount} HP.",
    "countercrit": "ловит крит и готовит ответный удар.",
    "stun": "оглушает соперника на один ход.",
    "cocoon": "прячется в кокон и готовит шипы.",
    "glass_crit": "наводит стеклянный глаз: крит усилен.",
    "blood_pact": "забирает из третьего удара {amount} HP.",
    "chill": "морозит соперника: следующий удар слабее.",
    "tesla": "выпускает разряд: {amount} урона.",
    "death_shield": "цепляется за медаль и поднимает аварийный щит.",
    "acid": "обливает оружие кислотой: следующий удар не увернуть.",
    # {amount} is the multiplier the spring is ACTUALLY worth. It read "двойной" for
    # every spring, including the legendary one that triples -- so a transcript showing a
    # tripled hit explained it as a doubling and the arithmetic looked broken.
    "spring": "сжимает пружину: следующий удар ×{amount}.",
    "candle": "зажигает чёрную свечу: {amount:+d}% к урону.",
    "armor_shred": "крошит броню: защита слабее ещё на {amount}%.",
    "wound": "оставляет глубокую рану: −{amount} текущего и максимального HP.",
    "burn": "поджигает соперника: {amount} урона от огня.",
    "bleed_heal_cut": "раскрывает рану: лечение соперника режется на {amount}%.",
    "poison_weaken": "отравлен — удар слабее на {amount}%.",
    "venom_blade": "отравляет соперника: {amount} урона.",
    "bleed": "раскрывает кровотечение: {amount} урона.",
    "shield_breaker": "пробивает защиту первым попаданием.",
    "heavy_combo": "попадает в такт: усиленный третий удар.",
    "safeguard": "смягчает первый удар на {amount} урона.",
    "gambler": "проверяет авось: {amount:+d}% к урону.",
    "adrenaline": "разгоняется от критического удара: +{amount} HP.",
    "phantom_step": "исчезает из-под первого удара.",
    "afterimage": "оставляет послеслед: следующая атака сильнее.",
    "rewind": "отменяет смертельный шаг и возвращает {amount} HP.",
    "echo_strike": "повторяет попадание эхом: {amount} урона.",
    "crushing_grip": "сжимает хватку: урон соперника ниже на {amount}%.",
    "perfect_parry": "парирует удар и запасает {amount} урона для ответа.",
    "shield_parry_stun": "щитом парирует {amount} урона и оглушает атакующего.",
    "shield_damage_heal": "щит возвращает {amount} HP из полученного урона.",
    "shield_counterattack": "щит отвечает контрударом: {amount} урона.",
    "guard": "держит защиту: поглощено {amount} урона.",
    "steel_heal": "впитывает стальной удар вместо урона: +{amount} HP.",
    "chain_crit": "ведёт линию дальше: крит открывает ещё одну атаку.",
    "double_strike": "качается обратно и бьёт второй раз.",
    "shatter": "оставляет осколок на сопернике.",
    "shatter_burst": "разбивает все осколки разом: {amount} урона.",
    "reap": "снимает с раненого соперника {amount} HP себе.",
    "tax": "взимает пошлину: {amount} урона.",
    "pressure": "поднимает давление: удар сильнее ещё на {amount}%.",
    "charge_crit": "заводит удар — и открывается ({amount} ход заряда).",
    "charge_crit_release": "спускает весь заряд разом.",
    "wild_swing": "бьёт вслепую и попадает идеально.",
    "wild_swing_heal": "промахивается так, что лечит соперника на {amount} HP.",
    "wild_swing_miss": "проворачивает рулетку и бьёт мимо всего сразу.",
    "blind_fury": "открывает глаза: {amount} хода без промаха.",
    "blind_fury_blind": "зажмуривается и бьёт в пустоту.",
    "glass_body": "звенит стеклом: бьёт сильнее и держит хуже.",
    "blood_price": "платит кровью за удар: {amount} HP.",
    "hunger": "голодает: −{amount} максимального HP, но урон растёт.",
    "soul_debt": "выкупает себя из смерти: +{amount} HP и вдвое хуже защита.",
    "recoil": "отдачей выбивает себя из следующего хода.",
}


def snapshot(fighter: "Fighter") -> dict:
    """A JSON-safe record of one fighter exactly as they entered the ring.

    Stored alongside the fight and its seed, which together are the whole of a replay:
    simulate() reads nothing but its arguments, so the same snapshot and the same seed
    reproduce the same fight blow for blow, forever. That is why the stats are stored
    rather than looked up when a replay is asked for -- a sold amulet, a shop rotation or
    a rebalanced catalogue must not be able to rewrite a fight that already happened.
    """
    return {
        "key": str(fighter.key),
        "name": fighter.name,
        "strength": fighter.strength,
        "health": fighter.health,
        "agility": fighter.agility,
        "luck": fighter.luck,
        "armor": fighter.armor,
        # Tuple -> list, and each effect copied: this goes through json, and a stored
        # effect must not stay a reference into the live catalogue.
        "effects": [
            dict(effect) if isinstance(effect, Mapping) else str(effect)
            for effect in (fighter.effects or ())
        ],
        "level": fighter.level,
        "skills": list(fighter.skills or ()),
        "personal_enchanted_scrolls": list(fighter.personal_enchanted_scrolls or ()),
        "shield": dict(fighter.shield) if isinstance(fighter.shield, Mapping) else None,
        "damage_multiplier": fighter.damage_multiplier,
        # This one was missing entirely, which meant a replayed Аквариус fight rebuilt the
        # ghost WITHOUT its immunity and played out a fight that never happened. It matters
        # more now that physical_damage_heals rides alongside it: restoring one without the
        # other would give the replay a boss that both heals from steel and takes it.
        "physical_damage_taken_multiplier": fighter.physical_damage_taken_multiplier,
        "physical_damage_heals": fighter.physical_damage_heals,
        "magic_reflect_multiplier": fighter.magic_reflect_multiplier,
        "enchant_reflect_multiplier": fighter.enchant_reflect_multiplier,
        "weapon_enchanted": fighter.weapon_enchanted,
        "starting_hp": fighter.starting_hp,
    }


def _stored_number(value, default):
    """Keep a stored stat's exact value; fall back only when it is not a number at all."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def normalize_attack_types(attack_types=()) -> tuple[str, ...]:
    """Return the stable damage tags for an attack.

    ``elemental`` is not an alternative to magic: it is a more specific magical attack,
    so it always gains the ``magic`` tag as well.  Unknown values are ignored rather
    than letting a bad catalogue row take down an otherwise valid fight.
    """
    rows = tuple(str(value) for value in (attack_types or ()) if str(value) in ATTACK_TYPES)
    if not rows:
        return (PHYSICAL,)
    if ELEMENTAL in rows and MAGIC not in rows:
        rows += (MAGIC,)
    return tuple(dict.fromkeys(rows))


def is_magic_attack(attack_types=()) -> bool:
    """Whether this attack belongs to magic, including elemental magic."""
    return bool(set(normalize_attack_types(attack_types)) & MAGICAL_ATTACK_TYPES)


def restore(data) -> "Fighter | None":
    """Rebuild a Fighter from `snapshot`, or None if the record cannot be trusted.

    None rather than a repaired guess: a replay assembled from defaults would play out a
    fight that never happened, which is worse than telling the player this one cannot be
    replayed.
    """
    if not isinstance(data, Mapping):
        return None
    key = str(data.get("key") or "")
    if not key:
        return None
    raw_skills = data.get("skills") or ()
    try:
        skills = SCROLLS.validate_loadout(raw_skills) if raw_skills else ()
    except ValueError:
        return None
    raw_shield = data.get("shield")
    shield = dict(raw_shield) if isinstance(raw_shield, Mapping) else None
    return Fighter(
        key=key,
        name=str(data.get("name") or "Существо"),
        strength=_stored_number(data.get("strength"), 1),
        health=_stored_number(data.get("health"), 1),
        agility=_stored_number(data.get("agility"), 1),
        luck=_stored_number(data.get("luck"), 1),
        armor=_stored_number(data.get("armor"), 0),
        effects=tuple(
            dict(effect) if isinstance(effect, Mapping) else str(effect)
            for effect in (data.get("effects") or ())
            if isinstance(effect, (Mapping, str))
        ),
        level=_stored_number(data.get("level"), 1),
        skills=skills,
        personal_enchanted_scrolls=tuple(
            code for code in (data.get("personal_enchanted_scrolls") or ())
            if isinstance(code, str) and SCROLLS.scroll(code) is not None
        ),
        shield=shield,
        damage_multiplier=_stored_number(data.get("damage_multiplier"), 1.0),
        physical_damage_taken_multiplier=_stored_number(
            data.get("physical_damage_taken_multiplier"), 1.0,
        ),
        physical_damage_heals=bool(data.get("physical_damage_heals", False)),
        magic_reflect_multiplier=_stored_number(data.get("magic_reflect_multiplier"), 0.0),
        enchant_reflect_multiplier=_stored_number(data.get("enchant_reflect_multiplier"), 0.0),
        weapon_enchanted=bool(data.get("weapon_enchanted", False)),
        starting_hp=_stored_number(data.get("starting_hp"), None),
    )


def _mirror(fighter: "Fighter", opponent: "Fighter", rng) -> "Fighter":
    """Зеркало души: come down to the opponent's numbers, then shake them.

    The point is a fair fight against somebody far below you. Each of the four stats is
    set to the OPPONENT's value and then jittered by up to ±value% -- so the wearer is
    roughly mirrored but never exactly, and the fight is a real one rather than a coin
    flip between two identical sheets.

    It only ever comes DOWN. Wearing it against somebody stronger would otherwise be a
    free upgrade, which is the opposite of what it is for: this is the item that lets a
    big pet pick on a small one without the arena having to punish anybody for it (the
    reward side of that bargain lives in pets.record_fight).

    Armour is mirrored too but never jittered upward past the opponent's, because armour
    is the one stat with a hard cap in derive() and a lucky roll there would quietly undo
    the whole point.
    """
    effect = _effect(_effect_specs(fighter), "mirror_soul")
    if effect is None:
        return fighter
    spread = max(0.0, _fraction(effect.get("value", 20)))
    mirrored = {}
    for stat in _STATS:
        mine, theirs = getattr(fighter, stat), getattr(opponent, stat)
        if mine <= theirs:
            mirrored[stat] = mine
            continue
        jitter = 1.0 + rng.uniform(-spread, spread)
        mirrored[stat] = max(1, round(theirs * jitter))
    armour = min(fighter.armor, opponent.armor)
    return replace(fighter, armor=armour, **mirrored)


def _effect_specs(fighter: "Fighter") -> tuple[dict, ...]:
    """Normalize catalogue metadata without letting malformed data into combat.

    ``Item.effect`` is intentionally data, not executable logic.  Keeping the parser
    here means tests and old fight snapshots can pass either a compact string or the
    full mapping stored by the catalogue.

    Two equipped items can now carry the same code -- weapons gained passives drawn from
    the same vocabulary as amulets, which a single amulet slot could never collide with.
    The lookups below read the first match, so a duplicate would silently do nothing;
    keeping the strongest instead makes that case deterministic and never a downgrade.
    """
    specs: list[dict] = []
    raw_effects = fighter.effects
    if isinstance(raw_effects, (str, Mapping)):
        raw_effects = (raw_effects,)
    for raw in raw_effects or ():
        if isinstance(raw, str):
            code, value = raw, None
        elif isinstance(raw, Mapping):
            code = raw.get("code", raw.get("effect", ""))
            value = raw.get("value")
        else:
            continue
        code = _EFFECT_ALIASES.get(str(code).strip().lower(), str(code).strip().lower())
        if code not in _EFFECT_DEFAULTS:
            continue
        try:
            value = _EFFECT_DEFAULTS[code] if value is None else float(value)
        except (TypeError, ValueError):
            value = _EFFECT_DEFAULTS[code]
        spec = dict(raw) if isinstance(raw, Mapping) else {}
        spec.update({"code": code, "value": value})
        existing = next((other for other in specs if other["code"] == code), None)
        if existing is None:
            specs.append(spec)
        elif abs(spec["value"]) > abs(existing["value"]):
            specs[specs.index(existing)] = spec
    return tuple(specs)


def _effect_value(effects: tuple[dict, ...], code: str) -> float | None:
    """Return a single equipped amulet effect's value (or ``None`` when absent)."""
    return next((effect["value"] for effect in effects if effect["code"] == code), None)


def _effect(effects: tuple[dict, ...], code: str) -> dict | None:
    return next((effect for effect in effects if effect["code"] == code), None)


def _fraction(value: float) -> float:
    """Accept compact test fractions and the catalogue's whole-number percentages."""
    return value / 100 if abs(value) > 1 else value


def _param(effects: tuple[dict, ...], code: str, key: str, default: float) -> float:
    effect = _effect(effects, code)
    if effect is None:
        return default
    try:
        return float(effect.get(key, default))
    except (TypeError, ValueError):
        return default


def _share_of_swing(percent: float, fighter: "Fighter", attack_damage: float) -> int:
    """Turn a catalogue percentage into hit points, from the owner's own swing.

    Fire, poison, venom, bleeding and the retaliation bonus are all written as a share of
    what their owner hits for (see pets_config). That is the whole implementation: no level
    curve, no reference constant, no divergence between the number on the item card and the
    number in the combat log. A pet whose swing doubles doubles what its burn ticks for,
    which is the only behaviour that keeps a damage-over-time passive worth the same share
    of a fight at level 5 and at level 500.
    """
    swing = max(1.0, float(attack_damage or 0))
    return max(1, round(swing * max(0.0, float(percent or 0)) / 100.0))


def _saturate(mx: float, k: float, s: float) -> float:
    """The shared dodge/crit/armor curve: `mx * s / (s + k)`, 0 below zero.

    See pets_config's comment on why this shape and not a linear one -- it reaches half
    of `mx` at `s == k` and approaches `mx` without ever touching it, so no stat can be
    pushed to a guaranteed proc or a guaranteed no-op."""
    if s <= 0:
        return 0.0
    return mx * s / (s + k)


def _stat_lead_bonus(mine: float, theirs: float) -> float:
    """The stronger stat gets its proportional lead as a bonus, capped at 30%."""
    if mine <= theirs:
        return 0.0
    if theirs <= 0:
        return C.DOMINANCE_BONUS
    return min(C.DOMINANCE_BONUS, mine / theirs - 1.0)


def _signature(fighter: "Fighter", opponent: "Fighter") -> tuple[str, int] | None:
    """The single highest 2x/3x stat advantage that may create a signature move."""
    candidates = []
    for stat in _SIGNATURE_STATS:
        mine, theirs = getattr(fighter, stat), getattr(opponent, stat)
        if mine <= 0 or theirs <= 0:
            continue
        ratio = mine / theirs
        tier = 3 if ratio >= C.STAT_OVERWHELMING_RATIO else 2 if ratio >= C.STAT_ADVANTAGE_RATIO else 0
        if tier:
            candidates.append((ratio, tier, stat))
    if not candidates:
        return None
    _, tier, stat = max(candidates, key=lambda candidate: candidate[0])
    return stat, tier


def _stat_deficits(fighter: "Fighter", opponent: "Fighter") -> tuple[str, ...]:
    """The two largest opponent-relative stat gaps that expose build weaknesses."""
    gaps = []
    for stat in _STATS:
        mine = max(1, getattr(fighter, stat))
        theirs = max(1, getattr(opponent, stat))
        if mine / theirs <= C.STAT_DEFICIT_RATIO:
            gaps.append((mine / theirs, stat))
    return tuple(stat for _, stat in sorted(gaps)[:C.STAT_DEFICIT_MAX])


def derive(fighter: "Fighter", opponent: "Fighter") -> dict:
    """The fight-start numbers for one side.

    Only the stat-derived part of `max_hp`/`damage` is multiplied by the stat-lead
    factor -- BASE_HP and BASE_DAMAGE are floors everybody gets, not a reward for
    out-scaling somebody (see pets_config). Armor is equipment-only and is deliberately
    excluded from the dominance comparison, so gear can never trigger someone else's bonus
    or lose its own.
    """
    stat_bonus = {
        stat: _stat_lead_bonus(getattr(fighter, stat), getattr(opponent, stat))
        for stat in _STATS
    }
    deficits = _stat_deficits(fighter, opponent)

    def factor(stat: str) -> float:
        return 1.0 + stat_bonus[stat]

    max_hp = C.BASE_HP + fighter.health * C.HP_PER_POINT * factor("health")
    if fighter.skills:
        max_hp += fighter.strength * C.HP_PER_STRENGTH_WITH_SKILLS * factor("strength")
    damage = C.BASE_DAMAGE + fighter.strength * C.DAMAGE_PER_POINT * factor("strength")
    dodge = _saturate(C.DODGE_MAX, C.DODGE_K, fighter.agility * factor("agility"))
    crit = C.CRIT_BASE + _saturate(C.CRIT_MAX, C.CRIT_K, fighter.luck * factor("luck"))
    signature = _signature(fighter, opponent)
    luck_tier = signature[1] if signature and signature[0] == "luck" else 0
    accuracy = 1.0
    if luck_tier == 3:
        dodge = C.LUCK_OVERWHELMING_DODGE_CHANCE
        crit = C.LUCK_OVERWHELMING_CRIT_CHANCE
    elif luck_tier == 2:
        crit = min(1.0, crit + C.LUCK_ADVANTAGE_CRIT_BONUS)
        accuracy = C.LUCK_ADVANTAGE_MISS_MULTIPLIER
    reduction = _saturate(C.ARMOR_MAX, C.ARMOR_K, fighter.armor)  # never dominance-boosted
    effects = _effect_specs(fighter)

    # These are start-of-fight passives.  Every multiplier is deliberately modest: an
    # amulet should make a build feel different, not turn a similarly-levelled fight
    # into an automatic win.  The hard combat ceilings still apply.
    if (value := _effect_value(effects, "vitality")) is not None:
        max_hp += value
    if (value := _effect_value(effects, "ferocity")) is not None:
        damage += value
    if (value := _effect_value(effects, "nimble")) is not None:
        dodge = min(.60, max(0.0, dodge + _fraction(value)))
    if (value := _effect_value(effects, "lucky")) is not None:
        crit = min(.60, max(0.0, crit + _fraction(value)))
    if (value := _effect_value(effects, "recoil")) is not None:
        # The recoil weapon's whole payload rides on landing a critical, so at an ordinary
        # luck stat it simply did not fire often enough to pay for the turns it costs --
        # raising the crit's SIZE past 620% bought almost nothing measurable, because the
        # blow already overkills. Raising how often it happens is the lever that works.
        crit = min(.60, max(0.0, crit + _fraction(_param(effects, "recoil", "crit", 0))))
    if (value := _effect_value(effects, "plating")) is not None:
        reduction = min(.70, max(0.0, reduction + _fraction(value)))
    if (value := _effect_value(effects, "precision")) is not None:
        # Existing accuracy is a *miss multiplier*, hence lower is better. The floor used
        # to be .25, which quietly capped the whole code at "misses cut to a quarter" --
        # a legendary written as 75% could not be worth more than a rare written as 60%,
        # and neither reached the band its tier is tuned to. .10 leaves the promise of a
        # near-perfect weapon reachable while still never removing dodging entirely.
        accuracy = max(.10, accuracy * (1 - max(-.50, _fraction(value))))

    if "health" in deficits:
        max_hp *= C.STAT_DEFICIT_HEALTH_MULTIPLIER
    if "strength" in deficits:
        dodge *= C.STAT_DEFICIT_DODGE_MULTIPLIER
    if "luck" in deficits:
        accuracy *= C.STAT_DEFICIT_ACCURACY_MULTIPLIER

    return {
        "max_hp": max_hp,
        "damage": damage,
        "dodge": dodge,
        "crit": crit,
        "accuracy": accuracy,
        "luck_tier": luck_tier,
        "signature": signature,
        "reduction": reduction,
        "dominance": {stat: bool(bonus) for stat, bonus in stat_bonus.items()},
        "stat_bonus": stat_bonus,
        "deficits": deficits,
        "incoming_damage_multiplier": (
            C.STAT_DEFICIT_AGILITY_DAMAGE_MULTIPLIER if "agility" in deficits else 1.0
        ),
        "effects": effects,
    }


def _resolve_blow(attacker: dict, defender: dict, rng) -> tuple:
    """One blow, in the fixed order the contract pins down. Returns (event, damage)."""
    if rng.random() < defender["dodge"] * attacker["accuracy"]:
        return "dodge", 0

    raw = attacker["damage"]
    event = "hit"
    if rng.random() < attacker["crit"]:
        # The crit's own bonus is NOT applied here. It is added into the attack-side
        # bonus bundle in the main loop instead, so it stacks WITH the item passives
        # rather than multiplying the total of them -- see the "crit" term there.
        # Applied here it turned a +100% crit into a doubling of everything else too,
        # which is how four legendary passives came to land an 18,891 hit on a 7,809 HP
        # target. This branch only names the event now.
        event = "crit"

    raw *= 1 + rng.uniform(-C.DAMAGE_VARIANCE, C.DAMAGE_VARIANCE)

    reduced = raw * (1 - defender["reduction"])
    if raw - reduced >= 0.25 * raw:
        event = "blocked"

    damage = max(1, round(reduced))
    if damage <= 15 and event == "hit":
        event = "low_damage"
    return event, damage


def resolved_scroll_effects(spell: Mapping, personal_paint: bool = False) -> tuple[dict, ...]:
    """Return exact effects used by combat, including the safe artwork boost."""
    if not personal_paint:
        return tuple(dict(effect) for effect in spell.get("effects", ()) if isinstance(effect, Mapping))
    scaled = []
    for raw in spell.get("effects", ()):
        if not isinstance(raw, Mapping):
            continue
        effect = dict(raw)
        op = str(effect.get("op") or "")
        if op in {"damage", "burn"} and isinstance(effect.get("amount"), (int, float)):
            effect["amount"] = float(effect["amount"]) * 1.30
        elif op in {"heal", "shield", "regen", "self_damage"} \
                and isinstance(effect.get("percent"), (int, float)):
            if op != "self_damage":
                effect["percent"] = float(effect["percent"]) * 1.30
        elif op in {"weaken", "vulnerable", "damage_boost", "reflect_next"} \
                and isinstance(effect.get("value"), (int, float)):
            effect["value"] = float(effect["value"]) * 1.30
        scaled.append(effect)
    return tuple(scaled)


def simulate(a: "Fighter", b: "Fighter", rng=None, seed: int | None = None,
             max_actions: int | None = None) -> "FightResult":
    """Run one fight to a finish. Deterministic for a given seeded `rng`.

    Leadership alternates once a first mover is picked; the pick and every roll after it
    come from `rng`, so replaying the same rng state replays the identical fight -- which
    is the whole point of keeping this module pure.
    """
    if rng is not None and seed is not None:
        raise ValueError("pass either rng or seed, not both")
    rng = random.Random(seed) if seed is not None else (rng or random)
    # Зеркало души rewrites its wearer BEFORE anything is derived, because it changes the
    # four numbers everything else is computed from. Rolled off this rng like every other
    # decision here, so a stored seed still replays the fight exactly (see snapshot()).
    a, b = _mirror(a, b, rng), _mirror(b, a, rng)
    derived = {a.key: derive(a, b), b.key: derive(b, a)}
    fighters = {a.key: a, b.key: b}
    max_hp = {a.key: derived[a.key]["max_hp"], b.key: derived[b.key]["max_hp"]}
    hp = {
        fighter.key: min(max_hp[fighter.key], max(1, int(fighter.starting_hp)))
        if fighter.starting_hp is not None else max_hp[fighter.key]
        for fighter in (a, b)
    }
    total_damage = {a.key: 0, b.key: 0}
    effects = {a.key: derived[a.key]["effects"], b.key: derived[b.key]["effects"]}
    skill_loadouts = {}
    for fighter in (a, b):
        try:
            skill_loadouts[fighter.key] = (
                SCROLLS.validate_loadout(fighter.skills) if fighter.skills else ()
            )
        except ValueError:
            skill_loadouts[fighter.key] = ()
    equipped_shields = {
        fighter.key: dict(fighter.shield) if isinstance(fighter.shield, Mapping) else None
        for fighter in (a, b)
    }
    # Collector, Trophy Compass, Coin Rake and Survivor settle in pets.record_fight.
    # Merely equipping one of them must
    # not switch combat to the passive pipeline or alter an otherwise identical replay.
    non_combat_codes = {"collector", "trophy_compass", "coin_rake", "survivor"}
    effectful = any(
        effect["code"] not in non_combat_codes
        for fighter_effects in effects.values() for effect in fighter_effects
    ) or any(skill_loadouts.values()) or any(equipped_shields.values()) or any(
        fighter.damage_multiplier != 1.0 for fighter in (a, b)
    ) or any(
        fighter.magic_reflect_multiplier or fighter.enchant_reflect_multiplier
        for fighter in (a, b)
    ) or any(fighter.weapon_enchanted for fighter in (a, b)) or any(
        fighter.physical_damage_taken_multiplier != 1.0 for fighter in (a, b)
    ) or any(fighter.physical_damage_heals for fighter in (a, b))
    shields = {a.key: 0.0, b.key: 0.0}
    used = {a.key: set() for a in (a, b)}
    # Reactive shield powers are deliberately separate from item passives: a weapon
    # and a shield may use the same operation name without silently sharing a charge.
    used_shield_reactions = {a.key: set() for a in (a, b)}
    landed_hits = {a.key: 0 for a in (a, b)}
    attacks_made = {a.key: 0 for a in (a, b)}
    focused_ready = {a.key: False for a in (a, b)}
    retaliation_bonus = {a.key: 0.0 for a in (a, b)}
    gambler_bonus = {a.key: 0.0 for a in (a, b)}
    pending_poison: dict[str, tuple[str, int] | None] = {a.key: None, b.key: None}
    stunned = {a.key: False, b.key: False}
    cocooned = {a.key: False, b.key: False}
    armor_burst_ready = {a.key: False, b.key: False}
    # Counters rather than flags: chill and phantom_step both fired exactly once whatever
    # the catalogue said, which capped two legendaries at about half the tier they are
    # priced at. A `hits` of 1 reproduces the old behaviour exactly.
    chilled = {a.key: 0, b.key: 0}
    phantom_dodges = {a.key: 0, b.key: 0}
    acid_ready = {a.key: False, b.key: False}
    spring_hits_taken = {a.key: 0, b.key: 0}
    spring_ready = {a.key: False, b.key: False}
    armor_shredded = {a.key: 0.0, b.key: 0.0}
    burning: dict[str, tuple[str, int, int, tuple[str, ...]] | None] = {a.key: None, b.key: None}
    # A shield burn needs different wording from a scroll/weapon burn even though the
    # damage lifecycle is identical. Kept outside the tuple for snapshot compatibility.
    burning_shield = {a.key: None, b.key: None}

    # One live component per source of flame:
    # {origin: [source, turns_left, damage, types, growth]}.
    # `burning` above stays the single combined view the tick and the transcript read, so
    # nothing downstream (or in a stored snapshot) has to learn a new shape.
    burn_stacks: dict[str, dict[str, list]] = {a.key: {}, b.key: {}}
    burn_source = {a.key: None, b.key: None}

    def sync_burn(target_key: str) -> None:
        rows = burn_stacks[target_key]
        if not rows:
            burning[target_key] = None
            burning_shield[target_key] = None
            return
        types: list[str] = []
        for row in rows.values():
            types.extend(row[3])
        burning[target_key] = (
            burn_source[target_key] or next(iter(rows.values()))[0],
            max(row[1] for row in rows.values()),
            sum(row[2] for row in rows.values()),
            normalize_attack_types(types),
        )

    def ignite(
        target_key: str, source_key: str, damage: int, turns: int, attack_types,
        origin: str, growth: float = 0.0,
    ) -> None:
        """Lay a burn on somebody who may already be burning.

        Fire from DIFFERENT sources combines: a weapon rune, a scroll and a shield burning
        the same target are three flames on one body, and the tick is their sum. A plain
        assignment here used to throw the previous one away, so lighting a second fire
        could actively reduce the damage already ticking.

        The SAME source flaring up again does not stack -- it refreshes. `origin` is what
        separates the two cases, and it matters most for a passive that re-triggers on
        every landed hit: without it a ten-hit fight would end with a burn ten times its
        printed value. Each component keeps its own countdown and expires on its own
        schedule, so the newest flame always gets its full duration and a long burn is
        never cut short by a short one landing on top of it.
        """
        damage, turns = max(1, round(damage)), max(1, int(turns))
        row = burn_stacks[target_key].get(origin)
        if row is None:
            burn_stacks[target_key][origin] = [
                source_key, turns, damage, normalize_attack_types(attack_types),
                max(0.0, float(growth)), damage * C.BURN_GROWTH_CEILING,
            ]
        else:
            row[0] = source_key
            row[1] = max(row[1], turns)   # refresh, never shorten what is already burning
            # `max`, not assignment: a flame that has already grown must not be doused by
            # the next hit re-laying it at its opening temperature. Re-lighting a fire
            # feeds it; it does not start it over.
            row[2] = max(row[2], damage)
            row[3] = normalize_attack_types(attack_types)
            row[4] = max(0.0, float(growth))
            row[5] = max(row[5], damage * C.BURN_GROWTH_CEILING)
        burn_source[target_key] = source_key
        sync_burn(target_key)

    def tick_burn(target_key: str) -> None:
        """Age every component by one turn, feed the ones still alive, drop the rest.

        Growth is what makes fire a different threat from bleeding and poison rather than
        a third name for the same tick. Fire is the one that gets WORSE the longer it is
        left alone: standing in it is the mistake, and putting the fight away quickly is
        the answer to it.
        """
        rows = burn_stacks[target_key]
        for origin in list(rows):
            rows[origin][1] -= 1
            if rows[origin][1] <= 0:
                del rows[origin]
            elif rows[origin][4]:
                rows[origin][2] = max(1, min(
                    round(rows[origin][5]),
                    round(rows[origin][2] * (1 + rows[origin][4])),
                ))
        sync_burn(target_key)

    def douse(target_key: str) -> None:
        burn_stacks[target_key].clear()
        sync_burn(target_key)
    venom_miss = {a.key: 0.0, b.key: 0.0}
    pending_venom: dict[str, tuple[str, int] | None] = {a.key: None, b.key: None}
    bleeding: dict[str, tuple[str, int, int] | None] = {a.key: None, b.key: None}
    afterimage_bonus = {a.key: 0.0, b.key: 0.0}
    damage_weakened = {a.key: 0.0, b.key: 0.0}
    # Legendary and cursed-legendary state. Every one of these is per fight and dies with
    # it, exactly like the dicts above, so a snapshot replay still reproduces the fight.
    extra_attacks = {a.key: 0, b.key: 0}   # chain_crit / double_strike follow-ups owed
    chained = {a.key: 0, b.key: 0}         # follow-ups owed specifically to chain_crit
    shatter_stacks = {a.key: 0, b.key: 0}  # shards sitting ON this key, laid by the other
    vulnerable = {a.key: 0.0, b.key: 0.0}  # extra share of incoming damage taken
    charging = {a.key: 0, b.key: 0}        # charge_crit turns already spent winding up
    blind_cycle = {a.key: 0, b.key: 0}     # blind_fury position in its sighted/blind loop
    hits_taken = {a.key: 0, b.key: 0}      # pressure: every blow that actually landed
    skip_turn = {a.key: False, b.key: False}  # recoil knocked this fighter out of a turn
    in_followup = {a.key: False, b.key: False}  # this strike is an earned extra attack
    heal_cut = {a.key: 0.0, b.key: 0.0}    # share of incoming healing bleeding denies
    poison_weaken = {a.key: 0.0, b.key: 0.0}  # poison sapping this fighter's next blow
    stun_procs = {a.key: 0, b.key: 0}
    guards = {a.key: 0.0, b.key: 0.0}
    used_scrolls = {a.key: set() for a in (a, b)}
    skill_statuses = {a.key: {} for a in (a, b)}
    for _key in (a.key, b.key):
        # Glass is glass from the opening bell, not from the first hit: the penalty half of
        # the bargain has to be live before anybody swings, or the curse would be a free
        # damage bonus for whoever moves first.
        if (_glass := _effect_value(effects[_key], "glass_body")) is not None:
            vulnerable[_key] += max(0, _fraction(_param(
                effects[_key], "glass_body", "taken", _glass,
            )))

    def healed_amount(key: str, amount: float) -> float:
        """What actually reaches a fighter's health bar, after bleeding takes its share.

        Every heal in the fight is routed through this -- lifesteal, regeneration, a
        medkit, a scroll, a shield's return. That is the point: bleeding is supposed to be
        the answer to a build that out-heals its damage, and a cut that only applied to
        some sources would just push players onto the ones it missed.
        """
        return max(0.0, float(amount)) * max(0.0, 1.0 - heal_cut[key])

    def swing_share(source_key: str, percent: float, code: str) -> int:
        """Hit points for one tick of a passive written as a share of the owner's swing."""
        spec = _effect(effects[source_key], code) or {}
        if spec.get("level_scaled") is False:
            # This value is already hit points and already derived from its owner's power
            # -- weapon rune fire computes it that way in pets_config. Same flag, same
            # meaning as before the catalogue moved to percentages: do not transform it.
            return max(1, round(max(0, percent)))
        fighter = fighters[source_key]
        return _share_of_swing(
            percent, fighter, C.BASE_DAMAGE + fighter.strength * C.DAMAGE_PER_POINT,
        )

    def queue_damage(
        queue: dict[str, tuple[str, int] | None], target_key: str,
        source_key: str, damage: int,
    ) -> None:
        """Keep every advertised poison hit until the target's next action."""
        pending = queue[target_key]
        if pending is not None and pending[0] == source_key:
            damage += pending[1]
        queue[target_key] = (source_key, damage)

    initiative = .5
    if effectful:
        if (value := _effect_value(effects[a.key], "first_strike")) is not None:
            initiative += _fraction(value)
        if (value := _effect_value(effects[b.key], "first_strike")) is not None:
            initiative -= _fraction(value)
    order = [a.key, b.key] if rng.random() < min(.95, max(.05, initiative)) else [b.key, a.key]
    opening = pets_flavor.line("opening", fighters[order[0]].name, fighters[order[1]].name, rng=rng)

    def combat_state() -> dict:
        """JSON-safe state immediately after the event being appended."""
        rows = {}
        for key in (a.key, b.key):
            rows[key] = {
                "hp": max(0, round(hp[key])), "max_hp": round(max_hp[key]),
                "shield": round(shields[key], 3), "guard": round(guards[key], 3),
                "stunned": bool(stunned[key]), "cocooned": bool(cocooned[key]),
                "chilled_hits": int(chilled[key]), "phantom_dodges": int(phantom_dodges[key]),
                "armor_shredded": round(armor_shredded[key], 4),
                "venom_miss": round(venom_miss[key], 4),
                "damage_weakened": round(damage_weakened[key], 4),
                "burning": list(burning[key]) if burning[key] is not None else None,
                "bleeding": list(bleeding[key]) if bleeding[key] is not None else None,
                "pending_poison": list(pending_poison[key]) if pending_poison[key] is not None else None,
                "pending_venom": list(pending_venom[key]) if pending_venom[key] is not None else None,
                "skill_statuses": dict(skill_statuses[key]),
                "attacks_made": int(attacks_made[key]), "landed_hits": int(landed_hits[key]),
                "total_damage": int(total_damage[key]), "stun_procs": int(stun_procs[key]),
                "used_effects": sorted(used[key]),
                "used_shield_reactions": sorted(used_shield_reactions[key]),
                "used_scrolls": sorted(used_scrolls[key]),
            }
        return {"fighters": rows}

    class _AuditedRounds(list):
        def append(self, row):
            super().append(replace(row, state=combat_state()))

    rounds = _AuditedRounds()

    for owner_key, other_key in ((a.key, b.key), (b.key, a.key)):
        for stat in derived[owner_key]["deficits"]:
            rounds.append(Round(
                number=0, attacker=owner_key, event=f"deficit_{stat}", damage=0,
                attacker_hp=round(hp[owner_key]), defender_hp=round(hp[other_key]),
                text=_DEFICIT_TEXT[stat].format(name=fighters[owner_key].name),
                is_action=False,
            ))

    def effect_round(
        number: int, owner_key: str, other_key: str, code: str, amount: int = 0,
        attack_types=(PHYSICAL,), *, is_action: bool = False,
    ):
        """Put an equipped-item proc in the normal transcript, without flavour RNG."""
        template = _EFFECT_TEXT.get(code, "срабатывает эффект снаряжения.")
        rounds.append(Round(
            number=number, attacker=owner_key, event=f"amulet_{code}", damage=amount,
            attacker_hp=round(hp[owner_key]), defender_hp=round(hp[other_key]),
            text=f"✨ {fighters[owner_key].name} {template.format(amount=amount)}",
            attack_types=normalize_attack_types(attack_types),
            is_action=is_action,
        ))

    def shield_round(
        number: int, owner_key: str, other_key: str, code: str, text: str,
        amount: int = 0, attack_types=(PHYSICAL,),
    ) -> None:
        """Append one explicitly named shield consequence, never an extra action."""
        rounds.append(Round(
            number=number, attacker=owner_key, event=f"shield_{code}", damage=amount,
            attacker_hp=round(hp[owner_key]), defender_hp=round(hp[other_key]),
            text=f"↳ 🛡 {text}", attack_types=normalize_attack_types(attack_types),
            is_action=False,
        ))

    def shield_name(key: str) -> str:
        return str((equipped_shields[key] or {}).get("name") or "Базовая защита")

    def display_shield_consequences(
        rows_to_order: list[Round], source_key: str, target_key: str,
    ) -> tuple[list[Round], int, int]:
        """Give the replay truthful HP frames: hit, then heal/counter consequences."""
        healed = sum(
            -row.damage for row in rows_to_order
            if row.event == "shield_damage_heal" and row.damage < 0
        )
        countered = sum(
            row.damage for row in rows_to_order
            if row.event == "shield_counterattack" and row.damage > 0
        )
        view_hp = {
            source_key: max(0, round(hp[source_key]) + countered),
            target_key: max(0, round(hp[target_key]) - healed),
        }
        primary_source_hp = view_hp[source_key]
        primary_target_hp = view_hp[target_key]
        ordered = []
        for row in rows_to_order:
            if row.event == "shield_damage_heal":
                view_hp[target_key] += max(0, -row.damage)
            elif row.event == "shield_counterattack":
                view_hp[source_key] = max(0, view_hp[source_key] - max(0, row.damage))
            other_key = target_key if row.attacker == source_key else source_key
            state = dict(row.state or {})
            state_fighters = {
                key: dict(value) for key, value in (state.get("fighters") or {}).items()
            }
            for key, value in view_hp.items():
                if key in state_fighters:
                    state_fighters[key]["hp"] = round(value)
            if state_fighters:
                state["fighters"] = state_fighters
            ordered.append(replace(
                row,
                attacker_hp=round(view_hp[row.attacker]),
                defender_hp=round(view_hp[other_key]),
                state=state or row.state,
            ))
        return ordered, primary_source_hp, primary_target_hp

    def hurt(
        source_key: str, target_key: str, damage: int, number: int,
        pierce_guard: float = 0.0,
        allow_shield_reactions: bool = False,
    ) -> tuple[int, bool]:
        """Apply damage and one-shot defensive effects. Returns (impact, knockout)."""
        damage = max(0, int(damage))
        damage = round(damage * derived[target_key]["incoming_damage_multiplier"])
        # A curse that trades defence for offence is charged here rather than at the
        # attacker's multiplier block, so it also applies to burns, bleeds, thorns and
        # every other source that reaches a fighter without going through an attack roll.
        # That is the whole bargain: a glass body is glass to everything.
        if effectful and damage and vulnerable[target_key]:
            damage = round(damage * (1 + vulnerable[target_key]))
        # The parry is an incoming-hit reaction, not a Defend hook: its wearer may be
        # attacked while taking any action.  It can proc once per fight, so it buys a
        # meaningful turn without creating a stun lock against a boss or a pet.
        if allow_shield_reactions and effectful and damage and source_key != target_key:
            shield = equipped_shields[target_key] or {}
            for effect in shield.get("on_hit_effects", ()):
                if str(effect.get("op") or "") != "parry_stun" \
                        or "parry_stun" in used_shield_reactions[target_key]:
                    continue
                chance = max(0.0, min(1.0, float(effect.get("chance", 0) or 0)))
                # This is the shield's first-hit roll, whether it succeeds or not.
                # Retrying a miss on every hit would make the listed chance misleading.
                used_shield_reactions[target_key].add("parry_stun")
                if rng.random() >= chance:
                    continue
                before_parry = damage
                reduction = max(0.0, min(1.0, float(effect.get("reduce", 0) or 0)))
                damage = round(damage * (1.0 - reduction))
                parried = max(0, before_parry - damage)
                if parried:
                    stunned[source_key] = True
                    shield_round(
                        number, target_key, source_key, "parry_stun",
                        f"{fighters[target_key].name} щитом «{shield_name(target_key)}» "
                        f"парирует {parried} урона и оглушает {fighters[source_key].name}. "
                        f"{fighters[source_key].name} пропустит следующий ход.",
                        parried,
                    )
        if damage and guards[target_key] > 0:
            before_guard = damage
            blocked_share = guards[target_key] * max(0.0, 1.0 - min(1.0, pierce_guard))
            damage = round(damage * max(0.0, 1.0 - blocked_share))
            guards[target_key] = 0.0
            absorbed = before_guard - damage
            shield_round(
                number, target_key, source_key, "guard",
                f"защита {fighters[target_key].name} щитом «{shield_name(target_key)}» "
                f"поглощает {absorbed} урона из этой атаки. Защита израсходована.",
                absorbed,
            )
        if effectful and damage and str(source_key).startswith("mob:") \
                and (value := _effect_value(effects[target_key], "mob_ward")) is not None:
            damage = round(damage * max(.10, 1 - max(0, _fraction(value))))
        if effectful and damage and "perfect_parry" not in used[target_key] \
                and (value := _effect_value(effects[target_key], "perfect_parry")) is not None:
            used[target_key].add("perfect_parry")
            before_parry = damage
            damage = round(damage * max(.10, 1 - max(0, _fraction(value))))
            absorbed = max(0, before_parry - damage)
            retaliation_bonus[target_key] += absorbed
            effect_round(number, target_key, source_key, "perfect_parry", absorbed)
        if effectful and damage and "safeguard" not in used[target_key] \
                and (value := _effect_value(effects[target_key], "safeguard")) is not None:
            used[target_key].add("safeguard")
            before_safeguard = damage
            damage = round(damage * max(.10, 1 - max(0, _fraction(value))))
            effect_round(number, target_key, source_key, "safeguard", before_safeguard - damage)
        if effectful and damage and armor_burst_ready[target_key]:
            armor_burst_ready[target_key] = False
            before_burst = damage
            value = _effect_value(effects[target_key], "armor_burst") or 0
            damage = round(damage * max(.10, 1 - max(0, _fraction(value))))
            effect_round(number, target_key, source_key, "armor_burst", before_burst - damage)
        if effectful and shields[target_key] > 0 and damage:
            absorbed = min(shields[target_key], damage)
            shields[target_key] -= absorbed
            damage -= absorbed
            # The normal hit line remains the readable primary action; this tells the
            # player why its HP did not move by that amount.
            effect_round(number, target_key, source_key, "opening_shield", round(absorbed))
        damage = max(0, damage)
        rescue = next(
            (code for code in ("soul_debt", "rewind", "death_shield", "last_stand")
             if code not in used[target_key] and _effect_value(effects[target_key], code) is not None),
            None,
        )
        if effectful and damage >= hp[target_key] and rescue:
            used[target_key].add(rescue)
            if rescue == "soul_debt":
                # Cheaper than rewind at the moment it fires and far more expensive
                # afterwards: the debt is collected for the rest of the fight, on every
                # source of damage, which is what makes this a curse and not a second life.
                restored = max(1, round(max_hp[target_key] * max(0, _fraction(
                    _effect_value(effects[target_key], "soul_debt") or 0
                ))))
                hp[target_key] = restored
                vulnerable[target_key] += max(0, _fraction(_param(
                    effects[target_key], "soul_debt", "debt", 100,
                )))
                effect_round(number, target_key, source_key, rescue, restored)
                return 0, False
            if rescue == "rewind":
                restored = max(1, round(max_hp[target_key] * max(0, _fraction(
                    _effect_value(effects[target_key], "rewind") or 0
                ))))
                hp[target_key] = restored
                effect_round(number, target_key, source_key, rescue, restored)
                return 0, False
            impact = max(0, round(hp[target_key]) - 1)
            hp[target_key] = 1.0
            if rescue == "death_shield":
                shields[target_key] += max(
                    1, round(max_hp[target_key] * max(0, _fraction(
                        _effect_value(effects[target_key], "death_shield") or 0
                    )))
                )
            effect_round(number, target_key, source_key, rescue)
            return impact, False
        before = hp[target_key]
        hp[target_key] = max(0.0, hp[target_key] - damage)
        return round(before - hp[target_key]), hp[target_key] <= 0

    def apply_attack(
        source_key: str, target_key: str, damage: int, number: int, *,
        attack_types=(PHYSICAL,), enchanted: bool = False, pierce_guard: float = 0.0,
        allow_reflection: bool = True, allow_shield_reactions: bool = True,
    ) -> tuple[int, bool, str | None]:
        """Deal one classified attack and, where applicable, resolve Antimage's return.

        Reflection happens only after the original hit lands.  Its return is marked as
        magical but cannot itself be reflected; otherwise two reflective rules could
        recurse forever instead of producing a readable, deterministic combat log.
        """
        attack_types = normalize_attack_types(attack_types)
        impact, knocked_out = hurt(
            source_key, target_key, damage, number, pierce_guard=pierce_guard,
            allow_shield_reactions=allow_shield_reactions,
        )
        if knocked_out or not impact:
            return impact, knocked_out, None
        if allow_shield_reactions:
            reaction_winner = resolve_shield_on_hit(source_key, target_key, impact, number)
            if reaction_winner:
                return impact, False, reaction_winner
        if not allow_reflection:
            return impact, False, None
        defender = fighters[target_key]
        reflect = max(
            defender.magic_reflect_multiplier if is_magic_attack(attack_types) else 0.0,
            defender.enchant_reflect_multiplier if enchanted else 0.0,
        )
        if reflect <= 0:
            return impact, False, None
        reflected = max(1, round(impact * reflect))
        back_impact, back_knockout = hurt(target_key, source_key, reflected, number)
        total_damage[target_key] += back_impact
        rounds.append(Round(
            number=number, attacker=target_key, event="antimagic_reflect",
            damage=back_impact, attacker_hp=round(hp[target_key]),
            defender_hp=round(hp[source_key]),
            text=(f"✦ {fighters[target_key].name} отражает {back_impact} магического урона "
                  f"обратно в {fighters[source_key].name}."),
            attack_types=(MAGIC,),
            is_action=False,
        ))
        return impact, False, target_key if back_knockout else None

    def resolve_shield_on_hit(
        source_key: str, target_key: str, impact: int, number: int,
    ) -> str | None:
        """Resolve the worn shield's post-hit hooks from actual HP lost.

        ``impact`` is returned by :func:`hurt` after armour, guard, barriers and rescue
        effects.  Healing from it therefore means exactly what the item says: a share
        of health actually lost, never a share of an advertised raw attack.  Counter-
        attacks are one per fight and suppress further shield reactions so two reactive
        shields cannot ping-pong forever.
        """
        if not impact:
            return None
        shield = equipped_shields[target_key] or {}
        for effect in shield.get("on_hit_effects", ()):
            op = str(effect.get("op") or "")
            if op == "damage_heal":
                percent = max(0.0, min(1.0, float(effect.get("percent", 0) or 0)))
                before = hp[target_key]
                hp[target_key] = min(max_hp[target_key], hp[target_key] + healed_amount(target_key, impact * percent))
                healed = round(hp[target_key] - before)
                if healed:
                    net = max(0, impact - healed)
                    shield_round(
                        number, target_key, source_key, "damage_heal",
                        f"«{shield_name(target_key)}» после прямого попадания возвращает "
                        f"{fighters[target_key].name} {healed} HP. Получено {impact}, "
                        f"итоговая потеря — {net} HP.",
                        -healed,
                    )
            elif op == "counterattack" and "counterattack" not in used_shield_reactions[target_key]:
                used_shield_reactions[target_key].add("counterattack")
                percent = max(0.0, float(effect.get("percent", 0) or 0))
                raw_counter = max(1, round(derived[target_key]["damage"] * percent))
                counter_impact, counter_ko, _counter_reflection = apply_attack(
                    target_key, source_key, raw_counter, number,
                    attack_types=(PHYSICAL,), allow_reflection=False,
                    allow_shield_reactions=False,
                )
                total_damage[target_key] += counter_impact
                shield_round(
                    number, target_key, source_key, "counterattack",
                    f"{fighters[target_key].name} щитом «{shield_name(target_key)}» отвечает "
                    f"контрударом и наносит {fighters[source_key].name} {counter_impact} урона. "
                    "Контрудар не расходует отдельный ход.",
                    counter_impact, (PHYSICAL,),
                )
                if counter_ko:
                    return target_key
        return None

    def skill_value(key: str, name: str) -> float:
        value = skill_statuses[key].get(name)
        return float(value[0]) if isinstance(value, list) and value else 0.0

    def put_skill_status(key: str, name: str, value: float, turns: int) -> None:
        old = skill_statuses[key].get(name)
        current_value = float(old[0]) if isinstance(old, list) and old else 0.0
        current_turns = int(old[1]) if isinstance(old, list) and len(old) > 1 else 0
        skill_statuses[key][name] = [max(current_value, float(value)), max(current_turns, turns)]

    def tick_skill_state(key: str) -> None:
        for name in ("blind", "weaken", "vulnerable", "damage_boost", "regen"):
            row = skill_statuses[key].get(name)
            if not isinstance(row, list):
                continue
            row[1] -= 1
            if row[1] <= 0:
                skill_statuses[key].pop(name, None)
    def harmful_status_allowed(target_key: str) -> bool:
        if not skill_statuses[target_key].pop("negative_ward", False):
            return True
        rounds.append(Round(
            number=0, attacker=target_key, event="skill_ward", damage=0,
            attacker_hp=round(hp[target_key]), defender_hp=round(hp[_other_key(target_key)]),
            text=f"🎗 {fighters[target_key].name} блокирует негативный эффект.",
            is_action=False,
        ))
        return False

    def _other_key(key: str) -> str:
        return b.key if key == a.key else a.key

    def reflect_skill_damage(source_key: str, target_key: str, impact: int, number: int) -> str | None:
        reflected = float(skill_statuses[target_key].pop("reflect_next", 0) or 0)
        if not reflected or not impact:
            return None
        back = max(1, round(impact * reflected))
        back_impact, back_ko, antimagic_winner = apply_attack(
            target_key, source_key, back, number, attack_types=(MAGIC,),
        )
        total_damage[target_key] += back_impact
        rounds.append(Round(
            number=number, attacker=target_key, event="skill_reflect", damage=back_impact,
            attacker_hp=round(hp[target_key]), defender_hp=round(hp[source_key]),
            text=f"🌹 {fighters[target_key].name} отражает {back_impact} урона.",
            attack_types=(MAGIC,),
            is_action=False,
        ))
        return antimagic_winner or (target_key if back_ko else None)

    def spell_damage(
        source_key: str, target_key: str, effect: Mapping, number: int,
        attack_types=(MAGIC,),
    ) -> tuple[int, str | None]:
        raw = (derived[source_key]["damage"] * fighters[source_key].damage_multiplier
               * max(0.0, float(effect.get("amount", 1.0))))
        raw *= 1 + rng.uniform(-C.DAMAGE_VARIANCE, C.DAMAGE_VARIANCE)
        if rng.random() < derived[source_key]["crit"]:
            raw *= C.CRIT_MULTIPLIER
        raw *= max(.10, 1.0 - skill_value(source_key, "weaken"))
        raw *= 1.0 + skill_value(source_key, "damage_boost")
        raw *= 1.0 + skill_value(target_key, "vulnerable")
        armor = derived[target_key]["reduction"] * max(
            0.0, 1.0 - min(1.0, float(effect.get("pierce_armor", 0) or 0)),
        )
        raw *= 1.0 - armor
        impact, knocked_out, reflection_winner = apply_attack(
            source_key, target_key, max(1, round(raw)), number,
            pierce_guard=max(0.0, float(effect.get("pierce_guard", 0) or 0)),
            attack_types=attack_types,
        )
        total_damage[source_key] += impact
        # A damage scroll can carry a share of its *actual* landed damage back to its
        # caster.  This is intentionally based on ``impact`` after armour, guards and
        # defensive procs, rather than its advertised raw multiplier: Predator Bite
        # promises to heal from what it dealt, and a fully blocked bite dealt nothing.
        lifesteal = max(0.0, min(1.0, float(effect.get("lifesteal", 0) or 0)))
        # A reactive counter can kill the caster before this post-hit step.  A dead
        # caster must not be revived by a lifesteal record written after the counter.
        if impact and lifesteal and not reflection_winner:
            before = hp[source_key]
            hp[source_key] = min(max_hp[source_key], hp[source_key] + healed_amount(source_key, impact * lifesteal))
            healed = round(hp[source_key] - before)
            if healed:
                rounds.append(Round(
                    number=number, attacker=source_key, event="skill_lifesteal", damage=-healed,
                    attacker_hp=round(hp[source_key]), defender_hp=round(hp[target_key]),
                    text=f"💚 {fighters[source_key].name} восстанавливает {healed} HP от удара.",
                    attack_types=normalize_attack_types(attack_types),
                    is_action=False,
                ))
        reflected_winner = None if knocked_out or reflection_winner else reflect_skill_damage(
            source_key, target_key, impact, number,
        )
        return impact, reflection_winner or reflected_winner or (source_key if knocked_out else None)

    def apply_scroll_effect(
        source_key: str, target_key: str, effect: Mapping, number: int,
        attack_types=(MAGIC,), origin: str = "scroll",
    ) -> tuple[int, str | None]:
        op = str(effect.get("op") or "")
        if op == "damage":
            return spell_damage(source_key, target_key, effect, number, attack_types)
        if op in {"burn", "weaken", "blind", "vulnerable", "stun"} \
                and not harmful_status_allowed(target_key):
            return 0, None
        if op == "heal":
            hp[source_key] = min(
                max_hp[source_key],
                hp[source_key] + healed_amount(
                    source_key, max_hp[source_key] * max(0.0, float(effect.get("percent", 0))),
                ),
            )
        elif op == "shield":
            shields[source_key] = min(
                max_hp[source_key], shields[source_key] + max(
                    1, round(max_hp[source_key] * max(0.0, float(effect.get("percent", 0))))
                ),
            )
        elif op == "burn":
            ignite(
                target_key, source_key,
                max(1, round(derived[source_key]["damage"] * max(0.0, float(effect.get("amount", 0))))),
                max(1, int(effect.get("turns", 1))),
                attack_types, origin,
            )
            # Generic scroll/weapon burns replace the attribution left by a shield.
            burning_shield[target_key] = None
        elif op in {"weaken", "blind", "vulnerable"}:
            put_skill_status(
                target_key, op, max(0.0, float(effect.get("value", 0))),
                max(1, int(effect.get("turns", 1))),
            )
        elif op == "stun":
            stunned[target_key] = True
        elif op == "dodge_next":
            skill_statuses[source_key]["dodge_next"] = True
        elif op == "reflect_next":
            skill_statuses[source_key]["reflect_next"] = max(
                float(skill_statuses[source_key].get("reflect_next", 0) or 0),
                max(0.0, float(effect.get("value", 0))),
            )
        elif op == "cleanse":
            douse(source_key)
            stunned[source_key] = False
            for name in ("burn", "blind", "weaken", "vulnerable"):
                skill_statuses[source_key].pop(name, None)
        elif op == "break_shield":
            shields[target_key] = 0.0
        elif op == "regen":
            # +1 because the status is created during this action, which is ticked below.
            put_skill_status(
                source_key, "regen",
                max_hp[source_key] * max(0.0, float(effect.get("percent", 0))),
                max(1, int(effect.get("turns", 1))) + 1,
            )
        elif op == "damage_boost":
            put_skill_status(
                source_key, "damage_boost", max(0.0, float(effect.get("value", 0))),
                max(1, int(effect.get("turns", 1))) + 1,
            )
        elif op == "negative_ward":
            skill_statuses[source_key]["negative_ward"] = True
        elif op == "self_damage":
            self_harm = max(1, round(
                max_hp[source_key] * max(0.0, float(effect.get("percent", 0)))
            ))
            before = hp[source_key]
            hp[source_key] = max(0.0, hp[source_key] - self_harm)
            if hp[source_key] <= 0:
                return 0, target_key
            total_damage[target_key] += round(before - hp[source_key])
        return 0, None

    def personal_paint_scroll_effects(source_key: str, code: str, spell: Mapping) -> tuple[dict, ...]:
        """Return a spell's effects, with a personal paint rune's safe +30% boost.

        Damage multipliers, heals, barriers and deterministic status strengths are
        numeric power.  We intentionally leave ``turns``, stun, dodge, cleanse and the
        blind chance untouched: the artwork is stronger, not longer or more random.
        """
        return resolved_scroll_effects(
            spell, code in set(fighters[source_key].personal_enchanted_scrolls or ()),
        )

    def active_actions(key: str) -> list[str]:
        loadout = skill_loadouts[key]
        actions = ["attack"]
        # Defend is deliberately unavailable on the fighter's first real action.  A
        # shield is a response after combat has started, not an opening barrier; allowing
        # it at action zero made shield hooks visibly fire on the first line of live logs.
        # ``attacks_made`` counts attacks, scrolls, Defend and Cocoon alike, so being
        # stunned before acting does not accidentally unlock a shield early.
        # Defend only when there is no guard already standing. A guard is a one-shot
        # block: it is set to a flat value here and zeroed by the hit it absorbs, so
        # raising it a second time writes the same number and buys nothing. That is not
        # a rare case -- leadership alternates round to round, so each fighter acts twice
        # in a row every other round (a, b, b, a, a, b), and a pair of Defends inside
        # that back-to-back turn threw the first one away.
        if attacks_made[key] >= 1 and guards[key] <= 0:
            actions.append("defend")
        for index, code in enumerate(loadout):
            # An empty slot offers nothing to choose. It still costs the creature
            # nothing else: Defend and the full action budget come with having slots,
            # not with having filled them.
            if not code or code in used_scrolls[key]:
                continue
            actions.append(f"skill_{index + 1}")
        return actions

    def start_active_turn(key: str, other_key: str, number: int) -> None:
        regen = skill_statuses[key].get("regen")
        if isinstance(regen, list) and regen[0] > 0 and hp[key] > 0:
            before = hp[key]
            hp[key] = min(max_hp[key], hp[key] + healed_amount(key, regen[0]))
            healed = round(hp[key] - before)
            if healed:
                rounds.append(Round(
                    number=number, attacker=key, event="skill_regen", damage=-healed,
                    attacker_hp=round(hp[key]), defender_hp=round(hp[other_key]),
                    text=f"💧 {fighters[key].name} восстанавливает {healed} HP.",
                    is_action=False,
                ))

    def take_active_action(
        source_key: str, target_key: str, action: str, number: int,
    ) -> str | None:
        if action == "defend":
            shield = equipped_shields[source_key] or {}
            guards[source_key] = max(.10, min(.80, float(shield.get("guard", .40) or .40)))
            name = shield_name(source_key)
            rounds.append(Round(
                number=number, attacker=source_key, event="defend", damage=0,
                attacker_hp=round(hp[source_key]), defender_hp=round(hp[target_key]),
                text=(f"🛡 {fighters[source_key].name} поднимает щит «{name}»: "
                      f"следующая прямая атака будет слабее на "
                      f"{round(guards[source_key] * 100)}%. Это не лечение."),
            ))
            for effect in shield.get("defend_effects", ()):
                op = str(effect.get("op") or "effect")
                before_hp = hp[source_key]
                before_barrier = shields[source_key]
                _impact, winner = apply_scroll_effect(
                    source_key, target_key, effect, number, (MAGIC,),
                    origin=f"shield:{name}",
                )
                if op == "burn":
                    burning_shield[target_key] = name
                    burn = burning[target_key]
                    if burn is not None:
                        _owner, turns, damage, _types = burn
                        detail = (
                            f"«{name}» поджигает {fighters[target_key].name}: "
                            f"{damage} урона перед каждым из {turns} следующих ходов. "
                            "Огонь не расходует ход."
                        )
                        shield_round(number, source_key, target_key, "defend_burn", detail)
                elif op == "heal":
                    healed = max(0, round(hp[source_key] - before_hp))
                    detail = (
                        f"особый эффект «{name}» лечит "
                        f"{fighters[source_key].name} на {healed} HP. "
                        "Базовая защита сама по себе HP не восстанавливает."
                    )
                    shield_round(number, source_key, target_key, "defend_heal", detail, -healed)
                elif op == "shield":
                    barrier = max(0, round(shields[source_key] - before_barrier))
                    shield_round(
                        number, source_key, target_key, "defend_barrier",
                        f"«{name}» создаёт барьер на {barrier} HP. Барьер — не лечение.",
                        barrier,
                    )
                elif op == "reflect_next":
                    value = round(max(0.0, float(effect.get("value", 0))) * 100)
                    shield_round(number, source_key, target_key, "defend_reflect",
                                 f"«{name}» отразит {value}% урона следующей прямой атаки.")
                elif op == "blind":
                    value = round(max(0.0, float(effect.get("value", 0))) * 100)
                    shield_round(number, source_key, target_key, "defend_blind",
                                 f"«{name}» ослепляет {fighters[target_key].name}: следующая атака промахнётся с шансом {value}%.")
                elif op == "weaken":
                    value = round(max(0.0, float(effect.get("value", 0))) * 100)
                    shield_round(number, source_key, target_key, "defend_weaken",
                                 f"«{name}» ослабляет следующую атаку {fighters[target_key].name} на {value}%.")
                elif op == "cleanse":
                    shield_round(number, source_key, target_key, "defend_cleanse",
                                 f"«{name}» снимает с {fighters[source_key].name} огонь, оглушение, ослепление и ослабления.")
                elif op == "dodge_next":
                    shield_round(number, source_key, target_key, "defend_dodge",
                                 f"«{name}» позволит {fighters[source_key].name} гарантированно уклониться от следующей атаки.")
                elif op == "damage_boost":
                    value = round(max(0.0, float(effect.get("value", 0))) * 100)
                    turns = max(1, int(effect.get("turns", 1)))
                    attacks_text = "следующую атаку" if turns == 1 else f"следующие {turns} атаки"
                    shield_round(number, source_key, target_key, "defend_damage_boost",
                                 f"«{name}» усиливает {attacks_text} {fighters[source_key].name} на {value}%.")
                if winner:
                    return winner
            tick_skill_state(source_key)
            return None

        index = int(action.removeprefix("skill_")) - 1
        code = skill_loadouts[source_key][index]
        spell = SCROLLS.scroll(code)
        if spell is None:
            # active_actions never offers an empty slot, so this cannot be reached today.
            # It stays because the alternative failure is a crashed fight: a hole in a
            # loadout must never be the thing that takes a live battle down.
            tick_skill_state(source_key)
            return None
        spell_effects = personal_paint_scroll_effects(source_key, code, spell)
        used_scrolls[source_key].add(code)
        spell_attack_types = (ELEMENTAL, MAGIC) if spell.get("element") else (MAGIC,)
        harmful = any(effect.get("op") in {
            "damage", "burn", "weaken", "blind", "vulnerable", "stun", "break_shield",
        } for effect in spell_effects)
        dodged = False
        if harmful and spell["dodgeable"]:
            if skill_statuses[target_key].pop("dodge_next", False):
                dodged = True
            else:
                miss_chance = min(
                    .85,
                    derived[target_key]["dodge"] * derived[source_key]["accuracy"]
                    + skill_value(source_key, "blind"),
                )
                dodged = rng.random() < miss_chance
        impact = 0
        winner = None
        if not dodged:
            for effect in spell_effects:
                dealt, winner = apply_scroll_effect(
                    source_key, target_key, effect, number, spell_attack_types,
                    origin=f"scroll:{code}",
                )
                impact += dealt
                if winner:
                    break
        rounds.append(Round(
            number=number, attacker=source_key,
            event="skill_dodge" if dodged else f"skill_{code}", damage=impact,
            attacker_hp=round(hp[source_key]), defender_hp=round(hp[target_key]),
            text=(f"💨 {fighters[target_key].name} ускользает от «{spell['name']}»." if dodged
                  else f"{spell['icon']} {fighters[source_key].name}: "
                       f"{str(spell['short']).rstrip('.')}"
                       + (f" — {impact} урона." if impact else ".")),
            attack_types=spell_attack_types,
        ))
        tick_skill_state(source_key)
        return winner

    if effectful:
        for owner_key, other_key in ((a.key, b.key), (b.key, a.key)):
            if (value := _effect_value(effects[owner_key], "opening_shield")) is not None:
                amount = max(1, round(max_hp[owner_key] * _fraction(value)))
                shields[owner_key] += amount
                effect_round(0, owner_key, other_key, "opening_shield", amount)
            if _effect_value(effects[owner_key], "battle_cry") is not None:
                effect_round(0, owner_key, other_key, "battle_cry")
            # Both gambles take an explicit `chance` of landing the good half. It used to
            # be a hardcoded coin flip, which made these two items worth nothing however
            # big the numbers grew: win rate responds to a damage multiplier steeply but
            # saturates, so an even bet between "+60% and nearly certain to win" and
            # "-30% and nearly certain to lose" averages back to the coin flip it started
            # as. Measured: gambler at 60/-30 scored BELOW carrying no amulet at all.
            # A gamble is only worth wearing when the downside stays small or the odds
            # are tilted, so the catalogue now says which of the two it is buying.
            for code in ("gambler", "candle"):
                if (value := _effect_value(effects[owner_key], code)) is None:
                    continue
                downside = _param(effects[owner_key], code, "downside", abs(value) / 2)
                chance = max(0.0, min(1.0, _param(effects[owner_key], code, "chance", 50) / 100))
                roll = _fraction(value if rng.random() < chance else -downside)
                gambler_bonus[owner_key] += roll
                effect_round(0, owner_key, other_key, code, round(roll * 100))
    signatures = {}
    for key in (a.key, b.key):
        signature = derived[key]["signature"]
        if signature and rng.random() < C.SIGNATURE_TRIGGER_CHANCES[signature[0]][signature[1]]:
            signatures[key] = signature

    if effectful:
        for owner_key, other_key in ((a.key, b.key), (b.key, a.key)):
            if (value := _effect_value(effects[owner_key], "opening_blast")) is None:
                continue
            damage = max(1, round(hp[other_key] * max(0.0, _fraction(value))))
            impact, knocked_out, reflection_winner = apply_attack(
                owner_key, other_key, damage, 0, attack_types=(MAGIC,),
            )
            total_damage[owner_key] += impact
            effect_round(0, owner_key, other_key, "opening_blast", impact, (MAGIC,))
            if knocked_out or reflection_winner:
                winner, loser = (
                    (reflection_winner, owner_key) if reflection_winner else (owner_key, other_key)
                )
                return FightResult(
                    winner=winner, loser=loser, rounds=tuple(rounds), opening=opening,
                    closing=pets_flavor.result_line(fighters[winner].name, fighters[loser].name, rng=rng),
                    total_damage=total_damage, stopped_early=False, is_draw=False, seed=seed, accident=None,
                    final_hp={key: max(0, round(value)) for key, value in hp.items()},
                )

    def signature_round(attacker_key: str, defender_key: str, event: str, damage: int) -> bool:
        shield_rows_from = len(rounds)
        if effectful or derived[defender_key]["incoming_damage_multiplier"] != 1.0:
            impact, knocked_out, reflection_winner = apply_attack(
                attacker_key, defender_key, damage, 0, attack_types=(PHYSICAL,),
            )
        else:
            hp[defender_key] = max(0.0, hp[defender_key] - damage)
            impact, knocked_out, reflection_winner = damage, hp[defender_key] <= 0, None
        # hurt() must resolve guard/parry/heal/counter before it can return the final
        # combat state, but the readable transcript starts with the attack that caused
        # them. Temporarily lift only shield consequences and put them immediately after
        # the primary attack row; their captured state remains untouched.
        shield_consequences = [
            row for row in rounds[shield_rows_from:]
            if row.event.startswith("shield_")
        ]
        if shield_consequences:
            rounds[shield_rows_from:] = [
                row for row in rounds[shield_rows_from:]
                if not row.event.startswith("shield_")
            ]
        shield_consequences, primary_attacker_hp, primary_defender_hp = \
            display_shield_consequences(
                shield_consequences, attacker_key, defender_key,
            )
        total_damage[attacker_key] += impact
        rounds.append(Round(
            number=0, attacker=attacker_key, event=event, damage=damage,
            attacker_hp=primary_attacker_hp, defender_hp=primary_defender_hp,
            text=pets_flavor.line(event, fighters[attacker_key].name, fighters[defender_key].name, damage, rng=rng),
        ))
        rounds.extend(shield_consequences)
        return knocked_out or reflection_winner is not None

    for attacker_key, defender_key in ((a.key, b.key), (b.key, a.key)):
        signature = signatures.get(attacker_key)
        if not signature or signature[0] not in ("strength", "luck"):
            continue
        stat, tier = signatures.pop(attacker_key)
        if stat == "strength":
            multiplier = 2.0 if tier == 3 else 1.5
            damage = max(1, round(derived[attacker_key]["damage"] * multiplier * (1 - derived[defender_key]["reduction"])))
        else:
            damage = max(1, round(hp[defender_key] * C.LUCK_OPENING_DAMAGE_SHARE))
        if signature_round(attacker_key, defender_key, f"signature_{stat}", damage):
            return FightResult(
                winner=attacker_key, loser=defender_key, rounds=tuple(rounds), opening=opening,
                closing=pets_flavor.result_line(fighters[attacker_key].name, fighters[defender_key].name, rng=rng),
                total_damage=total_damage, stopped_early=False, is_draw=False, seed=seed, accident=None,
                final_hp={key: max(0, round(value)) for key, value in hp.items()},
            )

    def strike(attacker_key: str, defender_key: str, round_number: int) -> str | None:
        """One blow, appended as a Round. Returns the key of a fighter knocked out."""
        attacker, defender = fighters[attacker_key], fighters[defender_key]
        # One budget for everybody, whether or not they carry a scroll. Tying it to the
        # loadout made an empty slot cost 14 actions on top of the missing scroll --
        # against an opponent who had one, that gap alone decided the fight far more
        # often than any scroll effect did (see pets_scroll_sim.py).
        personal_limit = C.MAX_SKILL_ACTIONS_PER_FIGHTER
        if attacks_made[attacker_key] >= personal_limit:
            return None
        # Statuses can come from shields as well as scrolls.  Their lifecycle must not
        # depend on whether the affected fighter happens to have a filled skill loadout.
        start_active_turn(attacker_key, defender_key, round_number)
        if effectful and stunned[attacker_key]:
            stunned[attacker_key] = False
            rounds.append(Round(
                number=round_number, attacker=attacker_key, event="stun_skip", damage=0,
                attacker_hp=round(hp[attacker_key]), defender_hp=round(hp[defender_key]),
                text=f"💫 {fighters[attacker_key].name} оглушён и пропускает ход.",
            ))
            tick_skill_state(attacker_key)
            return None
        if effectful and skip_turn[attacker_key]:
            # The recoil is charged one turn AFTER the oversized crit that caused it, so
            # the player reads the reward and its price as two consecutive lines rather
            # than as a number that silently failed to appear.
            skip_turn[attacker_key] = False
            attacks_made[attacker_key] += 1
            effect_round(round_number, attacker_key, defender_key, "recoil", is_action=True)
            tick_skill_state(attacker_key)
            return None
        if effectful and (value := _effect_value(effects[attacker_key], "hunger")) is not None:
            # Uncapped damage growth paid for in maximum health, every turn, forever. The
            # floor keeps the curse from killing its own owner outright -- it starves them
            # into a shape where one clean hit finishes it, which is a fight worth watching.
            decay = max(0, _fraction(_param(effects[attacker_key], "hunger", "decay", 4)))
            floor = derived[attacker_key]["max_hp"] * max(0.05, _fraction(_param(
                effects[attacker_key], "hunger", "floor", 30,
            )))
            lost = max(0.0, min(
                max_hp[attacker_key] - floor,
                derived[attacker_key]["max_hp"] * decay,
            ))
            if lost >= 1:
                max_hp[attacker_key] -= lost
                hp[attacker_key] = min(hp[attacker_key], max_hp[attacker_key])
                effect_round(round_number, attacker_key, defender_key, "hunger", round(lost))
                if hp[attacker_key] <= 0:
                    return defender_key
        if effectful and (value := _effect_value(effects[attacker_key], "charge_crit")) is not None:
            turns = max(1, round(_param(effects[attacker_key], "charge_crit", "turns", 3)))
            if charging[attacker_key] < turns:
                # Winding up is the whole action, and the wide-open guard it costs is
                # applied for exactly as long as the wind-up lasts.
                charging[attacker_key] += 1
                if charging[attacker_key] == 1:
                    vulnerable[attacker_key] += max(0, _fraction(_param(
                        effects[attacker_key], "charge_crit", "taken", 25,
                    )))
                attacks_made[attacker_key] += 1
                effect_round(
                    round_number, attacker_key, defender_key, "charge_crit",
                    charging[attacker_key], is_action=True,
                )
                tick_skill_state(attacker_key)
                return None
        if effectful and _effect_value(effects[attacker_key], "cocoon") is not None \
                and "cocoon" not in used[attacker_key]:
            used[attacker_key].add("cocoon")
            cocooned[attacker_key] = True
            attacks_made[attacker_key] += 1
            effect_round(round_number, attacker_key, defender_key, "cocoon", is_action=True)
            tick_skill_state(attacker_key)
            return None
        if effectful and (burn := burning[attacker_key]) is not None:
            source_key, _turns, burn_damage, burn_attack_types = burn
            burn_impact, burn_ko, reflection_winner = apply_attack(
                source_key, attacker_key, burn_damage, round_number,
                attack_types=burn_attack_types,
                # A burn tick is damage over time, not a fresh incoming attack.  It
                # must not spend a first-hit parry or provoke a counterattack.
                allow_shield_reactions=False,
            )
            total_damage[source_key] += burn_impact
            shield_source = burning_shield[attacker_key]
            if shield_source:
                shield_round(
                    round_number, source_key, attacker_key, "burn_tick",
                    f"огонь от щита «{shield_source}» наносит "
                    f"{fighters[attacker_key].name} {burn_impact} урона перед ходом. "
                    + (f"Ход {fighters[attacker_key].name} продолжается."
                       if not burn_ko else f"{fighters[attacker_key].name} повержен огнём."),
                    burn_impact, burn_attack_types,
                )
            else:
                effect_round(
                    round_number, source_key, attacker_key, "burn", burn_impact,
                    burn_attack_types,
                )
            # Each component ages on its own countdown, so a short flame can burn out
            # while a longer one laid on top of it keeps ticking.
            tick_burn(attacker_key)
            if reflection_winner:
                return reflection_winner
            if burn_ko:
                return source_key
        if effectful and bleeding[attacker_key] is None and heal_cut[attacker_key]:
            heal_cut[attacker_key] = 0.0
        if effectful and (bleed := bleeding[attacker_key]) is not None:
            source_key, stacks, bleed_damage = bleed
            bleed_impact, bleed_ko = hurt(
                source_key, attacker_key, stacks * bleed_damage, round_number,
            )
            total_damage[source_key] += bleed_impact
            effect_round(round_number, source_key, attacker_key, "bleed", bleed_impact)
            if bleed_ko:
                return source_key
        if effectful and (venom := pending_venom[attacker_key]) is not None:
            source_key, venom_damage = venom
            pending_venom[attacker_key] = None
            venom_impact, venom_ko = hurt(source_key, attacker_key, venom_damage, round_number)
            total_damage[source_key] += venom_impact
            effect_round(round_number, source_key, attacker_key, "venom_blade", venom_impact)
            if venom_ko:
                return source_key
        if effectful and (poison := pending_poison[attacker_key]) is not None:
            source_key, poison_damage = poison
            pending_poison[attacker_key] = None
            poison_impact, poison_ko = hurt(source_key, attacker_key, poison_damage, round_number)
            total_damage[source_key] += poison_impact
            effect_round(round_number, source_key, attacker_key, "poison", poison_impact)
            if poison_ko:
                return source_key
        if effectful and (value := _effect_value(effects[attacker_key], "regen")) is not None:
            before = hp[attacker_key]
            hp[attacker_key] = min(max_hp[attacker_key], hp[attacker_key] + healed_amount(attacker_key, max(0, value)))
            healed = round(hp[attacker_key] - before)
            if healed:
                effect_round(round_number, attacker_key, defender_key, "regen", healed)
        # An earned extra attack is an ATTACK. Letting the ordinary action roll run here
        # meant a chained crit could spend itself raising a shield, which is neither what
        # the item says nor what anybody reading the transcript expects to see.
        if not in_followup[attacker_key] and (
            skill_loadouts[attacker_key] or equipped_shields[attacker_key]
        ):
            choices = active_actions(attacker_key)
            # Every available scroll has the same one-ticket chance. A plain attack has
            # four tickets so active combat does not turn into an endless wall of heals
            # and shields; Defend keeps one ticket like any single ability.
            action = rng.choice(["attack"] * 4 + [row for row in choices if row != "attack"])
            if action != "attack":
                attacks_made[attacker_key] += 1
                return take_active_action(attacker_key, defender_key, action, round_number)
        signature = signatures.pop(defender_key, None)
        if signature and signature[0] == "agility":
            if signature[1] == 3:
                counter_damage = max(1, round(derived[defender_key]["damage"] * 0.5))
                return defender_key if signature_round(
                    defender_key, attacker_key, "signature_agility_counter", counter_damage,
                ) else None
            rounds.append(Round(
                number=round_number, attacker=attacker_key, event="signature_agility", damage=0,
                attacker_hp=round(hp[attacker_key]), defender_hp=round(hp[defender_key]),
                text=pets_flavor.line("signature_agility", attacker.name, defender.name, rng=rng),
            ))
            return None
        defender_numbers = derived[defender_key]
        acid_attack = effectful and acid_ready[attacker_key]
        shield_breaker_attack = effectful \
            and "shield_breaker" not in used[attacker_key] \
            and _effect_value(effects[attacker_key], "shield_breaker") is not None
        if acid_attack or armor_shredded[defender_key] or shield_breaker_attack:
            defender_numbers = dict(defender_numbers)
            defender_numbers["reduction"] *= max(0.0, 1 - armor_shredded[defender_key])
        if acid_attack:
            defender_numbers["dodge"] = 0.0
            acid_ready[attacker_key] = False
            effect_round(round_number, attacker_key, defender_key, "acid")
        if shield_breaker_attack:
            ignored = max(0, _fraction(
                _effect_value(effects[attacker_key], "shield_breaker") or 0
            ))
            defender_numbers["reduction"] *= max(0.0, 1 - ignored)
        # Blind fury runs a fixed, published loop rather than a chance: so many sighted
        # swings that cannot miss, then so many that cannot land. The counter advances on
        # every attack the weapon actually takes, so a stun or a charge-up never desyncs
        # the player's count from the engine's.
        blind_sighted = blind_blind = False
        if effectful and (value := _effect_value(effects[attacker_key], "blind_fury")) is not None:
            sighted = max(1, round(value))
            blind = max(1, round(_param(effects[attacker_key], "blind_fury", "blind", 2)))
            position = blind_cycle[attacker_key] % (sighted + blind)
            blind_cycle[attacker_key] += 1
            blind_sighted, blind_blind = position < sighted, position >= sighted
            if position == 0:
                effect_round(round_number, attacker_key, defender_key, "blind_fury", sighted)
        # A blind swing is spent before anything else is rolled: it is not a dodge the
        # defender earned, so no dodge-side passive may read it as one.
        if blind_blind:
            attacks_made[attacker_key] += 1
            landed_hits[attacker_key] = 0
            effect_round(round_number, attacker_key, defender_key, "blind_fury_blind", is_action=True)
            tick_skill_state(attacker_key)
            return None
        # The wild swing decides between its two extremes before the ordinary roll, so the
        # gift to the opponent replaces the attack outright instead of arriving on top of
        # one. Both branches consume the turn -- that is what makes the coin worth flipping.
        wild_crit = False
        if effectful and (value := _effect_value(effects[attacker_key], "wild_swing")) is not None:
            roll = rng.random()
            heal_chance = max(0.0, _fraction(_param(effects[attacker_key], "wild_swing", "heal", 18)))
            crit_chance = max(0.0, _fraction(_param(effects[attacker_key], "wild_swing", "crit", 30)))
            if roll < heal_chance:
                gift = max(1, round(max_hp[defender_key] * max(0, _fraction(_param(
                    effects[attacker_key], "wild_swing", "gift", 15,
                )))))
                before_gift = hp[defender_key]
                hp[defender_key] = min(max_hp[defender_key], hp[defender_key] + healed_amount(defender_key, gift))
                attacks_made[attacker_key] += 1
                landed_hits[attacker_key] = 0
                healed = round(hp[defender_key] - before_gift)
                # Against an opponent already at full health the gift heals nothing, and
                # "лечит соперника на 0 HP" is a sillier line than the wasted turn deserves.
                effect_round(
                    round_number, attacker_key, defender_key,
                    "wild_swing_heal" if healed else "wild_swing_miss",
                    healed, is_action=True,
                )
                tick_skill_state(attacker_key)
                return None
            wild_crit = roll < heal_chance + crit_chance
        forced_venom_miss = venom_miss[attacker_key] > 0 and rng.random() < venom_miss[attacker_key]
        venom_miss[attacker_key] = 0.0
        forced_skill_miss = False
        if skill_statuses[defender_key].pop("dodge_next", False):
            forced_skill_miss = True
        elif skill_value(attacker_key, "blind") > 0:
            forced_skill_miss = rng.random() < min(.80, skill_value(attacker_key, "blind"))
        phantom_dodge = effectful and not (blind_sighted or wild_crit) \
            and _effect_value(effects[defender_key], "phantom_step") is not None \
            and phantom_dodges[defender_key] < max(1, round(_param(
                effects[defender_key], "phantom_step", "hits", 1,
            )))
        if phantom_dodge:
            phantom_dodges[defender_key] += 1
            effect_round(round_number, defender_key, attacker_key, "phantom_step")
        # The chain is a chain because each link is another perfect hit -- rolling an
        # ordinary swing for the follow-up made the passive worth a third of its tier.
        chain_link = False
        if effectful and chained[attacker_key] > 0:
            chained[attacker_key] -= 1
            chain_link = True
        if blind_sighted or wild_crit or chain_link:
            # "Cannot miss" has to mean it, so the sighted half of the loop overrides every
            # miss source in the engine, not merely the defender's dodge stat.
            defender_numbers = dict(defender_numbers)
            defender_numbers["dodge"] = 0.0
            forced_venom_miss = forced_skill_miss = False
        event, damage = (
            ("dodge", 0) if forced_venom_miss or forced_skill_miss or phantom_dodge
            else _resolve_blow(derived[attacker_key], defender_numbers, rng)
        )
        if (wild_crit or chain_link) and event != "dodge":
            event = "crit"
        if signature and signature[0] == "health":
            damage = 0 if signature[1] == 3 else round(damage * 0.5)
            event = "signature_health"
        elif signature and signature[0] == "armor":
            if signature[1] == 3:
                recoil = max(1, round(hp[attacker_key] * 0.10))
                return defender_key if signature_round(
                    defender_key, attacker_key, "signature_armor_recoil", recoil,
                ) else None
            damage = round(damage * 0.3)
            event = "signature_armor"

        # Set before the modifier block because a dodge skips that block entirely and the
        # classification still has to be right for whatever reaches apply_attack below.
        basic_attack_types = (PHYSICAL, MAGIC) if attacker.weapon_enchanted else (PHYSICAL,)
        # Set alongside basic_attack_types for the same reason: a dodge must leave it at
        # its default (no heal) rather than skip past an undefined name.
        steel_heal = 0
        if effectful and damage:
            # Attack-side modifiers are evaluated after dodge/crit, which keeps a dodge
            # absolute and makes the combat log's primary hit number truthful.
            attack_no = attacks_made[attacker_key]
            multiplier = 1.0
            # Additive, like every other attack-side bonus below it: a crit is worth
            # +100% of the swing, not a doubling of whatever the passives have already
            # built. _resolve_blow deliberately leaves it to this line.
            if event == "crit":
                multiplier += max(0.0, C.CRIT_MULTIPLIER - 1.0)
            multiplier *= max(.10, 1 - damage_weakened[attacker_key])
            if poison_weaken[attacker_key]:
                multiplier *= max(.10, 1 - poison_weaken[attacker_key])
                # Filed under the POISONED fighter -- they are who the line is about, and
                # they are the one currently swinging. The poisoner already has their own
                # row from the hit that applied it.
                effect_round(
                    round_number, attacker_key, defender_key, "poison_weaken",
                    round(poison_weaken[attacker_key] * 100),
                )
                poison_weaken[attacker_key] = 0.0
            multiplier *= max(.10, 1 - skill_value(attacker_key, "weaken"))
            multiplier *= 1 + skill_value(attacker_key, "damage_boost")
            multiplier *= 1 + skill_value(defender_key, "vulnerable")
            if afterimage_bonus[attacker_key]:
                multiplier += afterimage_bonus[attacker_key]
                afterimage_bonus[attacker_key] = 0.0
                effect_round(round_number, attacker_key, defender_key, "afterimage")
            if _effect_value(effects[attacker_key], "battle_cry") is not None and attack_no == 0:
                multiplier += max(0, _fraction(_effect_value(effects[attacker_key], "battle_cry") or 0))
            # Initiative on its own is worth nothing measurable -- leadership alternates
            # every round anyway, so all three first_strike items scored dead level with
            # carrying no passive at all. The head start now buys something: the opening
            # burst, over `hits` attacks, which is what "перехватывает инициативу" means
            # to anybody reading it. The initiative roll above is unchanged.
            if (value := _effect_value(effects[attacker_key], "first_strike")) is not None \
                    and attack_no < max(1, round(_param(effects[attacker_key], "first_strike", "hits", 3))):
                multiplier += max(0, _fraction(value))
            if _effect_value(effects[attacker_key], "late_strike") is not None \
                    and attack_no == 0 and attacker_key == order[round_number % 2]:
                multiplier += max(0, _fraction(_effect_value(effects[attacker_key], "late_strike") or 0))
                effect_round(round_number, attacker_key, defender_key, "late_strike")
            if _effect_value(effects[attacker_key], "berserker") is not None and hp[attacker_key] <= max_hp[attacker_key] * _param(effects[attacker_key], "berserker", "threshold", 35) / 100:
                multiplier += max(0, _fraction(_effect_value(effects[attacker_key], "berserker") or 0))
            if _effect_value(effects[attacker_key], "executioner") is not None and hp[defender_key] <= max_hp[defender_key] * _param(effects[attacker_key], "executioner", "threshold", 30) / 100:
                multiplier += max(0, _fraction(_effect_value(effects[attacker_key], "executioner") or 0))
            if focused_ready[attacker_key] and (value := _effect_value(effects[attacker_key], "focused")) is not None:
                multiplier += max(0, _fraction(value))
                focused_ready[attacker_key] = False
            if _effect_value(effects[attacker_key], "momentum") is not None:
                momentum_cap = _param(effects[attacker_key], "momentum", "cap", 10) / 100
                multiplier += min(momentum_cap, max(0, _fraction(_effect_value(effects[attacker_key], "momentum") or 0)) * (round_number - 1))
            if _effect_value(effects[attacker_key], "combo") is not None:
                combo_cap = _param(effects[attacker_key], "combo", "cap", 15) / 100
                multiplier += min(combo_cap, _fraction(_effect_value(effects[attacker_key], "combo") or 0) * landed_hits[attacker_key])
            if (value := _effect_value(effects[attacker_key], "heavy_combo")) is not None:
                every = max(2, round(_param(effects[attacker_key], "heavy_combo", "every", 3)))
                if (landed_hits[attacker_key] + 1) % every == 0:
                    multiplier += max(0, _fraction(value))
                    effect_round(round_number, attacker_key, defender_key, "heavy_combo")
            # --- legendary and cursed multipliers -------------------------------------
            if (value := _effect_value(effects[attacker_key], "glass_body")) is not None:
                multiplier += max(0, _fraction(value))
            if (value := _effect_value(effects[attacker_key], "blood_price")) is not None:
                multiplier += max(0, _fraction(value))
            if (value := _effect_value(effects[attacker_key], "hunger")) is not None:
                # Deliberately uncapped. The starving half of the rule is the cap: the
                # longer this runs the harder it hits and the less there is left to hit
                # with, and which of the two arrives first is the fight.
                multiplier += max(0, _fraction(value)) * max(0, round_number - 1)
            if (value := _effect_value(effects[attacker_key], "pressure")) is not None:
                # Rises with blows TAKEN, not rounds survived, so the compressor rewards
                # standing in the fire rather than merely being in a long fight.
                multiplier += max(0, _fraction(value)) * hits_taken[attacker_key]
            if wild_crit and (value := _effect_value(effects[attacker_key], "wild_swing")) is not None:
                multiplier += max(0, _fraction(value))
            if blind_sighted:
                # "Cannot miss" alone is worth about what the defender's dodge stat is
                # worth -- a fraction of what two guaranteed whiffs cost. The open-eyed
                # swings carry the weight; the miss immunity is what makes them land.
                multiplier += max(0, _fraction(_param(
                    effects[attacker_key], "blind_fury", "power", 0,
                )))
            if event == "crit" and (value := _effect_value(
                effects[attacker_key], "recoil",
            )) is not None:
                multiplier += max(0, _fraction(value))
            if (value := _effect_value(effects[attacker_key], "double_strike")) is not None:
                # Both halves are charged the same discount, including the follow-up this
                # same block queues below, so "twice at 65%" is exactly what lands.
                multiplier *= max(0.10, _fraction(value))
            if charging[attacker_key] and (value := _effect_value(
                effects[attacker_key], "charge_crit",
            )) is not None:
                # Everything the wind-up bought is spent on this one blow: the stored
                # multiplier, and the open guard it cost closes again with it.
                charging[attacker_key] = 0
                vulnerable[attacker_key] = max(0.0, vulnerable[attacker_key] - max(0, _fraction(
                    _param(effects[attacker_key], "charge_crit", "taken", 25),
                )))
                multiplier += max(0, _fraction(value))
                event = "crit" if event != "dodge" else event
                effect_round(round_number, attacker_key, defender_key, "charge_crit_release")
            if _effect_value(effects[attacker_key], "giant_slayer") is not None and attacker.level < defender.level:
                multiplier += max(0, _fraction(_effect_value(effects[attacker_key], "giant_slayer") or 0))
            if _effect_value(effects[attacker_key], "mob_hunter") is not None \
                    and str(defender_key).startswith("mob:"):
                multiplier += max(0, _fraction(_effect_value(effects[attacker_key], "mob_hunter") or 0))
            # Armour is a much smaller number than the copy on these three items implies:
            # `reduction` is 3% at armour 5 and about 11% on a dedicated tank, so taking
            # away even ALL of it was worth under three win points in the mirror-match
            # harness (pets_effect_sim.py). They were percentages of a percentage. Each
            # now also lands as ordinary damage in its own right, which is what "броня
            # больше не держит" is supposed to feel like and what makes the number printed
            # on the item mean something to the player reading it.
            if armor_shredded[defender_key]:
                multiplier += armor_shredded[defender_key]
            if shield_breaker_attack:
                multiplier += max(0, _fraction(_param(
                    effects[attacker_key], "shield_breaker", "power",
                    _effect_value(effects[attacker_key], "shield_breaker") or 0,
                )))
            if (value := _effect_value(effects[attacker_key], "piercing")) is not None:
                # Restore a configurable part of armor's otherwise already-applied cut,
                # then charge the same share again as raw damage.
                pierce = max(0, _fraction(value))
                multiplier += derived[defender_key]["reduction"] * pierce + pierce
            if acid_attack:
                # Same armour arithmetic as piercing above, so the same second clause:
                # the splash is what makes the undodgeable hit worth waiting for.
                splash = max(0, _fraction(_effect_value(effects[attacker_key], "acid") or 0))
                multiplier += derived[defender_key]["reduction"] * splash + splash
            if spring_ready[attacker_key]:
                spring_ready[attacker_key] = False
                # The value used to be ignored -- a hardcoded double meant the rare and
                # the legendary spring were the same item with different copy, and all
                # three measured identically. 100 keeps the original doubling exactly.
                spring_multiplier = 1 + max(0, _fraction(
                    _effect_value(effects[attacker_key], "spring") or 100
                ))
                multiplier *= spring_multiplier
                effect_round(round_number, attacker_key, defender_key, "spring",
                             round(spring_multiplier))
            multiplier += gambler_bonus[attacker_key]
            flat_retaliation = retaliation_bonus[attacker_key]
            retaliation_bonus[attacker_key] = 0.0
            if event == "crit" and "crit_guard" not in used[defender_key] \
                    and _effect_value(effects[defender_key], "crit_guard") is not None:
                used[defender_key].add("crit_guard")
                multiplier *= max(0.0, 1 - _fraction(
                    _effect_value(effects[defender_key], "crit_guard") or 0
                ))
                effect_round(round_number, defender_key, attacker_key, "crit_guard")
            if event == "crit" and "glass_crit" not in used[attacker_key] \
                    and (value := _effect_value(effects[attacker_key], "glass_crit")) is not None:
                used[attacker_key].add("glass_crit")
                multiplier += max(0, _fraction(value))
                effect_round(round_number, attacker_key, defender_key, "glass_crit")
            if event == "crit" and "countercrit" not in used[defender_key] \
                    and (value := _effect_value(effects[defender_key], "countercrit")) is not None:
                used[defender_key].add("countercrit")
                multiplier = max(0.1, multiplier - max(0.0, C.CRIT_MULTIPLIER - 1.0))
                retaliation_bonus[defender_key] += max(1, round(damage * max(0, _fraction(value))))
                effect_round(round_number, defender_key, attacker_key, "countercrit")
            if chilled[attacker_key]:
                chilled[attacker_key] -= 1
                multiplier *= max(.10, 1 - max(0, _fraction(_effect_value(
                    effects[defender_key], "chill"
                ) or 0)))
                effect_round(round_number, defender_key, attacker_key, "chill")
            damage = max(1, round(
                damage * multiplier * fighters[attacker_key].damage_multiplier + flat_retaliation
            ))
            # A runed weapon is worth more and is no longer purely steel. The bonus goes on
            # first so the half that turns magical is a half of the improved swing, and the
            # physical resistance is then applied to the STEEL half alone -- which is the
            # whole point: a rune is what lets a blade hurt something physical damage
            # cannot touch at all (see the dungeon's spells_only boss).
            if attacker.weapon_enchanted:
                damage = max(1, round(damage * (1 + C.RUNE_WEAPON_POWER_BONUS)))
                magic_half = round(damage * C.RUNE_WEAPON_MAGIC_SHARE)
                steel_half = damage - magic_half
                damage = magic_half + max(0, round(
                    steel_half * fighters[defender_key].physical_damage_taken_multiplier
                ))
                basic_attack_types = (PHYSICAL, MAGIC)
            elif fighters[defender_key].physical_damage_heals:
                # A plain blade is not merely wasted here, it is the wrong tool: the
                # whole swing (crit, multipliers and all) becomes HP for the target
                # instead of leaving the fight. Only a runed weapon (the branch above)
                # still gets through as damage. Applied and capped below, alongside the
                # ordinary hit row, so the transcript shows the heal as its consequence.
                steel_heal = damage
                damage = 0
                basic_attack_types = (PHYSICAL,)
            else:
                damage = max(0, round(
                    damage * fighters[defender_key].physical_damage_taken_multiplier
                ))
                basic_attack_types = (PHYSICAL,)

        if effectful and damage and shield_breaker_attack:
            used[attacker_key].add("shield_breaker")
            shields[defender_key] = 0.0
            effect_round(round_number, attacker_key, defender_key, "shield_breaker")
            # Legendary shield-breakers keep part of the breach open for the rest of
            # the fight. Rare versions omit ``shred`` and retain their old behaviour.
            shred = max(0.0, _fraction(_param(
                effects[attacker_key], "shield_breaker", "shred", 0,
            )))
            armor_shredded[defender_key] = max(armor_shredded[defender_key], shred)

        shield_rows_from = len(rounds)
        if effectful or derived[defender_key]["incoming_damage_multiplier"] != 1.0:
            impact, knocked_out, reflection_winner = apply_attack(
                attacker_key, defender_key, damage, round_number,
                attack_types=basic_attack_types, enchanted=attacker.weapon_enchanted,
            )
        else:
            hp[defender_key] = max(0.0, hp[defender_key] - damage)
            impact, knocked_out, reflection_winner = damage, hp[defender_key] <= 0, None
        shield_consequences = [
            row for row in rounds[shield_rows_from:]
            if row.event.startswith("shield_")
        ]
        if shield_consequences:
            rounds[shield_rows_from:] = [
                row for row in rounds[shield_rows_from:]
                if not row.event.startswith("shield_")
            ]
        shield_consequences, primary_attacker_hp, primary_defender_hp = \
            display_shield_consequences(
                shield_consequences, attacker_key, defender_key,
            )
        total_damage[attacker_key] += impact
        text = pets_flavor.line(event, attacker.name, defender.name, damage, rng=rng)
        attacks_made[attacker_key] += 1
        if damage:
            landed_hits[attacker_key] += 1
        rounds.append(Round(
            number=round_number,
            attacker=attacker_key,
            event=event,
            damage=damage,
            attacker_hp=primary_attacker_hp,
            defender_hp=primary_defender_hp,
            text=text,
            # The transcript has to agree with what actually resolved: a runed blow is
            # tagged as magic too, and the fight log is where a player works out why a
            # physically immune enemy still took damage.
            attack_types=basic_attack_types,
        ))
        rounds.extend(shield_consequences)
        if reflection_winner:
            return reflection_winner
        if impact and not knocked_out:
            reflected_winner = reflect_skill_damage(
                attacker_key, defender_key, impact, round_number,
            )
            if reflected_winner:
                return reflected_winner

        # steel_heal never coexists with impact (the branch above zeroed damage before
        # apply_attack ran), so this is the hit's only HP consequence -- appended right
        # after the ordinary attack row, same placement as any other consequence of it,
        # and outside the `effectful and damage` gate below because damage is 0 here by
        # design. Capped at max_hp like every other heal in this file.
        if steel_heal:
            before_heal = hp[defender_key]
            hp[defender_key] = min(max_hp[defender_key], hp[defender_key] + healed_amount(defender_key, steel_heal))
            healed = round(hp[defender_key] - before_heal)
            if healed:
                effect_round(round_number, defender_key, attacker_key, "steel_heal", healed)

        # Post-hit effects occur after the ordinary attack line so the sequence reads
        # naturally in Telegram.  They can knock out the attacker too.
        if effectful and damage:
            if (value := _effect_value(effects[attacker_key], "echo_strike")) is not None \
                    and "echo_strike" not in used[attacker_key] and not knocked_out:
                used[attacker_key].add("echo_strike")
                echo_damage = max(1, round(impact * max(0, _fraction(value))))
                echo_impact, echo_ko = hurt(
                    attacker_key, defender_key, echo_damage, round_number,
                )
                total_damage[attacker_key] += echo_impact
                effect_round(round_number, attacker_key, defender_key, "echo_strike", echo_impact)
                if echo_ko:
                    return attacker_key
            if (value := _effect_value(effects[attacker_key], "crushing_grip")) is not None \
                    and "crushing_grip" not in used[attacker_key] and not knocked_out:
                used[attacker_key].add("crushing_grip")
                damage_weakened[defender_key] = max(
                    damage_weakened[defender_key], max(0, _fraction(value)),
                )
                effect_round(
                    round_number, attacker_key, defender_key, "crushing_grip", round(value),
                )
            if (value := _effect_value(effects[attacker_key], "vampiric")) is not None:
                before = hp[attacker_key]
                hp[attacker_key] = min(max_hp[attacker_key], hp[attacker_key] + healed_amount(attacker_key, impact * max(0, _fraction(value))))
                healed = round(hp[attacker_key] - before)
                if healed:
                    effect_round(round_number, attacker_key, defender_key, "vampiric", healed)
            # Pressure counts ordinary attacks that landed on somebody. Deliberately not
            # burn or bleed ticks: those arrive several times a round and would wind the
            # compressor far past anything the number on the item suggests.
            hits_taken[defender_key] += 1
            if (value := _effect_value(effects[attacker_key], "reap")) is not None:
                # A share of what the opponent has already LOST, not of what this blow
                # dealt: the drip pays nothing against a healthy enemy and pays enormously
                # against a dying one, which is a different weapon from vampirism.
                missing = max(0.0, max_hp[defender_key] - hp[defender_key])
                harvest = round(missing * max(0, _fraction(value)))
                if harvest >= 1:
                    before_reap = hp[attacker_key]
                    hp[attacker_key] = min(
                        max_hp[attacker_key],
                        hp[attacker_key] + healed_amount(attacker_key, harvest),
                    )
                    healed = round(hp[attacker_key] - before_reap)
                    if healed:
                        effect_round(round_number, attacker_key, defender_key, "reap", healed)
            if (value := _effect_value(effects[attacker_key], "tax")) is not None                     and not knocked_out:
                # Percent of CURRENT health, so it never finishes anybody on its own and
                # never stops mattering either -- and it is the combat half of a passive
                # whose other half is paid out of the arena purse in pets.record_fight.
                levy = max(1, round(hp[defender_key] * max(0, _fraction(value))))
                levy_impact, levy_ko = hurt(attacker_key, defender_key, levy, round_number)
                total_damage[attacker_key] += levy_impact
                effect_round(round_number, attacker_key, defender_key, "tax", levy_impact)
                if levy_ko:
                    return attacker_key
            if (value := _effect_value(effects[attacker_key], "shatter")) is not None                     and not knocked_out:
                every = max(2, round(_param(effects[attacker_key], "shatter", "every", 5)))
                shatter_stacks[defender_key] += 1
                if shatter_stacks[defender_key] % every:
                    effect_round(round_number, attacker_key, defender_key, "shatter")
                else:
                    burst = max(1, round(
                        max_hp[defender_key] * max(0, _fraction(value)) * every
                    ))
                    burst_impact, burst_ko, reflection_winner = apply_attack(
                        attacker_key, defender_key, burst, round_number,
                        attack_types=(MAGIC,), allow_shield_reactions=False,
                    )
                    total_damage[attacker_key] += burst_impact
                    effect_round(
                        round_number, attacker_key, defender_key, "shatter_burst",
                        burst_impact, (MAGIC,),
                    )
                    if reflection_winner:
                        return reflection_winner
                    if burst_ko:
                        return attacker_key
            if (value := _effect_value(effects[attacker_key], "blood_price")) is not None:
                # Charged after the blow lands, out of CURRENT health, and never lethal:
                # the curse is meant to leave its owner one hit from death, not to hand
                # the win to an opponent who never touched them.
                toll = max(1, round(hp[attacker_key] * max(0, _fraction(_param(
                    effects[attacker_key], "blood_price", "toll", 7,
                )))))
                toll = int(min(toll, max(0.0, hp[attacker_key] - 1)))
                if toll:
                    hp[attacker_key] -= toll
                    effect_round(round_number, attacker_key, defender_key, "blood_price", toll)
            if event == "crit" and _effect_value(effects[attacker_key], "recoil") is not None:
                skip_turn[attacker_key] = True
            if event == "crit" and (value := _effect_value(
                effects[attacker_key], "chain_crit",
            )) is not None and not knocked_out                     and not chain_link                     and extra_attacks[attacker_key] < max(1, round(value)):
                extra_attacks[attacker_key] += round(value)
                chained[attacker_key] += round(value)
                effect_round(round_number, attacker_key, defender_key, "chain_crit")
            if (value := _effect_value(effects[attacker_key], "poison")) is not None and not knocked_out:
                poison = swing_share(attacker_key, value, "poison")
                queue_damage(pending_poison, defender_key, attacker_key, poison)
                # Poison is the status that attacks the opponent's OFFENCE. It ticks for
                # less than fire or bleeding on purpose: what a poisoned fighter loses is
                # the strength of their own next blow, which is worth more than the tick
                # against anything that hits hard.
                poison_weaken[defender_key] = max(poison_weaken[defender_key], max(
                    0.0, _fraction(_param(effects[attacker_key], "poison", "weaken", 0)),
                ))
            if (value := _param(effects[attacker_key], "venom_blade", "weaken", 0)) \
                    and _effect_value(effects[attacker_key], "venom_blade") is not None \
                    and not knocked_out:
                poison_weaken[defender_key] = max(
                    poison_weaken[defender_key], max(0.0, _fraction(value)),
                )
            if (value := _effect_value(effects[attacker_key], "burn")) is not None \
                    and not knocked_out:
                burn_damage = swing_share(attacker_key, value, "burn")
                turns = max(1, round(_param(effects[attacker_key], "burn", "turns", 2)))
                # One origin for the whole passive: it re-triggers on every landed hit, so
                # stacking it against itself would multiply the printed value by the length
                # of the fight. Each new hit refreshes its own flame instead.
                ignite(
                    defender_key, attacker_key, burn_damage, turns, (ELEMENTAL, MAGIC),
                    origin="passive:burn",
                    growth=max(0.0, _fraction(_param(
                        effects[attacker_key], "burn", "grow", 0,
                    ))),
                )
                burning_shield[defender_key] = None
            if (value := _effect_value(effects[attacker_key], "venom_blade")) is not None \
                    and not knocked_out:
                venom_damage = swing_share(attacker_key, _param(
                    effects[attacker_key], "venom_blade", "poison", 2,
                ), "venom_blade")
                queue_damage(pending_venom, defender_key, attacker_key, venom_damage)
                venom_miss[defender_key] = max(venom_miss[defender_key], max(0, _fraction(value)))
            if (value := _effect_value(effects[attacker_key], "bleed")) is not None \
                    and not knocked_out:
                old = bleeding[defender_key]
                old_stacks = old[1] if old and old[0] == attacker_key else 0
                cap = max(1, round(_param(effects[attacker_key], "bleed", "cap", 3)))
                stacks = min(cap, old_stacks + 1)
                bleeding[defender_key] = (
                    attacker_key, stacks,
                    swing_share(attacker_key, value, "bleed"),
                )
                # Bleeding is the counter to a build that out-heals its damage: it is the
                # only status that closes off the opponent's recovery instead of racing
                # it. The cut scales with the stacks, so a single scratch is not a
                # shutdown and a fully opened wound very nearly is.
                cut = max(0.0, _fraction(_param(
                    effects[attacker_key], "bleed", "heal_cut", 0,
                ))) * stacks / max(1, cap)
                if cut > heal_cut[defender_key]:
                    heal_cut[defender_key] = min(0.95, cut)
                    effect_round(
                        round_number, attacker_key, defender_key, "bleed_heal_cut",
                        round(heal_cut[defender_key] * 100),
                    )
            if (value := _effect_value(effects[attacker_key], "armor_shred")) is not None \
                    and not knocked_out:
                cap = max(0, _fraction(_param(
                    effects[attacker_key], "armor_shred", "cap", value * 4,
                )))
                before_shred = armor_shredded[defender_key]
                armor_shredded[defender_key] = min(
                    cap, armor_shredded[defender_key] + max(0, _fraction(value)),
                )
                added_shred = round((armor_shredded[defender_key] - before_shred) * 100)
                if added_shred:
                    effect_round(
                        round_number, attacker_key, defender_key, "armor_shred", added_shred,
                    )
            if (value := _effect_value(effects[attacker_key], "wound")) is not None \
                    and not knocked_out:
                wound_every = max(1, round(_param(
                    effects[attacker_key], "wound", "every", 1,
                )))
                if landed_hits[attacker_key] % wound_every == 0:
                    original_max = derived[defender_key]["max_hp"]
                    floor = round(original_max * max(0.01, 1 - max(0, _fraction(_param(
                        effects[attacker_key], "wound", "cap", value * 5,
                    )))))
                    old_max = max_hp[defender_key]
                    max_hp[defender_key] = max(
                        floor,
                        old_max - max(1, round(original_max * max(0, float(value) / 100))),
                    )
                    lost_max = max(0, round(old_max - max_hp[defender_key]))
                    if lost_max:
                        before_wound = hp[defender_key]
                        hp[defender_key] = min(
                            max_hp[defender_key], max(0.0, hp[defender_key] - lost_max),
                        )
                        total_damage[attacker_key] += round(before_wound - hp[defender_key])
                        effect_round(round_number, attacker_key, defender_key, "wound", lost_max)
                        if hp[defender_key] <= 0:
                            return attacker_key
            if (value := _effect_value(effects[attacker_key], "bite")) is not None \
                    and "bite" not in used[attacker_key] and not knocked_out:
                used[attacker_key].add("bite")
                bite_damage = max(1, round(impact * .65))
                bite_impact, bite_ko = hurt(attacker_key, defender_key, bite_damage, round_number)
                total_damage[attacker_key] += bite_impact
                before = hp[attacker_key]
                hp[attacker_key] = min(
                    max_hp[attacker_key],
                    hp[attacker_key] + healed_amount(
                        attacker_key, bite_impact * max(0, _fraction(value)),
                    ),
                )
                effect_round(round_number, attacker_key, defender_key, "bite", bite_impact)
                if bite_ko:
                    return attacker_key
            if (value := _effect_value(effects[attacker_key], "blood_pact")) is not None \
                    and landed_hits[attacker_key] % 3 == 0:
                before = hp[attacker_key]
                hp[attacker_key] = min(
                    max_hp[attacker_key],
                    hp[attacker_key] + healed_amount(
                        attacker_key, impact * max(0, _fraction(value)),
                    ),
                )
                healed = round(hp[attacker_key] - before)
                if healed:
                    effect_round(round_number, attacker_key, defender_key, "blood_pact", healed)
            if (value := _effect_value(effects[attacker_key], "stun")) is not None \
                    and event == "crit" and not knocked_out \
                    and stun_procs[attacker_key] < max(1, round(_param(
                        effects[attacker_key], "stun", "cap", 1,
                    ))):
                stun_procs[attacker_key] += 1
                stunned[defender_key] = True
                effect_round(round_number, attacker_key, defender_key, "stun")
            if (value := _effect_value(effects[attacker_key], "chill")) is not None \
                    and "chill" not in used[attacker_key] and not knocked_out:
                used[attacker_key].add("chill")
                chilled[defender_key] = max(1, round(_param(
                    effects[attacker_key], "chill", "hits", 1,
                )))
                effect_round(round_number, attacker_key, defender_key, "chill")
            if (value := _effect_value(effects[attacker_key], "tesla")) is not None \
                    and landed_hits[attacker_key] >= 3 and "tesla" not in used[attacker_key] and not knocked_out:
                used[attacker_key].add("tesla")
                shock = max(1, round(max_hp[defender_key] * max(0, _fraction(value))))
                shock_impact, shock_ko, reflection_winner = apply_attack(
                    attacker_key, defender_key, shock, round_number, attack_types=(MAGIC,),
                )
                total_damage[attacker_key] += shock_impact
                effect_round(round_number, attacker_key, defender_key, "tesla", shock_impact, (MAGIC,))
                if reflection_winner:
                    return reflection_winner
                if shock_ko:
                    return attacker_key
            if not knocked_out and (value := _effect_value(effects[defender_key], "second_wind")) is not None \
                    and "second_wind" not in used[defender_key] \
                    and hp[defender_key] <= max_hp[defender_key] * _param(effects[defender_key], "second_wind", "threshold", 30) / 100:
                used[defender_key].add("second_wind")
                before = hp[defender_key]
                hp[defender_key] = min(max_hp[defender_key], hp[defender_key] + healed_amount(defender_key, max_hp[defender_key] * max(0, _fraction(value))))
                effect_round(round_number, defender_key, attacker_key, "second_wind", round(hp[defender_key] - before))
            if not knocked_out and (value := _effect_value(effects[defender_key], "medkit")) is not None \
                    and "medkit" not in used[defender_key] \
                    and hp[defender_key] <= max_hp[defender_key] * _param(effects[defender_key], "medkit", "threshold", 35) / 100:
                used[defender_key].add("medkit")
                before = hp[defender_key]
                hp[defender_key] = min(max_hp[defender_key], hp[defender_key] + healed_amount(defender_key, max_hp[defender_key] * max(0, _fraction(value))))
                effect_round(round_number, defender_key, attacker_key, "medkit", round(hp[defender_key] - before))
            if not knocked_out:
                if _effect_value(effects[defender_key], "armor_burst") is not None \
                        and "armor_burst" not in used[defender_key]:
                    used[defender_key].add("armor_burst")
                    armor_burst_ready[defender_key] = True
                if _effect_value(effects[defender_key], "spring") is not None \
                        and "spring" not in used[defender_key]:
                    spring_hits_taken[defender_key] += 1
                    if spring_hits_taken[defender_key] >= 2:
                        used[defender_key].add("spring")
                        spring_ready[defender_key] = True
                if cocooned[defender_key]:
                    cocooned[defender_key] = False
                    # The value was ignored here too: the cocoon always returned exactly
                    # the blow it ate, so raising the number on the item changed nothing.
                    # 100 reproduces the old behaviour, and the item pays for the turn it
                    # skipped only if it can send back more than it took.
                    recoil = max(1, round(impact * max(0, _fraction(
                        _effect_value(effects[defender_key], "cocoon") or 100
                    ))))
                    recoil_impact, recoil_ko = hurt(defender_key, attacker_key, recoil, round_number)
                    total_damage[defender_key] += recoil_impact
                    effect_round(round_number, defender_key, attacker_key, "cocoon", recoil_impact)
                    if recoil_ko:
                        return defender_key
                if (value := _effect_value(effects[defender_key], "thorns")) is not None:
                    recoil = max(1, round(impact * max(0, _fraction(value))))
                    recoil_impact, recoil_ko = hurt(defender_key, attacker_key, recoil, round_number)
                    total_damage[defender_key] += recoil_impact
                    effect_round(round_number, defender_key, attacker_key, "thorns", recoil_impact)
                    if recoil_ko:
                        return defender_key
                if (value := _effect_value(effects[defender_key], "retaliation")) is not None:
                    retaliation = swing_share(defender_key, value, "retaliation")
                    retaliation_bonus[defender_key] += retaliation
                    effect_round(round_number, defender_key, attacker_key, "retaliation", retaliation)
        elif effectful and event == "dodge" and (value := _effect_value(effects[defender_key], "dodge_heal")) is not None:
            landed_hits[attacker_key] = 0
            if _effect_value(effects[attacker_key], "focused") is not None:
                focused_ready[attacker_key] = True
            before = hp[defender_key]
            hp[defender_key] = min(max_hp[defender_key], hp[defender_key] + healed_amount(defender_key, max(0, value)))
            healed = round(hp[defender_key] - before)
            if healed:
                effect_round(round_number, defender_key, attacker_key, "dodge_heal", healed)
            if _effect_value(effects[attacker_key], "acid") is not None and "acid" not in used[attacker_key]:
                used[attacker_key].add("acid")
                acid_ready[attacker_key] = True
            if "afterimage" not in used[defender_key] \
                    and (value := _effect_value(effects[defender_key], "afterimage")) is not None:
                used[defender_key].add("afterimage")
                afterimage_bonus[defender_key] = max(0, _fraction(value))
        elif effectful and event == "dodge" and _effect_value(effects[attacker_key], "focused") is not None:
            landed_hits[attacker_key] = 0
            focused_ready[attacker_key] = True
            if "afterimage" not in used[defender_key] \
                    and (value := _effect_value(effects[defender_key], "afterimage")) is not None:
                used[defender_key].add("afterimage")
                afterimage_bonus[defender_key] = max(0, _fraction(value))
        elif effectful and event == "dodge":
            landed_hits[attacker_key] = 0
            if _effect_value(effects[attacker_key], "acid") is not None and "acid" not in used[attacker_key]:
                used[attacker_key].add("acid")
                acid_ready[attacker_key] = True
            if "afterimage" not in used[defender_key] \
                    and (value := _effect_value(effects[defender_key], "afterimage")) is not None:
                used[defender_key].add("afterimage")
                afterimage_bonus[defender_key] = max(0, _fraction(value))
        tick_skill_state(attacker_key)
        return attacker_key if knocked_out else None

    def take_turn(attacker_key: str, defender_key: str, round_number: int) -> str | None:
        """One fighter's whole turn: the blow, plus any follow-up blows it earned.

        Two legendary passives hand out extra attacks -- a chained crit and a weapon that
        swings twice -- and both have to resolve inside the turn that produced them rather
        than stealing the opponent's. The cap is absolute and lives here, not in the
        passives, so no future combination of them can turn one turn into an endless one.
        """
        winner = strike(attacker_key, defender_key, round_number)
        if winner is not None or not effectful:
            extra_attacks[attacker_key] = chained[attacker_key] = 0
            used[attacker_key].discard("double_strike")
            return winner
        if _effect_value(effects[attacker_key], "double_strike") is not None                 and "double_strike" not in used[attacker_key]                 and hp[attacker_key] > 0 and hp[defender_key] > 0:
            used[attacker_key].add("double_strike")
            extra_attacks[attacker_key] += 1
            effect_round(round_number, attacker_key, defender_key, "double_strike")
        for _ in range(C.MAX_EXTRA_ATTACKS_PER_TURN):
            if extra_attacks[attacker_key] <= 0 or hp[attacker_key] <= 0 or hp[defender_key] <= 0:
                break
            extra_attacks[attacker_key] -= 1
            in_followup[attacker_key] = True
            try:
                winner = strike(attacker_key, defender_key, round_number)
            finally:
                in_followup[attacker_key] = False
            if winner is not None:
                break
        extra_attacks[attacker_key] = chained[attacker_key] = 0
        used[attacker_key].discard("double_strike")
        return winner

    stopped_early = False
    winner_key = loser_key = None
    is_draw = False

    action_limit = max(1, int(max_actions or C.MAX_SKILL_ACTIONS_PER_FIGHTER))
    for round_number in range(1, action_limit + 1):
        leader_key = order[(round_number - 1) % 2]
        follower_key = order[round_number % 2]

        winner_key = take_turn(leader_key, follower_key, round_number)
        if winner_key:
            loser_key = follower_key if winner_key == leader_key else leader_key
            break
        winner_key = take_turn(follower_key, leader_key, round_number)
        if winner_key:
            loser_key = leader_key if winner_key == follower_key else follower_key
            break
    else:
        # The attack budget is exhausted. A knockout always wins. If both pets are still
        # alive, the one that dealt more cumulative damage wins. Equal damage is a draw:
        # there is deliberately no hidden HP or RNG tiebreaker for a rule players can see.
        stopped_early = True
        alive = {key: hp[key] > 0 for key in (a.key, b.key)}
        if alive[a.key] and not alive[b.key]:
            winner_key, loser_key = a.key, b.key
        elif alive[b.key] and not alive[a.key]:
            winner_key, loser_key = b.key, a.key
        elif total_damage[a.key] > total_damage[b.key]:
            winner_key, loser_key = a.key, b.key
        elif total_damage[b.key] > total_damage[a.key]:
            winner_key, loser_key = b.key, a.key
        else:
            is_draw = True

    closing = (
        pets_flavor.draw_line(a.name, b.name, rng=rng)
        if is_draw else
        pets_flavor.result_line(fighters[winner_key].name, fighters[loser_key].name, rng=rng)
    )

    return FightResult(
        winner=winner_key,
        loser=loser_key,
        rounds=tuple(rounds),
        opening=opening,
        closing=closing,
        total_damage=total_damage,
        stopped_early=stopped_early,
        is_draw=is_draw,
        seed=seed,
        accident=None,
        final_hp={key: max(0, round(value)) for key, value in hp.items()},
    )
