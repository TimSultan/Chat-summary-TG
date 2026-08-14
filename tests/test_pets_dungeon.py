import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pets
import pets_dungeon as dungeon
import pets_ui


class DungeonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch("stats._stats_dir", return_value=Path(self.temp.name))
        self.patch.start()
        self.entry, self.user_id = "dungeon-test", "42"
        pets.buy_cage(self.entry, self.user_id, 100_000)
        pets.tame(self.entry, self.user_id, 100_000, "Hero", None, "Tester")

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_floor_has_three_fixed_theme_mobs_and_every_fifth_floor_is_a_boss(self):
        first = dungeon.encounters_for_floor(1)
        self.assertEqual(len(first), 3)
        self.assertEqual(first, dungeon.encounters_for_floor(1))
        boss = dungeon.encounters_for_floor(5)
        self.assertEqual(len(boss), 1)
        self.assertTrue(boss[0]["boss"])
        self.assertEqual(boss[0]["gimmick"], "reincarnate")

    def test_reward_receipt_includes_loot_and_scroll(self):
        text = pets_ui.dungeon_reward_text({
            "reward": {"gold": 25, "xp": 15},
            "dropped": {"name": "Клинок", "auto_equipped": True},
            "scroll": {"granted": True, "icon": "🔥", "name": "Комета"},
        })
        self.assertIn("+25", text)
        self.assertIn("Клинок", text)
        self.assertIn("Комета", text)

    def test_dungeon_requires_five_rubies_to_enter(self):
        data = pets._load(self.entry)
        data["pets"][self.user_id]["stats"] = {
            "strength": 200, "health": 200, "agility": 200, "luck": 200, "endurance": 1,
        }
        pets._save(self.entry, data)
        ok, message = pets.enter_dungeon(self.entry, self.user_id)
        self.assertFalse(ok)
        self.assertIn(str(dungeon.ENTRY_RUBY_COST), message)
        state = pets.dungeon_status(self.entry, self.user_id)
        self.assertTrue(state["available"])
        self.assertEqual(state["entry_cost"], dungeon.ENTRY_RUBY_COST)

    def test_entry_is_gated_then_persists_the_run(self):
        ok, _ = pets.enter_dungeon(self.entry, self.user_id)
        self.assertFalse(ok)
        data = pets._load(self.entry)
        data["pets"][self.user_id]["stats"] = {
            "strength": 200, "health": 200, "agility": 200, "luck": 200, "endurance": 1,
        }
        pets._save(self.entry, data)
        pets.grant_rubies(self.entry, self.user_id, dungeon.ENTRY_RUBY_COST)
        ok, message = pets.enter_dungeon(self.entry, self.user_id)
        self.assertTrue(ok, message)
        state = pets.dungeon_status(self.entry, self.user_id)
        self.assertTrue(state["active"])
        self.assertEqual(state["floor"], 1)
        self.assertEqual(len(state["encounters"]), 3)
        self.assertFalse(pets.equip(self.entry, self.user_id, "w001")[0])

    def test_quit_clears_a_malformed_dungeon_run(self):
        data = pets._load(self.entry)
        data["pets"][self.user_id]["dungeon_run"] = {
            "floor": "broken", "hp": None, "cleared": "broken",
        }
        pets._save(self.entry, data)

        ok, message = pets.quit_dungeon(self.entry, self.user_id)

        self.assertTrue(ok, message)
        self.assertIsNone(pets.get_pet(self.entry, self.user_id)["dungeon_run"])


if __name__ == "__main__":
    unittest.main()