"""voting.py's announced results: the wording of the announcement text and the on-disk
record it is saved with.

The text tests are the strict ones -- that message is posted verbatim into the chat, so
they assert the whole string rather than "contains", and one of them sweeps every
character for an emoji (bot prose in this project is emoji-free by rule, and a medal
sneaking back into the top-3 lines is exactly the regression that rule exists to catch).
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import voting


def _entry(entry_id, name="Имя", username=None, text="", media=()):
    return voting.Entry(
        entry_id=entry_id,
        message_id=int(entry_id) if entry_id.isdigit() else 1,
        author_id=int(entry_id) if entry_id.isdigit() else None,
        author_name=name,
        author_username=username,
        text=text,
        media=list(media),
    )


def _is_emoji(character: str) -> bool:
    """Anything in the pictographic ranges Telegram renders as a picture. Deliberately
    crude and wide -- a false positive here is a test to fix, a false negative is a medal
    shipped to the chat."""
    code = ord(character)
    return (
        0x1F000 <= code <= 0x1FAFF     # emoji blocks proper (medals, faces, symbols)
        or 0x2600 <= code <= 0x27BF    # misc symbols + dingbats
        or 0x2B00 <= code <= 0x2BFF    # arrows/stars
        or code in (0xFE0F, 0x20E3)    # variation selector, keycap
    )


class FormatResultsTextTests(unittest.TestCase):
    def test_the_announcement_reads_exactly_as_dictated(self):
        standings = [
            (_entry("1", "Ник", username="nick"), 5),
            (_entry("2", "Вторая"), 3),
            (_entry("3", "Третий"), 1),
        ]
        self.assertEqual(
            voting.format_results_text(standings),
            "Результаты недельного голосования:\n"
            "1. Ник (@nick) — 5 голосов\n"
            "2. Вторая — 3 голоса\n"
            "3. Третий — 1 голос\n"
            "\n"
            "Всем спасибо за участие.\n"
            "Красим дальше.",
        )

    def test_a_username_is_appended_only_when_the_entry_has_one(self):
        standings = [(_entry("1", "Ник", username="nick"), 2), (_entry("2", "Безымянный"), 1)]
        text = voting.format_results_text(standings)
        self.assertIn("1. Ник (@nick) — 2 голоса", text)
        self.assertIn("2. Безымянный — 1 голос", text)
        self.assertNotIn("(@)", text)

    def test_the_list_is_as_long_as_the_contest_was(self):
        standings = [(_entry("1", "Первый"), 4), (_entry("2", "Второй"), 2)]
        text = voting.format_results_text(standings)
        self.assertIn("2. Второй — 2 голоса", text)
        self.assertNotIn("3.", text)

    def test_every_entrant_is_listed_including_the_ones_nobody_voted_for(self):
        # The whole contest is acknowledged, not just a podium: tally() hands back every
        # ADMITTED entry and all of them are named, a nought being a result too.
        standings = [(_entry("1", "Первый"), 3), (_entry("2", "Тихий"), 0), (_entry("3", "Молчун"), 0)]
        text = voting.format_results_text(standings)
        self.assertIn("1. Первый — 3 голоса", text)
        self.assertIn("2. Тихий — 0 голосов", text)
        self.assertIn("3. Молчун — 0 голосов", text)

    def test_nobody_voted_says_so_instead_of_an_empty_podium(self):
        text = voting.format_results_text([(_entry("1", "Первый"), 0)])
        self.assertEqual(
            text,
            "Результаты недельного голосования:\n"
            "В этот раз голосов не набрал никто.\n"
            "\n"
            "Всем спасибо за участие.\n"
            "Красим дальше.",
        )

    def test_an_empty_standings_list_is_the_same_honest_message(self):
        self.assertEqual(voting.format_results_text([]), voting.format_results_text([(_entry("1"), 0)]))

    def test_everyone_is_listed_by_default_and_the_cap_is_opt_in(self):
        standings = [(_entry(str(i), f"N{i}"), 10 - i) for i in range(1, 6)]
        self.assertIn("5. N5 — 5 голосов", voting.format_results_text(standings))
        capped = voting.format_results_text(standings, places=1)
        self.assertEqual([line for line in capped.splitlines() if line[:1].isdigit()],
                         ["1. N1 — 9 голосов"])
        self.assertNotIn("N2", capped)

    def test_the_vote_count_is_declined_for_russian(self):
        counts = [1, 2, 4, 5, 11, 12, 14, 21, 22, 25, 101, 0]
        expected = ["1 голос", "2 голоса", "4 голоса", "5 голосов", "11 голосов",
                    "12 голосов", "14 голосов", "21 голос", "22 голоса", "25 голосов",
                    "101 голос", "0 голосов"]
        self.assertEqual([voting.votes_label(n) for n in counts], expected)

    def test_no_emoji_anywhere_in_the_announcement(self):
        standings = [
            (_entry("1", "Ник", username="nick"), 5),
            (_entry("2", "Вторая"), 3),
            (_entry("3", "Третий"), 1),
        ]
        for text in (voting.format_results_text(standings), voting.format_results_text([])):
            offenders = [c for c in text if _is_emoji(c)]
            self.assertEqual(offenders, [], f"emoji in announcement text: {offenders!r}")

    def test_who_renders_the_author_with_and_without_a_handle(self):
        self.assertEqual(voting.who(_entry("1", "Ник", username="nick")), "Ник (@nick)")
        self.assertEqual(voting.who(_entry("1", "Ник")), "Ник")


class ResultsStorageTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("voting._voting_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def _poll(self, standings):
        poll = voting.Poll(
            poll_id="2026-08-02",
            entry="Chat",
            created_at="2026-08-02T00:00:00+00:00",
            entries=[e for e, _ in standings],
        )
        voting.set_approved(poll, [e.entry_id for e, _ in standings])
        for user_id, (e, votes) in enumerate(standings):
            for n in range(votes):
                voting.record_vote(poll, f"{user_id}-{n}", [e.entry_id])
        return poll

    def _standings(self):
        return [
            (_entry("1", "Ник", username="nick", text="работа", media=["1_0.jpg"]), 2),
            (_entry("2", "Вторая"), 1),
            (_entry("3", "Третий"), 0),
        ]

    def test_save_then_load_round_trips_the_whole_record(self):
        standings = self._standings()
        poll = self._poll(standings)
        text = voting.format_results_text(standings)

        path = voting.save_results(poll, standings, text)
        loaded = voting.load_results("Chat", "2026-08-02")

        self.assertEqual(path, voting.results_path("Chat", "2026-08-02"))
        self.assertTrue(path.exists())
        self.assertEqual(loaded["poll_id"], "2026-08-02")
        self.assertEqual(loaded["entry"], "Chat")
        self.assertEqual(loaded["created_at"], "2026-08-02T00:00:00+00:00")
        self.assertEqual(loaded["text"], text)
        self.assertEqual(loaded["voters"], len(poll.votes))
        self.assertTrue(loaded["announced_at"])

    def test_every_ranked_entry_is_recorded_not_just_the_announced_top_three(self):
        standings = self._standings()
        voting.save_results(self._poll(standings), standings, "t")
        recorded = voting.load_results("Chat", "2026-08-02")["standings"]

        self.assertEqual([row["place"] for row in recorded], [1, 2, 3])
        self.assertEqual([row["entry_id"] for row in recorded], ["1", "2", "3"])
        self.assertEqual([row["votes"] for row in recorded], [2, 1, 0])  # the zero too
        self.assertEqual(recorded[0]["author_username"], "nick")
        self.assertIsNone(recorded[1]["author_username"])
        self.assertEqual(recorded[0]["author_name"], "Ник")
        self.assertEqual(recorded[0]["text"], "работа")
        self.assertEqual(recorded[0]["media"], ["1_0.jpg"])

    def test_re_announcing_overwrites_rather_than_piling_up(self):
        first = self._standings()
        voting.save_results(self._poll(first), first, "старый текст")
        # Votes shifted after the first announcement, so the second one is the newer truth.
        second = [(_entry("2", "Вторая"), 9), (_entry("1", "Ник", username="nick"), 1)]
        voting.save_results(self._poll(second), second, "новый текст")

        loaded = voting.load_results("Chat", "2026-08-02")
        self.assertEqual(loaded["text"], "новый текст")
        self.assertEqual([row["entry_id"] for row in loaded["standings"]], ["2", "1"])
        directory = voting.results_path("Chat", "2026-08-02").parent
        self.assertEqual(len(list(directory.glob("*.json"))), 1)
        self.assertEqual(list(directory.glob("*.tmp")), [])

    def test_results_land_under_the_patched_voting_dir_and_out_of_the_poll_glob(self):
        standings = self._standings()
        voting.save_results(self._poll(standings), standings, "t")
        root = Path(self._temporary.name)
        self.assertEqual(voting.results_path("Chat", "p").parent, root / "results")
        # latest_poll globs the voting dir directly -- a results file must not land there.
        self.assertIsNone(voting.latest_poll("Chat"))

    def test_two_polls_of_the_same_chat_keep_separate_records(self):
        standings = self._standings()
        poll = self._poll(standings)
        voting.save_results(poll, standings, "первый")
        poll.poll_id = "2026-08-09"
        voting.save_results(poll, standings, "второй")

        self.assertEqual(voting.load_results("Chat", "2026-08-02")["text"], "первый")
        self.assertEqual(voting.load_results("Chat", "2026-08-09")["text"], "второй")

    def test_results_are_scoped_to_their_own_chat(self):
        standings = self._standings()
        voting.save_results(self._poll(standings), standings, "t")
        self.assertIsNone(voting.load_results("Other Chat", "2026-08-02"))

    def test_loading_results_that_were_never_saved_returns_none(self):
        self.assertIsNone(voting.load_results("Chat", "nope"))

    def test_a_corrupt_record_reads_as_nothing_announced_rather_than_raising(self):
        path = voting.results_path("Chat", "2026-08-02")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json at all", encoding="utf-8")
        self.assertIsNone(voting.load_results("Chat", "2026-08-02"))

    def test_a_record_that_is_valid_json_but_not_an_object_reads_as_none(self):
        path = voting.results_path("Chat", "2026-08-02")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(["not", "a", "record"]), encoding="utf-8")
        self.assertIsNone(voting.load_results("Chat", "2026-08-02"))

    def test_the_record_survives_a_real_json_reload_with_cyrillic_intact(self):
        standings = self._standings()
        poll = self._poll(standings)
        text = voting.format_results_text(standings)
        path = voting.save_results(poll, standings, text)
        # ensure_ascii=False is what keeps the file readable by a human opening it.
        self.assertIn("Результаты", path.read_text(encoding="utf-8"))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["text"], text)


if __name__ == "__main__":
    unittest.main()
