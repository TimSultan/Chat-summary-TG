"""hall_of_fame.py -- the Hall of Fame's own record of every closed contest.

The hall must outlive /vote's clears: a contest is copied in, photos and all, when its vote
closes, and what /vote announced before the hall existed comes in through import_history.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hall_of_fame
import voting

CHAT = "Chat"
WEEK = "2026-W41"
ANIME = "#аниме"


def _entry(entry_id, author_id=None, name=None, media=None, posted="2026-10-01T12:00:00+03:00"):
    author_id = int(entry_id) + 100 if author_id is None else author_id
    return voting.Entry(
        entry_id=str(entry_id), message_id=int(entry_id), author_id=author_id,
        author_name=name or f"Автор {entry_id}", author_username=f"user{entry_id}",
        text=f"работа {entry_id}", media=[f"{entry_id}_0.jpg"] if media is None else media,
        posted_at=posted,
    )


class _Store(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        self._patchers = [
            patch("voting._voting_dir", return_value=root / "voting"),
            patch("hall_of_fame._hall_dir", return_value=root / "hall"),
        ]
        for patcher in self._patchers:
            patcher.start()
        hall_of_fame._cache.clear()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for patcher in self._patchers:
            patcher.stop()
        hall_of_fame._cache.clear()
        self._temporary.cleanup()

    def _poll(self, poll_id=WEEK, entries=None, votes=None, hashtag=voting.CONTEST_HASHTAG,
              title="", badge="", colours=None, close=True):
        """A poll with real JPEGs on disk. `votes` maps voter -> entry ids."""
        entries = entries if entries is not None else [_entry(1), _entry(2), _entry(3)]
        poll = voting.Poll(poll_id=poll_id, entry=CHAT, created_at="2026-10-05T08:00:00+00:00",
                           entries=entries, hashtag=hashtag, title=title, badge=badge)
        media = voting.media_path(CHAT, poll_id)
        media.mkdir(parents=True, exist_ok=True)
        for index, entry in enumerate(entries):
            for name in entry.media:
                colour = (colours or {}).get(entry.entry_id, (40 * index, 90, 160))
                Image.new("RGB", (800, 600), colour).save(media / name)
        voting.set_approved(poll, [e.entry_id for e in entries])
        for voter, choices in (votes if votes is not None else {"9": ["2"], "10": ["2", "1"]}).items():
            voting.record_vote(poll, voter, choices)
        if close:
            voting.close_and_announce(poll)
        voting.save_poll(poll)
        return poll


class RecordTests(_Store):
    def test_a_closed_vote_is_recorded_whole_in_place_order(self):
        poll = self._poll()
        contest = hall_of_fame.record_poll(poll, poll.tally())
        self.assertEqual([(w.entry_id, w.place, w.votes) for w in contest.works],
                         [("2", 1, 2), ("1", 2, 1), ("3", 3, 0)])
        self.assertEqual(contest.voters, 2)
        self.assertEqual(contest.winner().entry_id, "2")
        # The nought is part of the field but not of the podium.
        self.assertEqual([w.entry_id for w in contest.podium()], ["2", "1"])
        self.assertEqual(contest.works[0].author_username, "user2")
        self.assertEqual(contest.works[0].posted_at, "2026-10-01T12:00:00+03:00")
        self.assertEqual(hall_of_fame.load_contest(CHAT, WEEK).to_dict(), contest.to_dict())

    def test_the_photos_are_copied_and_survive_the_vote_being_cleared(self):
        poll = self._poll()
        hall_of_fame.record_poll(poll, poll.tally())
        voting.archive_all_polls(CHAT)
        self.assertFalse(voting.media_path(CHAT, WEEK).exists())
        for name in ("1_0.jpg", "2_0.jpg", "3_0.jpg"):
            self.assertIsNotNone(hall_of_fame.media_file(CHAT, WEEK, name))

    def test_each_work_gets_a_square_cover(self):
        poll = self._poll()
        contest = hall_of_fame.record_poll(poll, poll.tally())
        thumb = hall_of_fame.media_file(CHAT, WEEK, contest.works[0].thumb)
        with Image.open(thumb) as image:
            self.assertEqual(image.size, (hall_of_fame.THUMB_SIDE, hall_of_fame.THUMB_SIDE))

    def test_the_cover_is_framed_the_way_the_administrator_framed_the_board(self):
        """A crop over the left half of a photo whose left half is red gives a red cover;
        without the crop the centre (half red, half blue) is what fills the square."""
        poll = self._poll(entries=[_entry(1)], votes={"9": ["1"]})
        path = voting.media_path(CHAT, WEEK) / "1_0.jpg"
        image = Image.new("RGB", (800, 400), (0, 0, 255))
        image.paste((255, 0, 0), (0, 0, 400, 400))
        image.save(path, quality=95)
        poll.crops = {"1": {"x": 0, "y": 0, "size": 400}}
        contest = hall_of_fame.record_poll(poll, poll.tally())
        with Image.open(hall_of_fame.media_file(CHAT, WEEK, contest.works[0].thumb)) as cover:
            red, green, blue = cover.convert("RGB").getpixel((cover.width - 5, cover.height // 2))
        self.assertGreater(red, 200)
        self.assertLess(blue, 60)

    def test_a_vote_nobody_voted_in_has_a_field_and_no_winner(self):
        poll = self._poll(votes={}, close=False)
        contest = hall_of_fame.record_poll(poll, poll.tally())
        self.assertIsNone(contest.winner())
        self.assertEqual(contest.podium(), [])
        self.assertEqual(len(contest.works), 3)

    def test_re_recording_replaces_the_record_and_keeps_the_photos(self):
        poll = self._poll()
        hall_of_fame.record_poll(poll, poll.tally())
        voting.archive_all_polls(CHAT)  # the source photos are gone now
        voting.record_vote(poll, "11", ["3"])
        voting.record_vote(poll, "12", ["3"])
        voting.record_vote(poll, "13", ["3"])
        contest = hall_of_fame.record_poll(poll, poll.tally())
        self.assertEqual(contest.winner().entry_id, "3")
        self.assertTrue(all(work.photos for work in contest.works))
        self.assertEqual(len(list(hall_of_fame.contests_dir(CHAT).glob("*.json"))), 1)

    def test_a_thematic_contest_keeps_its_name_and_badge(self):
        poll = self._poll(poll_id=voting.poll_id_for(WEEK, ANIME), hashtag=ANIME,
                          title="Лучший аниме-покрас", badge="🌸")
        contest = hall_of_fame.record_poll(poll, poll.tally())
        self.assertFalse(contest.is_weekly)
        self.assertEqual(contest.label(), "Лучший аниме-покрас")
        self.assertEqual(contest.winner_badge(), "🌸")
        self.assertEqual(contest.week(), WEEK)
        weekly = hall_of_fame.record_poll(self._poll(), None)
        self.assertEqual((weekly.label(), weekly.winner_badge()), ("Итоги недели", hall_of_fame.WEEKLY_BADGE))

    def test_a_missing_photo_costs_that_work_its_picture_not_the_record(self):
        poll = self._poll()
        (voting.media_path(CHAT, WEEK) / "1_0.jpg").unlink()
        contest = hall_of_fame.record_poll(poll, poll.tally())
        by_id = {w.entry_id: w for w in contest.works}
        self.assertEqual((by_id["1"].photos, by_id["1"].thumb), ([], None))
        self.assertEqual(by_id["2"].photos, ["2_0.jpg"])

    def test_a_picture_name_cannot_reach_outside_the_hall(self):
        poll = self._poll()
        hall_of_fame.record_poll(poll, poll.tally())
        for contest_id, name in ((WEEK, "../../voting/x.json"), ("..", "1_0.jpg"), (WEEK, "nope.jpg")):
            with self.subTest(name=name):
                self.assertIsNone(hall_of_fame.media_file(CHAT, contest_id, name))


class ImportTests(_Store):
    def test_announced_weeks_come_in_with_the_photos_their_clear_archived(self):
        early = self._poll(poll_id="2026-W39")
        voting.save_results(early, early.tally(), "текст")
        voting.archive_all_polls(CHAT)       # cleared: its photos are in the archive now
        late = self._poll(poll_id="2026-W40")
        voting.save_results(late, late.tally(), "текст")

        counts = hall_of_fame.import_history(CHAT)

        self.assertEqual(counts, {"added": 2, "known": 0, "without_photos": 0})
        cleared = hall_of_fame.load_contest(CHAT, "2026-W39")
        self.assertEqual([w.entry_id for w in cleared.works], ["2", "1", "3"])
        self.assertTrue(all(w.photos and w.thumb for w in cleared.works))
        # The posting time comes back from the archived poll file.
        self.assertEqual(cleared.works[0].posted_at, "2026-10-01T12:00:00+03:00")
        live = hall_of_fame.load_contest(CHAT, "2026-W40")
        self.assertTrue(all(w.photos for w in live.works))

    def test_a_week_cleared_before_photos_were_kept_comes_in_without_them(self):
        early = self._poll(poll_id="2026-W39")
        voting.save_results(early, early.tally(), "текст")
        voting.archive_all_polls(CHAT)
        for directory in voting.photo_dirs(CHAT, "2026-W39"):   # what an old clear did
            for photo in directory.glob("*"):
                photo.unlink()

        self.assertEqual(hall_of_fame.import_history(CHAT), {"added": 1, "known": 0, "without_photos": 1})
        self.assertTrue(all(not w.photos for w in hall_of_fame.load_contest(CHAT, "2026-W39").works))

    def test_a_copied_photo_shares_the_polls_bytes_where_the_disk_allows(self):
        poll = self._poll()
        hall_of_fame.record_poll(poll, poll.tally())
        original = voting.media_path(CHAT, WEEK) / "1_0.jpg"
        copy = hall_of_fame.media_file(CHAT, WEEK, "1_0.jpg")
        self.assertEqual(copy.read_bytes(), original.read_bytes())
        self.assertTrue(copy.samefile(original))

    def test_a_closed_vote_that_was_never_announced_comes_in_too(self):
        self._poll()
        self.assertEqual(hall_of_fame.import_history(CHAT)["added"], 1)
        self.assertEqual(hall_of_fame.load_contest(CHAT, WEEK).winner().entry_id, "2")

    def test_a_vote_still_running_is_not_history(self):
        self._poll(close=False)
        self.assertEqual(hall_of_fame.import_history(CHAT)["added"], 0)

    def test_importing_twice_never_overwrites_what_the_hall_has(self):
        poll = self._poll()
        voting.save_results(poll, poll.tally(), "текст")
        hall_of_fame.record_poll(poll, poll.tally(), closed_at="2026-10-06T10:00:00+00:00")
        before = hall_of_fame.contest_path(CHAT, WEEK).read_bytes()
        self.assertEqual(hall_of_fame.import_history(CHAT), {"added": 0, "known": 1, "without_photos": 0})
        self.assertEqual(hall_of_fame.contest_path(CHAT, WEEK).read_bytes(), before)

    def test_a_thematic_week_is_imported_under_its_own_name(self):
        poll = self._poll(poll_id=voting.poll_id_for(WEEK, ANIME), hashtag=ANIME, title="Лучший аниме-покрас")
        voting.save_results(poll, poll.tally(), "текст")
        hall_of_fame.import_history(CHAT)
        self.assertEqual(hall_of_fame.load_contest(CHAT, poll.poll_id).label(), "Лучший аниме-покрас")


class ReadingTests(_Store):
    def _contest(self, contest_id, rows, hashtag=voting.CONTEST_HASHTAG, title="", badge="", when="2026-10-01"):
        """rows: (entry id, author id, name, votes), in place order."""
        return hall_of_fame.Contest(
            contest_id=contest_id, entry=CHAT, hashtag=hashtag, title=title, badge=badge,
            closed_at=f"{when}T00:00:00+00:00",
            works=[hall_of_fame.Work(entry_id=e, place=i, votes=v, author_id=a, author_name=n)
                   for i, (e, a, n, v) in enumerate(rows, start=1)],
        )

    def test_artists_are_ranked_by_wins_then_podiums_then_votes(self):
        hall = hall_of_fame.build_hall([
            self._contest("2026-W39", [("1", 1, "Аня", 5), ("2", 2, "Боря", 3), ("3", 3, "Вера", 1)], when="2026-09-27"),
            self._contest("2026-W40", [("4", 2, "Боря", 6), ("5", 1, "Аня", 2), ("6", 4, "Гена", 0)], when="2026-10-04"),
            self._contest("2026-W40-abcdef", [("7", 1, "Аня Новая", 4)], hashtag=ANIME, title="Аниме", badge="🌸",
                          when="2026-10-05"),
        ])
        self.assertEqual([c.contest_id for c in hall.contests], ["2026-W40-abcdef", "2026-W40", "2026-W39"])
        self.assertEqual([a.key for a in hall.artists], ["1", "2", "3", "4"])
        anya = hall.by_key["1"]
        self.assertEqual((len(anya.wins), anya.podiums, anya.total_votes, len(anya.entries)), (2, 3, 11, 3))
        self.assertEqual(anya.name, "Аня Новая")          # the newest name wins
        self.assertEqual(hall.by_key["4"].podiums, 0)      # fourth, with a nought
        self.assertEqual(hall.by_key["3"].best_place(), 3)

    def test_the_ranking_is_a_medal_table_silver_before_any_number_of_bronzes(self):
        """Under the old rule (wins, then podium places) two bronzes beat one silver."""
        hall = hall_of_fame.build_hall([
            self._contest("2026-W39", [("1", 9, "Победа", 9), ("2", 1, "Серебро", 5), ("3", 2, "Бронза", 4)]),
            self._contest("2026-W40", [("4", 9, "Победа", 9), ("5", 3, "Другой", 6), ("6", 2, "Бронза", 5)]),
        ])
        bronze, silver = hall.by_key["2"], hall.by_key["1"]
        self.assertEqual((bronze.medals(1), bronze.medals(2), bronze.medals(3)), (0, 0, 2))
        self.assertEqual((silver.medals(1), silver.medals(2), silver.medals(3)), (0, 1, 0))
        self.assertEqual([a.key for a in hall.artists], ["9", "3", "1", "2"])

    def test_a_place_nobody_voted_for_earns_no_medal(self):
        hall = hall_of_fame.build_hall([self._contest("2026-W39", [("1", 1, "Аня", 3), ("2", 2, "Боря", 0)])])
        self.assertEqual(hall.by_key["2"].medals(2), 0)

    def test_a_post_with_a_hidden_sender_still_has_a_page(self):
        hall = hall_of_fame.build_hall([self._contest("2026-W39", [("1", None, "Скрытый", 3)])])
        (artist,) = hall.artists
        self.assertRegex(artist.key, r"^n[0-9a-f]{10}$")
        self.assertIs(hall.by_key[artist.key], artist)

    def test_the_hall_is_parsed_once_until_a_contest_changes(self):
        hall_of_fame.save_contest(self._contest("2026-W39", [("1", 1, "Аня", 5)]))
        hall_of_fame.save_contest(self._contest("2026-W40", [("2", 2, "Боря", 5)]))
        with patch.object(hall_of_fame.Contest, "from_dict", wraps=hall_of_fame.Contest.from_dict) as parse:
            first = hall_of_fame.snapshot(CHAT)
            self.assertEqual(parse.call_count, 2)
            for _ in range(5):
                self.assertIs(hall_of_fame.snapshot(CHAT), first)
            self.assertEqual(parse.call_count, 2)
            hall_of_fame.save_contest(self._contest("2026-W41", [("3", 3, "Вера", 5)]))
            self.assertEqual(len(hall_of_fame.snapshot(CHAT).contests), 3)
            self.assertEqual(parse.call_count, 5)

    def test_a_corrupt_record_costs_that_contest_not_the_hall(self):
        hall_of_fame.save_contest(self._contest("2026-W39", [("1", 1, "Аня", 5)]))
        hall_of_fame.contest_path(CHAT, "2026-W40").write_text("{not json", encoding="utf-8")
        self.assertEqual([c.contest_id for c in hall_of_fame.snapshot(CHAT).contests], ["2026-W39"])

    def test_a_record_survives_its_round_trip(self):
        contest = self._contest("2026-W39", [("1", 1, "Аня", 5)], hashtag=ANIME, title="Аниме", badge="🌸")
        raw = json.loads(json.dumps(contest.to_dict()))
        self.assertEqual(hall_of_fame.Contest.from_dict(raw).to_dict(), contest.to_dict())


if __name__ == "__main__":
    unittest.main()
