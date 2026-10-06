"""hall_web.py -- the Hall of Fame site at /hall, mounted on the voting server.

Public and read-only. Each screen gets only what it draws; the disk is read in a worker
thread; pictures and avatars are served only for what is actually in the hall.
"""

import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hall_of_fame
import hall_web
import vote_web
import voting
from tests.async_case import AsyncTestCase

CHAT = "Chat"
P = hall_web.ROUTE_PREFIX


def _contest(contest_id, rows, hashtag=voting.CONTEST_HASHTAG, title="", badge="", when="2026-10-01"):
    """rows: (entry id, author id, name, votes), in place order -- each with one photo."""
    return hall_of_fame.Contest(
        contest_id=contest_id, entry=CHAT, hashtag=hashtag, title=title, badge=badge,
        closed_at=f"{when}T18:00:00+00:00", voters=12,
        works=[hall_of_fame.Work(entry_id=e, place=i, votes=v, author_id=a, author_name=n,
                                 author_username=f"user{a}" if a else None, text=f"работа {e}",
                                 message_id=int(e), photos=[f"{e}_0.jpg", f"{e}_1.jpg"], thumb=f"t_{e}_0.jpg")
               for i, (e, a, n, v) in enumerate(rows, start=1)],
    )


class HallApiTests(AsyncTestCase):
    async def asyncSetUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        self._patchers = [
            patch("voting._voting_dir", return_value=root / "voting"),
            patch("hall_of_fame._hall_dir", return_value=root / "hall"),
        ]
        for patcher in self._patchers:
            patcher.start()
        hall_of_fame._cache.clear()

        for contest in (
            _contest("2026-W39", [("1", 1, "Аня", 5), ("2", 2, "Боря", 3), ("3", 3, "Вера", 0)], when="2026-09-27"),
            _contest("2026-W40", [("4", 1, "Аня", 7), ("5", 2, "Боря", 6)], when="2026-10-04"),
            _contest("2026-W40-abcdef", [("6", 2, "Боря", 4), ("7", 1, "Аня", 1)],
                     hashtag="#аниме", title="Лучший аниме-покрас", badge="🌸", when="2026-10-05"),
            _contest("2026-W41", [("8", 3, "Вера", 0)], when="2026-10-06"),   # nobody voted
        ):
            hall_of_fame.save_contest(contest)
            media = hall_of_fame.media_dir(CHAT, contest.contest_id)
            media.mkdir(parents=True, exist_ok=True)
            for work in contest.works:
                for name in work.photos + [work.thumb]:
                    (media / name).write_bytes(b"jpeg")

        self.avatar_requests = []
        self.avatar_fails = set()

        async def avatar(user_id):
            self.avatar_requests.append(user_id)
            if user_id in self.avatar_fails:
                raise RuntimeError("telegram is down")
            return b"face" if user_id != 2 else None

        self.chat_lookups = 0

        async def chat():
            self.chat_lookups += 1
            return "echx_chat", -1001234

        app = vote_web.create_app(
            SimpleNamespace(telegram_bot_token="x"), CHAT, lambda user: False, log=lambda *_: None,
            attach=lambda a: hall_web.attach(a, CHAT, log=lambda *_: None, avatar=avatar, chat=chat,
                                             bot_username="echx_bot"),
        )
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        for patcher in self._patchers:
            patcher.stop()
        hall_of_fame._cache.clear()
        self._temporary.cleanup()

    async def _json(self, path, status=200):
        response = await self.client.get(P + path)
        self.assertEqual(response.status, status, await response.text())
        return await response.json()

    # ---- screens ------------------------------------------------------------------------

    async def test_the_front_page_has_the_winners_the_best_artists_and_the_themes(self):
        data = await self._json("/api/overview")
        self.assertEqual(data["stats"], {"contests": 4, "works": 8, "artists": 3, "themes": 1})
        # Newest first, and a contest nobody voted in has no winner to show.
        self.assertEqual([c["id"] for c in data["latest"]], ["2026-W40-abcdef", "2026-W40", "2026-W39"])
        self.assertEqual(data["latest"][0]["winner"]["author"]["name"], "Боря")
        self.assertEqual([a["name"] for a in data["artists"]], ["Аня", "Боря", "Вера"])
        self.assertEqual([c["title"] for c in data["themes"]], ["Лучший аниме-покрас"])
        self.assertEqual(data["bot"], "echx_bot")
        # The front page carries podiums, never a contest's whole field or its captions.
        for contest in data["latest"]:
            self.assertNotIn("entries", contest)
            for work in contest["podium"]:
                self.assertNotIn("text", work)
                self.assertNotIn("photos", work)

    async def test_the_chronology_lists_every_contest_with_its_podium(self):
        data = await self._json("/api/contests")
        self.assertEqual([c["id"] for c in data["contests"]],
                         ["2026-W41", "2026-W40-abcdef", "2026-W40", "2026-W39"])
        first = data["contests"][-1]
        self.assertEqual([w["place"] for w in first["podium"]], [1, 2])  # third had no votes
        self.assertEqual((first["works"], first["voters"], first["week"], first["badge"]), (3, 12, "2026-W39", "🏆"))
        self.assertEqual(data["contests"][1]["badge"], "🌸")
        self.assertIsNone(data["contests"][0]["winner"])

    async def test_a_contest_page_has_every_work_with_its_photos_and_post(self):
        data = (await self._json("/api/contests/2026-W39"))["contest"]
        self.assertEqual([w["id"] for w in data["entries"]], ["1", "2", "3"])
        work = data["entries"][0]
        self.assertEqual(work["photos"], [f"{P}/media/2026-W39/1_0.jpg", f"{P}/media/2026-W39/1_1.jpg"])
        self.assertEqual(work["thumb"], f"{P}/media/2026-W39/t_1_0.jpg")
        self.assertEqual(work["post_url"], "https://t.me/echx_chat/1")
        self.assertEqual(work["author"]["avatar"], f"{P}/avatar/1")
        self.assertEqual(work["author"]["telegram"], "https://t.me/user1")
        self.assertTrue(work["winner"])
        # Where the chat is is asked of the bot once, not per request.
        await self._json("/api/contests/2026-W40")
        await self._json("/api/artists/1")
        self.assertEqual(self.chat_lookups, 1)

    async def test_an_unknown_contest_or_artist_is_a_404(self):
        await self._json("/api/contests/2026-W01", status=404)
        await self._json("/api/artists/999", status=404)
        for path in ("/api/contests/..%2f..", "/api/artists/../x", "/api/artists/abc"):
            with self.subTest(path=path):
                response = await self.client.get(P + path)
                self.assertEqual(response.status, 404)

    async def test_an_artist_page_has_every_work_and_every_badge(self):
        data = (await self._json("/api/artists/1"))["artist"]
        self.assertEqual((data["rank"], data["wins"], data["podiums"], data["works"], data["votes"]), (1, 2, 3, 3, 13))
        self.assertEqual([w["contest"] for w in data["entries"]], ["2026-W40-abcdef", "2026-W40", "2026-W39"])
        self.assertEqual(data["entries"][0]["contest_title"], "Лучший аниме-покрас")
        self.assertEqual(data["badges"], [
            {"badge": "🏆", "title": "Итоги недели", "weekly": True, "count": 2, "contests": ["2026-W40", "2026-W39"]},
        ])
        boria = (await self._json("/api/artists/2"))["artist"]
        self.assertEqual([(b["badge"], b["title"], b["count"]) for b in boria["badges"]],
                         [("🌸", "Лучший аниме-покрас", 1)])

    async def test_the_artist_list_is_the_whole_leaderboard(self):
        data = await self._json("/api/artists")
        self.assertEqual([(a["rank"], a["key"]) for a in data["artists"]], [(1, "1"), (2, "2"), (3, "3")])
        self.assertNotIn("entries", data["artists"][0])

    # ---- pictures -------------------------------------------------------------------------

    async def test_photos_are_served_and_nothing_outside_the_hall_is(self):
        response = await self.client.get(f"{P}/media/2026-W39/1_0.jpg")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), b"jpeg")
        voting._voting_dir().mkdir(parents=True, exist_ok=True)
        (voting._voting_dir() / "secret.json").write_text("{}")
        for path in ("/media/2026-W39/..%2F..%2F..%2Fvoting%2Fsecret.json", "/media/2026-W39/none.jpg",
                     "/media/..%2F/1_0.jpg"):
            with self.subTest(path=path):
                self.assertEqual((await self.client.get(P + path)).status, 404)

    async def test_an_avatar_is_served_only_for_somebody_in_the_hall_and_cached(self):
        first = await self.client.get(f"{P}/avatar/1")
        self.assertEqual((first.status, await first.read()), (200, b"face"))
        await self.client.get(f"{P}/avatar/1")
        self.assertEqual(self.avatar_requests, [1])
        self.assertEqual((await self.client.get(f"{P}/avatar/777")).status, 404)   # not in the hall
        self.assertEqual(self.avatar_requests, [1])
        self.assertEqual((await self.client.get(f"{P}/avatar/2")).status, 404)     # has no photo

    async def test_a_failed_avatar_fetch_is_retried_next_time(self):
        self.avatar_fails.add(3)
        self.assertEqual((await self.client.get(f"{P}/avatar/3")).status, 503)
        self.avatar_fails.clear()
        self.assertEqual((await self.client.get(f"{P}/avatar/3")).status, 200)
        self.assertEqual(self.avatar_requests, [3, 3])

    # ---- the page -------------------------------------------------------------------------

    async def test_the_page_is_served_with_its_prefix(self):
        response = await self.client.get(P)
        self.assertEqual(response.status, 200)
        page = await response.text()
        self.assertIn(f'const PREFIX = "{P}"', page)
        self.assertNotIn("__PREFIX__", page)
        self.assertNotIn("__BRAND__", page)

    async def test_a_stalled_hall_read_does_not_hold_up_the_ballot(self):
        """The hall is read on a worker thread: a snapshot stuck on a slow disk costs that
        one request, never the event loop the ballots are served from."""
        reached, release = threading.Event(), threading.Event()
        original = hall_of_fame.snapshot

        def stalled(entry):
            reached.set()
            release.wait(5)
            return original(entry)

        with patch("hall_of_fame.snapshot", stalled):
            stuck = asyncio.ensure_future(self.client.get(f"{P}/api/overview"))
            try:
                self.assertTrue(await asyncio.to_thread(reached.wait, 5))
                other = await asyncio.wait_for(self.client.get(f"{vote_web.ROUTE_PREFIX}/api/public"), timeout=5)
                self.assertEqual(other.status, 200)
                self.assertFalse(stuck.done())
            finally:
                release.set()
            self.assertEqual((await asyncio.wait_for(stuck, timeout=5)).status, 200)


class EmptyHallTests(AsyncTestCase):
    async def test_an_empty_hall_answers_with_nothing_rather_than_failing(self):
        with tempfile.TemporaryDirectory() as root, \
                patch("hall_of_fame._hall_dir", return_value=Path(root) / "hall"):
            hall_of_fame._cache.clear()
            app = vote_web.create_app(SimpleNamespace(telegram_bot_token="x"), CHAT, lambda u: False,
                                      log=lambda *_: None,
                                      attach=lambda a: hall_web.attach(a, CHAT, log=lambda *_: None))
            async with TestClient(TestServer(app)) as client:
                data = await (await client.get(f"{P}/api/overview")).json()
                self.assertEqual(data["stats"], {"contests": 0, "works": 0, "artists": 0, "themes": 0})
                self.assertEqual((data["latest"], data["artists"], data["themes"]), ([], [], []))
                contests = await (await client.get(f"{P}/api/contests")).json()
                self.assertEqual(contests, {"contests": []})
            hall_of_fame._cache.clear()


class PostLinkTests(unittest.TestCase):
    def test_a_private_supergroup_links_by_its_number_and_a_basic_group_not_at_all(self):
        async def run(answer):
            app = {hall_web._CHAT_CACHE_KEY: {}, hall_web._LOG_KEY: lambda *_: None}

            async def chat():
                return answer

            app[hall_web._CHAT_KEY] = chat
            return await hall_web._link_base(SimpleNamespace(app=app))

        self.assertEqual(asyncio.run(run(("echx", None))), "https://t.me/echx")
        self.assertEqual(asyncio.run(run((None, -1001234))), "https://t.me/c/1234")
        self.assertIsNone(asyncio.run(run((None, -1234))))


class HeroPhotoTests(unittest.TestCase):
    """The latest winner is shown whole: a tall miniature photo cut to a fixed frame lost
    its top and bottom on the front page."""

    def test_the_winners_photo_is_never_cropped_to_a_frame(self):
        page = hall_web.PAGE_HTML
        rule = re.search(r"\.heroPic img\.main \{([^}]*)\}", page).group(1)
        self.assertNotIn("object-fit: cover", rule)
        self.assertIn("max-width: 100%", rule)
        self.assertIn("height: auto", rule)
        hero = re.search(r"\.heroPic \{([^}]*)\}", page).group(1)
        self.assertNotIn("aspect-ratio", hero)
        # The full photo, not the square cover cut from it.
        self.assertIn("const heroPhoto = win.photo || win.thumb;", page)

    def test_the_podium_sits_under_the_winner_not_at_the_bottom_of_the_card(self):
        page = hall_web.PAGE_HTML
        self.assertNotIn("margin-top: auto", re.search(r"\.podiumMini \{([^}]*)\}", page).group(1))
        self.assertIn("align-self: start", re.search(r"\.heroInfo \{([^}]*)\}", page).group(1))


class PageScriptSyntaxTests(unittest.TestCase):
    """One broken literal and the page opens blank while every route still answers 200."""

    def test_the_page_is_valid_javascript(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not available to parse the page")
        scripts = re.findall(r"<script>(.*?)</script>", hall_web.PAGE_HTML, re.S)
        self.assertEqual(len(scripts), 1)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
            handle.write(scripts[0].replace("__PREFIX__", P).replace("__BRAND__", "ЕЧХ"))
            path = handle.name
        try:
            result = subprocess.run([node, "--check", path], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
