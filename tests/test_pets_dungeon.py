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
        # The exit is the narrow one on the left: Telegram sizes a row's buttons equally,
        # so sharing a row with the descent is what keeps it out from under the thumb.
        self.assertEqual(
            [button["text"] for button in keyboard["inline_keyboard"][-1]],
            ["🚪", "⬇️ Спуститься"],
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

    def test_dungeon_requires_ten_rubies_to_enter(self):
        # Pinned literally, not just against dungeon.ENTRY_RUBY_COST: this number is a
        # balance decision (five 10-ruby entries a day should roughly match a day's PVE +
        # quarry income, see pets_mobs.TIER_RUBY_CHANCE), so an accidental edit to the
        # constant should fail a test, not just silently retune the economy.
        self.assertEqual(dungeon.ENTRY_RUBY_COST, 10)
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
        empty_haul = {"gold": 0, "xp": 0, "rubies": 0, "kills": 0,
                      "items": [], "scrolls": [], "runes": []}
        self.assertEqual(run, {
            "kills": 0, "floor": 1, "hp": 1, "max_hp": 1, "cleared": [], "boss_lives": 0,
            "partial_heals_used": 0, "full_heals_used": 0,
            # A run from before the tallies existed starts them empty rather than
            # inventing a history for it.
            "haul": empty_haul, "floor_haul": dict(empty_haul),
            # Same for the pack healers' bookkeeping.
            "dead_at": {}, "revived": [], "order": [],
        })

    def test_the_loot_tallies_survive_the_whitelist_that_rebuilds_the_run(self):
        """The exact failure mode DEPLOYMENT.md documents: a field the normaliser forgets
        is silently dropped on every load, so the summary would always read "nothing"."""
        data = pets._load(self.entry)
        data["pets"][self.user_id]["dungeon_run"] = {
            "run_id": "abc", "floor": 3, "hp": 10, "max_hp": 10, "cleared": [],
            "haul": {"gold": 900, "xp": 40, "rubies": 2, "kills": 5,
                     "items": ["Меч"], "scrolls": ["Пламя"], "runes": ["fire"]},
            "floor_haul": {"gold": 120, "xp": 8, "rubies": 0, "kills": 1,
                           "items": [], "scrolls": [], "runes": []},
        }
        pets._save(self.entry, data)

        run = pets.get_pet(self.entry, self.user_id)["dungeon_run"]
        self.assertEqual(run["haul"]["gold"], 900)
        self.assertEqual(run["haul"]["items"], ["Меч"])
        self.assertEqual(run["haul"]["scrolls"], ["Пламя"])
        self.assertEqual(run["floor_haul"]["gold"], 120)

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

    def _enter_pack_floor(self):
        """Stand a very strong hero on floor 2 -- the ten-enemy pack with two healers."""
        data = pets._load(self.entry)
        data["pets"][self.user_id]["stats"] = {
            "strength": 900, "health": 900, "agility": 900, "luck": 900, "endurance": 1,
        }
        pets._save(self.entry, data)
        pets.grant_dungeon_ticket(self.entry, self.user_id)
        self.assertTrue(pets.enter_dungeon(self.entry, self.user_id)[0])
        data = pets._load(self.entry)
        data["pets"][self.user_id]["dungeon_run"]["floor"] = 2
        data["pets"][self.user_id]["dungeon_run"]["cleared"] = []
        pets._save(self.entry, data)
        return [row["index"] for row in dungeon.encounters_for_floor(2)
                if row.get("healer")]

    def test_the_pack_healers_raise_the_fallen_until_they_are_dead_themselves(self):
        healers = self._enter_pack_floor()
        self.assertEqual(len(healers), 2)
        victim = next(i for i in range(10) if i not in healers)
        other = next(i for i in range(10) if i not in healers and i != victim)

        # Kill an ordinary member, then act again: the healers have had their turn.
        self.assertTrue(pets.dungeon_fight(self.entry, self.user_id, victim)[0])
        self.assertIn(victim, pets._load(self.entry)["pets"][self.user_id]["dungeon_run"]["cleared"])
        ok, message, receipt = pets.dungeon_fight(self.entry, self.user_id, other)
        self.assertTrue(ok, message)
        self.assertIn(victim, receipt["raised"], "the healers did not raise the fallen")
        run = pets._load(self.entry)["pets"][self.user_id]["dungeon_run"]
        self.assertNotIn(victim, run["cleared"])
        self.assertIn(victim, run["revived"])

        # Killing a raised enemy pays absolutely nothing -- otherwise the pack is an
        # infinite loop with a purse attached.
        purse = economy.balance(self.entry, self.user_id, 0)
        ok, _m, again = pets.dungeon_fight(self.entry, self.user_id, victim)
        self.assertTrue(ok)
        self.assertTrue(again["revived_kill"])
        self.assertEqual(again["reward"]["gold"], 0)
        self.assertEqual(again["reward"]["xp"], 0)
        self.assertIsNone(again["dropped"])
        self.assertIsNone(again["scroll"])
        self.assertEqual(again["rubies"], 0)
        self.assertEqual(economy.balance(self.entry, self.user_id, 0), purse)

        # Put both healers down and the cycle stops: the fallen stay fallen.
        for healer in healers:
            pets.dungeon_fight(self.entry, self.user_id, healer)
        state = pets.dungeon_status(self.entry, self.user_id)
        self.assertEqual(state["healers_alive"], 0)
        before = set(pets._load(self.entry)["pets"][self.user_id]["dungeon_run"]["cleared"])
        survivor = next(i for i in range(10) if i not in before)
        pets.dungeon_fight(self.entry, self.user_id, survivor)
        after = set(pets._load(self.entry)["pets"][self.user_id]["dungeon_run"]["cleared"])
        self.assertTrue(before <= after, "a dead healer must not raise anybody")

    def test_descending_forgets_who_was_raised_on_the_floor_above(self):
        """Shipped as a bug: `revived` is keyed by the enemy's index within a floor, and
        indices start again at zero on the next one. Carried over, every later enemy --
        the boss at index 0 included -- looked raised and paid out nothing."""
        healers = self._enter_pack_floor()
        victim = next(i for i in range(10) if i not in healers)
        other = next(i for i in range(10) if i not in healers and i != victim)
        pets.dungeon_fight(self.entry, self.user_id, victim)
        pets.dungeon_fight(self.entry, self.user_id, other)
        self.assertTrue(pets._load(self.entry)["pets"][self.user_id]["dungeon_run"]["revived"])

        # Clear the floor the way it is meant to be cleared -- healers first, or they
        # keep putting the room back up -- and go down.
        for index in (*healers, *range(10)):
            pets.dungeon_fight(self.entry, self.user_id, index)
        self.assertEqual(
            len(pets._load(self.entry)["pets"][self.user_id]["dungeon_run"]["cleared"]), 10,
        )
        self.assertTrue(pets.dungeon_descend(self.entry, self.user_id)[0])

        run = pets._load(self.entry)["pets"][self.user_id]["dungeon_run"]
        self.assertEqual(run["revived"], [])
        self.assertEqual(run["dead_at"], {})
        self.assertEqual(run["order"], [])

    def test_a_boss_is_never_treated_as_something_a_healer_raised(self):
        """Belt and braces over the reset above: a room with no healers in it cannot
        contain anything they put back up, whatever stale state says."""
        self._enter_pack_floor()
        data = pets._load(self.entry)
        run = data["pets"][self.user_id]["dungeon_run"]
        run["floor"], run["cleared"] = 5, []
        run["revived"] = list(range(10))          # exactly the leak that shipped
        data["pets"][self.user_id]["stats"] = {
            "strength": 900, "health": 900, "agility": 900, "luck": 900, "endurance": 1,
        }
        pets._save(self.entry, data)

        for _ in range(4):
            ok, _message, receipt = pets.dungeon_fight(self.entry, self.user_id, 0)
            self.assertTrue(ok)
            self.assertFalse(receipt.get("revived_kill"), "a boss must never pay nothing")
            if pets._load(self.entry)["pets"][self.user_id]["dungeon_run"]["cleared"]:
                break
        self.assertGreater(receipt["reward"]["gold"], 0)

    def test_a_healer_never_raises_another_healer(self):
        healers = self._enter_pack_floor()
        self.assertTrue(pets.dungeon_fight(self.entry, self.user_id, healers[0])[0])
        # One healer still stands. Act again -- it must not put its colleague back up.
        other = next(i for i in range(10) if i not in healers)
        pets.dungeon_fight(self.entry, self.user_id, other)
        run = pets._load(self.entry)["pets"][self.user_id]["dungeon_run"]
        self.assertIn(healers[0], run["cleared"])

    def test_a_revival_reshuffles_the_room_so_it_cannot_be_counted_off(self):
        healers = self._enter_pack_floor()
        victim = next(i for i in range(10) if i not in healers)
        other = next(i for i in range(10) if i not in healers and i != victim)
        pets.dungeon_fight(self.entry, self.user_id, victim)
        with patch("random.shuffle", lambda seq: seq.reverse()):
            pets.dungeon_fight(self.entry, self.user_id, other)

        order = pets._load(self.entry)["pets"][self.user_id]["dungeon_run"]["order"]
        self.assertEqual(order, list(reversed(range(10))))
        # And the screen follows that order rather than the index order.
        shown = [row["index"] for row in
                 pets.dungeon_status(self.entry, self.user_id)["encounters"]]
        self.assertEqual(shown, order)

    def test_the_run_tallies_what_each_floor_and_the_whole_descent_paid(self):
        data = pets._load(self.entry)
        data["pets"][self.user_id]["stats"] = {
            "strength": 400, "health": 400, "agility": 400, "luck": 400, "endurance": 1,
        }
        pets._save(self.entry, data)
        pets.grant_dungeon_ticket(self.entry, self.user_id)
        self.assertTrue(pets.enter_dungeon(self.entry, self.user_id)[0])

        for index in range(len(dungeon.encounters_for_floor(1))):
            self.assertTrue(pets.dungeon_fight(self.entry, self.user_id, index)[0])

        haul = pets.dungeon_haul(self.entry, self.user_id)
        self.assertEqual(haul["floor"]["kills"], len(dungeon.encounters_for_floor(1)))
        self.assertEqual(haul["total"]["kills"], haul["floor"]["kills"])
        self.assertGreater(haul["total"]["gold"], 0)
        first_floor_gold = haul["floor"]["gold"]

        # Descending resets the per-floor tally but never the running total.
        self.assertTrue(pets.dungeon_descend(self.entry, self.user_id)[0])
        after = pets.dungeon_haul(self.entry, self.user_id)
        self.assertEqual(after["floor"]["kills"], 0)
        self.assertEqual(after["floor"]["gold"], 0)
        self.assertEqual(after["total"]["gold"], first_floor_gold)

        # Walking out keeps the receipt, so the screen after the run can still show it.
        self.assertTrue(pets.quit_dungeon(self.entry, self.user_id)[0])
        state = pets.dungeon_status(self.entry, self.user_id)
        self.assertFalse(state["active"])
        self.assertEqual(state["last_haul"]["gold"], first_floor_gold)
        self.assertIn("Всего за поход", pets_ui.dungeon_finished_text(state["last_haul"]))

    def test_a_lost_run_still_reports_what_it_had_earned(self):
        """Dying is exactly when a player most wants to know what the descent was worth,
        and it is the moment the run record is thrown away."""
        # Strong enough to be let in, then crippled inside: entering is gated on power,
        # and the point of this test is what happens on the way out.
        data = pets._load(self.entry)
        data["pets"][self.user_id]["stats"] = {
            "strength": 400, "health": 400, "agility": 400, "luck": 400, "endurance": 1,
        }
        pets._save(self.entry, data)
        pets.grant_dungeon_ticket(self.entry, self.user_id)
        self.assertTrue(pets.enter_dungeon(self.entry, self.user_id)[0])

        data = pets._load(self.entry)
        run = data["pets"][self.user_id]["dungeon_run"]
        run["haul"] = {**pets._new_haul(), "gold": 640, "kills": 3, "scrolls": ["Пламя"]}
        run["hp"] = 1
        data["pets"][self.user_id]["stats"] = {
            "strength": 1, "health": 1, "agility": 1, "luck": 1, "endurance": 1,
        }
        pets._save(self.entry, data)

        ok, _message, receipt = pets.dungeon_fight(self.entry, self.user_id, 0)
        self.assertFalse(ok)
        self.assertTrue(receipt.get("run_over"))
        self.assertEqual(receipt["haul"]["total"]["gold"], 640)
        text = pets_ui.dungeon_finished_text(pets.dungeon_status(
            self.entry, self.user_id)["last_haul"])
        self.assertIn("оборвался", text)
        self.assertIn("Пламя", text)

    def test_the_update_gift_pays_ten_rubies_once_and_lands_in_the_ledger(self):
        self.assertEqual(pets.grant_ruby_gift([self.entry]), 1)
        self.assertEqual(pets.ruby_balance(self.entry, self.user_id), 10)
        # A handout that skipped the ledger would read as a balance grown from nowhere.
        rows = [row for row in pets._load(self.entry)["ruby_log"]
                if row["reason"] == pets.RUBY_GIFT_REASON]
        self.assertEqual([(row["user_id"], row["delta"]) for row in rows],
                         [(self.user_id, 10)])
        self.assertEqual(pets.ruby_source_of(pets.RUBY_GIFT_REASON), "grants")
        # Minted counter moves too, so the all-time coverage figure stays honest.
        self.assertEqual(pets.economy_telemetry(self.entry)["rubies_minted"], 10)

        # A restart must not pay twice.
        self.assertEqual(pets.grant_ruby_gift([self.entry]), 0)
        self.assertEqual(pets.ruby_balance(self.entry, self.user_id), 10)

    def test_a_level_costs_three_rubies_and_the_gift_buys_three_of_them(self):
        self.assertEqual(pets_config.PET_LEVEL_UP_RUBY_COST, 3)
        pets.grant_ruby_gift([self.entry])
        data = pets._load(self.entry)
        data["pets"][self.user_id]["xp"] = 10_000_000
        pets._save(self.entry, data)

        for expected_level in (2, 3, 4):
            ok, message = pets.claim_pet_level(self.entry, self.user_id)
            self.assertTrue(ok, message)
            self.assertEqual(pets._load(self.entry)["pets"][self.user_id]["level"],
                             expected_level)
        # 10 rubies buys three levels at 3 apiece, and the fourth is refused.
        self.assertEqual(pets.ruby_balance(self.entry, self.user_id), 1)
        self.assertFalse(pets.claim_pet_level(self.entry, self.user_id)[0])

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

    def test_a_kill_records_whether_it_was_a_mob_or_a_boss_for_the_income_audit(self):
        """The boss flag only exists on the encounter row; if it is not written into the
        reason here it is gone by the time the audit reads the ledger back."""
        data = pets._load(self.entry)
        data["pets"][self.user_id]["stats"] = {
            "strength": 400, "health": 400, "agility": 400, "luck": 400, "endurance": 1,
        }
        pets._save(self.entry, data)
        pets.grant_dungeon_ticket(self.entry, self.user_id)
        self.assertTrue(pets.enter_dungeon(self.entry, self.user_id)[0])
        self.assertTrue(pets.dungeon_fight(self.entry, self.user_id, 0)[0])

        reasons = [row["reason"] for row in economy._load(self.entry)["log"]]
        self.assertIn("pet_dungeon_mob_win", reasons)
        self.assertEqual(economy._audit_source("pet_dungeon_mob_win"), "dungeon_mobs")

        # Floor 5 is the first boss floor, and a boss win has to read differently.
        run = pets._load(self.entry)["pets"][self.user_id]["dungeon_run"]
        run["floor"], run["cleared"] = 5, []
        data = pets._load(self.entry)
        data["pets"][self.user_id]["dungeon_run"] = run
        pets._save(self.entry, data)
        for _ in range(4):                       # the floor-5 boss revives once
            if pets.dungeon_fight(self.entry, self.user_id, 0)[0] and \
                    "pet_dungeon_boss_win" in [
                        r["reason"] for r in economy._load(self.entry)["log"]]:
                break
        self.assertIn("pet_dungeon_boss_win",
                      [row["reason"] for row in economy._load(self.entry)["log"]])
        self.assertEqual(economy._audit_source("pet_dungeon_boss_win"), "dungeon_boss")

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

class DungeonEndTests(DungeonTests):
    """The descent has a bottom now.

    BOSSES is indexed modulo its own length, so floor 50 served the floor-5 boss again and
    the corridor ran for ever -- enemies the player had already beaten, dressed as
    progress.
    """

    def test_the_last_floor_is_the_last_boss_the_catalogue_can_fill(self):
        self.assertEqual(dungeon.LAST_FLOOR, len(dungeon.BOSSES) * 5)
        self.assertTrue(dungeon.is_boss_floor(dungeon.LAST_FLOOR))
        # And the floor after it would have wrapped back to the very first boss.
        wrapped = dungeon.encounter(dungeon.LAST_FLOOR + 5, 0)
        self.assertEqual(wrapped["name"], dungeon.encounter(5, 0)["name"])

    def _standing_on(self, floor):
        data = pets._load(self.entry)
        record = data["pets"][self.user_id]
        record["dungeon_run"] = {
            "floor": floor, "hp": 100, "max_hp": 100,
            "cleared": [row["index"] for row in dungeon.encounters_for_floor(floor)],
        }
        record["dungeon_deepest"] = floor
        pets._save(self.entry, data)

    def test_clearing_the_last_floor_ends_the_run_with_the_notice(self):
        self._standing_on(dungeon.LAST_FLOOR)

        ok, message = pets.dungeon_descend(self.entry, self.user_id)

        self.assertTrue(ok)
        self.assertEqual(message, dungeon.DUNGEON_CLEARED_NOTICE)
        self.assertIn("приходи позже", message)
        # The run ends rather than parking the player on a finished floor with a dead
        # button -- that would be the same endless-corridor lie in a smaller shape.
        self.assertIsNone(pets.get_pet(self.entry, self.user_id)["dungeon_run"])

    def test_the_floor_below_the_last_one_still_descends(self):
        self._standing_on(dungeon.LAST_FLOOR - 1)
        ok, message = pets.dungeon_descend(self.entry, self.user_id)
        self.assertTrue(ok)
        self.assertIn(str(dungeon.LAST_FLOOR), message)
        run = pets.get_pet(self.entry, self.user_id)["dungeon_run"]
        self.assertEqual(run["floor"], dungeon.LAST_FLOOR)

    def test_the_escalator_can_never_be_sent_past_the_last_floor(self):
        """`dungeon_deepest` is what the escalator flies to, so it is capped as well."""
        data = pets._load(self.entry)
        data["pets"][self.user_id]["dungeon_deepest"] = 200   # a value from the old rules
        pets._save(self.entry, data)

        state = pets.dungeon_status(self.entry, self.user_id)

        self.assertEqual(state["deepest"], dungeon.LAST_FLOOR)
        self.assertTrue(state["cleared_everything"])
        self.assertEqual(state["last_floor"], dungeon.LAST_FLOOR)

    def test_the_screen_calls_the_last_descent_a_finish_rather_than_a_way_down(self):
        self._standing_on(dungeon.LAST_FLOOR)
        text, keyboard = pets_ui.dungeon_view(self.entry, self.user_id, 0)
        labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]
        self.assertIn("🏁 Закончить", labels)
        self.assertNotIn("⬇️ Спуститься", labels)
        self.assertIn("приходи позже", text)

        self._standing_on(dungeon.LAST_FLOOR - 5)
        _text, keyboard = pets_ui.dungeon_view(self.entry, self.user_id, 0)
        labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]
        self.assertIn("⬇️ Спуститься", labels)


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
