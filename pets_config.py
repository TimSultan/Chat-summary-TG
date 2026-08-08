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
The wallet is the chat's existing coin ledger (economy.py), not a second currency, so the
earn rate is already measured rather than guessed: over a 34-day window the most active
members earned 60-233 coins/week, the p90 member ~55/week, the median ~3/week.

Arena income is NOT flat. How many fights somebody gets is earned from what they did in
the chat yesterday (see daily_fight_allowance), and losing costs a share of what winning
pays (LOSS_GOLD_SHARE), so part of every fight is paid by a player rather than minted.
Both together are what make chat activity actually decide income:

    profile      msgs/day  fights  arena/wk  +chat  total/wk  3 stats -> 80
    lurker              1       2        52      3        55      7.2 years
    median             14       3        79     25       104      3.8 years
    p75                41       5       131     80       211      1.9 years
    p90                89       9       236    150       386      1.0 years
    p95               166      12       315    233       548      8 months

Against that, STAT_COST_EXPONENT = 1.2 puts one stat at 1 -> 80 at 6,896 gold and three
at 20,688. Arena gold stays comparable to, rather than overpowering, coins earned from
ordinary chat activity; cage bonuses remain the game-specific progression path.

Worth knowing before re-tuning: this replaced a flat 5 fights a day with a free loss,
which paid everybody ~1,575/week regardless of whether they ever wrote a word -- a 1.3x
spread between a lurker and the chat's busiest member. It is now 6.2x. If it should move
further, the levers are BASE_DAILY_FIGHTS and WIN_GOLD_*, not the stat costs: making
levels cheaper raises everybody equally and widens nothing. See PETS_BALANCE.md.
"""

# --------------------------------------------------------------------------- currency
# The pet game spends the SAME coins /stat and /shop already show. That is deliberate:
# it makes chat activity fund the game (which is what was asked for) and finally gives
# the coin economy a real sink -- see economy.py's note that, with one rentable title as
# the only drain, balances only grow.

CAGE_PRICE = 100            # buying the cage at all -- the entry ticket
LEGACY_CAGE_PRICE = 50      # one-time refund for cages bought before the price change
TAME_PRICE = 50             # taming the creature that lives in it
# Renaming is free, and making it cost something is NOT just a matter of raising this:
# pets.rename does not take the member's xp, and economy.balance needs it to price
# anything at all. Charging for a rename means widening that signature first. Left here as
# the marker for where the decision lives.
RENAME_PRICE = 0

# --------------------------------------------------------------------------- the cage
# The cage was asked for as "buy, then upgrade" without saying what an upgrade does, so
# it is the game's convenience track: each level buys one more fight per day and a cut
# of the winnings. Levels are cumulative -- CAGE_LEVELS[n] is what level n+1 costs.
# The price is deliberately FLAT rather than escalating: the original curve
# (400/1_200/3_000/7_000) put max cage out of reach of anyone but the top earners, and a
# flat 100 was asked for instead, making the whole ladder cost 400 to climb.

CAGE_MAX_LEVEL = 5
CAGE_UPGRADE_COSTS = (0, 100, 100, 100, 100)         # index = level - 1
# Flat one-time payout to everyone who ever bought an upgrade, regardless of how many
# levels they bought -- a goodwill refund that was asked for as a single per-owner sum,
# not a replay of what each of them actually paid out of CAGE_UPGRADE_COSTS.
CAGE_UPGRADE_REFUND = 350
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
# Tuned so an even fight naturally runs for ~20 blows -- about ten from each side -- at
# EVERY level. `MAX_ATTACKS_PER_FIGHTER` makes ten attacks each a hard ceiling; a fight
# that reaches it is awarded by total damage rather than continuing past the limit.
#
#   HP     = BASE_HP + health * HP_PER_POINT             (500 + 19/pt -> 2,020 at 80)
#   damage = BASE_DAMAGE + strength * DAMAGE_PER_POINT  (49.5 + 2.42/pt -> 243 at 80)
#
# The ratio is what matters, not either number alone. Blows to drop somebody is
# HP / (damage * (1 - their dodge) * (1 + their crit rate)), and since dodge and crit both
# grow with level, HP has to grow FASTER than damage just to stay level -- which is why
# HP_PER_POINT (19) is far above DAMAGE_PER_POINT (2.42) relative to their bases.
# Measured medians, 200 seeded fights per level: 10/10/10 blows per side at levels 1/40/80.
# Armour from gear can make a fight reach the cap, but cannot give either pet an eleventh
# attack.

BASE_HP = 500               # "все начинают с 500"
HP_PER_POINT = 19
BASE_DAMAGE = 49.5
DAMAGE_PER_POINT = 2.42
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

# A fighter whose strongest comparable stat is 2x or 3x their opponent's gets one
# signature moment per fight at most. It is intentionally one stat, not a stack: a pet
# that outscales in several places is formidable without becoming an automatic victory.
STAT_ADVANTAGE_RATIO = 2.0
STAT_OVERWHELMING_RATIO = 3.0
SIGNATURE_TRIGGER_CHANCES = {
    "strength": (0.0, 0.0, 0.20, 0.35),
    "health": (0.0, 0.0, 1.00, 1.00),
    "agility": (0.0, 0.0, 0.25, 0.45),
    "luck": (0.0, 0.0, 0.10, 0.20),
    "armor": (0.0, 0.0, 0.25, 0.35),
}

# Luck keeps a high crit rate, but the old accident was an instant win. Its signature
# now deals only 40% of the opponent's current health as an opening hit; at 3x Luck its
# dodge is deliberately low, so crit, dodge, and an instant kill cannot stack.
LUCK_ADVANTAGE_CRIT_BONUS = 0.25       # percentage points, not a multiplier
LUCK_ADVANTAGE_MISS_MULTIPLIER = 0.75  # the lucky attacker's misses are reduced
LUCK_OVERWHELMING_CRIT_CHANCE = 0.30
LUCK_OVERWHELMING_DODGE_CHANCE = 0.05
LUCK_OPENING_DAMAGE_SHARE = 0.40

ARMOR_MAX = 0.60            # hard ceiling on damage reduction, so armor can never zero a hit
ARMOR_K = 100.0             # armor 60 -> 22.5%, armor 150 -> 36%

# Each pet gets at most this many attacks. If neither side is knocked out by then, the
# living pet that dealt more total damage wins; see pets_combat.simulate.
MAX_ATTACKS_PER_FIGHTER = 10

# ------------------------------------------------------------------ stat lead bonus
# Every lead matters: a stat that is 10% higher contributes 10% more, rising linearly to
# the 30% cap once it is 30% ahead. Compared per stat at the start of the fight, on
# EFFECTIVE stats (levels + pet level + equipment). Only the stat-derived part of a value
# is boosted -- BASE_HP and BASE_DAMAGE are floors everybody gets, not a reward for
# out-scaling somebody.
DOMINANCE_RATIO = 1.30      # ratio where the gradual bonus reaches its ceiling
DOMINANCE_BONUS = 0.30      # maximum bonus to one stat's contribution

# ------------------------------------------------------------------------ the arena
#
# How many fights a day somebody gets is EARNED, not granted flat:
#
#     allowance = BASE_DAILY_FIGHTS
#               + messages_yesterday  * FIGHTS_PER_MESSAGE
#               + figurines_yesterday * FIGHTS_PER_FIGURINE
#               + CAGE_BONUS_FIGHTS[cage_level - 1]
#
# floored to a whole number and capped at MAX_DAILY_FIGHTS. Yesterday rather than today
# because a closed day is a finished, recorded fact -- pricing off a day still in progress
# would mean the allowance moved every time somebody typed, and a fight taken at noon
# could be un-taken by evening.
#
# Calibrated against the real chat, not guessed. Measured over 162 user-days in
# cache/stats: the median poster writes 14 messages a day, p75 writes 41, p90 writes 89,
# p95 writes 166, and the busiest single day by one person was 412. So at 8% a message:
#
#     lurker (0-1 msgs)   2 fights      p90  (89 msgs)    9 fights
#     median (14 msgs)    3 fights      p95  (166 msgs)  12 fights (the cap)
#     p75    (41 msgs)    5 fights      busiest (412)    12 fights (the cap)
#
# The rate is 8% rather than something rounder because of the median specifically: at 6%
# the median poster earned 0.84 of a fight, floored to zero, and got exactly what somebody
# who never wrote anything got. A rate that cannot tell the typical member apart from a
# lurker is not doing the job this formula exists for.
#
# The cap exists because the top of this distribution is very long -- uncapped, the busiest
# poster would open with 35 fights a day and out-earn everybody else on volume alone.
BASE_DAILY_FIGHTS = 2
FIGHTS_PER_MESSAGE = 0.08
# A painted figurine is the rarest and most valued thing anybody posts here, so it is
# worth roughly eight messages.
FIGHTS_PER_FIGURINE = 0.5
MAX_DAILY_FIGHTS = 12
ARENA_SAME_OPPONENT_DAILY_LIMIT = 3

# Matchmaking uses effective combat stats, including equipment and pet level, rather than
# a level window that could pair two very differently geared creatures.
POWER_RATING_BASE = 100
POWER_RATING_WEIGHTS = {
    "strength": 4,
    "health": 4,
    "agility": 2,
    "luck": 2,
    "armor": 3,
}
OPPONENT_POWER_WINDOW = 125
MAX_OPPONENT_REROLLS = 3

WIN_GOLD_MIN = 5            # "случайно 5-10 голды"
WIN_GOLD_MAX = 10
# The loser pays 30% of what the winner just took. This replaces the original "проигравший
# ничего не теряет": with a free loss, the best strategy was to press "напасть" without
# reading anything, and a fight nobody can lose is not a fight.
#
# It applies to whoever loses, INCLUDING a defender who never chose the fight. That is
# safe here specifically because opponents are drawn at random within a level window --
# nobody can pick a target, so there is no way to farm one person down. If matchmaking
# ever lets somebody choose, this rule has to be revisited at the same time.
#
# It reduces the faucet without making a passive defender lose too much gold.
LOSS_GOLD_SHARE = 0.3
# A debt is never created: somebody with less than this in their wallet simply pays what
# they have. economy.balance clamps at zero anyway, and a member who cannot see why they
# owe money is worse than one who got off lightly.

WIN_XP = 100
LOSS_XP = 35                # a loss still teaches something, so nobody dodges hard fights
DRAW_XP = 50                # both sides spent a fight; no gold or win is awarded
HISTORY_LIMIT = 10          # "список последних 10 боев"
DUEL_DAILY_LIMIT = 5
DUEL_COOLDOWN_SECONDS = 10 * 60
DUEL_SAME_OPPONENT_DAILY_LIMIT = 1


def daily_fight_allowance(messages: int = 0, figurines: int = 0, cage_level: int = 1) -> int:
    """How many fights one member gets today, from what they did in the chat YESTERDAY.

    Floored rather than rounded: half a fight is not a fight, and rounding up would hand a
    lurker who wrote three messages the same allowance as somebody who wrote ten.
    """
    earned = (
        BASE_DAILY_FIGHTS
        + max(0, messages) * FIGHTS_PER_MESSAGE
        + max(0, figurines) * FIGHTS_PER_FIGURINE
    )
    level = min(max(cage_level, 1), CAGE_MAX_LEVEL)
    return min(MAX_DAILY_FIGHTS + CAGE_BONUS_FIGHTS[level - 1], int(earned) + CAGE_BONUS_FIGHTS[level - 1])


def loss_gold_for(won_gold: int) -> int:
    """What the loser of a fight pays, given what the winner took."""
    return max(0, round(won_gold * LOSS_GOLD_SHARE))

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
    Item("stick", "Кисть-щетина №8", "weapon", 250, "shop", {"strength": 6},
        "Жёсткая, уверенная, для смелых мазков."),
    Item("fork", "Аэрограф Harder & Steenbeck", "weapon", 900, "shop", {"strength": 14, "luck": 4},
        "Ровный факел краски и немного магии в триггере."),
    Item("bone", "Компрессор старого мастера", "weapon", 0, "drop", {"strength": 20, "agility": -3},
        "Тяжёлый, гудит и выдаёт идеальное давление."),
    Item("bead", "Флакон Nuln Oil", "amulet", 200, "shop", {"luck": 8},
        "Одна капля на модель, другая непременно на стол."),
    Item("acorn", "Набор Scale75 Artist", "amulet", 1_100, "shop", {"luck": 16, "health": 5},
        "Пигмент настолько плотный, что вдохновляет на подвиги."),
    Item("mittens", "Нитриловые перчатки", "gloves", 220, "shop", {"armor": 20},
        "Защищают лапы от краски, грунта и внезапных проливов."),
    Item("claws", "Перчатки сухой кисти", "gloves", 1_000, "shop", {"agility": 10, "armor": 25},
        "Пыльные, ловкие и привычные к самым острым граням."),
    Item("slippers", "Тапки из малярного скотча", "boots", 180, "shop", {"agility": 7},
        "Лёгкие и липкие: ни одна база не убежит."),
    Item("springs", "Ботинки с банками Vallejo", "boots", 1_000, "shop", {"agility": 15, "armor": 10},
        "Шуршат шариками внутри и ускоряют путь к столу."),
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
