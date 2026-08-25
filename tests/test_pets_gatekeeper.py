"""Pure rules for the Steel Gatekeeper's prediction-and-lock encounter.

The fight this module pins is about MANAGING what the machine learns, so most of what is
tested here is the shape of that bargain: a pattern has to be fed before it can be broken,
feeding it costs, breaking it pays, and no single trick takes the whole chest.
"""

import json
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pets_gatekeeper as gatekeeper


def _fight(*, hero_hp=400_000, damage=500, spell_power=500,
           boss_hp=500_000, boss_damage=120, seed=1, **options):
    """A fight with enough health on both sides to study the rules without dying."""
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


def _legal(state):
    return {row["code"] for row in gatekeeper.actions(state)}


def _feed(state, pattern, seed=100):
    """Press a repeating pattern, substituting a legal button when one is not offered."""
    for index, wanted in enumerate(pattern):
        if gatekeeper.is_over(state):
            break
        legal = _legal(state)
        choice = wanted if wanted in legal else sorted(legal)[0]
        state = gatekeeper.take(state, choice, seed=seed + index)
    return state


def _feed_until_committed(state, pattern, limit=24, seed=100):
    """Show the machine a pattern until it commits to a prediction, or give up."""
    for index in range(limit):
        if state.get("committed") or gatekeeper.is_over(state):
            return state
        legal = _legal(state)
        wanted = pattern[index % len(pattern)]
        choice = wanted if wanted in legal else sorted(legal)[0]
        state = gatekeeper.take(state, choice, seed=seed + index)
    return state


def _break_choice(state):
    """A legal button that is neither expected nor covered."""
    blocked = {state.get("current_prediction"), state.get("covered_answer")}
    options = [code for code in sorted(_legal(state))
               if code not in blocked and code != gatekeeper.FALSE_STEP]
    return options[0] if options else None


class GatekeeperForecastTests(unittest.TestCase):
    def test_an_alternating_pattern_is_read_where_a_repeat_is_not_needed(self):
        """The old machine learned buttons, so not repeating yourself beat it.

        This one learns what follows what, which makes ⚔️→🛡→⚔️→🛡 -- four presses with
        no repeat anywhere in them -- the most legible thing a player can do.
        """
        state = _feed(_fight(), [gatekeeper.WEAPON, gatekeeper.DEFENCE] * 2)

        self.assertIsNotNone(state["current_prediction"])
        self.assertTrue(state["committed"], state["confidence"])
        self.assertGreaterEqual(state["confidence"], gatekeeper.CONFIDENCE_COMMITTED)

    def test_nothing_is_predicted_before_a_pattern_could_exist(self):
        """Two presses are not a habit, and a machine that commits to them is guessing."""
        state = _feed(_fight(), [gatekeeper.WEAPON, gatekeeper.MAGIC])

        self.assertIsNone(state["current_prediction"])
        self.assertEqual(state["confidence"], 0.0)
        self.assertFalse(state["committed"])

    def test_the_forecast_is_the_same_every_time_it_is_computed(self):
        """No dice anywhere in what the machine believes.

        A prediction with a random nudge in it cannot be audited from the row of evidence
        on screen, and a fight the player cannot audit is the guessing game this boss is
        explicitly not.
        """
        state = _feed(_fight(), [gatekeeper.WEAPON, gatekeeper.DEFENCE] * 2)
        first = gatekeeper._forecast(state)
        for _ in range(5):
            self.assertEqual(gatekeeper._forecast(state), first)

    def test_an_abandoned_pattern_stops_being_believed(self):
        """Feeding a NEW pattern has to be possible, so the old one has to fade."""
        state = _feed(_fight(), [gatekeeper.WEAPON, gatekeeper.DEFENCE] * 3)
        believed = state["current_prediction"]
        self.assertIsNotNone(believed)

        # Six turns of something else entirely.
        state = _feed(state, [gatekeeper.MOVEMENT, gatekeeper.MAGIC] * 3, seed=300)
        self.assertNotEqual(state["current_prediction"], believed)


class GatekeeperLockTests(unittest.TestCase):
    def test_a_lock_needs_a_committed_prediction_and_not_merely_a_surprise(self):
        """Pressing something unexpected early is not a break -- there is nothing to break."""
        state = _feed(_fight(), [gatekeeper.WEAPON, gatekeeper.MAGIC])
        self.assertFalse(state["committed"])

        state = gatekeeper.take(state, sorted(_legal(state))[0], seed=7)
        self.assertEqual(state["locks_open"], 0)

    def test_breaking_a_committed_prediction_opens_a_lock(self):
        state = _feed_until_committed(_fight(), [gatekeeper.WEAPON, gatekeeper.DEFENCE])
        self.assertTrue(state["committed"])
        choice = _break_choice(state)
        self.assertIsNotNone(choice)

        after = gatekeeper.take(state, choice, seed=11)

        self.assertEqual(after["locks_open"], 1)
        # Either way of failing counts; which one depends on whether the attack it chose
        # was the long wind-up built for the answer it was waiting for.
        self.assertEqual(len(after["locks_opened_by"]), 1)
        self.assertIn(after["locks_opened_by"][0],
                      (gatekeeper.PREDICTION_BREAK, gatekeeper.COMBAT_BAIT))
        self.assertTrue(any("ЩЁЛК" in line for line in after["log"]), after["log"])

    def test_doing_the_expected_thing_closes_a_lock_that_was_already_open(self):
        """The stake that makes feeding a pattern a decision rather than a free ride."""
        state = _feed_until_committed(_fight(), [gatekeeper.WEAPON, gatekeeper.DEFENCE])
        state = gatekeeper.take(state, _break_choice(state), seed=11)
        self.assertEqual(state["locks_open"], 1)

        state = _feed_until_committed(state, [gatekeeper.WEAPON, gatekeeper.DEFENCE], seed=400)
        self.assertTrue(state["committed"])
        expected = state["current_prediction"]
        self.assertIn(expected, _legal(state))

        after = gatekeeper.take(state, expected, seed=12)

        self.assertEqual(after["locks_open"], 0)
        self.assertTrue(any("закрывается" in line for line in after["log"]), after["log"])

    def test_an_ordinary_mistake_never_closes_a_lock(self):
        """Only a fully committed prediction costs a lock; a bad turn is just a bad turn."""
        state = _feed_until_committed(_fight(), [gatekeeper.WEAPON, gatekeeper.DEFENCE])
        state = gatekeeper.take(state, _break_choice(state), seed=11)
        self.assertEqual(state["locks_open"], 1)

        # A turn where the machine is not committed cannot take the lock back.
        state["committed"] = False
        state["current_prediction"] = None
        state["covered_answer"] = None
        after = gatekeeper.take(state, sorted(_legal(state))[0], seed=13)
        self.assertEqual(after["locks_open"], 1)

    def test_the_same_trick_cannot_take_every_lock(self):
        """Three locks, three kinds of failure, and a ceiling of two per kind."""
        state = _fight()
        state["locks_opened_by"] = [gatekeeper.PREDICTION_BREAK] * gatekeeper.LOCK_METHOD_LIMIT
        allowed, refusal = gatekeeper._lock_gate(state, gatekeeper.PREDICTION_BREAK)

        self.assertFalse(allowed)
        self.assertIn("обман", refusal.lower())
        # The other ways in are untouched.
        self.assertTrue(gatekeeper._lock_gate(state, gatekeeper.MOVEMENT_BREAK)[0])

    def test_a_second_prediction_break_demands_a_deeper_commitment(self):
        state = _fight()
        state["confidence"] = gatekeeper.CONFIDENCE_COMMITTED
        self.assertTrue(gatekeeper._lock_gate(state, gatekeeper.PREDICTION_BREAK)[0])

        state["locks_opened_by"] = [gatekeeper.PREDICTION_BREAK]
        self.assertFalse(gatekeeper._lock_gate(state, gatekeeper.PREDICTION_BREAK)[0])
        state["confidence"] = gatekeeper.CONFIDENCE_COMMITTED + gatekeeper.CONFIDENCE_REUSE_STEP
        self.assertTrue(gatekeeper._lock_gate(state, gatekeeper.PREDICTION_BREAK)[0])

    def test_baiting_out_a_prepared_counter_counts_as_its_own_trick(self):
        """The shield breaker swung at an expected shield is a different failure.

        Kept apart from an ordinary prediction break so a player who has spent one way in
        still has the other -- which is the whole reason the methods are counted.
        """
        state = _fight()
        state["current_prediction"] = gatekeeper.DEFENCE
        self.assertEqual(
            gatekeeper._break_method(state, "shield_breaker", gatekeeper.MAGIC),
            gatekeeper.COMBAT_BAIT,
        )
        # Same attack, but it is not what the machine was waiting for.
        state["current_prediction"] = gatekeeper.MOVEMENT
        self.assertEqual(
            gatekeeper._break_method(state, "shield_breaker", gatekeeper.MAGIC),
            gatekeeper.PREDICTION_BREAK,
        )


class GatekeeperCoveredAnswerTests(unittest.TestCase):
    def test_the_obvious_escape_is_covered_once_the_chest_starts_opening(self):
        """Otherwise "it expects weapon, press magic" is a solution again."""
        state = _feed_until_committed(_fight(), [gatekeeper.WEAPON, gatekeeper.DEFENCE])
        state = gatekeeper.take(state, _break_choice(state), seed=11)
        state = _feed_until_committed(state, [gatekeeper.WEAPON, gatekeeper.DEFENCE], seed=500)

        self.assertGreaterEqual(state["locks_open"], 1)
        self.assertTrue(state["committed"])
        self.assertIsNotNone(state["covered_answer"])
        self.assertNotEqual(state["covered_answer"], state["current_prediction"])

    def test_a_covered_answer_is_always_named_on_the_screen(self):
        """Hard is allowed; hidden is not. A counter nobody can read is a coin flip."""
        state = _feed_until_committed(_fight(), [gatekeeper.WEAPON, gatekeeper.DEFENCE])
        state = gatekeeper.take(state, _break_choice(state), seed=11)
        state = _feed_until_committed(state, [gatekeeper.WEAPON, gatekeeper.DEFENCE], seed=500)
        payload = gatekeeper.public(state)

        self.assertEqual(payload["covered"], state["covered_answer"])
        self.assertIn(payload["covered_label"], gatekeeper.CATEGORY_LABELS.values())
        self.assertEqual(payload["prediction"], state["current_prediction"])
        self.assertGreater(payload["confidence"], 0)
        self.assertEqual(payload["confidence_band"], gatekeeper.BAND_COMMITTED)

    def test_walking_into_the_covered_answer_costs_but_never_a_lock(self):
        state = _feed_until_committed(_fight(), [gatekeeper.WEAPON, gatekeeper.DEFENCE])
        state = gatekeeper.take(state, _break_choice(state), seed=11)
        state = _feed_until_committed(state, [gatekeeper.WEAPON, gatekeeper.DEFENCE], seed=500)
        covered = state["covered_answer"]
        if covered not in _legal(state):
            self.skipTest("this turn does not offer the covered answer")

        after = gatekeeper.take(state, covered, seed=17)

        self.assertEqual(after["locks_open"], state["locks_open"])
        self.assertTrue(any("перекрыто" in line for line in after["log"]), after["log"])


class GatekeeperFeintTests(unittest.TestCase):
    def test_false_step_only_appears_near_the_limit(self):
        state = _fight()
        self.assertNotIn(gatekeeper.FALSE_STEP, _legal(state))
        state["step_counter"] = state["step_limit"] - 1
        self.assertIn(gatekeeper.FALSE_STEP, _legal(state))

    def test_the_first_feint_works_and_the_third_is_simply_seen(self):
        """One trick cannot carry a fight, so this one wears out in three uses."""
        state = _fight()
        state["step_counter"] = state["step_limit"] - 1
        first = gatekeeper.take(state, gatekeeper.FALSE_STEP, seed=3)
        self.assertEqual(first["locks_open"], 1)
        self.assertEqual(first["false_step_adaptation"], 1)

        first["step_counter"] = first["step_limit"] - 1
        second = gatekeeper.take(first, gatekeeper.FALSE_STEP, seed=4)
        self.assertEqual(second["locks_open"], 2)
        self.assertTrue(any("проверяет" in line for line in second["log"]), second["log"])

        second["step_counter"] = second["step_limit"] - 1
        third = gatekeeper.take(second, gatekeeper.FALSE_STEP, seed=5)
        self.assertEqual(third["locks_open"], 2)
        self.assertTrue(any("уловку" in line for line in third["log"]), third["log"])

    def test_a_real_last_step_is_punished_instead(self):
        state = _fight()
        state["step_counter"] = state["step_limit"] - 1
        before = state["hero_hp"]

        after = gatekeeper.take(state, gatekeeper.MOVEMENT, seed=6)

        self.assertEqual(after["locks_open"], 0)
        self.assertEqual(after["step_counter"], 0)
        self.assertLess(after["hero_hp"], before)
        self.assertEqual(after["mistakes"], 1)

    def test_the_step_clock_warns_before_it_matters(self):
        state = _fight()
        state["step_counter"] = state["step_limit"] - 1
        self.assertIn("рассчитывает", gatekeeper.public(state)["step_hint"])


class GatekeeperEscalationTests(unittest.TestCase):
    def test_each_lock_turns_more_attention_to_sequences(self):
        """The third lock is fought against a machine reading two moves of context."""
        closed = gatekeeper._model_weights({"locks_open": 0})
        one = gatekeeper._model_weights({"locks_open": 1})
        two = gatekeeper._model_weights({"locks_open": 2})

        self.assertEqual(closed["order2"], 0.0)
        self.assertGreater(one["order2"], 0.0)
        self.assertGreater(two["order2"], one["order2"])
        self.assertGreater(two["order1"], closed["order1"])

    def test_emergency_mode_reads_deeper_and_forgets_slower(self):
        ordinary = gatekeeper._model_weights({"locks_open": 1})
        emergency = gatekeeper._model_weights({"locks_open": 1, "is_emergency_mode": True})

        self.assertGreater(emergency["order1"], ordinary["order1"])
        self.assertGreater(emergency["order2"], ordinary["order2"])
        self.assertGreater(gatekeeper.EMERGENCY_TENDENCY_DECAY, gatekeeper.TENDENCY_DECAY)

    def test_emergency_mode_wears_a_trick_out_twice_as_fast(self):
        state = _fight()
        state["is_emergency_mode"] = True
        state["step_counter"] = state["step_limit"] - 1

        after = gatekeeper.take(state, gatekeeper.FALSE_STEP, seed=8)

        self.assertEqual(after["false_step_adaptation"], 2)
        self.assertEqual(after["locks_open"], 2)

    def test_a_damage_window_resets_the_locks_but_not_the_lessons(self):
        """Every cycle starts further along than the last one did."""
        state = _feed(_fight(), [gatekeeper.WEAPON, gatekeeper.DEFENCE] * 3)
        state["is_core_open"] = True
        state["locks_open"] = state["locks_total"]
        learned = dict(state["transitions"])
        self.assertTrue(learned)

        after = gatekeeper.take(state, gatekeeper.CORE_WEAPON, seed=9)

        self.assertEqual(after["locks_open"], 0)
        self.assertEqual(after["locks_opened_by"], [])
        self.assertIsNone(after["current_prediction"])
        # Halved, not wiped.
        self.assertTrue(after["transitions"])
        self.assertLess(sum(after["transitions"].values()), sum(learned.values()))
        self.assertGreater(sum(after["tendency"].values()), 0)


class GatekeeperFairnessTests(unittest.TestCase):
    def test_a_turn_always_leaves_at_least_one_sane_answer(self):
        """Very bad choices are allowed. No choice at all is not.

        Played across many seeds and many fed patterns, because the state this guards
        against is the rare one where a prediction and a covered answer between them cover
        everything an attack leaves standing.
        """
        patterns = (
            [gatekeeper.WEAPON, gatekeeper.DEFENCE],
            [gatekeeper.MAGIC, gatekeeper.MOVEMENT],
            [gatekeeper.WEAPON, gatekeeper.MAGIC, gatekeeper.DEFENCE],
            [gatekeeper.DEFENCE],
        )
        for seed in range(40):
            rng = random.Random(seed)
            pattern = patterns[seed % len(patterns)]
            state = _fight(seed=seed)
            for turn in range(30):
                if gatekeeper.is_over(state):
                    break
                legal = _legal(state)
                self.assertTrue(legal, "a turn with no buttons at all")
                if not state.get("is_core_open"):
                    blocked = {
                        value for value in
                        (state["current_prediction"] if state.get("committed") else None,
                         state.get("covered_answer"))
                        if value
                    }
                    sane = gatekeeper._reasonable_answers(
                        state, state["current_boss_action"],
                    ) - blocked
                    self.assertTrue(
                        sane or gatekeeper.FALSE_STEP in legal,
                        f"seed {seed} turn {turn}: every sane answer was covered",
                    )
                wanted = pattern[turn % len(pattern)]
                choice = wanted if wanted in legal else rng.choice(sorted(legal))
                state = gatekeeper.take(state, choice, seed=rng.randrange(10 ** 6))

    def test_an_exhausted_trick_is_announced_rather_than_discovered(self):
        state = _fight()
        self.assertEqual(gatekeeper.public(state)["trick_hint"], "")
        state["locks_opened_by"] = [gatekeeper.MOVEMENT_BREAK] * gatekeeper.LOCK_METHOD_LIMIT
        self.assertTrue(gatekeeper.public(state)["trick_hint"])

    def test_the_committed_line_states_both_halves_of_the_stake(self):
        """A player who only knows the reward feeds the pattern one turn too long."""
        state = _feed_until_committed(_fight(), [gatekeeper.WEAPON, gatekeeper.DEFENCE])
        state["locks_open"] = 1
        hint = gatekeeper.public(state)["prediction_hint"]

        self.assertIn("откроется замок", hint)
        self.assertIn("закроется", hint)

    def test_careless_variety_stops_being_a_solution(self):
        """The whole point of the rework, measured the way the old fight was measured.

        A player who only avoids repeating themselves is now feeding the machine the
        cleanest pattern there is, and loses. Nothing here says the fight must be won by
        any particular policy -- only that THIS one, which used to win every time, no
        longer does.
        """
        wins = 0
        for seed in range(30):
            rng = random.Random(seed)
            state = _fight(hero_hp=2_600, damage=420, spell_power=400,
                           boss_hp=5_200, boss_damage=210, seed=seed)
            last = None
            for _ in range(200):
                if gatekeeper.is_over(state):
                    break
                legal = sorted(_legal(state))
                if state.get("is_core_open"):
                    choice = gatekeeper.CORE_MAGIC
                else:
                    options = [code for code in legal
                               if code != last and code != gatekeeper.FALSE_STEP]
                    choice = rng.choice(options or legal)
                state = gatekeeper.take(state, choice, seed=rng.randrange(10 ** 6))
                last = choice if choice in gatekeeper.CATEGORIES else gatekeeper.MOVEMENT
            wins += int(str(state.get("status")) == gatekeeper.VICTORY)
        self.assertLessEqual(wins, 6, f"never-repeat won {wins}/30 fights")


class GatekeeperPayloadTests(unittest.TestCase):
    def test_intro_has_configurable_locks_steps_and_contextual_actions(self):
        state = _fight(locks_total=4, step_limit=6)
        payload = gatekeeper.public(state)

        self.assertEqual(payload["locks_total"], 4)
        self.assertEqual(payload["step_limit"], 6)
        self.assertEqual(payload["locks"], [False] * 4)
        self.assertEqual(payload["steps"], [False] * 6)
        self.assertFalse(payload["over"])
        self.assertTrue(payload["telegraph"])
        self.assertTrue(payload["actions"])

    def test_the_payload_shows_the_working_beside_the_conclusion(self):
        """Both, because the conclusion is only trustworthy if the working is checkable."""
        state = _feed(_fight(), [gatekeeper.WEAPON, gatekeeper.DEFENCE] * 2)
        payload = gatekeeper.public(state)

        self.assertEqual(payload["observed"][-4:], [gatekeeper.WEAPON, gatekeeper.DEFENCE] * 2)
        self.assertEqual(payload["observed_icons"][-1], gatekeeper.CATEGORY_ICONS[gatekeeper.DEFENCE])
        self.assertEqual(payload["prediction"], state["current_prediction"])
        self.assertIn(payload["confidence_band"],
                      (gatekeeper.BAND_WATCHING, gatekeeper.BAND_TRACKING, gatekeeper.BAND_COMMITTED))

    def test_closed_armour_takes_chip_damage_and_magic_passes_more(self):
        state = _fight()
        state["current_boss_action"] = "sweep"
        by_weapon = gatekeeper.take(state, gatekeeper.WEAPON, seed=2)
        by_magic = gatekeeper.take(state, gatekeeper.MAGIC, seed=2)

        self.assertLess(by_weapon["boss_hp"], state["boss_hp"])
        self.assertLess(by_magic["boss_hp"], by_weapon["boss_hp"])

    def test_core_uses_real_power_but_caps_one_damage_window(self):
        state = _fight(damage=10 ** 6, spell_power=10 ** 6)
        state["is_core_open"] = True
        cap = round(state["boss_max_hp"] * gatekeeper.CORE_DAMAGE_CAP_SHARE)

        after = gatekeeper.take(state, gatekeeper.CORE_WEAPON, seed=4)

        self.assertEqual(state["boss_hp"] - after["boss_hp"], cap)
        self.assertEqual(after["cores_struck"], 1)

    def test_weighted_attacks_react_to_a_player_who_always_blocks(self):
        """Leanings, never guaranteed counters -- a hard counter hands over the script."""
        blocker = _fight()
        blocker["player_action_history"] = [gatekeeper.DEFENCE] * 5
        neutral = _fight()
        neutral["player_action_history"] = [gatekeeper.MOVEMENT] * 5

        blocker_breakers = sum(
            gatekeeper._pick_attack(blocker, random.Random(seed)) == "shield_breaker"
            for seed in range(300)
        )
        neutral_breakers = sum(
            gatekeeper._pick_attack(neutral, random.Random(seed)) == "shield_breaker"
            for seed in range(300)
        )
        self.assertGreater(blocker_breakers, neutral_breakers * 2)
        self.assertLess(blocker_breakers, 300, "a guaranteed counter is a script to drive")

    def test_state_round_trips_through_json(self):
        state = gatekeeper.take(_fight(), gatekeeper.WEAPON, seed=20)
        self.assertEqual(json.loads(json.dumps(state, ensure_ascii=False)), state)

    def test_a_fight_saved_by_the_old_engine_keeps_working(self):
        """A run mid-fight when this shipped must not crash on the next button press."""
        state = _fight()
        for key in ("tendency", "transitions", "pair_transitions", "covered_answer",
                    "committed", "confidence", "locks_opened_by", "false_step_adaptation"):
            state.pop(key, None)
        state["adaptation"] = {gatekeeper.WEAPON: 2.0}
        state["secondary_prediction"] = gatekeeper.MAGIC

        after = gatekeeper.take(state, gatekeeper.WEAPON, seed=21)

        self.assertEqual(after["status"], gatekeeper.ACTIVE)
        self.assertTrue(gatekeeper.public(after)["actions"])


if __name__ == "__main__":
    unittest.main()
