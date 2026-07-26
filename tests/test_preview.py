import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import preview
import tree


class PreviewRegistryTests(unittest.TestCase):
    def test_every_preview_renders(self):
        for preview_id in preview.preview_ids():
            with self.subTest(preview_id=preview_id):
                text = preview.render(preview_id, date(2026, 7, 26))
                self.assertTrue(text)

    def test_ids_are_unique_and_menu_safe(self):
        ids = preview.preview_ids()
        self.assertEqual(len(ids), len(set(ids)))
        for preview_id in ids:
            self.assertTrue(preview_id.isascii())
            self.assertNotIn(":", preview_id)  # would break parse_callback

    def test_group_test_id_is_not_also_a_dm_preview(self):
        # It posts to the chat rather than rendering into the DM, so a caller that looked
        # it up in PREVIEWS and sent the result would silently skip the group entirely.
        self.assertIsNone(preview.render(preview.GROUP_TEST_ID))
        self.assertNotIn(preview.GROUP_TEST_ID, preview.preview_ids())
        self.assertEqual(preview.title_for(preview.GROUP_TEST_ID), preview.GROUP_TEST_TITLE)

    def test_unknown_id_renders_nothing(self):
        self.assertIsNone(preview.render("nope"))
        self.assertIsNone(preview.title_for("nope"))

    def test_callback_data_fits_telegram_limit(self):
        for preview_id in preview.preview_ids() + (preview.GROUP_TEST_ID,):
            data = preview.callback_data(preview_id)
            self.assertLessEqual(len(data.encode()), 64)
            self.assertEqual(preview.parse_callback(data), preview_id)

    def test_menu_lists_every_preview_plus_the_group_test(self):
        text, keyboard = preview.menu_view()
        self.assertIn("Предпросмотр", text)
        rows = keyboard["inline_keyboard"]
        self.assertEqual(len(rows), len(preview.PREVIEWS) + 1)
        data = {row[0]["callback_data"] for row in rows}
        self.assertEqual(
            data,
            {preview.callback_data(i) for i in preview.preview_ids()}
            | {preview.callback_data(preview.GROUP_TEST_ID)},
        )

    def test_only_the_invitation_carries_the_planting_button(self):
        with_button = {i for i in preview.preview_ids() if preview.keyboard_for(i)}
        self.assertEqual(with_button, {"seed", "seedtoday"})

    def test_sample_button_never_carries_the_send_to_chat_payload(self):
        # The two buttons look identical on screen. Sharing a payload meant tapping the
        # button on a DM sample, to see what it does, posted to the 190-member chat.
        keyboard = preview.keyboard_for("seed")
        data = keyboard["inline_keyboard"][0][0]["callback_data"]
        self.assertEqual(data, preview.SAMPLE_CALLBACK)
        self.assertNotEqual(data, preview.callback_data(preview.GROUP_TEST_ID))
        self.assertEqual(preview.parse_callback(data), preview.SAMPLE_BUTTON_ID)

    def test_the_sample_button_id_is_not_a_menu_action(self):
        self.assertNotIn(preview.SAMPLE_BUTTON_ID, preview.preview_ids())
        self.assertIsNone(preview.render(preview.SAMPLE_BUTTON_ID))


class DeleteCallbackTests(unittest.TestCase):
    def test_round_trip(self):
        _, keyboard = preview.group_test_sent_view(-1001234567890, 4242)
        data = keyboard["inline_keyboard"][0][0]["callback_data"]
        self.assertLessEqual(len(data.encode()), 64)
        self.assertEqual(preview.parse_delete_callback(data), (-1001234567890, 4242))

    def test_a_plain_preview_button_is_not_a_delete(self):
        self.assertIsNone(preview.parse_delete_callback(preview.callback_data("seed")))

    def test_a_delete_button_is_not_a_plain_preview(self):
        _, keyboard = preview.group_test_sent_view(-100123, 7)
        data = keyboard["inline_keyboard"][0][0]["callback_data"]
        self.assertIsNone(preview.parse_callback(data))

    def test_garbage_is_rejected(self):
        for data in ("", "prev:del:abc:1", "prev:del:1", "other:del:1:2"):
            self.assertIsNone(preview.parse_delete_callback(data))


class CeremonyMessageTests(unittest.TestCase):
    def test_invitation_asks_for_the_button_not_a_reaction(self):
        text = tree.format_seed_ceremony_message()
        self.assertIn("кнопку", text)
        # 🎄 was the only tree in Telegram's reaction set; the button replaced it.
        self.assertNotIn("🎄", text)

    def test_invitation_says_tomorrow_by_default_and_today_when_asked(self):
        self.assertIn("Завтра в 10:00", tree.format_seed_ceremony_message())
        self.assertIn("Сегодня в 10:00", tree.format_seed_ceremony_message(same_day=True))

    def test_invitation_reveals_no_height_stage_or_deadline(self):
        text = tree.format_seed_ceremony_message()
        for leak in ("мм", "см", "года", "стади"):
            self.assertNotIn(leak, text)

    def test_roll_call_uses_handles_where_there_are_any(self):
        text = tree.format_planting_roll_call([("Дзура", "dzura"), ("Мария", None)])
        self.assertIn("@dzura", text)
        self.assertIn("Мария", text)
        self.assertNotIn("@Мария", text)

    def test_roll_call_escapes_display_names(self):
        text = tree.format_planting_roll_call([("<b>Аня</b>", None)])
        self.assertIn("&lt;b&gt;", text)
        self.assertNotIn("<b>Аня", text)

    def test_roll_call_carries_the_planting_advice_not_the_rotation(self):
        text = tree.format_planting_roll_call([("Аня", None)])
        self.assertIn("уходит в корни", text)
        self.assertNotIn(tree.advice_for(date(2026, 7, 26)), text)

    def test_roll_call_counts_people_in_russian(self):
        def count_line(n):
            planters = [(f"Имя{i}", None) for i in range(n)]
            return tree.format_planting_roll_call(planters).splitlines()[2]

        self.assertIn("1 человек:", count_line(1))
        self.assertIn("2 человека:", count_line(2))
        self.assertIn("5 человек:", count_line(5))
        self.assertIn("11 человек:", count_line(11))
        self.assertIn("21 человек:", count_line(21))
        self.assertIn("24 человека:", count_line(24))

    def test_awaiting_status_does_not_claim_a_height(self):
        text = tree.format_awaiting_planting_status()
        self.assertNotIn("0 мм", text)
        self.assertIn("кнопку", text)

    def test_seed_keyboard_carries_the_callers_payload(self):
        keyboard = tree.seed_keyboard("plant:1")
        button = keyboard["inline_keyboard"][0][0]
        self.assertEqual(button["callback_data"], "plant:1")
        self.assertEqual(button["text"], tree.SEED_BUTTON_TEXT)

    def test_button_shows_a_seed_a_shovel_and_a_tree(self):
        for emoji in ("🌰", "🪏", "🌳"):
            self.assertIn(emoji, tree.SEED_BUTTON_TEXT)


if __name__ == "__main__":
    unittest.main()
