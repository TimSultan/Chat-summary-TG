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
    "poison": "after_hit: apply level-scaled damage at the opponent's next turn",
    "thorns": "after_damage_taken: reflect a share of received damage",
    "second_wind": "on_low_health_once: heal once after crossing the threshold",
    "last_stand": "on_lethal_once: survive a lethal blow at one health",
    "dodge_heal": "after_dodge: restore a small amount of health",
    "crit_guard": "on_critical_taken_once: cancel one critical multiplier",
    "retaliation": "after_damage_taken: gain level-scaled damage after being hurt",
    "regen": "round_end: restore a small amount of health",
    "focused": "after_miss: next own attack gains damage",
    "momentum": "on_attack: gain a capped damage bonus as combat rounds pass",
    "gambler": "fight_start: deterministic seeded risk/reward roll",
    "safeguard": "fight_start: reduce the first incoming hit",
    "giant_slayer": "on_attack: deal more damage to a higher-level opponent",
    "mob_hunter": "on_attack: deal more damage to a mob",
    "mob_ward": "after_damage_taken: reduce damage received from a mob",
    "collector": "fight_end_win: increase the chance that any item drops",
    "survivor": "fight_end_loss: retain a fraction of loss-gold only",
    "mirror_soul": "fight_start: match the opponent's stats, then jitter each by value%",
    "bite": "after_first_hit: bite once, then heal for a share of bite damage",
    "armor_burst": "after_damage_taken_once: heavily reduce the next incoming hit",
    "late_strike": "on_first_attack_second: increase the opening counterattack",
    "medkit": "on_low_health_once: restore a share of maximum health",
    "countercrit": "on_critical_taken_once: cancel it and prepare a counterattack",
    "trophy_compass": "fight_end_win: increase drops after defeating a higher-level foe",
    "stun": "after_first_critical: the opponent skips one attack",
    "cocoon": "on_first_attack: skip it and reflect the next received hit",
    "glass_crit": "on_first_critical: greatly increase critical damage",
    "blood_pact": "after_every_third_hit: heal for a share of damage dealt",
    "chill": "after_first_hit: weaken the opponent's next attack",
    "tesla": "after_third_hit: shock the opponent for a share of maximum health",
    "death_shield": "on_lethal_once: survive at one health and gain a shield",
    "acid": "after_first_miss: next hit cannot be dodged and pierces armour",
    "spring": "after_two_hits_taken: double the next attack",
    "candle": "fight_start: gain a large random damage bonus or penalty",
    "armor_shred": "after_hit: progressively reduce enemy armour effectiveness",
    "wound": "after_hit: progressively reduce enemy maximum health for this fight",
    "burn": "after_hit: ignite the enemy for level-scaled damage on their next turns",
    "venom_blade": "after_hit: deal level-scaled poison and add a miss chance to the next attack",
    "coin_rake": "fight_end_win: add a capped coin bonus based on landed hits",
    "bleed": "after_hit: stack level-scaled bleeding damage on the enemy",
    "shield_breaker": "on_first_hit: ignore armour and break active shields",
    "heavy_combo": "on_nth_hit: greatly increase that hit's damage",
    "phantom_step": "on_first_attack_taken: force the ordinary attack to miss",
    "afterimage": "after_first_dodge: empower the next own attack",
    "rewind": "on_lethal_once: cancel the hit and restore a share of maximum health",
    "echo_strike": "after_first_hit: repeat a share of the damage dealt",
    "crushing_grip": "after_first_hit: permanently weaken enemy damage for this fight",
    "perfect_parry": "after_damage_taken_once: absorb damage and add it to the next attack",
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
    ("amulet_loud_key", "Брелок «ОЧЕНЬ ГРОМКО»", "Врага слышно заранее.", "common", {"luck": 2}, _effect("battle_cry", "Первый удар сильнее на 55%.", 55), 12, 20),
    ("amulet_left_sock", "Амулет левого носка", "Правый так и не нашёлся.", "common", {"agility": 2}, _effect("first_strike", "Чаще ходит первым, и первые три удара сильнее на 25%.", 25), 12, 20),
    ("amulet_tea_bag", "Чайный пакетик бессмертия", "Заварен уже пятый раз.", "common", {"health": 3}, _effect("vitality", "В бою +60 здоровья.", 60), 12, 20),
    ("amulet_angry_ravioli", "Злой пельмень в кулоне", "Смотрит с осуждением.", "common", {"strength": 2}, _effect("ferocity", "В бою +6 к урону.", 6), 12, 20),
    ("amulet_greased_coin", "Смазанная монетка", "Ускользает из кармана.", "common", {"agility": 2}, _effect("nimble", "Уворот в бою: +5%.", 5), 12, 20),
    ("amulet_broken_dice", "Кость с семью точками", "Математика в отпуске.", "common", {"luck": 3}, _effect("lucky", "Крит в бою: +5%.", 5), 12, 20),
    ("amulet_canned_lid", "Крышка от банки огурцов", "Пахнет победой и укропом.", "common", {"armor": 3}, _effect("plating", "Снижение входящего урона: +3%.", 3), 12, 20),
    ("amulet_laser_pointer", "Лазерная точка", "Все коты смотрят сюда.", "common", {"luck": 2}, _effect("precision", "Уворот врага ниже на 25%.", 25), 12, 20),
    ("amulet_hungry_calendar", "Голодный календарик", "Особенно зол по понедельникам.", "common", {"health": 2}, _effect("berserker", "Ниже 35% HP: +12% урона.", 12, threshold=35), 12, 20),
    ("amulet_tiny_hammer", "Молоточек для важных дел", "Для очень важных мелочей.", "common", {"strength": 2}, _effect("executioner", "Против врага ниже 30% HP: +16% урона.", 16, threshold=30), 12, 20),
    ("amulet_vampire_straw", "Вампирская трубочка", "Пьёт только боевой настрой.", "uncommon", {"strength": 3}, _effect("vampiric", "Лечит 9% нанесённого урона.", 9), 24, 12),
    ("amulet_drill_bit", "Сверло судьбы", "Проходит сквозь аргументы.", "uncommon", {"strength": 3}, _effect("piercing", "Игнорирует 12% защиты.", 12), 24, 12),
    ("amulet_staple_chain", "Цепочка из скрепок", "Каждый удар всё увереннее.", "uncommon", {"agility": 3}, _effect("combo", "Попадания: до +15% урона серией.", 5, cap=15), 24, 12),
    ("amulet_sour_candy", "Кислая конфета возмездия", "Язык помнит, враг тоже.", "uncommon", {"luck": 3}, _effect("poison", "Каждое попадание копит 15 базового урона к следующему ходу соперника; урон растёт с уровнем владельца.", 15), 24, 12),
    ("amulet_cactus_bead", "Бусина кактуса", "Обниматься не рекомендуется.", "uncommon", {"armor": 4}, _effect("thorns", "Возвращает 7% полученного урона.", 7), 24, 12),
    ("amulet_spare_heart", "Запасное сердечко", "Одноразовое, не стирать.", "uncommon", {"health": 4}, _effect("second_wind", "Один раз лечит 18% HP ниже 30%.", 18, threshold=30), 24, 12),
    ("amulet_cork_helmet", "Пробковый шлем", "Лоб крепче бюджета.", "uncommon", {"health": 3, "armor": 2}, _effect("last_stand", "Раз за бой переживает смертельный удар.", 1), 24, 12),
    ("amulet_rubber_duck", "Резиновая уточка тактика", "Крякает прямо в тайминг.", "uncommon", {"agility": 3}, _effect("dodge_heal", "Каждый уворот лечит 70 HP.", 70), 24, 12),
    ("amulet_tea_strainer", "Ситечко от критов", "Процеживает особо обидное.", "uncommon", {"armor": 4}, _effect("crit_guard", "Первый крит врага слабее на 30%.", 30), 24, 12),
    ("amulet_angry_sponge", "Губка с характером", "Впитывает удар и злится.", "uncommon", {"health": 3}, _effect("retaliation", "После каждого пропущенного удара следующая атака получает от +8 урона; бонус растёт с уровнем владельца.", 8), 24, 12),
    ("amulet_potted_moss", "Карманный мох", "Фотосинтезирует из принципа.", "rare", {"health": 5}, _effect("regen", "Лечит 9 HP перед каждым действием — около 120 HP за бой.", 9), 48, 5),
    ("amulet_paperclip_scope", "Прицел из скрепки", "Промахнулся — запомнил.", "rare", {"luck": 4}, _effect("focused", "После промаха: +110% урона следующей атаке.", 110), 48, 5),
    ("amulet_windup_wheel", "Заводное колесо", "Разгоняется к финишу.", "rare", {"agility": 4}, _effect("momentum", "Каждый раунд: +2% урона, максимум +18%.", 2, cap=18), 48, 5),
    ("amulet_roulette_button", "Кнопка «авось»", "Вероятность смотрит в стену.", "rare", {"luck": 5}, _effect("gambler", "Старт: чаще +60% урона, реже -10%.", 60, downside=10, chance=55), 48, 5),
    ("amulet_helmet_foil", "Шапочка из фольги", "Мысли врага не проходят.", "rare", {"armor": 5}, _effect("safeguard", "Первый полученный удар слабее на 90%.", 90), 48, 5),
    ("amulet_tall_ruler", "Линейка против больших", "Меряет противника по росту.", "rare", {"strength": 4}, _effect("giant_slayer", "Против более высокого уровня: +45% урона.", 45), 48, 5),
    ("amulet_trophy_magnet", "Магнит для трофеев", "Липнет к редким вещам.", "legendary", {"luck": 6}, _effect("collector", "Шанс выпадения любого предмета: +25%.", 25), 96, 1),
    ("amulet_last_receipt", "Последний чек", "Доказывает, что ты выжил.", "legendary", {"health": 6, "armor": 3}, _effect("survivor", "При поражении сохраняет 30% штрафа.", 30), 96, 1),
    ("amulet_hornet_sting", "Жало королевы шершней", "Гудит так, будто уже выбрало следующую жертву.", "legendary", {"luck": 8, "health": -7}, _effect("stun", "Первые три крита оглушают врага на один ход.", 1, cap=3), 96, 1),
    ("amulet_thorn_cocoon", "Шипастый кокон", "Выглядит мирно ровно до первого удара.", "legendary", {"health": 10, "strength": -6}, _effect("cocoon", "Первый ход пропускает и возвращает следующий полученный удар в 2.5 раза сильнее.", 250), 96, 1),
    ("amulet_glass_eye", "Всевидящий стеклянный глаз", "Видит идеальный удар и совсем не видит опасность.", "legendary", {"luck": 12, "armor": -10}, _effect("glass_crit", "Первый крит наносит на 200% больше урона.", 200), 96, 1),
    ("amulet_blood_pact", "Кровавый договор", "Подписан чем-то липким.", "legendary", {"strength": 9, "health": -10}, _effect("blood_pact", "Каждый третий успешный удар лечит на 70% нанесённого урона.", 70), 96, 1),
    ("amulet_frost_fang", "Клык вечной мерзлоты", "Рядом с ним даже злость покрывается инеем.", "legendary", {"agility": 9, "strength": -5}, _effect("chill", "Первое попадание почти полностью гасит два следующих удара врага.", 100, hits=2), 96, 1),
    ("amulet_tesla_coil", "Сердце шаровой молнии", "Трещит от нетерпения уже на втором ударе.", "legendary", {"strength": 9, "armor": -6}, _effect("tesla", "Третье попадание выпускает разряд на 15% максимального HP врага.", 15), 96, 1),
    ("amulet_not_today", "Медаль «Не сегодня»", "Один раз можно поспорить с судьбой.", "legendary", {"armor": 8, "luck": -6}, _effect("death_shield", "Смертельный удар оставляет 1 HP и даёт щит на 20% HP.", 20), 96, 1),
    ("amulet_broken_flask", "Битая колба", "Промахнулся — стало только опаснее.", "uncommon", {"strength": 5, "health": -4}, _effect("acid", "После первого промаха следующий удар нельзя увернуть, он пробивает броню и бьёт на 80% сильнее.", 80), 24, 12),
    ("amulet_angry_spring", "Пружина злости", "Два удара — и её уже не удержать.", "uncommon", {"health": 4, "armor": -3}, _effect("spring", "После двух полученных ударов следующий удар наносит двойной урон.", 100), 24, 12),
    ("amulet_black_candle", "Свеча чёрного солнца", "Светит только тому, кому сегодня повезло.", "legendary", {"luck": 10, "health": -8}, _effect("candle", "В начале боя: обычно +90% к урону, иногда -20%.", 90, downside=20, chance=70), 96, 1),
)


# The one amulet that is BOUGHT rather than found, and the only entry here with a price.
#
# Зеркало души is a deliberate exception to "amulets are drop-only": it exists so a strong
# pet can pick on a weak one without the arena punishing either side for it, and a thing
# that fixes a matchmaking problem has to be reliably obtainable rather than waiting on an
# 8% roll. It remains a normal catalog item and can appear in the personal rotating shelf
# like every other equipment item (see pets_config.daily_storefront_items and pets_ui._buyable_here).
#
# Priced at 250: above every other accessory on the shelf (10-170) because it is utility
# rather than stats, but well inside a week of ordinary play -- an item whose whole job is
# to make lopsided fights fair is worth little if the people who need it cannot buy it.
_SHOP_DATA: Final = (
    ("amulet_leech_fang", "Клык пиявки", "Улыбается, когда становится больно.", "rare",
     {"strength": 5, "agility": -4}, _effect(
         "bite", "Первое попадание дополнительно кусает врага и лечит на 90% урона укуса.", 90,
     ), 32, 0, 140),
    ("amulet_armor_capsule", "Бронекапсула", "Разбивается строго в самый нужный момент.", "uncommon",
     {"armor": 7, "luck": -3}, _effect(
         "armor_burst", "После первого полученного удара следующий слабее на 75%.", 75,
     ), 26, 0, 110),
    ("amulet_initiative_pendulum", "Маятник инициативы", "Любит, когда начинают не с него.", "uncommon",
     {"agility": 5, "health": -2}, _effect(
         "late_strike", "Первый удар вторым в раунде сильнее на 130%.", 130,
     ), 24, 0, 90),
    ("amulet_first_aid_heart", "Сердце аптечки", "Тихо шуршит бинтами.", "uncommon",
     {"health": 8, "strength": -2}, _effect(
         "medkit", "На 35% HP один раз восстанавливает 20% максимального здоровья.", 20, threshold=35,
     ), 26, 0, 130),
    ("amulet_crit_catcher", "Ловец критов", "Особенно любит ловить обидные.", "rare",
     {"armor": 4, "agility": -3}, _effect(
         "countercrit", "Первый крит врага отменяет и усиливает следующий ответный удар на 20%.", 20,
     ), 34, 0, 150),
    ("amulet_trophy_compass", "Компас трофеев", "Всегда указывает на того, кто сильнее.", "rare",
     {"luck": 4, "armor": -2}, _effect(
         "trophy_compass", "Победа над соперником выше уровнем: +35% к шансу дропа.", 35,
     ), 36, 0, 170),
    ("amulet_mob_ward", "Оберег охотника",
     "Не любит рычание и предпочитает, чтобы оно било мимо.", "rare",
     {}, _effect(
         "mob_ward",
         "От мобов: на 30% меньше получаемого урона.",
         30,
     ), 44, 0, 220),
)


# --- the vault ------------------------------------------------------------------------
# Withdrawn from the game but NOT deleted. Every stored fight snapshot, audit row and
# replay names the items its fighters wore, and a code that stops resolving turns those
# into blanks or crashes -- so a retired item keeps its catalogue entry and merely stops
# being obtainable. `source="vault"` is what does it: every shop shelf filters on
# source == "shop" and every loot pool on source == "drop", so a third value is excluded
# from both without either of them needing to learn about retirement.
#
# amulet_soul_mirror was withdrawn on 2026-08-16. pets.retire_soul_mirror takes it out of
# the inventories that already hold it and pays the purchase price back.
_VAULT_DATA: Final = (
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
) + tuple(
    AmuletSpec(
        code=code, name=name, description=description, rarity=rarity,
        bonuses=tuple(bonuses.items()), effect=effect, resale_price=resale,
        drop_weight=weight, source="vault", price=price,
    )
    for code, name, description, rarity, bonuses, effect, resale, weight, price in _VAULT_DATA
)
SHOP_AMULET_CODES: Final = frozenset(code for code, *_rest in _SHOP_DATA)
VAULT_AMULET_CODES: Final = frozenset(code for code, *_rest in _VAULT_DATA)
RAW_ITEMS: Final[tuple[dict[str, object], ...]] = tuple(item.raw_item() for item in AMULET_SPECS)
AMULET_COUNT: Final = len(AMULET_SPECS)
RARITY_COUNTS: Final = {rarity: sum(item.rarity == rarity for item in AMULET_SPECS) for rarity in RARITIES}


def _validate_catalogue() -> None:
    # 40 dropped plus however many are sold. Split rather than a single total, so adding
    # a shop amulet cannot quietly shrink the loot table it is meant to sit outside of.
    assert AMULET_COUNT == 40 + len(SHOP_AMULET_CODES) + len(VAULT_AMULET_CODES)
    assert len(_DATA) == 40
    assert len({item.code for item in AMULET_SPECS}) == AMULET_COUNT
    assert len({item.name for item in AMULET_SPECS}) == AMULET_COUNT
    assert len({item.effect_dict()["code"] for item in AMULET_SPECS}) == AMULET_COUNT
    assert all(item.slot == "amulet" for item in AMULET_SPECS)
    # Two populations with opposite invariants. A dropped amulet is free and must be
    # rollable (drop_weight > 0); a shop amulet is bought and must NOT be in the drop
    # table, or the thing you can always buy would also be taking up loot rolls.
    retired = [item for item in AMULET_SPECS if item.code in VAULT_AMULET_CODES]
    dropped = [
        item for item in AMULET_SPECS
        if item.code not in SHOP_AMULET_CODES and item.code not in VAULT_AMULET_CODES
    ]
    bought = [item for item in AMULET_SPECS if item.code in SHOP_AMULET_CODES]
    assert all(item.source == "drop" and item.price == 0 for item in dropped)
    assert all(item.drop_weight > 0 for item in dropped)
    assert all(item.source == "shop" and item.price > 0 for item in bought)
    assert all(item.drop_weight == 0 for item in bought)
    # A vaulted amulet is in neither population: it keeps its price so the retirement can
    # pay it back, and its weight stays at zero so it can never re-enter the loot table.
    assert all(item.source == "vault" and item.price > 0 for item in retired)
    assert all(item.drop_weight == 0 for item in retired)
    assert all(item.resale_price > 0 for item in AMULET_SPECS)
    assert all(item.rarity in RARITIES for item in AMULET_SPECS)
    assert all(item.effect_dict()["code"] in EFFECT_HOOKS for item in AMULET_SPECS)
    assert all(isinstance(item.effect_dict()["value"], int) for item in AMULET_SPECS)
    assert all(key in STAT_KEYS and isinstance(value, int) for item in AMULET_SPECS for key, value in item.bonuses)
    assert all(set(record) == RAW_ITEM_FIELDS for record in RAW_ITEMS)
    assert RARITY_COUNTS == {"common": 12, "uncommon": 15, "rare": 11, "legendary": 10}


_validate_catalogue()


__all__ = [
    "RARITIES", "STAT_KEYS", "RAW_ITEM_FIELDS", "EFFECT_HOOKS", "AmuletSpec",
    "AMULET_SPECS", "RAW_ITEMS", "AMULET_COUNT", "RARITY_COUNTS", "SHOP_AMULET_CODES",
]
