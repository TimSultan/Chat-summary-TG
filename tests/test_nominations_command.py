"""What "/vote3" does in the bot, and that it leaves "/vote" and "/vote2" where they were.

"/vote3" starts with "/vote", so the router has to look for it first or v1 would open its
ballot with a stray "3" for an argument -- the trap "/vote2" already fell into once. The
rest pins the panel (an administrator's controls, a voter's single button), the import
from v1 being a read, and a broken v3 saying so instead of going silent.
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import admin_menu
import bot_listener
import nominations
import voting

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
        webapp_public_url="https://example.com", listener_allowed_chats=[],
        stats_enabled=True, stats_top_limit=10,
        vote_announce_extra_chat=None, vote_miniapp_short_name=None,
    )


class FakeApi:
    def __init__(self):
        self.sent = []
        self.answered = []

    async def send_message(self, chat_id, text, reply_to_message_id=None,
                           reply_markup=None, parse_mode=None):
        item = {"message_id": 100 + len(self.sent), "chat_id": chat_id,
                "text": text, "reply_markup": reply_markup}
        self.sent.append(item)
        return item

    async def answer_callback_query(self, callback_id, text=None):
        self.answered.append((callback_id, text))


def _message(user, text="/vote3", chat_type="private"):
    return {
        "message_id": 5,
        "chat": {"id": DM_CHAT_ID if chat_type == "private" else MAIN_CHAT_ID, "type": chat_type},
        "from": user,
        "text": text,
    }


def _buttons(message):
    markup = message["reply_markup"] or {}
    return [b for row in markup.get("inline_keyboard", []) for b in row]


async def _resolves(client, entry, cache, log=print):
    return MAIN_CHAT_ID


class _Manager:
    def __init__(self, allowed):
        self.allowed = allowed

    async def __call__(self, api, chat_id, user, entry=None):
        return self.allowed


class _Storage(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        for target, path in (("nominations._nominations_dir", root / "nominations"),
                             ("voting._voting_dir", root / "voting")):
            patcher = patch(target, return_value=path)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def _type(self, user, manager, text="/vote3", chat_type="private"):
        api = FakeApi()
        with patch.object(bot_listener, "_can_manage_chat", _Manager(manager)), \
                patch.object(bot_listener, "_resolve_chat_id", _resolves):
            _run(bot_listener.handle_nominations_command(
                api, None, _cfg(), None, _message(user, text, chat_type), CHAT, BOT,
                set(), log=lambda *_: None,
            ))
        return api


class RoutingTests(unittest.TestCase):
    """Through _dispatch_update itself: a router bug is invisible to a test of the leaf."""

    def _route(self, text):
        reached = []

        def recorder(name):
            async def handle(*args, **kwargs):
                reached.append(name)
            return handle

        async def go():
            tasks = set()
            update = {"message": {
                "message_id": 5, "chat": {"id": DM_CHAT_ID, "type": "private"},
                "from": VOTER, "text": text,
            }}
            with patch.object(bot_listener, "handle_nominations_command", recorder("v3")), \
                    patch.object(bot_listener, "handle_arena_command", recorder("v2")), \
                    patch.object(bot_listener, "handle_vote_command", recorder("v1")):
                await bot_listener._dispatch_update(
                    update, FakeApi(), None, _cfg(), None, BOT, 1, set(), asyncio.Queue(),
                    tasks, CHAT, {}, {}, {}, {}, log=lambda *_: None,
                )
                # /vote, /vote2 and /vote3 all run as background tasks.
                await asyncio.gather(*tasks)

        _run(go())
        return reached

    def test_each_vote_command_reaches_its_own_system(self):
        for text, system in (
            ("/vote3", "v3"), ("/vote3 выбрать", "v3"), ("/vote3@testbot", "v3"),
            ("/голосование3", "v3"), ("/start vote3", "v3"),
            ("/vote", "v1"), ("/vote выбрать", "v1"), ("/голосование", "v1"), ("/start vote", "v1"),
            ("/vote2", "v2"), ("/голосование2", "v2"),
        ):
            with self.subTest(text=text):
                self.assertEqual(self._route(text), [system])

    def test_a_panel_button_reaches_the_v3_handler_and_no_other(self):
        reached = []

        async def handle(*args, **kwargs):
            reached.append(args[4]["data"])

        async def go():
            callback = {
                "id": "cbq", "from": ADMIN, "message": {"message_id": 9},
                "data": bot_listener._nominations_action_callback_data("results", DM_CHAT_ID, ADMIN["id"]),
            }
            with patch.object(bot_listener, "handle_nominations_action_callback", handle):
                await bot_listener._dispatch_update(
                    {"callback_query": callback}, FakeApi(), None, _cfg(), None, BOT, 1, set(),
                    asyncio.Queue(), set(), CHAT, {}, {}, {}, {}, log=lambda *_: None,
                )

        _run(go())
        self.assertEqual(reached, [f"nomaction:results:{DM_CHAT_ID}:{ADMIN['id']}"])

    def test_the_admin_panel_button_opens_v3(self):
        item = admin_menu.action("vote3")
        self.assertEqual(item["kind"], "open")
        self.assertEqual(item["section"], admin_menu.action("vote")["section"])
        self.assertIn(item["command"], bot_listener.NOMINATIONS_COMMANDS)


class PanelTests(_Storage):
    def test_a_voter_gets_one_button_and_it_opens_the_tabs(self):
        api = self._type(VOTER, manager=False)
        buttons = _buttons(api.sent[0])
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0]["web_app"]["url"], "https://example.com/nominations")

    def test_an_administrator_gets_the_whole_panel_bound_to_them(self):
        api = self._type(ADMIN, manager=True)
        self.assertIn("тестовая версия", api.sent[0]["text"])
        buttons = _buttons(api.sent[0])
        urls = [b["web_app"]["url"] for b in buttons if "web_app" in b]
        self.assertEqual(urls, ["https://example.com/nominations",
                                "https://example.com/nominations?mode=admin"])
        actions = [bot_listener._parse_nominations_action_callback(b["callback_data"])
                   for b in buttons if "callback_data" in b]
        self.assertEqual(sorted(a[0] for a in actions), ["clear", "collect", "import", "results"])
        self.assertTrue(all(a[2] == ADMIN["id"] for a in actions))

    def test_the_panel_reports_v3s_numbers_not_v1s(self):
        def build(contest):
            nominations.add_entries(contest, [voting.Entry("1", 1, 1, "A", None, "", ["1_0.jpg"])])
            anime = nominations.add_nomination(contest, "Аниме")
            nominations.set_nomination_entries(contest, anime.nomination_id, ["1"])
            nominations.record_vote(contest, anime.nomination_id, 9, ["1"])

        nominations.update_contest(CHAT, build, create=True)
        text = self._type(ADMIN, manager=True).sent[0]["text"]
        self.assertIn("Работ собрано: 1 · номинаций: 1", text)
        self.assertIn("• Аниме — работ 1, проголосовало 1", text)

    def test_in_a_group_everybody_gets_the_deep_link(self):
        for user, manager in ((VOTER, False), (ADMIN, True)):
            buttons = _buttons(self._type(user, manager, chat_type="group").sent[0])
            self.assertEqual(len(buttons), 1)
            self.assertEqual(buttons[0]["url"], f"https://t.me/{BOT}?start=vote3")

    def test_an_administrators_subcommand_from_a_voter_is_refused(self):
        for text in ("/vote3 собрать", "/vote3 импорт", "/vote3 итоги", "/vote3 очистить",
                     "/vote3 очистить да", "/vote3 выбрать"):
            with self.subTest(text=text):
                self.assertIn("администратор", self._type(VOTER, False, text=text).sent[0]["text"])

    def test_clearing_asks_first_and_the_answer_is_bound_to_the_asker(self):
        nominations.update_contest(CHAT, lambda c: nominations.add_nomination(c, "Аниме"), create=True)
        api = self._type(ADMIN, manager=True, text="/vote3 очистить")
        self.assertTrue(nominations.contest_path(CHAT).exists())
        (button,) = _buttons(api.sent[0])
        self.assertEqual(bot_listener._parse_nominations_action_callback(button["callback_data"]),
                         ("clearyes", DM_CHAT_ID, ADMIN["id"]))

        self._type(ADMIN, manager=True, text=bot_listener.NOMINATIONS_ACTIONS["clearyes"])
        self.assertIsNone(nominations.load_contest(CHAT))

    def test_results_print_every_nomination(self):
        nominations.update_contest(CHAT, lambda c: nominations.add_nomination(c, "Аниме"), create=True)
        text = self._type(ADMIN, manager=True, text="/vote3 итоги").sent[0]["text"]
        self.assertIn("Аниме (проголосовало: 0)", text)


class ImportTests(_Storage):
    def test_importing_reads_v1_and_writes_nothing_of_it(self):
        poll = voting.Poll(
            poll_id="2026-W40", entry=CHAT, created_at="2026-10-01T00:00:00+00:00",
            entries=[voting.Entry("1", 1, 1, "A", "a", "", ["1_0.jpg"])],
        )
        voting.save_poll(poll)
        media = voting.media_path(CHAT, poll.poll_id)
        media.mkdir(parents=True)
        (media / "1_0.jpg").write_bytes(b"photo")
        before = voting.poll_path(CHAT, poll.poll_id).read_bytes()

        api = self._type(ADMIN, manager=True, text="/vote3 импорт")

        self.assertIn("Взял из основного голосования: 1", api.sent[0]["text"])
        self.assertEqual([e.entry_id for e in nominations.load_contest(CHAT).entries], ["1"])
        self.assertEqual(voting.poll_path(CHAT, poll.poll_id).read_bytes(), before)
        self.assertEqual((media / "1_0.jpg").read_bytes(), b"photo")
        self.assertEqual((nominations.media_path(CHAT) / "1_0.jpg").read_bytes(), b"photo")

        again = self._type(ADMIN, manager=True, text="/vote3 импорт")
        self.assertIn("уже здесь", again.sent[0]["text"])

    def test_with_nothing_collected_in_v1_it_says_how_else_to_fill_the_pool(self):
        api = self._type(ADMIN, manager=True, text="/vote3 импорт")
        self.assertIn("/vote3 собрать", api.sent[0]["text"])
        self.assertIsNone(nominations.load_contest(CHAT))


class BrokenModuleTests(unittest.TestCase):
    """A v3 that failed to import must answer, not raise -- and must not reach /vote."""

    def test_the_modules_are_imported_behind_their_own_guard(self):
        self.assertTrue(bot_listener.nominations_available())
        self.assertIsNone(bot_listener.NOMINATIONS_IMPORT_ERROR)

    def test_a_broken_v3_command_says_so(self):
        api = FakeApi()
        with patch.object(bot_listener, "NOMINATIONS_IMPORT_ERROR", "Traceback: boom"):
            _run(bot_listener.handle_nominations_command(
                api, None, _cfg(), None, _message(ADMIN), CHAT, BOT, set(), log=lambda *_: None,
            ))
        self.assertEqual(api.sent[0]["text"], bot_listener.NOMINATIONS_UNAVAILABLE_NOTICE)

    def test_a_broken_v3_button_still_stops_the_spinner(self):
        api = FakeApi()
        callback = {"id": "cbq", "from": ADMIN, "data": "nomaction:results:1:42"}
        with patch.object(bot_listener, "NOMINATIONS_IMPORT_ERROR", "Traceback: boom"):
            _run(bot_listener.handle_nominations_action_callback(
                api, None, _cfg(), None, callback, CHAT, BOT, set(), log=lambda *_: None,
            ))
        self.assertEqual(api.answered, [("cbq", bot_listener.NOMINATIONS_UNAVAILABLE_NOTICE)])


if __name__ == "__main__":
    unittest.main()
