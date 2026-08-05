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
        self.announced = []  # (user, poll, standings) tuples, in call order
        self.exported = []   # paths of rendered board pictures handed over for delivery
        self.export_fails = False
        cfg = SimpleNamespace(telegram_bot_token=BOT_TOKEN)

        async def is_admin(user: dict) -> bool:
            return user.get("id") == self.admin_id

        async def announce(user, poll, standings):
            self.announced.append((user, poll, standings))

        async def export(user, poll, path):
            if self.export_fails:
                raise RuntimeError("bot could not DM this admin")
            self.exported.append(path)

        app = vote_web.create_app(
            cfg, CHAT, is_admin, announce=announce, export=export, log=lambda *_: None,
        )
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

    async def test_ballot_response_carries_results_and_voter_count(self):
        """The success response gives the page enough to render the standings right
        after a vote, without a second /api/poll round trip."""
        self._seed_poll(approved=("a", "b"))
        response = await self.client.post(
            f"{vote_web.ROUTE_PREFIX}/api/ballot",
            json={"init_data": _init_data(self.voter_id), "choices": ["a"]},
        )
        data = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(data["voter_count"], 1)
        self.assertEqual(
            [(r["id"], r["votes"]) for r in data["results"]], [("a", 1), ("b", 0)]
        )

    # ---- the membership gate -------------------------------------------------------------

    async def test_create_app_is_still_constructible_without_is_member(self):
        """The default (create_app called with no is_member, as in asyncSetUp) must be
        permissive -- this module has to keep working standalone, without bot_listener.py's
        real Telegram chat-membership check wired up."""
        self._seed_poll(approved=("a",))
        response = await self.client.get(
            f"{vote_web.ROUTE_PREFIX}/api/poll",
            headers={"X-Telegram-Init-Data": _init_data(self.voter_id)},
        )
        data = await response.json()
        self.assertTrue(data["is_member"])

    async def test_a_non_member_is_refused_by_ballot_and_nothing_is_recorded(self):
        async def is_admin(user: dict) -> bool:
            return user.get("id") == self.admin_id

        async def is_member(user: dict) -> bool:
            return user.get("id") != self.voter_id

        cfg = SimpleNamespace(telegram_bot_token=BOT_TOKEN)
        app = vote_web.create_app(cfg, CHAT, is_admin, is_member=is_member, log=lambda *_: None)
        async with TestClient(TestServer(app)) as client:
            self._seed_poll(approved=("a",))
            response = await client.post(
                f"{vote_web.ROUTE_PREFIX}/api/ballot",
                json={"init_data": _init_data(self.voter_id), "choices": ["a"]},
            )
            data = await response.json()

        self.assertEqual(response.status, 403)
        self.assertNotIn("ok", data)
        self.assertIsNone(voting.load_poll(CHAT, "2026-08-02").votes.get(str(self.voter_id)))

    async def test_the_poll_payload_reports_is_member(self):
        self._seed_poll(approved=("a",))
        response = await self.client.get(
            f"{vote_web.ROUTE_PREFIX}/api/poll",
            headers={"X-Telegram-Init-Data": _init_data(self.voter_id)},
        )
        data = await response.json()
        self.assertIn("is_member", data)

    # ---- results are gated on having earned the right to see them -------------------------

    async def test_poll_payload_always_carries_voter_count(self):
        poll = self._seed_poll(approved=("a",))
        voting.record_vote(poll, 42, ["a"])
        voting.save_poll(poll)

        response = await self.client.get(
            f"{vote_web.ROUTE_PREFIX}/api/poll",
            headers={"X-Telegram-Init-Data": _init_data(self.voter_id)},
        )
        data = await response.json()
        self.assertEqual(data["voter_count"], 1)

    async def test_results_are_absent_for_a_voter_who_has_not_voted_on_an_open_poll(self):
        """Showing a running vote count to somebody who hasn't cast their own ballot yet
        would bias what they pick -- so results are withheld, not sent empty."""
        self._seed_poll(approved=("a", "b"))
        response = await self.client.get(
            f"{vote_web.ROUTE_PREFIX}/api/poll",
            headers={"X-Telegram-Init-Data": _init_data(self.voter_id)},
        )
        data = await response.json()
        self.assertNotIn("results", data)

    async def test_results_appear_once_the_voter_has_voted_ranked_with_zero_vote_entries(self):
        poll = self._seed_poll(approved=("a", "b"))
        voting.record_vote(poll, self.voter_id, ["a"])
        voting.record_vote(poll, 99, ["a"])
        voting.save_poll(poll)

        response = await self.client.get(
            f"{vote_web.ROUTE_PREFIX}/api/poll",
            headers={"X-Telegram-Init-Data": _init_data(self.voter_id)},
        )
        data = await response.json()
        self.assertEqual(
            [(r["id"], r["votes"]) for r in data["results"]], [("a", 2), ("b", 0)]
        )

    async def test_a_results_row_carries_every_field_the_standings_render(self):
        """The page's who() reads `username` first and falls back to `author`, so a row
        missing either renders a blank name -- results rows must carry both, exactly like
        the entry payload they are rendered next to."""
        poll = self._seed_poll(approved=("a",))
        voting.record_vote(poll, self.voter_id, ["a"])
        voting.save_poll(poll)

        response = await self.client.get(
            f"{vote_web.ROUTE_PREFIX}/api/poll",
            headers={"X-Telegram-Init-Data": _init_data(self.voter_id)},
        )
        data = await response.json()
        row = data["results"][0]
        self.assertEqual(set(row), {"id", "author", "username", "votes", "photo"})
        self.assertEqual(row["author"], "Author")
        self.assertEqual(row["username"], "author")
        self.assertIsInstance(row["votes"], int)
        # The same url _entry_payload builds, so the standings thumbnail is served from
        # cache instead of fetching the picture a second time.
        self.assertEqual(row["photo"], f"{vote_web.ROUTE_PREFIX}/media/2026-08-02/a.jpg")

    async def test_a_results_row_for_an_entry_without_media_has_no_photo(self):
        """The standings row still has to render: the page emits an empty cell for a null
        photo, because a row one cell short would slide every row below it sideways."""
        poll = voting.Poll(
            poll_id="2026-08-02", entry=CHAT, created_at="2026-08-02T00:00:00+00:00",
            entries=[voting.Entry(
                entry_id="a", message_id=1, author_id=1, author_name="Author",
                author_username="author", text="", media=[],
            )],
        )
        voting.set_approved(poll, ["a"])
        voting.record_vote(poll, self.voter_id, ["a"])
        voting.save_poll(poll)

        response = await self.client.get(
            f"{vote_web.ROUTE_PREFIX}/api/poll",
            headers={"X-Telegram-Init-Data": _init_data(self.voter_id)},
        )
        data = await response.json()
        self.assertIsNone(data["results"][0]["photo"])

    async def test_the_ballot_response_carries_photos_in_its_standings_too(self):
        """handle_ballot builds its own results payload so the page can render the
        standings without a second round trip -- it must pass the same route prefix, or
        the thumbnails would be the one thing missing right after voting."""
        self._seed_poll(approved=("a",))
        response = await self.client.post(
            f"{vote_web.ROUTE_PREFIX}/api/ballot",
            json={"init_data": _init_data(self.voter_id), "choices": ["a"]},
        )
        data = await response.json()
        self.assertEqual(
            data["results"][0]["photo"], f"{vote_web.ROUTE_PREFIX}/media/2026-08-02/a.jpg"
        )

    async def test_results_are_present_on_a_closed_poll_even_for_a_non_voter(self):
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
        self.assertIn("results", data)

    async def test_results_are_present_in_admin_mode_regardless_of_voting(self):
        self._seed_poll(approved=("a", "b"))
        response = await self.client.get(
            f"{vote_web.ROUTE_PREFIX}/api/poll?mode=admin",
            headers={"X-Telegram-Init-Data": _init_data(self.admin_id)},
        )
        data = await response.json()
        self.assertEqual(
            [(r["id"], r["votes"]) for r in data["results"]], [("a", 0), ("b", 0)]
        )

    # ---- max_choices / allow_revote settings ---------------------------------------------

    async def test_a_ballot_over_the_max_choices_cap_is_rejected(self):
        poll = self._seed_poll(approved=("a", "b"))
        poll.max_choices = 1
        voting.save_poll(poll)

        response = await self.client.post(
            f"{vote_web.ROUTE_PREFIX}/api/ballot",
            json={"init_data": _init_data(self.voter_id), "choices": ["a", "b"]},
        )
        self.assertEqual(response.status, 400)
        self.assertIsNone(voting.load_poll(CHAT, "2026-08-02").votes.get(str(self.voter_id)))

    async def test_a_ballot_at_exactly_the_cap_is_accepted(self):
        poll = self._seed_poll(approved=("a", "b"))
        poll.max_choices = 2
        voting.save_poll(poll)

        response = await self.client.post(
            f"{vote_web.ROUTE_PREFIX}/api/ballot",
            json={"init_data": _init_data(self.voter_id), "choices": ["a", "b"]},
        )
        self.assertEqual(response.status, 200)

    async def test_revoting_is_refused_once_allow_revote_is_off(self):
        poll = self._seed_poll(approved=("a", "b"))
        poll.allow_revote = False
        voting.record_vote(poll, self.voter_id, ["a"])
        voting.save_poll(poll)

        response = await self.client.post(
            f"{vote_web.ROUTE_PREFIX}/api/ballot",
            json={"init_data": _init_data(self.voter_id), "choices": ["b"]},
        )
        self.assertEqual(response.status, 409)
        # The original ballot is untouched by the refused attempt.
        self.assertEqual(voting.load_poll(CHAT, "2026-08-02").votes[str(self.voter_id)], ["a"])

    async def test_an_empty_recorded_ballot_does_not_lock_the_voter_out(self):
        """A voter can hold a recorded-but-empty ballot: record_vote filters choices against
        the admitted set, so an admin un-admitting everything they picked leaves the key
        present with nothing behind it. Treating that as "already voted" would lock them out
        of the contest holding no vote at all, which is why the check tests the choices
        rather than the key."""
        poll = self._seed_poll(approved=("a", "b"))
        poll.allow_revote = False
        voting.record_vote(poll, self.voter_id, ["a"])
        voting.set_approved(poll, ["b"])          # "a" is withdrawn by an administrator
        voting.record_vote(poll, self.voter_id, poll.votes[str(self.voter_id)])
        voting.save_poll(poll)
        self.assertEqual(voting.load_poll(CHAT, "2026-08-02").votes[str(self.voter_id)], [])

        response = await self.client.post(
            f"{vote_web.ROUTE_PREFIX}/api/ballot",
            json={"init_data": _init_data(self.voter_id), "choices": ["b"]},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(voting.load_poll(CHAT, "2026-08-02").votes[str(self.voter_id)], ["b"])

    async def test_a_first_vote_is_still_accepted_when_allow_revote_is_off(self):
        poll = self._seed_poll(approved=("a",))
        poll.allow_revote = False
        voting.save_poll(poll)

        response = await self.client.post(
            f"{vote_web.ROUTE_PREFIX}/api/ballot",
            json={"init_data": _init_data(self.voter_id), "choices": ["a"]},
        )
        self.assertEqual(response.status, 200)

    async def test_the_poll_payload_carries_settings_for_voters_too(self):
        poll = self._seed_poll(approved=("a",))
        poll.max_choices = 1
        poll.allow_revote = False
        voting.save_poll(poll)

        response = await self.client.get(
            f"{vote_web.ROUTE_PREFIX}/api/poll",
            headers={"X-Telegram-Init-Data": _init_data(self.voter_id)},
        )
        data = await response.json()
        self.assertEqual(data["max_choices"], 1)
        self.assertFalse(data["allow_revote"])

    async def test_moderate_updates_the_settings_alongside_admitted_entries(self):
        self._seed_poll(approved=())
        response = await self.client.post(
            f"{vote_web.ROUTE_PREFIX}/api/moderate",
            json={
                "init_data": _init_data(self.admin_id), "approved": ["a"],
                "max_choices": 2, "allow_revote": False,
            },
        )
        data = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(data["max_choices"], 2)
        self.assertFalse(data["allow_revote"])
        stored = voting.load_poll(CHAT, "2026-08-02")
        self.assertEqual(stored.max_choices, 2)
        self.assertFalse(stored.allow_revote)

    async def test_moderate_can_clear_max_choices_back_to_unlimited(self):
        poll = self._seed_poll(approved=("a",))
        poll.max_choices = 2
        voting.save_poll(poll)

        response = await self.client.post(
            f"{vote_web.ROUTE_PREFIX}/api/moderate",
            json={"init_data": _init_data(self.admin_id), "approved": ["a"], "max_choices": None},
        )
        self.assertEqual(response.status, 200)
        self.assertIsNone(voting.load_poll(CHAT, "2026-08-02").max_choices)

    async def test_moderate_rejects_a_non_positive_max_choices(self):
        self._seed_poll(approved=("a",))
        response = await self.client.post(
            f"{vote_web.ROUTE_PREFIX}/api/moderate",
            json={"init_data": _init_data(self.admin_id), "approved": ["a"], "max_choices": 0},
        )
        self.assertEqual(response.status, 400)

    # ---- clearing the poll entirely ------------------------------------------------------

    async def test_a_non_admin_cannot_clear(self):
        self._seed_poll(approved=("a",))
        response = await self.client.post(
            f"{vote_web.ROUTE_PREFIX}/api/clear",
            json={"init_data": _init_data(self.voter_id)},
        )
        self.assertEqual(response.status, 403)
        self.assertIsNotNone(voting.load_poll(CHAT, "2026-08-02"))

    async def test_an_admin_can_clear_the_poll(self):
        self._seed_poll(approved=("a",))
        response = await self.client.post(
            f"{vote_web.ROUTE_PREFIX}/api/clear",
            json={"init_data": _init_data(self.admin_id)},
        )
        self.assertEqual(response.status, 200)
        self.assertIsNone(voting.load_poll(CHAT, "2026-08-02"))

        # A fresh poll GET after clearing reports no poll at all, not a stale one.
        response = await self.client.get(
            f"{vote_web.ROUTE_PREFIX}/api/poll",
            headers={"X-Telegram-Init-Data": _init_data(self.admin_id)},
        )
        data = await response.json()
        self.assertIsNone(data["poll_id"])

    async def test_clearing_with_no_poll_created_yet_is_a_clean_404(self):
        response = await self.client.post(
            f"{vote_web.ROUTE_PREFIX}/api/clear",
            json={"init_data": _init_data(self.admin_id)},
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
        user, announced_poll, standings = self.announced[0]
        self.assertEqual(user["id"], self.admin_id)
        self.assertEqual([(e.entry_id, v) for e, v in standings], [("a", 2), ("b", 1)])

    async def test_the_page_top_is_capped_at_three_but_the_announcer_gets_them_all(self):
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

        # The announcer draws a picture of the whole table, so it is handed every admitted
        # entry -- zero-vote ones included -- rather than the three the banner shows.
        self.assertEqual(len(self.announced), 1)
        _, _, standings = self.announced[0]
        self.assertEqual(
            [(e.entry_id, v) for e, v in standings], [("d", 3), ("c", 2), ("b", 1), ("a", 0)]
        )

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
        async def broken_announce(user, poll, standings):
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

    # ---- cropping + export ------------------------------------------------------------------

    def _seed_photo(self, name="a.jpg", size=(600, 400)):
        """A real JPEG where the poll's media lives, so the export path renders for real
        rather than against a mocked Pillow."""
        from PIL import Image

        directory = voting.media_path(CHAT, "2026-08-02")
        directory.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, (40, 90, 160)).save(directory / name, "JPEG")

    async def test_a_non_admin_cannot_save_crops(self):
        self._seed_poll()
        response = await self.client.post(f"{vote_web.ROUTE_PREFIX}/api/crops", json={
            "init_data": _init_data(self.voter_id), "crops": {"a": {"x": 0, "y": 0, "size": 10}},
        })
        self.assertEqual(response.status, 403)
        self.assertEqual(voting.load_poll(CHAT, "2026-08-02").crops, {})

    async def test_an_admin_saves_crops_and_they_survive_a_reload(self):
        self._seed_poll()
        response = await self.client.post(f"{vote_web.ROUTE_PREFIX}/api/crops", json={
            "init_data": _init_data(self.admin_id),
            "crops": {"a": {"x": -50.5, "y": 10, "size": 400}},
        })
        self.assertEqual(response.status, 200)
        self.assertEqual(
            voting.load_poll(CHAT, "2026-08-02").crops, {"a": {"x": -50.5, "y": 10.0, "size": 400.0}}
        )

    async def test_a_crop_for_an_unknown_entry_is_dropped_not_stored(self):
        self._seed_poll()
        await self.client.post(f"{vote_web.ROUTE_PREFIX}/api/crops", json={
            "init_data": _init_data(self.admin_id),
            "crops": {"a": {"x": 0, "y": 0, "size": 5}, "ghost": {"x": 0, "y": 0, "size": 5}},
        })
        self.assertEqual(list(voting.load_poll(CHAT, "2026-08-02").crops), ["a"])

    async def test_a_malformed_crop_is_dropped_without_losing_the_good_ones(self):
        self._seed_poll(approved=("a", "b"))
        await self.client.post(f"{vote_web.ROUTE_PREFIX}/api/crops", json={
            "init_data": _init_data(self.admin_id),
            "crops": {"a": {"x": 0, "y": 0, "size": 5}, "b": {"x": 0, "y": 0, "size": -1}},
        })
        self.assertEqual(list(voting.load_poll(CHAT, "2026-08-02").crops), ["a"])

    async def test_a_non_admin_cannot_export(self):
        self._seed_poll()
        response = await self.client.post(
            f"{vote_web.ROUTE_PREFIX}/api/export", json={"init_data": _init_data(self.voter_id)}
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(self.exported, [])

    async def test_exporting_renders_the_file_saves_the_crops_and_delivers_it(self):
        self._seed_poll()
        self._seed_photo()
        response = await self.client.post(f"{vote_web.ROUTE_PREFIX}/api/export", json={
            "init_data": _init_data(self.admin_id),
            "crops": {"a": {"x": 100, "y": 0, "size": 400}},
        })
        data = await response.json()

        self.assertEqual(response.status, 200)
        self.assertTrue(data["delivered"])
        self.assertEqual(data["entries"], 1)
        # The crops came with the export rather than through a separate save, and stuck.
        self.assertEqual(voting.load_poll(CHAT, "2026-08-02").crops["a"]["x"], 100.0)
        path = voting.export_image_path(CHAT, "2026-08-02")
        self.assertTrue(path.is_file())
        self.assertEqual(self.exported, [path])
        # And the url it hands back actually serves that file.
        served = await self.client.get(data["url"])
        self.assertEqual(served.status, 200)
        self.assertEqual(len(await served.read()), path.stat().st_size)

    async def test_a_delivery_failure_still_leaves_the_picture_on_disk(self):
        self._seed_poll()
        self._seed_photo()
        self.export_fails = True
        response = await self.client.post(
            f"{vote_web.ROUTE_PREFIX}/api/export", json={"init_data": _init_data(self.admin_id)}
        )
        data = await response.json()
        self.assertEqual(response.status, 200)
        self.assertFalse(data["delivered"])
        self.assertTrue(voting.export_image_path(CHAT, "2026-08-02").is_file())

    async def test_exporting_with_nothing_admitted_is_refused(self):
        self._seed_poll(approved=())
        response = await self.client.post(
            f"{vote_web.ROUTE_PREFIX}/api/export", json={"init_data": _init_data(self.admin_id)}
        )
        self.assertEqual(response.status, 409)

    async def test_the_admin_payload_carries_the_crops_and_a_voter_payload_does_not(self):
        poll = self._seed_poll()
        voting.set_crops(poll, {"a": {"x": 1, "y": 2, "size": 3}})
        voting.save_poll(poll)

        admin = await (await self.client.get(
            f"{vote_web.ROUTE_PREFIX}/api/poll?mode=admin",
            headers={"X-Telegram-Init-Data": _init_data(self.admin_id)},
        )).json()
        voter = await (await self.client.get(
            f"{vote_web.ROUTE_PREFIX}/api/poll",
            headers={"X-Telegram-Init-Data": _init_data(self.voter_id)},
        )).json()

        self.assertEqual(admin["crops"], {"a": {"x": 1.0, "y": 2.0, "size": 3.0}})
        self.assertNotIn("crops", voter)

    async def test_the_board_page_loads_without_auth_and_talks_to_the_admin_api(self):
        """The HTML shell is public like the ballot's; what makes it admin-only is that
        every request it makes is (mode=admin, /api/crops, /api/export)."""
        response = await self.client.get(f"{vote_web.ROUTE_PREFIX}/board")
        page = await response.text()
        self.assertEqual(response.status, 200)
        self.assertIn("/api/poll?mode=admin", page)
        self.assertIn("/api/crops", page)
        self.assertIn("/api/export", page)
        # The gesture surface: dragging pans, two fingers zoom, and the frame must not
        # scroll the page out from under the finger.
        self.assertIn('stage.addEventListener("pointerdown"', page)
        self.assertIn("touch-action: none", page)

    async def test_exporting_four_across_writes_its_own_wider_file(self):
        """Four-across is a different picture, not a replacement: both files survive, so
        an admin who rendered one and then the other still has both to choose from."""
        import vote_image

        self._seed_poll(approved=("a", "b"))
        self._seed_photo("a.jpg")
        three = await (await self.client.post(
            f"{vote_web.ROUTE_PREFIX}/api/export", json={"init_data": _init_data(self.admin_id)}
        )).json()
        four = await (await self.client.post(f"{vote_web.ROUTE_PREFIX}/api/export", json={
            "init_data": _init_data(self.admin_id), "columns": 4,
        })).json()

        self.assertEqual(three["columns"], 3)
        self.assertEqual(four["columns"], 4)
        self.assertNotEqual(three["url"], four["url"])
        self.assertTrue(voting.export_image_path(CHAT, "2026-08-02", 3).is_file())
        self.assertTrue(voting.export_image_path(CHAT, "2026-08-02", 4).is_file())
        served = await self.client.get(four["url"])
        self.assertEqual(served.status, 200)

    async def test_a_nonsense_column_count_falls_back_instead_of_failing(self):
        self._seed_poll()
        self._seed_photo()
        response = await self.client.post(f"{vote_web.ROUTE_PREFIX}/api/export", json={
            "init_data": _init_data(self.admin_id), "columns": "сколько-нибудь",
        })
        data = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(data["columns"], 3)

    async def test_the_board_page_hides_its_editor_until_a_card_is_tapped(self):
        """The crop editor sets `display: flex` to lay itself out, which outranks the UA
        rule behind the `hidden` attribute -- so without an explicit [hidden] rule it sat
        permanently over the grid, showing one empty frame and no photos at all."""
        page = await (await self.client.get(f"{vote_web.ROUTE_PREFIX}/board")).text()
        self.assertIn("[hidden] { display: none !important; }", page)
        self.assertIn('<div class="editor" id="editor" hidden>', page)

    async def test_the_ballot_page_hides_its_winner_banner_the_same_way(self):
        page = await (await self.client.get(vote_web.ROUTE_PREFIX)).text()
        self.assertIn("[hidden] { display: none !important; }", page)

    async def test_an_export_of_a_poll_that_was_never_rendered_is_a_404(self):
        response = await self.client.get(f"{vote_web.ROUTE_PREFIX}/export/2026-08-02.jpg")
        self.assertEqual(response.status, 404)

    async def test_the_export_route_refuses_a_traversal(self):
        response = await self.client.get(f"{vote_web.ROUTE_PREFIX}/export/..%2F..%2Fsecrets.jpg")
        self.assertIn(response.status, (400, 403, 404))

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

    async def test_the_standings_number_does_not_reuse_the_photo_badge_class(self):
        """`.count` is the grid thumbnail's absolutely positioned "+2 photos" badge. The
        standings tally borrowed the name and inherited position:absolute with it, which
        lifted every number out of its row -- hence class="num" and no `.results .count`
        rule left to grow one back."""
        page = await (await self.client.get(vote_web.ROUTE_PREFIX)).text()
        self.assertIn('<span class="num">', page)
        self.assertNotIn(".results .count", page)
        self.assertIn(".results .table { display: grid;", page)

    async def test_the_page_wires_a_tap_on_a_reel_photo_to_closing_it(self):
        """Tapping a picture in the reel goes back to the grid, and does it through the
        same closeReel() as the ✕ so Telegram's BackButton is always put away with it."""
        page = await (await self.client.get(vote_web.ROUTE_PREFIX)).text()
        self.assertIn('$("feed").addEventListener("pointerdown"', page)
        self.assertIn('$("feed").addEventListener("click"', page)
        self.assertIn("function closeReelAt(", page)
        self.assertIn("closeReelAt(card && card.dataset.entry)", page)


if __name__ == "__main__":
    unittest.main()
