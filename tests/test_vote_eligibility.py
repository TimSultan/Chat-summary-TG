"""vote_eligibility: only a member of Папка художников for at least a day may cast a ballot.

What is pinned: who is a member (getChatMember's answer, "restricted" and a departed owner
included); that administrators skip the join-date half; the 24-hour line itself, both
sides of it; that somebody the bot never saw join was a member before it watched, and may
vote; how the JoinLog writes joins down from chat_member updates, rubs out leaves, keeps
them across a restart, forgets old ones, and how often it touches the disk (counted);
and, end to end through bot_listener, the update routing, the verdict, the startup
check, and the VOTE_MEMBER_CHAT setting.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_api
import bot_listener
import config
import vote_eligibility
from errors import ChatSummaryError
from tests.async_case import AsyncTestCase

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
MEMBER = {"status": "member"}
USER = 42
PAPKA = "@papkahudojnicov"


def _member_update(user_id, old, new, when=NOW, chat=None):
    """A ChatMemberUpdated as Telegram sends it."""
    return {
        "chat": chat or {"id": -1001111, "type": "channel", "username": "papkahudojnicov"},
        "from": {"id": user_id},
        "date": int(when.timestamp()),
        "old_chat_member": {"status": old, "user": {"id": user_id}},
        "new_chat_member": {"status": new, "user": {"id": user_id, "username": f"u{user_id}"}},
    }


class InChatTests(unittest.TestCase):
    def test_creator_administrator_and_member_are_in_the_chat(self):
        for status in ("creator", "administrator", "member"):
            self.assertTrue(vote_eligibility.in_chat({"status": status}), status)

    def test_restricted_is_decided_by_its_own_flag(self):
        self.assertTrue(vote_eligibility.in_chat({"status": "restricted", "is_member": True}))
        self.assertFalse(vote_eligibility.in_chat({"status": "restricted", "is_member": False}))

    def test_an_owner_or_admin_marked_as_not_a_member_is_not(self):
        """An owner who left Папка художников keeps the ownership; voting needs membership."""
        self.assertFalse(vote_eligibility.in_chat({"status": "creator", "is_member": False}))
        self.assertFalse(vote_eligibility.in_chat({"status": "administrator", "is_member": False}))

    def test_left_kicked_and_a_failed_lookup_are_not(self):
        self.assertFalse(vote_eligibility.in_chat({"status": "left"}))
        self.assertFalse(vote_eligibility.in_chat({"status": "kicked"}))
        self.assertFalse(vote_eligibility.in_chat(None))


class JudgeTests(unittest.TestCase):
    def _judge(self, member, hours_ago=None):
        joined = None if hours_ago is None else NOW - timedelta(hours=hours_ago)
        return vote_eligibility.judge(member, joined, now=NOW)

    def test_a_member_the_bot_never_saw_join_may_vote(self):
        """The regression behind the screenshot: a long-standing member, no join date
        anywhere, must vote -- not be told the check could not be done."""
        verdict = self._judge(MEMBER)
        self.assertTrue(verdict.can_vote)
        self.assertIsNone(verdict.message())

    def test_a_member_of_more_than_a_day_may_vote(self):
        self.assertTrue(self._judge(MEMBER, hours_ago=25).can_vote)

    def test_exactly_a_day_is_enough(self):
        self.assertTrue(self._judge(MEMBER, hours_ago=24).can_vote)

    def test_a_member_of_less_than_a_day_may_not(self):
        verdict = self._judge(MEMBER, hours_ago=19)
        self.assertFalse(verdict.can_vote)
        self.assertTrue(verdict.is_member)
        self.assertEqual(verdict.reason, vote_eligibility.TOO_NEW)
        self.assertEqual(verdict.eligible_at, NOW + timedelta(hours=5))
        self.assertEqual(
            verdict.message(now=NOW),
            "Голосовать можно через сутки после вступления в Папку художников. "
            "Ваш голос можно будет отдать примерно через 5 ч.",
        )

    def test_a_minute_short_of_a_day_is_not(self):
        verdict = self._judge(MEMBER, hours_ago=24 - 1 / 60)
        self.assertFalse(verdict.can_vote)
        self.assertIn("меньше чем через час", verdict.message(now=NOW))

    def test_a_non_member_is_refused(self):
        for member in ({"status": "left"}, {"status": "kicked"}, None,
                       {"status": "restricted", "is_member": False}):
            verdict = self._judge(member, hours_ago=300)
            self.assertFalse(verdict.can_vote)
            self.assertFalse(verdict.is_member)
            self.assertEqual(verdict.reason, vote_eligibility.NOT_A_MEMBER)
            self.assertIn("участники Папки художников", verdict.message())

    def test_administrators_skip_the_join_date(self):
        for status in ("creator", "administrator"):
            self.assertTrue(self._judge({"status": status}, hours_ago=1).can_vote)

    def test_a_restricted_member_still_has_to_wait_a_day(self):
        verdict = self._judge({"status": "restricted", "is_member": True}, hours_ago=2)
        self.assertEqual(verdict.reason, vote_eligibility.TOO_NEW)


class MatchesChatTests(unittest.TestCase):
    def test_by_username_any_case(self):
        chat = {"id": -1001111, "username": "PapkaHudojnicov"}
        self.assertTrue(vote_eligibility.matches_chat(chat, PAPKA))
        self.assertFalse(vote_eligibility.matches_chat({"id": 1, "username": "other"}, PAPKA))
        self.assertFalse(vote_eligibility.matches_chat({"id": 1}, PAPKA))

    def test_by_numeric_id_or_resolved_id(self):
        self.assertTrue(vote_eligibility.matches_chat({"id": -1001111}, "-1001111"))
        self.assertTrue(vote_eligibility.matches_chat({"id": -100555}, "Chat", -100555))
        self.assertFalse(vote_eligibility.matches_chat({"id": -100555}, "Chat", None))
        self.assertFalse(vote_eligibility.matches_chat(None, PAPKA))


class JoinLogTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "joins" / "papka.json"
        self.now = NOW

    def _log(self):
        return vote_eligibility.JoinLog(self.path, now=lambda: self.now)

    def test_a_join_is_written_down_with_the_updates_own_date(self):
        log = self._log()
        joined = NOW - timedelta(hours=3)
        self.assertEqual(log.note(_member_update(USER, "left", "member", when=joined)), "joined")
        self.assertEqual(log.joined_at(USER), joined)
        self.assertEqual(log.joined_at(7), None)

    def test_a_leave_rubs_it_out_and_coming_back_starts_the_day_afresh(self):
        log = self._log()
        log.note(_member_update(USER, "left", "member", when=NOW - timedelta(hours=20)))
        self.assertEqual(log.note(_member_update(USER, "member", "left", when=NOW - timedelta(hours=2))), "left")
        self.assertIsNone(log.joined_at(USER))
        log.note(_member_update(USER, "kicked", "member", when=NOW - timedelta(hours=1)))
        self.assertEqual(log.joined_at(USER), NOW - timedelta(hours=1))

    def test_a_promotion_or_restriction_changes_nothing_and_writes_nothing(self):
        log = self._log()
        self.assertIsNone(log.note(_member_update(USER, "member", "administrator")))
        self.assertIsNone(log.note(_member_update(USER, "member", "restricted")
                                   | {"new_chat_member": {"status": "restricted", "is_member": True,
                                                          "user": {"id": USER}}}))
        self.assertEqual(log.writes, 0)
        self.assertFalse(self.path.exists())

    def test_somebody_unknown_leaving_writes_nothing(self):
        log = self._log()
        self.assertEqual(log.note(_member_update(USER, "member", "left")), "left")
        self.assertEqual(log.writes, 0)

    def test_it_survives_a_restart(self):
        self._log().note(_member_update(USER, "left", "member", when=NOW - timedelta(hours=4)))
        self.assertEqual(self._log().joined_at(USER), NOW - timedelta(hours=4))

    def test_old_joins_are_forgotten(self):
        log = self._log()
        log.note(_member_update(1, "left", "member", when=NOW - timedelta(days=3)))
        log.note(_member_update(2, "left", "member", when=NOW - timedelta(hours=1)))
        self.assertIsNone(log.joined_at(1))
        self.assertEqual(self._log().joined_at(2), NOW - timedelta(hours=1))
        self.assertNotIn('"1"', self.path.read_text(encoding="utf-8"))

    def test_the_disk_is_read_once_and_written_once_per_change(self):
        log = self._log()
        for uid in range(1, 21):
            log.note(_member_update(uid, "left", "member", when=NOW - timedelta(minutes=uid)))
        for _ in range(3):
            for uid in range(1, 51):
                log.joined_at(uid)
        self.assertEqual(log.reads, 1)
        self.assertEqual(log.writes, 20)

    def test_a_broken_file_reads_as_no_joins(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(self._log().joined_at(USER))


class MembershipOnlyTests(AsyncTestCase):
    async def test_it_turns_a_plain_membership_answer_into_a_verdict(self):
        async def is_member(user):
            return user["id"] == 1

        verdict = vote_eligibility.membership_only(is_member)
        self.assertTrue((await verdict({"id": 1})).can_vote)
        refused = await verdict({"id": 2})
        self.assertEqual(refused.reason, vote_eligibility.NOT_A_MEMBER)
        self.assertFalse(refused.is_member)


# ------------------------------------------------- bot_listener's wiring, end to end

class FakeBotApi:
    def __init__(self, members, error=None):
        self.members = members
        self.error = error
        self.calls = []
        self.sent = []

    async def get_chat_member(self, chat_id, user_id):
        self.calls.append((chat_id, user_id))
        if self.error is not None:
            raise self.error
        member = self.members.get(user_id)
        if member is None:
            raise ChatSummaryError("Telegram Bot API getChatMember failed: Bad Request: user not found")
        return member

    async def send_message(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return {"message_id": 1}


class _VotingDir:
    def _voting_dir(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        patcher = patch("voting._voting_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        bot_listener._VOTE_JOIN_LOGS.clear()
        self.addCleanup(bot_listener._VOTE_JOIN_LOGS.clear)


class VoteVerdictTests(_VotingDir, AsyncTestCase):
    def setUp(self):
        self._voting_dir()
        self.resolved = []
        self.cfg = SimpleNamespace(vote_member_chat=PAPKA)

    async def _verdict(self, api, user, chat_ref=PAPKA, home_chat_id=-100555):
        async def resolve_id(client, entry, known, log=print):
            self.resolved.append(entry)
            return home_chat_id

        with patch.object(bot_listener, "_resolve_chat_id", resolve_id):
            return await bot_listener._vote_verdict(api, None, chat_ref, user, {}, log=lambda *_: None)

    async def _see(self, update):
        await bot_listener._note_vote_member_update(self.cfg, update, "Chat", {}, log=lambda *_: None)

    async def test_a_long_standing_member_votes_with_no_userbot_at_all(self):
        """The screenshot: @RebelSoldier, in Папка художников, was refused because the
        userbot could not read its member list. Nothing but the bot is asked now."""
        verdict = await self._verdict(FakeBotApi({1: MEMBER}), {"id": 1, "username": "RebelSoldier"})
        self.assertTrue(verdict.can_vote)
        self.assertTrue(verdict.is_member)

    async def test_somebody_the_bot_saw_join_an_hour_ago_waits(self):
        await self._see(_member_update(2, "left", "member", when=datetime.now(timezone.utc) - timedelta(hours=1)))
        verdict = await self._verdict(FakeBotApi({2: MEMBER}), {"id": 2})
        self.assertEqual(verdict.reason, vote_eligibility.TOO_NEW)
        self.assertIn("примерно через 23 ч", verdict.message())

    async def test_both_questions_are_about_papka_not_the_tracked_chat(self):
        api = FakeBotApi({1: MEMBER, 3: {"status": "left"}})
        member, gone = [await self._verdict(api, {"id": uid}) for uid in (1, 3)]
        self.assertTrue(member.can_vote)
        self.assertEqual(gone.reason, vote_eligibility.NOT_A_MEMBER)
        self.assertEqual(api.calls, [(PAPKA, 1), (PAPKA, 3)])
        self.assertEqual(self.resolved, [])

    async def test_another_chats_joins_are_not_papkas(self):
        other = {"id": -100999, "type": "supergroup", "username": "elsewhere"}
        await self._see(_member_update(4, "left", "member", when=datetime.now(timezone.utc), chat=other))
        self.assertTrue((await self._verdict(FakeBotApi({4: MEMBER}), {"id": 4})).can_vote)

    async def test_somebody_who_never_joined_is_not_a_member(self):
        verdict = await self._verdict(FakeBotApi({}), {"id": 9})
        self.assertEqual(verdict.reason, vote_eligibility.NOT_A_MEMBER)

    async def test_a_bot_that_cannot_ask_refuses_as_unverified_not_as_a_stranger(self):
        api = FakeBotApi({}, error=ChatSummaryError("Bad Request: member list is inaccessible"))
        verdict = await self._verdict(api, {"id": 1})
        self.assertEqual(verdict.reason, vote_eligibility.UNVERIFIED)
        self.assertIn("состоите ли вы в Папке художников", verdict.message())

    async def test_no_member_chat_falls_back_to_resolving_the_tracked_chat(self):
        api = FakeBotApi({1: MEMBER})
        verdict = await self._verdict(api, {"id": 1}, chat_ref="Chat")
        self.assertTrue(verdict.can_vote)
        self.assertEqual(self.resolved, ["Chat"])
        self.assertEqual(api.calls, [(-100555, 1)])

    async def test_an_unresolvable_chat_refuses_as_unverified(self):
        verdict = await self._verdict(FakeBotApi({1: MEMBER}), {"id": 1}, chat_ref="Chat", home_chat_id=None)
        self.assertEqual(verdict.reason, vote_eligibility.UNVERIFIED)


class DispatchTests(_VotingDir, AsyncTestCase):
    """A chat_member update reaches the JoinLog through the bot's ordinary dispatcher,
    says nothing to anybody, and Telegram is asked to send such updates at all."""

    def setUp(self):
        self._voting_dir()

    async def _dispatch(self, update):
        api = FakeBotApi({})
        cfg = SimpleNamespace(vote_member_chat=PAPKA, listener_allowed_chats=["Chat"],
                              webapp_public_url=None, stats_enabled=True, stats_top_limit=10,
                              vote_miniapp_short_name=None, vote_announce_extra_chat=None)
        tasks: set = set()
        await bot_listener._dispatch_update(
            {"update_id": 1, "chat_member": update}, api, None, cfg, None, "testbot", 1, set(),
            asyncio.Queue(), tasks, "Chat", {}, {}, {}, {}, log=lambda *_: None,
        )
        await asyncio.gather(*tasks)
        return api

    async def test_a_join_to_papka_is_written_down_and_nothing_is_sent(self):
        joined = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=2)
        api = await self._dispatch(_member_update(USER, "left", "member", when=joined))
        self.assertEqual(bot_listener._vote_join_log(PAPKA).joined_at(USER), joined)
        self.assertEqual(api.sent, [])

    def test_getupdates_asks_for_chat_member(self):
        captured = {}

        async def fake_call(method, _http_timeout=20.0, **params):
            captured.update(params)
            return []

        client = bot_api.TelegramBotAPI("1:TOKEN", session=None)
        with patch.object(client, "_call", fake_call):
            asyncio.run(client.get_updates(offset=None, timeout=0))
        self.assertIn("chat_member", captured["allowed_updates"])


class StartupCheckTests(AsyncTestCase):
    async def _check(self, status):
        lines = []
        api = FakeBotApi({777: {"status": status}})
        await bot_listener._check_vote_join_watch(api, PAPKA, 777, log=lines.append)
        return lines

    async def test_an_administrator_bot_is_watching(self):
        self.assertIn("24-hour voting rule is on", (await self._check("administrator"))[0])

    async def test_a_plain_member_bot_is_warned_about(self):
        self.assertIn("not an administrator", (await self._check("member"))[0])


class MemberChatSettingTests(unittest.TestCase):
    def test_spellings(self):
        self.assertEqual(config._chat_handle("papkahudojnicov"), PAPKA)
        self.assertEqual(config._chat_handle(" @papkahudojnicov "), PAPKA)
        self.assertEqual(config._chat_handle("-1001234"), "-1001234")
        self.assertIsNone(config._chat_handle(""))
        self.assertIsNone(config._chat_handle(None))

    def _load(self, **extra_env):
        env = {"TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "h", "OPENAI_API_KEY": "k", **extra_env}
        with patch.dict(os.environ, env, clear=True):
            return config.load_config()

    def test_papka_is_the_default(self):
        self.assertEqual(self._load().vote_member_chat, PAPKA)

    def test_it_can_be_pointed_elsewhere_or_left_to_the_tracked_chat(self):
        self.assertEqual(self._load(VOTE_MEMBER_CHAT="otherpapka").vote_member_chat, "@otherpapka")
        self.assertIsNone(self._load(VOTE_MEMBER_CHAT="").vote_member_chat)


if __name__ == "__main__":
    unittest.main()
