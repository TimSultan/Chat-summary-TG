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
is stuck permanently swinging second. `MAX_ATTACKS_PER_FIGHTER` is a hard cap, so there
can be no more than that many blows from either side.
"""

import random
from dataclasses import dataclass

import pets_config as C
import pets_flavor

_STATS = ("strength", "health", "agility", "luck")
_SIGNATURE_STATS = _STATS + ("armor",)


@dataclass(frozen=True)
class Fighter:
    key: str            # opaque id (a user id as str) -- combat never interprets it
    name: str            # pet name, already display-ready
    strength: int        # EFFECTIVE values: purchased level + pet level + equipment
    health: int
    agility: int
    luck: int
    armor: int           # from equipment only, 0 for a bare pet


@dataclass(frozen=True)
class Round:
    number: int
    attacker: str        # Fighter.key
    event: str            # one of pets_flavor's combat events
    damage: int
    attacker_hp: int     # AFTER the blow
    defender_hp: int
    text: str             # the flavour line, ready to print


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

    def factor(stat: str) -> float:
        return 1.0 + stat_bonus[stat]

    max_hp = C.BASE_HP + fighter.health * C.HP_PER_POINT * factor("health")
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


def simulate(a: "Fighter", b: "Fighter", rng=None, seed: int | None = None) -> "FightResult":
    """Run one fight to a finish. Deterministic for a given seeded `rng`.

    Leadership alternates once a first mover is picked; the pick and every roll after it
    come from `rng`, so replaying the same rng state replays the identical fight -- which
    is the whole point of keeping this module pure.
    """
    if rng is not None and seed is not None:
        raise ValueError("pass either rng or seed, not both")
    rng = random.Random(seed) if seed is not None else (rng or random)
    derived = {a.key: derive(a, b), b.key: derive(b, a)}
    fighters = {a.key: a, b.key: b}
    hp = {a.key: derived[a.key]["max_hp"], b.key: derived[b.key]["max_hp"]}
    total_damage = {a.key: 0, b.key: 0}

    order = [a.key, b.key] if rng.random() < 0.5 else [b.key, a.key]
    opening = pets_flavor.line("opening", fighters[order[0]].name, fighters[order[1]].name, rng=rng)

    rounds = []
    signatures = {}
    for key in (a.key, b.key):
        signature = derived[key]["signature"]
        if signature and rng.random() < C.SIGNATURE_TRIGGER_CHANCES[signature[0]][signature[1]]:
            signatures[key] = signature

    def signature_round(attacker_key: str, defender_key: str, event: str, damage: int) -> bool:
        hp[defender_key] = max(0.0, hp[defender_key] - damage)
        total_damage[attacker_key] += damage
        rounds.append(Round(
            number=0, attacker=attacker_key, event=event, damage=damage,
            attacker_hp=round(hp[attacker_key]), defender_hp=round(hp[defender_key]),
            text=pets_flavor.line(event, fighters[attacker_key].name, fighters[defender_key].name, damage, rng=rng),
        ))
        return hp[defender_key] <= 0

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
            )

    def strike(attacker_key: str, defender_key: str, round_number: int) -> str | None:
        """One blow, appended as a Round. Returns the key of a fighter knocked out."""
        attacker, defender = fighters[attacker_key], fighters[defender_key]
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
        event, damage = _resolve_blow(derived[attacker_key], derived[defender_key], rng)
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
        hp[defender_key] = max(0.0, hp[defender_key] - damage)
        total_damage[attacker_key] += damage
        text = pets_flavor.line(event, attacker.name, defender.name, damage, rng=rng)
        rounds.append(Round(
            number=round_number,
            attacker=attacker_key,
            event=event,
            damage=damage,
            attacker_hp=round(hp[attacker_key]),
            defender_hp=round(hp[defender_key]),
            text=text,
        ))
        return attacker_key if hp[defender_key] <= 0 else None

    stopped_early = False
    winner_key = loser_key = None
    is_draw = False

    for round_number in range(1, C.MAX_ATTACKS_PER_FIGHTER + 1):
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
    )
