"""Drop-only boots and gloves for the arena equipment catalogue.

This module is deliberately data-only.  It exposes the same ``RAW_ITEMS`` record
shape as :mod:`pets_weapon_catalog`, so the main inventory code can merge it when
the new equipment slots are wired in.  No current starter item is replaced here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from pets_amulet_catalog import EFFECT_HOOKS


RARITIES: Final = ("common", "uncommon", "rare", "legendary")
STAT_KEYS: Final = ("strength", "health", "agility", "luck", "armor")
SLOTS: Final = ("boots", "gloves")
EFFECT_CODES: Final = frozenset({
    "phantom_step", "afterimage", "rewind", "echo_strike", "crushing_grip", "perfect_parry",
    "precision", "first_strike", "spring", "dodge_heal", "regen", "venom_blade", "bleed",
})


@dataclass(frozen=True, slots=True)
class GearSpec:
    """An immutable item record, adaptable to the existing ``Item`` constructor."""

    code: str
    name: str
    description: str
    slot: str
    rarity: str
    resale_price: int
    drop_weight: int
    bonuses: tuple[tuple[str, int], ...]
    effect: tuple[tuple[str, str | int | bool], ...] = ()
    source: str = "drop"
    buy_price: int = 0

    @property
    def price(self) -> int:
        """Compatibility spelling used by the live inventory model."""
        return self.buy_price

    def bonus_dict(self) -> dict[str, int]:
        return dict(self.bonuses)

    def effect_dict(self) -> dict[str, str | int | bool]:
        return dict(self.effect)

    def item_arguments(self) -> tuple[str, str, str, int, str, dict[str, int], str]:
        return (
            self.code, self.name, self.slot, self.buy_price, self.source,
            self.bonus_dict(), self.description,
        )

    def raw_item(self) -> dict[str, object]:
        """Return a fresh record in the ``RAW_ITEMS`` interoperability schema."""
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


# A common item is deliberately modest.  Legendary items are fun trophies, not a
# replacement for a good weapon.  Weights make a legendary one item in roughly 255
# drops from either of these slot pools (about 0.39% per specific legendary).
_TIER: Final = {
    "common": (8, 12),
    "uncommon": (15, 6),
    "rare": (35, 2),
    "legendary": (75, 1),
}


def _spec(
    code: str, name: str, description: str, slot: str, rarity: str,
    bonuses: tuple[tuple[str, int], ...],
    effect: tuple[tuple[str, str | int | bool], ...] = (),
) -> GearSpec:
    resale_price, drop_weight = _TIER[rarity]
    return GearSpec(
        code=code, name=name, description=description, slot=slot, rarity=rarity,
        resale_price=resale_price, drop_weight=drop_weight, bonuses=bonuses, effect=effect,
    )


def _effect(
    code: str, text: str, value: int, **params: int | bool,
) -> tuple[tuple[str, str | int | bool], ...]:
    """One passive, in the same shape ``pets_amulet_catalog`` uses.

    ``params`` carries the optional per-effect knobs the engine reads with ``_param``
    (``hits``, ``cap``, ``turns``, ``threshold`` ...). Gear had no use for them until the
    balance pass, and their absence here silently limited a couple of items to whatever
    the engine's default happened to be.
    """
    return tuple({"code": code, "text": text, "value": value, **params}.items())


BOOT_SPECS: Final[tuple[GearSpec, ...]] = (
    _spec("bt01", "Кроссовки с липучкой", "Бегут быстро, пока липучка не передумала.", "boots", "common", (("agility", 2),)),
    _spec("bt02", "Тапки переговорщика", "Позволяют тихо уйти от чужой правоты.", "boots", "common", (("agility", 1), ("luck", 1))),
    _spec("bt03", "Сапоги из пакета", "Шуршат так уверенно, будто есть план.", "boots", "common", (("health", 3), ("agility", -1))),
    _spec("bt04", "Кеды после физры", "Запах победы. Или просто запах.", "boots", "common", (("agility", 2), ("armor", 1))),
    _spec("bt05", "Ботинки на честном слове", "Шнурок держится исключительно из уважения.", "boots", "common", (("armor", 3), ("agility", -1))),
    _spec("bt06", "Валенки курьера", "Доставляют владельца прямо в неприятности.", "boots", "common", (("health", 4),)),
    _spec("bt07", "Шлёпанцы судьбы", "Один всегда летит в нужную сторону.", "boots", "common", (("luck", 2),)),
    _spec("bt08", "Ботинки с гвоздиком", "Гвоздик внутри. Характер тоже внутри.", "boots", "common", (("strength", 1), ("agility", 1))),
    _spec("bt09", "Кроссы на распродаже", "Цена низкая, самооценка высокая.", "boots", "common", (("agility", 2),)),
    _spec("bt10", "Сандалии с носком", "Эстетика сдалась, броня осталась.", "boots", "common", (("armor", 2), ("luck", 1))),
    _spec("bt11", "Угги боевого деда", "Тепло, тяжело и без лишних вопросов.", "boots", "common", (("health", 3), ("armor", 1))),
    _spec("bt12", "Туфли для ковра", "Не скользят, потому что ковёр всё запомнил.", "boots", "common", (("agility", 1), ("armor", 2))),
    _spec("bt13", "Резиновые сапоги тревоги", "Лужи обходят сами, враги — не всегда.", "boots", "common", (("health", 2), ("luck", 1))),
    _spec("bt14", "Кеды с динозавром", "Рычат при каждом особенно смелом шаге.", "boots", "common", (("strength", 1), ("agility", 1))),
    _spec("bt15", "Домашние тапки босса", "Официально разрешают командовать с дивана.", "boots", "common", (("health", 3),)),
    _spec("bt16", "Ботинки с картошкой", "В кармане припас, в походке — уверенность.", "boots", "common", (("health", 2), ("strength", 1))),
    _spec("bt17", "Коньки без льда", "Скользят везде, особенно в тактике.", "boots", "uncommon", (("agility", 4), ("armor", -1))),
    _spec("bt18", "Берцы вахтёра", "Пропускают только тех, кто выглядит виновато.", "boots", "uncommon", (("armor", 5), ("agility", -1))),
    _spec("bt19", "Кроссовки на турбине", "Свистят громче, чем ваш план отхода.", "boots", "uncommon", (("agility", 4), ("luck", 1))),
    _spec("bt20", "Ласты сухопутчика", "Манёвр странный, зато запоминающийся.", "boots", "uncommon", (("health", 4), ("agility", 2))),
    _spec("bt21", "Сапоги с секретным карманом", "Там лежит чек и маленькая надежда.", "boots", "uncommon", (("luck", 3), ("armor", 2))),
    _spec("bt22", "Туфли последнего звонка", "Звенят при опасном уровне пафоса.", "boots", "uncommon", (("agility", 3), ("strength", 1))),
    _spec("bt23", "Ботинки на тракторной подошве", "След остаётся даже в чужом настроении.", "boots", "uncommon", (("armor", 4), ("strength", 2), ("agility", -1))),
    _spec("bt24", "Кеды из маршрутки", "Знают короткий путь и два запрещённых поворота.", "boots", "uncommon", (("agility", 3), ("luck", 2))),
    _spec("bt25", "Сапоги семимильной очереди", "До кассы доходят без моральных потерь.", "boots", "uncommon", (("agility", 3), ("health", 3))),
    _spec("bt26", "Берцы ночного перекуса", "Тихо идут к холодильнику и к победе.", "boots", "rare", (("agility", 5), ("health", 4), ("luck", -1))),
    _spec("bt27", "Ботинки из чата дома", "Собраны всем подъездом и очень убедительны.", "boots", "rare", (("armor", 7), ("strength", 2), ("agility", -1))),
    _spec("bt28", "Кроссовки с красной кнопкой", "Нажимать нельзя. Поэтому нажали.", "boots", "rare", (("agility", 5), ("luck", 3), ("armor", -1))),
    _spec("bt29", "Сапоги главного по лужам", "Любая лужа становится личным кабинетом.", "boots", "rare", (("health", 6), ("armor", 4))),
    _spec("bt30", "Берцы полуночного призрака", "Легендарная форма берцев ночного перекуса.", "boots", "legendary", (("agility", 9), ("health", 5), ("luck", -3)), _effect("phantom_step", "Первые две обычные атаки врага гарантированно промахиваются.", 1, hits=2)),
    _spec("bt31", "Кроссовки исчезающей кнопки", "Красная кнопка теперь срабатывает между шагами.", "boots", "legendary", (("agility", 9), ("luck", 5), ("armor", -3)), _effect("afterimage", "После первого уворота следующая атака сильнее на 200%.", 200)),
    _spec("bt32", "Сапоги повелителя луж", "Лужи научились отматывать неудачный шаг назад.", "boots", "legendary", (("health", 10), ("armor", 6), ("agility", -4)), _effect("rewind", "Раз за бой смертельный удар отменяется и возвращает 25% максимального HP.", 25)),
    _spec("bt33", "Кроссовки нарушителя старта", "Стартуют на полшага раньше свистка.", "boots", "rare", (("agility", 5), ("luck", 3), ("health", -1)), _effect("first_strike", "Чаще ходит первым, и первые три удара сильнее на 60%.", 60)),
    _spec("bt34", "Кроссовки короля фальстартов", "Судьи сдались, свисток теперь просто разминка.", "boots", "legendary", (("agility", 9), ("luck", 6), ("health", -3)), _effect("first_strike", "Чаще ходит первым, и первые три удара сильнее на 100%.", 100)),
    _spec("bt35", "Ботинки на скрытой пружине", "Два удара — разбег. Третий — уже высказывание.", "boots", "rare", (("armor", 6), ("health", 3), ("agility", -1)), _effect("spring", "После двух полученных ударов следующий удар — двойной.", 100)),
    _spec("bt36", "Берцы на боевой пружине", "Два удара — терпение. Третий — разрыв контракта.", "boots", "legendary", (("armor", 10), ("health", 5), ("agility", -3)), _effect("spring", "После двух полученных ударов следующий удар — тройной.", 200)),
    _spec("bt37", "Сапоги с автогрелкой", "Каждый раунд греют ровно на один бинт вперёд.", "boots", "rare", (("health", 6), ("armor", 3)), _effect("regen", "Лечит 8 HP перед каждым действием — около 105 HP за бой.", 8)),
    _spec("bt38", "Сапоги с грелкой на максимум", "Раунд заканчивается — грелка выкладывается по полной.", "boots", "legendary", (("health", 10), ("armor", 5)), _effect("regen", "Лечит 16 HP перед каждым действием — около 210 HP за бой.", 16)),
    _spec("bt39", "Ботинки с кнопкой в носке", "Каждый удар оставляет маленькое, но обидное напоминание.", "boots", "rare", (("strength", 4), ("luck", 3), ("armor", -1)), _effect("bleed", "Каждое попадание добавляет кровотечение от 2 урона за раунд; стаки и урон растут с уровнем владельца.", 2)),
    _spec("bt40", "Берцы с гвоздями в подошве", "Напоминания копятся, и уже совсем не маленькие.", "boots", "legendary", (("strength", 8), ("luck", 6), ("armor", -3)), _effect("bleed", "Каждое попадание добавляет кровотечение от 5 урона за раунд; стаки и урон растут с уровнем владельца.", 5)),
)


GLOVE_SPECS: Final[tuple[GearSpec, ...]] = (
    _spec("gl01", "Варежки с котиком", "Котик осуждает, но лапы бережёт.", "gloves", "common", (("armor", 3),)),
    _spec("gl02", "Перчатки для пельменей", "Лепят удар ровно по шву.", "gloves", "common", (("strength", 2),)),
    _spec("gl03", "Рукавицы с дачи", "Помнят лопату и ни капли не боятся.", "gloves", "common", (("armor", 2), ("health", 2))),
    _spec("gl04", "Перчатки одного пальца", "Нажать кнопку могут. Обнять — уже нет.", "gloves", "common", (("luck", 2),)),
    _spec("gl05", "Митенки рокера-стажёра", "Громкие только в помещении с зеркалом.", "gloves", "common", (("strength", 1), ("agility", 1))),
    _spec("gl06", "Перчатки от свёклы", "Красный след — это просто автограф.", "gloves", "common", (("armor", 3), ("strength", -1))),
    _spec("gl07", "Варежки с резинкой", "Не теряются, даже когда хозяин старается.", "gloves", "common", (("health", 3),)),
    _spec("gl08", "Перчатки курьера", "Держат пакет, сдачу и выражение лица.", "gloves", "common", (("agility", 2),)),
    _spec("gl09", "Хозяйственные перчатки", "Справляются с грязью и неловкими разговорами.", "gloves", "common", (("armor", 2), ("luck", 1))),
    _spec("gl10", "Рукавицы с надписью «ОК»", "Согласие получено, спор закрыт.", "gloves", "common", (("strength", 2), ("armor", 1))),
    _spec("gl11", "Перчатки из бардачка", "Найдены рядом с проводом неизвестного назначения.", "gloves", "common", (("luck", 1), ("armor", 2))),
    _spec("gl12", "Варежки экономного дракона", "Огонь не дают, зато тепло не выпускают.", "gloves", "common", (("health", 3),)),
    _spec("gl13", "Перчатки для ремонта настроения", "После них всё либо работает, либо молчит.", "gloves", "common", (("strength", 1), ("armor", 2))),
    _spec("gl14", "Митенки с карманом", "Карман маленький, амбиции большие.", "gloves", "common", (("luck", 2),)),
    _spec("gl15", "Перчатки из автомата", "Выпали вместо игрушки, но готовы к бою.", "gloves", "common", (("agility", 1), ("armor", 2))),
    _spec("gl16", "Рукавицы строгой бабушки", "Щелчок по лбу становится дисциплиной.", "gloves", "common", (("strength", 2), ("health", 1))),
    _spec("gl17", "Перчатки с магнитом", "Притягивают мелочь и чужие проблемы.", "gloves", "uncommon", (("luck", 3), ("armor", 2))),
    _spec("gl18", "Варежки городского ниндзя", "Тихо шуршат только полиэтиленом.", "gloves", "uncommon", (("agility", 3), ("strength", 1))),
    _spec("gl19", "Перчатки мастера «потом»", "Надеты, чтобы отложить дело с комфортом.", "gloves", "uncommon", (("armor", 5), ("health", 2))),
    _spec("gl20", "Рукавицы с гречкой", "Тяжёлые, сытные, немного шуршат.", "gloves", "uncommon", (("strength", 3), ("health", 3), ("agility", -1))),
    _spec("gl21", "Перчатки соседского Wi-Fi", "Ловят сигнал даже сквозь неловкость.", "gloves", "uncommon", (("luck", 3), ("agility", 2))),
    _spec("gl22", "Митенки большой перемены", "Ударяют только после звонка.", "gloves", "uncommon", (("strength", 3), ("agility", 2))),
    _spec("gl23", "Перчатки с фонариком", "Светят на пыль, врага и плохие решения.", "gloves", "uncommon", (("armor", 4), ("luck", 2))),
    _spec("gl24", "Рукавицы аварийного оптимизма", "Держат крепко, когда план уже горит.", "gloves", "uncommon", (("health", 4), ("strength", 2))),
    _spec("gl25", "Перчатки с чеком", "Возврат не положен, бонусы положены.", "gloves", "uncommon", (("armor", 3), ("luck", 3))),
    _spec("gl26", "Рукавицы главного по банкам", "Открывают крышки и дипломатические двери.", "gloves", "rare", (("strength", 5), ("armor", 4), ("agility", -1))),
    _spec("gl27", "Перчатки с режимом «турбо»", "Режим включается ровно на самом видном месте.", "gloves", "rare", (("agility", 4), ("strength", 4), ("armor", -1))),
    _spec("gl28", "Варежки несгибаемого кассира", "Пересчитывают сдачу и противников дважды.", "gloves", "rare", (("armor", 7), ("luck", 2), ("agility", -1))),
    _spec("gl29", "Перчатки с запахом победы", "Никто не знает запаха, но все отступают.", "gloves", "rare", (("strength", 4), ("health", 5), ("luck", 1))),
    _spec("gl30", "Рукавицы повелителя банок", "Открывают крышки, двери и второй удар подряд.", "gloves", "legendary", (("strength", 9), ("armor", 6), ("agility", -3)), _effect("echo_strike", "Первое попадание повторяется эхом на 100% фактически нанесённого урона.", 100)),
    _spec("gl31", "Перчатки абсолютного турбо", "Режим больше не выключается после предупреждения.", "gloves", "legendary", (("strength", 9), ("agility", 6), ("armor", -4)), _effect("crushing_grip", "Первое попадание навсегда снижает урон врага на 20% в этом бою.", 20)),
    _spec("gl32", "Варежки последнего кассира", "Сдачу не дают, удары возвращают полностью.", "gloves", "legendary", (("armor", 10), ("luck", 5), ("agility", -4)), _effect("perfect_parry", "Первый полученный удар слабее на 90%; поглощённый урон добавляется к следующей атаке.", 90)),
    _spec("gl33", "Перчатки дартс-чемпиона", "Три вечера подряд без единого мимо.", "gloves", "rare", (("strength", 4), ("luck", 4), ("armor", -1)), _effect("precision", "Уворот врага ниже на 55%.", 55)),
    _spec("gl34", "Перчатки короля дартс-турнира", "Мимо давно не пролетало ничего, включая слухи.", "gloves", "legendary", (("strength", 8), ("luck", 7), ("armor", -3)), _effect("precision", "Уворот врага ниже на 85%.", 85)),
    _spec("gl35", "Перчатки дворового вратаря", "Ловит всё, что летит, и сразу назначает пенальти в ответ.", "gloves", "rare", (("armor", 5), ("strength", 3), ("agility", -1)), _effect("perfect_parry", "Первый полученный удар слабее на 70%; поглощённый урон добавляется к следующей атаке.", 70)),
    _spec("gl36", "Перчатки вратаря сборной двора", "Пенальти теперь назначает даже без повода.", "gloves", "legendary", (("armor", 9), ("strength", 5), ("agility", -3)), _effect("perfect_parry", "Первый полученный удар слабее на 110%; поглощённый урон добавляется к следующей атаке.", 110)),
    _spec("gl37", "Рукавицы уклониста-медика", "Уворачивается по инструкции, лечит по привычке.", "gloves", "rare", (("health", 5), ("agility", 3), ("luck", 1)), _effect("dodge_heal", "Каждый уворот лечит 90 HP.", 90)),
    _spec("gl38", "Рукавицы старшего уклониста-медика", "Уворачивается по призванию, лечит с запасом.", "gloves", "legendary", (("health", 9), ("agility", 5), ("luck", 2)), _effect("dodge_heal", "Каждый уворот лечит 160 HP.", 160)),
    _spec("gl39", "Перчатки с молотым перцем", "Прикосновение жжётся, соперник щурится ещё пару ходов.", "gloves", "rare", (("strength", 4), ("luck", 4), ("health", -1)), _effect("venom_blade", "Попадание копит яд от 4 урона и даёт следующей атаке врага 16% промаха; урон растёт с уровнем владельца.", 16, poison=4)),
    _spec("gl40", "Перчатки с контрабандным перцем чили", "Слёзы соперника теперь не высыхают два раунда подряд.", "gloves", "legendary", (("strength", 8), ("luck", 7), ("health", -3)), _effect("venom_blade", "Попадание копит яд от 7 урона и даёт следующей атаке врага 28% промаха; урон растёт с уровнем владельца.", 28, poison=7)),
)


GEAR_SPECS: Final[tuple[GearSpec, ...]] = BOOT_SPECS + GLOVE_SPECS
RAW_ITEMS: Final[tuple[dict[str, object], ...]] = tuple(item.raw_item() for item in GEAR_SPECS)
GEAR_COUNT: Final = len(GEAR_SPECS)
RARITY_COUNTS: Final = {
    rarity: sum(item.rarity == rarity for item in GEAR_SPECS) for rarity in RARITIES
}


def _validate_catalogue() -> None:
    assert len(BOOT_SPECS) == 40
    assert len(GLOVE_SPECS) == 40
    assert GEAR_COUNT == 80
    assert len({item.code for item in GEAR_SPECS}) == GEAR_COUNT
    assert len({item.name for item in GEAR_SPECS}) == GEAR_COUNT
    assert all(item.code.isascii() and item.code.isalnum() for item in GEAR_SPECS)
    assert all(item.slot in SLOTS and item.source == "drop" for item in GEAR_SPECS)
    assert all(item.buy_price == 0 and item.resale_price > 0 and item.drop_weight > 0
               for item in GEAR_SPECS)
    assert all(item.bonuses and all(key in STAT_KEYS and isinstance(value, int)
                                   for key, value in item.bonuses) for item in GEAR_SPECS)
    legendary = [item for item in GEAR_SPECS if item.rarity == "legendary"]
    assert len(legendary) == 14 and all(item.effect for item in legendary)
    assert all(item.effect_dict()["code"] in EFFECT_HOOKS for item in legendary)
    assert EFFECT_CODES <= set(EFFECT_HOOKS)
    assert not any(item.effect for item in GEAR_SPECS if item.rarity in ("common", "uncommon"))
    assert RARITY_COUNTS == {"common": 32, "uncommon": 18, "rare": 16, "legendary": 14}


_validate_catalogue()


__all__ = [
    "RARITIES", "STAT_KEYS", "SLOTS", "EFFECT_CODES", "GearSpec", "BOOT_SPECS", "GLOVE_SPECS",
    "GEAR_SPECS", "RAW_ITEMS", "GEAR_COUNT", "RARITY_COUNTS",
]
