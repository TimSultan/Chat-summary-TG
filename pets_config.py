"""Every number the pet game can be re-balanced with, in one file.

The point of this module is that re-tuning the economy is editing constants here and
nothing else -- no formula is written twice anywhere in pets.py, pets_combat.py or the
bot wiring, and no magic number lives outside this file. Adding an item is appending to
ITEMS. Making levels cheaper is one exponent. Making fights shorter is one damage base.

Nothing here imports anything from the game, so a balance session can be run straight
from a REPL:

    >>> import pets_config as C
    >>> C.total_stat_cost(60)          # gold to take one stat from 1 to 60
    3646
    >>> C.stat_upgrade_cost(40)        # gold for the single point 40 -> 41
    84

WHY THESE NUMBERS
-----------------
The wallet is the chat's existing coin ledger (economy.py), not a second currency, so
the earn rate is already measured rather than guessed: over a 34-day window the most
active members earned 60-233 coins/week, the p90 member ~55/week, the median ~3/week.
On top of that the arena pays WIN_GOLD per win, capped at DAILY_FIGHTS fights a day.

    active chatter          ~150/week
    5 fights/day at ~50%    ~790/week
    -------------------------------------
    combined                ~940/week, ~4,000/month

Against that, STAT_COST_EXPONENT = 1.2 puts one stat at 1 -> 80 at 6,896 gold and three
stats at 20,688 -- roughly five months for somebody who both chats and fights daily, and
level 40 in a stat (1,481 gold) inside the first two weeks. That is the shape asked for:
an active member reaches a high level in three stats, and nobody maxes all four quickly.

The one thing worth knowing before re-tuning: the arena is ~85% of the faucet, so
chatting matters much less than fighting. If that should change, the honest lever is
DAILY_FIGHTS and WIN_GOLD, not the stat costs -- lowering the costs makes the arena even
more dominant rather than less. See PETS_BALANCE.md.
"""

# --------------------------------------------------------------------------- currency
# The pet game spends the SAME coins /stat and /shop already show. That is deliberate:
# it makes chat activity fund the game (which is what was asked for) and finally gives
# the coin economy a real sink -- see economy.py's note that, with one rentable title as
# the only drain, balances only grow.

CAGE_PRICE = 100            # buying the cage at all -- the entry ticket
TAME_PRICE = 100            # taming the creature that lives in it
# Renaming is free, and making it cost something is NOT just a matter of raising this:
# pets.rename does not take the member's xp, and economy.balance needs it to price
# anything at all. Charging for a rename means widening that signature first. Left here as
# the marker for where the decision lives.
RENAME_PRICE = 0

# --------------------------------------------------------------------------- the cage
# The cage was asked for as "buy, then upgrade" without saying what an upgrade does, so
# it is the game's convenience track: each level buys one more fight per day and a cut
# of the winnings. Levels are cumulative -- CAGE_LEVELS[n] is what level n+1 costs.

CAGE_MAX_LEVEL = 5
CAGE_UPGRADE_COSTS = (0, 400, 1_200, 3_000, 7_000)   # index = level - 1
CAGE_BONUS_FIGHTS = (0, 1, 2, 3, 4)                  # extra fights/day at that level
CAGE_GOLD_BONUS_PCT = (0, 5, 10, 15, 25)             # % more gold from a win

# ---------------------------------------------------------------------- stat upgrades
# cost(L -> L+1) = round(STAT_COST_BASE * L ** STAT_COST_EXPONENT), so the first point
# costs exactly 1 gold ("каждый поинт стоит 1 голды") and the 79th costs 189. The
# exponent is the single knob: 1.0 is linear and cheap, 1.5 makes level 80 a fantasy.
#
#     exponent   1 stat -> 40   1 stat -> 80   3 stats -> 80
#     1.0                 780          3,160           9,480
#     1.2  (chosen)     1,481          6,896          20,688
#     1.35              2,437         12,446          37,338
#     1.5               4,005         22,557          67,671

STAT_MIN_LEVEL = 1
STAT_MAX_LEVEL = 80
STAT_COST_BASE = 1.0
STAT_COST_EXPONENT = 1.2

STAT_KEYS = ("strength", "health", "agility", "luck")
STAT_NAMES = {
    "strength": "Сила",
    "health": "Здоровье",
    "agility": "Ловкость",
    "luck": "Удача",
}
STAT_EMOJI = {
    "strength": "⚔️",
    "health": "❤️",
    "agility": "💨",
    "luck": "🍀",
}
# Armor is NOT purchasable -- it exists only on equipment, which is what makes the
# inventory worth having rather than a second stat screen.
ARMOR_NAME = "Броня"
ARMOR_EMOJI = "🛡"


def stat_upgrade_cost(level: int) -> int:
    """Gold to go from `level` to `level + 1`. Never free, never fractional."""
    if level < STAT_MIN_LEVEL or level >= STAT_MAX_LEVEL:
        return 0
    return max(1, round(STAT_COST_BASE * level ** STAT_COST_EXPONENT))


def total_stat_cost(target_level: int, from_level: int = STAT_MIN_LEVEL) -> int:
    """Gold to walk one stat from `from_level` up to `target_level`."""
    return sum(stat_upgrade_cost(level) for level in range(from_level, target_level))


# --------------------------------------------------------------------------- combat
# Tuned so a fight is 6-12 rounds at EVERY level, which is what keeps the battle log
# readable: a level-1 pair trade ~11 blows, a maxed pair ~8. Both HP and damage scale,
# damage slightly faster, so high-level fights are not slower slugfests.
#
#   HP     = BASE_HP + health * HP_PER_POINT          (500 + 13/pt -> 1,540 at 80)
#   damage = BASE_DAMAGE + strength * DAMAGE_PER_POINT (45 + 2.2/pt ->   221 at 80)

BASE_HP = 500               # "все начинают с 500"
HP_PER_POINT = 13
BASE_DAMAGE = 45
DAMAGE_PER_POINT = 2.2
# Every blow is nudged by +-15% so two identical pets do not play out identically.
DAMAGE_VARIANCE = 0.15

# Dodge, crit and armor all use the same saturating curve,
#
#     chance = MAX * stat / (stat + K)
#
# rather than anything linear, because a linear chance either does nothing at level 5 or
# reaches 100% before level 80. K is the stat value at which half the maximum is reached.
DODGE_MAX = 0.45
DODGE_K = 55.0              # agility 40 -> 19%, agility 80 -> 27%

CRIT_BASE = 0.03            # everybody lands the occasional lucky one
CRIT_MAX = 0.35
CRIT_K = 70.0               # luck 40 -> 16%, luck 80 -> 22%
CRIT_MULTIPLIER = 2.0       # "критического удара (х2)"

ARMOR_MAX = 0.60            # hard ceiling on damage reduction, so armor can never zero a hit
ARMOR_K = 100.0             # armor 60 -> 22.5%, armor 150 -> 36%

# A fight that somehow refuses to end is stopped and awarded on remaining HP share. With
# the numbers above this never triggers; it exists so a future item can never hang a bot
# thread in a while-loop.
MAX_ROUNDS = 40

# ------------------------------------------------------------------ dominance bonus
# "Если у героев значения каждых отдельных статов разнятся в пропорции 30% - то они
# начинают давать на 30% больше бонусов." Compared per stat, at the start of the fight,
# on EFFECTIVE stats (levels + pet level + equipment). Only the stat-derived part of a
# value is boosted -- BASE_HP and BASE_DAMAGE are floors everybody gets, not a reward for
# out-scaling somebody.
DOMINANCE_RATIO = 1.30      # how far ahead counts as dominant
DOMINANCE_BONUS = 0.30      # how much more that stat then gives

# ------------------------------------------------------------------------ the arena
DAILY_FIGHTS = 5            # before cage bonuses; CAGE_BONUS_FIGHTS adds to this
OPPONENT_LEVEL_WINDOW = 3   # "поиск по +- 3 уровня"
# Widened one step at a time if nobody is in range, so a chat with four pets can still
# fight. Set to () to make the window hard.
OPPONENT_WINDOW_FALLBACKS = (6, 12, None)   # None = anybody at all

WIN_GOLD_MIN = 30           # "случайно 30-60 голды"
WIN_GOLD_MAX = 60
LOSS_GOLD = 0               # "проигравший ничего не теряет пока"

WIN_XP = 100
LOSS_XP = 35                # a loss still teaches something, so nobody dodges hard fights
HISTORY_LIMIT = 10          # "список последних 10 боев"

# --------------------------------------------------------------------- pet levelling
# Pet levels are separate from stat levels: "у существ отдельный свой опыт и уровни. За
# каждый уровень существо получает +1 ко всем статам." That +1 is free and stacks ON TOP
# of the purchased cap, so a level-30 pet with a maxed stat is at an effective 110.
#
# xp(L -> L+1) = round(PET_XP_BASE * L ** PET_XP_EXPONENT), and WIN_XP is 100, so the
# curve reads directly in wins: level 10 at ~25 wins, level 20 at ~93, level 30 at ~196,
# level 50 at ~499. The exponent is below 1 on purpose -- the +1-to-everything per level
# is already the strongest thing in the game, so the curve only has to be long, not
# vertical.

PET_MAX_LEVEL = 50
PET_XP_BASE = 80.0
PET_XP_EXPONENT = 0.8
PET_LEVEL_STAT_BONUS = 1    # +1 to every stat per pet level


def pet_xp_for_next_level(level: int) -> int:
    """XP needed to go from `level` to `level + 1`. 0 once PET_MAX_LEVEL is reached."""
    if level < 1 or level >= PET_MAX_LEVEL:
        return 0
    return max(1, round(PET_XP_BASE * level ** PET_XP_EXPONENT))


# ------------------------------------------------------------------------ inventory
# Four slots, as asked. The catalogue is deliberately thin -- "доступные список добавим
# позже с ценами" -- but the shape is fixed, so adding an item later is one more Item()
# and nothing else: no new slot logic, no new stat plumbing, no migration.
#
# `source` says where an item comes from. "shop" items are buyable from the pet menu;
# "drop" items cannot be bought at any price and only fall out of arena wins. Anything
# with a price of 0 and source "drop" is a trophy.

SLOT_KEYS = ("weapon", "amulet", "gloves", "boots")
SLOT_NAMES = {
    "weapon": "Оружие",
    "amulet": "Амулет",
    "gloves": "Перчатки",
    "boots": "Сапоги",
}
SLOT_EMOJI = {
    "weapon": "🗡",
    "amulet": "📿",
    "gloves": "🧤",
    "boots": "👢",
}


class Item:
    """One equippable thing. `bonuses` maps any of STAT_KEYS or "armor" to a flat add."""

    __slots__ = ("code", "name", "slot", "price", "source", "bonuses", "description")

    def __init__(self, code, name, slot, price, source, bonuses, description=""):
        self.code = code
        self.name = name
        self.slot = slot
        self.price = price
        self.source = source
        self.bonuses = dict(bonuses)
        self.description = description


# Starter catalogue. Prices sit between a few wins and a few weeks so that gear is a
# real alternative to stat points rather than an afterthought, and every item is a
# trade-off -- there is no strictly-best weapon.
ITEMS = (
    Item("stick", "Палка судьбы", "weapon", 250, "shop", {"strength": 6},
         "Обычная палка. Судьба у неё так себе."),
    Item("fork", "Вилка титана", "weapon", 900, "shop", {"strength": 14, "luck": 4},
         "Три зубца, один смысл."),
    Item("bone", "Кость прадеда", "weapon", 0, "drop", {"strength": 20, "agility": -3},
         "Тяжёлая. Зато фамильная."),
    Item("bead", "Бусина", "amulet", 200, "shop", {"luck": 8},
         "Блестит. Этого достаточно."),
    Item("acorn", "Жёлудь удачи", "amulet", 1_100, "shop", {"luck": 16, "health": 5},
         "Пахнет осенью и лёгкой победой."),
    Item("mittens", "Варежки", "gloves", 220, "shop", {"armor": 20},
         "Бабушка вязала. Держат удар."),
    Item("claws", "Когти", "gloves", 1_000, "shop", {"agility": 10, "armor": 25},
         "Царапают всё, включая владельца."),
    Item("slippers", "Тапки", "boots", 180, "shop", {"agility": 7},
         "Домашние. Внезапно быстрые."),
    Item("springs", "Пружины", "boots", 1_000, "shop", {"agility": 15, "armor": 10},
         "Прыгучесть выше, достоинство ниже."),
)

# How often a win drops an item at all, and from which pool.
DROP_CHANCE = 0.08


def find_item(code: str):
    needle = (code or "").strip().lower()
    return next((item for item in ITEMS if item.code == needle), None)


def items_for_slot(slot: str, source: str | None = None):
    return [
        item for item in ITEMS
        if item.slot == slot and (source is None or item.source == source)
    ]
