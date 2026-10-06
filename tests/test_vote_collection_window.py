"""What a week's poll is allowed to contain.

The rule: a poll holds exactly the works the chat scan found in its collection window --
the previous and the current contest week -- and nothing copied out of another poll's
file. An earlier version pre-filled a new poll with last week's runners-up straight from
last week's poll, which made "очистить, then собрать" impossible to express because the
collect immediately put the cleared poll back.

This file replaces the old test_vote_carryover.py. The two rules that survived that
feature -- re-collecting must not undo moderation, and must not lose votes -- are kept
here, because they are properties of collecting, not of the carry-over.
"""

import asyncio
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
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


def _fake_poll_id(tz):
    """Stands in for bot_listener._current_vote_poll_id so the tests don't depend on which
    ISO week they happen to run in."""
    return THIS_WEEK


def _range():
    """The last two weeks, ending today -- a range the date picker would hand over.
    Relative to the day the suite runs, because a collect refuses days not yet come."""
    today = date.today()
    return today - timedelta(days=13), today


def _range_text():
    """What the picker's "Собрать" replays: the command with both dates on the end."""
    since, till = _range()
    return f"/vote собрать {since.isoformat()} {till.isoformat()}"


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

    def _collect(self, new_entries=(), text=None, poll_id=THIS_WEEK):
        text = text or _range_text()

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

    def test_clearing_then_collecting_copies_nothing_out_of_an_older_poll(self):
        """The reported bug: очистить left the week empty, and собрать refilled it from
        the previous poll's file rather than from the chat."""
        self._collect(new_entries=[_entry("90", media=["90.jpg"])])
        self.assertEqual(len(voting.load_poll(CHAT, THIS_WEEK).entries), 1)

        voting.delete_poll(CHAT, THIS_WEEK)
        again = self._collect()

        self.assertEqual(again.entries, [])
        last_week = voting.load_poll(CHAT, LAST_WEEK)
        self.assertEqual(len(last_week.entries), 5)  # untouched, still its own week

    def test_collected_nominations_land_in_this_weeks_poll_and_stay_pending(self):
        poll = self._collect(new_entries=[_entry("90"), _entry("91")])

        self.assertEqual([e.entry_id for e in poll.entries], ["90", "91"])
        self.assertEqual(poll.approved, [])  # every work still needs a human
        since, till = _range()
        self.assertIn(f"с {since:%d.%m} по {till:%d.%m}", " ".join(self.api.sent))

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

    def test_collect_reads_exactly_the_days_it_was_given(self):
        """From the first day's midnight up to the midnight AFTER the last day: "по 5
        октября" includes the 5th."""
        since, till = _range()
        self._collect()

        start, end = self.collect_kwargs["since"], self.collect_kwargs["until"]
        self.assertEqual((start.date(), start.hour, start.minute), (since, 0, 0))
        self.assertEqual((end.date(), end.hour, end.minute), (till + timedelta(days=1), 0, 0))
        self.assertIsNotNone(start.tzinfo)
        self.assertNotIn("weeks", self.collect_kwargs)

    def test_collect_all_reads_the_whole_window_past_works_already_collected(self):
        """Production, 2026-09-27: this week's poll already held two works, and a collect
        that stopped at them never reached last week."""
        self._collect(new_entries=[_entry("90")])

        self._collect()

        self.assertIs(self.collect_kwargs["stop_at_known"], False)
        self.assertEqual(self.collect_kwargs["skip_entry_ids"], {"90"})  # still no re-download
        self.assertIn("все заявки", " ".join(self.api.sent))

    def test_add_new_stops_at_the_newest_work_already_collected(self):
        self._collect(new_entries=[_entry("90")])

        poll = self._collect(new_entries=[_entry("91")], text="/vote добавить")

        self.assertIs(self.collect_kwargs["stop_at_known"], True)
        self.assertEqual(self.collect_kwargs["weeks"], 2)
        self.assertEqual([e.entry_id for e in poll.entries], ["90", "91"])
        self.assertEqual(poll.approved, [])

    def test_the_status_panel_offers_both_collect_buttons(self):
        """Bare /vote from an administrator: the two buttons must carry the two actions."""
        async def resolve(*args, **kwargs):
            return -100

        async def can_manage(*args, **kwargs):
            return True

        markups = []

        async def send_message(chat_id, text, reply_to_message_id=None, reply_markup=None,
                               parse_mode=None, disable_notification=False):
            markups.append(reply_markup)
            return {"message_id": 1}

        self.api.send_message = send_message
        message = {"message_id": 1, "chat": {"id": 5, "type": "private"},
                   "from": {"id": 7, "username": "admin"}, "text": "/vote"}
        cfg = SimpleNamespace(webapp_public_url="https://example.com",
                              vote_miniapp_short_name=None, vote_announce_extra_chat=None)
        with patch.object(bot_listener, "_resolve_chat_id", resolve), \
             patch.object(bot_listener, "_can_manage_chat", can_manage):
            asyncio.run(bot_listener.handle_vote_command(
                self.api, None, cfg, None, message, CHAT, "testbot", set(),
                log=lambda *_: None,
            ))

        buttons = {
            button["text"]: button.get("callback_data", "")
            for row in markups[-1]["inline_keyboard"] for button in row
        }
        self.assertIn(":collect:", buttons["🔄 Собрать все заявки"])
        self.assertIn(":collectnew:", buttons["➕ Добавить новые"])

    def test_a_work_already_in_last_weeks_poll_arrives_pending_and_leaves_that_poll_alone(self):
        """The window reaches into last week, so a work that was already in last week's
        poll can be found again. It comes in pending -- whether it runs a second time is
        the moderator's call -- and last week's poll keeps its admissions and its votes."""
        poll = self._collect(new_entries=[_entry("4", media=["4.jpg"]), _entry("90", media=["90.jpg"])])

        self.assertEqual([e.entry_id for e in poll.entries], ["4", "90"])
        self.assertEqual(poll.approved, [])
        self.assertEqual(poll.votes, {})
        last_week = voting.load_poll(CHAT, LAST_WEEK)
        self.assertEqual(sorted(last_week.approved), ["0", "1", "2", "3", "4"])
        self.assertEqual(len(last_week.votes), 10)

    def test_a_collect_takes_the_page_from_an_older_unmoderated_poll(self):
        """A poll the old "за прошлую неделю" button wrote can still be on disk and newer
        than this week's. The poll just collected is the one being worked on, so it is the
        one the moderation page opens.

        Last week is left unmoderated on purpose. A poll with admitted works is a live
        ballot and outranks both regardless of when it was collected -- that is
        latest_poll's own rule, tested separately, and it would mask the tie-break here.
        """
        finished = voting.load_poll(CHAT, LAST_WEEK)
        voting.set_approved(finished, [])
        finished.votes = {}
        voting.save_poll(finished)
        self._collect(new_entries=[_entry("80", media=["80.jpg"])])
        this_week = voting.load_poll(CHAT, THIS_WEEK)
        this_week.created_at = "2026-07-01"  # older than last week's "2026-07-20"
        voting.save_poll(this_week)
        self.assertEqual(voting.latest_poll(CHAT).poll_id, LAST_WEEK)

        self._collect()

        self.assertEqual(voting.latest_poll(CHAT).poll_id, THIS_WEEK)

    def test_a_collect_mid_vote_does_not_move_the_ballot_off_the_running_week(self):
        """Production, 2026-08-10: the previous week's vote was running -- 15 works
        admitted, 34 ballots cast -- when a collect found one new nomination, and that
        single pending work took the page away from the live vote, which then showed no
        candidates at all."""
        self.assertEqual(voting.latest_poll(CHAT).poll_id, LAST_WEEK)  # admitted, 10 ballots

        self._collect(new_entries=[_entry("80", media=["80.jpg"])])

        self.assertEqual(voting.latest_poll(CHAT).poll_id, LAST_WEEK)
        # ...the work found is still collected, waiting in this week's poll...
        self.assertEqual(len(voting.load_poll(CHAT, THIS_WEEK).entries), 1)
        # ...and the reply says outright why the page still shows the other week.
        self.assertIn("открыта пока другая неделя", " ".join(self.api.sent))

    def test_a_collect_that_finds_nothing_does_not_hide_the_week_being_voted_in(self):
        """The empty poll such a collect writes must not become what the ballot opens."""
        self._collect()

        self.assertEqual(voting.latest_poll(CHAT).poll_id, LAST_WEEK)

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
            "text": _range_text(),
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
            "text": _range_text(),
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

    def test_the_photos_leave_the_page_with_their_poll_and_are_kept(self):
        """They used to be the one thing a clear deleted -- and the one thing nothing could
        bring back."""
        voting.archive_all_polls(CHAT)

        for poll_id in (LAST_WEEK, THIS_WEEK):
            with self.subTest(poll_id=poll_id):
                self.assertFalse(voting.media_path(CHAT, poll_id).exists())
                (archived,) = voting.photo_dirs(CHAT, poll_id)[1:]
                self.assertEqual((archived / "a.jpg").read_bytes(), b"jpeg-ish")

    def test_clearing_the_same_week_twice_keeps_both_sets_of_photos(self):
        voting.archive_all_polls(CHAT)
        media = voting.media_path(CHAT, THIS_WEEK)
        media.mkdir(parents=True)
        (media / "b.jpg").write_bytes(b"second")
        voting.save_poll(voting.Poll(poll_id=THIS_WEEK, entry=CHAT, created_at="2026-08-01",
                                     entries=[_entry("2")]))

        voting.archive_all_polls(CHAT)

        names = [sorted(p.name for p in d.iterdir()) for d in voting.photo_dirs(CHAT, THIS_WEEK)[1:]]
        self.assertEqual(sorted(names), [["a.jpg"], ["b.jpg"]])

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
        # ...and their photos with them, rather than deleted.
        photos = sorted(p.name for p in (arena.archive_dir() / "media").rglob("*.jpg"))
        self.assertEqual(photos, ["a.jpg", "a.jpg"])


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
