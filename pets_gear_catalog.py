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


# A common item is deliberately modest.  The single legendary per slot is a fun
# trophy, not a replacement for a good weapon.  Weights make a legendary one item in
# 255 drops from either of these slot pools (about 0.39%).
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


def _effect(code: str, text: str, value: int) -> tuple[tuple[str, str | int | bool], ...]:
    return tuple({"code": code, "text": text, "value": value}.items())


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
    _spec("bt30", "Берцы полуночного призрака", "Легендарная форма берцев ночного перекуса.", "boots", "legendary", (("agility", 9), ("health", 5), ("luck", -3)), _effect("phantom_step", "Первая обычная атака врага гарантированно промахивается.", 1)),
    _spec("bt31", "Кроссовки исчезающей кнопки", "Красная кнопка теперь срабатывает между шагами.", "boots", "legendary", (("agility", 9), ("luck", 5), ("armor", -3)), _effect("afterimage", "После первого уворота следующая атака сильнее на 45%.", 45)),
    _spec("bt32", "Сапоги повелителя луж", "Лужи научились отматывать неудачный шаг назад.", "boots", "legendary", (("health", 10), ("armor", 6), ("agility", -4)), _effect("rewind", "Раз за бой смертельный удар отменяется и возвращает 25% максимального HP.", 25)),
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
    _spec("gl30", "Рукавицы повелителя банок", "Открывают крышки, двери и второй удар подряд.", "gloves", "legendary", (("strength", 9), ("armor", 6), ("agility", -3)), _effect("echo_strike", "Первое попадание повторяется эхом на 50% нанесённого урона.", 50)),
    _spec("gl31", "Перчатки абсолютного турбо", "Режим больше не выключается после предупреждения.", "gloves", "legendary", (("strength", 9), ("agility", 6), ("armor", -4)), _effect("crushing_grip", "Первое попадание навсегда снижает урон врага на 10% в этом бою.", 10)),
    _spec("gl32", "Варежки последнего кассира", "Сдачу не дают, удары возвращают полностью.", "gloves", "legendary", (("armor", 10), ("luck", 5), ("agility", -4)), _effect("perfect_parry", "Первый полученный удар слабее на 35%; поглощённый урон добавляется к следующей атаке.", 35)),
)


GEAR_SPECS: Final[tuple[GearSpec, ...]] = BOOT_SPECS + GLOVE_SPECS
RAW_ITEMS: Final[tuple[dict[str, object], ...]] = tuple(item.raw_item() for item in GEAR_SPECS)
GEAR_COUNT: Final = len(GEAR_SPECS)
RARITY_COUNTS: Final = {
    rarity: sum(item.rarity == rarity for item in GEAR_SPECS) for rarity in RARITIES
}


def _validate_catalogue() -> None:
    assert len(BOOT_SPECS) == 32
    assert len(GLOVE_SPECS) == 32
    assert GEAR_COUNT == 64
    assert len({item.code for item in GEAR_SPECS}) == GEAR_COUNT
    assert len({item.name for item in GEAR_SPECS}) == GEAR_COUNT
    assert all(item.code.isascii() and item.code.isalnum() for item in GEAR_SPECS)
    assert all(item.slot in SLOTS and item.source == "drop" for item in GEAR_SPECS)
    assert all(item.buy_price == 0 and item.resale_price > 0 and item.drop_weight > 0
               for item in GEAR_SPECS)
    assert all(item.bonuses and all(key in STAT_KEYS and isinstance(value, int)
                                   for key, value in item.bonuses) for item in GEAR_SPECS)
    legendary = [item for item in GEAR_SPECS if item.rarity == "legendary"]
    assert len(legendary) == 6 and all(item.effect for item in legendary)
    assert {item.effect_dict()["code"] for item in legendary} == EFFECT_CODES
    assert EFFECT_CODES <= set(EFFECT_HOOKS)
    assert not any(item.effect for item in GEAR_SPECS if item.rarity != "legendary")
    assert RARITY_COUNTS == {"common": 32, "uncommon": 18, "rare": 8, "legendary": 6}


_validate_catalogue()


__all__ = [
    "RARITIES", "STAT_KEYS", "SLOTS", "EFFECT_CODES", "GearSpec", "BOOT_SPECS", "GLOVE_SPECS",
    "GEAR_SPECS", "RAW_ITEMS", "GEAR_COUNT", "RARITY_COUNTS",
]
