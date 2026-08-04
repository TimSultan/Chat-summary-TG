"""Pins the voteaction: callback prefix wiring in _dispatch_update -- this codebase has
been bitten before (see test_poker.py/test_preview.py) by callback routing bugs that a
unit test of the leaf handler alone would never catch."""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_listener

ADMIN = {"id": 1, "username": "admin"}


def _press(data: str, user: dict) -> dict:
    return {"id": "cbq1", "data": data, "from": user, "message": {"message_id": 42}}


class _FakeApi:
    """Only what an unrecognized callback_data needs -- _dispatch_update's fallback
    branch answers it anyway so a stray button's spinner doesn't hang forever."""

    def __init__(self):
        self.answered = []

    async def answer_callback_query(self, callback_id, text=None):
        self.answered.append(callback_id)


class VoteActionDispatchTests(unittest.TestCase):
    def _dispatch(self, callback_query: dict, api=None):
        handled = []

        async def handle(*args, **kwargs):
            handled.append(args[4])  # (api, telethon_client, cfg, tz, callback, ...)

        async def go():
            with patch.object(bot_listener, "handle_vote_action_callback", handle):
                await bot_listener._dispatch_update(
                    {"callback_query": callback_query},
                    api or _FakeApi(), None, None, None, None, 1, set(), asyncio.Queue(),
                    set(), "chat", {}, {}, {}, {},
                    log=lambda *_: None,
                )

        asyncio.run(go())
        return handled

    def test_a_voteaction_button_reaches_its_handler_through_update_dispatch(self):
        handled = self._dispatch(_press(
            bot_listener._vote_action_callback_data("collect", -100, ADMIN["id"]), ADMIN
        ))
        self.assertEqual(len(handled), 1)
        self.assertEqual(handled[0]["data"], f"voteaction:collect:-100:{ADMIN['id']}")

    def test_an_unrelated_callback_does_not_reach_it(self):
        handled = self._dispatch(_press("someotherprefix:whatever", ADMIN))
        self.assertEqual(handled, [])


class VoteActionCallbackTests(unittest.TestCase):
    def test_data_round_trips(self):
        data = bot_listener._vote_action_callback_data("chat", -100, 42)
        self.assertEqual(bot_listener._parse_vote_action_callback(data), ("chat", -100, 42))

    def test_an_unknown_action_is_rejected(self):
        self.assertIsNone(bot_listener._parse_vote_action_callback("voteaction:bogus:1:2"))

    def test_malformed_data_is_rejected(self):
        self.assertIsNone(bot_listener._parse_vote_action_callback("voteaction:collect:1"))
        self.assertIsNone(bot_listener._parse_vote_action_callback(""))
        self.assertIsNone(bot_listener._parse_vote_action_callback(None))


if __name__ == "__main__":
    unittest.main()
