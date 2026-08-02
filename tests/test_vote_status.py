"""bot_listener._vote_status_text -- the standings block shown in an administrator's bare
/vote status message. Pure (reads the poll off disk, no Telegram calls), so it's tested
directly rather than through the full dispatch chain."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot_listener
import voting

CHAT = "Chat"


def _entry(entry_id, author_name="Author", username=None):
    return voting.Entry(
        entry_id=entry_id, message_id=1, author_id=1,
        author_name=author_name, author_username=username, text="",
    )


class VoteStatusTextTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("voting._voting_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def test_no_poll_at_all(self):
        self.assertEqual(bot_listener._vote_status_text(CHAT), "Голосование ещё не создано.")

    def test_nothing_admitted_yet(self):
        poll = voting.Poll(poll_id="p", entry=CHAT, created_at="t0", entries=[_entry("a")])
        voting.save_poll(poll)
        text = bot_listener._vote_status_text(CHAT)
        self.assertIn("Проголосовало: 0", text)
        self.assertIn("/vote выбрать", text)

    def test_admitted_but_no_votes_yet(self):
        poll = voting.Poll(poll_id="p", entry=CHAT, created_at="t0", entries=[_entry("a")])
        voting.set_approved(poll, ["a"])
        voting.save_poll(poll)
        self.assertIn("никто не проголосовал", bot_listener._vote_status_text(CHAT))

    def test_shows_the_top_3_with_medals_and_vote_counts(self):
        entries = [_entry(str(i), f"User{i}", f"u{i}") for i in range(3)]
        poll = voting.Poll(poll_id="p", entry=CHAT, created_at="t0", entries=entries)
        voting.set_approved(poll, ["0", "1", "2"])
        voting.record_vote(poll, 1, ["0"])
        voting.record_vote(poll, 2, ["0"])
        voting.record_vote(poll, 3, ["1"])
        voting.save_poll(poll)

        text = bot_listener._vote_status_text(CHAT)
        self.assertIn("Проголосовало: 3", text)
        self.assertIn("открыто", text)
        self.assertIn("🥇 User0 (@u0) — 2 голосов", text)
        self.assertIn("🥈 User1 (@u1) — 1 голосов", text)

    def test_shows_closed_once_the_poll_is_closed(self):
        entries = [_entry("a", "Winner", "winner")]
        poll = voting.Poll(poll_id="p", entry=CHAT, created_at="t0", entries=entries)
        voting.set_approved(poll, ["a"])
        voting.record_vote(poll, 1, ["a"])
        voting.close_and_announce(poll)
        voting.save_poll(poll)

        text = bot_listener._vote_status_text(CHAT)
        self.assertIn("закрыто", text)
        self.assertNotIn("открыто", text)


if __name__ == "__main__":
    unittest.main()
