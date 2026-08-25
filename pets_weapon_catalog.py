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
STAT_KEYS: Final = ("strength", "health", "agility", "luck", "magic", "armor")
# Which stat a weapon makes its wearer's ordinary swing read. The names match
# pets_config.WEAPON_SCALING_*; they are repeated here rather than imported so this data
# module stays importable on its own, exactly like EFFECT_HOOKS above.
SCALINGS: Final = ("strength", "magic", "hybrid")

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
    # Same shelf price as Strength: on a magic weapon this stat is the swing AND the
    # scroll line, so a magic shop weapon costs what an equivalent steel one costs.
    "magic": 4,
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
    # "strength" for every weapon in the catalogue's first five hundred; the magic shelf
    # (w527..w631) is what introduced the other two.
    scaling: str = "strength"
    # See pets_config.Item: cursed is a property that rides alongside rarity, so the three
    # rungs of the cursed ladder can be `cursed`, `rare` and `legendary` without a sixth
    # rarity existing anywhere. Every entry of the `cursed` RARITY is also cursed by
    # definition; `_build_catalogue` sets that rather than each row repeating it.
    cursed: bool = False

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
            "cursed": self.cursed,
            "scaling": self.scaling,
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
    _effect("burn", "Поджигает на 3 хода: 10% урона за ход, и пламя разгорается — каждый ход на 35% сильнее.", 10, turns=3, grow=35),
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
    _effect("burn", "Поджигает на 3 хода: 5% урона за ход, и пламя разгорается — каждый ход на 25% сильнее.", 5, turns=3, grow=25),
    _effect("venom_blade", "Отравляет на 8% урона. Следующая атака соперника: 24% промаха и −9% урона.", 24, poison=8, weaken=9),
    _effect("armor_shred", "Каждое попадание ослабляет броню и добавляет 6% урона, до +18%.", 6, cap=18),
    _effect("wound", "Каждое попадание наносит 2% начального максимума HP чистым уроном и на столько же снижает максимум HP до конца боя; всего до 12%.", 2, cap=12),
    # coin_rake is the one passive the combat harness cannot see, so it is judged against
    # the purse instead: a win pays WIN_GOLD_MIN..MAX, i.e. 15-30 coins, and this mints on
    # top of that. +10 is therefore already close to half a win again, and the tempting
    # "make it big like the others" number would have tripled the arena's gold faucet.
    _effect("coin_rake", "За победу: +2 монеты за попадание, максимум +10.", 2, cap=10),
    _effect("bleed", "Кровотечение: 6% урона за ход за заряд, до 4 зарядов, до конца боя. На полной ране лечение соперника режется на 40%.", 6, cap=4, heal_cut=40),
    _effect("shield_breaker", "Первое попадание ломает щит, игнорирует броню и бьёт вдвое сильнее.", 100, power=100),
    _effect("heavy_combo", "Каждое третье попадание наносит на 50% больше урона.", 50, every=3),
    _effect("precision", "Шанс промаха снижен на 60%.", 60),
    _effect("burn", "Поджигает на 3 хода: 4% урона за ход, и пламя разгорается — каждый ход на 20% сильнее.", 4, turns=3, grow=20),
    _effect("venom_blade", "Отравляет на 7% урона. Следующая атака соперника: 20% промаха и −8% урона.", 20, poison=7, weaken=8),
    _effect("armor_shred", "Каждое попадание ослабляет броню и добавляет 4% урона, до +16%.", 4, cap=16),
    _effect("wound", "Каждое второе попадание наносит 2% начального максимума HP чистым уроном и на столько же снижает максимум HP до конца боя; всего до 10%.", 2, cap=10, every=2),
    _effect("coin_rake", "За победу: +1 монета за попадание, максимум +8.", 1, cap=8),
    _effect("bleed", "Кровотечение: 3% урона за ход за заряд, до 4 зарядов, до конца боя. На полной ране лечение соперника режется на 30%.", 3, cap=4, heal_cut=30),
    _effect("shield_breaker", "Первое попадание ломает щит, игнорирует броню и бьёт на 90% сильнее.", 70, power=90),
    _effect("heavy_combo", "Каждое третье попадание наносит на 40% больше урона.", 40, every=3),
    _effect("focused", "После промаха: +120% урона следующей атаке.", 120),
    _effect("momentum", "Каждый раунд: +3% урона, максимум +18%.", 3, cap=18),
    _effect("combo", "Попадания: до +16% урона серией.", 5, cap=16),
    _effect("regen", "Лечит 9 HP перед каждым действием — около 120 HP за бой.", 9),
    _effect("retaliation", "После каждого пропущенного удара: +24% урона следующей атаке.", 24),
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
        _effect("venom_blade", "Отравляет на 14% урона. Следующая атака соперника: 30% промаха и −14% урона.", 30, poison=14, weaken=14),
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
        _effect("bleed", "Кровотечение: 9% урона за ход за заряд, до 4 зарядов, до конца боя. На полной ране лечение соперника режется на 60%.", 9, cap=4, heal_cut=60),
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


# Редкие проклятые пушки -- средняя ступень проклятой лестницы.
#
# The cursed line used to be two disconnected things: seventy-five junk weapons at the
# bottom whose only purpose was to be melted, and eight legendary curses at the top. There
# was no rung between them, so "проклятое" named both the worst items in the game and some
# of the best. These twelve are that rung, and they are what makes the ladder real:
# six cursed junk forge into one of these, five of these forge into a legendary curse.
#
# Each carries the SMALLER version of a legendary curse, which is the same rare/legendary
# pairing the ordinary catalogue uses everywhere else -- a player who finds one learns the
# rule cheaply before meeting it at full strength. The last four have no legendary parent
# and are the shelf's own ideas.
_RARE_CURSED_WEAPONS: Final = (
    (
        "w515", "Кувалда короткого счёта", "Замахивается коротко. Успевает чаще.",
        (("strength", 23), ("health", 6), ("agility", -4), ("armor", -3)),
        # One turn of wind-up, not the legendary's two. The rare rung is a different
        # RHYTHM rather than a smaller number, because this passive has almost no room
        # between the two: at a two-turn charge the break-even multiplier is x4 and the
        # legendary's x5.3 is already worth thirty win points, so a "smaller two-turn
        # charge" is either dead or nearly the legendary.
        _effect("charge_crit", "Заряжается ход и всё это время получает на 20% больше урона. Затем бьёт критом ×2.8.", 180, turns=1, taken=20),
    ),
    (
        "w516", "Орлянка драчуна", "Подбрасывает монетку прямо посреди замаха.",
        (("strength", 22), ("luck", 6), ("agility", -3), ("armor", -3)),
        _effect("wild_swing", "28%: удар вдвое сильнее. 18%: вместо удара лечит соперника на 10% его максимума.", 110, crit=28, heal=18, gift=10),
    ),
    (
        "w517", "Тесак вслепую", "Два взмаха по делу, два в белый свет.",
        (("strength", 24), ("agility", -3), ("luck", -3), ("armor", 4)),
        _effect("blind_fury", "2 хода не может промахнуться и бьёт на 90% сильнее. Следующие 2 хода промахивается всегда.", 2, blind=2, power=90),
    ),
    (
        "w518", "Бутылочная роза", "Острее некуда, держится на честном слове.",
        (("strength", 23), ("agility", 5), ("health", -5), ("armor", -6)),
        _effect("glass_body", "+88% своего урона и +45% всего получаемого урона.", 88, taken=45),
    ),
    (
        "w519", "Долговое шило", "Маленький долг, зато ежедневный.",
        (("strength", 24), ("luck", 4), ("health", -4), ("armor", -3)),
        _effect("blood_price", "+60% урона, но каждое попадание стоит 6% собственного HP.", 60, toll=6),
    ),
    (
        "w520", "Тощий лом", "Ест мало, но каждый раунд.",
        (("strength", 22), ("health", 5), ("agility", 3), ("armor", -4)),
        _effect("hunger", "+5% урона за каждый раунд, без потолка. Каждый ход теряет 3% максимального HP (не ниже 40%).", 5, decay=3, floor=40),
    ),
    (
        "w521", "Мятая расписка", "Отсрочка есть. Условия так себе.",
        (("strength", 21), ("health", 9), ("luck", 3), ("armor", -4)),
        _effect("soul_debt", "Один раз переживает смертельный удар и возвращается на 25% HP. После этого получает на 70% больше урона.", 25, debt=70),
    ),
    (
        "w522", "Ручной отбойник", "Отдача в плечо, но какой звук.",
        (("strength", 23), ("luck", 5), ("agility", -3), ("armor", -3)),
        _effect("recoil", "Крит в бою: +3%. Критический удар бьёт на 230% сильнее, но отдача забирает следующий ход.", 230, crit=3),
    ),
    (
        "w523", "Пиявочный крюк", "Тянет чужое, забывает своё.",
        (("strength", 23), ("health", 7), ("agility", -3), ("armor", -4)),
        _effect("reap", "Каждое попадание забирает себе 4% недостающего здоровья соперника.", 4),
    ),
    (
        "w524", "Ржавая пошлина", "Берёт немного и со всех.",
        (("strength", 22), ("agility", 5), ("luck", 4), ("armor", -4)),
        _effect("tax", "Каждое попадание взимает 5% текущего HP соперника. За победу: +2 монеты за попадание, максимум +10.", 5, cap=10),
    ),
    (
        "w525", "Погнутый маятник", "Качается неровно, зато дважды.",
        (("strength", 23), ("health", 7), ("agility", -4), ("armor", -3)),
        _effect("double_strike", "Каждый ход бьёт дважды, по 48% урона за удар.", 48),
    ),
    (
        "w526", "Треснувший манометр", "Стрелка давно за красной чертой.",
        (("strength", 22), ("health", 6), ("armor", -3), ("luck", 3)),
        _effect("pressure", "За каждый полученный удар: +5% урона, без потолка.", 5),
    ),
)


# --------------------------------------------------------------------- волшебное оружие
#
# Магия arrived as a sixth stat with nothing to hold: scrolls read off it, and every one
# of the five hundred weapons above makes its wearer's swing read Strength. A caster
# could buy scroll damage and then punch like a level-one pet for the other nine turns of
# the fight, which is not a build -- it is a handicap with a theme.
#
# These 105 are the other half. A magic weapon declares `scaling`, and `derive` reads the
# named stat instead of Strength for the ORDINARY swing:
#
#   * 80 of them scale from Магия alone. One stat then buys the whole fight, which is
#     exactly what makes the build worth its gold -- and exactly why it is fragile:
#     Strength quietly carries HP_PER_STRENGTH_WITH_SKILLS, so a pure caster fights in a
#     health bar four hundred points shorter than the equivalent brawler's.
#   * 25 of them are hybrids and average the two stats. Half a swing from each is worth
#     less than a full one from either, so the flat bonuses are correspondingly wider --
#     and the stat cost curve (level ** 1.5) pays a split build back for the difference.
#
# Twenty-one per rarity, five of them hybrid, exactly as commissioned. Rares and
# legendaries carry passives written against the scroll loadout rather than the swing:
# every one of them is worth nothing at all to a fighter with four empty slots.
_MAGIC_OBJECTS: Final = {
    "cursed": (
        ("Палочка из «всё по 50»", "Искрит ровно один раз и всегда не туда."),
        ("Посох на чужих батарейках", "Заряда хватает до первого честного удара."),
        ("Гримуар с вырванной серединой", "Начало бодрое, конец печальный."),
        ("Кристалл из кальяна", "Светится и пахнет вишней."),
        ("Жезл-указка", "Кот в восторге, соперник — нет."),
        ("Оберег из пищевой фольги", "Ловит сигнал, но не магию."),
        ("Свеча из морозилки", "Горит неохотно и с укором."),
        ("Метла без прутьев", "Летает низко и недолго."),
        ("Бубен с трещиной", "Ритм есть, смысла нет."),
        ("Пробирка с «эликсиром»", "На дне что-то шевелится."),
        ("Руна, нарисованная маркером", "Стирается от волнения."),
        ("Шар из снежного шара", "Внутри вечная метель и пластик."),
        ("Кадило из консервной банки", "Дым есть, благословения нет."),
        ("Свиток из кассовой ленты", "Заклинание длинное, чек длиннее."),
        ("Череп-говорун без челюсти", "Мычит пророчества."),
        ("Пентаграмма на липучке", "Отклеивается на третьем ходу."),
        ("Ржавый меч с рунной насечкой", "Руны стёрлись, зазубрины остались."),
        ("Топор с приклеенным кристаллом", "Клей держится лучше, чем магия."),
        ("Кочерга-жезл", "Одинаково плоха в обеих ролях."),
        ("Лопата с пентаграммой", "Копает и немного проклинает."),
        ("Молоток чародея-недоучки", "Бьёт по гвоздю и по реальности сразу."),
    ),
    "common": (
        ("Учебная палочка", "Одна искра, зато честная."),
        ("Посох подмастерья", "Держит вес хозяина и немного магии."),
        ("Карманный гримуар", "Три заклинания и список покупок."),
        ("Кварцевый брелок", "Слабо светится в темноте."),
        ("Ученический жезл", "Выдают вместе с методичкой."),
        ("Свеча первого круга", "Горит ровно и без сюрпризов."),
        ("Мелок для рун", "Хватает на десяток кругов."),
        ("Бубен начинающего", "Громкий и очень уверенный."),
        ("Кисточка с ворсом единорога", "Продавец клялся, что единорога."),
        ("Флакон со светлячками", "Светят по очереди, как договорились."),
        ("Компас на четыре стихии", "Стрелка выбирает по настроению."),
        ("Оберег из речного камня", "Тёплый и упрямый."),
        ("Колокольчик тишины", "Звенит, чтобы стало тихо."),
        ("Веер сквозняка", "Один взмах — один сквозняк."),
        ("Мешочек с солью", "Против всего сразу и понемногу."),
        ("Лупа для мелких чудес", "Чудо всё равно мелкое."),
        ("Тренировочный меч с рунами", "Руны учебные, синяки настоящие."),
        ("Посох с набалдашником", "Если магия не сработает, есть набалдашник."),
        ("Серп заклинателя", "Жнёт траву и слухи."),
        ("Молот с рунным клеймом", "Клеймо ставили на глаз."),
        ("Копьё с кварцевым наконечником", "Колет и подсвечивает место укола."),
    ),
    "uncommon": (
        ("Палочка с настоящим сердечником", "Внутри волос, и лучше не знать чей."),
        ("Посох странника", "Прошёл больше, чем его хозяин."),
        ("Гримуар в кожаном переплёте", "Переплёт держится, содержание пугает."),
        ("Аметистовый фокус", "Собирает свет в одну злую точку."),
        ("Жезл с тремя кольцами", "Каждое кольцо помнит своё заклинание."),
        ("Свеча долгой ночи", "Не гаснет, пока не досказано."),
        ("Резец по рунам", "Режет камень как масло, масло как камень."),
        ("Бубен грозы", "После него всегда пахнет озоном."),
        ("Кисть для боевой раскраски", "Красит соперника в цвет поражения."),
        ("Фонарь болотных огней", "Ведёт куда надо. Кому надо — вопрос."),
        ("Веер четырёх ветров", "Второй ветер обычно лишний."),
        ("Клепсидра чародея", "Отмеряет ровно один удачный ход."),
        ("Колокол сбора", "Созывает всё, что слышит."),
        ("Хрустальный маятник", "Качается против ветра."),
        ("Чернила из грозовой тучи", "Пишут с разрядом."),
        ("Линза истинного зрения", "Показывает больше, чем хотелось."),
        ("Рунный клинок", "Половина лезвия, половина строчки."),
        ("Боевой посох с окованным концом", "Спорит словом и железом."),
        ("Секира с вживлённым кристаллом", "Кристалл прижился, к сожалению."),
        ("Цеп заклинателя", "Крутится и договаривает за хозяина."),
        ("Алебарда с рунной кромкой", "Длинная во всех смыслах."),
    ),
}

# Rares and legendaries are hand-written down to the last number: each is a name, a joke,
# a stat line and the rule it exists for. The rare rung teaches a rule cheaply; the
# legendary rung is that same rule at a size a build gets planned around.
_MAGIC_RARE_WEAPONS: Final = (
    ("Посох треснувшего рассвета", "Трещина светится ровно на рассвете и во время драки.",
     "magic", (("magic", 22), ("luck", 4), ("agility", -2)),
     _effect("arcane_surge", "Свитки бьют на 45% сильнее весь бой.", 45)),
    ("Гримуар второго дыхания", "Открывается сам на нужной странице.",
     "magic", (("magic", 21), ("health", 7), ("agility", -2)),
     _effect("spell_siphon", "Каждый свиток возвращает 40% нанесённого урона здоровьем.", 40)),
    ("Жезл встречного огня", "Отвечает раньше, чем успеваешь подумать.",
     "magic", (("magic", 20), ("armor", 8), ("luck", -2)),
     _effect("spell_thorns", "32% полученного магического урона возвращается отправителю.", 32)),
    ("Кристалл долгой искры", "Копит весь день ради одной минуты.",
     "magic", (("magic", 21), ("agility", 4), ("armor", -2)),
     _effect("arcane_battery", "Каждый раунд: +9% к силе свитков, без потолка.", 9)),
    ("Резец висящей руны", "Руна висит в воздухе и ждёт продолжения.",
     "magic", (("magic", 20), ("strength", 5), ("agility", -2)),
     _effect("runic_charge", "Каждое попадание: +15% к силе свитков, максимум +105%.", 15, cap=105)),
    ("Линза точного слова", "Слово проходит там, где не проходит сталь.",
     "magic", (("magic", 23), ("luck", 3), ("health", -5)),
     _effect("spell_pierce", "Свитки игнорируют 40% брони и защиты соперника, а увернуться от них вдвое сложнее.", 40, dodge=50)),
    ("Свеча чужого срока", "Горит быстро и не своим воском.",
     "magic", (("magic", 24), ("agility", 3), ("health", -6)),
     _effect("mana_burn", "Свитки сильнее на 90%, но каждое прочтение стоит 5% максимального HP.", 90, toll=5)),
    ("Оберег холодной крови", "Чужая магия об него тупится.",
     "magic", (("magic", 20), ("health", 8), ("agility", -3)),
     _effect("ward", "Входящий магический урон ниже на 32%.", 32)),
    ("Клепсидра лишнего хода", "Один песок падает вверх.",
     "magic", (("magic", 21), ("agility", 5), ("armor", -3)),
     _effect("focus_shift", "После свитка следующий обычный удар сильнее на 65%.", 65)),
    ("Соляная печать", "Круг замыкается сам, если начать.",
     "magic", (("magic", 20), ("armor", 9), ("luck", -2)),
     _effect("spell_shield", "Каждый свиток поднимает щит на 6% максимального HP.", 6)),
    ("Чернила проклятой строки", "Строка липнет к тому, о ком написана.",
     "magic", (("magic", 22), ("luck", 4), ("armor", -2)),
     _effect("hex", "После свитка следующий удар соперника слабее на 35%.", 35, turns=1)),
    ("Бубен нарастающей грозы", "Каждый удар громче предыдущего.",
     "magic", (("magic", 21), ("agility", 4), ("health", -4)),
     _effect("burn", "Каждое попадание поджигает: 17% урона за ход, 3 хода, +25% за тик.", 17, turns=3, grow=25)),
    ("Фонарь встречного морока", "Показывает сопернику то, чего нет.",
     "magic", (("magic", 20), ("armor", 7), ("agility", 2), ("luck", -2)),
     _effect("chill", "После первого попадания следующий удар соперника слабее на 45%.", 45)),
    ("Колокол пустого поля", "После него на поле тише и просторнее.",
     "magic", (("magic", 22), ("health", 6), ("agility", -3)),
     _effect("tesla", "Каждый третий удар бьёт разрядом на 15% максимального HP соперника.", 15)),
    ("Пыльца дрожащих пальцев", "От неё чужие руки трясутся.",
     "magic", (("magic", 21), ("luck", 5), ("health", -4)),
     _effect("crushing_grip", "После первого попадания урон соперника ниже на 11% до конца боя.", 11)),
    ("Аркан на подкладке", "Пришит с изнанки и работает оттуда.",
     "magic", (("magic", 20), ("agility", 6), ("armor", -3)),
     _effect("precision", "Твои промахи режутся на 45%.", 45)),
    ("Меч дважды сказанного", "Одно лезвие для стали, второе для слова.",
     "hybrid", (("magic", 16), ("strength", 15), ("luck", 3), ("agility", -2)),
     _effect("double_cast", "Первые два свитка за бой дочитываются второй раз на 70% урона.", 70, casts=2)),
    ("Секира рунного эха", "Эхо доносится позже, но громче.",
     "hybrid", (("magic", 15), ("strength", 16), ("health", 5), ("armor", -3)),
     _effect("echo_strike", "Первое попадание повторяется эхом на 70% урона.", 70)),
    ("Копьё двух школ", "Спорит само с собой и всегда побеждает.",
     "hybrid", (("magic", 16), ("strength", 16), ("agility", 3), ("luck", -3)),
     _effect("combo", "Каждое попадание подряд: +6% урона, максимум +18%.", 6, cap=18)),
    ("Клевец наговорённый", "Наговор держится ровно до крови.",
     "hybrid", (("magic", 15), ("strength", 15), ("armor", 6), ("agility", -2)),
     _effect("venom_blade", "Каждое попадание отравляет на 18% урона и добавляет промах следующему удару соперника.", 18, weaken=15)),
    ("Цеп грозового круга", "Круг замыкается на сопернике.",
     "hybrid", (("magic", 17), ("strength", 14), ("health", 6), ("armor", -3)),
     _effect("thorns", "Каждый полученный удар возвращает 12% урона шипами.", 12)),
)

_MAGIC_LEGENDARY_WEAPONS: Final = (
    ("Посох Первого Слога", "Им сказали первое слово. Остальные подтянулись.",
     "magic", (("magic", 30), ("luck", 5), ("agility", -3)),
     _effect("arcane_surge", "Свитки бьют на 85% сильнее весь бой.", 85)),
    ("Гримуар Незакрытой Скобки", "Заклинание всё ещё длится. И будет длиться.",
     "magic", (("magic", 29), ("health", 9), ("agility", -3)),
     _effect("arcane_battery", "Каждый раунд: +18% к силе свитков, без потолка.", 18)),
    ("Жезл Вечной Отдачи", "Всё, что в него бьёт, возвращается с процентами.",
     "magic", (("magic", 28), ("armor", 11), ("luck", -3)),
     _effect("spell_thorns", "75% полученного магического урона возвращается отправителю.", 75)),
    ("Сердце Кварцевой Бури", "Внутри до сих пор идёт та самая гроза.",
     "magic", (("magic", 29), ("strength", 6), ("agility", -3)),
     _effect("runic_charge", "Каждое попадание: +26% к силе свитков, максимум +260%.", 26, cap=260)),
    ("Линза Безошибочного Слова", "Броня — это тоже просто чьё-то мнение.",
     "magic", (("magic", 31), ("luck", 4), ("health", -6)),
     _effect("spell_pierce", "Свитки игнорируют 90% брони и защиты соперника, и от них нельзя увернуться.", 90, dodge=100)),
    ("Свеча, Горящая Чужим", "Фитиль твой, воск — уже нет.",
     "magic", (("magic", 32), ("agility", 4), ("health", -8)),
     _effect("mana_burn", "Свитки сильнее на 155%, но каждое прочтение стоит 7% максимального HP.", 155, toll=7)),
    ("Оберег Стеклянной Крови", "Магия проходит насквозь и не находит, за что зацепиться.",
     "magic", (("magic", 28), ("health", 11), ("agility", -3)),
     _effect("ward", "Входящий магический урон ниже на 62%.", 62)),
    ("Клепсидра Украденного Хода", "Ход был не твой. Теперь твой.",
     "magic", (("magic", 29), ("agility", 6), ("armor", -3)),
     _effect("focus_shift", "После свитка следующий обычный удар сильнее на 145%.", 145)),
    ("Печать Соляного Моря", "Море высохло, круг остался.",
     "magic", (("magic", 28), ("armor", 12), ("luck", -3)),
     _effect("spell_shield", "Каждый свиток поднимает щит на 13% максимального HP.", 13)),
    ("Чернила Последней Строки", "После неё соперник дописывает молча.",
     "magic", (("magic", 30), ("luck", 5), ("armor", -3)),
     _effect("hex", "После свитка два следующих удара соперника слабее на 60%.", 60, turns=2)),
    ("Кисть, Что Красит Судьбу", "Один мазок — и биография переписана.",
     "magic", (("magic", 31), ("agility", 3), ("luck", 3), ("health", -6)),
     _effect("double_cast", "Каждый из трёх первых свитков дочитывается второй раз на 110% урона.", 110, casts=3)),
    ("Сосуд Обратного Тока", "Всё выпитое возвращается вдвойне.",
     "magic", (("magic", 29), ("health", 10), ("agility", -3)),
     _effect("spell_siphon", "Каждый свиток возвращает 90% нанесённого урона здоровьем.", 90)),
    ("Бубен Девятого Грома", "Восемь были репетицией.",
     "magic", (("magic", 30), ("agility", 4), ("health", -5)),
     _effect("burn", "Каждое попадание поджигает: 30% урона за ход, 3 хода, +40% за тик.", 30, turns=3, grow=40)),
    ("Колокол Немого Поля", "Звонит один раз. Больше не нужно.",
     "magic", (("magic", 30), ("health", 8), ("agility", -4)),
     _effect("tesla", "Каждый третий удар бьёт разрядом на 24% максимального HP соперника.", 24)),
    ("Фонарь Двух Теней", "Вторая тень чужая и очень занятая.",
     "magic", (("magic", 29), ("luck", 6), ("armor", -3)),
     _effect("shatter", "Каждое попадание оставляет осколок; на четвёртом все осколки взрываются на 190% урона.", 190, every=4)),
    ("Аркан С Изнанки", "Смотрит на бой с другой стороны ткани.",
     "magic", (("magic", 30), ("agility", 5), ("armor", -3)),
     _effect("precision", "Твои промахи режутся на 70%.", 70)),
    ("Меч Двойного Замаха", "Второй замах начинается раньше, чем кончился первый.",
     "hybrid", (("magic", 22), ("strength", 22), ("luck", 4), ("agility", -3)),
     _effect("double_strike", "Каждый ход бьёт дважды, по 62% урона за удар.", 62)),
    ("Секира Девятого Эха", "Эхо считает до девяти и бьёт на каждом.",
     "hybrid", (("magic", 21), ("strength", 23), ("health", 8), ("armor", -3)),
     _effect("echo_strike", "Первое попадание повторяется эхом на 130% урона.", 130)),
    ("Копьё Общей Раны", "Одна рана на двоих, но платит один.",
     "hybrid", (("magic", 22), ("strength", 21), ("armor", 9), ("luck", -3)),
     _effect("reap", "Каждое попадание забирает 18% недостающего здоровья соперника.", 18)),
    ("Молот Немого Приговора", "Приговор не зачитывают. Его приводят.",
     "hybrid", (("magic", 21), ("strength", 22), ("agility", 4), ("health", -4)),
     _effect("chain_crit", "Критический удар открывает ещё одну атаку на 95% урона.", 95)),
    ("Цеп Соборного Гула", "Гудит так, что соперник забывает защищаться.",
     "hybrid", (("magic", 23), ("strength", 21), ("health", 7), ("armor", -4)),
     _effect("pressure", "За каждый полученный удар: +11% урона, без потолка.", 11)),
)


def _magic_bonus_tuple(index: int, rarity: str, hybrid: bool) -> tuple[tuple[str, int], ...]:
    """Bonuses for one generated magic weapon: the steel bands, rewritten in Магия.

    A hybrid carries roughly 70% of the band in EACH of the two stats rather than half in
    each. Averaging halves what the swing sees, so an even split would leave a hybrid
    strictly worse than either pure weapon at everything; at 70/70 it swings a little
    softer, casts a little softer, and gets back the health Strength quietly pays out --
    plus a stat curve (level ** 1.5) that charges far less for two middling stats than
    for one enormous one.
    """
    variant = index % 5
    if rarity == "cursed":
        if hybrid:
            return (
                (("magic", -3), ("strength", -2), ("luck", 3)),
                (("magic", -2), ("strength", -3), ("armor", 6)),
                (("magic", -4), ("strength", 3), ("health", -4)),
                (("magic", 2), ("strength", -4), ("agility", -2)),
                (("magic", -3), ("strength", -2), ("armor", 5)),
            )[variant]
        return (
            (("magic", -4), ("luck", 2)),
            (("agility", -3), ("armor", 5), ("magic", -1)),
            (("health", -6), ("magic", 2)),
            (("magic", -2), ("agility", -2), ("armor", 7)),
            (("luck", -3), ("magic", 3)),
        )[variant]
    if rarity == "common":
        if hybrid:
            magic, strength = 5 + (index % 3), 4 + (index % 3)
            return (
                (("magic", magic), ("strength", strength)),
                (("magic", magic), ("strength", strength), ("agility", 1)),
                (("magic", magic), ("strength", strength), ("luck", 1)),
                (("magic", magic), ("strength", strength), ("armor", 3)),
                (("magic", magic), ("strength", strength), ("health", 2)),
            )[variant]
        magic = 6 + (index % 5)
        return (
            (("magic", magic),),
            (("magic", magic), ("agility", 1)),
            (("magic", magic), ("luck", 1)),
            (("magic", magic), ("armor", 3)),
            (("magic", magic), ("health", 2)),
        )[variant]
    if rarity == "uncommon":
        if hybrid:
            magic, strength = 9 + (index % 3), 8 + (index % 3)
            return (
                (("magic", magic), ("strength", strength), ("luck", 2)),
                (("magic", magic), ("strength", strength), ("agility", 2)),
                (("magic", magic), ("strength", strength), ("armor", 5), ("luck", -1)),
                (("magic", magic), ("strength", strength), ("health", 4), ("agility", -1)),
                (("magic", magic), ("strength", strength), ("agility", 3), ("armor", -2)),
            )[variant]
        magic = 12 + (index % 5)
        return (
            (("magic", magic), ("luck", 2)),
            (("magic", magic), ("agility", 2)),
            (("magic", magic), ("armor", 5), ("luck", -1)),
            (("magic", magic), ("health", 4), ("agility", -1)),
            (("magic", magic), ("agility", 3), ("armor", -2)),
        )[variant]
    raise ValueError(f"rare and legendary magic weapons are hand-written: {rarity}")


# The magic shelf's own drop weights. A rare magic weapon is deliberately findable at 4
# rather than the steel shelf's 10, and its legendaries stay at 1 in a pool that already
# held twenty-two: at the ordinary weights, twenty-one more legendaries would roughly
# have doubled how often a legendary weapon falls out of the arena -- an economy change
# nobody asked for while adding a shelf.
_MAGIC_DROP_WEIGHTS: Final = {"cursed": 1, "rare": 4, "legendary": 1}
_MAGIC_FIRST_CODE: Final = 527
# How many rare magic weapons are sold rather than found. Common and uncommon ones are
# all shop stock, for the same reason their steel equivalents are: a new caster has to be
# able to BUY the weapon their scrolls read, or the build stays unreachable until the
# arena happens to hand one over.
_MAGIC_SHOP_RARE_COUNT: Final = 5


def _magic_weapon_rows() -> tuple[tuple, ...]:
    """(code, name, description, rarity, scaling, bonuses, effect) for all 105."""
    rows: list[tuple] = []
    code_number = _MAGIC_FIRST_CODE
    for rarity in RARITIES:
        if rarity in ("rare", "legendary"):
            table = _MAGIC_RARE_WEAPONS if rarity == "rare" else _MAGIC_LEGENDARY_WEAPONS
            for name, description, scaling, bonuses, effect in table:
                rows.append((f"w{code_number:03d}", name, description, rarity,
                             scaling, bonuses, effect))
                code_number += 1
            continue
        for index, (name, description) in enumerate(_MAGIC_OBJECTS[rarity]):
            hybrid = index >= 16
            rows.append((
                f"w{code_number:03d}", name, description, rarity,
                "hybrid" if hybrid else "magic",
                _magic_bonus_tuple(index, rarity, hybrid), (),
            ))
            code_number += 1
    return tuple(rows)


MAGIC_WEAPON_ROWS: Final = _magic_weapon_rows()


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


def _drop_weight(rarity: str, source: str, cursed: bool = False) -> int:
    """Relative arena-drop chance; zero means this weapon is not in the drop pool.

    Forty rare weapons at weight 10, 75 cursed weapons at weight 1 and ten legendary
    weapons at weight 1 make legendary drops 10 / 485 (about 2.06%) of weapon drops.
    Cursed junk stays noticeable without overwhelming useful rewards.
    """
    if source != "drop":
        return 0
    if cursed and rarity == "rare":
        # The rare cursed rung is findable, but the FORGE is the reliable way to it: at the
        # ordinary rare weight of 10 these twelve would have been a fifth of every weapon
        # drop, which would make the middle of the cursed ladder something you trip over
        # rather than something you build toward, and the six-junk recipe pointless.
        return 2
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
        *((code, name, description, rarity, bonuses, effect, False)
          for code, name, description, rarity, bonuses, effect in _NEW_BUILD_WEAPONS),
        *((code, name, description, "legendary", bonuses, effect, True)
          for code, name, description, bonuses, effect in _CURSED_LEGENDARIES),
        *((code, name, description, "rare", bonuses, effect, True)
          for code, name, description, bonuses, effect in _RARE_CURSED_WEAPONS),
    ]
    for code, name, description, rarity, bonuses, effect, cursed in hand_written:
        buy_price, resale_price = _prices(rarity, "drop", bonuses)
        entries.append(WeaponSpec(
            code=code,
            name=name,
            description=description,
            rarity=rarity,
            source="drop",
            buy_price=buy_price,
            resale_price=resale_price,
            drop_weight=_drop_weight(rarity, "drop", cursed),
            bonuses=bonuses,
            effect=effect,
            cursed=cursed,
        ))
    # The magic shelf. Priced and sourced by its own rules rather than `_source_for` and
    # `_drop_weight`: those two are keyed to a rarity's rank inside the generated five
    # hundred, and w527 onwards is a separate shelf with its own shop/drop split.
    magic_rare_rank = 0
    for code, name, description, rarity, scaling, bonuses, effect in MAGIC_WEAPON_ROWS:
        if rarity in ("common", "uncommon"):
            source = "shop"
        elif rarity == "rare":
            magic_rare_rank += 1
            source = "shop" if magic_rare_rank <= _MAGIC_SHOP_RARE_COUNT else "drop"
        else:
            source = "drop"
        buy_price, resale_price = _prices(rarity, source, bonuses)
        entries.append(WeaponSpec(
            code=code,
            name=name,
            description=description,
            rarity=rarity,
            source=source,
            buy_price=buy_price,
            resale_price=resale_price,
            drop_weight=0 if source == "shop" else _MAGIC_DROP_WEIGHTS[rarity],
            bonuses=bonuses,
            effect=effect,
            scaling=scaling,
        ))
    # Every entry of the `cursed` RARITY is cursed by definition -- the bottom rung of the
    # ladder. Set once here rather than repeated on seventy-five generated rows.
    return tuple(
        replace(item, cursed=True) if item.rarity == "cursed" else item
        for item in entries
    )


ALL_WEAPON_SPECS: Final[tuple[WeaponSpec, ...]] = _build_catalogue()

# The bottom cursed rung used to contain 96 grey weapons: many different names for only
# a handful of stat shapes. Twelve keep every physical/magic/hybrid build represented;
# repeat drops now provide the six ingredients the forge actually needs. Retired codes
# remain aliases (below), so this catalogue cut never deletes an owned item.
ACTIVE_CURSED_BASE_CODES: Final = frozenset({
    # Five physical archetypes.
    "w007", "w013", "w020", "w074", "w171",
    # Five pure-magic archetypes and two hybrids.
    "w527", "w528", "w529", "w530", "w531", "w543", "w545",
})
_active_cursed_base = tuple(
    item for item in ALL_WEAPON_SPECS if item.code in ACTIVE_CURSED_BASE_CODES
)


def _spaced_codes(rows: list[WeaponSpec], count: int, required=()) -> frozenset[str]:
    """Keep a stable sample spanning a whole stat/power band, including legacy IDs."""
    ordered = sorted(rows, key=lambda item: item.code)
    selected = [item for item in ordered if item.code in set(required)]
    remaining = [item for item in ordered if item.code not in {row.code for row in selected}]
    needed = max(0, count - len(selected))
    if needed >= len(remaining):
        selected.extend(remaining)
    elif needed == 1:
        selected.append(remaining[len(remaining) // 2])
    elif needed > 1:
        selected.extend(
            remaining[round(index * (len(remaining) - 1) / (needed - 1))]
            for index in range(needed)
        )
    return frozenset(item.code for item in selected)


def _grey_rows(rarity: str, scaling: str) -> list[WeaponSpec]:
    return [
        item for item in ALL_WEAPON_SPECS
        if item.rarity == rarity and item.scaling == scaling
    ]


# Common and the legacy green "uncommon" both render as the same grey Common tier in the
# live game. Keep thirty varied shop weapons instead of 412 repeated catalogue entries:
# twenty physical, seven pure-magic and three hybrids, split across both power bands.
ACTIVE_GREY_WEAPON_CODES: Final = frozenset().union(
    _spaced_codes(_grey_rows("common", "strength"), 12, {"w001", "w004", "w052", "w098"}),
    _spaced_codes(_grey_rows("uncommon", "strength"), 8, {"w002", "w051"}),
    _spaced_codes(_grey_rows("common", "magic"), 4),
    _spaced_codes(_grey_rows("uncommon", "magic"), 3),
    _spaced_codes(_grey_rows("common", "hybrid"), 2),
    _spaced_codes(_grey_rows("uncommon", "hybrid"), 1, {"w586"}),
)
_active_replacement_rows = tuple(
    item for item in ALL_WEAPON_SPECS
    if item.code in ACTIVE_CURSED_BASE_CODES or item.code in ACTIVE_GREY_WEAPON_CODES
)


def _retired_weapon_replacement(item: WeaponSpec) -> str:
    """Closest survivor in the same rarity/scaling band whenever one exists."""
    candidates = [
        row for row in _active_replacement_rows
        if row.rarity == item.rarity and row.scaling == item.scaling
    ]
    if not candidates:
        candidates = [row for row in _active_replacement_rows if row.scaling == item.scaling]
    mine = dict(item.bonuses)
    return min(candidates, key=lambda row: (
        sum(abs(mine.get(key, 0) - dict(row.bonuses).get(key, 0)) for key in STAT_KEYS),
        row.code,
    )).code


RETIRED_WEAPON_REPLACEMENTS: Final = {
    item.code: _retired_weapon_replacement(item)
    for item in ALL_WEAPON_SPECS
    if (
        item.rarity == "cursed" and item.code not in ACTIVE_CURSED_BASE_CODES
    ) or (
        item.rarity in {"common", "uncommon"} and item.code not in ACTIVE_GREY_WEAPON_CODES
    )
}
WEAPON_SPECS: Final[tuple[WeaponSpec, ...]] = tuple(
    item for item in ALL_WEAPON_SPECS if item.code not in RETIRED_WEAPON_REPLACEMENTS
)
# Retired grey rows are intentionally absent: no shop, loot table, collection or API can
# expose them after migration. Their stable old codes resolve through the alias mapping.
RAW_ITEMS: Final[tuple[dict[str, object], ...]] = tuple(item.raw_item() for item in WEAPON_SPECS)
WEAPON_COUNT: Final = len(WEAPON_SPECS)
# Published so the UI, the balance report and the tests can name the cursed shelf without
# re-deriving it from a code list. These items are ordinary legendaries to every drop,
# forge and inventory path -- the set exists to LABEL them, never to gate them.
# The magic shelf, named once so the tests, the balance harness and both front ends can
# talk about it without re-deriving it from a code range.
MAGIC_WEAPON_CODES: Final = frozenset(row[0] for row in MAGIC_WEAPON_ROWS)
CURSED_LEGENDARY_CODES: Final = frozenset(row[0] for row in _CURSED_LEGENDARIES)
RARE_CURSED_CODES: Final = frozenset(row[0] for row in _RARE_CURSED_WEAPONS)
# The whole cursed line, all three rungs. This is what the forge and both front ends read;
# `rarity == "cursed"` alone would answer only for the bottom one.
CURSED_CODES: Final = frozenset(item.code for item in WEAPON_SPECS if item.cursed)
RARITY_COUNTS: Final = {rarity: sum(item.rarity == rarity for item in WEAPON_SPECS) for rarity in RARITIES}
PRE_REBALANCE_BUY_PRICES: Final = {
    item.code: _pre_rebalance_buy_price(item.code, item.rarity, item.source)
    for item in WEAPON_SPECS if item.source == "shop"
}


def _validate_catalogue() -> None:
    """Fail immediately if a future catalogue edit violates its public contract."""
    assert len(ALL_WEAPON_SPECS) == 631
    assert WEAPON_COUNT == 165
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
    assert RARITY_COUNTS == {"cursed": 12, "common": 18, "uncommon": 12, "rare": 80, "legendary": 43}
    assert len(ACTIVE_GREY_WEAPON_CODES) == 30
    assert len(RETIRED_WEAPON_REPLACEMENTS) == 466
    assert set(RETIRED_WEAPON_REPLACEMENTS).isdisjoint(item.code for item in WEAPON_SPECS)
    assert set(RETIRED_WEAPON_REPLACEMENTS.values()) <= (
        ACTIVE_CURSED_BASE_CODES | ACTIVE_GREY_WEAPON_CODES
    )
    # Every full magic tier remains 21 strong. The deliberately compact grey rung keeps
    # five pure casters and two hybrids instead of twenty-one near-identical names.
    magic_shelf = [item for item in WEAPON_SPECS if item.scaling != "strength"]
    assert len(magic_shelf) == 59
    assert all(item.scaling in SCALINGS for item in WEAPON_SPECS)
    assert all(item.code[1:].isdigit() and int(item.code[1:]) >= _MAGIC_FIRST_CODE
               for item in magic_shelf)
    for rarity in RARITIES:
        shelf = [item for item in magic_shelf if item.rarity == rarity]
        expected_count = {
            "cursed": 7, "common": 6, "uncommon": 4, "rare": 21, "legendary": 21,
        }[rarity]
        expected_hybrids = {
            "cursed": 2, "common": 2, "uncommon": 1, "rare": 5, "legendary": 5,
        }[rarity]
        assert len(shelf) == expected_count, (rarity, len(shelf))
        assert sum(item.scaling == "hybrid" for item in shelf) == expected_hybrids, rarity
    # A magic weapon that grants no Магия would make its own scaling a downgrade.
    assert all(
        any(key == "magic" for key, _value in item.bonuses) for item in magic_shelf
    ), "every magic weapon must carry the stat its swing reads"
    assert all(
        any(key == "strength" for key, _value in item.bonuses)
        for item in magic_shelf if item.scaling == "hybrid"
    ), "a hybrid weapon must carry both halves of what it averages"
    assert all(item.effect for item in magic_shelf
               if item.rarity in ("rare", "legendary"))
    assert not any(item.effect for item in magic_shelf
                   if item.rarity in ("cursed", "common", "uncommon"))
    # The cursed ladder, all three rungs. Twelve varied junk designs at the bottom are
    # repeatable forge material; twelve rares and eight legendaries remain above them.
    cursed_line = [item for item in WEAPON_SPECS if item.cursed]
    assert len(cursed_line) == 32
    assert {item.rarity for item in cursed_line} == {"cursed", "rare", "legendary"}
    assert all(item.source == "drop" for item in cursed_line)
    assert sum(1 for item in cursed_line if item.rarity == "rare") == 12
    assert sum(1 for item in cursed_line if item.rarity == "legendary") == 8
    # Every legendary carries a passive; the promoted five come from effect-bearing rare
    # lines, leaving twenty modified rares and twenty-five plain ones out of the generated
    # 500 -- plus the two effect-bearing rares and two legendaries in _NEW_BUILD_WEAPONS.
    with_effect = [item for item in WEAPON_SPECS if item.effect]
    assert all(item.rarity in {"rare", "legendary"} for item in with_effect)
    assert all(item.effect for item in WEAPON_SPECS if item.rarity == "legendary")
    # Twenty-two ordinary rares carry a passive, plus the twelve rare CURSED weapons --
    # every one of those carries one by definition, since a curse with no rule is just a
    # weapon with worse stats.
    assert sum(
        1 for item in WEAPON_SPECS
        if item.rarity == "rare" and item.effect and not item.cursed
        and item.scaling == "strength"
    ) == 22
    assert all(item.effect for item in WEAPON_SPECS if item.cursed and item.rarity != "cursed")
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
        if rarity == "rare":
            expected |= {dict(data[4])["code"] for data in _RARE_CURSED_WEAPONS}
        if rarity == "legendary":
            expected |= {dict(data[3])["code"] for data in _ASCENDED_LEGENDARIES.values()}
            expected |= {dict(data[4])["code"] for data in _CURSED_LEGENDARIES}
        expected |= {
            dict(row[6])["code"] for row in MAGIC_WEAPON_ROWS if row[3] == rarity and row[6]
        }
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
    "CURSED_LEGENDARY_CODES", "RARE_CURSED_CODES", "CURSED_CODES",
    "ALL_WEAPON_SPECS", "ACTIVE_CURSED_BASE_CODES", "ACTIVE_GREY_WEAPON_CODES",
    "RETIRED_WEAPON_REPLACEMENTS",
    "MAGIC_WEAPON_CODES", "MAGIC_WEAPON_ROWS", "SCALINGS",
]
