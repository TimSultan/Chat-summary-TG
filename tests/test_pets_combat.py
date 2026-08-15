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
from pets_gear_catalog import GEAR_SPECS
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
    def test_two_x_stat_gaps_expose_only_the_two_largest_build_weaknesses(self):
        specialist = Fighter(
            key="specialist", name="Спец", strength=20, health=20, agility=5, luck=4, armor=0,
        )
        balanced = _fighter("balanced", 20)

        derived = combat.derive(specialist, balanced)

        self.assertEqual(derived["deficits"], ("luck", "agility"))
        self.assertAlmostEqual(derived["accuracy"], 1.30)
        self.assertEqual(
            derived["incoming_damage_multiplier"], C.STAT_DEFICIT_AGILITY_DAMAGE_MULTIPLIER,
        )

    def test_strength_and_health_deficits_reduce_dodge_and_maximum_hp(self):
        frail = Fighter(key="frail", name="Хрупкий", strength=5, health=5, agility=20, luck=20, armor=0)
        balanced = _fighter("balanced", 20)

        derived = combat.derive(frail, balanced)

        self.assertEqual(derived["deficits"], ("health", "strength"))
        baseline_dodge = combat._saturate(C.DODGE_MAX, C.DODGE_K, frail.agility)
        self.assertAlmostEqual(
            derived["dodge"], baseline_dodge * C.STAT_DEFICIT_DODGE_MULTIPLIER,
        )
        self.assertAlmostEqual(
            derived["max_hp"],
            (C.BASE_HP + frail.health * C.HP_PER_POINT) * C.STAT_DEFICIT_HEALTH_MULTIPLIER,
        )

    def test_deficits_are_announced_at_fight_start(self):
        specialist = Fighter(key="specialist", name="Спец", strength=20, health=20, agility=5, luck=4, armor=0)
        balanced = _fighter("balanced", 20)

        result = combat.simulate(specialist, balanced, seed=12)

        notices = [round_ for round_ in result.rounds if round_.event.startswith("deficit_")]
        self.assertEqual({round_.event for round_ in notices}, {"deficit_luck", "deficit_agility"})
        self.assertTrue(all(round_.number == 0 and round_.damage == 0 for round_ in notices))

    def test_every_transcript_row_carries_post_event_audit_state(self):
        result = combat.simulate(_fighter("a", 12), _fighter("b", 12), seed=912)

        self.assertTrue(result.rounds)
        for row in result.rounds:
            self.assertEqual(set(row.state["fighters"]), {"a", "b"})
            for state in row.state["fighters"].values():
                self.assertIn("hp", state)
                self.assertIn("stunned", state)
                self.assertIn("used_scrolls", state)
                self.assertIn("used_shield_reactions", state)
        # The audit payload must be safe for the exact JSON store/API boundary.
        json.dumps([row.state for row in result.rounds])

    def test_agility_deficit_increases_damage_in_a_classic_fight(self):
        attacker = _fighter("attacker", 20)
        weak = Fighter(key="weak", name="Медленный", strength=20, health=200, agility=10, luck=20, armor=0)
        balanced = Fighter(key="balanced", name="Ровный", strength=20, health=200, agility=20, luck=20, armor=0)

        with patch.object(combat, "_resolve_blow", return_value=("hit", 10)), \
                patch.object(C, "MAX_SKILL_ACTIONS_PER_FIGHTER", 1), \
                patch.object(C, "SIGNATURE_TRIGGER_CHANCES", {
                    "strength": {2: 0.0, 3: 0.0},
                    "health": {2: 0.0, 3: 0.0},
                    "agility": {2: 0.0, 3: 0.0},
                    "luck": {2: 0.0, 3: 0.0},
                    "armor": {2: 0.0, 3: 0.0},
                }):
            vulnerable = combat.simulate(attacker, weak, seed=4)
            baseline = combat.simulate(attacker, balanced, seed=4)

        self.assertEqual(vulnerable.total_damage["attacker"], 12)
        self.assertEqual(baseline.total_damage["attacker"], 10)

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
        self.assertIn("signature_luck", [round_.event for round_ in result.rounds])
        signature_round = next(round_ for round_ in result.rounds if round_.event == "signature_luck")
        self.assertGreater(signature_round.damage, 0)

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
        self.assertIn("signature_agility_counter", [round_.event for round_ in result.rounds])

    def test_fights_allow_no_more_than_the_action_cap_per_fighter(self):
        # A round is a full exchange (leader strikes, follower counters back in the same
        # round unless already dead). The cap applies to each pet, not merely to total
        # blows, so a future change cannot let the opening attacker get an extra strike.
        a, b = _fighter("a", 40), _fighter("b", 40)
        for seed in range(200):
            result = combat.simulate(a, b, rng=random.Random(seed))
            attacks = {key: sum(rnd.attacker == key for rnd in result.rounds) for key in (a.key, b.key)}
            self.assertLessEqual(result.rounds[-1].number, C.MAX_SKILL_ACTIONS_PER_FIGHTER)
            self.assertLessEqual(attacks[a.key], C.MAX_SKILL_ACTIONS_PER_FIGHTER)
            self.assertLessEqual(attacks[b.key], C.MAX_SKILL_ACTIONS_PER_FIGHTER)
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

    def test_a_fighter_never_defends_twice_while_its_guard_is_still_up(self):
        """Leadership alternates round to round, so each fighter acts twice in a row every
        other round (a, b, b, a, a, b). A guard is a one-shot block set to a flat value,
        so a second Defend inside that back-to-back turn rewrote the same number and threw
        the first action away. It happened in roughly one fight in three."""
        import pets_scroll_catalog as scrolls

        def fighter(key, shield=None):
            return Fighter(
                key=key, name=key, strength=30, health=30, agility=25, luck=20,
                armor=8, level=8, skills=scrolls.SAMPLE_LOADOUT, shield=shield,
            )

        acting = {"defend", "hit", "crit", "blocked", "low_damage", "dodge", "amulet_guard"}
        # Once bare and once with a shield that heals on Defend -- the case where a
        # second Defend still did something, and so the tempting one to leave alone.
        for shield in (None, scrolls.shield("shield_lantern")):
            a, b = fighter("a", shield), fighter("b", shield)
            defends = 0
            for seed in range(400):
                previous_actor = previous_event = None
                for row in combat.simulate(a, b, seed=seed).rounds:
                    if not (row.event in acting or row.event.startswith("skill_")):
                        continue
                    if row.event == "defend":
                        defends += 1
                        self.assertFalse(
                            row.attacker == previous_actor and previous_event == "defend",
                            f"{row.attacker} defended twice in a row on seed {seed}",
                        )
                    previous_actor, previous_event = row.attacker, row.event
            # The rule must not have simply stopped anybody from ever defending.
            self.assertGreater(defends, 200)

    def test_an_equipped_shield_cannot_defend_before_the_second_personal_action(self):
        """Shield hooks used to be eligible immediately, so a seeded fight could open
        with healing, reflection or a barrier before its wearer had done anything."""
        import pets_scroll_catalog as scrolls

        class DefendWhenPossible:
            def random(self):
                return 0.0

            def uniform(self, _low, _high):
                return 0.0

            def choice(self, values):
                return "defend" if "defend" in values else values[0]

        shielded = Fighter(
            key="shielded", name="Shielded", strength=10, health=200,
            agility=10, luck=1, armor=0, shield=scrolls.shield("shield_lantern"),
        )
        target = Fighter(
            key="target", name="Target", strength=1, health=200,
            agility=1, luck=1, armor=0,
        )
        with patch.object(combat, "_signature", return_value=None), \
                patch.object(combat, "_resolve_blow", return_value=("hit", 1)):
            result = combat.simulate(
                shielded, target, rng=DefendWhenPossible(), max_actions=2,
            )

        own_actions = [
            row for row in result.rounds
            if row.attacker == shielded.key and row.event in {
                "hit", "crit", "blocked", "low_damage", "dodge", "defend",
            }
        ]
        self.assertGreaterEqual(len(own_actions), 2)
        self.assertNotEqual(own_actions[0].event, "defend")
        self.assertEqual(own_actions[1].event, "defend")
        self.assertFalse(any(
            row.event == "defend" and row.number == own_actions[0].number
            for row in result.rounds
        ))

    def test_shield_reflection_affects_an_opponent_without_scrolls(self):
        """Dungeon enemies have no scroll loadout. Shield statuses still have to affect
        them; the old loadout gate made bosses ignore Mirror and several other shields."""
        import pets_scroll_catalog as scrolls

        class DefendWhenPossible:
            def random(self):
                return 0.0

            def uniform(self, _low, _high):
                return 0.0

            def choice(self, values):
                return "defend" if "defend" in values else values[0]

        shielded = Fighter(
            key="hero", name="Hero", strength=10, health=200, agility=10, luck=1,
            armor=0, shield=scrolls.shield("shield_mirror"),
        )
        boss = Fighter(
            key="dungeon:boss_10", name="Boss", strength=10, health=200,
            agility=10, luck=1, armor=0,
        )
        with patch.object(combat, "_signature", return_value=None), \
                patch.object(combat, "_resolve_blow", return_value=("hit", 20)):
            result = combat.simulate(
                shielded, boss, rng=DefendWhenPossible(), max_actions=3,
            )

        reflected = next(row for row in result.rounds if row.event == "skill_reflect")
        self.assertEqual(reflected.attacker, shielded.key)
        self.assertGreater(reflected.damage, 0)

    def test_reactive_shields_work_against_dungeon_bosses_without_recursing(self):
        """The new hooks are worn-shield reactions, not scroll-only PvP effects.

        A deterministic boss opening lets this cover the three promised mechanics:
        parry-stun, healing from actual lost HP, and a single counterattack.  The latter
        must not recursively trigger the boss's own reactive shield.
        """
        import pets_scroll_catalog as scrolls

        class OpeningRng:
            def __init__(self, rolls=(0.0, 0.0)):
                self.rolls = iter(rolls)

            def random(self):
                return next(self.rolls, 0.99)

            def uniform(self, _low, _high):
                return 0.0

            def choice(self, values):
                return values[0]

        def fighters(shield):
            boss = Fighter(
                key="dungeon:boss_5", name="Boss", strength=20, health=200,
                agility=10, luck=1, armor=0,
            )
            hero = Fighter(
                key="hero", name="Hero", strength=20, health=200, agility=10,
                luck=1, armor=0, shield=shield, starting_hp=200,
            )
            return boss, hero

        with patch.object(combat, "_signature", return_value=None), \
                patch.object(combat, "_resolve_blow", return_value=("hit", 100)):
            boss, hero = fighters(scrolls.shield("shield_royal_riposte"))
            parry = combat.simulate(boss, hero, rng=OpeningRng(), max_actions=2)
            self.assertTrue(any(
                row.event == "amulet_shield_parry_stun" and row.attacker == hero.key
                for row in parry.rounds
            ))
            self.assertTrue(any(
                row.event == "stun_skip" and row.attacker == boss.key
                for row in parry.rounds
            ))

            boss, hero = fighters(scrolls.shield("shield_crimson_reliquary"))
            healing = combat.simulate(boss, hero, rng=OpeningRng(), max_actions=1)
            heal = next(row for row in healing.rounds if row.event == "amulet_shield_damage_heal")
            self.assertEqual(heal.attacker, hero.key)
            self.assertEqual(heal.damage, 50)

            boss, hero = fighters(scrolls.shield("shield_judgement"))
            counter = combat.simulate(boss, hero, rng=OpeningRng(), max_actions=2)
            counters = [row for row in counter.rounds if row.event == "amulet_shield_counterattack"]
            self.assertEqual(len(counters), 1)
            self.assertEqual(counters[0].attacker, hero.key)
            self.assertGreater(counters[0].damage, 0)

    def test_stun_skips_a_dungeon_boss_before_items_or_actions_can_trigger(self):
        """A boss uses the same status rules as a pet. In particular, stun resolves
        before Cocoon, regeneration, Defend, scrolls or its ordinary attack."""

        class StunFirst:
            calls = 0

            def random(self):
                self.calls += 1
                # Hero wins initiative; later rolls keep the stun scroll from dodging.
                return 0.0 if self.calls == 1 else 0.99

            def uniform(self, _low, _high):
                return 0.0

            def choice(self, values):
                return "skill_1" if "skill_1" in values else values[0]

        hero = Fighter(
            key="hero", name="Hero", strength=20, health=200, agility=20, luck=1,
            armor=0, skills=("scroll_gravity_thread", None, None, None),
        )
        boss = Fighter(
            key="dungeon:boss_5", name="Boss", strength=20, health=200,
            agility=20, luck=1, armor=0,
            effects=({"code": "cocoon", "value": 250}, {"code": "regen", "value": 20}),
        )
        with patch.object(combat, "_signature", return_value=None), \
                patch.object(combat, "_resolve_blow", return_value=("hit", 10)):
            result = combat.simulate(hero, boss, rng=StunFirst(), max_actions=1)

        self.assertIn("skill_scroll_gravity_thread", [row.event for row in result.rounds])
        skipped = next(row for row in result.rounds if row.event == "stun_skip")
        self.assertEqual(skipped.attacker, boss.key)
        self.assertIn("пропускает ход", skipped.text)
        self.assertFalse(any(
            row.attacker == boss.key and row.event in {
                "hit", "crit", "blocked", "low_damage", "dodge", "defend",
                "amulet_cocoon", "amulet_regen",
            }
            for row in result.rounds
        ))

    def test_even_fights_are_decided_by_a_knockout(self):
        """The damage tiebreak is a backstop, not an outcome the design leans on.

        This used to sit near 85%: a fighter without scrolls got 10 actions, and an even
        fight often ran out of them. Every fighter now gets the same 24 that a pet
        carrying scrolls always had, and at that budget the tiebreak was already almost
        never reached -- so this is bare fights joining live ones, not a new behaviour.
        """
        for level in (1, 40, 80):
            a, b = _fighter("a", level, name="A"), _fighter("b", level, name="B")
            rate = sum(
                not combat.simulate(a, b, seed=seed).stopped_early
                for seed in range(1_000)
            ) / 1_000
            self.assertGreaterEqual(rate, 0.95, f"level {level}: knockout rate {rate}")

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
        self.assertEqual(
            sum(round_.event == "hit" for round_ in result.rounds),
            2 * C.MAX_SKILL_ACTIONS_PER_FIGHTER,
        )
        # Derived rather than written out, so retuning the budget cannot leave this
        # test asserting a fight length the engine no longer runs.
        self.assertEqual(result.total_damage, {
            "a": 20 * C.MAX_SKILL_ACTIONS_PER_FIGHTER,
            "b": 10 * C.MAX_SKILL_ACTIONS_PER_FIGHTER,
        })
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
        self.assertEqual(result.total_damage, {
            "a": 20 * C.MAX_SKILL_ACTIONS_PER_FIGHTER,
            "b": 20 * C.MAX_SKILL_ACTIONS_PER_FIGHTER,
        })


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

    def test_flat_damage_effects_scale_with_the_owners_level(self):
        low = Fighter("low", "Low", 40, 40, 40, 40, 0, level=1)
        high = Fighter("high", "High", 40, 40, 40, 40, 0, level=25)
        self.assertEqual(combat._scaled_flat_damage(6, low, 100), 8)
        self.assertEqual(combat._scaled_flat_damage(6, high, 100), 12)

    def test_every_legacy_flat_damage_hook_uses_the_level_curve(self):
        opponent = Fighter("b", "B", 10, 200, 20, 20, 0, level=25)
        cases = (
            ({"code": "poison", "value": 15}, "amulet_poison", 15),
            ({"code": "burn", "value": 12, "turns": 2}, "amulet_burn", 12),
            ({"code": "venom_blade", "value": 20, "poison": 6}, "amulet_venom_blade", 6),
            ({"code": "bleed", "value": 5, "cap": 4}, "amulet_bleed", 5),
            ({"code": "retaliation", "value": 8}, "amulet_retaliation", 8),
        )
        with patch.object(combat, "_resolve_blow", return_value=("hit", 10)), \
                patch.object(combat, "_signature", return_value=None):
            for effect, event, old_flat_damage in cases:
                with self.subTest(effect=effect["code"]):
                    owner = Fighter(
                        "a", "A", 40, 200, 20, 20, 0,
                        effects=(effect,), level=25,
                    )
                    result = combat.simulate(owner, opponent, seed=0, max_actions=3)
                    rows = [row for row in result.rounds if row.event == event]
                    self.assertTrue(rows)
                    self.assertGreater(rows[0].damage, old_flat_damage)

    def test_consecutive_poison_hits_accumulate_until_the_target_acts(self):
        poison = {"code": "poison", "value": 10}
        owner = Fighter(
            "a", "A", 40, 200, 20, 20, 0, effects=(poison,), level=25,
        )
        opponent = Fighter("b", "B", 10, 200, 20, 20, 0, level=25)
        single = combat._scaled_flat_damage(
            poison["value"], owner, C.BASE_DAMAGE + owner.strength * C.DAMAGE_PER_POINT,
        )
        with patch.object(combat, "_resolve_blow", return_value=("hit", 10)), \
                patch.object(combat, "_signature", return_value=None):
            result = combat.simulate(owner, opponent, seed=0, max_actions=3)
        poison_rows = [row for row in result.rounds if row.event == "amulet_poison"]
        self.assertEqual([row.damage for row in poison_rows], [single * 2])

    def test_stat_scaled_rune_fire_is_not_level_scaled_twice(self):
        rune = {"code": "burn", "value": 30, "turns": 2, "level_scaled": False}
        owner = Fighter(
            "a", "A", 40, 200, 20, 20, 0, effects=(rune,), level=25,
        )
        opponent = Fighter("b", "B", 10, 200, 20, 20, 0, level=25)
        with patch.object(combat, "_resolve_blow", return_value=("hit", 10)), \
                patch.object(combat, "_signature", return_value=None):
            result = combat.simulate(owner, opponent, seed=0, max_actions=3)
        burn_rows = [row for row in result.rounds if row.event == "amulet_burn"]
        self.assertTrue(burn_rows)
        self.assertTrue(all(row.damage == 30 for row in burn_rows))

    def test_all_catalogue_effects_are_seeded_and_safe_to_replay(self):
        """A malformed metadata deployment must not make one arena click non-replayable."""
        effect_specs = [
            *AMULET_SPECS,
            *(spec for spec in WEAPON_SPECS if spec.effect),
            *(spec for spec in GEAR_SPECS if spec.effect),
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

    def test_every_effectful_boot_and_glove_actually_procs(self):
        """Both tiers now, not just legendary: the build items put effects on the rare
        rung too, and a rare that never fires is a slot the player filled for nothing."""
        opponent = Fighter(
            key="b", name="Opponent", strength=55, health=45, agility=35, luck=35,
            armor=10, level=8,
        )
        effectful_gear = [spec for spec in GEAR_SPECS if spec.effect]
        self.assertEqual(len(effectful_gear), 22)
        self.assertEqual({spec.rarity for spec in effectful_gear}, {"rare", "legendary"})
        # Two of them change the fight without ever writing a line in it: precision moves
        # the miss multiplier at derive time and first_strike only tilts the initiative
        # roll. Looking for a log row would call both of them broken, so each is checked
        # against the number it actually moves.
        silent = {"precision", "first_strike"}
        bare = self._fighter_with({"code": "regen", "value": 0})
        for spec in effectful_gear:
            code = spec.effect_dict()["code"]
            with self.subTest(effect=code):
                fighter = self._fighter_with(spec.effect_dict())
                if code == "precision":
                    self.assertLess(
                        combat.derive(fighter, opponent)["accuracy"],
                        combat.derive(bare, opponent)["accuracy"],
                    )
                    continue
                if code == "first_strike":
                    def leads(who):
                        return sum(
                            combat.simulate(who, opponent, seed=seed).rounds[0].attacker == "a"
                            for seed in range(300)
                        )
                    self.assertGreater(leads(fighter), leads(bare))
                    continue
                seen = any(
                    any(row.event == f"amulet_{code}" for row in combat.simulate(
                        fighter, opponent, seed=seed,
                    ).rounds)
                    for seed in range(120)
                )
                self.assertTrue(seen, f"{code} never proc'd")
        self.assertTrue(silent <= {spec.effect_dict()["code"] for spec in effectful_gear})

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
                            C.MAX_SKILL_ACTIONS_PER_FIGHTER,
                        )

    def test_legendary_sting_can_stun_twice_but_never_more(self):
        attacker = Fighter(
            key="a", name="Stinger", strength=5, health=200, agility=10, luck=10,
            armor=0, effects=({"code": "stun", "value": 1, "cap": 2},), level=1,
        )
        defender = Fighter(
            key="b", name="Tank", strength=5, health=200, agility=10, luck=10,
            armor=0, effects=(), level=1,
        )
        with patch.object(combat, "_resolve_blow", return_value=("crit", 5)), \
                patch.object(combat, "_signature", return_value=None):
            result = combat.simulate(attacker, defender, seed=75)
        applications = [
            row for row in result.rounds
            if row.event == "amulet_stun" and row.attacker == attacker.key
        ]
        skipped = [
            row for row in result.rounds
            if row.event == "stun_skip" and row.attacker == defender.key
        ]
        self.assertEqual(len(applications), 2)
        self.assertEqual(len(skipped), 2)

    def test_legendary_shield_breaker_keeps_the_breach_open(self):
        defender = Fighter(
            key="b", name="Armored", strength=10, health=200, agility=10, luck=10,
            armor=200, effects=(), level=1,
        )

        def attacker(effect):
            return Fighter(
                key="a", name="Breaker", strength=20, health=100, agility=20, luck=20,
                armor=0, effects=(effect,), level=1,
            )

        rare = combat.simulate(
            attacker({"code": "shield_breaker", "value": 100}), defender, seed=0,
        )
        legendary = combat.simulate(
            attacker({"code": "shield_breaker", "value": 100, "shred": 20}),
            defender, seed=0,
        )
        self.assertGreater(legendary.total_damage["a"], rare.total_damage["a"])

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


class EffectKnobTests(unittest.TestCase):
    """Every number a passive prints has to change the fight it is printed on.

    This is one test rather than a dozen because it guards a *class* of bug, and that
    class shipped repeatedly: `spring` hardcoded a double, `cocoon` reflected exactly the
    blow it ate, `chill` and `phantom_step` were one-shot flags, and `first_strike` only
    moved an initiative roll that alternates back the next round anyway. In every case
    the catalogue said one thing, the engine did another, and nothing failed -- the rare
    and the legendary version of the same item measured identically. A passive whose knob
    does nothing is worse than a missing passive, because the player pays for it.
    """

    OPPONENT = Fighter(
        key="b", name="B", strength=20, health=20, agility=20, luck=20, armor=5, level=10,
    )
    # (code, weak, strong) -- roughly the ends the catalogue ships between, widened where
    # the two shipped tiers are close enough that the gap between them is real but small.
    KNOBS = (
        ("spring", {"value": 100}, {"value": 200}),
        ("cocoon", {"value": 100}, {"value": 250}),
        ("chill", {"value": 100, "hits": 1}, {"value": 100, "hits": 2}),
        ("phantom_step", {"value": 1, "hits": 1}, {"value": 1, "hits": 2}),
        ("first_strike", {"value": 25}, {"value": 100}),
        ("armor_shred", {"value": 4, "cap": 16}, {"value": 10, "cap": 36}),
        ("shield_breaker", {"value": 70, "power": 10}, {"value": 100, "power": 150}),
        ("piercing", {"value": 6}, {"value": 24}),
        ("acid", {"value": 25}, {"value": 80}),
        ("precision", {"value": 45}, {"value": 90}),
        ("gambler", {"value": 60, "downside": 10, "chance": 10},
                    {"value": 60, "downside": 10, "chance": 90}),
        ("candle", {"value": 90, "downside": 20, "chance": 10},
                   {"value": 90, "downside": 20, "chance": 90}),
    )

    def _score(self, effect):
        hero = Fighter(
            key="a", name="A", strength=20, health=20, agility=20, luck=20, armor=5,
            level=10, effects=(effect,),
        )
        wins = 0.0
        for seed in range(600):
            result = combat.simulate(hero, self.OPPONENT, seed=seed)
            wins += 1.0 if result.winner == "a" else (.5 if result.is_draw else 0.0)
        return wins / 600 * 100

    def test_every_tunable_passive_is_stronger_at_its_higher_setting(self):
        for code, weak, strong in self.KNOBS:
            with self.subTest(effect=code):
                low = self._score({"code": code, **weak})
                high = self._score({"code": code, **strong})
                # Three points on 600 seeded fights is well clear of noise and far below
                # any of the real gaps; the assertion is "the knob is connected", not a
                # pin on today's tuning.
                self.assertGreater(
                    high, low + 3,
                    f"{code}: {weak} scored {low:.1f}%, {strong} scored {high:.1f}% -- "
                    "the catalogue value is not reaching the fight",
                )

    def test_the_default_settings_still_reproduce_the_old_one_shot_behaviour(self):
        """The three knobs added here default to the value the engine used to hardcode,
        so an item that never declares them fights exactly as it did before."""
        for code, params in (
            ("spring", {"value": 100}),
            ("cocoon", {"value": 100}),
            ("chill", {"value": 60}),
            ("phantom_step", {"value": 1}),
        ):
            with self.subTest(effect=code):
                bare = {"code": code, **params}
                explicit = {"code": code, **params, "hits": 1}
                hero_a = Fighter(
                    key="a", name="A", strength=20, health=20, agility=20, luck=20,
                    armor=5, level=10, effects=(bare,),
                )
                hero_b = Fighter(
                    key="a", name="A", strength=20, health=20, agility=20, luck=20,
                    armor=5, level=10, effects=(explicit,),
                )
                for seed in (11, 404, 9001):
                    self.assertEqual(
                        combat.simulate(hero_a, self.OPPONENT, seed=seed).rounds,
                        combat.simulate(hero_b, self.OPPONENT, seed=seed).rounds,
                    )


class AttackTypeTests(unittest.TestCase):
    def test_predator_bite_heals_from_the_damage_it_lands(self):
        class SkillRng:
            def random(self):
                return .99

            def uniform(self, _low, _high):
                return 0.0

            def choice(self, values):
                return "skill_1" if "skill_1" in values else values[0]

        biter = Fighter(
            key="biter", name="Biter", strength=20, health=200, agility=20, luck=1,
            armor=0, skills=("scroll_predator_bite", None, None, None), starting_hp=1_000,
        )
        target = Fighter(
            key="target", name="Target", strength=1, health=500, agility=1, luck=1,
            armor=0, starting_hp=10_000,
        )
        with patch.object(combat, "_signature", return_value=None):
            result = combat.simulate(biter, target, rng=SkillRng(), max_actions=1)

        bite = next(row for row in result.rounds if row.event == "skill_scroll_predator_bite")
        healed = next(row for row in result.rounds if row.event == "skill_lifesteal")
        self.assertEqual(healed.damage, -round(bite.damage * .70))
        self.assertGreater(healed.attacker_hp, 1_000)

    def test_elemental_damage_is_also_magic(self):
        self.assertEqual(
            combat.normalize_attack_types((combat.ELEMENTAL,)),
            (combat.ELEMENTAL, combat.MAGIC),
        )
        self.assertTrue(combat.is_magic_attack((combat.ELEMENTAL,)))
        self.assertFalse(combat.is_magic_attack((combat.PHYSICAL,)))

    def test_antimage_reflects_elemental_magic_back_to_its_source(self):
        class SkillRng:
            def random(self):
                return .99

            def uniform(self, _low, _high):
                return 0.0

            def choice(self, values):
                return "skill_1" if "skill_1" in values else values[0]

        mage = Fighter(
            key="mage", name="Mage", strength=20, health=200, agility=20, luck=1,
            armor=0, skills=("scroll_arcane_spark", None, None, None), starting_hp=1_000,
        )
        antimage = Fighter(
            key="antimage", name="Antimage", strength=1, health=200, agility=1, luck=1,
            armor=0, starting_hp=1_000, magic_reflect_multiplier=.85,
            enchant_reflect_multiplier=.85,
        )
        with patch.object(combat, "_signature", return_value=None):
            result = combat.simulate(mage, antimage, rng=SkillRng(), max_actions=1)

        spell = next(round_ for round_ in result.rounds if round_.event == "skill_scroll_arcane_spark")
        reflected = next(round_ for round_ in result.rounds if round_.event == "antimagic_reflect")
        self.assertEqual(spell.attack_types, (combat.ELEMENTAL, combat.MAGIC))
        self.assertEqual(reflected.damage, round(spell.damage * .85))
        self.assertEqual(reflected.attack_types, (combat.MAGIC,))

    def test_antimage_reflects_an_enchanted_physical_weapon(self):
        attacker = Fighter(
            key="rune-user", name="Rune user", strength=40, health=200, agility=1, luck=1,
            armor=0, starting_hp=1_000, weapon_enchanted=True,
        )
        antimage = Fighter(
            key="antimage", name="Antimage", strength=1, health=200, agility=1, luck=1,
            armor=0, starting_hp=1_000, enchant_reflect_multiplier=.85,
        )
        with patch.object(combat, "_signature", return_value=None):
            result = combat.simulate(attacker, antimage, seed=123, max_actions=1)

        hit = next(round_ for round_ in result.rounds if round_.attacker == "rune-user" and round_.damage)
        reflected = next(round_ for round_ in result.rounds if round_.event == "antimagic_reflect")
        self.assertEqual(hit.attack_types, (combat.PHYSICAL,))
        self.assertEqual(reflected.damage, round(hit.damage * .85))


if __name__ == "__main__":
    unittest.main()
