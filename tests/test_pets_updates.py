import json
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
        newest = pets_updates.latest(entry)

        self.assertIsNotNone(newest)
        self.assertTrue(pets_updates.has_unread(entry, user_id))
        self.assertIn(newest.title, pets_ui.updates_view(entry, user_id)[0])

        pets_updates.mark_latest_read(entry, user_id)
        self.assertFalse(pets_updates.has_unread(entry, user_id))
        self.assertTrue(pets_updates.has_unread(entry, 43))

    def test_latest_card_duel_news_pays_ten_diamonds(self):
        newest = pets_updates.latest("chat")
        self.assertEqual(newest.id, "202608-card-duels")
        self.assertEqual(newest.reward_rubies, 10)
        # The two numbers the note exists to announce.
        self.assertIn("втрое", newest.text)
        self.assertIn("5%", newest.text)

    def test_the_card_duel_note_corrects_the_arena_stake_note_above_it(self):
        """The older note still tells players an arena win takes 5% of the loser's
        diamonds, and that stopped being true when the card duel took that stake over.
        A shipped note is never rewritten, so the correction has to live IN the newer
        one -- otherwise the log's own history would read as a contradiction."""
        notes = {row.id: row for row in pets_updates.UPDATES}
        self.assertIn("5% алмазов",
                      notes["202608-arena-five-percent-stake"].text)
        self.assertIn("алмазы больше не забирают",
                      notes["202608-card-duels"].text)

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

    def test_menu_button_loses_exclamation_once_log_is_opened(self):
        entry = "chat"
        user_id = 42
        # A reward-free note on purpose: an owed reward deliberately outranks the "❗"
        # (see the gift test below), and this one is about the unread mark alone.
        plain = pets_updates.Update("plain-note", "Без награды", "Текст")
        with patch.object(pets_updates, "UPDATES", (plain,)):
            before = pets_ui.main_view(entry, user_id, 0)[1]
            self.assertIn("❗ 📰 Обновления", [
                button["text"] for row in before["inline_keyboard"] for button in row
            ])

            pets_updates.mark_latest_read(entry, user_id)
            after = pets_ui.main_view(entry, user_id, 0)[1]
            self.assertIn("📰 Обновления", [
                button["text"] for row in after["inline_keyboard"] for button in row
            ])


class ChatAuthoredUpdateTests(unittest.TestCase):
    """"/arenanews": an entry written from Telegram, with no deploy behind it."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self._stats_dir = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        self._stats_dir.start()
        self.addCleanup(self._stats_dir.stop)
        self.addCleanup(self._temporary.cleanup)

    def test_added_entry_lands_newest_first_and_reopens_the_red_dot_for_everybody(self):
        entry, reader = "chat", 42
        shipped = len(pets_updates.UPDATES)
        pets_updates.mark_latest_read(entry, reader)
        self.assertFalse(pets_updates.has_unread(entry, reader))

        added = pets_updates.add(entry, "Заголовок", "Тело записи", author_id=7)
        self.assertEqual(pets_updates.latest(entry), added)
        self.assertEqual(len(pets_updates.all_updates(entry)), shipped + 1)
        # Everyone who had already caught up is shown the dot again -- that is the whole
        # point of publishing, and it must not depend on who wrote the entry.
        self.assertTrue(pets_updates.has_unread(entry, reader))
        self.assertTrue(pets_updates.has_unread(entry, 43))

        text, _ = pets_ui.updates_view(entry, reader, page=0)
        self.assertIn("Заголовок", text)
        self.assertIn("Тело записи", text)
        self.assertIn(f"1/{shipped + 1}", text)
        # The shipped entries keep their order and stay reachable behind it.
        self.assertEqual(pets_updates.all_updates(entry)[:shipped], pets_updates.UPDATES)

    def test_entries_are_per_chat_and_survive_a_reload(self):
        pets_updates.add("chat-a", "Только для A", "")
        self.assertEqual(len(pets_updates.custom("chat-a")), 1)
        self.assertEqual(pets_updates.custom("chat-b"), ())
        self.assertEqual(pets_updates.custom("chat-a")[0].title, "Только для A")

    def test_typed_markup_is_escaped_rather_than_sent_as_html(self):
        """A stray "<" in a note must not fail the send of the whole screen."""
        entry = "chat"
        pets_updates.add(entry, "Баланс <b>урона</b>", "Урон < 100 & щит > 5")
        text, _ = pets_ui.updates_view(entry, 42, page=0)

        self.assertIn("&lt;b&gt;", text)
        self.assertIn("&lt; 100 &amp; щит &gt; 5", text)
        self.assertNotIn("<b>урона</b>", text)

    def test_a_one_line_note_is_all_headline_and_an_empty_one_is_refused(self):
        entry = "chat"
        only_title = pets_updates.add(entry, "Ферма стала быстрее", "")
        self.assertEqual(only_title.title, "Ферма стала быстрее")
        self.assertEqual(only_title.text, "")
        # No empty bold line left behind by the missing body.
        self.assertNotIn("<b></b>", pets_ui.updates_view(entry, 42, page=0)[0])

        with self.assertRaises(ValueError):
            pets_updates.add(entry, "   ", "\n  \n")

    def test_overlong_input_is_truncated_instead_of_losing_the_whole_note(self):
        entry = "chat"
        added = pets_updates.add(entry, "з" * 400, "т" * 5000)
        self.assertEqual(len(added.title), pets_updates.MAX_TITLE_LENGTH)
        self.assertEqual(len(added.text), pets_updates.MAX_TEXT_LENGTH)

    def test_ids_stay_unique_so_a_read_checkpoint_can_never_be_reused(self):
        entry = "chat"
        codes = {pets_updates.add(entry, f"Запись {n}", "").id for n in range(5)}
        self.assertEqual(len(codes), 5)
        self.assertTrue(all(code.startswith("chat-") for code in codes))

    def test_a_v1_store_and_a_damaged_row_load_without_losing_read_state(self):
        entry = "chat"
        path = pets_updates._path(entry)
        path.write_text(json.dumps({"version": 1, "read": {"42": "202608-gear"}}), encoding="utf-8")
        self.assertEqual(pets_updates.custom(entry), ())

        pets_updates.add(entry, "После миграции", "")
        # The v1 checkpoint survived the upgrade, so this reader is unread-by-progress
        # rather than unread-by-data-loss.
        self.assertEqual(pets_updates._load(entry)["read"]["42"], "202608-gear")

        stored = json.loads(path.read_text(encoding="utf-8"))
        stored["custom"].append({"title": "Без id", "text": "потеряет якорь"})
        path.write_text(json.dumps(stored), encoding="utf-8")
        self.assertEqual([row.title for row in pets_updates.custom(entry)], ["После миграции"])

    # ---- rewards -----------------------------------------------------------------------

    def _rewarding(self, amount=1, code="note-1"):
        note = pets_updates.Update(code, "Награда", "Текст", reward_rubies=amount)
        return patch.object(pets_updates, "UPDATES", (note,))

    def test_the_bot_view_offers_a_claim_button_and_then_reports_it_taken(self):
        entry, user_id = "chat", 42
        with self._rewarding(amount=2):
            text, markup = pets_ui.updates_view(entry, user_id)
            buttons = [b for row in markup["inline_keyboard"] for b in row]
            claim = next(b for b in buttons if "Забрать награду" in b["text"])
            # The id, not the page number: a note shipping later shifts every page, and a
            # stale button must not pay out a different entry's reward.
            self.assertTrue(claim["callback_data"].endswith(":newsclaim:note-1"))
            self.assertLessEqual(
                len(claim["callback_data"].encode("utf-8")), pets_ui.MAX_CALLBACK_BYTES,
            )
            self.assertIn("2 💎", claim["text"])
            self.assertIn("За эту новость", text)

            pets_updates.mark_claimed(entry, user_id, "note-1")
            text, markup = pets_ui.updates_view(entry, user_id)
            buttons = [b for row in markup["inline_keyboard"] for b in row]
            self.assertFalse([b for b in buttons if "Забрать награду" in b["text"]])
            self.assertIn("Награда получена", text)

    def test_the_menu_button_shows_what_is_owed_and_survives_being_read(self):
        entry, user_id = "chat", 42
        with self._rewarding(amount=7):
            self.assertIn("🎁 Обновления · 7 💎", _menu_labels(entry, user_id))
            # Reading clears the "❗" but never the gift: the diamonds are still owed.
            pets_updates.mark_latest_read(entry, user_id)
            self.assertIn("🎁 Обновления · 7 💎", _menu_labels(entry, user_id))

            pets_updates.mark_claimed(entry, user_id, "note-1")
            labels = _menu_labels(entry, user_id)
            self.assertIn("📰 Обновления", labels)
            self.assertNotIn("🎁 Обновления · 7 💎", labels)

    def test_only_a_shipped_note_can_pay_and_only_once_per_member(self):
        entry = "chat"
        with self._rewarding(amount=3):
            self.assertEqual([n.id for n in pets_updates.claimable(entry, 42)], ["note-1"])
            self.assertTrue(pets_updates.mark_claimed(entry, 42, "note-1"))
            self.assertFalse(pets_updates.mark_claimed(entry, 42, "note-1"))
            self.assertEqual(pets_updates.claimable(entry, 42), ())
            # A second member is unaffected by the first one's claim.
            self.assertEqual([n.id for n in pets_updates.claimable(entry, 43)], ["note-1"])
            self.assertNotEqual(pets_updates.reward_source("note-1", 42),
                                pets_updates.reward_source("note-1", 43))

    def test_a_v2_store_upgrades_with_every_reward_still_unclaimed(self):
        entry = "chat"
        path = pets_updates._path(entry)
        path.write_text(json.dumps({"version": 2, "read": {"42": "x"}, "custom": []}),
                        encoding="utf-8")
        with self._rewarding(amount=1):
            # Nobody could have claimed before the field existed, so "unclaimed" is the
            # only honest reading of an upgraded file.
            self.assertEqual([n.id for n in pets_updates.claimable(entry, 42)], ["note-1"])
        self.assertEqual(pets_updates._load(entry)["claimed"], {})


def _menu_labels(entry, user_id) -> list[str]:
    _text, markup = pets_ui.main_view(entry, user_id, 0)
    return [button["text"] for row in markup["inline_keyboard"] for button in row]
