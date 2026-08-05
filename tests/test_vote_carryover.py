"""Carrying last week's runners-up into the new week's poll.

The rule: when /vote собрать creates a poll for a week that has none yet, last week's
ADMITTED works minus its top 3 are already in it, already admitted. Everything about that
sentence is load-bearing, so each clause has a test.
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_listener
import voting

CHAT = "Chat"
LAST_WEEK = "2026-W30"
THIS_WEEK = "2026-W31"


def _entry(entry_id, name=None, media=("a.jpg",)):
    return voting.Entry(
        entry_id=entry_id, message_id=int(entry_id), author_id=int(entry_id),
        author_name=name or f"Автор {entry_id}", author_username=f"user{entry_id}",
        text="", media=list(media),
    )


class CarryOverEntriesTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("voting._voting_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def _voted_poll(self, poll_id=LAST_WEEK, count=6, approved=None):
        """A finished poll where entry i has i votes -- so "5" is first and "0" is last."""
        entries = [_entry(str(i)) for i in range(count)]
        poll = voting.Poll(poll_id=poll_id, entry=CHAT, created_at=f"2026-07-2{poll_id[-1]}", entries=entries)
        voting.set_approved(poll, approved if approved is not None else [e.entry_id for e in entries])
        voter = 0
        for index in range(count):
            for _ in range(index):
                voter += 1
                voting.record_vote(poll, voter, [str(index)])
        return poll

    def test_the_top_three_retire_and_everyone_else_runs_again(self):
        carried = voting.carry_over_entries(self._voted_poll())
        # 5, 4 and 3 won their week; 2, 1 and 0 come back, still best-first.
        self.assertEqual([e.entry_id for e in carried], ["2", "1", "0"])

    def test_a_work_nobody_admitted_is_not_carried_over(self):
        """Un-admitting is the only way to drop a post from a poll -- a carry-over that
        ignored it would undo that decision every single week."""
        poll = self._voted_poll(approved=["5", "4", "3", "2"])
        carried = voting.carry_over_entries(poll)
        self.assertEqual([e.entry_id for e in carried], ["2"])

    def test_a_week_nobody_voted_in_retires_nobody(self):
        poll = voting.Poll(
            poll_id=LAST_WEEK, entry=CHAT, created_at="t0",
            entries=[_entry(str(i)) for i in range(5)],
        )
        voting.set_approved(poll, [str(i) for i in range(5)])
        carried = voting.carry_over_entries(poll)
        self.assertEqual(len(carried), 5)

    def test_a_partial_podium_only_retires_the_works_that_scored(self):
        poll = voting.Poll(
            poll_id=LAST_WEEK, entry=CHAT, created_at="t0",
            entries=[_entry(str(i)) for i in range(4)],
        )
        voting.set_approved(poll, ["0", "1", "2", "3"])
        voting.record_vote(poll, 1, ["2"])
        carried = voting.carry_over_entries(poll)
        self.assertNotIn("2", [e.entry_id for e in carried])
        self.assertEqual(len(carried), 3)

    def test_the_skip_count_is_configurable(self):
        carried = voting.carry_over_entries(self._voted_poll(), skip_top=1)
        self.assertEqual([e.entry_id for e in carried], ["4", "3", "2", "1", "0"])


class SeedPollFromPreviousTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("voting._voting_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def _previous(self):
        entries = [_entry(str(i), media=[f"{i}.jpg"]) for i in range(5)]
        poll = voting.Poll(poll_id=LAST_WEEK, entry=CHAT, created_at="2026-07-20", entries=entries)
        voting.set_approved(poll, [e.entry_id for e in entries])
        for index in range(5):
            for voter in range(index):
                voting.record_vote(poll, f"{index}-{voter}", [str(index)])
        poll.max_choices = 2
        poll.allow_revote = False
        voting.set_crops(poll, {"1": {"x": 5, "y": 6, "size": 100}, "4": {"x": 0, "y": 0, "size": 10}})
        poll.open = False
        poll.winner_entry_id = "4"
        # The photos the entries point at have to exist to be copied anywhere.
        media = voting.media_path(CHAT, LAST_WEEK)
        media.mkdir(parents=True)
        for index in range(5):
            (media / f"{index}.jpg").write_bytes(b"jpeg-ish")
        voting.save_poll(poll)
        return poll

    def test_the_new_poll_starts_admitted_open_and_without_last_weeks_votes(self):
        poll = voting.seed_poll_from_previous(CHAT, THIS_WEEK, self._previous())

        self.assertEqual([e.entry_id for e in poll.entries], ["1", "0"])
        self.assertEqual(poll.approved, ["1", "0"])
        self.assertEqual(poll.votes, {})
        self.assertTrue(poll.open)
        self.assertIsNone(poll.winner_entry_id)
        self.assertEqual(poll.poll_id, THIS_WEEK)

    def test_the_ballot_settings_come_along(self):
        poll = voting.seed_poll_from_previous(CHAT, THIS_WEEK, self._previous())
        self.assertEqual(poll.max_choices, 2)
        self.assertFalse(poll.allow_revote)

    def test_framing_survives_for_the_works_that_carried_over(self):
        poll = voting.seed_poll_from_previous(CHAT, THIS_WEEK, self._previous())
        self.assertEqual(poll.crops, {"1": {"x": 5.0, "y": 6.0, "size": 100.0}})  # "4" retired

    def test_the_photos_are_copied_into_the_new_polls_media_directory(self):
        """Both the page and the export address a photo as <poll id>/<name>, so a carried
        entry whose file stayed behind would render as a 404 on every card."""
        poll = voting.seed_poll_from_previous(CHAT, THIS_WEEK, self._previous())
        target = voting.media_path(CHAT, THIS_WEEK)
        for item in poll.entries:
            for name in item.media:
                self.assertTrue((target / name).is_file(), f"{name} was not copied")
        # And the old week keeps its own copy: clearing either must not empty the other.
        self.assertTrue((voting.media_path(CHAT, LAST_WEEK) / "1.jpg").is_file())

    def test_clearing_the_old_week_leaves_the_new_ones_photos_alone(self):
        voting.seed_poll_from_previous(CHAT, THIS_WEEK, self._previous())
        voting.delete_poll(CHAT, LAST_WEEK)
        self.assertTrue((voting.media_path(CHAT, THIS_WEEK) / "1.jpg").is_file())

    def test_previous_poll_finds_last_week_and_not_the_week_being_started(self):
        self._previous()
        this = voting.Poll(poll_id=THIS_WEEK, entry=CHAT, created_at="2026-07-27")
        voting.save_poll(this)
        found = voting.previous_poll(CHAT, THIS_WEEK)
        self.assertIsNotNone(found)
        self.assertEqual(found.poll_id, LAST_WEEK)

    def test_previous_poll_is_none_when_there_has_never_been_one(self):
        self.assertIsNone(voting.previous_poll(CHAT, THIS_WEEK))


class CollectCarriesOverTests(unittest.TestCase):
    """/vote собрать end to end, with the Telegram side faked: the carry-over happens on
    the first collect of a new week and never again."""

    class FakeApi:
        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id, text, reply_to_message_id=None,
                                reply_markup=None, parse_mode=None):
            self.sent.append(text)
            return {"message_id": 1}

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("voting._voting_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)
        self.api = self.FakeApi()

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

    def _collect(self, new_entries=()):
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
            "text": "/vote собрать",
        }
        cfg = SimpleNamespace(
            webapp_public_url="https://example.com",
            vote_miniapp_short_name=None,
            vote_announce_extra_chat=None,
        )
        with patch.object(bot_listener, "_resolve_chat_id", resolve), \
             patch.object(bot_listener, "_can_manage_chat", can_manage), \
             patch.object(voting, "collect_entries", collect_entries), \
             patch.object(bot_listener, "_current_vote_poll_id", lambda tz: THIS_WEEK):
            asyncio.run(bot_listener.handle_vote_command(
                self.api, None, cfg, None, message, CHAT, "testbot", set(),
                log=lambda *_: None,
            ))
        return voting.load_poll(CHAT, THIS_WEEK)

    def test_the_first_collect_of_a_week_brings_last_weeks_runners_up(self):
        poll = self._collect()
        self.assertEqual([e.entry_id for e in poll.entries], ["1", "0"])
        self.assertEqual(poll.approved, ["1", "0"])
        self.assertIn("Перенёс с прошлого голосования: 2", " ".join(self.api.sent))

    def test_carried_works_are_not_re_collected_from_the_chat(self):
        self._collect()
        self.assertEqual(self.collect_kwargs["skip_entry_ids"], {"1", "0"})

    def test_new_nominations_land_alongside_the_carried_ones_but_stay_pending(self):
        poll = self._collect(new_entries=[_entry("90", media=["90.jpg"])])
        self.assertEqual([e.entry_id for e in poll.entries], ["1", "0", "90"])
        self.assertEqual(poll.approved, ["1", "0"])  # the new one still needs a human

    def test_a_second_collect_does_not_resurrect_an_un_admitted_work(self):
        """The moderator drops a carried work, then collects again. It must stay dropped --
        a carry-over that ran on every collect would put it straight back."""
        self._collect()
        poll = voting.load_poll(CHAT, THIS_WEEK)
        voting.set_approved(poll, ["0"])
        voting.save_poll(poll)

        again = self._collect()
        self.assertEqual(again.approved, ["0"])
        self.assertEqual([e.entry_id for e in again.entries], ["1", "0"])
        self.assertNotIn("Перенёс", " ".join(self.api.sent[-2:]))

    def test_votes_already_cast_this_week_survive_a_second_collect(self):
        self._collect()
        poll = voting.load_poll(CHAT, THIS_WEEK)
        voting.record_vote(poll, 42, ["1"])
        voting.save_poll(poll)

        again = self._collect()
        self.assertEqual(again.votes, {"42": ["1"]})


class CarryOverPreviewTests(unittest.TestCase):
    """"/vote перенос" -- the read-only "what would happen" button. The thing it must
    never do is make the thing it describes happen."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("voting._voting_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def _last_week(self, count=5):
        entries = [_entry(str(i)) for i in range(count)]
        poll = voting.Poll(poll_id=LAST_WEEK, entry=CHAT, created_at="2026-07-20", entries=entries)
        voting.set_approved(poll, [e.entry_id for e in entries])
        for index in range(count):
            for voter in range(index):
                voting.record_vote(poll, f"{index}-{voter}", [str(index)])
        voting.save_poll(poll)
        return poll

    def test_it_names_the_podium_that_retires_and_counts_what_returns(self):
        self._last_week()
        text = bot_listener._vote_carryover_preview_text(CHAT, THIS_WEEK)
        self.assertIn(LAST_WEEK, text)
        self.assertIn("Автор 4", text)          # top scorer, retiring
        self.assertIn("Перенесётся работ: 2", text)
        self.assertIn("Автор 1", text)          # a runner-up, returning

    def test_it_says_whether_the_carry_over_would_actually_fire(self):
        self._last_week()
        self.assertIn("ещё не создано", bot_listener._vote_carryover_preview_text(CHAT, THIS_WEEK))

        voting.save_poll(voting.Poll(poll_id=THIS_WEEK, entry=CHAT, created_at="2026-07-27"))
        self.assertIn("уже создано", bot_listener._vote_carryover_preview_text(CHAT, THIS_WEEK))

    def test_it_changes_nothing_on_disk(self):
        previous = self._last_week()
        before = sorted(p.name for p in Path(self._temporary.name).rglob("*"))
        bot_listener._vote_carryover_preview_text(CHAT, THIS_WEEK)
        after = sorted(p.name for p in Path(self._temporary.name).rglob("*"))

        self.assertEqual(before, after, "the preview created or copied something")
        self.assertIsNone(voting.load_poll(CHAT, THIS_WEEK), "the preview created this week's poll")
        reloaded = voting.load_poll(CHAT, LAST_WEEK)
        self.assertEqual(reloaded.to_dict(), previous.to_dict(), "the preview mutated last week")

    def test_with_no_previous_poll_it_says_so_rather_than_erroring(self):
        text = bot_listener._vote_carryover_preview_text(CHAT, THIS_WEEK)
        self.assertIn("Прошлого голосования нет", text)

    def test_a_long_field_is_summarised_rather_than_dumped(self):
        self._last_week(count=30)
        text = bot_listener._vote_carryover_preview_text(CHAT, THIS_WEEK)
        self.assertIn("Перенесётся работ: 27", text)
        self.assertIn("и ещё 12", text)
        self.assertLess(len(text), 4096, "a Telegram message cannot be longer than this")

    def test_the_menu_button_runs_the_preview_and_nothing_else(self):
        """The button hands handle_vote_command a synthetic "/vote перенос"; if that text
        stopped parsing, the button would silently open the plain ballot instead."""
        argument = bot_listener.VOTE_ACTIONS["carryover"][len("/vote"):].strip().lower()
        self.assertIn(argument, bot_listener.VOTE_CARRYOVER_WORDS)


if __name__ == "__main__":
    unittest.main()
