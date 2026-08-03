"""Summary answers are delivered to the requester's DM, not into the group.

What the group keeps is a receipt, and the receipt takes the request down with it -- so a
chat where twenty people ask for a summary a day is left with none of it. The cases worth
pinning down are the ones where that redirection can go wrong: a DM the bot may not write
to (Telegram forbids a bot from opening a conversation), and a request that was already
in a DM, which must keep being answered exactly where it was asked.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_listener
from errors import ChatSummaryError

GROUP = -1001234567890
REQUESTER = 4242
BOT = "summarybot"


class FakeApi:
    """Records what was sent, deleted, and probed. `dm_open` decides whether the
    sendChatAction probe behaves like a DM the user has opened."""

    def __init__(self, dm_open=True):
        self.dm_open = dm_open
        self.sent = []       # (chat_id, text, kwargs)
        self.deleted = []    # (chat_id, message_id)
        self.actions = []    # chat_ids sendChatAction was called for
        self.next_message_id = 900

    async def send_chat_action(self, chat_id, action="typing"):
        self.actions.append(chat_id)
        if not self.dm_open:
            raise ChatSummaryError("Forbidden: bot can't initiate conversation with a user")

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        self.next_message_id += 1
        return {"message_id": self.next_message_id}

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class ExplodingTelethon:
    """Any use at all is a failure: the DM-closed path must bail out before it fetches
    anything, which is the whole point of probing before the OpenAI work rather than
    after it."""

    def __getattr__(self, name):
        raise AssertionError(f"telethon client was used ({name}) -- the request should have bailed out")


def _run(coro):
    return asyncio.run(coro)


async def _drain(background_tasks: set):
    """schedule_bot_delete is fire-and-forget -- wait for the tasks it spawned."""
    while background_tasks:
        await asyncio.gather(*list(background_tasks))


def _message(chat_type="supergroup", chat_id=GROUP):
    return {
        "message_id": 77,
        "date": 1_770_000_000,
        "text": "сделай сводку за сегодня",
        "chat": {"id": chat_id, "type": chat_type, "title": "Home chat"},
        "from": {"id": REQUESTER, "first_name": "Аня", "username": "anya"},
    }


class CanDmTests(unittest.TestCase):
    def test_an_opened_dm_is_reachable(self):
        api = FakeApi(dm_open=True)
        self.assertTrue(_run(bot_listener._can_dm(api, REQUESTER, log=lambda *_: None)))
        self.assertEqual(api.actions, [REQUESTER])
        self.assertEqual(api.sent, [])  # the probe delivers nothing

    def test_a_never_opened_dm_is_not_reachable(self):
        api = FakeApi(dm_open=False)
        self.assertFalse(_run(bot_listener._can_dm(api, REQUESTER, log=lambda *_: None)))

    def test_no_user_id_is_not_reachable(self):
        api = FakeApi()
        self.assertFalse(_run(bot_listener._can_dm(api, None, log=lambda *_: None)))
        self.assertEqual(api.actions, [])  # nothing to probe, so nothing was asked


class SummaryReceiptTests(unittest.TestCase):
    def setUp(self):
        self._delay = bot_listener.SUMMARY_RECEIPT_DELETE_AFTER
        bot_listener.SUMMARY_RECEIPT_DELETE_AFTER = 0

    def tearDown(self):
        bot_listener.SUMMARY_RECEIPT_DELETE_AFTER = self._delay

    def _post(self, api, bot_username=BOT):
        async def go():
            background_tasks = set()
            await bot_listener._post_summary_receipt(
                api, GROUP, 77, bot_listener.SUMMARY_RECEIPT_TEXT, bot_username,
                background_tasks, log=lambda *_: None,
            )
            await _drain(background_tasks)

        _run(go())

    def test_the_receipt_replies_to_the_request_and_links_to_the_dm(self):
        api = FakeApi()
        self._post(api)
        chat_id, text, kwargs = api.sent[0]
        self.assertEqual(chat_id, GROUP)
        self.assertEqual(text, bot_listener.SUMMARY_RECEIPT_TEXT)
        self.assertEqual(kwargs["reply_to_message_id"], 77)
        button = kwargs["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(button["url"], f"https://t.me/{BOT}")

    def test_both_the_receipt_and_the_request_are_swept(self):
        api = FakeApi()
        self._post(api)
        self.assertEqual(set(api.deleted), {(GROUP, 901), (GROUP, 77)})

    def test_the_request_is_swept_even_when_the_receipt_could_not_be_sent(self):
        """Otherwise a Telegram hiccup on the receipt would strand the request in the
        chat forever -- the one thing this whole flow exists to avoid."""
        api = FakeApi()

        async def broken_send(chat_id, text, **kwargs):
            raise ChatSummaryError("Bad Request: have no rights to send a message")

        api.send_message = broken_send
        self._post(api)
        self.assertEqual(api.deleted, [(GROUP, 77)])

    def test_without_a_bot_username_the_receipt_carries_no_button(self):
        api = FakeApi()
        self._post(api, bot_username=None)
        self.assertIsNone(api.sent[0][2]["reply_markup"])


class SummaryRequestRoutingTests(unittest.TestCase):
    def setUp(self):
        self._delay = bot_listener.SUMMARY_RECEIPT_DELETE_AFTER
        bot_listener.SUMMARY_RECEIPT_DELETE_AFTER = 0

    def tearDown(self):
        bot_listener.SUMMARY_RECEIPT_DELETE_AFTER = self._delay

    def _handle(self, api, message, telethon_client, home_chat_ref="Home chat"):
        async def go():
            background_tasks = set()
            await bot_listener.handle_bot_summary_request(
                api, telethon_client, SimpleNamespace(), None, BOT, message,
                background_tasks, home_chat_ref, log=lambda *_: None,
            )
            await _drain(background_tasks)

        _run(go())

    def test_a_closed_dm_is_reported_in_the_group_and_costs_nothing(self):
        api = FakeApi(dm_open=False)
        self._handle(api, _message(), ExplodingTelethon())
        self.assertEqual(
            [(chat_id, text) for chat_id, text, _ in api.sent],
            [(GROUP, bot_listener.SUMMARY_DM_CLOSED_TEXT)],
        )
        self.assertEqual(set(api.deleted), {(GROUP, 901), (GROUP, 77)})

    def test_a_request_made_in_a_dm_is_still_answered_in_place(self):
        """No redirection, no receipt, nothing swept -- the answer is already where the
        person asked. Driven through the "no home chat configured" early return, which is
        the one branch that reaches `respond` without any OpenAI work."""
        api = FakeApi()
        message = _message(chat_type="private", chat_id=REQUESTER)
        self._handle(api, message, ExplodingTelethon(), home_chat_ref=None)
        self.assertEqual(len(api.sent), 1)
        chat_id, text, kwargs = api.sent[0]
        self.assertEqual(chat_id, REQUESTER)
        self.assertIn("Не настроен основной чат", text)
        self.assertEqual(kwargs.get("reply_to_message_id"), 77)
        self.assertEqual(api.actions, [])  # no probe: the DM is the chat we're already in
        self.assertEqual(api.deleted, [])


if __name__ == "__main__":
    unittest.main()
