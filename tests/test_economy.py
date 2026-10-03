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

    def test_admin_audit_splits_each_hour_by_source_and_keeps_casino_net_honest(self):
        times = iter([
            datetime(2026, 8, 12, 18, 5, tzinfo=timezone.utc),
            datetime(2026, 8, 12, 18, 10, tzinfo=timezone.utc),
            datetime(2026, 8, 12, 18, 12, tzinfo=timezone.utc),
            datetime(2026, 8, 12, 19, 1, tzinfo=timezone.utc),
        ])
        with patch("economy.app_now", side_effect=lambda: next(times)):
            economy.grant("chat", "1", 40, "grant:quest:submission-1")
            economy.grant("chat", "1", -10, "wager:casino:poker")
            economy.grant("chat", "1", 20, "wager_payout:casino:poker")
            economy.grant("chat", "1", 15, "pet_fight_win")

        report = economy.audit_report(
            "chat", "1", 24, now=datetime(2026, 8, 12, 19, 30, tzinfo=timezone.utc),
        )
        active = [row for row in report["hourly"] if row["transactions"]]
        self.assertEqual([row["label"] for row in active], ["12.08 18:00", "12.08 19:00"])
        self.assertEqual((report["earned"], report["spent"], report["net"]), (75, 10, 65))
        poker = next(row for row in report["sources"] if row["code"] == "casino_poker")
        self.assertEqual((poker["earned"], poker["spent"], poker["net"]), (20, 10, 10))
        self.assertEqual(report["transactions"][0]["source"], "pvp")
        self.assertTrue(report["xp_not_hourly"])

    def test_admin_audit_ignores_other_users_and_rejects_unbounded_windows(self):
        moment = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
        with patch("economy.app_now", return_value=moment):
            economy.grant("chat", "1", 10, "daily_bonus")
            economy.grant("chat", "2", 999, "pet_fight_win")
        report = economy.audit_report("chat", "1", 999_999, now=moment)
        self.assertEqual(report["hours"], 24)
        self.assertEqual(report["earned"], 10)
        self.assertEqual(economy.audit_user_ids("chat"), {"1", "2"})

    def test_idempotent_figurine_grants_stay_in_the_activity_audit_bucket(self):
        moment = datetime(2026, 8, 12, 18, 5, tzinfo=timezone.utc)
        with patch("economy.app_now", return_value=moment):
            self.assertTrue(economy.grant_once("chat", "1", 500, "figurine:777"))
            self.assertFalse(economy.grant_once("chat", "1", 500, "figurine:777"))

        report = economy.audit_report("chat", "1", 24, now=moment)
        activity = next(row for row in report["sources"] if row["code"] == "activity")
        self.assertEqual((activity["earned"], activity["net"]), (500, 500))

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
        user = stats.UserStats(user_id="1", active_days=65, messages=2_400)
        # Завсегдатай 2 and Собеседник 2.
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
        user = stats.UserStats(user_id="1", active_days=90, messages=5_000)
        extras = economy.stat_extras("chat", "1", 500, user)
        self.assertEqual(extras["reputation"], stats.medal_levels(user))
        # Without it, the same member scores only the peer-granted half.
        self.assertEqual(economy.stat_extras("chat", "1", 500)["reputation"], 0)

    def test_stat_extras_degrade_instead_of_breaking_stat(self):
        with patch("economy.balance", side_effect=OSError("disk gone")):
            self.assertEqual(economy.stat_extras("chat", "1", 500), {})


class PermanentLevelTests(unittest.TestCase):
    """The chat level is scored on all-time XP and never resets.

    It used to be scored on a calendar-quarter season, and the first boundary (1 October
    2026) dropped every member back to level 1-3 overnight -- what the chat reported as
    "levels broken". These pin that it cannot happen again.
    """

    def test_a_quarter_boundary_no_longer_resets_the_watermark(self):
        user = stats.UserStats(user_id="20", username="user", display_name="User")
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                with patch("stats.app_now", return_value=datetime(2026, 9, 30, tzinfo=timezone.utc)):
                    stats.record_level_observations("chat", [(user, 0)])
                    stats.record_level_observations("chat", [(user, 9_000)])
                    before = stats._load_level_state("chat")["users"]["20"]
                with patch("stats.app_now", return_value=datetime(2026, 10, 2, tzinfo=timezone.utc)):
                    stats.record_level_observations("chat", [(user, 9_050)])
                    after = stats._load_level_state("chat")["users"]["20"]

        self.assertGreater(before["chat_level"], 30)
        self.assertGreaterEqual(after["chat_level"], before["chat_level"])
        self.assertNotIn("season", after)

    def test_a_watermark_saved_with_a_season_is_still_read(self):
        """State written while seasons existed carries a "season" key. It must neither
        reset anybody nor announce anything when it is next compared."""
        user = stats.UserStats(user_id="20", username="user", display_name="User")
        saved = {
            "version": stats.LEVEL_STATE_VERSION,
            "users": {"20": {
                "chat_level": 35, "painter_figurines": 0, "season": "2026-S3",
            }},
        }
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                stats._write_json_atomic(stats._level_state_path("chat"), saved)
                announced = stats.record_level_observations("chat", [(user, 9_000)])
                after = stats._load_level_state("chat")["users"]["20"]

        self.assertEqual(announced, [])
        # Moved forward (a new name was reached), never back to the start of a season.
        self.assertEqual(after["chat_level"], stats.chat_level(9_000).number)

    def test_the_first_forty_levels_cost_what_they_always_did(self):
        """Same curve the seasonal ladder used, so dropping the season lowered nobody:
        all-time XP is never below one quarter's XP."""
        for number in range(2, 41):
            self.assertEqual(stats.chat_level_threshold(number), int(25 * number ** 1.6))
        # The calibration target still holds: three months at the p95 rate is level 40.
        self.assertEqual(stats.chat_level(103 * 90).number, 40)

    def test_the_ladder_has_no_top(self):
        top = stats.chat_level(10**7)
        self.assertGreater(top.number, 40)
        self.assertGreater(top.next_threshold, 10**7)
        self.assertLess(stats.chat_level_progress(10**7), 100)
        # The last name is reached eventually; the number keeps counting past it.
        self.assertIn("Вечный", top.label)

    def test_the_solved_level_matches_walking_the_ladder(self):
        def walked(xp):
            number = 1
            while stats.chat_level_threshold(number + 1) <= xp:
                number += 1
            return number

        for xp in list(range(0, 60_000, 37)) + [
            stats.chat_level_threshold(n) + delta for n in range(2, 150) for delta in (-1, 0, 1)
        ]:
            with self.subTest(xp=xp):
                self.assertEqual(stats.chat_level(xp).number, walked(xp))

    def test_negative_xp_is_level_one(self):
        self.assertEqual(stats.chat_level(-500).number, 1)


    def test_chat_levels_are_tracked_but_never_announced(self):
        """Deliberately silent: they are frequent, they come from the same handful of
        people, and the level is always visible in /stat."""
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
        talker = stats.chat_level(11_648)
        self.assertGreaterEqual(talker.number, 40)
        self.assertIn("Столп чата", talker.label)

        # ...and a prolific painter who barely talks still ranks on the craft track.
        rank, _ = stats.painter_rank(50)
        self.assertEqual(rank.name, "Легенда покраса")

    def test_chat_level_curve_is_strictly_increasing(self):
        thresholds = [stats.chat_level_threshold(n) for n in range(1, 500)]
        self.assertEqual(thresholds[0], 0)
        self.assertTrue(all(a < b for a, b in zip(thresholds, thresholds[1:])))

    def test_the_first_eight_names_keep_their_old_bands(self):
        """The ladder was extended upward only: up to level 45 every member reads the
        same name they read before, so adding names lowered nobody's."""
        old_names = [name for _, _, name in stats.CHAT_LEVEL_TIERS[:8]]
        for number in range(1, 46):
            with self.subTest(level=number):
                expected = old_names[min((number - 1) // 5, 7)]
                self.assertEqual(
                    stats.chat_level(stats.chat_level_threshold(number)).tier_name, expected
                )

    def test_names_widen_and_never_repeat(self):
        starts = [first for first, _, _ in stats.CHAT_LEVEL_TIERS]
        self.assertEqual(starts[0], 1)
        self.assertEqual(starts, sorted(set(starts)))
        gaps = [b - a for a, b in zip(starts[7:], starts[8:])]
        self.assertEqual(gaps, sorted(gaps), "bands above 36 must only get wider")
        names = [name for _, _, name in stats.CHAT_LEVEL_TIERS]
        self.assertEqual(len(names), len(set(names)))

    def test_veterans_still_have_names_ahead_of_them(self):
        """The reason for the extension: at the p95 rate (~103 XP/day) the last name
        used to arrive within three months and never change again."""
        a_year = stats.chat_level(103 * 365)
        self.assertLess(
            stats._chat_tier_index(a_year.number), len(stats.CHAT_LEVEL_TIERS) - 3,
        )
        # ...while the busiest member in the chat gets there in about two years.
        self.assertIn("Вечный", stats.chat_level(299 * 730).label)

    def test_painter_ranks_keep_their_old_steps_and_continue_past_fifty(self):
        old = [(0, "Серый новичок"), (3, "Ученик грунта"), (5, "Подмастерье кисти"),
               (10, "Укротитель аэрографа"), (20, "Повелитель проливок"),
               (35, "Мастер витрины"), (50, "Легенда покраса")]
        self.assertEqual([(m, n) for m, _, n in stats.PAINTER_RANKS[:7]], old)
        rank, next_rank = stats.painter_rank(50)
        self.assertEqual(next_rank.name, "Магистр лессировок")
        top, nothing = stats.painter_rank(10_000)
        self.assertEqual(top.name, "Бессмертная кисть")
        self.assertIsNone(nothing)

    def test_a_painter_already_past_the_old_top_is_announced_once(self):
        user = stats.UserStats(
            user_id="20", username="user", display_name="User", figurines_painted=80,
        )
        stored = {"version": stats.LEVEL_STATE_VERSION,
                  "users": {"20": {"chat_level": 1, "painter_figurines": 50}}}
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                stats._write_json_atomic(stats._level_state_path("chat"), stored)
                first = stats.record_level_observations("chat", [(user, 0)])
                again = stats.record_level_observations("chat", [(user, 0)])

        self.assertEqual(first, ["@user получил новое звание «✨ Магистр лессировок»! 🎉🎊🥳"])
        self.assertEqual(again, [])

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


# Any fixed calibration works here: what matters is that the SAME one is used to rank
# everybody, which is exactly why economy.daily_chatter_prizes demands it as an argument
# instead of guessing one.
WPP = 5.0


class DailyChatterPrizeTests(unittest.TestCase):
    """Top three earners of YESTERDAY, paid once, ranked the way the tree ranks."""

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

        paid = economy.daily_chatter_prizes("chat", self.day, WPP)

        self.assertEqual([row["user_id"] for row in paid], ["1", "2", "3"])
        self.assertEqual([row["amount"] for row in paid], list(economy.DAILY_CHATTER_PRIZES))
        self.assertEqual([row["place"] for row in paid], [1, 2, 3])
        for row in paid:
            self.assertEqual(economy.balance("chat", row["user_id"], 0), row["amount"])
        # Fourth place gets nothing at all.
        self.assertEqual(economy.balance("chat", "4", 0), 0)

        # Re-running the same day is a no-op: the loop that calls this runs hourly.
        self.assertEqual(economy.daily_chatter_prizes("chat", self.day, WPP), [])
        self.assertEqual(economy.balance("chat", "1", 0), economy.DAILY_CHATTER_PRIZES[0])

    def test_ranking_follows_earned_xp_rather_than_the_raw_message_count(self):
        """The distinguishing case, and the reason this was changed.

        One member posts many one-word messages, another posts a few substantial ones.
        Counting messages crowns the first; counting XP -- the same figure the ЕПХ tree
        reports for the same day -- crowns the second.
        """
        start = datetime(2026, 8, 1, 9, tzinfo=timezone.utc)
        messages, index = [], 0
        for sender_id, count, text in (
            (1, 30, "ага"),
            (2, 6, "а вот с металликами у меня совсем другая история получилась " * 6),
        ):
            for _ in range(count):
                index += 1
                messages.append(SimpleNamespace(
                    sender_id=sender_id, sender_name=f"U{sender_id}",
                    sender_username=f"u{sender_id}", text=text,
                    dt_local=start + timedelta(minutes=index), message_id=index,
                    is_reply=False,
                ))
        stats.record_day("chat", self.day, messages)

        paid = economy.daily_chatter_prizes("chat", self.day, WPP)

        self.assertEqual([row["user_id"] for row in paid], ["2", "1"])
        self.assertGreater(paid[0]["xp"], paid[1]["xp"])
        # The loud one really did send more messages -- this is not an accident of setup.
        self.assertLess(paid[0]["messages"], paid[1]["messages"])

    def test_the_prize_ranks_a_day_exactly_as_the_tree_ranks_it(self):
        """One implementation, so the morning tree post and the prize cannot disagree."""
        self._record({1: 30, 2: 20, 3: 10})
        ranked = stats.day_xp_ranking("chat", self.day, WPP)

        paid = economy.daily_chatter_prizes("chat", self.day, WPP)

        self.assertEqual(
            [row["user_id"] for row in paid],
            [user_id for user_id, _, _ in ranked[:len(economy.DAILY_CHATTER_PRIZES)]],
        )
        self.assertEqual([row["xp"] for row in paid], [xp for _, _, xp in ranked[:3]])

    def test_a_tie_is_broken_the_same_way_every_run(self):
        self._record({7: 10, 3: 10, 5: 10})
        first = [row["user_id"] for row in economy.daily_chatter_prizes("chat", self.day, WPP)]
        self.assertEqual(first, ["3", "5", "7"])

    def test_a_quiet_day_pays_only_the_people_who_actually_talked(self):
        self._record({1: 4})
        paid = economy.daily_chatter_prizes("chat", self.day, WPP)
        self.assertEqual([row["user_id"] for row in paid], ["1"])
        self.assertEqual(paid[0]["amount"], economy.DAILY_CHATTER_PRIZES[0])

    def test_an_unrecorded_day_pays_nobody_rather_than_raising(self):
        self.assertEqual(economy.daily_chatter_prizes("chat", date(2020, 1, 1), WPP), [])


if __name__ == "__main__":
    unittest.main()
