"""vote_web.py's HTTP surface: authentication is mandatory on every state-changing route,
voters only ever see admitted entries, and only an admin can admit or see counts."""

import hashlib
import hmac
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer

import vote_web
import voting

BOT_TOKEN = "123456:FAKE-TOKEN-FOR-TESTS"
CHAT = "Chat"


def _sign(fields: dict, bot_token: str = BOT_TOKEN) -> str:
    payload = dict(fields)
    check_string = "\n".join(f"{k}={payload[k]}" for k in sorted(payload))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    payload["hash"] = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    return urlencode(payload)


def _init_data(user_id: int, username: str = "voter") -> str:
    return _sign({
        "auth_date": str(int(time.time())),
        "user": json.dumps({"id": user_id, "username": username, "first_name": "V"}),
    })


def _entry(entry_id: str) -> voting.Entry:
    return voting.Entry(
        entry_id=entry_id, message_id=1, author_id=1, author_name="Author",
        author_username="author", text="", media=["a.jpg"],
    )


class VoteApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self._patcher = patch("voting._voting_dir", return_value=Path(self._temporary.name))
        self._patcher.start()

        self.admin_id = 1
        self.voter_id = 2
        self.announced = []  # (user, poll, winner_entry, votes) tuples, in call order
        cfg = SimpleNamespace(telegram_bot_token=BOT_TOKEN)

        async def is_admin(user: dict) -> bool:
            return user.get("id") == self.admin_id

        async def announce(user, poll, top):
            self.announced.append((user, poll, top))

        app = vote_web.create_app(cfg, CHAT, is_admin, announce=announce, log=lambda *_: None)
        self.server = TestServer(app)
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self._patcher.stop()
        self._temporary.cleanup()

    def _seed_poll(self, approved=("a",)):
        poll = voting.Poll(
            poll_id="2026-08-02", entry=CHAT, created_at="2026-08-02T00:00:00+00:00",
            entries=[_entry("a"), _entry("b")],
        )
        voting.set_approved(poll, list(approved))
        voting.save_poll(poll)
        return poll

    # ---- authentication is mandatory --------------------------------------------------

    async def test_poll_endpoint_refuses_no_init_data(self):
        response = await self.client.get(f"{vote_web.ROUTE_PREFIX}/api/poll")
        self.assertEqual(response.status, 401)

    async def test_poll_endpoint_refuses_a_forged_signature(self):
        response = await self.client.get(
            f"{vote_web.ROUTE_PREFIX}/api/poll",
            headers={"X-Telegram-Init-Data": "user=%7B%22id%22%3A1%7D&auth_date=1&hash=deadbeef"},
        )
        self.assertEqual(response.status, 401)

    async def test_ballot_endpoint_refuses_no_init_data(self):
        response = await self.client.post(f"{vote_web.ROUTE_PREFIX}/api/ballot", json={"choices": ["a"]})
        self.assertEqual(response.status, 401)

    # ---- an ordinary voter only ever sees admitted entries -----------------------------

    async def test_a_voter_sees_only_admitted_entries(self):
        self._seed_poll(approved=("a",))
        response = await self.client.get(
            f"{vote_web.ROUTE_PREFIX}/api/poll",
            headers={"X-Telegram-Init-Data": _init_data(self.voter_id)},
        )
        data = await response.json()
        self.assertEqual(response.status, 200)
        self.assertFalse(data["is_admin"])
        self.assertEqual([e["id"] for e in data["entries"]], ["a"])
        self.assertNotIn("counts", data)  # only the admin gets live counts

    async def test_an_unmoderated_poll_shows_a_voter_nothing(self):
        self._seed_poll(approved=())
        response = await self.client.get(
            f"{vote_web.ROUTE_PREFIX}/api/poll",
            headers={"X-Telegram-Init-Data": _init_data(self.voter_id)},
        )
        data = await response.json()
        self.assertEqual(data["entries"], [])

    # ---- moderation mode is opt-in, even for an admin -----------------------------------

    async def test_an_admin_without_mode_admin_gets_the_plain_ballot(self):
        """Bare /vote must never force an administrator into moderation -- they need to
        be able to cast their own vote like everyone else."""
        self._seed_poll(approved=("a",))
        response = await self.client.get(
            f"{vote_web.ROUTE_PREFIX}/api/poll",
            headers={"X-Telegram-Init-Data": _init_data(self.admin_id)},
        )
        data = await response.json()
        self.assertFalse(data["is_admin"])
        self.assertTrue(data["can_moderate"])  # still told they COULD moderate
        self.assertEqual([e["id"] for e in data["entries"]], ["a"])
        self.assertNotIn("counts", data)

    async def test_mode_admin_is_ignored_for_a_non_admin(self):
        self._seed_poll(approved=("a",))
        response = await self.client.get(
            f"{vote_web.ROUTE_PREFIX}/api/poll?mode=admin",
            headers={"X-Telegram-Init-Data": _init_data(self.voter_id)},
        )
        data = await response.json()
        self.assertFalse(data["is_admin"])
        self.assertFalse(data["can_moderate"])
        self.assertEqual([e["id"] for e in data["entries"]], ["a"])  # not entry "b" too

    # ---- voting -------------------------------------------------------------------------

    async def test_a_vote_is_recorded_and_reflected_back(self):
        self._seed_poll(approved=("a",))
        response = await self.client.post(
            f"{vote_web.ROUTE_PREFIX}/api/ballot",
            json={"init_data": _init_data(self.voter_id), "choices": ["a"]},
        )
        self.assertEqual(response.status, 200)
        poll = voting.load_poll(CHAT, "2026-08-02")
        self.assertEqual(poll.votes[str(self.voter_id)], ["a"])

    async def test_voting_for_an_unapproved_entry_is_silently_dropped_not_rejected(self):
        self._seed_poll(approved=("a",))
        response = await self.client.post(
            f"{vote_web.ROUTE_PREFIX}/api/ballot",
            json={"init_data": _init_data(self.voter_id), "choices": ["a", "b"]},
        )
        data = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(data["my_vote"], ["a"])

    async def test_voting_with_no_poll_created_yet_is_a_clean_404(self):
        response = await self.client.post(
            f"{vote_web.ROUTE_PREFIX}/api/ballot",
            json={"init_data": _init_data(self.voter_id), "choices": []},
        )
        self.assertEqual(response.status, 404)

    # ---- moderation is admin-only --------------------------------------------------------

    async def test_a_non_admin_cannot_moderate(self):
        self._seed_poll(approved=())
        response = await self.client.post(
            f"{vote_web.ROUTE_PREFIX}/api/moderate",
            json={"init_data": _init_data(self.voter_id), "approved": ["a", "b"]},
        )
        self.assertEqual(response.status, 403)
        poll = voting.load_poll(CHAT, "2026-08-02")
        self.assertEqual(poll.approved, [])  # unchanged

    async def test_an_admin_can_admit_entries(self):
        self._seed_poll(approved=())
        response = await self.client.post(
            f"{vote_web.ROUTE_PREFIX}/api/moderate",
            json={"init_data": _init_data(self.admin_id), "approved": ["a", "b"]},
        )
        self.assertEqual(response.status, 200)
        poll = voting.load_poll(CHAT, "2026-08-02")
        self.assertEqual(sorted(poll.approved), ["a", "b"])

    async def test_the_admin_view_includes_every_entry_and_live_counts(self):
        self._seed_poll(approved=("a",))
        voting.record_vote(voting.load_poll(CHAT, "2026-08-02"), self.voter_id, ["a"])
        poll = voting.load_poll(CHAT, "2026-08-02")
        voting.record_vote(poll, self.voter_id, ["a"])
        voting.save_poll(poll)

        response = await self.client.get(
            f"{vote_web.ROUTE_PREFIX}/api/poll?mode=admin",
            headers={"X-Telegram-Init-Data": _init_data(self.admin_id)},
        )
        data = await response.json()
        self.assertTrue(data["is_admin"])
        self.assertEqual({e["id"] for e in data["entries"]}, {"a", "b"})  # sees the unadmitted one too
        self.assertEqual(data["counts"]["a"], 1)

    # ---- closing the vote and announcing a winner ----------------------------------------

    async def test_a_non_admin_cannot_announce(self):
        self._seed_poll(approved=("a", "b"))
        response = await self.client.post(
            f"{vote_web.ROUTE_PREFIX}/api/announce",
            json={"init_data": _init_data(self.voter_id)},
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(self.announced, [])
        self.assertTrue(voting.load_poll(CHAT, "2026-08-02").open)

    async def test_announcing_with_no_votes_is_refused(self):
        self._seed_poll(approved=("a",))
        response = await self.client.post(
            f"{vote_web.ROUTE_PREFIX}/api/announce",
            json={"init_data": _init_data(self.admin_id)},
        )
        self.assertEqual(response.status, 409)
        self.assertEqual(self.announced, [])

    async def test_an_admin_can_close_the_vote_and_the_winner_is_announced(self):
        self._seed_poll(approved=("a", "b"))
        poll = voting.load_poll(CHAT, "2026-08-02")
        voting.record_vote(poll, 10, ["a"])
        voting.record_vote(poll, 11, ["a"])
        voting.record_vote(poll, 12, ["b"])
        voting.save_poll(poll)

        response = await self.client.post(
            f"{vote_web.ROUTE_PREFIX}/api/announce",
            json={"init_data": _init_data(self.admin_id)},
        )
        data = await response.json()

        self.assertEqual(response.status, 200)
        self.assertTrue(data["notified"])
        self.assertEqual(data["winner"]["id"], "a")
        self.assertEqual(data["votes"], 2)

        stored = voting.load_poll(CHAT, "2026-08-02")
        self.assertFalse(stored.open)
        self.assertEqual(stored.winner_entry_id, "a")

        self.assertEqual(len(self.announced), 1)
        user, announced_poll, top = self.announced[0]
        self.assertEqual(user["id"], self.admin_id)
        self.assertEqual([(e.entry_id, v) for e, v in top], [("a", 2), ("b", 1)])

    async def test_the_announced_top_is_capped_at_three_even_with_more_admitted_entries(self):
        poll = voting.Poll(
            poll_id="2026-08-02", entry=CHAT, created_at="2026-08-02T00:00:00+00:00",
            entries=[_entry("a"), _entry("b"), _entry("c"), _entry("d")],
        )
        voting.set_approved(poll, ["a", "b", "c", "d"])
        for voter_id, choice in [(1, "d"), (2, "d"), (3, "d"), (4, "c"), (5, "c"), (6, "b")]:
            voting.record_vote(poll, voter_id, [choice])
        voting.save_poll(poll)

        response = await self.client.post(
            f"{vote_web.ROUTE_PREFIX}/api/announce",
            json={"init_data": _init_data(self.admin_id)},
        )
        data = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(data["winner"]["id"], "d")
        self.assertEqual([t["entry"]["id"] for t in data["top"]], ["d", "c", "b"])
        self.assertEqual([t["votes"] for t in data["top"]], [3, 2, 1])

        self.assertEqual(len(self.announced), 1)
        _, _, top = self.announced[0]
        self.assertEqual([(e.entry_id, v) for e, v in top], [("d", 3), ("c", 2), ("b", 1)])

    async def test_a_closed_poll_still_reports_its_winner_to_a_voter(self):
        self._seed_poll(approved=("a", "b"))
        poll = voting.load_poll(CHAT, "2026-08-02")
        voting.record_vote(poll, 10, ["a"])
        voting.save_poll(poll)
        await self.client.post(
            f"{vote_web.ROUTE_PREFIX}/api/announce",
            json={"init_data": _init_data(self.admin_id)},
        )

        response = await self.client.get(
            f"{vote_web.ROUTE_PREFIX}/api/poll",
            headers={"X-Telegram-Init-Data": _init_data(self.voter_id)},
        )
        data = await response.json()
        self.assertFalse(data["open"])
        self.assertEqual(data["winner"]["id"], "a")

    async def test_a_delivery_failure_still_leaves_the_poll_closed(self):
        """The poll's own state is authoritative -- a failed send must not un-close it or
        forget who won, only report that the message itself didn't go out. Built with its
        own client, rather than mutating the shared one from setUp, since aiohttp warns
        against changing an Application's state after it's started."""
        async def broken_announce(user, poll, winner_entry, votes):
            raise RuntimeError("Bot API is down")

        async def is_admin(user: dict) -> bool:
            return user.get("id") == self.admin_id

        cfg = SimpleNamespace(telegram_bot_token=BOT_TOKEN)
        app = vote_web.create_app(cfg, CHAT, is_admin, announce=broken_announce, log=lambda *_: None)
        async with TestClient(TestServer(app)) as client:
            self._seed_poll(approved=("a",))
            poll = voting.load_poll(CHAT, "2026-08-02")
            voting.record_vote(poll, 10, ["a"])
            voting.save_poll(poll)

            response = await client.post(
                f"{vote_web.ROUTE_PREFIX}/api/announce",
                json={"init_data": _init_data(self.admin_id)},
            )
            data = await response.json()

        self.assertEqual(response.status, 200)
        self.assertFalse(data["notified"])
        self.assertFalse(voting.load_poll(CHAT, "2026-08-02").open)

    # ---- media ----------------------------------------------------------------------------

    async def test_media_outside_the_poll_directory_is_not_reachable(self):
        response = await self.client.get(f"{vote_web.ROUTE_PREFIX}/media/2026-08-02/../../secrets.txt")
        self.assertIn(response.status, (400, 403, 404))

    async def test_a_nonexistent_photo_is_a_404(self):
        response = await self.client.get(f"{vote_web.ROUTE_PREFIX}/media/2026-08-02/nope.jpg")
        self.assertEqual(response.status, 404)

    # ---- page + health --------------------------------------------------------------------

    async def test_the_health_check_needs_no_auth(self):
        response = await self.client.get("/health")
        self.assertEqual(response.status, 200)

    async def test_the_page_itself_needs_no_auth_to_load(self):
        """The HTML shell is public; every API call it makes still requires initData."""
        response = await self.client.get(vote_web.ROUTE_PREFIX)
        self.assertEqual(response.status, 200)
        self.assertIn("telegram-web-app.js", await response.text())


if __name__ == "__main__":
    unittest.main()
