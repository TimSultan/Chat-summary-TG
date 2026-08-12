"""One person a day at the top of the arena, with a greeting where the attack button is.

The rules this pins down, all of them things a birthday must not become: a way to mint
gold twice from one tap, a celebration that outlives its day, or a reason somebody cannot
be congratulated because they were out of fights. The reward is a win's worth for BOTH
sides -- the greeter for showing up, the celebrant per well-wisher -- and it costs no
arena fight at all.

Fixture copied from tests/test_pets.py (PetsTestCase, `_tame`) rather than imported, the
same way tests/test_pets_pve.py does it, so storage setup matches the rest of the suite.
"""

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import economy
import pets
import pets_config as C
import pets_ui


class PetsTestCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def _tame(self, entry, uid, name=None):
        economy.grant(entry, uid, C.CAGE_PRICE + C.TAME_PRICE, "test")
        self.assertTrue(pets.buy_cage(entry, uid, 0)[0])
        self.assertTrue(pets.tame(entry, uid, 0, name or f"Пет{uid}", f"file{uid}", f"Хозяин{uid}")[0])


class BirthdayTests(PetsTestCase):
    ENTRY = "chat"

    def setUp(self):
        super().setUp()
        for uid in ("1", "2", "3"):
            self._tame(self.ENTRY, uid)

    def test_nobody_is_celebrating_until_an_admin_says_so(self):
        self.assertIsNone(pets.birthday(self.ENTRY))
        with self.assertRaises(ValueError):
            pets.congratulate(self.ENTRY, "1")

    def test_a_greeting_pays_a_win_to_both_sides_and_spends_no_arena_fight(self):
        pets.set_birthday(self.ENTRY, "2", set_by="1")
        fights_before = pets.fights_left(self.ENTRY, "1")
        greeter_before = economy.balance(self.ENTRY, "1", 0)
        celebrant_before = economy.balance(self.ENTRY, "2", 0)
        # Level, not the xp field: `xp` is progress WITHIN a level, so a win's worth of it
        # at level 1 shows up as a level-up with the remainder carried, not as +WIN_XP.
        greeter_level = pets.get_pet(self.ENTRY, "1")["level"]
        celebrant_level = pets.get_pet(self.ENTRY, "2")["level"]

        receipt = pets.congratulate(self.ENTRY, "1")

        self.assertFalse(receipt["already"])
        self.assertEqual(receipt["celebrant"], "2")
        for paid in (receipt["gold"], receipt["celebrant_gold"]):
            self.assertGreaterEqual(paid, C.WIN_GOLD_MIN)
            self.assertLessEqual(paid, C.WIN_GOLD_MAX)
        self.assertEqual(receipt["xp"], C.WIN_XP)
        self.assertEqual(receipt["celebrant_xp"], C.WIN_XP)

        self.assertEqual(economy.balance(self.ENTRY, "1", 0) - greeter_before, receipt["gold"])
        self.assertEqual(
            economy.balance(self.ENTRY, "2", 0) - celebrant_before, receipt["celebrant_gold"],
        )
        self.assertGreater(pets.get_pet(self.ENTRY, "1")["level"], greeter_level)
        self.assertGreater(pets.get_pet(self.ENTRY, "2")["level"], celebrant_level)
        # The whole point of it being free: an empty bank must never block a greeting.
        self.assertEqual(pets.fights_left(self.ENTRY, "1"), fights_before)

    def test_a_second_tap_returns_the_first_receipt_and_pays_nothing_more(self):
        """A double-click, a retried request and a stale tab all land here. Paying twice
        would make one birthday the cheapest gold in the game."""
        pets.set_birthday(self.ENTRY, "2", set_by="1")
        first = pets.congratulate(self.ENTRY, "1")
        greeter = economy.balance(self.ENTRY, "1", 0)
        celebrant = economy.balance(self.ENTRY, "2", 0)

        again = pets.congratulate(self.ENTRY, "1")

        self.assertTrue(again["already"])
        self.assertEqual(again["gold"], first["gold"])
        self.assertEqual(economy.balance(self.ENTRY, "1", 0), greeter)
        self.assertEqual(economy.balance(self.ENTRY, "2", 0), celebrant)
        self.assertEqual(pets.birthday(self.ENTRY)["greeted_count"], 1)

    def test_everybody_else_can_greet_and_the_celebrant_is_paid_per_well_wisher(self):
        pets.set_birthday(self.ENTRY, "2", set_by="1")
        before = economy.balance(self.ENTRY, "2", 0)
        paid = sum(pets.congratulate(self.ENTRY, uid)["celebrant_gold"] for uid in ("1", "3"))
        self.assertEqual(economy.balance(self.ENTRY, "2", 0) - before, paid)
        self.assertEqual(pets.birthday(self.ENTRY)["greeted_count"], 2)

    def test_the_celebrant_cannot_congratulate_themselves(self):
        pets.set_birthday(self.ENTRY, "2", set_by="1")
        before = economy.balance(self.ENTRY, "2", 0)
        with self.assertRaises(ValueError):
            pets.congratulate(self.ENTRY, "2")
        self.assertEqual(economy.balance(self.ENTRY, "2", 0), before)

    def test_the_celebration_retires_itself_the_next_day(self):
        """Dated rather than a switch: an admin sets it and forgets, and a forgotten flag
        must not keep paying a stale celebrant all week."""
        pets.set_birthday(self.ENTRY, "2", day=date(2026, 8, 13), set_by="1")
        self.assertIsNotNone(pets.birthday(self.ENTRY, date(2026, 8, 13)))
        self.assertIsNone(pets.birthday(self.ENTRY, date(2026, 8, 14)))
        with self.assertRaises(ValueError):
            pets.congratulate(self.ENTRY, "1", day=date(2026, 8, 14))

    def test_resetting_the_same_person_keeps_the_greetings_and_a_new_one_starts_over(self):
        pets.set_birthday(self.ENTRY, "2", set_by="1")
        pets.congratulate(self.ENTRY, "1")
        pets.set_birthday(self.ENTRY, "2", set_by="1")
        self.assertEqual(pets.birthday(self.ENTRY)["greeted_count"], 1)
        # A different celebrant is a different celebration, so the log starts empty --
        # and the person who already greeted today may greet the new one too.
        pets.set_birthday(self.ENTRY, "3", set_by="1")
        self.assertEqual(pets.birthday(self.ENTRY)["greeted_count"], 0)
        self.assertFalse(pets.congratulate(self.ENTRY, "1")["already"])

    def test_clearing_takes_the_celebration_down_and_stops_the_payouts(self):
        pets.set_birthday(self.ENTRY, "2", set_by="1")
        self.assertTrue(pets.clear_birthday(self.ENTRY))
        self.assertFalse(pets.clear_birthday(self.ENTRY))
        self.assertIsNone(pets.birthday(self.ENTRY))
        with self.assertRaises(ValueError):
            pets.congratulate(self.ENTRY, "1")

    def test_the_viewer_fields_say_who_is_looking(self):
        """Both clients render from these instead of working it out twice."""
        pets.set_birthday(self.ENTRY, "2", set_by="1")
        self.assertTrue(pets.birthday(self.ENTRY, viewer="2")["is_me"])
        self.assertFalse(pets.birthday(self.ENTRY, viewer="1")["is_me"])
        self.assertFalse(pets.birthday(self.ENTRY, viewer="1")["greeted"])
        pets.congratulate(self.ENTRY, "1")
        self.assertTrue(pets.birthday(self.ENTRY, viewer="1")["greeted"])
        self.assertFalse(pets.birthday(self.ENTRY, viewer="3")["greeted"])

    def test_the_celebrant_gets_a_stored_copy_of_every_greeting(self):
        """The DM can fail -- a bot cannot write to somebody who never opened it -- so the
        news is banked before it is sent."""
        pets.set_birthday(self.ENTRY, "2", set_by="1")
        pets.congratulate(self.ENTRY, "1")
        rows = pets._load(self.ENTRY)["birthday_notifications"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user_id"], "2")
        self.assertEqual(rows[0]["greeter_name"], "Хозяин1")

    def test_a_greeter_without_a_creature_is_still_paid_gold(self):
        """Gold is the person's, xp is the creature's. Somebody who has not tamed one yet
        gets the coins rather than being turned away at the button."""
        pets.set_birthday(self.ENTRY, "2", set_by="1")
        before = economy.balance(self.ENTRY, "99", 0)
        receipt = pets.congratulate(self.ENTRY, "99")
        self.assertGreaterEqual(receipt["gold"], C.WIN_GOLD_MIN)
        self.assertEqual(receipt["xp"], 0)
        self.assertEqual(economy.balance(self.ENTRY, "99", 0) - before, receipt["gold"])


class BirthdayArenaScreenTests(PetsTestCase):
    ENTRY = "chat"

    def setUp(self):
        super().setUp()
        for uid in ("1", "2"):
            self._tame(self.ENTRY, uid)

    def test_the_arena_offers_the_greeting_and_then_stops_offering_it(self):
        pets.set_birthday(self.ENTRY, "2", set_by="1")
        text, keyboard = pets_ui.fight_view(self.ENTRY, "1", 0)
        self.assertIn("день рождения", text.lower())
        labels = [b["text"] for row in keyboard["inline_keyboard"] for b in row]
        self.assertTrue(any("Поздравить" in label for label in labels), labels)

        pets.congratulate(self.ENTRY, "1")
        text, keyboard = pets_ui.fight_view(self.ENTRY, "1", 0)
        labels = [b["text"] for row in keyboard["inline_keyboard"] for b in row]
        self.assertFalse(any("Поздравить" in label for label in labels), labels)
        self.assertIn("уже поздравил", text.lower())

    def test_the_celebrant_sees_a_tally_and_never_a_button_aimed_at_themselves(self):
        pets.set_birthday(self.ENTRY, "2", set_by="1")
        pets.congratulate(self.ENTRY, "1")
        text, keyboard = pets_ui.fight_view(self.ENTRY, "2", 0)
        labels = [b["text"] for row in keyboard["inline_keyboard"] for b in row]
        self.assertFalse(any("Поздравить" in label for label in labels), labels)
        self.assertIn("Тебя поздравили: 1", text)

    def test_an_empty_fight_bank_still_shows_the_greeting(self):
        """It costs no fight, so running out of them must not hide it."""
        pets.set_birthday(self.ENTRY, "2", set_by="1")
        data = pets._load(self.ENTRY)
        data["pets"]["1"]["fight_bank"] = 0
        pets._save(self.ENTRY, data)
        with patch.object(pets, "fights_left", return_value=0):
            _text, keyboard = pets_ui.fight_view(self.ENTRY, "1", 0)
        labels = [b["text"] for row in keyboard["inline_keyboard"] for b in row]
        self.assertTrue(any("Поздравить" in label for label in labels), labels)
        self.assertFalse(any("Найти соперника" in label for label in labels), labels)


if __name__ == "__main__":
    unittest.main()
