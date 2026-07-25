import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import economy
import stats


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def test_opening_balance_is_the_coins_stat_already_showed(self):
        # The chosen migration: nobody's visible number changes on deploy. It falls out
        # of the formula rather than needing a backfill script, because `spent` starts at
        # zero for everyone.
        self.assertEqual(economy.balance("chat", "1", 6_910), stats.coins_for_xp(6_910))
        self.assertEqual(economy.balance("chat", "1", 6_910), 691)

    def test_spend_refund_and_running_balance(self):
        # 5_000 XP -> 500 coins earned.
        self.assertEqual(economy.balance("chat", "1", 5_000), 500)

        ok, remaining = economy.spend("chat", "1", 5_000, 200, "buy:freeze")
        self.assertTrue(ok)
        self.assertEqual(remaining, 300)
        self.assertEqual(economy.balance("chat", "1", 5_000), 300)

        # Earning more XP later adds on top of what is already spent.
        self.assertEqual(economy.balance("chat", "1", 6_000), 400)

        refused, unchanged = economy.spend("chat", "1", 5_000, 10_000, "buy:impossible")
        self.assertFalse(refused)
        self.assertEqual(unchanged, 300)
        self.assertEqual(economy.balance("chat", "1", 5_000), 300)

        # A refund undoes the debit rather than granting a bonus, so a purchase that was
        # never delivered leaves no trace in lifetime spend.
        self.assertEqual(economy.refund("chat", "1", 5_000, 200, "freeze"), 500)

    def test_balance_never_goes_negative_when_xp_is_clawed_back(self):
        # /deletepokras removes a figurine and its 200 XP (20 coins) -- possibly after
        # those coins were already spent.
        economy.spend("chat", "1", 1_000, 100, "buy:roast")
        self.assertEqual(economy.balance("chat", "1", 1_000), 0)
        self.assertEqual(economy.balance("chat", "1", 800), 0)

    def test_transfer_burns_a_cut_and_moves_the_rest(self):
        ok, refusal, delivered = economy.transfer("chat", "1", 10_000, "2", 100)
        self.assertTrue(ok, refusal)
        self.assertEqual(delivered, 90)
        # The sender pays exactly what they typed; the burn comes out of what arrives.
        self.assertEqual(economy.balance("chat", "1", 10_000), 900)
        self.assertEqual(economy.balance("chat", "2", 0), 90)

    def test_transfer_refusals(self):
        ok, refusal, _ = economy.transfer("chat", "1", 10_000, "1", 100)
        self.assertFalse(ok)
        self.assertIn("самому себе", refusal)

        ok, refusal, _ = economy.transfer("chat", "1", 10_000, "2", 1)
        self.assertFalse(ok)
        self.assertIn("Минимальный перевод", refusal)

        ok, refusal, _ = economy.transfer("chat", "1", 0, "2", 50)
        self.assertFalse(ok)
        self.assertIn("Недостаточно", refusal)

    def test_purchase_enforces_price_then_cooldown(self):
        roast = economy.find_item("roast")

        ok, refusal, _ = economy.purchase("chat", "1", 0, roast)
        self.assertFalse(ok)
        self.assertIn(str(roast.price), refusal)

        ok, refusal, remaining = economy.purchase("chat", "1", 5_000, roast)
        self.assertTrue(ok, refusal)
        self.assertEqual(remaining, 500 - roast.price)

        # Bought again immediately: refused by the cooldown, and no second debit.
        ok, refusal, unchanged = economy.purchase("chat", "1", 5_000, roast)
        self.assertFalse(ok)
        self.assertIn("Ещё рано", refusal)
        self.assertEqual(unchanged, 500 - roast.price)


class EffectTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def test_title_is_rented_and_expires_on_read(self):
        economy.set_title("chat", "1", "  Повелитель   грунтовки  ")
        # Whitespace is collapsed and the length bounded, since it renders into /stat.
        self.assertEqual(economy.active_title("chat", "1"), "Повелитель грунтовки")

        later = datetime.now(timezone.utc) + timedelta(days=economy.TITLE_DAYS + 1)
        with patch("economy.app_now", return_value=later):
            self.assertIsNone(economy.active_title("chat", "1"))

    def test_a_freeze_bridges_a_gap_without_inventing_a_day(self):
        today = date(2026, 7, 25)
        # Posted every day except the 24th.
        active = {(today - timedelta(days=offset)).isoformat() for offset in range(0, 6)}
        active.discard("2026-07-24")

        # Without a freeze the streak stops at the gap.
        self.assertEqual(stats._current_streak(active, today), 1)

        economy.add_streak_freeze("chat", "1")
        covered = economy.apply_streak_freezes("chat", "1", active, today)
        self.assertEqual(covered, {"2026-07-24"})
        # 25th, [24th bridged but not counted], 23rd, 22nd, 21st, 20th.
        self.assertEqual(stats._current_streak(active, today, covered), 5)
        self.assertEqual(economy.streak_freezes("chat", "1"), 0)

    def test_freezes_are_not_spent_when_there_is_no_gap(self):
        today = date(2026, 7, 25)
        active = {(today - timedelta(days=offset)).isoformat() for offset in range(0, 5)}
        economy.add_streak_freeze("chat", "1")

        covered = economy.apply_streak_freezes("chat", "1", active, today)

        self.assertEqual(covered, set())
        self.assertEqual(economy.streak_freezes("chat", "1"), 1)

    def test_covering_the_same_gap_twice_costs_one_freeze(self):
        today = date(2026, 7, 25)
        active = {"2026-07-25", "2026-07-23", "2026-07-22"}
        economy.add_streak_freeze("chat", "1")
        economy.add_streak_freeze("chat", "1")

        first = economy.apply_streak_freezes("chat", "1", active, today)
        second = economy.apply_streak_freezes("chat", "1", active, today)

        self.assertEqual(first, second)
        self.assertEqual(economy.streak_freezes("chat", "1"), 1)

    def test_reputation_only_moves_on_peer_granted_input(self):
        # Nothing self-generated counts: posting all day leaves this at zero.
        self.assertEqual(economy.reputation_for("chat", "1"), 0)

        economy.transfer("chat", "2", 100_000, "1", 400)
        # 360 delivered // 20 = 18
        self.assertEqual(economy.reputation_for("chat", "1"), 18)
        self.assertEqual(stats.reputation_tier(18)[1], "Замеченный")

    def test_stat_extras_degrade_instead_of_breaking_stat(self):
        with patch("economy.balance", side_effect=OSError("disk gone")):
            self.assertEqual(economy.stat_extras("chat", "1", 500), {})


class LevelTrackTests(unittest.TestCase):
    def test_chat_level_has_no_figurine_gate(self):
        # The exact case the split exists for: a prolific talker who has never painted.
        talker = stats.chat_level(11_648)
        self.assertEqual(talker.number, 19)
        self.assertIn("Заводила", talker.label)

        # ...and a prolific painter who barely talks still ranks on the craft track.
        rank, _ = stats.painter_rank(50)
        self.assertEqual(rank.name, "Легенда покраса")

    def test_chat_level_curve_is_monotonic_and_capped(self):
        thresholds = [stats.chat_level_threshold(n) for n in range(1, stats.MAX_CHAT_LEVEL + 1)]
        self.assertEqual(thresholds, sorted(thresholds))
        self.assertEqual(thresholds[0], 0)
        top = stats.chat_level(10**9)
        self.assertEqual(top.number, stats.MAX_CHAT_LEVEL)
        self.assertIsNone(top.next_threshold)
        self.assertEqual(stats.chat_level_progress(10**9), 100)

    def test_progress_bar_shows_position_without_revealing_the_target(self):
        xp = (stats.chat_level_threshold(5) + stats.chat_level_threshold(6)) // 2
        percent = stats.chat_level_progress(xp)
        self.assertGreater(percent, 40)
        self.assertLess(percent, 60)

        user = stats.UserStats(user_id="1", display_name="Tester")
        text = stats.format_stat(user, rank=1, total=1, xp=xp, streak=0)
        self.assertIn("▓", text)
        self.assertNotIn(str(stats.chat_level_threshold(6)), text)

    def test_stat_renders_all_three_tracks_and_a_bought_title(self):
        user = stats.UserStats(user_id="1", display_name="Tester", figurines_painted=12)
        text = stats.format_stat(
            user, rank=1, total=1, xp=5_000, streak=0,
            coins=137, reputation=55, custom_title="Повелитель грунтовки",
        )

        self.assertIn("🪙 Монеты: 137", text)
        self.assertIn("🧩 Уровень:", text)
        self.assertIn("🎨 Звание: 💨 Укротитель аэрографа", text)
        self.assertIn("Репутация: 55 (Опора чата)", text)
        self.assertIn("«Повелитель грунтовки»", text)


class AntiFarmingTests(unittest.TestCase):
    @staticmethod
    def _message(moment, text, message_id=1, sender_id=20, is_reply=False):
        return SimpleNamespace(
            sender_id=sender_id, sender_name="User", sender_username="user",
            text=text, dt_local=moment, message_id=message_id, is_reply=is_reply,
        )

    def test_daily_word_cap_bounds_a_burst_but_not_a_normal_day(self):
        start = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
        ordinary = stats.compute_day_stats(
            [self._message(start + timedelta(minutes=i), "слово " * 20, i) for i in range(10)]
        )
        self.assertEqual(ordinary["20"]["words"], 200)

        farmed = stats.compute_day_stats(
            [self._message(start + timedelta(minutes=i), "слово " * 200, i) for i in range(50)]
        )
        self.assertEqual(farmed["20"]["words"], stats.XP_DAILY_WORD_CAP)
        # The message count itself is never capped -- it describes what happened.
        self.assertEqual(farmed["20"]["messages"], 50)

    def test_media_and_reply_caps(self):
        start = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
        media = stats.compute_day_stats(
            [self._message(start + timedelta(minutes=i), "[Photo] x", i) for i in range(80)]
        )
        self.assertEqual(media["20"]["media"], stats.XP_DAILY_MEDIA_CAP)

        replies = stats.compute_day_stats(
            [
                self._message(start + timedelta(minutes=i), "ага", i, is_reply=True)
                for i in range(150)
            ]
        )
        self.assertEqual(replies["20"]["replies"], stats.XP_DAILY_REPLY_CAP)

    def test_photo_bursts_are_not_penalised_by_default(self):
        # Measured on this chat's own history, a 45s cooldown suppressed half of all
        # media because painters post several angles of one model back to back. The
        # mechanism ships disabled; this pins that decision so re-enabling it is a
        # deliberate act with a failing test to look at.
        self.assertEqual(stats.XP_MESSAGE_COOLDOWN_SECONDS, 0)
        start = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
        burst = stats.compute_day_stats(
            [self._message(start + timedelta(seconds=3 * i), "[Photo] угол", i) for i in range(5)]
        )
        self.assertEqual(burst["20"]["media"], 5)

    def test_cooldown_suppresses_scoring_only_when_enabled(self):
        start = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
        burst = [
            self._message(start, "первое сообщение тут", 1),
            self._message(start + timedelta(seconds=5), "второе сообщение тут", 2),
            self._message(start + timedelta(seconds=90), "третье сообщение тут", 3),
        ]
        with patch("stats.XP_MESSAGE_COOLDOWN_SECONDS", 45):
            computed = stats.compute_day_stats(burst)

        # Message 2 is inside the window, so it does not score...
        self.assertEqual(computed["20"]["words"], 6)
        # ...but it is still a message that happened.
        self.assertEqual(computed["20"]["messages"], 3)
        self.assertEqual(sum(computed["20"]["hours"].values()), 3)


if __name__ == "__main__":
    unittest.main()
