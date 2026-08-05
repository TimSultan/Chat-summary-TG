"""The arena's HTTP surface: authentication on every route, voters only ever get the pair
in front of them, and the moderation data stays behind the admin check.

Mounted the way it really is -- onto the application vote_web builds -- so these also
prove the two systems coexist on one server without colliding.
"""

import hashlib
import hmac
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import arena
import arena_web
import voting
import vote_web

BOT_TOKEN = "123456:FAKE-TOKEN-FOR-TESTS"
CHAT = "Chat"
TID = "2026-W32"


def _sign(fields: dict) -> str:
    payload = dict(fields)
    check = "\n".join(f"{k}={payload[k]}" for k in sorted(payload))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    payload["hash"] = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    return urlencode(payload)


def _init_data(user_id: int) -> str:
    return _sign({
        "auth_date": str(int(time.time())),
        "user": json.dumps({"id": user_id, "username": f"u{user_id}", "first_name": "V"}),
    })


def _entry(entry_id):
    return voting.Entry(
        entry_id=str(entry_id), message_id=int(entry_id), author_id=int(entry_id),
        author_name=f"Автор {entry_id}", author_username=f"user{entry_id}", text="",
        media=[f"{entry_id}.jpg"],
    )


class ArenaApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        self._patchers = [
            patch("arena._arena_dir", return_value=root / "arena"),
            patch("voting._voting_dir", return_value=root / "voting"),
        ]
        for patcher in self._patchers:
            patcher.start()
        arena._standings_cache.clear()

        self.admin_id, self.voter_id = 1, 2
        cfg = SimpleNamespace(telegram_bot_token=BOT_TOKEN)

        async def is_admin(user):
            return user.get("id") == self.admin_id

        async def is_member(user):
            return user.get("id") != 99

        # Built exactly as production builds it: v1's app, with the arena attached.
        app = vote_web.create_app(
            cfg, CHAT, is_admin, log=lambda *_: None,
            attach=lambda a: arena_web.attach(a, cfg, CHAT, is_admin, is_member=is_member, log=lambda *_: None),
        )
        self.server = TestServer(app)
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        for patcher in self._patchers:
            patcher.stop()
        self._temporary.cleanup()

    def _seed(self, works=4, approved=None, pairs=2, open=True):
        entries = [_entry(i) for i in range(works)]
        tournament = arena.Tournament(
            tournament_id=TID, entry=CHAT, created_at="2026-08-01",
            entries=entries, pairs_per_voter=pairs, open=open,
        )
        arena.set_approved(
            tournament, approved if approved is not None else [e.entry_id for e in entries]
        )
        arena.save_tournament(tournament)
        return tournament

    def _auth(self, user_id):
        return {"X-Telegram-Init-Data": _init_data(user_id)}

    # ---- authentication ------------------------------------------------------------

    async def test_every_route_refuses_an_unsigned_caller(self):
        self._seed()
        for method, path in (
            ("get", "/api/state"), ("post", "/api/session"), ("post", "/api/pick"),
            ("get", "/api/standings"), ("get", "/api/progress"),
            ("post", "/api/moderate"), ("post", "/api/clear"),
        ):
            response = await getattr(self.client, method)(arena_web.ROUTE_PREFIX + path, json={})
            self.assertEqual(response.status, 401, f"{method} {path} let an unsigned caller in")

    async def test_the_page_itself_needs_no_auth(self):
        response = await self.client.get(arena_web.ROUTE_PREFIX)
        self.assertEqual(response.status, 200)
        self.assertIn("telegram-web-app.js", await response.text())

    # ---- voting --------------------------------------------------------------------

    async def test_opening_the_arena_does_not_create_a_ballot(self):
        """Reading the state must not use up a session -- starting is an explicit POST."""
        self._seed()
        response = await self.client.get(arena_web.ROUTE_PREFIX + "/api/state", headers=self._auth(self.voter_id))
        data = await response.json()
        self.assertIsNone(data["ballot"])
        self.assertEqual(arena.load_tournament(CHAT, TID).ballots, {})

    async def test_a_session_deals_a_pair_and_a_pick_advances_it(self):
        self._seed(pairs=2)
        started = await (await self.client.post(
            arena_web.ROUTE_PREFIX + "/api/session", json={"init_data": _init_data(self.voter_id)}
        )).json()
        ballot = started["ballot"]
        self.assertEqual(ballot["total"], 2)
        self.assertEqual(ballot["position"], 0)
        self.assertIn("left", ballot["pair"])
        self.assertTrue(ballot["pair"]["left"]["photos"][0].startswith(arena_web.ROUTE_PREFIX + "/media/"))

        picked = await (await self.client.post(arena_web.ROUTE_PREFIX + "/api/pick", json={
            "init_data": _init_data(self.voter_id), "position": 0,
            "pick": ballot["pair"]["left"]["id"],
        })).json()
        self.assertEqual(picked["ballot"]["position"], 1)

    async def test_finishing_every_pair_closes_the_ballot_for_good(self):
        self._seed(pairs=1)
        ballot = (await (await self.client.post(
            arena_web.ROUTE_PREFIX + "/api/session", json={"init_data": _init_data(self.voter_id)}
        )).json())["ballot"]
        done = await (await self.client.post(arena_web.ROUTE_PREFIX + "/api/pick", json={
            "init_data": _init_data(self.voter_id), "position": 0, "pick": "tie",
        })).json()
        self.assertEqual(done["ballot"]["status"], "done")
        self.assertIsNone(done["ballot"]["pair"])

        again = await self.client.post(
            arena_web.ROUTE_PREFIX + "/api/session", json={"init_data": _init_data(self.voter_id)}
        )
        self.assertEqual(again.status, 409)
        self.assertEqual((await again.json())["error"], "ALREADY_VOTED")

    async def test_a_repeated_pick_does_not_count_twice(self):
        self._seed(pairs=3)
        ballot = (await (await self.client.post(
            arena_web.ROUTE_PREFIX + "/api/session", json={"init_data": _init_data(self.voter_id)}
        )).json())["ballot"]
        first = ballot["pair"]["left"]["id"]
        for _ in range(2):
            await self.client.post(arena_web.ROUTE_PREFIX + "/api/pick", json={
                "init_data": _init_data(self.voter_id), "position": 0, "pick": first,
            })
        self.assertEqual(len(arena.load_tournament(CHAT, TID).ballots[str(self.voter_id)].picks), 1)

    async def test_a_non_member_cannot_start(self):
        self._seed()
        response = await self.client.post(
            arena_web.ROUTE_PREFIX + "/api/session", json={"init_data": _init_data(99)}
        )
        self.assertEqual(response.status, 403)

    async def test_a_closed_arena_refuses_new_sessions(self):
        self._seed(open=False)
        response = await self.client.post(
            arena_web.ROUTE_PREFIX + "/api/session", json={"init_data": _init_data(self.voter_id)}
        )
        self.assertEqual(response.status, 409)
        self.assertEqual((await response.json())["error"], "VOTING_CLOSED")

    # ---- what a voter is not shown --------------------------------------------------

    async def test_a_voter_gets_no_standings_no_entries_and_no_progress(self):
        """The running table is exactly the thing that would bias a voter mid-vote, and
        asking for admin mode is not the same as being an admin."""
        self._seed()
        data = await (await self.client.get(
            arena_web.ROUTE_PREFIX + "/api/state?mode=admin", headers=self._auth(self.voter_id)
        )).json()
        self.assertFalse(data["is_admin"])
        for key in ("standings", "entries", "approved", "progress"):
            self.assertNotIn(key, data)

        for path in ("/api/standings", "/api/progress"):
            response = await self.client.get(arena_web.ROUTE_PREFIX + path, headers=self._auth(self.voter_id))
            self.assertEqual(response.status, 403)

    async def test_an_admin_asking_for_admin_mode_gets_the_moderation_payload(self):
        self._seed()
        data = await (await self.client.get(
            arena_web.ROUTE_PREFIX + "/api/state?mode=admin", headers=self._auth(self.admin_id)
        )).json()
        self.assertTrue(data["is_admin"])
        self.assertEqual(len(data["entries"]), 4)
        self.assertIn("standings", data)
        self.assertIn("progress", data)

    async def test_an_admin_on_the_plain_arena_is_a_voter_like_anyone_else(self):
        self._seed()
        data = await (await self.client.get(
            arena_web.ROUTE_PREFIX + "/api/state", headers=self._auth(self.admin_id)
        )).json()
        self.assertFalse(data["is_admin"])
        self.assertTrue(data["can_moderate"])

    # ---- moderation ------------------------------------------------------------------

    async def test_only_an_admin_can_moderate_or_clear(self):
        self._seed()
        for path in ("/api/moderate", "/api/clear"):
            response = await self.client.post(arena_web.ROUTE_PREFIX + path, json={
                "init_data": _init_data(self.voter_id), "approved": [],
            })
            self.assertEqual(response.status, 403)
        self.assertIsNotNone(arena.load_tournament(CHAT, TID))

    async def test_moderation_saves_the_admitted_set_and_the_settings(self):
        self._seed()
        response = await self.client.post(arena_web.ROUTE_PREFIX + "/api/moderate", json={
            "init_data": _init_data(self.admin_id),
            "approved": ["0", "2"], "pairs_per_voter": 7, "pairing": "adaptive", "open": False,
        })
        self.assertEqual(response.status, 200)
        saved = arena.load_tournament(CHAT, TID)
        self.assertEqual(saved.approved, ["0", "2"])
        self.assertEqual(saved.pairs_per_voter, 7)
        self.assertEqual(saved.pairing, "adaptive")
        self.assertFalse(saved.open)

    async def test_a_nonsense_setting_is_refused_rather_than_stored(self):
        self._seed()
        for body in (
            {"approved": [], "pairs_per_voter": 0},
            {"approved": [], "pairs_per_voter": 999},
            {"approved": [], "pairing": "vibes"},
        ):
            body["init_data"] = _init_data(self.admin_id)
            response = await self.client.post(arena_web.ROUTE_PREFIX + "/api/moderate", json=body)
            self.assertEqual(response.status, 400)
        self.assertEqual(arena.load_tournament(CHAT, TID).pairs_per_voter, 2)

    # ---- living beside v1 --------------------------------------------------------------

    async def test_v1s_routes_still_answer_with_the_arena_mounted(self):
        """Both systems, one server: attaching the arena must not shadow or replace a
        single one of v1's routes."""
        self._seed()
        poll = voting.Poll(poll_id=TID, entry=CHAT, created_at="2026-08-01", entries=[_entry(0)])
        voting.set_approved(poll, ["0"])
        voting.save_poll(poll)

        health = await self.client.get("/health")
        self.assertEqual(health.status, 200)
        page = await self.client.get(vote_web.ROUTE_PREFIX)
        self.assertEqual(page.status, 200)
        v1 = await (await self.client.get(
            vote_web.ROUTE_PREFIX + "/api/poll", headers=self._auth(self.voter_id)
        )).json()
        self.assertEqual(v1["poll_id"], TID)
        self.assertEqual(len(v1["entries"]), 1)

    async def test_clearing_the_arena_leaves_v1s_poll_alone(self):
        self._seed()
        poll = voting.Poll(poll_id=TID, entry=CHAT, created_at="2026-08-01", entries=[_entry(0)])
        voting.set_approved(poll, ["0"])
        voting.record_vote(poll, 500, ["0"])
        voting.save_poll(poll)

        response = await self.client.post(
            arena_web.ROUTE_PREFIX + "/api/clear", json={"init_data": _init_data(self.admin_id)}
        )
        self.assertEqual(response.status, 200)
        self.assertIsNone(arena.load_tournament(CHAT, TID))
        self.assertEqual(voting.load_poll(CHAT, TID).votes, {"500": ["0"]})

    async def test_media_outside_the_arena_directory_is_not_reachable(self):
        response = await self.client.get(arena_web.ROUTE_PREFIX + f"/media/{TID}/../../secrets.txt")
        self.assertIn(response.status, (400, 403, 404))


if __name__ == "__main__":
    unittest.main()
