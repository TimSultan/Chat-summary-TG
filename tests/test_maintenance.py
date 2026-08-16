"""The pause switch, and the promise it makes to a player who is mid-game.

A pause has to hold two things at once: nothing new may START, and everything already
running must be untouched. Both halves are asserted here, because the failure mode of
getting the second one wrong is far worse than the first -- refusing a fight is an
inconvenience, eating a farm shift is somebody's evening.
"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_listener
import economy
import maintenance
import pets
import pets_config
import preflight
import stats


class PauseFlagTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def test_it_starts_open_and_survives_a_restart_once_closed(self):
        self.assertFalse(maintenance.is_paused())
        maintenance.pause("Обновление", by="Sultan")
        # A restart is just another read of the same file -- that is the whole reason it
        # is a file and not an environment variable.
        self.assertTrue(maintenance.is_paused())
        self.assertEqual(maintenance.status()["by"], "Sultan")
        self.assertIn("Обновление", maintenance.notice())
        maintenance.resume(by="Sultan")
        self.assertFalse(maintenance.is_paused())

    def test_a_pause_with_no_words_still_explains_itself(self):
        maintenance.pause("")
        self.assertEqual(maintenance.notice(), maintenance.DEFAULT_NOTICE)
        # The default has to answer the questions in the order they arrive: what is
        # happening, is my progress safe, how long.
        self.assertIn("обновление", maintenance.DEFAULT_NOTICE.lower())
        self.assertIn("на месте", maintenance.DEFAULT_NOTICE)

    def test_a_damaged_flag_leaves_the_game_open(self):
        """Fail OPEN, not closed. A corrupt byte in the file that exists to prevent an
        outage must not become one."""
        maintenance.pause("Обновление")
        maintenance._path().write_text("{ this is not json", encoding="utf-8")
        self.assertFalse(maintenance.is_paused())

    def test_the_environment_variable_is_only_a_boot_default(self):
        """Useful for bringing a risky release up already closed -- but once the flag file
        exists it is the answer, or turning the game back on would need a redeploy."""
        with patch.dict("os.environ", {"GAME_PAUSED": "1"}):
            self.assertTrue(maintenance.is_paused())
            maintenance.resume()
            self.assertFalse(maintenance.is_paused())


class PausedGameStillKeepsItsPromisesTests(unittest.TestCase):
    """What a pause must NOT do."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def test_a_farm_shift_running_through_a_pause_is_paid_in_full(self):
        """Shifts are settled from timestamps whenever they are next read, so a pause
        neither pays them early nor loses them. This is the promise the notice makes."""
        entry, start = "chat", datetime(2026, 8, 9, 9, 0)
        economy.grant(entry, "1", pets_config.TAME_PRICE + pets_config.FARM_UPGRADE_COSTS[0], "t")
        self.assertTrue(pets.buy_cage(entry, "1", 0)[0])
        self.assertTrue(pets.tame(entry, "1", 0, "Кабанчик", "file", "Player")[0])
        self.assertTrue(pets.upgrade_farm(entry, "1", 0, now=start)[0])
        self.assertTrue(pets.start_farm(entry, "1", 8, now=start)[0])

        maintenance.pause("Обновление")
        # The whole shift elapses while the game is closed.
        receipts = pets.settle_completed_farms(entry, now=start + timedelta(hours=8))
        maintenance.resume()

        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["hours"], 8)
        self.assertGreater(receipts[0]["gold"], 0)

    def test_reading_the_game_is_never_blocked(self):
        """Somebody who opens the game during an update should find their creature and an
        explanation, not an error. An explanation on a broken screen is no explanation."""
        entry = "chat"
        economy.grant(entry, "1", pets_config.TAME_PRICE, "t")
        pets.buy_cage(entry, "1", 0)
        pets.tame(entry, "1", 0, "Кабанчик", "file", "Player")
        maintenance.pause("Обновление")
        self.assertEqual(pets.get_pet(entry, "1")["name"], "Кабанчик")
        self.assertIsNotNone(pets.farm_status(entry, "1"))
        self.assertIsNotNone(pets.pet_leaderboard(entry))


class PauseGateTests(unittest.TestCase):
    def test_the_bot_allowlist_covers_navigation_and_nothing_that_plays(self):
        """Fail closed: the gate lists what is SAFE, so an action added later is refused
        until somebody has thought about it."""
        safe = bot_listener.PAUSE_SAFE_PET_ACTIONS
        for reading in ("main", "pet", "bag", "fight", "farm", "history", "leaderboard"):
            with self.subTest(action=reading):
                self.assertIn(reading, safe)
        # Everything that starts something, spends something or resolves a fight.
        for playing in ("search", "attack", "mob", "mobfight", "buycage", "buy",
                        "farmstart", "quarrystart", "dungeonenter", "dungeonfight",
                        "upfarm", "sell", "gift", "reforge", "cpoker"):
            with self.subTest(action=playing):
                self.assertNotIn(playing, safe)


class PreflightTests(unittest.TestCase):
    """The round-trip check that exists because normalisers keep eating fields."""

    def test_a_dropped_field_is_reported_as_lost(self):
        before = {"pets": {"1": {"dungeon_run": {"run_id": "abc", "kills": 7, "floor": 3}}}}
        after = {"pets": {"1": {"dungeon_run": {"floor": 3}}}}
        findings = preflight._compare(before, after)
        self.assertTrue(any("ПОТЕРЯНО" in line and "run_id" in line for line in findings))
        self.assertTrue(any("ПОТЕРЯНО" in line and "kills" in line for line in findings))

    def test_a_shortened_list_is_reported_even_though_no_key_vanished(self):
        """An inventory losing an item keeps every key it had -- the list is simply
        shorter. Recorded by length for exactly that reason."""
        before = {"pets": {"1": {"inventory": ["w001", "w002", "w003"]}}}
        after = {"pets": {"1": {"inventory": ["w001"]}}}
        findings = preflight._compare(before, after)
        self.assertTrue(any("УКОРОЧЕНО" in line for line in findings), findings)

    def test_a_wallet_going_down_is_a_failure_but_a_repair_is_not(self):
        shrunk = preflight._compare({"rubies": {"1": 140}}, {"rubies": {"1": 12}})
        self.assertTrue(any("УМЕНЬШИЛОСЬ" in line for line in shrunk))
        # A default being filled in is the normaliser working, not a loss.
        repaired = preflight._compare({"pets": {"1": {}}}, {"pets": {"1": {"level": 1}}})
        self.assertFalse(any(line.startswith("❌") for line in repaired), repaired)

    def test_an_unchanged_store_reports_nothing_at_all(self):
        same = {"pets": {"1": {"name": "Кабанчик", "inventory": ["w001"]}}}
        self.assertEqual(preflight._compare(same, dict(same)), [])

    def test_it_checks_the_call_that_took_the_service_down(self):
        """attach() losing a parameter its caller passes is a boot crash, and preflight
        runs before a deploy precisely to catch that class of thing."""
        self.assertEqual(preflight.check_imports(), [])


if __name__ == "__main__":
    unittest.main()
