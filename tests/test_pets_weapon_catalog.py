"""Contract tests for the standalone arena weapon catalogue."""

from dataclasses import FrozenInstanceError

import pytest

import pets_weapon_catalog as catalogue


def test_catalogue_has_exactly_500_unique_stable_weapons():
    assert catalogue.WEAPON_COUNT == 500
    assert len(catalogue.WEAPON_SPECS) == 500
    assert len({weapon.code for weapon in catalogue.WEAPON_SPECS}) == 500
    assert len({weapon.name for weapon in catalogue.WEAPON_SPECS}) == 500
    assert catalogue.WEAPON_SPECS[0].code == "w001"
    assert catalogue.WEAPON_SPECS[-1].code == "w500"
    assert all(weapon.code.isascii() and weapon.code.isalnum() for weapon in catalogue.WEAPON_SPECS)


def test_catalogue_is_immutable_and_can_adapt_to_existing_item_constructor():
    weapon = catalogue.WEAPON_SPECS[0]
    with pytest.raises(FrozenInstanceError):
        weapon.name = "Ordinary Stick"
    arguments = weapon.item_arguments()
    assert arguments[:5] == (weapon.code, weapon.name, "weapon", weapon.buy_price, weapon.source)
    assert arguments[5] == dict(weapon.bonuses)
    assert arguments[5] is not weapon.bonus_dict()


def test_first_three_ids_preserve_legacy_identity_stats_and_descriptions():
    assert [(weapon.code, weapon.name, weapon.source, weapon.price, dict(weapon.bonuses), weapon.description)
            for weapon in catalogue.WEAPON_SPECS[:3]] == [
        ("w001", "Мамин тапок", "shop", 10, {"strength": 6},
         "Летит точнее, чем кажется."),
        ("w002", "Мамина сковородка", "shop", 65,
         {"strength": 14, "luck": 4},
         "После неё спор окончен."),
        ("w003", "Швабра на изоленте", "drop", 0,
         {"strength": 20, "agility": -3},
         "Синяя. Значит, легендарная."),
    ]


def test_rarity_distribution_has_bad_average_good_and_few_legendary_items():
    assert catalogue.RARITY_COUNTS == {
        "cursed": 75, "common": 250, "uncommon": 120, "rare": 50, "legendary": 5,
    }
    cursed = [weapon for weapon in catalogue.WEAPON_SPECS if weapon.rarity == "cursed"]
    rares = [weapon for weapon in catalogue.WEAPON_SPECS if weapon.rarity == "rare"]
    legendary = [weapon for weapon in catalogue.WEAPON_SPECS if weapon.rarity == "legendary"]
    assert all(any(value < 0 for _, value in weapon.bonuses) for weapon in cursed)
    assert all(max(value for _, value in weapon.bonuses) >= 15 for weapon in rares)
    assert len(legendary) == 5
    assert all(dict(weapon.bonuses)["strength"] >= 19 for weapon in legendary)
    assert all(any(value < 0 for _, value in weapon.bonuses) for weapon in legendary)


def test_sources_prices_and_bonuses_are_sensible_for_the_current_combat_scale():
    for weapon in catalogue.WEAPON_SPECS:
        assert weapon.slot == "weapon"
        assert weapon.source in {"shop", "drop"}
        assert weapon.resale_price > 0
        assert all(key in catalogue.STAT_KEYS and isinstance(value, int)
                   for key, value in weapon.bonuses)
        if weapon.source == "shop":
            assert weapon.buy_price > 0
            assert weapon.resale_price <= weapon.buy_price // 4
        else:
            assert weapon.buy_price == 0
            assert weapon.drop_weight > 0
    ordinary_shop_strengths = [
        dict(weapon.bonuses).get("strength", 0)
        for weapon in catalogue.WEAPON_SPECS
        if weapon.rarity in {"common", "uncommon", "rare"} and weapon.source == "shop"
    ]
    assert min(ordinary_shop_strengths) >= 6
    assert max(ordinary_shop_strengths) <= 18
    assert max(dict(weapon.bonuses).get("strength", 0) for weapon in catalogue.WEAPON_SPECS) == 20


def test_shop_prices_match_fight_income_and_actual_combat_power():
    expected_bands = {
        "common": (10, 20),
        "uncommon": (50, 70),
        "rare": (130, 155),
    }
    for rarity, (minimum, maximum) in expected_bands.items():
        weapons = [
            weapon for weapon in catalogue.WEAPON_SPECS
            if weapon.source == "shop" and weapon.rarity == rarity
        ]
        assert min(weapon.buy_price for weapon in weapons) == minimum
        assert max(weapon.buy_price for weapon in weapons) == maximum
        for weapon in weapons:
            assert weapon.buy_price == catalogue.shop_price_for_bonuses(
                rarity, weapon.bonuses,
            )
            assert weapon.resale_price == max(1, weapon.buy_price * 20 // 100)

    # The cleanup migration still refunds what an old duplicate actually cost before
    # this rebalance, rather than today's much lower replacement price.
    assert catalogue.PRE_REBALANCE_BUY_PRICES["w001"] == 250
    assert catalogue.PRE_REBALANCE_BUY_PRICES["w002"] == 900


def test_raw_items_expose_trade_schema_and_legendary_drop_weight_is_about_one_percent():
    required = {
        "code", "name", "slot", "price", "source", "bonuses", "description", "rarity",
        "resale_price", "drop_weight",
    }
    assert len(catalogue.RAW_ITEMS) == 500
    assert all(required == set(record) for record in catalogue.RAW_ITEMS)
    drop_records = [record for record in catalogue.RAW_ITEMS if record["source"] == "drop"]
    legendary_weight = sum(record["drop_weight"] for record in drop_records if record["rarity"] == "legendary")
    total_weight = sum(record["drop_weight"] for record in drop_records)
    assert legendary_weight / total_weight == pytest.approx(5 / 530)
    cursed = [record for record in catalogue.RAW_ITEMS if record["rarity"] == "cursed"]
    assert cursed and all(record["source"] == "drop" for record in cursed)


def test_names_are_clear_and_descriptions_are_short():
    names = [weapon.name for weapon in catalogue.WEAPON_SPECS]
    descriptions = [weapon.description for weapon in catalogue.WEAPON_SPECS]
    assert len({name.split()[0] for name in names}) >= 40
    assert all("»:" not in name and "«" not in name for name in names)
    assert all(len(name) <= 50 for name in names)
    assert all(len(description) <= 65 for description in descriptions)
    assert any(name.startswith("Тапок ") for name in names)
    assert any(name.startswith("Сковородка ") for name in names)
    assert any("с авито" in name.lower() for name in names)
    assert any("из гаража" in name.lower() for name in names)
    assert not any(any(bad in name.lower() for bad in ("дедлайн", "созвон", "проверки")) for name in names)
    assert len(set(descriptions)) >= 100


def test_generated_rarities_are_interleaved_instead_of_front_loaded():
    first_fifty = [weapon.rarity for weapon in catalogue.WEAPON_SPECS[3:53]]
    assert len(set(first_fifty)) >= 4
    assert first_fifty.count("cursed") < 20


def test_generated_names_are_plain_readable_noun_phrases():
    generated_names = [weapon.name for weapon in catalogue.WEAPON_SPECS[3:]]
    assert all(not name.startswith("«") and ":" not in name for name in generated_names)
    legendary_names = {name for name, _ in catalogue._LEGENDARY_COPY}
    assert all(
        name in legendary_names or any(name.endswith(suffix) for suffix, _ in catalogue._THEMES)
        for name in generated_names
    )
