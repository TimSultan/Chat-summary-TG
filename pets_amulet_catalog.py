"""Drop-only amulets for the pet arena.

This module deliberately contains catalogue data, not combat rules.  Every entry has
one ``effect`` mapping which the combat layer can copy unchanged onto a fighter.  The
stable machine contract is:

``{"code": str, "text": str, "value": int, ...}``

``code`` is an engine identifier; ``text`` is the short player-facing explanation;
the remaining keys are scalar parameters interpreted only by that code.  Percentages
are whole numbers (``12`` means 12%), never floats.  ``RAW_ITEMS`` has the existing
item-record fields plus this one additive ``effect`` field.  Thus a legacy consumer can
drop ``effect`` and feed the rest directly to ``pets_config.Item``.

The combat integration must expose the effect on ``Item.effect`` and copy it to the
combat fighter before resolving a fight.  The event meanings are intentionally named
in :data:`EFFECT_HOOKS`; no text parsing or name-based behaviour is allowed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


RARITIES: Final = ("common", "uncommon", "rare", "legendary")
STAT_KEYS: Final = ("strength", "health", "agility", "luck", "armor")
RAW_ITEM_FIELDS: Final = frozenset({
    "code", "name", "slot", "price", "source", "bonuses", "description",
    "rarity", "resale_price", "drop_weight", "effect",
})

# Engine-facing contract.  Values in effects are intentionally small: one amulet
# should alter a close fight, not replace the four ordinary stats.
EFFECT_HOOKS: Final = {
    "opening_shield": "fight_start: grant a temporary shield based on maximum health",
    "opening_blast": "fight_start: deal a small opening hit",
    "battle_cry": "fight_start: boost own opening damage",
    "first_strike": "fight_start: increase chance to act first",
    "vitality": "fight_start: add temporary maximum health",
    "ferocity": "fight_start: add temporary damage",
    "nimble": "fight_start: add temporary dodge chance",
    "lucky": "fight_start: add temporary crit chance",
    "plating": "fight_start: add temporary damage reduction",
    "precision": "fight_start: reduce enemy dodge chance",
    "berserker": "on_attack: increase damage while below the health threshold",
    "executioner": "on_attack: increase damage against a low-health enemy",
    "vampiric": "after_damage: heal for a share of damage dealt",
    "piercing": "on_attack: ignore a share of enemy armour reduction",
    "combo": "after_hit: each consecutive hit adds a capped damage bonus",
    "poison": "after_hit: apply fixed damage at the opponent's next turn",
    "thorns": "after_damage_taken: reflect a share of received damage",
    "second_wind": "on_low_health_once: heal once after crossing the threshold",
    "last_stand": "on_lethal_once: survive a lethal blow at one health",
    "dodge_heal": "after_dodge: restore a small amount of health",
    "crit_guard": "on_critical_taken_once: cancel one critical multiplier",
    "retaliation": "after_damage_taken: gain damage after being hurt",
    "regen": "round_end: restore a small amount of health",
    "focused": "after_miss: next own attack gains damage",
    "momentum": "on_attack: gain a capped damage bonus as combat rounds pass",
    "gambler": "fight_start: deterministic seeded risk/reward roll",
    "safeguard": "fight_start: reduce the first incoming hit",
    "giant_slayer": "on_attack: deal more damage to a higher-level opponent",
    "collector": "fight_end_win: increase the chance that any item drops",
    "survivor": "fight_end_loss: retain a fraction of loss-gold only",
    "mirror_soul": "fight_start: match the opponent's stats, then jitter each by value%",
}


@dataclass(frozen=True, slots=True)
class AmuletSpec:
    """Immutable amulet record with an adapter for the shared item catalogue."""

    code: str
    name: str
    description: str
    rarity: str
    bonuses: tuple[tuple[str, int], ...]
    effect: tuple[tuple[str, str | int | bool], ...]
    resale_price: int
    drop_weight: int
    slot: str = "amulet"
    source: str = "drop"
    price: int = 0

    def bonus_dict(self) -> dict[str, int]:
        return dict(self.bonuses)

    def effect_dict(self) -> dict[str, str | int | bool]:
        return dict(self.effect)

    def item_arguments(self) -> tuple[str, str, str, int, str, dict[str, int], str]:
        """The legacy ``Item`` arguments; integration adds ``effect`` separately."""
        return (self.code, self.name, self.slot, self.price, self.source,
                self.bonus_dict(), self.description)

    def raw_item(self) -> dict[str, object]:
        return {
            "code": self.code, "name": self.name, "slot": self.slot,
            "price": self.price, "source": self.source,
            "bonuses": self.bonus_dict(), "description": self.description,
            "rarity": self.rarity, "resale_price": self.resale_price,
            "drop_weight": self.drop_weight, "effect": self.effect_dict(),
        }


def _effect(code: str, text: str, value: int, **params: int | bool) -> tuple[tuple[str, str | int | bool], ...]:
    return tuple({"code": code, "text": text, "value": value, **params}.items())


# Effects use only the 30 codes accepted by the passive-combat engine.  Rarity is
# deliberately spread 12 / 10 / 6 / 2 and all entries are drop-only.
_DATA: Final = (
    ("amulet_red_button", "Амулет красной кнопки", "Нажми — и щит на старте.", "common", {"armor": 3}, _effect("opening_shield", "В начале даёт щит на 3% максимального HP.", 3), 12, 20),
    ("amulet_crouton", "Сухарик последнего шанса", "Крошится, но больно летит.", "common", {"strength": 2}, _effect("opening_blast", "В начале наносит 4% текущего HP врага.", 4), 12, 20),
    ("amulet_loud_key", "Брелок «ОЧЕНЬ ГРОМКО»", "Врага слышно заранее.", "common", {"luck": 2}, _effect("battle_cry", "Первый удар сильнее на 12%.", 12), 12, 20),
    ("amulet_left_sock", "Амулет левого носка", "Правый так и не нашёлся.", "common", {"agility": 2}, _effect("first_strike", "Шанс ходить первым: +18%.", 18), 12, 20),
    ("amulet_tea_bag", "Чайный пакетик бессмертия", "Заварен уже пятый раз.", "common", {"health": 3}, _effect("vitality", "В бою +14 здоровья.", 14), 12, 20),
    ("amulet_angry_ravioli", "Злой пельмень в кулоне", "Смотрит с осуждением.", "common", {"strength": 2}, _effect("ferocity", "В бою +3 к урону.", 3), 12, 20),
    ("amulet_greased_coin", "Смазанная монетка", "Ускользает из кармана.", "common", {"agility": 2}, _effect("nimble", "Уворот в бою: +5%.", 5), 12, 20),
    ("amulet_broken_dice", "Кость с семью точками", "Математика в отпуске.", "common", {"luck": 3}, _effect("lucky", "Крит в бою: +5%.", 5), 12, 20),
    ("amulet_canned_lid", "Крышка от банки огурцов", "Пахнет победой и укропом.", "common", {"armor": 3}, _effect("plating", "Снижение входящего урона: +3%.", 3), 12, 20),
    ("amulet_laser_pointer", "Лазерная точка", "Все коты смотрят сюда.", "common", {"luck": 2}, _effect("precision", "Уворот врага ниже на 8%.", 8), 12, 20),
    ("amulet_hungry_calendar", "Голодный календарик", "Особенно зол по понедельникам.", "common", {"health": 2}, _effect("berserker", "Ниже 35% HP: +12% урона.", 12, threshold=35), 12, 20),
    ("amulet_tiny_hammer", "Молоточек для важных дел", "Для очень важных мелочей.", "common", {"strength": 2}, _effect("executioner", "Против врага ниже 30% HP: +16% урона.", 16, threshold=30), 12, 20),
    ("amulet_vampire_straw", "Вампирская трубочка", "Пьёт только боевой настрой.", "uncommon", {"strength": 3}, _effect("vampiric", "Лечит 9% нанесённого урона.", 9), 24, 12),
    ("amulet_drill_bit", "Сверло судьбы", "Проходит сквозь аргументы.", "uncommon", {"strength": 3}, _effect("piercing", "Игнорирует 12% защиты.", 12), 24, 12),
    ("amulet_staple_chain", "Цепочка из скрепок", "Каждый удар всё увереннее.", "uncommon", {"agility": 3}, _effect("combo", "Попадания: до +15% урона серией.", 5, cap=15), 24, 12),
    ("amulet_sour_candy", "Кислая конфета возмездия", "Язык помнит, враг тоже.", "uncommon", {"luck": 3}, _effect("poison", "Попадание: 3 урона в следующий ход.", 3), 24, 12),
    ("amulet_cactus_bead", "Бусина кактуса", "Обниматься не рекомендуется.", "uncommon", {"armor": 4}, _effect("thorns", "Возвращает 7% полученного урона.", 7), 24, 12),
    ("amulet_spare_heart", "Запасное сердечко", "Одноразовое, не стирать.", "uncommon", {"health": 4}, _effect("second_wind", "Один раз лечит 18% HP ниже 30%.", 18, threshold=30), 24, 12),
    ("amulet_cork_helmet", "Пробковый шлем", "Лоб крепче бюджета.", "uncommon", {"health": 3, "armor": 2}, _effect("last_stand", "Раз за бой переживает смертельный удар.", 1), 24, 12),
    ("amulet_rubber_duck", "Резиновая уточка тактика", "Крякает прямо в тайминг.", "uncommon", {"agility": 3}, _effect("dodge_heal", "После уворота лечит 7 HP.", 7), 24, 12),
    ("amulet_tea_strainer", "Ситечко от критов", "Процеживает особо обидное.", "uncommon", {"armor": 4}, _effect("crit_guard", "Первый крит врага слабее на 30%.", 30), 24, 12),
    ("amulet_angry_sponge", "Губка с характером", "Впитывает удар и злится.", "uncommon", {"health": 3}, _effect("retaliation", "После удара: +3 урона следующей атаке.", 3), 24, 12),
    ("amulet_potted_moss", "Карманный мох", "Фотосинтезирует из принципа.", "rare", {"health": 5}, _effect("regen", "В конце раунда лечит 4 HP.", 4), 48, 5),
    ("amulet_paperclip_scope", "Прицел из скрепки", "Промахнулся — запомнил.", "rare", {"luck": 4}, _effect("focused", "После промаха: +20% урона следующей атаке.", 20), 48, 5),
    ("amulet_windup_wheel", "Заводное колесо", "Разгоняется к финишу.", "rare", {"agility": 4}, _effect("momentum", "Каждый раунд: +2% урона, максимум +18%.", 2, cap=18), 48, 5),
    ("amulet_roulette_button", "Кнопка «авось»", "Вероятность смотрит в стену.", "rare", {"luck": 5}, _effect("gambler", "Старт: случайно +18% урона или -9%.", 18, downside=9), 48, 5),
    ("amulet_helmet_foil", "Шапочка из фольги", "Мысли врага не проходят.", "rare", {"armor": 5}, _effect("safeguard", "Первый полученный удар слабее на 35%.", 35), 48, 5),
    ("amulet_tall_ruler", "Линейка против больших", "Меряет противника по росту.", "rare", {"strength": 4}, _effect("giant_slayer", "Против более высокого уровня: +18% урона.", 18), 48, 5),
    ("amulet_trophy_magnet", "Магнит для трофеев", "Липнет к редким вещам.", "legendary", {"luck": 6}, _effect("collector", "Шанс выпадения любого предмета: +25%.", 25), 96, 1),
    ("amulet_last_receipt", "Последний чек", "Доказывает, что ты выжил.", "legendary", {"health": 6, "armor": 3}, _effect("survivor", "При поражении сохраняет 30% штрафа.", 30), 96, 1),
)


# The one amulet that is BOUGHT rather than found, and the only entry here with a price.
#
# Зеркало души is a deliberate exception to "amulets are drop-only": it exists so a strong
# pet can pick on a weak one without the arena punishing either side for it, and a thing
# that fixes a matchmaking problem has to be reliably obtainable rather than waiting on an
# 8% roll. source="shop" is all it takes to sit on the counter permanently -- only weapons
# rotate (see pets_config.daily_storefront_weapons and pets_ui._buyable_here).
#
# Priced at 250: above every other accessory on the shelf (10-170) because it is utility
# rather than stats, but well inside a week of ordinary play -- an item whose whole job is
# to make lopsided fights fair is worth little if the people who need it cannot buy it.
_SHOP_DATA: Final = (
    ("amulet_soul_mirror", "Зеркало души",
     "Показывает тебя ровно таким, каков соперник.", "rare",
     {}, _effect(
         "mirror_soul",
         "Перед боем опускает все твои статы до уровня соперника и разбрасывает их "
         "на ±20%. Награда за победу при этом не режется.",
         20,
     ), 50, 0, 250),
)


AMULET_SPECS: Final[tuple[AmuletSpec, ...]] = tuple(
    AmuletSpec(
        code=code, name=name, description=description, rarity=rarity,
        bonuses=tuple(bonuses.items()), effect=effect, resale_price=resale,
        drop_weight=weight,
    )
    for code, name, description, rarity, bonuses, effect, resale, weight in _DATA
) + tuple(
    AmuletSpec(
        code=code, name=name, description=description, rarity=rarity,
        bonuses=tuple(bonuses.items()), effect=effect, resale_price=resale,
        drop_weight=weight, source="shop", price=price,
    )
    for code, name, description, rarity, bonuses, effect, resale, weight, price in _SHOP_DATA
)
SHOP_AMULET_CODES: Final = frozenset(code for code, *_rest in _SHOP_DATA)
RAW_ITEMS: Final[tuple[dict[str, object], ...]] = tuple(item.raw_item() for item in AMULET_SPECS)
AMULET_COUNT: Final = len(AMULET_SPECS)
RARITY_COUNTS: Final = {rarity: sum(item.rarity == rarity for item in AMULET_SPECS) for rarity in RARITIES}


def _validate_catalogue() -> None:
    # 30 dropped plus however many are sold. Split rather than a single total, so adding
    # a shop amulet cannot quietly shrink the loot table it is meant to sit outside of.
    assert AMULET_COUNT == 30 + len(SHOP_AMULET_CODES)
    assert len(_DATA) == 30
    assert len({item.code for item in AMULET_SPECS}) == AMULET_COUNT
    assert len({item.name for item in AMULET_SPECS}) == AMULET_COUNT
    assert len({item.effect_dict()["code"] for item in AMULET_SPECS}) == AMULET_COUNT
    assert all(item.slot == "amulet" for item in AMULET_SPECS)
    # Two populations with opposite invariants. A dropped amulet is free and must be
    # rollable (drop_weight > 0); a shop amulet is bought and must NOT be in the drop
    # table, or the thing you can always buy would also be taking up loot rolls.
    dropped = [item for item in AMULET_SPECS if item.code not in SHOP_AMULET_CODES]
    bought = [item for item in AMULET_SPECS if item.code in SHOP_AMULET_CODES]
    assert all(item.source == "drop" and item.price == 0 for item in dropped)
    assert all(item.drop_weight > 0 for item in dropped)
    assert all(item.source == "shop" and item.price > 0 for item in bought)
    assert all(item.drop_weight == 0 for item in bought)
    assert all(item.resale_price > 0 for item in AMULET_SPECS)
    assert all(item.rarity in RARITIES for item in AMULET_SPECS)
    assert all(item.effect_dict()["code"] in EFFECT_HOOKS for item in AMULET_SPECS)
    assert all(isinstance(item.effect_dict()["value"], int) for item in AMULET_SPECS)
    assert all(key in STAT_KEYS and isinstance(value, int) for item in AMULET_SPECS for key, value in item.bonuses)
    assert all(set(record) == RAW_ITEM_FIELDS for record in RAW_ITEMS)
    assert RARITY_COUNTS == {"common": 12, "uncommon": 10, "rare": 7, "legendary": 2}


_validate_catalogue()


__all__ = [
    "RARITIES", "STAT_KEYS", "RAW_ITEM_FIELDS", "EFFECT_HOOKS", "AmuletSpec",
    "AMULET_SPECS", "RAW_ITEMS", "AMULET_COUNT", "RARITY_COUNTS", "SHOP_AMULET_CODES",
]
