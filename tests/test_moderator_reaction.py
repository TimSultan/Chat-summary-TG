"""The moderator's ✍️: reacting with it deletes the message (bot_listener.handle_moderator_reaction).

Deleting somebody's message is the one thing in this bot that cannot be undone, so most of
these pin who and what must NOT trigger it.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_api
import bot_listener

GROUP = {"id": -1001234, "type": "supergroup", "username": "examplechat", "title": "Example"}
CFG = SimpleNamespace(listener_allowed_chats=["examplechat"])


class FakeApi:
    def __init__(self):
        self.deleted = []

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


def _reaction(username="sultan_kembayev", new=("✍",), old=(), chat=None, message_id=77):
    update = {
        "chat": chat or GROUP,
        "message_id": message_id,
        "date": 0,
        "old_reaction": [{"type": "emoji", "emoji": emoji} for emoji in old],
        "new_reaction": [{"type": "emoji", "emoji": emoji} for emoji in new],
    }
    if username is not None:
        update["user"] = {"id": 1, "is_bot": False, "first_name": "S", "username": username}
    return update


def _handle(reaction):
    api = FakeApi()
    result = asyncio.run(
        bot_listener.handle_moderator_reaction(api, CFG, reaction, log=lambda *_: None)
    )
    return result, api.deleted


class ModeratorReactionTests(unittest.TestCase):
    def test_the_moderators_writing_hand_deletes_the_message(self):
        self.assertEqual(_handle(_reaction()), (True, [(-1001234, 77)]))

    def test_either_spelling_of_the_emoji_counts_and_the_name_ignores_case(self):
        self.assertTrue(_handle(_reaction(new=("✍️",)))[0])
        self.assertTrue(_handle(_reaction(username="Sultan_Kembayev"))[0])

    def test_it_counts_alongside_other_reactions_already_there(self):
        self.assertTrue(_handle(_reaction(new=("👍", "✍"), old=("👍",)))[0])

    def test_nobody_else_can_delete_with_it(self):
        self.assertEqual(_handle(_reaction(username="someone_else")), (False, []))
        self.assertEqual(_handle(_reaction(username=None)), (False, []))

    def test_any_other_reaction_from_the_moderator_deletes_nothing(self):
        self.assertEqual(_handle(_reaction(new=("👍",))), (False, []))
        self.assertEqual(_handle(_reaction(new=("🔥", "❤"))), (False, []))

    def test_taking_the_reaction_away_deletes_nothing(self):
        self.assertEqual(_handle(_reaction(new=(), old=("✍",))), (False, []))
        # Telegram resends the full set on any change; an old ✍ is not a new one.
        self.assertEqual(_handle(_reaction(new=("✍", "👍"), old=("✍",))), (False, []))

    def test_only_in_a_tracked_chat(self):
        other = {"id": -1009999, "type": "supergroup", "username": "elsewhere"}
        self.assertEqual(_handle(_reaction(chat=other)), (False, []))

    def test_a_custom_emoji_reaction_is_not_the_writing_hand(self):
        reaction = _reaction(new=())
        reaction["new_reaction"] = [{"type": "custom_emoji", "custom_emoji_id": "123"}]
        self.assertEqual(_handle(reaction), (False, []))


class DispatchTests(unittest.TestCase):
    def test_a_reaction_update_reaches_the_handler_and_nothing_else(self):
        api = FakeApi()

        async def go():
            await bot_listener._dispatch_update(
                {"update_id": 5, "message_reaction": _reaction()},
                api, None, CFG, None, "bot", 1, set(), asyncio.Queue(), set(),
                "examplechat", {}, {}, {}, {},
                log=lambda *_: None,
            )

        asyncio.run(go())
        self.assertEqual(api.deleted, [(-1001234, 77)])

    def test_the_bot_asks_telegram_for_reaction_updates(self):
        """Telegram never sends message_reaction unless it is named in allowed_updates."""
        captured = {}

        class _Probe(bot_api.TelegramBotAPI):
            async def _call(self, method, **params):
                captured.update(params)
                return []

        asyncio.run(_Probe("token", None).get_updates())
        self.assertIn("message_reaction", captured["allowed_updates"])
        self.assertIn("message", captured["allowed_updates"])
        self.assertIn("callback_query", captured["allowed_updates"])


if __name__ == "__main__":
    unittest.main()
