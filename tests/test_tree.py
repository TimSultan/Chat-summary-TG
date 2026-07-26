import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import stats
import tree

# The chat's own measured output: ~3,600 XP a day across everybody.
MEASURED_DAILY_XP = 3_600


class GrowthTests(unittest.TestCase):
    def test_the_final_stage_lands_at_about_three_years(self):
        three_years = MEASURED_DAILY_XP * 365 * 3
        number, _, name = tree.tree_stage(three_years)

        self.assertEqual(number, len(tree.TREE_STAGES))
        self.assertEqual(name, "Легендарное Древо ЕПХ")
        # ...and not appreciably sooner: two and a half years must still be short of it.
        earlier, _, _ = tree.tree_stage(MEASURED_DAILY_XP * 365 * 2.5)
        self.assertLess(earlier, len(tree.TREE_STAGES))

    def test_everyone_starts_from_a_seed(self):
        number, _, name = tree.tree_stage(0)
        self.assertEqual((number, name), (1, "Семечко"))
        self.assertEqual(tree.tree_height_mm(0), 0)

    def test_stages_are_ordered_and_never_go_backwards(self):
        heights = [minimum for minimum, _, _ in tree.TREE_STAGES]
        self.assertEqual(heights, sorted(heights))
        self.assertEqual(len(set(heights)), len(heights))

        seen = 0
        for days in range(0, 1200, 7):
            number, _, _ = tree.tree_stage(MEASURED_DAILY_XP * days)
            self.assertGreaterEqual(number, seen)
            seen = number

    def test_height_is_capped_so_the_tree_cannot_outgrow_its_last_name(self):
        # XP accrues forever; without the cap the tree would keep climbing past any
        # stage anybody has a word for.
        self.assertEqual(tree.tree_height_mm(10**12), tree.TREE_MAX_HEIGHT_MM)
        number, _, _ = tree.tree_stage(10**12)
        self.assertEqual(number, len(tree.TREE_STAGES))

    def test_a_days_growth_tracks_how_busy_the_chat_was(self):
        base = MEASURED_DAILY_XP * 100
        quiet = tree.tree_height_mm(base + 1_600) - tree.tree_height_mm(base)
        busy = tree.tree_height_mm(base + 5_600) - tree.tree_height_mm(base)

        self.assertGreater(busy, quiet)
        # An ordinary day is a readable number of millimetres, not 0 and not a metre.
        ordinary = tree.tree_height_mm(base + MEASURED_DAILY_XP) - tree.tree_height_mm(base)
        self.assertGreaterEqual(ordinary, 5)
        self.assertLessEqual(ordinary, 60)

    def test_next_stage_counts_down_and_stops_at_the_top(self):
        name, remaining = tree.next_stage(0)
        self.assertEqual(name, "Росток")
        self.assertGreater(remaining, 0)
        self.assertIsNone(tree.next_stage(10**12))

    def test_lengths_read_in_the_unit_that_suits_them(self):
        self.assertEqual(tree.format_length(7), "7 мм")
        self.assertEqual(tree.format_length(612), "61,2 см")
        self.assertEqual(tree.format_length(19_710), "19,71 м")
        # A day's growth stays in millimetres far longer -- "18 мм" reads as progress.
        self.assertEqual(tree.format_growth(18), "18 мм")
        self.assertEqual(tree.format_growth(0), "0 мм")


class AdviceTests(unittest.TestCase):
    def test_there_are_120_distinct_lines(self):
        self.assertEqual(len(tree.DAILY_ADVICE), 120)
        self.assertEqual(len(set(tree.DAILY_ADVICE)), 120)

    def test_every_line_is_a_usable_sentence(self):
        for line in tree.DAILY_ADVICE:
            with self.subTest(line=line):
                self.assertTrue(line.strip())
                self.assertLess(len(line), 200)
                self.assertEqual(line, line.strip())
                self.assertTrue(line[0].isupper())
                self.assertIn(line[-1], ".!?")

    def test_the_same_day_always_gives_the_same_line(self):
        # It is a shared greeting, not a personal fortune: everybody must see one line,
        # and a restart must not change it halfway through the morning.
        day = date(2026, 7, 26)
        self.assertEqual(tree.advice_for(day), tree.advice_for(day))
        self.assertNotEqual(tree.advice_for(day), tree.advice_for(day + timedelta(days=1)))

    def test_no_repeat_for_a_full_rotation(self):
        start = date(2026, 1, 1)
        picked = [tree.advice_for(start + timedelta(days=offset)) for offset in range(120)]
        self.assertEqual(len(set(picked)), 120)
        # Day 121 comes back round to the first.
        self.assertEqual(tree.advice_for(start + timedelta(days=120)), picked[0])


class DigestTests(unittest.TestCase):
    CONTRIBUTORS = [
        ("Первый", "first", 423),
        ("Второй", "second", 383),
        ("Третий", "third", 326),
        ("Четвёртый", "fourth", 12),
    ]

    def _digest(self, **kwargs):
        params = dict(
            total_xp=MEASURED_DAILY_XP * 100,
            yesterday_xp=MEASURED_DAILY_XP,
            contributors=self.CONTRIBUTORS,
            day=date(2026, 7, 26),
        )
        params.update(kwargs)
        return tree.format_morning_digest(**params)

    def test_it_greets_reports_growth_and_names_the_top_three(self):
        text = self._digest()

        self.assertIn("Доброе утро, ЕПХ-чане!", text)
        self.assertIn("выросло на", text)
        self.assertIn("@first — 423 XP", text)
        self.assertIn("@third — 326 XP", text)
        # Only three, so the fourth is not named.
        self.assertNotIn("fourth", text)
        self.assertIn("Напутствие на день", text)

    def test_a_member_without_a_username_is_named_and_escaped(self):
        text = self._digest(contributors=[("<Худож & ник>", None, 100)])
        self.assertIn("&lt;Худож &amp; ник&gt;", text)
        self.assertNotIn("<Худож", text)

    def test_nobody_earning_anything_drops_the_whole_block(self):
        text = self._digest(yesterday_xp=0, contributors=[("Никто", "nobody", 0)])
        self.assertNotIn("Самый большой вклад", text)
        self.assertIn("выросло на 0 мм", text)
        # The greeting and the advice still go out -- it is a morning post, not a report.
        self.assertIn("Доброе утро", text)
        self.assertIn("Напутствие на день", text)

    def test_the_countdown_disappears_at_the_final_stage(self):
        topped_out = self._digest(total_xp=10**12)
        self.assertNotIn("До стадии", topped_out)
        self.assertIn("Легендарное Древо ЕПХ", topped_out)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def test_it_is_pinned_to_moscow_at_ten(self):
        self.assertEqual(stats.TREE_DIGEST_HOUR, 10)
        self.assertEqual(str(stats.tree_digest_tz()), "Europe/Moscow")

    def test_a_restart_the_same_morning_does_not_greet_twice(self):
        today = date(2026, 7, 26)
        self.assertTrue(stats.should_send_tree_digest("chat", today))

        stats.mark_tree_digest_sent("chat", today)
        self.assertFalse(stats.should_send_tree_digest("chat", today))
        # Tomorrow is a fresh morning.
        self.assertTrue(stats.should_send_tree_digest("chat", today + timedelta(days=1)))

    def test_the_marker_is_per_chat(self):
        today = date(2026, 7, 26)
        stats.mark_tree_digest_sent("chat", today)
        self.assertTrue(stats.should_send_tree_digest("other", today))

    def test_the_digest_is_html_and_the_queue_carries_that(self):
        """The two digests sharing one queue need different parse modes: sending the
        tree post as plain text prints its tags verbatim, and sending the procrastinator
        list as HTML has Telegram reject it over an unescaped display name."""
        import inspect

        import listener

        text = tree.format_morning_digest(
            360_000, 3_600, [("Кто-то", "someone", 10)], date(2026, 7, 26)
        )
        self.assertIn("<b>", text)

        source = inspect.getsource(listener._send_tree_digests)
        self.assertIn('put((entry, text, "HTML"))', source)
        self.assertIn("put((entry, text, None))", inspect.getsource(listener._send_procrastinator_digests))


if __name__ == "__main__":
    unittest.main()
