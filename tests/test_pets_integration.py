"""The four pet modules composed, end to end.

Each of pets.py, pets_combat.py and pets_flavor.py is tested on its own elsewhere. This
file exists because those three were built independently against a written interface, and
a contract that every side honours in isolation can still leave a seam: a view that asks
for a key the store never writes, a fight result whose fields the recorder does not read,
a reward dict the report renders as "None". So this walks one member through the entire
game -- cage, taming, training, gear, a real fight, the history it leaves -- and renders
every screen at every step, asserting only what an integration test can usefully assert:
that nothing raises, that money actually moves, and that no screen shows a placeholder.

The one thing it does NOT do is re-test balance or storage semantics. Those belong to the
unit tests, and duplicating them here would mean two files to update for one decision.
"""

import random
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import economy
import pets
import pets_combat
import pets_config as C
import pets_ui
import stats

ENTRY = "chat"
ALICE = 111
BOB = 222
# Enough XP that economy.balance opens well above the whole catalogue, so a refusal in
# this file is always a real bug and never "the wallet happened to be empty".
RICH_XP = 400_000


class PetsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._dir = TemporaryDirectory()
        path = Path(self._dir.name)
        self._real_stats_dir = stats._stats_dir
        stats._stats_dir = lambda: path
        self.addCleanup(self._restore)

    def _restore(self):
        stats._stats_dir = self._real_stats_dir
        self._dir.cleanup()

    def _balance(self, user_id):
        return economy.balance(ENTRY, user_id, RICH_XP)

    def _found(self, user_id, name, owner):
        ok, note = pets.buy_cage(ENTRY, user_id, RICH_XP)
        self.assertTrue(ok, note)
        ok, note = pets.tame(ENTRY, user_id, RICH_XP, name, f"photo_{user_id}", owner)
        self.assertTrue(ok, note)

    def _render_every_screen(self, user_id):
        """Every view a player can reach, rendered. The assertions are deliberately weak --
        this is a "does it compose" check, and pinning wording here would make every copy
        edit break a test."""
        screens = [
            pets_ui.main_view(ENTRY, user_id, RICH_XP),
            pets_ui.cage_view(ENTRY, user_id, RICH_XP),
            pets_ui.train_view(ENTRY, user_id, RICH_XP),
            pets_ui.bag_view(ENTRY, user_id, RICH_XP),
            pets_ui.fight_view(ENTRY, user_id, RICH_XP),
            pets_ui.history_view(ENTRY, user_id),
            pets_ui.pet_view(ENTRY, user_id),
        ] + [
            pets_ui.slot_view(ENTRY, user_id, RICH_XP, slot) for slot in C.SLOT_KEYS
        ]
        for text, keyboard in screens:
            self.assertTrue(text.strip())
            # A view that leaked a Python None into the caption is the exact failure this
            # file is here to catch.
            self.assertNotIn("None", text)
            self.assertIn("inline_keyboard", keyboard)
            for row in keyboard["inline_keyboard"]:
                for button in row:
                    self.assertTrue(button.get("text"))
                    data = button.get("callback_data")
                    if data is not None:
                        # Telegram silently drops a button whose callback_data is over 64
                        # bytes, which would look like a dead button in production.
                        self.assertLessEqual(len(data.encode("utf-8")), pets_ui.MAX_CALLBACK_BYTES)
                        self.assertIsNotNone(pets_ui.parse_callback(data))
        return screens

    # ------------------------------------------------------------------ the walkthrough

    def test_a_member_can_play_the_whole_game(self):
        # Nothing yet: the menu must still render for somebody with no cage at all.
        self._render_every_screen(ALICE)
        self.assertIsNone(pets.get_pet(ENTRY, ALICE))

        opening = self._balance(ALICE)
        self._found(ALICE, "Кабанчик", "Alice")
        self.assertEqual(
            self._balance(ALICE), opening - C.CAGE_PRICE - C.TAME_PRICE,
            "founding a pet must debit exactly the cage plus the taming",
        )
        self._render_every_screen(ALICE)

        # Training: the wallet moves by the published price, not by something else.
        before = self._balance(ALICE)
        ok, note, spent = pets.upgrade_stat(ENTRY, ALICE, RICH_XP, "strength", times=10)
        self.assertTrue(ok, note)
        self.assertEqual(spent, C.total_stat_cost(11, 1))
        self.assertEqual(self._balance(ALICE), before - spent)
        self.assertEqual(pets.stat_level(ENTRY, ALICE, "strength"), 11)

        # Gear: buying, equipping, and the stat actually landing on the creature.
        bare = pets.effective_stats(ENTRY, ALICE)
        ok, note = pets.buy_item(ENTRY, ALICE, RICH_XP, "stick")
        self.assertTrue(ok, note)
        ok, note = pets.equip(ENTRY, ALICE, "stick")
        self.assertTrue(ok, note)
        armed = pets.effective_stats(ENTRY, ALICE)
        self.assertEqual(
            armed["strength"], bare["strength"] + C.find_item("stick").bonuses["strength"]
        )
        self._render_every_screen(ALICE)

    def test_a_fight_pays_out_and_shows_up_in_both_histories(self):
        self._found(ALICE, "Кабанчик", "Alice")
        self._found(BOB, "Тумблер", "Bob")

        self.assertEqual(pets.find_opponent(ENTRY, ALICE), str(BOB))
        left_before = pets.fights_left(ENTRY, ALICE, pets.today())

        result = pets_combat.simulate(
            _fighter(ALICE, "Кабанчик"), _fighter(BOB, "Тумблер"),
            rng=random.Random(7),
        )
        purse_before = self._balance(ALICE)
        reward = pets.record_fight(ENTRY, ALICE, BOB, result, pets.today())

        # The daily allowance is spent by the ATTACKER only.
        self.assertEqual(pets.fights_left(ENTRY, ALICE, pets.today()), left_before - 1)
        self.assertEqual(
            pets.fights_left(ENTRY, BOB, pets.today()),
            C.DAILY_FIGHTS + C.CAGE_BONUS_FIGHTS[0],
            "the defender did not choose this fight and must not be charged for it",
        )

        won = result.winner == str(ALICE)
        if won:
            self.assertGreaterEqual(reward["gold"], C.WIN_GOLD_MIN)
            self.assertLessEqual(reward["gold"], round(C.WIN_GOLD_MAX * 1.25))
            self.assertEqual(self._balance(ALICE), purse_before + reward["gold"])
        else:
            self.assertEqual(reward["gold"], C.LOSS_GOLD)
            self.assertEqual(self._balance(ALICE), purse_before)

        # The report renders, and reads from the winner's side either way.
        report = pets_ui.fight_report(
            result, str(ALICE), {str(ALICE): "Кабанчик", str(BOB): "Тумблер"}, reward,
        )
        self.assertIn("Победа" if won else "Поражение", report)
        self.assertNotIn("None", report)
        self.assertNotIn("{", report, "an unformatted flavour template reached the player")

        # One fight, two points of view.
        mine = pets.history(ENTRY, ALICE)
        theirs = pets.history(ENTRY, BOB)
        self.assertEqual(len(mine), 1)
        self.assertEqual(len(theirs), 1)
        self.assertEqual(str(mine[0]["attacker_id"]), str(ALICE))
        self.assertEqual(str(theirs[0]["attacker_id"]), str(ALICE))
        for text, _ in (pets_ui.history_view(ENTRY, ALICE), pets_ui.history_view(ENTRY, BOB)):
            self.assertNotIn("None", text)
            self.assertNotIn("?", text.replace("Боёв пока не было.", ""))

    def test_history_survives_a_rename(self):
        """The card shows the creature's name NOW; a fight that already happened keeps the
        name it was fought under. Snapshotting is the whole reason this is asserted."""
        self._found(ALICE, "Кабанчик", "Alice")
        self._found(BOB, "Тумблер", "Bob")
        result = pets_combat.simulate(
            _fighter(ALICE, "Кабанчик"), _fighter(BOB, "Тумблер"), rng=random.Random(3),
        )
        pets.record_fight(ENTRY, ALICE, BOB, result, pets.today())

        ok, note = pets.rename(ENTRY, ALICE, "Совершенно другое имя")
        self.assertTrue(ok, note)
        self.assertEqual(pets.history(ENTRY, ALICE)[0]["attacker_name"], "Кабанчик")
        self.assertIn("Совершенно другое имя", pets_ui.pet_card(ENTRY, ALICE, pets.get_pet(ENTRY, ALICE)))

    def test_the_daily_allowance_actually_runs_out(self):
        self._found(ALICE, "Кабанчик", "Alice")
        self._found(BOB, "Тумблер", "Bob")
        allowance = pets.fights_left(ENTRY, ALICE, pets.today())
        self.assertEqual(allowance, C.DAILY_FIGHTS + C.CAGE_BONUS_FIGHTS[0])

        for index in range(allowance):
            result = pets_combat.simulate(
                _fighter(ALICE, "Кабанчик"), _fighter(BOB, "Тумблер"),
                rng=random.Random(index),
            )
            pets.record_fight(ENTRY, ALICE, BOB, result, pets.today())
        self.assertEqual(pets.fights_left(ENTRY, ALICE, pets.today()), 0)

        # And the screen says so rather than offering a button that cannot work.
        text, keyboard = pets_ui.fight_view(ENTRY, ALICE, RICH_XP)
        actions = {
            pets_ui.parse_callback(b["callback_data"])[1]
            for row in keyboard["inline_keyboard"] for b in row if b.get("callback_data")
        }
        self.assertNotIn("search", actions)

        # History is capped at what the menu promises to show.
        self.assertLessEqual(len(pets.history(ENTRY, ALICE)), C.HISTORY_LIMIT)

    def test_a_pet_that_levels_up_gets_stronger_everywhere(self):
        """+1 to every stat per pet level is the strongest thing in the game, so it has to
        reach the fight, not just the card."""
        self._found(ALICE, "Кабанчик", "Alice")
        before = pets.effective_stats(ENTRY, ALICE)
        level, gained = pets.award_xp(ENTRY, ALICE, 50_000)
        self.assertGreater(gained, 0)
        after = pets.effective_stats(ENTRY, ALICE)
        for key in C.STAT_KEYS:
            self.assertEqual(after[key] - before[key], gained * C.PET_LEVEL_STAT_BONUS)

        fighter = _fighter(ALICE, "Кабанчик")
        derived = pets_combat.derive(fighter, fighter)
        self.assertGreater(derived["max_hp"], C.BASE_HP)


class FlavorGrammarTests(unittest.TestCase):
    """Russian numerals agree with the noun after them -- "92 очка", but "95 очков" -- and
    a template cannot know which it will get. Every damage figure in the bank therefore has
    to be followed by a word that does not decline ("урона", "HP") or by no noun at all.
    One batch of lines shipped with "{amount} очков здоровья" and rendered "92 очков"; this
    is here so the next batch cannot."""

    # Nouns that would have to agree with the number in front of them.
    COUNTABLE = ("очк", "единиц", "хитпоинт", "балл", "пункт")

    def test_no_damage_figure_is_followed_by_a_noun_that_must_agree(self):
        import re

        import pets_flavor

        for event, templates in pets_flavor.VARIANTS.items():
            for template in templates:
                for tail in re.findall(r"\{amount\}\s*(\w+)", template):
                    self.assertFalse(
                        tail.lower().startswith(self.COUNTABLE),
                        f"{event}: «{template}» -- «{tail}» must agree with the numeral",
                    )


def _fighter(user_id, name):
    effective = pets.effective_stats(ENTRY, user_id)
    return pets_combat.Fighter(
        key=str(user_id), name=name,
        strength=effective["strength"], health=effective["health"],
        agility=effective["agility"], luck=effective["luck"],
        armor=effective["armor"],
    )


if __name__ == "__main__":
    unittest.main()
