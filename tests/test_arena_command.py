"""What "/arena" puts in front of whoever typed it.

The gate itself (_can_manage_chat) has its own tests elsewhere and is stubbed here; what
these pin is the consequence of its answer -- an administrator gets the control panel, and
everybody else gets one button and nothing they cannot use. A button that only ever
answers "не для тебя" is worse than no button: it reads as a permission the reader has.
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import arena
import bot_listener

DM_CHAT_ID = 555
MAIN_CHAT_ID = -1001234567890
CHAT = "Chat"
BOT = "testbot"
ADMIN = {"id": 42, "username": "admin"}
VOTER = {"id": 77, "username": "voter"}


def _run(coro):
    return asyncio.run(coro)


def _cfg():
    return SimpleNamespace(
        webapp_public_url="https://example.com",
        vote_announce_extra_chat=None,
        vote_miniapp_short_name=None,
    )


class FakeApi:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_to_message_id=None,
                           reply_markup=None, parse_mode=None):
        item = {
            "message_id": 100 + len(self.sent),
            "chat_id": chat_id, "text": text, "reply_markup": reply_markup,
        }
        self.sent.append(item)
        return item

    async def answer_callback_query(self, callback_id, text=None):
        pass


def _message(user, text="/arena", chat_type="private"):
    return {
        "message_id": 5,
        "chat": {"id": DM_CHAT_ID if chat_type == "private" else MAIN_CHAT_ID, "type": chat_type},
        "from": user,
        "text": text,
    }


def _buttons(message):
    markup = message["reply_markup"] or {}
    return [b for row in markup.get("inline_keyboard", []) for b in row]


class _Manager:
    def __init__(self, allowed):
        self.allowed = allowed

    async def __call__(self, api, chat_id, user, entry=None):
        return self.allowed


async def _resolves(client, entry, cache, log=print):
    return MAIN_CHAT_ID


class ArenaPanelTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("arena._arena_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def _type(self, user, manager, text="/arena", chat_type="private", flows=None):
        api = FakeApi()
        with patch.object(bot_listener, "_can_manage_chat", _Manager(manager)), \
                patch.object(bot_listener, "_resolve_chat_id", _resolves):
            _run(bot_listener.handle_arena_command(
                api, None, _cfg(), None, _message(user, text, chat_type), CHAT, BOT,
                set(), log=lambda *_: None, vote_chat_flows=flows,
            ))
        return api

    def test_a_plain_voter_gets_one_button_and_it_opens_the_arena(self):
        api = self._type(VOTER, manager=False)
        buttons = _buttons(api.sent[0])
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0]["text"], bot_listener.ARENA_OPEN_BUTTON_TEXT)
        self.assertIn("web_app", buttons[0])

    def test_a_plain_voter_is_offered_no_administrator_button_at_all(self):
        """Not "shown and refused" -- not shown. Every callback button on the panel is an
        admin action, so none of them may appear on a voter's copy."""
        api = self._type(VOTER, manager=False)
        for button in _buttons(api.sent[0]):
            self.assertNotIn("callback_data", button)
        self.assertNotIn("mode=admin", str(api.sent[0]["reply_markup"]))

    def test_a_plain_voter_is_not_shown_the_standings_either(self):
        """The status block names the current top three, which is exactly what the Mini App
        withholds from somebody who has not finished voting."""
        api = self._type(VOTER, manager=False)
        self.assertNotIn("Топ по рейтингу", api.sent[0]["text"])

    def test_an_administrator_gets_the_whole_panel(self):
        api = self._type(ADMIN, manager=True)
        labels = [b["text"] for b in _buttons(api.sent[0])]
        self.assertIn(bot_listener.ARENA_OPEN_BUTTON_TEXT, labels)
        self.assertIn("📣 Объявление", labels)
        self.assertIn("🛠 Модерация", labels)
        self.assertIn("🗑 Очистить", labels)

    def test_every_panel_action_button_is_bound_to_the_admin_who_asked(self):
        api = self._type(ADMIN, manager=True)
        actions = [b for b in _buttons(api.sent[0]) if "callback_data" in b]
        self.assertTrue(actions)
        for button in actions:
            parsed = bot_listener._parse_arena_action_callback(button["callback_data"])
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed[2], ADMIN["id"])

    def test_every_action_on_the_panel_is_one_the_menu_knows(self):
        """A button whose callback_data does not parse is a dead button -- the dispatcher
        answers the spinner and does nothing at all."""
        api = self._type(ADMIN, manager=True)
        for button in _buttons(api.sent[0]):
            if "callback_data" in button:
                action = bot_listener._parse_arena_action_callback(button["callback_data"])[0]
                self.assertIn(action, bot_listener.ARENA_ACTIONS)

    def test_in_a_group_everybody_gets_the_same_deep_link_and_nothing_else(self):
        """A web_app button is private-chat only, so a group can only carry the url -- and
        an administrator typing /arena in the group is not in their DM, so they get no
        panel there either."""
        for user, manager in ((VOTER, False), (ADMIN, True)):
            api = self._type(user, manager=manager, chat_type="group")
            buttons = _buttons(api.sent[0])
            self.assertEqual(len(buttons), 1)
            self.assertEqual(buttons[0]["url"], f"https://t.me/{BOT}?start=arena")
            self.assertNotIn("web_app", buttons[0])

    def test_an_admin_only_subcommand_from_a_voter_is_refused(self):
        for text in ("/arena собрать", "/arena итоги", "/arena chat", "/arena очистить"):
            api = self._type(VOTER, manager=False, text=text, flows={})
            self.assertIn("администратор", api.sent[0]["text"], text)

    def test_an_admin_only_subcommand_in_a_group_is_sent_to_the_dm(self):
        api = self._type(ADMIN, manager=True, text="/arena chat", chat_type="group", flows={})
        self.assertIn("только в личке", api.sent[0]["text"])


class ArenaAnnouncementDraftTests(unittest.TestCase):
    """"/arena chat" opens the same composer "/vote chat" does -- tagged so the button it
    finally posts leads to the arena and not to v1."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("arena._arena_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def _draft(self, command, flows=None):
        api = FakeApi()
        flows = {} if flows is None else flows
        with patch.object(bot_listener, "_can_manage_chat", _Manager(True)), \
                patch.object(bot_listener, "_resolve_chat_id", _resolves):
            _run(bot_listener.handle_arena_command(
                api, None, _cfg(), None, _message(ADMIN, command), CHAT, BOT,
                set(), log=lambda *_: None, vote_chat_flows=flows,
            ))
        return api, flows

    def test_the_draft_is_parked_tagged_as_the_arenas(self):
        api, flows = self._draft("/arena chat")
        self.assertTrue(api.sent[0]["reply_markup"]["force_reply"])
        self.assertIn("объявлении об арене", api.sent[0]["text"])
        flow = next(iter(flows.values()))
        self.assertEqual(flow["system"], "arena")
        self.assertEqual(flow["user_id"], ADMIN["id"])
        self.assertEqual(flow["prompt_message_id"], api.sent[0]["message_id"])

    def test_every_spelling_of_the_subcommand_opens_it(self):
        for command in ("/arena chat", "/arena объявление", "/arena announce"):
            _, flows = self._draft(command)
            self.assertEqual(len(flows), 1, command)

    def test_starting_a_second_draft_abandons_the_first(self):
        _, flows = self._draft("/arena chat")
        first = next(iter(flows))
        self._draft("/arena chat", flows=flows)
        self.assertEqual(len(flows), 1)
        self.assertNotIn(first, flows)

    def test_a_vote_draft_and_an_arena_draft_do_not_both_wait_on_a_reply(self):
        """They share one prompt convention, so two live drafts for the same admin would
        both be listening for a reply and the wrong one could swallow it."""
        flows = {"v1": {
            "chat_id": DM_CHAT_ID, "user_id": ADMIN["id"], "entry": CHAT,
            "system": "vote", "admin_chat_id": MAIN_CHAT_ID, "prompt_message_id": 3,
            "created_at": 0.0,
        }}
        self._draft("/arena chat", flows=flows)
        self.assertEqual(len(flows), 1)
        self.assertEqual(next(iter(flows.values()))["system"], "arena")


class AnnouncementButtonTests(unittest.TestCase):
    def test_the_arenas_button_leads_to_the_arena(self):
        button = bot_listener._announce_button(_cfg(), BOT, "arena")
        self.assertEqual(button["text"], bot_listener.ARENA_OPEN_BUTTON_TEXT)
        self.assertEqual(button["url"], f"https://t.me/{BOT}?start=arena")

    def test_a_v1_mini_app_short_name_never_leaks_into_the_arenas_button(self):
        """VOTE_MINIAPP_SHORT_NAME is registered against v1's page in BotFather. Reusing it
        with ?startapp=arena would open the wrong ballot."""
        cfg = _cfg()
        cfg.vote_miniapp_short_name = "vote"
        self.assertEqual(
            bot_listener._announce_button(cfg, BOT, "arena")["url"],
            f"https://t.me/{BOT}?start=arena",
        )

    def test_an_untagged_flow_still_means_v1(self):
        button = bot_listener._announce_button(_cfg(), BOT, "vote")
        self.assertEqual(button["text"], bot_listener.VOTE_OPEN_BUTTON_TEXT)
        self.assertEqual(button["url"], f"https://t.me/{BOT}?start=vote")

    def test_without_a_bot_username_there_is_no_button_to_post(self):
        for system in ("vote", "arena"):
            self.assertIsNone(bot_listener._announce_button(_cfg(), None, system))


if __name__ == "__main__":
    unittest.main()
