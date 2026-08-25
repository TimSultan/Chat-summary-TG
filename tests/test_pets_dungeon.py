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
import pets_scroll_catalog as SCROLLS
import pets_ui


class DungeonTests(unittest.TestCase):
    def test_phoenix_totem_is_sold_for_fifteen_diamonds(self):
        item = dungeon.shop_item("phoenix_totem")
        self.assertEqual(item["price"], 15)
        self.assertEqual(item["currency"], "ruby")
        self.assertEqual(item["effect"], "resurrect")
        self.assertIn("навсегда", item["description"])

    def test_final_hp_carries_combat_healing_back_into_the_dungeon(self):
        result = SimpleNamespace(final_hp={self.user_id: 375}, rounds=())
        self.assertEqual(pets._hp_after_fight(result, self.user_id), 375)

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
        # Five, not ten: ten was ten near-identical fights in a row.
        self.assertEqual(len(pack), 5)
        self.assertTrue(all(row["gimmick"] == "pack_fury" for row in pack))
        self.assertEqual(sum(row["healer"] for row in pack), 2)
        self.assertIn("Пятеро", dungeon.floor_description(2))
        self.assertGreater(dungeon.pack_strength_multiplier(2, []), 1)
        self.assertEqual(dungeon.pack_strength_multiplier(2, range(4)), 1)
        boss = dungeon.encounters_for_floor(5)
        self.assertEqual(len(boss), 1)
        self.assertTrue(boss[0]["boss"])
        self.assertEqual(boss[0]["gimmick"], "reincarnate")

    def test_a_boss_without_a_gimmick_reads_its_own_floor_like_every_other_boss(self):
        """The plain bosses used to borrow the stat block of the boss five floors deeper.

        It was called a rehearsal -- beat the colossus on 20 and you have met exactly what
        the ghost on 25 brings. What it actually was is a cliff. Measured as the stat a
        player needs for a floor to cost a quarter of their health, floor 9 asked for 221
        and floor 10 asked for 413; floor 19 asked for 444 and floor 20 asked for 668. A
        run walked two cheap floors and then met the number belonging to a place it had
        not reached. Every boss reads its own floor now, and the same two steps are +24%
        and +26%.
        """
        for plain, gimmick_floor in ((10, 15), (20, 25)):
            with self.subTest(boss=plain):
                plain_boss = dungeon.encounter(plain, 0)
                deeper = dungeon.encounter(gimmick_floor, 0)
                self.assertEqual(plain_boss["gimmick"], "standard")
                self.assertLess(plain_boss["stats"]["strength"],
                                deeper["stats"]["strength"])
                self.assertLess(plain_boss["level"], deeper["level"])
                # And its own floor is what it reads: the plain boss on 10 is built from
                # floor 10, not from floor 15.
                self.assertEqual(plain_boss["level"], plain + 8)
        self.assertEqual(dungeon.encounter(15, 0)["gimmick"], "fire_only")
        self.assertEqual(dungeon.encounter(25, 0)["gimmick"], "spells_only")

    def test_a_boss_is_the_toughest_thing_standing_on_its_block(self):
        """A boss outlasts the corridor rather than out-hitting it.

        That is the whole shape of the change: BOSS_HEALTH_MULTIPLIER carries the
        difficulty and BOSS_POWER_MULTIPLIER barely rises, so a boss is a fight a player
        watches their resources drain in instead of one that ends in a single exchange.
        Health is therefore the yardstick here, not Сила -- an elite guard is allowed to
        swing harder than the boss, and measured, its floor is still the easier one.
        """
        for floor in range(10, 46, 5):
            with self.subTest(boss=floor):
                boss = dungeon.encounter(floor, 0)
                corridor = [row for f in range(max(1, floor - 4), floor)
                            if not dungeon.is_boss_floor(f)
                            for row in dungeon.encounters_for_floor(f)]
                self.assertGreater(
                    boss["stats"]["health"],
                    max(row["stats"]["health"] for row in corridor) * 1.2,
                    f"the floor {floor} boss dies as fast as the corridor in front of it",
                )

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

    def test_healing_is_sold_in_one_place_and_the_floor_only_points_at_it(self):
        """The two heals were on the floor screen AND in the shop, in two vocabularies.

        A cleared floor used to offer «🩹 +30% HP (2) · 💎 1» as a button and then list the
        same purchase again underneath as stock. Two controls for one thing is two places
        for the ration and the price to drift apart, and the player reading the screen has
        to work out that they are the same purchase. The shop keeps them; the floor sends
        the player there.
        """
        data = pets._load(self.entry)
        data["pets"][self.user_id]["dungeon_run"] = {
            "floor": 1, "hp": 10, "max_hp": 10, "cleared": [0, 1],
        }
        pets._save(self.entry, data)

        text, keyboard = pets_ui.dungeon_view(self.entry, self.user_id, 0)
        labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]

        self.assertIn("лавке", text)
        self.assertFalse([label for label in labels if "HP" in label], labels)
        self.assertTrue(any("Лавка" in label for label in labels), labels)
        rows = [[button["text"] for button in row]
                for row in keyboard["inline_keyboard"]]
        descend_row = next(i for i, row in enumerate(rows) if "⬇️ Спуститься" in row)
        shop_row = next(i for i, row in enumerate(rows) if any("Лавка" in x for x in row))
        self.assertEqual(rows[descend_row], ["⬇️ Спуститься"])
        self.assertLess(descend_row, shop_row)

        # Diamonds, and the shelf has to say so: a price in the wrong currency is the one
        # label a player cannot recover from misreading.
        shop_text, shop_keys = pets_ui.dungeon_shop_view(self.entry, self.user_id, 0)
        shop_labels = [button["text"] for row in shop_keys["inline_keyboard"]
                       for button in row]
        self.assertIn("💎", shop_text)
        self.assertNotIn("🪙", shop_text)
        self.assertTrue(any("HP" in label or "💎" in label for label in shop_labels),
                        shop_labels)

    def test_equipment_can_be_changed_at_any_point_of_a_run(self):
        """Gear used to be frozen until a floor was cleared. Every boss now states the
        damage it is weak to, so swapping a weapon to answer that is the play the hint
        invites -- freezing it only stopped players reacting to what they were told."""
        data = pets._load(self.entry)
        data["pets"][self.user_id]["inventory"].append("w001")
        data["pets"][self.user_id]["dungeon_run"] = {
            "floor": 1, "hp": 10, "max_hp": 10, "cleared": [],
        }
        pets._save(self.entry, data)

        # Mid-floor, with enemies still standing.
        ok, message = pets.equip(self.entry, self.user_id, "w001")
        self.assertTrue(ok, message)
        self.assertTrue(pets.unequip(self.entry, self.user_id, "weapon")[0])

        # And the run itself is untouched by it: no healing, no reset, no free floor.
        run = pets._load(self.entry)["pets"][self.user_id]["dungeon_run"]
        self.assertEqual(run["hp"], 10)
        self.assertEqual(run["floor"], 1)
        self.assertEqual(run["cleared"], [])

    def test_both_clients_let_a_runner_reach_their_gear(self):
        """pets.equip stopped refusing mid-run, and both transports went on refusing for
        it. The «🎒 Снаряжение» button on the floor screen bounced off the same gate that
        blocks the farm, so the rule changed in one file and nowhere a player could see.

        Pinned as a pair: the two clients must permit the same things, or re-arming works
        in the chat and not in the app.
        """
        import bot_listener
        import pets_web

        for action in ("bag", "bagitems", "equip", "unequip", "forge", "enchant"):
            with self.subTest(telegram=action):
                self.assertIn(action, bot_listener.PET_ACTIONS_ALLOWED_IN_A_RUN)
        for action in ("equip", "unequip", "enchant_weapon"):
            with self.subTest(webapp=action):
                self.assertIn(action, pets_web._ALLOWED_IN_DUNGEON)

        # The trip OUT of the dungeon is still shut. A run is a commitment, and the gate
        # exists to stop somebody wandering off to another mode in the middle of one.
        for action in ("farm", "search", "casino", "quests", "store"):
            with self.subTest(shut=action):
                self.assertNotIn(action, bot_listener.PET_ACTIONS_ALLOWED_IN_A_RUN)
        for action in ("farm_start", "buy", "sell", "quarry_start"):
            with self.subTest(shut=action):
                self.assertNotIn(action, pets_web._ALLOWED_IN_DUNGEON)

    def test_the_corridor_climbs_in_a_straight_line_and_nothing_shallow_moves(self):
        """The deep corridor had to start multiplying, and only the deep corridor.

        A flat +7 a floor was priced against a pet whose whole power was its stat block.
        Measured against a player carrying a legendary weapon, a rune and four scrolls, a
        floor-24 corridor enemy took a couple of percent of their health and did nothing
        whatsoever in a large share of fights. Three things are pinned here, because
        breaking any one of them puts the old complaint back:

        * floors up to the ramp are untouched, so a new runner's first bosses are the
          fight they always were;
        * a deep corridor enemy is meaningfully bigger than the old straight line;
        * bosses are NOT on the ramp -- they were already where every run ended, and the
          corridor is being lifted toward them rather than the wall being moved again.
        """
        for floor in range(1, dungeon.DEPTH_RAMP_START + 1):
            with self.subTest(unchanged=floor):
                self.assertEqual(dungeon._scale(floor), 22 + (floor - 1) * 7)
        # Bosses ride the ramp too, and must never fall behind the corridor they cap --
        # leaving them on the straight line made the floor's owner the easiest thing on
        # it (0.86x the elite by floor 25, 0.46x by 45, 0.20x past the roster). The
        # yardstick is how long a boss LIVES rather than how hard it swings: see
        # BOSS_HEALTH_MULTIPLIER, and test_a_boss_is_the_toughest_thing_standing_on_its_block.
        for floor in range(5, 81, 5):
            with self.subTest(boss=floor):
                boss = dungeon.encounter(floor, 0)
                corridor = max(
                    (row for f in range(max(1, floor - 4), floor)
                     if not dungeon.is_boss_floor(f)
                     for row in dungeon.encounters_for_floor(f)),
                    key=lambda row: row["stats"]["health"],
                )
                self.assertGreater(
                    boss["stats"]["health"], corridor["stats"]["health"],
                    f"the floor {floor} boss dies faster than the corridor in front of it",
                )
        # Past the ramp the corridor is a STRAIGHT LINE, and the step between floors is
        # the same one for ever. It used to compound, which is a different promise: every
        # floor multiplied the last, so the stat level needed to win half the time ran 245
        # at floor 10, 542 at 20 and 1790 at 45 -- and since a stat point costs
        # `level ** 1.5`, the gold behind those ran 1.9M, 13.6M and 271M. The corridor was
        # not getting harder, it was leaving.
        steps = {
            dungeon._scale(floor + 1) - dungeon._scale(floor)
            for floor in range(dungeon.DEPTH_RAMP_START, 90)
        }
        self.assertEqual(steps, {dungeon.DEEP_CORRIDOR_STAT_SLOPE})
        # Still meaningfully above the old flat step, which is what the ramp was for.
        self.assertGreater(dungeon.DEEP_CORRIDOR_STAT_SLOPE, dungeon.CORRIDOR_STAT_SLOPE)
        # Thin armour is a short fight, and a short fight is one the enemy spends dying
        # rather than hitting back. Deep enemies carry the boss's share of it.
        shallow = dungeon.encounter(dungeon.DEPTH_RAMP_START - 1, 0)
        deep = dungeon.encounter(dungeon.DEPTH_RAMP_START, 0)
        self.assertGreater(deep["armor"], shallow["armor"])

    def test_armour_never_climbs_towards_the_engine_ceiling(self):
        """Armour is the one enemy stat that multiplies against everything a player brings.

        Reduction saturates towards ARMOR_MAX, so an enemy that keeps accruing armour does
        not get tougher -- it gets out of reach. Left uncapped it reached 55% absorbed by
        floor 60 with thirty thousand health behind it, which is a wall rather than a
        fight. Health and damage can climb instead: a player answers those with more of
        their own.
        """
        worst = 0.0
        for floor in range(1, 120):
            for row in dungeon.encounters_for_floor(floor):
                # A boss owns its floor and is allowed the thicker plate; everything
                # walking the corridor in front of it is not.
                cap = (dungeon.BOSS_ARMOR_CAP if row.get("boss")
                       else dungeon.CORRIDOR_ARMOR_CAP)
                self.assertLessEqual(row["armor"], cap, floor)
                worst = max(worst, pets_config.ARMOR_MAX * row["armor"]
                            / (row["armor"] + pets_config.ARMOR_K))
            self.assertLessEqual(
                dungeon.mimic(floor)["armor"], dungeon.BOSS_ARMOR_CAP, floor,
            )
        # Comfortably under the engine's own 60% ceiling, for ever.
        self.assertLess(worst, .42)

    def test_every_enemy_shows_its_stat_block_to_both_clients(self):
        """The numbers a player is deciding against, on the floor screen itself.

        Worded once in pets_dungeon so the Mini App and Telegram can never print the same
        enemy differently -- both are handed the finished string rather than five fields
        and their own idea of how to lay them out.
        """
        import pets_ui

        row = dungeon.encounter(24, 1)
        line = dungeon.enemy_stat_line(row)
        for key in dungeon.STAT_LINE_KEYS:
            with self.subTest(stat=key):
                self.assertIn(str(row["stats"][key]), line)
                self.assertIn(pets_config.STAT_EMOJI[key], line)
        self.assertIn(f"{pets_config.ARMOR_EMOJI} {row['armor']}", line)
        # A mob has no endurance, so the block must not print an empty fifth column.
        self.assertNotIn(pets_config.STAT_EMOJI["endurance"], line)

        data = pets._load(self.entry)
        data["pets"][self.user_id]["dungeon_run"] = {
            "run_id": "r", "kills": 0, "floor": 24, "hp": 500, "max_hp": 500,
            "cleared": [0],
        }
        pets._save(self.entry, data)
        state = pets.dungeon_status(self.entry, self.user_id)
        by_index = {row["index"]: row for row in state["encounters"]}
        self.assertEqual(
            by_index[1]["stat_line"], dungeon.enemy_stat_line(dungeon.encounter(24, 1)),
        )
        # Nothing to decide about somebody already lying down.
        text, _keyboard = pets_ui.dungeon_view(self.entry, self.user_id, 0)
        self.assertIn(by_index[1]["stat_line"], text)
        self.assertNotIn(by_index[0]["stat_line"], text)

    def test_a_revealed_mimic_shows_the_same_block_as_the_corridor(self):
        """Fighting a mimic is a decision, and it is a decision about these numbers."""
        import pets_ui

        data = pets._load(self.entry)
        data["pets"][self.user_id]["dungeon_run"] = {
            "run_id": "r", "kills": 0, "floor": 24, "hp": 500, "max_hp": 500,
            "cleared": [], "chest": {"kind": "mimic", "floor": 24, "revealed": True},
        }
        pets._save(self.entry, data)

        chest = pets.dungeon_status(self.entry, self.user_id)["chest"]
        self.assertEqual(chest["stat_line"], dungeon.enemy_stat_line(dungeon.mimic(24)))
        lines, _rows = pets_ui.dungeon_chest_block(chest, self.user_id)
        self.assertTrue(any(chest["stat_line"] in line for line in lines))
        # A closed box gives nothing away -- naming its stats would answer the only
        # question the encounter asks.
        self.assertNotIn("stat_line", pets._chest_payload(
            {"kind": "mimic", "floor": 24, "revealed": False},
        ) or {})

    def test_both_clients_let_a_runner_change_scrolls(self):
        """Same rule as the gear pair above, for the other half of a reaction.

        A boss prints the damage it is weak to; the fire scroll that answers it lives on
        the scroll screen, not in the bag. pets.set_skill_slot never refused mid-run, so
        both transports had to be the ones opening -- and the floor screen has to offer
        the button, or the permission is one nobody can reach.
        """
        import bot_listener
        import pets_ui
        import pets_web

        for action in ("skills", "skillpick", "skillclear", "setskill"):
            with self.subTest(telegram=action):
                self.assertIn(action, bot_listener.PET_ACTIONS_ALLOWED_IN_A_RUN)
        self.assertIn("set_skill", pets_web._ALLOWED_IN_DUNGEON)

        data = pets._load(self.entry)
        record = data["pets"][self.user_id]
        record["dungeon_run"] = {"floor": 1, "hp": 10, "max_hp": 10, "cleared": []}
        pets._save(self.entry, data)
        _text, markup = pets_ui.dungeon_view(self.entry, self.user_id, 0)
        buttons = [
            button["callback_data"]
            for row in markup["inline_keyboard"] for button in row
        ]
        self.assertTrue(
            any(data.endswith(":skills") or ":skills:" in data for data in buttons),
            f"the floor screen offers no way to the scrolls: {buttons}",
        )

    def test_a_scroll_can_be_slotted_without_leaving_the_dungeon(self):
        data = pets._load(self.entry)
        record = data["pets"][self.user_id]
        code = SCROLLS.REGULAR_SCROLLS[0]["code"]
        record["owned_scrolls"] = [code]
        record["dungeon_run"] = {"floor": 1, "hp": 10, "max_hp": 10, "cleared": []}
        pets._save(self.entry, data)

        ok, message = pets.set_skill_slot(self.entry, self.user_id, 1, code)

        self.assertTrue(ok, message)
        self.assertEqual(
            pets._load(self.entry)["pets"][self.user_id]["skill_slots"][0], code,
        )

    def test_a_weapon_can_be_enchanted_without_leaving_the_dungeon(self):
        data = pets._load(self.entry)
        record = data["pets"][self.user_id]
        record["inventory"].append("w001")
        record["runes"] = {"fire": 1}
        record["dungeon_run"] = {"floor": 1, "hp": 10, "max_hp": 10, "cleared": []}
        data["rubies"] = {self.user_id: pets.RUNE_ENCHANT_RUBY_COST}
        pets._save(self.entry, data)

        ok, message = pets.enchant_weapon(self.entry, self.user_id, "w001", "fire")

        self.assertTrue(ok, message)
        self.assertEqual(
            pets._load(self.entry)["pets"][self.user_id]["weapon_enchantments"]["w001"],
            "fire",
        )

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

    def test_a_refused_entry_never_consumes_the_ticket(self):
        """The escalator is gone, but the guarantee it was tested for is not: a ticket
        pays admission and must survive an entry that gets refused."""
        data = pets._load(self.entry)
        data["pets"][self.user_id]["stats"] = {
            "strength": 1, "health": 1, "agility": 1, "luck": 1, "endurance": 1,
        }
        pets._save(self.entry, data)
        pets.grant_dungeon_ticket(self.entry, self.user_id)

        ok, _message = pets.enter_dungeon(self.entry, self.user_id)

        self.assertFalse(ok, "a creature under the power floor must not get in")
        self.assertEqual(pets.dungeon_tickets(self.entry, self.user_id), 1)

    def test_the_dungeon_screen_no_longer_offers_an_escalator(self):
        data = pets._load(self.entry)
        data["pets"][self.user_id]["dungeon_deepest"] = 7
        pets._save(self.entry, data)
        _text, keyboard = pets_ui.dungeon_view(self.entry, self.user_id, 0)
        labels = [b["text"] for row in keyboard["inline_keyboard"] for b in row]
        self.assertFalse([t for t in labels if "Эскалатор" in t])


    def test_the_opening_floors_still_pay_more_than_nothing(self):
        """This used to check that two floors of gold covered a full rest.

        Healing is bought with diamonds now, so a floor's coins no longer have a rest to
        be measured against -- but the reason the check existed is still live: the opening
        rooms have to be worth walking into at all, and reward_for is where that is
        decided for every floor at once.
        """
        floor_one = dungeon.reward_for(1, False, enemy_count=2)["gold"]
        floor_two = dungeon.reward_for(2, False, enemy_count=10)["gold"]
        low_roll_gold = 2 * round(floor_one * .8) + 10 * round(floor_two * .8)

        self.assertGreater(low_roll_gold, 0)
        self.assertGreater(floor_two, 0)

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
            "shop_used_phoenix_totem": 0,
            "phoenix_totem": False, "phoenix_totem_used": False,
            # A run from before the tallies existed starts them empty rather than
            # inventing a history for it.
            "haul": empty_haul, "floor_haul": dict(empty_haul),
            # Same for the pack healers' bookkeeping.
            "dead_at": {}, "revived": [], "order": [],
            # No find between these floors -- and the field is present rather than absent,
            # because the whitelist is what makes a chest survive a reload at all.
            "chest": None,
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
        """A runner that always wins, parked on `floor` with a fresh run.

        Unstoppable by STATS, which is only half of a fighter -- see `_rekill`, which
        keeps the other half still.
        """
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
        """Kill the same encounter `times` over, resetting only what a re-entry resets.

        The equipment is held still along with the health, and that is not tidiness: every
        kill rolls a real drop, and a drop auto-equips itself when it scores better than
        an empty slot -- which the first one always does. Over a long measurement the
        runner therefore re-dresses itself mid-sample, and one item in the catalogue ends
        the measurement outright. «Зеркало души» sets its wearer to the OPPONENT's stats,
        so the moment it lands the runner stops being a runner with 4,000 in everything
        and starts being an even match for the mob it is farming -- which it then loses
        22 times out of 30, and `assertTrue(ok)` below fails on a fight that was supposed
        to be a formality. Rare enough to pass alone and fail in a full suite.
        """
        payloads = []
        equipped = dict(pets._load(self.entry)["pets"][self.user_id].get("equipped") or {})
        for _ in range(times):
            data = pets._load(self.entry)
            data["pets"][self.user_id]["dungeon_run"]["cleared"] = []
            data["pets"][self.user_id]["dungeon_run"]["hp"] = 99_999
            data["pets"][self.user_id]["equipped"] = dict(equipped)
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

    def test_existing_dungeon_ticket_balances_are_capped_at_ten_once(self):
        for _ in range(14):
            pets.grant_dungeon_ticket(self.entry, self.user_id)
        other = "99"
        for _ in range(8):
            pets.grant_dungeon_ticket(self.entry, other)

        self.assertEqual(
            pets.cap_existing_dungeon_tickets([self.entry]),
            {"players": 1, "tickets": 4},
        )
        self.assertEqual(pets.dungeon_tickets(self.entry, self.user_id), 10)
        self.assertEqual(pets.dungeon_tickets(self.entry, other), 8)

        # It is a one-off correction, not a permanent cap on newly earned rewards.
        pets.grant_dungeon_ticket(self.entry, self.user_id)
        self.assertEqual(pets.cap_existing_dungeon_tickets([self.entry]),
                         {"players": 0, "tickets": 0})
        self.assertEqual(pets.dungeon_tickets(self.entry, self.user_id), 11)

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
        victim = next(i for i in range(5) if i not in healers)
        other = next(i for i in range(5) if i not in healers and i != victim)

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
        survivor = next(i for i in range(5) if i not in before)
        pets.dungeon_fight(self.entry, self.user_id, survivor)
        after = set(pets._load(self.entry)["pets"][self.user_id]["dungeon_run"]["cleared"])
        self.assertTrue(before <= after, "a dead healer must not raise anybody")

    def test_descending_forgets_who_was_raised_on_the_floor_above(self):
        """Shipped as a bug: `revived` is keyed by the enemy's index within a floor, and
        indices start again at zero on the next one. Carried over, every later enemy --
        the boss at index 0 included -- looked raised and paid out nothing."""
        healers = self._enter_pack_floor()
        victim = next(i for i in range(5) if i not in healers)
        other = next(i for i in range(5) if i not in healers and i != victim)
        pets.dungeon_fight(self.entry, self.user_id, victim)
        pets.dungeon_fight(self.entry, self.user_id, other)
        self.assertTrue(pets._load(self.entry)["pets"][self.user_id]["dungeon_run"]["revived"])

        # Clear the floor the way it is meant to be cleared -- healers first, or they
        # keep putting the room back up -- and go down.
        for index in (*healers, *range(5)):
            pets.dungeon_fight(self.entry, self.user_id, index)
        self.assertEqual(
            len(pets._load(self.entry)["pets"][self.user_id]["dungeon_run"]["cleared"]), 5,
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
        # Floor 10 rather than 5: the floor-5 Phoenix is fought by hand (pets_phoenix),
        # and dungeon_fight refuses it outright. What this test is about -- a boss never
        # being mistaken for something a healer raised -- is true of any boss floor.
        run["floor"], run["cleared"] = 10, []
        run["revived"] = list(range(5))           # exactly the leak that shipped
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
        other = next(i for i in range(5) if i not in healers)
        pets.dungeon_fight(self.entry, self.user_id, other)
        run = pets._load(self.entry)["pets"][self.user_id]["dungeon_run"]
        self.assertIn(healers[0], run["cleared"])

    def test_a_revival_reshuffles_the_room_so_it_cannot_be_counted_off(self):
        healers = self._enter_pack_floor()
        victim = next(i for i in range(5) if i not in healers)
        other = next(i for i in range(5) if i not in healers and i != victim)
        pets.dungeon_fight(self.entry, self.user_id, victim)
        with patch("random.shuffle", lambda seq: seq.reverse()):
            pets.dungeon_fight(self.entry, self.user_id, other)

        order = pets._load(self.entry)["pets"][self.user_id]["dungeon_run"]["order"]
        self.assertEqual(order, list(reversed(range(5))))
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
        rows = [row for row in pets.ruby_log_rows(self.entry)
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
        # Comfortably above the floor-10 boss below, so this assertion is about the
        # REASON STRING and never about whether a simulated fight happened to be won.
        data["pets"][self.user_id]["stats"] = {
            "strength": 900, "health": 900, "agility": 900, "luck": 900, "endurance": 1,
        }
        pets._save(self.entry, data)
        pets.grant_dungeon_ticket(self.entry, self.user_id)
        self.assertTrue(pets.enter_dungeon(self.entry, self.user_id)[0])
        self.assertTrue(pets.dungeon_fight(self.entry, self.user_id, 0)[0])

        reasons = [row["reason"] for row in economy._load(self.entry)["log"]]
        self.assertIn("pet_dungeon_mob_win", reasons)
        self.assertEqual(economy._audit_source("pet_dungeon_mob_win"), "dungeon_mobs")

        # Floor 10 is the first boss the auto-battler still resolves -- floor 5 is the
        # Phoenix, which is fought a turn at a time and pays out through its own path --
        # and a boss win has to read differently in the ledger either way.
        run = pets._load(self.entry)["pets"][self.user_id]["dungeon_run"]
        run["floor"], run["cleared"] = 10, []
        data = pets._load(self.entry)
        data["pets"][self.user_id]["dungeon_run"] = run
        pets._save(self.entry, data)
        for _ in range(4):
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

    def test_the_descent_no_longer_stops_at_the_last_built_floor(self):
        """It is endless again. What keeps that from being a money printer is the reward
        cap, not a wall -- see the payout test below."""
        self._standing_on(dungeon.LAST_FLOOR)

        ok, message = pets.dungeon_descend(self.entry, self.user_id)

        self.assertTrue(ok, message)
        run = pets.get_pet(self.entry, self.user_id)["dungeon_run"]
        self.assertIsNotNone(run, "the run must continue past the built bosses")
        self.assertEqual(run["floor"], dungeon.LAST_FLOOR + 1)

    def test_past_the_built_floors_enemies_grow_and_the_purse_does_not(self):
        cap = dungeon.REWARD_CAP_FLOOR
        at_cap = dungeon.reward_for(cap, boss=False, enemy_count=1)
        deeper = dungeon.reward_for(cap + 40, boss=False, enemy_count=1)
        self.assertEqual(deeper["gold"], at_cap["gold"])
        self.assertEqual(deeper["xp"], at_cap["xp"])
        self.assertEqual(deeper["scroll_chance"], at_cap["scroll_chance"])
        self.assertEqual(deeper["item_chance"], at_cap["item_chance"])
        self.assertEqual(
            dungeon.reward_for(cap + 40, boss=True)["gold"],
            dungeon.reward_for(cap, boss=True)["gold"],
        )
        # The enemies most certainly do keep growing -- that is the trade.
        self.assertGreater(dungeon._scale(cap + 40), dungeon._scale(cap))

    def test_the_floor_below_the_last_one_still_descends(self):
        self._standing_on(dungeon.LAST_FLOOR - 1)
        ok, message = pets.dungeon_descend(self.entry, self.user_id)
        self.assertTrue(ok)
        self.assertIn(str(dungeon.LAST_FLOOR), message)
        run = pets.get_pet(self.entry, self.user_id)["dungeon_run"]
        self.assertEqual(run["floor"], dungeon.LAST_FLOOR)

    def test_a_record_past_the_built_floors_is_kept_rather_than_clamped(self):
        data = pets._load(self.entry)
        data["pets"][self.user_id]["dungeon_deepest"] = 200
        pets._save(self.entry, data)

        state = pets.dungeon_status(self.entry, self.user_id)

        self.assertEqual(state["deepest"], 200)
        self.assertTrue(state["cleared_everything"])
        self.assertEqual(state["reward_cap_floor"], dungeon.REWARD_CAP_FLOOR)

    def test_the_screen_warns_that_the_deep_floors_stop_paying_more(self):
        self._standing_on(dungeon.REWARD_CAP_FLOOR)
        text, keyboard = pets_ui.dungeon_view(self.entry, self.user_id, 0)
        labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]
        # Still a way down, never a finish line.
        self.assertIn("⬇️ Спуститься", labels)
        self.assertNotIn("🏁 Закончить", labels)
        self.assertIn("награда больше не растёт", text)

        self._standing_on(dungeon.LAST_FLOOR - 5)
        _text, keyboard = pets_ui.dungeon_view(self.entry, self.user_id, 0)
        labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]
        self.assertIn("⬇️ Спуститься", labels)


class DungeonRestTests(DungeonTests):
    """Rests are rationed per run, and the ration is printed on the button that spends it."""

    def _cleared_floor(self, gold=100_000, rubies=50):
        economy.grant(self.entry, self.user_id, gold, "rest-test")
        # Healing is bought with diamonds. The gold stays because the rest of a run still
        # runs on it -- it just no longer buys the recovery.
        pets.grant_rubies_once(self.entry, self.user_id, rubies, "rest-test-rubies")
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
            self.assertIn(f"Осталось таких: {remaining}", message)

        ok, message = pets.dungeon_rest(self.entry, self.user_id, 0, "partial")
        self.assertFalse(ok)
        self.assertIn("(3 из 3)", message)
        self.assertIn("новом забеге", message)
        # The two allowances are separate: burning every partial leaves the full rests.
        self.assertTrue(pets.dungeon_rest(self.entry, self.user_id, 0, "full")[0])

    def _wound(self, hp=10):
        """Put the runner back in need of the next heal.

        Needed since the shop refuses to sell healing to somebody already at full health
        -- a kindness for the coin rows and the whole point for the diamond ones, but it
        means a ration can only be spent by actually taking damage between purchases.
        """
        data = pets._load(self.entry)
        data["pets"][self.user_id]["dungeon_run"]["hp"] = hp
        pets._save(self.entry, data)

    def test_a_refused_rest_does_not_take_the_diamonds(self):
        """The limit is checked before anything is taken, or the ration would be paid for."""
        self._cleared_floor()
        for _ in range(dungeon.SHOP_FULL_HEAL_USES):
            self._wound()
            self.assertTrue(pets.dungeon_rest(self.entry, self.user_id, 0, "full")[0])
        self._wound()
        before = pets.ruby_balance(self.entry, self.user_id)

        self.assertFalse(pets.dungeon_rest(self.entry, self.user_id, 0, "full")[0])

        self.assertEqual(pets.ruby_balance(self.entry, self.user_id), before)

    def test_healing_is_paid_for_in_diamonds_and_leaves_the_coins_alone(self):
        self._cleared_floor()
        self._wound()
        coins = economy.balance(self.entry, self.user_id, 0)
        rubies = pets.ruby_balance(self.entry, self.user_id)

        ok, message = pets.dungeon_rest(self.entry, self.user_id, 0, "full")

        self.assertTrue(ok, message)
        self.assertEqual(economy.balance(self.entry, self.user_id, 0), coins)
        self.assertEqual(
            pets.ruby_balance(self.entry, self.user_id),
            rubies - dungeon.SHOP_FULL_HEAL_RUBIES,
        )
        self.assertIn("💎", message)

    def test_the_shop_refuses_to_sell_healing_to_a_healthy_runner(self):
        """A diamond spent at full health would buy nothing at all."""
        self._cleared_floor()
        self._wound()
        self.assertTrue(pets.dungeon_rest(self.entry, self.user_id, 0, "full")[0])
        before = pets.ruby_balance(self.entry, self.user_id)
        ok, message = pets.dungeon_rest(self.entry, self.user_id, 0, "full")
        self.assertFalse(ok)
        self.assertIn("полное", message)
        self.assertEqual(pets.ruby_balance(self.entry, self.user_id), before)

    def test_a_heal_nobody_can_afford_is_refused_without_taking_anything(self):
        self._cleared_floor(rubies=0)
        self._wound()
        self.assertEqual(pets.ruby_balance(self.entry, self.user_id), 0)
        ok, message = pets.dungeon_buy(self.entry, self.user_id, 0, "heal_full")
        self.assertFalse(ok)
        self.assertIn("💎", message)
        self.assertEqual(pets.ruby_balance(self.entry, self.user_id), 0)
        # And the ration is untouched, so a refusal costs nothing at all.
        state = pets.dungeon_status(self.entry, self.user_id)
        self.assertEqual(state["full_heals_left"], dungeon.SHOP_FULL_HEAL_USES)

    def test_the_shelf_reaches_the_floor_payload_priced_for_this_runner(self):
        """The shop is data, so both clients draw the same shelf from the same row.

        It holds the two heals today and is written to hold whatever the dungeon turns
        out to need -- adding stock is adding a row, not teaching a client a button.
        """
        self._cleared_floor(rubies=dungeon.SHOP_PARTIAL_HEAL_RUBIES)
        rows = {row["code"]: row for row in pets.dungeon_status(self.entry, self.user_id)["shop"]}
        self.assertEqual(set(rows), set(dungeon.SHOP_CODES))
        self.assertTrue(all(row["currency"] == "ruby" for row in rows.values()))
        # One diamond reaches the bandages and not the field hospital.
        self.assertTrue(rows["heal_partial"]["affordable"])
        self.assertFalse(rows["heal_full"]["affordable"])
        self.assertEqual(rows["heal_full"]["left"], dungeon.SHOP_FULL_HEAL_USES)
        self.assertFalse(rows["heal_full"]["sold_out"])

    def test_the_remaining_count_reaches_the_shelf(self):
        """What is left of a ration is the whole reason to hold one back.

        It is on the shelf rather than on the floor screen, because the shelf is the one
        place a purchase is described -- and the number a player is planning a descent
        around must not have a second copy of itself somewhere else on the way there.
        """
        self._cleared_floor(rubies=99)
        pets.dungeon_rest(self.entry, self.user_id, 0, "partial")

        state = pets.dungeon_status(self.entry, self.user_id)
        self.assertEqual(state["partial_heals_left"], dungeon.SHOP_PARTIAL_HEAL_USES - 1)
        self.assertEqual(state["full_heals_left"], dungeon.SHOP_FULL_HEAL_USES)
        self.assertEqual(state["partial_heal_percent"], 30)

        text, _keyboard = pets_ui.dungeon_shop_view(self.entry, self.user_id, 0)
        self.assertIn(f"осталось {dungeon.SHOP_PARTIAL_HEAL_USES - 1}", text)
        self.assertIn(f"осталось {dungeon.SHOP_FULL_HEAL_USES}", text)

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


class ChestAndMimicModelTests(unittest.TestCase):
    """The pure half of the between-floors find: what it is and what it is worth.

    Nothing calls this yet -- the run state and the buttons are the next step -- so these
    pin the numbers before any of it can quietly drift.
    """

    def test_a_find_is_rare_and_about_half_of_them_bite(self):
        rng = random.Random(3)
        found = [dungeon.roll_chest(6, rng) for _ in range(4000)]
        hits = [row for row in found if row]
        self.assertAlmostEqual(len(hits) / 4000, dungeon.CHEST_CHANCE, delta=0.02)
        mimics = sum(row["kind"] == "mimic" for row in hits)
        self.assertAlmostEqual(mimics / len(hits), dungeon.MIMIC_SHARE, delta=0.06)
        self.assertEqual({row["floor"] for row in hits}, {6})

    def test_nine_descents_in_ten_find_nothing_at_all(self):
        """None, not an empty chest: the common case must cost no screen and no tap."""
        class _Never:
            def random(self):
                return 0.99
        self.assertIsNone(dungeon.roll_chest(6, _Never()))

    def test_a_mimic_is_a_shade_stronger_than_the_floor_it_hides_on(self):
        floor = 12
        beast = dungeon.mimic(floor)
        ordinary = dungeon.encounter(floor, 0)
        self.assertGreater(beast["stats"]["strength"], ordinary["stats"]["strength"])
        self.assertGreater(beast["stats"]["health"], ordinary["stats"]["health"])
        # A shade, not a boss: it must not read as the floor's real threat.
        boss = dungeon.encounter(15, 0)
        self.assertLess(beast["stats"]["strength"], boss["stats"]["strength"])
        self.assertFalse(beast["boss"])
        self.assertEqual(beast["gimmick"], "mimic")

    def test_a_bite_is_a_share_of_max_hp_so_it_stings_at_every_depth(self):
        self.assertEqual(dungeon.MIMIC_BITE_SHARE, 0.15)
        for max_hp in (500, 9000):
            self.assertEqual(round(max_hp * dungeon.MIMIC_BITE_SHARE), round(max_hp * 0.15))

    def test_chest_coins_follow_the_floor_they_were_found_between(self):
        shallow, deep = dungeon.chest_gold(3), dungeon.chest_gold(25)
        self.assertGreater(deep, shallow)
        # Worth a couple of ordinary kills, not a floor's whole budget.
        self.assertAlmostEqual(
            shallow / dungeon.reward_for(3, boss=False)["gold"],
            dungeon.CHEST_GOLD_SHARE, delta=0.05,
        )


class ChestBetweenFloorsTests(DungeonTests):
    """The half the model was missing: the chest has to actually turn up, survive a
    reload, pay out, and above all leave the rest of the floor alone."""

    def _a_win(self, hp_left=500):
        """A fight the hero wins outright, leaving them on `hp_left`.

        Scripted rather than simulated: combat luck decides whether a real fight on floor
        two is survived, and none of these tests are about that.
        """
        round_ = pets.pets_combat.Round(1, self.user_id, "hit", 200, hp_left, 0, "")
        return SimpleNamespace(winner=self.user_id, rounds=(round_,), final_hp={})

    def _descend_onto(self, kind, floor=2):
        """Stand the player on a cleared floor and walk them into a known find."""
        data = pets._load(self.entry)
        record = data["pets"][self.user_id]
        record["dungeon_run"] = {
            "run_id": "runid", "kills": 0, "floor": floor - 1, "hp": 400, "max_hp": 400,
            "cleared": [row["index"] for row in dungeon.encounters_for_floor(floor - 1)],
            "haul": pets._new_haul(), "floor_haul": pets._new_haul(),
        }
        pets._save(self.entry, data)
        with patch("pets_dungeon.roll_chest", return_value={"kind": kind, "floor": floor}):
            ok, message = pets.dungeon_descend(self.entry, self.user_id)
        self.assertTrue(ok, message)
        return message

    def test_a_descent_actually_rolls_a_find_and_it_survives_a_reload(self):
        """The whole feature shipped dead because the run whitelist ate this field on the
        next load, which looks exactly like a chest that never appeared."""
        message = self._descend_onto("chest")
        self.assertIn("сундук", message)

        # Read back through the repair path, which is the one that used to drop it.
        run = pets.get_pet(self.entry, self.user_id)["dungeon_run"]
        self.assertEqual(run["chest"], {"kind": "chest", "floor": 2, "revealed": False})
        state = pets.dungeon_status(self.entry, self.user_id)
        self.assertTrue(state["chest"]["present"])
        # And it does NOT say which of the two it is: the guess is the encounter.
        self.assertNotIn("kind", state["chest"])

    def test_nine_descents_in_ten_leave_the_screen_exactly_as_it_was(self):
        data = pets._load(self.entry)
        data["pets"][self.user_id]["dungeon_run"] = {
            "run_id": "runid", "floor": 1, "hp": 400, "max_hp": 400,
            "cleared": [row["index"] for row in dungeon.encounters_for_floor(1)],
        }
        pets._save(self.entry, data)
        with patch("pets_dungeon.roll_chest", return_value=None):
            _ok, message = pets.dungeon_descend(self.entry, self.user_id)
        self.assertNotIn("сундук", message)
        self.assertIsNone(pets.dungeon_status(self.entry, self.user_id)["chest"])

    def test_a_find_never_blocks_the_floor_it_was_found_on(self):
        """The answer to "does this break the other floors": it cannot. The box is a card
        beside the corridor, not a door across it."""
        self._descend_onto("mimic", floor=2)

        # Every ordinary encounter on the floor still fights, and the floor still clears.
        # Looped rather than counted: floor two has healers that put the room back up, and
        # what is being asserted is that the chest changes none of that.
        for _attempt in range(40):
            state = pets.dungeon_status(self.entry, self.user_id)
            standing = [row for row in state["encounters"] if not row["cleared"]]
            if not standing:
                break
            with patch("pets.pets_combat.simulate", return_value=self._a_win()):
                ok, message, _receipt = pets.dungeon_fight(
                    self.entry, self.user_id, standing[0]["index"])
            self.assertTrue(ok, message)
        state = pets.dungeon_status(self.entry, self.user_id)
        self.assertTrue(state["can_rest"])
        self.assertTrue(state["chest"]["present"], "an ignored chest simply waits")

        # And descending past an unopened one works, replacing it rather than stacking.
        with patch("pets_dungeon.roll_chest", return_value=None):
            self.assertTrue(pets.dungeon_descend(self.entry, self.user_id)[0])
        state = pets.dungeon_status(self.entry, self.user_id)
        self.assertEqual(state["floor"], 3)
        self.assertIsNone(state["chest"], "the corridor behind you is gone")

    def test_a_plain_chest_pays_coins_diamonds_a_cursed_item_and_a_rune(self):
        self._descend_onto("chest", floor=4)
        before = economy.balance(self.entry, self.user_id, 0)

        ok, _message, receipt = pets.dungeon_chest_open(self.entry, self.user_id)

        self.assertTrue(ok)
        self.assertEqual(receipt["reward"]["gold"], dungeon.chest_gold(4))
        self.assertEqual(economy.balance(self.entry, self.user_id, 0),
                         before + dungeon.chest_gold(4))
        self.assertIn(receipt["rubies"], range(*dungeon.CHEST_RUBY_RANGE[:1] + (4,)))
        self.assertEqual(pets.ruby_balance(self.entry, self.user_id), receipt["rubies"])
        self.assertEqual(len(receipt["dropped"]), 1)
        self.assertEqual(receipt["dropped"][0]["rarity"], "cursed")
        self.assertEqual(len(receipt["rune"]), 1)
        # It lands on the run's tally, so the closing receipt can report it.
        haul = pets.dungeon_haul(self.entry, self.user_id)["total"]
        self.assertEqual(haul["gold"], dungeon.chest_gold(4))
        self.assertEqual(haul["items"], [receipt["dropped"][0]["name"]])
        self.assertEqual(haul["kills"], 0, "opening a box is not a victory")
        # One press, one payout: the chest is gone afterwards.
        self.assertIsNone(pets.dungeon_status(self.entry, self.user_id)["chest"])
        self.assertFalse(pets.dungeon_chest_open(self.entry, self.user_id)[0])

    def test_the_wrong_box_bites_a_share_of_max_hp_and_then_offers_the_choice(self):
        self._descend_onto("mimic", floor=4)

        ok, message, receipt = pets.dungeon_chest_open(self.entry, self.user_id)

        self.assertTrue(ok)
        self.assertEqual(receipt["bite"], dungeon.mimic_bite(400))
        run = pets.get_pet(self.entry, self.user_id)["dungeon_run"]
        self.assertEqual(run["hp"], 400 - dungeon.mimic_bite(400))
        self.assertIn("HP", message)
        state = pets.dungeon_status(self.entry, self.user_id)["chest"]
        self.assertTrue(state["revealed"])
        self.assertEqual(state["kind"], "mimic")
        # The lid is not a second attack: opening it again does nothing.
        self.assertFalse(pets.dungeon_chest_open(self.entry, self.user_id)[0])

    def test_a_bite_can_hurt_but_never_ends_a_run_on_its_own(self):
        """A box that kills outright is a trap nobody opens twice. The FIGHT is where the
        run is allowed to end."""
        self._descend_onto("mimic", floor=4)
        data = pets._load(self.entry)
        data["pets"][self.user_id]["dungeon_run"]["hp"] = 3
        pets._save(self.entry, data)

        pets.dungeon_chest_open(self.entry, self.user_id)

        run = pets.get_pet(self.entry, self.user_id)["dungeon_run"]
        self.assertIsNotNone(run, "the run survives the lid")
        self.assertEqual(run["hp"], 1)

    def test_walking_away_from_a_woken_mimic_costs_the_bite_and_nothing_else(self):
        self._descend_onto("mimic", floor=4)
        pets.dungeon_chest_open(self.entry, self.user_id)
        bitten = pets.get_pet(self.entry, self.user_id)["dungeon_run"]["hp"]

        ok, message = pets.dungeon_chest_leave(self.entry, self.user_id)

        self.assertTrue(ok)
        self.assertIn("мимик", message.lower())
        run = pets.get_pet(self.entry, self.user_id)["dungeon_run"]
        self.assertEqual(run["hp"], bitten)
        self.assertIsNone(run["chest"])
        self.assertFalse(pets.dungeon_chest_fight(self.entry, self.user_id)[0])

    def test_beating_a_mimic_pays_better_than_the_chest_it_pretended_to_be(self):
        self._descend_onto("mimic", floor=4)
        pets.dungeon_chest_open(self.entry, self.user_id)

        loot = {"gold": 900, "rubies": 3, "cursed": 2, "drops": 1, "runes": 1}
        with patch("pets_dungeon.mimic_loot", return_value=loot), \
                patch("pets.pets_combat.simulate", return_value=self._a_win()):
            ok, _message, receipt = pets.dungeon_chest_fight(self.entry, self.user_id)

        self.assertTrue(ok)
        self.assertEqual(receipt["reward"]["gold"], 900)
        self.assertGreater(receipt["reward"]["xp"], 0, "a fight still pays experience")
        self.assertEqual(len(receipt["dropped"]), 3)
        # Two cursed by NAME plus one roll on the whole table -- which may itself land on
        # a cursed item, so the promise is a floor rather than an exact count.
        self.assertGreaterEqual(
            sum(row["rarity"] == "cursed" for row in receipt["dropped"]), 2)
        haul = pets.dungeon_haul(self.entry, self.user_id)["total"]
        self.assertEqual(haul["kills"], 1, "this one WAS a victory")
        self.assertEqual(len(haul["items"]), 3)
        self.assertIsNone(pets.dungeon_status(self.entry, self.user_id)["chest"])

    def test_a_mimic_that_was_empty_after_all_still_says_so(self):
        self._descend_onto("mimic", floor=4)
        pets.dungeon_chest_open(self.entry, self.user_id)

        with patch("pets_dungeon.mimic_loot", return_value=None), \
                patch("pets.pets_combat.simulate", return_value=self._a_win()):
            ok, message, receipt = pets.dungeon_chest_fight(self.entry, self.user_id)

        self.assertTrue(ok)
        self.assertEqual(message, dungeon.MIMIC_EMPTY_NOTICE)
        self.assertEqual(receipt["reward"]["gold"], 0)
        self.assertEqual(pets.dungeon_haul(self.entry, self.user_id)["total"]["kills"], 1)

    def test_losing_to_a_mimic_ends_the_run_and_keeps_the_receipt(self):
        self._descend_onto("mimic", floor=4)
        pets.dungeon_chest_open(self.entry, self.user_id)
        data = pets._load(self.entry)
        run = data["pets"][self.user_id]["dungeon_run"]
        run["haul"] = {**pets._new_haul(), "gold": 640, "kills": 3, "scrolls": ["Пламя"]}
        run["hp"] = 1
        data["pets"][self.user_id]["stats"] = {
            "strength": 1, "health": 1, "agility": 1, "luck": 1, "endurance": 1,
        }
        pets._save(self.entry, data)

        ok, _message, receipt = pets.dungeon_chest_fight(self.entry, self.user_id)

        self.assertFalse(ok)
        self.assertTrue(receipt["run_over"])
        self.assertEqual(receipt["haul"]["total"]["gold"], 640)
        state = pets.dungeon_status(self.entry, self.user_id)
        self.assertFalse(state["active"])
        self.assertEqual(state["last_haul"]["gold"], 640)


class RunSummaryOnTheDefeatScreenTests(DungeonTests):
    """What a dead run was worth, on the screen the player is standing on when it dies.

    dungeon_finished_text existed and was tested, and no screen in the game ever called
    it -- so the one moment a player most wants the number showed them the entry price
    instead.
    """

    def _dead_run(self, **haul):
        data = pets._load(self.entry)
        record = data["pets"][self.user_id]
        record["last_dungeon_haul"] = {**pets._new_haul(), "floor": 7, "won": False, **haul}
        pets._save(self.entry, data)
        return pets.dungeon_status(self.entry, self.user_id)

    def test_the_screen_after_a_defeat_prints_the_takings_and_the_praise(self):
        state = self._dead_run(gold=1240, xp=300, rubies=2, kills=9,
                               items=["Ржавый клык"], runes=["Огонь"])

        text, _keyboard = pets_ui.dungeon_view(self.entry, self.user_id, 0)

        self.assertIn("оборвался на этаже 7", text)
        self.assertIn("Побед: 9", text)
        self.assertIn("Всего за поход", text)
        self.assertIn("1.240", text)
        self.assertIn("Ржавый клык", text)
        self.assertIn("Хорошая нажива", text)
        # The tally the screen printed is the one the state carried.
        self.assertEqual(state["last_haul"]["gold"], 1240)

    def test_a_bigger_haul_gets_the_bigger_praise(self):
        self._dead_run(gold=9000, kills=40)
        text, _keyboard = pets_ui.dungeon_view(self.entry, self.user_id, 0)
        self.assertIn("Отличная нажива", text)

    def test_an_empty_run_is_told_it_was_empty_rather_than_told_nothing(self):
        self._dead_run()
        text, _keyboard = pets_ui.dungeon_view(self.entry, self.user_id, 0)
        self.assertIn("оборвался", text)
        self.assertIn("пустыми руками", text)


class BossWeaknessTests(unittest.TestCase):
    """A boss whose rule is hidden is a boss that kills you for something you had no way
    to know. Every gimmick that changes how damage lands says so on the floor screen."""

    def test_every_gimmick_in_the_roster_explains_itself(self):
        for name, gimmick, _hint in dungeon.BOSSES:
            with self.subTest(boss=name):
                if gimmick == "standard":
                    continue
                self.assertTrue(dungeon.boss_weakness(gimmick),
                                f"{name} fights by a rule nothing explains")

    def test_the_line_describes_the_boss_rather_than_ordering_the_player(self):
        """"Возьми огненное оружие" tells somebody what to do without telling them why.
        The same fact as a property of the enemy is what a hint is for."""
        fire = dungeon.boss_weakness("fire_only")
        self.assertIn("Уязвим к огню", fire)
        self.assertNotIn("Возьми", fire)
        for gimmick in ("fire_only", "frost_only", "spells_only", "antimagic"):
            self.assertNotIn("Возьми", dungeon.boss_weakness(gimmick))

    def test_the_encounter_carries_it_separately_from_the_flavour(self):
        boss = dungeon.encounter(15, 0)
        self.assertEqual(boss["weakness"], dungeon.boss_weakness("fire_only"))
        # The scene-setting line stays its own thing; the rule must not be buried in it.
        self.assertNotEqual(boss["hint"], boss["weakness"])
        self.assertEqual(dungeon.encounter(10, 0)["weakness"], "")
        # And an ordinary corridor mob has no rule at all.
        self.assertEqual(dungeon.encounter(2, 0).get("weakness", ""), "")


class DungeonCoinBonusTests(unittest.TestCase):
    def test_dungeon_coins_carry_the_thirty_percent_bonus(self):
        self.assertEqual(dungeon.COIN_REWARD_BONUS, 1.30)
        for floor, boss in ((3, False), (12, False), (15, True), (45, True)):
            with self.subTest(floor=floor, boss=boss):
                count = 1 if boss else len(dungeon.encounters_for_floor(floor))
                paid = dungeon.reward_for(floor, boss=boss, enemy_count=count)["gold"]
                base = (200 + floor * 15) if boss else (120 + floor * 6) // count
                self.assertEqual(paid, round(base * dungeon.COIN_REWARD_BONUS))

    def test_experience_is_deliberately_left_alone(self):
        """XP buys levels, which cost diamonds. Paying more of it would pay twice."""
        floor = 12
        count = len(dungeon.encounters_for_floor(floor))
        self.assertEqual(
            dungeon.reward_for(floor, boss=False, enemy_count=count)["xp"],
            (35 + floor * 12) // count,
        )


class ArenaLogLivesOutsideTheHotStoreTests(unittest.TestCase):
    """The arena log is round-by-round transcripts of two thousand duels, and it lived in
    the file every single pet action parses and rewrites in full.

    Measured on a forty-player store at the cap: 14.3 MB, of which the log was 97.6%. One
    dungeon press -- which never reads or writes a duel -- dragged it through seven parses
    and rewrites and took 1.25 seconds, of which the combat simulation was about two
    milliseconds. It is history and a replay archive, so it belongs in its own file.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self.temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.temp.cleanup)
        self.entry = "arena-log"
        for uid, name in (("1", "Один"), ("2", "Два")):
            pets.buy_cage(self.entry, uid, 100_000)
            pets.tame(self.entry, uid, 100_000, name, None, f"Owner{uid}")

    def _row(self, index, day="2026-08-19"):
        return {
            "fight_id": f"F-{index}", "ts": f"{day}T10:00:00+03:00", "date": day,
            "attacker_id": "1", "defender_id": "2", "winner_id": "1", "loser_id": "2",
            "draw": False, "gold": 10, "loss_gold": 0, "consolation_gold": 0,
            "rounds": [{"number": 1, "attacker": "1", "event": "hit", "damage": 5,
                        "attacker_hp": 90, "defender_hp": 80, "text": "x" * 80}],
        }

    def test_a_finished_duel_lands_in_the_log_file_and_not_in_the_store(self):
        result = SimpleNamespace(
            winner="1", loser="2", is_draw=False, seed=1, rounds=(),
            total_damage={"1": 10, "2": 4}, final_hp={},
        )
        pets.record_fight(self.entry, "1", "2", result, pets.today())

        self.assertEqual(len(pets.fight_log_rows(self.entry)), 1)
        self.assertNotIn("fights", pets._load(self.entry))
        self.assertTrue(pets._fight_log_path(self.entry).exists())
        # And the counters the fight moved are still in the store, where they belong:
        # the log is history, the record is the source of truth.
        self.assertEqual(pets.get_pet(self.entry, "1")["wins"], 1)
        self.assertEqual(pets.get_pet(self.entry, "1")["fights"], 1)

    def test_a_store_with_the_old_inline_log_is_migrated_off_it(self):
        import stats
        data = pets._load(self.entry)
        data["fights"] = [self._row(index) for index in range(300)]
        stats._write_json_atomic(pets._pets_path(self.entry), data)
        fat = pets._pets_path(self.entry).stat().st_size

        # Readable in full while the move is still in flight, and the screens that read
        # it still see it -- history shows its own page of the newest.
        self.assertEqual(len(pets.fight_log_rows(self.entry)), 300)
        self.assertEqual(len(pets.history(self.entry, "1")), pets_config.HISTORY_LIMIT)
        self.assertEqual(pets.history(self.entry, "1")[0]["fight_id"], "F-299")

        # The next ordinary write is what drops it, and it stays dropped even though this
        # caller loaded its copy before the log moved.
        stale = pets._load(self.entry)
        pets._save(self.entry, stale)

        self.assertNotIn("fights", pets._load(self.entry))
        self.assertLess(pets._pets_path(self.entry).stat().st_size, fat // 4)
        rows = pets.fight_log_rows(self.entry)
        self.assertEqual(len(rows), 300, "no duel may be lost moving house")
        self.assertEqual([row["fight_id"] for row in rows],
                         [f"F-{index}" for index in range(300)], "nor reordered")

    def test_migrating_twice_does_not_double_a_single_row(self):
        """The drain owns the move and the readers are pure. If reading migrated too, the
        rows would be written once by the reader and again by the next save."""
        import stats
        data = pets._load(self.entry)
        data["fights"] = [self._row(index) for index in range(20)]
        stats._write_json_atomic(pets._pets_path(self.entry), data)

        for _ in range(3):
            pets.fight_log_rows(self.entry)
            pets.history(self.entry, "1")
        pets._save(self.entry, pets._load(self.entry))
        pets._save(self.entry, pets._load(self.entry))

        rows = pets.fight_log_rows(self.entry)
        self.assertEqual(len(rows), 20)
        self.assertEqual(len({row["fight_id"] for row in rows}), 20)

    def test_a_dungeon_press_never_touches_the_arena_log_at_all(self):
        """The whole point. A kill in the dungeon has nothing to do with a duel, and used
        to rewrite every one of them anyway."""
        import stats
        data = pets._load(self.entry)
        data["pets"]["1"]["stats"] = {
            "strength": 400, "health": 400, "agility": 400, "luck": 400, "endurance": 1,
        }
        data["pets"]["1"]["dungeon_run"] = {
            "run_id": "r", "kills": 0, "floor": 1, "hp": 5000, "max_hp": 5000,
            "cleared": [], "haul": pets._new_haul(), "floor_haul": pets._new_haul(),
        }
        pets._save(self.entry, data)
        stats._write_json_atomic(
            pets._fight_log_path(self.entry), [self._row(index) for index in range(50)],
        )
        before = pets._fight_log_path(self.entry).stat().st_mtime_ns

        self.assertTrue(pets.dungeon_fight(self.entry, "1", 0)[0])

        self.assertEqual(pets._fight_log_path(self.entry).stat().st_mtime_ns, before,
                         "a dungeon kill must not rewrite two thousand duels")
        self.assertEqual(len(pets.fight_log_rows(self.entry)), 50)

    def test_the_log_is_capped_in_its_own_file(self):
        import stats
        stats._write_json_atomic(
            pets._fight_log_path(self.entry),
            [self._row(index) for index in range(pets.FIGHT_LOG_LIMIT + 40)],
        )
        result = SimpleNamespace(
            winner="1", loser="2", is_draw=False, seed=1, rounds=(),
            total_damage={"1": 10, "2": 4}, final_hp={},
        )
        pets.record_fight(self.entry, "1", "2", result, pets.today())

        rows = pets.fight_log_rows(self.entry)
        self.assertEqual(len(rows), pets.FIGHT_LOG_LIMIT)
        # The newest survives the trim and the oldest is what falls off it.
        self.assertNotEqual(rows[-1]["fight_id"], f"F-{pets.FIGHT_LOG_LIMIT + 39}")
        self.assertNotIn("F-0", {row["fight_id"] for row in rows})


class RubyLedgerLivesOutsideTheHotStoreTests(unittest.TestCase):
    """The ledger is written once per ruby and read only by the audit. Kept inside the
    pets store it was parsed and rewritten by EVERY pet action: at its old 50,000-row cap
    the store reached 8 MB and one dungeon press cost a quarter of a second before the
    game had done anything."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self.temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.temp.cleanup)
        self.entry, self.user_id = "ledger", "42"
        pets.buy_cage(self.entry, self.user_id, 100_000)
        pets.tame(self.entry, self.user_id, 100_000, "Hero", None, "Tester")

    def test_the_pets_store_never_carries_the_ledger(self):
        for _ in range(30):
            pets.grant_rubies(self.entry, self.user_id, 1, "pet_mob_win")
        self.assertEqual(len(pets.ruby_log_rows(self.entry)), 30)
        self.assertNotIn("ruby_log", pets._load(self.entry))
        self.assertTrue(pets._ruby_log_path(self.entry).exists())

    def test_a_store_with_the_old_inline_ledger_is_migrated_off_it(self):
        # Written straight to the file, because _save now drains the key on the way out
        # -- which is the fix. This is the shape production is actually carrying.
        import stats
        data = pets._load(self.entry)
        data["ruby_log"] = [
            {"ts": "2026-08-18T10:00:00+03:00", "user_id": self.user_id,
             "delta": 1, "reason": "dungeon-ruby:mob:a", "ref": ""}
            for _ in range(500)
        ]
        stats._write_json_atomic(pets._pets_path(self.entry), data)
        fat = pets._pets_path(self.entry).stat().st_size

        rows = pets.ruby_log_rows(self.entry)
        self.assertEqual(len(rows), 500, "no row may be lost moving house")
        self.assertEqual(
            pets.ruby_income_report(self.entry, days=None)["totals"]["earned"], 500,
        )

        # The next ordinary write is what actually drops it, and it must stay dropped even
        # though this caller loaded its copy before the ledger moved.
        stale = pets._load(self.entry)
        pets._save(self.entry, stale)
        self.assertNotIn("ruby_log", pets._load(self.entry))
        self.assertLess(pets._pets_path(self.entry).stat().st_size, fat // 4)
        self.assertEqual(len(pets.ruby_log_rows(self.entry)), 500, "and no row is lost")

    def test_the_ledger_is_capped_where_the_audit_stops_looking(self):
        self.assertEqual(pets.RUBY_LOG_LIMIT, 10_000)
        data = pets._load(self.entry)
        data["ruby_log"] = [
            {"ts": "2026-08-18T10:00:00+03:00", "user_id": self.user_id,
             "delta": 1, "reason": "x", "ref": ""}
            for _ in range(pets.RUBY_LOG_LIMIT + 250)
        ]
        pets._save(self.entry, data)
        pets.grant_rubies(self.entry, self.user_id, 1, "pet_mob_win")
        self.assertLessEqual(len(pets.ruby_log_rows(self.entry)), pets.RUBY_LOG_LIMIT)
