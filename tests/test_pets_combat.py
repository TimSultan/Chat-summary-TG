"""pets_combat.py is a pure simulation, so its tests are the balance proof: they check
that the numbers in pets_config.py keep an even fight near an 85% knockout rate within
the ten-attack cap, and that a stat lead reliably wins, not just that the code runs. See
PETS_CONTRACT.md for the exact list this file is required to cover.
"""

import random
import statistics
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pets_config as C
import pets_combat as combat
from pets_combat import Fighter


def _fighter(key, level, armor=0, name=None):
    """A bare pet with every stat at the same level -- the shape a fresh account has
    before choosing where to specialise, and the shape pets_config's own balance comment
    ("a level-1 pair", "a maxed pair") is talking about."""
    return Fighter(
        key=key, name=name or key,
        strength=level, health=level, agility=level, luck=level, armor=armor,
    )


class DeriveTests(unittest.TestCase):
    def test_symmetric_fighters_get_no_dominance_bonus(self):
        a, b = _fighter("a", 40), _fighter("b", 40)
        derived = combat.derive(a, b)
        self.assertTrue(all(v is False for v in derived["dominance"].values()))
        self.assertEqual(derived["max_hp"], C.BASE_HP + 40 * C.HP_PER_POINT)
        self.assertEqual(derived["damage"], C.BASE_DAMAGE + 40 * C.DAMAGE_PER_POINT)

    def test_dominance_bonus_applies_at_exactly_the_ratio_and_not_one_point_short(self):
        # theirs = 100 -> dominant threshold is exactly 130 (100 * DOMINANCE_RATIO).
        theirs = _fighter("theirs", 100)
        at_threshold = Fighter(key="mine", name="mine", strength=130, health=100,
                                agility=100, luck=100, armor=0)
        below_threshold = Fighter(key="mine", name="mine", strength=129, health=100,
                                   agility=100, luck=100, armor=0)

        derived_at = combat.derive(at_threshold, theirs)
        derived_below = combat.derive(below_threshold, theirs)

        self.assertTrue(derived_at["dominance"]["strength"])
        self.assertFalse(derived_below["dominance"]["strength"])

        expected_bonus_damage = C.BASE_DAMAGE + 130 * C.DAMAGE_PER_POINT * (1 + C.DOMINANCE_BONUS)
        expected_plain_damage = C.BASE_DAMAGE + 129 * C.DAMAGE_PER_POINT
        self.assertAlmostEqual(derived_at["damage"], expected_bonus_damage)
        self.assertAlmostEqual(derived_below["damage"], expected_plain_damage)

    def test_base_floors_are_not_multiplied_by_the_dominance_factor(self):
        # A fighter with 0 in the dominant stat still gets BASE_HP/BASE_DAMAGE untouched.
        theirs = _fighter("theirs", 1)
        dominant_zero_health = Fighter(key="mine", name="mine", strength=1, health=0,
                                        agility=1, luck=1, armor=0)
        derived = combat.derive(dominant_zero_health, theirs)
        self.assertEqual(derived["max_hp"], C.BASE_HP)

    def test_armor_caps_at_armor_max_and_is_never_dominance_boosted(self):
        huge_armor = Fighter(key="a", name="a", strength=1, health=1, agility=1, luck=1,
                              armor=1_000_000)
        weak = _fighter("b", 1)
        derived = combat.derive(huge_armor, weak)
        self.assertLess(derived["reduction"], C.ARMOR_MAX)
        self.assertGreater(derived["reduction"], C.ARMOR_MAX * 0.99)

        for armor in (0, 10, 60, 150, 5_000):
            self.assertLessEqual(combat.derive(
                Fighter(key="x", name="x", strength=1, health=1, agility=1, luck=1, armor=armor),
                weak,
            )["reduction"], C.ARMOR_MAX)

    def test_dodge_and_crit_never_exceed_their_config_ceiling(self):
        opponent = _fighter("o", 1)
        for stat in (0, 40, 80, 10_000):
            f = Fighter(key="f", name="f", strength=1, health=1, agility=stat, luck=stat, armor=0)
            derived = combat.derive(f, opponent)
            self.assertLessEqual(derived["dodge"], C.DODGE_MAX)
            self.assertLessEqual(derived["crit"], C.CRIT_BASE + C.CRIT_MAX)


class SimulateTests(unittest.TestCase):
    def test_same_seed_replays_the_identical_fight(self):
        a, b = _fighter("a", 40, name="Alpha"), _fighter("b", 40, name="Beta")
        result_1 = combat.simulate(a, b, seed=12345)
        result_2 = combat.simulate(a, b, seed=12345)
        self.assertEqual(result_1, result_2)
        self.assertEqual(result_1.seed, 12345)

    def test_fights_allow_no_more_than_ten_attacks_per_fighter(self):
        # A round is a full exchange (leader strikes, follower counters back in the same
        # round unless already dead). The cap applies to each pet, not merely to total
        # blows, so a future change cannot let the opening attacker get an extra strike.
        a, b = _fighter("a", 40), _fighter("b", 40)
        for seed in range(200):
            result = combat.simulate(a, b, rng=random.Random(seed))
            attacks = {key: sum(rnd.attacker == key for rnd in result.rounds) for key in (a.key, b.key)}
            self.assertLessEqual(result.rounds[-1].number, C.MAX_ATTACKS_PER_FIGHTER)
            self.assertLessEqual(attacks[a.key], C.MAX_ATTACKS_PER_FIGHTER)
            self.assertLessEqual(attacks[b.key], C.MAX_ATTACKS_PER_FIGHTER)
            self.assertGreater(len(result.rounds), 0)

    def test_hp_is_never_negative_in_any_round(self):
        a, b = _fighter("a", 80), _fighter("b", 1)
        for seed in range(200):
            result = combat.simulate(a, b, rng=random.Random(seed))
            for rnd in result.rounds:
                self.assertGreaterEqual(rnd.attacker_hp, 0)
                self.assertGreaterEqual(rnd.defender_hp, 0)

    def test_winner_and_loser_are_always_one_of_the_two_fighters(self):
        a, b = _fighter("a", 40), _fighter("b", 40)
        for seed in range(50):
            result = combat.simulate(a, b, rng=random.Random(seed))
            if result.is_draw:
                self.assertIsNone(result.winner)
                self.assertIsNone(result.loser)
            else:
                self.assertIn(result.winner, (a.key, b.key))
                self.assertIn(result.loser, (a.key, b.key))
                self.assertNotEqual(result.winner, result.loser)

    def test_even_fights_knock_out_about_eighty_five_percent_of_the_time(self):
        rates = {}
        for level in (1, 40, 80):
            a, b = _fighter("a", level, name="A"), _fighter("b", level, name="B")
            rate = sum(
                not combat.simulate(a, b, seed=seed).stopped_early
                for seed in range(1_000)
            ) / 1_000
            rates[level] = rate
            self.assertGreaterEqual(rate, 0.80, f"level {level}: knockout rate {rate}")
            self.assertLessEqual(rate, 0.92, f"level {level}: knockout rate {rate}")
        self.assertAlmostEqual(statistics.mean(rates.values()), 0.85, delta=0.04)

    def test_a_vastly_stronger_fighter_wins_at_least_90_percent_of_the_time(self):
        strong = Fighter(key="strong", name="Strong", strength=80, health=80,
                          agility=80, luck=80, armor=150)
        weak = Fighter(key="weak", name="Weak", strength=1, health=1,
                        agility=1, luck=1, armor=0)
        wins = sum(
            1 for seed in range(200)
            if combat.simulate(strong, weak, rng=random.Random(seed)).winner == "strong"
        )
        print(f"\n  measured win rate (strong vs weak): {wins / 200:.3f}")
        self.assertGreaterEqual(wins / 200, 0.90, f"win rate {wins / 200}")

    def test_capped_fight_awards_the_living_pet_with_more_damage(self):
        a = Fighter(key="a", name="A", strength=10, health=100, agility=1, luck=1, armor=0)
        b = Fighter(key="b", name="B", strength=1, health=100, agility=1, luck=1, armor=0)

        def fixed_blow(attacker, defender, rng):
            return "hit", 20 if attacker["damage"] > defender["damage"] else 10

        with patch.object(combat, "_resolve_blow", side_effect=fixed_blow):
            result = combat.simulate(a, b, rng=random.Random(1))

        self.assertTrue(result.stopped_early)
        self.assertEqual(len(result.rounds), 2 * C.MAX_ATTACKS_PER_FIGHTER)
        self.assertEqual(result.total_damage, {"a": 200, "b": 100})
        self.assertEqual(result.winner, "a")

    def test_a_knockout_wins_even_when_the_loser_dealt_more_damage(self):
        a = Fighter(key="a", name="A", strength=10, health=1, agility=1, luck=1, armor=0)
        b = Fighter(key="b", name="B", strength=1, health=100, agility=1, luck=1, armor=0)

        def fixed_blow(attacker, defender, rng):
            return "hit", 700 if attacker["damage"] > defender["damage"] else 600

        with patch.object(combat, "_resolve_blow", side_effect=fixed_blow):
            result = combat.simulate(a, b, rng=random.Random(1))

        self.assertFalse(result.stopped_early)
        self.assertEqual(result.total_damage, {"a": 700, "b": 600})
        self.assertEqual(result.winner, "b")

    def test_equal_damage_at_the_cap_is_a_draw(self):
        a = Fighter(key="a", name="A", strength=10, health=100, agility=1, luck=1, armor=0)
        b = Fighter(key="b", name="B", strength=1, health=100, agility=1, luck=1, armor=0)

        with patch.object(combat, "_resolve_blow", return_value=("hit", 20)):
            result = combat.simulate(a, b, seed=1)

        self.assertTrue(result.stopped_early)
        self.assertTrue(result.is_draw)
        self.assertIsNone(result.winner)
        self.assertIsNone(result.loser)
        self.assertEqual(result.total_damage, {"a": 200, "b": 200})


if __name__ == "__main__":
    unittest.main()
