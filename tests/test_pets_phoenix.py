import json
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pets_phoenix as phoenix


# The Phoenix is a fight the player is supposed to LEARN, so the right answer to every
# telegraph is part of the module's contract rather than an implementation detail. This
# table is the crib sheet a player builds in their head over a few attempts; if a move's
# best answer ever changes, this is the line that has to change with it.
BEST_ANSWER = {
    "wave": phoenix.SHIELD,
    "gather": phoenix.ATTACK,
    "dive": phoenix.DODGE,
    "stance": phoenix.MAGIC,
    "white_flame": phoenix.DODGE,
    "devour": phoenix.ATTACK,
    "vanish": phoenix.SHIELD,
    "final_sun": phoenix.ATTACK,
    "core_vulnerable": phoenix.ATTACK,
    "core_burst": phoenix.SHIELD,
}


def hero(**overrides) -> dict:
    """A pet of roughly the shape that reaches the Phoenix's floor."""
    base = {
        "name": "Тестовый", "max_hp": 1400, "damage": 150, "spell_power": 160,
        "crit": 0.0, "crit_power": 2.5, "reduction": 0.08, "guard": 0.40,
        "has_magic": True, "level": 30,
    }
    base.update(overrides)
    return base


def boss(**overrides) -> dict:
    """The floor-5 encounter's real numbers, read once out of pets.phoenix_boss_profile."""
    base = {"name": "Феникс пепельных залов", "max_hp": 2552, "damage": 296,
            "level": 13, "floor": 5}
    base.update(overrides)
    return base


def offered(state: dict) -> list[str]:
    return [row["code"] for row in phoenix.actions(state)]


def perfect_answer(state: dict) -> str:
    """What a player who has learned every telegraph presses."""
    codes = offered(state)
    if state["phase_state"] == phoenix.VULNERABLE:
        if phoenix.MAGIC in codes and state["hero"]["spell_power"] > state["hero"]["damage"]:
            return phoenix.MAGIC
        return phoenix.ATTACK
    code, side = state["attack"], state["side"]
    if code in ("wing", "double_wing"):
        # The sparks name the side that is about to burn, so the answer is the other one.
        danger = side if state["step"] == 1 else ("left" if side == "right" else "right")
        return phoenix.LEFT if danger == "right" else phoenix.RIGHT
    if code == "twins":
        # Ash rises beside the living bird and falls beside the copy.
        return phoenix.ATTACK_LEFT if side == "left" else phoenix.ATTACK_RIGHT
    want = BEST_ANSWER[code]
    return want if want in codes else (phoenix.SHIELD if phoenix.SHIELD in codes else codes[0])


def always_attacks(state: dict) -> str:
    """The player this rework exists to stop: one button, every turn."""
    codes = offered(state)
    for code in (phoenix.ATTACK, phoenix.ATTACK_LEFT, phoenix.ATTACK_RIGHT):
        if code in codes:
            return code
    return codes[0]


def play(hero_profile, boss_profile, policy, *, seed=1, limit=500):
    """Run a whole fight under one policy and hand back the state and the press count."""
    state = phoenix.start(hero_profile, boss_profile, seed=seed)
    presses = 0
    while not phoenix.is_over(state) and presses < limit:
        state = phoenix.take(state, policy(state), seed=seed * 1_000 + presses)
        presses += 1
    return state, presses


class PhoenixFightTests(unittest.TestCase):
    def test_a_player_who_reads_every_telegraph_wins_the_whole_fight(self):
        """The fight has to be beatable by KNOWLEDGE, or the telegraphs are decoration.

        Both lives, the rebirth between them and «Последнее Солнце» all have to be
        walkable end to end by somebody who answers each telegraph correctly -- otherwise
        the boss is just a stat check wearing prose.
        """
        for seed in range(1, 9):
            state, presses = play(hero(), boss(), perfect_answer, seed=seed)
            self.assertEqual(state["phase_state"], phoenix.VICTORY, seed)
            self.assertEqual(state["phase"], 2, seed)
            self.assertGreater(state["hero_hp"], 0, seed)
            # Long enough to be a boss, short enough to be a Telegram conversation.
            self.assertLess(presses, 40, seed)

    def test_pressing_attack_every_turn_loses_the_fight(self):
        """This is the whole point of the rework.

        A maxed pet used to beat every boss by holding one button. If ⚔️ every turn still
        wins here, nothing about the fight actually asks the player to read anything.
        """
        for seed in range(1, 9):
            state, _ = play(hero(), boss(), always_attacks, seed=seed)
            self.assertEqual(state["phase_state"], phoenix.DEFEAT, seed)
            self.assertGreater(state["boss_hp"], 0, seed)

    def test_one_mistake_leaves_a_healthy_hero_standing_and_three_kill_them(self):
        """The damage model's central promise, stated as a number.

        A boss that can kill from full health in one press teaches nothing -- the player
        never finds out which button was wrong. A boss that lets mistakes pile up for ever
        teaches nothing either. Пылающая защита answered with ⚔️ is the worst single
        answer in the fight, so it is the sharpest place to pin the rule.
        """
        state = phoenix.start(hero(), boss(), seed=7)
        state["phase_state"] = phoenix.TELEGRAPH
        survived = []
        for step in range(3):
            state["attack"], state["side"], state["step"] = "stance", "", 1
            state = phoenix.take(state, phoenix.ATTACK, seed=step)
            survived.append(state["hero_hp"])
            if phoenix.is_over(state):
                break
        self.assertGreater(survived[0], 0, "one mistake must never kill a healthy hero")
        self.assertGreater(survived[1], 0, "two mistakes are meant to be recoverable")
        self.assertEqual(state["phase_state"], phoenix.DEFEAT)
        self.assertEqual(state["hero_hp"], 0)

    def test_phase_one_overkill_is_discarded_and_the_second_life_always_starts(self):
        """Overkill must not buy anything, or the rebirth becomes a formality.

        Letting a huge killing blow spill into the second life would hand the whole
        interlude to whoever hits hardest, and the interlude is the part that has to be
        played rather than out-statted.
        """
        state = phoenix.start(hero(damage=99_999), boss(), seed=3)
        state.update({"phase_state": phoenix.VULNERABLE, "vulnerable": "full", "boss_hp": 1})
        state = phoenix.take(state, phoenix.ATTACK, seed=1)

        self.assertEqual(state["phase_state"], phoenix.REBIRTH)
        self.assertEqual(state["boss_hp"], 0)
        self.assertEqual(state["phase"], 1)

        for step in range(phoenix.REBIRTH_STEPS):
            state = phoenix.take(state, perfect_answer(state), seed=step)
        self.assertEqual(state["phase"], 2)
        self.assertEqual(state["phase_state"], phoenix.TELEGRAPH)
        # Even a flawless interlude leaves a real second bar to fight through.
        floor = phoenix.REBIRTH_FLOOR_SHARE * state["phase_2_max"]
        self.assertGreaterEqual(state["boss_hp"], floor)

    def test_no_single_exchange_removes_more_than_the_phase_cap(self):
        """Gear buys fewer windows, never zero of them.

        Without the cap a pet several floors above the Phoenix deletes a life in one press
        and never meets a single mechanic, which is the failure mode the whole encounter
        was written to avoid.
        """
        rng = random.Random(11)
        for seed in range(20):
            state = phoenix.start(hero(damage=5_000, spell_power=5_000, crit=1.0),
                                  boss(), seed=seed)
            presses = 0
            while not phoenix.is_over(state) and presses < 200:
                cap = round(state["boss_max_hp"] * phoenix.PHASE_DAMAGE_CAP_SHARE)
                before = state["boss_hp"]
                state = phoenix.take(state, rng.choice(offered(state)), seed=rng.randrange(10**6))
                self.assertLessEqual(max(0, before - state["boss_hp"]), cap)
                presses += 1

    def test_the_same_attack_is_never_telegraphed_twice_in_a_row(self):
        """A repeated telegraph makes a learned answer indistinguishable from a lucky one.

        The player is building a lookup table; the fight has to keep asking new questions
        for them to find out whether the table works.
        """
        for seed in range(12):
            state = phoenix.start(hero(), boss(), seed=seed)
            seen, presses = [], 0
            while not phoenix.is_over(state) and presses < 200:
                if state["phase_state"] in (phoenix.INTRO, phoenix.TELEGRAPH) and state["step"] == 1:
                    seen.append(state["attack"])
                state = phoenix.take(state, perfect_answer(state), seed=seed * 100 + presses)
                presses += 1
            self.assertGreater(len(seen), 4, seed)
            for first, second in zip(seen, seen[1:]):
                self.assertNotEqual(first, second, seen)

    def test_only_the_directional_attacks_offer_side_buttons(self):
        """← / → have to MEAN something the moment they appear.

        If every telegraph offered them the player would learn to treat them as a coin
        flip, and the one mechanic that is genuinely about reading a side would stop
        reading as one.
        """
        sided = {"wing", "double_wing"}
        for code, attack in phoenix._ATTACKS.items():
            buttons = set(attack["buttons"])
            has_sides = bool(buttons & {phoenix.LEFT, phoenix.RIGHT})
            self.assertEqual(has_sides, code in sided, code)

        looked_at = set()
        for seed in range(1, 9):
            state = phoenix.start(hero(), boss(), seed=seed)
            for press in range(200):
                if phoenix.is_over(state):
                    break
                if state["phase_state"] in (phoenix.INTRO, phoenix.TELEGRAPH):
                    codes = set(offered(state))
                    looked_at.add(state["attack"])
                    self.assertEqual(bool(codes & {phoenix.LEFT, phoenix.RIGHT}),
                                     state["attack"] in sided, state["attack"])
                state = phoenix.take(state, perfect_answer(state), seed=seed * 100 + press)
        self.assertIn("wing", looked_at)
        self.assertIn("double_wing", looked_at)

    def test_the_twin_illusion_asks_for_a_target_rather_than_a_manner(self):
        """Пепельные двойники is the one move where WHO you hit is the answer.

        A plain ⚔️ has no meaning when there are two silhouettes, so offering it would let
        the engine pick the target for the player.
        """
        codes = set(phoenix._ATTACKS["twins"]["buttons"])
        self.assertEqual(codes, {phoenix.ATTACK_LEFT, phoenix.ATTACK_RIGHT, phoenix.MAGIC})
        self.assertNotIn(phoenix.ATTACK, codes)

    def test_take_rejects_an_action_that_is_not_on_offer_and_leaves_the_state_alone(self):
        """A saved fight is replayed from a button press that may be minutes old.

        A stale or hand-typed callback must not be able to advance the fight, and a
        rejected press must not have half-applied itself before it was rejected.
        """
        state = phoenix.start(hero(), boss(), seed=2)
        state["attack"], state["side"], state["step"] = "wave", "", 1
        before = json.dumps(state, ensure_ascii=False, sort_keys=True)

        with self.assertRaises(ValueError):
            phoenix.take(state, phoenix.LEFT, seed=1)
        with self.assertRaises(ValueError):
            phoenix.take(state, "не-кнопка", seed=1)
        self.assertEqual(json.dumps(state, ensure_ascii=False, sort_keys=True), before)

        moved = phoenix.take(state, phoenix.SHIELD, seed=1)
        self.assertEqual(json.dumps(state, ensure_ascii=False, sort_keys=True), before)
        self.assertNotEqual(moved["actions_taken"], state["actions_taken"])

        finished = phoenix.start(hero(), boss(), seed=2)
        finished["phase_state"] = phoenix.VICTORY
        self.assertEqual(phoenix.actions(finished), ())
        with self.assertRaises(ValueError):
            phoenix.take(finished, phoenix.ATTACK, seed=1)

    def test_the_fight_state_survives_a_trip_through_the_save_file(self):
        """The whole fight lives in the pet's saved run between two button presses.

        Anything that is not str/int/float/bool/list/dict/None either fails to save or
        comes back as something else, and the fight would silently resume wrong.
        """
        rng = random.Random(13)
        state = phoenix.start(hero(), boss(), seed=6)
        for _ in range(60):
            self.assertEqual(json.loads(json.dumps(state)), state)
            if phoenix.is_over(state):
                break
            state = phoenix.take(state, rng.choice(offered(state)), seed=rng.randrange(10**6))
        self.assertEqual(json.loads(json.dumps(state)), state)
        # pets.py reads the hero's remaining health straight off the saved state.
        self.assertIsInstance(state["hero_hp"], int)

    def test_a_stronger_pet_finishes_the_same_fight_in_fewer_presses(self):
        """Gear must still be worth buying; it just cannot replace reading the telegraph.

        Nothing in the fight scales to the hero, so a bigger swing has to show up as a
        shorter fight -- while both pets are answering every telegraph correctly.
        """
        weak = hero(damage=60, spell_power=60)
        strong = hero(damage=220, spell_power=220)
        for seed in range(1, 6):
            _, slow = play(weak, boss(), perfect_answer, seed=seed)
            fast_state, fast = play(strong, boss(), perfect_answer, seed=seed)
            self.assertEqual(fast_state["phase_state"], phoenix.VICTORY, seed)
            self.assertLess(fast, slow, seed)

    def test_telegraphs_describe_the_boss_and_never_name_a_button(self):
        """A telegraph the player has to obey is a quiz; one they have to read is a fight.

        The prose says what the Phoenix is doing -- wings, ash, the colour of the flame --
        and the player works out the answer. The moment it names a button the encounter
        stops teaching anything.
        """
        forbidden = [label.split(" ", 1)[-1].lower() for label in phoenix.ACTION_LABELS.values()]
        forbidden += ["нажми", "жми", "кнопк", "выбери", "используй"]
        for code, attack in phoenix._ATTACKS.items():
            texts = [attack.get("telegraph") or ""]
            texts += list((attack.get("sides") or {}).values())
            texts += list((attack.get("sides_2") or {}).values())
            for text in texts:
                lowered = text.lower()
                for word in forbidden:
                    self.assertNotIn(word, lowered, f"{code}: {text}")

    def test_a_pet_without_scrolls_is_never_offered_a_spell(self):
        """✨ has to be a real option or no option at all.

        A pet with no scrolls has nothing to cast, so the button would be a dead end; every
        telegraph therefore keeps a non-magical answer that is at least safe.
        """
        for seed in range(1, 6):
            state = phoenix.start(hero(has_magic=False), boss(), seed=seed)
            for press in range(200):
                if phoenix.is_over(state):
                    break
                codes = offered(state)
                self.assertNotIn(phoenix.MAGIC, codes)
                self.assertTrue(codes, state["attack"])
                state = phoenix.take(state, perfect_answer(state), seed=seed * 100 + press)
            self.assertEqual(state["phase_state"], phoenix.VICTORY, seed)

    def test_burning_is_capped_and_a_clean_answer_puts_it_out(self):
        """Горение is the memory of past mistakes, which is why it must be clearable.

        Uncapped it would turn a bad opening into an unrecoverable fight; unclearable it
        would make playing well afterwards pointless.
        """
        state = phoenix.start(hero(), boss(), seed=4)
        state["phase_state"] = phoenix.TELEGRAPH
        for step in range(6):
            state["attack"], state["side"], state["step"] = "stance", "", 1
            state["hero_hp"] = state["hero_max_hp"]      # isolate the stacks from the damage
            state["mistake_streak"] = 0
            state = phoenix.take(state, phoenix.ATTACK, seed=step)
            self.assertLessEqual(state["burn"], phoenix.BURN_MAX_STACKS)
        self.assertEqual(state["burn"], phoenix.BURN_MAX_STACKS)

        state["phase_state"], state["attack"], state["step"] = phoenix.TELEGRAPH, "stance", 1
        state["hero_hp"] = state["hero_max_hp"]
        cleared = phoenix.take(state, phoenix.SHIELD, seed=1)
        self.assertLess(cleared["burn"], phoenix.BURN_MAX_STACKS)

    def test_public_answers_everything_a_screen_needs(self):
        """Both clients draw from this dict and nothing else.

        A client that worked out its own buttons would drift from the engine the first
        time a move changed, and the direction buttons are exactly where that breaks.
        """
        state = phoenix.start(hero(), boss(), seed=1)
        view = phoenix.public(state)
        self.assertEqual(
            set(view),
            {"boss_name", "phase", "phase_state", "boss_hp", "boss_max_hp", "hero_hp",
             "hero_max_hp", "burn", "telegraph", "scene", "log", "actions", "vulnerable",
             "over", "won"},
        )
        self.assertEqual(view["boss_name"], "Феникс пепельных залов")
        self.assertTrue(view["telegraph"])
        self.assertTrue(view["scene"])
        self.assertFalse(view["over"])
        self.assertEqual(view["actions"], [dict(row) for row in phoenix.actions(state)])

        reborn = play(hero(), boss(), perfect_answer, seed=1)[0]
        self.assertEqual(phoenix.public(reborn)["boss_name"], phoenix.PHASE_2_NAME)
        self.assertTrue(phoenix.public(reborn)["won"])
        self.assertEqual(phoenix.public(reborn)["telegraph"], "")


if __name__ == "__main__":
    unittest.main()
