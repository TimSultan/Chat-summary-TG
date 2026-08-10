"""What a week's poll is allowed to contain.

The rule: a poll holds exactly the works nominated in its own Monday-to-Sunday window.
Nothing rolls over from the previous week -- an earlier version pre-filled a new poll with
last week's runners-up, which made "очистить, then собрать" impossible to express because
the collect immediately put last week back.

This file replaces the old test_vote_carryover.py. The two rules that survived that
feature -- re-collecting must not undo moderation, and must not lose votes -- are kept
here, because they are properties of collecting, not of the carry-over.
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import arena
import bot_listener
import voting

CHAT = "Chat"
LAST_WEEK = "2026-W30"
THIS_WEEK = "2026-W31"


def _fake_poll_id(tz, weeks_ago=0):
    """Stands in for bot_listener._current_vote_poll_id so the tests don't depend on which
    ISO week they happen to run in -- and, since собрать now takes a window, on which week
    the command asked for."""
    return LAST_WEEK if weeks_ago else THIS_WEEK


def _entry(entry_id, name=None, media=("a.jpg",)):
    return voting.Entry(
        entry_id=entry_id, message_id=int(entry_id), author_id=int(entry_id),
        author_name=name or f"Автор {entry_id}", author_username=f"user{entry_id}",
        text="", media=list(media),
    )


class CollectWindowTests(unittest.TestCase):
    """/vote собрать end to end, with the chat scan itself stubbed out."""

    class FakeApi:
        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id, text, reply_to_message_id=None,
                               reply_markup=None, parse_mode=None,
                               disable_notification=False):
            self.sent.append(text)
            return {"message_id": 1}

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("voting._voting_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)
        self.api = self.FakeApi()
        self.collect_kwargs = {}

        # A finished previous week: five works, all admitted, entry i with i votes.
        entries = [_entry(str(i), media=[f"{i}.jpg"]) for i in range(5)]
        poll = voting.Poll(poll_id=LAST_WEEK, entry=CHAT, created_at="2026-07-20", entries=entries)
        voting.set_approved(poll, [e.entry_id for e in entries])
        for index in range(5):
            for voter in range(index):
                voting.record_vote(poll, f"{index}-{voter}", [str(index)])
        voting.save_poll(poll)
        media = voting.media_path(CHAT, LAST_WEEK)
        media.mkdir(parents=True)
        for index in range(5):
            (media / f"{index}.jpg").write_bytes(b"jpeg-ish")

    def _collect(self, new_entries=(), text="/vote собрать", poll_id=THIS_WEEK):
        async def collect_entries(**kwargs):
            self.collect_kwargs = kwargs
            return list(new_entries)

        async def resolve(*args, **kwargs):
            return -100

        async def can_manage(*args, **kwargs):
            return True

        message = {
            "message_id": 1,
            "chat": {"id": 5, "type": "private"},
            "from": {"id": 7, "username": "admin"},
            "text": text,
        }
        cfg = SimpleNamespace(
            webapp_public_url="https://example.com",
            vote_miniapp_short_name=None,
            vote_announce_extra_chat=None,
        )
        with patch.object(bot_listener, "_resolve_chat_id", resolve), \
             patch.object(bot_listener, "_can_manage_chat", can_manage), \
             patch.object(voting, "collect_entries", collect_entries), \
             patch.object(bot_listener, "_current_vote_poll_id", _fake_poll_id):
            asyncio.run(bot_listener.handle_vote_command(
                self.api, None, cfg, None, message, CHAT, "testbot", set(),
                log=lambda *_: None,
            ))
        return voting.load_poll(CHAT, poll_id)

    def test_a_new_week_starts_empty_instead_of_inheriting_last_weeks_works(self):
        poll = self._collect()

        self.assertEqual(poll.entries, [])
        self.assertEqual(poll.approved, [])
        self.assertIn("не нашлось", " ".join(self.api.sent))

    def test_clearing_then_collecting_does_not_bring_the_previous_week_back(self):
        """The reported bug: очистить leaves the week empty, and собрать refilled it."""
        self._collect(new_entries=[_entry("90", media=["90.jpg"])])
        self.assertEqual(len(voting.load_poll(CHAT, THIS_WEEK).entries), 1)

        voting.delete_poll(CHAT, THIS_WEEK)
        again = self._collect()

        self.assertEqual(again.entries, [])
        last_week = voting.load_poll(CHAT, LAST_WEEK)
        self.assertEqual(len(last_week.entries), 5)  # untouched, still its own week

    def test_only_this_weeks_nominations_land_in_the_poll_and_stay_pending(self):
        poll = self._collect(new_entries=[_entry("90"), _entry("91")])

        self.assertEqual([e.entry_id for e in poll.entries], ["90", "91"])
        self.assertEqual(poll.approved, [])  # every work still needs a human

    def test_the_scan_is_told_only_about_works_already_in_this_weeks_poll(self):
        self._collect(new_entries=[_entry("90")])
        self._collect()

        self.assertEqual(self.collect_kwargs["skip_entry_ids"], {"90"})

    def test_a_second_collect_does_not_resurrect_an_un_admitted_work(self):
        """A moderator drops a work, then collects again. It must stay dropped."""
        self._collect(new_entries=[_entry("90"), _entry("91")])
        poll = voting.load_poll(CHAT, THIS_WEEK)
        voting.set_approved(poll, ["90"])
        voting.save_poll(poll)

        again = self._collect()

        self.assertEqual(again.approved, ["90"])
        self.assertEqual([e.entry_id for e in again.entries], ["90", "91"])

    def test_the_default_collect_still_asks_for_the_week_in_progress(self):
        self._collect()

        self.assertEqual(self.collect_kwargs["weeks_ago"], 0)

    def test_the_previous_week_button_collects_into_the_previous_weeks_poll(self):
        """It is Monday: the works people have to vote on are all in the week just ended,
        and "this week" is a few hours old and empty."""
        poll = self._collect(
            new_entries=[_entry("90", media=["90.jpg"])],
            text="/vote собрать прошлая", poll_id=LAST_WEEK,
        )

        self.assertEqual(self.collect_kwargs["weeks_ago"], 1)
        self.assertIn("90", [e.entry_id for e in poll.entries])
        # ...merged into that week rather than replacing it: what it already held is still
        # there, still admitted, and its votes are still counted.
        self.assertEqual(len(poll.entries), 6)
        self.assertEqual(sorted(poll.approved), ["0", "1", "2", "3", "4"])
        self.assertEqual(len(poll.votes), 10)
        self.assertIsNone(voting.load_poll(CHAT, THIS_WEEK))  # untouched, not created

    def test_collecting_the_previous_week_makes_it_the_poll_the_page_opens(self):
        """The reported bug, in full: the week just collected is the week the moderator is
        working on, so it has to be the one the ballot and the moderation screen open --
        even when the week in progress has works of its own and is therefore the newer
        poll. Otherwise what was just collected is invisible."""
        self._collect(new_entries=[_entry("80", media=["80.jpg"])])
        self.assertEqual(voting.latest_poll(CHAT).poll_id, THIS_WEEK)

        self._collect(
            new_entries=[_entry("90", media=["90.jpg"])],
            text="/vote собрать прошлая", poll_id=LAST_WEEK,
        )

        self.assertEqual(voting.latest_poll(CHAT).poll_id, LAST_WEEK)

    def test_the_empty_week_just_begun_does_not_hide_the_week_being_voted_in(self):
        """Monday, the other way round: the previous week is collected and open, and then
        somebody presses "за эту неделю". Nothing has been posted yet, and the empty poll
        that writes must not become what the ballot opens."""
        self._collect(
            new_entries=[_entry("90", media=["90.jpg"])],
            text="/vote собрать прошлая", poll_id=LAST_WEEK,
        )
        self.assertEqual(voting.latest_poll(CHAT).poll_id, LAST_WEEK)

        self._collect()

        self.assertEqual(voting.latest_poll(CHAT).poll_id, LAST_WEEK)

    def test_the_previous_week_scan_is_told_what_that_week_already_holds(self):
        self._collect(text="/vote собрать прошлая", poll_id=LAST_WEEK)

        self.assertEqual(self.collect_kwargs["skip_entry_ids"], {"0", "1", "2", "3", "4"})

    def test_votes_already_cast_this_week_survive_a_second_collect(self):
        self._collect(new_entries=[_entry("90")])
        poll = voting.load_poll(CHAT, THIS_WEEK)
        # A collected work is pending until a human admits it, and record_vote drops
        # choices outside the admitted set -- so admitting is part of casting the vote.
        voting.set_approved(poll, ["90"])
        voting.record_vote(poll, 42, ["90"])
        voting.save_poll(poll)

        again = self._collect()

        self.assertEqual(again.votes, {"42": ["90"]})


class ConcurrentCollectTests(unittest.TestCase):
    """A slow collection must not be turned into two slow collections by an impatient tap."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("voting._voting_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)
        bot_listener._VOTE_COLLECTIONS_IN_PROGRESS.clear()
        self.addCleanup(bot_listener._VOTE_COLLECTIONS_IN_PROGRESS.clear)

    def test_a_second_collect_is_refused_while_the_first_is_still_running(self):
        started = asyncio.Event()
        release = asyncio.Event()
        scans = []
        api = CollectWindowTests.FakeApi()

        async def collect_entries(**kwargs):
            scans.append(kwargs)
            started.set()
            await release.wait()
            return []

        async def resolve(*args, **kwargs):
            return -100

        async def can_manage(*args, **kwargs):
            return True

        message = {
            "message_id": 1,
            "chat": {"id": 5, "type": "private"},
            "from": {"id": 7, "username": "admin"},
            "text": "/vote собрать",
        }
        cfg = SimpleNamespace(
            webapp_public_url="https://example.com",
            vote_miniapp_short_name=None,
            vote_announce_extra_chat=None,
        )

        async def scenario():
            with patch.object(bot_listener, "_resolve_chat_id", resolve), \
                 patch.object(bot_listener, "_can_manage_chat", can_manage), \
                 patch.object(voting, "collect_entries", collect_entries), \
                 patch.object(bot_listener, "_current_vote_poll_id", _fake_poll_id):
                first = asyncio.create_task(bot_listener.handle_vote_command(
                    api, None, cfg, None, message, CHAT, "testbot", set(), log=lambda *_: None,
                ))
                await started.wait()
                # The impatient second tap, while the first is mid-scan.
                await bot_listener.handle_vote_command(
                    api, None, cfg, None, message, CHAT, "testbot", set(), log=lambda *_: None,
                )
                release.set()
                await first

        asyncio.run(scenario())

        self.assertEqual(len(scans), 1, "the second tap started a second full scan")
        self.assertTrue(any("Уже собираю" in text for text in api.sent))

    def test_the_lock_is_released_even_when_the_scan_blows_up(self):
        api = CollectWindowTests.FakeApi()

        async def exploding(**kwargs):
            raise RuntimeError("telegram said no")

        async def resolve(*args, **kwargs):
            return -100

        async def can_manage(*args, **kwargs):
            return True

        message = {
            "message_id": 1,
            "chat": {"id": 5, "type": "private"},
            "from": {"id": 7, "username": "admin"},
            "text": "/vote собрать",
        }
        cfg = SimpleNamespace(
            webapp_public_url="https://example.com",
            vote_miniapp_short_name=None,
            vote_announce_extra_chat=None,
        )
        with patch.object(bot_listener, "_resolve_chat_id", resolve), \
             patch.object(bot_listener, "_can_manage_chat", can_manage), \
             patch.object(voting, "collect_entries", exploding), \
             patch.object(bot_listener, "_current_vote_poll_id", _fake_poll_id):
            asyncio.run(bot_listener.handle_vote_command(
                api, None, cfg, None, message, CHAT, "testbot", set(), log=lambda *_: None,
            ))

        # A failed collect that left the lock behind would wedge the command forever.
        self.assertEqual(bot_listener._VOTE_COLLECTIONS_IN_PROGRESS, set())
        self.assertTrue(any("Не получилось" in text for text in api.sent))


class ClearingTests(unittest.TestCase):
    """"Очистить" empties the contest without destroying what it recorded."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("voting._voting_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

        for poll_id in (LAST_WEEK, THIS_WEEK):
            poll = voting.Poll(
                poll_id=poll_id, entry=CHAT, created_at=f"2026-07-2{poll_id[-1]}",
                entries=[_entry("1")],
            )
            voting.set_approved(poll, ["1"])
            voting.save_poll(poll)
            media = voting.media_path(CHAT, poll_id)
            media.mkdir(parents=True)
            (media / "a.jpg").write_bytes(b"jpeg-ish")
            results = voting.results_path(CHAT, poll_id)
            results.parent.mkdir(parents=True, exist_ok=True)
            results.write_text('{"announced": true}', encoding="utf-8")
            export = voting.export_image_path(CHAT, poll_id)
            export.parent.mkdir(parents=True, exist_ok=True)
            export.write_bytes(b"rendered-board")

    def test_one_clear_empties_every_week_not_just_the_newest(self):
        """The reported bug: clearing removed one week and left the one before it live."""
        cleared = voting.archive_all_polls(CHAT)

        self.assertEqual(cleared, 2)
        self.assertEqual(voting.poll_ids(CHAT), [])
        self.assertIsNone(voting.latest_poll(CHAT))

    def test_the_announced_results_and_rendered_boards_survive(self):
        voting.archive_all_polls(CHAT)

        for poll_id in (LAST_WEEK, THIS_WEEK):
            with self.subTest(poll_id=poll_id):
                self.assertTrue(voting.results_path(CHAT, poll_id).exists())
                self.assertTrue(voting.export_image_path(CHAT, poll_id).exists())

    def test_the_polls_are_archived_rather_than_destroyed(self):
        voting.archive_all_polls(CHAT)

        archived = sorted(path.name for path in voting.archive_dir().glob("*.json"))
        self.assertEqual(len(archived), 2)
        # ...and the archive is invisible to everything that reads the live contest.
        self.assertEqual(voting.poll_ids(CHAT), [])

    def test_the_collected_photos_are_the_one_thing_actually_deleted(self):
        voting.archive_all_polls(CHAT)

        for poll_id in (LAST_WEEK, THIS_WEEK):
            with self.subTest(poll_id=poll_id):
                self.assertFalse(voting.media_path(CHAT, poll_id).exists())

    def test_clearing_twice_keeps_both_records_instead_of_overwriting(self):
        voting.archive_all_polls(CHAT)
        poll = voting.Poll(poll_id=THIS_WEEK, entry=CHAT, created_at="2026-08-01",
                           entries=[_entry("2")])
        voting.save_poll(poll)

        voting.archive_all_polls(CHAT)

        archived = list(voting.archive_dir().glob("*.json"))
        self.assertEqual(len(archived), 3)

    def test_clearing_an_empty_contest_is_zero_rather_than_an_error(self):
        voting.archive_all_polls(CHAT)
        self.assertEqual(voting.archive_all_polls(CHAT), 0)


class ArenaClearingTests(unittest.TestCase):
    """The arena keeps no separate results file, so its record IS the tournament."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("arena._arena_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

        for tournament_id in (LAST_WEEK, THIS_WEEK):
            tournament = arena.Tournament(
                tournament_id=tournament_id, entry=CHAT,
                created_at=f"2026-07-2{tournament_id[-1]}", entries=[_entry("1")],
            )
            arena.save_tournament(tournament)
            media = arena.media_path(CHAT, tournament_id)
            media.mkdir(parents=True)
            (media / "a.jpg").write_bytes(b"jpeg-ish")

    def test_one_clear_empties_every_tournament(self):
        cleared = arena.archive_all_tournaments(CHAT)

        self.assertEqual(cleared, 2)
        self.assertEqual(arena.tournament_ids(CHAT), [])
        self.assertIsNone(arena.latest_tournament(CHAT))

    def test_the_tournaments_are_archived_because_they_hold_their_own_statistics(self):
        arena.archive_all_tournaments(CHAT)

        archived = list(arena.archive_dir().glob("*.json"))
        self.assertEqual(len(archived), 2)
        self.assertIsNone(arena.latest_tournament(CHAT))


class CarryOverIsGoneTests(unittest.TestCase):
    """The removal is the feature, so it gets a test that notices it creeping back."""

    def test_voting_exposes_no_carry_over_machinery(self):
        for name in ("carry_over_entries", "seed_poll_from_previous",
                     "CARRY_OVER_SKIP_TOP", "previous_poll", "copy_entry_media"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(voting, name))

    def test_the_vote_menu_offers_no_carry_over_action(self):
        self.assertNotIn("carryover", bot_listener.VOTE_ACTIONS)


if __name__ == "__main__":
    unittest.main()
