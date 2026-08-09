"""Contract tests for the standalone drop-only boots and gloves catalogue."""

from dataclasses import FrozenInstanceError

import pytest

import pets_gear_catalog as catalogue


def test_catalogue_contains_exactly_thirty_items_for_each_new_slot():
    assert len(catalogue.BOOT_SPECS) == 30
    assert len(catalogue.GLOVE_SPECS) == 30
    assert catalogue.GEAR_COUNT == 60
    assert {item.slot for item in catalogue.BOOT_SPECS} == {"boots"}
    assert {item.slot for item in catalogue.GLOVE_SPECS} == {"gloves"}


def test_codes_and_names_are_unique_and_codes_are_stable_ascii():
    assert len({item.code for item in catalogue.GEAR_SPECS}) == 60
    assert len({item.name for item in catalogue.GEAR_SPECS}) == 60
    assert all(item.code.isascii() and item.code.isalnum() for item in catalogue.GEAR_SPECS)
    assert catalogue.BOOT_SPECS[0].code == "bt01"
    assert catalogue.GLOVE_SPECS[-1].code == "gl30"


def test_every_item_is_an_economical_drop_with_standard_stat_bonuses_only():
    for item in catalogue.GEAR_SPECS:
        assert item.source == "drop"
        assert item.buy_price == 0
        assert 1 <= item.drop_weight <= 12
        assert 1 <= item.resale_price <= 75
        assert item.bonuses
        assert all(key in catalogue.STAT_KEYS and isinstance(value, int)
                   for key, value in item.bonuses)
        assert all(-2 <= value <= 7 for _, value in item.bonuses)


def test_rarity_mix_is_balanced_and_legendary_items_are_very_uncommon():
    assert catalogue.RARITY_COUNTS == {
        "common": 32, "uncommon": 18, "rare": 8, "legendary": 2,
    }
    for slot_items in (catalogue.BOOT_SPECS, catalogue.GLOVE_SPECS):
        assert [item.rarity for item in slot_items].count("common") == 16
        assert [item.rarity for item in slot_items].count("uncommon") == 9
        assert [item.rarity for item in slot_items].count("rare") == 4
        assert [item.rarity for item in slot_items].count("legendary") == 1
        legendary_weight = sum(item.drop_weight for item in slot_items if item.rarity == "legendary")
        total_weight = sum(item.drop_weight for item in slot_items)
        assert legendary_weight / total_weight == pytest.approx(1 / 255)


def test_raw_items_match_the_existing_trade_record_schema_and_are_fresh_records():
    required = {
        "code", "name", "slot", "price", "source", "bonuses", "description",
        "rarity", "resale_price", "drop_weight",
    }
    assert len(catalogue.RAW_ITEMS) == 60
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
