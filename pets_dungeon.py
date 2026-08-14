"""Fixed encounters and rewards for the endless pet dungeon.

The run state belongs to :mod:`pets`; this module is deliberately pure so a floor can be
recreated after a restart without trusting anything supplied by the client.
"""

from __future__ import annotations

import random
from typing import Final


MIN_POWER: Final = 1_000
ENTRY_RUBY_COST: Final = 5
ESCALATOR_RUBY_COST: Final = 5
ANTIMAGIC_REFLECT_SHARE: Final = 0.85
SHOP_PARTIAL_HEAL_COST: Final = 160
SHOP_FULL_HEAL_COST: Final = 300
SCROLL_LOOT_START_FLOOR: Final = 10
DUNGEON_OPEN: Final = True
DUNGEON_CLOSED_NOTICE: Final = (
    "К подземелью подъехала экспедиция для расследования. "
    "Его оцепили и никого не пускают."
)

THEMES: Final = (
    ("Тролльи катакомбы", ("Костолом", "Мостовой громила", "Шаман гнили")),
    ("Багровый склеп", ("Кровавый паж", "Ночной аристократ", "Вампир-лекарь")),
    ("Змеиные норы", ("Песчаная гадюка", "Кобра-страж", "Удав-давитель")),
    ("Грибные шахты", ("Споровый ходок", "Мицелий-рыцарь", "Корневой жрец")),
    ("Затонувший храм", ("Краб-страж", "Сирена глубин", "Угорь молний")),
)

# Each room owns its encounter count and short story. A floor's reward budget is shared
# between these encounters, so a crowded room asks for more fights without printing money.
ROOMS: Final = (
    {"count": 2, "kind": "duo", "strength": 1.28, "health": 1.24,
     "description": "Два брата никого не пускают и даже не делают вид, что слушают.",
     "hint": "Один держит дверь, второй смотрит из-за плеча."},
    {"count": 10, "kind": "pack_fury", "strength": .68, "health": .72,
     "description": "Десять стайных бойцов заняли проход. Пока их много, они слишком смелые.",
     "hint": "Соседи подбадривают его."},
    {"count": 1, "kind": "elite", "strength": 1.65, "health": 1.70,
     "description": "Один старый страж остался у двери. Уходить он явно не собирается.",
     "hint": "Старые доспехи звенят, когда он делает шаг."},
    {"count": 4, "kind": "patrol", "strength": 1.02, "health": 1.04,
     "description": "Дозор заметил тебя раньше, чем ты успел выбрать дорогу.",
     "hint": "Он перекрывает путь к следующему залу."},
)

BOSSES: Final = (
    ("Феникс пепельных залов", "reincarnate", 0,
     "На чёрном камне остаются горячие перья, хотя птица давно не взмахивала крыльями."),
    ("Стальной привратник", "standard", 5,
     "В его забрале нет щели, но старый замок на груди всё ещё отсчитывает чужие шаги."),
    ("Ледяной дракон", "fire_only", 0,
     "Иней на его чешуе не тает даже рядом с факелами; в трещинах мерцает далёкий жар."),
    ("Молчаливый колосс", "standard", 5,
     "Каменные пальцы сжаты вокруг меча; кажется, он стоял здесь ещё до постройки подземелья."),
    ("Призрак Аквариуса", "spells_only", 0,
     "Пыль вокруг него ложится в страницы сама собой, а старые чернила светятся в темноте."),
    ("Антимаг без имени", "antimagic", 0,
     "Он возвращает 85% магического и рунного урона; здесь надёжнее простое оружие."),
    ("Плачущее дерево", "healing_pass", 0,
     "Сок медленно затягивает старые зарубки, а у корней журчит невидимый ручей."),
    ("Трёхглавая гидра", "three_heads", 0,
     "Три голоса спорят в одном горле, и каждый раз тишина длится подозрительно недолго."),
    ("Кузнец багровой кузни", "frost_only", 0,
     "Воздух перед ним дрожит от жара, но на молоте остаётся тонкая белая изморозь."),
)


def is_boss_floor(floor: int) -> bool:
    return floor > 0 and floor % 5 == 0


def _scale(floor: int, boss: bool = False) -> int:
    """Fixed stat value for a floor, independent of the challenger."""
    value = 22 + max(0, floor - 1) * 7
    return round(value * (1.80 if boss else 1.0))


def floor_name(floor: int) -> str:
    theme, _mobs = THEMES[((max(1, floor) - 1) // 3) % len(THEMES)]
    return theme


def _room(floor: int) -> dict:
    return ROOMS[(max(1, floor) - 1) % len(ROOMS)]


def floor_description(floor: int) -> str:
    return "Впереди ждёт хозяин этого места." if is_boss_floor(floor) else _room(floor)["description"]


def pack_strength_multiplier(floor: int, cleared) -> float:
    """Crowded packs lose their courage as individual members fall."""
    if is_boss_floor(floor) or _room(floor)["kind"] != "pack_fury":
        return 1.0
    cleared = {int(index) for index in cleared if str(index).isdigit()}
    remaining = sum(row["index"] not in cleared for row in encounters_for_floor(floor))
    return 1 + .12 * max(0, remaining - 1)


def encounter(floor: int, index: int) -> dict:
    """One reproducible enemy. ``index`` is zero-based within the floor."""
    floor = max(1, int(floor))
    if is_boss_floor(floor):
        name, gimmick, tier_ahead, hint = BOSSES[((floor // 5) - 1) % len(BOSSES)]
        value = _scale(floor + tier_ahead, boss=True)
        return {
            "code": f"boss_{floor}", "name": name, "floor": floor, "index": 0,
            "theme": floor_name(floor), "boss": True, "gimmick": gimmick, "hint": hint,
            "stats": {"strength": value + 12, "health": value + 18,
                      "agility": value - 4, "luck": value - 6},
            "armor": max(0, value // 3), "level": floor + 8 + tier_ahead,
            "reward": reward_for(floor, boss=True),
        }

    _theme, mobs = THEMES[((floor - 1) // 3) % len(THEMES)]
    room = _room(floor)
    count = room["count"]
    index = max(0, min(count - 1, int(index)))
    value = _scale(floor) + index * 2
    profiles = ((7, 12, -5, -6), (10, 2, 6, -4), (3, 18, -3, 5))
    strength, health, agility, luck = profiles[index % len(profiles)]
    base_name = mobs[index % len(mobs)]
    if room["kind"] == "duo":
        name = f"Брат {base_name}"
    elif room["kind"] == "pack_fury":
        name = f"Стайный {base_name} {index + 1}"
    elif room["kind"] == "elite":
        name = f"Старший {base_name}"
    else:
        name = f"Дозорный {base_name} {index + 1}"
    return {
        "code": f"floor_{floor}_{index}", "name": name, "floor": floor, "index": index,
        "theme": floor_name(floor), "boss": False, "gimmick": room["kind"],
        "hint": room["hint"],
        "stats": {"strength": round((value + strength) * room["strength"]),
                  "health": round((value + health) * room["health"]),
                  "agility": max(1, value + agility), "luck": max(1, value + luck)},
        "armor": max(0, value // 5 + index * 2), "level": floor + 2,
        "reward": reward_for(floor, boss=False, enemy_count=count),
    }


def encounters_for_floor(floor: int) -> tuple[dict, ...]:
    return (encounter(floor, 0),) if is_boss_floor(floor) else tuple(
        encounter(floor, index) for index in range(_room(floor)["count"])
    )


def reward_for(floor: int, boss: bool, enemy_count: int = 1) -> dict:
    """One victory's share of a floor budget.

    An entry costs five rubies and a full rest costs 300 gold, so the opening rooms must
    fund at least one recovery before a runner is asked to take on a boss.  The budget
    remains shared between a room's enemies: crowded rooms ask for more victories, not
    more total currency.
    """
    floor = max(1, int(floor))
    enemy_count = max(1, int(enemy_count))
    if boss:
        gold, xp = 450 + floor * 70, 80 + floor * 18
    else:
        gold, xp = (180 + floor * 30) // enemy_count, (35 + floor * 12) // enemy_count
    return {
        "gold": max(1, gold), "xp": max(1, xp),
        "item_chance": min(0.22, 0.012 + floor * 0.004) * (1.5 if boss else 1),
        "scroll_chance": 0.0 if floor < SCROLL_LOOT_START_FLOOR else min(
            0.25, 0.04 + (floor - SCROLL_LOOT_START_FLOOR) * 0.01,
        ),
    }


def roll_reward(floor: int, boss: bool, rng=None) -> dict:
    """Roll one victory's rewards around the floor's public baseline."""
    rng = rng or random.SystemRandom()
    enemy_count = 1 if boss else len(encounters_for_floor(floor))
    reward = dict(reward_for(floor, boss, enemy_count))
    reward["gold"] = rng.randint(round(reward["gold"] * .8), round(reward["gold"] * 1.2))
    reward["xp"] = rng.randint(max(1, round(reward["xp"] * .7)), round(reward["xp"] * 1.3))
    reward["item_chance"] = min(1.0, max(0.0, reward["item_chance"] * rng.uniform(.7, 1.3)))
    return reward


def shop_heal_cost(floor: int) -> int:
    """Compatibility price for callers that still show one healing option."""
    return SHOP_FULL_HEAL_COST
