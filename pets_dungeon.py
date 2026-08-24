"""Fixed encounters and rewards for the endless pet dungeon.

The run state belongs to :mod:`pets`; this module is deliberately pure so a floor can be
recreated after a restart without trusting anything supplied by the client.
"""

from __future__ import annotations

import random
from typing import Final

import pets_config as C


MIN_POWER: Final = 1_000
# Raised from 5 alongside the PVE ruby rework (see TIER_RUBY_CHANCE in pets_mobs.py) so
# five entries a day -- 50 rubies -- is roughly what a full day of PVE and quarrying
# actually earns, with a little left over.
ENTRY_RUBY_COST: Final = 10
ANTIMAGIC_REFLECT_SHARE: Final = 0.85
# Healing is bought with DIAMONDS. It used to come out of the run's own coin income --
# which is why the two are still cut together in reward_for -- but a floor's gold was
# also the thing being saved up for everything else, so recovery competed with the rest
# of the game for the same purse. Diamonds are the scarce currency and come from outside
# the dungeon, which makes a heal a decision about the RUN instead of a rounding error
# against the day's earnings.
SHOP_PARTIAL_HEAL_RUBIES: Final = 1
SHOP_FULL_HEAL_RUBIES: Final = 3
SHOP_PARTIAL_HEAL_SHARE: Final = 0.30
# Per RUN, not per floor. Unlimited healing turned a deep run into a question of how much
# gold the player had rather than how far they could actually get, so each kind of rest is
# rationed and the remaining count is printed on the button that spends it.
SHOP_PARTIAL_HEAL_USES: Final = 3
SHOP_FULL_HEAL_USES: Final = 3
SCROLL_LOOT_START_FLOOR: Final = 10
# Applied to dungeon COINS only, after the floor budget is split between a room's
# enemies. See reward_for.
COIN_REWARD_BONUS: Final = 1.30
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
    # Five, not ten. Ten was ten near-identical fights in a row, and two healers inside
    # it meant a player could be pressing the same button twenty times before the room
    # stayed down. Five keeps the puzzle -- two healers, three bodies -- and cuts the
    # typing. The per-enemy stats rise to match, so the ROOM is as dangerous as it was.
    {"count": 5, "kind": "pack_fury", "strength": 1.02, "health": 1.08,
     "description": "Пятеро стайных бойцов заняли проход. Двое из них — целители: пока "
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

# What each boss's rule actually MEANS for the fight, in one line, phrased as the boss's
# property rather than as an order to the player. "Возьми огненное оружие" tells somebody
# what to do without telling them why, and reads as a puzzle they have already failed if
# they are standing there without one; "уязвим к огню" is the same information as a fact
# about the enemy, which is what a hint is for.
#
# Every gimmick that changes how damage lands MUST have an entry here -- a boss with a
# hidden rule is a boss that kills you for something you had no way to know.
BOSS_WEAKNESS: Final = {
    "fire_only": "Уязвим к огню: огненный урон проходит в полную силу, обычная сталь — почти нет.",
    "frost_only": "Уязвим к холоду: ледяной урон проходит в полную силу, обычная сталь — почти нет.",
    "spells_only": "Неуязвим к простой стали: она его лечит. Ранят только магия и зачарованное оружие.",
    "antimagic": "Отражает 85% магического и рунного урона обратно. Простое оружие безопаснее.",
    "reincarnate": "Поднимается один раз после смерти. Его нужно добить второй раз.",
    "three_heads": "Три головы делят одно здоровье. Недобитая голова затягивает раны, срубленная не отрастает.",
    "healing_pass": "Его можно не убивать: лечение успокаивает его, и он пропускает дальше.",
    "standard": "",
}


def boss_weakness(gimmick: str) -> str:
    """The one-line explanation of a boss's rule, or "" when it fights plainly."""
    return BOSS_WEAKNESS.get(str(gimmick or ""), "")


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


# The descent is endless again. BOSSES is indexed modulo its own length, so past floor 45
# the roster repeats -- which is the honest trade for letting somebody who has cleared
# everything keep going, rather than parking them on a finished floor.
#
# What must NOT repeat is the payout. Enemy stats keep climbing (see _scale), but coins
# and experience stop at the last built floor's value: below it a descent is a curve, past
# it it is a treadmill that gets harder and pays the same. That is the whole reason an
# endless corridor was closed the first time -- it printed money for ever -- and capping
# the reward instead of the floor is what makes reopening it safe.
REWARD_CAP_FLOOR: Final = len(BOSSES) * 5
# Kept as the deepest floor the BOSS ROSTER covers, which is what the screens mean when
# they talk about having cleared everything. It is no longer a wall.
LAST_FLOOR: Final = len(BOSSES) * 5
DUNGEON_CLEARED_NOTICE: Final = (
    "Ты отпинал всех наших боссов, приходи позже, мы завезём новых."
)


# Every fifth floor belongs to a boss. Named because three other places counted in fives
# to find them -- the reward cap, the roster length and the admin boss workshop -- and a
# literal repeated in four files is a rule nobody can change.
BOSS_EVERY: Final = 5


# --------------------------------------------------------------------- лавка подземелья
#
# The shop is a SHELF rather than a pair of buttons, because it is going to grow: potions
# beyond healing, and whatever else a run turns out to need. Everything a purchase needs
# to be made, priced, rationed and described lives in one row here, so adding stock is
# adding a row -- no client learns a new button, no handler learns a new branch.
#
# The rations stay. Diamonds make a heal cost something real, but unlimited healing is
# what turned a deep descent into a question of how much the player could spend rather
# than how far they could actually get, and that is true of any currency.
def _stock(code, icon, name, description, currency, price, heal, uses=None):
    return {
        "code": code, "icon": icon, "name": name, "description": description,
        "currency": currency, "price": price, "heal": heal, "uses": uses,
        # Per-run counters are stored under the item's own key, so a new row cannot
        # collide with an existing one or silently share its ration.
        "used_key": f"shop_used_{code}",
    }


SHOP_STOCK: Final = (
    _stock("heal_partial", "🩹", "Бинты",
           f"Восстанавливает {round(SHOP_PARTIAL_HEAL_SHARE * 100)}% здоровья.",
           "ruby", SHOP_PARTIAL_HEAL_RUBIES, SHOP_PARTIAL_HEAL_SHARE, SHOP_PARTIAL_HEAL_USES),
    _stock("heal_full", "❤️", "Полевой лазарет",
           "Восстанавливает здоровье полностью.",
           "ruby", SHOP_FULL_HEAL_RUBIES, 1.0, SHOP_FULL_HEAL_USES),
)

# Both rows keep the keys the old rest counters used, so a run already in progress when
# this shipped does not get its ration handed back.
SHOP_STOCK = tuple(
    {**row, "used_key": "partial_heals_used"} if row["code"] == "heal_partial"
    else {**row, "used_key": "full_heals_used"} if row["code"] == "heal_full"
    else row
    for row in SHOP_STOCK
)
SHOP_CODES: Final = tuple(row["code"] for row in SHOP_STOCK)
# What `dungeon_rest`'s old "partial"/"full" argument means now. Both clients and every
# existing test still speak it, and it is a perfectly good shorthand for the two coin
# rows -- it just is not the whole shelf any more.
SHOP_REST_CODES: Final = {"partial": "heal_partial", "full": "heal_full"}


def shop_item(code: str) -> dict | None:
    """One row of stock by code, or None. Returns a copy: the shelf is data."""
    return next((dict(row) for row in SHOP_STOCK if row["code"] == str(code)), None)


def is_boss_floor(floor: int) -> bool:
    return floor > 0 and floor % BOSS_EVERY == 0


# --- the deep corridor ----------------------------------------------------------------
# Past DEPTH_RAMP_START an ordinary enemy stops growing by a flat +7 a floor and starts
# compounding. The flat step was priced against a pet whose whole power WAS its stat
# block; by the time somebody is walking floor 20 they are carrying a legendary weapon, a
# rune burning on it and four scrolls, and none of that exists on the mob's side of the
# fight. Measured, a corridor mob at floor 24 was taking 2--11% of such a player's health
# bar and doing NOTHING AT ALL in 15--46% of fights -- the enemy died inside two of the
# player's actions and never got a swing in. A flat ramp cannot answer a multiplying
# opponent, so this one multiplies too.
#
# Bosses are deliberately excluded. They were already the wall -- the same measurement had
# every build's run ending on a boss floor and never in the corridor -- so the gap is
# closed by lifting the corridor toward them rather than by moving the wall again.
# The corridor grows by a STRAIGHT LINE, and the slope is the only knob.
#
# It used to compound: a flat +7 a floor multiplied by 1.03 for every floor past the
# twelfth. The flat step really was too weak deep down -- a floor-24 mob was taking 2-11%
# of a geared player's health bar -- but compounding overshot the fix and kept going.
# Measured, the stat level needed to win a floor half the time ran 245 at floor 10, 542
# at 20 and 1790 at 45; and because a stat point costs `level ** 1.5`, the GOLD behind
# those runs 1.9M, 13.6M and 271M. The corridor was not getting harder, it was leaving.
#
# A line cannot do that. The slope below is chosen to sit near the old curve where it was
# originally tuned (floors 12-15) and to keep climbing at the same pace for ever, so the
# hundredth floor is harder than the fiftieth by exactly as much as the fiftieth is
# harder than the twenty-fifth.
DEPTH_RAMP_START: Final = 12
# Two slopes, not one. The shallow floors were never the problem and are left exactly as
# they were; only the part past DEPTH_RAMP_START changes, and it changes from a curve
# into a steeper straight line.
CORRIDOR_STAT_SLOPE: Final = 7
DEEP_CORRIDOR_STAT_SLOPE: Final = 9
# How much bigger the floor's owner is than the floor itself. Read against the room
# multipliers in ROOMS: the elite room is 1.65, so a boss leads its own corridor by
# roughly a fifth once both are on the ramp.
BOSS_STAT_MULTIPLIER: Final = 1.80
# Ordinary enemies carried a fifth of their stat value as armour against a boss's third,
# which is most of why a corridor fight was over before it started: thin armour means a
# short fight, and a short fight is one the enemy spends dying rather than hitting back.
# Deep floors get the boss's share. Shallow ones keep the old number, so nothing a new
# runner meets on the way to their first bosses changes at all.
CORRIDOR_ARMOR_DIVISOR: Final = 5
DEEP_CORRIDOR_ARMOR_DIVISOR: Final = 3
# Armour is the one enemy stat that multiplies against everything the player brought:
# reduction saturates towards ARMOR_MAX (60%), so an enemy that keeps accruing it does
# not get tougher, it gets further out of reach -- a deep mob was absorbing half of every
# blow and carrying 30,000 health behind it. The share of a swing an enemy may ever
# refuse is capped here instead, well under the engine's own ceiling. Everything past the
# cap has to come from health and damage, which the player can answer with more of their
# own; a wall of armour is the one thing they cannot.
CORRIDOR_ARMOR_CAP: Final = 120        # about 33% absorbed
BOSS_ARMOR_CAP: Final = 190            # about 39%


def _scale(floor: int, boss: bool = False) -> int:
    """Fixed stat value for a floor, independent of the challenger.

    One line for the corridor and the same line for the boss, multiplied. Bosses share it
    for a reason worth keeping: when only the corridor was lifted, two dozen floors of
    compounding made the owner of a floor the easiest thing standing on it -- 0.86x the
    elite behind it by floor 25, 0.46x by 45. Moving together is what keeps a wall a wall.

    A boss reads this at `floor + tier_ahead`, which is what keeps a plain boss and the
    gimmick boss five floors later on an identical stat block -- see BOSSES.
    """
    shallow = min(max(0, floor - 1), DEPTH_RAMP_START - 1)
    deep = max(0, floor - DEPTH_RAMP_START)
    value = 22 + shallow * CORRIDOR_STAT_SLOPE + deep * DEEP_CORRIDOR_STAT_SLOPE
    return round(value * (BOSS_STAT_MULTIPLIER if boss else 1.0))


def _corridor_armor(floor: int, value: int, index: int) -> int:
    """Armour for one ordinary enemy, thicker once the corridor is deep enough to need it."""
    divisor = (DEEP_CORRIDOR_ARMOR_DIVISOR if floor >= DEPTH_RAMP_START
               else CORRIDOR_ARMOR_DIVISOR)
    return max(0, min(CORRIDOR_ARMOR_CAP, value // divisor + max(0, int(index)) * 2))


# The order the stat block is read in, and the only four a dungeon enemy has. C.STAT_KEYS
# is six wide and a corridor mob carries neither endurance nor magic, so reading it here
# would print two empty columns -- and the magic one would be a lie twice over, since a
# dungeon enemy has no scroll loadout to spend it on either.
STAT_LINE_KEYS: Final = ("strength", "health", "agility", "luck")


def enemy_stat_line(row: dict) -> str:
    """One enemy's stat block as a single short line.

    Worded here rather than in either client, for the same reason a scroll's effects are:
    the Telegram screen and the Mini App must never describe the same enemy differently.
    Raw effective numbers on purpose -- they are what the player's own stat screen shows,
    so the halves that trigger «Слабое место» can actually be read off the two side by
    side instead of being discovered in the fight log afterwards.
    """
    stats = (row or {}).get("stats") or {}
    parts = [f"{C.STAT_EMOJI[key]} {int(stats.get(key, 0) or 0)}" for key in STAT_LINE_KEYS]
    parts.append(f"{C.ARMOR_EMOJI} {max(0, int((row or {}).get('armor', 0) or 0))}")
    return " · ".join(parts)


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
PACK_HEALER_INDEXES: Final = (1, 3)
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
        weakness = boss_weakness(gimmick)
        return {
            "code": f"boss_{floor}", "name": name, "floor": floor, "index": 0,
            "theme": floor_name(floor), "boss": True, "gimmick": gimmick,
            # The flavour line and the rule, kept apart: one sets the scene, the other is
            # the thing a player has to act on and must never be buried inside it.
            "hint": hint, "weakness": weakness,
            "stats": {"strength": value + 12, "health": value + 18,
                      "agility": value - 4, "luck": value - 6},
            "armor": max(0, min(BOSS_ARMOR_CAP, value // 3)),
            "level": floor + 8 + tier_ahead,
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
        "armor": _corridor_armor(floor, value, index), "level": floor + 2,
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
    # Past the built floors the enemies keep growing and the purse does not.
    paid_floor = min(floor, REWARD_CAP_FLOOR)
    # Cut, and above all FLATTENED. The dungeon was 93% of every player's daily gold, and
    # the floor ramp is what made that unbalanceable: it grew without limit while nothing
    # it fed grew with it. The ramp is cut five times harder than the base, so floor 30 is
    # now worth about 2.4x floor 1 rather than 30x. The dungeon stays the best gold in the
    # game for somebody who can survive down there -- it just stops being the only gold.
    #
    # XP is untouched. XP now buys LEVELS, which cost rubies, so the dungeon handing out
    # experience no longer hands out progression on its own.
    if boss:
        gold, xp = 200 + paid_floor * 15, 80 + paid_floor * 18
    else:
        gold, xp = (120 + paid_floor * 6) // enemy_count, (35 + paid_floor * 12) // enemy_count
    # Coins only, +30%: the dungeon asks for ten diamonds and a run's worth of health, and
    # the rests it has to fund come out of the same purse. XP is untouched -- it buys
    # levels, which cost diamonds, so paying more of it would be paying twice.
    gold = round(gold * COIN_REWARD_BONUS)
    # Base, ramp and cap all cut in half here -- the live drop rate was far too high, so
    # every number in this curve pays out at half its previous odds, floor for floor.
    scroll_chance = 0.0 if floor < SCROLL_LOOT_START_FLOOR else min(
        0.125, 0.02 + (paid_floor - SCROLL_LOOT_START_FLOOR) * 0.005,
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
        "item_chance": min(0.22, 0.012 + paid_floor * 0.004) * (BOSS_ITEM_MULTIPLIER if boss else 1),
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
    """Compatibility price for callers that still show one healing option. Diamonds."""
    return SHOP_FULL_HEAL_RUBIES


# --- chests and mimics ----------------------------------------------------------------
# Between floors, a one-in-ten find. Half the point is that it might not be a find at all:
# a chest that is always a chest is just a slower reward, whereas one that bites teaches a
# player to read the floor before reaching for it.
#
# The choice is deliberately real on both sides. Walking away from a mimic costs the bite
# and nothing else; fighting it risks the run for loot that is better than the corridor's.
CHEST_CHANCE: Final = 0.10
MIMIC_SHARE: Final = 0.45
# What opening the wrong box costs before you have decided anything. A share of MAX hp, so
# it stings equally at every depth rather than being a rounding error deep down.
MIMIC_BITE_SHARE: Final = 0.15
# A mimic is the floor's elite, slightly over: worth a real decision, not a free chest.
MIMIC_STRENGTH: Final = 1.15
MIMIC_HEALTH: Final = 1.10
# Even a beaten mimic can turn out to have been empty. It is the joke the encounter is
# built around, and it keeps "fight it" from being an automatic yes.
MIMIC_EMPTY_SHARE: Final = 0.25
MIMIC_EMPTY_NOTICE: Final = "Этот мимик был пустым."

CHEST_RUBY_RANGE: Final = (1, 3)
# Read against reward_for: a chest is worth a couple of ordinary kills in coin, and its
# real value is the cursed item and the rune, which the corridor hands out far more rarely.
CHEST_GOLD_SHARE: Final = 2.0


def chest_gold(floor: int) -> int:
    """Coins in a plain chest, priced off the floor it was found between."""
    return max(1, round(reward_for(max(1, int(floor)), boss=False)["gold"] * CHEST_GOLD_SHARE))


def mimic(floor: int) -> dict:
    """The enemy a mimic turns into, shaped like any other encounter on this floor."""
    floor = max(1, int(floor))
    value = _scale(floor)
    return {
        "code": f"mimic_{floor}", "name": "Мимик", "floor": floor, "index": 0,
        "theme": floor_name(floor), "boss": False, "gimmick": "mimic", "healer": False,
        "hint": "Он ждал, пока ты потянешься к крышке.",
        "stats": {"strength": round((value + 9) * MIMIC_STRENGTH),
                  "health": round((value + 15) * MIMIC_HEALTH),
                  "agility": max(1, value - 2), "luck": max(1, value + 3)},
        # A mimic is the floor's elite, so it is never thinner-skinned than the corridor
        # mobs standing either side of it -- which //4 quietly became once deep ordinary
        # enemies moved to the boss's //3.
        # Under the boss's ceiling like every other elite: `value // 4` is a floor that
        # keeps a mimic from being thinner-skinned than the corridor around it, not a
        # licence to climb past what any enemy is allowed to refuse.
        "armor": min(BOSS_ARMOR_CAP, max(_corridor_armor(floor, value, 0), value // 4)),
        "level": floor + 3,
        "reward": reward_for(floor, boss=False),
    }


def roll_chest(floor: int, rng=None) -> dict | None:
    """Decide what, if anything, is standing between two floors.

    Returns None nine times out of ten. Unseeded by default: this is rolled once when a
    descent happens and never replayed, so there is nothing to reproduce -- and a seed
    shared across descents is exactly how every chest on a floor became the same chest.
    """
    rng = rng or random.SystemRandom()
    if rng.random() >= CHEST_CHANCE:
        return None
    return {"kind": "mimic" if rng.random() < MIMIC_SHARE else "chest", "floor": max(1, int(floor))}


# What is actually inside the two boxes. Kept here, next to the odds, so the whole find is
# one readable table rather than a chance in this module and a payout in pets.py.
#
# A plain chest is the smaller, certain half: a cursed item, a handful of diamonds, a
# couple of kills' worth of coin and one rune. A beaten mimic is the same list paid better
# -- it cost health and a real fight, so it has to beat the box that cost neither.
MIMIC_GOLD_MULTIPLIER: Final = 1.6
MIMIC_RUBY_RANGE: Final = (2, 4)
MIMIC_CURSED_ITEMS: Final = 2
# On top of the cursed pair: one roll on the ordinary drop table, where the rare and
# legendary gear lives. This is the line that makes fighting a mimic worth the bite.
MIMIC_DROP_ROLLS: Final = 1
# Tickets for the meadow (see pets_meadow). Read from pets_config so the two faucets --
# a finished farm shift and a dungeon box -- are tuned side by side in one place.
MEADOW_TICKET_CHANCE: Final = C.MEADOW_TICKET_DUNGEON_CHANCE
MEADOW_TICKET_MIMIC_COUNT: Final = C.MEADOW_TICKET_MIMIC_COUNT


def chest_loot(floor: int, rng=None) -> dict:
    """What a plain chest holds. Never empty -- the empty one is the mimic's joke."""
    rng = rng or random.SystemRandom()
    return {
        "gold": chest_gold(floor), "rubies": rng.randint(*CHEST_RUBY_RANGE),
        "cursed": 1, "drops": 0, "runes": 1,
        # Meadow tickets are the only way onto the лотто field, and the dungeon is one of
        # its two faucets. Rolled here rather than granted flat so a chest stays a roll.
        "meadow_tickets": 1 if rng.random() < MEADOW_TICKET_CHANCE else 0,
    }


def mimic_loot(floor: int, rng=None) -> dict | None:
    """What a beaten mimic was guarding, or None when it was guarding nothing.

    The None is MIMIC_EMPTY_SHARE of the time and is the whole reason the fight is a
    decision: a mimic that always paid would simply be a chest with extra steps.
    """
    rng = rng or random.SystemRandom()
    if rng.random() < MIMIC_EMPTY_SHARE:
        return None
    return {
        "gold": max(1, round(chest_gold(floor) * MIMIC_GOLD_MULTIPLIER)),
        "rubies": rng.randint(*MIMIC_RUBY_RANGE),
        "cursed": MIMIC_CURSED_ITEMS, "drops": MIMIC_DROP_ROLLS, "runes": 1,
        # A beaten mimic pays more of everything it was guarding, tickets included, and
        # unlike the plain chest it never pays zero of them -- that is the reward for
        # having fought the lid instead of walking away from it.
        "meadow_tickets": MEADOW_TICKET_MIMIC_COUNT,
    }


def mimic_bite(max_hp: int) -> int:
    """Damage the lid does before anybody has decided anything.

    A share of MAX health, so it stings the same at every depth. Floored at 1 so a bite is
    never free; the caller is the one that keeps it from being lethal, because whether a
    run may end is a rule about the run and not about the tooth.
    """
    max_hp = max(1, int(max_hp or 1))
    return max(1, round(max_hp * MIMIC_BITE_SHARE))
