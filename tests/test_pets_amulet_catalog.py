"""Contract tests for the amulet catalogue: 40 findable, plus whatever is sold.

The two populations have opposite invariants and are asserted separately on purpose. A
dropped amulet is free and has to be rollable; a shop amulet is bought and must stay OUT
of the loot table, or the one thing anybody can buy would also be eating loot rolls.
"""

from dataclasses import FrozenInstanceError

import pytest

import pets_amulet_catalog as catalogue


def _dropped():
    # Three populations, not two: a vaulted amulet is withdrawn from the game but kept in
    # the catalogue so stored fight snapshots still resolve its code. It belongs to
    # neither the loot table nor the shelves.
    return [item for item in catalogue.AMULET_SPECS
            if item.code not in catalogue.SHOP_AMULET_CODES
            and item.code not in catalogue.VAULT_AMULET_CODES]


def _bought():
    return [item for item in catalogue.AMULET_SPECS
            if item.code in catalogue.SHOP_AMULET_CODES]


def _vaulted():
    return [item for item in catalogue.AMULET_SPECS
            if item.code in catalogue.VAULT_AMULET_CODES]


def test_exactly_forty_unique_drop_only_amulets():
    dropped = _dropped()
    assert len(dropped) == 40
    assert catalogue.AMULET_COUNT == (
        40 + len(catalogue.SHOP_AMULET_CODES) + len(catalogue.VAULT_AMULET_CODES)
    )
    # A vaulted amulet may never leak back into the loot table or onto a shelf.
    assert all(item.source == "vault" and item.drop_weight == 0 for item in _vaulted())
    assert len({item.code for item in catalogue.AMULET_SPECS}) == catalogue.AMULET_COUNT
    assert len({item.name for item in catalogue.AMULET_SPECS}) == catalogue.AMULET_COUNT
    assert all(item.slot == "amulet" for item in catalogue.AMULET_SPECS)
    assert all(item.source == "drop" and item.price == 0 for item in dropped)
    assert all(item.drop_weight > 0 for item in dropped)


def test_a_shop_amulet_is_priced_and_never_rolls_as_loot():
    bought = _bought()
    assert bought, "the catalogue is expected to sell at least one amulet"
    assert all(item.source == "shop" for item in bought)
    assert all(item.price > 0 for item in bought)
    # Weight zero is what keeps it out of every drop table -- a purchasable item that can
    # also drop devalues both the purchase and the roll.
    assert all(item.drop_weight == 0 for item in bought)
    assert all(item.resale_price > 0 for item in bought)


def test_each_amulet_has_one_machine_readable_unique_effect():
    codes = []
    for item in catalogue.AMULET_SPECS:
        effect = item.effect_dict()
        assert effect["code"] in catalogue.EFFECT_HOOKS
        assert isinstance(effect["text"], str) and effect["text"]
        assert isinstance(effect["value"], int)
        codes.append(effect["code"])
    assert len(set(codes)) == catalogue.AMULET_COUNT


def test_raw_records_extend_existing_item_shape_only_with_effect():
    assert len(catalogue.RAW_ITEMS) == catalogue.AMULET_COUNT
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
    dropped_rarities = {rarity: sum(item.rarity == rarity for item in _dropped())
                        for rarity in catalogue.RARITIES}
    assert dropped_rarities == {"common": 12, "uncommon": 12, "rare": 6, "legendary": 10}
    assert all(len(item.name) <= 50 and len(item.description) <= 65
               for item in catalogue.AMULET_SPECS)
