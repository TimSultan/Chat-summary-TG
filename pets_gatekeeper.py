"""Interactive fight for the Steel Gatekeeper.

The Phoenix asks the player to read the boss.  This encounter reverses the direction: the
Gatekeeper reads the player, and the player wins by deciding what it learns.  The module
is pure and JSON-safe; persistence and dungeon rewards live in :mod:`pets`.

WHY THE PREDICTION IS ON SCREEN AGAIN
-------------------------------------
It used to be hidden.  The machine tracked how often each button was pressed, printed only
the evidence, and the player worked the answer out -- because when the prediction WAS
printed, "press anything else" won every fight.  That fix treated the symptom.  The real
problem was that the machine learned single buttons, so not repeating yourself beat it
whether or not you could see what it thought.

So the model changed, and with it the reason for hiding anything.  What the Gatekeeper
learns now is TRANSITIONS -- what tends to follow what -- over a window of the last
HISTORY_LIMIT actions, plus the pair before that, plus a standing tendency that outlives a
damage window.  ⚔️→✨→⚔️→✨ is a pattern; so is ⚔️→🛡→⚔️→🛡; so is leaning on movement
whenever the chain comes out.  A player who merely alternates is now the EASIEST to read,
which is the exact opposite of the old fight.

Knowing what it expects is therefore no longer the answer, because:

  * only a HIGH-confidence prediction commits it, and only a broken commitment opens a
    lock (`_lock_gate`), so the player has to feed the pattern long enough for it to
    believe -- and feeding it is dangerous, because doing the predicted thing while it is
    committed is the hardest hit in the fight and CLOSES a lock already open;
  * once a lock is open it also covers the obvious escape (`covered_answer`), so
    "it expects weapon, press magic" stops working and stays visible while it does;
  * its attacks are chosen against the player's own habits, so the answer the attack wants
    is often exactly the answer it is waiting for;
  * the same trick cannot take all three locks: prediction breaks, movement feints and
    combat baits are counted separately and each is spent after LOCK_METHOD_LIMIT.

Everything the machine will do is on the screen before the choice: what it expects, how
sure it is, what else it has covered, and how full the step clock is.  Nothing rolls dice
against the player -- `_forecast` is deterministic -- so a defeat is always something the
player could have read.  That is the line this fight walks: hard, never unfair.
"""

from __future__ import annotations

import copy
import random
from typing import Final


WEAPON: Final = "weapon"
DEFENCE: Final = "defence"
MAGIC: Final = "magic"
MOVEMENT: Final = "movement"
FALSE_STEP: Final = "false_step"
CORE_WEAPON: Final = "core_weapon"
CORE_MAGIC: Final = "core_magic"
CATEGORIES: Final = (WEAPON, DEFENCE, MAGIC, MOVEMENT)

ACTIVE: Final = "active"
VICTORY: Final = "victory"
DEFEAT: Final = "defeat"

LOCKS_TOTAL: Final = 3
STEP_LIMIT: Final = 4
EMERGENCY_HP_SHARE: Final = 0.30

# How many of the player's own actions the machine reasons over.  Everything the forecast
# uses is drawn from this window plus the two decayed tables below, and the window is on
# screen, so the player can always audit the conclusion.
HISTORY_LIMIT: Final = 10
OBSERVED_SHOWN: Final = 6
# Nothing is predicted from one or two presses.  A pattern needs to exist before it can be
# read, and a machine that commits on the second turn is guessing.
MIN_HISTORY_FOR_PREDICTION: Final = 3

# How fast the two learned tables fade.  Tendency (which button this player likes) is
# long-memoried on purpose -- it is the part that survives a damage window.  Transitions
# (what follows what) fade faster, so a pattern deliberately abandoned really does stop
# being believed within a few turns, which is what makes feeding a NEW pattern possible.
TENDENCY_DECAY: Final = 0.86
TRANSITION_DECAY: Final = 0.80
EMERGENCY_TENDENCY_DECAY: Final = 0.95
EMERGENCY_TRANSITION_DECAY: Final = 0.93

# The three confidence bands.  Below TRACKING the machine says it is still watching and
# does nothing with the guess; between the two it hedges (a partial counter, no lock at
# stake); at or above COMMITTED it commits, which is the only state where a lock can be
# won or lost.
CONFIDENCE_TRACKING: Final = 0.42
CONFIDENCE_COMMITTED: Final = 0.60
# What repeating a trick costs.  The second prediction break of a cycle needs a visibly
# deeper commitment than the first, and there is no third -- see LOCK_METHOD_LIMIT.
CONFIDENCE_REUSE_STEP: Final = 0.12

BAND_WATCHING: Final = "watching"
BAND_TRACKING: Final = "tracking"
BAND_COMMITTED: Final = "committed"

# The three ways the machine can be made to fail.  Kept apart so that no single trick
# takes the whole chest: each is spent after LOCK_METHOD_LIMIT uses in a cycle.  The names
# are internal -- the screen shows locks, not taxonomy.
PREDICTION_BREAK: Final = "prediction_break"
MOVEMENT_BREAK: Final = "movement_break"
COMBAT_BAIT: Final = "combat_bait"
LOCK_METHOD_LIMIT: Final = 2

# Attacks built around punishing one specific answer.  Baiting one of these out and then
# NOT giving it what it prepared for is its own kind of break -- see _break_method.
BAIT_TARGETS: Final = {
    "shield_breaker": DEFENCE,
    "magnetic": WEAPON,
}

# How the forecast is weighted as the chest opens.  Closed, it reasons mostly from raw
# habit; each lock turns more of its attention to sequences, so the third lock is fought
# against a machine reading two moves of context rather than one press.
MODEL_WEIGHTS: Final = (
    {"tendency": 1.00, "order1": 0.85, "order2": 0.00},
    {"tendency": 0.70, "order1": 1.40, "order2": 1.05},
    {"tendency": 0.60, "order1": 1.55, "order2": 1.70},
)
EMERGENCY_MODEL_BONUS: Final = {"tendency": 0.0, "order1": 0.35, "order2": 0.45}

# A feint works once.  The second is checked rather than swallowed whole, and the third is
# simply seen -- which is what pushes a player who has found one good trick to find
# another.
FALSE_STEP_CHECKED: Final = 1
FALSE_STEP_SEEN: Final = 2

# What a damage window does NOT wash away.  The locks reset and the current belief is
# dropped, but the machine keeps better than half of what it learned about this player, so
# every cycle starts further along than the last one did.
CORE_CARRY_SHARE: Final = 0.55

CLOSED_WEAPON_SHARE: Final = 0.18
CLOSED_MAGIC_SHARE: Final = 0.42
CLOSED_DAMAGE_CAP_SHARE: Final = 0.08
# A minimum-power dungeon runner needs several earned windows, not a dozen; advanced
# heroes quickly meet the percentage cap and still cannot erase the encounter in one hit.
CORE_WEAPON_SHARE: Final = 5.00
CORE_MAGIC_SHARE: Final = 4.60
CORE_DAMAGE_CAP_SHARE: Final = 0.38
LOG_LINES: Final = 7

CATEGORY_LABELS: Final = {
    WEAPON: "⚔️ Оружие",
    DEFENCE: "🛡 Защита",
    MAGIC: "✨ Магия",
    MOVEMENT: "👣 Движение",
}
# The same four, stripped to the picture, for the row of evidence.  A row of names would
# not be readable at a glance, and the whole point of the row is that it is read at a
# glance and counted.
CATEGORY_ICONS: Final = {
    WEAPON: "⚔️",
    DEFENCE: "🛡",
    MAGIC: "✨",
    MOVEMENT: "👣",
}
# The bare words, for prose.  A label carries its icon, which reads fine as a heading and
# badly in the middle of a sentence -- "повторяет взглядом ⚔️ оружие" is a hiccup, not a
# line -- so anything written as a sentence uses these instead.
CATEGORY_WORDS: Final = {
    WEAPON: "оружие",
    DEFENCE: "защиту",
    MAGIC: "магию",
    MOVEMENT: "движение",
}
# The same four as the subject of a sentence rather than its object.
CATEGORY_SUBJECTS: Final = {
    WEAPON: "удар оружием",
    DEFENCE: "защита",
    MAGIC: "магия",
    MOVEMENT: "движение",
}

ATTACKS: Final = {
    "sweep": {
        "name": "Размашистый удар",
        "telegraph": "Привратник медленно отводит тяжёлый клинок в сторону.",
        "labels": {
            WEAPON: "⚔️ Атаковать",
            DEFENCE: "🛡 Принять на щит",
            MAGIC: "✨ Ударить магией",
            MOVEMENT: "👣 Отойти",
        },
    },
    "shield_breaker": {
        "name": "Дробитель щита",
        "telegraph": "Привратник перехватывает оружие двумя руками и опускает его почти к земле.",
        "labels": {
            WEAPON: "⚔️ Атаковать первым",
            DEFENCE: "🛡 Поднять щит",
            MAGIC: "✨ Ударить магией",
            MOVEMENT: "👣 Отойти",
        },
    },
    "chain": {
        "name": "Захват цепью",
        "telegraph": "Из корпуса Привратника с металлическим скрежетом вырывается тяжёлая цепь.",
        "labels": {
            WEAPON: "⚔️ Перерубить",
            MAGIC: "✨ Разрушить магией",
            MOVEMENT: "👣 Отступить",
        },
    },
    "magnetic": {
        "name": "Магнитный захват",
        "telegraph": "Символ на груди Привратника вспыхивает. Металлическое оружие резко тянет вперёд.",
        "labels": {
            WEAPON: "⚔️ Удержать оружие и ударить",
            DEFENCE: "🛡 Упереться",
            MAGIC: "✨ Использовать магию",
            MOVEMENT: "👣 Отпустить оружие и отойти",
        },
    },
    "steel_wall": {
        "name": "Стальной заслон",
        "telegraph": "Привратник опускается ниже и закрывает корпус массивными пластинами брони.",
        "labels": {
            WEAPON: "⚔️ Атаковать броню",
            DEFENCE: "🛡 Переждать",
            MAGIC: "✨ Ударить сквозь заслон",
            MOVEMENT: "👣 Обойти",
        },
    },
}


def _safe_int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _safe_float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _hero_profile(hero: dict) -> dict:
    hero = dict(hero or {})
    return {
        "name": str(hero.get("name") or "Существо"),
        "max_hp": max(1, _safe_int(hero.get("max_hp"), 100)),
        "damage": max(1, _safe_int(hero.get("damage"), 10)),
        "spell_power": max(1, _safe_int(hero.get("spell_power"), 10)),
        "crit": max(0.0, min(0.95, _safe_float(hero.get("crit"), 0.0))),
        "crit_power": max(1.0, _safe_float(hero.get("crit_power"), 1.5)),
        "reduction": max(0.0, min(0.90, _safe_float(hero.get("reduction"), 0.0))),
        "guard": max(0.10, min(0.85, _safe_float(hero.get("guard"), 0.40))),
    }


def _boss_profile(boss: dict) -> dict:
    boss = dict(boss or {})
    return {
        "name": str(boss.get("name") or "Стальной привратник"),
        "max_hp": max(30, _safe_int(boss.get("max_hp"), 1000)),
        "damage": max(1, _safe_int(boss.get("damage"), 50)),
        "level": max(1, _safe_int(boss.get("level"), 1)),
        "floor": max(1, _safe_int(boss.get("floor"), 10)),
    }


def start(hero: dict, boss: dict, *, seed: int | None = None,
          locks_total: int = LOCKS_TOTAL, step_limit: int = STEP_LIMIT) -> dict:
    """Create the first persisted turn.  No prediction exists until behaviour exists."""
    hero_row, boss_row = _hero_profile(hero), _boss_profile(boss)
    rng = random.Random(seed)
    first_attack = _weighted_choice(rng, {code: 1.0 for code in ATTACKS})
    hero_hp = max(1, min(hero_row["max_hp"], _safe_int(hero.get("hp"), hero_row["max_hp"])))
    return {
        "version": 2,
        "status": ACTIVE,
        "hero": hero_row,
        "boss": boss_row,
        "hero_hp": hero_hp,
        "hero_max_hp": hero_row["max_hp"],
        "boss_hp": boss_row["max_hp"],
        "boss_max_hp": boss_row["max_hp"],
        "locks_open": 0,
        "locks_total": max(2, _safe_int(locks_total, LOCKS_TOTAL)),
        "step_counter": 0,
        "step_limit": max(3, _safe_int(step_limit, STEP_LIMIT)),
        "player_action_history": [],
        # What this player tends to press, and what tends to follow what.  Both survive a
        # damage window at CORE_CARRY_SHARE -- see _resolve_core.
        "tendency": {category: 0.0 for category in CATEGORIES},
        "transitions": {},
        "pair_transitions": {},
        "current_prediction": None,
        "covered_answer": None,
        "confidence": 0.0,
        "committed": False,
        "locks_opened_by": [],
        "false_step_adaptation": 0,
        "current_boss_action": first_attack,
        "previous_boss_actions": [],
        "is_core_open": False,
        "is_emergency_mode": False,
        "shield_disrupted": 0,
        "turn": 1,
        "mistakes": 0,
        "systems_fooled": 0,
        "cores_struck": 0,
        "log": [],
        "scene": (
            "В его забрале нет цели, но старый замок на груди всё ещё отсчитывает "
            "чужие шаги. Привратник пока только наблюдает."
        ),
    }


def is_over(state: dict) -> bool:
    return str((state or {}).get("status") or "") in (VICTORY, DEFEAT)


def actions(state: dict) -> list[dict]:
    """Only currently legal buttons; both clients render this list verbatim."""
    state = state or {}
    if is_over(state):
        return []
    if bool(state.get("is_core_open")):
        return [
            {"code": CORE_WEAPON, "label": "⚔️ Ударить в ядро", "category": WEAPON},
            {"code": CORE_MAGIC, "label": "✨ Ударить ядро магией", "category": MAGIC},
        ]
    attack = ATTACKS.get(str(state.get("current_boss_action") or ""), ATTACKS["sweep"])
    offered = [
        {"code": category, "label": label, "category": category}
        for category, label in attack["labels"].items()
    ]
    if _safe_int(state.get("step_counter")) >= _safe_int(state.get("step_limit"), STEP_LIMIT) - 1:
        offered.append({"code": FALSE_STEP, "label": "👣 Ложный шаг", "category": MOVEMENT})
    return offered


def _history(state: dict) -> list[str]:
    return [value for value in (state.get("player_action_history") or []) if value in CATEGORIES]


def _observed(state: dict) -> list[str]:
    """The categories the player has actually pressed, newest last.

    Still on screen even though the conclusion is shown too: the conclusion is only worth
    trusting if the player can check the working, and this row is the working.
    """
    return _history(state)[-OBSERVED_SHOWN:]


def _band(confidence: float) -> str:
    if confidence >= CONFIDENCE_COMMITTED:
        return BAND_COMMITTED
    if confidence >= CONFIDENCE_TRACKING:
        return BAND_TRACKING
    return BAND_WATCHING


def public(state: dict) -> dict:
    state = state or {}
    observed = _observed(state)
    locks_total = max(2, _safe_int(state.get("locks_total"), LOCKS_TOTAL))
    steps_total = max(3, _safe_int(state.get("step_limit"), STEP_LIMIT))
    status = str(state.get("status") or ACTIVE)
    confidence = max(0.0, min(1.0, _safe_float(state.get("confidence"), 0.0)))
    prediction = state.get("current_prediction")
    prediction = prediction if prediction in CATEGORIES else None
    covered = state.get("covered_answer")
    covered = covered if covered in CATEGORIES else None
    band = _band(confidence) if prediction else BAND_WATCHING
    return {
        "boss_name": str((state.get("boss") or {}).get("name") or "Стальной привратник"),
        "status": status,
        "boss_hp": max(0, _safe_int(state.get("boss_hp"))),
        "boss_max_hp": max(1, _safe_int(state.get("boss_max_hp"), 1)),
        "hero_hp": max(0, _safe_int(state.get("hero_hp"))),
        "hero_max_hp": max(1, _safe_int(state.get("hero_max_hp"), 1)),
        "locks_open": max(0, min(locks_total, _safe_int(state.get("locks_open")))),
        "locks_total": locks_total,
        "locks": [index < _safe_int(state.get("locks_open")) for index in range(locks_total)],
        "step_counter": max(0, min(steps_total, _safe_int(state.get("step_counter")))),
        "step_limit": steps_total,
        "steps": [index < _safe_int(state.get("step_counter")) for index in range(steps_total)],
        # The working, and now the conclusion beside it -- see the module docstring for
        # why showing the conclusion stopped being the whole answer.
        "observed": observed,
        "observed_icons": [CATEGORY_ICONS[category] for category in observed],
        "prediction": prediction,
        "prediction_label": CATEGORY_LABELS.get(prediction or "", ""),
        "prediction_icon": CATEGORY_ICONS.get(prediction or "", ""),
        "confidence": round(confidence, 3),
        "confidence_band": band,
        "committed": bool(state.get("committed")) and prediction is not None,
        # The second thing it has ready: the obvious way out of the first.  Never hidden,
        # because a counter the player cannot see is a coin flip wearing a costume.
        "covered": covered,
        "covered_label": CATEGORY_LABELS.get(covered or "", ""),
        "covered_icon": CATEGORY_ICONS.get(covered or "", ""),
        "prediction_hint": _prediction_hint(state, prediction, band),
        "adaptation_hint": _adaptation_hint(state),
        "step_hint": _step_hint(state),
        "trick_hint": _trick_hint(state),
        "spent_tricks": _spent_tricks(state),
        "false_step_adaptation": max(0, _safe_int(state.get("false_step_adaptation"))),
        "boss_action": str(state.get("current_boss_action") or ""),
        "boss_action_name": str(ATTACKS.get(
            str(state.get("current_boss_action") or ""), {}
        ).get("name") or ""),
        "telegraph": "" if bool(state.get("is_core_open")) or is_over(state) else str(
            ATTACKS.get(str(state.get("current_boss_action") or ""), {}).get("telegraph") or ""
        ),
        "is_core_open": bool(state.get("is_core_open")),
        "is_emergency_mode": bool(state.get("is_emergency_mode")),
        "shield_disrupted": max(0, _safe_int(state.get("shield_disrupted"))),
        "turn": max(1, _safe_int(state.get("turn"), 1)),
        "scene": str(state.get("scene") or ""),
        "log": list(state.get("log") or [])[-LOG_LINES:],
        "actions": [dict(row) for row in actions(state)],
        "over": status in (VICTORY, DEFEAT),
        "won": status == VICTORY,
    }


def take(state: dict, action: str, *, seed: int | None = None) -> dict:
    """Resolve one choice and select the next response from the player's actual history."""
    nxt = copy.deepcopy(dict(state or {}))
    if is_over(nxt):
        raise ValueError("Этот бой уже закончен.")
    allowed = {row["code"] for row in actions(nxt)}
    if action not in allowed:
        raise ValueError("Это действие сейчас недоступно.")
    rng = random.Random(seed)
    nxt["log"] = []
    nxt["scene"] = ""
    if action in (CORE_WEAPON, CORE_MAGIC):
        _resolve_core(nxt, action, rng)
    elif action == FALSE_STEP:
        _resolve_false_step(nxt, rng)
    else:
        _resolve_turn(nxt, action, rng)
    nxt["log"] = list(nxt.get("log") or [])[-LOG_LINES:]
    return nxt


# ------------------------------------------------------------------- what it believes
#
# Deterministic on purpose.  A forecast with a random nudge in it cannot be audited by the
# player, and a fight the player cannot audit is the guessing game this boss is not
# supposed to be.  Everything below reads only from the history window and the two decayed
# tables, all three of which are either on screen or derived from what is.

def _model_weights(state: dict) -> dict[str, float]:
    index = max(0, min(len(MODEL_WEIGHTS) - 1, _safe_int(state.get("locks_open"))))
    weights = dict(MODEL_WEIGHTS[index])
    if state.get("is_emergency_mode"):
        for key, bonus in EMERGENCY_MODEL_BONUS.items():
            weights[key] = weights.get(key, 0.0) + bonus
    return weights


def _normalised(row: dict[str, float]) -> dict[str, float] | None:
    total = sum(max(0.0, value) for value in row.values())
    if total <= 0:
        return None
    return {key: max(0.0, value) / total for key, value in row.items()}


def _forecast(state: dict) -> tuple[dict[str, float], float]:
    """The chance of each category being next, and how sure the machine is of the top one.

    Three readings, blended by `_model_weights`: the standing tendency, what has followed
    the last action before, and what has followed the last PAIR of actions before.  The
    third is the one that reads ⚔️→🛡→⚔️→🛡 as a pattern rather than as four unremarkable
    presses, and it is deliberately switched off until the first lock is open.
    """
    history = _history(state)
    if len(history) < MIN_HISTORY_FOR_PREDICTION:
        return {category: 0.0 for category in CATEGORIES}, 0.0
    weights = _model_weights(state)
    scores = {category: 0.0 for category in CATEGORIES}

    tendency = _normalised({
        category: max(0.0, _safe_float((state.get("tendency") or {}).get(category), 0.0))
        for category in CATEGORIES
    })
    if tendency:
        for category in CATEGORIES:
            scores[category] += weights.get("tendency", 0.0) * tendency[category]

    transitions = state.get("transitions") or {}
    order1 = _normalised({
        category: max(0.0, _safe_float(transitions.get(f"{history[-1]}>{category}"), 0.0))
        for category in CATEGORIES
    })
    if order1:
        for category in CATEGORIES:
            scores[category] += weights.get("order1", 0.0) * order1[category]

    if len(history) >= 2 and weights.get("order2", 0.0) > 0:
        pairs = state.get("pair_transitions") or {}
        prefix = f"{history[-2]}>{history[-1]}"
        order2 = _normalised({
            category: max(0.0, _safe_float(pairs.get(f"{prefix}>{category}"), 0.0))
            for category in CATEGORIES
        })
        if order2:
            for category in CATEGORIES:
                scores[category] += weights.get("order2", 0.0) * order2[category]

    probabilities = _normalised(scores)
    if not probabilities:
        return {category: 0.0 for category in CATEGORIES}, 0.0
    top = max(CATEGORIES, key=lambda category: (probabilities[category], category))
    return probabilities, probabilities[top]


def _remember(state: dict, category: str) -> None:
    """Fold one press into the window and both learned tables."""
    history = _history(state)
    previous = history[-1] if history else None
    pair = f"{history[-2]}>{history[-1]}" if len(history) >= 2 else None
    history.append(category)
    state["player_action_history"] = history[-HISTORY_LIMIT:]

    emergency = bool(state.get("is_emergency_mode"))
    tendency_decay = EMERGENCY_TENDENCY_DECAY if emergency else TENDENCY_DECAY
    transition_decay = EMERGENCY_TRANSITION_DECAY if emergency else TRANSITION_DECAY

    tendency = {
        key: round(max(0.0, _safe_float((state.get("tendency") or {}).get(key), 0.0)) * tendency_decay, 4)
        for key in CATEGORIES
    }
    tendency[category] = round(tendency[category] + 1.0, 4)
    state["tendency"] = tendency

    for table_key in ("transitions", "pair_transitions"):
        # Dropping the crumbs keeps the saved state small and means an abandoned pattern
        # really does leave the model rather than lingering at four decimal places.
        state[table_key] = {
            key: round(_safe_float(value, 0.0) * transition_decay, 4)
            for key, value in (state.get(table_key) or {}).items()
            if _safe_float(value, 0.0) * transition_decay >= 0.02
        }
    if previous:
        key = f"{previous}>{category}"
        state["transitions"][key] = round(_safe_float(state["transitions"].get(key), 0.0) + 1.0, 4)
    if pair:
        key = f"{pair}>{category}"
        state["pair_transitions"][key] = round(
            _safe_float(state["pair_transitions"].get(key), 0.0) + 1.0, 4
        )


# ------------------------------------------------------------------- winning a lock
#
# Three ways in, counted separately, each spent after LOCK_METHOD_LIMIT.  Deterministic:
# whether a lock opens is never a roll, so a player who understands the rule can plan two
# turns ahead, which is the whole point of the encounter.

def _spent_tricks(state: dict) -> int:
    used = [value for value in (state.get("locks_opened_by") or [])]
    return len([
        method for method in (PREDICTION_BREAK, MOVEMENT_BREAK, COMBAT_BAIT)
        if used.count(method) >= LOCK_METHOD_LIMIT
    ])


def _lock_gate(state: dict, method: str) -> tuple[bool, str]:
    """Whether this trick can still take a lock, and what to say when it cannot."""
    used = [value for value in (state.get("locks_opened_by") or [])].count(method)
    if used >= LOCK_METHOD_LIMIT:
        return False, "Механизм уже дважды попадался на этот обман. Замок не поддаётся."
    if method == MOVEMENT_BREAK:
        return True, ""
    needed = CONFIDENCE_COMMITTED + used * CONFIDENCE_REUSE_STEP
    if _safe_float(state.get("confidence"), 0.0) + 1e-9 < needed:
        return False, (
            "Привратник качнулся, но не вложился в расчёт: этого обмана он ждал "
            "и на этот раз не открылся."
        )
    return True, ""


def _break_method(state: dict, attack: str, category: str) -> str:
    """Which kind of failure this was.

    An attack built to punish exactly what the machine is expecting -- the shield breaker
    against an expected shield, the magnet against an expected blade -- is a longer, more
    committed wind-up than an ordinary swing.  Walking it out and then answering with
    something offensive is a different trick from simply not being where it looked, and it
    is counted separately so a player who has spent one still has the other.
    """
    if BAIT_TARGETS.get(attack) == state.get("current_prediction") and category in (WEAPON, MAGIC):
        return COMBAT_BAIT
    return PREDICTION_BREAK


def _open_locks(state: dict, amount: int, method: str) -> None:
    before = _safe_int(state.get("locks_open"))
    total = _safe_int(state.get("locks_total"), LOCKS_TOTAL)
    state["locks_open"] = min(total, before + max(0, int(amount)))
    gained = state["locks_open"] - before
    if not gained:
        return
    state["systems_fooled"] = _safe_int(state.get("systems_fooled")) + 1
    state["locks_opened_by"] = list(state.get("locks_opened_by") or []) + [method]
    state["log"].append(f"Открыто замков: {state['locks_open']} из {total}.")


def _close_one_lock(state: dict) -> bool:
    """The price of walking into a committed prediction, and only that."""
    if _safe_int(state.get("locks_open")) <= 0:
        return False
    state["locks_open"] = _safe_int(state.get("locks_open")) - 1
    opened = list(state.get("locks_opened_by") or [])
    if opened:
        # The method that won the lock is handed back with it, so a player who loses a
        # lock also gets that trick back rather than being locked out of their own plan.
        opened.pop()
    state["locks_opened_by"] = opened
    state["log"].extend([
        "Привратник встречает ваше движение ещё до того, как оно началось.",
        "Механизм на его груди делает обратный оборот.",
        "ЩЁЛК. Один из замков закрывается.",
    ])
    return True


# --------------------------------------------------------------------- resolving a turn

def _resolve_turn(state: dict, category: str, rng: random.Random) -> None:
    attack_code = str(state.get("current_boss_action") or "sweep")
    prediction = state.get("current_prediction")
    prediction = prediction if prediction in CATEGORIES else None
    covered = state.get("covered_answer")
    covered = covered if covered in CATEGORIES else None
    committed = bool(state.get("committed")) and prediction is not None
    confidence = _safe_float(state.get("confidence"), 0.0)

    outgoing, incoming_share, successful, lines = _outcome(state, attack_code, category, rng)
    state["log"].extend(lines)

    step_trap = False
    if category == MOVEMENT:
        state["step_counter"] = _safe_int(state.get("step_counter")) + 1
        if state["step_counter"] >= _safe_int(state.get("step_limit"), STEP_LIMIT):
            step_trap = True
            successful = False
            state["step_counter"] = 0
            incoming_share += 0.90
            mechanical = max(1, round(state["hero_max_hp"] * 0.14))
            state["hero_hp"] = max(0, _safe_int(state.get("hero_hp")) - mechanical)
            state["mistakes"] = _safe_int(state.get("mistakes")) + 1
            state["log"].append(
                f"Последний щелчок выдаёт позицию. Расчётный удар наносит {mechanical} урона сквозь броню."
            )
        elif state["step_counter"] == _safe_int(state.get("step_limit"), STEP_LIMIT) - 1:
            state["log"].append(
                "Замок вращается быстрее: следующий настоящий шаг завершит расчёт позиции."
            )

    if committed and category == prediction:
        # The expensive half of feeding a pattern.  Everything above hedges; this one
        # takes a lock back, and it is the only thing in the fight that does.
        outgoing = round(outgoing * 0.10)
        incoming_share += 0.85
        mechanical = max(1, round(state["hero_max_hp"] * 0.09))
        state["hero_hp"] = max(0, _safe_int(state.get("hero_hp")) - mechanical)
        state["mistakes"] = _safe_int(state.get("mistakes")) + 1
        state["log"].append(
            f"{CATEGORY_LABELS[category]} — ровно то, к чему Привратник готовился. "
            f"Контрмера срабатывает полностью: ещё {mechanical} урона."
        )
        _close_one_lock(state)
    elif covered is not None and category == covered:
        # Warned about, in words, before the choice.  Losing to this is losing to a line
        # of text the player had time to read.
        outgoing = round(outgoing * 0.45)
        incoming_share += 0.35
        state["log"].append(
            f"{CATEGORY_LABELS[category]} было перекрыто заранее: руны на груди гасят "
            "половину удара."
        )
    elif committed:
        method = _break_method(state, attack_code, category)
        allowed, refusal = _lock_gate(state, method)
        if not successful:
            state["log"].append(
                "Прогноз не оправдался, но и ответ не удался: механизм остаётся закрытым."
            )
        elif allowed:
            state["log"].extend(_break_lines(state, attack_code, method, prediction))
            _open_locks(state, 2 if state.get("is_emergency_mode") else 1, method)
        else:
            state["log"].append(refusal)
    elif prediction is not None and category == prediction and confidence >= CONFIDENCE_TRACKING:
        # It was leaning that way without committing: a partial parry, no lock at stake.
        outgoing = round(outgoing * 0.62)
        incoming_share += 0.22
        state["log"].append(
            f"Привратник успевает довернуть корпус: {CATEGORY_SUBJECTS[category]} "
            "проходит вполсилы."
        )

    dealt, critical = _deal_closed_damage(state, outgoing, category, rng)
    if dealt:
        state["log"].append(
            f"{'Критический удар' if critical else 'Атака'} по закрытой броне: −{dealt} HP босса."
        )
    received = _hurt_hero(state, incoming_share, category == DEFENCE, rng)
    if received:
        state["log"].append(f"Ответ Привратника: −{received} HP героя.")

    _remember(state, category)
    if _finish_if_needed(state):
        return
    if _safe_int(state.get("locks_open")) >= _safe_int(state.get("locks_total"), LOCKS_TOTAL):
        _open_core(state)
        return
    _enter_emergency_if_needed(state)
    _prepare_next_turn(state, rng)


def _break_lines(state: dict, attack: str, method: str, prediction: str | None) -> list[str]:
    if method == COMBAT_BAIT:
        return [
            f"Привратник дожимает длинный замах, рассчитанный на {CATEGORY_WORDS.get(prediction or '', 'ответ')}.",
            "Ответ приходит не оттуда — и приходит раньше, чем замах успевает закончиться.",
            "ЩЁЛК. Контрмера сработала вхолостую.",
        ]
    return [
        "Привратник переносит вес вперёд и заранее готовит контрмеру.",
        "Он встречает удар, которого нет.",
        "ЩЁЛК. Прогноз сломан.",
    ]


def _resolve_false_step(state: dict, rng: random.Random) -> None:
    adaptation = max(0, _safe_int(state.get("false_step_adaptation")))
    state["step_counter"] = 0
    allowed, refusal = _lock_gate(state, MOVEMENT_BREAK)
    state["log"].append("Последний щелчок.")

    if adaptation >= FALSE_STEP_SEEN or not allowed:
        # Seen it twice; the machine simply does not commit any more.  No lock, and the
        # step clock is still spent, which is the cost of trying a trick that is used up.
        state["log"].append(
            "Привратник не двигается с места. Он уже знает эту уловку и ждёт настоящего шага."
        )
        if not allowed and adaptation < FALSE_STEP_SEEN:
            state["log"].append(refusal)
        received = _hurt_hero(state, 0.30, False, rng)
        if received:
            state["log"].append(f"Ответ Привратника: −{received} HP героя.")
    elif adaptation == FALSE_STEP_CHECKED:
        state["log"].extend([
            "Привратник коротко проверяет движение, прежде чем ударить.",
            "Он всё же обрушивает оружие в рассчитанную точку — с опозданием, но обрушивает.",
            "ЩЁЛК. Механизм обманут, хотя и не полностью.",
        ])
        _open_locks(state, 2 if state.get("is_emergency_mode") else 1, MOVEMENT_BREAK)
        received = _hurt_hero(state, 0.22, False, rng)
        if received:
            state["log"].append(f"Задетый вскользь: −{received} HP героя.")
    else:
        state["log"].extend([
            "Привратник обрушивает оружие в рассчитанную точку — но там никого нет.",
            "ЩЁЛК. Ложный шаг обманул старый механизм.",
        ])
        _open_locks(state, 2 if state.get("is_emergency_mode") else 1, MOVEMENT_BREAK)

    state["false_step_adaptation"] = adaptation + (2 if state.get("is_emergency_mode") else 1)
    _remember(state, MOVEMENT)
    if _finish_if_needed(state):
        return
    if _safe_int(state.get("locks_open")) >= _safe_int(state.get("locks_total"), LOCKS_TOTAL):
        _open_core(state)
        return
    _enter_emergency_if_needed(state)
    _prepare_next_turn(state, rng)


def _resolve_core(state: dict, action: str, rng: random.Random) -> None:
    hero = state["hero"]
    category = WEAPON if action == CORE_WEAPON else MAGIC
    share = CORE_WEAPON_SHARE if category == WEAPON else CORE_MAGIC_SHARE
    power = hero["damage"] if category == WEAPON else hero["spell_power"]
    raw, critical = _critical(round(power * share), hero, rng)
    cap = max(1, round(state["boss_max_hp"] * CORE_DAMAGE_CAP_SHARE))
    dealt = min(cap, max(1, raw), _safe_int(state.get("boss_hp")))
    state["boss_hp"] = max(0, _safe_int(state.get("boss_hp")) - dealt)
    state["cores_struck"] = _safe_int(state.get("cores_struck")) + 1
    state["log"].append(
        f"{'Критический ' if critical else ''}{'удар оружием' if category == WEAPON else 'магический удар'} "
        f"в открытое ядро: −{dealt} HP босса."
    )
    _remember(state, category)
    if _finish_if_needed(state):
        return
    state["is_core_open"] = False
    state["locks_open"] = 0
    state["step_counter"] = 0
    # The locks reset and the belief is dropped, but what it learned about this player is
    # only halved -- so the second cycle opens against a machine that already knows the
    # habits the first one taught it.  See CORE_CARRY_SHARE.
    state["tendency"] = {
        key: round(max(0.0, _safe_float((state.get("tendency") or {}).get(key), 0.0)) * CORE_CARRY_SHARE, 4)
        for key in CATEGORIES
    }
    for table_key in ("transitions", "pair_transitions"):
        state[table_key] = {
            key: round(_safe_float(value, 0.0) * CORE_CARRY_SHARE, 4)
            for key, value in (state.get(table_key) or {}).items()
            if _safe_float(value, 0.0) * CORE_CARRY_SHARE >= 0.02
        }
    state["player_action_history"] = _history(state)[-2:]
    state["current_prediction"] = None
    state["covered_answer"] = None
    state["committed"] = False
    state["confidence"] = 0.0
    state["locks_opened_by"] = []
    state["scene"] = (
        "Пластины сходятся. Все замки закрываются — но расчёт начинается не с чистого "
        "листа: механизм помнит, как вы дрались."
    )
    _enter_emergency_if_needed(state)
    _prepare_next_turn(state, rng, preserve_scene=True)


def _outcome(state: dict, attack: str, category: str,
             rng: random.Random) -> tuple[int, float, bool, list[str]]:
    hero = state["hero"]
    power = hero["damage"] if category == WEAPON else hero["spell_power"] if category == MAGIC else 0
    outgoing = 0
    incoming = 0.0
    successful = True
    lines: list[str] = []
    if category == WEAPON:
        outgoing = round(power * (0.06 if attack == "steel_wall" else 0.12 if attack == "magnetic" else CLOSED_WEAPON_SHARE))
    elif category == MAGIC:
        outgoing = round(power * (0.62 if attack == "steel_wall" else CLOSED_MAGIC_SHARE))

    if attack == "sweep":
        incoming = {DEFENCE: 0.70, MOVEMENT: 0.05, WEAPON: 0.82, MAGIC: 0.72}[category]
        if category == DEFENCE:
            lines.append("Щит встречает размашистый удар и принимает основную тяжесть.")
        elif category == MOVEMENT:
            lines.append("Клинок проходит следом за героем, не находя цели.")
    elif attack == "shield_breaker":
        incoming = {DEFENCE: 1.55, MOVEMENT: 0.04, WEAPON: 1.00, MAGIC: 0.82}[category]
        if category == DEFENCE:
            state["shield_disrupted"] = 2
            successful = False
            lines.append("Дробитель вминает поднятый щит. Его эффективность временно снижена.")
        elif category == MOVEMENT:
            lines.append("Тяжёлый удар проваливается в пустоту.")
    elif attack == "chain":
        incoming = {MOVEMENT: 0.05, WEAPON: 0.10, MAGIC: 0.10}[category]
        if category in (WEAPON, MAGIC):
            threshold = state["boss"]["damage"] * (0.62 if category == WEAPON else 0.55)
            if power < threshold:
                incoming = 1.12
                successful = False
                lines.append("Цепь выдерживает удар и захлёстывает героя.")
            else:
                lines.append("Цепь лопается прежде, чем успевает сомкнуться.")
        else:
            lines.append("Герой отступает за пределы захвата цепи.")
    elif attack == "magnetic":
        incoming = {DEFENCE: 0.48, MOVEMENT: 0.14, WEAPON: 0.78, MAGIC: 0.32}[category]
        if category == WEAPON and hero["damage"] < state["boss"]["damage"] * 0.75:
            incoming = 1.15
            successful = False
            lines.append("Магнитный рывок ломает стойку и утягивает оружие к Привратнику.")
        elif category == MAGIC:
            lines.append("Магнитному полю нечего схватить в потоке магии.")
        elif category == MOVEMENT:
            lines.append("Герой отпускает натяжение и уходит с линии рывка.")
    else:  # steel_wall
        incoming = 0.0
        if category == WEAPON:
            lines.append("Клинок оставляет на массивных пластинах лишь светлую царапину.")
        elif category == MAGIC:
            lines.append("Магия просачивается между пластинами, но ядро всё ещё закрыто.")
        elif category == MOVEMENT:
            lines.append("Герой обходит заслон, заставляя корпус Привратника поворачиваться следом.")
        else:
            lines.append("Обе стороны выжидают, не отдавая друг другу темп.")
    return outgoing, incoming, successful, lines


def _deal_closed_damage(state: dict, outgoing: int, category: str,
                        rng: random.Random) -> tuple[int, bool]:
    if outgoing <= 0 or category not in (WEAPON, MAGIC):
        return 0, False
    raw, critical = _critical(outgoing, state["hero"], rng)
    cap = max(1, round(state["boss_max_hp"] * CLOSED_DAMAGE_CAP_SHARE))
    dealt = min(cap, max(1, raw), _safe_int(state.get("boss_hp")))
    state["boss_hp"] = max(0, _safe_int(state.get("boss_hp")) - dealt)
    return dealt, critical


def _critical(raw: int, hero: dict, rng: random.Random) -> tuple[int, bool]:
    critical = rng.random() < _safe_float(hero.get("crit"), 0.0)
    if critical:
        raw = round(raw * _safe_float(hero.get("crit_power"), 1.5))
    return max(0, int(raw)), critical


def _hurt_hero(state: dict, share: float, guarding: bool, rng: random.Random) -> int:
    if share <= 0 or _safe_int(state.get("hero_hp")) <= 0:
        return 0
    raw = state["boss"]["damage"] * max(0.0, float(share)) * rng.uniform(0.93, 1.07)
    if state.get("is_emergency_mode"):
        raw *= 1.15
    raw *= 1.0 - _safe_float(state["hero"].get("reduction"), 0.0)
    if guarding:
        guard = _safe_float(state["hero"].get("guard"), 0.40)
        if _safe_int(state.get("shield_disrupted")) > 0:
            guard *= 0.35
        raw *= 1.0 - guard
    dealt = min(_safe_int(state.get("hero_hp")), max(0, round(raw)))
    state["hero_hp"] = max(0, _safe_int(state.get("hero_hp")) - dealt)
    return dealt


def _open_core(state: dict) -> None:
    state["is_core_open"] = True
    state["current_prediction"] = None
    state["covered_answer"] = None
    state["committed"] = False
    state["scene"] = (
        "Механизм на груди начинает вращаться в разные стороны. Раздаётся три "
        "металлических щелчка. Броневые пластины расходятся. ЯДРО ОТКРЫТО."
    )


# ----------------------------------------------------------------- choosing an attack

def _pick_attack(state: dict, rng: random.Random) -> str:
    """Weighted by how this player actually fights, never a guaranteed counter.

    A hard counter -- block twice, get the shield breaker every time -- would hand the
    player the boss's script to drive.  These are leanings: pressing a button often makes
    the answer to it likelier, not certain.
    """
    recent = _history(state)[-6:]
    counts = {category: recent.count(category) for category in CATEGORIES}
    weights = {
        "sweep": 1.0,
        "shield_breaker": 0.8 + counts[DEFENCE] * 1.3,
        "chain": 0.8 + max(0, 3 - counts[MOVEMENT]) * 0.42,
        "magnetic": 0.8 + counts[WEAPON] * 0.78,
        "steel_wall": 0.8 + counts[WEAPON] * 0.42 + counts[MAGIC] * 0.20,
    }
    previous = list(state.get("previous_boss_actions") or [])
    if previous and previous[-1] in weights:
        weights[previous[-1]] *= 0.22
    blocked = {
        value for value in (state.get("current_prediction"), state.get("covered_answer"))
        if value in CATEGORIES
    }
    false_step_ready = (
        _safe_int(state.get("step_counter")) >=
        _safe_int(state.get("step_limit"), STEP_LIMIT) - 1
    )
    for attack in tuple(weights):
        if not (_reasonable_answers(state, attack) - blocked) and not false_step_ready:
            weights[attack] = 0.0
    if sum(weights.values()) <= 0:
        # Nothing left that leaves the player a sane answer, so the SECOND counter is the
        # thing that goes.  A turn with no good button is the one state this fight is not
        # allowed to reach -- see the module docstring.  Steel Wall answers to all four
        # categories, so dropping one counter always refills the pool; the fallback below
        # exists so a future attack table cannot turn that into a recursion.
        if state.get("covered_answer") in CATEGORIES:
            state["covered_answer"] = None
            return _pick_attack(state, rng)
        return "steel_wall"
    return _weighted_choice(rng, weights)


def _reasonable_answers(state: dict, attack: str) -> set[str]:
    """Responses that do not deliberately walk into the attack's main punishment.

    The last guard against a prediction plus a covered answer between them covering every
    sensible button.  Steel Wall always leaves answers, so the pool can never be empty.
    """
    hero, boss = state["hero"], state["boss"]
    if attack == "sweep":
        return {DEFENCE, MOVEMENT}
    if attack == "shield_breaker":
        return {MOVEMENT}
    if attack == "chain":
        result = {MOVEMENT}
        if hero["damage"] >= boss["damage"] * 0.62:
            result.add(WEAPON)
        if hero["spell_power"] >= boss["damage"] * 0.55:
            result.add(MAGIC)
        return result
    if attack == "magnetic":
        return {DEFENCE, MAGIC, MOVEMENT}
    return set(CATEGORIES)


def _weighted_choice(rng: random.Random, weights: dict[str, float]) -> str:
    total = sum(max(0.0, value) for value in weights.values())
    point = rng.random() * total
    for key, value in weights.items():
        point -= max(0.0, value)
        if point <= 0:
            return key
    return next(reversed(weights))


def _prepare_next_turn(state: dict, rng: random.Random, *, preserve_scene: bool = False) -> None:
    state["turn"] = _safe_int(state.get("turn"), 1) + 1
    if _safe_int(state.get("shield_disrupted")) > 0:
        state["shield_disrupted"] = max(0, _safe_int(state.get("shield_disrupted")) - 1)

    probabilities, confidence = _forecast(state)
    ordered = sorted(CATEGORIES, key=lambda key: (probabilities[key], key), reverse=True)
    prediction = ordered[0] if confidence >= CONFIDENCE_TRACKING else None
    committed = prediction is not None and confidence >= CONFIDENCE_COMMITTED
    state["current_prediction"] = prediction
    state["confidence"] = round(confidence, 4)
    state["committed"] = committed
    # The second counter only exists once the chest has started opening, and only while
    # the machine is actually committed -- otherwise it would be covering an escape from a
    # prediction it has not made.  It is the runner-up outright, with no floor under it:
    # a floor made the cleanest patterns -- the ones where the runner-up is furthest
    # behind -- the SAFEST to break, which is exactly backwards.
    covered = ordered[1] if committed and _safe_int(state.get("locks_open")) >= 1 else None
    state["covered_answer"] = covered

    previous = list(state.get("previous_boss_actions") or [])
    current = str(state.get("current_boss_action") or "")
    if current:
        previous.append(current)
    state["previous_boss_actions"] = previous[-5:]
    state["current_boss_action"] = _pick_attack(state, rng)
    if not preserve_scene:
        state["scene"] = _scene_for(state)


def _scene_for(state: dict) -> str:
    prediction = state.get("current_prediction")
    if prediction not in CATEGORIES:
        return _adaptation_hint(state)
    band = _band(_safe_float(state.get("confidence"), 0.0))
    covered = state.get("covered_answer")
    if band != BAND_COMMITTED:
        return (
            "Голова Привратника поворачивается вслед за героем. "
            "Он всё внимательнее повторяет движение взглядом."
        )
    if covered in CATEGORIES:
        return (
            "Привратник переносит вес вперёд и заранее поднимает клинок. "
            "По рунам на груди одновременно проходит холодное свечение — "
            "перекрыт и очевидный выход."
        )
    return (
        "Привратник переносит вес вперёд и заранее поднимает клинок для парирования. "
        "Он уже не смотрит — он ждёт."
    )


def _prediction_hint(state: dict, prediction: str | None, band: str) -> str:
    """What the band MEANS, next to a header that already names the category.

    Repeating "он ждёт оружие" under a line that says "🎯 Ожидает: ⚔️ Оружие" spends the
    most valuable row on the screen saying nothing.  At the committed band the useful
    thing is the stake -- both halves of it, because a player who only knows the reward
    will feed the pattern one turn too long.
    """
    if prediction not in CATEGORIES:
        return "Привратник наблюдает и пока не выбрал, какое действие ждать."
    word = CATEGORY_WORDS[prediction]
    if band == BAND_COMMITTED:
        stake = ("Сломай прогноз — откроется замок. "
                 "Сделай ожидаемое — контрмера сработает полностью")
        return stake + (
            " и один открытый замок закроется."
            if _safe_int(state.get("locks_open")) > 0 else "."
        )
    if band == BAND_TRACKING:
        return (
            f"Привратник всё внимательнее повторяет взглядом {word}. "
            "Он ещё не вложился в расчёт — сломать его сейчас нечего."
        )
    return f"Привратник присматривается к тому, как вы используете {word}."


def _step_hint(state: dict) -> str:
    counter = _safe_int(state.get("step_counter"))
    limit = _safe_int(state.get("step_limit"), STEP_LIMIT)
    if counter >= limit - 1:
        return (
            "Привратник больше не следит за вами взглядом. Кажется, он уже рассчитывает, "
            "где вы окажетесь после следующего шага."
        )
    if counter >= limit - 2:
        return "Замок на груди отсчитывает ваши шаги всё быстрее."
    return ""


def _adaptation_hint(state: dict) -> str:
    """How far the machine has got when it has not got far enough to name a category."""
    history = _history(state)
    if len(history) < MIN_HISTORY_FOR_PREDICTION:
        return "Привратник наблюдает и пока не выбрал, какое действие ждать."
    return "Замок на груди тихо щёлкает. Привратник ищет закономерность и пока не находит."


def _trick_hint(state: dict) -> str:
    """Said out loud when a way in has been used up, because being surprised by an
    exhausted trick is exactly the kind of unfairness this fight avoids.  Which trick is
    never named -- the taxonomy is internal -- but that one is gone is not a secret.
    """
    if not _spent_tricks(state):
        return ""
    if _spent_tricks(state) > 1:
        return "Механизм больше не поддаётся прежним обманам. Остался один способ."
    return "Один и тот же обман больше не проходит. Нужен другой."


def _enter_emergency_if_needed(state: dict) -> None:
    if state.get("is_emergency_mode") or _safe_int(state.get("boss_hp")) <= 0:
        return
    if _safe_int(state.get("boss_hp")) > round(state["boss_max_hp"] * EMERGENCY_HP_SHARE):
        return
    state["is_emergency_mode"] = True
    state["scene"] = (
        "Механизм внутри груди вращается слишком быстро. Раздаётся металлический треск. "
        "СИСТЕМА НАВЕДЕНИЯ ПЕРЕХОДИТ В АВАРИЙНЫЙ РЕЖИМ. Она перестаёт забывать."
    )
    state["log"].append(
        "Расчёт идёт быстрее, обман изнашивается вдвое быстрее — но и обманутый "
        "механизм открывает по два замка."
    )


def _finish_if_needed(state: dict) -> bool:
    if _safe_int(state.get("boss_hp")) <= 0:
        state["boss_hp"] = 0
        state["status"] = VICTORY
        state["is_core_open"] = False
        state["scene"] = "Ядро гаснет. Стальной привратник замирает, так и не завершив последний расчёт."
        return True
    if _safe_int(state.get("hero_hp")) <= 0:
        state["hero_hp"] = 0
        state["status"] = DEFEAT
        state["is_core_open"] = False
        state["scene"] = "Прогноз сходится. Стальной привратник перекрывает путь дальше."
        return True
    return False
