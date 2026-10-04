"""nominations.py: the rules of vote v3, and the promise that it never touches /vote.

Ballots are per nomination and independent; a work taken out of a nomination stops
counting and counts again when it is put back; a refused change is never saved; and the
pool, which mirrors what /vote collected, is synced by reading v1's polls and photos
without changing either.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import nominations
import voting

CHAT = "Chat"


def _entry(entry_id, media=None) -> voting.Entry:
    return voting.Entry(
        entry_id=str(entry_id), message_id=int(entry_id), author_id=int(entry_id),
        author_name=f"Автор {entry_id}", author_username=f"user{entry_id}", text="",
        media=[f"{entry_id}_0.jpg"] if media is None else media,
    )


def _contest(*ids) -> nominations.Contest:
    return nominations.Contest(entry=CHAT, created_at="2026-10-01", entries=[_entry(i) for i in ids])


class _Storage(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        self._patchers = [
            patch("nominations._nominations_dir", return_value=root / "nominations"),
            patch("voting._voting_dir", return_value=root / "voting"),
        ]
        for patcher in self._patchers:
            patcher.start()

    def tearDown(self):
        for patcher in self._patchers:
            patcher.stop()
        self._temporary.cleanup()


class NominationTests(unittest.TestCase):
    def test_a_name_is_trimmed_and_its_whitespace_collapsed(self):
        contest = _contest()
        nomination = nominations.add_nomination(contest, "  Лучшее \n  аниме ")
        self.assertEqual(nomination.name, "Лучшее аниме")
        self.assertEqual(len(nomination.nomination_id), 8)

    def test_an_empty_or_overlong_name_is_refused(self):
        contest = _contest()
        for name in ("", "   ", None, 7, "я" * (nominations.NAME_MAX_LENGTH + 1)):
            with self.subTest(name=name), self.assertRaises(nominations.ContestError):
                nominations.add_nomination(contest, name)
        self.assertEqual(contest.nominations, [])

    def test_two_tabs_cannot_read_the_same(self):
        contest = _contest()
        nominations.add_nomination(contest, "Аниме")
        with self.assertRaises(nominations.ContestError) as caught:
            nominations.add_nomination(contest, "аНиМе")
        self.assertEqual(caught.exception.status, 409)

    def test_renaming_to_its_own_name_is_not_a_duplicate(self):
        contest = _contest()
        anime = nominations.add_nomination(contest, "Аниме")
        nominations.rename_nomination(contest, anime.nomination_id, "АНИМЕ")
        self.assertEqual(anime.name, "АНИМЕ")

    def test_there_is_a_ceiling_on_how_many_tabs(self):
        contest = _contest()
        for index in range(nominations.MAX_NOMINATIONS):
            nominations.add_nomination(contest, f"N{index}")
        with self.assertRaises(nominations.ContestError):
            nominations.add_nomination(contest, "one too many")

    def test_works_are_kept_in_pool_order_and_unknown_ones_dropped(self):
        contest = _contest(3, 2, 1)
        anime = nominations.add_nomination(contest, "Аниме")
        nominations.set_nomination_entries(contest, anime.nomination_id, ["1", "3", "1", "nope"])
        self.assertEqual(anime.entry_ids, ["3", "1"])

    def test_an_unknown_nomination_is_a_404(self):
        with self.assertRaises(nominations.ContestError) as caught:
            nominations.set_nomination_entries(_contest(1), "missing", ["1"])
        self.assertEqual(caught.exception.status, 404)


class BallotTests(unittest.TestCase):
    def setUp(self):
        self.contest = _contest(1, 2, 3)
        self.anime = nominations.add_nomination(self.contest, "Аниме")
        self.fantasy = nominations.add_nomination(self.contest, "Фэнтези")
        nominations.set_nomination_entries(self.contest, self.anime.nomination_id, ["1", "2"])
        nominations.set_nomination_entries(self.contest, self.fantasy.nomination_id, ["2", "3"])

    def test_a_ballot_in_one_nomination_is_not_a_ballot_in_another(self):
        nominations.record_vote(self.contest, self.anime.nomination_id, 7, ["2"])
        self.assertEqual(self.contest.ballot(self.anime, 7), ["2"])
        self.assertEqual(self.contest.ballot(self.fantasy, 7), [])
        self.assertEqual(self.contest.voter_count(self.fantasy), 0)

    def test_voting_again_replaces_only_that_nominations_ballot(self):
        nominations.record_vote(self.contest, self.anime.nomination_id, 7, ["1"])
        nominations.record_vote(self.contest, self.fantasy.nomination_id, 7, ["3"])
        nominations.record_vote(self.contest, self.anime.nomination_id, 7, ["2"])
        self.assertEqual(self.contest.ballot(self.anime, 7), ["2"])
        self.assertEqual(self.contest.ballot(self.fantasy, 7), ["3"])

    def test_choices_outside_the_nomination_are_dropped(self):
        recorded = nominations.record_vote(self.contest, self.anime.nomination_id, 7, ["3", "1", "1"])
        self.assertEqual(recorded, ["1"])

    def test_going_over_the_cap_is_refused_and_not_trimmed(self):
        self.contest.max_choices = 1
        with self.assertRaises(nominations.ContestError):
            nominations.record_vote(self.contest, self.anime.nomination_id, 7, ["1", "2"])
        self.assertNotIn("7", self.anime.votes)

    def test_a_closed_contest_takes_no_ballots(self):
        self.contest.open = False
        with self.assertRaises(nominations.ContestError) as caught:
            nominations.record_vote(self.contest, self.anime.nomination_id, 7, ["1"])
        self.assertEqual(caught.exception.status, 409)

    def test_an_empty_ballot_withdraws_the_voter(self):
        nominations.record_vote(self.contest, self.anime.nomination_id, 7, ["1"])
        nominations.record_vote(self.contest, self.anime.nomination_id, 7, [])
        self.assertNotIn("7", self.anime.votes)
        self.assertEqual(self.contest.voter_count(self.anime), 0)

    def test_a_work_taken_out_stops_counting_and_counts_again_when_put_back(self):
        nominations.record_vote(self.contest, self.anime.nomination_id, 7, ["2"])
        nominations.set_nomination_entries(self.contest, self.anime.nomination_id, ["1"])
        self.assertEqual(self.contest.ballot(self.anime, 7), [])
        self.assertEqual([(e.entry_id, n) for e, n in self.contest.tally(self.anime)], [("1", 0)])
        nominations.set_nomination_entries(self.contest, self.anime.nomination_id, ["1", "2"])
        self.assertEqual(self.contest.ballot(self.anime, 7), ["2"])

    def test_the_tally_ranks_by_votes_and_breaks_ties_in_nomination_order(self):
        for voter, choice in ((1, "3"), (2, "3"), (3, "2")):
            nominations.record_vote(self.contest, self.fantasy.nomination_id, voter, [choice])
        self.assertEqual(
            [(e.entry_id, n) for e, n in self.contest.tally(self.fantasy)], [("3", 2), ("2", 1)],
        )
        self.assertEqual(
            [(e.entry_id, n) for e, n in self.contest.tally(self.anime)], [("1", 0), ("2", 0)],
        )

    def test_the_results_text_names_every_nomination(self):
        nominations.record_vote(self.contest, self.fantasy.nomination_id, 1, ["3"])
        text = nominations.results_text(self.contest)
        self.assertIn("Аниме (проголосовало: 0)\nголосов пока нет", text)
        self.assertIn("Фэнтези (проголосовало: 1)\n1. Автор 3 (@user3) — 1 голос\n2. Автор 2", text)


class StorageTests(_Storage):
    def test_a_contest_survives_a_round_trip(self):
        contest, anime = nominations.update_contest(
            CHAT, lambda c: nominations.add_nomination(c, "Аниме"), create=True,
        )
        nominations.update_contest(CHAT, lambda c: nominations.add_entries(c, [_entry(1)]))
        nominations.update_contest(
            CHAT, lambda c: nominations.set_nomination_entries(c, anime.nomination_id, ["1"]),
        )
        nominations.update_contest(
            CHAT, lambda c: nominations.record_vote(c, anime.nomination_id, 5, ["1"]),
        )
        loaded = nominations.load_contest(CHAT)
        self.assertEqual([e.entry_id for e in loaded.entries], ["1"])
        self.assertEqual(loaded.nomination(anime.nomination_id).name, "Аниме")
        self.assertEqual(loaded.nomination(anime.nomination_id).entry_ids, ["1"])
        self.assertEqual(loaded.nomination(anime.nomination_id).votes, {"5": ["1"]})

    def test_without_create_a_missing_contest_is_a_404(self):
        with self.assertRaises(nominations.ContestError) as caught:
            nominations.update_contest(CHAT, lambda c: None)
        self.assertEqual(caught.exception.status, 404)
        self.assertFalse(nominations.contest_path(CHAT).exists())

    def test_a_refused_change_is_never_saved(self):
        nominations.update_contest(CHAT, lambda c: nominations.add_nomination(c, "Аниме"), create=True)
        before = nominations.contest_path(CHAT).read_bytes()

        def half_then_refuse(contest):
            contest.nominations.clear()
            raise nominations.ContestError("no")

        with self.assertRaises(nominations.ContestError):
            nominations.update_contest(CHAT, half_then_refuse)
        self.assertEqual(nominations.contest_path(CHAT).read_bytes(), before)

    def test_a_corrupt_file_is_reported_not_overwritten(self):
        path = nominations.contest_path(CHAT)
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(nominations.ContestError) as caught:
            nominations.update_contest(CHAT, lambda c: None, create=True)
        self.assertEqual(caught.exception.status, 500)
        self.assertEqual(path.read_text(encoding="utf-8"), "{not json")

    def test_adding_works_twice_adds_them_once(self):
        nominations.update_contest(CHAT, lambda c: nominations.add_entries(c, [_entry(1)]), create=True)
        contest, added = nominations.update_contest(
            CHAT, lambda c: nominations.add_entries(c, [_entry(2), _entry(1)]),
        )
        self.assertEqual(added, 1)
        self.assertEqual([e.entry_id for e in contest.entries], ["2", "1"])

    def test_v3_lives_outside_the_directory_v1_globs(self):
        nominations.update_contest(CHAT, lambda c: None, create=True)
        self.assertIsNone(voting.latest_poll(CHAT))
        self.assertEqual(voting.poll_ids(CHAT), [])


class SyncFromV1Tests(_Storage):
    """v3 offers the works /vote collected -- one collection for both -- and only reads it."""

    def _seed_poll(self, poll_id="2026-W40", works=None, created_at="2026-10-01T00:00:00+00:00",
                   approved=("1",)):
        works = works or [_entry(1), _entry(2, media=["2_0.jpg", "2_1.jpg"]), _entry(3)]
        poll = voting.Poll(poll_id=poll_id, entry=CHAT, created_at=created_at, entries=works)
        voting.set_approved(poll, list(approved))
        voting.record_vote(poll, 9, list(approved)[:1])
        voting.save_poll(poll)
        media = voting.media_path(CHAT, poll_id)
        media.mkdir(parents=True, exist_ok=True)
        for work in works:
            if work.entry_id == "3":
                continue  # work 3 never got a photo on disk
            for name in work.media:
                (media / name).write_bytes(name.encode())
        return poll, media

    def _snapshot(self):
        root = voting._voting_dir()
        return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    def _pool(self):
        contest = nominations.load_contest(CHAT)
        return [e.entry_id for e in contest.entries] if contest else None

    def test_the_pool_is_what_v1_collected_and_v1_is_left_exactly_as_it_was(self):
        self._seed_poll()
        before = self._snapshot()

        contest, added, admitted = nominations.sync_from_v1(CHAT)

        # Every COLLECTED work with a picture, admitted in v1 or not; work 3 had none.
        self.assertEqual(added, 2)
        self.assertEqual(self._pool(), ["1", "2"])
        self.assertEqual(admitted, {"1"})
        self.assertEqual(sorted(p.name for p in nominations.media_path(CHAT).iterdir()),
                         ["1_0.jpg", "2_0.jpg", "2_1.jpg"])
        self.assertEqual(self._snapshot(), before)

    def test_every_live_week_counts_newest_first(self):
        self._seed_poll("2026-W39", [_entry(5)], created_at="2026-09-24T00:00:00+00:00", approved=("5",))
        self._seed_poll("2026-W40", [_entry(1)], created_at="2026-10-01T00:00:00+00:00")
        nominations.sync_from_v1(CHAT)
        self.assertEqual(self._pool(), ["1", "5"])

    def test_with_nothing_collected_there_is_nothing_to_create(self):
        self.assertEqual(nominations.sync_from_v1(CHAT), (None, 0, set()))
        self.assertFalse(nominations.contest_path(CHAT).exists())

    def test_syncing_twice_writes_once(self):
        self._seed_poll()
        nominations.sync_from_v1(CHAT)
        with patch("nominations.save_contest", side_effect=AssertionError("must not save")):
            contest, added, _ = nominations.sync_from_v1(CHAT)
        self.assertEqual(added, 0)
        self.assertEqual([e.entry_id for e in contest.entries], ["1", "2"])

    def test_a_work_collected_later_joins_and_keeps_the_nominations_intact(self):
        self._seed_poll(works=[_entry(1)])
        nominations.sync_from_v1(CHAT)
        _, anime = nominations.update_contest(CHAT, lambda c: nominations.add_nomination(c, "Аниме"))
        nominations.update_contest(
            CHAT, lambda c: nominations.set_nomination_entries(c, anime.nomination_id, ["1"]))

        self._seed_poll(works=[_entry(7), _entry(1)])  # /vote collected one more
        _, added, _ = nominations.sync_from_v1(CHAT)

        self.assertEqual(added, 1)
        self.assertEqual(self._pool(), ["7", "1"])
        self.assertEqual(nominations.load_contest(CHAT).nomination(anime.nomination_id).entry_ids, ["1"])

    def test_a_work_v1_drops_leaves_the_pool_unless_a_nomination_plays_it(self):
        self._seed_poll(works=[_entry(1), _entry(2)])
        nominations.sync_from_v1(CHAT)
        _, anime = nominations.update_contest(CHAT, lambda c: nominations.add_nomination(c, "Аниме"))
        nominations.update_contest(
            CHAT, lambda c: nominations.set_nomination_entries(c, anime.nomination_id, ["2"]))

        voting.archive_all_polls(CHAT)  # /vote очистить: its photos are deleted too
        nominations.sync_from_v1(CHAT)

        # Work 1 was only raw material and goes; work 2 is a candidate in a running vote
        # and stays, picture and all, because v3 kept its own copy.
        self.assertEqual(self._pool(), ["2"])
        self.assertTrue((nominations.media_path(CHAT) / "2_0.jpg").is_file())

    def test_clearing_v3_deletes_its_own_photos_and_never_v1s(self):
        self._seed_poll()
        nominations.sync_from_v1(CHAT)
        before = self._snapshot()

        self.assertTrue(nominations.archive_contest(CHAT))

        self.assertIsNone(nominations.load_contest(CHAT))
        self.assertFalse(nominations.media_path(CHAT).exists())
        archived = list(nominations.archive_dir().glob("*.json"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(len(json.loads(archived[0].read_text(encoding="utf-8"))["entries"]), 2)
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(voting.latest_poll(CHAT).votes, {"9": ["1"]})

    def test_clearing_nothing_says_so(self):
        self.assertFalse(nominations.archive_contest(CHAT))


if __name__ == "__main__":
    unittest.main()
