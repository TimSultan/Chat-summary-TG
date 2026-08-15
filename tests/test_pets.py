import json
import random
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs

import app_time
import economy
import pets
import pets_combat
import pets_config
import pets_ui
import pets_weapon_catalog
import stats


# Shop items priced on utility rather than on stat bonuses. Enumerated here on purpose:
# the pricing test below waives the power formula only for these exact codes, so a future
# hand-added three-figure accessory still has to justify itself against the formula.
UTILITY_SHOP_CODES = frozenset({
    "amulet_leech_fang", "amulet_armor_capsule", "amulet_initiative_pendulum",
    "amulet_first_aid_heart", "amulet_crit_catcher", "amulet_trophy_compass",
    "amulet_soul_mirror", "amulet_mob_ward",
})


class PetsTestCase(unittest.TestCase):
    """Base fixture: point stats._stats_dir (and therefore both economy's and pets'
    storage) at a throwaway directory, the same way tests/test_economy.py does."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def _tame(self, entry, uid, name=None):
        """Fund and walk one member all the way to a named pet."""
        name = name or f"Питомец{uid}"
        economy.grant(entry, uid, pets_config.TAME_PRICE, "test")
        ok, msg = pets.buy_cage(entry, uid, 0)
        self.assertTrue(ok, msg)
        ok, msg = pets.tame(entry, uid, 0, name, f"file{uid}", f"Owner{uid}")
        self.assertTrue(ok, msg)


class HeroGoldBalanceTests(unittest.TestCase):
    def test_first_five_levels_are_unchanged_then_every_source_grows_forever(self):
        for source in pets_config.HERO_GOLD_SOURCE_WEIGHTS:
            self.assertEqual(pets_config.gold_for_hero(100, 1, source), 100)
            self.assertEqual(pets_config.gold_for_hero(100, 5, source), 100)
            payouts = [
                pets_config.gold_for_hero(100, level, source)
                for level in (5, 10, 25, 50, 100, 200)
            ]
            self.assertEqual(payouts, sorted(payouts), source)
            self.assertGreater(payouts[-1], payouts[0], source)

    def test_source_weights_avoid_double_scaling_existing_progression(self):
        level = 100
        arena = pets_config.hero_gold_multiplier(level, "arena")
        self.assertGreater(arena, pets_config.hero_gold_multiplier(level, "quest"))
        self.assertGreater(
            pets_config.hero_gold_multiplier(level, "quest"),
            pets_config.hero_gold_multiplier(level, "dungeon"),
        )


class CageAndTamingTests(PetsTestCase):
    def test_everyone_starts_with_a_free_base_cage(self):
        entry = "chat"
        self.assertTrue(pets.has_cage(entry, "1"))
        self.assertEqual(pets.cage_level(entry, "1"), 1)

        ok, msg = pets.buy_cage(entry, "1", 0)  # compatibility for a stale button
        self.assertTrue(ok, msg)
        self.assertEqual(economy.balance(entry, "1", 0), 0)
        self.assertEqual(pets.cage_level(entry, "1"), 1)

    def test_tame_allocates_the_free_cage_without_charging(self):
        entry = "chat"
        economy.grant(entry, "1", 123, "test")
        ok, msg = pets.tame(entry, "1", 0, "Рекс", "file123", "Owner")
        self.assertTrue(ok, msg)
        self.assertEqual(economy.balance(entry, "1", 0), 123)
        self.assertIsNotNone(pets.get_pet(entry, "1"))
        self.assertIn("боях против других игроков", msg)

    def test_legacy_cages_are_refunded_once(self):
        entry = "chat"
        economy.grant(entry, "1", pets_config.LEGACY_CAGE_PRICE, "legacy_cage_funds")
        ok, _ = economy.spend(entry, "1", 0, pets_config.LEGACY_CAGE_PRICE, "buy:pet_cage")
        self.assertTrue(ok)
        data = pets._empty()
        data["pets"]["1"] = pets._new_record()
        data["pets"]["1"].pop("cage_price_paid")
        pets._save(entry, data)

        self.assertEqual(pets.refund_legacy_cages([entry]), 1)
        self.assertEqual(pets.refund_legacy_cages([entry]), 0)
        self.assertEqual(economy.balance(entry, "1", 0), pets_config.LEGACY_CAGE_PRICE)
        self.assertEqual(pets._load(entry)["pets"]["1"]["cage_price_paid"], pets_config.LEGACY_CAGE_PRICE)

    def test_cage_upgrades_are_refunded_once(self):
        entry = "chat"
        self._tame(entry, "1")
        economy.grant(entry, "1", pets_config.CAGE_UPGRADE_COSTS[1], "upgrade_funds")
        ok, msg = pets.upgrade_cage(entry, "1", 0)
        self.assertTrue(ok, msg)
        # A second owner who only ever bought the cage is not part of this refund.
        self._tame(entry, "2")

        self.assertEqual(pets.refund_cage_upgrades([entry]), 1)
        self.assertEqual(pets.refund_cage_upgrades([entry]), 0)
        # Paid in full: a `spent`-side refund would have been clamped to what this owner
        # had actually spent, which is less than the flat payout.
        self.assertEqual(economy.balance(entry, "1", 0), pets_config.CAGE_UPGRADE_REFUND)
        self.assertEqual(economy.balance(entry, "2", 0), 0)

    def test_cage_upgrades_bought_after_the_refund_are_not_paid_out(self):
        """The window closes at the first run -- otherwise, with upgrades at 100 and the
        payout at 350, upgrading after the migration would print coins."""
        entry = "chat"
        self._tame(entry, "1")
        self.assertEqual(pets.refund_cage_upgrades([entry]), 0)

        economy.grant(entry, "1", pets_config.CAGE_UPGRADE_COSTS[1], "upgrade_funds")
        ok, msg = pets.upgrade_cage(entry, "1", 0)
        self.assertTrue(ok, msg)

        self.assertEqual(pets.refund_cage_upgrades([entry]), 0)
        self.assertEqual(economy.balance(entry, "1", 0), 0)

    def test_farm_builders_are_refunded_the_gap_exactly_once(self):
        entry = "chat"
        self._tame(entry, "1")
        economy.grant(entry, "1", pets_config.FARM_UPGRADE_COSTS[0], "build_funds")
        self.assertTrue(pets.upgrade_farm(entry, "1", 0)[0])
        # Somebody who never built a farm is not part of this refund.
        self._tame(entry, "2")

        self.assertEqual(pets.refund_farm_builds([entry]), 1)
        self.assertEqual(pets.refund_farm_builds([entry]), 0)
        self.assertEqual(economy.balance(entry, "1", 0), pets_config.FARM_BUILD_REFUND)
        self.assertEqual(economy.balance(entry, "2", 0), 0)

    def test_farms_built_after_the_refund_do_not_print_coins(self):
        """The window has to close at the first run: the payout (65) is larger than the
        new build price (10), so a still-open window would make building profitable."""
        entry = "chat"
        self._tame(entry, "1")
        self.assertEqual(pets.refund_farm_builds([entry]), 0)

        economy.grant(entry, "1", pets_config.FARM_UPGRADE_COSTS[0], "build_funds")
        self.assertTrue(pets.upgrade_farm(entry, "1", 0)[0])

        self.assertEqual(pets.refund_farm_builds([entry]), 0)
        self.assertEqual(economy.balance(entry, "1", 0), 0)
        self.assertGreater(pets_config.FARM_BUILD_REFUND, pets_config.FARM_UPGRADE_COSTS[0])

    def test_weaponless_pets_each_get_a_free_common_weapon(self):
        entry = "chat"
        for uid in ("1", "2", "3"):
            self._tame(entry, uid)
        # This one already has a weapon and must be left alone.
        armed = next(iter(pets_config.items_for_slot("weapon", "shop")))
        pets._load(entry)
        data = pets._load(entry)
        data["pets"]["3"]["inventory"].append(armed.code)
        pets._save(entry, data)

        self.assertEqual(pets.grant_starter_weapons([entry]), 2)

        for uid in ("1", "2"):
            pet = pets.get_pet(entry, uid)
            weapons = [
                item for code in pet["inventory"]
                if (item := pets_config.find_item(code)) is not None and item.slot == "weapon"
            ]
            self.assertEqual(len(weapons), 1, uid)
            self.assertEqual(weapons[0].rarity, "common")
            # Nothing was in the slot, so the gift is worn rather than left in the bag.
            self.assertEqual(pet["equipped"]["weapon"], weapons[0].code)
            self.assertIn(weapons[0].code, pet["discovered"])

        self.assertEqual(pets.get_pet(entry, "3")["inventory"], [armed.code])

    def test_selling_the_gift_later_does_not_earn_a_second_one(self):
        """The per-chat flag is the only thing standing between "owns no weapon" and an
        infinite weapon faucet, since a player can re-enter that state at will."""
        entry = "chat"
        self._tame(entry, "1")
        self.assertEqual(pets.grant_starter_weapons([entry]), 1)

        gift = pets.get_pet(entry, "1")["equipped"]["weapon"]
        self.assertTrue(pets.unequip(entry, "1", "weapon")[0])
        self.assertTrue(pets.sell_item(entry, "1", gift)[0])
        self.assertEqual(pets.grant_starter_weapons([entry]), 0)
        self.assertEqual(pets.get_pet(entry, "1")["inventory"], [])

    def test_duplicate_name_refuses_case_insensitively(self):
        entry = "chat"
        for uid in ("1", "2"):
            economy.grant(entry, uid, pets_config.TAME_PRICE, "test")
            ok, msg = pets.buy_cage(entry, uid, 0)
            self.assertTrue(ok, msg)

        ok, msg = pets.tame(entry, "1", 0, "Рекс", "f1", "Owner1")
        self.assertTrue(ok, msg)

        ok, msg = pets.tame(entry, "2", 0, "рекс", "f2", "Owner2")
        self.assertFalse(ok)
        self.assertIsNone(pets.get_pet(entry, "2"))
        # The would-be tamer's coins were never touched by a refused purchase.
        self.assertEqual(economy.balance(entry, "2", 0), pets_config.TAME_PRICE)

    def test_validate_name_rules(self):
        self.assertEqual(pets.validate_name("  Рекс   Пёс  "), "Рекс Пёс")
        self.assertEqual(pets.validate_name("a\nb\tc"), "a b c")
        with self.assertRaises(ValueError):
            pets.validate_name("   ")
        with self.assertRaises(ValueError):
            pets.validate_name("a" * 25)
        with self.assertRaises(ValueError):
            pets.validate_name("<script>")

    def test_rename_and_set_photo_require_a_tamed_pet(self):
        entry = "chat"
        ok, msg = pets.rename(entry, "1", "Новое имя")
        self.assertFalse(ok)
        ok, msg = pets.set_photo(entry, "1", "file9")
        self.assertFalse(ok)

        self._tame(entry, "1", "Старое имя")
        ok, msg = pets.rename(entry, "1", "Новое имя")
        self.assertTrue(ok, msg)
        self.assertEqual(pets.get_pet(entry, "1")["name"], "Новое имя")

        ok, msg = pets.set_photo(entry, "1", "file9")
        self.assertTrue(ok, msg)
        self.assertEqual(pets.get_pet(entry, "1")["photo_file_id"], "file9")


class StatUpgradeTests(PetsTestCase):
    def test_respec_costs_rubies_then_spends_points_before_gold(self):
        entry = "respec"
        self._tame(entry, "1")
        data = pets._load(entry)
        record = data["pets"]["1"]
        record["stats"]["strength"] = 6
        record["stats"]["health"] = 4
        pets._save(entry, data)
        pets.grant_rubies(entry, "1", pets_config.STAT_RESPEC_RUBY_COST)

        ok, message, points = pets.respec_stats(entry, "1")

        self.assertTrue(ok, message)
        self.assertEqual(points, 8)
        self.assertEqual(pets.ruby_balance(entry, "1"), 0)
        pet = pets.get_pet(entry, "1")
        self.assertTrue(all(level == pets_config.STAT_MIN_LEVEL for level in pet["stats"].values()))
        self.assertEqual(pets.available_stat_points(pet), 8)
        text, keyboard = pets_ui.train_view(entry, "1", 0)
        self.assertIn("Свободные очки: <b>8</b>", text)
        callbacks = [button["callback_data"] for row in keyboard["inline_keyboard"] for button in row]
        self.assertIn(pets_ui.callback_data("1", "respec"), callbacks)

        ok, message, spent = pets.upgrade_stat(entry, "1", 0, "strength", times=5)
        self.assertTrue(ok, message)
        self.assertEqual(spent, 0)
        self.assertEqual(pets.stat_level(entry, "1", "strength"), 6)
        self.assertEqual(pets.available_stat_points(pets.get_pet(entry, "1")), 3)

        gold_cost = pets_config.stat_upgrade_cost(4)
        economy.grant(entry, "1", gold_cost, "test")
        ok, message, spent = pets.upgrade_stat(entry, "1", 0, "agility", times=4)
        self.assertTrue(ok, message)
        self.assertEqual(spent, gold_cost)
        self.assertEqual(pets.stat_level(entry, "1", "agility"), 5)
        self.assertEqual(pets.available_stat_points(pets.get_pet(entry, "1")), 0)
        self.assertEqual(economy.balance(entry, "1", 0), 0)

    def test_upgrade_stat_has_no_level_ceiling(self):
        entry = "chat"
        self._tame(entry, "1")
        data = pets._load(entry)
        data["pets"]["1"]["stats"]["strength"] = 80
        pets._save(entry, data)
        economy.grant(entry, "1", 100_000, "test")

        ok, msg, spent = pets.upgrade_stat(entry, "1", 0, "strength", times=1)
        self.assertTrue(ok, msg)
        self.assertEqual(spent, pets_config.stat_upgrade_cost(80))
        self.assertEqual(pets.stat_level(entry, "1", "strength"), 81)

    def test_endurance_is_saved_but_does_not_change_power_yet(self):
        entry = "endurance"
        self._tame(entry, "1")
        legacy = pets._load(entry)
        legacy["pets"]["1"]["stats"].pop("endurance")
        pets._save(entry, legacy)
        self.assertEqual(
            pets.get_pet(entry, "1")["stats"]["endurance"], pets_config.STAT_MIN_LEVEL,
        )
        before = pets.power_rating(entry, "1")
        economy.grant(entry, "1", 100, "test")
        ok, message, _spent = pets.upgrade_stat(entry, "1", 0, "endurance", times=10)
        self.assertTrue(ok, message)
        self.assertEqual(pets.stat_level(entry, "1", "endurance"), 11)
        self.assertEqual(pets.power_rating(entry, "1"), before)
        self.assertIn("Выносливость", pets_ui.train_view(entry, "1", 0)[0])
        self.assertIn("эффект появится позже", pets_ui.train_view(entry, "1", 0)[0])

    def test_upgrade_stat_times_charges_the_sum_of_steps_not_a_flat_multiple(self):
        entry = "chat"
        self._tame(entry, "1")
        start_level = pets_config.STAT_MIN_LEVEL
        step_costs = [pets_config.stat_upgrade_cost(start_level + i) for i in range(10)]
        # A flat "N * first step cost" would be way off since costs climb -- prove that.
        self.assertNotEqual(len(set(step_costs)), 1)

        economy.grant(entry, "1", sum(step_costs), "test")
        ok, msg, spent = pets.upgrade_stat(entry, "1", 0, "strength", times=10)
        self.assertTrue(ok, msg)
        self.assertEqual(spent, sum(step_costs))
        self.assertEqual(pets.stat_level(entry, "1", "strength"), start_level + 10)
        self.assertEqual(economy.balance(entry, "1", 0), 0)

    def test_upgrade_stat_stops_early_when_the_wallet_runs_out_and_buys_nothing_on_debt(self):
        entry = "chat"
        self._tame(entry, "1")
        start_level = pets_config.STAT_MIN_LEVEL
        step_costs = [pets_config.stat_upgrade_cost(start_level + i) for i in range(10)]
        affordable = sum(step_costs[:5])  # exactly 5 steps, not a coin more
        economy.grant(entry, "1", affordable, "test")

        ok, msg, spent = pets.upgrade_stat(entry, "1", 0, "strength", times=10)
        self.assertTrue(ok, msg)  # partial success is still success, honestly reported
        self.assertEqual(spent, affordable)
        self.assertEqual(pets.stat_level(entry, "1", "strength"), start_level + 5)
        self.assertEqual(economy.balance(entry, "1", 0), 0)
        self.assertIn("5", msg)
        self.assertIn("10", msg)

    def test_upgrade_stat_with_zero_balance_refuses_the_whole_batch(self):
        entry = "chat"
        self._tame(entry, "1")
        ok, msg, spent = pets.upgrade_stat(entry, "1", 0, "strength", times=3)
        self.assertFalse(ok)
        self.assertEqual(spent, 0)
        self.assertEqual(
            pets.stat_level(entry, "1", "strength"), pets_config.STAT_MIN_LEVEL
        )

    def test_upgrade_stat_without_a_pet_refuses(self):
        ok, msg, spent = pets.upgrade_stat("chat", "1", 0, "strength", times=1)
        self.assertFalse(ok)
        self.assertEqual(spent, 0)


class EffectiveStatsAndEquipmentTests(PetsTestCase):
    def test_effective_stats_adds_pet_level_and_equipment(self):
        entry = "chat"
        self._tame(entry, "1")
        data = pets._load(entry)
        data["pets"]["1"]["stats"]["strength"] = 10
        data["pets"]["1"]["level"] = 3
        pets._save(entry, data)

        item = next(
            weapon for weapon in pets.daily_storefront_weapons(entry, user_id="1")
            if "strength" in weapon.bonuses
        )
        economy.grant(entry, "1", item.price, "test")
        ok, msg = pets.buy_item(entry, "1", 0, item.code)
        self.assertTrue(ok, msg)
        ok, msg = pets.equip(entry, "1", item.code)
        self.assertTrue(ok, msg)

        derived = pets.effective_stats(entry, "1")
        expected = 10 + 3 * pets_config.PET_LEVEL_STAT_BONUS + item.bonuses["strength"]
        self.assertEqual(derived["strength"], expected)
        self.assertEqual(derived["armor"], 0)

    def test_effective_stats_has_sane_defaults_and_zero_armor_with_no_pet(self):
        # No record at all -- defaults read as a level-1 pet with unpurchased stats
        # (1 purchased + 1 pet-level bonus), never negative or missing.
        derived = pets.effective_stats("chat", "999")
        for key in pets_config.STAT_KEYS:
            self.assertEqual(derived[key], pets_config.STAT_MIN_LEVEL + pets_config.PET_LEVEL_STAT_BONUS)
        self.assertEqual(derived["armor"], 0)

    def test_effective_stats_never_goes_below_one_even_with_a_malus_item(self):
        entry = "chat"
        self._tame(entry, "1")
        bone = pets_config.find_item("bone")  # agility -3, drop-only so seed it directly
        data = pets._load(entry)
        data["pets"]["1"]["inventory"].append(bone.code)
        data["pets"]["1"]["equipped"]["weapon"] = bone.code
        pets._save(entry, data)

        derived = pets.effective_stats(entry, "1")
        self.assertGreaterEqual(derived["agility"], 1)

    def test_buy_item_refuses_a_drop_only_item(self):
        entry = "chat"
        self._tame(entry, "1")
        drop_item = next(item for item in pets_config.ITEMS if item.source == "drop")
        economy.grant(entry, "1", 100_000, "test")
        ok, msg = pets.buy_item(entry, "1", 0, drop_item.code)
        self.assertFalse(ok)
        self.assertNotIn(drop_item.code, pets.get_pet(entry, "1")["inventory"])

    def test_equipping_a_second_weapon_replaces_the_first(self):
        entry = "chat"
        self._tame(entry, "1")
        stick, fork = pets.daily_storefront_weapons(entry, user_id="1")[:2]
        economy.grant(entry, "1", stick.price + fork.price, "test")
        self.assertTrue(pets.buy_item(entry, "1", 0, stick.code)[0])
        self.assertTrue(pets.buy_item(entry, "1", 0, fork.code)[0])

        self.assertTrue(pets.equip(entry, "1", stick.code)[0])
        self.assertTrue(pets.equip(entry, "1", fork.code)[0])

        pet = pets.get_pet(entry, "1")
        self.assertEqual(pet["equipped"]["weapon"], fork.code)
        # The replaced weapon stays in the inventory rather than vanishing.
        self.assertIn(stick.code, pet["inventory"])

        ok, msg = pets.unequip(entry, "1", "weapon")
        self.assertTrue(ok, msg)
        self.assertIsNone(pets.get_pet(entry, "1")["equipped"]["weapon"])


class FamiliarFaceTests(PetsTestCase):
    """The price that replaced the per-opponent daily cap."""

    def _pair(self, entry="familiar"):
        for uid in ("1", "2", "3"):
            self._tame(entry, uid)
        return entry

    def _beat(self, entry, attacker, defender, times, day=None):
        """Log `times` arena fights, topping the bank up so the count is the only variable."""
        result = SimpleNamespace(winner=attacker, loser=defender)
        with patch("random.random", return_value=1.0):
            for _ in range(times):
                data = pets._load(entry)
                data["pets"][attacker]["fight_bank"] = 99
                pets._save(entry, data)
                pets.record_fight(entry, attacker, defender, result, day or pets.today())

    def test_each_stack_takes_ten_percent_and_the_floor_keeps_a_fight_fightable(self):
        self.assertEqual(pets.familiar_face_scale(0), 1.0)
        self.assertAlmostEqual(pets.familiar_face_scale(1), 0.9)
        self.assertAlmostEqual(pets.familiar_face_scale(3), 0.7)
        # Nine stacks would reach the floor exactly; a hundred must not go below it.
        self.assertEqual(pets.familiar_face_scale(100), pets_config.FAMILIAR_FACE_SCALE_FLOOR)
        self.assertIsNone(pets.familiar_face_for(0))
        mark = pets.familiar_face_for(2)
        self.assertEqual(mark["stacks"], 2)
        self.assertEqual(mark["percent"], 20)
        self.assertIn("×2", mark["tag"])
        self.assertTrue(pets.familiar_face_for(50)["capped"])

    def test_stats_drop_only_against_the_face_that_was_fought(self):
        entry = self._pair()
        self._beat(entry, "1", "2", 3)
        # Read AFTER the fights: winning also pays experience, so a before/after snapshot
        # would be measuring the level-up rather than the effect.
        clean = pets.effective_stats(entry, "1")

        against_two = pets.effective_stats(entry, "1", vs="2")
        against_three = pets.effective_stats(entry, "1", vs="3")
        for key in (*pets_config.STAT_KEYS, "armor"):
            self.assertEqual(against_three.get(key), clean.get(key), key)
            expected = round(clean[key] * 0.7) if key == "armor" else max(1, round(clean[key] * 0.7))
            self.assertEqual(against_two.get(key), expected, key)
        # Nothing is stored: the stacks are read back out of today's fight log.
        self.assertEqual(pets.effective_stats(entry, "1"), clean)

    def test_the_shake_is_directional_and_belongs_to_the_attacker(self):
        entry = self._pair("familiar-direction")
        clean_two = pets.effective_stats(entry, "2")
        self._beat(entry, "1", "2", 2)
        # "1" has been staring at "2" all day. "2" has been on the receiving end and is
        # exactly as strong against "1" as against anybody.
        self.assertEqual(pets.effective_stats(entry, "2", vs="1"), clean_two)
        self.assertLess(
            pets.effective_stats(entry, "1", vs="2")["strength"],
            pets.effective_stats(entry, "1")["strength"],
        )

    def test_it_multiplies_on_top_of_a_granted_mark_rather_than_replacing_it(self):
        entry = self._pair("familiar-mark")
        self._beat(entry, "1", "2", 1)
        pets.set_debuff(entry, "1", "impostor")
        record = pets.get_pet(entry, "1")
        mark_scale = pets.debuff_scale(record)
        self.assertLess(mark_scale, 1.0)

        raw = (
            pets.stat_level(entry, "1", "strength")
            + record.get("level", 1) * pets_config.PET_LEVEL_STAT_BONUS
        )
        # The mark alone, then the mark AND one stack: a marked creature that keeps
        # punching the same face pays for both.
        self.assertEqual(pets.effective_stats(entry, "1")["strength"], max(1, round(raw * mark_scale)))
        self.assertEqual(
            pets.effective_stats(entry, "1", vs="2")["strength"],
            max(1, round(raw * mark_scale * 0.9)),
        )

    def test_the_cap_is_gone_and_a_tenth_fight_is_allowed(self):
        entry = self._pair("familiar-nocap")
        self._beat(entry, "1", "2", 9)
        self.assertTrue(pets.can_attack_in_arena(entry, "1", "2"))
        self.assertEqual(pets.familiar_face(entry, "1", "2")["stacks"], 9)
        # A farming attacker is still refused: that gate was never the per-opponent one.
        data = pets._load(entry)
        data["pets"]["1"]["farm_level"] = 1
        pets._save(entry, data)
        self.assertTrue(pets.start_farm(entry, "1", 8)[0])
        self.assertFalse(pets.can_attack_in_arena(entry, "1", "2"))


class FightBankAndOpponentTests(PetsTestCase):
    def test_duels_have_a_ten_minute_cooldown_and_daily_cap(self):
        entry = "chat"
        base = datetime(2026, 8, 1, 12, 0, 0)
        ok, _ = pets.claim_duel(entry, "1", "2", base)
        self.assertTrue(ok)
        ok, note = pets.claim_duel(entry, "1", "3", base + timedelta(minutes=9, seconds=59))
        self.assertFalse(ok)
        self.assertIn("0:01", note)
        for index in range(1, pets_config.DUEL_DAILY_LIMIT):
            ok, _ = pets.claim_duel(entry, "1", str(index + 2), base + timedelta(minutes=10 * index))
            self.assertTrue(ok)
        ok, _ = pets.claim_duel(entry, "1", "99", base + timedelta(minutes=60))
        self.assertFalse(ok)

    def test_same_opponent_limits_reset_on_the_next_day(self):
        entry = "chat"
        base = datetime(2026, 8, 1, 12, 0, 0)
        self.assertTrue(pets.claim_duel(entry, "1", "2", base)[0])
        ok, note = pets.claim_duel(entry, "1", "2", base + timedelta(minutes=10))
        self.assertFalse(ok)
        self.assertIn("этим соперником", note)
        self.assertTrue(pets.claim_duel(entry, "1", "2", base + timedelta(days=1))[0])

        self._tame(entry, "1", "Attacker")
        self._tame(entry, "2", "Defender")
        result = SimpleNamespace(winner="1", loser="2")
        day = base.date()
        with patch("random.random", return_value=1.0):
            for _ in range(6):
                data = pets._load(entry)
                data["pets"]["1"]["fight_bank"] = 99   # the bank is not what is under test
                pets._save(entry, data)
                pets.record_fight(entry, "1", "2", result, day)
        # The arena no longer shuts the door on a repeat opponent -- it charges for them.
        self.assertTrue(pets.can_attack_in_arena(entry, "1", "2", day))
        self.assertEqual(pets.familiar_face(entry, "1", "2", day)["stacks"], 6)
        # And the count is derived from the day, so tomorrow starts clean by itself.
        self.assertIsNone(pets.familiar_face(entry, "1", "2", day + timedelta(days=1)))

    def test_fight_bank_recharges_one_per_complete_hour_and_keeps_fraction(self):
        entry = "chat"
        self._tame(entry, "1", "Attacker")
        start = datetime(2026, 8, 1, 12, 0)
        data = pets._load(entry)
        record = data["pets"]["1"]
        record.update({"fight_bank": 0, "fight_bank_cap": 5, "fight_bank_checkpoint": start.isoformat()})
        pets._save(entry, data)

        self.assertEqual(pets.fights_left(entry, "1", now=start + timedelta(minutes=59, seconds=59)), 0)
        self.assertEqual(pets.fights_left(entry, "1", now=start + timedelta(hours=1)), 1)
        self.assertEqual(pets.fights_left(entry, "1", now=start + timedelta(hours=2, minutes=59)), 2)
        status = pets.fight_allowance_breakdown(entry, "1", now=start + timedelta(hours=2, minutes=59))
        self.assertEqual(status["seconds_until_next"], 60)

    def test_fight_bank_caps_overflow_and_legacy_daily_state_migrates_conservatively(self):
        entry = "chat"
        self._tame(entry, "1", "Attacker")
        now = datetime(2026, 8, 1, 12, 0)
        data = pets._load(entry)
        record = data["pets"]["1"]
        record.update({"fight_bank": 0, "fight_bank_cap": 5, "fight_bank_checkpoint": now.isoformat()})
        pets._save(entry, data)
        self.assertEqual(pets.fights_left(entry, "1", now=now + timedelta(hours=20)), 5)
        # Overflow is discarded at the cap: spending much later starts a fresh hour.
        capped = pets._load(entry)
        capped["pets"]["1"]["fight_bank"] = 4
        pets._save(entry, capped)
        self.assertEqual(pets.fights_left(entry, "1", now=now + timedelta(hours=20, minutes=59)), 4)

        legacy = pets._load(entry)
        legacy["pets"]["1"].pop("fight_bank", None)
        legacy["pets"]["1"].pop("fight_bank_cap", None)
        legacy["pets"]["1"].pop("fight_bank_checkpoint", None)
        legacy["pets"]["1"].update({"fights_today": 8, "fights_day": now.date().isoformat()})
        pets._save(entry, legacy)
        self.assertEqual(pets.fights_left(entry, "1", now=now), 2)
        migrated = pets.get_pet(entry, "1")
        self.assertNotIn("fights_today", migrated)
        self.assertNotIn("fights_day", migrated)

    def test_fight_bank_does_not_reset_at_midnight(self):
        entry = "chat"
        self._tame(entry, "1", "Attacker")
        start = datetime(2026, 8, 1, 23, 30)
        data = pets._load(entry)
        data["pets"]["1"].update({
            "fight_bank": 0, "fight_bank_cap": 5,
            "fight_bank_checkpoint": start.isoformat(),
        })
        pets._save(entry, data)

        self.assertEqual(pets.fights_left(entry, "1", now=start + timedelta(minutes=59)), 0)
        self.assertEqual(pets.fights_left(entry, "1", now=start + timedelta(hours=1)), 1)

    def test_corrupt_fight_bank_is_repaired_without_minting_old_hours(self):
        entry = "chat"
        self._tame(entry, "1", "Attacker")
        now = datetime(2026, 8, 2, 12, 0)
        data = pets._load(entry)
        data["pets"]["1"].update({
            "fight_bank": "broken", "fight_bank_cap": 5,
            "fight_bank_checkpoint": (now - timedelta(days=30)).isoformat(),
        })
        pets._save(entry, data)

        status = pets.fight_allowance_breakdown(entry, "1", now=now)
        self.assertEqual(status["fights_left"], 0)
        repaired = pets.get_pet(entry, "1")
        self.assertEqual(repaired["fight_bank"], 0)
        self.assertEqual(repaired["fight_bank_checkpoint"], now.isoformat())

    def test_legacy_migration_preserves_the_old_double_paint_remainder(self):
        entry = "paint-migration"
        self._tame(entry, "1", "Attacker")
        now = datetime(2026, 8, 1, 12, 0)
        stats.record_figurine_live(
            entry, now.date(), "1", "owner", "Owner", message_id=77,
        )
        data = pets._load(entry)
        record = data["pets"]["1"]
        record.pop("fight_bank", None)
        record.pop("fight_bank_cap", None)
        record.pop("fight_bank_checkpoint", None)
        # Old allowance: 10 base + 2 for this paint. Eleven were spent, so one remains.
        record.update({"fights_today": 11, "fights_day": now.date().isoformat()})
        pets._save(entry, data)

        status = pets.fight_allowance_breakdown(entry, "1", now=now)
        self.assertEqual((status["fights_left"], status["capacity"]), (1, 6))

    def test_capacity_upgrade_does_not_retroactively_credit_old_hours(self):
        entry = "chat"
        self._tame(entry, "1", "Attacker")
        start = datetime(2026, 8, 1, 12, 0)
        data = pets._load(entry)
        data["pets"]["1"].update({
            "fight_bank": 0, "fight_bank_cap": 5, "fight_bank_checkpoint": start.isoformat(),
        })
        pets._save(entry, data)
        economy.grant(entry, "1", pets_config.CAGE_UPGRADE_COSTS[1], "test_upgrade")
        ok, note = pets.upgrade_cage(entry, "1", 0, now=start + timedelta(hours=3))
        self.assertTrue(ok, note)
        # Three old-cap hours were collected; the extra capacity slot itself is empty.
        status = pets.fight_allowance_breakdown(entry, "1", now=start + timedelta(hours=3))
        self.assertEqual((status["fights_left"], status["capacity"]), (3, 6))
        self.assertEqual(pets.fights_left(entry, "1", now=start + timedelta(hours=4)), 4)

    def test_find_opponent_draws_uniformly_from_all_pets_regardless_of_power_gap(self):
        entry = "chat"
        self._tame(entry, "1")
        self._tame(entry, "2")
        self._tame(entry, "3")
        data = pets._load(entry)
        data["pets"]["1"]["stats"]["strength"] = 20
        data["pets"]["2"]["stats"]["strength"] = 22
        data["pets"]["3"]["stats"]["strength"] = 80
        pets._save(entry, data)

        # The finder must not quietly funnel players back into a power band: both the
        # near and wildly stronger pet belong to the same draw pool.
        rng = random.Random(1)
        seen = set()
        for _ in range(25):
            opponent = pets.find_opponent(entry, "1", rng=rng)
            self.assertIsNotNone(opponent)
            self.assertNotEqual(opponent, "1")
            seen.add(opponent)
        self.assertEqual(seen, {"2", "3"})

    def test_find_opponent_does_not_fall_back_to_the_nearest_power(self):
        entry = "chat"
        self._tame(entry, "1")
        self._tame(entry, "2")
        self._tame(entry, "3")
        data = pets._load(entry)
        data["pets"]["1"]["stats"]["strength"] = 1
        data["pets"]["2"]["stats"]["strength"] = 70
        data["pets"]["3"]["stats"]["strength"] = 80
        pets._save(entry, data)

        # This seeded draw selects id 3.  The old finder would return id 2 because it
        # was the nearest power match, even though both were outside the old window.
        opponent = pets.find_opponent(entry, "1", rng=random.Random(0))
        self.assertEqual(opponent, "3")

    def test_pet_leaderboard_lists_tamed_pets_by_power(self):
        entry = "chat"
        self._tame(entry, "1", "Первый")
        self._tame(entry, "2", "Второй")
        data = pets._load(entry)
        data["pets"]["1"]["stats"]["strength"] = 20
        data["pets"]["2"]["stats"]["strength"] = 50
        data["pets"]["2"]["owner_username"] = "second"
        pets._save(entry, data)

        rows = pets.pet_leaderboard(entry)

        self.assertEqual([row["name"] for row in rows], ["Второй", "Первый"])
        self.assertEqual(rows[0]["owner_username"], "second")
        self.assertGreater(rows[0]["power"], rows[1]["power"])

    def test_leaderboard_view_shows_owner_pet_and_power(self):
        entry = "chat"
        self._tame(entry, "1", "Первый")
        data = pets._load(entry)
        data["pets"]["1"]["owner_username"] = "first"
        pets._save(entry, data)

        text, keyboard = pets_ui.leaderboard_view(entry, "1")

        self.assertIn("@first", text)
        self.assertIn("Первый", text)
        self.assertIn(str(pets.power_rating(entry, "1")), text)
        self.assertEqual(
            pets_ui.parse_callback(keyboard["inline_keyboard"][-1][0]["callback_data"])[1],
            "main",
        )

    def test_find_opponent_excludes_the_current_card_when_rerolling(self):
        entry = "chat"
        self._tame(entry, "1")
        self._tame(entry, "2")
        self._tame(entry, "3")
        opponent = pets.find_opponent(entry, "1", rng=random.Random(1), exclude_ids={"2"})
        self.assertEqual(opponent, "3")

    def test_reroll_excludes_the_current_card_when_an_alternative_exists(self):
        entry = "chat"
        for user_id in ("1", "2", "3"):
            self._tame(entry, user_id)
        opponent = pets.find_opponent(
            entry, "1", rng=random.Random(1), exclude_ids={"2"}, attackable_only=True,
        )
        self.assertEqual(opponent, "3")

    def test_reroll_can_return_the_only_available_card(self):
        entry = "chat"
        self._tame(entry, "1")
        self._tame(entry, "2")
        opponent = pets.find_opponent(
            entry, "1", rng=random.Random(1), exclude_ids={"2"}, attackable_only=True,
        )
        self.assertEqual(opponent, "2")

    def test_a_heavily_fought_target_stays_attackable_and_merely_costs_more(self):
        """The per-opponent cap is gone: Знакомое лицо prices the repeat, it does not ban it."""
        entry = "chat"
        self._tame(entry, "1")
        self._tame(entry, "2")
        self._tame(entry, "3")
        result = SimpleNamespace(winner="1", loser="2")
        today = pets.today()
        with patch("random.random", return_value=1.0):
            for _ in range(3):
                pets.record_fight(entry, "1", "2", result, today)

        self.assertTrue(pets.can_attack_in_arena(entry, "1", "2", today))
        self.assertIn(
            pets.find_opponent(entry, "1", rng=random.Random(1), attackable_only=True),
            {"2", "3"},
        )

    def test_find_opponent_returns_none_only_when_the_chat_has_no_other_pet(self):
        entry = "chat"
        self._tame(entry, "1")
        self.assertIsNone(pets.find_opponent(entry, "1", rng=random.Random(1)))


class FarmPassiveIncomeTests(PetsTestCase):
    def _build_farm(self, entry="chat", user_id="1", level=1, now=None):
        self._tame(entry, user_id)
        economy.grant(
            entry, user_id, sum(pets_config.FARM_UPGRADE_COSTS[:level]), "test",
        )
        for expected_level in range(1, level + 1):
            ok, message = pets.upgrade_farm(entry, user_id, 0, now=now)
            self.assertTrue(ok, message)
            self.assertEqual(pets.farm_level(entry, user_id), expected_level)

    def test_income_only_credits_complete_elapsed_hours_and_keeps_fraction(self):
        start = datetime(2026, 8, 8, 14, 55)
        self._build_farm(now=start)
        self.assertEqual(pets.settle_passive_income("chat", "1", now=start + timedelta(minutes=6))["credited"], 0)
        self.assertEqual(pets.settle_passive_income("chat", "1", now=start + timedelta(hours=1, minutes=5))["credited"], 1)
        # The five-minute remainder is retained, so another 55 minutes completes hour 2.
        self.assertEqual(pets.settle_passive_income("chat", "1", now=start + timedelta(hours=2))["credited"], 1)
        self.assertEqual(economy.balance("chat", "1", 0), 2)

    def test_income_cap_and_restart_retry_do_not_double_credit(self):
        start = datetime(2026, 8, 8, 10)
        self._build_farm(now=start)
        later = start + timedelta(days=10)
        self.assertEqual(
            pets.settle_passive_income("chat", "1", now=later)["credited"],
            pets_config.FARM_PASSIVE_STORAGE_CAP[1],
        )
        # New loads (the same path a process restart takes) see the advanced checkpoint.
        self.assertEqual(pets.settle_passive_income("chat", "1", now=later)["credited"], 0)
        self.assertEqual(
            economy.balance("chat", "1", 0), pets_config.FARM_PASSIVE_STORAGE_CAP[1],
        )

    def test_corrupt_income_checkpoint_is_reset_without_crashing(self):
        self._build_farm(now=datetime(2026, 8, 8, 10))
        data = economy._load("chat")
        data["users"]["1"]["effects"][economy.FARM_PASSIVE_EFFECT_KEY]["last_hour"] = "not-a-date"
        economy._save("chat", data)
        result = pets.settle_passive_income("chat", "1", now=datetime(2026, 8, 8, 12))
        self.assertEqual(result["credited"], 0)
        repaired = economy._load("chat")["users"]["1"]["effects"][economy.FARM_PASSIVE_EFFECT_KEY]["last_hour"]
        self.assertEqual(repaired, "2026-08-08T12:00:00")

    def test_upgrade_settles_old_rate_and_refuses_without_money(self):
        start = datetime(2026, 8, 8, 10)
        self._build_farm(level=2, now=start)
        later = start + timedelta(hours=3)
        ok, message = pets.upgrade_farm("chat", "1", 0, now=later)
        self.assertFalse(ok)
        self.assertIn("Нужно", message)
        self.assertEqual(economy.balance("chat", "1", 0), 3)
        economy.grant("chat", "1", pets_config.FARM_UPGRADE_COSTS[2], "test")
        ok, message = pets.upgrade_farm("chat", "1", 0, now=later)
        self.assertTrue(ok, message)
        self.assertEqual(pets.farm_level("chat", "1"), 3)
        self.assertEqual(pets.settle_passive_income("chat", "1", now=later + timedelta(hours=2))["credited"], 4)

    def test_farm_view_shows_passive_rate_and_upgrade_callback(self):
        self._build_farm()
        text, keyboard = pets_ui.farm_view("chat", "1", 0)
        # The passive rate now lives in the timer block at the very bottom, because it is
        # a countdown rather than something the player presses.
        self.assertIn("Пассив +1/ч — начисление в", text)
        self.assertLess(text.index("<b>Смена</b>"), text.index("<b>⏱ Таймеры</b>"))
        callbacks = [pets_ui.parse_callback(button["callback_data"])[1]
                     for row in keyboard["inline_keyboard"] for button in row]
        self.assertIn("upfarm", callbacks)
        self.assertNotIn("uphamsterator", callbacks)

    def test_retired_facility_is_removed_and_refunded_exactly_once(self):
        self._tame("chat", "1")
        data = pets._load("chat")
        data["pets"]["1"]["hamsterator_level"] = 3
        pets._save("chat", data)

        expected = sum(pets_config.LEGACY_HAMSTERATOR_UPGRADE_COSTS[:3])
        self.assertEqual(
            pets.retire_hamsterators(["chat"]), {"players": 1, "gold": expected},
        )
        self.assertNotIn("hamsterator_level", pets._load("chat")["pets"]["1"])
        self.assertEqual(economy.balance("chat", "1", 0), expected)
        self.assertEqual(
            pets.retire_hamsterators(["chat"]), {"players": 0, "gold": 0},
        )
        self.assertEqual(economy.balance("chat", "1", 0), expected)


class EquipmentTradingTests(PetsTestCase):
    def _two_pets(self, entry="chat"):
        self._tame(entry, "1", "One")
        self._tame(entry, "2", "Two")
        data = pets._load(entry)
        data["pets"]["1"]["level"] = pets_config.GIFT_MIN_PET_LEVEL
        pets._save(entry, data)

    def test_legacy_codes_and_duplicates_canonicalize_on_read(self):
        self._two_pets()
        data = pets._load("chat")
        data["pets"]["1"]["inventory"] = ["stick", "stick", "fork"]
        data["pets"]["1"]["equipped"]["weapon"] = "bone"
        pets._save("chat", data)
        pet = pets.get_pet("chat", "1")
        self.assertEqual(pet["inventory"], ["w001", "w002", "w003"])
        self.assertEqual(pet["equipped"]["weapon"], "w003")

    def test_new_drop_catalogues_are_integrated_into_all_three_equipment_slots(self):
        # 40 dropped amulets + 2 starter shop ones + the utility shelf.
        self.assertEqual(
            len([item for item in pets_config.ITEMS if item.slot == "amulet"]),
            42 + len(UTILITY_SHOP_CODES),
        )
        self.assertEqual(len([item for item in pets_config.ITEMS if item.slot == "boots"]), 42)
        self.assertEqual(len([item for item in pets_config.ITEMS if item.slot == "gloves"]), 42)
        # The three DROP catalogues. Matched on source as well as prefix: the amulet
        # catalogue also sells a utility item now, and it shares the prefix without
        # belonging to the loot table this counts.
        new_drops = [
            item for item in pets_config.ITEMS
            if item.code.startswith(("amulet_", "bt", "gl")) and item.source == "drop"
        ]
        self.assertEqual(len(new_drops), 120)
        self.assertTrue(all(item.source == "drop" and item.drop_weight > 0 for item in new_drops))
        self.assertEqual(len([item for item in new_drops if item.effect]), 62)

    def test_every_equipment_slot_has_at_least_three_effectful_legendaries(self):
        for slot in pets_config.SLOT_KEYS:
            legendary = [
                item for item in pets_config.ITEMS
                if item.slot == slot and item.rarity == "legendary"
            ]
            self.assertGreaterEqual(len(legendary), 3, slot)
            self.assertTrue(all(item.effect for item in legendary), slot)

    def test_equipped_amulet_passive_reaches_combat_and_is_visible_in_the_bag(self):
        self._two_pets()
        # Weapons carry passives too now, so this must pick an amulet explicitly --
        # the bag view below is filtered to the amulet slot.
        amulet = next(
            item for item in pets_config.ITEMS if item.slot == "amulet" and item.effect
        )
        data = pets._load("chat")
        data["pets"]["1"]["inventory"] = [amulet.code]
        pets._save("chat", data)

        self.assertTrue(pets.equip("chat", "1", amulet.code)[0])

        self.assertEqual(pets.equipped_combat_effects("chat", "1"), (amulet.effect,))
        text, _ = pets_ui.bag_items_view("chat", "1", 0, "amulet")
        self.assertIn(amulet.effect["text"], text)

    def test_sell_refuses_equipped_and_pays_explicit_resale(self):
        self._two_pets()
        item = next(
            weapon for weapon in pets.daily_storefront_weapons("chat", user_id="1")
            if weapon.rarity not in {"rare", "legendary"}
        )
        economy.grant("chat", "1", item.price, "test")
        self.assertTrue(pets.buy_item("chat", "1", 0, item.code)[0])
        self.assertTrue(pets.equip("chat", "1", item.code)[0])
        self.assertFalse(pets.sell_item("chat", "1", item.code)[0])
        self.assertTrue(pets.unequip("chat", "1", "weapon")[0])
        ok, _, value = pets.sell_item("chat", "1", item.code)
        self.assertTrue(ok)
        self.assertEqual(value, pets_config.resale_value(item))
        self.assertNotIn(item.code, pets.get_pet("chat", "1")["inventory"])

    def test_gift_is_unique_atomic_and_refuses_equipped_or_receiver_duplicate(self):
        self._two_pets()
        item = next(
            weapon for weapon in pets.daily_storefront_weapons("chat", user_id="1")
            if weapon.rarity not in {"rare", "legendary"}
        )
        economy.grant("chat", "1", item.price, "test")
        self.assertTrue(pets.buy_item("chat", "1", 0, item.code)[0])
        self.assertTrue(pets.equip("chat", "1", item.code)[0])
        self.assertFalse(pets.gift_item("chat", "1", "2", item.code)[0])
        self.assertTrue(pets.unequip("chat", "1", "weapon")[0])
        data = pets._load("chat")
        data["pets"]["2"]["inventory"].append(item.code)
        pets._save("chat", data)
        self.assertFalse(pets.gift_item("chat", "1", "2", item.code)[0])
        data = pets._load("chat")
        data["pets"]["2"]["inventory"].remove(item.code)
        pets._save("chat", data)
        self.assertTrue(pets.gift_item("chat", "1", "2", item.code)[0])
        self.assertNotIn(item.code, pets.get_pet("chat", "1")["inventory"])
        self.assertIn(item.code, pets.get_pet("chat", "2")["inventory"])
        self.assertFalse(pets.gift_item("chat", "2", "2", item.code)[0])

    def test_gifted_weapon_keeps_its_rune_owner_tag_and_counters(self):
        self._two_pets()
        item = next(
            weapon for weapon in pets.daily_storefront_weapons("chat", user_id="1")
            if weapon.rarity not in {"rare", "legendary"}
        )
        economy.grant("chat", "1", item.price, "test")
        self.assertTrue(pets.buy_item("chat", "1", 0, item.code)[0])
        data = pets._load("chat")
        data["pets"]["1"].setdefault("weapon_enchantments", {})[item.code] = "fire"
        data["pets"]["1"]["weapon_records"][item.code].update({
            "pet_wins": 7, "mob_wins": 5, "boss_wins": 2,
        })
        pets._save("chat", data)

        self.assertTrue(pets.gift_item("chat", "1", "2", item.code)[0])

        details = pets.weapon_details("chat", "2", item.code)
        receiver = pets.get_pet("chat", "2")
        self.assertEqual(details, {
            "first_owner": "One", "pet_wins": 7, "mob_wins": 5, "boss_wins": 2,
        })
        self.assertEqual(receiver["weapon_enchantments"][item.code], "fire")
        self.assertNotIn(item.code, pets.get_pet("chat", "1").get("weapon_enchantments", {}))

    def test_pet_victory_increments_the_equipped_weapons_counter(self):
        self._two_pets()
        item = next(
            weapon for weapon in pets.daily_storefront_weapons("chat", user_id="1")
            if weapon.rarity not in {"rare", "legendary"}
        )
        economy.grant("chat", "1", item.price, "test")
        self.assertTrue(pets.buy_item("chat", "1", 0, item.code)[0])
        self.assertTrue(pets.equip("chat", "1", item.code)[0])

        pets.record_fight(
            "chat", "1", "2", SimpleNamespace(winner="1", loser="2", is_draw=False), date(2026, 8, 1),
        )

        self.assertEqual(pets.weapon_details("chat", "1", item.code)["pet_wins"], 1)

    def test_same_weapon_drop_can_belong_to_two_players(self):
        self._two_pets()

        first = pets.grant_random_drop("chat", "1", 1.0, seed="shared-weapon")
        second = pets.grant_random_drop("chat", "2", 1.0, seed="shared-weapon")

        self.assertIsNotNone(first)
        self.assertEqual(first["code"], second["code"])
        self.assertIn(first["code"], pets.get_pet("chat", "1")["inventory"])
        self.assertIn(second["code"], pets.get_pet("chat", "2")["inventory"])

    def test_weapon_catalogue_is_paginated_with_compact_callbacks(self):
        self._two_pets()
        text, keyboard = pets_ui.slot_view("chat", "1", 0, "weapon", 1)
        self.assertIn("2/", text)
        callbacks = [button["callback_data"] for row in keyboard["inline_keyboard"] for button in row]
        self.assertTrue(any(":slot:weapon," in data for data in callbacks))
        self.assertTrue(all(len(data.encode("utf-8")) <= pets_ui.MAX_CALLBACK_BYTES for data in callbacks))

    def test_owned_late_catalogue_weapon_is_promoted_to_first_page(self):
        self._two_pets()
        late = pets_config.find_item("w500")
        data = pets._load("chat")
        data["pets"]["1"]["inventory"].append(late.code)
        pets._save("chat", data)

        text, keyboard = pets_ui.slot_view("chat", "1", 0, "weapon", 0)

        self.assertIn(late.name, text)
        callbacks = [button["callback_data"] for row in keyboard["inline_keyboard"] for button in row]
        self.assertIn(pets_ui.callback_data("1", "gift", late.code), callbacks)
        self.assertIn(pets_ui.callback_data("1", "sell", late.code), callbacks)

    def test_accessory_slot_view_first_page_has_a_buy_button_when_nothing_is_owned(self):
        """The production bug this guards: amulet/gloves/boots each have ~30 drop-only
        catalogue entries against 2 purchasable shop items, and the drop-only codes
        ("amulet_...", "bt..", "gl..") mostly sort ahead of the shop items' names. An
        owner-less player opening any of the three tabs saw several pages of "только из
        боёв" before ever reaching a working "Купить" button, which read like the shop
        sold nothing but weapons. Purchasable stock must now win its own sort tier and
        land on page one regardless of how its code compares to the drop-only pool."""
        self._two_pets()
        for slot in ("amulet", "gloves", "boots", "shield"):
            text, keyboard = pets_ui.slot_view("chat", "1", 0, slot, 0)
            buy_callbacks = [
                button["callback_data"]
                for row in keyboard["inline_keyboard"] for button in row
                if pets_ui.parse_callback(button["callback_data"])[1] == "buy"
            ]
            self.assertTrue(buy_callbacks, f"no buy button on page 1 of the {slot} slot:\n{text}")
            self.assertNotIn("Сейчас купить здесь нечего", text)

    def test_shop_opens_accessory_shelves_that_sell_rather_than_the_full_catalogue(self):
        """The sort-tier fix above was not enough on its own, and this is why.

        Owned gear keeps the first sort tiers in slot_view, so once a player owns a dozen
        drop accessories -- routine for anyone who has been fighting -- the two buyable
        items get pushed off page one again and the shop looks exactly as broken as it did
        before. The 🛒 buttons therefore open a dedicated shelf that lists only what is on
        sale, so "buy an amulet" stays two taps no matter how full the bag is.
        """
        self._two_pets()
        data = pets._load("chat")
        for slot in ("amulet", "gloves", "boots", "shield"):
            drops = [item.code for item in pets_config.items_for_slot(slot, "drop")][:12]
            self.assertTrue(drops, slot)
            data["pets"]["1"]["inventory"].extend(drops)
            data["pets"]["1"].setdefault("equipped", {})[slot] = drops[0]
        pets._save("chat", data)

        _, storefront = pets_ui.store_view("chat", "1", 0)
        routes = {
            pets_ui.parse_callback(button["callback_data"])[2]:
                pets_ui.parse_callback(button["callback_data"])[1]
            for row in storefront["inline_keyboard"] for button in row
        }
        for slot in ("amulet", "gloves", "boots", "shield"):
            self.assertEqual(routes.get(slot), "shopslot", f"{slot} tab must open the shelf")
            text, keyboard = pets_ui.shop_slot_view("chat", "1", 0, slot)
            buys = [
                button for row in keyboard["inline_keyboard"] for button in row
                if pets_ui.parse_callback(button["callback_data"])[1] == "buy"
            ]
            # Every personal offer is visible, with no paging to reach any of them.
            self.assertEqual(len(buys), pets_config.DAILY_STOREFRONT_SIZE)
            self.assertNotIn("только из боёв", text)
            # The full catalogue stays reachable, it is just not what the shop opens onto.
            self.assertIn("slot", [
                pets_ui.parse_callback(button["callback_data"])[1]
                for row in keyboard["inline_keyboard"] for button in row
            ])

    def test_buying_an_accessory_leaves_a_hole_only_on_the_buyers_shelf(self):
        self._two_pets()
        before = pets.daily_storefront_items("chat", "boots", user_id="1")
        bought = before[0]
        economy.grant("chat", "1", bought.price, "test")
        self.assertTrue(pets.buy_item("chat", "1", 0, bought.code)[0])
        after = pets.daily_storefront_items("chat", "boots", user_id="1")
        other = pets.daily_storefront_items("chat", "boots", user_id="2")
        self.assertEqual(len(after), pets_config.DAILY_STOREFRONT_SIZE - 1)
        self.assertNotIn(bought.code, {item.code for item in after})
        self.assertEqual(len(other), pets_config.DAILY_STOREFRONT_SIZE)

    def test_buy_accessory_lands_in_inventory_for_every_non_weapon_slot(self):
        """pets.buy_item's own logic already worked for accessories before this fix --
        only slot_view's ordering hid the button. Exercise the full purchase path
        end-to-end anyway, so a future change to buy_item's weapon-only branches (the
        daily-window and single-owner checks) cannot silently break accessories again."""
        self._two_pets()
        for slot in ("amulet", "gloves", "boots", "shield"):
            item = pets.daily_storefront_items("chat", slot, user_id="1")[0]
            economy.grant("chat", "1", item.price, "test")
            ok, note = pets.buy_item("chat", "1", 0, item.code)
            self.assertTrue(ok, note)
            self.assertIn(item.code, pets.get_pet("chat", "1")["inventory"])
            self.assertTrue(pets.equip("chat", "1", item.code)[0])

    def test_equipment_hub_routes_to_owned_bag_and_daily_shop(self):
        self._two_pets()
        text, keyboard = pets_ui.bag_view("chat", "1", 0)
        callbacks = [button["callback_data"] for row in keyboard["inline_keyboard"] for button in row]

        self.assertIn("Снаряжение", text)
        self.assertIn(pets_ui.callback_data("1", "bagitems", "weapon,0"), callbacks)
        self.assertIn(pets_ui.callback_data("1", "store"), callbacks)
        self.assertNotIn(pets_ui.callback_data("1", "collection"), callbacks)
        self.assertNotIn(pets_ui.callback_data("1", "slot", "weapon"), callbacks)

        _, store_keyboard = pets_ui.store_view("chat", "1", 0)
        store_callbacks = [
            button["callback_data"]
            for row in store_keyboard["inline_keyboard"]
            for button in row
        ]
        # Each accessory tab opens that slot's shop SHELF, not its full catalogue: see
        # test_shop_opens_accessory_shelves_that_sell_rather_than_the_full_catalogue.
        for slot in ("amulet", "gloves", "boots"):
            self.assertIn(pets_ui.callback_data("1", "shopslot", slot), store_callbacks)

    def test_owned_bag_is_paginated_without_catalogue_noise(self):
        self._two_pets()
        owned = [item.code for item in pets_config.items_for_slot("weapon")[:7]]
        data = pets._load("chat")
        data["pets"]["1"]["inventory"] = owned
        pets._save("chat", data)

        text, keyboard = pets_ui.bag_items_view("chat", "1", 0, "weapon", 1)
        callbacks = [button["callback_data"] for row in keyboard["inline_keyboard"] for button in row]

        self.assertIn("2/2", text)
        self.assertIn(pets_config.find_item(owned[-1]).name, text)
        self.assertNotIn(pets_config.find_item("w100").name, text)
        self.assertTrue(any(":bagitems:weapon,0" in data for data in callbacks))
        self.assertTrue(all(len(data.encode("utf-8")) <= pets_ui.MAX_CALLBACK_BYTES for data in callbacks))

    def test_locked_item_hides_gift_and_sale_from_owned_bag(self):
        self._two_pets()
        item = pets_config.find_item("w001")
        data = pets._load("chat")
        data["pets"]["1"]["inventory"] = [item.code]
        data["pets"]["1"]["locked_items"] = [item.code]
        pets._save("chat", data)

        _, keyboard = pets_ui.bag_items_view("chat", "1", 0, "weapon")
        callbacks = [button["callback_data"] for row in keyboard["inline_keyboard"] for button in row]

        self.assertIn(pets_ui.callback_data("1", "lock", item.code), callbacks)
        self.assertNotIn(pets_ui.callback_data("1", "gift", item.code), callbacks)
        self.assertNotIn(pets_ui.callback_data("1", "sell", item.code), callbacks)


class ForgeTests(PetsTestCase):
    def test_reforge_consumes_five_weakest_free_items_and_grants_next_rarity(self):
        self._tame("forge", "1")
        common = [
            item for item in pets_config.ITEMS
            if item.source == "drop" and item.rarity == "common"
        ][:6]
        data = pets._load("forge")
        data["pets"]["1"]["inventory"] = [item.code for item in common]
        data["pets"]["1"]["locked_items"] = [common[-1].code]
        pets._save("forge", data)

        status = pets.forge_status("forge", "1")["recipes"][0]
        self.assertEqual(status["required"], 5)
        self.assertEqual(len(status["ingredients"]), 5)
        self.assertTrue(status["can_forge"])
        self.assertNotIn(common[-1].code, status["ingredients"])
        ok, message, result_code = pets.reforge_items("forge", "1", "common", random.Random(7))

        self.assertTrue(ok, message)
        result = pets_config.find_item(result_code)
        self.assertEqual(result.rarity, "rare")
        self.assertEqual(result.source, "drop")
        inventory = pets.get_pet("forge", "1")["inventory"]
        self.assertIn(common[-1].code, inventory)
        self.assertIn(result.code, inventory)
        self.assertEqual(len(inventory), 2)

    def test_common_reforge_requires_five_free_items(self):
        self._tame("forge-requires-five", "1")
        common = [
            item for item in pets_config.ITEMS
            if item.source == "drop" and item.rarity == "common"
        ][:4]
        data = pets._load("forge-requires-five")
        data["pets"]["1"]["inventory"] = [item.code for item in common]
        pets._save("forge-requires-five", data)

        recipe = pets.forge_status("forge-requires-five", "1")["recipes"][0]
        self.assertEqual(recipe["required"], 5)
        self.assertFalse(recipe["can_forge"])
        ok, _message, result_code = pets.reforge_items(
            "forge-requires-five", "1", "common", random.Random(7),
        )
        self.assertFalse(ok)
        self.assertIsNone(result_code)

    def test_legendary_reforge_requires_and_consumes_seven_rares(self):
        self._tame("forge-legend", "1")
        rares = [
            item for item in pets_config.ITEMS
            if item.source == "drop" and item.rarity == "rare"
        ][:7]
        data = pets._load("forge-legend")
        data["pets"]["1"]["inventory"] = [item.code for item in rares]
        pets._save("forge-legend", data)

        recipe = pets.forge_status("forge-legend", "1")["recipes"][1]
        self.assertEqual(recipe["required"], 7)
        self.assertTrue(recipe["can_forge"])
        ok, message, result_code = pets.reforge_items(
            "forge-legend", "1", "rare", random.Random(11),
        )

        self.assertTrue(ok, message)
        self.assertEqual(pets_config.find_item(result_code).rarity, "legendary")
        self.assertEqual(pets.get_pet("forge-legend", "1")["inventory"], [result_code])

    def test_reforge_never_consumes_equipped_or_locked_items(self):
        self._tame("forge-safe", "1")
        common = [item for item in pets_config.ITEMS if item.rarity == "common"][:4]
        data = pets._load("forge-safe")
        record = data["pets"]["1"]
        record["inventory"] = [item.code for item in common]
        record["equipped"][common[0].slot] = common[0].code
        record["locked_items"] = [common[1].code]
        pets._save("forge-safe", data)

        ok, _message, result_code = pets.reforge_items("forge-safe", "1", "common")
        self.assertFalse(ok)
        self.assertIsNone(result_code)
        self.assertEqual(pets.get_pet("forge-safe", "1")["inventory"], [item.code for item in common])


class StorefrontAndCollectionTests(PetsTestCase):
    def _two_pets(self, entry="shop-chat"):
        self._tame(entry, "1", "One")
        self._tame(entry, "2", "Two")
        data = pets._load(entry)
        data["pets"]["1"]["level"] = pets_config.GIFT_MIN_PET_LEVEL
        pets._save(entry, data)

    def test_daily_storefront_is_stable_sized_and_changes_each_twelve_hours(self):
        moment = datetime(2026, 8, 8, 3)
        for slot in pets_config.SLOT_KEYS:
            with self.subTest(slot=slot):
                first = pets_config.daily_storefront_items("shop-chat", slot, moment, user_id="1")
                again = pets_config.daily_storefront_items(
                    "shop-chat", slot, moment.replace(hour=11), user_id="1",
                )
                afternoon = pets_config.daily_storefront_items(
                    "shop-chat", slot, moment.replace(hour=12), user_id="1",
                )
                other_player = pets_config.daily_storefront_items(
                    "shop-chat", slot, moment, user_id="2",
                )
                self.assertEqual([item.code for item in first], [item.code for item in again])
                self.assertEqual(pets_config.DAILY_STOREFRONT_SIZE, 6)
                self.assertEqual(len(first), pets_config.DAILY_STOREFRONT_SIZE)
                self.assertEqual(len({item.code for item in first}), pets_config.DAILY_STOREFRONT_SIZE)
                self.assertNotEqual([item.code for item in first], [item.code for item in afternoon])
                self.assertNotEqual([item.code for item in first], [item.code for item in other_player])
                self.assertTrue(all(item.source == "shop" and item.slot == slot for item in first))
                self.assertEqual(sum(item.rarity == "common" for item in first), 5)
                self.assertEqual(sum(item.rarity == "rare" for item in first), 1)

    def test_storefront_windows_turn_at_midnight_and_noon_moscow_time(self):
        before_midnight_moscow = datetime(2026, 8, 8, 20, 59, tzinfo=timezone.utc)
        midnight_moscow = before_midnight_moscow + timedelta(minutes=1)
        before_noon_moscow = datetime(2026, 8, 9, 8, 59, tzinfo=timezone.utc)
        noon_moscow = before_noon_moscow + timedelta(minutes=1)

        self.assertNotEqual(
            pets_config.storefront_window(before_midnight_moscow),
            pets_config.storefront_window(midnight_moscow),
        )
        self.assertNotEqual(
            pets_config.storefront_window(before_noon_moscow),
            pets_config.storefront_window(noon_moscow),
        )

    def test_every_daily_storefront_has_a_weapon_at_the_ordinary_price_floor(self):
        # The rotation advances in twelve-hour windows; a full year guards against a
        # future catalogue change silently removing the weakest ordinary choice.
        for entry in ("shop-chat", "other-chat", "-100123"):
            for offset in range(366):
                stock = pets_config.daily_storefront_weapons(
                    entry, date(2026, 1, 1) + timedelta(days=offset),
                )
                self.assertTrue(
                    any(item.price <= pets_config.STARTER_WEAPON_MAX_PRICE for item in stock),
                    (entry, offset, [(item.code, item.price) for item in stock]),
                )

    def test_starter_weapon_resale_stays_low(self):
        starters = [
            item for item in pets_config.items_for_slot("weapon", "shop")
            if item.price <= pets_config.STARTER_WEAPON_MAX_PRICE
        ]
        self.assertTrue(starters)
        self.assertTrue(all(item.resale_price <= item.price // 5 for item in starters))

    def test_storefront_backfills_personal_items_after_a_purchase(self):
        full = pets_config.daily_storefront_weapons("shop-chat", date(2026, 8, 8), user_id="1")
        owned = next(item for item in full if item.rarity == "common")
        stock = pets_config.daily_storefront_weapons(
            "shop-chat", date(2026, 8, 8),
            excluded_codes={owned.code}, user_id="1",
        )
        self.assertEqual(len(stock), len(full))
        self.assertNotIn(owned.code, {item.code for item in stock})

    def test_weapon_price_bands_keep_504_unique_weapons_and_rare_goals(self):
        weapons = pets_config.items_for_slot("weapon")
        self.assertEqual(len(weapons), 504)
        self.assertEqual(len({item.code for item in weapons}), 504)
        self.assertEqual(len({item.name for item in weapons}), 504)
        shop = [item for item in weapons if item.source == "shop"]
        prices = {rarity: [item.price for item in shop if item.rarity == rarity]
                  for rarity in ("common", "rare")}
        self.assertEqual((min(prices["common"]), max(prices["common"])), (60, 105))
        self.assertFalse(any(item.rarity == "uncommon" for item in weapons))
        self.assertEqual((min(prices["rare"]), max(prices["rare"])), (160, 195))
        self.assertTrue(all(item.resale_price <= item.price // 5 for item in shop))
        self.assertTrue(all(
            item.source == "drop" and item.price == 0
            for item in weapons if item.rarity == "legendary"
        ))

    def test_three_shop_commons_cannot_bypass_the_rare_price_or_resale(self):
        ordinary = [
            item for item in pets_config.ITEMS
            if item.source == "shop" and item.rarity == "common"
        ]
        shop_rares = [
            item for item in pets_config.ITEMS
            if item.source == "shop" and item.rarity == "rare"
        ]
        forged_rares = [
            item for item in pets_config.ITEMS
            if item.source == "drop" and item.rarity == "rare"
        ]
        cheapest_recipe = 3 * min(item.price for item in ordinary)
        self.assertGreaterEqual(cheapest_recipe, min(item.price for item in shop_rares))
        self.assertGreater(
            cheapest_recipe,
            max(pets_config.resale_value(item) for item in forged_rares),
        )

    def test_every_shop_item_prices_like_its_power_and_rarity_not_its_catalogue(self):
        """Regression guard for the pre-2026-08 accessory prices: bead/acorn/mittens/
        claws/slippers/springs were hand-picked numbers up to 1,100 coins against a
        5-10 gold arena win, wildly out of scale with a comparable weapon. Every
        source == "shop" item -- weapon or accessory -- must price exactly the way
        pets_weapon_catalog.shop_price_for_bonuses would price a weapon of the same
        rarity and the same stat bonuses, so a future hand-added item cannot silently
        reintroduce a three-figure accessory."""
        shop_items = [item for item in pets_config.ITEMS if item.source == "shop"]
        self.assertTrue(shop_items)
        # 6 accessories (bead/acorn/mittens/claws/slippers/springs), 3 shields and the
        # weapon catalogue's 375 shop weapons (250 common + 120 uncommon + 5 rare),
        # plus the utility items below.
        self.assertEqual(len(shop_items), 384 + len(UTILITY_SHOP_CODES))
        for item in shop_items:
            if item.code in UTILITY_SHOP_CODES:
                # Utility amulets are priced for their combat effect as well as their
                # stats, so the pure-stat weapon formula would underprice them.
                self.assertLessEqual(item.price, 1_000, f"{item.code} is priced off-scale")
                continue
            expected = {
                pets_weapon_catalog.shop_price_for_bonuses(rarity, item.bonuses.items())
                for rarity in ({"common", "uncommon"} if item.rarity == "common" else {item.rarity})
            }
            self.assertIn(item.price, expected)
            self.assertLessEqual(item.price, 195, f"{item.code} is priced off-scale")

    def test_core_purchase_refuses_weapon_outside_daily_window(self):
        entry = "shop-chat"
        self._two_pets(entry)
        offered = next(item for item in pets.daily_storefront_weapons(entry, user_id="1")
                       if item.rarity not in {"rare", "legendary"})
        on_sale = {item.code for item in pets.daily_storefront_weapons(entry, user_id="1")}
        outside = next(item for item in pets_config.items_for_slot("weapon", "shop")
                       if item.code not in on_sale)
        economy.grant(entry, "1", offered.price + outside.price, "test")
        self.assertTrue(pets.buy_item(entry, "1", 0, offered.code)[0])
        ok, note = pets.buy_item(entry, "1", 0, outside.code)
        self.assertFalse(ok)
        self.assertIn("витрин", note)

    def test_shop_purchase_does_not_remove_another_players_personal_store(self):
        entry = "shop-chat"
        self._two_pets(entry)
        item = next(weapon for weapon in pets.daily_storefront_weapons(entry, user_id="1")
                    if weapon.rarity == "common")
        economy.grant(entry, "1", item.price, "test")
        self.assertTrue(pets.buy_item(entry, "1", 0, item.code)[0])
        first_player_stock = pets.daily_storefront_weapons(entry, user_id="1")
        second_player_stock = pets.daily_storefront_weapons(entry, user_id="2")
        self.assertEqual(len(first_player_stock), pets_config.DAILY_STOREFRONT_SIZE - 1)
        self.assertNotIn(item.code, {weapon.code for weapon in first_player_stock})
        self.assertEqual(len(second_player_stock), pets_config.DAILY_STOREFRONT_SIZE)

    def test_buying_every_offer_leaves_the_personal_store_empty_until_refresh(self):
        entry = "empty-shop"
        self._two_pets(entry)
        moment = datetime(2026, 8, 8, 3)
        with patch("pets.app_now", return_value=moment):
            stock = pets.daily_storefront_weapons(entry, moment, user_id="1")
            economy.grant(entry, "1", sum(item.price for item in stock), "test")
            for expected_left, item in zip(range(len(stock) - 1, -1, -1), stock):
                self.assertTrue(pets.buy_item(entry, "1", 0, item.code)[0])
                self.assertEqual(
                    len(pets.daily_storefront_weapons(entry, moment, user_id="1")),
                    expected_left,
                )

        refreshed = pets.daily_storefront_weapons(entry, moment.replace(hour=12), user_id="1")
        self.assertEqual(len(refreshed), pets_config.DAILY_STOREFRONT_SIZE)

    def test_discovery_survives_sale_and_gift_and_old_inventory_migrates(self):
        entry = "shop-chat"
        self._two_pets(entry)
        first, second = [item for item in pets.daily_storefront_weapons(entry, user_id="1")
                         if item.rarity not in {"rare", "legendary"}][:2]
        economy.grant(entry, "1", first.price + second.price, "test")
        self.assertTrue(pets.buy_item(entry, "1", 0, first.code)[0])
        self.assertTrue(pets.buy_item(entry, "1", 0, second.code)[0])
        self.assertTrue(pets.sell_item(entry, "1", first.code)[0])
        self.assertTrue(pets.gift_item(entry, "1", "2", second.code)[0])
        giver = pets.get_pet(entry, "1")
        receiver = pets.get_pet(entry, "2")
        self.assertIn(first.code, giver["discovered"])
        self.assertIn(second.code, giver["discovered"])
        self.assertIn(second.code, receiver["discovered"])
        self.assertIn("Открыто", pets_ui.collection_view(entry, "1", 0)[0])

    def test_lock_blocks_sale_and_gift(self):
        entry = "shop-chat"
        self._two_pets(entry)
        item = next(item for item in pets.daily_storefront_weapons(entry, user_id="1")
                    if item.rarity not in {"rare", "legendary"})
        economy.grant(entry, "1", item.price, "test")
        self.assertTrue(pets.buy_item(entry, "1", 0, item.code)[0])
        self.assertTrue(pets.toggle_item_lock(entry, "1", item.code)[0])
        self.assertFalse(pets.sell_item(entry, "1", item.code)[0])
        self.assertFalse(pets.gift_item(entry, "1", "2", item.code)[0])
        self.assertTrue(pets.toggle_item_lock(entry, "1", item.code)[0])
        self.assertTrue(pets.gift_item(entry, "1", "2", item.code)[0])

    def test_rare_actions_require_one_time_server_confirmation(self):
        entry = "shop-chat"
        self._two_pets(entry)
        rare = next(item for item in pets_config.items_for_slot("weapon") if item.rarity == "rare")
        data = pets._load(entry)
        data["pets"]["1"]["inventory"].append(rare.code)
        pets._save(entry, data)
        self.assertFalse(pets.sell_item(entry, "1", rare.code)[0])
        ok, _, token = pets.begin_item_confirmation(entry, "1", "sell", rare.code)
        self.assertTrue(ok)
        self.assertTrue(pets.sell_item(entry, "1", rare.code, token)[0])
        self.assertFalse(pets.sell_item(entry, "1", rare.code, token)[0])

    def test_store_collection_filters_and_callbacks_fit_telegram_limit(self):
        entry = "shop-chat"
        self._two_pets(entry)
        screens = [
            pets_ui.store_view(entry, "1", 0),
            pets_ui.store_view(entry, "1", 0, "common"),
            pets_ui.collection_view(entry, "1", 0, "rare,1"),
        ]
        for _, keyboard in screens:
            callbacks = [button["callback_data"] for row in keyboard["inline_keyboard"] for button in row]
            self.assertTrue(all(len(value.encode("utf-8")) <= pets_ui.MAX_CALLBACK_BYTES for value in callbacks))
        rare = next(item for item in pets_config.items_for_slot("weapon") if item.rarity == "rare")
        data = pets._load(entry)
        data["pets"]["1"]["inventory"].append(rare.code)
        pets._save(entry, data)
        ok, _, token = pets.begin_item_confirmation(entry, "1", "sell", rare.code)
        self.assertTrue(ok)
        _, keyboard = pets_ui.item_confirmation_view(entry, "1", 0, "sell", rare.code, token)
        callbacks = [button["callback_data"] for row in keyboard["inline_keyboard"] for button in row]
        self.assertTrue(all(len(value.encode("utf-8")) <= pets_ui.MAX_CALLBACK_BYTES for value in callbacks))

    def test_store_separates_each_weapon_with_a_blank_line(self):
        entry = "shop-chat"
        self._two_pets(entry)
        text, _ = pets_ui.store_view(entry, "1", 0)
        visible_names = [
            item.name for item in pets.daily_storefront_weapons(entry, user_id="1")
        ]
        for current, following in zip(visible_names, visible_names[1:]):
            separator = text.index("\n\n", text.index(current))
            self.assertLess(separator, text.index(following))

    def test_store_numbers_every_item_and_groups_purchase_numbers_in_three_rows(self):
        entry = "shop-chat"
        self._two_pets(entry)
        stock = pets.daily_storefront_weapons(entry, user_id="1")
        text, keyboard = pets_ui.store_view(entry, "1", 0)

        for number, item in enumerate(stock, 1):
            self.assertIn(f"<b>{number}. {item.name}</b>", text)
        purchase_rows = [
            row for row in keyboard["inline_keyboard"]
            if row and all(":buy:" in button["callback_data"] for button in row)
        ]
        self.assertEqual(len(purchase_rows), 3)
        self.assertEqual(
            [button["text"] for row in purchase_rows for button in row],
            [str(number) for number in range(1, len(stock) + 1)],
        )
        self.assertTrue(all(len(row) <= 6 for row in purchase_rows))

    def test_store_uses_stat_and_coin_icons_with_price_on_its_own_line(self):
        entry = "shop-chat"
        self._two_pets(entry)
        item = pets.daily_storefront_weapons(entry, user_id="1")[0]
        text, _ = pets_ui.store_view(entry, "1", 0)
        item_start = text.index(item.name)
        item_end = text.find("\n\n", item_start)
        block = text[item_start:item_end]

        self.assertIn(f"🪙 {pets_ui._money(item.price)}", block)
        self.assertNotIn(f"{item.price} монет", block)
        for key, value in item.bonuses.items():
            emoji = pets_config.ARMOR_EMOJI if key == "armor" else pets_config.STAT_EMOJI[key]
            self.assertIn(f"{emoji} {value:+d}", block)
            label = pets_config.ARMOR_NAME if key == "armor" else pets_config.STAT_NAMES[key]
            self.assertNotIn(label, block)
        lines = block.splitlines()
        self.assertTrue(any(line.startswith("🪙 ") for line in lines[1:]))

    def test_amulet_shop_lists_each_stat_and_effect_before_buying(self):
        self._two_pets("amulet-shelf")
        text, _ = pets_ui.shop_slot_view("amulet-shelf", "1", 0, "amulet")
        stock = pets.daily_storefront_items("amulet-shelf", "amulet", user_id="1")
        self.assertEqual(len(stock), pets_config.DAILY_STOREFRONT_SIZE)
        for item in stock:
            with self.subTest(item=item.code):
                if item.effect:
                    self.assertIn(item.effect["text"], text)
                for key, value in item.bonuses.items():
                    emoji = pets_config.ARMOR_EMOJI if key == "armor" else pets_config.STAT_EMOJI[key]
                    self.assertIn(f"{emoji} {value:+d}", text)

    def test_weapon_shop_lists_the_modifier_before_buying(self):
        self._two_pets("weapon-effect-shelf")
        weapon = next(
            item for item in pets_config.items_for_slot("weapon", "shop")
            if getattr(item, "effect", {}).get("code") == "precision"
        )
        with patch.object(pets, "daily_storefront_weapons", return_value=(weapon,)):
            text, _ = pets_ui.store_view("weapon-effect-shelf", "1", 0)
        self.assertIn(weapon.effect["text"], text)
        for key, value in weapon.bonuses.items():
            emoji = pets_config.ARMOR_EMOJI if key == "armor" else pets_config.STAT_EMOJI[key]
            self.assertIn(f"{emoji} {value:+d}", text)

    def test_collection_lists_only_chat_discoveries_and_their_current_owners(self):
        entry = "shop-chat"
        self._two_pets(entry)
        first, second = pets.daily_storefront_weapons(entry, user_id="1")[:2]
        hidden = next(
            item for item in pets_config.items_for_slot("weapon")
            if item.code not in {first.code, second.code}
        )
        data = pets._load(entry)
        data["pets"]["1"]["owner_username"] = "alice"
        data["pets"]["1"]["discovered"] = [first.code, second.code]
        data["pets"]["1"]["inventory"] = [first.code]
        data["pets"]["2"]["owner_username"] = "bob"
        data["pets"]["2"]["discovered"] = [second.code]
        data["pets"]["2"]["inventory"] = [second.code]
        pets._save(entry, data)

        text, _ = pets_ui.collection_view(entry, "1", 0)

        self.assertIn(first.name, text)
        self.assertIn(second.name, text)
        self.assertIn("Владелец: @alice", text)
        self.assertIn("Владелец: @bob", text)
        self.assertNotIn(hidden.name, text)
        self.assertNotIn(f"/{len(pets_config.items_for_slot('weapon'))}", text)
        self.assertNotIn("Неизвестное оружие", text)


class RecordFightTests(PetsTestCase):
    def _set_pet_level(self, entry, user_id, level, cage_level=1):
        data = pets._load(entry)
        record = data["pets"][str(user_id)]
        record["level"] = level
        record["xp"] = 0
        record["cage_level"] = cage_level
        pets._save(entry, data)

    def test_win_rewards_follow_capped_level_difference_curve(self):
        """Gold and XP reward a harder win, while weak-target farming is capped.

        Gold is derived from WIN_GOLD_MAX and the shared multiplier table rather than
        written out, so re-tuning the arena payout (5-10 -> 15-30, and whatever comes
        after) re-states this test instead of breaking it. The multiplier CURVE is what is
        being pinned here; the size of the pot is a balance knob.
        """
        cases = {
            delta: (
                expected_xp,
                pets_config.gold_for_hero(
                    round(pets_config.WIN_GOLD_MAX
                          * pets_config.arena_level_reward_multiplier(10 + delta, 10)),
                    10 + delta, "arena",
                ),
            )
            for delta, expected_xp in {
                -3: 125, -2: 116, 0: 100, 2: 85, 3: 75,
                9: 75,  # the +3 stronger-winner penalty is the cap
            }.items()
        }
        result = SimpleNamespace(winner="1", loser="2")

        for delta, (expected_xp, expected_gold) in cases.items():
            with self.subTest(delta=delta):
                entry = f"level-delta-{delta}"
                self._tame(entry, "1", "Attacker")
                self._tame(entry, "2", "Defender")
                self._set_pet_level(entry, "1", 10 + delta)
                self._set_pet_level(entry, "2", 10)

                with patch("random.randint", return_value=pets_config.WIN_GOLD_MAX), \
                     patch("random.random", return_value=1.0):
                    outcome = pets.record_fight(entry, "1", "2", result, date(2026, 8, 1))

                self.assertEqual(outcome["xp"], expected_xp)
                self.assertEqual(outcome["gold"], expected_gold)
                self.assertEqual(pets.get_pet(entry, "1")["xp"], expected_xp)
                self.assertEqual(economy.balance(entry, "1", 0), expected_gold)

    def test_level_scaled_gold_composes_with_the_cage_bonus(self):
        entry = "cage-and-level-reward"
        self._tame(entry, "1", "Attacker")
        self._tame(entry, "2", "Defender")
        self._set_pet_level(entry, "1", 13, cage_level=5)
        self._set_pet_level(entry, "2", 10)
        result = SimpleNamespace(winner="1", loser="2")

        with patch("random.randint", return_value=pets_config.WIN_GOLD_MAX), \
             patch("random.random", return_value=1.0):
            outcome = pets.record_fight(entry, "1", "2", result, date(2026, 8, 1))

        # Cage bonus (+25% at level 5) composes with the +3 level-difference penalty.
        # Both factors are named rather than folded into a literal, so the arena payout
        # can be re-tuned without this test having to be re-derived by hand.
        self.assertEqual(
            outcome["gold"],
            pets_config.gold_for_hero(
                round(pets_config.WIN_GOLD_MAX
                      * (1 + pets_config.CAGE_GOLD_BONUS_PCT[4] / 100)
                      * pets_config.arena_level_reward_multiplier(13, 10)),
                13, "arena",
            ),
        )

    def test_luck_raises_the_find_chance_on_a_saturating_curve(self):
        """The shape is the point, not any single number.

        Monotonic so a point of luck is never wasted, saturating so it cannot run away,
        and worth having at a level somebody actually reaches -- half the maximum bonus
        by luck 50, not only at the 6,896 coins it costs to reach 80.
        """
        curve = [pets_config.luck_drop_multiplier(luck) for luck in range(0, 81)]
        self.assertEqual(curve[0], 1.0)
        self.assertTrue(all(b >= a for a, b in zip(curve, curve[1:])))
        self.assertLess(curve[-1], 1 + pets_config.LUCK_DROP_BONUS_MAX)
        # Half the maximum bonus is reached exactly at K, by construction.
        self.assertAlmostEqual(
            pets_config.luck_drop_multiplier(pets_config.LUCK_DROP_K),
            1 + pets_config.LUCK_DROP_BONUS_MAX / 2,
        )
        # "Considerable, not a lot": a dedicated luck build lands between +40% and +60%
        # relative, and a starting pet gets effectively nothing.
        self.assertLess(curve[1] - 1, 0.05)
        self.assertGreater(curve[80] - 1, 0.40)
        self.assertLess(curve[80] - 1, 0.60)

    def test_luck_lifts_the_arena_drop_rate_measurably(self):
        """A behavioural check, not just arithmetic: the winner's luck is what pays."""
        entry = "luck-drops"
        self._tame(entry, "1", "Attacker")
        self._tame(entry, "2", "Defender")
        result = SimpleNamespace(winner="1", loser="2")
        base = pets_config.DROP_CHANCE
        lucky = base * pets_config.luck_drop_multiplier(80)
        self.assertGreater(lucky, base)

        # A roll that lands between the two thresholds drops for a lucky pet and not for
        # an unlucky one -- the cleanest statement that luck is actually consulted.
        between = (base + lucky) / 2
        with patch("random.random", return_value=between), \
             patch("random.randint", return_value=pets_config.WIN_GOLD_MAX):
            outcome = pets.record_fight(entry, "1", "2", result, date(2026, 8, 1))
        self.assertIsNone(outcome.get("dropped_item"))

        data = pets._load(entry)
        data["pets"]["1"]["stats"]["luck"] = 80
        pets._save(entry, data)
        with patch("random.random", return_value=between), \
             patch("random.randint", return_value=pets_config.WIN_GOLD_MAX):
            outcome = pets.record_fight(entry, "1", "2", result, date(2026, 8, 2))
        self.assertIsNotNone(outcome.get("dropped_item"))

    def test_draw_consumes_one_fight_without_gold_or_a_win(self):
        entry = "chat"
        self._tame(entry, "1", "Attacker")
        self._tame(entry, "2", "Defender")
        result = SimpleNamespace(
            winner=None, loser=None, is_draw=True, seed=123,
            total_damage={"1": 200, "2": 200},
        )

        outcome = pets.record_fight(entry, "1", "2", result, date(2026, 8, 1))

        self.assertTrue(outcome["draw"])
        self.assertEqual(outcome["gold"], 0)
        self.assertEqual(outcome["loss_gold"], 0)
        # A draw has no winner to have taken anything from, so there is nothing to pay a
        # penalty on or to console anyone for -- both stay at zero, exactly like gold.
        self.assertEqual(outcome["consolation_gold"], 0)
        self.assertEqual(outcome["xp"], pets_config.DRAW_XP)
        self.assertEqual(pets.get_pet(entry, "1")["wins"], 0)
        self.assertEqual(pets.get_pet(entry, "2")["wins"], 0)
        row = pets.history(entry, "1")[0]
        self.assertTrue(row["draw"])
        self.assertEqual(row["combat_seed"], 123)

    def test_attacker_alone_consumes_a_fight_and_defender_still_gets_loss_xp(self):
        entry = "chat"
        self._tame(entry, "1", "Attacker")
        self._tame(entry, "2", "Defender")
        today = date(2026, 8, 1)

        defender_fight_bank_before = pets.get_pet(entry, "2")["fight_bank"]
        defender_xp_before = pets.get_pet(entry, "2")["xp"]
        result = SimpleNamespace(winner="2", loser="1")  # attacker loses this one

        with patch("random.randint", return_value=pets_config.WIN_GOLD_MIN), \
             patch("random.random", return_value=1.0):  # no item drop
            outcome = pets.record_fight(entry, "1", "2", result, today)

        # Attacker's accumulated bank went down by exactly one.
        self.assertEqual(
            pets.fights_left(entry, "1", today),
            pets.daily_allowance(entry, "1", today) - 1,
        )
        # Defender's own bank is completely untouched.
        self.assertEqual(pets.get_pet(entry, "2")["fight_bank"], defender_fight_bank_before)

        # The returned dict is the ATTACKER's own outcome; attacker lost here.
        self.assertEqual(outcome["gold"], 0)
        self.assertEqual(outcome["xp"], pets_config.LOSS_XP)
        self.assertIsNone(outcome["dropped_item"])
        self.assertEqual(outcome["level"], pets.get_pet(entry, "1")["level"])

        # Loser still gained C.LOSS_XP even though they lost.
        self.assertGreater(pets.get_pet(entry, "1")["xp"] + 0, 0)
        self.assertNotEqual(pets.get_pet(entry, "2")["xp"], defender_xp_before)  # winner leveled/xp moved

        # Winner (defender "2") got the gold and a win on the board.
        self.assertEqual(economy.balance(entry, "2", 0), pets_config.WIN_GOLD_MIN)
        self.assertEqual(pets.get_pet(entry, "2")["wins"], 1)
        self.assertEqual(pets.get_pet(entry, "1")["wins"], 0)

    def test_empty_bank_rejects_fights_without_mutation(self):
        entry = "empty-bank"
        self._tame(entry, "1", "Attacker")
        self._tame(entry, "2", "Defender")
        moment = datetime(2026, 8, 1, 12, 0)
        data = pets._load(entry)
        data["pets"]["1"].update({
            "fight_bank": 0, "fight_bank_cap": 5,
            "fight_bank_checkpoint": moment.isoformat(),
        })
        pets._save(entry, data)
        result = SimpleNamespace(winner="1", loser="2")

        with self.assertRaisesRegex(ValueError, "No accumulated"):
            pets.record_fight(entry, "1", "2", result, moment.date(), now=moment)
        self.assertEqual(pets.get_pet(entry, "1")["fights"], 0)
        self.assertEqual(pets.get_pet(entry, "2")["fights"], 0)
        self.assertEqual(pets.history(entry, "1"), [])

    def test_record_fight_can_roll_a_drop_item_for_the_winner_only(self):
        entry = "chat"
        self._tame(entry, "1", "Attacker")
        self._tame(entry, "2", "Defender")
        drop_item = next(item for item in pets_config.ITEMS if item.source == "drop")
        result = SimpleNamespace(winner="1", loser="2")

        with patch("random.randint", return_value=pets_config.WIN_GOLD_MIN), \
             patch("random.random", return_value=0.0), \
             patch("random.choice", return_value=drop_item):
            outcome = pets.record_fight(entry, "1", "2", result, date(2026, 8, 1))

        self.assertEqual(outcome["dropped_item"], drop_item.code)
        self.assertIn(drop_item.code, pets.get_pet(entry, "1")["inventory"])
        self.assertNotIn(drop_item.code, pets.get_pet(entry, "2")["inventory"])

    def test_collector_amulet_increases_item_drop_chance_by_twenty_five_percent(self):
        entry = "collector-chat"
        self._tame(entry, "1", "Attacker")
        self._tame(entry, "2", "Defender")
        collector = next(
            item for item in pets_config.ITEMS
            if getattr(item, "effect", {}).get("code") == "collector"
        )
        drop_item = next(
            item for item in pets_config.ITEMS
            if item.source == "drop" and item.slot == "boots"
        )
        data = pets._load(entry)
        data["pets"]["1"]["inventory"] = [collector.code]
        data["pets"]["1"]["equipped"]["amulet"] = collector.code
        pets._save(entry, data)
        result = SimpleNamespace(winner="1", loser="2")

        # 9% misses the ordinary 8% roll but lands inside Collector's 10% roll.
        with patch("random.randint", return_value=pets_config.WIN_GOLD_MIN), \
             patch("random.random", return_value=0.09), \
             patch("random.choice", return_value=drop_item):
            outcome = pets.record_fight(entry, "1", "2", result, date(2026, 8, 1))

        self.assertEqual(outcome["dropped_item"], drop_item.code)

    def test_coin_rake_adds_only_capped_landed_hit_gold(self):
        entry = "coin-rake-chat"
        self._tame(entry, "1", "Attacker")
        self._tame(entry, "2", "Defender")
        rake = next(
            item for item in pets_config.ITEMS
            if getattr(item, "effect", {}).get("code") == "coin_rake"
            and item.rarity == "legendary"
        )
        data = pets._load(entry)
        data["pets"]["1"]["inventory"] = [rake.code]
        data["pets"]["1"]["equipped"]["weapon"] = rake.code
        pets._save(entry, data)
        rounds = [
            SimpleNamespace(attacker="1", number=index, event="hit", damage=10)
            for index in range(1, 9)
        ]
        rounds.extend((
            SimpleNamespace(attacker="1", number=8, event="amulet_burn", damage=99),
            SimpleNamespace(attacker="1", number=9, event="dodge", damage=0),
        ))
        result = SimpleNamespace(winner="1", loser="2", rounds=rounds)

        with patch("random.randint", return_value=10), patch("random.random", return_value=1.0):
            outcome = pets.record_fight(entry, "1", "2", result, date(2026, 8, 1))

        # Eight ordinary landed hits; the burn row and the dodge must not be counted.
        # Read the cap off the catalogue rather than pinning a total, so a balance pass
        # cannot make this test fail for the one reason it is not testing.
        expected = 10 + min(int(rake.effect["cap"]), 8 * int(rake.effect["value"]))
        self.assertEqual(outcome["gold"], expected)
        self.assertEqual(economy.balance(entry, "1", 0), expected)

    def test_survivor_amulet_preserves_thirty_percent_of_the_attackers_loss_penalty(self):
        """Survivor only ever discounts a penalty, and only the ATTACKER pays one now (a
        defender is paid a consolation instead, never a penalty -- see the tests below),
        so this amulet only does anything when the ATTACKER is the one wearing and losing."""
        entry = "survivor-chat"
        self._tame(entry, "1", "Attacker")
        self._tame(entry, "2", "Defender")
        survivor = next(
            item for item in pets_config.ITEMS
            if getattr(item, "effect", {}).get("code") == "survivor"
        )
        data = pets._load(entry)
        data["pets"]["1"]["inventory"] = [survivor.code]
        data["pets"]["1"]["equipped"]["amulet"] = survivor.code
        pets._save(entry, data)
        economy.grant(entry, "1", 100, "test")
        result = SimpleNamespace(winner="2", loser="1")  # attacker loses, wearing Survivor

        with patch("random.randint", return_value=10), patch("random.random", return_value=1.0):
            outcome = pets.record_fight(entry, "1", "2", result, date(2026, 8, 1))

        self.assertEqual(pets_config.loss_gold_for(10), 3)
        self.assertEqual(outcome["loss_gold"], 2)
        self.assertEqual(outcome["consolation_gold"], 0)
        self.assertEqual(economy.balance(entry, "1", 0), 98)

    def test_survivor_amulet_does_not_touch_a_defenders_consolation(self):
        """`_equipped_effect(loser, "survivor")` is only read inside the attacker-lost
        branch of record_fight; a losing defender's consolation is a flat share of the
        winner's gold with no equipment discount, because there is no penalty left on
        that side for a piece of gear to shrink."""
        entry = "survivor-defender-chat"
        self._tame(entry, "1", "Attacker")
        self._tame(entry, "2", "Defender")
        survivor = next(
            item for item in pets_config.ITEMS
            if getattr(item, "effect", {}).get("code") == "survivor"
        )
        data = pets._load(entry)
        data["pets"]["2"]["inventory"] = [survivor.code]
        data["pets"]["2"]["equipped"]["amulet"] = survivor.code
        pets._save(entry, data)
        result = SimpleNamespace(winner="1", loser="2")  # defender loses, wearing Survivor

        with patch("random.randint", return_value=10), patch("random.random", return_value=1.0):
            outcome = pets.record_fight(entry, "1", "2", result, date(2026, 8, 1))

        self.assertEqual(pets_config.defender_consolation_for(10), 2)
        self.assertEqual(outcome["opponent_consolation_gold"], 2)
        self.assertEqual(outcome["opponent_loss_gold"], 0)
        self.assertEqual(economy.balance(entry, "2", 0), 2)

    def test_attacker_who_loses_pays_the_same_penalty_as_before_this_change(self):
        """Regression pin: the attacker side of record_fight must be byte-for-byte
        unchanged by adding the defender consolation."""
        entry = "attacker-pays-chat"
        self._tame(entry, "1", "Attacker")
        self._tame(entry, "2", "Defender")
        economy.grant(entry, "1", 1000, "test")
        before = economy.balance(entry, "1", 0)
        result = SimpleNamespace(winner="2", loser="1")

        with patch("random.randint", return_value=pets_config.WIN_GOLD_MAX), \
             patch("random.random", return_value=1.0):
            outcome = pets.record_fight(entry, "1", "2", result, date(2026, 8, 1))

        expected_penalty = pets_config.loss_gold_for(pets_config.WIN_GOLD_MAX)
        self.assertGreater(expected_penalty, 0)
        self.assertEqual(outcome["loss_gold"], expected_penalty)
        self.assertEqual(outcome["consolation_gold"], 0)
        self.assertEqual(economy.balance(entry, "1", 0), before - expected_penalty)

    def test_defender_who_loses_pays_nothing_and_receives_a_consolation_instead(self):
        """The core of this change: a defender never chose the fight, so a loss must not
        cost them anything, and DEFENDER_CONSOLATION_SHARE mints a small amount onto their
        balance instead -- persisted so history() can show the same story later."""
        entry = "defender-consoled-chat"
        self._tame(entry, "1", "Attacker")
        self._tame(entry, "2", "Defender")
        self.assertEqual(economy.balance(entry, "2", 0), 0)
        result = SimpleNamespace(winner="1", loser="2")

        with patch("random.randint", return_value=pets_config.WIN_GOLD_MAX), \
             patch("random.random", return_value=1.0):
            outcome = pets.record_fight(entry, "1", "2", result, date(2026, 8, 1))

        expected_consolation = pets_config.defender_consolation_for(pets_config.WIN_GOLD_MAX)
        self.assertGreater(expected_consolation, 0)
        self.assertEqual(outcome["opponent_loss_gold"], 0)
        self.assertEqual(outcome["opponent_consolation_gold"], expected_consolation)
        self.assertEqual(economy.balance(entry, "2", 0), expected_consolation)

        row = pets.history(entry, "2")[0]
        self.assertEqual(row["loss_gold"], 0)
        self.assertEqual(row["consolation_gold"], expected_consolation)

    def test_defender_with_zero_balance_is_never_driven_negative_by_a_loss(self):
        """A losing defender is only ever credited (economy.grant), never debited, so an
        empty wallet needs no clamp here -- unlike the attacker's penalty path, there is
        nothing that could push the balance below zero."""
        entry = "defender-zero-balance-chat"
        self._tame(entry, "1", "Attacker")
        self._tame(entry, "2", "Defender")
        self.assertEqual(economy.balance(entry, "2", 0), 0)
        result = SimpleNamespace(winner="1", loser="2")

        with patch("random.randint", return_value=pets_config.WIN_GOLD_MIN), \
             patch("random.random", return_value=1.0):
            pets.record_fight(entry, "1", "2", result, date(2026, 8, 1))

        after = economy.balance(entry, "2", 0)
        self.assertGreaterEqual(after, 0)
        self.assertEqual(after, pets_config.defender_consolation_for(pets_config.WIN_GOLD_MIN))

    def test_winners_gold_is_identical_whichever_side_loses(self):
        """The consolation is minted, not carved out of the winner's reward -- same as the
        attacker's penalty was never paid TO the winner either. Whether the loser is the
        attacker or the defender, the winner's own payout must not move."""
        golds = {}
        for label, result, winner_uid in (
            ("defender_loses", SimpleNamespace(winner="1", loser="2"), "1"),
            ("attacker_loses", SimpleNamespace(winner="2", loser="1"), "2"),
        ):
            entry = f"winner-gold-{label}"
            self._tame(entry, "1", "Attacker")
            self._tame(entry, "2", "Defender")
            with patch("random.randint", return_value=pets_config.WIN_GOLD_MAX), \
                 patch("random.random", return_value=1.0):
                pets.record_fight(entry, "1", "2", result, date(2026, 8, 1))
            golds[label] = economy.balance(entry, winner_uid, 0)

        self.assertEqual(golds["defender_loses"], pets_config.WIN_GOLD_MAX)
        self.assertEqual(golds["defender_loses"], golds["attacker_loses"])

    def test_drop_pool_excludes_accessory_already_owned_by_winner(self):
        entry = "accessory-drop-chat"
        self._tame(entry, "1", "Attacker")
        self._tame(entry, "2", "Defender")
        first, second = [
            item for item in pets_config.ITEMS
            if item.source == "drop" and item.slot == "boots"
        ][:2]
        data = pets._load(entry)
        data["pets"]["1"]["inventory"] = [first.code]
        pets._save(entry, data)
        result = SimpleNamespace(winner="1", loser="2")

        with patch("random.randint", return_value=pets_config.WIN_GOLD_MIN), \
             patch("random.random", return_value=0.0), \
             patch("random.choice", return_value=second) as choose:
            outcome = pets.record_fight(entry, "1", "2", result, date(2026, 8, 1))

        self.assertEqual(outcome["dropped_item"], second.code)
        self.assertNotIn(first, choose.call_args.args[0])

    def test_drop_auto_equips_empty_or_better_slot_but_keeps_stronger_item(self):
        weak = pets_config.find_item("bt01")
        strong = pets_config.find_item("bt30")
        self.assertGreater(pets_config.equipment_score(strong), pets_config.equipment_score(weak))
        result = SimpleNamespace(winner="1", loser="2")

        for entry, current, dropped, expected, auto_equipped in (
            ("empty-slot", None, weak, weak, True),
            ("better-drop", weak, strong, strong, True),
            ("worse-drop", strong, weak, strong, False),
        ):
            with self.subTest(entry=entry):
                self._tame(entry, "1", "Attacker")
                self._tame(entry, "2", "Defender")
                if current:
                    data = pets._load(entry)
                    data["pets"]["1"]["inventory"] = [current.code]
                    data["pets"]["1"]["equipped"]["boots"] = current.code
                    pets._save(entry, data)
                with patch("random.randint", return_value=pets_config.WIN_GOLD_MIN), \
                     patch("random.random", return_value=0.0), \
                     patch("random.choice", return_value=dropped):
                    outcome = pets.record_fight(
                        entry, "1", "2", result, date(2026, 8, 1),
                    )

                pet = pets.get_pet(entry, "1")
                self.assertEqual(pet["equipped"]["boots"], expected.code)
                self.assertIn(dropped.code, pet["inventory"])
                self.assertEqual(outcome["auto_equipped"], auto_equipped)

    def test_drop_pool_excludes_code_owned_by_another_player(self):
        entry = "chat"
        self._tame(entry, "1", "Attacker")
        self._tame(entry, "2", "Defender")
        first, second = [
            item for item in pets_config.ITEMS
            if item.source == "drop" and item.slot == "weapon" and item.rarity != "legendary"
        ][:2]
        data = pets._load(entry)
        data["pets"]["2"]["inventory"] = [first.code]
        pets._save(entry, data)
        result = SimpleNamespace(winner="1", loser="2")

        with patch("random.randint", return_value=pets_config.WIN_GOLD_MIN), \
             patch("random.random", return_value=0.0), \
             patch("random.choice", return_value=second) as choose:
            outcome = pets.record_fight(entry, "1", "2", result, date(2026, 8, 1))

        self.assertEqual(outcome["dropped_item"], second.code)
        self.assertNotIn(first, choose.call_args.args[0])
        winner_inventory = pets.get_pet(entry, "1")["inventory"]
        self.assertNotIn(first.code, winner_inventory)
        self.assertEqual(winner_inventory.count(second.code), 1)
        self.assertEqual(pets.get_pet(entry, "2")["inventory"].count(first.code), 1)

    def test_drop_pool_allows_one_legendary_design_for_two_players(self):
        entry = "shared-legendary"
        self._tame(entry, "1", "Attacker")
        self._tame(entry, "2", "Defender")
        legendary = next(
            item for item in pets_config.ITEMS
            if item.source == "drop" and item.slot == "weapon"
            and item.rarity == "legendary" and item.code != pets.REMOVED_MOP_CODE
        )
        data = pets._load(entry)
        data["pets"]["2"]["inventory"] = [legendary.code]
        pets._save(entry, data)
        result = SimpleNamespace(winner="1", loser="2")

        with patch("random.randint", return_value=pets_config.WIN_GOLD_MIN), \
             patch("random.random", return_value=0.0), \
             patch("random.choice", return_value=legendary):
            outcome = pets.record_fight(entry, "1", "2", result, date(2026, 8, 1))

        self.assertEqual(outcome["dropped_item"], legendary.code)
        self.assertIn(legendary.code, pets.get_pet(entry, "1")["inventory"])
        self.assertIn(legendary.code, pets.get_pet(entry, "2")["inventory"])

    def test_history_snapshots_names_and_owners_and_zeroes_gold_on_the_losers_row(self):
        entry = "chat"
        self._tame(entry, "1", "Attacker")
        self._tame(entry, "2", "Defender")
        result = SimpleNamespace(winner="1", loser="2")

        with patch("random.randint", return_value=pets_config.WIN_GOLD_MAX), \
             patch("random.random", return_value=1.0):
            pets.record_fight(entry, "1", "2", result, date(2026, 8, 1))

        attacker_row = pets.history(entry, "1")[0]
        self.assertEqual(attacker_row["attacker_id"], "1")
        self.assertEqual(attacker_row["defender_id"], "2")
        self.assertEqual(attacker_row["winner_id"], "1")
        self.assertEqual(attacker_row["attacker_name"], "Attacker")
        self.assertEqual(attacker_row["defender_name"], "Defender")
        self.assertEqual(attacker_row["attacker_owner"], "Owner1")
        self.assertEqual(attacker_row["defender_owner"], "Owner2")
        self.assertEqual(attacker_row["gold"], pets_config.WIN_GOLD_MAX)
        self.assertIn("ts", attacker_row)

        # Same fight, read from the loser's own history: gold reads zero on their row.
        defender_row = pets.history(entry, "2")[0]
        self.assertEqual(defender_row["gold"], 0)

        # A later rename must not rewrite what already happened.
        pets.rename(entry, "1", "Новое имя")
        self.assertEqual(pets.history(entry, "1")[0]["attacker_name"], "Attacker")

    def test_history_is_newest_first_and_capped_at_history_limit(self):
        entry = "chat"
        self._tame(entry, "1", "Attacker")
        self._tame(entry, "2", "Defender")
        result = SimpleNamespace(winner="1", loser="2")

        start = datetime(2026, 8, 1, 12, 0)
        with patch("random.random", return_value=1.0):
            for day_offset in range(pets_config.HISTORY_LIMIT + 3):
                pets.record_fight(
                    entry, "1", "2", result, date(2026, 8, 1) + timedelta(days=day_offset),
                    now=start + timedelta(hours=day_offset),
                )

        rows = pets.history(entry, "1")
        self.assertEqual(len(rows), pets_config.HISTORY_LIMIT)
        # Newest first: the last recorded date sorts to index 0.
        dates = [row["date"] for row in rows]
        self.assertEqual(dates, sorted(dates, reverse=True))


class PityGiftAndTelemetryTests(PetsTestCase):
    def _two_pets(self, entry="chat"):
        self._tame(entry, "1", "One")
        self._tame(entry, "2", "Two")

    def _level(self, entry, user_id, level):
        data = pets._load(entry)
        data["pets"][str(user_id)]["level"] = level
        data["pets"][str(user_id)]["xp"] = 0
        pets._save(entry, data)

    def test_sixth_empty_arena_win_forces_an_ordinary_item_drop(self):
        self._two_pets()
        data = pets._load("chat")
        data["pets"]["1"]["item_pity_wins"] = pets_config.ITEM_PITY_ELIGIBLE_WINS - 1
        pets._save("chat", data)
        result = SimpleNamespace(winner="1", loser="2")

        with patch("random.randint", return_value=pets_config.WIN_GOLD_MIN), \
             patch("random.random", return_value=1.0):
            outcome = pets.record_fight("chat", "1", "2", result, date(2026, 8, 1))

        self.assertIsNotNone(outcome["dropped_item"])
        self.assertEqual(pets.get_pet("chat", "1").get("item_pity_wins"), 0)

    def test_pity_forces_an_unowned_legendary_at_the_documented_ceiling(self):
        self._two_pets()
        threshold = pets_config.LEGENDARY_PITY_ELIGIBLE_WINS
        data = pets._load("chat")
        data["pets"]["1"]["legendary_pity_wins"] = threshold - 1
        pets._save("chat", data)
        legendary = next(
            item for item in pets_config.ITEMS
            if item.source == "drop" and item.rarity == "legendary"
        )
        result = SimpleNamespace(winner="1", loser="2")
        with patch("random.randint", return_value=pets_config.WIN_GOLD_MIN), \
             patch("random.choice", return_value=legendary):
            outcome = pets.record_fight("chat", "1", "2", result, date(2026, 8, 1))
        self.assertEqual(outcome["dropped_item"], legendary.code)
        self.assertEqual(pets.legendary_pity_progress("chat", "1")["wins_without_legend"], 0)
        self.assertEqual(pets.economy_telemetry("chat")["drops_by_rarity"]["legendary"], 1)

    def test_completed_legendary_set_keeps_normal_drops_and_clears_unreachable_pity(self):
        self._two_pets()
        legends = [item for item in pets_config.ITEMS if item.source == "drop" and item.rarity == "legendary"]
        ordinary = next(item for item in pets_config.ITEMS if item.source == "drop" and item.rarity != "legendary")
        data = pets._load("chat")
        data["pets"]["1"]["inventory"] = [item.code for item in legends]
        data["pets"]["1"]["legendary_pity_wins"] = 123
        pets._save("chat", data)
        result = SimpleNamespace(winner="1", loser="2")
        with patch("random.randint", return_value=pets_config.WIN_GOLD_MIN), \
             patch("random.random", return_value=0.0), \
             patch("random.choice", return_value=ordinary):
            outcome = pets.record_fight("chat", "1", "2", result, date(2026, 8, 1))
        self.assertEqual(outcome["dropped_item"], ordinary.code)
        progress = pets.legendary_pity_progress("chat", "1")
        self.assertFalse(progress["eligible"])
        self.assertEqual(progress["wins_without_legend"], 0)

    def test_legendaries_owned_by_others_do_not_exhaust_a_players_pity_pool(self):
        self._two_pets()
        legends = [
            item for item in pets_config.ITEMS
            if item.source == "drop" and item.slot == "weapon" and item.rarity == "legendary"
        ]
        data = pets._load("chat")
        data["pets"]["1"]["inventory"] = [item.code for item in legends[::2]]
        data["pets"]["2"]["inventory"] = [item.code for item in legends[1::2]]
        data["pets"]["1"]["legendary_pity_wins"] = 123
        pets._save("chat", data)

        progress = pets.legendary_pity_progress("chat", "1")

        self.assertTrue(progress["eligible"])
        self.assertEqual(progress["wins_without_legend"], 123)

    def test_gift_requires_level_records_audit_and_enforces_daily_cooldown(self):
        self._two_pets()
        self._level("chat", "1", pets_config.GIFT_MIN_PET_LEVEL - 1)
        item = pets_config.find_item("w001")
        data = pets._load("chat")
        data["pets"]["1"]["inventory"] = [item.code]
        pets._save("chat", data)
        before = list(pets.get_pet("chat", "1")["inventory"])
        ok, message = pets.gift_item("chat", "1", "2", item.code)
        self.assertFalse(ok)
        self.assertIn(str(pets_config.GIFT_MIN_PET_LEVEL), message)
        self.assertEqual(pets.get_pet("chat", "1")["inventory"], before)

        self._level("chat", "1", pets_config.GIFT_MIN_PET_LEVEL)
        moment = datetime(2026, 8, 8, 12)
        self.assertTrue(pets.gift_item("chat", "1", "2", item.code, now=moment)[0])
        audit = pets.gift_history("chat")
        self.assertEqual(audit[0]["giver_id"], "1")
        self.assertEqual(audit[0]["receiver_id"], "2")
        self.assertEqual(audit[0]["item_code"], item.code)
        self.assertEqual(pets.economy_telemetry("chat")["gifts"], 1)

        data = pets._load("chat")
        data["pets"]["1"]["inventory"].append(item.code)
        data["pets"]["2"]["inventory"].remove(item.code)
        pets._save("chat", data)
        ok, message = pets.gift_item("chat", "1", "2", item.code, now=moment + timedelta(hours=1))
        self.assertFalse(ok)
        self.assertIn("Следующий подарок", message)
        self.assertIn(item.code, pets.get_pet("chat", "1")["inventory"])

    def test_telemetry_tracks_sale_and_arena_gold(self):
        self._two_pets()
        self._level("chat", "1", pets_config.GIFT_MIN_PET_LEVEL)
        item = pets_config.find_item("w001")
        data = pets._load("chat")
        data["pets"]["1"]["inventory"] = [item.code]
        pets._save("chat", data)
        self.assertTrue(pets.sell_item("chat", "1", item.code)[0])
        result = SimpleNamespace(winner="1", loser="2")
        with patch("random.randint", return_value=pets_config.WIN_GOLD_MIN), \
             patch("random.random", return_value=1.0):
            outcome = pets.record_fight("chat", "1", "2", result, date(2026, 8, 8))
        metrics = pets.economy_telemetry("chat")
        self.assertEqual(metrics["item_sale_gold"], pets_config.resale_value(item))
        self.assertEqual(metrics["arena_reward_gold"], outcome["gold"])

    def test_passive_telemetry_credits_once_per_settled_hour(self):
        start = datetime(2026, 8, 8, 10)
        self._tame("chat", "1")
        economy.grant("chat", "1", pets_config.FARM_UPGRADE_COSTS[0], "test")
        self.assertTrue(pets.upgrade_farm("chat", "1", 0, now=start)[0])
        later = start + timedelta(hours=2)
        self.assertEqual(pets.settle_passive_income("chat", "1", now=later)["credited"], 2)
        self.assertEqual(pets.settle_passive_income("chat", "1", now=later)["credited"], 0)
        self.assertEqual(pets.economy_telemetry("chat")["passive_gold_minted"], 2)


class MailTests(PetsTestCase):
    """The mailbox is a READ over three stores that already exist, so these tests write
    through the real game calls (record_fight / gift_item / a settled farm receipt) and
    then assert on what one player is told -- never on a fourth store, because there
    isn't one to get out of step."""

    def _fight(self, entry, attacker, defender, winner, when, gold=None):
        loser = defender if winner == attacker else attacker
        result = SimpleNamespace(winner=winner, loser=loser)
        with patch("random.randint", return_value=gold or pets_config.WIN_GOLD_MIN), \
             patch("random.random", return_value=1.0):     # 1.0 == no drop
            return pets.record_fight(
                entry, attacker, defender, result, when.date(), now=when,
            )

    def test_mail_merges_fights_farm_and_gifts_with_newest_at_bottom(self):
        entry = "chat"
        self._tame(entry, "1", "Мой")
        self._tame(entry, "2", "Чужой")

        self._fight(entry, "1", "2", "1", datetime(2026, 8, 9, 10, 5))
        self._fight(entry, "2", "1", "2", datetime(2026, 8, 9, 11, 30))
        data = pets._load(entry)
        data["pets"]["1"]["farm_notifications"] = [{
            "run_id": "r1", "pet_name": "Мой", "hours": 6, "gold": 64, "xp": 40,
            "levels_gained": 1, "item_code": "w001", "auto_equipped": True,
            "settled_at": datetime(2026, 8, 9, 12, 15).isoformat(), "notified_at": None,
        }]
        data["gift_history"] = [{
            "ts": datetime(2026, 8, 9, 13, 40).isoformat(),
            "giver_id": "2", "receiver_id": "1", "item_code": "w002",
        }]
        pets._save(entry, data)

        rows = pets.mail(entry, "1")
        self.assertEqual([row["kind"] for row in rows],
                         ["attack", "defense", "farm", "gift_in"])
        self.assertEqual([row["at"] for row in rows],
                         ["10.05", "11.30", "12.15", "13.40"])
        self.assertTrue(all(row["day"] == "2026-08-09" for row in rows))

        attack, defense, farm, gift = rows
        self.assertEqual(gift["item_name"], pets_config.find_item("w002").name)
        self.assertEqual(gift["pet_name"], "Чужой")
        self.assertEqual(farm["hours"], 6)
        self.assertEqual(farm["coins"], 64)
        self.assertEqual(farm["xp"], 40)
        self.assertTrue(farm["auto_equipped"])
        self.assertEqual(attack["pet_name"], "Чужой")
        self.assertEqual(attack["outcome"], "win")
        self.assertEqual(defense["outcome"], "loss")

        # The other player's mailbox is the same events from the other side.
        theirs = pets.mail(entry, "2")
        self.assertEqual([row["kind"] for row in theirs],
                         ["defense", "attack", "gift_out"])
        self.assertEqual(theirs[1]["outcome"], "win")

    def test_coins_are_signed_from_the_readers_own_side(self):
        entry = "chat"
        self._tame(entry, "1", "Мой")
        self._tame(entry, "2", "Чужой")
        economy.grant(entry, "1", 500, "test")

        # 1 attacks and loses: an attacker-loser is the only side that ever pays.
        self._fight(entry, "1", "2", "2", datetime(2026, 8, 9, 10, 0))
        mine = pets.mail(entry, "1")[0]
        self.assertEqual(mine["kind"], "attack")
        self.assertLess(mine["coins"], 0)
        self.assertEqual(pets.mail(entry, "2")[0]["coins"], pets.history(entry, "2")[0]["gold"])

        # 2 attacks and loses: 1 defended, lost, and is PAID a consolation rather than
        # charged -- so their line has to read positive.
        self._fight(entry, "2", "1", "2", datetime(2026, 8, 9, 11, 0))
        defended = pets.mail(entry, "1")[-1]
        self.assertEqual(defended["kind"], "defense")
        self.assertEqual(defended["outcome"], "loss")
        self.assertGreater(defended["coins"], 0)

    def test_a_find_rides_on_the_winners_row_and_never_the_losers(self):
        entry = "chat"
        self._tame(entry, "1", "Мой")
        self._tame(entry, "2", "Чужой")
        dropped = next(item for item in pets_config.ITEMS if item.source == "drop")
        result = SimpleNamespace(winner="1", loser="2")
        with patch("random.randint", return_value=pets_config.WIN_GOLD_MIN), \
             patch("random.random", return_value=0.0), \
             patch("random.choice", return_value=dropped):
            pets.record_fight(entry, "1", "2", result, date(2026, 8, 9),
                              now=datetime(2026, 8, 9, 10, 0))

        winner_row = pets.mail(entry, "1")[0]
        self.assertEqual(winner_row["item"], dropped.code)
        self.assertEqual(winner_row["item_name"], dropped.name)
        self.assertEqual(winner_row["item_rarity"], dropped.rarity)
        loser_row = pets.mail(entry, "2")[0]
        self.assertIsNone(loser_row["item"])
        self.assertIsNone(loser_row["item_name"])

    def test_mail_is_capped_and_survives_damaged_rows(self):
        entry = "chat"
        self._tame(entry, "1", "Мой")
        self._tame(entry, "2", "Чужой")
        start = datetime(2026, 8, 1, 9, 0)
        for index in range(pets_config.MAIL_LIMIT + 5):
            self._fight(entry, "1", "2", "1", start + timedelta(hours=index))

        data = pets._load(entry)
        # A row with no usable timestamp cannot be placed in a chronological feed, so it
        # is dropped rather than sorted to an invented position.
        data["fights"].append({"attacker_id": "1", "defender_id": "2", "ts": "not a date"})
        data["gift_history"] = ["nonsense", {"ts": None, "giver_id": "1", "receiver_id": "2"}]
        pets._save(entry, data)

        rows = pets.mail(entry, "1")
        self.assertEqual(len(rows), pets_config.MAIL_LIMIT)
        times = [row["ts"] for row in rows]
        self.assertEqual(times, sorted(times))
        # Capped means the NEWEST kept, not the first thirty written.
        self.assertEqual(rows[-1]["at"], (start + timedelta(hours=pets_config.MAIL_LIMIT + 4)).strftime("%H.%M"))

    def test_mail_view_groups_by_day_and_escapes_names(self):
        entry = "chat"
        self._tame(entry, "1", "Мой")
        self._tame(entry, "2", "Чужой")
        # validate_name refuses angle brackets in a PET name, but an owner name is
        # whatever Telegram says the account is called -- so that is the one that has to
        # be escaped, and the one this asserts on.
        data = pets._load(entry)
        data["pets"]["2"]["owner_name"] = "<b>Злой</b>"
        pets._save(entry, data)
        today = pets.today()
        self._fight(entry, "2", "1", "2",
                    datetime(today.year, today.month, today.day, 9, 15))
        self._fight(entry, "1", "2", "1",
                    datetime(today.year, today.month, today.day, 8, 0) - timedelta(days=1))

        text, keyboard = pets_ui.mail_view(entry, "1")
        self.assertIn("📬", text)
        self.assertIn("Сегодня", text)
        self.assertIn("Вчера", text)
        self.assertIn("09.15", text)
        self.assertIn("&lt;b&gt;Злой&lt;/b&gt;", text)
        self.assertNotIn("<b>Злой</b>", text)
        self.assertIn("На тебя напал", text)
        self.assertIn("Ты напал на", text)
        self.assertTrue(keyboard["inline_keyboard"])

    def test_empty_mailbox_says_so_and_the_menu_links_to_it(self):
        entry = "chat"
        self._tame(entry, "1", "Мой")
        text, _ = pets_ui.mail_view(entry, "1")
        self.assertIn("Пока пусто", text)

        _menu, keyboard = pets_ui.main_view(entry, "1", 0)
        actions = [
            pets_ui.parse_callback(button["callback_data"])[1]
            for row in keyboard["inline_keyboard"] for button in row
            if "callback_data" in button
        ]
        self.assertIn("mail", actions)
        # The pure fight log did not disappear with the menu button -- it moved into the
        # arena screen, which is the only other place it was ever linked from.
        _fight_text, fight_keyboard = pets_ui.fight_view(entry, "1", 0)
        fight_actions = [
            pets_ui.parse_callback(button["callback_data"])[1]
            for row in fight_keyboard["inline_keyboard"] for button in row
            if "callback_data" in button
        ]
        self.assertIn("history", fight_actions)


class FarmTicketTests(PetsTestCase):
    """A ticket buys the WAITING, not the work: it moves the finish line and nothing else,
    so the payout has to come out of settlement exactly as if the pet had stayed."""

    def _farming(self, entry="chat", uid="1", hours=8, start=None):
        # Naive, like every other farm test here: the store round-trips whatever it is
        # given, and FarmTests' own fixtures set the precedent.
        start = start or datetime(2026, 8, 9, 9, 0)
        self._tame(entry, uid)
        economy.grant(entry, uid, pets_config.FARM_UPGRADE_COSTS[0], "test")
        self.assertTrue(pets.upgrade_farm(entry, uid, 0, now=start)[0])
        ok, note = pets.start_farm(entry, uid, hours, now=start)
        self.assertTrue(ok, note)
        return start

    def test_a_ticket_ends_an_eight_hour_shift_in_a_minute_and_still_pays_for_eight(self):
        entry = "chat"
        start = self._farming(entry, "1", hours=8)
        expected = pets.farm_status(entry, "1", now=start)["reward"]
        self.assertGreater(expected["gold"], 0)

        self.assertTrue(pets.grant_farm_ticket(entry, "1", "figurine:1"))
        moment = start + timedelta(minutes=3)
        ok, note = pets.use_farm_ticket(entry, "1", now=moment)
        self.assertTrue(ok, note)
        self.assertIn("8", note)

        # Still farming for the last minute -- and therefore still unable to pick a fight.
        self.assertTrue(pets.is_farming(entry, "1", now=moment))
        status = pets.farm_status(entry, "1", now=moment)
        self.assertEqual(status["seconds_left"], pets_config.FARM_TICKET_SECONDS)
        self.assertEqual(status["planned_hours"], 8)
        self.assertEqual(status["tickets"], 0)

        settled = pets.settle_completed_farms(
            entry, now=moment + timedelta(seconds=pets_config.FARM_TICKET_SECONDS))
        self.assertEqual(len(settled), 1)
        receipt = settled[0]
        self.assertEqual(receipt["hours"], 8)
        # The whole point: the same money the untouched shift was going to pay.
        self.assertEqual(receipt["gold"], expected["gold"])
        self.assertEqual(receipt["xp"], expected["xp"])
        self.assertEqual(receipt["item_code"], expected["item_code"])

    def test_a_ticket_is_not_cancel_farm(self):
        """Cancelling three minutes into an eight-hour shift pays nothing, because nothing
        whole was worked. A ticket at the same moment pays the full eight hours. If these
        two ever converge, the ticket has silently become worthless."""
        entry = "chat"
        start = self._farming(entry, "1", hours=8)
        moment = start + timedelta(minutes=3)

        self.assertTrue(pets.grant_farm_ticket(entry, "1", "a"))
        self.assertTrue(pets.use_farm_ticket(entry, "1", now=moment)[0])
        ticketed = pets.settle_completed_farms(
            entry, now=moment + timedelta(seconds=pets_config.FARM_TICKET_SECONDS))[0]

        second_start = self._farming("other", "1", hours=8)
        cancel_moment = second_start + timedelta(minutes=3)
        self.assertTrue(pets.cancel_farm("other", "1", now=cancel_moment)[0])
        cancelled = pets.get_pet("other", "1")["farm_notifications"][-1]

        self.assertEqual(ticketed["hours"], 8)
        self.assertEqual(cancelled["hours"], 0)
        self.assertGreater(ticketed["gold"], cancelled["gold"])

    def test_a_ticket_is_refused_when_it_would_buy_nothing(self):
        entry = "chat"
        start = self._farming(entry, "1", hours=1)
        self.assertTrue(pets.grant_farm_ticket(entry, "1", "a"))

        # Inside the last minute there is nothing left to cut, so the ticket is kept.
        late = start + timedelta(hours=1) - timedelta(seconds=30)
        ok, note = pets.use_farm_ticket(entry, "1", now=late)
        self.assertFalse(ok, note)
        self.assertEqual(pets.farm_tickets(entry, "1"), 1)
        self.assertFalse(pets.farm_status(entry, "1", now=late)["can_ticket"])

        # And with no shift running at all there is nothing to shorten either.
        pets.settle_completed_farms(entry, now=start + timedelta(hours=2))
        ok, note = pets.use_farm_ticket(entry, "1", now=start + timedelta(hours=2))
        self.assertFalse(ok, note)
        self.assertEqual(pets.farm_tickets(entry, "1"), 1)

    def test_a_ticket_without_one_in_hand_is_refused_and_spends_nothing(self):
        entry = "chat"
        start = self._farming(entry, "1", hours=6)
        ok, note = pets.use_farm_ticket(entry, "1", now=start + timedelta(minutes=1))
        self.assertFalse(ok)
        self.assertIn("покрас", note)
        self.assertEqual(
            pets.farm_status(entry, "1", now=start + timedelta(minutes=1))["seconds_left"],
            int(timedelta(hours=6, minutes=-1).total_seconds()),
        )

    def test_a_replayed_paint_post_does_not_mint_a_second_ticket(self):
        """Listener deliveries are normally exactly once, but a reconnect can replay an
        update -- which is why record_figurine_live refuses to count the same message
        twice, and why the ticket for it is keyed on the same message id."""
        entry = "chat"
        self.assertTrue(pets.grant_farm_ticket(entry, "7", "figurine:100"))
        self.assertFalse(pets.grant_farm_ticket(entry, "7", "figurine:100"))
        self.assertEqual(pets.farm_tickets(entry, "7"), 1)

        self.assertTrue(pets.grant_farm_ticket(entry, "7", "figurine:101"))
        self.assertEqual(pets.farm_tickets(entry, "7"), 2)
        # Someone with no pet still banks them -- painting is what earns a ticket, and a
        # cage bought next week must not cost them the ones they already have.
        self.assertIsNone(pets.get_pet(entry, "7"))
        self.assertEqual(pets.farm_status(entry, "7")["tickets"], 2)

    def test_the_farm_screen_offers_the_button_only_while_it_would_work(self):
        entry = "chat"
        # farm_view takes no clock of its own, so this one has to run against the real
        # one -- the shift must genuinely still be in progress as the screen renders.
        self._farming(entry, "1", hours=8, start=app_time.now())
        _text, keyboard = pets_ui.farm_view(entry, "1", 0)
        actions = [
            pets_ui.parse_callback(button["callback_data"])[1]
            for row in keyboard["inline_keyboard"] for button in row
            if "callback_data" in button
        ]
        self.assertNotIn("farmticket", actions)   # nothing in the wallet yet

        self.assertTrue(pets.grant_farm_ticket(entry, "1", "a"))
        text, keyboard = pets_ui.farm_view(entry, "1", 0)
        actions = [
            pets_ui.parse_callback(button["callback_data"])[1]
            for row in keyboard["inline_keyboard"] for button in row
            if "callback_data" in button
        ]
        self.assertIn("farmticket", actions)
        # And the screen says what the button will do -- specifically that, unlike
        # «Забрать сейчас» right below it, this one keeps the whole payout.
        self.assertIn("🎟", text)
        self.assertIn("билет", text)
        self.assertIn("как за все 8 ч", text)
        labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]
        self.assertIn("🎟 Билет: закончить смену (1)", labels)



class FightLookupTests(PetsTestCase):
    def test_live_fight_gets_stable_id_and_full_audit_record(self):
        entry = "audit"
        self._tame(entry, "1", "One")
        self._tame(entry, "2", "Two")
        a = pets._dungeon_fighter(pets._load(entry)["pets"]["1"], "1")
        b = pets._dungeon_fighter(pets._load(entry)["pets"]["2"], "2")
        result = pets_combat.simulate(a, b, seed=441)
        snapshot = {"seed": 441, "fighters": {
            "1": pets_combat.snapshot(a), "2": pets_combat.snapshot(b),
        }}

        reward = pets.record_fight(
            entry, "1", "2", result, date(2026, 8, 15), combat_snapshot=snapshot,
            now=datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc),
        )
        self.assertRegex(reward["fight_id"], r"^F-20260815-[0-9A-F]{12}$")
        audit = pets.find_fight_audit(entry, reward["fight_id"])
        self.assertEqual(audit["fight_id"], reward["fight_id"])
        self.assertEqual(audit["kind"], "arena")
        self.assertEqual(set(audit["fighters"]), {"1", "2"})
        self.assertEqual(len(audit["moves"]), len(result.rounds))
        self.assertIn("state", audit["moves"][0])
        json.dumps(audit)

    def test_a_fight_id_is_url_safe_and_only_its_participants_can_read_it(self):
        """The id travels in a query string, and a raw "+" from the timezone offset would
        decode there as a space -- a silent miss for any caller that forgot to encode it.
        And a fight belongs to two people: the chat-wide log must not be readable by
        guessing a timestamp."""
        entry = "chat"
        self._tame(entry, "1", "Мой")
        self._tame(entry, "2", "Чужой")
        self._tame(entry, "3", "Третий")
        result = SimpleNamespace(winner="1", loser="2")
        with patch("random.random", return_value=1.0):
            pets.record_fight(entry, "1", "2", result, date(2026, 8, 9))

        stored = pets._load(entry)["fights"][0]
        wire_id = pets.fight_id(stored)
        # Not "needs no escaping at all" -- a colon is legal in a query and survives
        # verbatim. The characters that would come back as something else are the ones
        # that must not appear: "+" reads as a space, and the rest end the value.
        for hostile in "+&=#%?":
            self.assertNotIn(hostile, wire_id)
        self.assertEqual(parse_qs(f"id={wire_id}")["id"], [wire_id])

        for participant in ("1", "2"):
            found = pets.find_fight(entry, participant, wire_id)
            self.assertIsNotNone(found)
            self.assertEqual(found["ts"], stored["ts"])
        self.assertIsNone(pets.find_fight(entry, "3", wire_id))
        self.assertIsNone(pets.find_fight(entry, "1", ""))
        self.assertIsNone(pets.find_fight(entry, "1", "2020-01-01T00:00:00"))


class QuarryTests(PetsTestCase):
    def _give_pickaxe_charge(self, entry="quarry", uid="1"):
        self._tame(entry, uid)
        data = pets._load(entry)
        data["pets"][uid]["pickaxe_runs"] = 1
        pets._save(entry, data)

    def test_quarry_offers_four_reward_previews_in_one_choice_set(self):
        self._give_pickaxe_charge()
        status = pets.quarry_status("quarry", "1")
        previews = status["hour_previews"]
        self.assertEqual([row["hours"] for row in previews], [1, 2, 4, 8])
        self.assertTrue(all(
            row["ruby_min"] > 0 and row["ruby_max"] >= row["ruby_min"]
            and row["gold"] > 0 and row["xp"] > 0 and row["drop_chance"] > 0
            for row in previews
        ))
        self.assertEqual(
            [row["gold"] for row in previews],
            sorted(row["gold"] for row in previews),
        )

        text, keyboard = pets_ui.farm_view("quarry", "1", 0)
        quarry_row = next(
            row for row in keyboard["inline_keyboard"]
            if len(row) == 4 and all(
                pets_ui.parse_callback(button["callback_data"])[1] == "quarrystart"
                for button in row
            )
        )
        self.assertEqual(
            [pets_ui.parse_callback(button["callback_data"])[2] for button in quarry_row],
            ["1", "2", "4", "8"],
        )
        self.assertIn("🎁 30%", text)

    def test_two_hour_quarry_pays_every_promised_reward_channel(self):
        entry, uid = "quarry-two", "1"
        self._give_pickaxe_charge(entry, uid)
        started = datetime(2026, 8, 15, 10, 0)
        with patch.object(pets, "app_now", return_value=started):
            ok, message = pets.start_quarry(entry, uid, 2)
        self.assertTrue(ok, message)
        run = pets._load(entry)["pets"][uid]["quarry_run"]
        self.assertEqual(run["hours"], 2)
        self.assertEqual(datetime.fromisoformat(run["ready_at"]), started + timedelta(hours=2))

        receipt = pets.settle_quarry(entry, uid, started + timedelta(hours=2))
        self.assertEqual(receipt["hours"], 2)
        self.assertEqual(receipt["gold"], pets_config.QUARRY_GOLD_BY_HOURS[2])
        self.assertEqual(receipt["xp"], pets_config.QUARRY_XP_BY_HOURS[2])
        self.assertEqual(receipt["drop_chance"], pets_config.QUARRY_DROP_CHANCE_BY_HOURS[2])
        self.assertGreaterEqual(receipt["rubies"], pets_config.QUARRY_RUBIES_BY_HOURS[2][0])
        self.assertLessEqual(receipt["rubies"], pets_config.QUARRY_RUBIES_BY_HOURS[2][1])
        self.assertEqual(economy.balance(entry, uid, 0), receipt["gold"])
        self.assertEqual(pets.ruby_balance(entry, uid), receipt["rubies"])
        self.assertEqual(pets.get_pet(entry, uid)["xp"], receipt["xp"])
        self.assertIsNone(pets._load(entry)["pets"][uid]["quarry_run"])

    def test_quarry_rejects_an_unsupported_duration_without_spending_a_charge(self):
        self._give_pickaxe_charge()
        ok, _message = pets.start_quarry("quarry", "1", 3)
        self.assertFalse(ok)
        self.assertEqual(pets.quarry_status("quarry", "1")["pickaxe_runs"], 1)

    def test_quarry_freezes_the_departure_level_gold_in_its_preview_and_receipt(self):
        entry, uid = "quarry-level", "1"
        self._give_pickaxe_charge(entry, uid)
        data = pets._load(entry)
        data["pets"][uid]["level"] = 100
        pets._save(entry, data)
        preview = next(
            row for row in pets.quarry_status(entry, uid)["hour_previews"]
            if row["hours"] == 2
        )
        started = datetime(2026, 8, 15, 10, 0)
        with patch.object(pets, "app_now", return_value=started):
            self.assertTrue(pets.start_quarry(entry, uid, 2)[0])
        data = pets._load(entry)
        data["pets"][uid]["level"] = 200
        pets._save(entry, data)

        receipt = pets.settle_quarry(entry, uid, started + timedelta(hours=2))

        self.assertEqual(receipt["gold"], preview["gold"])
        self.assertAlmostEqual(
            receipt["gold_multiplier"],
            pets_config.hero_gold_multiplier(100, "quarry"),
        )


class ToolMasterworkTests(PetsTestCase):
    def test_rune_pickaxe_is_unlimited_and_scales_every_quarry_reward(self):
        entry, uid = "masterwork-pickaxe", "1"
        self._tame(entry, uid)
        self.assertEqual(pets.unlock_tool_for_rune_quest(entry, uid, "rune_paint_pickaxe"), "pickaxe")
        self.assertIsNone(pets.unlock_tool_for_rune_quest(entry, uid, "rune_paint_pickaxe"))
        status = pets.quarry_status(entry, uid)
        self.assertTrue(status["pickaxe_unlimited"])
        self.assertEqual(status["pickaxe_efficiency"], 1.5)
        preview = next(row for row in status["hour_previews"] if row["hours"] == 2)
        self.assertEqual(preview["gold"], round(pets_config.QUARRY_GOLD_BY_HOURS[2] * 1.5))
        self.assertEqual(preview["xp"], round(pets_config.QUARRY_XP_BY_HOURS[2] * 1.5))
        self.assertEqual(preview["drop_chance"], pets_config.QUARRY_DROP_CHANCE_BY_HOURS[2] * 1.5)

        start = datetime(2026, 8, 15, 10, 0)
        with patch.object(pets, "app_now", return_value=start):
            self.assertTrue(pets.start_quarry(entry, uid, 2)[0])
        receipt = pets.settle_quarry(entry, uid, start + timedelta(hours=2))
        self.assertEqual(receipt["gold"], preview["gold"])
        self.assertEqual(receipt["xp"], preview["xp"])
        self.assertEqual(receipt["drop_chance"], preview["drop_chance"])
        self.assertTrue(pets.start_quarry(entry, uid, 1)[0])  # no purchased charge required

    def test_shovel_consumes_one_farm_charge_then_masterwork_is_unlimited(self):
        entry, uid = "masterwork-shovel", "1"
        self._tame(entry, uid)
        data = pets._load(entry)
        data["pets"][uid]["farm_level"] = 1
        pets._save(entry, data)
        _text, keyboard = pets_ui.farm_view(entry, uid, 0)
        actions = {
            pets_ui.parse_callback(button["callback_data"])[1]
            for row in keyboard["inline_keyboard"] for button in row
        }
        self.assertIn("shovelbuy", actions)
        economy.grant(entry, uid, pets_config.SHOVEL_COST, "test:shovel")
        self.assertTrue(pets.buy_shovel(entry, uid, 0)[0])
        status = pets.farm_status(entry, uid)
        self.assertEqual(status["shovel_runs"], pets_config.SHOVEL_RUNS)
        self.assertEqual(status["shovel_gold_multiplier"], 1.25)
        six = next(row for row in status["hour_previews"] if row["hours"] == 6)
        self.assertEqual(six["gold"], round(pets_config.farm_gold_for(1, 6) * 1.25))
        _text, keyboard = pets_ui.farm_view(entry, uid, 0)
        actions = {
            pets_ui.parse_callback(button["callback_data"])[1]
            for row in keyboard["inline_keyboard"] for button in row
        }
        self.assertNotIn("shovelbuy", actions)  # bought charges are used automatically

        start = datetime(2026, 8, 15, 10, 0)
        self.assertTrue(pets.start_farm(entry, uid, 6, now=start)[0])
        self.assertEqual(pets.farm_status(entry, uid, start)["shovel_runs"], pets_config.SHOVEL_RUNS - 1)
        self.assertEqual(pets.farm_status(entry, uid, start)["reward"]["gold"], six["gold"])
        pets.settle_completed_farms(entry, start + timedelta(hours=6))

        self.assertEqual(pets.unlock_tool_for_rune_quest(entry, uid, "rune_paint_shovel"), "shovel")
        status = pets.farm_status(entry, uid)
        self.assertTrue(status["shovel_upgraded"])
        self.assertEqual(status["shovel_gold_multiplier"], 1.5)
        before = status["shovel_runs"]
        self.assertTrue(pets.start_farm(entry, uid, 1, now=start + timedelta(hours=7))[0])
        self.assertEqual(pets.farm_status(entry, uid, start + timedelta(hours=7))["shovel_runs"], before)


class WorkplaceFigurineTests(PetsTestCase):
    """One creature, one place -- and the pair of figurines that lifts the rule."""

    def _worker(self, entry="figurine", uid="1"):
        self._tame(entry, uid)
        data = pets._load(entry)
        data["pets"][uid]["farm_level"] = 3
        data["pets"][uid]["pickaxe_runs"] = 5
        pets._save(entry, data)

    def test_quarry_locks_the_farm_out_until_both_figurines_are_painted(self):
        entry, uid = "figurine-lock", "1"
        self._worker(entry, uid)
        start = datetime(2026, 8, 15, 10, 0)
        with patch.object(pets, "app_now", return_value=start):
            self.assertTrue(pets.start_quarry(entry, uid, 4)[0])

        ok, message = pets.start_farm(entry, uid, 8, now=start)
        self.assertFalse(ok)
        self.assertIn("обе фигурки", message)
        status = pets.farm_status(entry, uid, start)
        self.assertFalse(status["can_start"])
        self.assertTrue(status["blocked_by_quarry"])

        # ONE figurine is not a second worker: the pair is the unlock, deliberately.
        self.assertEqual(pets.unlock_tool_for_rune_quest(entry, uid, "rune_paint_farmer"), "farmer")
        self.assertFalse(pets.start_farm(entry, uid, 8, now=start)[0])
        self.assertFalse(pets.farm_status(entry, uid, start)["can_start"])

        self.assertEqual(pets.unlock_tool_for_rune_quest(entry, uid, "rune_paint_miner"), "miner")
        self.assertTrue(pets.farm_status(entry, uid, start)["parallel_work"])
        self.assertTrue(pets.start_farm(entry, uid, 8, now=start)[0])
        self.assertTrue(pets.farm_status(entry, uid, start)["running"])
        self.assertTrue(pets.quarry_status(entry, uid, start)["running"])

    def test_farm_locks_the_quarry_out_and_hides_its_buttons(self):
        entry, uid = "figurine-farm-lock", "1"
        self._worker(entry, uid)
        start = datetime(2026, 8, 15, 10, 0)
        self.assertTrue(pets.start_farm(entry, uid, 8, now=start)[0])

        with patch.object(pets, "app_now", return_value=start):
            ok, message = pets.start_quarry(entry, uid, 4)
        self.assertFalse(ok)
        self.assertIn("обе фигурки", message)
        self.assertFalse(pets.quarry_status(entry, uid, start)["can_start"])

        # The screen must not offer what the core would refuse.
        _text, keyboard = pets_ui.farm_view(entry, uid, 0)
        actions = [
            pets_ui.parse_callback(button["callback_data"])[1]
            for row in keyboard["inline_keyboard"] for button in row
        ]
        self.assertNotIn("quarrystart", actions)
        self.assertNotIn("farmstart", actions)

    def test_a_finished_but_unsettled_run_still_holds_the_creature(self):
        """The pet is on its way home with the payout; a second job would double-book it."""
        entry, uid = "figurine-settle", "1"
        self._worker(entry, uid)
        start = datetime(2026, 8, 15, 10, 0)
        self.assertTrue(pets.start_farm(entry, uid, 1, now=start)[0])
        done = start + timedelta(hours=1, minutes=1)
        self.assertTrue(pets.farm_status(entry, uid, done)["ready"])
        # start_quarry settles the finished shift itself rather than refusing on a run
        # that is over in everything but the bookkeeping.
        with patch.object(pets, "app_now", return_value=done):
            self.assertTrue(pets.start_quarry(entry, uid, 4)[0])

    def test_one_figurine_still_pays_extra_experience_at_its_own_station(self):
        entry, uid = "figurine-xp", "1"
        self._worker(entry, uid)
        plain = next(row for row in pets.farm_status(entry, uid)["hour_previews"] if row["hours"] == 8)
        plain_quarry = next(
            row for row in pets.quarry_status(entry, uid)["hour_previews"] if row["hours"] == 8
        )

        self.assertEqual(pets.unlock_tool_for_rune_quest(entry, uid, "rune_paint_farmer"), "farmer")
        painted = next(row for row in pets.farm_status(entry, uid)["hour_previews"] if row["hours"] == 8)
        self.assertEqual(painted["xp"], round(plain["xp"] * (1 + pets_config.FIGURINE_XP_BONUS)))
        self.assertEqual(painted["gold"], plain["gold"])           # experience only
        # The farmer figurine is not a miner: the quarry is untouched by it.
        self.assertEqual(
            next(row for row in pets.quarry_status(entry, uid)["hour_previews"] if row["hours"] == 8)["xp"],
            plain_quarry["xp"],
        )

        start = datetime(2026, 8, 15, 10, 0)
        self.assertTrue(pets.start_farm(entry, uid, 8, now=start)[0])
        self.assertEqual(pets.farm_status(entry, uid, start)["reward"]["xp"], painted["xp"])

    def test_a_figurine_painted_mid_shift_only_improves_the_next_one(self):
        entry, uid = "figurine-frozen", "1"
        self._worker(entry, uid)
        start = datetime(2026, 8, 15, 10, 0)
        promised = next(row for row in pets.farm_status(entry, uid)["hour_previews"] if row["hours"] == 8)
        self.assertTrue(pets.start_farm(entry, uid, 8, now=start)[0])
        self.assertEqual(pets.unlock_tool_for_rune_quest(entry, uid, "rune_paint_farmer"), "farmer")
        self.assertEqual(pets.farm_status(entry, uid, start)["reward"]["xp"], promised["xp"])


class FarmTests(PetsTestCase):
    def _build_farm(self, entry="farm", uid="1", level=1):
        self._tame(entry, uid)
        total = sum(pets_config.FARM_UPGRADE_COSTS[:level])
        economy.grant(entry, uid, total, "farm-test-funds")
        for expected_level in range(1, level + 1):
            ok, message = pets.upgrade_farm(entry, uid, 0)
            self.assertTrue(ok, message)
            self.assertEqual(pets.farm_level(entry, uid), expected_level)

    def test_farm_luck_is_frozen_at_the_start_of_the_shift(self):
        """Same rule as farm level and buildings: what the pet had when it left is what
        the shift pays, so buying luck mid-shift cannot re-roll a run already in flight."""
        entry, start = "farm-luck", datetime(2026, 8, 1, 10)
        self._build_farm(entry)
        self.assertTrue(pets.start_farm(entry, "1", 8, now=start)[0])

        data = pets._load(entry)
        run_luck = data["pets"]["1"]["farm_run"]["luck"]
        data["pets"]["1"]["stats"]["luck"] = 80
        pets._save(entry, data)
        self.assertEqual(run_luck, pets_config.STAT_MIN_LEVEL)

        status = pets.farm_status(entry, "1", start)
        self.assertTrue(status["running"])
        # The preview for the NEXT shift moves with the new luck...
        self.assertGreater(
            status["hour_previews"][-1]["drop_chance"],
            pets_config.FARM_DROP_CHANCE_BY_HOURS[8],
        )
        # ...while the shift already running keeps the odds it was promised.
        self.assertAlmostEqual(
            pets._farm_multipliers({}, 8, run_luck)[2],
            pets_config.FARM_DROP_CHANCE_BY_HOURS[8]
            * pets_config.luck_drop_multiplier(pets_config.STAT_MIN_LEVEL),
        )

    def test_farm_requires_construction_and_has_ten_priced_levels(self):
        self._tame("farm", "1")
        ok, _ = pets.start_farm("farm", "1", now=datetime(2026, 8, 1, 10))
        self.assertFalse(ok)

        total = sum(pets_config.FARM_UPGRADE_COSTS)
        economy.grant("farm", "1", total, "farm-test-funds")
        for level in range(1, pets_config.FARM_MAX_LEVEL + 1):
            ok, message = pets.upgrade_farm("farm", "1", 0)
            self.assertTrue(ok, message)
            self.assertEqual(pets.farm_level("farm", "1"), level)
        self.assertFalse(pets.upgrade_farm("farm", "1", 0)[0])
        self.assertEqual(economy.balance("farm", "1", 0), 0)

    def test_six_hour_run_is_fixed_idempotent_and_delivers_notification(self):
        entry, start = "farm", datetime(2026, 8, 1, 10, 15)
        self._build_farm(entry)
        ok, message = pets.start_farm(entry, "1", now=start)
        self.assertTrue(ok, message)
        status = pets.farm_status(entry, "1", start)
        self.assertTrue(status["running"])
        self.assertEqual(status["ready_at"], (start + timedelta(hours=6)).isoformat())
        reward = dict(status["reward"])
        self.assertTrue(pets.is_farming(entry, "1", start + timedelta(hours=5, minutes=59)))
        self.assertFalse(pets.is_farming(entry, "1", start + timedelta(hours=6)))
        self.assertEqual(pets.settle_completed_farms(entry, start + timedelta(hours=5, minutes=59)), [])

        before_gold = economy.balance(entry, "1", 0)
        before_xp = pets.get_pet(entry, "1")["xp"]
        receipts = pets.settle_completed_farms(entry, start + timedelta(hours=6))
        self.assertEqual(len(receipts), 1)
        receipt = receipts[0]
        self.assertEqual(receipt["user_id"], "1")
        self.assertEqual(receipt["gold"], reward["gold"])
        self.assertEqual(receipt["xp"], reward["xp"])
        self.assertEqual(economy.balance(entry, "1", 0), before_gold + reward["gold"])
        self.assertGreaterEqual(pets.get_pet(entry, "1")["xp"], before_xp)
        self.assertEqual(pets.settle_completed_farms(entry, start + timedelta(hours=7)), [])
        self.assertEqual(economy.balance(entry, "1", 0), before_gold + reward["gold"])
        self.assertEqual(pets.pending_farm_notifications(entry)[0]["run_id"], receipt["run_id"])
        self.assertTrue(pets.mark_farm_notified(entry, "1", receipt["run_id"], now=start + timedelta(hours=6)))
        self.assertFalse(pets.mark_farm_notified(entry, "1", receipt["run_id"]))
        self.assertEqual(pets.pending_farm_notifications(entry), [])

    def test_building_the_farm_is_repaid_by_its_own_first_shift(self):
        """The farm is the one thing that pays while you are not playing, so it has to be
        the first thing a newcomer reaches. Pinning it against the level-1 six-hour shift
        rather than against a bare number keeps that true if either side is re-tuned."""
        build_cost = pets_config.FARM_UPGRADE_COSTS[0]
        self.assertLessEqual(
            build_cost,
            pets_config.farm_gold_for(1, pets_config.FARM_DURATION_HOURS),
            "building the farm must cost no more than its first reference shift returns",
        )

        entry, start = "farm-entry", datetime(2026, 8, 1, 10)
        self._tame(entry, "1")
        economy.grant(entry, "1", build_cost, "farm-test-funds")
        ok, message = pets.upgrade_farm(entry, "1", 0, now=start)
        self.assertTrue(ok, message)
        self.assertEqual(pets.farm_level(entry, "1"), 1)
        # Exactly enough, not "enough with change": the entry price is the whole point.
        self.assertEqual(economy.balance(entry, "1", 0), 0)
        self.assertTrue(pets.start_farm(entry, "1", 6, now=start)[0])

    def test_first_basic_farm_run_is_only_part_of_an_ordinary_weapon(self):
        entry, start = "farm-shop", datetime(2026, 8, 1, 10)
        self._tame(entry, "1")
        economy.grant(entry, "1", pets_config.FARM_UPGRADE_COSTS[0], "farm-test-funds")
        self.assertTrue(pets.upgrade_farm(entry, "1", 0, now=start)[0])
        self.assertTrue(pets.start_farm(entry, "1", now=start)[0])
        finish = start + timedelta(hours=pets_config.FARM_DURATION_HOURS)
        receipt = pets.settle_completed_farms(entry, finish)[0]
        self.assertEqual(receipt["gold"], pets_config.FARM_GOLD_PER_RUN[1])

        with patch("pets.app_now", return_value=finish):
            starter = min(
                pets.daily_storefront_weapons(entry, finish, user_id="1"),
                key=lambda item: item.price,
            )
            ok, message = pets.buy_item(entry, "1", 0, starter.code)
        self.assertFalse(ok)
        self.assertIn(str(starter.price), message)

    def test_farm_reward_is_snapshotted_and_auto_equips_a_found_item(self):
        entry, start = "farm", datetime(2026, 8, 1, 10)
        self._build_farm(entry, level=1)
        self.assertTrue(pets.start_farm(entry, "1", now=start)[0])
        before = pets.farm_status(entry, "1", start)["reward"]
        # Buying an upgrade while the pet is away affects only the next run.
        economy.grant(entry, "1", pets_config.FARM_UPGRADE_COSTS[1], "farm-test-funds")
        self.assertTrue(pets.upgrade_farm(entry, "1", 0)[0])
        self.assertEqual(pets.farm_status(entry, "1", start)["reward"], before)

        data = pets._load(entry)
        data["pets"]["1"]["farm_run"]["reward"] = {"gold": 17, "xp": 23, "item_code": "bt01"}
        pets._save(entry, data)
        receipt = pets.settle_completed_farms(entry, start + timedelta(hours=6))[0]
        self.assertEqual(receipt["gold"], 17)
        self.assertEqual(receipt["item_code"], "bt01")
        self.assertTrue(receipt["auto_equipped"])
        pet = pets.get_pet(entry, "1")
        self.assertIn("bt01", pet["inventory"])
        self.assertEqual(pet["equipped"]["boots"], "bt01")

    def test_features_have_single_purchase_and_visible_effects(self):
        self._build_farm("farm")
        total = sum(spec["cost"] for spec in pets_config.FARM_FEATURES.values())
        economy.grant("farm", "1", total, "farm-test-funds")
        for key, spec in pets_config.FARM_FEATURES.items():
            ok, message = pets.upgrade_farm_feature("farm", "1", 0, key)
            self.assertTrue(ok, message)
            self.assertIn(spec["name"], message)
            self.assertFalse(pets.upgrade_farm_feature("farm", "1", 0, key)[0])
        status = pets.farm_status("farm", "1")
        self.assertTrue(all(spec["level"] == 1 for spec in status["features"].values()))
        # `drop_chance` is the six-hour-anchor row of hour_previews, kept for backward
        # compatibility -- beds' +0.05 still applies on top of that anchor's base rate.
        # Luck multiplies the finished chance, so the beds bonus is read through the
        # pet's current luck rather than compared against the raw table.
        self.assertAlmostEqual(
            status["drop_chance"],
            (pets_config.FARM_DROP_CHANCE_BY_HOURS[6] + 0.05)
            * pets_config.luck_drop_multiplier(pets.stat_level("farm", "1", "luck")),
        )

    def test_due_run_without_id_is_recovered_and_cannot_block_the_pet(self):
        """A run that merely lost its `run_id` (hours/level/features intact) keeps its
        full, normally-rolled payout -- only a run with nothing usable at all falls back
        to a zeroed stub. See _repair_farm_run_id."""
        entry, start = "farm", datetime(2026, 8, 1, 10)
        self._build_farm(entry)
        self.assertTrue(pets.start_farm(entry, "1", now=start)[0])
        data = pets._load(entry)
        run = data["pets"]["1"]["farm_run"]
        expected = pets._farm_reward(data, data["pets"]["1"], run)
        del data["pets"]["1"]["farm_run"]["run_id"]
        pets._save(entry, data)

        receipt = pets.settle_completed_farms(entry, start + timedelta(hours=6))[0]
        self.assertTrue(receipt["run_id"].startswith("recovered-"))
        self.assertEqual(receipt["gold"], expected["gold"])
        self.assertEqual(receipt["hours"], 6)
        self.assertFalse(pets.farm_status(entry, "1", start + timedelta(hours=6))["running"])
        self.assertTrue(pets.start_farm(entry, "1", now=start + timedelta(hours=6))[0])

    def test_due_run_with_nothing_usable_settles_to_a_conservative_zero(self):
        """The narrower case _repair_farm_run_id's docstring warns about: no run_id, no
        reward, and no hours/level/features snapshot either -- genuinely blank, not just
        missing an id. It must not invent a payout."""
        entry, start = "farm", datetime(2026, 8, 1, 10)
        self._build_farm(entry)
        self.assertTrue(pets.start_farm(entry, "1", now=start)[0])
        data = pets._load(entry)
        run = data["pets"]["1"]["farm_run"]
        del run["run_id"]
        run.pop("hours", None)
        run.pop("level", None)
        run.pop("features", None)
        pets._save(entry, data)

        before_gold = economy.balance(entry, "1", 0)
        receipt = pets.settle_completed_farms(entry, start + timedelta(hours=6))[0]
        self.assertEqual(receipt["gold"], 0)
        self.assertIsNone(receipt["item_code"])
        self.assertEqual(economy.balance(entry, "1", 0), before_gold)

    def test_farming_pet_cannot_attack_but_can_be_attacked(self):
        """A farming pet lost its old, blanket attack immunity: it still cannot start a
        fight itself, but it is now a perfectly ordinary, attackable defender -- see the
        _is_farming_record call sites in claim_duel/can_attack_in_arena/find_opponent/
        record_fight."""
        entry, start = "farm", datetime(2026, 8, 1, 10)
        self._build_farm(entry, "1")
        self._tame(entry, "2")
        self.assertTrue(pets.start_farm(entry, "1", now=start)[0])
        with patch("pets.app_now", return_value=start):
            # "1" is away: it cannot be the ATTACKER, in either pairing. `attackable_only`
            # is what routes find_opponent through can_attack_in_arena for the SEEKER.
            self.assertFalse(pets.can_attack_in_arena(entry, "1", "2", start.date()))
            self.assertIsNone(
                pets.find_opponent(entry, "1", rng=random.Random(1), attackable_only=True)
            )
            self.assertFalse(pets.claim_duel(entry, "1", "2", now=start)[0])
            with self.assertRaises(ValueError):
                pets.record_fight(entry, "1", "2", SimpleNamespace(winner="1", loser="2"), start.date())

            # "2" can still find, duel and defeat "1" while it is farming.
            self.assertTrue(pets.can_attack_in_arena(entry, "2", "1", start.date()))
            self.assertEqual(
                pets.find_opponent(entry, "2", rng=random.Random(1), attackable_only=True), "1"
            )
            self.assertTrue(pets.claim_duel(entry, "2", "1", now=start)[0])
            outcome = pets.record_fight(
                entry, "2", "1", SimpleNamespace(winner="2", loser="1"), start.date(),
            )
            self.assertIsInstance(outcome, dict)
            # The farm lock itself is untouched by having been attacked.
            self.assertTrue(pets.is_farming(entry, "1", start))

    # ---------------------------------------------------------- 1-8 hour duration choice

    def test_gold_and_xp_formulas_are_monotonic_across_all_eight_durations(self):
        """farm_gold_for/farm_xp_for are the one place the duration formula is written;
        every level must see a longer shift pay at least as much as a shorter one, with
        the two endpoints genuinely different (not a flat table)."""
        for level in range(1, pets_config.FARM_MAX_LEVEL + 1):
            gold = [pets_config.farm_gold_for(level, hours) for hours in pets_config.FARM_HOUR_CHOICES]
            xp = [pets_config.farm_xp_for(level, hours) for hours in pets_config.FARM_HOUR_CHOICES]
            self.assertEqual(gold, sorted(gold), f"gold not monotonic at level {level}: {gold}")
            self.assertEqual(xp, sorted(xp), f"xp not monotonic at level {level}: {xp}")
            self.assertLess(gold[0], gold[-1])
            self.assertLess(xp[0], xp[-1])
        # The six-hour anchor is untouched by the rebalance.
        self.assertEqual(pets_config.farm_gold_for(1, 6), pets_config.FARM_GOLD_PER_RUN[1])
        self.assertEqual(pets_config.farm_xp_for(1, 6), pets_config.FARM_XP_PER_RUN[1])

    def test_farm_gold_is_rewarding_at_level_one_and_scales_with_pet_level(self):
        """A starter can buy basic gear from one six-hour shift, while a developed
        farm still contributes to the uncapped late-game stat economy."""
        self.assertEqual(
            [pets_config.farm_gold_for(1, hours) for hours in (1, 2, 4, 8)],
            [6, 13, 28, 69],
        )
        self.assertEqual(pets_config.farm_gold_for(1, 6), 45)
        self.assertEqual(pets_config.farm_gold_for(1, 6, pet_level=100), 152)
        self.assertEqual(
            [pets_config.farm_gold_for(10, hours, 1.5, 100) for hours in (1, 2, 4, 8)],
            [169, 349, 746, 1825],
        )

    def test_farm_pet_level_gold_bonus_is_frozen_when_a_shift_starts(self):
        entry, start = "farm-level-snapshot", datetime(2026, 8, 1, 10)
        self._build_farm(entry)
        self.assertTrue(pets.start_farm(entry, "1", 6, now=start)[0])
        before = pets.farm_status(entry, "1", start)["reward"]
        data = pets._load(entry)
        data["pets"]["1"]["level"] = 100
        pets._save(entry, data)
        status = pets.farm_status(entry, "1", start)
        self.assertEqual(status["reward"], before)
        self.assertEqual(status["estimated_gold"], 152)

    def test_hour_previews_are_monotonic_in_gold_xp_and_drop_chance(self):
        entry = "farm-previews"
        self._build_farm(entry, level=6)
        total = sum(spec["cost"] for spec in pets_config.FARM_FEATURES.values())
        economy.grant(entry, "1", total, "test")
        for key in pets_config.FARM_FEATURES:
            self.assertTrue(pets.upgrade_farm_feature(entry, "1", 0, key)[0])
        previews = pets.farm_status(entry, "1")["hour_previews"]
        self.assertEqual([row["hours"] for row in previews], list(pets_config.FARM_HOUR_CHOICES))
        gold = [row["gold"] for row in previews]
        xp = [row["xp"] for row in previews]
        drop = [row["drop_chance"] for row in previews]
        self.assertEqual(gold, sorted(gold))
        self.assertEqual(xp, sorted(xp))
        self.assertEqual(drop, sorted(drop))
        self.assertLess(gold[0], gold[-1])
        self.assertLess(xp[0], xp[-1])
        self.assertLess(drop[0], drop[-1])

    def test_drop_chance_table_rises_with_hours_and_reaches_half_at_eight(self):
        chances = [pets_config.FARM_DROP_CHANCE_BY_HOURS[hours] for hours in pets_config.FARM_HOUR_CHOICES]
        self.assertEqual(chances, sorted(chances))
        self.assertLess(chances[0], chances[-1])
        self.assertEqual(pets_config.FARM_DROP_CHANCE_BY_HOURS[8], 0.50)

    def test_legendary_is_impossible_under_seven_hours_and_reachable_at_eight(self):
        """FARM_LOOT_RARITY_WEIGHTS omits "legendary" below 7 hours entirely, so this is a
        structural guarantee, not a probabilistic one -- checked here with enough seeds per
        length to catch a regression that accidentally reintroduced it."""
        entry = "farm-legendary"
        self._tame(entry, "1")
        data = pets._load(entry)
        record = data["pets"]["1"]
        for hours in range(pets_config.FARM_MIN_HOURS, 7):
            for seed in range(60):
                # chance=1.0 forces the "something was found" branch every single call.
                found = pets._farm_item_for(data, record, random.Random(seed), hours, 1.0)
                self.assertFalse(
                    found is not None and found.rarity == "legendary",
                    f"legendary rolled at {hours}h, seed {seed}",
                )
        legendary_seen = any(
            (found := pets._farm_item_for(data, record, random.Random(seed), 8, 1.0)) is not None
            and found.rarity == "legendary"
            for seed in range(500)
        )
        self.assertTrue(legendary_seen, "no legendary rolled across 500 seeds at 8 hours")

    def test_farm_legendary_can_repeat_across_players_but_not_inside_one_bag(self):
        entry = "farm-weapon-loot"
        self._tame(entry, "1")
        self._tame(entry, "2")
        data = pets._load(entry)
        # Another player's legendary set must not exhaust the finite design catalogue.
        owned_elsewhere = [
            item.code for item in pets_config.ITEMS
            if item.source == "drop" and item.slot == "weapon" and item.rarity == "legendary"
        ]
        data["pets"]["2"]["inventory"] = owned_elsewhere
        pets._save(entry, data)
        data = pets._load(entry)
        record = data["pets"]["1"]
        repeated_legendary = None
        for seed in range(400):
            found = pets._farm_item_for(data, record, random.Random(seed), 8, 1.0)
            if found is not None and found.slot == "weapon" and found.code in owned_elsewhere:
                repeated_legendary = found
                break
        self.assertIsNotNone(repeated_legendary)
        record["inventory"] = [repeated_legendary.code]
        for seed in range(400):
            found = pets._farm_item_for(data, record, random.Random(seed), 8, 1.0)
            self.assertTrue(found is None or found.code != repeated_legendary.code)

    # -------------------------------------------------------------------- cancelling early

    def test_cancel_at_two_hours_thirty_five_minutes_pays_exactly_two_hours(self):
        entry, start = "farm-cancel", datetime(2026, 8, 1, 10)
        self._build_farm(entry, level=3)
        self.assertTrue(pets.start_farm(entry, "1", 6, now=start)[0])
        cancel_at = start + timedelta(hours=2, minutes=35)
        before_gold = economy.balance(entry, "1", 0)
        ok, message = pets.cancel_farm(entry, "1", now=cancel_at)
        self.assertTrue(ok, message)
        self.assertIn("2 из 6 ч", message)
        self.assertFalse(pets.is_farming(entry, "1", cancel_at))
        self.assertFalse(pets.farm_status(entry, "1", cancel_at)["running"])
        # Paid at the SHORT (2 h) rate, not a 2/6 proration of the 6 h payout.
        expected_gold = pets_config.farm_gold_for(3, 2)
        self.assertEqual(economy.balance(entry, "1", 0), before_gold + expected_gold)

    def test_cancel_under_one_hour_pays_nothing_but_still_ends_the_shift(self):
        entry, start = "farm-cancel-short", datetime(2026, 8, 1, 10)
        self._build_farm(entry, level=2)
        self.assertTrue(pets.start_farm(entry, "1", 4, now=start)[0])
        before_gold = economy.balance(entry, "1", 0)
        ok, message = pets.cancel_farm(entry, "1", now=start + timedelta(minutes=45))
        self.assertTrue(ok, message)
        self.assertIn("меньше часа", message)
        self.assertEqual(economy.balance(entry, "1", 0), before_gold)
        self.assertFalse(pets.is_farming(entry, "1", start + timedelta(minutes=45)))

    def test_cancel_refuses_when_nothing_is_running_or_already_ready(self):
        entry, start = "farm-cancel-refuse", datetime(2026, 8, 1, 10)
        self._build_farm(entry)
        self.assertFalse(pets.cancel_farm(entry, "1", now=start)[0])
        self.assertTrue(pets.start_farm(entry, "1", 1, now=start)[0])
        ready_at = start + timedelta(hours=1)
        self.assertFalse(pets.cancel_farm(entry, "1", now=ready_at)[0])

    # ------------------------------------------------------------------------- settlement

    def test_settlement_is_idempotent_and_reproducible_for_an_hour_scaled_run(self):
        """Two guarantees a retried settlement relies on: (1) recomputing a still-persisted
        run's reward from scratch -- as a genuine crash-before-clear retry would -- must
        reproduce the identical numbers rather than re-rolling, and (2) once the run is
        cleared, calling settle_completed_farms again must mint nothing further."""
        entry, start = "farm-idempotent", datetime(2026, 8, 1, 10)
        self._build_farm(entry, level=4)
        self.assertTrue(pets.start_farm(entry, "1", 5, now=start)[0])
        finish = start + timedelta(hours=5)

        data = pets._load(entry)
        run = data["pets"]["1"]["farm_run"]
        reroll = pets._farm_reward(pets._load(entry), pets._load(entry)["pets"]["1"], dict(run))

        before_gold = economy.balance(entry, "1", 0)
        receipts = pets.settle_completed_farms(entry, finish)
        self.assertEqual(len(receipts), 1)
        receipt = receipts[0]
        self.assertGreater(receipt["gold"], 0)
        self.assertEqual(receipt["hours"], 5)
        self.assertEqual(receipt["gold"], reroll["gold"])
        self.assertEqual(receipt["xp"], reroll["xp"])
        self.assertEqual(receipt["item_code"], reroll["item_code"])
        self.assertEqual(economy.balance(entry, "1", 0), before_gold + receipt["gold"])

        # The run is now cleared; a repeat call must find nothing due and mint nothing more.
        self.assertEqual(pets.settle_completed_farms(entry, finish + timedelta(hours=1)), [])
        self.assertEqual(economy.balance(entry, "1", 0), before_gold + receipt["gold"])


class MiscApiTests(PetsTestCase):
    def test_today_cage_level_and_balance_for(self):
        entry = "chat"
        self.assertIsInstance(pets.today(), date)
        self.assertEqual(pets.cage_level(entry, "1"), 1)

        self._tame(entry, "1")
        self.assertEqual(pets.cage_level(entry, "1"), 1)

        economy.grant(entry, "1", 42, "test")
        self.assertEqual(pets.balance_for(entry, "1", 0), 42)
        self.assertEqual(pets.balance_for(entry, "1", 0), economy.balance(entry, "1", 0))

    def test_fight_refresh_counts_to_the_next_hour(self):
        moment = datetime(2026, 8, 9, 18, 35, 20)
        self.assertEqual(pets.fight_refresh_seconds(moment), 24 * 60 + 40)

    def test_award_xp_reports_level_ups(self):
        entry = "chat"
        self._tame(entry, "1")
        new_level, gained = pets.award_xp(entry, "1", 1)
        self.assertEqual(new_level, 1)
        self.assertEqual(gained, 0)

        # Enough xp to blow well past several thresholds at once.
        new_level, gained = pets.award_xp(entry, "1", 100_000)
        self.assertGreater(gained, 0)
        self.assertEqual(new_level, pets.get_pet(entry, "1")["level"])
        self.assertGreater(new_level, 50)


if __name__ == "__main__":
    unittest.main()
