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


def _dominant(mine: float, theirs: float) -> bool:
    """"30% ahead gives 30% more" -- compared per stat, effective values, at fight start."""
    return mine >= theirs * C.DOMINANCE_RATIO


def _luck_tier(luck: float, opponent_luck: float) -> int:
    if luck <= 0 or opponent_luck <= 0:
        return 0
    if luck >= opponent_luck * C.LUCK_OVERWHELMING_RATIO:
        return 3
    if luck >= opponent_luck * C.LUCK_ADVANTAGE_RATIO:
        return 2
    return 0


def derive(fighter: "Fighter", opponent: "Fighter") -> dict:
    """The fight-start numbers for one side.

    Only the stat-derived part of `max_hp`/`damage` is multiplied by the dominance
    factor -- BASE_HP and BASE_DAMAGE are floors everybody gets, not a reward for
    out-scaling somebody (see pets_config). Armor is equipment-only and is deliberately
    excluded from the dominance comparison, so gear can never trigger someone else's bonus
    or lose its own.
    """
    dominance = {
        stat: _dominant(getattr(fighter, stat), getattr(opponent, stat))
        for stat in _STATS
    }

    def factor(stat: str) -> float:
        return 1.0 + C.DOMINANCE_BONUS if dominance[stat] else 1.0

    max_hp = C.BASE_HP + fighter.health * C.HP_PER_POINT * factor("health")
    damage = C.BASE_DAMAGE + fighter.strength * C.DAMAGE_PER_POINT * factor("strength")
    dodge = _saturate(C.DODGE_MAX, C.DODGE_K, fighter.agility * factor("agility"))
    crit = C.CRIT_BASE + _saturate(C.CRIT_MAX, C.CRIT_K, fighter.luck * factor("luck"))
    luck_tier = _luck_tier(fighter.luck, opponent.luck)
    accuracy = 1.0
    accident_chance = 0.0
    if luck_tier == 3:
        dodge = C.LUCK_OVERWHELMING_DODGE_CHANCE
        crit = C.LUCK_OVERWHELMING_CRIT_CHANCE
        accident_chance = C.LUCK_OVERWHELMING_ACCIDENT_CHANCE
    elif luck_tier == 2:
        crit = min(1.0, crit + C.LUCK_ADVANTAGE_CRIT_BONUS)
        accuracy = C.LUCK_ADVANTAGE_MISS_MULTIPLIER
        accident_chance = C.LUCK_ADVANTAGE_ACCIDENT_CHANCE
    reduction = _saturate(C.ARMOR_MAX, C.ARMOR_K, fighter.armor)  # never dominance-boosted

    return {
        "max_hp": max_hp,
        "damage": damage,
        "dodge": dodge,
        "crit": crit,
        "accuracy": accuracy,
        "luck_tier": luck_tier,
        "accident_chance": accident_chance,
        "reduction": reduction,
        "dominance": dominance,
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

    for winner_key, loser_key in ((a.key, b.key), (b.key, a.key)):
        accident_chance = derived[winner_key]["accident_chance"]
        if accident_chance and rng.random() < accident_chance:
            accident = pets_flavor.accident_line(
                fighters[winner_key].name, fighters[loser_key].name, rng=rng,
            )
            return FightResult(
                winner=winner_key,
                loser=loser_key,
                rounds=(),
                opening=opening,
                closing=pets_flavor.result_line(
                    fighters[winner_key].name, fighters[loser_key].name, rng=rng,
                ),
                total_damage=total_damage,
                stopped_early=False,
                is_draw=False,
                seed=seed,
                accident=accident,
            )

    def strike(attacker_key: str, defender_key: str, round_number: int) -> bool:
        """One blow, appended as a Round. Returns whether the defender went down."""
        attacker, defender = fighters[attacker_key], fighters[defender_key]
        event, damage = _resolve_blow(derived[attacker_key], derived[defender_key], rng)
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
        return hp[defender_key] <= 0

    stopped_early = False
    winner_key = loser_key = None
    is_draw = False

    for round_number in range(1, C.MAX_ATTACKS_PER_FIGHTER + 1):
        leader_key = order[(round_number - 1) % 2]
        follower_key = order[round_number % 2]

        if strike(leader_key, follower_key, round_number):
            winner_key, loser_key = leader_key, follower_key
            break
        if strike(follower_key, leader_key, round_number):
            winner_key, loser_key = follower_key, leader_key
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
