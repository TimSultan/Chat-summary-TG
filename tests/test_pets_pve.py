"""PVE (mobs, rubies) and Зеркало души, the two features layered on top of the arena's
own duel/fight-bank/reward machinery.

pets_mobs.py's module docstring is the spec these tests pin down: a mob is generated
relative to the PLAYER who picked the fight (never a fixed stat block), it pays about
half an arena win because nobody is on the other side to lose anything, and each mob's
gold/loot/ruby fields are multipliers on that halved purse rather than second economy.
Зеркало души is the other half of the file: it lets a strong pet fight a weak one
without the fight being a foregone conclusion or the reward being docked for it.

Fixture copied from tests/test_pets.py (PetsTestCase, `_tame`) rather than imported, so
this file's storage setup matches the rest of the suite exactly.
"""

import random
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import economy
import pets
import pets_combat
import pets_config
import pets_mobs
import pets_ui


class PetsTestCase(unittest.TestCase):
    """Base fixture: point stats._stats_dir (and therefore both economy's and pets'
    storage) at a throwaway directory, the same way tests/test_pets.py does."""

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


class _NoJitter:
    """A stand-in rng whose `.uniform` always lands dead centre -- isolates a formula
    from the ± spread layered on top of it, the same way pinning `random.uniform` to
    0.0 would if the target function read from the `random` module directly."""

    def uniform(self, low, high):
        return 0.0


class MobRollAndBlockTests(PetsTestCase):
    def test_owned_mob_gear_auto_equips_for_one_fight_then_restores_loadout(self):
        entry = "chat"
        self._tame(entry, "1")
        data = pets._load(entry)
        record = data["pets"]["1"]
        record["inventory"] = ["w001", pets.MIRROR_AMULET_CODE, "w009", "amulet_mob_ward"]
        record["equipped"]["weapon"] = "w001"
        record["equipped"]["amulet"] = pets.MIRROR_AMULET_CODE
        pets._save(entry, data)

        self.assertEqual(
            set(pets.auto_equip_mob_gear(entry, "1")), {"w009", "amulet_mob_ward"},
        )
        active = pets.get_pet(entry, "1")["equipped"]
        self.assertEqual(active["weapon"], "w009")
        self.assertEqual(active["amulet"], "amulet_mob_ward")

        self.assertTrue(pets.restore_after_mob_gear(entry, "1"))
        restored = pets.get_pet(entry, "1")["equipped"]
        self.assertEqual(restored["weapon"], "w001")
        self.assertEqual(restored["amulet"], pets.MIRROR_AMULET_CODE)
        self.assertFalse(pets.restore_after_mob_gear(entry, "1"))

    def test_store_keeps_the_two_mob_items_available_and_their_effects_are_targeted(self):
        weapon = pets_config.find_item("w009")
        amulet = pets_config.find_item("amulet_mob_ward")
        self.assertEqual(weapon.name, "Копьё зверобоя")
        self.assertEqual(weapon.effect["code"], "mob_hunter")
        self.assertEqual(amulet.effect["code"], "mob_ward")
        # Both counters are RARE shop items, so they take the one rare slot on their
        # shelf when their turn comes rather than sitting there permanently. What has to
        # hold is that they are genuinely reachable -- in the rotation and turning up
        # inside a couple of months -- not that they are on sale every single day.
        for item in (weapon, amulet):
            with self.subTest(item=item.code):
                self.assertEqual(item.source, "shop")
                days = [
                    day for day in range(90)
                    if item.code in {
                        offer.code for offer in pets_config.daily_storefront_items(
                            "chat", item.slot, pets.today() + timedelta(days=day),
                        )
                    }
                ]
                self.assertTrue(days, f"{item.code} never reaches the shelf")

        def fighter(key, effects=()):
            return pets_combat.Fighter(
                key=key, name=key, strength=40, health=40, agility=10, luck=10,
                armor=0, effects=effects, level=10,
            )

        hunter = fighter("player", ({"code": "mob_hunter", "value": 15},))
        vs_mob = pets_combat.simulate(hunter, fighter("mob:orc"), seed=42)
        vs_pet = pets_combat.simulate(hunter, fighter("pet"), seed=42)
        self.assertGreater(vs_mob.total_damage["player"], vs_pet.total_damage["player"])

        ward = fighter("player", ({"code": "mob_ward", "value": 15},))
        ward_vs_mob = pets_combat.simulate(ward, fighter("mob:orc"), seed=42)
        ward_vs_pet = pets_combat.simulate(ward, fighter("pet"), seed=42)
        self.assertLess(ward_vs_mob.total_damage["mob:orc"], ward_vs_pet.total_damage["pet"])

    def test_mob_fight_callback_keeps_both_code_and_tier(self):
        """A compound callback argument must survive Telegram's outer separators.

        Losing the tier made `mob_block` reject every normal «В бой» tap as if the mob
        had disappeared, because it received only the code.
        """
        callback = pets_ui.callback_data("123", "mobfight", "goblin:hard")
        self.assertEqual(pets_ui.parse_callback(callback), ("123", "mobfight", "goblin:hard"))

    def test_mob_result_offers_another_mob_not_a_player_search(self):
        keyboard = pets_ui.mob_result_keyboard("123")
        button = keyboard["inline_keyboard"][0][0]
        self.assertEqual(button["text"], "👾 Найти моба")
        self.assertEqual(pets_ui.parse_callback(button["callback_data"]), ("123", "mob", ""))

    def test_roll_mob_scales_to_the_callers_own_stats_and_returns_none_without_a_pet(self):
        entry = "chat"
        self.assertIsNone(pets.roll_mob(entry, "nobody"))

        self._tame(entry, "1")
        data = pets._load(entry)
        data["pets"]["1"]["stats"] = {"strength": 20, "health": 22, "agility": 24, "luck": 26}
        pets._save(entry, data)
        mine = pets.effective_stats(entry, "1")

        block = pets.roll_mob(entry, "1", rng=random.Random(3))
        self.assertIsNotNone(block)
        self.assertIn(block["code"], {mob.code for mob in pets_mobs.MOBS})
        self.assertIn(block["tier"], pets_mobs.TIERS)
        scale, spread = pets_mobs.TIER_SCALING[block["tier"]]
        variable_power = pets._power_from(mine, mine["armor"]) - pets_config.POWER_RATING_BASE
        target = variable_power * scale
        # One jitter is applied to the complete profile, not independently to every
        # stat. Integer stat points can move the shown rating by at most eight points.
        shown = block["power"] - pets_config.POWER_RATING_BASE
        self.assertGreaterEqual(shown, target * (1 - spread) - 8)
        self.assertLessEqual(shown, target * (1 + spread) + 8)
        self.assertEqual(block["level"], pets.get_pet(entry, "1")["level"])
        self.assertEqual(block["power"], pets._power_from(block["stats"], block["armor"]))

    def test_prefetched_roster_has_five_distinct_mobs_and_every_difficulty(self):
        entry = "chat"
        self.assertIsNone(pets.roll_mobs(entry, "nobody", rng=random.Random(7)))
        self._tame(entry, "1")

        roster = pets.roll_mobs(entry, "1", count=5, rng=random.Random(7))

        self.assertEqual(len(roster), 5)
        self.assertEqual(len({row["code"] for row in roster}), 5)
        self.assertEqual({row["tier"] for row in roster}, set(pets_mobs.TIERS))
        self.assertTrue(all(row["power"] == pets._power_from(row["stats"], row["armor"])
                            for row in roster))

    def test_each_tier_scales_one_combat_profile_as_documented(self):
        """With jitter pinned, a tier changes total variable combat power once.

        The profile stays recognisable: strength, health, dodge, crit and armour cannot
        independently spike into a much tougher fight than the tier says.
        """
        entry = "chat"
        self._tame(entry, "1")
        data = pets._load(entry)
        data["pets"]["1"]["stats"] = {"strength": 40, "health": 50, "agility": 60, "luck": 70}
        pets._save(entry, data)
        mine = pets.effective_stats(entry, "1")
        mob = pets_mobs.MOBS[0]

        expected_scale = {"easy": 0.78, "medium": 0.95, "hard": 1.05}
        variable_power = pets._power_from(mine, mine["armor"]) - pets_config.POWER_RATING_BASE
        for tier, scale in expected_scale.items():
            self.assertEqual(pets_mobs.TIER_SCALING[tier][0], scale)
            block = pets.mob_block(entry, "1", mob.code, tier, rng=_NoJitter())
            profile_scale = round(variable_power * scale) / variable_power
            for key in pets_config.STAT_KEYS:
                self.assertEqual(
                    block["stats"][key], max(0, round(mine[key] * profile_scale)), (tier, key),
                )
            self.assertEqual(block["armor"], round(mine["armor"] * profile_scale))

    def test_mob_block_rebuilds_stats_from_code_and_tier_only(self):
        """A client is handed a block and hands the code+tier back; this is what makes
        that safe. Editing the stats -- or anything else -- in what it hands back must
        not change what mob_block regenerates, because mob_block's signature never
        accepts a stats argument at all: it always recomputes from the player's own
        current numbers."""
        entry = "chat"
        self._tame(entry, "1")
        data = pets._load(entry)
        data["pets"]["1"]["stats"] = {"strength": 30, "health": 30, "agility": 30, "luck": 30}
        pets._save(entry, data)
        mine = pets.effective_stats(entry, "1")
        mob = pets_mobs.MOBS[0]
        scale, spread = pets_mobs.TIER_SCALING["hard"]
        variable_power = pets._power_from(mine, mine["armor"]) - pets_config.POWER_RATING_BASE
        profile_scale = round(variable_power * scale) / variable_power

        shown = pets.mob_block(entry, "1", mob.code, "hard", rng=_NoJitter())
        # A rogue client tries to hand back a weakened, higher-level forgery.
        forged_stats = {key: 1 for key in pets_config.STAT_KEYS}
        forged_level = 999

        rebuilt = pets.mob_block(entry, "1", shown["code"], shown["tier"], rng=_NoJitter())
        for key in pets_config.STAT_KEYS:
            self.assertEqual(rebuilt["stats"][key], max(0, round(mine[key] * profile_scale)))
            self.assertNotEqual(rebuilt["stats"][key], forged_stats[key])
        self.assertNotEqual(rebuilt["level"], forged_level)
        self.assertEqual(rebuilt["level"], pets.get_pet(entry, "1")["level"])

        # Two independent rebuilds of the same code+tier land in the identical band,
        # regardless of anything else (a fresh rng here) passed alongside them.
        again = pets.mob_block(entry, "1", mob.code, "hard", rng=random.Random(4))
        shown_variable = again["power"] - pets_config.POWER_RATING_BASE
        self.assertGreaterEqual(shown_variable, variable_power * scale * (1 - spread) - 8)
        self.assertLessEqual(shown_variable, variable_power * scale * (1 + spread) + 8)


class MobFightBankAndRewardTests(PetsTestCase):
    def test_mob_fights_spend_their_own_counter_and_never_the_arena_bank(self):
        """PVE has an allowance of its own. If the two ever share again, an empty arena
        bank silently takes the mobs with it -- and a night of PVE eats the duels the
        arena's hourly recharge was pacing."""
        entry = "chat"
        self._tame(entry, "1", "Attacker")
        self._tame(entry, "2", "Defender")
        now = datetime(2026, 8, 9, 9, 0)
        arena_before = pets.fights_left(entry, "1", now=now)
        self.assertEqual(
            pets.pve_allowance(entry, "1", now=now)["available"],
            pets_config.PVE_ATTACKS_PER_WINDOW,
        )

        block = pets.mob_block(entry, "1", pets_mobs.MOBS[0].code, "medium", rng=random.Random(1))
        loss = SimpleNamespace(winner=f"mob:{block['code']}", is_draw=False)
        for spent in range(1, pets_config.PVE_ATTACKS_PER_WINDOW + 1):
            pets.record_mob_fight(entry, "1", block, loss, now=now)
            self.assertEqual(
                pets.pve_allowance(entry, "1", now=now)["available"],
                pets_config.PVE_ATTACKS_PER_WINDOW - spent,
            )
        # Ten mob fights later the arena bank has not moved at all.
        self.assertEqual(pets.fights_left(entry, "1", now=now), arena_before)

        with self.assertRaises(ValueError):
            pets.record_mob_fight(entry, "1", block, loss, now=now)
        # And a duel still works, because it was never the thing that ran out.
        duel = SimpleNamespace(winner="1", loser="2", is_draw=False)
        pets.record_fight(entry, "1", "2", duel, now.date(), now=now)
        self.assertEqual(pets.fights_left(entry, "1", now=now), arena_before - 1)

    def test_an_empty_arena_bank_still_allows_mob_fights(self):
        """The other direction of the same separation, and the one the Mini App got wrong.

        Out of duels is not out of mobs. The arena panel greyed the PVE buttons out
        whenever the duel bank hit zero, describing a rule the server has never had --
        so this pins the server's actual answer from an exhausted arena.
        """
        entry = "chat"
        self._tame(entry, "1", "Attacker")
        self._tame(entry, "2", "Defender")
        now = datetime(2026, 8, 9, 9, 0)

        duel = SimpleNamespace(winner="1", loser="2", is_draw=False)
        for _ in range(pets.fights_left(entry, "1", now=now)):
            pets.record_fight(entry, "1", "2", duel, now.date(), now=now)
        self.assertEqual(pets.fights_left(entry, "1", now=now), 0)

        # The mob allowance never noticed, and a mob fight goes through on an empty bank.
        self.assertEqual(
            pets.pve_allowance(entry, "1", now=now)["available"],
            pets_config.PVE_ATTACKS_PER_WINDOW,
        )
        block = pets.mob_block(entry, "1", pets_mobs.MOBS[0].code, "medium", rng=random.Random(1))
        loss = SimpleNamespace(winner=f"mob:{block['code']}", is_draw=False)
        pets.record_mob_fight(entry, "1", block, loss, now=now)
        self.assertEqual(
            pets.pve_allowance(entry, "1", now=now)["available"],
            pets_config.PVE_ATTACKS_PER_WINDOW - 1,
        )
        self.assertEqual(pets.fights_left(entry, "1", now=now), 0)

    def test_the_pve_window_resets_for_everybody_at_the_same_wall_clock_moment(self):
        """"Таймер сбрасывается у всех на сервере одновременно" -- so the window is a
        fixed block of the chat's own clock (00:00 / 08:00 / 16:00), not a countdown
        started by each player's first fight."""
        entry = "chat"
        self._tame(entry, "1", "Early")
        self._tame(entry, "2", "Late")
        block = pets.mob_block(entry, "1", pets_mobs.MOBS[0].code, "easy", rng=random.Random(1))
        loss = SimpleNamespace(winner=f"mob:{block['code']}", is_draw=False)

        # Two players start hours apart inside the same block.
        pets.record_mob_fight(entry, "1", block, loss, now=datetime(2026, 8, 9, 8, 5))
        pets.record_mob_fight(entry, "2", block, loss, now=datetime(2026, 8, 9, 15, 55))
        for user_id in ("1", "2"):
            spent = pets.pve_allowance(entry, user_id, now=datetime(2026, 8, 9, 15, 59))
            self.assertEqual(spent["used"], 1)
            self.assertEqual(spent["resets_at"][11:16], "16:00")

        # ...and both come back full at the very same moment.
        for user_id in ("1", "2"):
            after = pets.pve_allowance(entry, user_id, now=datetime(2026, 8, 9, 16, 0))
            self.assertEqual(after["available"], pets_config.PVE_ATTACKS_PER_WINDOW)
            self.assertEqual(after["used"], 0)

    def test_the_windows_are_eight_hours_and_start_at_midnight(self):
        for hour, ends in ((0, "08:00"), (7, "08:00"), (8, "16:00"),
                           (15, "16:00"), (16, "00:00"), (23, "00:00")):
            with self.subTest(hour=hour):
                moment = datetime(2026, 8, 11, hour, 30)
                allowance = pets.pve_allowance("chat", "nobody", now=moment)
                self.assertEqual(allowance["resets_at"][11:16], ends)
                self.assertEqual(allowance["window_hours"], pets_config.PVE_WINDOW_HOURS)

    def test_a_farming_pet_cannot_start_a_mob_fight(self):
        entry = "chat"
        self._tame(entry, "1")
        now = datetime(2026, 8, 9, 9, 0)
        data = pets._load(entry)
        data["pets"]["1"]["farm_run"] = {"ready_at": (now + timedelta(hours=1)).isoformat()}
        pets._save(entry, data)

        block = pets.mob_block(entry, "1", pets_mobs.MOBS[0].code, "easy", rng=random.Random(1))
        with self.assertRaises(ValueError):
            pets.record_mob_fight(entry, "1", block, SimpleNamespace(winner="1"), now=now)
        # The guard runs before the bank is touched.
        self.assertEqual(pets.fights_left(entry, "1", now=now), pets_config.BASE_FIGHT_BANK_CAPACITY)

    def test_a_win_pays_gold_and_xp_a_loss_pays_only_loss_xp_and_neither_touches_history(self):
        entry = "chat"
        self._tame(entry, "1")
        now = datetime(2026, 8, 9, 9, 0)
        block = pets.mob_block(entry, "1", pets_mobs.MOBS[0].code, "medium", rng=random.Random(1))

        win = pets.record_mob_fight(
            entry, "1", block, SimpleNamespace(winner="1", is_draw=False), now=now,
        )
        self.assertGreater(win["gold"], 0)
        expected_win_xp = max(1, round(
            pets_config.WIN_XP * pets_config.PVE_XP_SHARE * pets_mobs.TIER_REWARD["medium"]
        ))
        self.assertEqual(win["xp"], expected_win_xp)

        loss = pets.record_mob_fight(
            entry, "1", block, SimpleNamespace(winner=f"mob:{block['code']}", is_draw=False), now=now,
        )
        self.assertEqual(loss["gold"], 0)
        self.assertEqual(loss["xp"], pets_config.LOSS_XP)

        # A mob has no duel history to keep -- nothing was ever appended to the arena log.
        self.assertEqual(pets.fight_log_rows(entry), [])

    def test_mob_gold_still_rolls_the_undoubled_base_after_the_duel_purse_doubled(self):
        """The arena purse was doubled into its own constants. A mob reads the shared base
        pair, and must keep reading it -- otherwise doubling duels doubled PVE by accident."""
        entry = "chat"
        self._tame(entry, "1")
        now = datetime(2026, 8, 9, 9, 0)
        rolled = []

        def echo(low, high):
            rolled.append((low, high))
            return high

        block = pets.mob_block(entry, "1", pets_mobs.MOBS[0].code, "medium",
                               rng=random.Random(1))
        with patch("random.randint", echo):
            pets.record_mob_fight(
                entry, "1", block, SimpleNamespace(winner="1", is_draw=False), now=now,
            )
        self.assertIn((pets_config.WIN_GOLD_MIN, pets_config.WIN_GOLD_MAX), rolled)
        self.assertNotIn(
            (pets_config.ARENA_WIN_GOLD_MIN, pets_config.ARENA_WIN_GOLD_MAX), rolled,
        )

    def test_mob_gold_matches_half_the_arena_purse_times_tier_and_the_mobs_own_purse(self):
        """Every mob's gold multiplier is checked against the formula, not a table of
        expected numbers, so this still passes if the roster or its prices change."""
        entry = "chat"
        self._tame(entry, "1")
        now = datetime(2026, 8, 9, 9, 0)

        with patch("random.randint", return_value=pets_config.WIN_GOLD_MAX):
            for mob in pets_mobs.MOBS:
                data = pets._load(entry)
                hero_level = data["pets"]["1"]["level"]
                # Refill the bank to exactly one fight before each mob, independent of
                # cage/capacity bookkeeping -- this test is only about the payout formula.
                data["pets"]["1"]["fight_bank"] = 1
                data["pets"]["1"]["fight_bank_checkpoint"] = now.isoformat()
                pets._save(entry, data)

                block = pets.mob_block(entry, "1", mob.code, "medium", rng=random.Random(1))
                outcome = pets.record_mob_fight(
                    entry, "1", block, SimpleNamespace(winner="1", is_draw=False), now=now,
                )
                expected_base = max(1, round(
                    pets_config.WIN_GOLD_MAX * pets_config.PVE_GOLD_SHARE
                    * pets_mobs.TIER_REWARD["medium"] * mob.gold
                ))
                expected = pets_config.gold_for_hero(expected_base, hero_level, "pve")
                self.assertEqual(outcome["gold_base"], expected_base, mob.code)
                self.assertEqual(outcome["gold"], expected, mob.code)


class RubyWalletTests(PetsTestCase):
    def test_rubies_accumulate_never_go_negative_and_are_scoped_per_user_and_per_chat(self):
        entry = "chat-a"
        self.assertEqual(pets.ruby_balance(entry, "1"), 0)
        self.assertEqual(pets.grant_rubies(entry, "1", 2), 2)
        self.assertEqual(pets.grant_rubies(entry, "1", 3), 5)
        self.assertEqual(pets.ruby_balance(entry, "1"), 5)

        # A non-positive grant changes nothing and never drives the balance negative.
        self.assertEqual(pets.grant_rubies(entry, "1", -100), 5)
        self.assertEqual(pets.ruby_balance(entry, "1"), 5)

        # A different user in the same chat has their own, untouched wallet.
        self.assertEqual(pets.ruby_balance(entry, "2"), 0)

        # The same user id in a different chat is a completely different wallet.
        self.assertEqual(pets.ruby_balance("chat-b", "1"), 0)
        pets.grant_rubies("chat-b", "1", 1)
        self.assertEqual(pets.ruby_balance(entry, "1"), 5)
        self.assertEqual(pets.ruby_balance("chat-b", "1"), 1)

    def test_a_mob_win_can_drop_rubies_and_a_loss_never_does(self):
        entry = "chat"
        self._tame(entry, "1")
        now = datetime(2026, 8, 9, 9, 0)
        block = pets.mob_block(entry, "1", pets_mobs.MOBS[0].code, "hard", rng=random.Random(1))

        with patch("random.random", return_value=0.0):
            win = pets.record_mob_fight(
                entry, "1", block, SimpleNamespace(winner="1", is_draw=False), now=now,
            )
        self.assertGreaterEqual(win["rubies"], pets_config.PVE_RUBY_MIN)
        self.assertLessEqual(win["rubies"], pets_config.PVE_RUBY_MAX)
        self.assertEqual(pets.ruby_balance(entry, "1"), win["rubies"])

        with patch("random.random", return_value=0.0):
            loss = pets.record_mob_fight(
                entry, "1", block,
                SimpleNamespace(winner=f"mob:{block['code']}", is_draw=False), now=now,
            )
        self.assertEqual(loss["rubies"], 0)
        # Unchanged by the loss, even though the same forced-low roll would have hit.
        self.assertEqual(pets.ruby_balance(entry, "1"), win["rubies"])

    def test_an_easy_win_never_pays_a_ruby_but_medium_and_hard_can(self):
        """Rubies track risk: an easy mob is no risk at all, so TIER_RUBY_CHANCE zeroes it
        out entirely rather than just making it rare -- a forced-guaranteed roll must still
        come back empty-handed."""
        entry = "chat"
        self._tame(entry, "1")
        now = datetime(2026, 8, 9, 9, 0)
        self.assertEqual(pets_mobs.TIER_RUBY_CHANCE["easy"], 0.0)

        easy_block = pets.mob_block(entry, "1", pets_mobs.MOBS[0].code, "easy", rng=random.Random(1))
        with patch("random.random", return_value=0.0):
            easy_win = pets.record_mob_fight(
                entry, "1", easy_block, SimpleNamespace(winner="1", is_draw=False), now=now,
            )
        self.assertEqual(easy_win["rubies"], 0)

        for tier in ("medium", "hard"):
            block = pets.mob_block(entry, "1", pets_mobs.MOBS[0].code, tier, rng=random.Random(1))
            with patch("random.random", return_value=0.0):
                win = pets.record_mob_fight(
                    entry, "1", block, SimpleNamespace(winner="1", is_draw=False), now=now,
                )
            self.assertGreaterEqual(win["rubies"], pets_config.PVE_RUBY_MIN, tier)

    def test_a_farm_shift_can_drop_a_ruby_seeded_on_the_run_id_exactly_once(self):
        """Seeded on the run id (like the rest of the payout) rather than rolled fresh,
        so a settlement that runs twice for the same finished shift cannot mint rubies
        twice -- see settle_completed_farms's own comment on why gold uses the same
        trick with economy.grant_once."""
        entry, start = "farm-ruby", datetime(2026, 8, 9, 9, 0)
        self._tame(entry, "1")
        economy.grant(entry, "1", pets_config.FARM_UPGRADE_COSTS[0], "test")
        self.assertTrue(pets.upgrade_farm(entry, "1", 0, now=start)[0])
        self.assertTrue(pets.start_farm(entry, "1", 6, now=start)[0])

        data = pets._load(entry)
        # This exact id was found offline to draw below FARM_RUBY_CHANCE, so the shift
        # is guaranteed (not merely likely) to pay a ruby.
        data["pets"]["1"]["farm_run"]["run_id"] = "run11"
        pets._save(entry, data)

        finish = start + timedelta(hours=6)
        receipts = pets.settle_completed_farms(entry, now=finish)
        self.assertEqual(len(receipts), 1)
        self.assertGreater(receipts[0]["rubies"], 0)
        self.assertEqual(pets.ruby_balance(entry, "1"), receipts[0]["rubies"])

        # The run is already cleared, so settling again is a no-op: nothing further mints.
        again = pets.settle_completed_farms(entry, now=finish + timedelta(hours=1))
        self.assertEqual(again, [])
        self.assertEqual(pets.ruby_balance(entry, "1"), receipts[0]["rubies"])


class MirrorSoulCombatTests(unittest.TestCase):
    """pets_combat._mirror is pure, so it is tested the same way the rest of
    pets_combat is tested (see test_pets_combat.py): no storage, just Fighters."""

    def _wearer(self, key, name, **stat_kwargs):
        return pets_combat.Fighter(
            key=key, name=name, effects=({"code": "mirror_soul", "value": 20},), **stat_kwargs,
        )

    def test_mirror_pulls_a_stronger_wearer_down_to_the_opponents_own_numbers(self):
        """Zero jitter isolates "come down to their numbers" from "then shake them",
        which is checked separately below."""
        weak = pets_combat.Fighter(
            key="weak", name="Weak", strength=10, health=12, agility=8, luck=6, armor=3,
        )
        strong = self._wearer(
            "strong", "Strong", strength=80, health=70, agility=60, luck=50, armor=40,
        )
        mirrored = pets_combat._mirror(strong, weak, _NoJitter())
        for stat in ("strength", "health", "agility", "luck"):
            self.assertEqual(getattr(mirrored, stat), getattr(weak, stat))
        self.assertEqual(mirrored.armor, min(strong.armor, weak.armor))

    def test_mirror_jitter_stays_inside_plus_minus_twenty_percent_of_the_opponent(self):
        weak = pets_combat.Fighter(
            key="weak", name="Weak", strength=10, health=10, agility=10, luck=10, armor=0,
        )
        strong = self._wearer(
            "strong", "Strong", strength=80, health=80, agility=80, luck=80, armor=0,
        )
        lo, hi = max(1, round(10 * 0.8)), round(10 * 1.2)
        for seed in range(50):
            mirrored = pets_combat._mirror(strong, weak, random.Random(seed))
            for stat in ("strength", "health", "agility", "luck"):
                value = getattr(mirrored, stat)
                self.assertGreaterEqual(value, lo)
                self.assertLessEqual(value, hi)

    def test_mirror_never_upgrades_a_wearer_who_is_already_the_weaker_side(self):
        """It only ever comes down. A fighter below the opponent on every stat (and
        armour) must be handed back completely unchanged -- if this ever became an
        upgrade instead of a no-op, wearing the amulet by mistake would be free power."""
        strong_opponent = pets_combat.Fighter(
            key="opp", name="Opp", strength=80, health=80, agility=80, luck=80, armor=40,
        )
        weak_wearer = self._wearer(
            "weak", "Weak", strength=10, health=10, agility=10, luck=10, armor=3,
        )
        unaffected = pets_combat._mirror(weak_wearer, strong_opponent, random.Random(1))
        self.assertEqual(unaffected, weak_wearer)

    def test_a_mirrored_fight_still_replays_identically_from_the_same_seed(self):
        """The jitter is drawn from the fight's OWN rng inside simulate(), not a side
        channel -- if it were not, a stored seed would stop reproducing the fight that
        was actually shown to the players the moment either side wore the mirror."""
        weak = pets_combat.Fighter(
            key="weak", name="Weak", strength=8, health=8, agility=8, luck=8, armor=0,
        )
        strong = self._wearer(
            "strong", "Strong", strength=70, health=70, agility=70, luck=70, armor=20,
        )
        first = pets_combat.simulate(strong, weak, seed=2026)
        second = pets_combat.simulate(strong, weak, seed=2026)
        self.assertEqual(first, second)
        self.assertGreater(len(first.rounds), 0)


class MirrorSoulAutoEquipAndRewardTests(PetsTestCase):
    def test_auto_equip_fires_only_for_the_level_gap_and_ownership_then_restore_undoes_it(self):
        entry = "chat"
        self._tame(entry, "attacker", "Attacker")
        self._tame(entry, "defender", "Defender")
        data = pets._load(entry)
        data["pets"]["attacker"]["level"] = 6
        data["pets"]["defender"]["level"] = 1  # gap of 5: exactly MIRROR_LEVEL_GAP
        # A different amulet already worn, to prove restore_after_mirror puts it back.
        data["pets"]["attacker"]["inventory"] = ["bead"]
        data["pets"]["attacker"]["equipped"]["amulet"] = "bead"
        pets._save(entry, data)

        # The gap qualifies, but nothing is owned yet -- nothing is swapped.
        self.assertIsNone(pets.auto_equip_mirror(entry, "attacker", "defender"))
        self.assertEqual(pets.get_pet(entry, "attacker")["equipped"]["amulet"], "bead")

        data = pets._load(entry)
        data["pets"]["attacker"]["inventory"].append(pets.MIRROR_AMULET_CODE)
        pets._save(entry, data)

        self.assertEqual(
            pets.auto_equip_mirror(entry, "attacker", "defender"), pets.MIRROR_AMULET_CODE,
        )
        self.assertEqual(
            pets.get_pet(entry, "attacker")["equipped"]["amulet"], pets.MIRROR_AMULET_CODE,
        )

        self.assertTrue(pets.restore_after_mirror(entry, "attacker"))
        self.assertEqual(pets.get_pet(entry, "attacker")["equipped"]["amulet"], "bead")

        # One level short of the gap: owning the amulet is no longer enough.
        data = pets._load(entry)
        data["pets"]["defender"]["level"] = 2  # gap of 4
        pets._save(entry, data)
        self.assertIsNone(pets.auto_equip_mirror(entry, "attacker", "defender"))
        self.assertEqual(pets.get_pet(entry, "attacker")["equipped"]["amulet"], "bead")

    def test_vaulting_the_mirror_strips_every_copy_and_pays_for_it(self):
        """Withdrawn from the game, but nobody may end up worse off for it."""
        entry = "chat"
        self._tame(entry, "owner", "Owner")
        data = pets._load(entry)
        record = data["pets"]["owner"]
        record["inventory"] = ["bead", pets.MIRROR_AMULET_CODE]
        record["equipped"]["amulet"] = pets.MIRROR_AMULET_CODE
        record["mirror_restore"] = "bead"          # a swap caught mid-flight
        pets._save(entry, data)
        before = economy.balance(entry, "owner", 0)
        price = pets_config.find_item(pets.MIRROR_AMULET_CODE).price

        result = pets.retire_soul_mirror([entry])

        after = pets.get_pet(entry, "owner")
        self.assertNotIn(pets.MIRROR_AMULET_CODE, after["inventory"])
        self.assertEqual(after["equipped"]["amulet"], "bead", "the displaced amulet returns")
        self.assertNotIn("mirror_restore", after)
        self.assertEqual(economy.balance(entry, "owner", 0), before + price)
        self.assertEqual(result["gold"], price)

        # Idempotent: a restart re-runs this on every boot and must not pay twice.
        again = pets.retire_soul_mirror([entry])
        self.assertEqual(again["gold"], 0)
        self.assertEqual(economy.balance(entry, "owner", 0), before + price)

    def test_vaulting_returns_a_personal_paint_rune_bound_to_the_mirror(self):
        """That rune is somebody's own painted miniature; it must not go down with it."""
        entry = "chat"
        self._tame(entry, "owner", "Owner")
        data = pets._load(entry)
        record = data["pets"]["owner"]
        record["inventory"] = [pets.MIRROR_AMULET_CODE]
        record["personal_enchantments"] = {
            pets.MIRROR_AMULET_CODE: {
                "target": "amulet", "rune_id": "paint-abc", "quest_code": "rune_paint_amulet",
                "photo_file_id": "photo-1", "applied_at": "2026-08-01T00:00:00+03:00",
            },
        }
        pets._save(entry, data)

        result = pets.retire_soul_mirror([entry])

        self.assertEqual(result["runes"], 1)
        after = pets.get_pet(entry, "owner")
        self.assertNotIn(pets.MIRROR_AMULET_CODE, after.get("personal_enchantments", {}))
        wallet = pets._personal_paint_rune_wallet(pets._load(entry), "owner")
        returned = next(row for row in wallet if row["id"] == "paint-abc")
        self.assertEqual(returned["photo_file_id"], "photo-1")
        self.assertEqual(returned["target"], "amulet")

    def test_the_vaulted_mirror_still_resolves_so_old_fights_keep_rendering(self):
        """It leaves the shelves, not the catalogue: stored snapshots name its code."""
        item = pets_config.find_item(pets.MIRROR_AMULET_CODE)
        self.assertIsNotNone(item, "an unresolvable code turns an old replay into blanks")
        self.assertEqual(item.source, "vault")
        self.assertEqual(item.drop_weight, 0)
        # And it is on no shelf and in no loot pool, because both filter on an exact source.
        self.assertNotIn(item, [i for i in pets_config.ITEMS if i.source in ("shop", "drop")])

    def test_a_swap_stranded_by_a_crashed_fight_is_handed_back_on_the_next_one(self):
        """A fight that died between the swap and the swap back used to strand the amulet.

        The stranded state is self-sustaining: auto_equip_mirror returns early when the
        mirror is already worn, so no later fight would reach the restore either. The
        player's own amulet sat in `mirror_restore` for good.
        """
        entry = "chat"
        self._tame(entry, "attacker", "Attacker")
        self._tame(entry, "defender", "Defender")
        data = pets._load(entry)
        data["pets"]["attacker"]["level"] = 6
        data["pets"]["defender"]["level"] = 1
        data["pets"]["attacker"]["inventory"] = ["bead", pets.MIRROR_AMULET_CODE]
        # Exactly what a crashed fight leaves behind: mirror worn, real amulet in limbo.
        data["pets"]["attacker"]["equipped"]["amulet"] = pets.MIRROR_AMULET_CODE
        data["pets"]["attacker"]["mirror_restore"] = "bead"
        pets._save(entry, data)

        # The next fight recognises the strand and reports the swap, so its caller
        # restores at the end instead of walking past it again.
        self.assertEqual(
            pets.auto_equip_mirror(entry, "attacker", "defender"), pets.MIRROR_AMULET_CODE,
        )
        self.assertTrue(pets.restore_after_mirror(entry, "attacker"))

        attacker = pets.get_pet(entry, "attacker")
        self.assertEqual(attacker["equipped"]["amulet"], "bead")
        self.assertNotIn("mirror_restore", attacker)

    def test_a_stranded_slip_for_an_amulet_no_longer_owned_is_simply_dropped(self):
        """Sold or reforged since: there is nothing to give back, so clear the slip."""
        entry = "chat"
        self._tame(entry, "attacker", "Attacker")
        self._tame(entry, "defender", "Defender")
        data = pets._load(entry)
        data["pets"]["attacker"]["level"] = 6
        data["pets"]["defender"]["level"] = 1
        data["pets"]["attacker"]["inventory"] = [pets.MIRROR_AMULET_CODE]
        data["pets"]["attacker"]["equipped"]["amulet"] = pets.MIRROR_AMULET_CODE
        data["pets"]["attacker"]["mirror_restore"] = "bead"
        pets._save(entry, data)

        self.assertIsNone(pets.auto_equip_mirror(entry, "attacker", "defender"))
        self.assertNotIn("mirror_restore", pets.get_pet(entry, "attacker"))

    def test_winning_with_the_mirror_equipped_is_not_docked_by_the_level_multiplier(self):
        """«Награда за победу при этом не режется» -- the reward multiplier for a
        lopsided win is clamped to 1.0 while wearing the mirror instead of being
        replaced by whatever ARENA_LEVEL_REWARD_MULTIPLIERS would otherwise apply,
        because the wearer already paid for the fair fight by giving up their stats."""
        entry = "chat"
        for uid in ("1", "2", "3", "4"):
            self._tame(entry, uid, f"Pet{uid}")
        data = pets._load(entry)
        for winner_uid, loser_uid in (("1", "2"), ("3", "4")):
            data["pets"][winner_uid]["level"] = 10
            data["pets"][loser_uid]["level"] = 1
        data["pets"]["3"]["inventory"].append(pets.MIRROR_AMULET_CODE)
        data["pets"]["3"]["equipped"]["amulet"] = pets.MIRROR_AMULET_CODE
        pets._save(entry, data)

        multiplier = pets_config.arena_level_reward_multiplier(10, 1)
        self.assertLess(multiplier, 1.0)  # confirms this matchup would normally be docked
        pinned_roll = pets_config.WIN_GOLD_MAX
        now = datetime(2026, 8, 9, 9, 0)

        with patch("random.randint", return_value=pinned_roll):
            without_mirror = pets.record_fight(
                entry, "1", "2", SimpleNamespace(winner="1", loser="2", is_draw=False),
                now.date(), now=now,
            )
            with_mirror = pets.record_fight(
                entry, "3", "4", SimpleNamespace(winner="3", loser="4", is_draw=False),
                now.date(), now=now,
            )

        self.assertEqual(
            without_mirror["gold"],
            pets_config.gold_for_hero(round(pinned_roll * multiplier), 10, "arena"),
        )
        # Clamped to 1.0, not replaced by it -- the mirror removes the penalty, it does
        # not also hand out the bonus a genuine upward win would earn.
        self.assertEqual(
            with_mirror["gold"], pets_config.gold_for_hero(pinned_roll, 10, "arena"),
        )
        self.assertGreater(with_mirror["gold"], without_mirror["gold"])


if __name__ == "__main__":
    unittest.main()
