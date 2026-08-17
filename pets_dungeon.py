"""Fixed encounters and rewards for the endless pet dungeon.

The run state belongs to :mod:`pets`; this module is deliberately pure so a floor can be
recreated after a restart without trusting anything supplied by the client.
"""

from __future__ import annotations

import random
from typing import Final


MIN_POWER: Final = 1_000
# Raised from 5 alongside the PVE ruby rework (see TIER_RUBY_CHANCE in pets_mobs.py) so
# five entries a day -- 50 rubies -- is roughly what a full day of PVE and quarrying
# actually earns, with a little left over.
ENTRY_RUBY_COST: Final = 10
ESCALATOR_RUBY_COST: Final = 5
ANTIMAGIC_REFLECT_SHARE: Final = 0.85
# Cut with the rewards, not independently of them: the dungeon's shop is paid for out of
# the dungeon's own income, so shrinking one without the other would leave a runner
# unable to afford the recovery their floor was supposed to fund (see reward_for).
SHOP_PARTIAL_HEAL_COST: Final = 100
SHOP_FULL_HEAL_COST: Final = 180
SHOP_PARTIAL_HEAL_SHARE: Final = 0.30
# Per RUN, not per floor. Unlimited healing turned a deep run into a question of how much
# gold the player had rather than how far they could actually get, so each kind of rest is
# rationed and the remaining count is printed on the button that spends it.
SHOP_PARTIAL_HEAL_USES: Final = 3
SHOP_FULL_HEAL_USES: Final = 3
SCROLL_LOOT_START_FLOOR: Final = 10
# A boss is the one enemy on a floor worth building a run around, so it is also the one
# worth a real jump in loot rather than a rounding difference.
BOSS_ITEM_MULTIPLIER: Final = 1.5
BOSS_SCROLL_MULTIPLIER: Final = 3.0
# How far a single kill's chances may wander either side of the floor's public baseline.
# Wide on purpose: identical mobs on identical floors should not feel like a vending
# machine, and the spread is what makes a lucky kill feel lucky.
LOOT_CHANCE_JITTER: Final = (0.55, 1.45)

# --- the hydra ------------------------------------------------------------------------
# Three heads that share ONE boss's health rather than owning a full boss each. The old
# encounter gave every head the boss's whole HP pool and then, every third press, healed
# all three back up to half -- including heads already beaten below that, so a hit could
# leave a head healthier than it started. Net progress was structurally zero and the fight
# could not be won at any stat level.
#
# What makes it a hydra now is per-head regrowth: a head you fail to finish in one go
# grows a slice of itself back, so a slow runner races the regrowth while a decisive one
# does not. A head that actually dies stays dead -- the threat is being too slow, never
# having your work taken away.
HYDRA_HEADS: Final = 3
HYDRA_HEAD_HP_SHARE: Final = 1 / HYDRA_HEADS
HYDRA_REGROWTH_SHARE: Final = 0.20
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
     "description": "Десять стайных бойцов заняли проход. Двое из них — целители: пока "
                    "хоть один жив, остальные поднимаются снова, и стая всё время "
                    "перестраивается.",
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
     "Клинок без руны он впитывает как воду: простая сталь его лечит, а не ранит."),
    ("Антимаг без имени", "antimagic", 0,
     "Он возвращает 85% магического и рунного урона; здесь надёжнее простое оружие."),
    ("Плачущее дерево", "healing_pass", 0,
     "Сок медленно затягивает старые зарубки, а у корней журчит невидимый ручей."),
    ("Трёхглавая гидра", "three_heads", 0,
     "Недобитая голова затягивает раны на глазах; срубленная не отрастает. Бей до конца."),
    ("Кузнец багровой кузни", "frost_only", 0,
     "Воздух перед ним дрожит от жара, но на молоте остаётся тонкая белая изморозь."),
)


# Where the dungeon actually ends. BOSSES is indexed modulo its own length, so floor 50
# used to serve the floor-5 boss again and the descent ran for ever -- an endless corridor
# of enemies the players had already beaten, dressed up as progress. The last boss stands
# on the last boss floor the list can fill, and past it there is nothing built yet.
LAST_FLOOR: Final = len(BOSSES) * 5
DUNGEON_CLEARED_NOTICE: Final = (
    "Ты отпинал всех наших боссов, приходи позже, мы завезём новых."
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


# --- the pack's healers ---------------------------------------------------------------
# Ten enemies in one room was ten identical fights in a row. Two of them are now healers:
# while either still stands, everything else in the room comes back a turn after it dies,
# and the order of the list is reshuffled so the survivors cannot simply be counted off
# left to right. Killing the healers first turns a wall into a puzzle with an answer.
#
# Positions rather than a random draw: the floor's enemy list is reproducible from
# (floor, index) everywhere in the game, and a healer chosen by RNG would move between two
# reads of the same room.
PACK_HEALER_INDEXES: Final = (3, 7)
# One action's grace. Long enough that killing a healer between two revivals is possible,
# short enough that grinding the pack while a healer lives is visibly pointless.
PACK_REVIVE_DELAY: Final = 1


def is_pack_healer(floor: int, index: int) -> bool:
    room = _room(floor)
    return (not is_boss_floor(floor) and room["kind"] == "pack_fury"
            and int(index) in PACK_HEALER_INDEXES)


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
        name = (f"Целитель стаи {index + 1}" if is_pack_healer(floor, index)
                else f"Стайный {base_name} {index + 1}")
    elif room["kind"] == "elite":
        name = f"Старший {base_name}"
    else:
        name = f"Дозорный {base_name} {index + 1}"
    healer = is_pack_healer(floor, index)
    return {
        "code": f"floor_{floor}_{index}", "name": name, "floor": floor, "index": index,
        "theme": floor_name(floor), "boss": False, "gimmick": room["kind"],
        "healer": healer,
        "hint": ("Пока он жив, павшие в этом зале встают снова."
                 if healer else room["hint"]),
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
    # Cut, and above all FLATTENED. The dungeon was 93% of every player's daily gold, and
    # the floor ramp is what made that unbalanceable: it grew without limit while nothing
    # it fed grew with it. The ramp is cut five times harder than the base, so floor 30 is
    # now worth about 2.4x floor 1 rather than 30x. The dungeon stays the best gold in the
    # game for somebody who can survive down there -- it just stops being the only gold.
    #
    # XP is untouched. XP now buys LEVELS, which cost rubies, so the dungeon handing out
    # experience no longer hands out progression on its own.
    if boss:
        gold, xp = 200 + floor * 15, 80 + floor * 18
    else:
        gold, xp = (120 + floor * 6) // enemy_count, (35 + floor * 12) // enemy_count
    # Base, ramp and cap all cut in half here -- the live drop rate was far too high, so
    # every number in this curve pays out at half its previous odds, floor for floor.
    scroll_chance = 0.0 if floor < SCROLL_LOOT_START_FLOOR else min(
        0.125, 0.02 + (floor - SCROLL_LOOT_START_FLOOR) * 0.005,
    )
    # Divided by the room's population for the same reason gold and xp are, and it was
    # the one number that wasn't: the chance is rolled per KILL, so an undivided 10% on a
    # ten-enemy pack floor paid a whole scroll per floor while a lone enemy on the next
    # floor paid a tenth of one. Measured over a full descent that was 13.3 scrolls out of
    # a 40-scroll catalogue -- a third of everything permanent, in one run. Per floor the
    # budget is now flat, so crowded rooms mean more victories, not more scrolls.
    scroll_chance /= enemy_count
    return {
        "gold": max(1, gold), "xp": max(1, xp),
        "item_chance": min(0.22, 0.012 + floor * 0.004) * (BOSS_ITEM_MULTIPLIER if boss else 1),
        # The boss multiplier is applied AFTER the ordinary cap, not inside it: capping
        # first and multiplying after is what lets a boss actually out-drop the corridor
        # it stands at the end of instead of flattening into the same 12.5%.
        "scroll_chance": min(1.0, scroll_chance * (BOSS_SCROLL_MULTIPLIER if boss else 1)),
    }


def roll_reward(floor: int, boss: bool, rng=None) -> dict:
    """Roll one victory's rewards around the floor's public baseline.

    Every number here is rolled per KILL, including both drop chances: two runners who
    kill the same mob on the same floor are not owed the same odds, and neither is the
    same runner on their second pass. Defaults to SystemRandom -- nothing about a dungeon
    kill needs to be reproducible, and a seed shared across kills is exactly how identical
    mobs started paying out identical loot.
    """
    rng = rng or random.SystemRandom()
    enemy_count = 1 if boss else len(encounters_for_floor(floor))
    reward = dict(reward_for(floor, boss, enemy_count))
    reward["gold"] = rng.randint(round(reward["gold"] * .8), round(reward["gold"] * 1.2))
    reward["xp"] = rng.randint(max(1, round(reward["xp"] * .7)), round(reward["xp"] * 1.3))
    low, high = LOOT_CHANCE_JITTER
    for key in ("item_chance", "scroll_chance"):
        reward[key] = min(1.0, max(0.0, reward[key] * rng.uniform(low, high)))
    return reward


def shop_heal_cost(floor: int) -> int:
    """Compatibility price for callers that still show one healing option."""
    return SHOP_FULL_HEAL_COST
