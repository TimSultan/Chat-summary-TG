import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pets_ui
import pets_updates
import stats


class PetUpdatesTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self._stats_dir = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        self._stats_dir.start()
        self.addCleanup(self._stats_dir.stop)
        self.addCleanup(self._temporary.cleanup)

    def test_newest_entry_is_initial_and_read_state_is_per_member(self):
        entry = "chat"
        user_id = 42
        newest = pets_updates.latest()

        self.assertIsNotNone(newest)
        self.assertTrue(pets_updates.has_unread(entry, user_id))
        self.assertIn(newest.title, pets_ui.updates_view(entry, user_id)[0])

        pets_updates.mark_latest_read(entry, user_id)
        self.assertFalse(pets_updates.has_unread(entry, user_id))
        self.assertTrue(pets_updates.has_unread(entry, 43))

    def test_log_has_compact_owner_bound_arrow_buttons(self):
        entry = "chat"
        user_id = 1234567890123456789
        text, keyboard = pets_ui.updates_view(entry, user_id, page=0)

        self.assertIn("1/", text)
        buttons = [button for row in keyboard["inline_keyboard"] for button in row]
        arrow = next(button for button in buttons if button["text"] == "◀️")
        self.assertEqual(
            pets_ui.parse_callback(arrow["callback_data"]),
            (str(user_id), "updates", "1"),
        )
        self.assertLessEqual(len(arrow["callback_data"].encode("utf-8")), pets_ui.MAX_CALLBACK_BYTES)

    def test_menu_button_loses_red_dot_once_log_is_opened(self):
        entry = "chat"
        user_id = 42
        before = pets_ui.main_view(entry, user_id, 0)[1]
        self.assertIn("🔴 Обновления", [
            button["text"] for row in before["inline_keyboard"] for button in row
        ])

        pets_updates.mark_latest_read(entry, user_id)
        after = pets_ui.main_view(entry, user_id, 0)[1]
        self.assertIn("📰 Обновления", [
            button["text"] for row in after["inline_keyboard"] for button in row
        ])

