"""The "/vote chat" announcement flow: the force-reply that parks a draft, and the
destination buttons that decide which chat(s) it is finally posted into.

Nothing here touches voting.py or a poll file -- the whole flow lives in the in-memory
vote_chat_flows dict -- so there is no poll state to set up, and the admin check is
stubbed rather than exercised (it has its own tests elsewhere) to keep every case a pure
Telegram-API assertion.
"""

import asyncio
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_listener

DM_CHAT_ID = 555
MAIN_CHAT_ID = -1001234567890
EXTRA_CHAT = "@papkahudojnicov"
ADMIN = {"id": 42, "username": "admin"}
PROMPT_ID = 7
BOT = "testbot"


def _run(coro):
    return asyncio.run(coro)


def _cfg(extra=EXTRA_CHAT, short_name=None):
    return SimpleNamespace(
        webapp_public_url="https://example.com",
        vote_announce_extra_chat=extra,
        vote_miniapp_short_name=short_name,
    )


class FakeApi:
    """sendMessage plus answerCallbackQuery, with a per-chat failure injection so a
    destination that rejects the post can be tested next to one that accepts it."""

    def __init__(self, failing_chats=()):
        self.sent = []
        self.answered = []
        self.failing_chats = set(failing_chats)

    async def send_message(self, chat_id, text, reply_to_message_id=None,
                           reply_markup=None, parse_mode=None):
        if chat_id in self.failing_chats:
            raise RuntimeError("Bad Request: chat not found")
        self.sent.append({
            "chat_id": chat_id, "text": text, "reply_markup": reply_markup,
            "reply_to_message_id": reply_to_message_id,
        })
        return {"message_id": 100 + len(self.sent)}

    async def answer_callback_query(self, callback_id, text=None):
        self.answered.append({"id": callback_id, "text": text})

    def posts_to(self, chat_id):
        return [s for s in self.sent if s["chat_id"] == chat_id]


def _flow(text="Голосуем за итоги недели"):
    return {
        "chat_id": DM_CHAT_ID,
        "user_id": ADMIN["id"],
        "entry": "Chat",
        "admin_chat_id": MAIN_CHAT_ID,
        "prompt_message_id": PROMPT_ID,
        "created_at": time.monotonic(),
        "text": text,
    }


def _reply_message(text):
    return {
        "message_id": 8,
        "chat": {"id": DM_CHAT_ID, "type": "private"},
        "from": ADMIN,
        "text": text,
        "reply_to_message": {"message_id": PROMPT_ID},
    }


def _press(destination, flow_id):
    return {
        "id": "cbq1",
        "data": bot_listener._vote_chat_dest_callback_data(destination, flow_id),
        "from": ADMIN,
        "message": {"message_id": 9},
    }


class _AlwaysAdmin:
    """Stands in for _can_manage_chat, which would otherwise ask Telegram for the chat's
    administrator list (and read the badge-manager file) on every one of these cases."""

    def __init__(self, allowed=True):
        self.allowed = allowed

    async def __call__(self, api, chat_id, user, entry=None):
        return self.allowed


class DraftAsksWhereItGoesTests(unittest.TestCase):
    def _feed(self, text, cfg=None, flows=None, admin=True):
        api = FakeApi()
        flows = {"f1": _flow()} if flows is None else flows
        with patch.object(bot_listener, "_can_manage_chat", _AlwaysAdmin(admin)):
            handled = _run(bot_listener.handle_vote_chat_text_input(
                api, cfg or _cfg(), _reply_message(text), flows, log=lambda *_: None,
            ))
        return api, flows, handled

    def test_the_draft_is_not_posted_yet_and_the_flow_survives_the_reply(self):
        api, flows, handled = self._feed("Новое голосование")
        self.assertTrue(handled)
        self.assertEqual([s["chat_id"] for s in api.sent], [DM_CHAT_ID])
        self.assertIn("Куда отправить объявление", api.sent[0]["text"])
        self.assertEqual(flows["f1"]["text"], "Новое голосование")

    def test_all_three_destinations_are_offered_when_the_second_group_is_configured(self):
        api, _, _ = self._feed("Текст")
        labels = [b["text"] for row in api.sent[0]["reply_markup"]["inline_keyboard"] for b in row]
        self.assertEqual(labels, ["📣 В чат", "🎨 В Папку художников", "📣 В оба", "Отмена"])

    def test_without_a_second_group_only_the_main_chat_is_offered(self):
        api, _, _ = self._feed("Текст", cfg=_cfg(extra=None))
        labels = [b["text"] for row in api.sent[0]["reply_markup"]["inline_keyboard"] for b in row]
        self.assertEqual(labels, ["📣 В чат", "Отмена"])

    def test_cancelling_at_the_text_step_drops_the_draft(self):
        api, flows, _ = self._feed("отмена")
        self.assertEqual(flows, {})
        self.assertIn("отменён", api.sent[0]["text"])


class DestinationCallbackTests(unittest.TestCase):
    def _tap(self, destination, cfg=None, flows=None, failing_chats=(), admin=True,
             bot_username=BOT):
        api = FakeApi(failing_chats=failing_chats)
        flows = {"f1": _flow()} if flows is None else flows
        with patch.object(bot_listener, "_can_manage_chat", _AlwaysAdmin(admin)):
            _run(bot_listener.handle_vote_chat_destination_callback(
                api, cfg or _cfg(), _press(destination, "f1"), flows, bot_username,
                log=lambda *_: None,
            ))
        return api, flows

    def test_main_posts_into_the_tracked_chat_only(self):
        api, flows = self._tap("main")
        self.assertEqual(len(api.posts_to(MAIN_CHAT_ID)), 1)
        self.assertEqual(api.posts_to(EXTRA_CHAT), [])
        self.assertEqual(api.posts_to(MAIN_CHAT_ID)[0]["text"], _flow()["text"])
        self.assertEqual(flows, {})

    def test_extra_posts_into_the_second_group_only(self):
        api, _ = self._tap("extra")
        self.assertEqual(len(api.posts_to(EXTRA_CHAT)), 1)
        self.assertEqual(api.posts_to(MAIN_CHAT_ID), [])

    def test_both_posts_into_each_of_them_once(self):
        api, _ = self._tap("both")
        self.assertEqual(len(api.posts_to(MAIN_CHAT_ID)), 1)
        self.assertEqual(len(api.posts_to(EXTRA_CHAT)), 1)
        report = api.posts_to(DM_CHAT_ID)[-1]["text"]
        self.assertIn("основной чат", report)
        self.assertIn("Папка художников", report)

    def test_one_failing_destination_does_not_block_the_other(self):
        api, _ = self._tap("both", failing_chats=(MAIN_CHAT_ID,))
        self.assertEqual(len(api.posts_to(EXTRA_CHAT)), 1)
        report = api.posts_to(DM_CHAT_ID)[-1]["text"]
        self.assertIn("Папка художников", report.split("Не получилось")[0])
        self.assertIn("основной чат", report.split("Не получилось")[1])
        self.assertIn("chat not found", report)

    def test_every_destination_failing_is_still_reported_rather_than_silent(self):
        api, flows = self._tap("both", failing_chats=(MAIN_CHAT_ID, EXTRA_CHAT))
        self.assertIn("Не получилось отправить", api.posts_to(DM_CHAT_ID)[-1]["text"])
        self.assertEqual(flows, {})

    def test_the_button_is_a_plain_url_not_a_web_app_one(self):
        """A web_app button is private-chat only -- Telegram rejects one posted to a
        group, which is where every destination here is."""
        api, _ = self._tap("main")
        button = api.posts_to(MAIN_CHAT_ID)[0]["reply_markup"]["inline_keyboard"][0][0]
        self.assertNotIn("web_app", button)
        self.assertEqual(button["url"], f"https://t.me/{BOT}?start=vote")

    def test_a_direct_link_mini_app_short_name_opens_the_app_from_the_group(self):
        api, _ = self._tap("main", cfg=_cfg(short_name="vote"))
        button = api.posts_to(MAIN_CHAT_ID)[0]["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(button["url"], f"https://t.me/{BOT}/vote?startapp=vote")

    def test_the_announcement_is_never_scheduled_for_deletion(self):
        """The one message in this bot that has to still be there tomorrow -- no sweep
        path may touch it, on any destination."""
        scheduled = []
        with patch.object(bot_listener, "schedule_bot_delete", lambda *a, **k: scheduled.append(a)):
            api, _ = self._tap("both")
        self.assertEqual(scheduled, [])
        self.assertEqual(len(api.posts_to(MAIN_CHAT_ID)), 1)

    def test_cancel_posts_nothing_anywhere(self):
        api, flows = self._tap("cancel")
        self.assertEqual(api.posts_to(MAIN_CHAT_ID), [])
        self.assertEqual(api.posts_to(EXTRA_CHAT), [])
        self.assertIn("отменено", api.posts_to(DM_CHAT_ID)[-1]["text"])
        self.assertEqual(flows, {})

    def test_the_spinner_is_stopped_before_anything_is_posted(self):
        api, _ = self._tap("both")
        self.assertEqual(len(api.answered), 1)

    def test_somebody_else_pressing_it_posts_nothing(self):
        api = FakeApi()
        flows = {"f1": _flow()}
        press = _press("both", "f1")
        press["from"] = {"id": 99, "username": "someone"}
        with patch.object(bot_listener, "_can_manage_chat", _AlwaysAdmin(True)):
            _run(bot_listener.handle_vote_chat_destination_callback(
                api, _cfg(), press, flows, BOT, log=lambda *_: None,
            ))
        self.assertEqual(api.sent, [])
        self.assertIn("не для тебя", api.answered[0]["text"])
        self.assertIn("f1", flows)

    def test_an_expired_draft_posts_nothing(self):
        flow = _flow()
        flow["created_at"] -= bot_listener.VOTE_CHAT_FLOW_TTL_SECONDS + 1
        api, flows = self._tap("both", flows={"f1": flow})
        self.assertEqual(api.sent, [])
        self.assertEqual(flows, {})

    def test_losing_admin_rights_between_the_draft_and_the_button_posts_nothing(self):
        api, _ = self._tap("both", admin=False)
        self.assertEqual(api.posts_to(MAIN_CHAT_ID), [])
        self.assertIn("администратор", api.posts_to(DM_CHAT_ID)[-1]["text"])

    def test_an_unknown_bot_username_posts_nothing(self):
        api, _ = self._tap("main", bot_username=None)
        self.assertEqual(api.posts_to(MAIN_CHAT_ID), [])


class DestinationCallbackDataTests(unittest.TestCase):
    def test_data_round_trips(self):
        data = bot_listener._vote_chat_dest_callback_data("both", "abc123")
        self.assertEqual(bot_listener._parse_vote_chat_dest_callback(data), ("both", "abc123"))

    def test_an_unknown_destination_is_rejected(self):
        self.assertIsNone(bot_listener._parse_vote_chat_dest_callback("votechatdest:nowhere:f1"))

    def test_malformed_data_is_rejected(self):
        self.assertIsNone(bot_listener._parse_vote_chat_dest_callback("votechatdest:main"))
        self.assertIsNone(bot_listener._parse_vote_chat_dest_callback("votechatdest:main:"))
        self.assertIsNone(bot_listener._parse_vote_chat_dest_callback(""))
        self.assertIsNone(bot_listener._parse_vote_chat_dest_callback(None))


class DestinationDispatchTests(unittest.TestCase):
    """Same reason test_vote_dispatch.py pins the voteaction prefix: a callback that never
    reaches its handler looks exactly like a handler bug from the outside."""

    def test_a_destination_button_reaches_its_handler_through_update_dispatch(self):
        handled = []

        async def handle(*args, **kwargs):
            handled.append(args[2])  # (api, cfg, callback, flows, bot_username)

        async def go():
            with patch.object(bot_listener, "handle_vote_chat_destination_callback", handle):
                await bot_listener._dispatch_update(
                    {"callback_query": _press("both", "f1")},
                    FakeApi(), None, None, None, BOT, 1, set(), asyncio.Queue(),
                    set(), "chat", {}, {}, None, {}, {}, {},
                    log=lambda *_: None,
                )

        _run(go())
        self.assertEqual(len(handled), 1)
        self.assertEqual(handled[0]["data"], "votechatdest:both:f1")


if __name__ == "__main__":
    unittest.main()
