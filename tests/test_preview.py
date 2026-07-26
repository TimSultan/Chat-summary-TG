import asyncio
import re
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_listener
import preview
import tree

ADMIN = {"id": 7, "username": "sultan_kembayev", "first_name": "Sultan"}
MEMBER = {"id": 8, "username": "someone", "first_name": "Кто-то"}
DM_CHAT = 999
GROUP_CHAT = -1001234567890


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


class TelegramHtmlTests(unittest.TestCase):
    """Telegram rejects a message outright if its HTML doesn't parse, and the bot's own
    error handling turns that into a button that looks broken: the send raises, the
    handler logs it, and the presser sees nothing at all. So the markup is checked here
    rather than discovered in the chat."""

    # Everything Telegram's HTML parse mode accepts that this project could plausibly use.
    ALLOWED = {"b", "strong", "i", "em", "u", "s", "code", "pre", "a", "blockquote"}

    def _check(self, label, text):
        stack = []
        for match in re.finditer(r"<(/?)([a-zA-Z0-9]+)[^>]*>", text):
            closing, tag = match.group(1), match.group(2).lower()
            with self.subTest(label=label, tag=tag):
                self.assertIn(tag, self.ALLOWED, f"{label}: Telegram will reject <{tag}>")
            if closing:
                self.assertTrue(stack, f"{label}: </{tag}> with nothing open")
                self.assertEqual(stack.pop(), tag, f"{label}: tags cross over")
            else:
                stack.append(tag)
        self.assertEqual(stack, [], f"{label}: unclosed {stack}")

    def test_every_preview_is_valid_telegram_html(self):
        for preview_id in preview.preview_ids():
            self._check(preview_id, preview.render(preview_id, date(2026, 7, 26)))

    def test_the_menu_is_valid_telegram_html(self):
        self._check("menu", preview.menu_view()[0])

    def test_the_group_test_receipt_is_valid_telegram_html(self):
        self._check("receipt", preview.group_test_sent_view(-100123, 7)[0])

    def test_every_advice_line_survives_being_put_in_html(self):
        # One stray "<" or "&" in 120 hand-written lines would kill exactly one morning
        # post, on one unpredictable day, months from now.
        for offset in range(len(tree.DAILY_ADVICE)):
            day = date.fromordinal(date(2026, 1, 1).toordinal() + offset)
            self._check(f"advice {offset}", tree.format_morning_digest(42_000, 3_600, [], day))


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


class FakeAPI:
    def __init__(self):
        self.sent = []
        self.answers = []
        self.deleted = []
        self._next_id = 500

    async def send_message(self, chat_id, text, reply_to_message_id=None,
                           reply_markup=None, parse_mode=None):
        self._next_id += 1
        self.sent.append({
            "chat_id": chat_id, "text": text,
            "reply_markup": reply_markup, "parse_mode": parse_mode,
        })
        return {"message_id": self._next_id}

    async def answer_callback_query(self, callback_query_id, text=None):
        self.answers.append(text)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    async def get_chat_administrators(self, chat_id):
        return [{"user": ADMIN}]


def _callback(data, presser=ADMIN, chat_id=DM_CHAT, chat_type="private"):
    return {
        "id": "cb1",
        "data": data,
        "from": presser,
        "message": {"message_id": 42, "chat": {"id": chat_id, "type": chat_type}},
    }


class PreviewCallbackTests(unittest.TestCase):
    """Drives the real callback handler, because every one of these paths is a button
    that either does nothing visible or posts to 190 people."""

    def setUp(self):
        self.api = FakeAPI()

    def _press(self, data, presser=ADMIN, chat_id=DM_CHAT, chat_type="private",
               known_chat_ids=None):
        asyncio.run(bot_listener.handle_preview_callback(
            self.api, None, _callback(data, presser, chat_id, chat_type),
            "chat", {"chat": GROUP_CHAT} if known_chat_ids is None else known_chat_ids,
            log=lambda *_: None,
        ))

    def test_a_menu_button_sends_that_sample_to_the_dm(self):
        self._press(preview.callback_data("rollcall"))
        self.assertEqual(len(self.api.sent), 1)
        sent = self.api.sent[0]
        self.assertEqual(sent["chat_id"], DM_CHAT)
        self.assertEqual(sent["parse_mode"], "HTML")
        self.assertIn("Семечко в земле", sent["text"])

    def test_every_menu_button_answers_and_sends_something(self):
        for preview_id in preview.preview_ids():
            with self.subTest(preview_id=preview_id):
                self.api = FakeAPI()
                self._press(preview.callback_data(preview_id))
                self.assertEqual(len(self.api.sent), 1, "button produced no message")
                self.assertEqual(self.api.sent[0]["chat_id"], DM_CHAT)
                self.assertEqual(len(self.api.answers), 1, "spinner never stopped")

    def test_the_spinner_stops_before_anything_that_can_block(self):
        # The bug this pins: resolving the group chat goes through the Telethon session,
        # and a session that cannot connect waits instead of failing. Every one of these
        # buttons sat lit up forever because the resolve came first.
        seen = []

        async def _resolve(*args, **kwargs):
            seen.append(len(self.api.answers))
            return GROUP_CHAT

        with patch.object(bot_listener, "_resolve_chat_id", _resolve):
            self._press(preview.callback_data("seed"), known_chat_ids={})
        self.assertEqual(seen, [1], "the chat was resolved before the spinner was stopped")

    def test_a_dead_telethon_session_explains_itself_instead_of_hanging(self):
        async def _resolve(*args, **kwargs):
            return None  # what _resolve_chat_id returns once its timeout expires

        with patch.object(bot_listener, "_resolve_chat_id", _resolve):
            self._press(preview.callback_data("seed"), known_chat_ids={})

        self.assertEqual(len(self.api.answers), 1)
        self.assertIn("TELEGRAM_SESSION_STRING", self.api.sent[0]["text"])

    def test_the_test_button_posts_to_the_chat_and_offers_an_undo(self):
        self._press(preview.callback_data(preview.GROUP_TEST_ID))
        chats = [item["chat_id"] for item in self.api.sent]
        self.assertEqual(chats, [GROUP_CHAT, DM_CHAT])
        posted, receipt = self.api.sent
        self.assertEqual(
            posted["reply_markup"]["inline_keyboard"][0][0]["text"], tree.SEED_BUTTON_TEXT
        )
        undo = receipt["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.assertEqual(preview.parse_delete_callback(undo), (GROUP_CHAT, 501))

    def test_the_undo_deletes_the_post_from_the_chat(self):
        self._press(f"{preview.CALLBACK_PREFIX}:del:{GROUP_CHAT}:501")
        self.assertEqual(self.api.deleted, [(GROUP_CHAT, 501)])
        self.assertIn("Удалил", self.api.sent[0]["text"])

    def test_the_sample_button_posts_nothing_wherever_it_is_pressed(self):
        # The one button a member can reach: it sits on the test post in the group.
        for chat_id, chat_type in ((GROUP_CHAT, "supergroup"), (DM_CHAT, "private")):
            with self.subTest(chat_type=chat_type):
                self.api = FakeAPI()
                self._press(preview.SAMPLE_CALLBACK, presser=MEMBER,
                            chat_id=chat_id, chat_type=chat_type)
                self.assertEqual(self.api.sent, [])
                self.assertEqual(self.api.answers, [tree.SEED_BUTTON_TEST_ACK])

    def test_a_member_cannot_fire_the_group_test(self):
        self._press(preview.callback_data(preview.GROUP_TEST_ID), presser=MEMBER)
        self.assertEqual([item["chat_id"] for item in self.api.sent], [DM_CHAT])
        self.assertIn("только администратор", self.api.sent[0]["text"])
        self.assertEqual(len(self.api.answers), 1, "spinner never stopped")


if __name__ == "__main__":
    unittest.main()
