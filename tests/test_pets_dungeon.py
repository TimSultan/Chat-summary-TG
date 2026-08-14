import sys
import random
import tempfile
import unittest
from types import SimpleNamespace
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

    def test_each_boss_has_a_distinct_hidden_quirk_and_lore_hint(self):
        bosses = [dungeon.encounter(floor, 0) for floor in range(5, 35, 5)]
        self.assertEqual(len({boss["gimmick"] for boss in bosses}), len(bosses))
        self.assertTrue(all(boss["hint"] and "только" not in boss["hint"].lower() for boss in bosses))

    def test_reward_receipt_includes_loot_and_scroll(self):
        text = pets_ui.dungeon_reward_text({
            "reward": {"gold": 25, "xp": 15},
            "dropped": {"name": "Клинок", "auto_equipped": True},
            "scroll": {"granted": True, "icon": "🔥", "name": "Комета"},
        })
        self.assertIn("+25", text)
        self.assertIn("Клинок", text)
        self.assertIn("Комета", text)
        self.assertNotIn("<b>", text)

    def test_rewards_vary_between_mobs(self):
        rewards = [dungeon.roll_reward(3, False, random.Random(seed)) for seed in range(5)]
        self.assertGreater(len({reward["xp"] for reward in rewards}), 1)
        self.assertGreater(len({reward["item_chance"] for reward in rewards}), 1)

    def test_rest_controls_show_coins_and_prompt(self):
        data = pets._load(self.entry)
        data["pets"][self.user_id]["dungeon_run"] = {
            "floor": 1, "hp": 10, "max_hp": 10, "cleared": [0, 1, 2],
        }
        pets._save(self.entry, data)

        text, keyboard = pets_ui.dungeon_view(self.entry, self.user_id, 0)
        labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]

        self.assertIn("Отдохнуть?", text)
        self.assertTrue(any("🪙" in label for label in labels))

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

    def test_malformed_dungeon_run_is_repaired_before_a_fight(self):
        data = pets._load(self.entry)
        data["pets"][self.user_id]["dungeon_run"] = {
            "floor": "broken", "hp": None, "cleared": "broken",
        }
        pets._save(self.entry, data)

        run = pets.get_pet(self.entry, self.user_id)["dungeon_run"]

        self.assertEqual(run, {
            "floor": 1, "hp": 1, "max_hp": 1, "cleared": [], "boss_lives": 0,
        })

    def test_existing_pet_owners_receive_three_dungeon_tickets_once(self):
        self.assertEqual(pets.grant_dungeon_ticket_gift([self.entry]), 1)
        self.assertEqual(pets.dungeon_tickets(self.entry, self.user_id), 3)
        self.assertEqual(pets.grant_dungeon_ticket_gift([self.entry]), 0)
        self.assertEqual(pets.dungeon_tickets(self.entry, self.user_id), 3)

    def test_first_dungeon_fight_resolves_without_callback_error(self):
        data = pets._load(self.entry)
        data["pets"][self.user_id]["stats"] = {
            "strength": 200, "health": 200, "agility": 200, "luck": 200, "endurance": 1,
        }
        pets._save(self.entry, data)
        pets.grant_dungeon_ticket(self.entry, self.user_id)
        self.assertTrue(pets.enter_dungeon(self.entry, self.user_id)[0])

        _ok, _message, receipt = pets.dungeon_fight(self.entry, self.user_id, 0)

        self.assertIsNotNone(receipt)

    def test_dungeon_runner_cannot_attack_but_remains_an_arena_defender(self):
        other_user = "43"
        pets.buy_cage(self.entry, other_user, 100_000)
        pets.tame(self.entry, other_user, 100_000, "Opponent", None, "Tester")
        data = pets._load(self.entry)
        data["pets"][self.user_id]["dungeon_run"] = {
            "floor": 1, "hp": 1, "max_hp": 10, "cleared": [],
        }
        pets._save(self.entry, data)

        self.assertTrue(pets.is_in_dungeon(self.entry, self.user_id))
        self.assertFalse(pets.can_attack_in_arena(self.entry, self.user_id, other_user))
        self.assertTrue(pets.can_attack_in_arena(self.entry, other_user, self.user_id))

    def test_magic_boss_allows_the_fight_without_magic_but_wins_it(self):
        data = pets._load(self.entry)
        data["pets"][self.user_id]["dungeon_run"] = {
            "floor": 15, "hp": 500, "max_hp": 500, "cleared": [],
        }
        pets._save(self.entry, data)

        ok, message, receipt = pets.dungeon_fight(self.entry, self.user_id, 0)

        self.assertFalse(ok)
        self.assertIn("Аквариус", message)
        self.assertIsNotNone(receipt)
        self.assertIsNone(pets.get_pet(self.entry, self.user_id)["dungeon_run"])

    def test_frost_boss_grants_prepared_pet_elemental_damage_bonus(self):
        data = pets._load(self.entry)
        record = data["pets"][self.user_id]
        record["dungeon_run"] = {"floor": 30, "hp": 500, "max_hp": 500, "cleared": []}
        record["equipped"]["weapon"] = "w001"
        record.setdefault("weapon_enchantments", {})["w001"] = "frost"
        pets._save(self.entry, data)
        result = SimpleNamespace(winner=self.user_id, rounds=(), final_hp={})

        with patch("pets.pets_combat.simulate", return_value=result) as simulate:
            ok, _message, _receipt = pets.dungeon_fight(self.entry, self.user_id, 0)

        self.assertTrue(ok)
        self.assertEqual(simulate.call_args.args[0].damage_multiplier, 5)

    def test_hydra_restores_all_heads_after_third_incomplete_move(self):
        data = pets._load(self.entry)
        data["pets"][self.user_id]["dungeon_run"] = {
            "floor": 25, "hp": 500, "max_hp": 500, "cleared": [],
            "hydra_head_hp": [0, 100, 100], "hydra_moves": 2,
        }
        pets._save(self.entry, data)
        round_ = pets.pets_combat.Round(1, self.user_id, "hit", 200, 500, 0, "")
        result = SimpleNamespace(winner=self.user_id, rounds=(round_,), final_hp={"dungeon:boss_25": 0})

        with patch("pets.pets_combat.simulate", return_value=result) as simulate:
            ok, message, receipt = pets.dungeon_fight(self.entry, self.user_id, 0)

        run = pets.get_pet(self.entry, self.user_id)["dungeon_run"]
        self.assertTrue(ok)
        self.assertTrue(receipt["regenerated"])
        self.assertIn("зазвучали вновь", message)
        self.assertEqual(simulate.call_args.kwargs["max_actions"], 1)
        self.assertEqual(run["hydra_moves"], 0)
        self.assertEqual(len(set(run["hydra_head_hp"])), 1)
        self.assertGreater(run["hydra_head_hp"][0], 0)


if __name__ == "__main__":
    unittest.main()