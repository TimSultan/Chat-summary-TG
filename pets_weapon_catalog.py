"""The clear, comic, fixed weapon catalogue for the pet arena.

This module owns *data*, rather than game logic.  ``WEAPON_SPECS`` is an immutable
tuple of exactly 500 :class:`WeaponSpec` values.  It is safe for ``pets_config`` to
turn a spec into its mutable ``Item`` object with ``spec.item_arguments()``; the raw
catalogue itself cannot be changed accidentally during a fight.

The catalogue is generated from curated word banks at import time so that it remains
reviewable and deterministic without maintaining a 500-line hand-written list.  Codes
are stable ASCII identifiers (``w001`` through ``w500``), not display names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


RARITIES: Final = ("cursed", "common", "uncommon", "rare", "legendary")
SOURCES: Final = ("shop", "drop")
STAT_KEYS: Final = ("strength", "health", "agility", "luck", "armor")


@dataclass(frozen=True, slots=True)
class WeaponSpec:
    """One immutable catalogue entry.

    ``bonuses`` intentionally is a tuple of pairs, rather than a dict: all raw
    catalogue data is immutable.  ``item_arguments`` supplies the dict expected by
    the existing ``pets_config.Item`` constructor when the catalogue is wired in.
    """

    code: str
    name: str
    description: str
    rarity: str
    source: str
    buy_price: int
    resale_price: int
    drop_weight: int
    bonuses: tuple[tuple[str, int], ...]
    slot: str = "weapon"

    @property
    def price(self) -> int:
        """Compatibility spelling used by ``pets_config.Item``."""
        return self.buy_price

    def bonus_dict(self) -> dict[str, int]:
        """Return a fresh mutable copy for combat/configuration consumers."""
        return dict(self.bonuses)

    def item_arguments(self) -> tuple[str, str, str, int, str, dict[str, int], str]:
        """Arguments in the order accepted by ``pets_config.Item``."""
        return (
            self.code,
            self.name,
            self.slot,
            self.buy_price,
            self.source,
            self.bonus_dict(),
            self.description,
        )

    def raw_item(self) -> dict[str, object]:
        """Dictionary adapter for catalogue/trade code that consumes item records.

        The returned dict is intentionally a new compatibility record.  Canonical
        data remains the frozen :class:`WeaponSpec`; callers must not mutate the
        record expecting to alter the catalogue.
        """
        return {
            "code": self.code,
            "name": self.name,
            "slot": self.slot,
            "price": self.buy_price,
            "source": self.source,
            "bonuses": self.bonus_dict(),
            "description": self.description,
            "rarity": self.rarity,
            "resale_price": self.resale_price,
            "drop_weight": self.drop_weight,
        }


# 50 familiar objects x 10 recognisable situations = 500 unique names.  Every name is
# a plain Russian noun phrase: the joke should be understood immediately, not decoded
# from two unrelated fantasy word banks.  The second tuple value is a deliberately short
# shop description; item stats carry the detailed information.
_OBJECTS: Final = (
    ("Тапок", "Мягкий, но убедительный."),
    ("Сковородка", "Тяжёлая кухонная дипломатия."),
    ("Швабра", "Достаёт даже из угла."),
    ("Половник", "Черпает неприятности."),
    ("Дуршлаг", "Защищает и унижает."),
    ("Кабачок", "С виду овощ. Это ловушка."),
    ("Багет", "Хрустит с угрозой."),
    ("Пульт", "Всегда находит нужную кнопку."),
    ("Клавиатура", "Печатает последний аргумент."),
    ("Мышка", "Курсор навёлся сам."),
    ("Степлер", "Скрепляет бой намертво."),
    ("Дырокол", "Оставляет два вопроса."),
    ("Калькулятор", "Считает шансы без жалости."),
    ("Термос", "Долго держит удар."),
    ("Чайник", "Закипает с пол-оборота."),
    ("Веник", "Выметает с арены."),
    ("Ёршик", "Лучше не уточнять."),
    ("Гантеля", "Весомый аргумент."),
    ("Расчёска", "Наводит боевой порядок."),
    ("Зонт", "Прогнозирует удары."),
    ("Костыль", "Поддерживает наступление."),
    ("Табуретка", "Четыре ножки ярости."),
    ("Подушка", "Усыпляет без сказки."),
    ("Коврик", "Встречает без гостеприимства."),
    ("Банка огурцов", "Открывается только в бою."),
    ("Пакет с пакетами", "Внутри всё самое важное."),
    ("Рулон обоев", "Ремонт переходит в атаку."),
    ("Удлинитель", "Дотягивается до каждого."),
    ("Зарядка", "Подходит не к тому порту."),
    ("Селфи-палка", "Держит врага в кадре."),
    ("Совок", "Собирает последствия."),
    ("Грабли", "Работают повторно."),
    ("Лопата", "Копает до победы."),
    ("Щётка", "Счищает лишний пафос."),
    ("Бутылка", "Пустая, зато звонкая."),
    ("Кружка", "Полна решимости."),
    ("Тостер", "Поджаривает аргументы."),
    ("Миксер", "Взбивает обстановку."),
    ("Фен", "Сдувает уверенность."),
    ("Утюг", "Разглаживает конфликты."),
    ("Пылесос", "Засасывает инициативу."),
    ("Микроволновка", "Греет угрозы изнутри."),
    ("Принтер", "Бьёт только после ошибки."),
    ("Роутер", "Раздаёт по полной."),
    ("Будильник", "Пробуждает инстинкты."),
    ("Домофон", "Не пускает без боя."),
    ("Чемодан", "Настроен на вылет."),
    ("Вешалка", "Цепляется за победу."),
    ("Кочерга", "Поддерживает жар."),
    ("Клавиша Enter", "Подтверждает поражение."),
)

_THEMES: Final = (
    ("последнего предупреждения", "Второго не будет."),
    ("семейного совета", "Решает голосованием."),
    ("внеплановой проверки", "Находит нарушения."),
    ("пятничного созвона", "Заканчивает встречу."),
    ("соседского ремонта", "Слышно через три этажа."),
    ("битвы за парковку", "Место уже ваше."),
    ("защиты диплома", "Вопросов больше нет."),
    ("ночного дожора", "Крошек не оставляет."),
    ("очереди в поликлинике", "Талончик не нужен."),
    ("финального дедлайна", "Сдаёт раньше вас."),
)


def _bonus_tuple(index: int, rarity: str) -> tuple[tuple[str, int], ...]:
    """Create restrained bonuses compatible with the existing combat scale."""
    variant = index % 5
    if rarity == "cursed":
        patterns = (
            (("strength", -4), ("luck", 2)),
            (("agility", -3), ("armor", 5)),
            (("health", -6), ("strength", 2)),
            (("strength", -2), ("agility", -2), ("armor", 7)),
            (("luck", -3), ("strength", 3)),
        )
        return patterns[variant]
    if rarity == "common":
        strength = 6 + (index % 5)  # ordinary shop weapons stay in the requested +6..10 band
        patterns = (
            (("strength", strength),),
            (("strength", strength), ("agility", 1)),
            (("strength", strength), ("luck", 1)),
            (("strength", strength), ("armor", 3)),
            (("strength", strength), ("health", 2)),
        )
        return patterns[variant]
    if rarity == "uncommon":
        strength = 10 + (index % 5)  # +10..14, useful but still shop-scale
        patterns = (
            (("strength", strength), ("luck", 2)),
            (("strength", strength), ("agility", 2)),
            (("strength", strength), ("armor", 5), ("luck", -1)),
            (("strength", strength), ("health", 4), ("agility", -1)),
            (("strength", strength), ("agility", 3), ("armor", -2)),
        )
        return patterns[variant]
    if rarity == "rare":
        strength = 15 + (index % 4)  # +15..18, below the present +20 trophy
        patterns = (
            (("strength", strength), ("luck", 4), ("agility", -1)),
            (("strength", strength), ("agility", 4), ("armor", -2)),
            (("strength", strength), ("armor", 9), ("luck", -2)),
            (("strength", strength), ("health", 7), ("agility", -2)),
            (("strength", strength), ("agility", 2), ("luck", 2), ("armor", -2)),
        )
        return patterns[variant]
    # Legendary weapons deliberately top out at the existing drop's +20 strength;
    # their secondary stat comes with a meaningful negative rather than raw power creep.
    patterns = (
        (("strength", 20), ("luck", 5), ("agility", -3)),
        (("strength", 20), ("agility", 5), ("armor", -3)),
        (("strength", 20), ("armor", 12), ("luck", -3)),
        (("strength", 19), ("health", 10), ("agility", -3)),
        (("strength", 20), ("agility", 3), ("luck", 3), ("armor", -3)),
    )
    return patterns[variant]


def _rarity_for(index: int) -> str:
    """Compatibility helper for the generated (non-legacy) catalogue entries."""
    return _GENERATED_RARITIES[index]


def _interleaved_rarities(counts: dict[str, int]) -> tuple[str, ...]:
    """Spread each rarity smoothly through pages instead of grouping it by tier.

    This deterministic weighted scheduler creates exact counts while avoiding a front
    page full of cursed entries (or a last page full of legendaries).
    """
    total = sum(counts.values())
    score = {rarity: 0 for rarity in RARITIES}
    remaining = dict(counts)
    scheduled: list[str] = []
    for _ in range(total):
        for rarity in RARITIES:
            score[rarity] += counts[rarity]
        winner = max(
            (rarity for rarity in RARITIES if remaining[rarity]),
            key=lambda rarity: (score[rarity], -RARITIES.index(rarity)),
        )
        scheduled.append(winner)
        remaining[winner] -= 1
        score[winner] -= total
    return tuple(scheduled)


# w001..w003 are exact legacy replacements.  The remaining schedule completes the
# public distribution while keeping every rarity mixed through the catalogue.
_GENERATED_RARITIES: Final = _interleaved_rarities({
    "cursed": 75,
    "common": 249,
    "uncommon": 119,
    "rare": 50,
    "legendary": 4,
})

_LEGACY_WEAPONS: Final = (
    # These IDs are the target of the stick/fork/bone migration aliases. Their prices
    # and stats remain lossless; display copy follows the clearer catalogue voice.
    ("w001", "Тапок последнего предупреждения", "Мягкий, но убедительный. Второго не будет.",
     "common", "shop", 250, 50, 0, (("strength", 6),)),
    ("w002", "Сковородка семейного совета", "Тяжёлая кухонная дипломатия. Решает голосованием.",
     "uncommon", "shop", 900, 180, 0, (("strength", 14), ("luck", 4))),
    ("w003", "Швабра внеплановой проверки", "Достаёт даже из угла. Находит нарушения.",
     "legendary", "drop", 0, 220, 1, (("strength", 20), ("agility", -3))),
)


def _source_for(rarity: str, rarity_rank: int) -> str:
    # The shop offers all entry/mid-game gear and some rare aspirational purchases.
    # The remainder is arena loot, keeping truly strong weapons from being wallet-only.
    if rarity in {"cursed", "common", "uncommon"}:
        return "shop"
    if rarity == "rare":
        # A handful remain high-end shop goals; 45 are earned through arena drops.
        return "shop" if rarity_rank <= 5 else "drop"
    return "drop"


def _prices(index: int, rarity: str, source: str) -> tuple[int, int]:
    """Return buy and deliberately modest resale prices."""
    if source == "drop":
        # Drops cannot be bought.  Their sale value is a consolation, not a gold faucet.
        return 0, 220 if rarity == "legendary" else 110
    if rarity == "cursed":
        buy = 35 + (index % 6) * 10
    elif rarity == "common":
        buy = 120 + (index % 13) * 30
    elif rarity == "uncommon":
        buy = 450 + (index % 11) * 50
    else:
        buy = 900 + (index % 6) * 100
    # 20% (rounded down) makes selling a convenience and inventory sink, never an
    # arbitrage route.  Even the cheapest cursed trinket stays below the 25% cap.
    return buy, max(5, buy * 20 // 100)


def _drop_weight(rarity: str, source: str) -> int:
    """Relative arena-drop chance; zero means this weapon is not in the drop pool.

    Forty-five rare weapons at weight 10 and five legendary weapons at weight 1 make
    legendary drops 5 / 455 (about 1.1%) of weapon drops.  This preserves a genuinely
    exciting tier without making it a required progression path.
    """
    if source != "drop":
        return 0
    return 1 if rarity == "legendary" else 10


def _build_catalogue() -> tuple[WeaponSpec, ...]:
    entries: list[WeaponSpec] = []
    for code, name, description, rarity, source, buy_price, resale_price, drop_weight, bonuses in _LEGACY_WEAPONS:
        entries.append(WeaponSpec(
            code=code, name=name, description=description, rarity=rarity, source=source,
            buy_price=buy_price, resale_price=resale_price, drop_weight=drop_weight, bonuses=bonuses,
        ))
    rarity_seen = {rarity: 0 for rarity in RARITIES}
    # Adjacent codes intentionally rotate both object and situation.  Daily storefronts
    # use contiguous code windows, so a grouped Cartesian product would show sixteen
    # near-identical "...дедлайна" names at once even though the full catalogue varied.
    combinations = tuple(
        (*_OBJECTS[index % len(_OBJECTS)],
         *_THEMES[(index // len(_OBJECTS) + index % len(_OBJECTS)) % len(_THEMES)])
        for index in range(len(_OBJECTS) * len(_THEMES))
    )
    for index, (object_name, object_description, suffix, theme_description) in enumerate(
        combinations[3:], start=3
    ):
        if len(entries) == 500:
            break
        rarity = _rarity_for(index - 3)
        rarity_seen[rarity] += 1
        source = _source_for(rarity, rarity_seen[rarity])
        buy_price, resale_price = _prices(index, rarity, source)
        description = (
            f"{object_description} Есть подвох."
            if rarity == "cursed"
            else f"{object_description} {theme_description}"
        )
        entries.append(WeaponSpec(
            code=f"w{index + 1:03d}",
            name=f"{object_name} {suffix}",
            description=description,
            rarity=rarity,
            source=source,
            buy_price=buy_price,
            resale_price=resale_price,
            drop_weight=_drop_weight(rarity, source),
            bonuses=_bonus_tuple(index, rarity),
        ))
    # There are 50 * 10 name combinations, of which the first three are replaced by
    # migration-safe legacy entries.  The early stop above keeps the public range w001..w500.
    return tuple(entries)


WEAPON_SPECS: Final[tuple[WeaponSpec, ...]] = _build_catalogue()
# Compatibility records for item/trade code.  Use only these 500 records when wiring
# the equipment system: existing ``stick``, ``fork`` and ``bone`` are replacements,
# not additions, if the total weapon count must remain exactly 500.
RAW_ITEMS: Final[tuple[dict[str, object], ...]] = tuple(item.raw_item() for item in WEAPON_SPECS)
WEAPON_COUNT: Final = len(WEAPON_SPECS)
RARITY_COUNTS: Final = {rarity: sum(item.rarity == rarity for item in WEAPON_SPECS) for rarity in RARITIES}


def _validate_catalogue() -> None:
    """Fail immediately if a future catalogue edit violates its public contract."""
    assert WEAPON_COUNT == 500
    assert len({item.code for item in WEAPON_SPECS}) == WEAPON_COUNT
    assert len({item.name for item in WEAPON_SPECS}) == WEAPON_COUNT
    assert all(item.code.isascii() and item.code.isalnum() for item in WEAPON_SPECS)
    assert all(item.slot == "weapon" and item.rarity in RARITIES for item in WEAPON_SPECS)
    assert all(item.source in SOURCES for item in WEAPON_SPECS)
    assert all(item.buy_price >= 0 and item.resale_price >= 1 for item in WEAPON_SPECS)
    assert all(item.buy_price == 0 for item in WEAPON_SPECS if item.source == "drop")
    assert all(item.buy_price > 0 for item in WEAPON_SPECS if item.source == "shop")
    assert all(item.bonuses and all(key in STAT_KEYS and isinstance(value, int)
                                   for key, value in item.bonuses) for item in WEAPON_SPECS)
    assert all(item.drop_weight == 0 for item in WEAPON_SPECS if item.source == "shop")
    assert all(item.drop_weight > 0 for item in WEAPON_SPECS if item.source == "drop")
    assert RARITY_COUNTS == {"cursed": 75, "common": 250, "uncommon": 120, "rare": 50, "legendary": 5}


_validate_catalogue()


__all__ = [
    "RARITIES", "SOURCES", "STAT_KEYS", "WeaponSpec", "WEAPON_SPECS", "RAW_ITEMS", "WEAPON_COUNT",
    "RARITY_COUNTS",
]
