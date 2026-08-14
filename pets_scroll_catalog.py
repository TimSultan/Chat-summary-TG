"""Editable scroll and shield tables shared by live and test combat.

Scrolls are permanent abilities rather than inventory items; shields are converted into
ordinary live items by ``pets_shield_catalog``. Designers can tune the dictionaries
below without touching either turn engine: every numeric combat property lives here,
while the engines only implement the small vocabulary listed in ``EFFECT_OPS``.
"""

from __future__ import annotations


EFFECT_OPS = frozenset({
    "damage", "heal", "shield", "burn", "weaken", "blind", "vulnerable",
    "stun", "dodge_next", "reflect_next", "cleanse", "break_shield",
    "regen", "damage_boost", "negative_ward", "self_damage",
    # Reactive shield hooks.  Unlike the operations above, these live in a shield's
    # ``on_hit_effects`` and resolve when its wearer actually loses HP.
    "parry_stun", "damage_heal", "counterattack",
})

# These are presentation-only for now.  Combat deliberately does not read an element:
# the future elemental affinities will be introduced without having to migrate scrolls.
ELEMENTS = {
    "fire": ("🔥", "Огненный"),
    "frost": ("❄️", "Морозный"),
    "water": ("💧", "Водный"),
    "earth": ("🪨", "Земля"),
    "plants": ("🌿", "Растения"),
    "air": ("💨", "Воздушный"),
}


def _scroll(
        code: str, name: str, short: str, *, category: str = "magic",
        element: str, dodgeable: bool = True, ultimate: bool = False,
    effects: tuple[dict, ...], icon: str = "✨",
) -> dict:
    return {
        "code": code, "name": name, "short": short, "category": category,
        "element": element,
        "uses": 1, "dodgeable": dodgeable, "ultimate": ultimate,
        "auto_weight": 1,
        "effects": tuple(dict(effect) for effect in effects), "icon": icon,
    }


def element_label(element: str) -> str:
    """Russian player-facing name for the immutable elemental catalogue code."""
    icon, name = ELEMENTS[str(element)]
    return f"{icon} {name}"


# Every equipped scroll is a once-per-fight power. Regular scrolls need to justify giving
# up an attack, so each deals roughly attack-level damage or brings an immediate second
# benefit; the fourth-slot ultimate is the fight's largest single swing.
REGULAR_SCROLLS = (
    _scroll("scroll_arcane_spark", "Магический свиток: Искра эфира",
            "Точный разряд чистой магии.", element="air",
            effects=({"op": "damage", "amount": 1.45},), icon="⚡"),
    _scroll("scroll_crimson_comet", "Магический свиток: Багровая комета",
            "Тяжёлый удар и два тика пламени.", element="fire",
            effects=({"op": "damage", "amount": 1.20},
                     {"op": "burn", "amount": .45, "turns": 2}), icon="☄️"),
    _scroll("scroll_chain_lightning", "Магический свиток: Цепная молния",
            "Надёжный разряд, который всегда находит цель.", element="air",
            dodgeable=False, effects=({"op": "damage", "amount": 1.25},), icon="🌩"),
    _scroll("scroll_frost_seal", "Магический свиток: Ледяная печать",
            "Ранит и сильно ослабляет следующий удар врага.", element="frost",
            effects=({"op": "damage", "amount": 1.05},
                     {"op": "weaken", "value": .40, "turns": 1}), icon="❄️"),
    _scroll("scroll_gravity_thread", "Магический свиток: Нить гравитации",
            "Бьёт и пришивает врага к земле, лишая хода.", element="earth",
            effects=({"op": "damage", "amount": .80}, {"op": "stun", "turns": 1}), icon="🪐"),
    _scroll("scroll_twilight_needle", "Магический свиток: Игла сумрака",
            "Прокалывает большую часть защиты.", element="frost",
            effects=({"op": "damage", "amount": 1.25, "pierce_guard": .65,
                      "pierce_armor": .50},), icon="🌒"),
    _scroll("scroll_royal_barrier", "Магический свиток: Королевский барьер",
            "Создаёт прочный щит и усиливает следующий удар.", element="frost", dodgeable=False,
            effects=({"op": "shield", "percent": .22},
                     {"op": "damage_boost", "value": .25, "turns": 1}), icon="👑"),
    _scroll("scroll_healing_rain", "Магический свиток: Лечебный дождь",
            "Сильно лечит и оставляет защитный барьер.", element="water", dodgeable=False,
            effects=({"op": "heal", "percent": .24}, {"op": "shield", "percent": .10}), icon="🌧"),
    _scroll("scroll_astral_step", "Магический свиток: Астральный шаг",
            "Гарантирует уклонение от следующего обычного удара или заклинания.",
            element="air", dodgeable=False, effects=({"op": "dodge_next"},
                                                       {"op": "damage_boost", "value": .35, "turns": 1}), icon="🌌"),
    _scroll("scroll_misprint_curse", "Магический свиток: Проклятие опечатки",
            "Враг путает буквы и сильно чаще промахивается.",
            element="plants", dodgeable=False, effects=({"op": "blind", "value": .45, "turns": 2},), icon="📜"),
    _scroll("scroll_dispersal_ray", "Магический свиток: Луч рассеивания",
            "Срывает барьер перед мощным точным разрядом.",
            element="fire", effects=({"op": "break_shield"}, {"op": "damage", "amount": 1.15,
                                        "pierce_armor": .35}), icon="🔆"),
    _scroll("scroll_mirror_shard", "Магический свиток: Осколок зеркала",
            "Отражает следующий удар и ставит небольшой барьер.", element="frost", dodgeable=False,
            effects=({"op": "reflect_next", "value": .75}, {"op": "shield", "percent": .08}), icon="🪞"),
    _scroll("scroll_time_sand", "Магический свиток: Песок времени",
            "Сильно лечит, очищает проклятия и усиливает ответ.", element="earth", dodgeable=False,
            effects=({"op": "heal", "percent": .18}, {"op": "cleanse"},
                     {"op": "damage_boost", "value": .20, "turns": 1}), icon="⌛"),
    _scroll("scroll_rune_mark", "Магический свиток: Руна мишени",
            "Ранит цель и делает её уязвимой для следующих ударов.",
            element="earth", dodgeable=False, effects=({"op": "damage", "amount": .70},
                                                         {"op": "vulnerable", "value": .40, "turns": 2}), icon="🎯"),
    _scroll("scroll_poltergeist_push", "Магический свиток: Толчок полтергейста",
            "Простой и надёжный телекинетический удар.", element="air",
            effects=({"op": "damage", "amount": 1.35},), icon="👻"),
    _scroll("scroll_nmm_glint", "Магический свиток: Блик NMM",
            "Идеальный белый блик полностью игнорирует защиту.", element="air",
            effects=({"op": "damage", "amount": 1.30, "pierce_guard": 1.0,
                      "pierce_armor": 1.0},), icon="🖌"),
    _scroll("scroll_enamel_varnish", "Магический свиток: Лаковый панцирь",
            "Толстый глянцевый слой принимает урон и усиливает ответ.", element="earth", dodgeable=False,
            effects=({"op": "shield", "percent": .28},
                     {"op": "damage_boost", "value": .20, "turns": 1}), icon="🫧"),
    _scroll("scroll_ink_wash", "Магический свиток: Чернильная проливка",
            "Ранит, затекает в слабые места и делает цель уязвимой.",
            element="water", dodgeable=False, effects=({"op": "damage", "amount": .65},
                                                         {"op": "vulnerable", "value": .35, "turns": 2}), icon="🖤"),
    _scroll("scroll_drybrush", "Свиток умения: Сухая кисть",
            "Быстрый удар готовит две усиленные атаки.", category="skill",
            element="fire", effects=({"op": "damage", "amount": 1.00},
                     {"op": "damage_boost", "value": .35, "turns": 2}), icon="🖌"),
    _scroll("scroll_pigment_fog", "Магический свиток: Пигментный туман",
            "Цветная пыль ранит и мешает врагу прицелиться.", element="plants", dodgeable=False,
            effects=({"op": "damage", "amount": .65}, {"op": "blind", "value": .45, "turns": 2}), icon="🌫"),
    _scroll("scroll_masking_tape", "Свиток умения: Малярная лента",
            "Не пропускает негативный эффект и ставит барьер.", category="skill",
            element="earth", dodgeable=False, effects=({"op": "negative_ward"},
                                                         {"op": "shield", "percent": .16}), icon="🟨"),
    _scroll("scroll_wet_palette", "Свиток умения: Влажная палитра",
            "Сразу лечит и продолжает восстанавливать здоровье.", category="skill",
            element="water", dodgeable=False, effects=({"op": "heal", "percent": .12},
                                                         {"op": "regen", "percent": .09, "turns": 2}), icon="🎨"),
    _scroll("scroll_solvent_splash", "Магический свиток: Растворитель",
            "Растворяет барьер, ранит и открывает слабые места.", element="water",
            effects=({"op": "break_shield"},
                     {"op": "damage", "amount": .80, "pierce_armor": .25},
                     {"op": "vulnerable", "value": .30, "turns": 2}), icon="🧪"),
    _scroll("scroll_predator_bite", "Свиток умения: Хищный укус",
            "Тяжёлый укус лечит владельца на большую часть нанесённого урона.", category="skill",
            element="plants", effects=({"op": "damage", "amount": 1.30, "lifesteal": .70},), icon="🦷"),
    _scroll("scroll_thorn_cocoon", "Свиток умения: Шипастый кокон",
            "Возвращает весь следующий урон и ставит барьер.", category="skill",
            element="plants", dodgeable=False, effects=({"op": "reflect_next", "value": 1.0},
                                                          {"op": "shield", "percent": .12}), icon="🌹"),
    _scroll("scroll_iron_stance", "Свиток умения: Железная стойка",
            "Сильно уменьшает следующий входящий урон и готовит ответ.", category="skill",
            element="earth", dodgeable=False, effects=({"op": "shield", "percent": .20},
                                                         {"op": "damage_boost", "value": .30, "turns": 1}), icon="🗿"),
    _scroll("scroll_headlong_rush", "Свиток умения: Лобовая атака",
            "Рискованный, но сокрушительный удар.", category="skill",
            element="earth", effects=({"op": "damage", "amount": 1.75},), icon="🐏"),
    _scroll("scroll_field_bandage", "Свиток умения: Полевая перевязка",
            "Большое мгновенное лечение и защита от следующего удара.", category="skill", dodgeable=False,
            element="plants", effects=({"op": "heal", "percent": .30}, {"op": "shield", "percent": .10}), icon="🩹"),
    _scroll("scroll_feint", "Свиток умения: Финт",
            "Уклонение от следующего удара и сильное усиление ответа.", category="skill",
            element="air", dodgeable=False, effects=({"op": "dodge_next"},
                                     {"op": "damage_boost", "value": .35, "turns": 2}), icon="🤺"),
    _scroll("scroll_grappling_hook", "Свиток умения: Крюк",
            "Подтягивает врага тяжёлым ударом и оставляет его открытым.", category="skill",
            element="air", effects=({"op": "damage", "amount": 1.10},
                     {"op": "vulnerable", "value": .40, "turns": 2}), icon="🪝"),
)


# Slot four accepts only these and the engine spends each exactly once.
ULTIMATE_SCROLLS = (
    _scroll("ultimate_starfall", "Магический свиток: Звездопад", "Небо падает на одну цель.",
            element="fire", ultimate=True, effects=({"op": "damage", "amount": 2.35},), icon="🌠"),
    _scroll("ultimate_dragon_breath", "Магический свиток: Дыхание дракона",
            "Пламя продолжает гореть три хода.", element="fire", ultimate=True,
            effects=({"op": "damage", "amount": 1.25},
                     {"op": "burn", "amount": .55, "turns": 3}), icon="🐉"),
    _scroll("ultimate_masterpiece", "Магический свиток: Шедевр",
            "Безупречный удар оставляет врага открытым.", element="plants", ultimate=True,
            effects=({"op": "damage", "amount": 1.50},
                     {"op": "vulnerable", "value": .40, "turns": 2}), icon="🖼"),
    _scroll("ultimate_time_reversal", "Магический свиток: Обратный ход",
            "Возвращает здоровье и отменяет проклятия.", element="water", ultimate=True,
            dodgeable=False, effects=({"op": "heal", "percent": .32}, {"op": "cleanse"}), icon="⏪"),
    _scroll("ultimate_glass_citadel", "Магический свиток: Стеклянная цитадель",
            "Огромный барьер отражает следующий удар.", element="frost", ultimate=True,
            dodgeable=False, effects=({"op": "shield", "percent": .35},
                                     {"op": "reflect_next", "value": .50}), icon="🏰"),
    _scroll("ultimate_kraken", "Свиток умения: Кракен из ведра",
            "Хватается за врага и лишает его хода.", category="skill", ultimate=True,
            element="water", effects=({"op": "damage", "amount": .90},
                                 {"op": "stun", "turns": 1}), icon="🐙"),
    _scroll("ultimate_final_gambit", "Свиток умения: Последняя ставка",
            "Сокрушительный удар ценой собственного здоровья.", category="skill",
            element="fire", ultimate=True, effects=({"op": "damage", "amount": 2.55},
                                               {"op": "self_damage", "percent": .12}), icon="🎲"),
    _scroll("ultimate_rainbow_flood", "Магический свиток: Радужный потоп",
            "Магическая волна одновременно ранит и лечит.", element="water", ultimate=True,
            effects=({"op": "damage", "amount": 1.75},
                     {"op": "heal", "percent": .15}), icon="🌈"),
    _scroll("ultimate_blackout", "Магический свиток: Затмение",
            "Густая тьма ранит и ослепляет.", element="frost", ultimate=True,
            dodgeable=False, effects=({"op": "damage", "amount": 1.10},
                                      {"op": "blind", "value": .45, "turns": 2}), icon="🌑"),
    _scroll("ultimate_golden_frame", "Магический свиток: Золотая рама",
            "Заключает героя в сияющую защиту и усиливает урон.", element="fire", ultimate=True,
            dodgeable=False, effects=({"op": "shield", "percent": .20},
                                                 {"op": "damage_boost", "value": .35,
                                                  "turns": 2}), icon="🖼"),
)


SHIELDS = (
    {"code": "shield_paper_buckler", "name": "Картонный баклер", "icon": "📦",
     "short": "При защите создаёт барьер 8% HP.",
     "defend_effects": ({"op": "shield", "percent": .08},)},
    {"code": "shield_mirror", "name": "Зеркальный щит", "icon": "🪞",
     "short": "Отражает 35% следующего урона.",
     "defend_effects": ({"op": "reflect_next", "value": .35},)},
    {"code": "shield_ink_lid", "name": "Крышка чернильницы", "icon": "⚫",
     "short": "После защиты ослепляет врага на ход.",
     "defend_effects": ({"op": "blind", "value": .20, "turns": 1},)},
    {"code": "shield_thorn", "name": "Щит шиповника", "icon": "🌹",
     "short": "Отражает 55% следующего урона.",
     "defend_effects": ({"op": "reflect_next", "value": .55},)},
    {"code": "shield_frost", "name": "Ледяной щит", "icon": "🧊",
     "short": "Ослабляет следующий удар врага на 30%.",
     "defend_effects": ({"op": "weaken", "value": .30, "turns": 1},)},
    {"code": "shield_palette", "name": "Палитра-хамелеон", "icon": "🎨",
     "short": "При защите очищает негативные эффекты.",
     "defend_effects": ({"op": "cleanse"},)},
    {"code": "shield_lantern", "name": "Фонарный щит", "icon": "🏮",
     "short": "При защите лечит 6% HP.",
     "defend_effects": ({"op": "heal", "percent": .06},)},
    {"code": "shield_kite", "name": "Воздушный щит", "icon": "🪁",
     "short": "Даёт гарантированное уклонение от следующей уворотной атаки.",
     "defend_effects": ({"op": "dodge_next"},)},
    {"code": "shield_tower", "name": "Башенный щит", "icon": "🧱",
     "short": "Базовая защита поглощает 60% вместо 40%.", "guard": .60,
     "defend_effects": ()},
    {"code": "shield_rune", "name": "Рунный щит", "icon": "🔷",
     "short": "При защите создаёт барьер 12% HP.",
     "defend_effects": ({"op": "shield", "percent": .12},)},
    {"code": "shield_clothespin", "name": "Бельевая прищепка", "icon": "🗜️",
     "short": "При защите усиливает свой следующий удар на 25%.",
     "defend_effects": ({"op": "damage_boost", "value": .25, "turns": 1},)},
    {"code": "shield_forge_clamp", "name": "Кузнечный зажим", "icon": "⚙️",
     "short": "При защите усиливает свои следующие два удара на 35%.",
     "defend_effects": ({"op": "damage_boost", "value": .35, "turns": 2},)},
    {"code": "shield_solvent_jar", "name": "Баночка растворителя", "icon": "🧪",
     "short": "При защите поджигает врага: 35% урона за ход, 2 хода.",
     "defend_effects": ({"op": "burn", "amount": .35, "turns": 2},)},
    {"code": "shield_solvent_drum", "name": "Бочка растворителя", "icon": "🛢️",
     "short": "При защите поджигает врага: 45% урона за ход, 3 хода.",
     "defend_effects": ({"op": "burn", "amount": .45, "turns": 3},)},
    {"code": "shield_duelist_buckler", "name": "Баклер дуэлянта", "icon": "🤺",
     "short": "При первом полученном уроне: 28% шанс парировать 65% урона и оглушить атакующего.",
     "defend_effects": (),
     "on_hit_effects": ({"op": "parry_stun", "chance": .28, "reduce": .65},)},
    {"code": "shield_medic_emblem", "name": "Щит полевого медика", "icon": "⚕️",
     "short": "После каждого полученного урона восстанавливает 30% фактически потерянного HP.",
     "defend_effects": (),
     "on_hit_effects": ({"op": "damage_heal", "percent": .30},)},
    {"code": "shield_spiked_targe", "name": "Шипованный тарч", "icon": "🦔",
     "short": "После первого полученного урона отвечает контрударом на 45% обычного урона.",
     "defend_effects": (),
     "on_hit_effects": ({"op": "counterattack", "percent": .45},)},
    {"code": "shield_royal_riposte", "name": "Королевский рипост", "icon": "👑",
     "short": "При первом полученном уроне: 45% шанс парировать 80% урона и оглушить атакующего.",
     "defend_effects": (),
     "on_hit_effects": ({"op": "parry_stun", "chance": .45, "reduce": .80},)},
    {"code": "shield_crimson_reliquary", "name": "Багровый реликварий", "icon": "🩸",
     "short": "После каждого полученного урона восстанавливает 50% фактически потерянного HP.",
     "defend_effects": (),
     "on_hit_effects": ({"op": "damage_heal", "percent": .50},)},
    {"code": "shield_judgement", "name": "Щит воздаяния", "icon": "⚖️",
     "short": "После первого полученного урона отвечает контрударом на 85% обычного урона.",
     "defend_effects": (),
     "on_hit_effects": ({"op": "counterattack", "percent": .85},)},
)


SCROLLS = REGULAR_SCROLLS + ULTIMATE_SCROLLS
SCROLL_BY_CODE = {row["code"]: row for row in SCROLLS}
SHIELD_BY_CODE = {row["code"]: row for row in SHIELDS}
# Four slots, all empty. A creature owns its slots the way it owns its equipment slots:
# they exist from the moment it is tamed, and an empty one simply has nothing in it.
# There is deliberately no starter set -- every one of the forty scrolls is earned.
EMPTY_LOADOUT = (None, None, None, None)
# Only for the designer sandbox in pets_test_combat, which needs something equipped to
# demonstrate a turn. Never granted to a player and never written to a save.
SAMPLE_LOADOUT = (
    "scroll_arcane_spark", "scroll_healing_rain", "scroll_nmm_glint",
    "ultimate_starfall",
)
DEFAULT_SHIELD = "shield_paper_buckler"


def scroll(code: str) -> dict | None:
    return SCROLL_BY_CODE.get(str(code or ""))


def shield(code: str) -> dict | None:
    return SHIELD_BY_CODE.get(str(code or ""))


def validate_loadout(codes) -> tuple:
    """Four slots, each holding a scroll or nothing. Returns codes with None for empty.

    Always four entries, never fewer: the slots are the creature's, not the scrolls'.
    An empty slot is a legal resting state rather than an error, so a player who owns
    two scrolls equips two and leaves the rest open -- exactly how an empty weapon or
    amulet slot behaves. What stays enforced is what a filled slot may hold: ordinary
    scrolls in the first three, an ultimate in the fourth, and never the same scroll
    twice, since a scroll is a once-per-fight power and a second copy would be dead.
    """
    values = tuple(codes or ())
    if len(values) != 4:
        raise ValueError("У существа четыре слота под свитки.")
    slots = []
    for index, raw in enumerate(values):
        code = str(raw or "")
        if not code:
            slots.append(None)
            continue
        row = scroll(code)
        if row is None:
            raise ValueError("В наборе есть неизвестный свиток.")
        if bool(row["ultimate"]) != (index == 3):
            raise ValueError("Первые три слота — обычные свитки, четвёртый — ультимейт.")
        slots.append(code)
    filled = [code for code in slots if code]
    if len(set(filled)) != len(filled):
        raise ValueError("Один свиток нельзя поставить сразу в два слота.")
    return tuple(slots)


def equipped_codes(loadout) -> tuple[str, ...]:
    """Just the scrolls actually in the slots, in slot order. Empty slots drop out."""
    return tuple(code for code in (loadout or ()) if code)


# Blind is the one number a player would read as a lie: the turn engine caps a forced
# miss at 80% however large the catalogue value grows (pets_combat, forced_skill_miss).
BLIND_CAP = .80


def _percent(value) -> str:
    return f"{round(float(value or 0) * 100)}%"


def _turns(effect) -> str:
    """`2 хода`. Scroll durations are 1-3, so the third Russian plural form never shows."""
    turns = max(1, int(effect.get("turns", 1) or 1))
    if turns == 1:
        return "1 ход"
    return f"{turns} хода" if turns < 5 else f"{turns} ходов"


def effect_text(effect: dict) -> str:
    """One player-facing sentence for one combat effect, numbers included.

    The single place the effect tables are put into words: Telegram's scroll screen and
    the Mini App both render this, so a tuned number cannot start meaning two different
    things in two clients. Wording follows what the engine actually does with the field
    -- `amount` multiplies a normal hit, `percent` is a share of max health, `value` is a
    status strength -- because a player comparing two scrolls is really comparing these.
    """
    op = str(effect.get("op") or "")
    if op == "damage":
        parts = [f"Урон {_percent(effect.get('amount', 1.0))} от обычного удара"]
        if float(effect.get("pierce_armor") or 0) > 0:
            parts.append(f"игнорирует {_percent(effect['pierce_armor'])} брони")
        if float(effect.get("pierce_guard") or 0) > 0:
            parts.append(f"пробивает {_percent(effect['pierce_guard'])} блока")
        if float(effect.get("lifesteal") or 0) > 0:
            parts.append(f"восстанавливает {_percent(effect['lifesteal'])} нанесённого урона")
        return ", ".join(parts)
    if op == "heal":
        return f"Восстанавливает {_percent(effect.get('percent'))} здоровья"
    if op == "shield":
        return f"Щит на {_percent(effect.get('percent'))} здоровья"
    if op == "burn":
        return f"Поджигает: {_percent(effect.get('amount'))} удара за ход, {_turns(effect)}"
    if op == "weaken":
        return f"Враг бьёт слабее на {_percent(effect.get('value'))}, {_turns(effect)}"
    if op == "blind":
        chance = min(BLIND_CAP, float(effect.get("value") or 0))
        return f"Враг промахивается с шансом {_percent(chance)}, {_turns(effect)}"
    if op == "vulnerable":
        return f"Враг получает на {_percent(effect.get('value'))} больше урона, {_turns(effect)}"
    if op == "stun":
        return "Враг пропускает ход"
    if op == "dodge_next":
        return "Уклоняется от следующего удара"
    if op == "reflect_next":
        return f"Отражает {_percent(effect.get('value'))} следующего удара"
    if op == "cleanse":
        return "Снимает с себя все негативные эффекты"
    if op == "break_shield":
        return "Разбивает щит врага"
    if op == "regen":
        return f"Восстанавливает {_percent(effect.get('percent'))} здоровья за ход, {_turns(effect)}"
    if op == "damage_boost":
        return f"Свой урон выше на {_percent(effect.get('value'))}, {_turns(effect)}"
    if op == "negative_ward":
        return "Поглощает следующий негативный эффект"
    if op == "self_damage":
        return f"Забирает {_percent(effect.get('percent'))} своего здоровья"
    if op == "parry_stun":
        return (
            f"{_percent(effect.get('chance'))} шанс парировать "
            f"{_percent(effect.get('reduce'))} полученного урона и оглушить атакующего"
        )
    if op == "damage_heal":
        return f"Восстанавливает {_percent(effect.get('percent'))} фактически потерянного здоровья"
    if op == "counterattack":
        return f"Контрудар на {_percent(effect.get('percent'))} обычного урона"
    return ""


def effect_lines(row: dict) -> tuple[str, ...]:
    """Every effect of one scroll or shield, in the order the engine applies them."""
    effects = row.get("effects")
    if effects is None:
        effects = tuple(row.get("defend_effects", ())) + tuple(row.get("on_hit_effects", ()))
    lines = [effect_text(effect) for effect in effects]
    return tuple(line for line in lines if line)


def public_scroll(row: dict) -> dict:
    return {
        **row,
        "effects": [dict(effect) for effect in row["effects"]],
        "effects_text": list(effect_lines(row)),
    }


def public_shield(row: dict) -> dict:
    return {
        **row,
        "defend_effects": [dict(effect) for effect in row.get("defend_effects", ())],
        "on_hit_effects": [dict(effect) for effect in row.get("on_hit_effects", ())],
        "effects_text": list(effect_lines(row)),
    }


def _validate() -> None:
    codes = [row["code"] for row in SCROLLS]
    if len(codes) != 40 or len(set(codes)) != len(codes):
        raise ValueError("scroll catalogue must contain 40 unique entries")
    for row in SCROLLS:
        prefix = "Магический свиток:" if row["category"] == "magic" else "Свиток умения:"
        if not row["name"].startswith(prefix):
            raise ValueError(f"bad scroll name: {row['code']}")
        if row["uses"] != 1 or row["auto_weight"] != 1:
            raise ValueError(f"bad scroll timing: {row['code']}")
        if row.get("element") not in ELEMENTS:
            raise ValueError(f"unknown scroll element: {row['code']}")
        for effect in row["effects"]:
            if effect.get("op") not in EFFECT_OPS:
                raise ValueError(f"unknown scroll operation: {effect.get('op')}")
            # Both clients print effect_text and nothing else, so an op that grows a new
            # spelling here must not reach players as a silently blank line.
            if not effect_text(effect):
                raise ValueError(f"scroll effect has no wording: {row['code']} {effect.get('op')}")
        if not row["dodgeable"] and any(effect.get("op") == "stun" for effect in row["effects"]):
            raise ValueError(f"undodgeable stun is not allowed: {row['code']}")
    if {row["element"] for row in SCROLLS} != set(ELEMENTS):
        raise ValueError("every scroll element must be represented")
    shield_codes = [row["code"] for row in SHIELDS]
    if len(shield_codes) != 20 or len(set(shield_codes)) != len(shield_codes):
        raise ValueError("shield catalogue must contain 20 unique entries")
    for row in SHIELDS:
        effects = tuple(row.get("defend_effects", ())) + tuple(row.get("on_hit_effects", ()))
        for effect in effects:
            if effect.get("op") not in EFFECT_OPS or not effect_text(effect):
                raise ValueError(f"shield effect has no valid wording: {row['code']}")
    validate_loadout(SAMPLE_LOADOUT)
    # Four empty slots is the state every creature is tamed into, so it has to survive
    # the same validator every equip goes through, unchanged.
    if validate_loadout(EMPTY_LOADOUT) != EMPTY_LOADOUT:
        raise ValueError("an empty loadout must validate to four empty slots")


_validate()
