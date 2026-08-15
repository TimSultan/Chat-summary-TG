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
    "poison": 3, "thorns": 7, "second_wind": 18, "last_stand": 1,
    "dodge_heal": 7, "crit_guard": 30, "retaliation": 3, "regen": 4,
    "focused": 20, "momentum": 2, "gambler": 18, "safeguard": 35,
    "giant_slayer": 18, "mob_hunter": 15, "mob_ward": 15, "collector": 25,
    "survivor": 30, "mirror_soul": 20,
    "bite": 50, "armor_burst": 75, "late_strike": 35, "medkit": 20,
    "countercrit": 20, "trophy_compass": 35, "stun": 1, "cocoon": 100,
    "glass_crit": 60, "blood_pact": 35, "chill": 40, "tesla": 15,
    "death_shield": 20, "acid": 25, "spring": 100, "candle": 40,
    "armor_shred": 6, "wound": 1, "burn": 3, "venom_blade": 18,
    "coin_rake": 1, "bleed": 2, "shield_breaker": 100, "heavy_combo": 50,
    "phantom_step": 1, "afterimage": 45, "rewind": 25,
    "echo_strike": 50, "crushing_grip": 10, "perfect_parry": 35,
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
    "spring": "сжимает пружину: следующий удар двойной.",
    "candle": "зажигает чёрную свечу: {amount:+d}% к урону.",
    "armor_shred": "крошит броню: защита слабее ещё на {amount}%.",
    "wound": "оставляет глубокую рану: −{amount} текущего и максимального HP.",
    "burn": "поджигает соперника: {amount} урона от огня.",
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


def _scaled_flat_damage(value: float, fighter: "Fighter", attack_damage: float) -> int:
    """Scale old flat damage with its owner's level and effective attack power.

    Percentage-based effects already grow with the combatants (echo, thorns, wounds,
    scroll burns, rune fire, and so on). This is only for legacy catalogue values such
    as ``6 poison`` or ``2 bleed``. The stronger of the level and damage curves wins,
    rather than multiplying them together, so the effect stays useful without double-
    dipping every stat increase.
    """
    base = max(0.0, float(value or 0))
    level = max(1, int(fighter.level or 1))
    level_factor = 1.0 + C.FLAT_EFFECT_LEVEL_SQRT_GROWTH * math.sqrt(level - 1)
    damage_factor = math.sqrt(max(
        1.0, float(attack_damage or 0) / C.FLAT_EFFECT_DAMAGE_REFERENCE,
    ))
    return max(1, round(base * max(level_factor, damage_factor)))


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
        raw *= C.CRIT_MULTIPLIER
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
    )
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
    venom_miss = {a.key: 0.0, b.key: 0.0}
    pending_venom: dict[str, tuple[str, int] | None] = {a.key: None, b.key: None}
    bleeding: dict[str, tuple[str, int, int] | None] = {a.key: None, b.key: None}
    afterimage_bonus = {a.key: 0.0, b.key: 0.0}
    damage_weakened = {a.key: 0.0, b.key: 0.0}
    stun_procs = {a.key: 0, b.key: 0}
    guards = {a.key: 0.0, b.key: 0.0}
    used_scrolls = {a.key: set() for a in (a, b)}
    skill_statuses = {a.key: {} for a in (a, b)}

    def scaled_flat_damage(source_key: str, value: float, code: str) -> int:
        spec = _effect(effects[source_key], code) or {}
        if spec.get("level_scaled") is False:
            return max(1, round(max(0, value)))
        fighter = fighters[source_key]
        return _scaled_flat_damage(
            value, fighter, C.BASE_DAMAGE + fighter.strength * C.DAMAGE_PER_POINT,
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
            ))

    def effect_round(
        number: int, owner_key: str, other_key: str, code: str, amount: int = 0,
        attack_types=(PHYSICAL,),
    ):
        """Put an equipped-item proc in the normal transcript, without flavour RNG."""
        template = _EFFECT_TEXT.get(code, "срабатывает эффект снаряжения.")
        rounds.append(Round(
            number=number, attacker=owner_key, event=f"amulet_{code}", damage=amount,
            attacker_hp=round(hp[owner_key]), defender_hp=round(hp[other_key]),
            text=f"✨ {fighters[owner_key].name} {template.format(amount=amount)}",
            attack_types=normalize_attack_types(attack_types),
        ))

    def hurt(
        source_key: str, target_key: str, damage: int, number: int,
        pierce_guard: float = 0.0,
        allow_shield_reactions: bool = False,
    ) -> tuple[int, bool]:
        """Apply damage and one-shot defensive effects. Returns (impact, knockout)."""
        damage = max(0, int(damage))
        damage = round(damage * derived[target_key]["incoming_damage_multiplier"])
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
                    effect_round(number, target_key, source_key, "shield_parry_stun", parried)
        if damage and guards[target_key] > 0:
            before_guard = damage
            blocked_share = guards[target_key] * max(0.0, 1.0 - min(1.0, pierce_guard))
            damage = round(damage * max(0.0, 1.0 - blocked_share))
            guards[target_key] = 0.0
            effect_round(number, target_key, source_key, "guard", before_guard - damage)
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
            (code for code in ("rewind", "death_shield", "last_stand")
             if code not in used[target_key] and _effect_value(effects[target_key], code) is not None),
            None,
        )
        if effectful and damage >= hp[target_key] and rescue:
            used[target_key].add(rescue)
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
                hp[target_key] = min(max_hp[target_key], hp[target_key] + impact * percent)
                healed = round(hp[target_key] - before)
                if healed:
                    effect_round(number, target_key, source_key, "shield_damage_heal", healed)
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
                effect_round(
                    number, target_key, source_key, "shield_counterattack", counter_impact,
                    (PHYSICAL,),
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
            hp[source_key] = min(max_hp[source_key], hp[source_key] + impact * lifesteal)
            healed = round(hp[source_key] - before)
            if healed:
                rounds.append(Round(
                    number=number, attacker=source_key, event="skill_lifesteal", damage=-healed,
                    attacker_hp=round(hp[source_key]), defender_hp=round(hp[target_key]),
                    text=f"💚 {fighters[source_key].name} восстанавливает {healed} HP от удара.",
                    attack_types=normalize_attack_types(attack_types),
                ))
        reflected_winner = None if knocked_out or reflection_winner else reflect_skill_damage(
            source_key, target_key, impact, number,
        )
        return impact, reflection_winner or reflected_winner or (source_key if knocked_out else None)

    def apply_scroll_effect(
        source_key: str, target_key: str, effect: Mapping, number: int,
        attack_types=(MAGIC,),
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
                hp[source_key] + max_hp[source_key] * max(0.0, float(effect.get("percent", 0))),
            )
        elif op == "shield":
            shields[source_key] = min(
                max_hp[source_key], shields[source_key] + max(
                    1, round(max_hp[source_key] * max(0.0, float(effect.get("percent", 0))))
                ),
            )
        elif op == "burn":
            burning[target_key] = (
                source_key,
                max(1, int(effect.get("turns", 1))),
                max(1, round(derived[source_key]["damage"] * max(0.0, float(effect.get("amount", 0))))),
                normalize_attack_types(attack_types),
            )
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
            burning[source_key] = None
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
            hp[key] = min(max_hp[key], hp[key] + regen[0])
            healed = round(hp[key] - before)
            if healed:
                rounds.append(Round(
                    number=number, attacker=key, event="skill_regen", damage=-healed,
                    attacker_hp=round(hp[key]), defender_hp=round(hp[other_key]),
                    text=f"💧 {fighters[key].name} восстанавливает {healed} HP.",
                ))

    def take_active_action(
        source_key: str, target_key: str, action: str, number: int,
    ) -> str | None:
        if action == "defend":
            shield = equipped_shields[source_key] or {}
            guards[source_key] = max(.10, min(.80, float(shield.get("guard", .40) or .40)))
            for effect in shield.get("defend_effects", ()):
                _impact, winner = apply_scroll_effect(
                    source_key, target_key, effect, number, (MAGIC,),
                )
                if winner:
                    return winner
            rounds.append(Round(
                number=number, attacker=source_key, event="defend", damage=0,
                attacker_hp=round(hp[source_key]), defender_hp=round(hp[target_key]),
                text=(f"🛡 {fighters[source_key].name} защищается"
                      + (f" щитом «{shield.get('name')}»." if shield.get("name") else ".")),
            ))
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
        if effectful or derived[defender_key]["incoming_damage_multiplier"] != 1.0:
            impact, knocked_out, reflection_winner = apply_attack(
                attacker_key, defender_key, damage, 0, attack_types=(PHYSICAL,),
            )
        else:
            hp[defender_key] = max(0.0, hp[defender_key] - damage)
            impact, knocked_out, reflection_winner = damage, hp[defender_key] <= 0, None
        total_damage[attacker_key] += impact
        rounds.append(Round(
            number=0, attacker=attacker_key, event=event, damage=damage,
            attacker_hp=round(hp[attacker_key]), defender_hp=round(hp[defender_key]),
            text=pets_flavor.line(event, fighters[attacker_key].name, fighters[defender_key].name, damage, rng=rng),
        ))
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
        if effectful and _effect_value(effects[attacker_key], "cocoon") is not None \
                and "cocoon" not in used[attacker_key]:
            used[attacker_key].add("cocoon")
            cocooned[attacker_key] = True
            attacks_made[attacker_key] += 1
            effect_round(round_number, attacker_key, defender_key, "cocoon")
            tick_skill_state(attacker_key)
            return None
        if effectful and (burn := burning[attacker_key]) is not None:
            source_key, turns, burn_damage, burn_attack_types = burn
            burn_impact, burn_ko, reflection_winner = apply_attack(
                source_key, attacker_key, burn_damage, round_number,
                attack_types=burn_attack_types,
                # A burn tick is damage over time, not a fresh incoming attack.  It
                # must not spend a first-hit parry or provoke a counterattack.
                allow_shield_reactions=False,
            )
            total_damage[source_key] += burn_impact
            effect_round(
                round_number, source_key, attacker_key, "burn", burn_impact,
                burn_attack_types,
            )
            burning[attacker_key] = (
                source_key, turns - 1, burn_damage, burn_attack_types
            ) if turns > 1 else None
            if reflection_winner:
                return reflection_winner
            if burn_ko:
                return source_key
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
            hp[attacker_key] = min(max_hp[attacker_key], hp[attacker_key] + max(0, value))
            healed = round(hp[attacker_key] - before)
            if healed:
                effect_round(round_number, attacker_key, defender_key, "regen", healed)
        if skill_loadouts[attacker_key] or equipped_shields[attacker_key]:
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
        forced_venom_miss = venom_miss[attacker_key] > 0 and rng.random() < venom_miss[attacker_key]
        venom_miss[attacker_key] = 0.0
        forced_skill_miss = False
        if skill_statuses[defender_key].pop("dodge_next", False):
            forced_skill_miss = True
        elif skill_value(attacker_key, "blind") > 0:
            forced_skill_miss = rng.random() < min(.80, skill_value(attacker_key, "blind"))
        phantom_dodge = effectful \
            and _effect_value(effects[defender_key], "phantom_step") is not None \
            and phantom_dodges[defender_key] < max(1, round(_param(
                effects[defender_key], "phantom_step", "hits", 1,
            )))
        if phantom_dodge:
            phantom_dodges[defender_key] += 1
            effect_round(round_number, defender_key, attacker_key, "phantom_step")
        event, damage = (
            ("dodge", 0) if forced_venom_miss or forced_skill_miss or phantom_dodge
            else _resolve_blow(derived[attacker_key], defender_numbers, rng)
        )
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

        if effectful and damage:
            # Attack-side modifiers are evaluated after dodge/crit, which keeps a dodge
            # absolute and makes the combat log's primary hit number truthful.
            attack_no = attacks_made[attacker_key]
            multiplier = 1.0
            multiplier *= max(.10, 1 - damage_weakened[attacker_key])
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
                multiplier *= 1 + max(0, _fraction(
                    _effect_value(effects[attacker_key], "spring") or 100
                ))
                effect_round(round_number, attacker_key, defender_key, "spring")
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
                multiplier /= max(1, C.CRIT_MULTIPLIER)
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
            damage = max(0, round(
                damage * fighters[defender_key].physical_damage_taken_multiplier
            ))

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

        if effectful or derived[defender_key]["incoming_damage_multiplier"] != 1.0:
            impact, knocked_out, reflection_winner = apply_attack(
                attacker_key, defender_key, damage, round_number,
                attack_types=(PHYSICAL,), enchanted=attacker.weapon_enchanted,
            )
        else:
            hp[defender_key] = max(0.0, hp[defender_key] - damage)
            impact, knocked_out, reflection_winner = damage, hp[defender_key] <= 0, None
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
            attacker_hp=round(hp[attacker_key]),
            defender_hp=round(hp[defender_key]),
            text=text,
            attack_types=(PHYSICAL,),
        ))
        if reflection_winner:
            return reflection_winner
        if impact and not knocked_out:
            reflected_winner = reflect_skill_damage(
                attacker_key, defender_key, impact, round_number,
            )
            if reflected_winner:
                return reflected_winner

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
                hp[attacker_key] = min(max_hp[attacker_key], hp[attacker_key] + impact * max(0, _fraction(value)))
                healed = round(hp[attacker_key] - before)
                if healed:
                    effect_round(round_number, attacker_key, defender_key, "vampiric", healed)
            if (value := _effect_value(effects[attacker_key], "poison")) is not None and not knocked_out:
                poison = scaled_flat_damage(attacker_key, value, "poison")
                queue_damage(pending_poison, defender_key, attacker_key, poison)
            if (value := _effect_value(effects[attacker_key], "burn")) is not None \
                    and not knocked_out:
                burn_damage = scaled_flat_damage(attacker_key, value, "burn")
                turns = max(1, round(_param(effects[attacker_key], "burn", "turns", 2)))
                burning[defender_key] = (
                    attacker_key, turns, burn_damage, (ELEMENTAL, MAGIC),
                )
            if (value := _effect_value(effects[attacker_key], "venom_blade")) is not None \
                    and not knocked_out:
                venom_damage = scaled_flat_damage(attacker_key, _param(
                    effects[attacker_key], "venom_blade", "poison", 2,
                ), "venom_blade")
                queue_damage(pending_venom, defender_key, attacker_key, venom_damage)
                venom_miss[defender_key] = max(venom_miss[defender_key], max(0, _fraction(value)))
            if (value := _effect_value(effects[attacker_key], "bleed")) is not None \
                    and not knocked_out:
                old = bleeding[defender_key]
                old_stacks = old[1] if old and old[0] == attacker_key else 0
                cap = max(1, round(_param(effects[attacker_key], "bleed", "cap", 3)))
                bleeding[defender_key] = (
                    attacker_key, min(cap, old_stacks + 1),
                    scaled_flat_damage(attacker_key, value, "bleed"),
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
                    max_hp[attacker_key], hp[attacker_key] + bite_impact * max(0, _fraction(value))
                )
                effect_round(round_number, attacker_key, defender_key, "bite", bite_impact)
                if bite_ko:
                    return attacker_key
            if (value := _effect_value(effects[attacker_key], "blood_pact")) is not None \
                    and landed_hits[attacker_key] % 3 == 0:
                before = hp[attacker_key]
                hp[attacker_key] = min(
                    max_hp[attacker_key], hp[attacker_key] + impact * max(0, _fraction(value))
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
                hp[defender_key] = min(max_hp[defender_key], hp[defender_key] + max_hp[defender_key] * max(0, _fraction(value)))
                effect_round(round_number, defender_key, attacker_key, "second_wind", round(hp[defender_key] - before))
            if not knocked_out and (value := _effect_value(effects[defender_key], "medkit")) is not None \
                    and "medkit" not in used[defender_key] \
                    and hp[defender_key] <= max_hp[defender_key] * _param(effects[defender_key], "medkit", "threshold", 35) / 100:
                used[defender_key].add("medkit")
                before = hp[defender_key]
                hp[defender_key] = min(max_hp[defender_key], hp[defender_key] + max_hp[defender_key] * max(0, _fraction(value)))
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
                    retaliation = scaled_flat_damage(defender_key, value, "retaliation")
                    retaliation_bonus[defender_key] += retaliation
                    effect_round(round_number, defender_key, attacker_key, "retaliation", retaliation)
        elif effectful and event == "dodge" and (value := _effect_value(effects[defender_key], "dodge_heal")) is not None:
            landed_hits[attacker_key] = 0
            if _effect_value(effects[attacker_key], "focused") is not None:
                focused_ready[attacker_key] = True
            before = hp[defender_key]
            hp[defender_key] = min(max_hp[defender_key], hp[defender_key] + max(0, value))
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

    stopped_early = False
    winner_key = loser_key = None
    is_draw = False

    action_limit = max(1, int(max_actions or C.MAX_SKILL_ACTIONS_PER_FIGHTER))
    for round_number in range(1, action_limit + 1):
        leader_key = order[(round_number - 1) % 2]
        follower_key = order[round_number % 2]

        winner_key = strike(leader_key, follower_key, round_number)
        if winner_key:
            loser_key = follower_key if winner_key == leader_key else leader_key
            break
        winner_key = strike(follower_key, leader_key, round_number)
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
