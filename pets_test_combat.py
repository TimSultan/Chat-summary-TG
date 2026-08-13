"""Pure turn-based combat used only by the browser prototype.

The live arena remains in :mod:`pets_combat`.  This module receives read-only fighter
snapshots and returns a new JSON-safe state for every action; it imports neither the
economy nor pet storage and therefore cannot award, spend or record anything.
"""

from __future__ import annotations

import copy
import hashlib
import secrets

import pets_scroll_catalog as catalog


TEST_BASE_HP = 620
TEST_HP_PER_HEALTH = 24
# Strength lengthens a test fight as well as making attacks stronger. This is deliberately
# prototype-only until manual combat has produced enough real balance data.
TEST_HP_PER_STRENGTH = 5
TEST_BASE_DAMAGE = 70.0
TEST_DAMAGE_PER_STRENGTH = 5.0
TEST_TURN_LIMIT = 60
BASE_GUARD = .40

_HARMFUL = frozenset({"damage", "burn", "weaken", "blind", "vulnerable", "stun", "break_shield"})
_NEGATIVE = frozenset({"burn", "weaken", "blind", "vulnerable", "stun"})


def _number(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _integer(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _fighter(key: str, snapshot: dict, loadout, shield_code: str) -> dict:
    codes = catalog.validate_loadout(loadout)
    shield = catalog.shield(shield_code)
    if shield is None:
        raise ValueError("Неизвестный щит.")
    stats = snapshot.get("stats") or {}
    strength = max(1, _integer(stats.get("strength"), 1))
    health = max(1, _integer(stats.get("health"), 1))
    agility = max(1, _integer(stats.get("agility"), 1))
    luck = max(1, _integer(stats.get("luck"), 1))
    armor = max(0, _integer(stats.get("armor"), 0))
    max_hp = TEST_BASE_HP + health * TEST_HP_PER_HEALTH + strength * TEST_HP_PER_STRENGTH
    return {
        "key": key,
        "name": str(snapshot.get("name") or ("Герой" if key == "player" else "Соперник")),
        "portrait": snapshot.get("portrait"), "crop": snapshot.get("crop"),
        # Which sort of thing the photograph shows, so the browser can pick an idle
        # animation that matches it. Carried on the fighter rather than looked up by
        # the client, for the same reason the portrait is: this module is handed a
        # read-only snapshot and must not start reading the pet store.
        "kind": str(snapshot.get("kind") or "creature"),
        # Who the picture belongs to, purely so the browser can ask the server what the
        # photograph shows. Never used by the simulation itself.
        "owner_id": (str(snapshot["owner_id"]) if snapshot.get("owner_id") else None),
        "stats": {
            "strength": strength, "health": health, "agility": agility,
            "luck": luck, "armor": armor,
        },
        "max_hp": max_hp, "hp": max_hp,
        "damage": TEST_BASE_DAMAGE + strength * TEST_DAMAGE_PER_STRENGTH,
        "dodge": min(.42, .42 * agility / (agility + 70.0)),
        "crit": min(.32, .04 + .28 * luck / (luck + 80.0)),
        "armor_reduction": min(.55, .55 * armor / (armor + 120.0)) if armor else 0.0,
        "skills": list(codes), "shield": shield_code,
        "used_scrolls": [], "ultimate_used": False, "barrier": 0,
        "guard": 0.0, "statuses": {}, "damage_done": 0,
    }


def start_battle(
    player: dict, enemy: dict, player_loadout=None, enemy_loadout=None,
    player_shield: str | None = None, enemy_shield: str | None = None,
    seed: int | None = None,
) -> dict:
    """Create a self-contained fight. Caller data is copied and never mutated."""
    state = {
        "version": 1, "seed": int(seed if seed is not None else secrets.randbits(63)),
        "roll": 0, "turn": 1, "actor": "player", "winner": None, "draw": False,
        "finished": False, "log": [], "last_action": None,
    }
    state["fighters"] = {
        "player": _fighter(
            "player", copy.deepcopy(player), player_loadout or catalog.SAMPLE_LOADOUT,
            player_shield or catalog.DEFAULT_SHIELD,
        ),
        "enemy": _fighter(
            "enemy", copy.deepcopy(enemy), enemy_loadout or catalog.SAMPLE_LOADOUT,
            enemy_shield or catalog.DEFAULT_SHIELD,
        ),
    }
    state["log"].append({"turn": 0, "kind": "start", "text": "Тестовый бой начался."})
    return state


def _random(state: dict) -> float:
    raw = f"{state['seed']}:{state['roll']}".encode("utf-8")
    state["roll"] += 1
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big") / 2**64


def _choice(state: dict, values: list[str]) -> str:
    if not values:
        raise ValueError("Нет доступных действий.")
    return values[min(len(values) - 1, int(_random(state) * len(values)))]


def _other(key: str) -> str:
    return "enemy" if key == "player" else "player"


def _add_log(state: dict, kind: str, text: str, actor: str | None = None, amount: int = 0) -> None:
    state["log"].append({
        "turn": state["turn"], "kind": kind, "actor": actor, "text": text,
        "amount": int(amount or 0),
    })
    state["log"] = state["log"][-100:]


def _heal(state: dict, fighter: dict, amount: float, source: str) -> int:
    before = fighter["hp"]
    fighter["hp"] = min(fighter["max_hp"], fighter["hp"] + max(0, round(amount)))
    gained = fighter["hp"] - before
    if gained:
        _add_log(state, "heal", f"{fighter['name']} восстанавливает {gained} HP: {source}.",
                 fighter["key"], gained)
    return gained


def _status_set(fighter: dict, name: str, value: float, turns: int) -> None:
    statuses = fighter["statuses"]
    statuses[name] = max(_number(statuses.get(name)), value)
    statuses[name + "_turns"] = max(_integer(statuses.get(name + "_turns")), turns)


def _blocked_negative(state: dict, target: dict, op: str) -> bool:
    if op not in _NEGATIVE or not target["statuses"].pop("negative_ward", False):
        return False
    _add_log(state, "ward", f"Защитная лента {target['name']} блокирует эффект.", target["key"])
    return True


def _misses(state: dict, actor: dict, target: dict, dodgeable: bool) -> bool:
    if not dodgeable:
        return False
    if target["statuses"].pop("dodge_next", False):
        return True
    blind = _number(actor["statuses"].get("blind"))
    return _random(state) < min(.80, target["dodge"] + blind)


def _deal_damage(state: dict, actor: dict, target: dict, effect: dict, label: str) -> int:
    amount = max(0.0, _number(effect.get("amount"), 1.0))
    raw = actor["damage"] * amount * (.90 + _random(state) * .20)
    statuses = actor["statuses"]
    raw *= 1.0 + _number(statuses.get("damage_boost"))
    raw *= 1.0 - min(.90, _number(statuses.get("weaken")))
    critical = _random(state) < actor["crit"]
    if critical:
        raw *= 1.65

    vulnerable = _number(target["statuses"].get("vulnerable"))
    raw *= 1.0 + vulnerable
    if vulnerable:
        left = _integer(target["statuses"].get("vulnerable_turns")) - 1
        target["statuses"]["vulnerable_turns"] = max(0, left)
        if left <= 0:
            target["statuses"].pop("vulnerable", None)

    armor = target["armor_reduction"] * (1.0 - min(1.0, _number(effect.get("pierce_armor"))))
    raw *= 1.0 - armor
    guard = _number(target.get("guard"))
    pierced_guard = min(1.0, _number(effect.get("pierce_guard")))
    guarded_amount = raw * guard * (1.0 - pierced_guard)
    raw -= guarded_amount
    target["guard"] = 0.0
    damage = max(1, round(raw))

    barrier_hit = min(target["barrier"], damage)
    target["barrier"] -= barrier_hit
    hp_hit = min(target["hp"], damage - barrier_hit)
    target["hp"] -= hp_hit
    impact = barrier_hit + hp_hit
    actor["damage_done"] += impact
    crit_text = " Крит!" if critical else ""
    guard_text = f" Защита поглотила {round(guarded_amount)}." if guarded_amount else ""
    _add_log(state, "damage", f"{actor['name']}: {label} — {impact} урона.{crit_text}{guard_text}",
             actor["key"], impact)

    reflected = _number(target["statuses"].pop("reflect_next", 0))
    if reflected and impact:
        back = min(actor["hp"], max(1, round(impact * reflected)))
        actor["hp"] -= back
        _add_log(state, "reflect", f"{target['name']} отражает {back} урона.", target["key"], back)
    lifesteal = _number(effect.get("lifesteal"))
    if lifesteal and impact:
        _heal(state, actor, impact * lifesteal, "похищение жизни")
    return impact


def _cleanse(fighter: dict) -> None:
    for key in (
        "burn", "burn_turns", "weaken", "weaken_turns", "blind", "blind_turns",
        "vulnerable", "vulnerable_turns", "stun",
    ):
        fighter["statuses"].pop(key, None)


def _apply_effect(state: dict, actor: dict, target: dict, effect: dict, label: str) -> None:
    op = effect.get("op")
    recipient = target if op in _HARMFUL else actor
    if recipient is target and _blocked_negative(state, target, op):
        return
    if op == "damage":
        _deal_damage(state, actor, target, effect, label)
    elif op == "heal":
        _heal(state, actor, actor["max_hp"] * _number(effect.get("percent")), label)
    elif op == "shield":
        gained = max(1, round(actor["max_hp"] * _number(effect.get("percent"))))
        actor["barrier"] = min(actor["max_hp"], actor["barrier"] + gained)
        _add_log(state, "shield", f"{actor['name']} получает барьер {gained} HP.", actor["key"], gained)
    elif op == "burn":
        _status_set(target, "burn", actor["damage"] * _number(effect.get("amount")),
                    max(1, _integer(effect.get("turns"), 1)))
        _add_log(state, "status", f"{target['name']} горит.", actor["key"])
    elif op in ("weaken", "blind", "vulnerable"):
        _status_set(target, op, _number(effect.get("value")), max(1, _integer(effect.get("turns"), 1)))
        names = {"weaken": "ослаблен", "blind": "ослеплён", "vulnerable": "уязвим"}
        _add_log(state, "status", f"{target['name']} {names[op]}.", actor["key"])
    elif op == "stun":
        target["statuses"]["stun"] = max(
            _integer(target["statuses"].get("stun")), max(1, _integer(effect.get("turns"), 1))
        )
        _add_log(state, "status", f"{target['name']} оглушён.", actor["key"])
    elif op == "dodge_next":
        actor["statuses"]["dodge_next"] = True
        _add_log(state, "status", f"{actor['name']} готовится исчезнуть от следующего удара.", actor["key"])
    elif op == "reflect_next":
        actor["statuses"]["reflect_next"] = max(
            _number(actor["statuses"].get("reflect_next")), _number(effect.get("value"))
        )
        _add_log(state, "status", f"Вокруг {actor['name']} поднимаются отражающие шипы.", actor["key"])
    elif op == "cleanse":
        _cleanse(actor)
        _add_log(state, "cleanse", f"{actor['name']} снимает негативные эффекты.", actor["key"])
    elif op == "break_shield":
        broken = target["barrier"]
        target["barrier"] = 0
        _add_log(state, "break", f"Барьер {target['name']} разрушен ({broken}).", actor["key"], broken)
    elif op == "regen":
        _status_set(actor, "regen", actor["max_hp"] * _number(effect.get("percent")),
                    max(1, _integer(effect.get("turns"), 1)))
        _add_log(state, "status", f"{actor['name']} начинает восстанавливаться.", actor["key"])
    elif op == "damage_boost":
        _status_set(actor, "damage_boost", _number(effect.get("value")),
                    max(1, _integer(effect.get("turns"), 1)))
        _add_log(state, "status", f"Урон {actor['name']} временно усиливается.", actor["key"])
    elif op == "negative_ward":
        actor["statuses"]["negative_ward"] = True
        _add_log(state, "ward", f"{actor['name']} защищён от следующего проклятия.", actor["key"])
    elif op == "self_damage":
        harm = min(actor["hp"], max(1, round(actor["max_hp"] * _number(effect.get("percent")))))
        actor["hp"] -= harm
        _add_log(state, "damage", f"{actor['name']} платит {harm} HP за силу приёма.", actor["key"], harm)


def _start_of_turn(state: dict, fighter: dict) -> bool:
    statuses = fighter["statuses"]
    burn_turns = _integer(statuses.get("burn_turns"))
    if burn_turns > 0:
        damage = min(fighter["hp"], max(1, round(_number(statuses.get("burn")))))
        fighter["hp"] -= damage
        statuses["burn_turns"] = burn_turns - 1
        _add_log(state, "burn", f"{fighter['name']} получает {damage} урона от огня.",
                 fighter["key"], damage)
        if burn_turns <= 1:
            statuses.pop("burn", None)
    regen_turns = _integer(statuses.get("regen_turns"))
    if fighter["hp"] > 0 and regen_turns > 0:
        _heal(state, fighter, _number(statuses.get("regen")), "восстановление")
        statuses["regen_turns"] = regen_turns - 1
        if regen_turns <= 1:
            statuses.pop("regen", None)
    if fighter["hp"] <= 0:
        return False
    stunned = _integer(statuses.get("stun"))
    if stunned > 0:
        statuses["stun"] = stunned - 1
        _add_log(state, "stun", f"{fighter['name']} оглушён и пропускает ход.", fighter["key"])
        return False
    return True


def _tick_actor_statuses(fighter: dict) -> None:
    for name in ("blind", "weaken", "damage_boost"):
        turns_key = name + "_turns"
        turns = _integer(fighter["statuses"].get(turns_key))
        if turns > 0:
            fighter["statuses"][turns_key] = turns - 1
            if turns <= 1:
                fighter["statuses"].pop(name, None)
def legal_actions(state: dict, actor: str | None = None) -> list[str]:
    if state.get("finished"):
        return []
    key = actor or state.get("actor")
    if key != state.get("actor") or key not in state.get("fighters", {}):
        return []
    fighter = state["fighters"][key]
    actions = ["attack", "defend"]
    for index, code in enumerate(fighter["skills"]):
        spell = catalog.scroll(code)
        if not spell or code in fighter.get("used_scrolls", []):
            continue
        if spell["ultimate"] and fighter["ultimate_used"]:
            continue
        actions.append(f"skill_{index + 1}")
    return actions


def _finish_or_advance(state: dict) -> None:
    player = state["fighters"]["player"]
    enemy = state["fighters"]["enemy"]
    if player["hp"] <= 0 or enemy["hp"] <= 0:
        state["finished"] = True
        if player["hp"] <= 0 and enemy["hp"] <= 0:
            state["draw"] = True
        else:
            state["winner"] = "player" if player["hp"] > 0 else "enemy"
    elif state["turn"] >= TEST_TURN_LIMIT:
        state["finished"] = True
        player_ratio = player["hp"] / player["max_hp"]
        enemy_ratio = enemy["hp"] / enemy["max_hp"]
        if abs(player_ratio - enemy_ratio) < .001:
            state["draw"] = True
        else:
            state["winner"] = "player" if player_ratio > enemy_ratio else "enemy"
    if state["finished"]:
        verdict = "Ничья." if state["draw"] else f"Побеждает {state['fighters'][state['winner']]['name']}."
        _add_log(state, "finish", verdict)
        state["actor"] = None
    else:
        state["actor"] = _other(state["actor"])
        state["turn"] += 1


def _take_turn(state: dict, actor_key: str, action: str) -> None:
    if state.get("finished"):
        raise ValueError("Бой уже завершён.")
    if actor_key != state.get("actor"):
        raise ValueError("Сейчас ход другого бойца.")
    if action not in legal_actions(state, actor_key):
        raise ValueError("Это действие сейчас недоступно.")
    actor = state["fighters"][actor_key]
    target = state["fighters"][_other(actor_key)]
    state["last_action"] = action
    if not _start_of_turn(state, actor):
        _tick_actor_statuses(actor)
        _finish_or_advance(state)
        return

    if action == "attack":
        if _misses(state, actor, target, True):
            _add_log(state, "dodge", f"{target['name']} уклоняется от атаки {actor['name']}.", actor_key)
        else:
            _deal_damage(state, actor, target, {"amount": 1.0}, "обычная атака")
    elif action == "defend":
        shield = catalog.shield(actor["shield"])
        actor["guard"] = _number(shield.get("guard"), BASE_GUARD)
        _add_log(state, "defend", f"{actor['name']} защищается: следующий удар слабее.", actor_key)
        for effect in shield.get("defend_effects", ()):
            _apply_effect(state, actor, target, effect, shield["name"])
    else:
        index = _integer(action.removeprefix("skill_"), 0) - 1
        if index < 0 or index >= len(actor["skills"]):
            raise ValueError("Неизвестный слот свитка.")
        used_code = actor["skills"][index]
        spell = catalog.scroll(used_code)
        actor.setdefault("used_scrolls", []).append(used_code)
        if spell["ultimate"]:
            actor["ultimate_used"] = True
        if _misses(state, actor, target, bool(spell["dodgeable"])):
            _add_log(state, "dodge", f"{target['name']} уклоняется: {spell['name']} не попадает.", actor_key)
        else:
            _add_log(state, "spell", f"{actor['name']} использует «{spell['name']}».", actor_key)
            for effect in spell["effects"]:
                _apply_effect(state, actor, target, effect, spell["name"])

    _tick_actor_statuses(actor)
    _finish_or_advance(state)


def take_turn(state: dict, actor: str, action: str) -> dict:
    """Return a new state after one validated action; never mutate the supplied state."""
    result = copy.deepcopy(state)
    _take_turn(result, actor, str(action or ""))
    return result


def auto_turn(state: dict) -> dict:
    """Take one uniformly random legal action for the current fighter."""
    result = copy.deepcopy(state)
    actor = result.get("actor")
    action = _choice(result, legal_actions(result, actor))
    _take_turn(result, actor, action)
    return result


def run_auto(state: dict) -> dict:
    """Resolve the whole prototype through the same turn function used by manual play."""
    result = copy.deepcopy(state)
    while not result.get("finished") and result.get("turn", 0) <= TEST_TURN_LIMIT:
        action = _choice(result, legal_actions(result, result["actor"]))
        _take_turn(result, result["actor"], action)
    return result


def public_state(state: dict) -> dict:
    """Browser payload without the deterministic seed/roll stream."""
    payload = copy.deepcopy(state)
    payload.pop("seed", None)
    payload.pop("roll", None)
    for fighter in payload.get("fighters", {}).values():
        fighter["slots"] = []
        for index, code in enumerate(fighter.pop("skills", []), start=1):
            spell = catalog.public_scroll(catalog.scroll(code))
            spell["slot"] = index
            spell["available"] = code not in fighter.get("used_scrolls", [])
            fighter["slots"].append(spell)
        fighter["shield"] = catalog.public_shield(catalog.shield(fighter["shield"]))
    payload["legal_actions"] = legal_actions(state)
    payload["test_only"] = True
    return payload
