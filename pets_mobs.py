"""PVE: the mobs a pet can pick a fight with, and how hard they come out.

Data, not rules. `pets.py` fights these through exactly the same pets_combat.simulate()
that resolves a duel -- a mob is built into an ordinary Fighter and the transcript, the
replay and the fight bank all behave identically. What is different is only what is on
the other side of the ring and what it pays.

WHY A MOB PAYS ABOUT HALF. A duel is player-versus-player: the coins the winner takes are
partly paid BY the loser (see LOSS_GOLD_SHARE), so the arena mints far less than it moves
around. A mob pays entirely out of thin air, and nobody is on the other end to lose
anything, so the same payout would be a pure faucet running at double the rate. Halving it
keeps PVE worth doing without making the arena the slow way to earn.

THREE TIERS, priced off the PLAYER rather than off a fixed stat block. A mob is generated
relative to whoever picked the fight, so it stays a real opponent at every pet level
instead of being trivial at 40 and impossible at 3:

    лёгкий   22% weaker, ±4%     -- usually a win, occasionally a scare
    средний   5% weaker, ±4%     -- a real fight
    сильный   5% stronger, ±4%   -- the risky option, paid accordingly

The ± is per-stat, not per-mob, so an easy mob can still be quicker than you while being
weaker overall, and the same mob is never quite the same twice.

EACH MOB HAS ITS OWN PURSE. `gold` and `loot` below are multipliers on the tier's base,
which is what makes the roster a set of choices rather than a reskin: the courier is fast
money and almost never drops anything, the neighbour with the drill is a slog that pays
badly but rattles loose good loot, and the Авито seller is where the rubies are.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


TIERS: Final = ("easy", "medium", "hard")
TIER_NAMES: Final = {"easy": "лёгкий", "medium": "средний", "hard": "сильный"}

# tier -> (target total combat-power ratio, ± one profile jitter).  A mob is built as
# one coherent version of the player's build, rather than independently multiplying
# every stat.  That avoids the old case where a hard mob rolled high strength, health,
# dodge and crit at once and became much stronger than its displayed tier.
TIER_SCALING: Final = {
    "easy": (0.78, 0.04),
    "medium": (0.95, 0.04),
    "hard": (1.05, 0.04),
}
# What each tier pays, as a multiplier on the halved arena purse. A hard mob is worth
# picking precisely because it is likely to beat you.
TIER_REWARD: Final = {"easy": 0.75, "medium": 1.0, "hard": 1.6}
# Rubies only ever come off a mob (and rarely off the farm). The chance rises steeply with
# tier so the currency tracks risk rather than time spent.
TIER_RUBY_CHANCE: Final = {"easy": 0.10, "medium": 0.20, "hard": 0.38}


@dataclass(frozen=True, slots=True)
class Mob:
    """One opponent, and what beating it is worth.

    `gold` and `loot` and `ruby` are all multipliers, never absolute numbers: absolutes
    here would have to be re-tuned every time the arena's own economy moved, and the whole
    point of pets_config is that the economy has exactly one set of dials.
    """

    code: str
    name: str
    flavour: str
    gold: float          # × the tier purse
    loot: float          # × the base PVE item-drop chance
    ruby: float          # × the tier's ruby chance
    taunt: str           # what it says when you pick the fight


_DATA: Final = (
    ("wb_courier", "Курьер ВБ",
     "Принёс не то, не туда и уже уехал.",
     1.15, 0.6, 0.8,
     "«Заказ на Пушкина, 12? Нет? Ну распишитесь всё равно.»"),

    ("ozon_worker", "Сотрудник Озона",
     "Знает, где твоя посылка. Не скажет.",
     1.0, 1.0, 1.0,
     "«Ваш заказ прибыл в сортировочный центр. Снова.»"),

    ("drill_neighbour", "Сосед с дрелью",
     "Начинает в восемь утра в воскресенье.",
     0.75, 1.5, 0.7,
     "«Я на пять минуточек, тут полочку повесить.»"),

    ("avito_seller", "Продавец на Авито",
     "«Всё работает, просто полежало».",
     0.9, 1.1, 1.8,
     "«Последняя цена — и так отдаю себе в убыток.»"),

    ("primer_can", "Пустой баллон грунта",
     "Шипит, плюётся и красит воздух.",
     0.85, 1.2, 0.9,
     "«Пшшш.» (это было последнее)"),

    ("cat_on_desk", "Кот на рабочем столе",
     "Лёг ровно на палитру. Довольный.",
     0.8, 1.35, 1.1,
     "Смотрит в глаза и медленно двигает лапой кисточку к краю."),

    ("lost_bit", "Улетевшая деталь",
     "Отскочила под шкаф в другом измерении.",
     0.7, 1.6, 1.3,
     "Где-то под плинтусом раздаётся тихое «клац»."),

    ("tax_notice", "Уведомление из налоговой",
     "Пришло в самый неподходящий вечер.",
     1.3, 0.7, 1.4,
     "«Сумма к уплате указана в приложении №1.»"),

    ("marketplace_sale", "Распродажа на маркетплейсе",
     "Скидка 80% на то, что тебе не нужно.",
     1.45, 0.5, 1.6,
     "«В вашей корзине 14 товаров. Купить всё?»"),
)


MOBS: Final[tuple[Mob, ...]] = tuple(
    Mob(code=code, name=name, flavour=flavour, gold=gold, loot=loot, ruby=ruby, taunt=taunt)
    for code, name, flavour, gold, loot, ruby, taunt in _DATA
)
MOB_COUNT: Final = len(MOBS)
_BY_CODE: Final = {mob.code: mob for mob in MOBS}


def find_mob(code: str) -> Mob | None:
    """The mob for this code, or None -- the code may be untrusted client input."""
    return _BY_CODE.get(code)


def _validate_catalogue() -> None:
    assert MOB_COUNT >= 9
    assert len({mob.code for mob in MOBS}) == MOB_COUNT
    assert len({mob.name for mob in MOBS}) == MOB_COUNT
    assert all(mob.code and mob.name and mob.flavour and mob.taunt for mob in MOBS)
    assert all(mob.gold > 0 and mob.loot >= 0 and mob.ruby >= 0 for mob in MOBS)
    assert set(TIER_SCALING) == set(TIERS) == set(TIER_REWARD) == set(TIER_RUBY_CHANCE)
    assert set(TIER_NAMES) == set(TIERS)
    # An easy mob has to be easier than a hard one at every dial, or the tiers are a lie.
    assert TIER_SCALING["easy"][0] < TIER_SCALING["medium"][0] < TIER_SCALING["hard"][0]
    assert TIER_REWARD["easy"] < TIER_REWARD["medium"] < TIER_REWARD["hard"]
    assert TIER_RUBY_CHANCE["easy"] < TIER_RUBY_CHANCE["medium"] < TIER_RUBY_CHANCE["hard"]


_validate_catalogue()


__all__ = [
    "TIERS", "TIER_NAMES", "TIER_SCALING", "TIER_REWARD", "TIER_RUBY_CHANCE",
    "Mob", "MOBS", "MOB_COUNT", "find_mob",
]
