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
