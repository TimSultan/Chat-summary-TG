import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot_listener
import cabinet
import economy
import stats


def _user(**kwargs):
    defaults = dict(user_id="20", username="user", display_name="Tester", messages=500)
    defaults.update(kwargs)
    return stats.UserStats(**defaults)


class ViewTests(unittest.TestCase):
    """The views are pure, so they can be checked without a bot token or a network."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def test_main_view_shows_the_card_and_every_section_button(self):
        text, keyboard = cabinet.main_view("chat", _user(), 5_000, rank=3, total=190, streak=4)

        self.assertIn("Личный кабинет", text)
        self.assertIn("🪙 Монеты: 500", text)
        self.assertIn("📈 Место в рейтинге: 3 из 190", text)
        self.assertIn("🔥 Серия: 4 дня", text)

        actions = {
            cabinet.parse_callback(button["callback_data"])[1]
            for row in keyboard["inline_keyboard"]
            for button in row
        }
        self.assertEqual(
            actions, {"stats", "shop", "works", "badges", "title", "send", "main"}
        )

    def test_every_button_stays_inside_telegrams_callback_data_limit(self):
        _, keyboard = cabinet.main_view("chat", _user(user_id="1" * 19), 5_000, 1, 1, 0)
        _, shop = cabinet.shop_view("chat", _user(user_id="1" * 19), 5_000)
        for board in (keyboard, shop):
            for row in board["inline_keyboard"]:
                for button in row:
                    self.assertLessEqual(
                        len(button["callback_data"].encode("utf-8")),
                        cabinet.MAX_CALLBACK_BYTES,
                        button,
                    )

    def test_callback_round_trip(self):
        data = cabinet.callback_data(20, "buy", "freeze")
        self.assertEqual(cabinet.parse_callback(data), ("20", "buy", "freeze"))
        self.assertEqual(cabinet.parse_callback(cabinet.callback_data(20, "main")), ("20", "main", ""))
        self.assertIsNone(cabinet.parse_callback("roast:20:x"))
        self.assertIsNone(cabinet.parse_callback(""))

    def test_shop_view_marks_affordable_locked_and_cooling_down(self):
        user = _user()
        # 1_500 XP -> 150 coins: enough for the roast, not for the title.
        text, keyboard = cabinet.shop_view("chat", user, 1_500)
        self.assertIn("✅", text)
        self.assertIn("🔒", text)

        economy.purchase("chat", user.user_id, 1_500, economy.find_item("roast"))
        text, _ = cabinet.shop_view("chat", user, 1_500)
        self.assertIn("⏳", text)

    def test_every_leaf_view_offers_a_way_back(self):
        user = _user()
        views = [
            cabinet.shop_view("chat", user, 5_000),
            cabinet.works_view(user, [], None, None),
            cabinet.badges_view(user, []),
            cabinet.title_view("chat", user, 5_000),
            cabinet.send_view("chat", user, 5_000),
            cabinet.result_view(user.user_id, "готово"),
        ]
        for text, keyboard in views:
            last_row = keyboard["inline_keyboard"][-1]
            self.assertEqual(last_row[0]["text"], cabinet.BACK_BUTTON)
            self.assertEqual(cabinet.parse_callback(last_row[0]["callback_data"])[1], "main")

    def test_user_controlled_text_is_escaped_into_html(self):
        economy.set_title("chat", "20", "<b>hax</b>")
        text, _ = cabinet.main_view(
            "chat", _user(display_name="<script>alert(1)</script>"), 5_000, 1, 1, 0
        )
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("&lt;b&gt;hax", text)

    def test_works_view_handles_empty_and_capped_histories(self):
        empty, _ = cabinet.works_view(_user(), [], None, None)
        self.assertIn("Пока пусто", empty)

        many = [f"https://t.me/c/1/{n}" for n in range(cabinet.WORKS_SHOWN + 5)]
        capped, _ = cabinet.works_view(_user(figurines_painted=35), many, None, None)
        self.assertIn("и ещё 5", capped)

    def test_transfer_request_parsing(self):
        self.assertEqual(cabinet.parse_transfer_request("@someone 50"), ("@someone", 50))
        self.assertEqual(cabinet.parse_transfer_request("someone   120"), ("someone", 120))
        self.assertIsNone(cabinet.parse_transfer_request("@someone"))
        self.assertIsNone(cabinet.parse_transfer_request("@someone много"))
        self.assertIsNone(cabinet.parse_transfer_request(""))


class FakeAPI:
    """Records what the handler would have sent, so the callback flow can be driven
    without Telegram."""

    def __init__(self):
        self.sent = []
        self.edits = []
        self.answers = []
        self._next_id = 500

    async def send_message(self, chat_id, text, reply_to_message_id=None,
                           reply_markup=None, parse_mode=None):
        self._next_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"message_id": self._next_id}

    async def edit_message_text(self, chat_id, message_id, text,
                                reply_markup=None, parse_mode=None):
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text,
                           "reply_markup": reply_markup})

    async def answer_callback_query(self, callback_query_id, text=None):
        self.answers.append(text)


class CallbackTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)
        self.api = FakeAPI()

    @staticmethod
    def _callback(owner_id, action, actor_id, argument=""):
        return {
            "id": "cb1",
            "data": cabinet.callback_data(owner_id, action, argument),
            "from": {"id": actor_id, "username": "user", "first_name": "Tester"},
            "message": {"message_id": 42, "chat": {"id": 999, "type": "private"}},
        }

    def _run(self, coro):
        return asyncio.run(coro)

    def test_another_member_cannot_press_your_buttons(self):
        # The owner id travels inside the button, so a forwarded menu is inert.
        self._run(
            bot_listener.handle_cabinet_callback(
                self.api, None, None, None,
                self._callback(owner_id=20, action="shop", actor_id=99),
                "chat", {}, log=lambda *_: None,
            )
        )
        self.assertEqual(self.api.answers, ["Это чужой кабинет."])
        self.assertEqual(self.api.edits, [])

    def test_a_missing_home_chat_is_reported_not_crashed(self):
        self._run(
            bot_listener.handle_cabinet_callback(
                self.api, None, None, None,
                self._callback(owner_id=20, action="shop", actor_id=20),
                None, {}, log=lambda *_: None,
            )
        )
        self.assertEqual(self.api.answers, ["Основной чат не настроен."])

    def test_text_entry_actions_open_a_force_reply_and_register_a_flow(self):
        flows = {}
        self._run(
            bot_listener.handle_cabinet_callback(
                self.api, None, None, None,
                self._callback(owner_id=20, action="title_set", actor_id=20),
                "chat", flows, log=lambda *_: None,
            )
        )
        self.assertEqual(len(flows), 1)
        flow = next(iter(flows.values()))
        self.assertEqual(flow["awaiting"], "title")
        self.assertEqual(flow["user_id"], 20)
        self.assertTrue(self.api.sent[0]["reply_markup"]["force_reply"])

    def test_a_reply_that_matches_no_flow_is_left_alone(self):
        handled = self._run(
            bot_listener.handle_cabinet_text_input(
                self.api, None, None,
                {"chat": {"id": 999}, "from": {"id": 20}, "message_id": 7,
                 "text": "просто сообщение"},
                {}, log=lambda *_: None,
            )
        )
        self.assertFalse(handled)


if __name__ == "__main__":
    unittest.main()
