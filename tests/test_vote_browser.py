"""Voting without the Mini App: the browser page, and the bot taking the ballot it sends.

Some people's Telegram will not open the Mini App. For them there is a plain page for a
phone's browser -- the same works, the same close look -- which cannot vote by itself:
it has nobody to vote AS. Its last step is a t.me link to the bot carrying the choices,
and the ballot is cast by the Telegram account that sends it. So what is pinned here:

- the page and its data need no Telegram at all, and the data is only what the chat saw;
- the page's link and the bot agree on the format;
- a ballot that arrives that way keeps every rule a Mini App ballot keeps;
- every v1 vote button now has "Если Бот не работает" beside it.
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_listener
import vote_web
import voting
from tests.async_case import AsyncTestCase

CHAT = "Chat"
BOT = "testbot"
DM = 555
MAIN_CHAT_ID = -1001234567890
ADMIN = {"id": 42, "username": "admin"}
VOTER = {"id": 77, "username": "voter"}
BROWSER_URL = "https://example.com/vote/web"


def _cfg():
    return SimpleNamespace(webapp_public_url="https://example.com", vote_miniapp_short_name=None,
                           vote_announce_extra_chat=None, telegram_bot_token="1:TOKEN")


def _entry(entry_id, name=None):
    return voting.Entry(entry_id=str(entry_id), message_id=int(entry_id), author_id=int(entry_id),
                        author_name=name or f"Автор {entry_id}", author_username=f"user{entry_id}",
                        text="подпись", media=[f"{entry_id}.jpg"])


def _seed(approved=("101", "202"), open_=True, max_choices=None, allow_revote=True):
    poll = voting.Poll(poll_id="2026-W41", entry=CHAT, created_at="2026-10-05T00:00:00+00:00",
                       entries=[_entry(101), _entry(202), _entry(303)])
    voting.set_approved(poll, list(approved))
    poll.open, poll.max_choices, poll.allow_revote = open_, max_choices, allow_revote
    voting.save_poll(poll)
    return poll


class _Storage:
    def _storage(self):
        self._temporary = tempfile.TemporaryDirectory()
        self._patcher = patch("voting._voting_dir", return_value=Path(self._temporary.name))
        self._patcher.start()

    def _unstorage(self):
        self._patcher.stop()
        self._temporary.cleanup()


# ------------------------------------------------------------------------- the link

class LinkFormatTests(unittest.TestCase):
    def test_choices_survive_the_round_trip(self):
        ids = ["12961", "1", "999999999"]
        payload = voting.encode_ballot_link(ids)
        self.assertTrue(re.fullmatch(r"vote-[0-9a-z-]+", payload))
        self.assertEqual(voting.decode_ballot_link(payload), ids)
        self.assertEqual(voting.decode_ballot_link(payload.upper()), ids)  # the bot lowercases anyway

    def test_a_weeks_worth_of_choices_fits_in_one_start_link(self):
        """Message ids in this chat are six digits; ten of them still fit Telegram's 64."""
        payload = voting.encode_ballot_link([str(150000 + i) for i in range(10)])
        self.assertLessEqual(len(payload), voting.BALLOT_LINK_MAX)

    def test_anything_else_is_not_a_ballot(self):
        for payload in ("vote", "vote-", "vote_admin", "vote2", "vote-12-ж", "vote-1--2",
                        "cabinet", "", "vote-" + "z" * 13):
            with self.subTest(payload=payload):
                self.assertIsNone(voting.decode_ballot_link(payload))

    def test_a_repeated_choice_counts_once(self):
        self.assertEqual(voting.decode_ballot_link("vote-a-a-b"), ["10", "11"])

    def test_the_page_builds_exactly_the_link_the_bot_reads(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not available to run the page's encoder")
        source = vote_web.BROWSER_HTML
        function = re.search(r"function ballotPayload\(ids\) \{.*?\n\}", source, re.S).group(0)
        ids = ["12961", "1", "35", "36", "150042", "999999999"]
        script = function + "\nprocess.stdout.write(ballotPayload(" + json.dumps(ids) + "));"
        result = subprocess.run([node, "-e", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, voting.encode_ballot_link(ids))


# --------------------------------------------------------------------- the web side

class BrowserPageTests(_Storage, AsyncTestCase):
    async def asyncSetUp(self):
        self._storage()

        async def is_admin(user):
            return False

        app = vote_web.create_app(_cfg(), CHAT, is_admin, log=lambda *_: None, bot_username="@" + BOT)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self._unstorage()

    async def _public(self):
        response = await self.client.get("/vote/api/public")
        self.assertEqual(response.status, 200)
        return await response.json()

    async def test_the_page_needs_no_telegram(self):
        response = await self.client.get("/vote/web")
        self.assertEqual(response.status, 200)
        page = await response.text()
        self.assertNotIn("telegram-web-app.js", page)
        self.assertIn('const PREFIX = "/vote";', page)

    async def test_the_data_needs_no_signature_and_carries_only_what_the_chat_saw(self):
        poll = _seed()
        voting.record_vote(poll, 9, ["101"])
        voting.set_crops(poll, {"101": {"x": 1, "y": 2, "size": 3}})
        voting.save_poll(poll)

        data = await self._public()

        self.assertEqual([e["id"] for e in data["entries"]], ["101", "202"])  # 303 is not admitted
        self.assertEqual(data["bot"], BOT)
        self.assertEqual(data["voter_count"], 1)
        self.assertEqual(data["entries"][0]["crop"], {"x": 1.0, "y": 2.0, "size": 3.0})
        for secret in ("votes", "results", "counts", "my_vote", "approved", "crops"):
            self.assertNotIn(secret, data)
        self.assertIsNone(data["winner"])

    async def test_a_closed_vote_shows_its_winner(self):
        poll = _seed()
        voting.record_vote(poll, 9, ["202"])
        voting.close_and_announce(poll)
        voting.save_poll(poll)
        data = await self._public()
        self.assertFalse(data["open"])
        self.assertEqual(data["winner"]["id"], "202")

    async def test_a_thematic_contest_is_named_on_its_ballot(self):
        poll = _seed()
        self.assertEqual((await self._public())["title"], "Итоги недели")
        poll.hashtag, poll.title = "#аниме", "Лучший аниме-покрас"
        voting.save_poll(poll)
        vote_web_cache = self.client.server.app[vote_web._PUBLIC_CACHE_KEY]
        vote_web_cache.clear()
        data = await self._public()
        self.assertEqual((data["title"], data["hashtag"]), ("Лучший аниме-покрас", "#аниме"))
        self.assertIn('$("eyebrow").textContent', vote_web.BROWSER_HTML)
        self.assertIn('$("title").textContent = poll.title', vote_web.PAGE_HTML)

    async def test_no_poll_is_said_plainly(self):
        data = await self._public()
        self.assertIsNone(data["poll_id"])
        self.assertEqual(data["entries"], [])

    async def test_a_crowd_of_requests_reads_the_disk_once(self):
        _seed()
        calls = []
        real = voting.latest_poll

        def counting(entry):
            calls.append(entry)
            return real(entry)

        with patch.object(voting, "latest_poll", counting):
            for _ in range(5):
                await self._public()
        self.assertEqual(len(calls), 1)

    async def test_the_mini_app_routes_still_refuse_an_unsigned_caller(self):
        _seed()
        self.assertEqual((await self.client.get("/vote/api/poll")).status, 401)
        response = await self.client.post("/vote/api/ballot", json={"choices": ["101"]})
        self.assertEqual(response.status, 401)


class BrowserPageScriptTests(unittest.TestCase):
    def test_the_page_is_valid_javascript(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not available to parse the page")
        scripts = re.findall(r"<script>(.*?)</script>", vote_web.BROWSER_HTML, re.S)
        self.assertEqual(len(scripts), 1)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
            handle.write(scripts[0])
            path = handle.name
        try:
            result = subprocess.run([node, "--check", path], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            os.unlink(path)

    def test_every_voting_page_closes_its_picture_on_esc_and_on_the_space_around_it(self):
        """The ✕ is not the only way out: Esc steps back a layer, a click on the black
        around a picture closes it at any zoom, and so does a click on the reel's empty
        space. Checked with real key and mouse events in a browser when it was written;
        pinned here so none of the three pages quietly loses it."""
        import nominations_web

        pages = {
            "browser": vote_web.BROWSER_HTML,
            "v1 mini app": vote_web.PAGE_HTML,
            "v3 nominations": nominations_web.PAGE_HTML,
        }
        for name, page in pages.items():
            with self.subTest(page=name):
                self.assertIn('event.key === "Escape"' if name == "browser" else 'event.key !== "Escape"', page)
                self.assertIn('$("lensImg").getBoundingClientRect()', page)
                self.assertIn('$("reel").addEventListener("click"', page)

    def test_the_mini_app_page_is_valid_javascript_too(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not available to parse the page")
        scripts = re.findall(r"<script>(.*?)</script>", vote_web.PAGE_HTML, re.S)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
            handle.write("\n".join(scripts))
            path = handle.name
        try:
            result = subprocess.run([node, "--check", path], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            os.unlink(path)

    def test_the_page_tells_people_how_the_vote_reaches_the_bot(self):
        page = vote_web.BROWSER_HTML
        self.assertIn("Открыть Telegram и проголосовать", page)
        self.assertIn('"https://t.me/" + encodeURIComponent(bot) + "?start=" + payload', page)
        self.assertIn('"/start " + payload', page)  # the copy-and-send fallback


# ---------------------------------------------------------------------- the bot side

class FakeApi:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_to_message_id=None, reply_markup=None,
                           parse_mode=None, disable_notification=False):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"message_id": 100 + len(self.sent)}

    async def answer_callback_query(self, callback_id, text=None):
        pass


def _buttons(item):
    return [b for row in (item["reply_markup"] or {}).get("inline_keyboard", []) for b in row]


class LinkBallotTests(_Storage, unittest.TestCase):
    def setUp(self):
        self._storage()
        self.subscribed = True

    def tearDown(self):
        self._unstorage()

    def _send(self, ids, user=VOTER):
        api = FakeApi()
        message = {"message_id": 1, "chat": {"id": DM, "type": "private"}, "from": user}

        async def resolve(*args, **kwargs):
            return MAIN_CHAT_ID

        async def member(api_, chat_id, user_id):
            return self.subscribed

        payload = voting.encode_ballot_link(ids) if isinstance(ids, list) else ids
        with patch.object(bot_listener, "_resolve_chat_id", resolve), \
                patch.object(bot_listener, "_is_chat_member", member):
            asyncio.run(bot_listener.handle_vote_link_ballot(
                api, None, message, CHAT, payload, log=lambda *_: None))
        return api.sent[-1]

    def _poll(self):
        return voting.load_poll(CHAT, "2026-W41")

    def test_the_sender_is_the_voter(self):
        _seed()
        reply = self._send(["101", "202"])
        self.assertEqual(self._poll().votes, {str(VOTER["id"]): ["101", "202"]})
        self.assertEqual(self._poll().subscriber_votes, {str(VOTER["id"]): True})
        self.assertIn("Ваш голос учтён", reply["text"])
        self.assertIn("1. Автор 101 (@user101)", reply["text"])
        self.assertIsNone(reply["reply_markup"])

    def test_it_replaces_a_ballot_cast_in_the_mini_app(self):
        poll = _seed()
        voting.record_vote(poll, VOTER["id"], ["101"])
        voting.save_poll(poll)
        self._send(["202"])
        self.assertEqual(self._poll().votes, {str(VOTER["id"]): ["202"]})

    def test_a_non_subscriber_is_counted_and_invited(self):
        _seed()
        self.subscribed = False
        reply = self._send(["101"])
        self.assertEqual(self._poll().subscriber_votes, {str(VOTER["id"]): False})
        self.assertEqual(_buttons(reply)[0]["url"], vote_web.SUBSCRIBE_URL)

    def test_a_closed_vote_takes_nothing(self):
        _seed(open_=False)
        self.assertIn("закрыто", self._send(["101"])["text"])
        self.assertEqual(self._poll().votes, {})

    def test_a_work_no_longer_admitted_is_dropped_and_said_so(self):
        _seed()
        reply = self._send(["101", "303"])
        self.assertEqual(self._poll().votes, {str(VOTER["id"]): ["101"]})
        self.assertIn("пропущено: 1", reply["text"])

    def test_a_link_with_nothing_valid_left_never_wipes_a_ballot(self):
        poll = _seed()
        voting.record_vote(poll, VOTER["id"], ["101"])
        voting.save_poll(poll)
        reply = self._send(["303"])
        self.assertIn("больше нет", reply["text"])
        self.assertEqual(self._poll().votes, {str(VOTER["id"]): ["101"]})

    def test_a_locked_ballot_stays_locked(self):
        poll = _seed(allow_revote=False)
        voting.record_vote(poll, VOTER["id"], ["101"])
        voting.save_poll(poll)
        self.assertIn("нельзя", self._send(["202"])["text"])
        self.assertEqual(self._poll().votes, {str(VOTER["id"]): ["101"]})

    def test_going_over_the_cap_is_refused_not_trimmed(self):
        _seed(max_choices=1)
        self.assertIn("не более 1", self._send(["101", "202"])["text"])
        self.assertEqual(self._poll().votes, {})

    def test_a_mangled_link_is_explained(self):
        _seed()
        self.assertIn("Не понял ссылку", self._send("vote-not!valid")["text"])
        self.assertEqual(self._poll().votes, {})


class LinkRoutingTests(unittest.TestCase):
    def _route(self, text, chat_type="private"):
        reached = []

        def recorder(name):
            async def handle(*args, **kwargs):
                reached.append((name, args))
            return handle

        async def go():
            tasks = set()
            update = {"message": {"message_id": 5, "chat": {"id": DM, "type": chat_type},
                                  "from": VOTER, "text": text}}
            cfg = SimpleNamespace(webapp_public_url="https://example.com", listener_allowed_chats=[],
                                  stats_enabled=True, stats_top_limit=10, vote_miniapp_short_name=None,
                                  vote_announce_extra_chat=None)
            with patch.object(bot_listener, "handle_vote_link_ballot", recorder("link")), \
                    patch.object(bot_listener, "handle_vote_command", recorder("vote")):
                await bot_listener._dispatch_update(
                    update, FakeApi(), None, cfg, None, BOT, 1, set(), asyncio.Queue(),
                    tasks, CHAT, {}, {}, {}, {}, log=lambda *_: None)
                await asyncio.gather(*tasks)

        asyncio.run(go())
        return reached

    def test_a_ballot_link_reaches_the_ballot_handler_with_its_payload(self):
        reached = self._route("/start vote-2x1-3a")
        self.assertEqual([name for name, _ in reached], ["link"])
        self.assertEqual(reached[0][1][4], "vote-2x1-3a")

    def test_the_plain_vote_link_still_opens_the_vote(self):
        self.assertEqual([name for name, _ in self._route("/start vote")], ["vote"])

    def test_a_ballot_link_in_a_group_is_ignored(self):
        self.assertEqual(self._route("/start vote-2x1", chat_type="group"), [])


class FallbackButtonTests(unittest.TestCase):
    """"Если Бот не работает" beside every v1 vote button, leading to the browser page."""

    def _type(self, user, manager, chat_type="private"):
        api = FakeApi()
        message = {"message_id": 5, "from": user, "text": "/vote",
                   "chat": {"id": DM if chat_type == "private" else MAIN_CHAT_ID, "type": chat_type}}

        async def resolve(*args, **kwargs):
            return MAIN_CHAT_ID

        async def can_manage(*args, **kwargs):
            return manager

        with patch.object(bot_listener, "_resolve_chat_id", resolve), \
                patch.object(bot_listener, "_can_manage_chat", can_manage), \
                patch.object(voting, "latest_poll", lambda entry: None):
            asyncio.run(bot_listener.handle_vote_command(
                api, None, _cfg(), None, message, CHAT, BOT, set(), log=lambda *_: None))
        return api.sent[0]

    def _fallback(self, item):
        return [b for b in _buttons(item) if b["text"] == bot_listener.VOTE_BROWSER_BUTTON_TEXT]

    def test_a_voter_gets_the_mini_app_the_page_and_the_address_to_copy(self):
        reply = self._type(VOTER, manager=False)
        self.assertIn("web_app", _buttons(reply)[0])
        self.assertEqual(self._fallback(reply), [{"text": "Если Бот не работает", "url": BROWSER_URL}])
        self.assertIn(BROWSER_URL, reply["text"])

    def test_the_administrators_panel_has_it_too(self):
        self.assertEqual(len(self._fallback(self._type(ADMIN, manager=True))), 1)

    def test_the_group_reply_has_it(self):
        self.assertEqual(len(self._fallback(self._type(VOTER, manager=False, chat_type="group"))), 1)

    def test_v1s_announcement_carries_it_and_the_arenas_does_not(self):
        def post(system):
            api = FakeApi()
            flow = {"chat_id": DM, "user_id": ADMIN["id"], "entry": CHAT, "admin_chat_id": MAIN_CHAT_ID,
                    "prompt_message_id": 7, "created_at": time.monotonic(), "text": "Голосуем", "system": system}
            press = {"id": "cbq", "from": ADMIN, "message": {"message_id": 9},
                     "data": bot_listener._vote_chat_dest_callback_data("main", "f1")}

            async def can_manage(*args, **kwargs):
                return True

            with patch.object(bot_listener, "_can_manage_chat", can_manage):
                asyncio.run(bot_listener.handle_vote_chat_destination_callback(
                    api, _cfg(), press, {"f1": flow}, BOT, log=lambda *_: None))
            return next(item for item in api.sent if item["chat_id"] == MAIN_CHAT_ID)

        self.assertEqual(len(self._fallback(post("vote"))), 1)
        self.assertEqual(self._fallback(post("arena")), [])

    def test_without_a_public_address_there_is_no_button_to_a_page_that_is_not_served(self):
        self.assertEqual(bot_listener._vote_browser_row(SimpleNamespace(webapp_public_url=None)), [])


if __name__ == "__main__":
    unittest.main()
