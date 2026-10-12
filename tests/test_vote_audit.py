"""The administrators' vote audit log (vote_audit.py) and the parts of the ballot paths
that feed it.

What is pinned:
- a recorded event keeps who/what/when/how and the launch origin, and nothing more;
- the log is append-only and survives a torn last line;
- a Mini App ballot (vote_web) and a link ballot (bot_listener) both write one event with
  the right `via`, membership snapshot and choices;
- recording never turns an accepted ballot into an error, even when the disk refuses;
- the rollup collapses re-votes to the final ballot while keeping the submission count;
- the log lives under the voting store, so redirecting that store redirects it too.
"""

import asyncio
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

import bot_listener
import vote_audit
import vote_web
import voting
from tests.async_case import AsyncTestCase

BOT_TOKEN = "123456:FAKE-TOKEN-FOR-TESTS"
CHAT = "Chat"


def _sign(fields: dict) -> str:
    payload = dict(fields)
    check_string = "\n".join(f"{k}={payload[k]}" for k in sorted(payload))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    payload["hash"] = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    return urlencode(payload)


def _init_data(user_id: int, username="voter", **extra) -> str:
    fields = {
        "auth_date": str(int(time.time())),
        "user": json.dumps({"id": user_id, "username": username, "first_name": "V"}),
    }
    fields.update(extra)
    return _sign(fields)


_AUTHOR_IDS = {"a": 101, "b": 202}


def _entry(entry_id: str) -> voting.Entry:
    numeric = _AUTHOR_IDS.get(entry_id, abs(hash(entry_id)) % 10000)
    return voting.Entry(
        entry_id=entry_id, message_id=numeric, author_id=numeric,
        author_name=f"Author {entry_id}", author_username=f"author{entry_id}",
        text="", media=[f"{entry_id}.jpg"],
    )


def _seed(approved=("a", "b"), open_=True, allow_revote=True, max_choices=None):
    poll = voting.Poll(poll_id="2026-W41", entry=CHAT, created_at="2026-10-05T00:00:00+00:00",
                       entries=[_entry("a"), _entry("b")])
    voting.set_approved(poll, list(approved))
    poll.open, poll.allow_revote, poll.max_choices = open_, allow_revote, max_choices
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


# ---------------------------------------------------------------- the log, in isolation

class AuditLogTests(_Storage, unittest.TestCase):
    def setUp(self):
        self._storage()

    def tearDown(self):
        self._unstorage()

    def test_an_event_keeps_only_what_a_ballot_already_handled(self):
        vote_audit.record_vote_event(
            CHAT, system="v1", poll_id="2026-W41", voter_id=7, name="Vera",
            username="vera", choices=["a", "b"], is_member=True, via="miniapp",
            source={"chat_type": "sender", "start_param": "vote"},
        )
        events = vote_audit.read_events(CHAT)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["voter_id"], "7")
        self.assertEqual(event["choices"], ["a", "b"])
        self.assertEqual(event["is_member"], True)
        self.assertEqual(event["via"], "miniapp")
        self.assertEqual(event["source"], {"chat_type": "sender", "start_param": "vote"})
        # No reaching for anything beyond the agreed fields -- no ip, device, or profile.
        self.assertEqual(
            set(event),
            {"ts", "system", "poll_id", "voter_id", "name", "username", "via", "choices",
             "is_member", "source"},
        )

    def test_it_is_append_only_and_keeps_order(self):
        for i in range(3):
            vote_audit.record_vote_event(
                CHAT, system="v1", poll_id="2026-W41", voter_id=i, name=f"u{i}",
                username=None, choices=["a"], is_member=None, via="link",
            )
        self.assertEqual([e["voter_id"] for e in vote_audit.read_events(CHAT)], ["0", "1", "2"])

    def test_a_torn_last_line_is_skipped_not_fatal(self):
        vote_audit.record_vote_event(
            CHAT, system="v1", poll_id="2026-W41", voter_id=1, name="u", username=None,
            choices=["a"], is_member=True, via="miniapp",
        )
        with vote_audit.log_path(CHAT).open("a", encoding="utf-8") as handle:
            handle.write('{"voter_id": "2", "cho')  # a crash mid-append
        events = vote_audit.read_events(CHAT)
        self.assertEqual([e["voter_id"] for e in events], ["1"])

    def test_filters_by_poll_and_system(self):
        vote_audit.record_vote_event(CHAT, system="v1", poll_id="w1", voter_id=1, name="a",
                                     username=None, choices=["a"], is_member=None, via="miniapp")
        vote_audit.record_vote_event(CHAT, system="v3", poll_id="nom:x", voter_id=2, name="b",
                                     username=None, choices=["a"], is_member=None, via="nomination")
        self.assertEqual(len(vote_audit.read_events(CHAT, system="v1")), 1)
        self.assertEqual(len(vote_audit.read_events(CHAT, poll_id="nom:x")), 1)

    def test_rollup_collapses_revotes_but_counts_them(self):
        for choice in (["a"], ["a", "b"], ["b"]):
            vote_audit.record_vote_event(CHAT, system="v1", poll_id="w1", voter_id=5, name="Vera",
                                         username="vera", choices=choice, is_member=True, via="miniapp")
        rollup = vote_audit.voter_rollup(vote_audit.read_events(CHAT))
        self.assertEqual(rollup["5"]["submissions"], 3)
        self.assertEqual(rollup["5"]["choices"], ["b"])  # the last ballot wins, as the poll does
        self.assertLessEqual(rollup["5"]["first_ts"], rollup["5"]["last_ts"])

    def test_recording_never_raises_back_into_the_ballot_path(self):
        logged = []
        with patch.object(vote_audit, "_audit_dir", side_effect=OSError("disk full")):
            vote_audit.record_vote_event(
                CHAT, system="v1", poll_id="w1", voter_id=1, name="u", username=None,
                choices=["a"], is_member=True, via="miniapp", log=logged.append,
            )
        self.assertTrue(any("could not record" in line for line in logged))

    def test_parse_source_reads_launch_origin_only(self):
        init_data = _init_data(7, chat_type="group", chat_instance="999", start_param="vote")
        self.assertEqual(
            vote_audit.parse_source(init_data),
            {"chat_type": "group", "chat_instance": "999", "start_param": "vote"},
        )
        self.assertEqual(vote_audit.parse_source(""), {})

    def test_the_log_lives_under_the_voting_store(self):
        vote_audit.record_vote_event(CHAT, system="v1", poll_id="w1", voter_id=1, name="u",
                                     username=None, choices=["a"], is_member=None, via="link")
        self.assertTrue(vote_audit.log_path(CHAT).exists())
        self.assertEqual(vote_audit.log_path(CHAT).parent, Path(self._temporary.name) / "audit")


# ------------------------------------------------------------ the Mini App ballot path

class MiniAppBallotWritesAuditTests(AsyncTestCase):
    async def asyncSetUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self._patcher = patch("voting._voting_dir", return_value=Path(self._temporary.name))
        self._patcher.start()
        _seed()
        cfg = SimpleNamespace(telegram_bot_token=BOT_TOKEN)

        async def is_admin(user):
            return False

        async def is_member(user):
            return user.get("id") == 7  # 7 is a member, 8 is not

        app = vote_web.create_app(cfg, CHAT, is_admin, is_member=is_member, log=lambda *_: None)
        self.server = TestServer(app)
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self._patcher.stop()
        self._temporary.cleanup()

    async def _ballot(self, user_id, choices, **source):
        resp = await self.client.post(f"{vote_web.ROUTE_PREFIX}/api/ballot", json={
            "init_data": _init_data(user_id, **source), "choices": choices,
        })
        self.assertEqual(resp.status, 200, await resp.text())

    async def test_a_miniapp_ballot_writes_one_event(self):
        await self._ballot(7, ["a"], chat_type="sender", start_param="vote")
        events = vote_audit.read_events(CHAT, system="v1")
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["voter_id"], "7")
        self.assertEqual(event["via"], "miniapp")
        self.assertEqual(event["is_member"], True)
        self.assertEqual(event["choices"], ["a"])
        self.assertEqual(event["source"], {"chat_type": "sender", "start_param": "vote"})

    async def test_a_non_member_is_recorded_as_such(self):
        await self._ballot(8, ["a"])
        event = vote_audit.read_events(CHAT, system="v1")[0]
        self.assertEqual(event["is_member"], False)

    async def test_revotes_each_leave_a_line(self):
        await self._ballot(7, ["a"])
        await self._ballot(7, ["a", "b"])
        rollup = vote_audit.voter_rollup(vote_audit.read_events(CHAT))
        self.assertEqual(rollup["7"]["submissions"], 2)
        self.assertEqual(sorted(rollup["7"]["choices"]), ["a", "b"])


# ----------------------------------------------------------------- the link ballot path

class LinkBallotWritesAuditTests(_Storage, unittest.TestCase):
    def setUp(self):
        self._storage()

    def tearDown(self):
        self._unstorage()

    def test_a_link_ballot_writes_a_link_event(self):
        # A link ballot carries entry ids as base-36 message ids, so this poll's entries
        # are numeric (unlike the symbolic a/b the Mini App tests use).
        poll = voting.Poll(poll_id="2026-W41", entry=CHAT, created_at="2026-10-05T00:00:00+00:00",
                           entries=[_entry("101"), _entry("202")])
        voting.set_approved(poll, ["101", "202"])
        voting.save_poll(poll)

        class FakeApi:
            async def send_message(self, *a, **k):
                return {"message_id": 1}

        message = {"message_id": 1, "chat": {"id": 555, "type": "private"},
                   "from": {"id": 9, "username": "linkvoter", "first_name": "L"}}

        async def resolve(*a, **k):
            return -1001234567890

        async def member(api_, chat_id, user_id):
            return True

        payload = voting.encode_ballot_link(["101"])
        with patch.object(bot_listener, "_resolve_chat_id", resolve), \
                patch.object(bot_listener, "_is_chat_member", member):
            asyncio.run(bot_listener.handle_vote_link_ballot(
                FakeApi(), None, message, CHAT, payload, log=lambda *_: None))

        events = vote_audit.read_events(CHAT, system="v1")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["via"], "link")
        self.assertEqual(events[0]["voter_id"], "9")
        self.assertEqual(events[0]["choices"], ["101"])
        self.assertEqual(events[0]["is_member"], True)
        self.assertEqual(events[0]["source"], {})  # a link has no Mini App launch data


if __name__ == "__main__":
    unittest.main()
