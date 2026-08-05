"""The arena (v2): pairing, ranking, session rules, and -- the point of the whole exercise
-- that none of it can touch v1.

The session rules come from import/CLAUDE.md's "rules that must not regress"; the port of
its pairing and ranking is arena_core.py. Each rule has a test here because each one is a
thing somebody could "simplify" away.
"""

import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import arena
import arena_core
import voting
from arena_core import TIE

CHAT = "Chat"
TID = "2026-W32"


def _entry(entry_id, media=("a.jpg",)):
    return voting.Entry(
        entry_id=str(entry_id), message_id=int(entry_id), author_id=int(entry_id),
        author_name=f"Автор {entry_id}", author_username=f"user{entry_id}", text="",
        media=list(media),
    )


class PairingTests(unittest.TestCase):
    def _exposure(self, works, wanted, seed):
        ids = [str(i) for i in range(works)]
        pairs = arena_core.build_pairs_random(ids, wanted, rng=random.Random(seed))
        self.assertEqual(len(pairs), wanted, "asked for a number of pairs and got fewer")
        seen = {i: 0 for i in ids}
        for a, b in pairs:
            seen[a] += 1
            seen[b] += 1
        return max(seen.values()) - min(seen.values())

    def test_a_realistic_field_is_dealt_perfectly_evenly(self):
        """20 works, 10 pairs -- CLAUDE.md's own sizing example, which is half a round:
        every work is shown at most once and the spread is zero."""
        for seed in range(30):
            self.assertEqual(self._exposure(20, 10, seed), 0)

    def test_exposure_stays_tight_even_when_rounds_do_not_divide_evenly(self):
        """8 works over 8 pairs is two full rounds, and a repeat matchup in the second one
        makes the deal spill into a third -- so exposure is "similar", not identical (which
        is exactly what the reference implementation promises). What must not happen is one
        work being shown far more than another: an under-exposed work gets a misleadingly
        wide margin, and an over-exposed one crowds the ballot."""
        for seed in range(50):
            self.assertLessEqual(self._exposure(8, 8, seed), 2)

    def test_a_voter_never_sees_the_same_matchup_twice(self):
        pairs = arena_core.build_pairs_random([str(i) for i in range(5)], 10, rng=random.Random(3))
        keys = [tuple(sorted(pair)) for pair in pairs]
        self.assertEqual(len(keys), len(set(keys)))

    def test_a_pair_is_never_a_work_against_itself(self):
        for seed in range(20):
            for a, b in arena_core.build_pairs_random(["a", "b", "c"], 6, rng=random.Random(seed)):
                self.assertNotEqual(a, b)

    def test_adaptive_falls_back_to_random_during_the_warm_up(self):
        """Below the warm-up the ratings are mostly prior, and pairing on noise is worse
        than not pairing at all."""
        ids = [str(i) for i in range(6)]
        standings = {"rows": [{"entry_id": i, "rating": 1500, "margin": 400} for i in ids]}
        pairs = arena_core.build_pairs_adaptive(
            ids, 6, standings=standings, ballots_so_far=2, rng=random.Random(5)
        )
        self.assertEqual(len(pairs), 6)

    def test_adaptive_pairs_works_of_similar_rating_once_it_is_warm(self):
        ids = [str(i) for i in range(10)]
        rows = [{"entry_id": i, "rating": 1200 + 80 * int(i), "margin": 60} for i in ids]
        adaptive = arena_core.build_pairs_adaptive(
            ids, 8, standings={"rows": rows}, ballots_so_far=50, rng=random.Random(7)
        )
        rating = {row["entry_id"]: row["rating"] for row in rows}
        gap = sum(abs(rating[a] - rating[b]) for a, b in adaptive) / len(adaptive)
        random_pairs = arena_core.build_pairs_random(ids, 8, rng=random.Random(7))
        random_gap = sum(abs(rating[a] - rating[b]) for a, b in random_pairs) / len(random_pairs)
        self.assertLess(gap, random_gap)


class RankingTests(unittest.TestCase):
    def _ballots(self, pairs_and_picks):
        return [arena.Ballot(user_id=str(i), pairs=[list(p) for p, _ in group],
                             picks=[pick for _, pick in group])
                for i, group in enumerate(pairs_and_picks)]

    def test_the_winner_of_every_duel_ranks_first(self):
        entries = [_entry(i) for i in range(3)]
        ballots = self._ballots([[
            (("0", "1"), "0"), (("0", "2"), "0"), (("1", "2"), "1"),
        ]] * 5)
        rows = arena_core.compute_standings(entries, ballots)["rows"]
        self.assertEqual([row["entry_id"] for row in rows], ["0", "1", "2"])

    def test_ranking_is_order_independent(self):
        """THE rule: refitted from the whole table every time. Incremental Elo would make
        the result depend on the order votes arrived in."""
        entries = [_entry(i) for i in range(4)]
        random.seed(11)
        ballots = self._ballots([
            [((a, b), a if random.random() < 0.7 else b)
             for a, b in arena_core.build_pairs_random(["0", "1", "2", "3"], 4, rng=random.Random(v))]
            for v in range(12)
        ])
        first = arena_core.compute_standings(entries, ballots)
        shuffled = list(reversed(ballots))
        second = arena_core.compute_standings(entries, shuffled)
        for a, b in zip(first["rows"], second["rows"]):
            self.assertEqual(a["entry_id"], b["entry_id"])
            self.assertAlmostEqual(a["rating"], b["rating"], places=9)

    def test_a_tie_is_half_a_point_each(self):
        entries = [_entry(0), _entry(1)]
        ballots = self._ballots([[(("0", "1"), TIE)] for _ in range(4)])
        rows = arena_core.compute_standings(entries, ballots)["rows"]
        self.assertAlmostEqual(rows[0]["rating"], rows[1]["rating"], places=6)
        self.assertEqual(rows[0]["score"], 2.0)

    def test_a_pick_for_a_removed_work_is_skipped_not_fatal(self):
        """A work can be un-admitted after people have voted on it; the other nine
        judgements on that ballot must still count."""
        entries = [_entry(0), _entry(1)]
        ballots = self._ballots([[(("0", "9"), "0"), (("0", "1"), "0")]])
        result = arena_core.compute_standings(entries, ballots)
        self.assertEqual(result["judgements"], 1)
        self.assertEqual(result["rows"][0]["entry_id"], "0")

    def test_an_unplayed_work_still_gets_a_finite_rating(self):
        entries = [_entry(0), _entry(1), _entry(2)]
        ballots = self._ballots([[(("0", "1"), "0")]])
        rows = {row["entry_id"]: row for row in arena_core.compute_standings(entries, ballots)["rows"]}
        self.assertTrue(all(abs(row["rating"]) < 1e6 for row in rows.values()))
        self.assertEqual(rows["2"]["played"], 0)
        self.assertIsNone(rows["2"]["win_rate"])

    def test_recovering_a_planted_order_from_simulated_voters(self):
        """The sizing claim from CLAUDE.md, checked rather than believed: enough judgements
        and the fit finds the hidden preference order."""
        entries = [_entry(i) for i in range(6)]
        rng = random.Random(42)
        strength = {str(i): 1200 + 120 * i for i in range(6)}
        ballots = []
        for voter in range(60):
            pairs = arena_core.build_pairs_random([str(i) for i in range(6)], 10, rng=rng)
            picks = [
                a if rng.random() < arena_core.win_probability(strength[a], strength[b]) else b
                for a, b in pairs
            ]
            ballots.append(arena.Ballot(user_id=str(voter), pairs=[list(p) for p in pairs], picks=picks))
        rows = arena_core.compute_standings(entries, ballots)["rows"]
        self.assertEqual(rows[0]["entry_id"], "5")
        self.assertEqual(rows[-1]["entry_id"], "0")

    def test_two_works_with_overlapping_margins_are_not_separated(self):
        a = {"rating": 1520, "margin": 60}
        b = {"rating": 1500, "margin": 60}
        self.assertFalse(arena_core.is_separated(a, b))
        self.assertTrue(arena_core.is_separated({"rating": 1900, "margin": 50}, b))


class SessionRuleTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("arena._arena_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)
        arena._standings_cache.clear()

    def _tournament(self, works=6, pairs=3):
        entries = [_entry(i) for i in range(works)]
        tournament = arena.Tournament(
            tournament_id=TID, entry=CHAT, created_at="2026-08-01",
            entries=entries, pairs_per_voter=pairs,
        )
        arena.set_approved(tournament, [e.entry_id for e in entries])
        return tournament

    def test_one_voter_one_ballot_resumed_not_restarted(self):
        tournament = self._tournament()
        first = arena.start_session(tournament, 7, "Аня")
        arena.record_pick(tournament, 7, 0, first.pairs[0][0])
        again = arena.start_session(tournament, 7, "Аня")
        self.assertEqual(again.pairs, first.pairs)
        self.assertEqual(len(again.picks), 1)
        self.assertEqual(len(tournament.ballots), 1)

    def test_a_finished_ballot_never_reopens(self):
        tournament = self._tournament(pairs=2)
        ballot = arena.start_session(tournament, 7)
        arena.record_pick(tournament, 7, 0, ballot.pairs[0][0])
        arena.record_pick(tournament, 7, 1, TIE)
        self.assertEqual(ballot.status, "done")
        with self.assertRaises(arena.ArenaError) as caught:
            arena.start_session(tournament, 7)
        self.assertEqual(caught.exception.code, "ALREADY_VOTED")

    def test_a_stale_or_repeated_submit_is_not_counted_twice(self):
        tournament = self._tournament()
        ballot = arena.start_session(tournament, 7)
        arena.record_pick(tournament, 7, 0, ballot.pairs[0][0])
        arena.record_pick(tournament, 7, 0, ballot.pairs[0][1])  # the same tap again
        self.assertEqual(len(ballot.picks), 1)

    def test_going_back_returns_to_the_pair_before(self):
        tournament = self._tournament()
        ballot = arena.start_session(tournament, 7)
        arena.record_pick(tournament, 7, 0, ballot.pairs[0][0])
        arena.record_pick(tournament, 7, 1, TIE)
        arena.undo_pick(tournament, 7)
        self.assertEqual(ballot.position, 1)
        self.assertEqual(ballot.picks, [ballot.pairs[0][0]])
        # and the pair itself is the one that was dealt, not a fresh one
        self.assertEqual(len(ballot.pairs), 3)

    def test_going_back_repeats_all_the_way_to_the_first_pair(self):
        tournament = self._tournament()
        ballot = arena.start_session(tournament, 7)
        for at in range(2):
            arena.record_pick(tournament, 7, at, ballot.pairs[at][0])
        arena.undo_pick(tournament, 7)
        arena.undo_pick(tournament, 7)
        self.assertEqual(ballot.picks, [])
        with self.assertRaises(arena.ArenaError) as caught:
            arena.undo_pick(tournament, 7)
        self.assertEqual(caught.exception.code, "NOTHING_TO_UNDO")

    def test_a_re_answered_pair_replaces_the_pick_it_took_back(self):
        tournament = self._tournament()
        ballot = arena.start_session(tournament, 7)
        arena.record_pick(tournament, 7, 0, ballot.pairs[0][0])
        arena.undo_pick(tournament, 7)
        arena.record_pick(tournament, 7, 0, ballot.pairs[0][1])
        self.assertEqual(ballot.picks, [ballot.pairs[0][1]])

    def test_going_back_cannot_reopen_a_finished_ballot(self):
        tournament = self._tournament(pairs=2)
        ballot = arena.start_session(tournament, 7)
        arena.record_pick(tournament, 7, 0, ballot.pairs[0][0])
        arena.record_pick(tournament, 7, 1, TIE)
        with self.assertRaises(arena.ArenaError) as caught:
            arena.undo_pick(tournament, 7)
        self.assertEqual(caught.exception.code, "BALLOT_COMPLETE")
        self.assertEqual(ballot.status, "done")
        self.assertEqual(len(ballot.picks), 2)

    def test_a_pick_outside_the_pair_is_refused(self):
        tournament = self._tournament()
        arena.start_session(tournament, 7)
        with self.assertRaises(arena.ArenaError) as caught:
            arena.record_pick(tournament, 7, 0, "не-из-этой-пары")
        self.assertEqual(caught.exception.code, "BAD_PICK")

    def test_a_closed_arena_starts_no_new_ballots(self):
        tournament = self._tournament()
        tournament.open = False
        with self.assertRaises(arena.ArenaError) as caught:
            arena.start_session(tournament, 7)
        self.assertEqual(caught.exception.code, "VOTING_CLOSED")

    def test_fewer_than_two_works_is_refused_rather_than_dealt_an_empty_ballot(self):
        tournament = self._tournament(works=1)
        with self.assertRaises(arena.ArenaError) as caught:
            arena.start_session(tournament, 7)
        self.assertEqual(caught.exception.code, "NOT_ENOUGH_WORKS")

    def test_only_admitted_works_are_ever_dealt(self):
        tournament = self._tournament(works=6)
        arena.set_approved(tournament, ["0", "1", "2"])
        ballot = arena.start_session(tournament, 7)
        for pair in ballot.pairs:
            self.assertTrue(set(pair) <= {"0", "1", "2"})

    def test_a_tournament_round_trips_through_disk(self):
        tournament = self._tournament()
        ballot = arena.start_session(tournament, 7, "Аня")
        arena.record_pick(tournament, 7, 0, ballot.pairs[0][0])
        arena.save_tournament(tournament)

        loaded = arena.load_tournament(CHAT, TID)
        self.assertEqual(loaded.approved, tournament.approved)
        self.assertEqual(loaded.ballots["7"].pairs, ballot.pairs)
        self.assertEqual(loaded.ballots["7"].picks, ballot.picks)
        self.assertEqual(loaded.ballots["7"].name, "Аня")
        self.assertEqual(loaded.pairs_per_voter, tournament.pairs_per_voter)

    def test_re_collecting_keeps_ballots_and_admitting(self):
        tournament = self._tournament()
        arena.start_session(tournament, 7)
        arena.save_tournament(tournament)

        rebuilt = arena.build_tournament(
            CHAT, TID, tournament.entries + [_entry(99)], existing=tournament
        )
        self.assertEqual(rebuilt.approved, tournament.approved)   # 99 is not admitted
        self.assertIn("7", rebuilt.ballots)
        self.assertEqual(len(rebuilt.entries), 7)

    def test_standings_ignore_votes_for_a_work_that_was_un_admitted(self):
        tournament = self._tournament(works=3, pairs=1)
        ballot = arena.start_session(tournament, 7)
        arena.record_pick(tournament, 7, 0, ballot.pairs[0][0])
        arena.set_approved(tournament, ["0"])
        rows = tournament.standings()["rows"]
        self.assertEqual([row["entry_id"] for row in rows], ["0"])


class IsolationFromV1Tests(unittest.TestCase):
    """The arena is a second system, not a change to the first one. These are the tests
    that would fail if the two ever started sharing state."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        for target, name in (("arena._arena_dir", "arena"), ("voting._voting_dir", "voting")):
            patcher = patch(target, return_value=root / name)
            patcher.start()
            self.addCleanup(patcher.stop)
        arena._standings_cache.clear()

    def _v1_poll(self):
        entries = [_entry(i, media=[f"{i}.jpg"]) for i in range(4)]
        poll = voting.Poll(poll_id=TID, entry=CHAT, created_at="2026-08-01", entries=entries)
        voting.set_approved(poll, ["0", "1", "2"])   # "3" was rejected in v1
        voting.record_vote(poll, 500, ["0"])
        voting.save_poll(poll)
        media = voting.media_path(CHAT, TID)
        media.mkdir(parents=True)
        for i in range(4):
            (media / f"{i}.jpg").write_bytes(b"jpeg-ish")
        return poll

    def test_importing_from_v1_copies_and_changes_nothing_there(self):
        poll = self._v1_poll()
        before = poll.to_dict()

        tournament = arena.Tournament(tournament_id=TID, entry=CHAT, created_at="2026-08-01")
        added = arena.import_entries_from_poll(tournament, poll)

        self.assertEqual(added, 3)                                  # only what v1 admitted
        self.assertEqual([e.entry_id for e in tournament.entries], ["0", "1", "2"])
        self.assertEqual(tournament.approved, [])                   # v2 moderates for itself
        self.assertEqual(voting.load_poll(CHAT, TID).to_dict(), before)

    def test_imported_photos_are_copied_into_the_arenas_own_directory(self):
        poll = self._v1_poll()
        tournament = arena.Tournament(tournament_id=TID, entry=CHAT, created_at="2026-08-01")
        arena.import_entries_from_poll(tournament, poll)

        arena_media = arena.media_path(CHAT, TID)
        self.assertTrue((arena_media / "0.jpg").is_file())
        self.assertNotEqual(arena_media, voting.media_path(CHAT, TID))

    def test_importing_twice_adds_nothing_the_second_time(self):
        poll = self._v1_poll()
        tournament = arena.Tournament(tournament_id=TID, entry=CHAT, created_at="2026-08-01")
        arena.import_entries_from_poll(tournament, poll)
        self.assertEqual(arena.import_entries_from_poll(tournament, poll), 0)
        self.assertEqual(len(tournament.entries), 3)

    def test_clearing_the_arena_leaves_v1_untouched(self):
        poll = self._v1_poll()
        tournament = arena.Tournament(tournament_id=TID, entry=CHAT, created_at="2026-08-01")
        arena.import_entries_from_poll(tournament, poll)
        arena.save_tournament(tournament)

        arena.delete_tournament(CHAT, TID)

        self.assertIsNone(arena.load_tournament(CHAT, TID))
        survivor = voting.load_poll(CHAT, TID)
        self.assertIsNotNone(survivor)
        self.assertEqual(survivor.approved, ["0", "1", "2"])
        self.assertEqual(survivor.votes, {"500": ["0"]})
        self.assertTrue((voting.media_path(CHAT, TID) / "0.jpg").is_file())

    def test_clearing_v1_leaves_the_arena_untouched(self):
        poll = self._v1_poll()
        tournament = arena.Tournament(tournament_id=TID, entry=CHAT, created_at="2026-08-01")
        arena.import_entries_from_poll(tournament, poll)
        arena.set_approved(tournament, ["0", "1", "2"])
        ballot = arena.start_session(tournament, 7)
        arena.record_pick(tournament, 7, 0, ballot.pairs[0][0])
        arena.save_tournament(tournament)

        voting.delete_poll(CHAT, TID)

        survivor = arena.load_tournament(CHAT, TID)
        self.assertIsNotNone(survivor)
        self.assertEqual(len(survivor.entries), 3)
        self.assertEqual(len(survivor.ballots["7"].picks), 1)
        self.assertTrue((arena.media_path(CHAT, TID) / "0.jpg").is_file())

    def test_the_two_systems_keep_their_files_apart(self):
        self._v1_poll()
        tournament = arena.Tournament(tournament_id=TID, entry=CHAT, created_at="2026-08-01")
        arena.save_tournament(tournament)
        self.assertNotEqual(
            arena.tournament_path(CHAT, TID), voting.poll_path(CHAT, TID)
        )
        self.assertFalse(
            str(arena.tournament_path(CHAT, TID)).startswith(str(voting._voting_dir()))
        )


if __name__ == "__main__":
    unittest.main()
