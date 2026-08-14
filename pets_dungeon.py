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

BOSSES: Final = (
    ("Феникс пепельных залов", "reincarnate",
    "На чёрном камне остаются горячие перья, хотя птица давно не взмахивала крыльями."),
    ("Ледяной дракон", "fire_only",
    "Иней на его чешуе не тает даже рядом с факелами; в трещинах мерцает далёкий жар."),
    ("Призрак Аквариуса", "spells_only",
    "Пыль вокруг него ложится в страницы сама собой, а старые чернила светятся в темноте."),
    ("Плачущее дерево", "healing_pass",
    "Сок медленно затягивает старые зарубки, а у корней журчит невидимый ручей."),
    ("Трёхглавая гидра", "three_heads",
    "Три голоса спорят в одном горле, и каждый раз тишина длится подозрительно недолго."),
    ("Кузнец багровой кузни", "frost_only",
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


def encounter(floor: int, index: int) -> dict:
    """One reproducible enemy. ``index`` is zero-based within the floor."""
    floor = max(1, int(floor))
    if is_boss_floor(floor):
        name, gimmick, hint = BOSSES[((floor // 5) - 1) % len(BOSSES)]
        value = _scale(floor, boss=True)
        return {
            "code": f"boss_{floor}", "name": name, "floor": floor, "index": 0,
            "theme": floor_name(floor), "boss": True, "gimmick": gimmick, "hint": hint,
            "stats": {"strength": value + 12, "health": value + 18,
                      "agility": value - 4, "luck": value - 6},
            "armor": max(0, value // 3), "level": floor + 8,
            "reward": reward_for(floor, boss=True),
        }

    _theme, mobs = THEMES[((floor - 1) // 3) % len(THEMES)]
    index = max(0, min(2, int(index)))
    value = _scale(floor) + index * 3
    profiles = ((7, 12, -5, -6), (10, 2, 6, -4), (3, 18, -3, 5))
    strength, health, agility, luck = profiles[index]
    return {
        "code": f"floor_{floor}_{index}", "name": mobs[index], "floor": floor, "index": index,
        "theme": floor_name(floor), "boss": False, "gimmick": None, "hint": "",
        "stats": {"strength": value + strength, "health": value + health,
                  "agility": max(1, value + agility), "luck": max(1, value + luck)},
        "armor": max(0, value // 5 + index * 2), "level": floor + 2,
        "reward": reward_for(floor, boss=False),
    }


def encounters_for_floor(floor: int) -> tuple[dict, ...]:
    return (encounter(floor, 0),) if is_boss_floor(floor) else tuple(
        encounter(floor, index) for index in range(3)
    )


def reward_for(floor: int, boss: bool) -> dict:
    multiplier = 3 if boss else 1
    return {
        "gold": (20 + floor * 8) * multiplier,
        "xp": (10 + floor * 5) * multiplier,
        "item_chance": min(0.22, 0.025 + floor * 0.005) * (1.5 if boss else 1),
        "scroll_chance": 0.0 if floor < SCROLL_LOOT_START_FLOOR else min(
            0.25, 0.04 + (floor - SCROLL_LOOT_START_FLOOR) * 0.01,
        ),
    }


def roll_reward(floor: int, boss: bool, rng=None) -> dict:
    """Roll one victory's rewards around the floor's public baseline."""
    rng = rng or random.SystemRandom()
    reward = dict(reward_for(floor, boss))
    reward["gold"] = rng.randint(round(reward["gold"] * .8), round(reward["gold"] * 1.2))
    reward["xp"] = rng.randint(max(1, round(reward["xp"] * .7)), round(reward["xp"] * 1.3))
    reward["item_chance"] = min(1.0, max(0.0, reward["item_chance"] * rng.uniform(.7, 1.3)))
    return reward


def shop_heal_cost(floor: int) -> int:
    """Compatibility price for callers that still show one healing option."""
    return SHOP_FULL_HEAL_COST
