"""A mark an admin hands out by name, and the one condition that lifts it.

The rules this pins down are the ones that make the feature safe to hand to an admin at
all: it can only ever weaken somebody by the stated amount, it reaches the fight and the
matchmaking rating through the same single choke point every screen already reads, it
explains itself everywhere it appears, and -- the whole design -- it comes off by itself
when the player changes the picture it was given for. Nobody has to remember to lift it,
which is what stops it becoming a permanent punishment by neglect.

Fixture copied from tests/test_pets_birthday.py, which copied it from tests/test_pets.py,
so storage setup matches the rest of the suite.
"""

import tempfile
import unittest
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

    def _grow(self, entry, uid, level=30):
        """A developed creature, because a fresh one cannot show a percentage.

        A brand-new pet has every stat at 2, and 5% of 2 rounds back to 2 -- the game
        stores whole stat points and has nowhere to put a tenth of one. That is the honest
        behaviour of a small percentage on small numbers rather than a bug, and
        test_a_tiny_creature_rounds_the_penalty_away pins it down deliberately. Everything
        else here needs numbers big enough for 5% to exist.
        """
        data = pets._load(entry)
        record = pets._tamed_record(data, uid)
        record["stats"] = {key: level for key in C.STAT_KEYS}
        pets._save(entry, data)


class DebuffTests(PetsTestCase):
    ENTRY = "chat"
    CODE = "impostor"

    def setUp(self):
        super().setUp()
        for uid in ("1", "2"):
            self._tame(self.ENTRY, uid)
            self._grow(self.ENTRY, uid)

    def test_the_mark_takes_the_stated_percentage_off_every_stat_including_armour(self):
        before = pets.effective_stats(self.ENTRY, "1")
        pets.set_debuff(self.ENTRY, "1", self.CODE, set_by="admin")
        after = pets.effective_stats(self.ENTRY, "1")

        scale = C.DEBUFFS[self.CODE]["scale"]
        for key in C.STAT_KEYS:
            with self.subTest(stat=key):
                self.assertEqual(after[key], max(1, round(before[key] * scale)))
        # Armour floors at zero rather than one -- a creature wearing nothing has none,
        # and the mark must not conjure a point of it into existence.
        self.assertEqual(after["armor"], max(0, round(before["armor"] * scale)))
        # Some stat has to have actually moved, or the assertion above is vacuous on a
        # fresh pet whose numbers are small enough to round back to themselves.
        self.assertLess(sum(after.values()), sum(before.values()))

    def test_a_mark_can_never_wipe_a_creature_out(self):
        """The floor is a hard limit on the whole feature, not on one entry in the table.

        Every shipped debuff is mild, so nothing exercises this today -- which is exactly
        why it is worth a test: the next one added will be written by whoever wants it to
        sting, and a typo'd scale must not be able to delete somebody's creature.
        """
        with patch.dict(C.DEBUFFS, {self.CODE: {**C.DEBUFFS[self.CODE], "scale": -5.0}}):
            pets.set_debuff(self.ENTRY, "1", self.CODE)
            record = pets._tamed_record(pets._load(self.ENTRY), "1")
            self.assertEqual(pets.debuff_scale(record), C.DEBUFF_STAT_SCALE_FLOOR)
            stats = pets.effective_stats(self.ENTRY, "1")
            self.assertTrue(all(stats[key] >= 1 for key in C.STAT_KEYS))
            self.assertGreaterEqual(stats["armor"], 0)

    def test_the_power_rating_drops_with_the_stats_so_matchmaking_sees_it(self):
        before = pets.power_rating(self.ENTRY, "1")
        pets.set_debuff(self.ENTRY, "1", self.CODE)
        self.assertLess(pets.power_rating(self.ENTRY, "1"), before)

    def test_changing_the_picture_lifts_the_mark_without_anybody_removing_it(self):
        pets.set_debuff(self.ENTRY, "1", self.CODE)
        weakened = pets.effective_stats(self.ENTRY, "1")
        self.assertIsNotNone(pets.debuff(self.ENTRY, "1"))

        self.assertTrue(pets.set_photo(self.ENTRY, "1", "a-freshly-painted-one")[0])

        self.assertIsNone(pets.debuff(self.ENTRY, "1"))
        restored = pets.effective_stats(self.ENTRY, "1")
        self.assertGreater(sum(restored.values()), sum(weakened.values()))

    def test_putting_the_old_picture_back_does_not_bring_the_mark_back(self):
        """The row stays behind as a record, so the comparison has to be against the
        picture it was GIVEN for -- but a player who changed the picture has already met
        the condition, and switching to a third one must not re-arm anything."""
        original = pets._tamed_record(pets._load(self.ENTRY), "1")["photo_file_id"]
        pets.set_debuff(self.ENTRY, "1", self.CODE)
        pets.set_photo(self.ENTRY, "1", "something-else")
        self.assertIsNone(pets.debuff(self.ENTRY, "1"))
        # Re-uploading the exact same file id is the one way back in, and it is honest:
        # that is literally the picture the admin objected to.
        pets.set_photo(self.ENTRY, "1", original)
        self.assertIsNotNone(pets.debuff(self.ENTRY, "1"))

    def test_regranting_the_same_mark_re_arms_it_against_the_current_picture(self):
        pets.set_debuff(self.ENTRY, "1", self.CODE)
        pets.set_photo(self.ENTRY, "1", "still-not-painted")
        self.assertIsNone(pets.debuff(self.ENTRY, "1"))

        pets.set_debuff(self.ENTRY, "1", self.CODE)
        self.assertIsNotNone(pets.debuff(self.ENTRY, "1"))

    def test_a_creature_with_no_picture_at_all_stays_marked(self):
        data = pets._load(self.ENTRY)
        pets._tamed_record(data, "1")["photo_file_id"] = None
        pets._save(self.ENTRY, data)
        pets.set_debuff(self.ENTRY, "1", self.CODE)
        self.assertIsNotNone(pets.debuff(self.ENTRY, "1"))

    def test_clearing_by_hand_works_and_reports_whether_there_was_anything_to_clear(self):
        self.assertFalse(pets.clear_debuff(self.ENTRY, "1"))
        pets.set_debuff(self.ENTRY, "1", self.CODE)
        self.assertTrue(pets.clear_debuff(self.ENTRY, "1"))
        self.assertIsNone(pets.debuff(self.ENTRY, "1"))
        self.assertEqual(pets.effective_stats(self.ENTRY, "1"),
                         pets.effective_stats(self.ENTRY, "2"))

    def test_an_unknown_code_is_refused_and_an_unknown_player_too(self):
        with self.assertRaises(ValueError):
            pets.set_debuff(self.ENTRY, "1", "no-such-mark")
        with self.assertRaises(ValueError):
            pets.set_debuff(self.ENTRY, "999", self.CODE)
        with self.assertRaises(ValueError):
            pets.set_debuff(self.ENTRY, "", self.CODE)

    def test_a_mark_whose_definition_disappeared_simply_stops_applying(self):
        """A save written by a newer build must never be able to break a fight."""
        pets.set_debuff(self.ENTRY, "1", self.CODE)
        data = pets._load(self.ENTRY)
        pets._tamed_record(data, "1")[pets.DEBUFF_KEY]["code"] = "retired_mark"
        pets._save(self.ENTRY, data)
        self.assertIsNone(pets.debuff(self.ENTRY, "1"))
        self.assertEqual(pets.effective_stats(self.ENTRY, "1"),
                         pets.effective_stats(self.ENTRY, "2"))

    def test_a_tiny_creature_rounds_the_penalty_away(self):
        """Documented, not accidental: stats are whole numbers, so 5% of a stat of 2 has
        nowhere to go. The mark therefore costs a developed creature real points and a
        just-tamed one nothing, which is the right way round for a punishment to scale."""
        self._tame(self.ENTRY, "3")
        before = pets.effective_stats(self.ENTRY, "3")
        pets.set_debuff(self.ENTRY, "3", self.CODE)
        self.assertEqual(pets.effective_stats(self.ENTRY, "3"), before)

    def test_only_the_marked_player_is_affected(self):
        clean = pets.effective_stats(self.ENTRY, "2")
        pets.set_debuff(self.ENTRY, "1", self.CODE)
        self.assertEqual(pets.effective_stats(self.ENTRY, "2"), clean)
        self.assertEqual([row["user_id"] for row in pets.debuff_holders(self.ENTRY)], ["1"])


class DebuffCopyTests(PetsTestCase):
    """Wherever the mark shows up, it has to say what it costs and how to be rid of it.

    A −5% with no explanation attached is indistinguishable from the game miscounting, so
    "is the description present" is a correctness test rather than a style preference.
    """

    ENTRY = "chat"
    CODE = "impostor"

    def setUp(self):
        super().setUp()
        for uid in ("1", "2"):
            self._tame(self.ENTRY, uid)
            self._grow(self.ENTRY, uid)
        pets.set_debuff(self.ENTRY, "1", self.CODE)
        self.mark = C.DEBUFFS[self.CODE]

    def _assert_explained(self, text, *, hint=True):
        self.assertIn(self.mark["title"], text)
        self.assertIn(self.mark["line"], text)
        self.assertIn(self.mark["description"], text)
        if hint:
            self.assertIn(self.mark["hint"], text)

    def test_the_owners_own_pet_card_explains_it(self):
        card = pets_ui.pet_card(self.ENTRY, "1", pets.get_pet(self.ENTRY, "1"))
        self._assert_explained(card)

    def test_the_arena_screen_explains_it(self):
        text, _ = pets_ui.fight_view(self.ENTRY, "1", 0)
        self._assert_explained(text)

    def test_the_opponent_card_names_it_when_somebody_else_is_marked(self):
        """The stats printed there are the reduced ones, so it says why -- but not how
        the other person gets rid of it, which is none of the attacker's business."""
        text, _ = pets_ui.opponent_view(self.ENTRY, "2", "1", 0)
        self._assert_explained(text, hint=False)

    def test_an_unmarked_players_screens_say_nothing_about_it(self):
        card = pets_ui.pet_card(self.ENTRY, "2", pets.get_pet(self.ENTRY, "2"))
        text, _ = pets_ui.fight_view(self.ENTRY, "2", 0)
        for rendered in (card, text):
            self.assertNotIn(self.mark["title"], rendered)
            self.assertNotIn(self.mark["description"], rendered)

    def test_the_public_shape_carries_every_line_a_screen_needs(self):
        mark = pets.debuff(self.ENTRY, "1")
        for key in ("emoji", "title", "line", "description", "hint", "percent"):
            with self.subTest(field=key):
                self.assertTrue(mark[key], key)
        self.assertEqual(mark["percent"], 5)


if __name__ == "__main__":
    unittest.main()
