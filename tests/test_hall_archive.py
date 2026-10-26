"""hall_archive/ -- the contests run in the channel before the bot, as the administrators
checked them. These tests hold the data itself to what the channel showed: every poll
option is a work, the votes add up to the poll's voters, the winner is the one the channel
announced, and every photo the record names is in the repository.
"""

import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hall_of_fame
import voting

ARCHIVE = hall_of_fame.ARCHIVE_DIR
CHAT = "Chat"


def _records():
    return hall_of_fame.load_archive(ARCHIVE)


def _winner_row(record):
    rows = record["works"]
    return min(enumerate(rows), key=lambda pair: (-pair[1]["votes"], pair[0]))[1]


class ArchiveDataTests(unittest.TestCase):
    def setUp(self):
        self.records = _records()
        if not self.records:
            self.skipTest("no hall_archive/contests.json")

    def test_every_contest_has_a_unique_safe_id_and_its_kind(self):
        ids = [r["contest_id"] for r in self.records]
        self.assertEqual(len(ids), len(set(ids)))
        for record in self.records:
            with self.subTest(record["contest_id"]):
                self.assertRegex(record["contest_id"], r"^[A-Za-z0-9_.-]+$")
                if record["hashtag"] == hall_of_fame.MONTHLY_HASHTAG:
                    self.assertIn(record["tier"], (1, 2, 3))
                    self.assertRegex(record["contest_id"], r"^\d{4}-\d{2}-(gold|silver|bronze)$")
                else:
                    self.assertEqual(record["hashtag"], voting.CONTEST_HASHTAG)
                    self.assertEqual(record["tier"], 0)
                    self.assertRegex(record["contest_id"], r"^\d{4}-W\d{2}$")

    def test_every_poll_option_is_a_work_and_the_votes_add_up(self):
        for record in self.records:
            sources = record["sources"]
            with self.subTest(record["contest_id"]):
                self.assertEqual(len(record["works"]), sources["poll_options"])
                if not sources["multiple_choice"]:
                    # One choice per voter: every vote is somebody's, nobody's is lost.
                    self.assertEqual(sum(w["votes"] for w in record["works"]), record["voters"])

    def test_the_winner_is_the_one_the_channel_announced(self):
        for record in self.records:
            with self.subTest(record["contest_id"]):
                winner = _winner_row(record)
                announced = record["sources"]["announced_winner"]
                self.assertIn(announced, (winner.get("channel"), winner.get("author_username")))

    def test_every_work_is_somebodys_and_every_named_photo_is_here(self):
        for record in self.records:
            entry_ids = [w["entry_id"] for w in record["works"]]
            self.assertEqual(len(entry_ids), len(set(entry_ids)), record["contest_id"])
            for work in record["works"]:
                with self.subTest(record["contest_id"], work=work["entry_id"]):
                    self.assertTrue(work["author_name"])
                    self.assertTrue(work["author_id"] is None or isinstance(work["author_id"], int))
                    # A bare number is the bot's own kind of entry id -- a chat message id --
                    # and is only ever used for exactly that.
                    if work["entry_id"].isdigit():
                        self.assertEqual(int(work["entry_id"]), work["message_id"])
                    if work["photo"]:
                        path = ARCHIVE / "media" / record["contest_id"] / work["photo"]
                        with Image.open(path) as image:
                            self.assertEqual(image.format, "JPEG")
                    else:
                        # No picture kept: the bot fetches it from the post, so there must be one.
                        self.assertGreater(work["message_id"], 0)

    def test_a_monthly_round_reruns_that_months_places(self):
        """July's rounds are made of the July weeks' works at the round's place -- the
        same people the weekly records name there."""
        weekly = {r["contest_id"]: r for r in self.records if r["tier"] == 0}
        july = [weekly[w] for w in ("2026-W27", "2026-W28", "2026-W29", "2026-W30") if w in weekly]
        if len(july) < 4:
            self.skipTest("July's weeks are not all in the archive")
        for record in self.records:
            if not record["contest_id"].startswith("2026-07-"):
                continue
            place = record["tier"]
            expected = sorted(
                (row["channel"], row["author_id"])
                for week in july
                for row in [sorted(enumerate(week["works"]), key=lambda p: (-p[1]["votes"], p[0]))[place - 1][1]]
            )
            got = sorted((row["channel"], row["author_id"]) for row in record["works"])
            with self.subTest(record["contest_id"]):
                self.assertEqual(got, expected)


class ArchiveImportsTests(unittest.TestCase):
    """The real archive, written into an empty hall."""

    def setUp(self):
        if not _records():
            self.skipTest("no hall_archive/contests.json")
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        self._patchers = [patch("hall_of_fame._hall_dir", return_value=root / "hall"),
                          patch("voting._voting_dir", return_value=root / "voting")]
        for patcher in self._patchers:
            patcher.start()
        hall_of_fame._cache.clear()

    def tearDown(self):
        for patcher in self._patchers:
            patcher.stop()
        hall_of_fame._cache.clear()
        self._temporary.cleanup()

    def test_it_comes_in_once_with_every_work_and_the_announced_winners(self):
        records = _records()
        counts = hall_of_fame.import_archive(CHAT)
        self.assertEqual(counts["added"], len(records))
        self.assertEqual(hall_of_fame.import_archive(CHAT)["known"], len(records))
        hall = hall_of_fame.snapshot(CHAT)
        self.assertEqual(len(hall.contests), len(records))
        for record in records:
            contest = hall.by_id[record["contest_id"]]
            with self.subTest(record["contest_id"]):
                self.assertEqual(len(contest.works), len(record["works"]))
                self.assertEqual(contest.winner().author_id, _winner_row(record)["author_id"])
                self.assertEqual(contest.tier, record["tier"])
                for work in contest.works:
                    self.assertTrue(work.photos or work.message_id)

    def test_a_monthly_rounds_winner_takes_only_that_rounds_medal(self):
        hall_of_fame.import_archive(CHAT)
        hall = hall_of_fame.snapshot(CHAT)
        for contest in hall.contests:
            if not contest.tier:
                continue
            with self.subTest(contest.contest_id):
                medals = [contest.medal_for(w) for w in contest.works]
                self.assertEqual(medals[0], contest.tier)
                self.assertTrue(all(m is None for m in medals[1:]))
                self.assertEqual(contest.label().split(" · ")[-1],
                                 {1: "золото", 2: "серебро", 3: "бронза"}[contest.tier])


if __name__ == "__main__":
    unittest.main()
