"""Contract tests for the standalone arena weapon catalogue."""

from dataclasses import FrozenInstanceError

import pytest

import pets_weapon_catalog as catalogue


def test_catalogue_has_exactly_514_unique_stable_weapons():
    assert catalogue.WEAPON_COUNT == 514
    assert len(catalogue.WEAPON_SPECS) == 514
    assert len({weapon.code for weapon in catalogue.WEAPON_SPECS}) == 514
    assert len({weapon.name for weapon in catalogue.WEAPON_SPECS}) == 514
    assert catalogue.WEAPON_SPECS[0].code == "w001"
    # w501-w506 are the hand-written build and legendary-only weapons and w507-w514 the
    # cursed legendaries, all appended after the generated run.
    assert catalogue.WEAPON_SPECS[-1].code == "w514"
    assert all(weapon.code.isascii() and weapon.code.isalnum() for weapon in catalogue.WEAPON_SPECS)


def test_catalogue_is_immutable_and_can_adapt_to_existing_item_constructor():
    weapon = catalogue.WEAPON_SPECS[0]
    with pytest.raises(FrozenInstanceError):
        weapon.name = "Ordinary Stick"
    arguments = weapon.item_arguments()
    assert arguments[:5] == (weapon.code, weapon.name, "weapon", weapon.buy_price, weapon.source)
    assert arguments[5] == dict(weapon.bonuses)
    assert arguments[5] is not weapon.bonus_dict()


def test_first_three_ids_preserve_legacy_identity_and_descriptions():
    """Codes, sources and prices are the migration contract; names and stats are not.

    w003 has now been through three identities: «Кость прадеда», then «Компрессор старого
    мастера» when the game was rethemed around painting, then «Швабра на изоленте» when the
    500-weapon catalogue overwrote it. It is now back to the compressor by request, with
    the description recovered from the commit that first wrote it, and +21 strength -- close
    to the +20 it carried for both of its earlier lives.
    """
    assert [(weapon.code, weapon.name, weapon.source, weapon.price, dict(weapon.bonuses), weapon.description)
            for weapon in catalogue.WEAPON_SPECS[:3]] == [
        ("w001", "Мамин тапок", "shop", 60, {"strength": 6},
         "Летит точнее, чем кажется."),
        ("w002", "Мамина сковородка", "shop", 100,
         {"strength": 14, "luck": 4},
         "После неё спор окончен."),
        ("w003", "Старый компрессор", "drop", 0,
         {"strength": 21, "agility": -3},
         "Тяжёлый, гудит и выдаёт идеальное давление."),
    ]


def test_rarity_distribution_has_bad_average_good_and_more_legendary_items():
    assert catalogue.RARITY_COUNTS == {
        "cursed": 75, "common": 250, "uncommon": 120, "rare": 47, "legendary": 22,
    }
    cursed = [weapon for weapon in catalogue.WEAPON_SPECS if weapon.rarity == "cursed"]
    rares = [weapon for weapon in catalogue.WEAPON_SPECS if weapon.rarity == "rare"]
    legendary = [weapon for weapon in catalogue.WEAPON_SPECS if weapon.rarity == "legendary"]
    assert all(any(value < 0 for _, value in weapon.bonuses) for weapon in cursed)
    assert all(max(value for _, value in weapon.bonuses) >= 20 for weapon in rares)
    assert len(legendary) == 22
    assert all(any(value < 0 for _, value in weapon.bonuses) for weapon in legendary)

    # w003 is a deliberate, requested exception to the two rules below: at +21 strength it
    # is the antique of the tier, weaker on paper than the best rare (+24) and out-scored
    # by several rares. It keeps its legendary passive and its 220-coin salvage. The
    # rules still bind every other legendary, including the five ascended rare designs.
    generated = [weapon for weapon in legendary if weapon.code != "w003"]
    assert len(generated) == 21
    assert all(dict(weapon.bonuses)["strength"] >= 28 for weapon in generated)
    assert min(dict(weapon.bonuses)["strength"] for weapon in generated) > max(
        dict(weapon.bonuses)["strength"] for weapon in rares
    )


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
    assert max(ordinary_shop_strengths) <= 24
    # The cursed legendaries carry the only +33..35 strength lines in the catalogue. They
    # are allowed past the ordinary legendary ceiling because every one of them also
    # carries a passive that can lose the fight outright -- see the cursed-shelf test.
    ordinary = [
        weapon for weapon in catalogue.WEAPON_SPECS
        if weapon.code not in catalogue.CURSED_LEGENDARY_CODES
    ]
    assert max(dict(weapon.bonuses).get("strength", 0) for weapon in ordinary) == 32
    assert max(dict(weapon.bonuses).get("strength", 0) for weapon in catalogue.WEAPON_SPECS) == 35


def test_shop_prices_match_fight_income_and_actual_combat_power():
    expected_bands = {
        "common": (60, 75),
        "uncommon": (85, 105),
        "rare": (160, 195),
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


def test_raw_items_expose_trade_schema_and_expanded_legendary_drop_weight():
    required = {
        "code", "name", "slot", "price", "source", "bonuses", "description", "rarity",
        "resale_price", "drop_weight", "effect",
    }
    assert len(catalogue.RAW_ITEMS) == 514
    assert all(required == set(record) for record in catalogue.RAW_ITEMS)
    drop_records = [record for record in catalogue.RAW_ITEMS if record["source"] == "drop"]
    legendary_weight = sum(record["drop_weight"] for record in drop_records if record["rarity"] == "legendary")
    total_weight = sum(record["drop_weight"] for record in drop_records)
    # Ten new legendaries -- two legendary-only rules and the eight cursed ones -- nearly
    # double the tier's share of a weapon drop, from 2.4% to 4.3%. That is deliberate: a
    # shelf nobody can find is not a shelf, and eight of the twenty-two now cost the
    # holder something real rather than being pure upside.
    assert legendary_weight / total_weight == pytest.approx(22 / 517)
    cursed = [record for record in catalogue.RAW_ITEMS if record["rarity"] == "cursed"]
    assert cursed and all(record["source"] == "drop" for record in cursed)


def test_every_legendary_and_twenty_remaining_rares_carry_a_passive_effect():
    import pets_amulet_catalog

    by_rarity = {rarity: [] for rarity in catalogue.RARITIES}
    for weapon in catalogue.WEAPON_SPECS:
        by_rarity[weapon.rarity].append(weapon)

    assert all(weapon.effect for weapon in by_rarity["legendary"])
    rare_with_effect = [weapon for weapon in by_rarity["rare"] if weapon.effect]
    assert len(rare_with_effect) == 22
    # Nothing below rare gets one, so a passive stays a mark of a real find.
    assert not any(
        weapon.effect
        for rarity in ("cursed", "common", "uncommon")
        for weapon in by_rarity[rarity]
    )

    # Weapons reuse the amulet engine's vocabulary rather than inventing a second one.
    for weapon in by_rarity["legendary"] + rare_with_effect:
        effect = weapon.effect_dict()
        assert effect["code"] in pets_amulet_catalog.EFFECT_HOOKS
        assert isinstance(effect["value"], int)
        assert effect["text"] and effect["text"].endswith(".")


def test_weapon_passives_reach_the_item_record_combat_reads():
    import pets_config

    compressor = pets_config.find_item("w003")
    assert compressor.rarity == "legendary"
    assert compressor.effect["code"] == "pressure"
    # The point is that the optional per-effect params survive the trip into the item
    # record combat reads, not what the balance pass currently tunes them to. The cursed
    # shelf carries the most of them, so it is the honest place to check the round trip.
    declared = dict(next(
        row[4] for row in catalogue._CURSED_LEGENDARIES if row[0] == "w507"
    ))
    hammer = pets_config.find_item("w507")
    assert hammer.effect["code"] == "charge_crit"
    assert hammer.effect["turns"] == declared["turns"]
    assert hammer.effect["taken"] == declared["taken"]


def test_rare_modifiers_are_varied_and_repeated_at_distinct_strengths():
    modified = [
        weapon for weapon in catalogue.WEAPON_SPECS
        if weapon.rarity == "rare" and weapon.effect
    ]
    by_code = {}
    for weapon in modified:
        effect = weapon.effect_dict()
        by_code.setdefault(effect["code"], []).append(effect)

    assert len(modified) == 22
    assert len(by_code) == 18
    for code in ("precision", "burn", "armor_shred", "wound"):
        assert len(by_code[code]) == 2
        assert by_code[code][0] != by_code[code][1]
    # The stronger form of these three lines moved into the legendary tier.
    for code in ("venom_blade", "bleed", "shield_breaker"):
        assert len(by_code[code]) == 1
        legendary = next(
            weapon.effect_dict() for weapon in catalogue.WEAPON_SPECS
            if weapon.rarity == "legendary" and weapon.effect_dict()["code"] == code
        )
        assert legendary != by_code[code][0]
    # These two ascended into a legendary rule of their OWN rather than a bigger number:
    # `coin_rake` became `tax` (which also bites in combat, where the old legendary did
    # literally nothing at all) and `heavy_combo` became `double_strike`.
    for code in ("coin_rake", "heavy_combo"):
        assert len(by_code[code]) == 1
        assert not any(
            weapon.effect_dict()["code"] == code
            for weapon in catalogue.WEAPON_SPECS if weapon.rarity == "legendary"
        )


def test_names_are_clear_and_descriptions_are_short():
    names = [weapon.name for weapon in catalogue.WEAPON_SPECS]
    descriptions = [weapon.description for weapon in catalogue.WEAPON_SPECS]
    assert len({name.split()[0] for name in names}) >= 40
    assert all("»:" not in name and "«" not in name for name in names)
    assert all(len(name) <= 50 for name in names)
    assert all(len(description) <= 65 for description in descriptions)
    assert any(name.startswith("Тапок ") for name in names)
    assert any(name.startswith("Сковородка ") for name in names)
    assert any("для важных переговоров" in name.lower() for name in names)
    assert any("после странной доставки" in name.lower() for name in names)
    assert not any(any(bad in name.lower() for bad in ("дедлайн", "созвон", "проверки")) for name in names)
    assert not any(any(tired in name.lower() for tired in (
        "без чека", "от соседа", "с авито", "из гаража",
    )) for name in names)
    assert len(set(descriptions)) == len(descriptions)


def test_no_generic_name_story_is_repeated_more_than_ten_times():
    counts = {
        suffix: sum(weapon.name.endswith(suffix) for weapon in catalogue.WEAPON_SPECS)
        for suffix, _description in catalogue._THEMES
    }
    assert len(catalogue._THEMES) == 50
    assert max(counts.values()) <= 10


def test_generated_rarities_are_interleaved_instead_of_front_loaded():
    first_fifty = [weapon.rarity for weapon in catalogue.WEAPON_SPECS[3:53]]
    assert len(set(first_fifty)) >= 4
    assert first_fifty.count("cursed") < 20


def test_generated_names_are_plain_readable_noun_phrases():
    # The build weapons are hand-written, like the first three, so the theme-suffix rule
    # that governs the generated run does not apply to them -- they get their own check
    # below rather than being quietly exempted from all of them.
    hand_written = {row[0] for row in catalogue._NEW_BUILD_WEAPONS}
    hand_written |= catalogue.CURSED_LEGENDARY_CODES
    generated_names = [
        weapon.name for weapon in catalogue.WEAPON_SPECS[3:]
        if weapon.code not in hand_written
    ]
    assert all(not name.startswith("«") and ":" not in name for name in generated_names)
    legendary_names = {name for name, _ in catalogue._LEGENDARY_COPY}
    legendary_names |= {data[0] for data in catalogue._ASCENDED_LEGENDARIES.values()}
    special_names = {name for name, _ in catalogue._RARE_SPECIAL_COPY}
    assert all(
        name in legendary_names or name in special_names
        or any(name.endswith(suffix) for suffix, _ in catalogue._THEMES)
        for name in generated_names
    )


def test_hand_written_build_weapons_read_like_the_rest_of_the_catalogue():
    hand_written = {row[0] for row in catalogue._NEW_BUILD_WEAPONS}
    weapons = [w for w in catalogue.WEAPON_SPECS if w.code in hand_written]
    assert len(weapons) == 6
    for weapon in weapons:
        assert not weapon.name.startswith("«") and ":" not in weapon.name
        assert weapon.source == "drop" and weapon.buy_price == 0
        assert weapon.effect, weapon.code
    pairs = {}
    for weapon in weapons:
        pairs.setdefault(weapon.effect_dict()["code"], []).append(weapon)
    # Two builds, each a rare and the same effect again at legendary strength, plus two
    # legendary-only rules that no lower tier carries at all.
    assert set(pairs) == {"lucky", "vampiric", "shatter", "reap"}
    for code, pair in pairs.items():
        if len(pair) == 1:
            assert pair[0].rarity == "legendary", code
            continue
        rare, legendary = sorted(pair, key=lambda w: w.rarity == "legendary")
        assert rare.rarity == "rare" and legendary.rarity == "legendary"
        assert legendary.effect_dict()["value"] > rare.effect_dict()["value"], code


def test_more_than_half_the_legendary_tier_is_a_rule_no_lower_tier_has():
    """
    Six of the twelve legendary weapons used to be the bottom of their own tier.

    Every one of them carried a passive a rare weapon already had with a slightly larger
    number on it -- `precision` 90 against `precision` 70, `heavy_combo` 45 against 40 --
    and the balance harness measured them at +19.8 to +23.0 win points against rares
    scoring +15.7. One, the coin rake, had no combat effect whatsoever. Fourteen passives
    are now legendary-only, which is what this pins: not that the tier is strong, but that
    most of it is a RULE nothing below it has rather than a bigger number.
    """
    by_rarity = {}
    for weapon in catalogue.WEAPON_SPECS:
        if weapon.effect:
            by_rarity.setdefault(weapon.rarity, set()).add(weapon.effect_dict()["code"])
    legendary_only = by_rarity["legendary"] - by_rarity["rare"]
    assert legendary_only == {
        "pressure", "chain_crit", "double_strike", "tax", "shatter", "reap",
        "charge_crit", "wild_swing", "blind_fury", "glass_body", "blood_price",
        "hunger", "soul_debt", "recoil",
    }
    assert len(legendary_only) > len(by_rarity["legendary"] & by_rarity["rare"])


def test_cursed_legendaries_pair_an_oversized_effect_with_a_real_penalty():
    """
    The cursed shelf is defined by its cost, so the cost is what gets pinned here.

    They stay `legendary` rather than becoming a sixth rarity on purpose: a new rarity
    would have to be taught to the drop tables, the forge, every badge table and both
    front ends, and would say nothing the name and the effect text do not already say.
    """
    cursed = [
        weapon for weapon in catalogue.WEAPON_SPECS
        if weapon.code in catalogue.CURSED_LEGENDARY_CODES
    ]
    assert len(cursed) == 8
    for weapon in cursed:
        assert weapon.rarity == "legendary"
        assert weapon.source == "drop" and weapon.buy_price == 0
        # A real stat cost on the item card...
        assert any(value < 0 for _, value in weapon.bonuses), weapon.code
        # ...and a passive that exists nowhere else in the catalogue.
        assert weapon.effect, weapon.code
        code = weapon.effect_dict()["code"]
        assert sum(
            other.effect_dict().get("code") == code
            for other in catalogue.WEAPON_SPECS if other.effect
        ) == 1, code
