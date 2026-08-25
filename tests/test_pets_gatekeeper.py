"""Pure rules for the Steel Gatekeeper's prediction-and-lock encounter."""

import json
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pets_gatekeeper as gatekeeper


def _fight(*, hero_hp=4_000, damage=500, spell_power=500,
           boss_hp=5_000, boss_damage=120, seed=1, **options):
    hero = {
        "name": "Герой", "hp": hero_hp, "max_hp": hero_hp,
        "damage": damage, "spell_power": spell_power,
        "crit": 0, "crit_power": 1.5, "reduction": 0, "guard": 0.55,
    }
    boss = {
        "name": "Стальной привратник", "max_hp": boss_hp,
        "damage": boss_damage, "level": 18, "floor": 10,
    }
    return gatekeeper.start(hero, boss, seed=seed, **options)


class GatekeeperRulesTests(unittest.TestCase):
    def test_intro_has_configurable_locks_steps_and_contextual_actions(self):
        state = _fight(locks_total=5, step_limit=6)
        shown = gatekeeper.public(state)

        self.assertEqual(shown["locks"], [False] * 5)
        self.assertEqual(shown["steps"], [False] * 6)
        self.assertEqual(shown["predictions"], [])
        self.assertTrue(shown["telegraph"])
        self.assertGreaterEqual(len(shown["actions"]), 3)
        self.assertTrue(all(row["category"] in gatekeeper.CATEGORIES
                            for row in shown["actions"]))

    def test_two_repeated_actions_create_a_prediction_that_can_be_broken(self):
        state = _fight()
        state = gatekeeper.take(state, gatekeeper.WEAPON, seed=2)
        self.assertIsNone(state["current_prediction"])
        self.assertIn("тихо щёлкает", gatekeeper.public(state)["adaptation_hint"])
        state = gatekeeper.take(state, gatekeeper.WEAPON, seed=3)
        self.assertEqual(state["current_prediction"], gatekeeper.WEAPON)

        alternate = next(
            row["code"] for row in gatekeeper.actions(state)
            if row["category"] != gatekeeper.WEAPON
            and row["category"] in (gatekeeper.MAGIC, gatekeeper.MOVEMENT)
        )
        after = gatekeeper.take(state, alternate, seed=4)

        self.assertEqual(after["locks_open"], 1)
        self.assertEqual(after["systems_fooled"], 1)
        self.assertTrue(any("Прогноз сломан" in line for line in after["log"]))

    def test_doing_exactly_what_was_predicted_is_punished(self):
        state = _fight()
        state = gatekeeper.take(state, gatekeeper.WEAPON, seed=2)
        state = gatekeeper.take(state, gatekeeper.WEAPON, seed=3)
        before_hp = state["hero_hp"]

        after = gatekeeper.take(state, gatekeeper.WEAPON, seed=4)

        self.assertEqual(after["locks_open"], 0)
        self.assertEqual(after["mistakes"], 1)
        self.assertLess(after["hero_hp"], before_hp)
        self.assertTrue(any("предсказано" in line for line in after["log"]))

    def test_false_step_only_appears_near_the_limit_and_opens_one_lock(self):
        state = _fight(step_limit=5)
        self.assertNotIn(gatekeeper.FALSE_STEP, {row["code"] for row in gatekeeper.actions(state)})
        state["step_counter"] = 4
        self.assertIn(gatekeeper.FALSE_STEP, {row["code"] for row in gatekeeper.actions(state)})

        after = gatekeeper.take(state, gatekeeper.FALSE_STEP, seed=8)

        self.assertEqual(after["step_counter"], 0)
        self.assertEqual(after["locks_open"], 1)
        self.assertEqual(after["systems_fooled"], 1)

    def test_real_last_step_triggers_percentage_damage_instead_of_a_lock(self):
        state = _fight(step_limit=4)
        state["step_counter"] = 3
        before_hp = state["hero_hp"]

        after = gatekeeper.take(state, gatekeeper.MOVEMENT, seed=5)

        self.assertEqual(after["step_counter"], 0)
        self.assertEqual(after["locks_open"], 0)
        self.assertEqual(after["mistakes"], 1)
        self.assertLessEqual(after["hero_hp"], before_hp - round(state["hero_max_hp"] * 0.14))

    def test_closed_armour_takes_chip_damage_and_magic_passes_more(self):
        state = _fight(damage=500, spell_power=500, boss_hp=1_000)
        state["current_boss_action"] = "steel_wall"

        weapon = gatekeeper.take(state, gatekeeper.WEAPON, seed=11)
        magic = gatekeeper.take(state, gatekeeper.MAGIC, seed=11)
        weapon_damage = state["boss_hp"] - weapon["boss_hp"]
        magic_damage = state["boss_hp"] - magic["boss_hp"]

        self.assertGreater(magic_damage, weapon_damage)
        self.assertLessEqual(magic_damage, round(state["boss_max_hp"] * 0.08))

    def test_core_uses_real_power_but_caps_one_damage_window(self):
        state = _fight(damage=10_000, spell_power=10_000, boss_hp=1_000)
        state["locks_open"] = state["locks_total"]
        state["is_core_open"] = True

        after = gatekeeper.take(state, gatekeeper.CORE_WEAPON, seed=12)

        self.assertEqual(state["boss_hp"] - after["boss_hp"], 380)
        self.assertEqual(after["locks_open"], 0)
        self.assertFalse(after["is_core_open"])
        self.assertEqual(after["cores_struck"], 1)

    def test_emergency_mode_tracks_two_categories_and_a_feint_opens_two_locks(self):
        state = _fight()
        state.update({
            "is_emergency_mode": True,
            "current_prediction": gatekeeper.WEAPON,
            "secondary_prediction": gatekeeper.MAGIC,
            "current_boss_action": "steel_wall",
            "player_action_history": [gatekeeper.WEAPON, gatekeeper.WEAPON],
            "adaptation": {
                gatekeeper.WEAPON: 3.0, gatekeeper.MAGIC: 2.0,
                gatekeeper.DEFENCE: 0.0, gatekeeper.MOVEMENT: 0.0,
            },
        })

        after = gatekeeper.take(state, gatekeeper.DEFENCE, seed=13)

        self.assertEqual(after["locks_open"], 2)
        self.assertEqual(after["systems_fooled"], 1)
        self.assertEqual(len(gatekeeper.public(after)["predictions"]), 2)

    def test_attack_selection_never_covers_every_reasonable_answer(self):
        categories = gatekeeper.CATEGORIES
        for primary in categories:
            for secondary in categories:
                if primary == secondary:
                    continue
                for seed in range(20):
                    state = _fight(damage=1, spell_power=1, seed=seed)
                    state.update({
                        "is_emergency_mode": True,
                        "current_prediction": primary,
                        "secondary_prediction": secondary,
                        "step_counter": 0,
                    })
                    attack = gatekeeper._pick_attack(state, random.Random(seed))
                    sensible = gatekeeper._reasonable_answers(state, attack)
                    self.assertTrue(
                        sensible - {primary, secondary},
                        (primary, secondary, attack, sensible),
                    )

    def test_weighted_attacks_react_to_a_player_who_always_blocks(self):
        neutral = _fight()
        blocker = _fight()
        blocker["player_action_history"] = [gatekeeper.DEFENCE] * 6
        neutral_breakers = sum(
            gatekeeper._pick_attack(neutral, random.Random(seed)) == "shield_breaker"
            for seed in range(250)
        )
        blocker_breakers = sum(
            gatekeeper._pick_attack(blocker, random.Random(seed)) == "shield_breaker"
            for seed in range(250)
        )
        self.assertGreater(blocker_breakers, neutral_breakers * 2)

    def test_state_round_trips_through_json(self):
        state = gatekeeper.take(_fight(), gatekeeper.WEAPON, seed=20)
        self.assertEqual(json.loads(json.dumps(state, ensure_ascii=False)), state)


if __name__ == "__main__":
    unittest.main()
