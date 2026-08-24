"""Contract tests for the standalone arena weapon catalogue."""

from dataclasses import FrozenInstanceError

import pytest

import pets_weapon_catalog as catalogue


def test_catalogue_has_exactly_631_unique_stable_weapons():
    assert catalogue.WEAPON_COUNT == 631
    assert len(catalogue.WEAPON_SPECS) == 631
    assert len({weapon.code for weapon in catalogue.WEAPON_SPECS}) == 631
    assert len({weapon.name for weapon in catalogue.WEAPON_SPECS}) == 631
    assert catalogue.WEAPON_SPECS[0].code == "w001"
    # w501-w506 are the hand-written build and legendary-only weapons, w507-w514 the
    # cursed legendaries, w515-w526 the rare cursed rung between the junk curses and
    # them, and w527-w631 the magic shelf -- all appended after the generated run.
    assert catalogue.WEAPON_SPECS[-1].code == "w631"
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
    # The magic shelf added exactly 21 of every rarity on top of the steel catalogue.
    assert catalogue.RARITY_COUNTS == {
        "cursed": 96, "common": 271, "uncommon": 141, "rare": 80, "legendary": 43,
    }
    # Scoped to the steel shelf: the magic weapons are banded in Магия rather than
    # Strength, so a Strength floor read across both would be measuring the wrong stat.
    steel = [
        weapon for weapon in catalogue.WEAPON_SPECS
        if weapon.code not in catalogue.MAGIC_WEAPON_CODES
    ]
    cursed = [weapon for weapon in steel if weapon.rarity == "cursed"]
    rares = [weapon for weapon in steel if weapon.rarity == "rare"]
    legendary = [weapon for weapon in steel if weapon.rarity == "legendary"]
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


def test_the_magic_shelf_is_twenty_one_per_rarity_and_never_scales_from_strength():
    """The commission, pinned: 21 magic weapons in every rarity, five of them hybrids.

    A magic weapon's whole point is that `derive` reads Магия instead of Strength for the
    ordinary swing, so the two things that must never drift are the scaling and the stat
    that backs it -- a "magic" weapon granting no Магия would make equipping it a strict
    downgrade for the build it is named after.
    """
    shelf = [
        weapon for weapon in catalogue.WEAPON_SPECS
        if weapon.code in catalogue.MAGIC_WEAPON_CODES
    ]
    assert len(shelf) == 105
    assert all(weapon.scaling in ("magic", "hybrid") for weapon in shelf)
    assert all(
        weapon.scaling == "strength" for weapon in catalogue.WEAPON_SPECS
        if weapon.code not in catalogue.MAGIC_WEAPON_CODES
    )
    for rarity in catalogue.RARITIES:
        tier = [weapon for weapon in shelf if weapon.rarity == rarity]
        assert len(tier) == 21, rarity
        assert sum(weapon.scaling == "hybrid" for weapon in tier) == 5, rarity
    assert all(dict(weapon.bonuses).get("magic") for weapon in shelf)
    assert all(
        dict(weapon.bonuses).get("strength") for weapon in shelf
        if weapon.scaling == "hybrid"
    )
    # The bands rise the way the steel ones do, read in Магия.
    floors = {"common": 4, "uncommon": 8, "rare": 15, "legendary": 21}
    for rarity, floor in floors.items():
        tier = [weapon for weapon in shelf if weapon.rarity == rarity]
        assert min(dict(weapon.bonuses)["magic"] for weapon in tier) >= floor, rarity
    # Every rare and legendary carries a rule; nothing below them does, exactly as on the
    # steel shelf. A passive stays the mark of a real find on both.
    assert all(
        weapon.effect for weapon in shelf if weapon.rarity in ("rare", "legendary")
    )
    assert not any(
        weapon.effect for weapon in shelf
        if weapon.rarity in ("cursed", "common", "uncommon")
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
    # Read on whichever stat the weapon actually swings with, so the magic shelf's shop
    # rows are held to the same band rather than counted as zero-strength items.
    ordinary_shop_strengths = [
        max(dict(weapon.bonuses).get("strength", 0), dict(weapon.bonuses).get("magic", 0))
        for weapon in catalogue.WEAPON_SPECS
        if weapon.rarity in {"common", "uncommon", "rare"} and weapon.source == "shop"
    ]
    assert min(ordinary_shop_strengths) >= 5
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
    # The magic shelf tops out at the same ordinary legendary ceiling, in its own stat.
    assert max(dict(weapon.bonuses).get("magic", 0) for weapon in catalogue.WEAPON_SPECS) == 32


def test_shop_prices_match_fight_income_and_actual_combat_power():
    # The uncommon and rare ceilings rose with the magic shelf: a hybrid carries two stat
    # lines instead of one, and shop_price_for_bonuses charges for exactly that.
    expected_bands = {
        "common": (60, 75),
        "uncommon": (85, 125),
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
        # Weapons carry one field the other catalogues do not: whether the item belongs to
        # the cursed ladder. See pets_config.Item -- it is a property, not a rarity.
        "cursed",
        # ...and one more since the magic shelf: which stat the swing reads.
        "scaling",
    }
    assert len(catalogue.RAW_ITEMS) == 631
    assert all(required == set(record) for record in catalogue.RAW_ITEMS)
    drop_records = [record for record in catalogue.RAW_ITEMS if record["source"] == "drop"]
    legendary_weight = sum(record["drop_weight"] for record in drop_records if record["rarity"] == "legendary")
    total_weight = sum(record["drop_weight"] for record in drop_records)
    # 22 steel legendaries plus the magic shelf's 21, in a drop pool the same shelf also
    # widened. Its rares are weighted 4 rather than the steel shelf's 10 precisely to hold
    # this ratio down: without that, 105 new weapons would have moved the legendary share
    # of a weapon drop from 4.1% to well past 6% as a side effect of adding a shelf.
    assert legendary_weight / total_weight == pytest.approx(43 / 647)
    # The rare cursed rung drops at a fifth of an ordinary rare's weight. It is findable,
    # but the forge is the reliable route to it -- see _drop_weight.
    cursed_rare_weight = sum(
        record["drop_weight"] for record in drop_records
        if record["rarity"] == "rare" and record["cursed"]
    )
    assert cursed_rare_weight == 24
    cursed = [record for record in catalogue.RAW_ITEMS if record["rarity"] == "cursed"]
    assert cursed and all(record["source"] == "drop" for record in cursed)


def test_every_legendary_and_twenty_remaining_rares_carry_a_passive_effect():
    import pets_amulet_catalog

    by_rarity = {rarity: [] for rarity in catalogue.RARITIES}
    for weapon in catalogue.WEAPON_SPECS:
        by_rarity[weapon.rarity].append(weapon)

    assert all(weapon.effect for weapon in by_rarity["legendary"])
    rare_with_effect = [weapon for weapon in by_rarity["rare"] if weapon.effect]
    # Twenty-two ordinary rares, the twelve rare CURSED weapons -- every one of which
    # carries a passive by definition, since a curse with no rule is only worse stats --
    # and the magic shelf's twenty-one, where a passive is what the whole tier is for.
    assert len(rare_with_effect) == 55
    assert sum(1 for weapon in rare_with_effect if weapon.cursed) == 12
    assert sum(
        1 for weapon in rare_with_effect
        if weapon.code in catalogue.MAGIC_WEAPON_CODES
    ) == 21
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
    # The steel shelf only. The magic rares deliberately re-use several of these codes at
    # their own values -- that is the point of a second shelf, and counting it here would
    # report every shared rule as a duplicate.
    modified = [
        weapon for weapon in catalogue.WEAPON_SPECS
        if weapon.rarity == "rare" and weapon.effect
        and weapon.code not in catalogue.MAGIC_WEAPON_CODES
    ]
    by_code = {}
    for weapon in modified:
        effect = weapon.effect_dict()
        by_code.setdefault(effect["code"], []).append(effect)

    assert len(modified) == 34
    assert len(by_code) == 30
    for code in ("precision", "burn", "armor_shred", "wound"):
        assert len(by_code[code]) == 2
        assert by_code[code][0] != by_code[code][1]
    # The stronger form of these three lines moved into the legendary tier.
    for code in ("venom_blade", "bleed", "shield_breaker"):
        assert len(by_code[code]) == 1
        legendary = next(
            weapon.effect_dict() for weapon in catalogue.WEAPON_SPECS
            if weapon.rarity == "legendary" and weapon.effect_dict()["code"] == code
            and weapon.code not in catalogue.MAGIC_WEAPON_CODES
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


def test_every_magic_rule_gets_bigger_between_the_rare_and_legendary_shelf():
    """The rare rung teaches a rule cheaply, the legendary rung charges full price.

    Twelve of the magic passives are new vocabulary that exists nowhere else in the
    catalogue; each appears once on the rare shelf and once, larger, on the legendary one.
    """
    def strengths(rarity):
        return {
            weapon.effect_dict()["code"]: weapon.effect_dict()["value"]
            for weapon in catalogue.WEAPON_SPECS
            if weapon.code in catalogue.MAGIC_WEAPON_CODES and weapon.rarity == rarity
        }

    rare, legendary = strengths("rare"), strengths("legendary")
    assert len(rare) == 21 and len(legendary) == 21
    magic_only = {
        "arcane_surge", "arcane_battery", "runic_charge", "mana_burn", "spell_siphon",
        "spell_pierce", "spell_thorns", "spell_shield", "ward", "hex", "focus_shift",
        "double_cast",
    }
    assert magic_only <= set(rare) and magic_only <= set(legendary)
    # Nothing outside the magic shelf may carry one of them: they read spell power and a
    # scroll loadout, so on a steel weapon they would be a rule with nothing to act on.
    assert all(
        weapon.code in catalogue.MAGIC_WEAPON_CODES
        for weapon in catalogue.WEAPON_SPECS
        if weapon.effect and weapon.effect_dict()["code"] in magic_only
    )
    for code in sorted(set(rare) & set(legendary)):
        assert rare[code] < legendary[code], code


def test_names_are_clear_and_descriptions_are_short():
    names = [weapon.name for weapon in catalogue.WEAPON_SPECS]
    assert all(len(name) <= 50 for name in names)
    descriptions = [weapon.description for weapon in catalogue.WEAPON_SPECS]
    assert len({name.split()[0] for name in names}) >= 40
    assert all("»:" not in name and "«" not in name for name in names
               if name not in {
                   weapon.name for weapon in catalogue.WEAPON_SPECS
                   if weapon.code in catalogue.MAGIC_WEAPON_CODES
               })
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
    hand_written |= catalogue.CURSED_LEGENDARY_CODES | catalogue.RARE_CURSED_CODES
    # The magic shelf is hand-written too -- its names are objects a wizard would own
    # rather than the household-object-plus-theme-suffix pattern the generated run uses.
    hand_written |= catalogue.MAGIC_WEAPON_CODES
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
    # Steel shelf only, on both sides. The magic shelf is a matched rare/legendary pair
    # of every rule it owns by design, so folding it in here would report a deliberate
    # ladder as a legendary tier with nothing of its own.
    ordinary_rare = {
        weapon.effect_dict()["code"] for weapon in catalogue.WEAPON_SPECS
        if weapon.effect and weapon.rarity == "rare" and not weapon.cursed
        and weapon.code not in catalogue.MAGIC_WEAPON_CODES
    }
    legendary = {
        weapon.effect_dict()["code"] for weapon in catalogue.WEAPON_SPECS
        if weapon.effect and weapon.rarity == "legendary"
        and weapon.code not in catalogue.MAGIC_WEAPON_CODES
    }
    # Measured against the ORDINARY rare shelf on purpose. The rare CURSED rung shares
    # eight of these deliberately -- that sharing is the cursed ladder, and counting it
    # here would report the ladder as power creep.
    legendary_only = legendary - ordinary_rare
    assert legendary_only == {
        "pressure", "chain_crit", "double_strike", "tax", "shatter", "reap",
        "charge_crit", "wild_swing", "blind_fury", "glass_body", "blood_price",
        "hunger", "soul_debt", "recoil",
    }
    assert len(legendary_only) > len(legendary & ordinary_rare)


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
        assert weapon.rarity == "legendary" and weapon.cursed
        assert weapon.source == "drop" and weapon.buy_price == 0
        # A real stat cost on the item card...
        assert any(value < 0 for _, value in weapon.bonuses), weapon.code
        # ...and a passive that only ever appears on the cursed ladder. It is no longer
        # unique in the catalogue -- each of these now has a rare cursed parent, which is
        # what makes the ladder a ladder -- but nothing OUTSIDE the line may carry it.
        assert weapon.effect, weapon.code
        code = weapon.effect_dict()["code"]
        assert all(
            other.cursed for other in catalogue.WEAPON_SPECS
            if other.effect and other.effect_dict().get("code") == code
        ), code
    # And the magic shelf's junk rung carries no rule at all, so it cannot smuggle one of
    # these out of the line by being cursed and magic at the same time.
    assert not any(
        weapon.effect for weapon in catalogue.WEAPON_SPECS
        if weapon.code in catalogue.MAGIC_WEAPON_CODES and weapon.rarity == "cursed"
    )


def test_the_cursed_ladder_has_three_rungs_and_the_forge_can_climb_it():
    """
    Cursed used to be a dead end: seventy-five junk weapons whose only purpose was to be
    melted into an ORDINARY rare, and eight legendary curses reachable only from the
    ordinary ladder. One word named both the worst items in the game and some of the best,
    with nothing in between. The middle rung is what makes it a line a player climbs.
    """
    line = [weapon for weapon in catalogue.WEAPON_SPECS if weapon.cursed]
    by_rarity = {}
    for weapon in line:
        by_rarity.setdefault(weapon.rarity, []).append(weapon)
    assert sorted(by_rarity) == ["cursed", "legendary", "rare"]
    # 75 steel junk curses plus the magic shelf's 21, which join the ladder rather than
    # sitting beside it: same worthless drop, same six-into-one forge recipe.
    assert len(by_rarity["cursed"]) == 96
    assert len(by_rarity["rare"]) == 12
    assert len(by_rarity["legendary"]) == 8
    # Every rung above the junk one carries a rule, and every one of them is a weapon:
    # the shelf is deliberately weapons-only, so no other slot needs a cursed pool.
    for weapon in by_rarity["rare"] + by_rarity["legendary"]:
        assert weapon.slot == "weapon" and weapon.effect, weapon.code
        assert any(value < 0 for _, value in weapon.bonuses), weapon.code
    # Nothing outside the line is cursed, and the flag never contradicts the rarity.
    assert all(
        weapon.cursed for weapon in catalogue.WEAPON_SPECS if weapon.rarity == "cursed"
    )


def test_each_rare_curse_is_the_smaller_version_of_a_legendary_one_or_its_own_idea():
    """
    The rare rung teaches a rule cheaply before the legendary rung charges full price for
    it -- the same rare/legendary pairing the ordinary catalogue uses everywhere. Where a
    pair exists the rare half must actually be the WEAKER one, or the ladder runs backwards.
    """
    def strengths(codes):
        return {
            weapon.effect_dict()["code"]: weapon.effect_dict()["value"]
            for weapon in catalogue.WEAPON_SPECS if weapon.code in codes
        }

    rare = strengths(catalogue.RARE_CURSED_CODES)
    legendary = strengths(catalogue.CURSED_LEGENDARY_CODES)
    shared = set(rare) & set(legendary)
    assert len(shared) == 8, "every legendary curse should have a rare parent"
    for code in sorted(shared):
        assert rare[code] < legendary[code], code
