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

        item = pets_config.find_item("stick")  # weapon, +6 strength
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
        stick = pets_config.find_item("stick")
        fork = pets_config.find_item("fork")
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

    def test_find_opponent_prefers_candidates_inside_the_power_window(self):
        entry = "chat"
        self._tame(entry, "1")
        self._tame(entry, "2")
        self._tame(entry, "3")
        data = pets._load(entry)
        data["pets"]["1"]["stats"]["strength"] = 20
        data["pets"]["2"]["stats"]["strength"] = 22
        data["pets"]["3"]["stats"]["strength"] = 80
        pets._save(entry, data)

        rng = random.Random(1)
        seen = set()
        for _ in range(25):
            opponent = pets.find_opponent(entry, "1", rng=rng)
            self.assertIsNotNone(opponent)
            self.assertNotEqual(opponent, "1")
            seen.add(opponent)
        self.assertEqual(seen, {"2"})

    def test_find_opponent_uses_the_nearest_power_when_nobody_is_in_window(self):
        entry = "chat"
        self._tame(entry, "1")
        self._tame(entry, "2")
        self._tame(entry, "3")
        data = pets._load(entry)
        data["pets"]["1"]["stats"]["strength"] = 1
        data["pets"]["2"]["stats"]["strength"] = 70
        data["pets"]["3"]["stats"]["strength"] = 80
        pets._save(entry, data)

        opponent = pets.find_opponent(entry, "1", rng=random.Random(1))
        self.assertEqual(opponent, "2")

    def test_find_opponent_excludes_the_current_card_when_rerolling(self):
        entry = "chat"
        self._tame(entry, "1")
        self._tame(entry, "2")
        self._tame(entry, "3")
        opponent = pets.find_opponent(entry, "1", rng=random.Random(1), exclude_ids={"2"})
        self.assertEqual(opponent, "3")

    def test_opponent_cycle_is_seeded_and_never_repeats(self):
        entry = "chat"
        for user_id in ("1", "2", "3", "4", "5"):
            self._tame(entry, user_id)
        first = pets.opponent_cycle(entry, "1", 123)
        second = pets.opponent_cycle(entry, "1", 123)
        self.assertEqual(first, second)
        self.assertEqual(len(first), len(set(first)))

    def test_find_opponent_returns_none_only_when_the_chat_has_no_other_pet(self):
        entry = "chat"
        self._tame(entry, "1")
        self.assertIsNone(pets.find_opponent(entry, "1", rng=random.Random(1)))


class RecordFightTests(PetsTestCase):
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
