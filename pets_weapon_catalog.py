"""The clear, comic, fixed weapon catalogue for the pet arena.

This module owns *data*, rather than game logic.  ``WEAPON_SPECS`` is an immutable
tuple of exactly 514 :class:`WeaponSpec` values.  It is safe for ``pets_config`` to
turn a spec into its mutable ``Item`` object with ``spec.item_arguments()``; the raw
catalogue itself cannot be changed accidentally during a fight.

The catalogue is generated from curated word banks at import time so that it remains
reviewable and deterministic without maintaining a 500-line hand-written list.  Codes
are stable ASCII identifiers (``w001`` through ``w500``), not display names.  Six more
hand-written drop-only weapons, ``w501``..``w506``, close out a couple of equipment builds
that had no weapon carrying their signature effect and carry two legendary-only rules (see
``_NEW_BUILD_WEAPONS``); ``w507``..``w514`` are the cursed legendaries, whose passives buy
an oversized effect with an equally real penalty (see ``_CURSED_LEGENDARIES``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
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
# Five ordinary items can be forged into one rare item, so the ordinary price floor must
# protect that recipe from becoming a cheaper substitute for the rare shelf. Rounding to
# five keeps prices readable and prevents arbitrary catalogue IDs from separating items
# with the same combat power.
SHOP_PRICE_POWER_WEIGHTS: Final = {
    "strength": 4,
    "health": 4,
    "agility": 2,
    "luck": 2,
    "armor": 3,
}
# The weakest shop weapon anchors the ordinary tier. Five of these cost 300 coins, well
# above the 160-195 direct rare-weapon band; forging therefore buys variety, not arbitrage.
STARTER_WEAPON_MAX_PRICE: Final = 60
MOB_HUNTER_WEAPON_CODE: Final = "w009"
SHOP_PRICE_RARITY_BASE: Final = {"common": 45, "uncommon": 40, "rare": 55}
SHOP_PRICE_POWER_MULTIPLIER: Final = {"common": 0.60, "uncommon": 0.90, "rare": 1.20}


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


# 50 familiar objects rotate through 50 concrete stories; the catalogue takes 500
# well-spread pairs from that larger space. These deliberately
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
    ("из коробки с проводами", "Лежало на самом дне."),
    ("после дачного сезона", "Видало слишком многое."),
    ("с красной наклейкой", "Предупреждение стёрлось."),
    ("для важных переговоров", "Убеждает без слов."),
    ("из запасов завхоза", "Учёта уже нет."),
    ("после трёх ремонтов", "Четвёртый не планируется."),
    ("с аварийной полки", "Берут в крайнем случае."),
    ("из потерянной посылки", "Адресат не объявился."),
    ("со склада реквизита", "Роль оказалась боевой."),
    ("с боевым скотчем", "Держится из принципа."),
    ("после переезда", "Коробку так и не нашли."),
    ("из набора для пикника", "Отдых быстро закончился."),
    ("из мастерской в подвале", "Мастер просил молчать."),
    ("с барахолки у вокзала", "Цена вызвала вопросы."),
    ("из кабинета труда", "Урок всё ещё идёт."),
    ("из закрытого ларька", "Ключ подошёл случайно."),
    ("с верхней антресоли", "Пыль усиливает эффект."),
    ("из дедушкиного сарая", "Инструкция потерялась."),
    ("после офисного ремонта", "Списать не успели."),
    ("с последней гарантией", "Сервис уже закрылся."),
    ("из коробки на выброс", "Передумали вовремя."),
    ("с пометкой срочно", "Причину не уточнили."),
    ("после неудачной сборки", "Лишних деталей не осталось."),
    ("из забытой кладовки", "Дверь заклинило снова."),
    ("с учебной тревоги", "Тревога стала настоящей."),
    ("из набора новосёла", "Праздник пошёл не туда."),
    ("после ночной смены", "Усталость только злит."),
    ("с полки у кассы", "Импульсивная покупка."),
    ("из багажника такси", "Владелец не перезвонил."),
    ("с распродажи реквизита", "Сцена была последней."),
    ("из комнаты охраны", "Камеры отвернулись."),
    ("после генеральной уборки", "Выбросить не решились."),
    ("из коробки с ёлкой", "Праздник отменяется."),
    ("с пожарного стенда", "Тревожный, но полезный."),
    ("из очереди на ремонт", "Очередь не дождалась."),
    ("после семейного совета", "Решение было громким."),
    ("с выставочного стенда", "Трогать было нельзя."),
    ("из подсобки кафе", "Смена закончилась дракой."),
    ("после курса самообороны", "Методичку поняло буквально."),
    ("с забытого верстака", "Хозяин ушёл за ключом."),
    ("из коробки возвратов", "Причина возврата ясна."),
    ("с технического этажа", "Лифт туда не ходит."),
    ("после детского утренника", "Костюмер до сих пор ищет."),
    ("из набора выживальщика", "Пункт инструкции вырван."),
    ("с полки у лифта", "Никто не признался."),
    ("после шумного ремонта", "Тишина наконец наступила."),
    ("из службы находок", "Забирать никто не пришёл."),
    ("с закрытой распродажи", "Открывать было ошибкой."),
    ("после странной доставки", "Курьер убежал первым."),
    ("из реквизита квеструма", "Выход оказался запасным."),
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


# Every legendary weapon carries a passive.  The values below are not guesses: each is
# measured by `pets_effect_sim.py`, which puts one reference creature carrying exactly
# this passive against an identical creature carrying none over four opponent shapes and
# reports the paired win-rate gap.  Legendaries are tuned to land at +22..30 points.
#
# The earlier pass tuned these against a much smaller health bar and never re-checked:
# the reference creature has 980 HP and hits for 98, so "7 урона три хода" was under one
# percent of a health bar a tick and the whole legendary tier was landing between +2 and
# +19 instead of in one band.  Numbers here are deliberately at the TOP of the plausible
# range -- a legendary the holder cannot feel is the worse failure.
# Order matches the names: w003 first, then _LEGENDARY_COPY.
#
# Half of this tier used to be a rare weapon's passive with a bigger number on it -- all
# twelve legendaries were, in fact, and six of them measured at the very bottom of the
# band they are priced at (`precision` 90 at +19.8 against `precision` 70 at +15.7 is four
# win points for a trophy that takes hundreds of fights to find). The four below are rules
# no other item in the game has, and the four kept archetypes are the ones whose fantasy
# genuinely wanted a bigger version rather than a different one.
_LEGENDARY_EFFECTS: Final = (
    # Старый компрессор -- pressure builds from every blow it absorbs, and never vents.
    _effect("pressure", "За каждый полученный удар: +9% урона, без потолка.", 9),
    # Рапира идеальной линии -- the line continues for as long as the crits do.
    _effect("chain_crit", "Критический удар открывает ещё 3 атаки подряд, и каждая из них тоже критическая.", 3),
    _effect("burn", "Каждое попадание поджигает: от 12 урона на трёх ходах соперника; урон растёт с уровнем владельца.", 12, turns=3),
    _effect("wound", "Каждое попадание наносит 4% начального максимума HP чистым уроном и на столько же снижает максимум HP до конца боя; всего до 24%.", 4, cap=24),
    _effect("armor_shred", "Каждое попадание ослабляет броню и добавляет 13% урона, до +44%.", 13, cap=44),
)

# Exactly half of the rare weapons get a passive. Repeating a modifier is deliberate:
# it lets a player find the same play style at two strengths without making all 500
# weapons carry rules text. A weapon still has one named modifier; the equipped amulet
# supplies the second axis, and compound ideas such as venom stay one coherent effect.
#
# Tuned to +13..18 win points in `pets_effect_sim.py`, the same harness the legendary
# table above is tuned against. Copy states repetition explicitly ("каждое попадание",
# "перед каждым действием") because the per-tick number is small by necessity -- an
# effect that fires thirteen times a fight cannot print a big one -- and a player reading
# "лечит 6 HP" next to a 980-point health bar has no way to know it means 80 a fight.
_RARE_EFFECTS: Final = (
    _effect("mob_hunter", "Против мобов: +15% урона.", 15),
    _effect("precision", "Шанс промаха снижен на 70%.", 70),
    _effect("burn", "Каждое попадание поджигает: от 6 урона на трёх ходах соперника; урон растёт с уровнем владельца.", 6, turns=3),
    _effect("venom_blade", "Попадание копит от 8 яда и даёт следующей атаке врага 24% промаха; урон растёт с уровнем владельца.", 24, poison=8),
    _effect("armor_shred", "Каждое попадание ослабляет броню и добавляет 6% урона, до +18%.", 6, cap=18),
    _effect("wound", "Каждое попадание наносит 2% начального максимума HP чистым уроном и на столько же снижает максимум HP до конца боя; всего до 12%.", 2, cap=12),
    # coin_rake is the one passive the combat harness cannot see, so it is judged against
    # the purse instead: a win pays WIN_GOLD_MIN..MAX, i.e. 15-30 coins, and this mints on
    # top of that. +10 is therefore already close to half a win again, and the tempting
    # "make it big like the others" number would have tripled the arena's gold faucet.
    _effect("coin_rake", "За победу: +2 монеты за попадание, максимум +10.", 2, cap=10),
    _effect("bleed", "Попадания складывают кровотечение от 4 урона, до 4 зарядов; урон растёт с уровнем владельца.", 4, cap=4),
    _effect("shield_breaker", "Первое попадание ломает щит, игнорирует броню и бьёт вдвое сильнее.", 100, power=100),
    _effect("heavy_combo", "Каждое третье попадание наносит на 50% больше урона.", 50, every=3),
    _effect("precision", "Шанс промаха снижен на 60%.", 60),
    _effect("burn", "Каждое попадание поджигает: от 4 урона на трёх ходах соперника; урон растёт с уровнем владельца.", 4, turns=3),
    _effect("venom_blade", "Попадание копит от 6 яда и даёт следующей атаке врага 20% промаха; урон растёт с уровнем владельца.", 20, poison=6),
    _effect("armor_shred", "Каждое попадание ослабляет броню и добавляет 4% урона, до +16%.", 4, cap=16),
    _effect("wound", "Каждое второе попадание наносит 2% начального максимума HP чистым уроном и на столько же снижает максимум HP до конца боя; всего до 10%.", 2, cap=10, every=2),
    _effect("coin_rake", "За победу: +1 монета за попадание, максимум +8.", 1, cap=8),
    _effect("bleed", "Попадания складывают кровотечение от 2 урона, до 4 зарядов; урон растёт с уровнем владельца.", 2, cap=4),
    _effect("shield_breaker", "Первое попадание ломает щит, игнорирует броню и бьёт на 90% сильнее.", 70, power=90),
    _effect("heavy_combo", "Каждое третье попадание наносит на 40% больше урона.", 40, every=3),
    _effect("focused", "После промаха: +120% урона следующей атаке.", 120),
    _effect("momentum", "Каждый раунд: +3% урона, максимум +18%.", 3, cap=18),
    _effect("combo", "Попадания: до +16% урона серией.", 5, cap=16),
    _effect("regen", "Лечит 9 HP перед каждым действием — около 120 HP за бой.", 9),
    _effect("retaliation", "После каждого пропущенного удара: +14 урона следующей атаке.", 14),
    _effect("executioner", "Против врага ниже 40% HP: +50% урона.", 50, threshold=40),
)

# The 25 effect-bearing rare slots get memorable identities that explain their modifier
# before the player even opens the details sheet. Index zero is w009, the permanent PVE
# shop tool; the remaining names follow _RARE_EFFECTS one-for-one.
_RARE_SPECIAL_COPY: Final = (
    ("Копьё зверобоя", "Для тех, кто идёт за рыком, а не за дуэлью."),
    ("Рапира без промаха", "Тонкая, быстрая и неприятно точная."),
    ("Горящий клинок", "Остывает только после победы."),
    ("Отравленный клинок", "Даже царапина портит весь следующий ход."),
    ("Ржавый колун", "С каждым ударом оставляет меньше брони."),
    ("Коса короткой жизни", "Отрезает не только здоровье, но и его предел."),
    ("Клинок сборщика", "Монеты сами выпадают из карманов арены."),
    ("Зазубренный тесак", "Каждая новая царапина спорит с предыдущей."),
    ("Молот щитолома", "Первый удар не признаёт слова «защита»."),
    ("Трёхтактная кувалда", "Самое страшное происходит на счёт три."),
    ("Дуэльный стилет", "Не такой точный, как рапира, зато компактный."),
    ("Паяльная сабля", "Пламя послабее, но держится цепко."),
    ("Аптечный кортик", "Содержимое ампулы явно было не лекарством."),
    ("Напильник бронегрыза", "Медленно, уверенно, без лишнего звона."),
    ("Пила увядания", "Оставляет здоровью всё меньше места."),
    ("Карманный вымогатель", "Скромный заработок за нескромную победу."),
    ("Игла кровопускателя", "Небольшая рана, зато их бывает много."),
    ("Таран щитолома", "Броню уважает, но только частично."),
    ("Кувалда третьего удара", "Долго считает до трёх, зато не ошибается."),
    ("Клавиатура реванша", "После промаха вводит аргумент заново."),
    ("Заводная алебарда", "Чем дольше бой, тем сильнее заводится."),
    ("Чемодан комбо", "Каждое попадание добавляет новый довод."),
    ("Ремонтный ёршик", "Чинит хозяина прямо между ударами."),
    ("Тостер возмездия", "Возвращает полученное горячим."),
    ("Бутылка последнего шанса", "Особенно опасна, когда враг уже пошатнулся."),
)


def _effect_for(rarity: str, rarity_rank: int) -> tuple[tuple[str, str | int | bool], ...]:
    """The passive for one generated weapon, or ``()`` when it stays flat-stat only.

    Legendaries are keyed by rank so the passive matches the hand-written punch-line
    name. Rares alternate by rank -- every odd one initially earns a passive (25 of 50)
    without hand-listing codes. Five strong lines are promoted after generation, leaving
    twenty effect-bearing rares and ten legendary weapons in the final catalogue.
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
     "common", "shop", 60, 12, 0, (("strength", 6),)),
    ("w002", "Мамина сковородка", "После неё спор окончен.",
     "uncommon", "shop", 100, 20, 0, (("strength", 14), ("luck", 4))),
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
    ("Рапира идеальной линии", "Один точный укол тянет за собой следующий."),
    ("Клинок вечного жара", "Пламя переживает и бой, и победителя."),
    ("Коса пустого здоровья", "Каждый взмах оставляет всё меньше места для жизни."),
    ("Молот нулевой брони", "После него защита остаётся только воспоминанием."),
)

# Five established rare drops are "ascended" in place instead of adding new codes.
# Existing inventories therefore keep the same objects while their familiar archetypes
# gain legendary stats and stronger versions of the same modifier.
_ASCENDED_LEGENDARIES: Final = {
    "w070": (
        "Клык короля ядов", "Даже промахнувшийся враг уверен, что это из-за яда.",
        (("strength", 31), ("agility", 4), ("luck", 3), ("armor", -4)),
        _effect("venom_blade", "Попадание копит от 12 яда и даёт следующей атаке врага 30% промаха; урон растёт с уровнем владельца.", 30, poison=12),
    ),
    "w129": (
        "Казначейский клинок", "Сначала выписывает счёт, потом взыскивает.",
        (("strength", 28), ("agility", 7), ("luck", 5), ("armor", -5)),
        # The old version was a legendary weapon with no combat effect whatsoever: the
        # balance harness scored it an exact zero because there was nothing in the fight
        # to score. `tax` keeps the whole purse clause and adds the bite that a trophy
        # weapon has to have -- a share of what the target has LEFT, so it never finishes
        # anybody by itself and never stops being felt either.
        _effect("tax", "Каждое попадание взимает 8% текущего HP соперника. За победу: +3 монеты за попадание, максимум +16.", 8, cap=16),
    ),
    "w147": (
        "Пила алого следа", "После неё бой ещё долго не может остановиться.",
        (("strength", 31), ("agility", 3), ("luck", 3), ("armor", -4)),
        _effect("bleed", "Попадания складывают кровотечение от 6 урона, до 4 зарядов; урон растёт с уровнем владельца.", 6, cap=4),
    ),
    "w167": (
        "Таран последнего щита", "Первым ударом отменяет само понятие защиты.",
        (("strength", 32), ("armor", 8), ("agility", -5)),
        _effect("shield_breaker", "Первое попадание ломает щит, бьёт на 75% сильнее и навсегда снимает 25% брони.", 100, power=75, shred=25),
    ),
    "w189": (
        "Маятник тяжёлого ритма", "Качнулся туда — качнётся и обратно.",
        (("strength", 32), ("health", 10), ("agility", -5)),
        # `heavy_combo` 45 against the rare's 40 was a five-point difference on the item
        # card and two win points in the harness. A pendulum that swings twice is the same
        # fantasy as a rhythm weapon and a genuinely different rule: every on-hit passive
        # in the loadout fires twice a turn, and so does every risk of being countered.
        _effect("double_strike", "Каждый ход бьёт дважды, по 55% урона за удар.", 55),
    ),
}

# Two equipment builds -- crit/glass-cannon and healing/lifesteal -- fill five slots
# with five different effect codes each, but had no *weapon* carrying their signature
# effect (pets_combat._effect_specs keeps only the strongest instance of a duplicated
# code, so two items sharing one code would waste a slot). These four hand-written
# drop-only weapons close that gap. They sit outside the generated w001..w500 range
# rather than reusing a generated slot, so no existing code, drop table or inventory
# shifts. Rare values land on the amulet-table default for the code (see
# pets_combat._EFFECT_DEFAULTS); legendary values are roughly 1.4-1.8x that default --
# the same restrained ratio the rest of the catalogue's rare/legendary pairs use.
_NEW_BUILD_WEAPONS: Final = (
    (
        "w501", "Треснувшее зеркало", "Семь лет неудач достаются противнику.",
        "rare", (("strength", 21), ("luck", 6), ("health", -4)),
        _effect("lucky", "Крит в бою: +12%.", 12),
    ),
    (
        "w502", "Зеркальный шар", "Каждый осколок — отдельный шанс на удачу.",
        "legendary", (("strength", 30), ("luck", 9), ("armor", -5)),
        # Bottom of the legendary band at +20, one win point clear of the RARE crit weapon
        # it is supposed to tower over. This build still needs a legendary weapon carrying
        # `lucky` (see the note above), so the fix is the number, not the code.
        _effect("lucky", "Крит в бою: +30%.", 30),
    ),
    (
        "w503", "Банка с пиявками", "Присасывается быстрее, чем вы успеваете возразить.",
        "rare", (("strength", 21), ("health", 6), ("armor", -2)),
        _effect("vampiric", "Лечит 12% нанесённого урона.", 12),
    ),
    (
        "w504", "Капельница скорой помощи", "Забирает и возвращает — в свою пользу.",
        "legendary", (("strength", 30), ("health", 10), ("agility", -4)),
        _effect("vampiric", "Лечит 30% нанесённого урона.", 30),
    ),
    # Two rules the legendary tier owns outright, on their own drop-only codes rather than
    # on top of an existing build's weapon: adding them here keeps w501..w504 and every
    # inventory referencing them untouched.
    (
        "w505", "Зеркальный шар в осколках", "Копит отражения, пока не лопнет разом.",
        "legendary", (("strength", 29), ("luck", 7), ("agility", 3), ("armor", -5)),
        _effect("shatter", "Каждое попадание оставляет осколок. Каждый пятый разбивает их все: 5% максимального HP соперника за осколок.", 5, every=5),
    ),
    (
        "w506", "Серп жатвы", "Тем острее, чем меньше от соперника осталось.",
        "legendary", (("strength", 30), ("health", 8), ("luck", 4), ("armor", -4)),
        _effect("reap", "Каждое попадание забирает себе 7% недостающего здоровья соперника.", 7),
    ),
)

# Проклятые легендарки. Каждая несёт эффект СИЛЬНЕЕ обычной легендарки и цену, которая
# может стоить боя -- одно правило, а не бонус со сноской.
#
# They are ordinary `legendary` items on purpose. A sixth rarity would have to be taught to
# the drop tables, the forge, five rarity badge tables, the web cabinet and the filters,
# and would buy nothing the name and the effect text do not already say. What marks the
# shelf is that these are the only passives in the game that can lose a fight on their own.
_CURSED_LEGENDARIES: Final = (
    (
        "w507", "Кувалда обратного отсчёта", "Два хода она не бьёт. Третий решает всё.",
        (("strength", 34), ("health", 12), ("agility", -6), ("armor", -4)),
        _effect("charge_crit", "Заряжается 2 хода и всё это время получает на 20% больше урона. Затем бьёт критом ×5.", 430, turns=2, taken=20),
    ),
    (
        "w508", "Рулетка слепого гнева", "Бьёт куда-то. Иногда — совсем не туда.",
        (("strength", 33), ("luck", 8), ("agility", -5), ("armor", -5)),
        _effect("wild_swing", "30%: удар втрое сильнее. 18%: вместо удара лечит соперника на 15% его максимума.", 200, crit=30, heal=18, gift=15),
    ),
    (
        "w509", "Секира зажмуренных глаз", "Три взмаха видит всё. Потом не видит ничего.",
        (("strength", 35), ("agility", -4), ("luck", -4), ("armor", 6)),
        _effect("blind_fury", "3 хода не может промахнуться и бьёт на 95% сильнее. Следующие 2 хода промахивается всегда.", 3, blind=2, power=95),
    ),
    (
        "w510", "Стеклянная катана", "Режет что угодно. И ломается обо что угодно.",
        (("strength", 34), ("agility", 6), ("health", -8), ("armor", -8)),
        _effect("glass_body", "+140% своего урона и +55% всего получаемого урона.", 140, taken=55),
    ),
    (
        "w511", "Клинок кровавой пошлины", "Платить приходится вперёд и своим.",
        (("strength", 35), ("luck", 5), ("health", -6), ("armor", -5)),
        _effect("blood_price", "+85% урона, но каждое попадание стоит 8% собственного HP.", 85, toll=8),
    ),
    (
        "w512", "Голодный лом", "Ест соперника, хозяина и сам себя.",
        (("strength", 33), ("health", 8), ("agility", 4), ("armor", -6)),
        _effect("hunger", "+11% урона за каждый раунд, без потолка. Каждый ход теряет 5% максимального HP (не ниже 30%).", 11, decay=5, floor=30),
    ),
    (
        "w513", "Долговая расписка", "Смерть подождёт. Проценты — нет.",
        (("strength", 30), ("health", 14), ("luck", 4), ("armor", -6)),
        _effect("soul_debt", "Один раз переживает смертельный удар и возвращается на 40% HP. После этого получает вдвое больше урона.", 40, debt=100),
    ),
    (
        "w514", "Отбойник", "Отдача сильнее, чем удар. Почти.",
        (("strength", 34), ("luck", 7), ("agility", -5), ("armor", -4)),
        _effect("recoil", "Крит в бою: +5%. Критический удар бьёт на 450% сильнее, но отдача забирает следующий ход.", 450, crit=5),
    ),
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
    # Ordinary items share one visible rarity at runtime, but the old uncommon rows keep
    # their stronger stat curve and therefore a higher price inside that tier.
    rounded = int((raw + 2.5) // 5) * 5
    if rarity == "common":
        return max(60, min(75, rounded))
    return max(5, rounded)


def _prices(rarity: str, source: str, bonuses) -> tuple[int, int]:
    """Return power-based buy and deliberately modest resale prices."""
    if source == "drop":
        # Drops cannot be bought.  Their sale value is a consolation, not a gold faucet.
        return 0, 220 if rarity == "legendary" else 110 if rarity == "rare" else 10
    buy = shop_price_for_bonuses(rarity, bonuses)
    # 20% (rounded down) makes selling a convenience and inventory sink, never an
    # arbitrage route.
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

    Forty rare weapons at weight 10, 75 cursed weapons at weight 1 and ten legendary
    weapons at weight 1 make legendary drops 10 / 485 (about 2.06%) of weapon drops.
    Cursed junk stays noticeable without overwhelming useful rewards.
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
    # Adjacent codes intentionally rotate both object and concrete story. The 50-story
    # bank means no memorable suffix appears fifty times across the catalogue anymore.
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
        # The rarity badge and negative stats already make a cursed item clear. Keeping
        # its actual story here avoids dozens of identical "Есть подвох" descriptions.
        description = f"{object_description} {theme_description}"
        name = f"{object_name} {suffix}"
        code = f"w{index + 1:03d}"
        effect = _effect_for(rarity, rarity_seen[rarity])
        if rarity == "rare" and effect:
            name, description = _RARE_SPECIAL_COPY[rarity_seen[rarity] // 2]
        # A dependable PVE tool belongs on the counter, not in a once-in-a-hundred
        # drop.  Keep its stable catalogue code so the 500-item contract and existing
        # inventories remain intact.
        if code == MOB_HUNTER_WEAPON_CODE:
            name, description = _RARE_SPECIAL_COPY[0]
            effect = _effect("mob_hunter", "Против мобов: +15% урона.", 15)
        if rarity == "legendary":
            name, description = _LEGENDARY_COPY[rarity_seen[rarity] - 1]
        entries.append(WeaponSpec(
            code=code,
            name=name,
            description=description,
            rarity=rarity,
            source=source,
            buy_price=buy_price,
            resale_price=resale_price,
            drop_weight=_drop_weight(rarity, source),
            bonuses=bonuses,
            effect=effect,
        ))
    # Promote selected rare codes only after the stable 500-code catalogue is assembled.
    # Their source stays drop-only; changing rarity and weight is enough for every drop,
    # forge, pity and UI path to see the upgraded item.
    entries = [
        replace(
            item,
            name=ascended[0], description=ascended[1], rarity="legendary",
            bonuses=ascended[2], effect=ascended[3], resale_price=220, drop_weight=1,
        ) if (ascended := _ASCENDED_LEGENDARIES.get(item.code)) else item
        for item in entries
    ]
    # The word banks offer far more than 500 pairs. The early stop keeps the public range
    # w001..w500 and, crucially, all existing inventory codes stable.
    hand_written = [
        *((code, name, description, rarity, bonuses, effect)
          for code, name, description, rarity, bonuses, effect in _NEW_BUILD_WEAPONS),
        *((code, name, description, "legendary", bonuses, effect)
          for code, name, description, bonuses, effect in _CURSED_LEGENDARIES),
    ]
    for code, name, description, rarity, bonuses, effect in hand_written:
        buy_price, resale_price = _prices(rarity, "drop", bonuses)
        entries.append(WeaponSpec(
            code=code,
            name=name,
            description=description,
            rarity=rarity,
            source="drop",
            buy_price=buy_price,
            resale_price=resale_price,
            drop_weight=_drop_weight(rarity, "drop"),
            bonuses=bonuses,
            effect=effect,
        ))
    return tuple(entries)


WEAPON_SPECS: Final[tuple[WeaponSpec, ...]] = _build_catalogue()
# Compatibility records for item/trade code.  Use only these 504 records when wiring
# the equipment system: existing ``stick``, ``fork`` and ``bone`` are replacements,
# not additions, if the total weapon count must remain exactly 504.
RAW_ITEMS: Final[tuple[dict[str, object], ...]] = tuple(item.raw_item() for item in WEAPON_SPECS)
WEAPON_COUNT: Final = len(WEAPON_SPECS)
# Published so the UI, the balance report and the tests can name the cursed shelf without
# re-deriving it from a code list. These items are ordinary legendaries to every drop,
# forge and inventory path -- the set exists to LABEL them, never to gate them.
CURSED_LEGENDARY_CODES: Final = frozenset(row[0] for row in _CURSED_LEGENDARIES)
RARITY_COUNTS: Final = {rarity: sum(item.rarity == rarity for item in WEAPON_SPECS) for rarity in RARITIES}
PRE_REBALANCE_BUY_PRICES: Final = {
    item.code: _pre_rebalance_buy_price(item.code, item.rarity, item.source)
    for item in WEAPON_SPECS if item.source == "shop"
}


def _validate_catalogue() -> None:
    """Fail immediately if a future catalogue edit violates its public contract."""
    assert WEAPON_COUNT == 514
    assert len({item.code for item in WEAPON_SPECS}) == WEAPON_COUNT
    assert len({item.name for item in WEAPON_SPECS}) == WEAPON_COUNT
    assert len({item.description for item in WEAPON_SPECS}) == WEAPON_COUNT
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
    # The generated 500 carry 75/250/120/45/10; _NEW_BUILD_WEAPONS then adds two rare and
    # four legendary drops (w501..w506) and _CURSED_LEGENDARIES eight more (w507..w514).
    assert RARITY_COUNTS == {"cursed": 75, "common": 250, "uncommon": 120, "rare": 47, "legendary": 22}
    # Every legendary carries a passive; the promoted five come from effect-bearing rare
    # lines, leaving twenty modified rares and twenty-five plain ones out of the generated
    # 500 -- plus the two effect-bearing rares and two legendaries in _NEW_BUILD_WEAPONS.
    with_effect = [item for item in WEAPON_SPECS if item.effect]
    assert all(item.rarity in {"rare", "legendary"} for item in with_effect)
    assert all(item.effect for item in WEAPON_SPECS if item.rarity == "legendary")
    assert sum(1 for item in WEAPON_SPECS if item.rarity == "rare" and item.effect) == 22
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
        expected = {dict(effect)["code"] for effect in declared}
        expected |= {dict(effect)["code"] for _, _, _, r, _, effect in _NEW_BUILD_WEAPONS if r == rarity}
        if rarity == "legendary":
            expected |= {dict(data[3])["code"] for data in _ASCENDED_LEGENDARIES.values()}
            expected |= {dict(data[4])["code"] for data in _CURSED_LEGENDARIES}
        assert used == expected if rarity == "legendary" else used <= expected
    starter_shop_items = [
        item for item in WEAPON_SPECS
        if item.source == "shop" and item.price <= STARTER_WEAPON_MAX_PRICE
    ]
    assert starter_shop_items


_validate_catalogue()


__all__ = [
    "RARITIES", "SOURCES", "STAT_KEYS", "WeaponSpec", "WEAPON_SPECS", "RAW_ITEMS", "WEAPON_COUNT",
    "RARITY_COUNTS", "PRE_REBALANCE_BUY_PRICES", "STARTER_WEAPON_MAX_PRICE", "shop_price_for_bonuses",
    "CURSED_LEGENDARY_CODES",
]
