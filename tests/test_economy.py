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

    def test_figurine_reward_is_five_hundred_coins(self):
        self.assertEqual(economy.FIGURINE_COIN_REWARD, 500)

    def test_opening_balance_is_the_coins_stat_already_showed(self):
        # The chosen migration: nobody's visible number changes on deploy. It falls out
        # of the formula rather than needing a backfill script, because `spent` starts at
        # zero for everyone.
        self.assertEqual(economy.balance("chat", "1", 6_910), stats.coins_for_xp(6_910))

    def test_spend_refund_and_running_balance(self):
        # XP amounts are expressed in whole coins rather than as literals: XP_PER_COIN is
        # a live balance knob (halved to 5 to lift the floor for members who only chat),
        # and none of the arithmetic being pinned here depends on the rate itself.
        xp = stats.XP_PER_COIN * 500          # exactly 500 coins earned
        more_xp = stats.XP_PER_COIN * 600     # exactly 600
        self.assertEqual(economy.balance("chat", "1", xp), 500)

        ok, remaining = economy.spend("chat", "1", xp, 200, "buy:freeze")
        self.assertTrue(ok)
        self.assertEqual(remaining, 300)
        self.assertEqual(economy.balance("chat", "1", xp), 300)

        # Earning more XP later adds on top of what is already spent.
        self.assertEqual(economy.balance("chat", "1", more_xp), 400)

        refused, unchanged = economy.spend("chat", "1", xp, 10_000, "buy:impossible")
        self.assertFalse(refused)
        self.assertEqual(unchanged, 300)
        self.assertEqual(economy.balance("chat", "1", xp), 300)

        # A refund undoes the debit rather than granting a bonus, so a purchase that was
        # never delivered leaves no trace in lifetime spend.
        self.assertEqual(economy.refund("chat", "1", xp, 200, "freeze"), 500)

    def test_balance_never_goes_negative_when_xp_is_clawed_back(self):
        # /deletepokras removes a figurine and its XP -- possibly after the coins it was
        # worth have already been spent. Spend the whole balance, then claw back XP.
        xp = stats.XP_PER_COIN * 100
        economy.spend("chat", "1", xp, 100, "buy:roast")
        self.assertEqual(economy.balance("chat", "1", xp), 0)
        self.assertEqual(economy.balance("chat", "1", xp - stats.XP_PER_COIN * 20), 0)

    def test_catalogue_is_the_title_alone(self):
        self.assertEqual([item.code for item in economy.SHOP_ITEMS], ["title"])
        self.assertIsNone(economy.find_item("roast"))
        self.assertIsNone(economy.find_item("freeze"))

    def test_purchase_enforces_price_then_cooldown(self):
        # No listed item carries a cooldown now, so the rule is exercised directly --
        # re-listing anything with one must keep working.
        item = economy.ShopItem("probe", "Проба", 100, "", cooldown_hours=24)

        ok, refusal, _ = economy.purchase("chat", "1", 0, item)
        self.assertFalse(ok)
        self.assertIn("100", refusal)

        funded = stats.XP_PER_COIN * 500      # exactly 500 coins, whatever the rate is
        ok, refusal, remaining = economy.purchase("chat", "1", funded, item)
        self.assertTrue(ok, refusal)
        self.assertEqual(remaining, 400)

        # Bought again immediately: refused by the cooldown, and no second debit.
        ok, refusal, unchanged = economy.purchase("chat", "1", funded, item)
        self.assertFalse(ok)
        self.assertIn("Ещё рано", refusal)
        self.assertEqual(unchanged, 400)


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

    def test_the_peer_granted_half_moves_only_on_peer_granted_input(self):
        # Called without a UserStats -- the earned-badge half has nothing to read, so
        # this is the peer-granted score on its own.
        self.assertEqual(economy.reputation_for("chat", "1"), 0)

        badge = stats.create_custom_badge("chat", "🎯", "Меткий глаз", 10, "Admin")
        stats.give_custom_badge("chat", badge.badge_id, "1", "User", 10, "Admin")
        self.assertEqual(economy.reputation_for("chat", "1"), stats.REPUTATION_PER_BADGE_RECEIVED)

        stats.record_weekly_contest_winner("chat", 1, "1", "User", 10, "Admin")
        self.assertEqual(
            economy.reputation_for("chat", "1"),
            stats.REPUTATION_PER_BADGE_RECEIVED + stats.REPUTATION_PER_CONTEST_WIN,
        )

    def test_earned_badges_add_reputation_once_a_userstats_is_passed(self):
        """The /stat paths all hold a UserStats, so this is what members actually see."""
        user = stats.UserStats(user_id="1", figurines_painted=10, media=25)
        # Painting tiers 1-3, plus the gallery badge.
        self.assertEqual(stats.medal_levels(user), 4)
        self.assertEqual(
            economy.reputation_for("chat", "1", user),
            4 * stats.REPUTATION_PER_MEDAL_LEVEL,
        )

        stats.record_weekly_contest_winner("chat", 1, "1", "User", 10, "Admin")
        self.assertEqual(
            economy.reputation_for("chat", "1", user),
            4 * stats.REPUTATION_PER_MEDAL_LEVEL + stats.REPUTATION_PER_CONTEST_WIN,
        )

    def test_stat_extras_passes_the_userstats_through_to_reputation(self):
        user = stats.UserStats(user_id="1", figurines_painted=50, media=25)
        extras = economy.stat_extras("chat", "1", 500, user)
        self.assertEqual(extras["reputation"], stats.medal_levels(user))
        # Without it, the same member scores only the peer-granted half.
        self.assertEqual(economy.stat_extras("chat", "1", 500)["reputation"], 0)

    def test_stat_extras_degrade_instead_of_breaking_stat(self):
        with patch("economy.balance", side_effect=OSError("disk gone")):
            self.assertEqual(economy.stat_extras("chat", "1", 500), {})


class SeasonTests(unittest.TestCase):
    def test_seasons_are_calendar_quarters(self):
        self.assertEqual(
            stats.season_bounds(date(2026, 7, 25)), (date(2026, 7, 1), date(2026, 9, 30))
        )
        self.assertEqual(
            stats.season_bounds(date(2026, 1, 1)), (date(2026, 1, 1), date(2026, 3, 31))
        )
        # The Q4 boundary is the one that would break a naive month+3 calculation.
        self.assertEqual(
            stats.season_bounds(date(2026, 12, 31)), (date(2026, 10, 1), date(2026, 12, 31))
        )
        self.assertEqual(stats.season_key(date(2026, 7, 25)), "2026-S3")
        self.assertNotEqual(
            stats.season_key(date(2026, 9, 30)), stats.season_key(date(2026, 10, 1))
        )

    def test_season_xp_counts_only_days_inside_the_season(self):
        combined = {}
        payload = lambda day: {
            "day": day,
            "users": {"20": {"messages": 10, "words": 500, "media": 0, "replies": 0}},
        }
        season_start = date(2026, 7, 1)
        stats._merge_day(combined, payload("2026-06-20"), season_start=season_start)
        stats._merge_day(combined, payload("2026-07-10"), season_start=season_start)
        user = combined["20"]

        self.assertEqual(user.words, 1_000)
        self.assertEqual(user.season_words, 500)
        self.assertEqual(user.active_days, 2)
        self.assertEqual(user.season_active_days, 1)
        # All-time is strictly larger, and the level reads the smaller number.
        self.assertGreater(user.xp(5.0), user.season_xp(5.0))

    def test_season_xp_falls_back_to_all_time_when_no_window_was_applied(self):
        # Aggregates built without a season_start (older callers, tests) must still
        # render a sensible level rather than reporting everybody at level 1.
        combined = {}
        stats._merge_day(combined, {
            "day": "2026-07-10",
            "users": {"20": {"messages": 10, "words": 500, "media": 0, "replies": 0}},
        })
        user = combined["20"]
        self.assertEqual(user.season_xp(5.0), user.xp(5.0))

    def test_a_season_of_p95_activity_reaches_the_top_level(self):
        """The calibration target: ~103 XP/day for 90 days maxes the ladder."""
        season_xp = 103 * 90
        self.assertEqual(stats.chat_level(season_xp).number, stats.MAX_CHAT_LEVEL)
        # ...and a season at the p90 rate gets most of the way, not all of it.
        self.assertLess(stats.chat_level(68 * 90).number, stats.MAX_CHAT_LEVEL)
        self.assertGreater(stats.chat_level(68 * 90).number, 25)

    def test_a_new_season_rebaselines_the_watermark_without_a_word(self):
        user = stats.UserStats(user_id="20", username="user", display_name="User")
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                with patch("stats.app_now", return_value=datetime(2026, 7, 15, tzinfo=timezone.utc)):
                    stats.record_level_observations("chat", [(user, 0)])
                    climbed = stats.record_level_observations("chat", [(user, 9_000)])
                    high = stats._load_level_state("chat")["users"]["20"]

                # New quarter: everybody's season XP restarts at zero, so the watermark
                # must come down with it rather than freezing them at last season's peak.
                with patch("stats.app_now", return_value=datetime(2026, 10, 2, tzinfo=timezone.utc)):
                    reset = stats.record_level_observations("chat", [(user, 0)])
                    after = stats._load_level_state("chat")["users"]["20"]

        self.assertEqual(climbed, [])
        self.assertEqual(reset, [])
        self.assertEqual(high["season"], "2026-S3")
        self.assertGreater(high["chat_level"], 1)
        self.assertEqual(after["season"], "2026-S4")
        self.assertEqual(after["chat_level"], 1)

    def test_chat_levels_are_tracked_but_never_announced(self):
        """Deliberately silent: on the seasonal curve these come round every quarter for
        the same handful of people, and the level is always visible in /stat."""
        user = stats.UserStats(user_id="20", username="user", display_name="User")
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                stats.record_level_observations("chat", [(user, 0)])
                within_tier = stats.record_level_observations(
                    "chat", [(user, stats.chat_level_threshold(3))]
                )
                new_tier = stats.record_level_observations(
                    "chat", [(user, stats.chat_level_threshold(6))]
                )
                stored = stats._load_level_state("chat")["users"]["20"]

                # A painting rank still is announced -- it is all-time and rare.
                user.figurines_painted = 3
                rank_up = stats.record_level_observations("chat", [(user, 0)])

        self.assertEqual(within_tier, [])
        self.assertEqual(new_tier, [])
        # ...but the watermark keeps moving, so re-enabling the line needs no migration.
        self.assertEqual(stored["chat_level"], 6)
        self.assertEqual(rank_up, ["@user получил новое звание «⚪ Ученик грунта»! 🎉🎊🥳"])


class LevelTrackTests(unittest.TestCase):
    def test_chat_level_has_no_figurine_gate(self):
        # The exact case the split exists for: a prolific talker who has never painted.
        # 11.6k XP is a full season at the very top of the chat, so it maxes the ladder.
        talker = stats.chat_level(11_648)
        self.assertEqual(talker.number, stats.MAX_CHAT_LEVEL)
        self.assertIn("Хранитель чата", talker.label)

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


class DailyBonusTests(unittest.TestCase):
    """The one faucet that asks for nothing but showing up."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def test_an_unbroken_week_walks_up_the_table_and_then_stays_flat(self):
        day = date(2026, 8, 1)
        for offset, expected in enumerate(economy.DAILY_BONUS_BY_STREAK):
            claimed, amount, streak = economy.claim_daily_bonus(
                "chat", "1", day + timedelta(days=offset),
            )
            self.assertTrue(claimed)
            self.assertEqual(amount, expected)
            self.assertEqual(streak, offset + 1)

        # Day 8 and beyond stay at the top of the table rather than growing forever --
        # an unbounded streak would become the largest faucet in the game.
        top = economy.DAILY_BONUS_BY_STREAK[-1]
        claimed, amount, streak = economy.claim_daily_bonus(
            "chat", "1", day + timedelta(days=len(economy.DAILY_BONUS_BY_STREAK)),
        )
        self.assertTrue(claimed)
        self.assertEqual(amount, top)
        self.assertEqual(streak, len(economy.DAILY_BONUS_BY_STREAK) + 1)

    def test_a_missed_day_sends_the_streak_back_to_the_start(self):
        economy.claim_daily_bonus("chat", "1", date(2026, 8, 1))
        economy.claim_daily_bonus("chat", "1", date(2026, 8, 2))
        # 3 August skipped entirely.
        claimed, amount, streak = economy.claim_daily_bonus("chat", "1", date(2026, 8, 4))

        self.assertTrue(claimed)
        self.assertEqual(streak, 1)
        self.assertEqual(amount, economy.DAILY_BONUS_BY_STREAK[0])

    def test_a_second_claim_the_same_day_pays_nothing(self):
        day = date(2026, 8, 1)
        _, first, _ = economy.claim_daily_bonus("chat", "1", day)
        balance_after_first = economy.balance("chat", "1", 0)

        claimed, amount, _ = economy.claim_daily_bonus("chat", "1", day)

        self.assertFalse(claimed)
        self.assertEqual(amount, 0)
        self.assertEqual(economy.balance("chat", "1", 0), balance_after_first)
        self.assertEqual(balance_after_first, first)

    def test_status_promises_exactly_what_a_claim_then_pays(self):
        day = date(2026, 8, 1)
        for offset in range(4):
            moment = day + timedelta(days=offset)
            promised = economy.daily_bonus_status("chat", "1", moment)
            self.assertTrue(promised["can_claim"])
            claimed, amount, streak = economy.claim_daily_bonus("chat", "1", moment)
            self.assertTrue(claimed)
            self.assertEqual(amount, promised["amount"])
            self.assertEqual(streak, promised["next_streak"])

            settled = economy.daily_bonus_status("chat", "1", moment)
            self.assertFalse(settled["can_claim"])
            self.assertEqual(settled["streak"], streak)

    def test_a_clock_that_jumped_backwards_cannot_reclaim(self):
        """A `last` in the future must read as "already claimed", never as a broken
        streak that quietly pays again -- otherwise a timezone correction is a faucet."""
        economy.claim_daily_bonus("chat", "1", date(2026, 8, 10))
        funded = economy.balance("chat", "1", 0)

        status = economy.daily_bonus_status("chat", "1", date(2026, 8, 9))
        self.assertFalse(status["can_claim"])
        claimed, amount, _ = economy.claim_daily_bonus("chat", "1", date(2026, 8, 9))
        self.assertFalse(claimed)
        self.assertEqual(amount, 0)
        self.assertEqual(economy.balance("chat", "1", 0), funded)


class DailyChatterPrizeTests(unittest.TestCase):
    """Top three talkers of YESTERDAY, paid once."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)
        self.day = date(2026, 8, 1)

    def _record(self, counts: dict[int, int]):
        """Record one day in which each sender_id posted `counts[sender_id]` messages."""
        start = datetime(2026, 8, 1, 9, tzinfo=timezone.utc)
        messages, index = [], 0
        for sender_id, count in counts.items():
            for _ in range(count):
                index += 1
                messages.append(SimpleNamespace(
                    sender_id=sender_id, sender_name=f"U{sender_id}",
                    sender_username=f"u{sender_id}",
                    text="достаточно длинное сообщение для подсчёта",
                    dt_local=start + timedelta(minutes=index), message_id=index,
                    is_reply=False,
                ))
        stats.record_day("chat", self.day, messages)

    def test_the_three_loudest_are_paid_in_order_and_only_once(self):
        self._record({1: 30, 2: 20, 3: 10, 4: 5})

        paid = economy.daily_chatter_prizes("chat", self.day)

        self.assertEqual([row["user_id"] for row in paid], ["1", "2", "3"])
        self.assertEqual([row["amount"] for row in paid], list(economy.DAILY_CHATTER_PRIZES))
        self.assertEqual([row["place"] for row in paid], [1, 2, 3])
        for row in paid:
            self.assertEqual(economy.balance("chat", row["user_id"], 0), row["amount"])
        # Fourth place gets nothing at all.
        self.assertEqual(economy.balance("chat", "4", 0), 0)

        # Re-running the same day is a no-op: the loop that calls this runs hourly.
        self.assertEqual(economy.daily_chatter_prizes("chat", self.day), [])
        self.assertEqual(economy.balance("chat", "1", 0), economy.DAILY_CHATTER_PRIZES[0])

    def test_a_tie_is_broken_the_same_way_every_run(self):
        self._record({7: 10, 3: 10, 5: 10})
        first = [row["user_id"] for row in economy.daily_chatter_prizes("chat", self.day)]
        self.assertEqual(first, ["3", "5", "7"])

    def test_a_quiet_day_pays_only_the_people_who_actually_talked(self):
        self._record({1: 4})
        paid = economy.daily_chatter_prizes("chat", self.day)
        self.assertEqual([row["user_id"] for row in paid], ["1"])
        self.assertEqual(paid[0]["amount"], economy.DAILY_CHATTER_PRIZES[0])

    def test_an_unrecorded_day_pays_nobody_rather_than_raising(self):
        self.assertEqual(economy.daily_chatter_prizes("chat", date(2020, 1, 1)), [])


if __name__ == "__main__":
    unittest.main()
