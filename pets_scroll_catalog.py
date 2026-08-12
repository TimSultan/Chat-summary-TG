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
    element: str, cooldown: int = 2, dodgeable: bool = True, ultimate: bool = False,
    effects: tuple[dict, ...], icon: str = "✨",
) -> dict:
    return {
        "code": code, "name": name, "short": short, "category": category,
        "element": element,
        "cooldown": cooldown, "dodgeable": dodgeable, "ultimate": ultimate,
        "uses": 1 if ultimate else None, "auto_weight": 1,
        "effects": tuple(dict(effect) for effect in effects), "icon": icon,
    }


def element_label(element: str) -> str:
    """Russian player-facing name for the immutable elemental catalogue code."""
    icon, name = ELEMENTS[str(element)]
    return f"{icon} {name}"


# Three ordinary slots draw from these.  A cooldown is counted in that fighter's actions:
# cooldown 2 means two other actions must be made before the scroll is ready again.
REGULAR_SCROLLS = (
    _scroll("scroll_arcane_spark", "Магический свиток: Искра эфира",
            "Точный разряд чистой магии.", element="air", cooldown=1,
            effects=({"op": "damage", "amount": 1.20},), icon="⚡"),
    _scroll("scroll_crimson_comet", "Магический свиток: Багровая комета",
            "Удар и два тика пламени.", element="fire", cooldown=3,
            effects=({"op": "damage", "amount": 1.05},
                     {"op": "burn", "amount": .35, "turns": 2}), icon="☄️"),
    _scroll("scroll_chain_lightning", "Магический свиток: Цепная молния",
            "Слабее обычной молнии, зато всегда находит цель.", element="air", cooldown=3,
            dodgeable=False, effects=({"op": "damage", "amount": .95},), icon="🌩"),
    _scroll("scroll_frost_seal", "Магический свиток: Ледяная печать",
            "Ранит и ослабляет следующий удар врага.", element="frost", cooldown=2,
            effects=({"op": "damage", "amount": .75},
                     {"op": "weaken", "value": .30, "turns": 1}), icon="❄️"),
    _scroll("scroll_gravity_thread", "Магический свиток: Нить гравитации",
            "Пришивает врага к земле и лишает хода.", element="earth", cooldown=4,
            effects=({"op": "stun", "turns": 1},), icon="🪐"),
    _scroll("scroll_twilight_needle", "Магический свиток: Игла сумрака",
            "Прокалывает большую часть защиты.", element="frost", cooldown=2,
            effects=({"op": "damage", "amount": 1.05, "pierce_guard": .50,
                      "pierce_armor": .35},), icon="🌒"),
    _scroll("scroll_royal_barrier", "Магический свиток: Королевский барьер",
            "Создаёт прочный магический щит.", element="frost", cooldown=3, dodgeable=False,
            effects=({"op": "shield", "percent": .13},), icon="👑"),
    _scroll("scroll_healing_rain", "Магический свиток: Лечебный дождь",
            "Возвращает часть максимального здоровья.", element="water", cooldown=3, dodgeable=False,
            effects=({"op": "heal", "percent": .12},), icon="🌧"),
    _scroll("scroll_astral_step", "Магический свиток: Астральный шаг",
            "Гарантирует уклонение от следующего обычного удара или заклинания.",
            element="air", cooldown=3, dodgeable=False, effects=({"op": "dodge_next"},), icon="🌌"),
    _scroll("scroll_misprint_curse", "Магический свиток: Проклятие опечатки",
            "Враг два хода путает буквы и чаще промахивается.", cooldown=3,
            element="plants", dodgeable=False, effects=({"op": "blind", "value": .30, "turns": 2},), icon="📜"),
    _scroll("scroll_dispersal_ray", "Магический свиток: Луч рассеивания",
            "Срывает барьер перед нанесением урона.", cooldown=3,
            element="fire", effects=({"op": "break_shield"}, {"op": "damage", "amount": .65}), icon="🔆"),
    _scroll("scroll_mirror_shard", "Магический свиток: Осколок зеркала",
            "Отражает половину следующего полученного урона.", element="frost", cooldown=3, dodgeable=False,
            effects=({"op": "reflect_next", "value": .50},), icon="🪞"),
    _scroll("scroll_time_sand", "Магический свиток: Песок времени",
            "Лечит и снимает один набор негативных эффектов.", element="earth", cooldown=3, dodgeable=False,
            effects=({"op": "heal", "percent": .08}, {"op": "cleanse"}), icon="⌛"),
    _scroll("scroll_rune_mark", "Магический свиток: Руна мишени",
            "Следующие удары по цели причиняют больше урона.", cooldown=2,
            element="earth", dodgeable=False, effects=({"op": "vulnerable", "value": .30, "turns": 1},), icon="🎯"),
    _scroll("scroll_poltergeist_push", "Магический свиток: Толчок полтергейста",
            "Простой и надёжный телекинетический удар.", element="air", cooldown=2,
            effects=({"op": "damage", "amount": 1.10},), icon="👻"),
    _scroll("scroll_nmm_glint", "Магический свиток: Блик NMM",
            "Идеальный белый блик полностью игнорирует защиту.", element="air", cooldown=3,
            effects=({"op": "damage", "amount": 1.00, "pierce_guard": 1.0,
                      "pierce_armor": 1.0},), icon="🖌"),
    _scroll("scroll_enamel_varnish", "Магический свиток: Лаковый панцирь",
            "Толстый глянцевый слой принимает урон на себя.", element="earth", cooldown=4, dodgeable=False,
            effects=({"op": "shield", "percent": .18},), icon="🫧"),
    _scroll("scroll_ink_wash", "Магический свиток: Чернильная проливка",
            "Затекает в слабые места и делает цель уязвимой.", cooldown=3,
            element="water", dodgeable=False, effects=({"op": "vulnerable", "value": .25, "turns": 2},), icon="🖤"),
    _scroll("scroll_drybrush", "Свиток умения: Сухая кисть",
            "Быстрый удар готовит усиленную следующую атаку.", category="skill", cooldown=2,
            element="fire", effects=({"op": "damage", "amount": .80},
                     {"op": "damage_boost", "value": .30, "turns": 2}), icon="🖌"),
    _scroll("scroll_pigment_fog", "Магический свиток: Пигментный туман",
            "Цветная пыль мешает врагу прицелиться.", element="plants", cooldown=3, dodgeable=False,
            effects=({"op": "blind", "value": .35, "turns": 1},), icon="🌫"),
    _scroll("scroll_masking_tape", "Свиток умения: Малярная лента",
            "Не пропускает следующий негативный эффект.", category="skill", cooldown=3,
            element="earth", dodgeable=False, effects=({"op": "negative_ward"},), icon="🟨"),
    _scroll("scroll_wet_palette", "Свиток умения: Влажная палитра",
            "Постепенно восстанавливает здоровье два хода.", category="skill", cooldown=3,
            element="water", dodgeable=False, effects=({"op": "regen", "percent": .06, "turns": 2},), icon="🎨"),
    _scroll("scroll_solvent_splash", "Магический свиток: Растворитель",
            "Растворяет барьер и открывает слабые места.", element="water", cooldown=4,
            effects=({"op": "break_shield"},
                     {"op": "vulnerable", "value": .25, "turns": 2}), icon="🧪"),
    _scroll("scroll_predator_bite", "Свиток умения: Хищный укус",
            "Лечит владельца на половину нанесённого урона.", category="skill", cooldown=3,
            element="plants", effects=({"op": "damage", "amount": 1.00, "lifesteal": .50},), icon="🦷"),
    _scroll("scroll_thorn_cocoon", "Свиток умения: Шипастый кокон",
            "Возвращает весь следующий полученный урон.", category="skill", cooldown=4,
            element="plants", dodgeable=False, effects=({"op": "reflect_next", "value": 1.0},), icon="🌹"),
    _scroll("scroll_iron_stance", "Свиток умения: Железная стойка",
            "Сильно уменьшает следующий входящий урон.", category="skill", cooldown=3,
            element="earth", dodgeable=False, effects=({"op": "shield", "percent": .11},), icon="🗿"),
    _scroll("scroll_headlong_rush", "Свиток умения: Лобовая атака",
            "Рискованный, но очень тяжёлый удар.", category="skill", cooldown=3,
            element="earth", effects=({"op": "damage", "amount": 1.45},), icon="🐏"),
    _scroll("scroll_field_bandage", "Свиток умения: Полевая перевязка",
            "Большое мгновенное лечение.", category="skill", cooldown=4, dodgeable=False,
            element="plants", effects=({"op": "heal", "percent": .17},), icon="🩹"),
    _scroll("scroll_feint", "Свиток умения: Финт",
            "Уклонение от следующего удара и усиление ответа.", category="skill", cooldown=3,
            element="air", dodgeable=False, effects=({"op": "dodge_next"},
                                     {"op": "damage_boost", "value": .20, "turns": 2}), icon="🤺"),
    _scroll("scroll_grappling_hook", "Свиток умения: Крюк",
            "Подтягивает врага и оставляет его открытым.", category="skill", cooldown=3,
            element="air", effects=({"op": "damage", "amount": .85},
                     {"op": "vulnerable", "value": .35, "turns": 2}), icon="🪝"),
)


# Slot four accepts only these and the engine spends each exactly once.
ULTIMATE_SCROLLS = (
    _scroll("ultimate_starfall", "Магический свиток: Звездопад", "Небо падает на одну цель.",
            element="fire", ultimate=True, cooldown=0, effects=({"op": "damage", "amount": 2.35},), icon="🌠"),
    _scroll("ultimate_dragon_breath", "Магический свиток: Дыхание дракона",
            "Пламя продолжает гореть три хода.", element="fire", ultimate=True, cooldown=0,
            effects=({"op": "damage", "amount": 1.25},
                     {"op": "burn", "amount": .55, "turns": 3}), icon="🐉"),
    _scroll("ultimate_masterpiece", "Магический свиток: Шедевр",
            "Безупречный удар оставляет врага открытым.", element="plants", ultimate=True, cooldown=0,
            effects=({"op": "damage", "amount": 1.50},
                     {"op": "vulnerable", "value": .40, "turns": 2}), icon="🖼"),
    _scroll("ultimate_time_reversal", "Магический свиток: Обратный ход",
            "Возвращает здоровье и отменяет проклятия.", element="water", ultimate=True, cooldown=0,
            dodgeable=False, effects=({"op": "heal", "percent": .32}, {"op": "cleanse"}), icon="⏪"),
    _scroll("ultimate_glass_citadel", "Магический свиток: Стеклянная цитадель",
            "Огромный барьер отражает следующий удар.", element="frost", ultimate=True, cooldown=0,
            dodgeable=False, effects=({"op": "shield", "percent": .35},
                                     {"op": "reflect_next", "value": .50}), icon="🏰"),
    _scroll("ultimate_kraken", "Свиток умения: Кракен из ведра",
            "Хватается за врага и лишает его хода.", category="skill", ultimate=True,
            element="water", cooldown=0, effects=({"op": "damage", "amount": .90},
                                 {"op": "stun", "turns": 1}), icon="🐙"),
    _scroll("ultimate_final_gambit", "Свиток умения: Последняя ставка",
            "Сокрушительный удар ценой собственного здоровья.", category="skill",
            element="fire", ultimate=True, cooldown=0, effects=({"op": "damage", "amount": 2.55},
                                               {"op": "self_damage", "percent": .12}), icon="🎲"),
    _scroll("ultimate_rainbow_flood", "Магический свиток: Радужный потоп",
            "Магическая волна одновременно ранит и лечит.", element="water", ultimate=True, cooldown=0,
            effects=({"op": "damage", "amount": 1.75},
                     {"op": "heal", "percent": .15}), icon="🌈"),
    _scroll("ultimate_blackout", "Магический свиток: Затмение",
            "Густая тьма ранит и ослепляет.", element="frost", ultimate=True, cooldown=0,
            dodgeable=False, effects=({"op": "damage", "amount": 1.10},
                                      {"op": "blind", "value": .45, "turns": 2}), icon="🌑"),
    _scroll("ultimate_golden_frame", "Магический свиток: Золотая рама",
            "Заключает героя в сияющую защиту и усиливает урон.", element="fire", ultimate=True,
            cooldown=0, dodgeable=False, effects=({"op": "shield", "percent": .20},
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
)


SCROLLS = REGULAR_SCROLLS + ULTIMATE_SCROLLS
SCROLL_BY_CODE = {row["code"]: row for row in SCROLLS}
SHIELD_BY_CODE = {row["code"]: row for row in SHIELDS}
DEFAULT_LOADOUT = (
    "scroll_arcane_spark", "scroll_healing_rain", "scroll_nmm_glint",
    "ultimate_starfall",
)
DEFAULT_SHIELD = "shield_paper_buckler"


def scroll(code: str) -> dict | None:
    return SCROLL_BY_CODE.get(str(code or ""))


def shield(code: str) -> dict | None:
    return SHIELD_BY_CODE.get(str(code or ""))


def validate_loadout(codes) -> tuple[str, str, str, str]:
    values = tuple(str(code or "") for code in (codes or ()))
    if len(values) != 4 or len(set(values)) != 4:
        raise ValueError("Выбери четыре разных свитка.")
    found = [scroll(code) for code in values]
    if any(row is None for row in found):
        raise ValueError("В наборе есть неизвестный свиток.")
    if any(row["ultimate"] for row in found[:3]) or not found[3]["ultimate"]:
        raise ValueError("Первые три слота — обычные свитки, четвёртый — ультимейт.")
    return values


def public_scroll(row: dict) -> dict:
    return {**row, "effects": [dict(effect) for effect in row["effects"]]}


def public_shield(row: dict) -> dict:
    return {**row, "defend_effects": [dict(effect) for effect in row.get("defend_effects", ())]}


def _validate() -> None:
    codes = [row["code"] for row in SCROLLS]
    if len(codes) != 40 or len(set(codes)) != len(codes):
        raise ValueError("scroll catalogue must contain 40 unique entries")
    for row in SCROLLS:
        prefix = "Магический свиток:" if row["category"] == "magic" else "Свиток умения:"
        if not row["name"].startswith(prefix):
            raise ValueError(f"bad scroll name: {row['code']}")
        if row["cooldown"] < 0 or row["auto_weight"] != 1:
            raise ValueError(f"bad scroll timing: {row['code']}")
        if row.get("element") not in ELEMENTS:
            raise ValueError(f"unknown scroll element: {row['code']}")
        for effect in row["effects"]:
            if effect.get("op") not in EFFECT_OPS:
                raise ValueError(f"unknown scroll operation: {effect.get('op')}")
        if not row["dodgeable"] and any(effect.get("op") == "stun" for effect in row["effects"]):
            raise ValueError(f"undodgeable stun is not allowed: {row['code']}")
    if {row["element"] for row in SCROLLS} != set(ELEMENTS):
        raise ValueError("every scroll element must be represented")
    shield_codes = [row["code"] for row in SHIELDS]
    if len(shield_codes) != 10 or len(set(shield_codes)) != len(shield_codes):
        raise ValueError("shield catalogue must contain 10 unique entries")
    validate_loadout(DEFAULT_LOADOUT)


_validate()
