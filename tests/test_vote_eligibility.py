"""vote_eligibility: only a member of the chat for at least a day may cast a ballot.

What is pinned: who is in the chat (getChatMember's answer, "restricted" included); that
administrators skip the join-date half; the 24-hour line itself, both sides of it; that a
check which cannot finish refuses rather than allows; and how the kept member list is
read -- once for everybody already in it, again only for somebody it does not list, at
most once per refresh interval, and one read at a time however many voters ask together.
The last block drives bot_listener._vote_verdict end to end with fake Telegram clients.
"""

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_listener
import vote_eligibility
from errors import ChatSummaryError
from tests.async_case import AsyncTestCase

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
MEMBER = {"status": "member"}
USER = 42


class FakeClock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


class CountingFetch:
    """The member list as the userbot would read it, counting the reads."""

    def __init__(self, dates=None, error=None):
        self.dates = dict(dates or {})
        self.error = error
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return dict(self.dates)


def _dates(fetch, clock=None, refresh_seconds=30.0):
    return vote_eligibility.JoinDates(
        fetch, refresh_seconds=refresh_seconds, clock=clock or FakeClock(),
        now=lambda: NOW, log=lambda *_: None,
    )


async def _judge(member, dates, user_id=USER):
    return await vote_eligibility.judge(member, user_id, dates, now=NOW, log=lambda *_: None)


class InChatTests(unittest.TestCase):
    def test_creator_administrator_and_member_are_in_the_chat(self):
        for status in ("creator", "administrator", "member"):
            self.assertTrue(vote_eligibility.in_chat({"status": status}), status)

    def test_restricted_is_decided_by_its_own_flag(self):
        self.assertTrue(vote_eligibility.in_chat({"status": "restricted", "is_member": True}))
        self.assertFalse(vote_eligibility.in_chat({"status": "restricted", "is_member": False}))

    def test_left_kicked_and_a_failed_lookup_are_not(self):
        self.assertFalse(vote_eligibility.in_chat({"status": "left"}))
        self.assertFalse(vote_eligibility.in_chat({"status": "kicked"}))
        self.assertFalse(vote_eligibility.in_chat(None))


class JudgeTests(AsyncTestCase):
    async def test_a_member_of_more_than_a_day_may_vote(self):
        fetch = CountingFetch({USER: NOW - timedelta(hours=25)})
        verdict = await _judge(MEMBER, _dates(fetch))
        self.assertTrue(verdict.can_vote)
        self.assertTrue(verdict.is_member)
        self.assertIsNone(verdict.message())

    def _too_new(self, verdict, hours_left):
        self.assertFalse(verdict.can_vote)
        self.assertTrue(verdict.is_member)
        self.assertEqual(verdict.reason, vote_eligibility.TOO_NEW)
        self.assertEqual(verdict.eligible_at, NOW + timedelta(hours=hours_left))

    async def test_a_member_of_less_than_a_day_may_not(self):
        fetch = CountingFetch({USER: NOW - timedelta(hours=19)})
        verdict = await _judge(MEMBER, _dates(fetch))
        self._too_new(verdict, 5)
        self.assertEqual(
            verdict.message(now=NOW),
            "Голосовать можно через сутки после вступления в чат. "
            "Ваш голос можно будет отдать примерно через 5 ч.",
        )

    async def test_exactly_a_day_is_enough(self):
        fetch = CountingFetch({USER: NOW - vote_eligibility.MIN_MEMBERSHIP})
        self.assertTrue((await _judge(MEMBER, _dates(fetch))).can_vote)

    async def test_a_minute_short_of_a_day_is_not(self):
        joined = NOW - vote_eligibility.MIN_MEMBERSHIP + timedelta(minutes=1)
        verdict = await _judge(MEMBER, _dates(CountingFetch({USER: joined})))
        self.assertFalse(verdict.can_vote)
        self.assertIn("меньше чем через час", verdict.message(now=NOW))

    async def test_a_non_member_is_refused_without_reading_the_member_list(self):
        fetch = CountingFetch({USER: NOW - timedelta(days=300)})
        for member in ({"status": "left"}, {"status": "kicked"}, None,
                       {"status": "restricted", "is_member": False}):
            verdict = await _judge(member, _dates(fetch))
            self.assertFalse(verdict.can_vote)
            self.assertFalse(verdict.is_member)
            self.assertEqual(verdict.reason, vote_eligibility.NOT_A_MEMBER)
            self.assertIn("не меньше суток", verdict.message())
        self.assertEqual(fetch.calls, 0)

    async def test_administrators_skip_the_join_date(self):
        """An administrator promoted an hour ago is still trusted -- and the member list is
        not read for them at all."""
        fetch = CountingFetch({USER: NOW - timedelta(hours=1)})
        for status in ("creator", "administrator"):
            self.assertTrue((await _judge({"status": status}, _dates(fetch))).can_vote)
        self.assertEqual(fetch.calls, 0)

    async def test_a_restricted_member_still_has_to_wait_a_day(self):
        fetch = CountingFetch({USER: NOW - timedelta(hours=2)})
        verdict = await _judge({"status": "restricted", "is_member": True}, _dates(fetch))
        self.assertEqual(verdict.reason, vote_eligibility.TOO_NEW)

    async def test_a_member_the_list_does_not_have_joined_after_it_was_read(self):
        fetch = CountingFetch({1: NOW - timedelta(days=30)})
        verdict = await _judge(MEMBER, _dates(fetch))
        self._too_new(verdict, 24)

    async def test_a_list_that_cannot_be_read_refuses_rather_than_allows(self):
        fetch = CountingFetch(error=ChatSummaryError("no Telethon session"))
        verdict = await _judge(MEMBER, _dates(fetch))
        self.assertFalse(verdict.can_vote)
        self.assertTrue(verdict.is_member)
        self.assertEqual(verdict.reason, vote_eligibility.UNVERIFIED)
        self.assertIn("Попробуйте ещё раз", verdict.message())

    async def test_a_list_read_that_hangs_times_out_as_unverified(self):
        async def hang():
            await asyncio.Event().wait()

        dates = vote_eligibility.JoinDates(hang, timeout=0.01, log=lambda *_: None)
        verdict = await _judge(MEMBER, dates)
        self.assertEqual(verdict.reason, vote_eligibility.UNVERIFIED)

    async def test_the_creators_record_has_no_date_and_counts_as_founding(self):
        creator = SimpleNamespace(user_id=USER, admin_rights=None, rank=None)
        joined = vote_eligibility.join_date_of(creator)
        self.assertEqual(joined, vote_eligibility.FOUNDING)
        self.assertTrue((await _judge(MEMBER, _dates(CountingFetch({USER: joined})))).can_vote)


class JoinDateOfTests(unittest.TestCase):
    def test_a_record_with_a_date_is_that_date(self):
        joined = datetime(2026, 1, 2, tzinfo=timezone.utc)
        self.assertEqual(vote_eligibility.join_date_of(SimpleNamespace(date=joined)), joined)

    def test_a_naive_date_is_read_as_utc(self):
        self.assertEqual(
            vote_eligibility.join_date_of(SimpleNamespace(date=datetime(2026, 1, 2))),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

    def test_a_restricted_record_that_left_is_nobody(self):
        self.assertIsNone(vote_eligibility.join_date_of(SimpleNamespace(date=NOW, left=True)))
        self.assertIsNone(vote_eligibility.join_date_of(None))


class JoinDatesReadTests(AsyncTestCase):
    """How often the chat's member list is read -- counted, since every read is a round of
    userbot requests and a wave of voters must not turn into a wave of reads."""

    async def test_one_read_answers_everybody_already_in_the_chat(self):
        fetch = CountingFetch({uid: NOW - timedelta(days=uid) for uid in range(1, 51)})
        dates = _dates(fetch)
        for uid in range(1, 51):
            self.assertEqual(await dates.joined_at(uid), NOW - timedelta(days=uid))
        self.assertEqual(fetch.calls, 1)

    async def test_somebody_unlisted_is_read_again_but_not_more_than_once_per_interval(self):
        clock = FakeClock()
        fetch = CountingFetch({1: NOW - timedelta(days=3)})
        dates = _dates(fetch, clock=clock, refresh_seconds=30.0)
        await dates.joined_at(1)
        clock.value += 31
        joined_since = NOW - timedelta(hours=2)
        fetch.dates[2] = joined_since
        self.assertEqual(await dates.joined_at(2), joined_since)
        self.assertEqual(fetch.calls, 2)

        # Three strangers within the interval: no further read, each counted as joined
        # at the moment of the last one.
        for uid in (3, 4, 5):
            self.assertEqual(await dates.joined_at(uid), NOW)
        self.assertEqual(fetch.calls, 2)

        clock.value += 31
        await dates.joined_at(3)
        self.assertEqual(fetch.calls, 3)

    async def test_voters_asking_together_share_one_read(self):
        release = asyncio.Event()
        started = asyncio.Event()
        calls = 0

        async def fetch():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return {uid: NOW - timedelta(days=2) for uid in range(1, 11)}

        dates = _dates(fetch)
        waiting = [asyncio.create_task(dates.joined_at(uid)) for uid in range(1, 11)]
        await started.wait()
        release.set()
        results = await asyncio.gather(*waiting)
        self.assertEqual(calls, 1)
        self.assertEqual(results, [NOW - timedelta(days=2)] * 10)

    async def test_a_listed_member_is_not_held_up_by_somebody_elses_read(self):
        release = asyncio.Event()
        reads = 0

        async def fetch():
            nonlocal reads
            reads += 1
            if reads == 2:
                await release.wait()
            return {1: NOW - timedelta(days=5)}

        clock = FakeClock()
        dates = _dates(fetch, clock=clock)
        await dates.joined_at(1)
        clock.value += 60
        stalled = asyncio.create_task(dates.joined_at(2))  # starts the second, stuck read
        await asyncio.sleep(0)
        self.assertEqual(await asyncio.wait_for(dates.joined_at(1), timeout=1),
                         NOW - timedelta(days=5))
        self.assertFalse(stalled.done())
        release.set()
        await stalled

    async def test_a_failed_read_is_retried_on_the_next_question(self):
        fetch = CountingFetch(error=ChatSummaryError("flood wait"))
        dates = _dates(fetch)
        with self.assertRaises(ChatSummaryError):
            await dates.joined_at(1)
        fetch.error = None
        fetch.dates = {1: NOW - timedelta(days=9)}
        self.assertEqual(await dates.joined_at(1), NOW - timedelta(days=9))
        self.assertEqual(fetch.calls, 2)


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
    def __init__(self, members):
        self.members = members
        self.calls = []

    async def get_chat_member(self, chat_id, user_id):
        self.calls.append((chat_id, user_id))
        member = self.members.get(user_id)
        if member is None:
            raise ChatSummaryError("Bad Request: user not found")
        return member


class FakeTelethon:
    """iter_participants only, yielding records shaped like Telethon's: a user with a
    `.participant` carrying the join date (or none, for the creator)."""

    def __init__(self, joined):
        self.joined = joined
        self.reads = 0

    async def iter_participants(self, chat):
        self.reads += 1
        for user_id, date in self.joined.items():
            participant = SimpleNamespace(user_id=user_id)
            if date is not None:
                participant.date = date
            yield SimpleNamespace(id=user_id, participant=participant)


class VoteVerdictTests(AsyncTestCase):
    CHAT_ID = -1001234567890
    ENTRY = "Chat"

    def setUp(self):
        bot_listener._VOTE_JOIN_DATES.clear()
        self.addCleanup(bot_listener._VOTE_JOIN_DATES.clear)

    async def _verdict(self, api, telethon, user_id, chat_id=CHAT_ID):
        async def resolve_id(*args, **kwargs):
            return chat_id

        async def resolve_chat(client, entry):
            return SimpleNamespace(id=chat_id)

        with patch.object(bot_listener, "_resolve_chat_id", resolve_id), \
                patch.object(bot_listener, "resolve_chat", resolve_chat):
            return await bot_listener._vote_verdict(
                api, telethon, self.ENTRY, user_id, {}, log=lambda *_: None,
            )

    async def test_the_two_halves_come_from_the_two_clients(self):
        now = datetime.now(timezone.utc)
        api = FakeBotApi({1: MEMBER, 2: MEMBER, 3: {"status": "left"}})
        telethon = FakeTelethon({1: now - timedelta(days=40), 2: now - timedelta(hours=3)})

        old, fresh, gone = [await self._verdict(api, telethon, uid) for uid in (1, 2, 3)]
        self.assertTrue(old.can_vote)
        self.assertEqual(fresh.reason, vote_eligibility.TOO_NEW)
        self.assertEqual(gone.reason, vote_eligibility.NOT_A_MEMBER)
        # One getChatMember each, one member-list read for all of them.
        self.assertEqual(api.calls, [(self.CHAT_ID, 1), (self.CHAT_ID, 2), (self.CHAT_ID, 3)])
        self.assertEqual(telethon.reads, 1)

    async def test_somebody_who_never_joined_is_not_a_member(self):
        verdict = await self._verdict(FakeBotApi({}), FakeTelethon({}), 9)
        self.assertEqual(verdict.reason, vote_eligibility.NOT_A_MEMBER)

    async def test_an_unresolvable_chat_refuses_as_unverified(self):
        verdict = await self._verdict(FakeBotApi({1: MEMBER}), FakeTelethon({}), 1, chat_id=None)
        self.assertEqual(verdict.reason, vote_eligibility.UNVERIFIED)
        self.assertFalse(verdict.can_vote)

    async def test_no_userbot_refuses_as_unverified(self):
        verdict = await self._verdict(FakeBotApi({1: MEMBER}), None, 1)
        self.assertEqual(verdict.reason, vote_eligibility.UNVERIFIED)
        self.assertTrue(verdict.is_member)

    async def test_the_creator_without_a_date_may_vote(self):
        telethon = FakeTelethon({1: None})
        self.assertTrue((await self._verdict(FakeBotApi({1: MEMBER}), telethon, 1)).can_vote)


if __name__ == "__main__":
    unittest.main()
