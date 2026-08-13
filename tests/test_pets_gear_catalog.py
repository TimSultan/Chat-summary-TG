"""Contract tests for the standalone drop-only boots and gloves catalogue."""

from dataclasses import FrozenInstanceError

import pytest

import pets_gear_catalog as catalogue


def test_catalogue_contains_exactly_forty_items_for_each_new_slot():
    assert len(catalogue.BOOT_SPECS) == 40
    assert len(catalogue.GLOVE_SPECS) == 40
    assert catalogue.GEAR_COUNT == 80
    assert {item.slot for item in catalogue.BOOT_SPECS} == {"boots"}
    assert {item.slot for item in catalogue.GLOVE_SPECS} == {"gloves"}


def test_codes_and_names_are_unique_and_codes_are_stable_ascii():
    assert len({item.code for item in catalogue.GEAR_SPECS}) == 80
    assert len({item.name for item in catalogue.GEAR_SPECS}) == 80
    assert all(item.code.isascii() and item.code.isalnum() for item in catalogue.GEAR_SPECS)
    assert catalogue.BOOT_SPECS[0].code == "bt01"
    assert catalogue.GLOVE_SPECS[-1].code == "gl40"


def test_every_item_is_an_economical_drop_with_standard_stat_bonuses_only():
    for item in catalogue.GEAR_SPECS:
        assert item.source == "drop"
        assert item.buy_price == 0
        assert 1 <= item.drop_weight <= 12
        assert 1 <= item.resale_price <= 75
        assert item.bonuses
        assert all(key in catalogue.STAT_KEYS and isinstance(value, int)
                   for key, value in item.bonuses)
        limit = 10 if item.rarity == "legendary" else 7
        floor = -5 if item.rarity == "legendary" else -2
        assert all(floor <= value <= limit for _, value in item.bonuses)


def test_rarity_mix_is_balanced_and_legendary_items_are_very_uncommon():
    assert catalogue.RARITY_COUNTS == {
        "common": 32, "uncommon": 18, "rare": 16, "legendary": 14,
    }
    for slot_items in (catalogue.BOOT_SPECS, catalogue.GLOVE_SPECS):
        assert [item.rarity for item in slot_items].count("common") == 16
        assert [item.rarity for item in slot_items].count("uncommon") == 9
        assert [item.rarity for item in slot_items].count("rare") == 8
        assert [item.rarity for item in slot_items].count("legendary") == 7
        # Still about one legendary in forty drops from this pool. The build items
        # doubled the rare and legendary shelves, so the ratio moved -- what has to
        # hold is that a legendary stays a trophy rather than an expectation.
        legendary_weight = sum(item.drop_weight for item in slot_items if item.rarity == "legendary")
        total_weight = sum(item.drop_weight for item in slot_items)
        assert legendary_weight / total_weight == pytest.approx(7 / 269)


def test_every_rare_and_legendary_build_item_carries_a_declared_effect():
    """Effects used to be legendary-only. Builds need a rare rung too, so the rule is
    now about which tiers may carry one -- not that only the top tier does."""
    legendary = [item for item in catalogue.GEAR_SPECS if item.rarity == "legendary"]
    assert len(legendary) == 14
    assert all(item.effect for item in legendary)
    effectful = [item for item in catalogue.GEAR_SPECS if item.effect]
    assert {item.rarity for item in effectful} == {"rare", "legendary"}
    assert {item.effect_dict()["code"] for item in effectful} == catalogue.EFFECT_CODES


def test_raw_items_match_the_existing_trade_record_schema_and_are_fresh_records():
    required = {
        "code", "name", "slot", "price", "source", "bonuses", "description",
        "rarity", "resale_price", "drop_weight", "effect",
    }
    assert len(catalogue.RAW_ITEMS) == 80
    assert all(set(record) == required for record in catalogue.RAW_ITEMS)
    first = catalogue.GEAR_SPECS[0]
    record = first.raw_item()
    record["bonuses"]["agility"] = 99
    assert first.bonus_dict()["agility"] == 2
    assert first.item_arguments()[:5] == (
        first.code, first.name, first.slot, 0, "drop",
    )


def test_specs_are_immutable_and_russian_copy_is_short_and_readable():
    with pytest.raises(FrozenInstanceError):
        catalogue.BOOT_SPECS[0].name = "Новые ботинки"
    assert all(len(item.name) <= 50 and len(item.description) <= 80
               for item in catalogue.GEAR_SPECS)
    assert all(any("А" <= character <= "я" for character in item.name)
               for item in catalogue.GEAR_SPECS)
