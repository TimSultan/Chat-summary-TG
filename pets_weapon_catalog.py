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


try:
    # Weapon passives reuse the amulet engine's hook names verbatim -- there is exactly
    # one effect vocabulary.  The fallback keeps this data module importable on its own.
    from pets_amulet_catalog import EFFECT_HOOKS
except ImportError:  # pragma: no cover - standalone import of the data module
    EFFECT_HOOKS = {}

RARITIES: Final = ("cursed", "common", "uncommon", "rare", "legendary")
SOURCES: Final = ("shop", "drop")
STAT_KEYS: Final = ("strength", "health", "agility", "luck", "armor")

# Shop prices follow the same relative combat weights as the arena power rating.  The
# entry tier is intentionally cheap enough for a first level-1 farm harvest (14 coins):
# a player gets a useful choice immediately, while uncommon and rare gear stay goals.
# Rounding to five keeps larger prices readable and prevents arbitrary catalogue IDs
# from making two equally strong weapons cost hundreds of coins apart.
SHOP_PRICE_POWER_WEIGHTS: Final = {
    "strength": 4,
    "health": 4,
    "agility": 2,
    "luck": 2,
    "armor": 3,
}
# A daily shelf must always contain at least one item at or below this cost whenever an
# unowned starter weapon remains.  It deliberately equals the guaranteed gold from a
# level-1 farm run; the passive +1/hour is a pleasant extra, never a requirement.
STARTER_WEAPON_MAX_PRICE: Final = 14
SHOP_PRICE_RARITY_BASE: Final = {"common": 5, "uncommon": 18, "rare": 55}
SHOP_PRICE_POWER_MULTIPLIER: Final = {"common": 0.30, "uncommon": 0.75, "rare": 1.20}


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
    # Optional passive, in exactly the shape ``pets_amulet_catalog`` already uses.  The
    # combat engine reads effects off every equipped slot, so a weapon needs no new
    # resolver -- only this field and its place in ``raw_item``.  Empty means flat stats.
    effect: tuple[tuple[str, str | int | bool], ...] = ()
    slot: str = "weapon"

    @property
    def price(self) -> int:
        """Compatibility spelling used by ``pets_config.Item``."""
        return self.buy_price

    def bonus_dict(self) -> dict[str, int]:
        """Return a fresh mutable copy for combat/configuration consumers."""
        return dict(self.bonuses)

    def effect_dict(self) -> dict[str, str | int | bool]:
        """Return a fresh mutable copy; empty for a plain flat-stat weapon."""
        return dict(self.effect)

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
            "effect": self.effect_dict(),
        }


# 50 familiar objects x 10 concrete origins = 500 unique names.  These deliberately
# sound like nicknames people would actually give a stupid improvised weapon: no
# corporate jargon, random abstractions or fantasy-name soup.  The second tuple value is
# a short physical joke about the object; stats carry the detailed information.
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
    ("Арматура", "Кривая, тяжёлая и убедительная."),
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
    ("с Авито", "Продавец удалил аккаунт."),
    ("из гаража", "Пахнет бензином."),
    ("на синей изоленте", "Значит, надёжно."),
    ("без чека", "Возврат не примут."),
    ("из общаги", "Комендант уже ищет."),
    ("от соседа", "Он просил вернуть."),
    ("с балкона", "Долетело не сразу."),
    ("за триста рублей", "Торг был уместен."),
    ("в аренду", "Плата за каждый промах."),
    ("из 2007-го", "Пережило три ремонта."),
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
        strength = 12 + (index % 5)  # +12..16, clear of the common ceiling of +10
        patterns = (
            (("strength", strength), ("luck", 2)),
            (("strength", strength), ("agility", 2)),
            (("strength", strength), ("armor", 5), ("luck", -1)),
            (("strength", strength), ("health", 4), ("agility", -1)),
            (("strength", strength), ("agility", 3), ("armor", -2)),
        )
        return patterns[variant]
    if rarity == "rare":
        strength = 20 + (index % 5)  # lands on +21..24, a real step over uncommon's +16
        patterns = (
            (("strength", strength), ("luck", 4), ("agility", -1)),
            (("strength", strength), ("agility", 4), ("armor", -2)),
            (("strength", strength), ("armor", 9), ("luck", -2)),
            (("strength", strength), ("health", 7), ("agility", -2)),
            (("strength", strength), ("agility", 2), ("luck", 2), ("armor", -2)),
        )
        return patterns[variant]
    # Legendary weapons used to top out at the same +20 the very first drop had, which
    # left the best rare weapon -- buyable outright -- a coin flip against a trophy that
    # takes hundreds of wins to earn.  The +30..32 these actually land on restores a
    # premium the holder can feel, and each still carries a real negative rather than
    # pure power creep.  Every legendary also carries a passive (see _LEGENDARY_EFFECTS).
    patterns = (
        (("strength", 30), ("luck", 5), ("agility", -3)),
        (("strength", 32), ("agility", 5), ("armor", -3)),
        (("strength", 28), ("armor", 12), ("luck", -3)),
        (("strength", 29), ("health", 10), ("agility", -3)),
        (("strength", 31), ("agility", 3), ("luck", 3), ("armor", -3)),
    )
    return patterns[variant]


def _effect(code: str, text: str, value: int, **params: int | bool) -> tuple[tuple[str, str | int | bool], ...]:
    """Build one passive in the shared catalogue shape (see ``pets_amulet_catalog``)."""
    return tuple({"code": code, "text": text, "value": value, **params}.items())


# Every legendary weapon carries a passive.  The values below are not guesses: each was
# measured in a mirror match (identical stats, one side with the passive, 4,000 seeded
# fights) and tuned to land at 59-62%.  Codes that read well but proved inert there were
# rejected -- notably first_strike, which moves initiative but not outcomes over ten
# rounds, and piercing/gambler, which barely register even when scaled up.
# Order matches the names: w003 first, then _LEGENDARY_COPY.
_LEGENDARY_EFFECTS: Final = (
    # Швабра на изоленте -- the angry mop swings harder the worse it is going.
    _effect("berserker", "Ниже 45% HP: +28% урона.", 28, threshold=45),
    # Пульт от реальности -- the off button cancels the enemy's best moment.
    _effect("crit_guard", "Первый крит врага слабее на 40%.", 40),
    # Табурет Судного дня -- four legs that answer back.
    _effect("thorns", "Возвращает 12% полученного урона.", 12),
    # Красная кнопка -- nobody knows what it does, so it goes off immediately.
    _effect("opening_blast", "В начале наносит 12% текущего HP врага.", 12),
    # Дедовский кипятильник -- heats the water, the air, and the holder.
    _effect("vampiric", "Лечит 10% нанесённого урона.", 10),
)

# Exactly half of the rare weapons get a passive, tuned the same way to 53-55% -- a
# visible edge that stays clearly under the legendary band.  One-shot heals (second_wind,
# last_stand) are deliberately absent: they measured 63-72% and cannot be tuned down,
# which would make a rare drop hit harder than a legendary one.
_RARE_EFFECTS: Final = (
    _effect("focused", "После промаха: +18% урона следующей атаке.", 18),
    _effect("momentum", "Каждый раунд: +3% урона, максимум +18%.", 3, cap=18),
    _effect("combo", "Попадания: до +16% урона серией.", 5, cap=16),
    _effect("regen", "В конце раунда лечит 6 HP.", 6),
    _effect("retaliation", "После удара: +6 урона следующей атаке.", 6),
)


def _effect_for(rarity: str, rarity_rank: int) -> tuple[tuple[str, str | int | bool], ...]:
    """The passive for one generated weapon, or ``()`` when it stays flat-stat only.

    Legendaries are keyed by rank so the passive matches the hand-written punch-line
    name.  Rares alternate by rank -- every odd one earns a passive, which makes "half of
    rare" exact (25 of 50) without hand-listing codes -- and step through the five rare
    passives in turn so all five actually appear in the catalogue.
    """
    if rarity == "legendary":
        return _LEGENDARY_EFFECTS[rarity_rank % len(_LEGENDARY_EFFECTS)]
    if rarity == "rare" and rarity_rank % 2 == 1:
        return _RARE_EFFECTS[(rarity_rank // 2) % len(_RARE_EFFECTS)]
    return ()


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
    # These IDs are the target of the stick/fork/bone migration aliases. Their identity
    # and stats remain lossless; prices use the current arena-income scale.
    ("w001", "Мамин тапок", "Летит точнее, чем кажется.",
     "common", "shop", 10, 2, 0, (("strength", 6),)),
    ("w002", "Мамина сковородка", "После неё спор окончен.",
     "uncommon", "shop", 65, 13, 0, (("strength", 14), ("luck", 4))),
    # Renamed back to the compressor it was before the 500-weapon catalogue overwrote it
    # with a mop; the description is the original one, recovered from f52cb1e. Its stats
    # are deliberately below the other four legendaries -- this is the one legendary that
    # is a heavy, awkward antique rather than a punch line about power.
    ("w003", "Старый компрессор", "Тяжёлый, гудит и выдаёт идеальное давление.",
     "legendary", "drop", 0, 220, 1, (("strength", 21), ("agility", -3))),
)

# Four generated legendary slots get actual punch-line names instead of inheriting a
# catalogue suffix.  w003 above is the fifth legendary and remains migration-compatible.
_LEGENDARY_COPY: Final = (
    ("Пульт от реальности", "Кнопка выключения всё-таки нашлась."),
    ("Табурет Судного дня", "Четыре ножки. Ни одной надежды."),
    ("Красная кнопка", "Никто не знает, что она делает."),
    ("Дедовский кипятильник", "Греет воду, воздух и обстановку."),
)


def _source_for(rarity: str, rarity_rank: int) -> str:
    # Cursed junk belongs to arena drops, never a shop shelf. The shop offers normal
    # entry/mid-game gear and a few rare aspirational purchases; stronger gear remains
    # arena loot rather than wallet-only progression.
    if rarity == "cursed":
        return "drop"
    if rarity in {"common", "uncommon"}:
        return "shop"
    if rarity == "rare":
        # A handful remain high-end shop goals; 45 are earned through arena drops.
        return "shop" if rarity_rank <= 5 else "drop"
    return "drop"


def shop_price_for_bonuses(rarity: str, bonuses) -> int:
    """Value one shop weapon from its actual arena impact, rounded to five coins."""
    base = SHOP_PRICE_RARITY_BASE[rarity]
    power = max(0, sum(
        SHOP_PRICE_POWER_WEIGHTS[key] * int(value) for key, value in bonuses
    ))
    raw = base + power * SHOP_PRICE_POWER_MULTIPLIER[rarity]
    # The first common weapon is the onboarding purchase: pure +6 strength costs ten.
    # Other commons remain only slightly dearer (10/15/20), while the two higher shop
    # tiers retain a visible scarcity premium.
    rounded = int((raw + 2.5) // 5) * 5
    if rarity == "common":
        return max(10, min(20, rounded))
    return max(5, rounded)


def _prices(rarity: str, source: str, bonuses) -> tuple[int, int]:
    """Return power-based buy and deliberately modest resale prices."""
    if source == "drop":
        # Drops cannot be bought.  Their sale value is a consolation, not a gold faucet.
        return 0, 220 if rarity == "legendary" else 110 if rarity == "rare" else 10
    buy = shop_price_for_bonuses(rarity, bonuses)
    # 20% (rounded down) makes selling a convenience and inventory sink, never an
    # arbitrage route.  The old 5-coin floor became too generous once starter weapons
    # cost ten, so cheap gear can now be sold for its actual low fraction.
    return buy, max(1, buy * 20 // 100)


def _pre_rebalance_buy_price(code: str, rarity: str, source: str) -> int:
    """Price paid before the 2026-08 income rebalance, retained for duplicate refunds."""
    if source != "shop":
        return 0
    if code == "w001":
        return 250
    if code == "w002":
        return 900
    index = int(code[1:]) - 1
    if rarity == "common":
        return 120 + (index % 13) * 30
    if rarity == "uncommon":
        return 450 + (index % 11) * 50
    return 900 + (index % 6) * 100


def _drop_weight(rarity: str, source: str) -> int:
    """Relative arena-drop chance; zero means this weapon is not in the drop pool.

    Forty-five rare weapons at weight 10, 75 cursed weapons at weight 1 and five
    legendary weapons at weight 1 make legendary drops 5 / 530 (about 0.94%) of weapon
    drops. Cursed junk stays noticeable without overwhelming useful rewards.
    """
    if source != "drop":
        return 0
    return 10 if rarity == "rare" else 1


def _build_catalogue() -> tuple[WeaponSpec, ...]:
    entries: list[WeaponSpec] = []
    for code, name, description, rarity, source, buy_price, resale_price, drop_weight, bonuses in _LEGACY_WEAPONS:
        entries.append(WeaponSpec(
            code=code, name=name, description=description, rarity=rarity, source=source,
            buy_price=buy_price, resale_price=resale_price, drop_weight=drop_weight, bonuses=bonuses,
            # w003 is the fifth legendary and takes the first passive; the generated
            # four take the rest.  The other legacy entries are ordinary shop weapons.
            effect=_LEGENDARY_EFFECTS[0] if rarity == "legendary" else (),
        ))
    rarity_seen = {rarity: 0 for rarity in RARITIES}
    # Adjacent codes intentionally rotate both object and concrete origin. Daily storefronts
    # use contiguous code windows, so a grouped Cartesian product would show sixteen
    # near-identical "...с Авито" names at once even though the full catalogue varied.
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
        bonuses = _bonus_tuple(index, rarity)
        buy_price, resale_price = _prices(rarity, source, bonuses)
        description = (
            f"{object_description} Есть подвох."
            if rarity == "cursed"
            else f"{object_description} {theme_description}"
        )
        name = f"{object_name} {suffix}"
        if rarity == "legendary":
            name, description = _LEGENDARY_COPY[rarity_seen[rarity] - 1]
        entries.append(WeaponSpec(
            code=f"w{index + 1:03d}",
            name=name,
            description=description,
            rarity=rarity,
            source=source,
            buy_price=buy_price,
            resale_price=resale_price,
            drop_weight=_drop_weight(rarity, source),
            bonuses=bonuses,
            effect=_effect_for(rarity, rarity_seen[rarity]),
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
PRE_REBALANCE_BUY_PRICES: Final = {
    item.code: _pre_rebalance_buy_price(item.code, item.rarity, item.source)
    for item in WEAPON_SPECS if item.source == "shop"
}


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
    # Every legendary and exactly half the rares carry a passive; nothing below rare does.
    with_effect = [item for item in WEAPON_SPECS if item.effect]
    assert all(item.rarity in {"rare", "legendary"} for item in with_effect)
    assert all(item.effect for item in WEAPON_SPECS if item.rarity == "legendary")
    assert sum(1 for item in WEAPON_SPECS if item.rarity == "rare" and item.effect) == 25
    # Guarded: EFFECT_HOOKS is empty when this data module is imported on its own, and
    # the fallback must stay a fallback rather than becoming an import-time failure.
    assert not EFFECT_HOOKS or all(
        item.effect_dict()["code"] in EFFECT_HOOKS for item in with_effect
    )
    assert all(isinstance(item.effect_dict()["value"], int) for item in with_effect)
    assert all(str(item.effect_dict()["text"]) for item in with_effect)
    # Every declared passive must actually reach the catalogue.  An earlier keying bug
    # silently shipped only three of the five rare passives, which no other check caught.
    for rarity, declared in (("rare", _RARE_EFFECTS), ("legendary", _LEGENDARY_EFFECTS)):
        used = {item.effect_dict()["code"] for item in with_effect if item.rarity == rarity}
        assert used == {dict(effect)["code"] for effect in declared}
    starter_shop_items = [
        item for item in WEAPON_SPECS
        if item.source == "shop" and item.price <= STARTER_WEAPON_MAX_PRICE
    ]
    assert starter_shop_items


_validate_catalogue()


__all__ = [
    "RARITIES", "SOURCES", "STAT_KEYS", "WeaponSpec", "WEAPON_SPECS", "RAW_ITEMS", "WEAPON_COUNT",
    "RARITY_COUNTS", "PRE_REBALANCE_BUY_PRICES", "STARTER_WEAPON_MAX_PRICE", "shop_price_for_bonuses",
]
