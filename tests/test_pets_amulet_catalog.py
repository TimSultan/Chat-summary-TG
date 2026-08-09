"""Contract tests for the independent 30-amulet drop catalogue."""

from dataclasses import FrozenInstanceError

import pytest

import pets_amulet_catalog as catalogue


def test_exactly_thirty_unique_drop_only_amulets():
    assert catalogue.AMULET_COUNT == 30
    assert len(catalogue.AMULET_SPECS) == 30
    assert len({item.code for item in catalogue.AMULET_SPECS}) == 30
    assert len({item.name for item in catalogue.AMULET_SPECS}) == 30
    assert all(item.slot == "amulet" and item.source == "drop" and item.price == 0
               for item in catalogue.AMULET_SPECS)
    assert all(item.drop_weight > 0 for item in catalogue.AMULET_SPECS)


def test_each_amulet_has_one_machine_readable_unique_effect():
    codes = []
    for item in catalogue.AMULET_SPECS:
        effect = item.effect_dict()
        assert effect["code"] in catalogue.EFFECT_HOOKS
        assert isinstance(effect["text"], str) and effect["text"]
        assert isinstance(effect["value"], int)
        codes.append(effect["code"])
    assert len(set(codes)) == 30


def test_raw_records_extend_existing_item_shape_only_with_effect():
    assert len(catalogue.RAW_ITEMS) == 30
    assert all(set(record) == catalogue.RAW_ITEM_FIELDS for record in catalogue.RAW_ITEMS)
    assert all(set(record["bonuses"]).issubset(catalogue.STAT_KEYS) for record in catalogue.RAW_ITEMS)
    assert all(record["effect"]["code"] in catalogue.EFFECT_HOOKS for record in catalogue.RAW_ITEMS)


def test_specs_are_frozen_and_legacy_item_adapter_stays_compatible():
    amulet = catalogue.AMULET_SPECS[0]
    with pytest.raises(FrozenInstanceError):
        amulet.name = "Подмена"
    arguments = amulet.item_arguments()
    assert arguments[:5] == (amulet.code, amulet.name, "amulet", 0, "drop")
    assert arguments[5] == dict(amulet.bonuses)
    assert arguments[6] == amulet.description


def test_rarities_are_spread_and_copy_is_short():
    assert catalogue.RARITY_COUNTS == {"common": 12, "uncommon": 10, "rare": 6, "legendary": 2}
    assert all(len(item.name) <= 50 and len(item.description) <= 65 for item in catalogue.AMULET_SPECS)
