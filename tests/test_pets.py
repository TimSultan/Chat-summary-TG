import random
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import economy
import pets
import pets_config
import pets_ui
import stats


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
        economy.grant(entry, uid, pets_config.CAGE_PRICE + pets_config.TAME_PRICE, "test")
        ok, msg = pets.buy_cage(entry, uid, 0)
        self.assertTrue(ok, msg)
        ok, msg = pets.tame(entry, uid, 0, name, f"file{uid}", f"Owner{uid}")
        self.assertTrue(ok, msg)


class CageAndTamingTests(PetsTestCase):
    def test_buy_cage_debits_exact_price_and_refuses_one_short(self):
        entry = "chat"
        economy.grant(entry, "1", pets_config.CAGE_PRICE - 1, "test")

        ok, msg = pets.buy_cage(entry, "1", 0)
        self.assertFalse(ok, msg)
        self.assertFalse(pets.has_cage(entry, "1"))
        self.assertEqual(pets.cage_level(entry, "1"), 0)

        economy.grant(entry, "1", 1, "test")  # exactly enough now
        ok, msg = pets.buy_cage(entry, "1", 0)
        self.assertTrue(ok, msg)
        self.assertEqual(economy.balance(entry, "1", 0), 0)
        self.assertTrue(pets.has_cage(entry, "1"))
        self.assertEqual(pets.cage_level(entry, "1"), 1)

        # Buying twice is refused outright, no double debit.
        economy.grant(entry, "1", pets_config.CAGE_PRICE, "test")
        ok, msg = pets.buy_cage(entry, "1", 0)
        self.assertFalse(ok, msg)
        self.assertEqual(economy.balance(entry, "1", 0), pets_config.CAGE_PRICE)

    def test_tame_without_cage_refuses(self):
        ok, msg = pets.tame("chat", "1", 0, "Рекс", "file123", "Owner")
        self.assertFalse(ok)
        self.assertIsNone(pets.get_pet("chat", "1"))

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

    def test_duplicate_name_refuses_case_insensitively(self):
        entry = "chat"
        for uid in ("1", "2"):
            economy.grant(entry, uid, pets_config.CAGE_PRICE + pets_config.TAME_PRICE, "test")
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
    def test_upgrade_stat_at_max_level_refuses(self):
        entry = "chat"
        self._tame(entry, "1")
        data = pets._load(entry)
        data["pets"]["1"]["stats"]["strength"] = pets_config.STAT_MAX_LEVEL
        pets._save(entry, data)
        economy.grant(entry, "1", 100_000, "test")

        ok, msg, spent = pets.upgrade_stat(entry, "1", 0, "strength", times=1)
        self.assertFalse(ok)
        self.assertEqual(spent, 0)
        self.assertEqual(pets.stat_level(entry, "1", "strength"), pets_config.STAT_MAX_LEVEL)

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
            weapon for weapon in pets_config.daily_storefront_weapons(entry, pets.today())
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
        stick, fork = pets_config.daily_storefront_weapons(entry, pets.today())[:2]
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


class DailyFightsAndOpponentTests(PetsTestCase):
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
            for _ in range(pets_config.ARENA_SAME_OPPONENT_DAILY_LIMIT):
                pets.record_fight(entry, "1", "2", result, day)
        self.assertFalse(pets.can_attack_in_arena(entry, "1", "2", day))
        self.assertTrue(pets.can_attack_in_arena(entry, "1", "2", day + timedelta(days=1)))

    def test_daily_counter_resets_on_a_new_date(self):
        entry = "chat"
        self._tame(entry, "1", "Attacker")
        self._tame(entry, "2", "Defender")
        day1 = date(2026, 8, 1)
        result = SimpleNamespace(winner="1", loser="2")

        with patch("random.random", return_value=1.0):  # never drop, keep it simple
            pets.record_fight(entry, "1", "2", result, day1)

        self.assertEqual(pets.fights_left(entry, "1", day1), pets.daily_allowance(entry, '1', day1) - 1)

        day2 = day1 + timedelta(days=1)
        self.assertEqual(pets.fights_left(entry, "1", day2), pets.daily_allowance(entry, '1', day2))

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

    def test_attackable_finder_hides_daily_capped_targets_instead_of_reporting_them_later(self):
        entry = "chat"
        self._tame(entry, "1")
        self._tame(entry, "2")
        self._tame(entry, "3")
        result = SimpleNamespace(winner="1", loser="2")
        today = pets.today()
        with patch("random.random", return_value=1.0):
            for _ in range(pets_config.ARENA_SAME_OPPONENT_DAILY_LIMIT):
                pets.record_fight(entry, "1", "2", result, today)

        self.assertEqual(
            pets.find_opponent(entry, "1", rng=random.Random(1), attackable_only=True),
            "3",
        )

    def test_find_opponent_returns_none_only_when_the_chat_has_no_other_pet(self):
        entry = "chat"
        self._tame(entry, "1")
        self.assertIsNone(pets.find_opponent(entry, "1", rng=random.Random(1)))


class HamsteratorTests(PetsTestCase):
    def _build_level_one(self, entry="chat", user_id="1", now=None):
        economy.grant(
            entry, user_id,
            pets_config.CAGE_PRICE + pets_config.HAMSTERATOR_UPGRADE_COSTS[0], "test",
        )
        ok, message = pets.buy_cage(entry, user_id, 0)
        self.assertTrue(ok, message)
        ok, message = pets.upgrade_hamsterator(entry, user_id, 0, now=now)
        self.assertTrue(ok, message)
        self.assertIn("Хомяколатор", message)

    def test_income_only_credits_complete_elapsed_hours_and_keeps_fraction(self):
        start = datetime(2026, 8, 8, 14, 55)
        self._build_level_one(now=start)
        self.assertEqual(pets.settle_passive_income("chat", "1", now=start + timedelta(minutes=6))["credited"], 0)
        self.assertEqual(pets.settle_passive_income("chat", "1", now=start + timedelta(hours=1, minutes=5))["credited"], 1)
        # The five-minute remainder is retained, so another 55 minutes completes hour 2.
        self.assertEqual(pets.settle_passive_income("chat", "1", now=start + timedelta(hours=2))["credited"], 1)
        self.assertEqual(economy.balance("chat", "1", 0), 2)

    def test_income_cap_and_restart_retry_do_not_double_credit(self):
        start = datetime(2026, 8, 8, 10)
        self._build_level_one(now=start)
        later = start + timedelta(days=10)
        self.assertEqual(pets.settle_passive_income("chat", "1", now=later)["credited"], pets_config.HAMSTERATOR_STORAGE_CAP[1])
        # New loads (the same path a process restart takes) see the advanced checkpoint.
        self.assertEqual(pets.settle_passive_income("chat", "1", now=later)["credited"], 0)
        self.assertEqual(economy.balance("chat", "1", 0), pets_config.HAMSTERATOR_STORAGE_CAP[1])

    def test_corrupt_income_checkpoint_is_reset_without_crashing(self):
        self._build_level_one(now=datetime(2026, 8, 8, 10))
        data = economy._load("chat")
        data["users"]["1"]["effects"]["pet_hamsterator"]["last_hour"] = "not-a-date"
        economy._save("chat", data)
        result = pets.settle_passive_income("chat", "1", now=datetime(2026, 8, 8, 12))
        self.assertEqual(result["credited"], 0)
        repaired = economy._load("chat")["users"]["1"]["effects"]["pet_hamsterator"]["last_hour"]
        self.assertEqual(repaired, "2026-08-08T12:00:00")

    def test_upgrade_settles_old_rate_and_refuses_without_money(self):
        start = datetime(2026, 8, 8, 10)
        self._build_level_one(now=start)
        later = start + timedelta(hours=3)
        ok, message = pets.upgrade_hamsterator("chat", "1", 0, now=later)
        self.assertFalse(ok)
        self.assertIn("Нужно", message)
        self.assertEqual(economy.balance("chat", "1", 0), 3)
        economy.grant("chat", "1", pets_config.HAMSTERATOR_UPGRADE_COSTS[1], "test")
        ok, message = pets.upgrade_hamsterator("chat", "1", 0, now=later)
        self.assertTrue(ok, message)
        self.assertEqual(pets.hamsterator_level("chat", "1"), 2)
        self.assertEqual(pets.settle_passive_income("chat", "1", now=later + timedelta(hours=2))["credited"], 4)

    def test_facility_view_has_upgrade_callback_and_russian_copy(self):
        entry = "chat"
        economy.grant(entry, "1", pets_config.CAGE_PRICE, "test")
        self.assertTrue(pets.buy_cage(entry, "1", 0)[0])
        text, keyboard = pets_ui.hamsterator_view(entry, "1", 0)
        self.assertIn("Хомяколатор", text)
        callbacks = [pets_ui.parse_callback(button["callback_data"])[1]
                     for row in keyboard["inline_keyboard"] for button in row]
        self.assertIn("uphamsterator", callbacks)


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
        self.assertEqual(len([item for item in pets_config.ITEMS if item.slot == "amulet"]), 32)
        self.assertEqual(len([item for item in pets_config.ITEMS if item.slot == "boots"]), 32)
        self.assertEqual(len([item for item in pets_config.ITEMS if item.slot == "gloves"]), 32)
        new_drops = [
            item for item in pets_config.ITEMS
            if item.code.startswith(("amulet_", "bt", "gl"))
        ]
        self.assertEqual(len(new_drops), 90)
        self.assertTrue(all(item.source == "drop" and item.drop_weight > 0 for item in new_drops))
        self.assertEqual(len([item for item in new_drops if item.effect]), 30)

    def test_equipped_amulet_passive_reaches_combat_and_is_visible_in_the_bag(self):
        self._two_pets()
        amulet = next(item for item in pets_config.ITEMS if item.effect)
        data = pets._load("chat")
        data["pets"]["1"]["inventory"] = [amulet.code]
        pets._save("chat", data)

        self.assertTrue(pets.equip("chat", "1", amulet.code)[0])

        self.assertEqual(pets.equipped_combat_effects("chat", "1"), (amulet.effect,))
        text, _ = pets_ui.bag_items_view("chat", "1", 0, "amulet")
        self.assertIn(amulet.effect["text"], text)

    def test_unique_weapon_migration_removes_every_mop_and_deduplicates_once(self):
        self._two_pets()
        mop = pets_config.find_item("w003")
        duplicate = pets_config.find_item("w001")
        data = pets._load("chat")
        for user_id in ("1", "2"):
            data["pets"][user_id]["inventory"] = [mop.code, duplicate.code]
            data["pets"][user_id]["discovered"] = [mop.code, duplicate.code]
        data["pets"]["1"]["equipped"]["weapon"] = mop.code
        data["pets"]["1"]["locked_items"] = [mop.code]
        data["pets"]["1"]["pending_item_actions"] = {f"gift:{mop.code}": "token"}
        # The equipped duplicate is the copy the deterministic migration preserves.
        data["pets"]["2"]["equipped"]["weapon"] = duplicate.code
        pets._save("chat", data)

        report = pets.enforce_unique_weapons(["chat"])

        self.assertEqual(report["removed_mops"], 2)
        self.assertEqual(report["mop_grants"], 2)
        self.assertEqual(report["deduplicated"], 1)
        old_duplicate_price = pets_config.PRE_REBALANCE_WEAPON_BUY_PRICES[duplicate.code]
        self.assertEqual(economy.balance("chat", "1", 0), 100 + old_duplicate_price)
        self.assertEqual(economy.balance("chat", "2", 0), 100)
        first = pets.get_pet("chat", "1")
        second = pets.get_pet("chat", "2")
        self.assertNotIn(mop.code, first["inventory"])
        self.assertNotIn(mop.code, second["inventory"])
        self.assertIsNone(first["equipped"]["weapon"])
        self.assertNotIn(mop.code, first["locked_items"])
        self.assertNotIn(f"gift:{mop.code}", first["pending_item_actions"])
        self.assertNotIn(duplicate.code, first["inventory"])
        self.assertIn(duplicate.code, second["inventory"])
        self.assertIn(mop.code, first["discovered"])

        self.assertEqual(pets.enforce_unique_weapons(["chat"])["removed_mops"], 0)
        self.assertEqual(economy.balance("chat", "1", 0), 100 + old_duplicate_price)
        self.assertEqual(economy.balance("chat", "2", 0), 100)

    def test_sell_refuses_equipped_and_pays_explicit_resale(self):
        self._two_pets()
        item = next(
            weapon for weapon in pets_config.daily_storefront_weapons("chat", pets.today())
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
            weapon for weapon in pets_config.daily_storefront_weapons("chat", pets.today())
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

    def test_equipment_hub_routes_to_owned_bag_and_daily_shop(self):
        self._two_pets()
        text, keyboard = pets_ui.bag_view("chat", "1", 0)
        callbacks = [button["callback_data"] for row in keyboard["inline_keyboard"] for button in row]

        self.assertIn("Снаряжение", text)
        self.assertIn(pets_ui.callback_data("1", "bagitems", "weapon,0"), callbacks)
        self.assertIn(pets_ui.callback_data("1", "store"), callbacks)
        self.assertIn(pets_ui.callback_data("1", "collection"), callbacks)
        self.assertNotIn(pets_ui.callback_data("1", "slot", "weapon"), callbacks)

        _, store_keyboard = pets_ui.store_view("chat", "1", 0)
        store_callbacks = [
            button["callback_data"]
            for row in store_keyboard["inline_keyboard"]
            for button in row
        ]
        for slot in ("amulet", "gloves", "boots"):
            self.assertIn(
                pets_ui.callback_data("1", "slot", pets_ui.slot_argument(slot)),
                store_callbacks,
            )

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


class StorefrontAndCollectionTests(PetsTestCase):
    def _two_pets(self, entry="shop-chat"):
        self._tame(entry, "1", "One")
        self._tame(entry, "2", "Two")
        data = pets._load(entry)
        data["pets"]["1"]["level"] = pets_config.GIFT_MIN_PET_LEVEL
        pets._save(entry, data)

    def test_daily_storefront_is_stable_sized_and_changes_tomorrow(self):
        day = date(2026, 8, 8)
        first = pets_config.daily_storefront_weapons("shop-chat", day)
        again = pets_config.daily_storefront_weapons("shop-chat", day)
        tomorrow = pets_config.daily_storefront_weapons("shop-chat", day + timedelta(days=1))
        self.assertEqual(first, again)
        self.assertEqual(pets_config.DAILY_STOREFRONT_SIZE, 10)
        self.assertEqual(len(first), pets_config.DAILY_STOREFRONT_SIZE)
        self.assertEqual(len({item.code for item in first}), pets_config.DAILY_STOREFRONT_SIZE)
        self.assertNotEqual([item.code for item in first], [item.code for item in tomorrow])
        self.assertTrue(all(item.source == "shop" and item.slot == "weapon" for item in first))
        self.assertTrue(all(item.rarity != "cursed" for item in first))

    def test_core_purchase_refuses_weapon_outside_daily_window(self):
        entry = "shop-chat"
        self._two_pets(entry)
        offered = next(item for item in pets_config.daily_storefront_weapons(entry, pets.today())
                       if item.rarity not in {"rare", "legendary"})
        outside = next(item for item in pets_config.items_for_slot("weapon", "shop")
                       if item.code not in {offered.code for offered in pets_config.daily_storefront_weapons(entry, pets.today())})
        economy.grant(entry, "1", offered.price + outside.price, "test")
        self.assertTrue(pets.buy_item(entry, "1", 0, offered.code)[0])
        ok, note = pets.buy_item(entry, "1", 0, outside.code)
        self.assertFalse(ok)
        self.assertIn("витрин", note)

    def test_shop_weapon_can_have_only_one_owner_in_the_chat(self):
        entry = "shop-chat"
        self._two_pets(entry)
        item = next(
            weapon for weapon in pets.daily_storefront_weapons(entry, pets.today())
            if weapon.rarity not in {"rare", "legendary"}
        )
        economy.grant(entry, "1", item.price, "test")
        economy.grant(entry, "2", item.price, "test")

        self.assertTrue(pets.buy_item(entry, "1", 0, item.code)[0])
        remaining_stock = pets.daily_storefront_weapons(entry, pets.today())
        self.assertEqual(len(remaining_stock), pets_config.DAILY_STOREFRONT_SIZE)
        self.assertNotIn(item.code, {weapon.code for weapon in remaining_stock})
        ok, note = pets.buy_item(entry, "2", 0, item.code)
        self.assertFalse(ok)
        self.assertIn("принадлежит другому игроку", note)
        self.assertEqual(economy.balance(entry, "2", 0), item.price)

    def test_discovery_survives_sale_and_gift_and_old_inventory_migrates(self):
        entry = "shop-chat"
        self._two_pets(entry)
        first, second = [item for item in pets_config.daily_storefront_weapons(entry, pets.today())
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
        item = next(item for item in pets_config.daily_storefront_weapons(entry, pets.today())
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
            item.name for item in pets_config.daily_storefront_weapons(entry, pets.today())
        ]
        for current, following in zip(visible_names, visible_names[1:]):
            separator = text.index("\n\n", text.index(current))
            self.assertLess(separator, text.index(following))

    def test_store_numbers_every_item_and_groups_purchase_numbers_in_three_rows(self):
        entry = "shop-chat"
        self._two_pets(entry)
        stock = pets_config.daily_storefront_weapons(entry, pets.today())
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
        item = pets_config.daily_storefront_weapons(entry, pets.today())[0]
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

    def test_collection_lists_only_chat_discoveries_and_their_current_owners(self):
        entry = "shop-chat"
        self._two_pets(entry)
        first, second = pets_config.daily_storefront_weapons(entry, pets.today())[:2]
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
        """Gold and XP reward a harder win, while weak-target farming is capped."""
        cases = {
            -3: (125, 12),
            -2: (116, 12),
             0: (100, 10),
             2: (85, 8),
             3: (75, 8),
             9: (75, 8),  # the +3 stronger-winner penalty is the cap
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

        self.assertEqual(outcome["gold"], round(10 * 1.25 * 0.75))

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

        defender_fights_today_before = pets.get_pet(entry, "2")["fights_today"]
        defender_xp_before = pets.get_pet(entry, "2")["xp"]
        result = SimpleNamespace(winner="2", loser="1")  # attacker loses this one

        with patch("random.randint", return_value=pets_config.WIN_GOLD_MIN), \
             patch("random.random", return_value=1.0):  # no item drop
            outcome = pets.record_fight(entry, "1", "2", result, today)

        # Attacker's daily budget went down by exactly one.
        self.assertEqual(
            pets.fights_left(entry, "1", today),
            pets.daily_allowance(entry, "1", today) - 1,
        )
        # Defender's own budget is completely untouched.
        self.assertEqual(pets.get_pet(entry, "2")["fights_today"], defender_fights_today_before)

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

    def test_survivor_amulet_preserves_thirty_percent_of_loss_penalty(self):
        entry = "survivor-chat"
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
        economy.grant(entry, "2", 100, "test")
        result = SimpleNamespace(winner="1", loser="2")

        with patch("random.randint", return_value=10), patch("random.random", return_value=1.0):
            outcome = pets.record_fight(entry, "1", "2", result, date(2026, 8, 1))

        self.assertEqual(pets_config.loss_gold_for(10), 3)
        self.assertEqual(outcome["opponent_loss_gold"], 2)
        self.assertEqual(economy.balance(entry, "2", 0), 98)

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
        first, second = [item for item in pets_config.ITEMS if item.source == "drop"][:2]
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

        with patch("random.random", return_value=1.0):
            for day_offset in range(pets_config.HISTORY_LIMIT + 3):
                pets.record_fight(
                    entry, "1", "2", result, date(2026, 8, 1) + timedelta(days=day_offset)
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

    def test_legendaries_owned_across_the_chat_exhaust_the_shared_pity_pool(self):
        self._two_pets()
        legends = [
            item for item in pets_config.ITEMS
            if item.source == "drop" and item.rarity == "legendary"
        ]
        data = pets._load("chat")
        data["pets"]["1"]["inventory"] = [item.code for item in legends[::2]]
        data["pets"]["2"]["inventory"] = [item.code for item in legends[1::2]]
        data["pets"]["1"]["legendary_pity_wins"] = 123
        pets._save("chat", data)

        progress = pets.legendary_pity_progress("chat", "1")

        self.assertFalse(progress["eligible"])
        self.assertEqual(progress["wins_without_legend"], 0)

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

    def test_telemetry_tracks_sale_arena_gold_and_guard(self):
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
        pets.record_guardian_intervention("chat", "1", "2", date(2026, 8, 8))
        metrics = pets.economy_telemetry("chat")
        self.assertEqual(metrics["item_sale_gold"], pets_config.resale_value(item))
        self.assertEqual(metrics["arena_reward_gold"], outcome["gold"])
        self.assertEqual(metrics["guardian_interventions"], 1)

    def test_passive_telemetry_credits_once_per_settled_hour(self):
        start = datetime(2026, 8, 8, 10)
        economy.grant(
            "chat", "1", pets_config.CAGE_PRICE + pets_config.HAMSTERATOR_UPGRADE_COSTS[0], "test",
        )
        self.assertTrue(pets.buy_cage("chat", "1", 0)[0])
        self.assertTrue(pets.upgrade_hamsterator("chat", "1", 0, now=start)[0])
        later = start + timedelta(hours=2)
        self.assertEqual(pets.settle_passive_income("chat", "1", now=later)["credited"], 2)
        self.assertEqual(pets.settle_passive_income("chat", "1", now=later)["credited"], 0)
        self.assertEqual(pets.economy_telemetry("chat")["passive_gold_minted"], 2)


class FarmTests(PetsTestCase):
    def _build_farm(self, entry="farm", uid="1", level=1):
        self._tame(entry, uid)
        total = sum(pets_config.FARM_UPGRADE_COSTS[:level])
        economy.grant(entry, uid, total, "farm-test-funds")
        for expected_level in range(1, level + 1):
            ok, message = pets.upgrade_farm(entry, uid, 0)
            self.assertTrue(ok, message)
            self.assertEqual(pets.farm_level(entry, uid), expected_level)

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
        self.assertEqual(status["drop_chance"], pets_config.FARM_DROP_CHANCE + 0.05)

    def test_due_run_without_id_is_recovered_and_cannot_block_the_pet(self):
        entry, start = "farm", datetime(2026, 8, 1, 10)
        self._build_farm(entry)
        self.assertTrue(pets.start_farm(entry, "1", now=start)[0])
        data = pets._load(entry)
        reward = dict(data["pets"]["1"]["farm_run"]["reward"])
        data["pets"]["1"]["farm_run"].pop("run_id")
        pets._save(entry, data)

        receipt = pets.settle_completed_farms(entry, start + timedelta(hours=6))[0]
        self.assertTrue(receipt["run_id"].startswith("recovered-"))
        self.assertEqual(receipt["gold"], reward["gold"])
        self.assertFalse(pets.farm_status(entry, "1", start + timedelta(hours=6))["running"])
        self.assertTrue(pets.start_farm(entry, "1", now=start + timedelta(hours=6))[0])

    def test_farming_pet_cannot_attack_or_be_dealt_as_opponent(self):
        entry, start = "farm", datetime(2026, 8, 1, 10)
        self._build_farm(entry, "1")
        self._tame(entry, "2")
        self.assertTrue(pets.start_farm(entry, "1", now=start)[0])
        with patch("pets.app_now", return_value=start):
            self.assertFalse(pets.can_attack_in_arena(entry, "1", "2", start.date()))
            self.assertFalse(pets.can_attack_in_arena(entry, "2", "1", start.date()))
            self.assertIsNone(pets.find_opponent(entry, "2", rng=random.Random(1)))
            self.assertFalse(pets.claim_duel(entry, "1", "2", now=start)[0])
            result = SimpleNamespace(winner="1", loser="2")
            with self.assertRaises(ValueError):
                pets.record_fight(entry, "1", "2", result, start.date())


class MiscApiTests(PetsTestCase):
    def test_today_cage_level_and_balance_for(self):
        entry = "chat"
        self.assertIsInstance(pets.today(), date)
        self.assertEqual(pets.cage_level(entry, "1"), 0)

        self._tame(entry, "1")
        self.assertEqual(pets.cage_level(entry, "1"), 1)

        economy.grant(entry, "1", 42, "test")
        self.assertEqual(pets.balance_for(entry, "1", 0), 42)
        self.assertEqual(pets.balance_for(entry, "1", 0), economy.balance(entry, "1", 0))

    def test_fight_refresh_uses_the_same_local_midnight_as_daily_reset(self):
        moment = datetime(2026, 8, 9, 18, 35, 20)
        self.assertEqual(pets.fight_refresh_seconds(moment), 5 * 3600 + 24 * 60 + 40)

    def test_award_xp_reports_level_ups(self):
        entry = "chat"
        self._tame(entry, "1")
        new_level, gained = pets.award_xp(entry, "1", 1)
        self.assertEqual(new_level, 1)
        self.assertEqual(gained, 0)

        # Enough xp to blow well past several thresholds at once.
        new_level, gained = pets.award_xp(entry, "1", 10_000)
        self.assertGreater(gained, 0)
        self.assertEqual(new_level, pets.get_pet(entry, "1")["level"])
        self.assertLessEqual(new_level, pets_config.PET_MAX_LEVEL)


if __name__ == "__main__":
    unittest.main()
