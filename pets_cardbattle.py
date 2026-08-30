"""Slay-the-Spire-style card duel, as a pure state machine.

The web layer owns persistence, session tokens and who is allowed to fight whom. This
module only moves cards, energy, statuses and HP around JSON-safe dictionaries: every
public function takes a plain dict and gives a plain dict back, so one duel can be kept
in a session, logged or replayed without importing anything else from the game.

Nothing here reads the pet store, the economy or the item catalogue. Both decks arrive
already built as plain dicts (see `pets_web._card_deck_for`), which is what keeps a whole
duel -- start to last card, however many turns -- to exactly one store read.

The shape of a turn, which every helper below assumes:

    player turn   block reset -> energy refilled -> draw up to HAND_SIZE -> cards played
    end_turn()    hand discarded -> player's own burn/poison/regen tick
    enemy turn    enemy block reset -> enemy plays the plan the player was shown ->
                  enemy's own burn/poison/regen tick -> timed statuses count down
    next turn     a new plan is drawn and shown before the player acts again

The opponent spends the same energy on the same kind of turn the player does -- three
energy, several cards -- so `enemy_intent` is that whole planned turn, a LIST of cards,
not a single move. It is chosen at the end of the current turn and published, so the
player always plays against a known answer rather than a surprise.

Choosing it a turn early is also the whole reason the choice is random rather than
clever: an opponent that read the board would have to choose after seeing the player's
turn, and then there would be nothing to put on screen a turn ahead.
"""

from __future__ import annotations

import copy
import random
from dataclasses import dataclass, field
from typing import Final


HAND_SIZE: Final = 5
MAX_HAND: Final = 10          # cards drawn past this burn, as in the genre
ENERGY_PER_TURN: Final = 3
TURN_LIMIT: Final = 40

VULNERABLE_BONUS: Final = 0.50    # extra damage taken while vulnerable
WEAK_PENALTY: Final = 0.33        # damage lost while weak

# One row per status: what it is called, how it is drawn, and the rule in the words the
# card text and the fighter badge both quote. `decays` marks the ones that lose a point
# at the end of a round; strength and thorns are permanent, which is why either one is
# worth a whole card on its own.
STATUS_META: Final = {
    "burn": {
        "icon": "🔥", "name": "Ожог", "decays": True,
        "hint": "В конце своего хода жжёт на своё число и слабеет на 1. Блок не спасает.",
    },
    "poison": {
        "icon": "☠️", "name": "Яд", "decays": True,
        "hint": "В конце своего хода травит на своё число и слабеет на 1. Блок не спасает.",
    },
    "vulnerable": {
        "icon": "💔", "name": "Уязвимость", "decays": True,
        "hint": "Получает на 50% больше урона. −1 за ход.",
    },
    "weak": {
        "icon": "🥀", "name": "Слабость", "decays": True,
        "hint": "Наносит на 33% меньше урона. −1 за ход.",
    },
    "regen": {
        "icon": "🌿", "name": "Регенерация", "decays": True,
        "hint": "В конце своего хода лечит на своё число и слабеет на 1.",
    },
    "stun": {
        "icon": "💫", "name": "Оглушение", "decays": False,
        "hint": "Пропускает столько своих ходов.",
    },
    "strength": {
        "icon": "💪", "name": "Сила", "decays": False,
        "hint": "+1 к урону каждого удара за единицу. Держится весь бой.",
    },
    "thorns": {
        "icon": "🌵", "name": "Шипы", "decays": False,
        "hint": "Тот, кто бьёт, получает столько же в ответ. Держится весь бой.",
    },
}

STATUS_KEYS: Final = tuple(STATUS_META)


@dataclass(frozen=True)
class CardTemplate:
    """One card. Every number on it is absolute -- scaling against the owner's stats has
    already happened in `base_deck` / `card_from_item`, so the engine never multiplies
    anything by a stat and a card means the same in the hand as it did in the deck list.
    """

    code: str
    name: str
    cost: int
    icon: str = "🂠"
    damage: int = 0
    hits: int = 1
    block: int = 0
    heal: int = 0
    energy: int = 0
    draw: int = 0
    self_damage: int = 0
    pierce: bool = False           # ignores the target's block
    lifesteal: int = 0             # percent of damage dealt returned as HP
    execute: int = 0               # extra percent against a target under 40% HP
    apply: dict = field(default_factory=dict)    # statuses forced onto the target
    boost: dict = field(default_factory=dict)    # statuses granted to whoever played it
    exhaust: bool = False
    effect: str = ""
    rarity: str = "common"
    source: str = "base"
    item_code: str = ""
    item_slot: str = ""

    def public(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "icon": self.icon,
            "cost": max(0, int(self.cost)),
            "damage": max(0, int(self.damage)),
            "hits": max(1, int(self.hits)),
            "block": max(0, int(self.block)),
            "heal": max(0, int(self.heal)),
            "energy": int(self.energy),
            "draw": max(0, int(self.draw)),
            "self_damage": max(0, int(self.self_damage)),
            "pierce": bool(self.pierce),
            "lifesteal": max(0, int(self.lifesteal)),
            "execute": max(0, int(self.execute)),
            "apply": {k: int(v) for k, v in (self.apply or {}).items() if v},
            "boost": {k: int(v) for k, v in (self.boost or {}).items() if v},
            "exhaust": bool(self.exhaust),
            "effect": self.effect,
            "rarity": self.rarity,
            "source": self.source,
            "item_code": self.item_code,
            "item_slot": self.item_slot,
        }


# --------------------------------------------------------------------------- card text
def tags(card: dict) -> list[str]:
    """The badges under a card's name.

    Short enough to sit in a row on a phone, and every one of them is a number the rules
    actually use -- never a rounded-off "много урона", because the point of a card is
    that the whole turn can be added up before any energy is spent on it.
    """
    rows: list[str] = []
    damage = int(card.get("damage", 0) or 0)
    hits = max(1, int(card.get("hits", 1) or 1))
    if damage:
        rows.append("⚔️ " + str(damage) + (f" ×{hits}" if hits > 1 else ""))
    if card.get("pierce") and damage:
        rows.append("🗡 сквозь блок")
    if int(card.get("block", 0) or 0):
        rows.append("🛡 " + str(int(card["block"])))
    if int(card.get("heal", 0) or 0):
        rows.append("❤️ " + str(int(card["heal"])))
    if int(card.get("energy", 0) or 0):
        rows.append(f"⚡ {int(card['energy']):+d}")
    if int(card.get("draw", 0) or 0):
        rows.append("🃏 +" + str(int(card["draw"])))
    if int(card.get("lifesteal", 0) or 0):
        rows.append("🩸 " + str(int(card["lifesteal"])) + "% в HP")
    if int(card.get("execute", 0) or 0):
        rows.append("☠️ +" + str(int(card["execute"])) + "% добивание")
    for key, value in (card.get("apply") or {}).items():
        meta = STATUS_META.get(key) or {}
        rows.append(f"{meta.get('icon', '•')} {meta.get('name', key)} {int(value)}")
    for key, value in (card.get("boost") or {}).items():
        meta = STATUS_META.get(key) or {}
        rows.append(f"{meta.get('icon', '•')} себе +{int(value)} {meta.get('name', key)}")
    if int(card.get("self_damage", 0) or 0):
        rows.append("💢 −" + str(int(card["self_damage"])) + " HP себе")
    if card.get("exhaust"):
        rows.append("♻️ исчезает")
    return rows


# --------------------------------------------------------------------------- base deck
def base_deck(attack: int, max_hp: int) -> list[dict]:
    """The cards everybody has, whatever is in their bag.

    A player who has never found a legendary still gets a whole deck out of this, and a
    player who owns thirty of them still opens on these -- the copy counts below are what
    stop a big collection from turning every hand into a spike of finishers with no
    energy left to play any of them.
    """
    attack = max(3, int(attack))
    block = max(3, round(attack * 0.85))
    heal = max(3, round(max_hp * 0.08))
    templates: list[tuple[CardTemplate, int]] = [
        (CardTemplate(
            "strike", "Удар", 1, "⚔️", damage=attack,
            effect="Обычная атака.",
        ), 4),
        (CardTemplate(
            "guard", "Защита", 1, "🛡", block=block,
            effect="Блок держится до конца хода соперника и гасит его атаку.",
        ), 3),
        (CardTemplate(
            "spark", "Искра", 0, "✨", damage=max(2, round(attack * 0.55)), draw=1,
            effect="Ничего не стоит: бьёт немного и сразу возвращает карту в руку.",
        ), 1),
        (CardTemplate(
            "breather", "Передышка", 0, "💨", block=max(2, round(block * 0.5)), energy=1,
            effect="Отдаёт энергию обратно и оставляет немного блока.",
        ), 1),
        (CardTemplate(
            "tactics", "Тактика", 1, "🃏", draw=2,
            effect="Взять 2 карты. Карт в руке становится больше, энергии — нет.",
        ), 1),
        (CardTemplate(
            "overload", "Перегрузка", 0, "🔌", energy=2, draw=1,
            self_damage=max(1, round(max_hp * 0.035)), exhaust=True,
            effect="+2 энергии и карта, ценой своего здоровья. Одна на бой: уходит навсегда.",
        ), 1),
        (CardTemplate(
            "whet", "Заточка", 1, "💪", boost={"strength": 2},
            effect="+2 к урону каждой следующей атаки до конца боя.",
        ), 1),
        (CardTemplate(
            "pierce", "Пробой", 2, "🗡", damage=round(attack * 1.6), pierce=True,
            effect="Проходит сквозь блок целиком.",
        ), 1),
        (CardTemplate(
            "tonic", "Настойка", 1, "🧪", heal=heal, boost={"regen": 2},
            effect="Лечит сразу и ещё два хода понемногу.",
        ), 1),
    ]
    deck: list[dict] = []
    for template, copies in templates:
        for _ in range(copies):
            deck.append(template.public())
    return deck


# ------------------------------------------------------- legendary items become cards
# One row per legendary passive in the catalogue, translated into something a card can
# actually do on a board of energy, block and statuses. The passives themselves are
# written for the auto-simulator ("за каждый полученный удар: +9% урона, без потолка"),
# which has no turns a player spends and no hand to spend them from -- so the mapping
# keeps the IDEA of each passive (pressure grows, chain_crit repeats, wound ignores
# defence) and re-prices it in card terms instead of trying to port the arithmetic.
#
# `damage`, `block`, `heal` and `self_damage` are fractions -- of the owner's card attack
# for the first two, of the owner's maximum HP for the last two. Everything else is an
# absolute number, because "ожог 4" means the same on any board and "4 урона" does not.
# Anything this table has never heard of falls through to `_slot_rule`.
_ITEM_RULES: Final = {
    # --- growing and repeating ------------------------------------------------------
    "pressure":       {"cost": 1, "damage": 0.9, "boost": {"strength": 3}},
    "charge_crit":    {"cost": 1, "damage": 0.8, "boost": {"strength": 4}},
    "blind_fury":     {"cost": 1, "damage": 1.5, "boost": {"strength": 2}, "self_damage": 0.03},
    "momentum":       {"cost": 1, "damage": 1.0, "boost": {"strength": 2}},
    "runic_charge":   {"cost": 1, "damage": 0.7, "boost": {"strength": 2}, "energy": 1},
    "chain_crit":     {"cost": 2, "damage": 0.95, "hits": 3},
    "double_strike":  {"cost": 1, "damage": 0.8, "hits": 2},
    "echo_strike":    {"cost": 2, "damage": 0.85, "hits": 3},
    "double_cast":    {"cost": 2, "damage": 0.8, "hits": 2, "draw": 1},
    "wild_swing":     {"cost": 1, "damage": 1.1, "hits": 2, "self_damage": 0.03},
    "tesla":          {"cost": 2, "damage": 0.9, "hits": 2, "apply": {"weak": 1}},
    "heavy_combo":    {"cost": 2, "damage": 2.4},
    # --- fire, poison and bleeding ---------------------------------------------------
    "burn":           {"cost": 1, "damage": 0.7, "apply": {"burn": 5}},
    "candle":         {"cost": 1, "damage": 0.5, "apply": {"burn": 4}, "draw": 1},
    "venom_blade":    {"cost": 1, "damage": 0.6, "apply": {"poison": 4, "weak": 1}},
    "bleed":          {"cost": 1, "damage": 0.6, "apply": {"poison": 5}},
    "hex":            {"cost": 1, "apply": {"poison": 4, "vulnerable": 2}},
    "mana_burn":      {"cost": 1, "damage": 0.8, "apply": {"weak": 2}},
    "chill":          {"cost": 1, "damage": 0.6, "apply": {"weak": 2, "vulnerable": 1}},
    "stun":           {"cost": 2, "damage": 0.8, "apply": {"stun": 1}},
    # --- defence stripped off the target ----------------------------------------------
    "armor_shred":    {"cost": 1, "damage": 0.8, "apply": {"vulnerable": 2}},
    "shatter":        {"cost": 1, "damage": 0.9, "apply": {"vulnerable": 2}},
    "crushing_grip":  {"cost": 2, "damage": 1.4, "apply": {"vulnerable": 2}},
    "shield_breaker": {"cost": 1, "damage": 1.3, "pierce": True},
    "spell_pierce":   {"cost": 1, "damage": 1.2, "pierce": True},
    "wound":          {"cost": 1, "damage": 1.1, "pierce": True, "apply": {"poison": 3}},
    "glass_crit":     {"cost": 2, "damage": 1.6, "execute": 60},
    "hunger":         {"cost": 1, "damage": 1.2, "execute": 50, "self_damage": 0.02},
    "blood_price":    {"cost": 0, "damage": 1.4, "self_damage": 0.06},
    "recoil":         {"cost": 1, "damage": 1.3, "boost": {"thorns": 3}},
    "spell_thorns":   {"cost": 1, "block": 0.8, "boost": {"thorns": 4}},
    # --- health back -------------------------------------------------------------------
    "vampiric":       {"cost": 1, "damage": 1.0, "lifesteal": 50},
    "blood_pact":     {"cost": 1, "damage": 1.3, "lifesteal": 40, "self_damage": 0.02},
    "soul_debt":      {"cost": 1, "damage": 1.1, "lifesteal": 35},
    "reap":           {"cost": 2, "damage": 1.5, "lifesteal": 30},
    "regen":          {"cost": 1, "heal": 0.55, "boost": {"regen": 4}},
    "dodge_heal":     {"cost": 1, "block": 0.7, "heal": 0.35},
    "rewind":         {"cost": 1, "heal": 0.5, "draw": 1},
    # --- defence for the owner ---------------------------------------------------------
    "defend_effect":  {"cost": 1, "block": 1.4},
    "perfect_parry":  {"cost": 1, "block": 1.3, "boost": {"thorns": 3}},
    "ward":           {"cost": 1, "block": 1.2, "draw": 1},
    "spell_shield":   {"cost": 1, "block": 1.3, "energy": 1},
    "death_shield":   {"cost": 2, "block": 2.2},
    "cocoon":         {"cost": 1, "block": 1.5, "boost": {"regen": 3}},
    "survivor":       {"cost": 1, "block": 1.1, "heal": 0.25},
    "glass_body":     {"cost": 0, "block": 1.0, "self_damage": 0.02},
    "afterimage":     {"cost": 0, "block": 0.9, "draw": 1},
    "phantom_step":   {"cost": 0, "block": 0.8, "energy": 1},
    # --- tempo: energy and cards -------------------------------------------------------
    "arcane_surge":   {"cost": 0, "energy": 2, "draw": 1},
    "arcane_battery": {"cost": 0, "energy": 2, "block": 0.5},
    "focus_shift":    {"cost": 0, "energy": 1, "draw": 2},
    "first_strike":   {"cost": 0, "energy": 1, "damage": 0.6},
    "spring":         {"cost": 0, "energy": 1, "block": 0.6},
    "spell_siphon":   {"cost": 1, "damage": 0.7, "energy": 1},
    "lucky":          {"cost": 0, "draw": 2},
    "collector":      {"cost": 1, "draw": 2, "block": 0.6},
    "tax":            {"cost": 1, "damage": 0.7, "draw": 1},
    "coin_rake":      {"cost": 1, "damage": 1.0, "draw": 1},
    "combo":          {"cost": 1, "damage": 1.1, "draw": 1},
    # --- plain, but bigger than a Strike ------------------------------------------------
    "precision":      {"cost": 1, "damage": 1.25},
    "focused":        {"cost": 1, "damage": 1.3},
    "mob_hunter":     {"cost": 1, "damage": 1.2},
    "executioner":    {"cost": 1, "damage": 1.0, "execute": 55},
    "retaliation":    {"cost": 1, "block": 1.0, "boost": {"thorns": 3}},
}

# What a legendary with no passive at all, or one this table has never heard of, becomes.
# Read off the slot, because the slot is the one thing every item has: a weapon swings, a
# shield blocks, boots buy a turn, gloves fish for a card, an amulet mends.
_SLOT_RULES: Final = {
    "weapon": {"cost": 1, "damage": 1.3},
    "shield": {"cost": 1, "block": 1.5},
    "boots":  {"cost": 0, "energy": 1, "block": 0.6},
    "gloves": {"cost": 1, "damage": 0.7, "draw": 1},
    "amulet": {"cost": 1, "heal": 0.45, "block": 0.7},
}
_FALLBACK_RULE: Final = {"cost": 1, "damage": 1.1}

_SLOT_ICONS: Final = {
    "weapon": "🗡", "shield": "🛡", "boots": "👢", "gloves": "🧤", "amulet": "📿",
}


def _slot_rule(slot: str) -> dict:
    return dict(_SLOT_RULES.get(slot) or _FALLBACK_RULE)


def card_from_item(item: dict, attack: int, max_hp: int) -> dict:
    """One legendary out of the bag, as one card.

    `item` is the flat dict the web layer builds from a catalogue entry: code, name,
    slot, effect_code, effect_text. Nothing is imported from the catalogue here, so the
    engine stays testable against a literal.
    """
    attack = max(3, int(attack))
    max_hp = max(10, int(max_hp))
    slot = str(item.get("slot") or "")
    code = str(item.get("code") or "item")
    effect_code = str(item.get("effect_code") or "")
    rule = dict(_ITEM_RULES.get(effect_code) or _slot_rule(slot))

    self_damage = round(max_hp * float(rule.get("self_damage", 0) or 0))
    if rule.get("self_damage"):
        # A card that costs health has to cost at least a point of it, or the drawback in
        # its own rules text is a lie on a small health bar.
        self_damage = max(1, self_damage)

    # The catalogue's own sentence is the card's rules text wherever there is one: the
    # player has already read it on the item, and printing a second wording for the same
    # object is how two descriptions of one thing start to disagree.
    effect = str(item.get("effect_text") or "").strip()
    if not effect:
        effect = "Легендарный предмет из сумки. Дерётся как карта, не занимая слот."

    template = CardTemplate(
        code=f"item:{code}",
        name=str(item.get("name") or "Легендарная карта"),
        cost=int(rule.get("cost", 1)),
        icon=_SLOT_ICONS.get(slot, "🟣"),
        damage=round(attack * float(rule.get("damage", 0) or 0)),
        hits=int(rule.get("hits", 1) or 1),
        block=round(attack * float(rule.get("block", 0) or 0)),
        heal=round(max_hp * float(rule.get("heal", 0) or 0)),
        energy=int(rule.get("energy", 0) or 0),
        draw=int(rule.get("draw", 0) or 0),
        self_damage=self_damage,
        pierce=bool(rule.get("pierce")),
        lifesteal=int(rule.get("lifesteal", 0) or 0),
        execute=int(rule.get("execute", 0) or 0),
        apply=dict(rule.get("apply") or {}),
        boost=dict(rule.get("boost") or {}),
        effect=effect,
        rarity="legendary",
        source="item",
        item_code=code,
        item_slot=slot,
    )
    return template.public()


def build_deck(items: list[dict], attack: int, max_hp: int) -> list[dict]:
    """Base cards plus one card per legendary owned.

    Order does not matter -- `start` shuffles. The deck is rebuilt from the bag at the
    opening of every duel rather than stored, which is what makes selling a legendary
    take its card away with it and finding one put a card in without any migration.
    """
    deck = base_deck(attack, max_hp)
    for item in items or ():
        deck.append(card_from_item(item, attack, max_hp))
    return deck


# ------------------------------------------------------------------------------ engine
def start(player: dict, enemy: dict, player_cards: list[dict], enemy_cards: list[dict],
          *, seed: int | None = None) -> dict:
    """Open a duel. `player` and `enemy` are {id, name, hp, max_hp, attack}."""
    state: dict = {
        "seed": int(seed or 0),
        "rng": int(seed or 0),
        "turn": 1,
        "finished": False,
        "winner": None,
        "draw": False,
        "energy": ENERGY_PER_TURN,
        "max_energy": ENERGY_PER_TURN,
        "fighters": {"player": _fighter(player), "enemy": _fighter(enemy)},
        "player_draw": _with_uids(player_cards, "p"),
        "player_discard": [],
        "player_exhaust": [],
        "hand": [],
        "enemy_draw": _with_uids(enemy_cards, "e"),
        "enemy_discard": [],
        "enemy_exhaust": [],
        "enemy_hand": [],
        "enemy_intent": None,
        "log": [],
    }
    rng = _rng(state)
    rng.shuffle(state["player_draw"])
    rng.shuffle(state["enemy_draw"])
    _draw(state, "player", HAND_SIZE, rng)
    state["enemy_intent"] = _pick_plan(state, rng)
    state["log"] = ["Ход 1. Соперник уже показал, чем ответит."]
    return state


def play(state: dict, card_uid: str) -> dict:
    """Spend energy on one card out of the hand."""
    nxt = copy.deepcopy(dict(state or {}))
    _assert_live(nxt)
    rng = _rng(nxt)
    hand = list(nxt.get("hand") or [])
    index = next((i for i, row in enumerate(hand) if row.get("uid") == card_uid), -1)
    if index < 0:
        raise ValueError("Этой карты нет в руке.")
    card = hand[index]
    cost = max(0, int(card.get("cost", 0) or 0))
    if cost > int(nxt.get("energy", 0) or 0):
        raise ValueError("Не хватает энергии на эту карту.")
    hand.pop(index)
    nxt["hand"] = hand
    nxt["energy"] = int(nxt.get("energy", 0) or 0) - cost
    nxt["log"] = []
    _resolve(nxt, "player", "enemy", card, rng)
    if card.get("exhaust"):
        nxt.setdefault("player_exhaust", []).append(card)
    else:
        nxt.setdefault("player_discard", []).append(card)
    _check_finished(nxt)
    return nxt


def end_turn(state: dict) -> dict:
    """Hand the turn over. The enemy plays exactly the intent that was on screen."""
    nxt = copy.deepcopy(dict(state or {}))
    _assert_live(nxt)
    rng = _rng(nxt)
    nxt["log"] = []

    nxt.setdefault("player_discard", []).extend(nxt.get("hand") or [])
    nxt["hand"] = []
    _tick(nxt, "player")
    _check_finished(nxt)
    if nxt.get("finished"):
        return nxt

    enemy = nxt["fighters"]["enemy"]
    enemy["block"] = 0
    if int(enemy["status"].get("stun", 0) or 0) > 0:
        enemy["status"]["stun"] = int(enemy["status"]["stun"]) - 1
        nxt["log"].append(f"{enemy['name']} оглушён и пропускает ход.")
    else:
        # Exactly the cards that were on screen, in the order they were shown. Nothing is
        # re-picked here: an opponent that revised its plan after seeing the player's turn
        # would make the intent panel a lie.
        for card in list(nxt.get("enemy_intent") or ()):
            _resolve(nxt, "enemy", "player", dict(card), rng)
            if int(nxt["fighters"]["player"].get("hp", 0) or 0) <= 0:
                break
    _tick(nxt, "enemy")
    _check_finished(nxt)
    if nxt.get("finished"):
        return nxt

    _decay(nxt, "player")
    _decay(nxt, "enemy")

    nxt["turn"] = int(nxt.get("turn", 1) or 1) + 1
    if int(nxt["turn"]) > TURN_LIMIT:
        nxt["finished"] = True
        nxt["draw"] = True
        nxt["winner"] = None
        nxt["log"].append(f"Ходов вышло {TURN_LIMIT}. Ничья.")
        return nxt

    nxt["fighters"]["player"]["block"] = 0
    nxt["energy"] = int(nxt.get("max_energy", ENERGY_PER_TURN) or ENERGY_PER_TURN)
    _draw(nxt, "player", HAND_SIZE, rng)
    nxt["enemy_intent"] = _pick_plan(nxt, rng)
    return nxt


def public(state: dict) -> dict:
    """What the screen is allowed to know: its own hand, both fighters, the sizes of the
    piles, and the one enemy card that has already been announced.

    The rest of the opponent's deck stays on the server. A duel where the player can read
    the other draw pile out of the response is not the game this shows.
    """
    state = dict(state or {})
    fighters = state.get("fighters") or {}
    return {
        "turn": int(state.get("turn", 1) or 1),
        "turn_limit": TURN_LIMIT,
        "finished": bool(state.get("finished")),
        "winner": state.get("winner"),
        "draw": bool(state.get("draw")),
        "energy": int(state.get("energy", 0) or 0),
        "max_energy": int(state.get("max_energy", ENERGY_PER_TURN) or ENERGY_PER_TURN),
        "hand_limit": MAX_HAND,
        "fighters": {
            "player": _public_fighter(fighters.get("player") or {}),
            "enemy": _public_fighter(fighters.get("enemy") or {}),
        },
        "hand": [_public_card(row) for row in state.get("hand") or ()],
        # The opponent's whole announced turn, in the order it will be played.
        "enemy_intent": [_public_card(row) for row in state.get("enemy_intent") or ()],
        "deck": {
            "draw": len(state.get("player_draw") or ()),
            "discard": len(state.get("player_discard") or ()),
            "exhaust": len(state.get("player_exhaust") or ()),
            "enemy_draw": len(state.get("enemy_draw") or ()),
            "enemy_discard": len(state.get("enemy_discard") or ()),
        },
        "log": list(state.get("log") or ()),
        "statuses": {key: dict(meta) for key, meta in STATUS_META.items()},
    }


# ----------------------------------------------------------------------------- helpers
def _rng(state: dict) -> random.Random:
    """A fresh generator per call, advanced from a counter that lives in the state.

    Keeping the counter in the dict rather than in the session is what makes a duel
    reproducible from `seed` alone: the same starting seed and the same sequence of plays
    shuffles and draws the same way every time, which is the only reason a report of "the
    deck dealt me nothing for six turns" can be looked into after the fact.
    """
    step = (int(state.get("rng", 0) or 0) * 6364136223846793005 + 1442695040888963407)
    step %= 1 << 64
    state["rng"] = step
    return random.Random(step)


def _fighter(row: dict) -> dict:
    max_hp = max(1, int(row.get("max_hp", 1) or 1))
    hp = int(row.get("hp", max_hp) or max_hp)
    return {
        "id": str(row.get("id") or ""),
        "name": str(row.get("name") or "Существо"),
        "hp": max(1, min(max_hp, hp)),
        "max_hp": max_hp,
        "block": 0,
        "attack": max(1, int(row.get("attack", 1) or 1)),
        "status": {key: 0 for key in STATUS_KEYS},
    }


def _public_fighter(row: dict) -> dict:
    status = dict(row.get("status") or {})
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "hp": int(row.get("hp", 0) or 0),
        "max_hp": int(row.get("max_hp", 1) or 1),
        "block": int(row.get("block", 0) or 0),
        "status": {key: int(value) for key, value in status.items() if int(value or 0)},
    }


def _public_card(card: dict) -> dict:
    row = dict(card or {})
    row["tags"] = tags(row)
    return row


def _with_uids(cards: list[dict], prefix: str) -> list[dict]:
    """A stable per-copy identity. Four Strikes are four different cards to the client,
    which is what lets it say which one was tapped without matching on the name."""
    rows = []
    for index, card in enumerate(cards or ()):
        row = dict(card)
        row["uid"] = f"{prefix}{index}"
        rows.append(row)
    return rows


def _draw(state: dict, side: str, amount: int, rng: random.Random) -> None:
    draw_key = f"{side}_draw"
    discard_key = f"{side}_discard"
    hand_key = "hand" if side == "player" else "enemy_hand"
    hand = state.setdefault(hand_key, [])
    for _ in range(max(0, int(amount))):
        if len(hand) >= MAX_HAND:
            if side == "player":
                state["log"].append("Рука полна — карта сгорела.")
            break
        if not state.get(draw_key):
            discard = list(state.get(discard_key) or [])
            if not discard:
                break                    # draw pile and discard both empty: nothing left
            rng.shuffle(discard)
            state[draw_key] = discard
            state[discard_key] = []
            if side == "player":
                state["log"].append("Сброс перетасован обратно в колоду.")
        hand.append(state[draw_key].pop(0))


def _pick_plan(state: dict, rng: random.Random) -> list[dict]:
    """The opponent's whole next turn, chosen now so all of it can be shown now.

    The opponent gets the same hand and the same three energy the player does, and spends
    them here rather than on its own turn: without that it would answer a three-card turn
    with one card and lose every duel on tempo alone.

    Which cards it picks is random -- this is the first version of the mode and the intent
    panel is the feature, not the AI. The one thing the picker is careful about is energy:
    a card that hands energy back extends the same budget it would on the player's board,
    and a card that draws does its drawing here, so the plan on screen is exactly the turn
    that will be played and never a turn the opponent could not afford.
    """
    state.setdefault("enemy_discard", []).extend(state.get("enemy_hand") or ())
    state["enemy_hand"] = []
    _draw(state, "enemy", HAND_SIZE, rng)

    budget = int(state.get("max_energy", ENERGY_PER_TURN) or ENERGY_PER_TURN)
    plan: list[dict] = []
    while len(plan) < MAX_HAND:
        hand = list(state.get("enemy_hand") or ())
        affordable = [row for row in hand if int(row.get("cost", 0) or 0) <= budget]
        if not affordable:
            break
        card = dict(rng.choice(affordable))
        state["enemy_hand"] = [row for row in hand if row.get("uid") != card.get("uid")]
        budget -= max(0, int(card.get("cost", 0) or 0))
        budget += int(card.get("energy", 0) or 0)
        draw = max(0, int(card.get("draw", 0) or 0))
        if draw:
            _draw(state, "enemy", draw, rng)
        if card.get("exhaust"):
            state.setdefault("enemy_exhaust", []).append(card)
        else:
            state.setdefault("enemy_discard", []).append(card)
        plan.append(card)
    return plan


def _resolve(state: dict, source: str, target: str, card: dict,
             rng: random.Random) -> None:
    actor = state["fighters"][source]
    defender = state["fighters"][target]
    name = str(card.get("name") or "Карта")

    hits = max(1, int(card.get("hits", 1) or 1))
    base = max(0, int(card.get("damage", 0) or 0))
    total = 0
    for _ in range(hits if base else 0):
        total += _strike(state, actor, defender, base, card)
        if int(defender.get("hp", 0) or 0) <= 0:
            break
    if base:
        suffix = f" ×{hits}" if hits > 1 else ""
        state["log"].append(f"{actor['name']}: {name}{suffix} — {total} урона.")

    lifesteal = max(0, int(card.get("lifesteal", 0) or 0))
    if lifesteal and total:
        healed = _heal(actor, round(total * lifesteal / 100))
        if healed:
            state["log"].append(f"{actor['name']}: {name} возвращает {healed} HP.")

    block = max(0, int(card.get("block", 0) or 0))
    if block:
        actor["block"] = max(0, int(actor.get("block", 0) or 0)) + block
        state["log"].append(f"{actor['name']}: {name} даёт {block} блока.")

    heal = max(0, int(card.get("heal", 0) or 0))
    if heal:
        state["log"].append(f"{actor['name']}: {name} лечит {_heal(actor, heal)} HP.")

    for key, value in (card.get("apply") or {}).items():
        if key not in STATUS_META or not int(value or 0):
            continue
        defender["status"][key] = int(defender["status"].get(key, 0) or 0) + int(value)
        meta = STATUS_META[key]
        state["log"].append(
            f"{defender['name']}: {meta['icon']} {meta['name']} +{int(value)}."
        )

    for key, value in (card.get("boost") or {}).items():
        if key not in STATUS_META or not int(value or 0):
            continue
        actor["status"][key] = int(actor["status"].get(key, 0) or 0) + int(value)
        meta = STATUS_META[key]
        state["log"].append(
            f"{actor['name']}: {meta['icon']} {meta['name']} +{int(value)}."
        )

    self_damage = max(0, int(card.get("self_damage", 0) or 0))
    if self_damage:
        actor["hp"] = max(0, int(actor.get("hp", 0) or 0) - self_damage)
        state["log"].append(f"{actor['name']}: {name} забирает {self_damage} своих HP.")

    # Energy and drawing are the player's board only. The opponent plays one announced
    # card a turn by design, so an intent that "draws two" would draw into a hand nobody
    # ever sees and quietly change what the next announced intent can be.
    if source == "player":
        energy = int(card.get("energy", 0) or 0)
        if energy:
            state["energy"] = max(0, int(state.get("energy", 0) or 0) + energy)
            state["log"].append(f"{name}: энергия {energy:+d}.")
        draw = max(0, int(card.get("draw", 0) or 0))
        if draw:
            before = len(state.get("hand") or ())
            _draw(state, "player", draw, rng)
            state["log"].append(
                f"{name}: взято карт — {len(state.get('hand') or ()) - before}."
            )


def _strike(state: dict, actor: dict, defender: dict, amount: int, card: dict) -> int:
    """One hit of a possibly multi-hit card. Returns the HP actually removed."""
    damage = amount + int(actor["status"].get("strength", 0) or 0)
    if int(actor["status"].get("weak", 0) or 0) > 0:
        damage = round(damage * (1 - WEAK_PENALTY))
    if int(defender["status"].get("vulnerable", 0) or 0) > 0:
        damage = round(damage * (1 + VULNERABLE_BONUS))
    execute = max(0, int(card.get("execute", 0) or 0))
    if execute and int(defender.get("hp", 0) or 0) * 100 < int(defender.get("max_hp", 1) or 1) * 40:
        damage = round(damage * (1 + execute / 100))
    damage = max(0, int(damage))

    if not card.get("pierce"):
        absorbed = min(int(defender.get("block", 0) or 0), damage)
        defender["block"] = int(defender.get("block", 0) or 0) - absorbed
        damage -= absorbed
    dealt = min(damage, int(defender.get("hp", 0) or 0))
    defender["hp"] = int(defender.get("hp", 0) or 0) - dealt

    thorns = int(defender["status"].get("thorns", 0) or 0)
    if thorns and dealt:
        actor["hp"] = max(0, int(actor.get("hp", 0) or 0) - thorns)
        state["log"].append(f"{defender['name']}: 🌵 Шипы возвращают {thorns} урона.")
    return dealt


def _heal(fighter: dict, amount: int) -> int:
    before = int(fighter.get("hp", 0) or 0)
    fighter["hp"] = min(int(fighter.get("max_hp", 1) or 1), before + max(0, int(amount)))
    return int(fighter["hp"]) - before


def _tick(state: dict, side: str) -> None:
    """The end of one side's own turn: burn, poison and regen all fire here, so a status
    put on somebody costs them on THEIR turn and can be seen coming a turn ahead."""
    fighter = state["fighters"][side]
    status = fighter["status"]
    for key in ("burn", "poison"):
        amount = int(status.get(key, 0) or 0)
        if amount <= 0:
            continue
        dealt = min(amount, int(fighter.get("hp", 0) or 0))
        fighter["hp"] = int(fighter.get("hp", 0) or 0) - dealt
        meta = STATUS_META[key]
        state["log"].append(
            f"{fighter['name']}: {meta['icon']} {meta['name']} — {dealt} урона."
        )
    regen = int(status.get("regen", 0) or 0)
    if regen > 0:
        healed = _heal(fighter, regen)
        if healed:
            state["log"].append(f"{fighter['name']}: 🌿 Регенерация — +{healed} HP.")


def _decay(state: dict, side: str) -> None:
    status = state["fighters"][side]["status"]
    for key, meta in STATUS_META.items():
        if not meta.get("decays"):
            continue
        value = int(status.get(key, 0) or 0)
        if value > 0:
            status[key] = value - 1


def _check_finished(state: dict) -> None:
    player_hp = int(state["fighters"]["player"].get("hp", 0) or 0)
    enemy_hp = int(state["fighters"]["enemy"].get("hp", 0) or 0)
    if player_hp > 0 and enemy_hp > 0:
        return
    state["finished"] = True
    if player_hp <= 0 and enemy_hp <= 0:
        state["draw"] = True
        state["winner"] = None
        state["log"].append("Оба падают одновременно. Ничья.")
    elif enemy_hp <= 0:
        state["winner"] = "player"
        state["log"].append("Соперник повержен.")
    else:
        state["winner"] = "enemy"
        state["log"].append("Твоё существо больше не держится на лапах.")


def _assert_live(state: dict) -> None:
    if state.get("finished"):
        raise ValueError("Карточный бой уже закончен.")
