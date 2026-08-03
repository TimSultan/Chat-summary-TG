"""Two /vote changes tested together since both gate who gets to see a ballot:

- _is_chat_member -- the "только подписчики" rule a Mini App request is checked against
  (see bot_listener._is_vote_member and vote_web's is_member hook).
- handle_vote_command's group-chat branch no longer schedules its own post for deletion
  -- unlike a /stat reply, the vote announcement is meant to stay findable in the chat.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_listener
from errors import ChatSummaryError

CHAT_ID = -1001234567890
USER_ID = 42


def _run(coro):
    return asyncio.run(coro)


class FakeMemberApi:
    """Only get_chat_member -- all _is_chat_member ever calls."""

    def __init__(self, member=None, error=None):
        self.member = member
        self.error = error
        self.calls = []

    async def get_chat_member(self, chat_id, user_id):
        self.calls.append((chat_id, user_id))
        if self.error is not None:
            raise self.error
        return self.member


class IsChatMemberTests(unittest.TestCase):
    def _check(self, member=None, error=None):
        api = FakeMemberApi(member=member, error=error)
        result = _run(bot_listener._is_chat_member(api, CHAT_ID, USER_ID))
        self.assertEqual(api.calls, [(CHAT_ID, USER_ID)])
        return result

    def test_creator_is_a_member(self):
        self.assertTrue(self._check({"status": "creator"}))

    def test_administrator_is_a_member(self):
        self.assertTrue(self._check({"status": "administrator"}))

    def test_plain_member_is_a_member(self):
        self.assertTrue(self._check({"status": "member"}))

    def test_restricted_but_still_in_the_chat_is_a_member(self):
        self.assertTrue(self._check({"status": "restricted", "is_member": True}))

    def test_restricted_and_no_longer_in_the_chat_is_not_a_member(self):
        self.assertFalse(self._check({"status": "restricted", "is_member": False}))

    def test_left_is_not_a_member(self):
        self.assertFalse(self._check({"status": "left"}))

    def test_kicked_is_not_a_member(self):
        self.assertFalse(self._check({"status": "kicked"}))

    def test_a_telegram_error_fails_closed(self):
        """A user who never joined the chat makes getChatMember error out, same as any
        transient API failure -- both must deny the vote rather than allow it."""
        self.assertFalse(self._check(error=ChatSummaryError("Bad Request: user not found")))


class VoteGroupPostIsNotAutoDeletedTests(unittest.TestCase):
    """handle_vote_command's group-chat branch: the announcement is deliberately left in
    the chat now, unlike the /stat replies this codebase otherwise sweeps away."""

    class FakeApi:
        def __init__(self):
            self.sent = []
            self.deleted = []

        async def send_message(self, chat_id, text, reply_to_message_id=None,
                                reply_markup=None, parse_mode=None):
            self.sent.append({"chat_id": chat_id, "text": text})
            return {"message_id": 999}

        async def delete_message(self, chat_id, message_id):
            self.deleted.append((chat_id, message_id))

    def test_a_bare_vote_in_a_group_sends_but_never_schedules_a_delete(self):
        api = self.FakeApi()
        cfg = SimpleNamespace(
            webapp_public_url="https://example.com",
            vote_miniapp_short_name=None,
            vote_announce_extra_chat=None,
        )
        message = {
            "message_id": 1,
            "chat": {"id": CHAT_ID, "type": "group"},
            "from": {"id": USER_ID, "username": "someone"},
            "text": "/vote",
        }
        background_tasks: set = set()

        _run(bot_listener.handle_vote_command(
            api, None, cfg, None, message, "entry-chat", "testbot",
            background_tasks, log=lambda *_: None,
        ))

        self.assertEqual(len(api.sent), 1)
        self.assertIn("Голосование за итоги недели", api.sent[0]["text"])
        # No self-delete was scheduled, and none of its background tasks (if any were
        # somehow spawned) ever got around to deleting anything either.
        self.assertEqual(background_tasks, set())
        if background_tasks:
            _run(asyncio.gather(*background_tasks))
        self.assertEqual(api.deleted, [])


if __name__ == "__main__":
    unittest.main()
