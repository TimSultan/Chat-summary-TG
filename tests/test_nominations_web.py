"""nominations_web.py: vote v3 over HTTP, mounted the way production mounts it -- onto the
application vote_web builds -- so these also prove it sits beside /vote without touching it.

What is pinned: every route authenticates; a voter is sent the nominations that have works
and only those works (never the pool); standings are earned per nomination; only an
administrator can edit; a v3 write that is stuck does not hold up a v1 request; and nothing
v3 does changes a byte of v1's poll.
"""

import asyncio
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import nominations
import nominations_web
import vote_web
import voting
from tests.async_case import AsyncTestCase

BOT_TOKEN = "123456:FAKE-TOKEN-FOR-TESTS"
CHAT = "Chat"
API = nominations_web.ROUTE_PREFIX + "/api"
ADMIN, VOTER, OTHER = 1, 2, 3


def _init_data(user_id: int) -> str:
    fields = {
        "auth_date": str(int(time.time())),
        "user": json.dumps({"id": user_id, "username": f"u{user_id}", "first_name": "V"}),
    }
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    return urlencode(fields)


def _entry(entry_id) -> voting.Entry:
    return voting.Entry(
        entry_id=str(entry_id), message_id=int(entry_id), author_id=int(entry_id),
        author_name=f"Автор {entry_id}", author_username=f"user{entry_id}", text="подпись",
        media=[f"{entry_id}_0.jpg"],
    )


class NominationsApiTests(AsyncTestCase):
    async def asyncSetUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        self._patchers = [
            patch("nominations._nominations_dir", return_value=root / "nominations"),
            patch("voting._voting_dir", return_value=root / "voting"),
        ]
        for patcher in self._patchers:
            patcher.start()
        cfg = SimpleNamespace(telegram_bot_token=BOT_TOKEN)

        async def is_admin(user):
            return user.get("id") == ADMIN

        app = vote_web.create_app(
            cfg, CHAT, is_admin, log=lambda *_: None,
            attach=lambda a: nominations_web.attach(a, cfg, CHAT, is_admin, log=lambda *_: None),
        )
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        for patcher in self._patchers:
            patcher.stop()
        self._temporary.cleanup()

    # ---- helpers ------------------------------------------------------------------------

    def _seed(self):
        """A pool of four works; "Аниме" plays 1 and 2, "Фэнтези" plays 2 and 3, and
        "Пустая" has none. Work 4 is in the pool and in no nomination."""
        def build(contest):
            nominations.add_entries(contest, [_entry(i) for i in (4, 3, 2, 1)])
            ids = {}
            for name, members in (("Аниме", ["1", "2"]), ("Фэнтези", ["2", "3"]), ("Пустая", [])):
                nomination = nominations.add_nomination(contest, name)
                nominations.set_nomination_entries(contest, nomination.nomination_id, members)
                ids[name] = nomination.nomination_id
            return ids

        _, ids = nominations.update_contest(CHAT, build, create=True)
        return ids

    async def _get(self, user, mode=None):
        path = f"{API}/state" + (f"?mode={mode}" if mode else "")
        headers = {"X-Telegram-Init-Data": _init_data(user)} if user else {}
        return await self.client.get(path, headers=headers)

    async def _post(self, route, user, **body):
        return await self.client.post(f"{API}/{route}", json={"init_data": _init_data(user), **body})

    # ---- authentication -----------------------------------------------------------------

    async def test_every_route_refuses_an_unsigned_caller(self):
        self._seed()
        self.assertEqual((await self._get(None)).status, 401)
        for route in ("ballot", "nominations/create", "nominations/update",
                      "nominations/delete", "settings"):
            with self.subTest(route=route):
                response = await self.client.post(f"{API}/{route}", json={"init_data": "hash=forged"})
                self.assertEqual(response.status, 401)

    async def test_the_page_is_served_with_its_own_prefix(self):
        response = await self.client.get(nominations_web.ROUTE_PREFIX)
        self.assertEqual(response.status, 200)
        self.assertIn('const PREFIX = "/nominations";', await response.text())

    # ---- what a voter is sent -----------------------------------------------------------

    async def test_with_no_contest_a_voter_is_told_so(self):
        data = await (await self._get(VOTER)).json()
        self.assertEqual(data["exists"], False)
        self.assertEqual(data["nominations"], [])

    async def test_a_voter_gets_only_nominations_with_works_and_only_their_works(self):
        self._seed()
        data = await (await self._get(VOTER)).json()
        self.assertEqual([n["name"] for n in data["nominations"]], ["Аниме", "Фэнтези"])
        # Work 4 is pool-only: the administrator's raw material, never sent to a voter.
        self.assertEqual([e["id"] for e in data["entries"]], ["3", "2", "1"])
        self.assertEqual(data["entries"][0]["photos"], ["/nominations/media/3_0.jpg"])
        self.assertFalse(data["is_admin"])
        self.assertFalse(data["can_moderate"])

    async def test_asking_for_the_admin_view_is_not_enough_to_get_it(self):
        self._seed()
        data = await (await self._get(VOTER, mode="admin")).json()
        self.assertFalse(data["is_admin"])
        self.assertEqual(len(data["entries"]), 3)

    async def test_standings_are_earned_one_nomination_at_a_time(self):
        ids = self._seed()
        data = await (await self._get(VOTER)).json()
        self.assertTrue(all(n["results"] is None for n in data["nominations"]))

        response = await self._post("ballot", VOTER, nomination_id=ids["Аниме"], choices=["2"])
        self.assertEqual(response.status, 200)
        answer = await response.json()
        self.assertEqual(answer["nomination"]["my_vote"], ["2"])
        self.assertEqual(answer["nomination"]["results"], [{"id": "2", "votes": 1}, {"id": "1", "votes": 0}])

        data = {n["name"]: n for n in (await (await self._get(VOTER)).json())["nominations"]}
        self.assertIsNotNone(data["Аниме"]["results"])
        self.assertIsNone(data["Фэнтези"]["results"])
        self.assertEqual(data["Фэнтези"]["my_vote"], [])

    async def test_a_ballot_in_one_nomination_leaves_the_others_alone(self):
        ids = self._seed()
        await self._post("ballot", VOTER, nomination_id=ids["Фэнтези"], choices=["3"])
        await self._post("ballot", VOTER, nomination_id=ids["Аниме"], choices=["1"])
        await self._post("ballot", VOTER, nomination_id=ids["Аниме"], choices=["2"])
        contest = nominations.load_contest(CHAT)
        self.assertEqual(contest.nomination(ids["Аниме"]).votes, {str(VOTER): ["2"]})
        self.assertEqual(contest.nomination(ids["Фэнтези"]).votes, {str(VOTER): ["3"]})

    async def test_a_ballot_over_the_cap_or_after_closing_is_refused(self):
        ids = self._seed()
        await self._post("settings", ADMIN, max_choices=1)
        response = await self._post("ballot", VOTER, nomination_id=ids["Аниме"], choices=["1", "2"])
        self.assertEqual(response.status, 400)
        await self._post("settings", ADMIN, open=False)
        response = await self._post("ballot", VOTER, nomination_id=ids["Аниме"], choices=["1"])
        self.assertEqual(response.status, 409)
        self.assertEqual(nominations.load_contest(CHAT).nomination(ids["Аниме"]).votes, {})

    async def test_a_ballot_for_a_deleted_nomination_is_a_404(self):
        self._seed()
        response = await self._post("ballot", VOTER, nomination_id="gone", choices=["1"])
        self.assertEqual(response.status, 404)

    # ---- administration -----------------------------------------------------------------

    async def test_only_an_administrator_can_edit(self):
        ids = self._seed()
        for route, body in (
            ("nominations/create", {"name": "Новая"}),
            ("nominations/update", {"nomination_id": ids["Аниме"], "name": "X"}),
            ("nominations/delete", {"nomination_id": ids["Аниме"]}),
            ("settings", {"open": False}),
        ):
            with self.subTest(route=route):
                self.assertEqual((await self._post(route, VOTER, **body)).status, 403)
        contest = nominations.load_contest(CHAT)
        self.assertEqual([n.name for n in contest.nominations], ["Аниме", "Фэнтези", "Пустая"])
        self.assertTrue(contest.open)

    async def test_the_admin_view_carries_the_whole_pool_and_every_count(self):
        ids = self._seed()
        await self._post("ballot", VOTER, nomination_id=ids["Фэнтези"], choices=["3"])
        data = await (await self._get(ADMIN, mode="admin")).json()
        self.assertTrue(data["is_admin"])
        self.assertEqual([e["id"] for e in data["entries"]], ["4", "3", "2", "1"])
        by_name = {n["name"]: n for n in data["nominations"]}
        self.assertEqual(list(by_name), ["Аниме", "Фэнтези", "Пустая"])
        self.assertEqual(by_name["Фэнтези"]["results"][0], {"id": "3", "votes": 1})
        self.assertEqual(by_name["Фэнтези"]["voter_count"], 1)

    async def test_an_administrator_can_build_a_nomination_from_nothing(self):
        # Before anything is collected: the nomination is named now, filled later.
        response = await self._post("nominations/create", ADMIN, name="  Аниме ")
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual([n["name"] for n in data["state"]["nominations"]], ["Аниме"])
        self.assertEqual(data["nomination_id"], data["state"]["nominations"][0]["id"])
        self.assertTrue(data["state"]["is_admin"])

        nominations.update_contest(CHAT, lambda c: nominations.add_entries(c, [_entry(2), _entry(1)]))
        response = await self._post(
            "nominations/update", ADMIN, nomination_id=data["nomination_id"], entry_ids=["1", "2"],
        )
        state = (await response.json())["state"]
        # Stored in POOL order, not the order they were sent in.
        self.assertEqual(state["nominations"][0]["entry_ids"], ["2", "1"])

        response = await self._post("nominations/update", ADMIN, nomination_id=data["nomination_id"], name="Аниме и манга")
        self.assertEqual((await response.json())["state"]["nominations"][0]["name"], "Аниме и манга")

    async def test_a_duplicate_or_empty_name_is_refused_with_a_reason(self):
        self._seed()
        response = await self._post("nominations/create", ADMIN, name="аниме")
        self.assertEqual(response.status, 409)
        self.assertIn("уже есть", (await response.json())["error"])
        self.assertEqual((await self._post("nominations/create", ADMIN, name="  ")).status, 400)

    async def test_deleting_a_nomination_takes_its_ballots_and_nothing_else(self):
        ids = self._seed()
        await self._post("ballot", VOTER, nomination_id=ids["Аниме"], choices=["1"])
        await self._post("ballot", VOTER, nomination_id=ids["Фэнтези"], choices=["3"])
        response = await self._post("nominations/delete", ADMIN, nomination_id=ids["Аниме"])
        self.assertEqual(response.status, 200)
        contest = nominations.load_contest(CHAT)
        self.assertEqual([n.name for n in contest.nominations], ["Фэнтези", "Пустая"])
        self.assertEqual(contest.nomination(ids["Фэнтези"]).votes, {str(VOTER): ["3"]})
        self.assertEqual(len(contest.entries), 4)

    async def test_a_nonsense_setting_is_refused(self):
        self._seed()
        for value in (0, -1, "3", True, 1.5):
            with self.subTest(value=value):
                self.assertEqual((await self._post("settings", ADMIN, max_choices=value)).status, 400)
        self.assertIsNone(nominations.load_contest(CHAT).max_choices)

    async def test_a_body_that_is_not_an_object_is_refused(self):
        response = await self.client.post(f"{API}/ballot", data="[]", headers={"Content-Type": "application/json"})
        self.assertEqual(response.status, 400)

    # ---- photos -------------------------------------------------------------------------

    async def test_photos_come_from_v3s_own_directory_and_nowhere_else(self):
        media = nominations.media_path(CHAT)
        media.mkdir(parents=True)
        (media / "1_0.jpg").write_bytes(b"photo")
        secret = media.parent / "outside.jpg"
        secret.write_bytes(b"secret")

        response = await self.client.get(f"{nominations_web.ROUTE_PREFIX}/media/1_0.jpg")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), b"photo")
        for name in ("..%2Foutside.jpg", "missing.jpg", "a b.jpg"):
            with self.subTest(name=name):
                response = await self.client.get(f"{nominations_web.ROUTE_PREFIX}/media/{name}")
                self.assertEqual(response.status, 404)

    # ---- /vote is not affected ----------------------------------------------------------

    def _seed_v1(self):
        poll = voting.Poll(
            poll_id="2026-W40", entry=CHAT, created_at="2026-10-01T00:00:00+00:00",
            entries=[_entry(1), _entry(2)],
        )
        voting.set_approved(poll, ["1", "2"])
        voting.save_poll(poll)
        return voting.poll_path(CHAT, poll.poll_id)

    async def test_everything_v3_does_leaves_v1s_poll_byte_for_byte(self):
        poll_file = self._seed_v1()
        before = poll_file.read_bytes()

        ids = self._seed()
        await self._post("ballot", VOTER, nomination_id=ids["Аниме"], choices=["1"])
        await self._post("nominations/create", ADMIN, name="Новая")
        await self._post("nominations/update", ADMIN, nomination_id=ids["Аниме"], entry_ids=["1"])
        await self._post("nominations/delete", ADMIN, nomination_id=ids["Пустая"])
        await self._post("settings", ADMIN, open=False, max_choices=2)

        self.assertEqual(poll_file.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in poll_file.parent.glob("*.json")), [poll_file.name])

    async def test_v1_still_takes_ballots_with_v3_mounted_beside_it(self):
        poll_file = self._seed_v1()
        self._seed()
        v3_before = nominations.contest_path(CHAT).read_bytes()
        response = await self.client.post(
            f"{vote_web.ROUTE_PREFIX}/api/ballot",
            json={"init_data": _init_data(VOTER), "choices": ["2"]},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(voting.load_poll(CHAT, "2026-W40").votes, {str(VOTER): ["2"]})
        self.assertEqual(nominations.contest_path(CHAT).read_bytes(), v3_before)
        self.assertTrue(poll_file.exists())

    async def test_a_stalled_v3_write_does_not_hold_up_a_v1_request(self):
        """The disk work runs on a worker thread, so a v3 writer stuck behind its lock
        costs that one request, never the event loop /vote is served from."""
        self._seed_v1()
        ids = self._seed()
        reached_the_lock = threading.Event()
        original = nominations.update_contest

        def flagged(*args, **kwargs):
            reached_the_lock.set()
            return original(*args, **kwargs)

        nominations._write_lock.acquire()
        try:
            with patch("nominations.update_contest", flagged):
                stuck = asyncio.ensure_future(
                    self._post("ballot", VOTER, nomination_id=ids["Аниме"], choices=["1"])
                )
                self.assertTrue(await asyncio.to_thread(reached_the_lock.wait, 5))

                v1 = await asyncio.wait_for(
                    self.client.get(
                        f"{vote_web.ROUTE_PREFIX}/api/poll",
                        headers={"X-Telegram-Init-Data": _init_data(OTHER)},
                    ),
                    timeout=5,
                )
                self.assertEqual(v1.status, 200)
                self.assertEqual(len((await v1.json())["entries"]), 2)
                self.assertFalse(stuck.done())
        finally:
            nominations._write_lock.release()
        response = await asyncio.wait_for(stuck, timeout=5)
        self.assertEqual(response.status, 200)


class PageScriptSyntaxTests(unittest.TestCase):
    """One broken literal in the page's script and the Mini App opens blank while every
    route still answers 200 -- test_pets_web has the story. Parsed by node, as there."""

    def test_the_page_is_valid_javascript(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not available to parse the page")
        page = nominations_web.PAGE_HTML.replace("__PREFIX__", nominations_web.ROUTE_PREFIX)
        scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
        self.assertEqual(len(scripts), 1)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
            handle.write(scripts[0])
            path = handle.name
        try:
            result = subprocess.run([node, "--check", path], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            os.unlink(path)

    def test_the_name_field_holds_exactly_what_the_server_accepts(self):
        self.assertIn(f'maxlength="{nominations.NAME_MAX_LENGTH}"', nominations_web.PAGE_HTML)


if __name__ == "__main__":
    unittest.main()
