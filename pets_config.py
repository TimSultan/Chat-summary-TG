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

Arena income is paced by one accumulated fight per hour and a five-fight base capacity.
The cage, farm and recent painted miniatures expand that bank (see
daily_fight_allowance); losing transfers a share of the loser's current wallets to the
winner, so every duel has a stake beyond its minted purse.

Against that, STAT_COST_EXPONENT = 1.5 puts one stat at 1 -> 80 at 22,557 gold and three
at 67,671. Arena gold stays comparable to, rather than overpowering, coins earned from
ordinary chat activity; cage bonuses remain the game-specific progression path.

If it should move further, the levers are BASE_FIGHT_BANK_CAPACITY,
FIGHT_BANK_RECHARGE_SECONDS and WIN_GOLD_*, not the stat costs: making levels cheaper
raises everybody equally. See PETS_BALANCE.md.
"""

import hashlib
import math
from datetime import date as _date, datetime as _datetime
from zoneinfo import ZoneInfo as _ZoneInfo

# --------------------------------------------------------------------------- currency
# The pet game spends the SAME coins /stat and /shop already show. That is deliberate:
# it makes chat activity fund the game (which is what was asked for) and finally gives
# the coin economy a real sink -- see economy.py's note that, with one rentable title as
# the only drain, balances only grow.

CAGE_PRICE = 100            # buying the cage at all -- the entry ticket
LEGACY_CAGE_PRICE = 50      # one-time refund for cages bought before the price change
TAME_PRICE = 0              # every player can create their first creature for free
# Renaming is free, and making it cost something is NOT just a matter of raising this:
# pets.rename does not take the member's xp, and economy.balance needs it to price
# anything at all. Charging for a rename means widening that signature first. Left here as
# the marker for where the decision lives.
RENAME_PRICE = 0

# ---------------------------------------------------------------- level-scaled payouts
# Stat prices grow as level^1.2 while several reward faucets used to stay flat forever.
# The common square-root curve grows forever but more slowly than the main stat sink. A
# source weight says how much of that curve it needs: arena/PVE had no scaling at all,
# while dungeon floors and farm buildings already do much of the work themselves.
HERO_GOLD_SQRT_BONUS = 0.45
HERO_GOLD_FREE_LEVEL = 5
HERO_GOLD_SOURCE_WEIGHTS = {
    "arena": 1.00,
    "pve": 1.00,
    "quest": 0.65,
    "quarry": 0.45,
    "farm": 0.68,
    "passive": 0.35,
    "dungeon": 0.20,
    "birthday": 0.50,
}


def hero_gold_multiplier(hero_level: int = 1, source: str = "arena") -> float:
    """Unbounded, diminishing gold growth for one reward source.

    Levels one through five remain byte-for-byte unchanged. Even at very high levels the
    multiplier grows slower than stat costs, so progression becomes longer without
    becoming stuck.
    """
    level = max(1, int(hero_level or 1))
    weight = float(HERO_GOLD_SOURCE_WEIGHTS.get(source, 1.0))
    growth = max(0.0, math.sqrt(level) - math.sqrt(HERO_GOLD_FREE_LEVEL))
    return 1.0 + HERO_GOLD_SQRT_BONUS * growth * weight


def gold_for_hero(base_gold: int | float, hero_level: int = 1, source: str = "arena") -> int:
    """Scale a non-negative base payout and keep zero as zero."""
    base = max(0.0, float(base_gold or 0))
    return 0 if base <= 0 else max(1, round(base * hero_gold_multiplier(hero_level, source)))

# --------------------------------------------------------------------------- the cage
# The cage was asked for as "buy, then upgrade" without saying what an upgrade does, so
# it is the game's convenience track: each level buys one more fight-bank slot and a cut
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
CAGE_BONUS_FIGHTS = (0, 1, 2, 3, 4)                  # extra bank capacity at that level
CAGE_GOLD_BONUS_PCT = (0, 5, 10, 15, 25)             # % more gold from a win

# Historic prices are retained only for the one-time retirement refund. There is no
# longer a separate passive-income building or any way to buy these levels.
LEGACY_HAMSTERATOR_UPGRADE_COSTS = (250, 750, 1_500, 3_000, 6_000)

# ------------------------------------------------------------------------------ farm
# A farm run is now a deliberate, player-chosen 1-8 hour shift: the pet cannot start a
# fight while it works (but, unlike before, CAN still be attacked -- see _is_farming_record
# call sites in claim_duel/can_attack_in_arena/find_opponent/record_fight), so the reward
# needs to be useful without replacing the hourly arena loop.  A fully developed farm
# costs 6,785 coins (levels + permanent facilities), and should pay that back in a few
# days of deliberate shifts rather than in a month.  It also needs to keep mattering after
# a pet outgrows the starter economy, so the shift purse scales gently with pet level.
FARM_MAX_LEVEL = 10
# FARM_DURATION_HOURS is kept as the balance ANCHOR, not a default a player is steered
# toward: FARM_GOLD_PER_RUN/FARM_XP_PER_RUN/FARM_DROP_CHANCE_BY_HOURS are all stated "per
# six hours", and any farm_run persisted before this feature shipped never recorded an
# explicit hours field -- it was always exactly six, so that is what a missing field means.
FARM_DURATION_HOURS = 6
FARM_MIN_HOURS = 1
FARM_MAX_HOURS = 8
FARM_HOUR_CHOICES = tuple(range(FARM_MIN_HOURS, FARM_MAX_HOURS + 1))
# The backend still accepts every whole duration from 1 through 8 (including old
# Telegram callbacks), but the two current interfaces deliberately present only four
# useful presets instead of an eight-button wall.
FARM_QUICK_HOUR_CHOICES = (1, 2, 4, 8)
# Index = hours. 6 h is the balance anchor at 1.00 so today's numbers are unchanged there;
# shorter shifts pay slightly less per hour (less to supervise, less risk of the pet being
# unavailable), longer shifts pay slightly more (tying up the pet -- and the arena-bank
# slot it can't fill in the meantime -- for most of a day earns a premium). Index 0 is
# unused padding so FARM_DURATION_BONUS[hours] reads directly off the hour count.
FARM_DURATION_BONUS = (0.0, 0.85, 0.88, 0.91, 0.94, 0.97, 1.00, 1.06, 1.15)
# Index is the current level; index 0 builds the first level.
#
# Building at all costs 10 -- a token, not a gate. It was 75, which on top of the cage
# (100) and taming (50) meant a new player spent 225 before the farm existed for them, and
# the farm is the one part of the game that pays out while you are not playing it: the
# thing a newcomer should reach first, not last. Every level after it keeps its old price,
# so the ladder is unchanged for anybody already climbing it.
FARM_UPGRADE_COSTS = (10, 100, 150, 225, 325, 450, 625, 850, 1_150, 1_500)
# The difference, paid back once to everybody who already built at 75. See
# pets.refund_farm_builds -- and note this is the gap, not the old price: they keep the
# farm they paid for, they just end up having paid today's price for it.
FARM_BUILD_REFUND = 75 - FARM_UPGRADE_COSTS[0]
# Six-hour REFERENCE payouts -- see farm_gold_for/farm_xp_for for how an actual `hours`
# length is derived from them.  Level one pays for a basic shop weapon in one shift;
# level ten with every facility produces about 1,530 coins/day before the pet-level
# bonus.  The active shift locks the pet out of starting arena/PvE fights, so this is
# meaningful income, not a free background faucet.
# Roughly doubled at the top, but NOT at the bottom, and the difference is the point.
# The farm was 2.0% of a day's gold -- a station with ten upgrade levels and its own tools
# that moved nothing. Raising it flat would have broken a pacing rule worth keeping: one
# first shift must not pay for a weapon (the cheapest is 60), or the opening hour hands
# out gear the player has not chosen anything to earn. So level 1 stays under that line
# and the curve steepens instead -- 1 -> 10 was x5.2, it is now x9.4, which pays for
# upgrading the farm rather than merely owning one.
FARM_GOLD_PER_RUN = (0, 50, 70, 95, 125, 160, 205, 255, 315, 385, 470)
FARM_XP_PER_RUN = (0, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95)

# ------------------------------------------------------------------------ поляна
#
# The meadow is a ticket sink, not a coin sink: tickets are the only way in, and they drop
# from finished farm shifts and from the dungeon. That keeps the diamond faucet tied to
# time already spent playing rather than to a wallet, and it is why the drop chances below
# are the real balance lever -- the board's payout is fixed and public.
#
# Rates are per completed farm shift and per dungeon chest/mimic. A six-hour shift landing
# a ticket about one time in three means roughly one small entry a day for somebody who
# farms twice, which is the cadence this is priced for.
MEADOW_TICKET_FARM_CHANCE = 0.34
MEADOW_TICKET_DUNGEON_CHANCE = 0.50
# A mimic already pays more of everything else it guards; tickets follow the same rule.
MEADOW_TICKET_MIMIC_COUNT = 2
# A farm's buildings stop at level 10 while pet levels deliberately do not. It uses the
# shared diminishing hero curve at the farm weight: stronger than the old logarithmic
# bonus at established levels, but still slower than the level^1.2 stat-cost sink:
#
#   pet level    1     10     50     100
#   multiplier 1.00   1.66   2.48    3.37
#
# It is snapshotted when the shift starts, just like farm level and facilities.
FARM_PET_LEVEL_GOLD_LOG2_BONUS = 0.20
# Index = hours. Long shifts are now the farm's deliberate loot route: eight hours reaches
# 50%, while short shifts remain useful chiefly for coins and XP. Only 7-8 h can roll a
# legendary weapon (see FARM_LOOT_RARITY_WEIGHTS below).
FARM_DROP_CHANCE_BY_HOURS = (0.0, 0.01, 0.02, 0.04, 0.07, 0.12, 0.20, 0.32, 0.50)
# Rarity is now picked FIRST (from this hours-indexed table), and only then is an item of
# that rarity drawn from the eligible pool. Legendary is deliberately absent below 7 h --
# a farm shift is unattended and reservation-free, so the only lever keeping a legendary
# WEAPON rare is gating which shifts can roll for one at all, same spirit as arena's
# 500-win pity being a ceiling rather than the normal path.
FARM_LOOT_RARITY_WEIGHTS = {
    1: {"common": 100},
    2: {"common": 100},
    3: {"common": 97, "rare": 3},
    4: {"common": 94, "rare": 6},
    5: {"common": 90, "rare": 10},
    6: {"common": 85, "rare": 15},
    7: {"common": 78, "rare": 21, "legendary": 1},
    8: {"common": 69, "rare": 27, "legendary": 4},
}
# Farm levels also generate passive gold. The top stays at the former 5 coins/hour so
# merging the two buildings improves clarity without doubling the passive faucet.
FARM_PASSIVE_GOLD_PER_HOUR = (0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5)
FARM_PASSIVE_STORAGE_CAP = (0, 24, 36, 72, 96, 144, 180, 240, 288, 360, 480)
FARM_FEATURES = {
    # A well makes every harvest reliably more valuable.
    "well": {"name": "Колодец", "cost": 150, "gold_multiplier": 1.25},
    # A sprinkler gives practice value without inflating the coin faucet.
    "sprinkler": {"name": "Поливалка", "cost": 250, "xp_multiplier": 1.25},
    # Better beds are the only direct way to improve the small find chance.
    "beds": {"name": "Грядка", "cost": 400, "drop_bonus": 0.05},
    # The tractor helps both yields; it has no effect on how long a shift is, because
    # that choice now belongs to the player, not to any building.
    "tractor": {"name": "Трактор", "cost": 600, "gold_multiplier": 1.20, "xp_multiplier": 1.20},
}


def farm_pet_level_gold_multiplier(pet_level: int = 1) -> float:
    """Farm share of the common curve, never nerfing its previous logarithmic bonus."""
    level = max(1, int(pet_level or 1))
    legacy = 1.0 + FARM_PET_LEVEL_GOLD_LOG2_BONUS * math.log2(level)
    return max(legacy, hero_gold_multiplier(level, "farm"))


def farm_gold_for(
    level: int, hours: int, gold_multiplier: float = 1.0, pet_level: int = 1,
) -> int:
    """Gold for one farm shift, including its snapshotted pet-level bonus."""
    level = min(max(1, int(level)), FARM_MAX_LEVEL)
    hours = min(max(FARM_MIN_HOURS, int(hours)), FARM_MAX_HOURS)
    return max(1, round(
        FARM_GOLD_PER_RUN[level] * hours / FARM_DURATION_HOURS
        * FARM_DURATION_BONUS[hours] * gold_multiplier
        * farm_pet_level_gold_multiplier(pet_level)
    ))


def farm_xp_for(level: int, hours: int, xp_multiplier: float = 1.0) -> int:
    """Pet XP for one farm shift -- same shape as farm_gold_for, different table."""
    level = min(max(1, int(level)), FARM_MAX_LEVEL)
    hours = min(max(FARM_MIN_HOURS, int(hours)), FARM_MAX_HOURS)
    return max(1, round(
        FARM_XP_PER_RUN[level] * hours / FARM_DURATION_HOURS
        * FARM_DURATION_BONUS[hours] * xp_multiplier
    ))

# ---------------------------------------------------------------------- stat upgrades
# cost(L -> L+1) = round(STAT_COST_BASE * L ** STAT_COST_EXPONENT), so the first point
# costs exactly 1 gold ("каждый поинт стоит 1 голды"). There is no level ceiling; the
# exponent is the single knob that makes each next point progressively more expensive.
#
#     exponent   1 stat -> 40   1 stat -> 80   3 stats -> 80
#     1.0                 780          3,160           9,480
#     1.2               1,481          6,896          20,688
#     1.35              2,437         12,446          37,338
#     1.5  (chosen)     4,005         22,557          67,671
#
# Raised from 1.2 because the sink was the wrong SHAPE for the faucet, not merely too
# small: dungeon gold scales with floor and has no ceiling, while 1.2 is barely steeper
# than a straight line, so income outran the only thing that absorbed it. At 1.2 a
# mid-level player finished all six stats to 80 in about a day and a half and then had
# nowhere to put gold at all; at 1.5 that is roughly three weeks of ordinary play.

STAT_MIN_LEVEL = 1
STAT_MAX_LEVEL = None
STAT_COST_BASE = 1.0
STAT_COST_EXPONENT = 1.5
STAT_RESPEC_RUBY_COST = 15

STAT_KEYS = ("strength", "health", "agility", "luck", "magic", "endurance")
STAT_NAMES = {
    "strength": "Сила",
    "health": "Здоровье",
    "agility": "Ловкость",
    "luck": "Удача",
    "magic": "Магия",
    "endurance": "Выносливость",
}
STAT_EMOJI = {
    "strength": "⚔️",
    "health": "❤️",
    "agility": "💨",
    "luck": "🍀",
    "magic": "🔮",
    "endurance": "🫁",
}
# Armor is NOT purchasable -- it exists only on equipment, which is what makes the
# inventory worth having rather than a second stat screen.
ARMOR_NAME = "Броня"
ARMOR_EMOJI = "🛡"


def stat_upgrade_cost(level: int) -> int:
    """Gold to go from `level` to `level + 1`. Never free, never fractional."""
    if level < STAT_MIN_LEVEL:
        return 0
    return max(1, round(STAT_COST_BASE * level ** STAT_COST_EXPONENT))


def total_stat_cost(target_level: int, from_level: int = STAT_MIN_LEVEL) -> int:
    """Gold to walk one stat from `from_level` up to `target_level`."""
    return sum(stat_upgrade_cost(level) for level in range(from_level, target_level))


# --------------------------------------------------------------------------- combat
# Tuned so an even fight naturally runs for ~20 blows -- about ten from each side -- at
# EVERY level. `MAX_SKILL_ACTIONS_PER_FIGHTER` is the ceiling rather than the target: a
# fight that reaches it is awarded by total damage rather than continuing past the limit.
# Doubled from 24 to 48 on the owner's call: fights that went the distance were being
# settled by the total-damage tiebreak rather than by a knockout, and a longer rope lets
# more of them finish on their own terms. This is headroom only -- it does not move the
# ~10-blow median below, which is set by the HP and damage curves rather than by the cap.
# What it does change is anything paid per action: damage over time gets more ticks, while
# a once-per-fight scroll is diluted across a longer fight.
#
#   HP     = BASE_HP + health * HP_PER_POINT             (500 + 19/pt -> 2,020 at 80)
#   damage = BASE_DAMAGE + swing_stat * DAMAGE_PER_POINT  (49.5 + 2.42/pt -> 243 at 80)
#
# `swing_stat` is Strength for every weapon in the game bar the magic shelf, which reads
# Магия or the average of the two instead; see WEAPON_SCALING_* below. The curve itself
# is untouched by that -- a magic weapon changes which stat is read, never the rate.
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
# Live scroll battles give Strength a small durability contribution too. It only applies
# to fighters carrying a four-scroll loadout, so historic replay snapshots keep their
# exact old HP while every migrated/current pet gets the longer format.
HP_PER_STRENGTH_WITH_SKILLS = 5
BASE_DAMAGE = 49.5
DAMAGE_PER_POINT = 2.42
# Every blow is nudged by +-15% so two identical pets do not play out identically.
DAMAGE_VARIANCE = 0.15

# ------------------------------------------------------------------------- magic
# Scroll damage used to be written as a share of the OWNER'S PHYSICAL SWING: a 1.45x
# scroll on a Strength-80 pet hit for 352, which is 145% of a punch it did not have to
# throw with a stat it was already buying for the punches. Strength therefore paid twice
# and there was no such thing as a caster -- a "mage" was a Strength build holding paper.
#
# Магия is that second damage stat, and scrolls read off it instead:
#
#     spell_power = max(BASE_SPELL_POWER + magic * SPELL_POWER_PER_POINT,
#                       swing * SPELL_POWER_SWING_FLOOR)
#
# The numbers are chosen so that at magic == strength the two lines nearly meet
# (magic 20 -> 74 against a 98 swing, magic 80 -> 230 against a 243 swing): a scroll is
# worth about what a punch is worth when you actually paid for the stat behind it, and
# a fraction of that when you did not. The floor is what keeps a scroll from becoming a
# wasted turn for everyone who has not respecced yet -- a Strength build's scrolls are
# cut to 45% of what they were, which is the whole point of the rework, but they still
# beat standing still and the four-slot loadout is still worth filling.
# Four scrolls of one element. Small on purpose: it is a reason to prefer one shape of
# loadout over another, not a reason every loadout has to become mono-element. The four
# slots already cost a great deal of freedom to align -- a pure-fire set gives up the
# stun, the barrier and the cleanse that live on other elements -- so the bonus pays for
# a restriction the player has already accepted rather than handing out free damage.
ELEMENTAL_RESONANCE_BONUS = 0.10

# A creature chooses one permanent affinity. The six form one closed ring, so every
# choice has exactly one favourable matchup, one unfavourable matchup and four neutral
# ones -- no element is broadly better merely because it covers more opponents.
CHARACTER_ELEMENT_DAMAGE_BONUS = 0.10
CHARACTER_ELEMENTS = ("fire", "plants", "earth", "air", "frost", "water")
CHARACTER_ELEMENT_NAMES = {
    "fire": "Огонь", "plants": "Растения", "earth": "Земля",
    "air": "Воздух", "frost": "Лёд", "water": "Вода",
}
CHARACTER_ELEMENT_ICONS = {
    "fire": "🔥", "plants": "🌿", "earth": "🪨",
    "air": "💨", "frost": "❄️", "water": "💧",
}
# Read as attacker -> defender: fire burns plants, plants overgrow earth, earth stops
# air, air scatters frost, frost freezes water, water extinguishes fire.
CHARACTER_ELEMENT_STRONG_AGAINST = {
    "fire": "plants", "plants": "earth", "earth": "air",
    "air": "frost", "frost": "water", "water": "fire",
}


def character_element_multiplier(attacker: str | None, defender: str | None) -> float:
    """Outgoing damage multiplier for one permanent creature-element matchup."""
    if CHARACTER_ELEMENT_STRONG_AGAINST.get(str(attacker or "")) == str(defender or ""):
        return 1.0 + CHARACTER_ELEMENT_DAMAGE_BONUS
    return 1.0

BASE_SPELL_POWER = 22.0
SPELL_POWER_PER_POINT = 2.60
SPELL_POWER_SWING_FLOOR = 0.45

# A weapon declares which stat its ORDINARY SWING reads. Magic weapons are the other half
# of the caster build: without one a mage buys scroll damage and punches like a level-one
# pet, and with one every blow of the fight scales off the same stat the scrolls do.
# Hybrids average the two stats, so they are the honest pick for a split build and the
# worse pick for either pure one.
# A hybrid reads the AVERAGE of the two stats, and an average of two stats bought at
# level N is N -- against a specialist who put the same gold into one stat and reached
# far past N. Measured over an equal-gold round robin, that left the hybrid dominated: it
# collected roughly three quarters of the brawler's health AND three quarters of the
# caster's spell power AND a swing a third smaller than either, which is not a trade-off,
# it is a worse version of both. The two schools working together buy the swing back.
HYBRID_SCALING_BONUS = 1.25
WEAPON_SCALING_STRENGTH = "strength"
WEAPON_SCALING_MAGIC = "magic"
WEAPON_SCALING_HYBRID = "hybrid"
WEAPON_SCALINGS = (WEAPON_SCALING_STRENGTH, WEAPON_SCALING_MAGIC, WEAPON_SCALING_HYBRID)
WEAPON_SCALING_LABELS = {
    WEAPON_SCALING_STRENGTH: "⚔️ урон от Силы",
    # Both halves of the promise, because both bite: the swing reads Магия, and the swing
    # IS magic -- it walks through anything that only resists steel, and it is exactly
    # what a ward or an antimage is waiting for.
    # A wand, not the crystal ball: 🔮 is Магия's own stat badge, and a weapon wearing it
    # reads as "this is the stat" rather than "this reads from it". A wand is a thing you
    # hold, which is what a weapon is.
    WEAPON_SCALING_MAGIC: "🪄 урон от Магии · удар магический",
    WEAPON_SCALING_HYBRID: "🪄⚔️ урон от Магии и Силы поровну · удар наполовину магический",
}


def spell_power(magic: int, swing: float) -> float:
    """Damage one scroll point of `amount` is worth, given a caster's Magic and swing."""
    return max(
        BASE_SPELL_POWER + max(0, magic) * SPELL_POWER_PER_POINT,
        swing * SPELL_POWER_SWING_FLOOR,
    )

# Damage-over-time (fire, poison, venom, bleeding) and the retaliation bonus are written
# in the catalogue as a PERCENTAGE OF THE OWNER'S OWN SWING, not as a number of hit points.
#
# They used to be flat numbers pushed through a level curve, and every part of that was a
# problem. The card said "12 damage a turn" while the engine actually dealt 23 at level 10
# and 36 at level 100, so the printed number was true nowhere; the curve was sublinear, so
# the same passive quietly lost half its worth as a pet levelled; and "урон растёт с
# уровнем владельца" had to be stapled onto every single line of copy to explain it.
#
# A share of the swing needs no curve and no footnote: it grows with the pet exactly as
# fast as the fight around it does, it is the same number on the card and in the log, and
# "яд наносит 29% урона" is a sentence a player can act on. Nothing else in the file has
# to move -- at the balance harness's reference creature the two schemes agree exactly,
# because the old reference divisor below is precisely one swing's worth of the old value.
#
#   percent = old_flat_value / (BASE_DAMAGE + DAMAGE_PER_POINT) * 100
DOT_PERCENT_OF_SWING = True

# Fire is the damage-over-time that grows: left burning, each tick is hotter than the last
# (see the `grow` parameter in the catalogues). Because a fresh hit refreshes the flame
# without cooling it, an uncapped curve compounds for as long as the fight does -- a 16%
# tick reached 148 damage, 15% of a health bar, by the seventh round of a test fight. This
# is how many times its opening tick one flame may ever reach. Three is still a dramatic
# curve; it just cannot run away with a long fight.
BURN_GROWTH_CEILING = 3.0

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
CRIT_MULTIPLIER = 2.0       # "критического удара (х2)" -- the floor, at Удача 0
# Luck used to buy the CHANCE of a critical and nothing else, and the chance saturates:
# from Удача 40 to 52 it moves 15.7% -> 17.9%, which is about +2% damage over a fight.
# Measured against an identical pet with the same coins in another stat, that made Удача
# the worst purchase in the game by a wide margin -- 55% where Сила and Здоровье bought
# 89%. It now also buys how HARD a critical lands, which is the half that compounds:
# more of them AND bigger, so a coin spent here finally curves the way the others do.
#
# Written LINEAR, unlike the chance beside it, and that is the whole repair. A chance has
# to saturate -- it is a probability -- but saturation is also why Удача could not convert
# a lead: DOMINANCE_BONUS pays +30% of a stat's contribution to whoever is ahead in it,
# and 30% more of a number already flattened against its ceiling is nothing. Сила and
# Здоровье are linear and collect that bonus in full, which is most of why they measured
# 85-89% against Удача's 55%. Damage per critical has no ceiling to flatten against.
# The gentle end of the useful range, and that is a measurement rather than caution:
# 0.02, 0.03 and 0.04 all buy the same 64-65% for an ordinary twelve-level purchase --
# the repair was making this line LINEAR at all, not making it steep. What the steeper
# values did change is what a deliberate dump costs, from 3.1% down to 1.6%, and a stat
# whose absence is four times more punishing than Сила's is a tax rather than a choice.
CRIT_DAMAGE_PER_POINT = 0.02   # luck 40 -> x2.8, luck 80 -> x3.6
# ...and a hard ceiling on it, because "linear" and "unbounded" are not the same promise.
# A deep corridor enemy carries Удача in the hundreds (floor 41 is about 700), which an
# uncapped line turned into a x16 critical -- and the dungeon's own test caught it: a
# runner with 4,000 in every stat and 125,300 HP was losing 30 fights in 200 to a mob
# with an eighth of its swing. The cap sits far above anything a player reaches, so the
# line stays straight everywhere it is actually read and only the absurd tail is clipped.
CRIT_DAMAGE_MAX_MULTIPLIER = 5.0   # reached at Удача 150

# ------------------------------------------------------------------- неудача
# A pet whose Удача is under half its opponent's does not merely crit less -- it fumbles.
# This is the OTHER half of making the stat matter: the first half rewards buying it, and
# this one is what being caught without it costs. It rides on the existing "слабое место"
# system (STAT_DEFICIT_RATIO), so it is a real gap against a real opponent rather than a
# tax on any low number, and like every other deficit only the two largest ones bite.
LUCK_FUMBLE_CHANCE = 0.12        # per action taken while the luck deficit is live
LUCK_FUMBLE_SELF_CHANCE = 0.40   # of those, the share that land on the fumbler instead
LUCK_FUMBLE_SELF_SHARE = 0.45    # of their own swing, when a blow comes back at them

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

# Each pet gets at most this many actions -- attacks, Defends and scrolls alike. If
# neither side is knocked out by then, the living pet that dealt more total damage wins;
# see pets_combat.simulate.
#
# One budget for every fighter, whether it carries scrolls or not. It used to be 10 for a
# scroll-less fighter and 24 for one with a loadout, which quietly made an empty slot cost
# 14 actions on top of the missing scroll -- worth more than any scroll's own effect, and
# the reason PVE mobs (which never carry scrolls) fought at well under half a player's
# turns. Scrolls and Defend spend actions without always dealing damage, so the budget
# stays at the larger number: at 10, a healing or guard build would run out of fight
# before it had used its loadout.
MAX_SKILL_ACTIONS_PER_FIGHTER = 48
# Two legendary weapons buy extra attacks inside their own turn -- a crit that chains and
# a weapon that swings twice. This is the hard ceiling on how many a single turn may
# contain, wherever they came from, so no combination of passives can hold the floor
# indefinitely. `MAX_SKILL_ACTIONS_PER_FIGHTER` still caps the fight as a whole.
MAX_EXTRA_ATTACKS_PER_TURN = 3

# ------------------------------------------------------------------ stat lead bonus
# Every lead matters: a stat that is 10% higher contributes 10% more, rising linearly to
# the 30% cap once it is 30% ahead. Compared per stat at the start of the fight, on
# EFFECTIVE stats (levels + pet level + equipment). Only the stat-derived part of a value
# is boosted -- BASE_HP and BASE_DAMAGE are floors everybody gets, not a reward for
# out-scaling somebody.
DOMINANCE_RATIO = 1.30      # ratio where the gradual bonus reaches its ceiling
DOMINANCE_BONUS = 0.30      # maximum bonus to one stat's contribution

# A specialised build is valid, but leaving a stat at less than half the opponent's
# effective value exposes a concrete weakness. Only the two largest gaps bite in one
# fight: being generally outmatched is already hard enough without stacking all four.
STAT_DEFICIT_RATIO = 0.50
STAT_DEFICIT_MAX = 2
STAT_DEFICIT_DODGE_MULTIPLIER = 0.65
STAT_DEFICIT_ACCURACY_MULTIPLIER = 1.30
STAT_DEFICIT_AGILITY_DAMAGE_MULTIPLIER = 1.15
STAT_DEFICIT_HEALTH_MULTIPLIER = 0.90

# ------------------------------------------------------------------------ the arena
#
# Arena fights live in a per-pet bank, not a daily counter. A complete elapsed hour
# credits one fight, up to the current capacity. Every existing bonus unit adds one slot:
#
#     capacity = BASE_FIGHT_BANK_CAPACITY
#               + CAGE_BONUS_FIGHTS[cage_level - 1]
#               + floor(farm_level / FARM_LEVELS_PER_FIGHT)
#               + recent_figurines * FIGHTS_PER_RECENT_FIGURINE
#
# `recent_figurines` means qualifying #япокрасил posts in the rolling seven-day window.
# It is derived from stats' canonical records rather than stored as a mutable counter,
# so retries cannot grant the buff twice. Expiry only clamps the bank to its new cap.
BASE_FIGHT_BANK_CAPACITY = 5
# Compatibility name for integrations which still call daily_allowance. It now means
# bank capacity; fights do not reset at midnight.
BASE_DAILY_FIGHTS = BASE_FIGHT_BANK_CAPACITY
FARM_LEVELS_PER_FIGHT = 2
FIGHTS_PER_RECENT_FIGURINE = 1
RECENT_FIGURINE_FIGHT_BUFF_DAYS = 7
FIGHT_BANK_RECHARGE_SECONDS = 60 * 60

# --- Fights already had today -------------------------------------------------------
# There used to be a hard cap of three arena fights per opponent per day, then a stat
# penalty ("Знакомое лицо") that grew with every repeat. Both are gone: the cap was a shut
# door, and the penalty quietly decided fights on bookkeeping rather than on the creatures
# in them -- a player could lose to somebody weaker and have no way to see why.
#
# What is left is the COUNT, shown as «⚔️ ×N» on the opponent's card. It says the same
# useful thing the penalty was carrying ("you have been here today") and costs nothing.
# Derived from today's fight log, so it clears itself at midnight along with the day.
REPEAT_FIGHT_EMOJI = "⚔️"

# Matchmaking uses effective combat stats, including equipment and pet level, rather than
# a level window that could pair two very differently geared creatures.
POWER_RATING_BASE = 100
POWER_RATING_WEIGHTS = {
    "strength": 4,
    "health": 4,
    "agility": 2,
    # Raised from 2 with the crit-damage repair. Measured against an identical pet with
    # the same coins elsewhere, Удача now contributes like Магия (63% against 67%, where
    # it used to buy 55%), and it still buys the drop rate on top -- so pricing it at half
    # a stat in matchmaking would have kept telling players it is half a stat.
    "luck": 3,
    # Scroll damage and, with a magic weapon, every ordinary swing. Weighted below
    # Strength because it buys no health: Strength quietly carries HP_PER_STRENGTH_WITH_SKILLS
    # on top of the swing, and a caster pays for that difference in a shorter health bar.
    "magic": 3,
    # Reserved for its future mechanic. Buying it must not distort matchmaking before
    # it changes an actual combat value.
    "endurance": 0,
    "armor": 3,
}
OPPONENT_POWER_WINDOW = 125

# Was 5-10 ("случайно 5-10 голды"). Tripled because the arena was not paying for itself:
# ten fights a day at a 50% win rate netted about 26 coins after losses, against 1,481 for
# one stat to level 40 -- roughly a month of daily play for a single stat, which is what
# "everyone is poor" actually measured. At 15-30 the same day nets about 79.
# Was 15-30. PVP was 1.0% of daily gold: the only mode with a live opponent, real gear
# and tactics paid less than pressing a farm button and walking away. 2.5x fixes the
# thing the whole arena is for.
WIN_GOLD_MIN = 38
WIN_GOLD_MAX = 75
# The ARENA purse, doubled on 2026-08-17: a duel is the only mode with a live opponent,
# real gear and real tactics, and it should pay accordingly.
#
# Deliberately its own pair rather than a doubling of WIN_GOLD_MIN/MAX above, because
# that pair is ALSO the base for mob gold (PVE_GOLD_SHARE, tuned against it) and for the
# birthday greeting. Doubling it in place would have quietly doubled two other faucets
# nobody asked to change -- the exact kind of accident the income audit exists to catch.
#
ARENA_WIN_GOLD_MIN = 76
ARENA_WIN_GOLD_MAX = 150
# Every non-draw arena loser transfers this share of BOTH current wallets to the winner:
# coins and rubies. It applies equally to attacker and defender and is calculated from
# what the loser owns when the fight settles, never from the randomly rolled win purse.
ARENA_LOSS_TRANSFER_SHARE = 0.05

WIN_XP = 100
LOSS_XP = 35                # a loss still teaches something, so nobody dodges hard fights
DRAW_XP = 50                # both sides spent a fight; no gold or win is awarded
# Winning against a pet several levels below yours is less valuable, while an upset is
# worth more. The delta is loser level minus winner level, capped at three levels so a
# rare lopsided match cannot turn into a punitive tax or an outsized reward. Cage gold
# is applied first, then this multiplier, so both progression bonuses compose.
ARENA_LEVEL_REWARD_MULTIPLIERS = {
    -3: 0.75,
    -2: 0.85,
    -1: 0.93,
     0: 1.00,
     1: 1.08,
     2: 1.16,
     3: 1.25,
}
# How far below you an opponent has to be before Зеркало души is put on automatically.
# Five: the arena's own reward multiplier already stops caring past three levels, so by
# five the fight is both unrewarding and unfair, which is precisely the gap the mirror
# exists to close.
MIRROR_LEVEL_GAP = 5

# ------------------------------------------------------------------------------- PVE
# A duel moves coins between two players in addition to its winner's purse. A mob pays out
# of nothing at all, with nobody on the other side to lose anything, so the identical
# purse would be a faucet running at twice the arena's real rate. Half, then, as asked:
# "мобы давали примерно в два раза меньше денег".
# Was 0.5. A mob paid half an arena purse, and PVE came to 1.5% of daily gold -- a mode
# that existed without mattering. 1.2 makes thirty mob fights a real part of a day.
PVE_GOLD_SHARE = 1.2
# PVE has its OWN allowance, not a share of the arena bank. Ten attacks per window, and
# the window is a fixed 8-hour block of the chat's own clock -- 00:00, 08:00, 16:00 --
# so it refills for everybody on the server at the same moment rather than trickling back
# per player the way the arena's hourly recharge does. That is the whole point of the
# difference: the arena rewards checking in often, PVE is a batch you come back for.
PVE_WINDOW_HOURS = 8
PVE_ATTACKS_PER_WINDOW = 10
# XP is cut less hard than gold. Coins are the thing that inflates; pet levels are paced
# by the fight bank either way, and a PVE-only player should not level at half speed.
PVE_XP_SHARE = 0.7
# Loot is rarer than the arena's 20%, before each mob's own multiplier -- a mob can be
# fought at will within the bank, where a duel needs another player to exist.
PVE_DROP_CHANCE = 0.12
# Руби, the PVE currency. Nothing spends them yet by design; the numbers are small so
# whatever they end up buying can be priced against a supply that grew slowly.
PVE_RUBY_MIN = 1
PVE_RUBY_MAX = 3
# Additional rare PvE rewards. The tier multipliers live with the mob roster, while these
# are global base chances because they are economy dials rather than monster flavour.
PVE_RUNE_CHANCE = 0.008
PVE_FARM_TICKET_CHANCE = 0.006
PVE_DUNGEON_TICKET_CHANCE = 0.004
# The farm's occasional ruby, "случайно пусть падает с фермы иногда": per SHIFT, not per
# hour, and rare enough that the arena stays the place rubies actually come from.
FARM_RUBY_MIN = 5
FARM_RUBY_MAX = 8
ARENA_RUBY_CHANCE = 0.03
DUNGEON_RUBY_CHANCE = 0.04
# One pickaxe charge can fund a short check-in or a full workday.  Longer runs are more
# efficient per charge, which keeps the 8-hour choice meaningful without locking the
# quarry behind a single timer.  The 8-hour ruby range preserves the original payout.
QUARRY_HOUR_CHOICES = (1, 2, 4, 8)
QUARRY_DURATION_HOURS = 8  # legacy run fallback
QUARRY_RUBIES_BY_HOURS = {
    1: (1, 2), 2: (3, 5), 4: (8, 12), 8: (18, 25),
}
# Raised ~1.6x with the farm/PVE/PVP lifts: at 2.6% of a day's gold the quarry was a
# whole station nobody had a reason to use.
QUARRY_GOLD_BY_HOURS = {1: 40, 2: 88, 4: 192, 8: 416}
QUARRY_XP_BY_HOURS = {1: 20, 2: 45, 4: 100, 8: 220}
QUARRY_DROP_CHANCE_BY_HOURS = {1: .02, 2: .05, 4: .12, 8: .30}
QUARRY_RUBY_MIN, QUARRY_RUBY_MAX = QUARRY_RUBIES_BY_HOURS[QUARRY_DURATION_HOURS]
PICKAXE_COST = 150
PICKAXE_RUNS = 5
# The shovel is deliberately parallel to the pickaxe: a bought tool has five jobs, but
# a masterpiece upgrade earned from its dedicated rune-paint quest never runs dry.  The
# upgraded tool is 50% more effective than the base tool, not a blanket 50% bonus to the
# entire farm/quarry economy.
SHOVEL_COST = 150
SHOVEL_RUNS = 5
SHOVEL_GOLD_BONUS = 0.25
SHOVEL_MASTERWORK_GOLD_BONUS = 0.50
TOOL_MASTERWORK_MULTIPLIER = 1.50

# --- workplace figurines --------------------------------------------------------------
# A creature works in ONE place at a time: the farm shift and the quarry are the same
# creature's day, so starting one locks the other out.  A painted figurine is a stand-in
# worker for its own station -- and only owning BOTH lifts the rule, because only then is
# there somebody to leave at each of the two places at once.  Earned by their rune-paint
# quests (rune_paint_farmer / rune_paint_miner), never bought.
WORKPLACE_FIGURINES = ("farmer", "miner")
# What ONE painted figurine is worth on its own, so that the first of the pair is not a
# dead reward while its partner is still unpainted: its own station pays more experience.
FIGURINE_XP_BONUS = 0.25

HISTORY_LIMIT = 10          # "список последних 10 боев"
# The mailbox merges three feeds (fights, farm shifts, gifts) into one, so it is capped
# at what one Telegram message can carry comfortably rather than at HISTORY_LIMIT: it is
# a whole day of activity for an active player, and every older event is still in the
# stores it was read from.
MAIL_LIMIT = 30
DUEL_DAILY_LIMIT = 5
DUEL_COOLDOWN_SECONDS = 10 * 60
DUEL_SAME_OPPONENT_DAILY_LIMIT = 1


def daily_fight_allowance(
    cage_level: int = 1, farm_level: int = 0, recent_figurines: int = 0,
) -> int:
    """The maximum number of arena fights a member can bank at once.

    This is intentionally a pure, integer-only formula. The caller obtains the rolling
    paint count from ``stats.recent_figurine_fight_bonus_count`` so the allowance cannot
    drift from a duplicated delivery or a deleted painting.
    """
    level = min(max(cage_level, 1), CAGE_MAX_LEVEL)
    return (
        BASE_FIGHT_BANK_CAPACITY
        + CAGE_BONUS_FIGHTS[level - 1]
        + max(0, int(farm_level)) // FARM_LEVELS_PER_FIGHT
        + max(0, int(recent_figurines)) * FIGHTS_PER_RECENT_FIGURINE
    )


def arena_loss_transfer(balance: int, retained_share: float = 0.0) -> int:
    """Five percent of a loser's current wallet, after any explicit retention effect."""
    share = ARENA_LOSS_TRANSFER_SHARE * (1 - min(1.0, max(0.0, retained_share)))
    current = max(0, int(balance or 0))
    return min(current, max(0, round(current * share + 1e-9)))


def arena_level_reward_multiplier(winner_level: int, loser_level: int) -> float:
    """Return the capped win-reward multiplier for this level matchup.

    Loss and draw XP stay flat: losing is still useful practice and a draw has no
    winner to evaluate. Only the reward for defeating the opponent is scaled.
    """
    delta = max(-3, min(3, int(loser_level) - int(winner_level)))
    return ARENA_LEVEL_REWARD_MULTIPLIERS[delta]


# --------------------------------------------------------------------- pet levelling
# Pet levels are separate from stat levels: "у существ отдельный свой опыт и уровни. За
# каждый уровень существо получает +1 ко всем статам." That +1 is free and stacks ON TOP
# of the purchased cap, so a level-30 pet with a maxed stat is at an effective 110.
#
# xp(L -> L+1) = round(PET_XP_BASE * L ** PET_XP_EXPONENT), and WIN_XP is 100, so the
# curve reads directly in wins: level 10 at ~25 wins, level 20 at ~93, level 30 at ~196,
# level 50 at ~499. The exponent is below 1 on purpose -- the +1-to-everything per level
# is already the strongest thing in the game, so the curve only has to be long, not
# vertical.  There is deliberately no pet-level ceiling: XP remains valuable after 50.

PET_MAX_LEVEL = None
PET_XP_BASE = 80.0
PET_XP_EXPONENT = 0.8
PET_LEVEL_STAT_BONUS = 1    # +1 to every stat per pet level
# A level is BOUGHT, not granted: the XP banks up and the player spends rubies to turn it
# into the +1. Priced in rubies rather than gold on purpose -- the dungeon prints gold and
# almost no rubies, so this is what stops the richest faucet from being a substitute for
# playing everything else (see the economy audit: one undifferentiated input is what lets
# a rational player ignore four fifths of the game).
PET_LEVEL_UP_RUBY_COST = 3


def pet_xp_for_next_level(level: int) -> int:
    """XP needed to go from ``level`` to ``level + 1`` (the ladder is unbounded)."""
    if level < 1:
        return 0
    return max(1, round(PET_XP_BASE * level ** PET_XP_EXPONENT))


# A rune is a paid, permanent weapon enhancement, so its benefit must remain visible as
# pets' combat stats grow.  Flat 3--5 point effects disappeared into a 10--48 action
# fight once a pet had progressed beyond its starter stats.  Values are derived from the
# same base HP/damage equations as combat; percentage effects retain safe hard ceilings.
RUNE_REGEN_MAX_HP_SHARE = .015
RUNE_FIRE_DAMAGE_SHARE = .30
RUNE_CHILL_BASE = 18
RUNE_CHILL_AGILITY_DIVISOR = 8
RUNE_CHILL_MAX = 35
RUNE_PLATING_BASE = 5
RUNE_PLATING_MAX = 16
RUNE_PRECISION_BASE = 12
RUNE_PRECISION_AGILITY_DIVISOR = 7
RUNE_PRECISION_MAX = 30
RUNE_VAMPIRIC_BASE = 5
RUNE_VAMPIRIC_STRENGTH_DIVISOR = 20
RUNE_VAMPIRIC_MAX = 15

# What carrying ANY elemental rune does to the weapon itself, on top of that element's own
# effect. Half the blade's damage stops being steel, which is the point: a runed weapon
# still bites something that shrugs off physical damage entirely, and it pays for that by
# becoming reflectable by anything that returns magic. The flat power bump is the reason
# to enchant a weapon you already like rather than treating a rune as a side effect.
RUNE_WEAPON_MAGIC_SHARE = .50
RUNE_WEAPON_POWER_BONUS = .10


def rune_enchantment_effect(element: str, stats: dict) -> dict | None:
    """Build a combat-effect snapshot for one elemental weapon rune.

    ``stats`` must be the owner's effective stats, so equipment and pet levels are
    included without the combat engine needing access to persisted player records.
    """
    strength = max(1, int(stats.get("strength", 1) or 1))
    health = max(1, int(stats.get("health", 1) or 1))
    agility = max(1, int(stats.get("agility", 1) or 1))
    armor = max(0, int(stats.get("armor", 0) or 0))
    max_hp = BASE_HP + health * HP_PER_POINT
    damage = BASE_DAMAGE + strength * DAMAGE_PER_POINT
    if element == "fire":
        # This value already scales from effective Strength. The passive combat layer
        # must not apply its legacy flat-damage curve a second time.
        return {
            "code": "burn", "value": max(15, round(damage * RUNE_FIRE_DAMAGE_SHARE)),
            "turns": 2, "level_scaled": False,
        }
    if element == "frost":
        return {"code": "chill", "value": min(RUNE_CHILL_MAX, RUNE_CHILL_BASE + agility // RUNE_CHILL_AGILITY_DIVISOR)}
    if element == "water":
        return {"code": "regen", "value": max(8, round(max_hp * RUNE_REGEN_MAX_HP_SHARE))}
    if element == "earth":
        return {"code": "plating", "value": min(RUNE_PLATING_MAX, RUNE_PLATING_BASE + armor // 8 + health // 80)}
    if element == "air":
        return {"code": "precision", "value": min(RUNE_PRECISION_MAX, RUNE_PRECISION_BASE + agility // RUNE_PRECISION_AGILITY_DIVISOR)}
    if element == "plants":
        return {"code": "vampiric", "value": min(RUNE_VAMPIRIC_MAX, RUNE_VAMPIRIC_BASE + strength // RUNE_VAMPIRIC_STRENGTH_DIVISOR)}
    return None


# -------------------------------------------------------------------- granted debuffs
# Marks an admin hands out by name from the Mini App. Nothing in the game awards one and
# no amount of playing removes one -- the only way out is the condition the debuff names.
#
# `scale` multiplies EVERY effective stat, armour included, in exactly one place
# (pets._effective_stats_for), so it reaches combat, the power rating and therefore
# matchmaking, both pet cards and both arenas without any of them knowing it exists.
#
# `clears_on` is the whole point of the design rather than a convenience. "photo" means
# the mark is compared against the picture the creature wore when it was handed out, so
# changing that picture lifts it -- not on a timer, not by an admin remembering, and not
# through a hook that some other code path could bypass. A punishment nobody can lift is
# a ban with extra steps; this one is a nudge with a door in it.
#
# Every field here is player-facing copy. `description` is the joke and `hint` is the way
# out, and both travel with the debuff everywhere it is shown -- a −5% that appears on a
# card with no explanation reads as a bug in the game.
DEBUFF_STAT_SCALE_FLOOR = 0.50   # a mark may sting; it may not delete somebody's creature

DEBUFFS = {
    "impostor": {
        "emoji": "🎭",
        "title": "Самозванец",
        "line": "−5% ко всем статам",
        "description": "Что-то на этой аватарке подозрительно мало твоей краски…",
        "hint": "Спадёт само, как только сменишь картинку существа.",
        "scale": 0.95,
        "clears_on": "photo",
    },
}


def debuff_spec(code) -> dict | None:
    """One debuff's data, or None for an unknown code.

    Unknown codes are survivable on purpose: a save written by a newer build must not be
    able to crash a fight, and a mark whose definition has gone simply stops applying.
    """
    spec = DEBUFFS.get(str(code or ""))
    return dict(spec) if spec else None


# ------------------------------------------------------------------------ inventory
# Four slots, as asked. The catalogue is deliberately thin -- "доступные список добавим
# позже с ценами" -- but the shape is fixed, so adding an item later is one more Item()
# and nothing else: no new slot logic, no new stat plumbing, no migration.
#
# `source` says where an item comes from. "shop" items are buyable from the pet menu;
# "drop" items cannot be bought at any price and only fall out of arena wins. Anything
# with a price of 0 and source "drop" is a trophy.

SLOT_KEYS = ("weapon", "amulet", "gloves", "boots", "shield")
SLOT_NAMES = {
    "weapon": "Оружие",
    "amulet": "Амулет",
    "gloves": "Перчатки",
    "boots": "Сапоги",
    "shield": "Щит",
}
SLOT_EMOJI = {
    "weapon": "🗡",
    "amulet": "📿",
    "gloves": "🧤",
    "boots": "👢",
    "shield": "🛡",
}


class Item:
    """One equippable thing. `bonuses` maps any of STAT_KEYS or "armor" to a flat add."""

    __slots__ = (
        "code", "name", "slot", "price", "source", "bonuses", "description",
        "rarity", "resale_price", "drop_weight", "effect", "cursed", "scaling",
    )

    def __init__(
        self, code, name, slot, price, source, bonuses, description="", rarity="common",
        resale_price=None, drop_weight=1, effect=None, cursed=False,
        scaling=WEAPON_SCALING_STRENGTH,
    ):
        self.code = code
        self.name = name
        self.slot = slot
        self.price = price
        self.source = source
        self.bonuses = dict(bonuses)
        self.description = description
        # The old uncommon tier was merged into common.  Normalize while constructing
        # runtime items so old catalogue rows and persisted codes keep their stats and
        # prices without exposing a second green rarity anywhere in the game.
        self.rarity = "common" if rarity == "uncommon" else rarity
        self.resale_price = resale_price
        self.drop_weight = drop_weight
        self.effect = dict(effect or {})
        # Cursed is a PROPERTY, not a rarity. It used to be only the junk tier at the
        # bottom of the weapon catalogue, while the eight legendary curses were ordinary
        # legendaries that merely read as cursed -- so one word meant two unrelated things
        # and the forge could not tell them apart. As a flag it rides alongside rarity,
        # which is what lets a cursed ladder exist (cursed junk -> rare cursed -> legendary
        # cursed) without teaching a sixth rarity to the drop tables, the badges, the
        # filters and both front ends.
        self.cursed = bool(cursed)
        # Weapons only. Which stat the wearer's ordinary swing reads; every other slot
        # leaves it at the default and `weapon_scaling` below never looks at them.
        self.scaling = scaling if scaling in WEAPON_SCALINGS else WEAPON_SCALING_STRENGTH


def weapon_scaling(item) -> str:
    """The stat an equipped weapon's swing reads. Bare paws punch with Strength."""
    scaling = getattr(item, "scaling", None) if item is not None else None
    return scaling if scaling in WEAPON_SCALINGS else WEAPON_SCALING_STRENGTH


# Starter catalogue. Six of these nine entries are the original hand-written items; the
# three weapons are legacy identifiers only -- once pets_weapon_catalog loads (see
# below), stick/fork/bone are entirely REPLACED by w001/w002/w003 (their weapon-slot
# entries get filtered out of ITEMS), LEGACY_ITEM_CODES redirects find_item() there, and
# the prices below matter only for the degraded no-catalogue-module fallback. They are
# kept numerically identical to w001/w002's real catalogue prices (60/100) so that
# fallback path can never regress to the pre-rebalance 250/900 economy.
#
# The six accessories (bead/acorn/mittens/claws/slippers/springs) are NOT replaced by
# anything: pets_amulet_catalog and pets_gear_catalog are entirely source="drop" (see
# their own _validate_catalogue asserts), so these six are the ONLY amulet/gloves/boots
# items anyone can ever buy. Unlike the weapons above, their prices were never touched by
# the 2026-08 income rebalance and still assumed the old arena economy -- a 1,100-coin
# amulet against a farm run that pays 14-33 gold is nonsense on that scale. Repriced here
# exactly the way pets_weapon_catalog prices a weapon rather than by guessing: rarity is
# picked from where each item's SHOP_PRICE_POWER_WEIGHTS-weighted power actually falls
# relative to the generated weapon bands (common 24-48, uncommon 52-74, rare 86-116),
# then shop_price_for_bonuses(rarity, bonuses) supplies the number -- so an accessory and
# a weapon of comparable power now cost comparable money, and resale_value() (20% of
# price) stops paying out three-digit refunds for a vial of paint.
ITEMS = (
    # power 24 (str 6*4) -- the common floor. Identical shape to w001 ("+6 strength,
    # nothing else"), so the price matches w001's real catalogue price exactly.
    Item("stick", "Кисть-щетина №8", "weapon", 60, "shop", {"strength": 6},
        "Жёсткая, уверенная, для смелых мазков."),
    # power 64 (str 14*4 + luck 4*2) -- uncommon band. Identical shape to w002, same price.
    Item("fork", "Аэрограф Harder & Steenbeck", "weapon", 100, "shop", {"strength": 14, "luck": 4},
        "Ровный факел краски и немного магии в триггере."),
    Item("bone", "Компрессор старого мастера", "weapon", 0, "drop", {"strength": 20, "agility": -3},
        "Тяжёлый, гудит и выдаёт идеальное давление."),
    # power 16 (luck 8*2) -- below even the weakest generated common weapon (24), so it
    # sits at shop_price_for_bonuses' common floor: 60.
    Item("bead", "Флакон Nuln Oil", "amulet", 60, "shop", {"luck": 8},
        "Одна капля на модель, другая непременно на стол."),
    # power 52 (luck 16*2 + health 5*4) lands inside the uncommon weapon band (52-74) --
    # was the second-worst offender at 1,100, over fifty times its actual combat weight.
    Item("acorn", "Набор Scale75 Artist", "amulet", 85, "shop", {"luck": 16, "health": 5},
        "Пигмент настолько плотный, что вдохновляет на подвиги.", rarity="uncommon"),
    # power 60 (armor 20*3) -- squarely uncommon, same tier and price as springs below.
    Item("mittens", "Нитриловые перчатки", "gloves", 95, "shop", {"armor": 20},
        "Защищают лапы от краски, грунта и внезапных проливов.", rarity="uncommon"),
    # power 95 (agility 10*2 + armor 25*3) lands inside the rare weapon band (86-116) --
    # the worst offender at the old 1,000; now priced like the shop's other aspirational
    # rare weapons (160-195) instead of near the top of the entire economy.
    Item("claws", "Перчатки сухой кисти", "gloves", 170, "shop", {"agility": 10, "armor": 25},
        "Пыльные, ловкие и привычные к самым острым граням.", rarity="rare"),
    # power 14 (agility 7*2) -- below the common floor, same as bead.
    Item("slippers", "Тапки из малярного скотча", "boots", 60, "shop", {"agility": 7},
        "Лёгкие и липкие: ни одна база не убежит."),
    # power 60 (agility 15*2 + armor 10*3) -- uncommon, same tier and price as mittens.
    Item("springs", "Ботинки с банками Vallejo", "boots", 95, "shop", {"agility": 15, "armor": 10},
        "Шуршат шариками внутри и ускоряют путь к столу.", rarity="uncommon"),
)

# The large weapon catalogue is intentionally data-only so balancing/plumbing stays
# here.  During partial deployments its absence leaves the compact starter catalogue
# usable; when present it replaces the three starter weapons while preserving the six
# non-weapon starters.
try:
    from pets_weapon_catalog import (
        MOB_HUNTER_WEAPON_CODE,
        PRE_REBALANCE_BUY_PRICES as PRE_REBALANCE_WEAPON_BUY_PRICES,
        RAW_ITEMS as _RAW_WEAPON_ITEMS,
        RETIRED_WEAPON_REPLACEMENTS,
        STARTER_WEAPON_MAX_PRICE,
    )
except ImportError:
    _RAW_WEAPON_ITEMS = ()
    RETIRED_WEAPON_REPLACEMENTS = {}
    PRE_REBALANCE_WEAPON_BUY_PRICES = {}
    STARTER_WEAPON_MAX_PRICE = FARM_GOLD_PER_RUN[1]
    MOB_HUNTER_WEAPON_CODE = ""

try:
    from pets_amulet_catalog import RAW_ITEMS as _RAW_AMULET_ITEMS
except ImportError:
    _RAW_AMULET_ITEMS = ()

try:
    from pets_gear_catalog import RAW_ITEMS as _RAW_GEAR_ITEMS
except ImportError:
    _RAW_GEAR_ITEMS = ()

try:
    from pets_shield_catalog import RAW_ITEMS as _RAW_SHIELD_ITEMS
except ImportError:
    _RAW_SHIELD_ITEMS = ()


def _catalog_item(spec):
    if isinstance(spec, Item):
        return spec
    if not isinstance(spec, dict):
        raise TypeError("weapon catalogue entries must be Item objects or dicts")
    return Item(
        spec["code"], spec["name"], spec["slot"], spec.get("price", spec.get("buy_price", 0)),
        spec.get("source", "shop"), spec.get("bonuses", {}),
        spec.get("description", ""), spec.get("rarity", "common"),
        spec.get("resale_price"), spec.get("drop_weight", 1), spec.get("effect"),
        spec.get("cursed", False), spec.get("scaling", WEAPON_SCALING_STRENGTH),
    )


if _RAW_WEAPON_ITEMS:
    _catalogue_weapons = tuple(_catalog_item(spec) for spec in _RAW_WEAPON_ITEMS)
    # Counted against the catalogue itself rather than a number written twice: the
    # literal that used to live here made every addition to the catalogue a
    # ValueError at import time in a file that has nothing to do with the change.
    if len(_catalogue_weapons) != len(_RAW_WEAPON_ITEMS) or any(
        item.slot != "weapon" for item in _catalogue_weapons
    ):
        raise ValueError("weapon catalogue must contain only weapon entries")
    _codes = [item.code for item in _catalogue_weapons]
    if len(set(_codes)) != len(_codes):
        raise ValueError("weapon catalogue contains duplicate item codes")
    ITEMS = _catalogue_weapons + tuple(item for item in ITEMS if item.slot != "weapon")

_catalogue_amulets = tuple(_catalog_item(spec) for spec in _RAW_AMULET_ITEMS)
_catalogue_gear = tuple(_catalog_item(spec) for spec in _RAW_GEAR_ITEMS)
_catalogue_shields = tuple(_catalog_item(spec) for spec in _RAW_SHIELD_ITEMS)
# The loot table is what this guards: exactly 40 amulets have to be findable, and every
# amulet has to be an amulet. Sold ones are counted separately -- adding something to the
# shop counter must not be able to quietly take a slot out of the drop pool.
_dropped_amulets = tuple(item for item in _catalogue_amulets if item.source == "drop")
if _RAW_AMULET_ITEMS and (
    len(_dropped_amulets) != 40
    or any(item.slot != "amulet" for item in _catalogue_amulets)
    # "vault" is a withdrawn item kept only so old fight snapshots still resolve its code.
    # Every shelf and every loot pool filters on an exact source, so it appears in neither.
    or any(item.source not in ("drop", "shop", "vault") for item in _catalogue_amulets)
    or any(item.price <= 0 for item in _catalogue_amulets if item.source == "shop")
):
    raise ValueError("amulet catalogue must contain exactly 40 drop-only amulets, plus priced shop ones")
if _RAW_GEAR_ITEMS and (
    len(_catalogue_gear) != 80
    or sum(item.slot == "boots" for item in _catalogue_gear) != 40
    or sum(item.slot == "gloves" for item in _catalogue_gear) != 40
    or any(item.source != "drop" for item in _catalogue_gear)
):
    raise ValueError("gear catalogue must contain 40 drop-only boots and 40 gloves")
if _RAW_SHIELD_ITEMS and (
    # The size floor lives in pets_shield_catalog, next to the reason for it. Here we only
    # check what this file is responsible for: everything claiming to be a shield is one,
    # and the slot has a shop to sell them in.
    any(item.slot != "shield" for item in _catalogue_shields)
    or sum(item.source == "shop" for item in _catalogue_shields) < 3
):
    raise ValueError("shield catalogue must hold only shields, at least three sold in shops")


def _spread_items(rows, count):
    """Keep a representative slice from a long, ordered stat-only progression."""
    rows = tuple(rows)
    count = min(len(rows), max(0, int(count)))
    if count >= len(rows):
        return rows
    if count <= 1:
        return rows[:count]
    indices = {round(index * (len(rows) - 1) / (count - 1)) for index in range(count)}
    return tuple(row for index, row in enumerate(rows) if index in indices)


def _retire_plain_grey(rows, *, slot: str, target_total: int, permanent=()):
    """Hide redundant grey stat sticks and map every owned copy to a close survivor."""
    rows = tuple(rows)
    plain = tuple(row for row in rows if row.rarity in ("common", "uncommon"))
    permanent = tuple(row for row in permanent if row.slot == slot and row.rarity in ("common", "uncommon"))
    permanent_codes = {row.code for row in permanent}
    selectable = tuple(row for row in plain if row.code not in permanent_codes)
    kept_plain = _spread_items(selectable, max(0, target_total - len(permanent)))
    kept_codes = {row.code for row in kept_plain} | permanent_codes
    kept = tuple(row for row in rows if row.rarity not in ("common", "uncommon") or row.code in kept_codes)
    candidates = kept_plain + permanent
    aliases = {}
    for old in plain:
        if old.code in kept_codes:
            continue
        def distance(new):
            keys = set(old.bonuses) | set(new.bonuses)
            stat_gap = sum(abs(int(old.bonuses.get(key, 0)) - int(new.bonuses.get(key, 0))) for key in keys)
            old_effect = (old.effect or {}).get("code") if isinstance(old.effect, dict) else ""
            new_effect = (new.effect or {}).get("code") if isinstance(new.effect, dict) else ""
            return (old_effect != new_effect, old.rarity != new.rarity, stat_gap, new.code)
        aliases[old.code] = min(candidates, key=distance).code
    return kept, aliases


# Grey boots/gloves are pure stat variations: ten live designs per slot are enough for
# drops, duplicates and forging. Grey amulets stay because nearly every one has a distinct
# combat hook. Shields retain eight ordinary designs, comfortably above a four-item forge.
_starter_accessories = tuple(item for item in ITEMS if item.slot != "weapon")
_boots = tuple(item for item in _catalogue_gear if item.slot == "boots")
_gloves = tuple(item for item in _catalogue_gear if item.slot == "gloves")
_boots, _retired_boots = _retire_plain_grey(
    _boots, slot="boots", target_total=10, permanent=_starter_accessories,
)
_gloves, _retired_gloves = _retire_plain_grey(
    _gloves, slot="gloves", target_total=10, permanent=_starter_accessories,
)
_catalogue_gear = _boots + _gloves
_catalogue_shields, _retired_shields = _retire_plain_grey(
    _catalogue_shields, slot="shield", target_total=8,
    permanent=tuple(item for item in _catalogue_shields if item.source == "shop"),
)
_RETIRED_GREY_REPLACEMENTS = {
    **_retired_boots, **_retired_gloves, **_retired_shields,
}
_new_catalogue_items = _catalogue_amulets + _catalogue_gear + _catalogue_shields
if _new_catalogue_items:
    existing_codes = {item.code for item in ITEMS}
    new_codes = [item.code for item in _new_catalogue_items]
    if len(set(new_codes)) != len(new_codes) or existing_codes.intersection(new_codes):
        raise ValueError("equipment catalogues contain duplicate item codes")
    ITEMS = ITEMS + _new_catalogue_items

# The forge creates this singular rare relic from six discarded cursed weapons. It is a
# real item rather than a synthetic inventory record so it works with all existing bag,
# equipment, collection and combat paths.
ITEMS = ITEMS + (
    Item(
        "cursed_relic", "Реликвия шести проклятий", "weapon", 0, "forge",
        {"strength": 24, "luck": 5, "health": -8},
        "Шесть проклятий договорились, но одно всё ещё шепчет.", rarity="rare",
        resale_price=125, drop_weight=0,
        effect={"code": "candle", "text": "В начале боя: +55% к урону или -25%.",
                "value": 55, "downside": 25, "chance": 60},
        # It is called "Реликвия шести проклятий" and it is what six curses become, so it
        # belongs to the cursed line rather than sitting outside it: that makes it a valid
        # ingredient for the next rung up instead of a dead end a player cannot spend.
        cursed=True,
    ),
)

# Save files from both catalogue reductions keep working. The retired grey weapon aliases
# are also the migration recipe: inventories retain every physical copy, merely under the
# closest surviving design code.
LEGACY_ITEM_CODES = (
    {
        "stick": "w001", "fork": "w002", "bone": "w003",
        **RETIRED_WEAPON_REPLACEMENTS, **_RETIRED_GREY_REPLACEMENTS,
    }
    if _RAW_WEAPON_ITEMS else {}
)


# Shop gear pays back only 20%; drop-only trophies have an explicit salvage value in
# catalogue data or a conservative value based on their stat impact.
ITEM_RESALE_SHARE = 0.20
RARITY_LABELS = {
    "cursed": "☠️ Проклятое",
    "common": "⚪ Обычное",
    "uncommon": "🟢 Необычное",
    "rare": "🔵 Редкое",
    "legendary": "🟣 Легендарное",
}

# Automatic drop equipment compares actual combat contribution, then adds a modest
# premium for rarity and for an amulet passive.  Rarity is deliberately not an absolute
# ordering: a genuinely stronger old shop item should not be replaced by a shiny but
# numerically worse trophy.
AUTO_EQUIP_RARITY_BONUS = {
    "cursed": -20,
    "common": 0,
    "uncommon": 10,
    "rare": 25,
    "legendary": 40,
}
AUTO_EQUIP_EFFECT_BONUS = {
    "cursed": 0,
    "common": 8,
    "uncommon": 15,
    "rare": 22,
    "legendary": 25,
}


def equipment_score(item: Item | None) -> int:
    """One deterministic comparison score for sorting forge candidates."""
    if item is None:
        return -10_000
    score = sum(
        int(value) * POWER_RATING_WEIGHTS.get(key, 0)
        for key, value in item.bonuses.items()
    )
    score += AUTO_EQUIP_RARITY_BONUS.get(item.rarity, 0)
    if getattr(item, "effect", None):
        score += AUTO_EQUIP_EFFECT_BONUS.get(item.rarity, 0)
    return score


def resale_value(item: Item) -> int:
    if item.resale_price is not None:
        return max(1, int(item.resale_price))
    if item.price > 0:
        return max(1, round(item.price * ITEM_RESALE_SHARE))
    impact = sum(abs(int(value)) for value in item.bonuses.values())
    return max(5, impact * 3)


# How often a win drops an item at all, and from which pool.  Only the winner rolls,
# so at the old 8% a player on the base five-fight bank saw an item about once every
# five days -- long enough that most fights felt like they paid nothing. 20% plus the
# six-win ceiling makes empty streaks short while leaving the conditional rarity split
# (which item, once a drop happens) untouched.
DROP_CHANCE = 0.20

# Luck is now the "find things" stat as well as the crit stat: it multiplies the chance of
# an item dropping, in the arena and on the farm alike.
#
#     multiplier = 1 + LUCK_DROP_BONUS_MAX * luck / (luck + LUCK_DROP_K)
#
# Same saturating shape as DODGE/CRIT above, and chosen for the same reason: a linear bonus
# either does nothing at luck 10 or doubles drops before luck 40, whereas this pays a
# luck-focused build the whole way up without any single point being a cliff. K is the luck
# at which half the maximum bonus is reached.
#
#     luck    1     10     20     40     60     80
#     bonus  +2%   +13%   +23%   +36%   +44%   +49%
#     arena  20.3% 22.7%  24.6%  27.1%  28.6%  29.8%   (from a 20% base)
#     farm 8h 50.8% 56.7% 61.4% 67.8% 71.4% 74.6%   (from a 50% base)
#
# Deliberately a multiplier on the base rather than a flat addition: a one-hour farm shift
# is meant to be a poor way to hunt for loot, and a flat bonus would make luck turn it into
# the best one. Half the maximum by luck 50 also means the bonus is real for somebody who
# merely favours luck, not only for the 6,896 coins it costs to take it to 80.
LUCK_DROP_BONUS_MAX = 0.80
LUCK_DROP_K = 50.0


def luck_drop_multiplier(luck: int) -> float:
    """How much one pet's luck multiplies its item-find chance. 1.0 at zero luck."""
    value = max(0, int(luck or 0))
    return 1.0 + LUCK_DROP_BONUS_MAX * value / (value + LUCK_DROP_K)


# A normal win has roughly 0.05% chance to produce a legendary of any kind -- the
# weighted pool grew once gear and amulets joined it, so natural legendaries are far
# rarer than the raw drop rate suggests and the ceiling is what most players actually
# reach.  300 wins is deliberately conservative: luck still matters, but an active
# player cannot miss every legendary forever.
LEGENDARY_PITY_ELIGIBLE_WINS = 300
# Even ordinary loot cannot hide behind an unlucky streak forever.  This counter is
# independent of the legendary-weapon ceiling above and resets on every item found.
ITEM_PITY_ELIGIBLE_WINS = 6

# Gifts are social rather than a fast alt-account funnel.  The giver needs a creature
# with a little arena history, and each giver can move only one item per day.  The
# values live here so they are visible alongside the rest of the economy knobs.
GIFT_MIN_PET_LEVEL = 3
GIFT_COOLDOWN_SECONDS = 24 * 60 * 60
GIFT_AUDIT_LIMIT = 500

# The shop deliberately has small, personal changing windows instead of asking players
# to scroll through hundreds of items. Each window selects a handful of ordinary items and
# one rare one for every equipment slot.
#
# Cut and slowed together: the shelf used to refresh twice a day with five ordinary offers,
# which is ten ordinary items a day per slot -- more choice than a player could ever spend
# on, and it made gold the only thing standing between anybody and a full set. One window a
# day of three plus one, at twice the price, turns a shopping trip into a decision again.
STOREFRONT_ROTATION_HOURS = 24
STOREFRONT_NORMAL_COUNT = 3
STOREFRONT_RARE_COUNT = 1
# Applied to the OFFER, never to the catalogue: an item's stated price still drives its
# resale value and every balance table built on it, so doubling here makes the shop more
# expensive without quietly doubling what the same item sells back for.
STOREFRONT_PRICE_MULTIPLIER = 2
STOREFRONT_RARITIES = ("common", "rare")
DAILY_STOREFRONT_SIZE = STOREFRONT_NORMAL_COUNT + STOREFRONT_RARE_COUNT
STOREFRONT_TIMEZONE_NAME = "Europe/Moscow"
_STOREFRONT_TIMEZONE = _ZoneInfo(STOREFRONT_TIMEZONE_NAME)


def storefront_window(day: _date | _datetime | str | None = None) -> int:
    """Stable identifier for the shop window a moment falls in, in Moscow time."""
    moment = day or _datetime.now(_STOREFRONT_TIMEZONE)
    if isinstance(moment, str):
        try:
            moment = _datetime.fromisoformat(moment)
        except ValueError:
            moment = _date.fromisoformat(moment)
    if isinstance(moment, _date) and not isinstance(moment, _datetime):
        moment = _datetime.combine(moment, _datetime.min.time())
    # Naive values are Moscow wall time for deterministic tests and previews. Aware
    # values may come from another app timezone, so convert before choosing the window.
    if moment.tzinfo is not None:
        moment = moment.astimezone(_STOREFRONT_TIMEZONE)
    # Derived from the rotation rather than hard-coded at two windows a day, so changing
    # STOREFRONT_ROTATION_HOURS actually changes the shop instead of silently producing
    # two identical windows that both look like "today".
    per_day = max(1, 24 // STOREFRONT_ROTATION_HOURS)
    return moment.date().toordinal() * per_day + moment.hour // STOREFRONT_ROTATION_HOURS


def storefront_price(item: Item) -> int:
    """The sale price for a rotating offer without changing its drop provenance."""
    if item.price > 0:
        return int(item.price) * STOREFRONT_PRICE_MULTIPLIER
    power = sum(
        {"strength": 4, "health": 4, "agility": 2, "luck": 2, "armor": 3}.get(key, 0)
        * int(value)
        for key, value in item.bonuses.items()
    )
    base = 55 if item.rarity == "rare" else 45
    multiplier = 1.20 if item.rarity == "rare" else .60
    return max(60, min(195, int((base + power * multiplier + 2.5) // 5) * 5)) \
        * STOREFRONT_PRICE_MULTIPLIER


def _storefront_offer(item: Item) -> Item:
    """Make a purchasable view of a catalog item without modifying the catalog itself."""
    return Item(
        item.code, item.name, item.slot, storefront_price(item), "shop", item.bonuses,
        item.description, item.rarity, item.resale_price, item.drop_weight, item.effect,
    )


def daily_storefront_items(
    entry: str,
    slot: str,
    day: _date | _datetime | str | None = None,
    excluded_codes: set[str] | frozenset[str] | None = None,
    *,
    user_id: str | int | None = None,
) -> tuple[Item, ...]:
    """The stable twelve-hour set of purchasable items for one player and slot.

    This is intentionally a pure function: a restart or second button tap keeps a
    player's stock stable, while another player gets a different selection. `day`
    makes balance tests and previews deterministic without changing the server clock.
    """
    window = storefront_window(day)
    if slot not in SLOT_KEYS:
        return ()
    pool = tuple(sorted(
        (
            item for item in items_for_slot(slot)
            if item.rarity in STOREFRONT_RARITIES
            # A withdrawn item is kept in ITEMS only so old fight snapshots still resolve
            # its code -- putting it on a shelf sells the very thing that was withdrawn.
            # "forge" is likewise a reward you make, not one you buy.
            and item.source in ("shop", "drop")
            and (slot != "weapon" or item.source == "shop")
        ),
        key=lambda item: item.code,
    ))
    excluded = excluded_codes or set()
    player = str(user_id or "preview")
    stock = []
    counts = {"common": STOREFRONT_NORMAL_COUNT, "rare": STOREFRONT_RARE_COUNT}
    for rarity in STOREFRONT_RARITIES:
        candidates = [item for item in pool if item.rarity == rarity and item.code not in excluded]
        candidates.sort(key=lambda item: hashlib.sha256(
            f"{entry}:{player}:{window}:{rarity}:{item.code}".encode("utf-8")
        ).digest())
        stock.extend(candidates[:counts[rarity]])
    # Every personal shelf contains an affordable first purchase when one is available.
    if stock and not any(item.price <= STARTER_WEAPON_MAX_PRICE for item in stock):
        starters = [
            item for item in pool
            if item.code not in {offered.code for offered in stock}
            and item.code not in excluded
            and item.price <= STARTER_WEAPON_MAX_PRICE
        ]
        if starters:
            starter = min(starters, key=lambda item: hashlib.sha256(
                f"{entry}:{player}:{window}:starter:{item.code}".encode("utf-8")
            ).digest())
            common_indexes = [i for i, item in enumerate(stock) if item.rarity == "common"]
            stock[common_indexes[-1] if common_indexes else -1] = starter
    return tuple(_storefront_offer(item) for item in stock)


def daily_storefront_weapons(
    entry: str,
    day: _date | _datetime | str | None = None,
    excluded_codes: set[str] | frozenset[str] | None = None,
    *,
    user_id: str | int | None = None,
) -> tuple[Item, ...]:
    """Compatibility wrapper for the weapon shelf."""
    return daily_storefront_items(entry, "weapon", day, excluded_codes, user_id=user_id)


# Code -> item, built once. find_item used to walk all 596 items looking for one code, and
# it is called thousands of times to serve a single screen: normalising a stored creature
# alone resolves every code in every inventory, and there are as many inventories as there
# are players. Profiled at over seven thousand calls for ONE dungeon fight, which is a few
# million string comparisons for work a dict does in one hop.
_ITEMS_BY_CODE: dict = {item.code: item for item in ITEMS}


def find_item(code: str):
    needle = (code or "").strip().lower()
    return _ITEMS_BY_CODE.get(LEGACY_ITEM_CODES.get(needle, needle))


def items_for_slot(slot: str, source: str | None = None):
    return [
        item for item in ITEMS
        if item.slot == slot and (source is None or item.source == source)
    ]
