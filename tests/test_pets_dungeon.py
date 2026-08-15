import sys
import random
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import economy
import pets
import pets_config
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

    def test_floors_have_variable_story_driven_mob_counts_and_bosses(self):
        first = dungeon.encounters_for_floor(1)
        self.assertEqual(len(first), 2)
        self.assertEqual(first, dungeon.encounters_for_floor(1))
        self.assertIn("Два", dungeon.floor_description(1))
        pack = dungeon.encounters_for_floor(2)
        self.assertEqual(len(pack), 10)
        self.assertTrue(all(row["gimmick"] == "pack_fury" for row in pack))
        self.assertIn("Десять", dungeon.floor_description(2))
        self.assertGreater(dungeon.pack_strength_multiplier(2, []), 1)
        self.assertEqual(dungeon.pack_strength_multiplier(2, range(9)), 1)
        boss = dungeon.encounters_for_floor(5)
        self.assertEqual(len(boss), 1)
        self.assertTrue(boss[0]["boss"])
        self.assertEqual(boss[0]["gimmick"], "reincarnate")

    def test_gimmick_bosses_are_preceded_by_equally_strong_plain_bosses(self):
        gatekeeper = dungeon.encounter(10, 0)
        dragon = dungeon.encounter(15, 0)
        colossus = dungeon.encounter(20, 0)
        aquarius = dungeon.encounter(25, 0)

        self.assertEqual(gatekeeper["gimmick"], "standard")
        self.assertEqual(colossus["gimmick"], "standard")
        self.assertEqual(gatekeeper["stats"], dragon["stats"])
        self.assertEqual(colossus["stats"], aquarius["stats"])
        self.assertEqual(dragon["gimmick"], "fire_only")
        self.assertEqual(aquarius["gimmick"], "spells_only")

    def test_antimage_is_a_distinct_boss_with_a_clear_reflection_hint(self):
        boss = dungeon.encounter(30, 0)
        self.assertEqual(boss["gimmick"], "antimagic")
        self.assertIn("85%", boss["hint"])

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

    def test_both_drop_chances_are_rolled_per_kill_not_fixed_per_floor(self):
        """Identical mobs on an identical floor must not offer identical odds."""
        floor = dungeon.SCROLL_LOOT_START_FLOOR + 5
        rolls = [dungeon.roll_reward(floor, False) for _ in range(20)]
        self.assertGreater(len({round(row["scroll_chance"], 6) for row in rolls}), 1)
        self.assertGreater(len({round(row["item_chance"], 6) for row in rolls}), 1)
        # The jitter is a spread around the baseline, not a licence to invent loot.
        baseline = dungeon.reward_for(floor, False, len(dungeon.encounters_for_floor(floor)))
        low, high = dungeon.LOOT_CHANCE_JITTER
        for row in rolls:
            self.assertLessEqual(row["scroll_chance"], baseline["scroll_chance"] * high + 1e-9)
            self.assertGreaterEqual(row["scroll_chance"], baseline["scroll_chance"] * low - 1e-9)

    def test_a_boss_is_worth_a_real_jump_in_scroll_chance(self):
        floor = dungeon.SCROLL_LOOT_START_FLOOR + 12   # deep enough to hit the mob cap
        mob = dungeon.reward_for(floor, False, enemy_count=1)
        boss = dungeon.reward_for(floor, True)
        self.assertGreater(boss["scroll_chance"], mob["scroll_chance"])
        self.assertAlmostEqual(
            boss["scroll_chance"], mob["scroll_chance"] * dungeon.BOSS_SCROLL_MULTIPLIER,
        )
        self.assertGreater(boss["item_chance"], mob["item_chance"])

    def test_crowded_rooms_pay_less_per_enemy_and_deep_floors_pay_more(self):
        pack = dungeon.reward_for(2, False, enemy_count=10)
        duo = dungeon.reward_for(2, False, enemy_count=2)
        deep = dungeon.reward_for(20, False, enemy_count=2)
        self.assertLess(pack["gold"], duo["gold"])
        self.assertLess(pack["xp"], duo["xp"])
        self.assertGreater(deep["gold"], duo["gold"])
        self.assertGreater(deep["xp"], duo["xp"])

    def test_rest_controls_show_coins_and_prompt(self):
        data = pets._load(self.entry)
        data["pets"][self.user_id]["dungeon_run"] = {
            "floor": 1, "hp": 10, "max_hp": 10, "cleared": [0, 1],
        }
        pets._save(self.entry, data)

        text, keyboard = pets_ui.dungeon_view(self.entry, self.user_id, 0)
        labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]

        self.assertIn("Отдохнуть?", text)
        self.assertTrue(any("🪙" in label for label in labels))
        self.assertEqual(
            [button["text"] for button in keyboard["inline_keyboard"][-1]],
            ["🚪 Выйти", "⬇️ Спуститься"],
        )

    def test_equipment_can_be_changed_only_after_clearing_a_floor(self):
        data = pets._load(self.entry)
        data["pets"][self.user_id]["inventory"].append("w001")
        data["pets"][self.user_id]["dungeon_run"] = {
            "floor": 1, "hp": 10, "max_hp": 10, "cleared": [],
        }
        pets._save(self.entry, data)
        self.assertFalse(pets.equip(self.entry, self.user_id, "w001")[0])

        data = pets._load(self.entry)
        data["pets"][self.user_id]["dungeon_run"]["cleared"] = [0, 1]
        pets._save(self.entry, data)
        self.assertTrue(pets.equip(self.entry, self.user_id, "w001")[0])

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
        self.assertEqual(len(state["encounters"]), 2)
        self.assertFalse(pets.equip(self.entry, self.user_id, "w001")[0])

    def test_dungeon_preview_scales_floor_reward_only_mildly_by_hero_level(self):
        data = pets._load(self.entry)
        record = data["pets"][self.user_id]
        record["level"] = 100
        record["dungeon_run"] = {
            "floor": 1, "hp": 10, "max_hp": 10, "cleared": [],
        }
        pets._save(self.entry, data)

        reward = pets.dungeon_status(self.entry, self.user_id)["encounters"][0]["reward"]

        self.assertEqual(
            reward["gold"],
            pets_config.gold_for_hero(reward["gold_base"], 100, "dungeon"),
        )
        self.assertGreater(reward["gold"], reward["gold_base"])
        self.assertLess(
            reward["gold_multiplier"], pets_config.hero_gold_multiplier(100, "arena"),
        )

    def test_dungeon_ticket_replaces_the_ruby_entry_fee_and_is_consumed(self):
        data = pets._load(self.entry)
        data["pets"][self.user_id]["stats"] = {
            "strength": 200, "health": 200, "agility": 200, "luck": 200, "endurance": 1,
        }
        pets._save(self.entry, data)
        pets.grant_dungeon_ticket(self.entry, self.user_id)

        ok, message = pets.enter_dungeon(self.entry, self.user_id)

        self.assertTrue(ok, message)
        self.assertIn("билету", message)
        self.assertEqual(pets.dungeon_tickets(self.entry, self.user_id), 0)
        self.assertEqual(pets.ruby_balance(self.entry, self.user_id), 0)

    def test_failed_escalator_does_not_consume_the_entry_ticket(self):
        data = pets._load(self.entry)
        data["pets"][self.user_id]["stats"] = {
            "strength": 200, "health": 200, "agility": 200, "luck": 200, "endurance": 1,
        }
        data["pets"][self.user_id]["dungeon_deepest"] = 2
        pets._save(self.entry, data)
        pets.grant_dungeon_ticket(self.entry, self.user_id)

        _text, keyboard = pets_ui.dungeon_view(self.entry, self.user_id, 0)
        escalator = next(
            button for row in keyboard["inline_keyboard"] for button in row
            if "Эскалатор" in button["text"]
        )
        self.assertIn(f"билет + {dungeon.ESCALATOR_RUBY_COST} 💎", escalator["text"])

        ok, message = pets.enter_dungeon(self.entry, self.user_id, escalator=True)

        self.assertFalse(ok)
        self.assertIn(str(dungeon.ESCALATOR_RUBY_COST), message)
        self.assertEqual(pets.dungeon_tickets(self.entry, self.user_id), 1)

    def test_first_two_floor_budgets_pay_for_a_full_rest_even_on_low_rolls(self):
        floor_one = dungeon.reward_for(1, False, enemy_count=2)["gold"]
        floor_two = dungeon.reward_for(2, False, enemy_count=10)["gold"]
        low_roll_gold = 2 * round(floor_one * .8) + 10 * round(floor_two * .8)

        self.assertGreaterEqual(low_roll_gold, dungeon.SHOP_FULL_HEAL_COST)

    def test_rune_effects_scale_with_the_equipped_pet_stats_in_dungeon_and_arena(self):
        data = pets._load(self.entry)
        record = data["pets"][self.user_id]
        record["equipped"]["weapon"] = "w001"
        record.setdefault("weapon_enchantments", {})["w001"] = "water"
        record["stats"] = {
            "strength": 200, "health": 200, "agility": 200, "luck": 200, "endurance": 1,
        }
        pets._save(self.entry, data)

        arena_effect = next(effect for effect in pets.equipped_combat_effects(self.entry, self.user_id)
                            if effect["code"] == "regen")
        dungeon_effect = next(effect for effect in pets._dungeon_fighter(
            pets.get_pet(self.entry, self.user_id), self.user_id
        ).effects if effect["code"] == "regen")

        self.assertGreaterEqual(arena_effect["value"], 60)
        self.assertEqual(dungeon_effect, arena_effect)

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

        # A run predating the identity fields is given one rather than left to collapse
        # every kill's loot token onto the same string.
        self.assertTrue(run.pop("run_id"))
        self.assertEqual(run, {
            "kills": 0, "floor": 1, "hp": 1, "max_hp": 1, "cleared": [], "boss_lives": 0,
            "partial_heals_used": 0, "full_heals_used": 0,
        })

    def test_the_run_identity_survives_the_normaliser_that_rebuilds_the_run(self):
        """The normaliser is a whitelist, so anything it forgets is dropped on every load.

        run_id and kills are what make each victory's loot key unique; losing them is
        exactly how the same mob came to pay out the same scroll for ever.
        """
        data = pets._load(self.entry)
        data["pets"][self.user_id]["dungeon_run"] = {
            "run_id": "abc123", "kills": 7, "floor": 4, "hp": 50, "max_hp": 50, "cleared": [],
        }
        pets._save(self.entry, data)

        run = pets.get_pet(self.entry, self.user_id)["dungeon_run"]
        self.assertEqual(run["run_id"], "abc123")
        self.assertEqual(run["kills"], 7)

    def _unstoppable_runner(self, floor):
        """A runner that always wins, parked on `floor` with a fresh run."""
        data = pets._load(self.entry)
        record = data["pets"][self.user_id]
        record["level"] = 200
        for key in pets_config.STAT_KEYS:
            record["stats"][key] = 4_000
        record["dungeon_run"] = {
            "run_id": "run-a", "kills": 0, "floor": floor,
            "hp": 99_999, "max_hp": 99_999, "cleared": [],
        }
        pets._save(self.entry, data)

    def _rekill(self, floor, index=0, times=1):
        """Kill the same encounter `times` over, resetting only what a re-entry resets."""
        payloads = []
        for _ in range(times):
            data = pets._load(self.entry)
            data["pets"][self.user_id]["dungeon_run"]["cleared"] = []
            data["pets"][self.user_id]["dungeon_run"]["hp"] = 99_999
            pets._save(self.entry, data)
            ok, _message, payload = pets.dungeon_fight(self.entry, self.user_id, index)
            self.assertTrue(ok)
            payloads.append(payload)
        return payloads

    def test_killing_the_same_mob_twice_does_not_replay_the_first_kills_loot(self):
        """The bug this guards: loot was keyed on floor+index alone.

        Those keys are memoised (grant_scroll_reward) or used as an RNG seed
        (grant_random_drop), so a re-killable enemy behind a fixed key paid out the same
        scroll and the same item for ever.
        """
        floor = dungeon.SCROLL_LOOT_START_FLOOR + 5
        self._unstoppable_runner(floor)
        self._rekill(floor, times=6)

        log = pets._load(self.entry)["scroll_wallets"][self.user_id]["reward_log"]
        sources = [key for key in log if key.startswith("dungeon:")]
        self.assertEqual(len(sources), 6, "each kill must own its loot key")
        self.assertEqual(len(set(sources)), 6)

    def test_a_run_of_dungeon_misses_never_forces_a_guaranteed_scroll(self):
        """No pity in the dungeon: a floor that owes a scroll every Nth kill is a shop."""
        receipts = [
            pets.grant_scroll_reward(
                self.entry, self.user_id, source=f"dungeon:none:{n}", kind="dungeon",
                chance=0.0, pity_after=None,
            )
            for n in range(40)
        ]
        self.assertTrue(all(not row["granted"] for row in receipts))
        self.assertTrue(all(row["reason"] == "miss" for row in receipts))
        self.assertTrue(all(row["pity_after"] is None for row in receipts))
        # And no counter is quietly ticking behind it: the dungeon has no pity bucket.
        self.assertNotIn(
            "dungeon", pets._load(self.entry)["scroll_wallets"][self.user_id]["pity"],
        )

    def test_scrolls_still_drop_and_are_not_the_same_one_every_time(self):
        # A deep ORDINARY floor: the gimmick bosses (hydra, reincarnate) deliberately end
        # a turn without paying anything, so they are useless for measuring drop rates.
        floor = 41
        self.assertFalse(dungeon.is_boss_floor(floor))
        self._unstoppable_runner(floor)
        # The floor caps at a 12.5% baseline (halved from the old 25%), so the sample is
        # doubled to 240 to keep the same margin of safety the old 120-kill/25% pair had.
        granted = [
            payload["scroll"]["code"]
            for payload in self._rekill(floor, times=240)
            if payload.get("scroll") and payload["scroll"].get("granted")
        ]
        self.assertGreater(len(granted), 3, "a 12.5% baseline over 240 kills must pay out")
        self.assertGreater(len(set(granted)), 1, "the same scroll every time is the bug")

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
            "floor": 25, "hp": 500, "max_hp": 500, "cleared": [],
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
        record["dungeon_run"] = {"floor": 45, "hp": 500, "max_hp": 500, "cleared": []}
        record["equipped"]["weapon"] = "w001"
        record.setdefault("weapon_enchantments", {})["w001"] = "frost"
        pets._save(self.entry, data)
        result = SimpleNamespace(winner=self.user_id, rounds=(), final_hp={})

        with patch("pets.pets_combat.simulate", return_value=result) as simulate:
            ok, _message, _receipt = pets.dungeon_fight(self.entry, self.user_id, 0)

        self.assertTrue(ok)
        self.assertEqual(simulate.call_args.args[0].damage_multiplier, 5)

class DungeonRestTests(DungeonTests):
    """Rests are rationed per run, and the ration is printed on the button that spends it."""

    def _cleared_floor(self, gold=100_000):
        economy.grant(self.entry, self.user_id, gold, "rest-test")
        data = pets._load(self.entry)
        data["pets"][self.user_id]["dungeon_run"] = {
            "floor": 1, "hp": 10, "max_hp": 1000,
            "cleared": [row["index"] for row in dungeon.encounters_for_floor(1)],
        }
        pets._save(self.entry, data)

    def test_each_rest_runs_out_after_its_own_allowance(self):
        self._cleared_floor()
        for remaining in range(dungeon.SHOP_PARTIAL_HEAL_USES - 1, -1, -1):
            ok, message = pets.dungeon_rest(self.entry, self.user_id, 0, "partial")
            self.assertTrue(ok, message)
            self.assertIn(f"Осталось таких лечений: {remaining}", message)

        ok, message = pets.dungeon_rest(self.entry, self.user_id, 0, "partial")
        self.assertFalse(ok)
        self.assertIn("кончилось", message)
        # The two allowances are separate: burning every partial leaves the full rests.
        self.assertTrue(pets.dungeon_rest(self.entry, self.user_id, 0, "full")[0])

    def test_a_refused_rest_does_not_take_the_coins(self):
        """The limit is checked before economy.spend, or the ration would be charged for."""
        self._cleared_floor()
        for _ in range(dungeon.SHOP_FULL_HEAL_USES):
            self.assertTrue(pets.dungeon_rest(self.entry, self.user_id, 0, "full")[0])
        before = economy.balance(self.entry, self.user_id, 0)

        self.assertFalse(pets.dungeon_rest(self.entry, self.user_id, 0, "full")[0])

        self.assertEqual(economy.balance(self.entry, self.user_id, 0), before)

    def test_the_remaining_count_reaches_the_buttons(self):
        self._cleared_floor()
        pets.dungeon_rest(self.entry, self.user_id, 0, "partial")

        state = pets.dungeon_status(self.entry, self.user_id)
        self.assertEqual(state["partial_heals_left"], dungeon.SHOP_PARTIAL_HEAL_USES - 1)
        self.assertEqual(state["full_heals_left"], dungeon.SHOP_FULL_HEAL_USES)
        self.assertEqual(state["partial_heal_percent"], 30)

        _text, keyboard = pets_ui.dungeon_view(self.entry, self.user_id, 0)
        labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]
        self.assertTrue(any("+30% HP (2)" in label for label in labels), labels)
        self.assertTrue(any("+100% HP (3)" in label for label in labels), labels)

    def test_the_ration_survives_the_normaliser_that_rebuilds_the_run(self):
        """That rebuild is a whitelist: dropped here, the count would reset on every load."""
        self._cleared_floor()
        pets.dungeon_rest(self.entry, self.user_id, 0, "partial")
        pets.dungeon_rest(self.entry, self.user_id, 0, "full")

        run = pets.get_pet(self.entry, self.user_id)["dungeon_run"]
        self.assertEqual(run["partial_heals_used"], 1)
        self.assertEqual(run["full_heals_used"], 1)


class HydraTests(DungeonTests):
    """The three-headed boss, which used to be mathematically unwinnable.

    Every head carried a FULL boss's health pool, each press resolved a single action, and
    every third press healed all three heads back up to half -- including heads already
    beaten below that, so a hit could leave a head healthier than it started. Net progress
    was structurally zero. These tests pin the properties that make it a fight again.
    """

    HYDRA_FLOOR = 40

    def _park_on_the_hydra(self, heads=None, hp=500):
        data = pets._load(self.entry)
        run = {
            "floor": self.HYDRA_FLOOR, "hp": hp, "max_hp": hp, "cleared": [],
        }
        if heads is not None:
            run["hydra_head_hp"] = list(heads)
        data["pets"][self.user_id]["dungeon_run"] = run
        pets._save(self.entry, data)

    def _win_one_head(self, remaining_hp=0):
        """A press the hero wins, leaving the current head at `remaining_hp`."""
        round_ = pets.pets_combat.Round(1, self.user_id, "hit", 200, 500, 0, "")
        return SimpleNamespace(
            winner=self.user_id, rounds=(round_,),
            final_hp={f"dungeon:boss_{self.HYDRA_FLOOR}": remaining_hp},
        )

    def test_three_heads_share_one_boss_rather_than_owning_a_boss_each(self):
        self._park_on_the_hydra()
        with patch("pets.pets_combat.simulate", return_value=self._win_one_head(1)) as simulate:
            pets.dungeon_fight(self.entry, self.user_id, 0)

        run = pets.get_pet(self.entry, self.user_id)["dungeon_run"]
        self.assertEqual(len(run["hydra_head_hp"]), dungeon.HYDRA_HEADS)
        # A press is a whole exchange now. One action per press is what made three
        # full-health heads hopeless: a head needed hundreds of them.
        self.assertNotIn("max_actions", simulate.call_args.kwargs)

    def test_a_head_left_alive_grows_back_but_never_past_its_own_maximum(self):
        self._park_on_the_hydra(heads=[80, 100, 100])
        with patch("pets.pets_combat.simulate", return_value=self._win_one_head(40)):
            ok, message, receipt = pets.dungeon_fight(self.entry, self.user_id, 0)

        run = pets.get_pet(self.entry, self.user_id)["dungeon_run"]
        self.assertTrue(ok)
        self.assertTrue(receipt["regenerated"])
        self.assertIn("затягивает раны", message)
        # It grew from the 40 it was left on, and it is still the wounded head -- the
        # other two are untouched, and nothing was healed above its own ceiling.
        self.assertGreater(run["hydra_head_hp"][0], 40)
        self.assertEqual(run["hydra_head_hp"][1:], [100, 100])

    def test_a_felled_head_stays_felled_even_while_another_grows_back(self):
        """The old rule healed dead heads back to half. Progress has to be permanent.

        Head 0 is already down, so the press lands on head 1 and leaves it alive -- the
        one case where regrowth fires. The dead head must not come along for the ride.
        """
        self._park_on_the_hydra(heads=[0, 100, 100])
        with patch("pets.pets_combat.simulate", return_value=self._win_one_head(60)):
            ok, _message, receipt = pets.dungeon_fight(self.entry, self.user_id, 0)

        run = pets.get_pet(self.entry, self.user_id)["dungeon_run"]
        self.assertTrue(ok)
        self.assertTrue(receipt["regenerated"], "the wounded head 1 is the one regrowing")
        self.assertEqual(run["hydra_head_hp"][0], 0, "a felled head must stay felled")
        self.assertGreater(run["hydra_head_hp"][1], 60)

    def test_the_last_head_falling_clears_the_floor_instead_of_ending_the_run(self):
        """Killing the hydra used to be able to register as a defeat.

        The victory path opens by checking `result.winner`, and the last head can take the
        hero's final blow while still out-damaging them on the tally -- so falling through
        to that check deleted the whole run at the moment of victory.
        """
        self._park_on_the_hydra(heads=[0, 0, 40])
        # The hero kills the head but LOSES the damage tally for this one exchange.
        losing_tally = SimpleNamespace(
            winner="dungeon:boss_40",
            rounds=(pets.pets_combat.Round(1, self.user_id, "hit", 40, 500, 0, ""),),
            final_hp={"dungeon:boss_40": 0},
        )
        with patch("pets.pets_combat.simulate", return_value=losing_tally):
            ok, message, receipt = pets.dungeon_fight(self.entry, self.user_id, 0)

        run = pets.get_pet(self.entry, self.user_id)["dungeon_run"]
        self.assertTrue(ok, message)
        self.assertIsNotNone(run, "the run must survive killing the hydra")
        self.assertIn(0, run["cleared"])
        self.assertIsNone(run.get("hydra_head_hp"))
        self.assertIn("reward", receipt)

    def test_a_run_saved_under_the_old_rules_is_rescued_rather_than_left_stuck(self):
        """Heads stored at a full boss's HP each are three times what the fight can hold.

        Somebody was soft-locked mid-hydra when the numbers changed under them; the clamp
        has to bring those heads down to the new maximum instead of leaving that run
        permanently unwinnable.
        """
        self._park_on_the_hydra(heads=[14060, 14060, 14060], hp=5000)
        with patch("pets.pets_combat.simulate", return_value=self._win_one_head(99_999)):
            pets.dungeon_fight(self.entry, self.user_id, 0)

        run = pets.get_pet(self.entry, self.user_id)["dungeon_run"]
        self.assertTrue(all(hp < 14060 for hp in run["hydra_head_hp"]), run["hydra_head_hp"])


if __name__ == "__main__":
    unittest.main()
