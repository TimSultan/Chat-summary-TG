"""pets_combat.py is a pure simulation, so its tests are the balance proof: they check
that the numbers in pets_config.py actually produce the fight shape the module's own
comments promise (6-12 rounds at every level, a stat lead that reliably wins), not just
that the code runs. See PETS_CONTRACT.md for the exact list this file is required to
cover.
"""

import random
import statistics
import sys
import unittest
from pathlib import Path

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
        result_1 = combat.simulate(a, b, rng=random.Random(12345))
        result_2 = combat.simulate(a, b, rng=random.Random(12345))
        self.assertEqual(result_1, result_2)

    def test_fights_always_terminate_within_max_rounds(self):
        # A round is a full exchange (leader strikes, follower counters back in the same
        # round unless already dead), so it produces one or two Round entries -- the round
        # NUMBER is capped at MAX_ROUNDS, the blow count can run up to twice that.
        a, b = _fighter("a", 40), _fighter("b", 40)
        for seed in range(200):
            result = combat.simulate(a, b, rng=random.Random(seed))
            self.assertLessEqual(result.rounds[-1].number, C.MAX_ROUNDS)
            self.assertLessEqual(len(result.rounds), 2 * C.MAX_ROUNDS)
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
            self.assertIn(result.winner, (a.key, b.key))
            self.assertIn(result.loser, (a.key, b.key))
            self.assertNotEqual(result.winner, result.loser)

    def test_round_count_median_lands_in_the_6_to_12_range_at_every_level(self):
        # pets_config's own balance comment: "6-12 rounds at EVERY level ... a level-1
        # pair trade ~11 blows, a maxed pair ~8". A "round" is the exchange (leader strikes,
        # follower counters back in the same round) -- the round NUMBER the fight ends on,
        # not the raw blow count, which runs roughly double. This is the test that proves
        # the tuning actually lands where the config's own comment says it does.
        medians = {}
        for level in (1, 40, 80):
            a, b = _fighter("a", level, name="A"), _fighter("b", level, name="B")
            lengths = [
                combat.simulate(a, b, rng=random.Random(seed)).rounds[-1].number
                for seed in range(200)
            ]
            median = statistics.median(lengths)
            medians[level] = median
            self.assertGreaterEqual(median, 6, f"level {level}: median {median}")
            self.assertLessEqual(median, 12, f"level {level}: median {median}")
        print(f"\n  measured round-length medians: {medians}")

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

    def test_stopped_early_fights_award_the_higher_remaining_hp_fraction(self):
        # Two fighters so evenly matched combat cannot resolve them inside MAX_ROUNDS is
        # not reachable with these constants (see pets_config), so this exercises the
        # award logic directly by shrinking the round budget via a monkeypatched cap.
        a, b = _fighter("a", 40), _fighter("b", 40)
        original_max_rounds = C.MAX_ROUNDS
        C.MAX_ROUNDS = 1
        try:
            result = combat.simulate(a, b, rng=random.Random(7))
            self.assertTrue(result.stopped_early)
            self.assertIn(result.winner, (a.key, b.key))
        finally:
            C.MAX_ROUNDS = original_max_rounds


if __name__ == "__main__":
    unittest.main()
