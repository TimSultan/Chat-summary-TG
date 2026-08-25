"""Interactive fight for the Steel Gatekeeper.

The Phoenix asks the player to read the boss.  This encounter deliberately reverses the
direction: the Gatekeeper reads a short history of broad action categories and the player
progresses by feeding it a pattern, then breaking that prediction.  The module is pure and
JSON-safe; persistence and dungeon rewards live in :mod:`pets`.

WHAT THE PLAYER IS SHOWN, AND WHY IT IS NOT THE ANSWER
------------------------------------------------------
This screen used to print the prediction in words before the choice -- "🎯 Ожидает:
⚔️ Оружие" -- and then say the same thing twice more in prose.  Measured over 300 fights
at the gear the floor is priced for, a policy that read that one line and pressed anything
else won 100% of the time, exactly as often as a perfect engine search; only leftover
health differed, and leftover health at the end of a fight buys nothing.  A fight whose
whole solution is one line of text is a reading exercise.

What is shown now is the EVIDENCE: the categories the player has actually been pressing,
newest last.  The rule that turns evidence into a prediction is stated on the boss itself
(pets_dungeon.BOSS_WEAKNESS) -- recent actions weigh more, a repeat weighs double -- so
nothing is hidden and nothing is guessed.  The player has to do the arithmetic the machine
does.  The history is capped at HISTORY_LIMIT and the memory decays by ADAPTATION_DECAY a
turn, so what is on screen really is everything the machine is working from: after eight
turns an older action is worth 0.72 ** 8 -- about 7% of one press -- and cannot change an
answer on its own.
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
# How fast the machine forgets a category it is not being fed.  Emergency mode barely
# forgets at all, which is its whole escalation: it used to be "now it tracks two
# categories instead of one", but two is the ordinary state now.  A machine that stops
# forgetting is worse to fight and, unlike a third tracked category, it cannot corner the
# player -- see _reasonable_answers, where a third prediction would zero most of the
# attack pool and leave Steel Wall on a loop.
ADAPTATION_DECAY: Final = 0.72
EMERGENCY_ADAPTATION_DECAY: Final = 0.92
# The scores a category needs before the machine commits to tracking it at all.  The
# second track is deliberately close behind the first: with two of four categories read,
# choosing between the two that are left is a decision, where three safe buttons out of
# four was a formality.
PREDICTION_THRESHOLD: Final = 2.15
SECONDARY_THRESHOLD: Final = 1.35
CLOSED_WEAPON_SHARE: Final = 0.18
CLOSED_MAGIC_SHARE: Final = 0.42
CLOSED_DAMAGE_CAP_SHARE: Final = 0.08
# A minimum-power dungeon runner needs several earned windows, not a dozen; advanced
# heroes quickly meet the percentage cap and still cannot erase the encounter in one hit.
CORE_WEAPON_SHARE: Final = 5.00
CORE_MAGIC_SHARE: Final = 4.60
CORE_DAMAGE_CAP_SHARE: Final = 0.38
HISTORY_LIMIT: Final = 8
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
# How much of the history is on screen.  Shorter than HISTORY_LIMIT would hide evidence the
# machine is still using; longer would show presses whose weight has decayed to nothing.
OBSERVED_SHOWN: Final = 6

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


def _hero_profile(hero: dict) -> dict:
    hero = dict(hero or {})
    return {
        "name": str(hero.get("name") or "Существо"),
        "max_hp": max(1, _safe_int(hero.get("max_hp"), 100)),
        "damage": max(1, _safe_int(hero.get("damage"), 10)),
        "spell_power": max(1, _safe_int(hero.get("spell_power"), 10)),
        "crit": max(0.0, min(0.95, float(hero.get("crit", 0) or 0))),
        "crit_power": max(1.0, float(hero.get("crit_power", 1.5) or 1.5)),
        "reduction": max(0.0, min(0.90, float(hero.get("reduction", 0) or 0))),
        "guard": max(0.10, min(0.85, float(hero.get("guard", 0.40) or 0.40))),
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
        "version": 1,
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
        "adaptation": {category: 0.0 for category in CATEGORIES},
        "current_prediction": None,
        "secondary_prediction": None,
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


def _observed(state: dict) -> list[str]:
    """The categories the player has actually pressed, newest last.

    This is the evidence, and deliberately not the conclusion: `current_prediction` stays
    out of the payload entirely so that no client can put it back on screen.
    """
    return [value for value in (state.get("player_action_history") or [])
            if value in CATEGORIES][-OBSERVED_SHOWN:]


def public(state: dict) -> dict:
    state = state or {}
    observed = _observed(state)
    locks_total = max(2, _safe_int(state.get("locks_total"), LOCKS_TOTAL))
    steps_total = max(3, _safe_int(state.get("step_limit"), STEP_LIMIT))
    status = str(state.get("status") or ACTIVE)
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
        # What the machine has SEEN, for the player to draw their own conclusion from.
        # The conclusion itself is not here on purpose -- see the module docstring.
        "observed": observed,
        "observed_icons": [CATEGORY_ICONS[category] for category in observed],
        "tracking_count": len([
            value for value in (state.get("current_prediction"),
                                state.get("secondary_prediction"))
            if value in CATEGORIES
        ]),
        "adaptation_hint": _adaptation_hint(state),
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


def _resolve_turn(state: dict, category: str, rng: random.Random) -> None:
    attack_code = str(state.get("current_boss_action") or "sweep")
    predictions = {
        value for value in (state.get("current_prediction"), state.get("secondary_prediction"))
        if value in CATEGORIES
    }
    predicted = category in predictions
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

    if predicted:
        outgoing = round(outgoing * 0.16)
        incoming_share += 0.65
        mechanical = max(1, round(state["hero_max_hp"] * 0.06))
        state["hero_hp"] = max(0, _safe_int(state.get("hero_hp")) - mechanical)
        state["mistakes"] = _safe_int(state.get("mistakes")) + 1
        state["log"].append(
            f"{CATEGORY_LABELS[category]} было предсказано: Привратник встречает действие контратакой "
            f"и наносит ещё {mechanical} урона."
        )

    dealt, critical = _deal_closed_damage(state, outgoing, category, rng)
    if dealt:
        state["log"].append(
            f"{'Критический удар' if critical else 'Атака'} по закрытой броне: −{dealt} HP босса."
        )
    received = _hurt_hero(state, incoming_share, category == DEFENCE, rng)
    if received:
        state["log"].append(f"Ответ Привратника: −{received} HP героя.")

    fooled = bool(predictions) and not predicted and successful and not step_trap
    if fooled:
        state["log"].extend([
            "Привратник начинает движение для контратаки, но ожидаемого действия не происходит.",
            "ЩЁЛК. Прогноз сломан.",
        ])
        _open_locks(state, 2 if state.get("is_emergency_mode") else 1)

    _remember(state, category)
    if _finish_if_needed(state):
        return
    if _safe_int(state.get("locks_open")) >= _safe_int(state.get("locks_total"), LOCKS_TOTAL):
        _open_core(state)
        return
    _enter_emergency_if_needed(state)
    _prepare_next_turn(state, rng)


def _resolve_false_step(state: dict, rng: random.Random) -> None:
    state["step_counter"] = 0
    state["log"].extend([
        "Последний щелчок.",
        "Привратник обрушивает оружие в рассчитанную точку — но там никого нет.",
        "ЩЁЛК. Ложный шаг обманул старый механизм.",
    ])
    _open_locks(state, 2 if state.get("is_emergency_mode") else 1)
    _remember(state, MOVEMENT)
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
    state["adaptation"] = {
        key: round(float(value or 0) * 0.45, 3)
        for key, value in (state.get("adaptation") or {}).items()
        if key in CATEGORIES
    }
    for category_name in CATEGORIES:
        state["adaptation"].setdefault(category_name, 0.0)
    state["player_action_history"] = list(state.get("player_action_history") or [])[-2:]
    state["current_prediction"] = None
    state["secondary_prediction"] = None
    state["scene"] = "Пластины сходятся. Все замки закрываются, и система начинает новый расчёт."
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
    critical = rng.random() < float(hero.get("crit", 0) or 0)
    if critical:
        raw = round(raw * float(hero.get("crit_power", 1.5) or 1.5))
    return max(0, int(raw)), critical


def _hurt_hero(state: dict, share: float, guarding: bool, rng: random.Random) -> int:
    if share <= 0 or _safe_int(state.get("hero_hp")) <= 0:
        return 0
    raw = state["boss"]["damage"] * max(0.0, float(share)) * rng.uniform(0.93, 1.07)
    if state.get("is_emergency_mode"):
        raw *= 1.15
    raw *= 1.0 - float(state["hero"].get("reduction", 0) or 0)
    if guarding:
        guard = float(state["hero"].get("guard", 0.40) or 0.40)
        if _safe_int(state.get("shield_disrupted")) > 0:
            guard *= 0.35
        raw *= 1.0 - guard
    dealt = min(_safe_int(state.get("hero_hp")), max(0, round(raw)))
    state["hero_hp"] = max(0, _safe_int(state.get("hero_hp")) - dealt)
    return dealt


def _open_locks(state: dict, amount: int) -> None:
    before = _safe_int(state.get("locks_open"))
    total = _safe_int(state.get("locks_total"), LOCKS_TOTAL)
    state["locks_open"] = min(total, before + max(0, int(amount)))
    gained = state["locks_open"] - before
    state["systems_fooled"] = _safe_int(state.get("systems_fooled")) + int(gained > 0)
    if gained:
        state["log"].append(f"Открыто замков: {state['locks_open']} из {total}.")


def _open_core(state: dict) -> None:
    state["is_core_open"] = True
    state["scene"] = (
        "ЩЁЛК. Последний замок раскрывается. Пластины расходятся, и за бронёй становится "
        "видно тусклое ядро. ЯДРО ОТКРЫТО."
    )


def _remember(state: dict, category: str) -> None:
    history = [value for value in (state.get("player_action_history") or []) if value in CATEGORIES]
    repeated = bool(history and history[-1] == category)
    history.append(category)
    state["player_action_history"] = history[-HISTORY_LIMIT:]
    adaptation = dict(state.get("adaptation") or {})
    decay = EMERGENCY_ADAPTATION_DECAY if state.get("is_emergency_mode") else ADAPTATION_DECAY
    for key in CATEGORIES:
        adaptation[key] = round(max(0.0, float(adaptation.get(key, 0) or 0)) * (1.0 if key == category else decay), 3)
    adaptation[category] = round(adaptation[category] + 1.0 + (0.45 if repeated else 0.0), 3)
    state["adaptation"] = adaptation


def _prediction_scores(state: dict, rng: random.Random) -> dict[str, float]:
    scores = {category: max(0.0, float((state.get("adaptation") or {}).get(category, 0) or 0))
              for category in CATEGORIES}
    history = [value for value in (state.get("player_action_history") or []) if value in CATEGORIES][-6:]
    for index, category in enumerate(history, 1):
        scores[category] += 0.18 + index * 0.10
    # A tiny seeded wobble makes mixed histories non-scripted without overpowering a
    # deliberate repeated pattern.
    return {category: value + rng.random() * 0.18 for category, value in scores.items()}


def _choose_predictions(state: dict, rng: random.Random) -> tuple[str | None, str | None]:
    history = [value for value in (state.get("player_action_history") or []) if value in CATEGORIES]
    if len(history) < 2:
        return None, None
    scores = _prediction_scores(state, rng)
    ordered = sorted(CATEGORIES, key=lambda key: scores[key], reverse=True)
    # Two identical actions are a deliberate bait and therefore always readable. Mixed
    # play needs a little more evidence before the machine commits to a prediction.
    repeated = len(history) >= 2 and history[-1] == history[-2]
    primary = ordered[0] if repeated or scores[ordered[0]] >= PREDICTION_THRESHOLD else None
    if primary is None:
        return None, None
    # The second track is the ordinary state, not the emergency one.  With one category
    # read, three of four buttons were safe and the choice made itself; with two, the
    # player picks between the two that are left and that pick has consequences.
    secondary = ordered[1] if scores[ordered[1]] >= SECONDARY_THRESHOLD else None
    return primary, secondary


def _pick_attack(state: dict, rng: random.Random) -> str:
    recent = [value for value in (state.get("player_action_history") or []) if value in CATEGORIES][-6:]
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
    predictions = {
        value for value in (state.get("current_prediction"), state.get("secondary_prediction"))
        if value in CATEGORIES
    }
    false_step_ready = (
        _safe_int(state.get("step_counter")) >=
        _safe_int(state.get("step_limit"), STEP_LIMIT) - 1
    )
    for attack in tuple(weights):
        reasonable = _reasonable_answers(state, attack)
        if not (reasonable - predictions) and not false_step_ready:
            weights[attack] = 0.0
    return _weighted_choice(rng, weights)


def _reasonable_answers(state: dict, attack: str) -> set[str]:
    """Responses that do not deliberately walk into the attack's main punishment.

    Prediction is selected from learned history before the next attack.  Filtering the
    weighted attack pool here is the final guard against a dual prediction covering every
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
    primary, secondary = _choose_predictions(state, rng)
    state["current_prediction"], state["secondary_prediction"] = primary, secondary
    previous = list(state.get("previous_boss_actions") or [])
    current = str(state.get("current_boss_action") or "")
    if current:
        previous.append(current)
    state["previous_boss_actions"] = previous[-5:]
    state["current_boss_action"] = _pick_attack(state, rng)
    if not preserve_scene:
        if primary is None:
            state["scene"] = _adaptation_hint(state)
        elif secondary is None:
            state["scene"] = (
                "Голова Привратника поворачивается вслед за героем. "
                "Один из замков встаёт на предохранитель."
            )
        else:
            state["scene"] = (
                "Система наведения ведёт сразу две траектории. "
                "Два замка встают на предохранитель."
            )


def _adaptation_hint(state: dict) -> str:
    """How far the machine has got, never WHAT it has got to.

    Every line here used to name the category out loud, which meant the fight could be
    won without remembering a single thing the player had done.  Saying that the machine
    has committed is fair warning; saying what it committed to is the answer.
    """
    tracked = len([
        value for value in (state.get("current_prediction"), state.get("secondary_prediction"))
        if value in CATEGORIES
    ])
    if tracked >= 2:
        return "Ведутся две траектории. Обе перекрыты."
    if tracked == 1:
        return "Расчёт сошёлся. Одно из направлений перекрыто."
    adaptation = state.get("adaptation") or {}
    strength = max(
        (float(adaptation.get(key, 0) or 0) for key in CATEGORIES), default=0.0
    )
    if strength < 0.75:
        return "Привратник наблюдает и пока не выбрал, какое действие ждать."
    if strength < 1.6:
        return "Замок на груди тихо щёлкает. Привратник что-то запоминает."
    return "Привратник почти уверен в том, что сейчас произойдёт."


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
    state["log"].append("Броня стала нестабильнее: обман системы открывает по два замка.")


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
