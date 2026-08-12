"""pets_combat.py is a pure simulation, so its tests are the balance proof: they check
that the numbers in pets_config.py keep an even fight near an 85% knockout rate within
the ten-attack cap, and that a stat lead reliably wins, not just that the code runs. See
PETS_CONTRACT.md for the exact list this file is required to cover.
"""

import json
import random
import statistics
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pets_config as C
import pets_combat as combat
from pets_amulet_catalog import AMULET_SPECS
from pets_weapon_catalog import WEAPON_SPECS
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

    def test_stat_lead_bonus_scales_until_its_thirty_percent_cap(self):
        # Theirs = 100: a 10-point lead gives 10%, and the cap starts at 130.
        theirs = _fighter("theirs", 100)
        small_lead = Fighter(key="mine", name="mine", strength=110, health=100,
                             agility=100, luck=100, armor=0)
        at_cap = Fighter(key="mine", name="mine", strength=130, health=100,
                         agility=100, luck=100, armor=0)
        above_cap = Fighter(key="mine", name="mine", strength=200, health=100,
                            agility=100, luck=100, armor=0)

        derived_small = combat.derive(small_lead, theirs)
        derived_at_cap = combat.derive(at_cap, theirs)
        derived_above_cap = combat.derive(above_cap, theirs)

        self.assertAlmostEqual(derived_small["stat_bonus"]["strength"], 0.10)
        self.assertAlmostEqual(derived_at_cap["stat_bonus"]["strength"], C.DOMINANCE_BONUS)
        self.assertAlmostEqual(derived_above_cap["stat_bonus"]["strength"], C.DOMINANCE_BONUS)

        expected_small_damage = C.BASE_DAMAGE + 110 * C.DAMAGE_PER_POINT * 1.10
        expected_cap_damage = C.BASE_DAMAGE + 130 * C.DAMAGE_PER_POINT * (1 + C.DOMINANCE_BONUS)
        self.assertAlmostEqual(derived_small["damage"], expected_small_damage)
        self.assertAlmostEqual(derived_at_cap["damage"], expected_cap_damage)

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
        opponent = _fighter("o", 10_000)
        for stat in (0, 40, 80, 10_000):
            f = Fighter(key="f", name="f", strength=1, health=1, agility=stat, luck=stat, armor=0)
            derived = combat.derive(f, opponent)
            self.assertLessEqual(derived["dodge"], C.DODGE_MAX)
            self.assertLessEqual(derived["crit"], C.CRIT_BASE + C.CRIT_MAX)

    def test_luck_advantage_and_overwhelming_tiers_override_normal_rates(self):
        weak = _fighter("weak", 10)
        doubled = Fighter(key="doubled", name="doubled", strength=10, health=10,
                          agility=10, luck=20, armor=0)
        tripled = Fighter(key="tripled", name="tripled", strength=10, health=10,
                          agility=10, luck=30, armor=0)

        doubled_rates = combat.derive(doubled, weak)
        tripled_rates = combat.derive(tripled, weak)

        self.assertEqual(doubled_rates["signature"], ("luck", 2))
        self.assertEqual(doubled_rates["luck_tier"], 2)
        self.assertAlmostEqual(
            doubled_rates["crit"],
            C.CRIT_BASE + C.LUCK_ADVANTAGE_CRIT_BONUS
            + C.CRIT_MAX * (20 * (1 + C.DOMINANCE_BONUS))
            / (20 * (1 + C.DOMINANCE_BONUS) + C.CRIT_K),
        )
        self.assertEqual(doubled_rates["accuracy"], C.LUCK_ADVANTAGE_MISS_MULTIPLIER)
        self.assertEqual(tripled_rates["luck_tier"], 3)
        self.assertEqual(tripled_rates["crit"], C.LUCK_OVERWHELMING_CRIT_CHANCE)
        self.assertEqual(tripled_rates["dodge"], C.LUCK_OVERWHELMING_DODGE_CHANCE)

    def test_strongest_two_x_stat_is_the_only_signature(self):
        strong = Fighter(key="strong", name="Strong", strength=30, health=20,
                         agility=10, luck=10, armor=0)
        weak = _fighter("weak", 10)
        self.assertEqual(combat.derive(strong, weak)["signature"], ("strength", 3))


class SnapshotTests(unittest.TestCase):
    """A stored fight is a seed plus two snapshots, and nothing else. These prove the
    round trip is lossless, because a replay assembled from a lossy one would play out a
    fight that never happened."""

    def test_a_snapshot_survives_json_and_replays_the_identical_fight(self):
        a = Fighter(key="a", name="Альфа", strength=31, health=27, agility=14, luck=9,
                    armor=4, effects=({"code": "vampiric", "value": 12}, "thorns"), level=7)
        b = _fighter("b", 22, armor=1, name="Бета")

        restored_a = combat.restore(json.loads(json.dumps(combat.snapshot(a))))
        restored_b = combat.restore(json.loads(json.dumps(combat.snapshot(b))))
        self.assertEqual(restored_a, a)
        self.assertEqual(restored_b, b)
        # The point of all of it: same snapshot, same seed, same fight.
        self.assertEqual(combat.simulate(restored_a, restored_b, seed=99),
                         combat.simulate(a, b, seed=99))

    def test_argument_order_is_part_of_the_replay(self):
        """simulate() spends the rng's first roll picking who moves first, so replaying
        with the fighters swapped is a different fight from the same seed. Anything
        rebuilding a stored fight has to put the attacker back in the first argument."""
        a, b = _fighter("a", 30, name="Альфа"), _fighter("b", 30, name="Бета")
        self.assertNotEqual(combat.simulate(a, b, seed=7).rounds,
                            combat.simulate(b, a, seed=7).rounds)

    def test_an_unusable_snapshot_is_refused_rather_than_repaired(self):
        for broken in (None, {}, {"name": "Без ключа"}, "not a mapping", []):
            with self.subTest(broken=broken):
                self.assertIsNone(combat.restore(broken))
        # A partial record still yields a usable fighter: the key is the only field whose
        # absence makes the record meaningless.
        salvaged = combat.restore({"key": "a", "name": "Альфа"})
        self.assertEqual(salvaged.key, "a")
        self.assertEqual(salvaged.effects, ())


class SimulateTests(unittest.TestCase):
    def test_same_seed_replays_the_identical_fight(self):
        a, b = _fighter("a", 40, name="Alpha"), _fighter("b", 40, name="Beta")
        result_1 = combat.simulate(a, b, seed=12345)
        result_2 = combat.simulate(a, b, seed=12345)
        self.assertEqual(result_1, result_2)
        self.assertEqual(result_1.seed, 12345)

    def test_luck_signature_is_a_survivable_opening_hit(self):
        lucky = Fighter(key="lucky", name="Lucky", strength=10, health=10,
                        agility=10, luck=20, armor=0)
        unlucky = _fighter("unlucky", 10, name="Unlucky")
        chances = dict(C.SIGNATURE_TRIGGER_CHANCES)
        chances["luck"] = (0.0, 0.0, 1.0, 1.0)
        with patch.object(C, "SIGNATURE_TRIGGER_CHANCES", chances):
            result = combat.simulate(lucky, unlucky, seed=123)

        self.assertIsNone(result.accident)
        self.assertEqual(result.rounds[0].event, "signature_luck")
        self.assertGreater(result.rounds[0].damage, 0)

    def test_agility_counter_knockout_awards_the_defender(self):
        attacker = Fighter(key="attacker", name="Attacker", strength=10, health=0,
                           agility=10, luck=10, armor=0)
        agile = Fighter(key="agile", name="Agile", strength=400, health=10,
                        agility=30, luck=10, armor=0)
        chances = dict(C.SIGNATURE_TRIGGER_CHANCES)
        chances["agility"] = (0.0, 0.0, 1.0, 1.0)

        def signatures(fighter, opponent):
            return ("agility", 3) if fighter.key == "agile" else None

        with patch.object(C, "SIGNATURE_TRIGGER_CHANCES", chances), \
                patch.object(combat, "_signature", side_effect=signatures):
            result = combat.simulate(attacker, agile, seed=1)

        self.assertEqual(result.winner, "agile")
        self.assertEqual(result.loser, "attacker")
        self.assertEqual(result.rounds[0].event, "signature_agility_counter")

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

        with patch.object(combat, "_resolve_blow", side_effect=fixed_blow), \
            patch.object(combat, "_signature", return_value=None):
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

        with patch.object(combat, "_resolve_blow", side_effect=fixed_blow), \
            patch.object(combat, "_signature", return_value=None):
            result = combat.simulate(a, b, rng=random.Random(1))

        self.assertFalse(result.stopped_early)
        self.assertEqual(result.total_damage, {"a": 700, "b": 600})
        self.assertEqual(result.winner, "b")

    def test_equal_damage_at_the_cap_is_a_draw(self):
        a = Fighter(key="a", name="A", strength=10, health=100, agility=1, luck=1, armor=0)
        b = Fighter(key="b", name="B", strength=1, health=100, agility=1, luck=1, armor=0)

        with patch.object(combat, "_resolve_blow", return_value=("hit", 20)), \
            patch.object(combat, "_signature", return_value=None):
            result = combat.simulate(a, b, seed=1)

        self.assertTrue(result.stopped_early)
        self.assertTrue(result.is_draw)
        self.assertIsNone(result.winner)
        self.assertIsNone(result.loser)
        self.assertEqual(result.total_damage, {"a": 200, "b": 200})


class AmuletEffectTests(unittest.TestCase):
    def _fighter_with(self, effect):
        return Fighter(
            key="a", name="Amulet", strength=40, health=40, agility=40, luck=40,
            armor=0, effects=(effect,), level=3,
        )

    def test_empty_effect_snapshot_is_exactly_the_legacy_fight(self):
        bare = _fighter("a", 40, name="A")
        explicit_empty = Fighter(
            key="a", name="A", strength=40, health=40, agility=40, luck=40,
            armor=0, effects=(), level=1,
        )
        opponent = _fighter("b", 40, name="B")
        self.assertEqual(
            combat.simulate(bare, opponent, seed=917),
            combat.simulate(explicit_empty, opponent, seed=917),
        )

    def test_economy_only_effects_do_not_change_combat_replay(self):
        bare = _fighter("a", 40, name="A")
        opponent = _fighter("b", 40, name="B")
        expected = combat.simulate(bare, opponent, seed=918)
        for code in ("collector", "trophy_compass", "coin_rake", "survivor"):
            with self.subTest(effect=code):
                equipped = Fighter(
                    key="a", name="A", strength=40, health=40, agility=40, luck=40,
                    armor=0, effects=({"code": code, "value": 25},), level=1,
                )
                self.assertEqual(expected, combat.simulate(equipped, opponent, seed=918))

    def test_a_weapon_and_amulet_sharing_a_code_keep_the_stronger_passive(self):
        """Weapons carry amulet-vocabulary passives, so both slots can collide.

        The hook lookups read the first match, so without deduplication the weaker of
        the two would win purely on equip order and the other would silently vanish.
        """
        fighter = Fighter(
            key="a", name="A", strength=40, health=40, agility=40, luck=40, armor=0,
            effects=(
                {"code": "vampiric", "text": "weapon", "value": 6},
                {"code": "vampiric", "text": "amulet", "value": 14},
            ),
            level=1,
        )
        specs = combat._effect_specs(fighter)
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["value"], 14)
        self.assertEqual(specs[0]["text"], "amulet")

    def test_all_catalogue_effects_are_seeded_and_safe_to_replay(self):
        """A malformed metadata deployment must not make one arena click non-replayable."""
        effect_specs = [
            *AMULET_SPECS,
            *(spec for spec in WEAPON_SPECS if spec.effect),
        ]
        self.assertEqual(
            {spec.effect_dict()["code"] for spec in effect_specs},
            set(combat._EFFECT_DEFAULTS),
        )
        opponent = Fighter(
            key="b", name="Opponent", strength=40, health=40, agility=40, luck=40,
            armor=0, level=8,
        )
        for spec in effect_specs:
            with self.subTest(effect=spec.effect_dict()["code"]):
                fighter = self._fighter_with(spec.effect_dict())
                first = combat.simulate(fighter, opponent, seed=41)
                self.assertEqual(first, combat.simulate(fighter, opponent, seed=41))
                self.assertGreater(len(first.rounds), 0)

    def test_new_weapon_modifiers_proc_inside_the_attack_cap(self):
        opponent = Fighter(
            key="b", name="Opponent", strength=40, health=40, agility=40, luck=40,
            armor=40, level=3,
        )
        effects = (
            {"code": "armor_shred", "value": 6, "cap": 24},
            {"code": "wound", "value": 1, "cap": 6},
            {"code": "burn", "value": 4, "turns": 2},
            {"code": "venom_blade", "value": 18, "poison": 2},
            {"code": "bleed", "value": 2, "cap": 3},
            {"code": "shield_breaker", "value": 100},
            {"code": "heavy_combo", "value": 20, "every": 3},
        )
        with patch.object(combat, "_resolve_blow", return_value=("hit", 20)), \
                patch.object(combat, "_signature", return_value=None):
            for effect in effects:
                with self.subTest(effect=effect["code"]):
                    result = combat.simulate(self._fighter_with(effect), opponent, seed=73)
                    self.assertTrue(any(
                        round_.event == f"amulet_{effect['code']}"
                        for round_ in result.rounds
                    ))
                    ordinary = [
                        round_ for round_ in result.rounds
                        if not round_.event.startswith("amulet_")
                    ]
                    for fighter_key in ("a", "b"):
                        self.assertLessEqual(
                            sum(round_.attacker == fighter_key for round_ in ordinary),
                            C.MAX_ATTACKS_PER_FIGHTER,
                        )

    def test_wound_reduction_is_capped_and_replays_exactly(self):
        effect = {"code": "wound", "value": 1, "cap": 6}
        opponent = Fighter(
            key="b", name="Opponent", strength=40, health=40, agility=40, luck=40,
            armor=0, level=3,
        )
        with patch.object(combat, "_resolve_blow", return_value=("hit", 1)), \
                patch.object(combat, "_signature", return_value=None):
            result = combat.simulate(self._fighter_with(effect), opponent, seed=74)
            replay = combat.simulate(self._fighter_with(effect), opponent, seed=74)
        self.assertEqual(result, replay)
        removed = sum(
            round_.damage for round_ in result.rounds
            if round_.event == "amulet_wound"
        )
        starting_hp = combat.derive(opponent, self._fighter_with(effect))["max_hp"]
        self.assertLessEqual(removed, round(starting_hp * 0.06) + 1)

    def test_start_stats_and_visible_procs_use_catalogue_percentages(self):
        opponent = _fighter("b", 40, name="B")
        base = _fighter("a", 40, name="A")
        vitality = self._fighter_with({"code": "vitality", "value": 14})
        ferocity = self._fighter_with({"code": "ferocity", "value": 3})
        self.assertEqual(combat.derive(vitality, opponent)["max_hp"], combat.derive(base, opponent)["max_hp"] + 14)
        self.assertEqual(combat.derive(ferocity, opponent)["damage"], combat.derive(base, opponent)["damage"] + 3)

        for effect in (
            {"code": "opening_shield", "value": 3},
            {"code": "opening_blast", "value": 4},
            {"code": "vampiric", "value": 9},
            {"code": "poison", "value": 3},
            {"code": "thorns", "value": 7},
            {"code": "regen", "value": 4},
        ):
            result = combat.simulate(self._fighter_with(effect), opponent, seed=7)
            self.assertTrue(
                any(round_.event == f"amulet_{effect['code']}" for round_ in result.rounds),
                effect["code"],
            )


if __name__ == "__main__":
    unittest.main()
