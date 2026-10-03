import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot_listener
import stats


class GamificationTests(unittest.TestCase):
    def test_one_time_xp_grant_persists_and_uses_normal_coin_conversion(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)), patch(
                "stats.app_now", return_value=datetime(2026, 8, 14, tzinfo=timezone.utc),
            ):
                self.assertTrue(stats.grant_xp_once(
                    "chat", "42", 10_000_000, "admin-grant",
                    username="london_leads", display_name="London Leads",
                ))
                self.assertFalse(stats.grant_xp_once(
                    "chat", "42", 10_000_000, "admin-grant",
                ))
                user = stats.aggregate_all_time("chat")["42"]

        self.assertEqual(user.xp(5.0), 10_000_000)
        self.assertEqual(stats.coins_for_xp(user.xp(5.0)), 2_000_000)
        self.assertEqual(user.username, "london_leads")

    def test_xp_coins_and_levels(self):
        user = stats.UserStats(
            user_id="1",
            legacy_message_points=900,
            media=20,
            replies=25,
            active_days=11,
        )
        self.assertEqual(user.xp(5.0), 1_000)
        # Stated against the rate rather than a literal: XP_PER_COIN is a balance knob
        # (halved to 5 to lift the floor for members who only chat), and what is being
        # pinned here is the floor-division contract, not the rate of the day.
        self.assertEqual(stats.coins_for_xp(stats.XP_PER_COIN * 101 - 1), 100)
        self.assertEqual(stats.coins_for_xp(stats.XP_PER_COIN * 101), 101)
        self.assertEqual(stats.coins_for_xp(stats.XP_PER_COIN - 1), 0)

    def test_painter_rank_moves_on_figurines_alone(self):
        for figurines, label in (
            (0, "🩶 Серый новичок"),
            (2, "🩶 Серый новичок"),
            (3, "⚪ Ученик грунта"),
            (5, "🖌️ Подмастерье кисти"),
            (10, "💨 Укротитель аэрографа"),
            (20, "💧 Повелитель проливок"),
            (35, "🏛️ Мастер витрины"),
        ):
            with self.subTest(figurines=figurines):
                self.assertEqual(stats.painter_rank(figurines)[0].label, label)
        legend, next_rank = stats.painter_rank(50)
        self.assertEqual(legend.label, "👑 Легенда покраса")
        self.assertEqual(next_rank.label, "✨ Магистр лессировок")
        final_rank, no_more = stats.painter_rank(500)
        self.assertEqual(final_rank.label, "♾️ Бессмертная кисть")
        self.assertIsNone(no_more)

    def test_only_two_automatic_badges_exist(self):
        """Everything else the old dozen read is still recorded, just no longer shown."""
        first_day = date(2026, 1, 1)
        user = stats.UserStats(
            user_id="1",
            figurines_painted=50,
            messages=1_000,
            media=25,
            replies=100,
            active_days=30,
            active_day_dates={(first_day + timedelta(days=n)).isoformat() for n in range(30)},
            hours={str(hour): 300 for hour in range(6)},
            not_gay_hashtag_uses=1,
            weekly_contest_weeks={"2026-W30"},
        )

        self.assertEqual(
            [(badge.badge_id, badge.label) for badge in stats.earned_badges(user)],
            [("active_days", "📅 Завсегдатай 1"), ("messages", "💬 Собеседник 1")],
        )

    def test_active_days_badge_levels_every_thirty_days(self):
        for days, expected in ((0, None), (29, None), (30, 1), (59, 1), (60, 2), (365, 12)):
            with self.subTest(days=days):
                badges = stats.earned_badges(stats.UserStats(user_id="1", active_days=days))
                self.assertEqual(
                    [badge.name for badge in badges],
                    [] if expected is None else [f"Завсегдатай {expected}"],
                )

    def test_messages_badge_levels_every_thousand_messages(self):
        for messages, expected in ((999, None), (1_000, 1), (1_999, 1), (12_345, 12)):
            with self.subTest(messages=messages):
                badges = stats.earned_badges(stats.UserStats(user_id="1", messages=messages))
                self.assertEqual(
                    [badge.name for badge in badges],
                    [] if expected is None else [f"Собеседник {expected}"],
                )
        twelve = stats.earned_badges(stats.UserStats(user_id="1", messages=12_345))[0]
        self.assertIn("12.000+ сообщений", twelve.description)

    def test_hashtag_counters_are_still_derived_from_messages(self):
        """No badge reads these any more, but day files keep recording them, so bringing
        one back is a display change rather than a re-scan of the chat."""
        def message(moment, text, message_id):
            return SimpleNamespace(
                sender_id=20,
                sender_name="User",
                sender_username="user",
                text=text,
                dt_local=moment,
                message_id=message_id,
                is_reply=False,
            )

        day_one = stats.compute_day_stats(
            [
                message(datetime(2026, 7, 20, 12, tzinfo=timezone.utc), "#ЯНеПидор", 1),
                message(datetime(2026, 7, 20, 13, tzinfo=timezone.utc), "#итогинедели", 2),
                message(datetime(2026, 7, 21, 13, tzinfo=timezone.utc), "#ИТОГИНЕДЕЛИ ещё раз", 3),
            ]
        )
        day_two = stats.compute_day_stats(
            [message(datetime(2026, 7, 27, 13, tzinfo=timezone.utc), "#итогинедели", 4)]
        )
        combined = {}
        stats._merge_day(combined, {"day": "2026-07-20", "users": day_one})
        stats._merge_day(combined, {"day": "2026-07-27", "users": day_two})
        user = combined["20"]

        self.assertEqual(user.not_gay_hashtag_uses, 1)
        self.assertEqual(user.weekly_contest_weeks, {"2026-W30", "2026-W31"})

    def test_showcase_hashtags_are_tracked_as_linkable_posts(self):
        def message(moment, text, message_id):
            return SimpleNamespace(
                sender_id=20,
                sender_name="User",
                sender_username="user",
                text=text,
                dt_local=moment,
                message_id=message_id,
                is_reply=False,
            )

        day_one = stats.compute_day_stats(
            [
                message(datetime(2026, 7, 15, 12, tzinfo=timezone.utc), "[Photo] #МояЛучшая", 1),
                message(datetime(2026, 7, 15, 13, tzinfo=timezone.utc), "[Photo] #рабочееместо", 2),
                # The organizer's text-only announcement: carries the tag, has no photo,
                # and must never become somebody's "best work" link.
                message(datetime(2026, 7, 15, 14, tzinfo=timezone.utc), "Показываем #моялучшая", 3),
                # A longer lookalike tag is a different tag.
                message(datetime(2026, 7, 15, 15, tzinfo=timezone.utc), "[Photo] #моялучшаяработа", 4),
            ]
        )
        # The underscored spelling is a genuinely different Telegram tag, and a newer
        # post of either supersedes the older one in /stat.
        day_two = stats.compute_day_stats(
            [message(datetime(2026, 7, 22, 9, tzinfo=timezone.utc), "[Video] #рабочее_место", 5)]
        )

        combined = {}
        stats._merge_day(combined, {"day": "2026-07-15", "users": day_one})
        stats._merge_day(combined, {"day": "2026-07-22", "users": day_two})
        user = combined["20"]

        self.assertEqual([post[1] for post in user.best_work_posts], [1])
        self.assertEqual([post[1] for post in user.workplace_posts], [5, 2])
        # Showcase posts are not figurines and must not silently earn figurine XP.
        self.assertEqual(user.figurines_painted, 0)

        best_link, workplace_link = stats.showcase_message_links("example", None, user)
        self.assertEqual(best_link, "https://t.me/example/1")
        self.assertEqual(workplace_link, "https://t.me/example/5")

    def test_reposting_a_showcase_tag_supersedes_the_previous_link(self):
        def message(moment, text, message_id):
            return SimpleNamespace(
                sender_id=20,
                sender_name="User",
                sender_username="user",
                text=text,
                dt_local=moment,
                message_id=message_id,
                is_reply=False,
            )

        # Two workplace posts on the SAME day, plus a third on a later day using the
        # other spelling: /stat must always show the most recent one.
        same_day = stats.compute_day_stats(
            [
                message(datetime(2026, 7, 20, 9, tzinfo=timezone.utc), "[Photo] #рабочееместо", 10),
                message(datetime(2026, 7, 20, 18, tzinfo=timezone.utc), "[Photo] #рабочееместо переставил стол", 11),
            ]
        )
        combined = {}
        stats._merge_day(combined, {"day": "2026-07-20", "users": same_day})
        _, workplace_link = stats.showcase_message_links("example", None, combined["20"])
        self.assertEqual(workplace_link, "https://t.me/example/11")

        later_day = stats.compute_day_stats(
            [message(datetime(2026, 7, 24, 8, tzinfo=timezone.utc), "[Photo] #рабочее_место", 12)]
        )
        stats._merge_day(combined, {"day": "2026-07-24", "users": later_day})
        user = combined["20"]
        _, workplace_link = stats.showcase_message_links("example", None, user)
        self.assertEqual(workplace_link, "https://t.me/example/12")
        # Superseded posts are still retained, newest-first, so the display choice stays
        # reversible without re-scanning any transcripts.
        self.assertEqual([post[1] for post in user.workplace_posts], [12, 11, 10])

        # Re-merging an already-merged day (a backfill re-visit, or a live overlay of a
        # day the transcript cache has since caught up on) must not resurrect an older
        # post or duplicate the current one.
        stats._merge_day(combined, {"day": "2026-07-20", "users": same_day})
        _, workplace_link = stats.showcase_message_links("example", None, combined["20"])
        self.assertEqual(workplace_link, "https://t.me/example/12")
        self.assertEqual([post[1] for post in combined["20"].workplace_posts], [12, 11, 10])

    def test_showcase_links_render_before_the_figurine_line(self):
        user = stats.UserStats(user_id="1", display_name="Tester", figurines_painted=3)
        text = stats.format_stat(
            user,
            rank=1,
            total=1,
            xp=100,
            streak=0,
            best_work_link="https://t.me/example/7",
            workplace_link="https://t.me/example/8",
        )

        # Both ride on the figurine line as links named for what they are.
        self.assertIn(
            "🖼️ Фигурок: 3 (#япокрасил)"
            ' · <a href="https://t.me/example/8">🛠️ Рабочее место</a>'
            ' · <a href="https://t.me/example/7">💎 Моя лучшая</a>',
            text,
        )

        without = stats.format_stat(user, rank=1, total=1, xp=100, streak=0)
        self.assertNotIn("Рабочее место", without)
        self.assertNotIn("Моя лучшая", without)

    def test_showcase_backfill_reaches_days_recorded_under_the_previous_schema(self):
        message = SimpleNamespace(
            sender_id=20,
            sender_name="User",
            sender_username="user",
            text="[Photo] #моялучшая",
            dt_local=datetime(2026, 7, 15, 12, tzinfo=timezone.utc),
            message_id=42,
            is_reply=False,
        )
        payload = {
            "badge_stats_schema_version": 1,
            "entry": "chat",
            "day": "2026-07-15",
            "users": {"20": {"display_name": "User", "messages": 99, "media": 7}},
        }

        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                backfilled = stats._backfill_day_badge_stats(
                    "chat", date(2026, 7, 15), payload, [message], log=lambda _: None
                )
                saved = stats._load_day("chat", date(2026, 7, 15))

        self.assertTrue(backfilled)
        self.assertEqual(saved["users"]["20"]["best_work_posts"], [["2026-07-15T12:00:00+00:00", 42]])
        self.assertEqual(saved["users"]["20"]["workplace_posts"], [])
        # The whole point of the backfill: existing XP inputs stay byte-for-byte intact.
        self.assertEqual(saved["users"]["20"]["messages"], 99)
        self.assertEqual(saved["users"]["20"]["media"], 7)

    def test_badges_are_immediately_before_clickable_works(self):
        user = stats.UserStats(user_id="1", display_name="Tester")
        custom = stats.Badge("custom", "🏹", "Лучник", custom=True)
        text = stats.format_stat(
            user,
            rank=1,
            total=1,
            xp=1_234,
            streak=0,
            figurine_links=["https://t.me/example/1"],
            custom_badges=[custom],
        )

        # XP and coins share one line. The coin figure is derived from the rate, not
        # written out, so re-tuning XP_PER_COIN does not have to be re-typed here.
        self.assertIn(f"⭐️ XP: 1.234 · 🪙 Монеты: {stats.coins_for_xp(1_234)}", text)
        # Chat level moves on all-time XP alone; the painting rank is its own track.
        self.assertIn("🧩 Уровень: 🗣️ Голос чата 11", text)
        self.assertIn("🎨 Звание: 🩶 Серый новичок", text)
        self.assertNotIn("До уровня", text)
        # A hand-made badge has its own line, above the works.
        self.assertLess(text.index("✨ Уникальные значки:"), text.index("🎨 Работы"))
        self.assertIn("🏹 Лучник", text)
        self.assertIn('<a href="https://t.me/example/1">1</a>', text)

    def test_stat_is_compact_and_badges_share_one_line(self):
        custom = [
            stats.Badge(f"custom-{number}", emoji, name, custom=True)
            for number, (emoji, name) in enumerate(
                [
                    ("🏹", "Лучник"),
                    ("🎯", "Меткий глаз"),
                    ("🛡️", "Защитник"),
                ],
                start=1,
            )
        ]
        text = stats.format_stat(
            stats.UserStats(
                user_id="1", display_name="Tester", messages=2_000, active_days=31,
                hours={"21": 5},
            ),
            rank=1,
            total=1,
            xp=0,
            streak=0,
            custom_badges=custom,
        )

        # The name is the header; there is no separate "Имя:" line.
        self.assertIn("📊 Статистика Tester:\n\n⭐️ XP:", text)
        self.assertNotIn("Имя:", text)
        self.assertIn("Пока тихо)\n\n🖼️ Фигурок:", text)
        self.assertIn("💬 Сообщений: 2.000 (64.5 в день) · 🕘 21:00–22:00", text)
        self.assertNotIn("Любимое время", text)
        self.assertIn("✨ Уникальные значки: 🏹 Лучник · 🎯 Меткий глаз · 🛡️ Защитник\n", text)
        self.assertIn("🏅 Значки: 📅 Завсегдатай 1 · 💬 Собеседник 2", text)
        # One line per fact: a stat card for a member with everything is still short.
        self.assertLessEqual(len(text.splitlines()), 16)

    def test_stat_shows_work_names_but_keeps_the_numbers(self):
        user = stats.UserStats(user_id="1", display_name="T", figurines_painted=3)
        links = [f"https://t.me/example/{n}" for n in (105, 104, 103)]

        text = stats.format_stat(
            user, rank=1, total=1, xp=0, streak=0,
            figurine_links=links, work_names=[None, "Дредноут", None],
        )

        self.assertIn(">2. Дредноут</a>", text)
        # The number must survive: /deletepokras takes it as its argument, so an
        # administrator needs something to point at.
        self.assertIn(">1</a>", text)
        self.assertIn(">3</a>", text)

    def test_work_names_are_escaped_and_a_short_list_is_tolerated(self):
        user = stats.UserStats(user_id="1", display_name="T", figurines_painted=2)
        links = ["https://t.me/example/105", "https://t.me/example/104"]

        # Fewer names than links must not raise -- the two lists are built from the same
        # source, but a stale cache could briefly disagree.
        text = stats.format_stat(
            user, rank=1, total=1, xp=0, streak=0,
            figurine_links=links, work_names=["<b>hax</b>"],
        )

        self.assertNotIn("<b>hax", text)
        self.assertIn("&lt;b&gt;hax", text)
        self.assertIn(">2</a>", text)

    def test_work_name_list_lines_up_with_the_links(self):
        user = stats.UserStats(
            user_id="1",
            recent_figurine_posts=[["t3", 105], ["t2", 104], ["t1", 103]],
        )
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                stats.set_work_name("chat", "1", 104, "Дредноут")
                labels = stats.work_name_list("chat", user)

        self.assertEqual(labels, [None, "Дредноут", None])

    def test_unique_badges_lead_and_earned_ones_follow(self):
        user = stats.UserStats(user_id="1", display_name="Tester", messages=1_000)
        unique = stats.Badge("custom", "🏹", "Лучник", custom=True)
        # Assigned by an administrator, but won -- it belongs with the earned ones.
        won = stats.Badge("weekly_contest_winner", "🏆", "Победитель ×1")

        text = stats.format_stat(
            user, rank=1, total=1, xp=0, streak=0, custom_badges=[unique, won]
        )

        self.assertLess(text.index("✨ Уникальные значки:"), text.index("🏅 Значки:"))
        self.assertLess(text.index("🏹 Лучник"), text.index("🏅 Значки:"))
        self.assertGreater(text.index("🏆 Победитель ×1"), text.index("🏅 Значки:"))

    def test_the_no_badges_notice_only_shows_when_there_are_truly_none(self):
        blank = stats.format_stat(
            stats.UserStats(user_id="1", display_name="T"), rank=1, total=1, xp=0, streak=0
        )
        self.assertIn("🏅 Значки: пока нет", blank)

        # A unique badge alone must not read as "no badges yet".
        unique = stats.Badge("custom", "🏹", "Лучник", custom=True)
        only_unique = stats.format_stat(
            stats.UserStats(user_id="1", display_name="T"),
            rank=1, total=1, xp=0, streak=0, custom_badges=[unique],
        )
        self.assertNotIn("пока нет", only_unique)
        self.assertIn("✨ Уникальные значки:", only_unique)

    def test_the_cabinet_link_is_last_and_omitted_without_a_bot(self):
        user = stats.UserStats(user_id="1", display_name="Tester", figurines_painted=1)

        linked = stats.format_stat(
            user, rank=1, total=1, xp=0, streak=0,
            figurine_links=["https://t.me/example/1"], bot_username="Trash_Modelist",
        )
        self.assertIn("t.me/Trash_Modelist?start=cabinet", linked)
        # Truly last, below even the works list.
        self.assertGreater(linked.index("Открыть личный кабинет"), linked.index("🎨 Работы"))
        self.assertTrue(linked.rstrip().endswith("</a>"))

        # Without a bot to link to there is no cabinet line at all.
        self.assertNotIn("Открыть личный кабинет", stats.format_stat(
            user, rank=1, total=1, xp=0, streak=0
        ))

    def test_every_tracked_figurine_post_gets_a_link(self):
        user = stats.UserStats(
            user_id="1",
            recent_figurine_posts=[
                ["2026-07-05T12:00:00", 105],
                ["2026-07-04T12:00:00", 104],
                ["2026-07-03T12:00:00", 103],
                ["2026-07-02T12:00:00", 102],
                ["2026-07-01T12:00:00", 101],
            ],
        )

        links = stats.figurine_message_links("example", -1001, user)
        text = stats.format_stat(user, rank=1, total=1, xp=0, streak=0, figurine_links=links)

        self.assertEqual(len(links), 5)
        for number, message_id in enumerate(range(105, 100, -1), start=1):
            self.assertIn(
                f'<a href="https://t.me/example/{message_id}">{number}</a>',
                text,
            )

    def test_deleted_figurine_tombstone_removes_link_count_and_xp(self):
        payload = {
            "entry": "chat",
            "day": "2026-07-01",
            "users": {
                "20": {
                    "username": "user",
                    "display_name": "User",
                    "messages": 3,
                    "chars": 30,
                    "words": 15,
                    "media": 3,
                    "replies": 0,
                    "figurines": 3,
                    "not_gay_hashtag_uses": 0,
                    "weekly_contest_weeks": [],
                    "figurine_posts": [
                        ["2026-07-01T15:00:00", 303],
                        ["2026-07-01T14:00:00", 302],
                        ["2026-07-01T13:00:00", 301],
                    ],
                    "hours": {"13": 1, "14": 1, "15": 1},
                    "last_message_at": "2026-07-01T15:00:00",
                }
            },
        }

        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                stats._write_json_atomic(stats._path("chat", date(2026, 7, 1)), payload)
                before = stats.aggregate_all_time("chat")["20"]
                created = stats.delete_figurine_submission("chat", "20", 302, "10", "Admin")
                duplicate = stats.delete_figurine_submission("chat", "20", 302, "10", "Admin")
                after = stats.aggregate_all_time("chat")["20"]

        self.assertTrue(created)
        self.assertFalse(duplicate)
        self.assertEqual(before.figurines_painted, 3)
        self.assertEqual(after.figurines_painted, 2)
        self.assertEqual([post[1] for post in after.recent_figurine_posts], [303, 301])
        self.assertEqual(before.xp(5.0) - after.xp(5.0), stats.XP_PER_FIGURINE)

    def test_stat_html_escapes_user_controlled_fields(self):
        user = stats.UserStats(user_id="1", display_name="<Painter & Friend>")
        custom = stats.Badge("custom", "🏹", "A < B & C", custom=True)

        text = stats.format_stat(
            user,
            rank=1,
            total=1,
            xp=0,
            streak=0,
            custom_badges=[custom],
        )

        self.assertIn("📊 Статистика &lt;Painter &amp; Friend&gt;:", text)
        self.assertIn("🏹 A &lt; B &amp; C", text)

    def test_stat_hides_next_level_requirements(self):
        text = stats.format_stat(
            stats.UserStats(user_id="1", figurines_painted=3),
            rank=1,
            total=1,
            xp=2_000,
            streak=0,
        )

        self.assertNotIn("До уровня", text)
        self.assertNotIn("фигурки", text)

    def test_stat_uses_compact_activity_lines(self):
        user = stats.UserStats(
            user_id="1",
            display_name="Tester",
            messages=1_842,
            active_days=96,
            figurines_painted=12,
        )
        text = stats.format_stat(user, rank=1, total=1, xp=12_480, streak=11)

        self.assertIn("🖼️ Фигурок: 12 (#япокрасил)", text)
        self.assertIn("📅 Активных дней: 96 · 🔥 Серия: 11 дней", text)
        self.assertIn("💬 Сообщений: 1.842 (19.2 в день)", text)
        self.assertNotIn("Среднее сообщений в день:", text)
        self.assertNotIn("+200 XP за фигурку", text)

        without_streak = stats.format_stat(user, rank=1, total=1, xp=12_480, streak=0)
        self.assertIn("📅 Активных дней: 96\n", without_streak)
        self.assertNotIn("Серия:", without_streak)

    def test_level_announcements_are_persistent_and_emit_once(self):
        user = stats.UserStats(
            user_id="20",
            username="user",
            display_name="User",
            figurines_painted=0,
        )
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                # First sighting only baselines -- a deployment must never announce a
                # chat's entire back catalogue of levels.
                self.assertEqual(stats.record_level_observations("chat", [(user, 0)]), [])

                # The chat level moves on XP alone, but is deliberately NOT announced.
                chat_promotion = stats.record_level_observations("chat", [(user, 2_500)])
                repeated = stats.record_level_observations("chat", [(user, 2_500)])

                # ...and the painting rank moves on figurines alone, independently.
                user.figurines_painted = 3
                painter_promotion = stats.record_level_observations("chat", [(user, 2_500)])

        self.assertEqual(chat_promotion, [])
        self.assertEqual(repeated, [])
        self.assertEqual(
            painter_promotion,
            ["@user получил новое звание «⚪ Ученик грунта»! 🎉🎊🥳"],
        )

    def test_retired_level_state_is_rebaselined_instead_of_re_announced(self):
        user = stats.UserStats(user_id="20", username="user", display_name="User")
        # What a pre-split deployment left on disk: a watermark from a ladder that no
        # longer exists. Comparing new track positions against it would fire a promotion
        # for effectively every member at once.
        stale = {
            "version": 1,
            "users": {"20": {"minimum_xp": 2_500, "level_name": "Ученик грунта"}},
        }
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                stats._write_json_atomic(stats._level_state_path("chat"), stale)
                first = stats.record_level_observations("chat", [(user, 30_000)])
                second = stats.record_level_observations("chat", [(user, 30_000)])

        self.assertEqual(first, [])
        self.assertEqual(second, [])

    def test_a_hand_granted_badge_stacks_and_only_shows_a_multiplier_past_one(self):
        """
        Winning the same thing twice is worth seeing on the card.

        Giving a badge to somebody who already held it used to do nothing at all, so the
        second week somebody won the contest looked exactly like the first. It now counts,
        and the count is written on the badge -- but only once there is more than one of
        them, because "×1" is not a thing anybody says.
        """
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                emoji, name = stats.parse_custom_badge_spec("🏆 Победитель")
                badge = stats.create_custom_badge("chat", emoji, name, 10, "Admin")

                def label():
                    return stats.custom_badges_for_user("chat", 20)[0].label

                for expected_count in (1, 2, 3):
                    _, changed, count = stats.give_custom_badge(
                        "chat", badge.badge_id, 20, "User", 10, "Admin", stack=True,
                    )
                    self.assertTrue(changed)
                    self.assertEqual(count, expected_count)
                self.assertEqual(label(), "🏆 Победитель ×3")

                # Exactly one badge, not three copies of it in the list.
                self.assertEqual(len(stats.custom_badges_for_user("chat", 20)), 1)

                # Revoking takes ONE off, so an accidental extra award is fixable with the
                # same number of presses that caused it.
                stats.revoke_custom_badge("chat", badge.badge_id, 20)
                self.assertEqual(label(), "🏆 Победитель ×2")
                stats.revoke_custom_badge("chat", badge.badge_id, 20)
                self.assertEqual(label(), "🏆 Победитель")
                stats.revoke_custom_badge("chat", badge.badge_id, 20)
                self.assertEqual(stats.custom_badges_for_user("chat", 20), [])
                self.assertIsNone(
                    stats.revoke_custom_badge("chat", badge.badge_id, 20)
                )

    def test_an_automatic_badge_award_never_stacks(self):
        """
        Only a human pressing «Выдать значок» means "another one".

        Two callers award badges on their own: a quest hands one out on completion, and
        `award_founder_badges` re-runs on every retried ceremony post. If those stacked, a
        repeatable quest would inflate its badge forever and one retried post would give
        the whole guest list a founder badge ×2. The default is what protects them, so the
        default is what this pins.
        """
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                emoji, name = stats.parse_custom_badge_spec("🌱 Основатель")
                badge = stats.create_custom_badge("chat", emoji, name, 10, "Admin")
                for _ in range(3):
                    _, changed, count = stats.give_custom_badge(
                        "chat", badge.badge_id, 20, "User", 10, "Admin",
                    )
                self.assertFalse(changed)
                self.assertEqual(count, 1)
                self.assertEqual(
                    stats.custom_badges_for_user("chat", 20)[0].label, "🌱 Основатель"
                )

    def test_custom_badges_persist_and_duplicate_awards_are_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                emoji, name = stats.parse_custom_badge_spec("🎯 Меткий глаз")
                badge = stats.create_custom_badge("chat", emoji, name, 10, "Admin")
                awarded, created, count = stats.give_custom_badge(
                    "chat", badge.badge_id, 20, "User", 10, "Admin",
                )
                self.assertEqual(count, 1)
                awarded_again, created_again, _count_again = stats.give_custom_badge(
                    "chat", badge.badge_id, 20, "User", 10, "Admin"
                )

                self.assertTrue(created)
                self.assertFalse(created_again)
                self.assertEqual(awarded.label, "🎯 Меткий глаз")
                self.assertEqual(awarded_again.badge_id, badge.badge_id)
                self.assertEqual(
                    [item.badge_id for item in stats.custom_badges_for_user("chat", 20)],
                    [badge.badge_id],
                )

    def test_renaming_a_custom_badge_keeps_its_id_holders_and_stack_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                badge = stats.create_custom_badge("chat", "🏆", "Победитель", 10, "Admin")
                for _ in range(3):
                    stats.give_custom_badge(
                        "chat", badge.badge_id, 20, "User", 10, "Admin", stack=True,
                    )

                renamed = stats.rename_custom_badge(
                    "chat", badge.badge_id, "  Чемпион   недели  "
                )

                self.assertEqual(renamed.badge_id, badge.badge_id)
                self.assertEqual(renamed.label, "🏆 Чемпион недели")
                held = stats.custom_badges_for_user("chat", 20)
                self.assertEqual(len(held), 1)
                self.assertEqual(held[0].badge_id, badge.badge_id)
                self.assertEqual(held[0].label, "🏆 Чемпион недели ×3")

    def test_renaming_a_custom_badge_takes_the_whole_label_emoji_and_all(self):
        """Typing what the badge should read is the one spelling that used to fail.

        Rename took the NAME alone and kept the old emoji, so an admin who typed the label
        they could see -- "🏆 Чемпион" -- stored the emoji as part of the name and got
        "⭐ 🏆 Чемпион" back, with the old picture still stuck to the front. All three
        readings work now, and each one is something somebody would reasonably type.
        """
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                badge = stats.create_custom_badge("chat", "⭐", "Майор", 10, "Admin")
                stats.give_custom_badge("chat", badge.badge_id, 20, "User", 10, "Admin")

                whole = stats.rename_custom_badge("chat", badge.badge_id, "🎖 Майор запаса")
                self.assertEqual(whole.label, "🎖 Майор запаса")
                # The name on its own still keeps the picture the badge already has.
                name_only = stats.rename_custom_badge("chat", badge.badge_id, "Полковник")
                self.assertEqual(name_only.label, "🎖 Полковник")
                # And the picture on its own keeps the name, rather than becoming it.
                emoji_only = stats.rename_custom_badge("chat", badge.badge_id, "🏆")
                self.assertEqual(emoji_only.label, "🏆 Полковник")

                # Through all of it: same id, same holder, still one badge.
                self.assertEqual(emoji_only.badge_id, badge.badge_id)
                held = stats.custom_badges_for_user("chat", 20)
                self.assertEqual([item.label for item in held], ["🏆 Полковник"])

    def test_renaming_a_custom_badge_refuses_a_duplicate_label(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                first = stats.create_custom_badge("chat", "🏆", "Первый", 10, "Admin")
                stats.create_custom_badge("chat", "🏆", "Второй", 10, "Admin")

                with self.assertRaisesRegex(ValueError, "уже существует"):
                    stats.rename_custom_badge("chat", first.badge_id, "второй")

                self.assertEqual(
                    next(item for item in stats.list_custom_badges("chat")
                         if item.badge_id == first.badge_id).name,
                    "Первый",
                )

    def test_custom_badge_requires_an_emoji(self):
        with self.assertRaisesRegex(ValueError, "эмодзи"):
            stats.parse_custom_badge_spec("VIP Пользователь")

    def test_emoji_outside_the_pictograph_block_are_still_emoji(self):
        """Reported from the chat, 2026-08-10: "⭐ Майор" was refused as "not an emoji".

        The star is U+2B50 and the hourglass U+231B -- both outside the ranges the old
        hand-written block list named, and both things people actually pick for a badge.
        """
        for spec, expected in (
            ("⭐ Майор", "⭐"),          # star
            ("⌛ Терпеливый", "⌛"),      # hourglass
            ("⏰ Ранняя пташка", "⏰"),   # alarm clock
            ("⭕ Меткий", "⭕"),          # heavy large circle
            ("↔️ Связной", "↔️"),  # arrow, emoji presentation
            ("1️⃣ Первый", "1️⃣"),  # keycap
            ("\U0001F3AF Меткий глаз", "\U0001F3AF"),   # a pictograph, as before
        ):
            with self.subTest(spec=spec):
                emoji, name = stats.parse_custom_badge_spec(spec)
                self.assertEqual(emoji, expected)
                self.assertTrue(name)

    def test_a_bare_symbol_is_still_not_an_emoji(self):
        """The check must stay tight enough to catch a missing emoji: these are the first
        tokens somebody types when they have simply written a name."""
        for spec in ("VIP Пользователь", "+ Майор", "5 Майор", "- Майор", "Майор Иванов"):
            with self.subTest(spec=spec):
                with self.assertRaisesRegex(ValueError, "эмодзи"):
                    stats.parse_custom_badge_spec(spec)

    def test_most_improved_compares_equal_windows_by_xp_delta(self):
        current = {
            "1": stats.UserStats(user_id="1", display_name="A", legacy_message_points=100),
            "2": stats.UserStats(user_id="2", display_name="B", legacy_message_points=50),
        }
        previous = {
            "1": stats.UserStats(user_id="1", display_name="A", legacy_message_points=20),
        }

        user, delta = stats.most_improved_user(current, previous, 5.0)
        self.assertEqual(user.user_id, "1")
        self.assertEqual(delta, 80)

    def test_weekly_winner_weeks_are_unique_and_counted(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                first = stats.record_weekly_contest_winner("chat", 1, 20, "User", 10, "Admin")
                repeated = stats.record_weekly_contest_winner("chat", 1, 20, "User", 10, "Admin")
                second = stats.record_weekly_contest_winner("chat", 2, 20, "User", 10, "Admin")
                conflict = stats.record_weekly_contest_winner("chat", 2, 30, "Other", 10, "Admin")

                self.assertEqual(first[:2], ("awarded", 1))
                self.assertEqual(repeated[:2], ("already", 1))
                self.assertEqual(second[:2], ("awarded", 2))
                self.assertEqual(conflict[0], "taken")
                self.assertEqual(
                    stats.weekly_winner_badges_for_user("chat", 20)[0].label,
                    "🏆 Победитель Недельного Конкурса ×2",
                )

    def test_hashtag_backfill_preserves_existing_xp_counters(self):
        message = SimpleNamespace(
            sender_id=20,
            sender_name="User",
            sender_username="user",
            text="#янепидор #итогинедели",
            dt_local=datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
            message_id=1,
            is_reply=False,
        )
        payload = {
            "entry": "chat",
            "day": "2026-07-20",
            "users": {
                "20": {
                    "display_name": "User",
                    "messages": 99,
                    "media": 7,
                    "replies": 8,
                }
            },
        }

        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                stats._backfill_day_badge_stats(
                    "chat", date(2026, 7, 20), payload, [message], log=lambda _: None
                )
                saved = stats._load_day("chat", date(2026, 7, 20))

        self.assertEqual(saved["users"]["20"]["messages"], 99)
        self.assertEqual(saved["users"]["20"]["media"], 7)
        self.assertEqual(saved["users"]["20"]["replies"], 8)
        self.assertEqual(saved["users"]["20"]["not_gay_hashtag_uses"], 1)
        self.assertEqual(saved["users"]["20"]["weekly_contest_weeks"], ["2026-W30"])


def _painter_with_works(count, **kwargs):
    """A member with `count` works, message ids 1000+, newest first."""
    posts = [
        [f"2026-07-{1 + (n % 28):02d}T12:00:{n % 60:02d}", 1000 + n]
        for n in range(count, 0, -1)
    ]
    defaults = dict(
        user_id="20", username="painter", display_name="Painter",
        figurines_painted=count, recent_figurine_posts=posts,
    )
    defaults.update(kwargs)
    return stats.UserStats(**defaults)


class WorksListTests(unittest.TestCase):
    """/stat shows the newest STAT_WORKS_SHOWN works; /работы lists every one."""

    def test_stat_links_only_the_newest_ten_and_points_to_the_rest(self):
        user = _painter_with_works(25)
        links = stats.figurine_message_links("example", -1001, user)
        text = stats.format_stat(user, rank=1, total=1, xp=0, streak=0, figurine_links=links)

        self.assertIn("🎨 Последние работы: ", text)
        for number, message_id in zip(range(1, 11), range(1025, 1015, -1)):
            self.assertIn(f'<a href="https://t.me/example/{message_id}">{number}</a>', text)
        self.assertNotIn(">11</a>", text)
        self.assertIn("📂 Все 25: /работы @painter", text)

    def test_ten_or_fewer_works_need_no_pointer(self):
        user = _painter_with_works(10)
        links = stats.figurine_message_links("example", -1001, user)
        text = stats.format_stat(user, rank=1, total=1, xp=0, streak=0, figurine_links=links)

        self.assertIn("🎨 Работы: ", text)
        self.assertIn(">10</a>", text)
        self.assertNotIn("/работы", text)

    def test_the_pointer_names_somebody_without_a_username_by_display_name(self):
        user = _painter_with_works(11, username=None, display_name="Саша <К>")
        links = stats.figurine_message_links("example", -1001, user)
        text = stats.format_stat(user, rank=1, total=1, xp=0, streak=0, figurine_links=links)
        self.assertIn("/работы Саша &lt;К&gt;", text)

    def test_works_lists_every_work_with_names_numbered_like_stat(self):
        user = _painter_with_works(25)
        links = stats.figurine_message_links("example", -1001, user)
        names = [None] * 25
        names[11] = "Дредноут"

        messages = stats.format_works(user, links, names)

        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0].startswith("🎨 Работы Painter — 25 (#япокрасил)\n\n"))
        for number in range(1, 26):
            self.assertIn(f">{number}" if number != 12 else ">12. Дредноут</a>", messages[0])
        self.assertIn('<a href="https://t.me/example/1001">25</a>', messages[0])

    def test_a_long_history_is_split_under_telegrams_limit(self):
        user = _painter_with_works(400)
        links = stats.figurine_message_links("example", -1001, user)
        names = ["Космодесантник Ультрамаринов " + str(n) for n in range(400)]

        messages = stats.format_works(user, links, names)

        self.assertGreater(len(messages), 1)
        visible = [
            len(__import__("re").sub(r"<[^>]+>", "", message)) for message in messages
        ]
        self.assertTrue(all(length <= 4096 for length in visible), visible)
        # The header once, and every work exactly once across the parts.
        self.assertEqual(sum(message.count("🎨 Работы") for message in messages), 1)
        self.assertEqual(sum(message.count("<a href=") for message in messages), 400)

    def test_works_escapes_names_and_handles_an_empty_history(self):
        user = _painter_with_works(1, display_name="<b>")
        links = stats.figurine_message_links("example", -1001, user)
        text = stats.format_works(user, links, ["<i>x</i>"])[0]
        self.assertNotIn("<b>", text.split("\n")[0])
        self.assertIn("&lt;i&gt;x&lt;/i&gt;", text)

        empty = stats.format_works(stats.UserStats(user_id="1", display_name="T"), [])
        self.assertEqual(len(empty), 1)
        self.assertIn("Пока ни одной", empty[0])

    def test_works_command_spellings(self):
        self.assertEqual(stats.parse_works_command("/работы"), "")
        self.assertEqual(stats.parse_works_command("/РАБОТЫ @painter"), "@painter")
        self.assertEqual(stats.parse_works_command("/works Саша Иванов"), "Саша Иванов")
        self.assertIsNone(stats.parse_works_command("/worksheet"))
        self.assertIsNone(stats.parse_works_command("мои /работы"))
        self.assertIsNone(stats.parse_works_command("/stat"))
        # Telegram's own "/works@bot" form is stripped before this is asked.
        self.assertEqual(
            stats.parse_works_command(stats.strip_command_bot_mention("/works@Trash_Modelist", "Trash_Modelist")),
            "",
        )

    def test_a_post_without_a_message_id_takes_no_number_anywhere(self):
        """A live-recorded post with no id cannot be linked. Every numbered screen skips
        it the same way, or /stat's "2" and the cabinet's "2" become different works."""
        user = stats.UserStats(
            user_id="1",
            recent_figurine_posts=[["t4", 104], ["t3", None], ["t2", 102], ["t1", 101]],
        )
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                stats.set_work_name("chat", "1", 102, "Дредноут")
                names = stats.work_name_list("chat", user)

        links = stats.figurine_message_links("example", -1001, user)
        self.assertEqual(links[1], "https://t.me/example/102")
        self.assertEqual(names, [None, "Дредноут", None])
        import cabinet
        self.assertEqual(cabinet.message_id_for_position(user, 2), 102)


class WorksCommandDispatchTests(unittest.TestCase):
    """/работы reaches its handler from a real update, in the group and in a DM."""

    class _Api:
        def __init__(self):
            self.sent = []
            self.next_id = 500

        async def send_message(self, chat_id, text, **kwargs):
            self.next_id += 1
            self.sent.append((chat_id, text, kwargs))
            return {"message_id": self.next_id}

        async def delete_message(self, chat_id, message_id):
            pass

        async def set_message_reaction(self, *args, **kwargs):
            pass

    def _dispatch(self, chat, text, resolved_user, known_chat_ids=None):
        import asyncio

        api = self._Api()
        resolve = AsyncMock(return_value=(resolved_user, 1, 1, 0, 0))
        cfg = SimpleNamespace(
            listener_allowed_chats=["examplechat"], stats_enabled=True, stats_top_limit=10,
        )

        async def go():
            background = set()
            with patch("stats.resolve_stat_target", resolve), \
                    patch("stats.work_name_list", return_value=[]):
                await bot_listener._dispatch_update(
                    {"message": {
                        "message_id": 7, "chat": chat, "text": text,
                        "from": {"id": 20, "username": "painter", "first_name": "Painter"},
                    }},
                    api, None, cfg, timezone.utc, "Trash_Modelist", 1, set(),
                    asyncio.Queue(), background, "examplechat",
                    known_chat_ids if known_chat_ids is not None else {}, {}, {}, {},
                    log=lambda *_: None,
                )
                for task in list(background):
                    task.cancel()

        asyncio.run(go())
        return api, resolve

    def test_in_the_group_it_lists_every_work_with_links_into_the_group(self):
        group = {"id": -1001, "type": "supergroup", "username": "examplechat"}
        api, resolve = self._dispatch(group, "/работы", _painter_with_works(12))

        self.assertEqual(len(api.sent), 1)
        chat_id, text, kwargs = api.sent[0]
        self.assertEqual(chat_id, -1001)
        self.assertEqual(kwargs.get("parse_mode"), "HTML")
        self.assertIn("🎨 Работы Painter — 12", text)
        self.assertIn('<a href="https://t.me/examplechat/1001">12</a>', text)
        # Asked about themselves, so the lookup goes by their Telegram id.
        self.assertEqual(resolve.await_args.kwargs.get("requester_id"), 20)
        self.assertEqual(resolve.await_args.args[3], "")

    def test_a_bare_stat_is_compact_and_looks_its_requester_up_by_id(self):
        group = {"id": -1001, "type": "supergroup", "username": "examplechat"}
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                api, resolve = self._dispatch(
                    group, "/stat", _painter_with_works(14, messages=1_200, active_days=33),
                )

        _, text, kwargs = api.sent[0]
        self.assertEqual(kwargs.get("parse_mode"), "HTML")
        self.assertEqual(resolve.await_args.kwargs.get("requester_id"), 20)
        self.assertIn("🏅 Значки: 📅 Завсегдатай 1 · 💬 Собеседник 1", text)
        self.assertIn("🎨 Последние работы: ", text)
        self.assertIn("📂 Все 14: /работы @painter", text)
        self.assertNotIn(">11</a>", text)

    def test_works_at_bot_with_a_name_asks_about_that_member(self):
        group = {"id": -1001, "type": "supergroup", "username": "examplechat"}
        _, resolve = self._dispatch(
            group, "/works@Trash_Modelist @someone", _painter_with_works(1)
        )
        self.assertEqual(resolve.await_args.args[3], "@someone")

    def test_in_a_dm_the_links_still_point_at_the_group(self):
        """The DM has no t.me/c/ address of its own; links built from it came out empty,
        so /stat in a DM used to show no works at all."""
        dm = {"id": 20, "type": "private"}
        with patch("bot_listener._cabinet_chat_ref", new=AsyncMock(return_value=(-1001234, None))):
            api, _ = self._dispatch(dm, "/работы", _painter_with_works(2))

        _, text, _ = api.sent[0]
        self.assertIn('<a href="https://t.me/c/1234/1002">1</a>', text)


class StatTargetTests(unittest.TestCase):
    """Whose stats a bare /stat shows."""

    def test_an_exact_display_name_beats_a_longer_one_that_contains_it(self):
        users = {
            "1": stats.UserStats(user_id="1", display_name="Саша Иванов"),
            "2": stats.UserStats(user_id="2", display_name="Саша"),
        }
        self.assertEqual(stats._find_user(users, "Саша").user_id, "2")
        self.assertEqual(stats._find_user(users, "иванов").user_id, "1")

    def test_the_requesters_own_id_wins_over_any_name(self):
        users = {
            "1": stats.UserStats(user_id="1", display_name="Саша Иванов", messages=50),
            "2": stats.UserStats(user_id="2", display_name="Саша", messages=5),
        }

        async def go(requester_id):
            with patch("stats.aggregate_all_time_live", new=AsyncMock(return_value=users)), \
                    patch("stats.words_per_point", new=AsyncMock(return_value=5.0)):
                return await stats.resolve_stat_target(
                    None, "chat", "chat", "", None, "Саша Иванов", timezone.utc,
                    log=lambda *_: None, requester_id=requester_id,
                )

        import asyncio
        user, rank, total, xp, streak = asyncio.run(go(2))
        self.assertEqual(user.user_id, "2")
        self.assertEqual(total, 2)
        # Without an id the name decides, as before.
        user, *_ = asyncio.run(go(None))
        self.assertEqual(user.user_id, "1")

    def test_rank_ties_break_the_same_way_in_top_and_stat(self):
        users = {
            "9": stats.UserStats(user_id="9", display_name="Девять", messages=10),
            "3": stats.UserStats(user_id="3", display_name="Три", messages=10),
        }

        async def go():
            with patch("stats.aggregate_all_time_live", new=AsyncMock(return_value=users)), \
                    patch("stats.words_per_point", new=AsyncMock(return_value=5.0)):
                stat = await stats.resolve_stat_target(
                    None, "chat", "chat", "Девять", None, "", timezone.utc,
                    log=lambda *_: None,
                )
                top = await stats.format_top(None, "chat", "chat", "all", timezone.utc, 10,
                                             log=lambda *_: None)
            return stat, top

        import asyncio
        (_, rank, *_), top = asyncio.run(go())
        self.assertEqual(rank, 2)
        self.assertLess(top.index("Три"), top.index("Девять"))


class MedalReputationTests(unittest.TestCase):
    """A point of reputation per earned-badge level (stats.medal_levels)."""

    def test_a_member_with_no_badges_scores_nothing(self):
        self.assertEqual(stats.medal_levels(stats.UserStats(user_id="1")), 0)

    def test_each_badge_level_is_worth_a_point(self):
        for days, messages, expected in (
            (29, 999, 0), (30, 0, 1), (0, 1_000, 1), (90, 2_999, 5), (61, 4_000, 6),
        ):
            with self.subTest(days=days, messages=messages):
                user = stats.UserStats(user_id="1", active_days=days, messages=messages)
                self.assertEqual(stats.medal_levels(user), expected)

    def test_the_points_stop_one_short_of_two_contest_wins(self):
        """The badges never run out, so grinding them is capped below being valued."""
        veteran = stats.UserStats(user_id="1", active_days=3_000, messages=500_000)
        self.assertEqual(stats.medal_levels(veteran), stats.REPUTATION_MEDAL_LEVEL_CAP)
        self.assertLess(stats.REPUTATION_MEDAL_LEVEL_CAP, 2 * stats.REPUTATION_PER_CONTEST_WIN)

    def test_the_count_matches_the_badges_stat_actually_shows(self):
        """medal_levels must never award a point for a medal the member cannot see."""
        user = stats.UserStats(user_id="1", active_days=65, messages=3_100)
        labels = [badge.label for badge in stats.earned_badges(user)]
        self.assertEqual(labels, ["📅 Завсегдатай 2", "💬 Собеседник 3"])
        self.assertEqual(stats.medal_levels(user), 2 + 3)

    def test_retired_badges_no_longer_score(self):
        """Painting, streaks, night shifts, the gallery and the two hashtags were dropped
        from /stat, so they no longer move reputation either."""
        user = stats.UserStats(
            user_id="1", figurines_painted=50, media=25,
            hours={str(hour): 200 for hour in range(6)},
            not_gay_hashtag_uses=1, weekly_contest_weeks={"2026-W30"},
        )
        self.assertEqual(stats.medal_levels(user), 0)

    def test_reputation_score_adds_medals_to_the_peer_granted_half(self):
        self.assertEqual(
            stats.reputation_score(1, 1, 40, 7),
            stats.REPUTATION_PER_CONTEST_WIN
            + stats.REPUTATION_PER_BADGE_RECEIVED
            + 2
            + 7 * stats.REPUTATION_PER_MEDAL_LEVEL,
        )

    def test_medals_default_to_zero_for_a_caller_without_userstats(self):
        """economy.reputation_for is reachable from the ledger, which has no UserStats."""
        self.assertEqual(stats.reputation_score(1, 0, 0), stats.REPUTATION_PER_CONTEST_WIN)

    def test_a_negative_medal_count_cannot_subtract_reputation(self):
        self.assertEqual(stats.reputation_score(0, 0, 0, -5), 0)


class FakeBotAPI:
    def __init__(self):
        self.sent = []
        self.callbacks = []
        self.next_message_id = 100

    async def get_chat_administrators(self, chat_id):
        return [{"user": {"id": 10, "first_name": "Admin"}}]

    async def send_message(self, chat_id, text, **kwargs):
        message = {
            "message_id": self.next_message_id,
            "chat": {
                "id": chat_id,
                "type": "private" if chat_id > 0 else "supergroup",
            },
            "text": text,
        }
        self.next_message_id += 1
        self.sent.append((message, kwargs))
        return message

    async def answer_callback_query(self, callback_query_id, text=None):
        self.callbacks.append((callback_query_id, text))


class BadgeRecipientParsingTests(unittest.TestCase):
    def test_one_name_is_one_recipient(self):
        self.assertEqual(bot_listener._parse_badge_recipients("@user"), ["@user"])

    def test_commas_semicolons_and_newlines_all_separate(self):
        self.assertEqual(
            bot_listener._parse_badge_recipients("@a, @b; @c\n@d"),
            ["@a", "@b", "@c", "@d"],
        )

    def test_a_two_word_display_name_stays_one_recipient(self):
        """The reason spaces are not a separator: this is one member, not two."""
        self.assertEqual(
            bot_listener._parse_badge_recipients("Алексей Белявский"),
            ["Алексей Белявский"],
        )
        self.assertEqual(
            bot_listener._parse_badge_recipients("Алексей Белявский, @user"),
            ["Алексей Белявский", "@user"],
        )

    def test_repeats_are_dropped_ignoring_case_and_the_at_sign(self):
        self.assertEqual(bot_listener._parse_badge_recipients("@User, user, @user"), ["@User"])

    def test_blank_chunks_and_stray_separators_vanish(self):
        self.assertEqual(bot_listener._parse_badge_recipients(",,  @a ,\n\n, @b ,"), ["@a", "@b"])

    def test_empty_input_is_no_recipients(self):
        for text in ("", "   ", ",,,", None):
            with self.subTest(text=text):
                self.assertEqual(bot_listener._parse_badge_recipients(text), [])


class BadgeFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_can_create_and_give_a_badge_in_bot_dm(self):
        api = FakeBotAPI()
        flows = {}
        admin = {"id": 10, "first_name": "Admin"}
        target = {"id": 20, "first_name": "User"}
        command = {
            "message_id": 1,
            "chat": {"id": 10, "type": "private"},
            "from": admin,
            "text": "/badge",
        }

        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                await bot_listener.handle_badge_command(api, command, "chat", -1001, flows)
                create_flow_id = next(iter(flows))
                menu_message = api.sent[-1][0]
                await bot_listener.handle_badge_callback(
                    api,
                    {
                        "id": "create-callback",
                        "from": admin,
                        "message": menu_message,
                        "data": bot_listener._badge_callback_data("create", create_flow_id),
                    },
                    flows,
                )
                prompt_message = api.sent[-1][0]
                consumed = await bot_listener.handle_badge_text_input(
                    api,
                    None,
                    {
                        "message_id": 2,
                        "chat": command["chat"],
                        "from": admin,
                        "text": "🎯 Меткий глаз",
                        "reply_to_message": prompt_message,
                    },
                    timezone.utc,
                    flows,
                )
                self.assertTrue(consumed)

                await bot_listener.handle_badge_command(api, command, "chat", -1001, flows)
                give_flow_id = next(iter(flows))
                give_menu = api.sent[-1][0]
                await bot_listener.handle_badge_callback(
                    api,
                    {
                        "id": "list-callback",
                        "from": admin,
                        "message": give_menu,
                        "data": bot_listener._badge_callback_data("list", give_flow_id),
                    },
                    flows,
                )
                badge = stats.list_custom_badges("chat")[0]
                badge_list_message = api.sent[-1][0]
                await bot_listener.handle_badge_callback(
                    api,
                    {
                        "id": "give-callback",
                        "from": admin,
                        "message": badge_list_message,
                        "data": bot_listener._badge_callback_data("give", give_flow_id, badge.badge_id),
                    },
                    flows,
                )
                target_prompt = api.sent[-1][0]
                tracked_target = stats.UserStats(
                    user_id=str(target["id"]),
                    username="user",
                    display_name=target["first_name"],
                )
                with patch(
                    "stats.resolve_stat_target",
                    new=AsyncMock(return_value=(tracked_target, 1, 1, 0, 0)),
                ):
                    consumed = await bot_listener.handle_badge_text_input(
                        api,
                        None,
                        {
                            "message_id": 3,
                            "chat": command["chat"],
                            "from": admin,
                            "text": "@user",
                            "reply_to_message": target_prompt,
                        },
                        timezone.utc,
                        flows,
                    )
                self.assertTrue(consumed)

                self.assertEqual(
                    [item.label for item in stats.custom_badges_for_user("chat", target["id"])],
                    ["🎯 Меткий глаз"],
                )
                sent = [call[0] for call in api.sent]
                # The admin gets a confirmation in the DM...
                self.assertTrue(
                    any(
                        "получает значок 🎯 Меткий глаз" in message["text"]
                        and message["chat"]["id"] == command["chat"]["id"]
                        for message in sent
                    ),
                    sent,
                )
                # ...and the group is told, naming the recipient by @username.
                announcement = next(
                    message for message in sent
                    if "получил уникальный значок" in message["text"]
                )
                self.assertEqual(announcement["text"], "@user получил уникальный значок: 🎯 Меткий глаз")
                self.assertNotEqual(announcement["chat"]["id"], command["chat"]["id"])

    async def _give(self, api, flows, admin, entry_action, reply_text, targets):
        """Drive /badge from the menu to the award, and return the DM/group messages.

        `entry_action` is "list" (announced) or "listq" (quiet); `targets` is what
        resolve_stat_target should return, in the order the names are resolved.
        """
        command = {
            "message_id": 1,
            "chat": {"id": 10, "type": "private"},
            "from": admin,
            "text": "/badge",
        }
        await bot_listener.handle_badge_command(api, command, "chat", -1001, flows)
        flow_id = next(iter(flows))
        await bot_listener.handle_badge_callback(
            api,
            {"id": "c1", "from": admin, "message": api.sent[-1][0],
             "data": bot_listener._badge_callback_data(entry_action, flow_id)},
            flows,
        )
        badge = stats.list_custom_badges("chat")[0]
        await bot_listener.handle_badge_callback(
            api,
            {"id": "c2", "from": admin, "message": api.sent[-1][0],
             "data": bot_listener._badge_callback_data("give", flow_id, badge.badge_id)},
            flows,
        )
        prompt = api.sent[-1][0]
        with patch("stats.resolve_stat_target", new=AsyncMock(side_effect=[
            (target, 1, 1, 0, 0) for target in targets
        ])):
            await bot_listener.handle_badge_text_input(
                api, None,
                {"message_id": 3, "chat": command["chat"], "from": admin,
                 "text": reply_text, "reply_to_message": prompt},
                timezone.utc, flows,
            )
        return [message for message, _ in api.sent]

    def _make_badge(self):
        return stats.create_custom_badge("chat", "🎯", "Меткий глаз", 10, "Admin")

    async def test_a_quiet_award_never_reaches_the_group(self):
        api = FakeBotAPI()
        admin = {"id": 10, "first_name": "Admin"}
        target = stats.UserStats(user_id="20", username="user", display_name="User")
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                self._make_badge()
                sent = await self._give(api, {}, admin, "listq", "@user", [target])

                # The badge is really awarded...
                self.assertEqual(
                    [item.label for item in stats.custom_badges_for_user("chat", 20)],
                    ["🎯 Меткий глаз"],
                )
        # ...the admin is still told, in the DM...
        self.assertIn("получает значок 🎯 Меткий глаз", sent[-1]["text"])
        self.assertIn("Без объявления в чате.", sent[-1]["text"])
        # ...and nothing at all went to the group.
        self.assertEqual([m for m in sent if m["chat"]["id"] == -1001], [])

    async def test_a_normal_award_still_announces(self):
        api = FakeBotAPI()
        admin = {"id": 10, "first_name": "Admin"}
        target = stats.UserStats(user_id="20", username="user", display_name="User")
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                self._make_badge()
                sent = await self._give(api, {}, admin, "list", "@user", [target])
        group = [m for m in sent if m["chat"]["id"] == -1001]
        self.assertEqual(len(group), 1)
        self.assertEqual(group[0]["text"], "@user получил уникальный значок: 🎯 Меткий глаз")
        self.assertNotIn("Без объявления", sent[-2]["text"])

    async def test_several_recipients_share_one_announcement(self):
        api = FakeBotAPI()
        admin = {"id": 10, "first_name": "Admin"}
        people = [
            stats.UserStats(user_id="20", username="one", display_name="Один"),
            stats.UserStats(user_id="21", username="two", display_name="Два"),
            stats.UserStats(user_id="22", username=None, display_name="Алексей Белявский"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                self._make_badge()
                sent = await self._give(
                    api, {}, admin, "list", "@one, @two\nАлексей Белявский", people
                )
                for user_id in (20, 21, 22):
                    self.assertEqual(
                        [b.label for b in stats.custom_badges_for_user("chat", user_id)],
                        ["🎯 Меткий глаз"],
                        user_id,
                    )
        group = [m for m in sent if m["chat"]["id"] == -1001]
        self.assertEqual(len(group), 1, "three people must not mean three group posts")
        self.assertEqual(
            group[0]["text"],
            "@one, @two, Алексей Белявский получили уникальный значок: 🎯 Меткий глаз",
        )
        self.assertIn("получают (3)", sent[-2]["text"])

    async def test_unknown_names_are_reported_without_losing_the_rest(self):
        api = FakeBotAPI()
        admin = {"id": 10, "first_name": "Admin"}
        found = stats.UserStats(user_id="20", username="one", display_name="Один")
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                self._make_badge()
                sent = await self._give(api, {}, admin, "list", "@one, @ghost", [found, None])
                self.assertEqual(len(stats.custom_badges_for_user("chat", 20)), 1)
        summary = sent[-2]["text"]
        self.assertIn("получает значок", summary)
        self.assertIn("Не нашёл в статистике: @ghost", summary)

    async def test_nobody_found_re_prompts_instead_of_awarding(self):
        api = FakeBotAPI()
        admin = {"id": 10, "first_name": "Admin"}
        flows = {}
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                self._make_badge()
                sent = await self._give(api, flows, admin, "list", "@ghost", [None])
        self.assertIn("Не нашёл в статистике: @ghost", sent[-1]["text"])
        # The flow stays open so the admin can simply reply again.
        self.assertEqual(len(flows), 1)
        self.assertEqual([m for m in sent if m["chat"]["id"] == -1001], [])

    async def test_the_same_person_named_twice_is_awarded_once(self):
        api = FakeBotAPI()
        admin = {"id": 10, "first_name": "Admin"}
        same = stats.UserStats(user_id="20", username="one", display_name="Один")
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                self._make_badge()
                # Two different spellings, one member behind them.
                sent = await self._give(api, {}, admin, "list", "@one, Один", [same, same])
        self.assertIn("получает значок", sent[-2]["text"])
        self.assertNotIn("получают (2)", sent[-2]["text"])

    async def test_admin_can_record_numbered_weekly_winner_in_bot_dm(self):
        api = FakeBotAPI()
        command = {
            "message_id": 1,
            "chat": {"id": 10, "type": "private"},
            "from": {"id": 10, "first_name": "Admin"},
            "text": "/weekwinner 1 @user",
        }

        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                tracked_target = stats.UserStats(
                    user_id="20",
                    username="user",
                    display_name="User",
                )
                with patch(
                    "stats.resolve_stat_target",
                    new=AsyncMock(return_value=(tracked_target, 1, 1, 0, 0)),
                ):
                    await bot_listener.handle_week_winner_command(
                        api,
                        None,
                        command,
                        command["text"],
                        "chat",
                        -1001,
                        timezone.utc,
                    )

                self.assertIn("победитель Недельного Конкурса №1", api.sent[-1][0]["text"])
                self.assertEqual(
                    stats.weekly_winner_badges_for_user("chat", 20)[0].label,
                    # One of something is not "×1" -- the multiplier appears on the second
                    # win and never before. See stats.stacked_badge_name.
                    "🏆 Победитель Недельного Конкурса",
                )

    async def test_sultan_can_manage_without_group_admin_status(self):
        api = FakeBotAPI()
        delegated_user = {
            "id": 99,
            "username": "Sultan_Kembayev",
            "first_name": "Sultan",
        }
        self.assertTrue(
            await bot_listener._can_manage_chat(api, -1001, delegated_user)
        )
        self.assertFalse(
            await bot_listener._can_manage_chat(
                api,
                -1001,
                {"id": 98, "username": "someone_else"},
            )
        )

        command = {
            "message_id": 1,
            "chat": {"id": 99, "type": "private"},
            "from": delegated_user,
            "text": "/weekwinner 2 @user",
        }
        tracked_target = stats.UserStats(
            user_id="20",
            username="user",
            display_name="User",
        )
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                with patch(
                    "stats.resolve_stat_target",
                    new=AsyncMock(return_value=(tracked_target, 1, 1, 0, 0)),
                ):
                    await bot_listener.handle_week_winner_command(
                        api,
                        None,
                        command,
                        command["text"],
                        "chat",
                        -1001,
                        timezone.utc,
                    )

                self.assertEqual(
                    stats.weekly_winner_badges_for_user("chat", 20)[0].label,
                    # One of something is not "×1" -- the multiplier appears on the second
                    # win and never before. See stats.stacked_badge_name.
                    "🏆 Победитель Недельного Конкурса",
                )
        self.assertIn("победитель Недельного Конкурса №2", api.sent[-1][0]["text"])

    async def test_admin_can_delete_numbered_pokras_in_bot_dm(self):
        api = FakeBotAPI()
        command = {
            "message_id": 1,
            "chat": {"id": 10, "type": "private"},
            "from": {"id": 10, "first_name": "Admin"},
            "text": "/deletepokras @user 2",
        }
        tracked_target = stats.UserStats(
            user_id="20",
            username="user",
            display_name="User",
            figurines_painted=3,
            recent_figurine_posts=[
                ["2026-07-03T12:00:00", 103],
                ["2026-07-02T12:00:00", 102],
                ["2026-07-01T12:00:00", 101],
            ],
        )

        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                with patch(
                    "stats.resolve_stat_target",
                    new=AsyncMock(return_value=(tracked_target, 1, 1, 0, 0)),
                ):
                    await bot_listener.handle_delete_pokras_command(
                        api,
                        None,
                        command,
                        command["text"],
                        "chat",
                        -1001,
                        timezone.utc,
                    )
                deleted = stats._load_deleted_figurines("chat")["posts"]

        self.assertEqual(deleted["102"]["user_id"], "20")
        self.assertIn("Удалил работу №2", api.sent[-1][0]["text"])
        self.assertIn("Фигурок осталось: 2", api.sent[-1][0]["text"])

    async def test_management_commands_are_silent_in_group_chat(self):
        api = FakeBotAPI()
        group_message = {
            "message_id": 1,
            "chat": {"id": -1001, "type": "supergroup", "title": "Chat"},
            "from": {"id": 10, "first_name": "Admin"},
            "text": "/badge",
        }

        flows = {}
        await bot_listener.handle_badge_command(api, group_message, "chat", -1001, flows)
        await bot_listener.handle_week_winner_command(
            api,
            None,
            {**group_message, "text": "/weekwinner 1 @user"},
            "/weekwinner 1 @user",
            "chat",
            -1001,
            timezone.utc,
        )
        await bot_listener.handle_delete_pokras_command(
            api,
            None,
            {**group_message, "text": "/deletepokras @user 1"},
            "/deletepokras @user 1",
            "chat",
            -1001,
            timezone.utc,
        )

        self.assertEqual(api.sent, [])
        self.assertEqual(flows, {})


class BadgeBackButtonTests(unittest.IsolatedAsyncioTestCase):
    """Every screen below the badge menu has a way back to it.

    Reported from the chat, 2026-08-10 ("не везде есть кнопка Назад"): the flow had none
    at all, so a mistaken tap could only be escaped by abandoning the menu and typing
    /badge again -- and the delete confirmation offered "Да, удалить" as its only button.
    """

    ADMIN = {"id": 10, "first_name": "Admin"}

    async def _menu(self, api, flows):
        await bot_listener.handle_badge_command(
            api,
            {"message_id": 1, "chat": {"id": 10, "type": "private"},
             "from": self.ADMIN, "text": "/badge"},
            "chat", -1001, flows,
        )
        return next(iter(flows))

    async def _tap(self, api, flows, flow_id, action, badge_id=None, **kwargs):
        await bot_listener.handle_badge_callback(
            api,
            {"id": "cb", "from": self.ADMIN, "message": api.sent[-1][0],
             "data": bot_listener._badge_callback_data(action, flow_id, badge_id)},
            flows,
            **kwargs,
        )
        return api.sent[-1]

    def _buttons(self, sent_call):
        markup = sent_call[1].get("reply_markup") or {}
        return [b for row in markup.get("inline_keyboard", []) for b in row]

    async def test_every_screen_below_the_menu_offers_a_way_back(self):
        api, flows = FakeBotAPI(), {}
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                badge = stats.create_custom_badge("chat", "🎯", "Меткий глаз", 10, "Admin")
                flow_id = await self._menu(api, flows)

                for action, badge_id in (
                    ("list", None), ("listq", None), ("renlist", None), ("revlist", None),
                    ("dellist", None), ("del", badge.badge_id),
                ):
                    with self.subTest(action=action):
                        sent = await self._tap(api, flows, flow_id, action, badge_id)
                        labels = [b["text"] for b in self._buttons(sent)]
                        self.assertIn(bot_listener.BADGE_BACK_BUTTON_TEXT, labels)

    async def test_admin_can_rename_a_badge_from_the_menu(self):
        api, flows = FakeBotAPI(), {}
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                badge = stats.create_custom_badge("chat", "🎯", "Меткий глаз", 10, "Admin")
                stats.give_custom_badge(
                    "chat", badge.badge_id, 20, "User", 10, "Admin", stack=True,
                )
                flow_id = await self._menu(api, flows)
                menu_labels = [b["text"] for b in self._buttons(api.sent[-1])]
                self.assertIn(bot_listener.BADGE_RENAME_BUTTON_TEXT, menu_labels)

                await self._tap(api, flows, flow_id, "renlist")
                prompt = (await self._tap(api, flows, flow_id, "rename", badge.badge_id))[0]
                self.assertIn("вместе с эмодзи", prompt["text"])

                consumed = await bot_listener.handle_badge_text_input(
                    api, None,
                    {"message_id": 6, "chat": {"id": 10, "type": "private"},
                     "from": self.ADMIN, "text": "Снайпер", "reply_to_message": prompt},
                    timezone.utc, flows,
                )

                self.assertTrue(consumed)
                self.assertEqual(stats.list_custom_badges("chat")[0].label, "🎯 Снайпер")
                self.assertEqual(
                    stats.custom_badges_for_user("chat", 20)[0].label, "🎯 Снайпер"
                )
                self.assertIn("Значок переименован: 🎯 Снайпер", api.sent[-1][0]["text"])
                # The menu SURVIVES the action it was used for, with nothing half-chosen
                # left on it. Deleting the flow here is what made the message the admin
                # was still looking at answer "Меню устарело" to its own next button.
                self.assertEqual(set(flows), {flow_id})
                self.assertIsNone(flows[flow_id]["awaiting"])
                self.assertIsNone(flows[flow_id]["selected_badge_id"])
                back = await self._tap(api, flows, flow_id, "menu")
                self.assertIn(
                    bot_listener.BADGE_RENAME_BUTTON_TEXT,
                    [button["text"] for button in self._buttons(back)],
                )

    async def test_a_menu_outlives_the_process_that_drew_it(self):
        """The flow is in memory and nothing else, so a deploy wipes every open menu.

        That is what "Меню устарело или принадлежит другому администратору" was really
        saying most of the time: not that the menu belonged to somebody else, but that the
        bot had restarted since it was drawn. The ten-minute TTL did the same to an admin
        who stepped away. Neither is a reason to refuse -- the flow holds no permission,
        only a half-finished step, and every press re-checks who is pressing. So the flow
        is rebuilt under the id the buttons already carry, and the menu simply works.
        """
        api, flows = FakeBotAPI(), {}
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                stats.create_custom_badge("chat", "🎯", "Меткий глаз", 10, "Admin")
                flow_id = await self._menu(api, flows)

                flows.clear()  # the deploy
                sent = await self._tap(api, flows, flow_id, "renlist",
                                       admin_chat_id=-1001, entry="chat")

                self.assertEqual(set(flows), {flow_id})
                self.assertIn("Меткий глаз",
                              [b["text"] for b in self._buttons(sent)][0])
                self.assertNotIn("устарел", sent[0]["text"])

    async def test_a_menu_that_cannot_be_rebuilt_still_refuses_rather_than_guessing(self):
        """Rebuilding needs the chat being managed. Without it, the old dead end stands.

        This is the honest half of the revival: a flow carries no authority, but it does
        carry WHICH chat the badges belong to, and that is not something to invent.
        """
        api, flows = FakeBotAPI(), {}
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                stats.create_custom_badge("chat", "🎯", "Меткий глаз", 10, "Admin")
                flow_id = await self._menu(api, flows)
                flows.clear()

                await self._tap(api, flows, flow_id, "renlist")

                self.assertEqual(flows, {})
                self.assertIn("другому администратору", api.callbacks[-1][1])

    async def test_one_admins_menu_is_not_another_admins(self):
        """The revival must not turn a live menu into a shared one."""
        api, flows = FakeBotAPI(), {}
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                stats.create_custom_badge("chat", "🎯", "Меткий глаз", 10, "Admin")
                flow_id = await self._menu(api, flows)

                await bot_listener.handle_badge_callback(
                    api,
                    {"id": "cb", "from": {"id": 11, "first_name": "Другой"},
                     "message": api.sent[-1][0],
                     "data": bot_listener._badge_callback_data("renlist", flow_id)},
                    flows, admin_chat_id=-1001, entry="chat",
                )

                self.assertIn("другому администратору", api.callbacks[-1][1])
                self.assertEqual(flows[flow_id]["admin_id"], 10)

    async def test_the_irreversible_delete_confirmation_can_be_backed_out_of(self):
        api, flows = FakeBotAPI(), {}
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                badge = stats.create_custom_badge("chat", "🎯", "Меткий глаз", 10, "Admin")
                flow_id = await self._menu(api, flows)
                await self._tap(api, flows, flow_id, "dellist")
                await self._tap(api, flows, flow_id, "del", badge.badge_id)

                await self._tap(api, flows, flow_id, "menu")

                # Back on the menu, and the badge is still there.
                self.assertIn(bot_listener.BADGE_CREATE_BUTTON_TEXT,
                              [b["text"] for b in self._buttons(api.sent[-1])])
                self.assertEqual(len(stats.list_custom_badges("chat")), 1)

    async def test_going_back_forgets_what_the_abandoned_step_had_selected(self):
        """Otherwise Назад out of "выдать" and into "удалить" would still be carrying the
        badge and the quiet flag the previous step had set."""
        api, flows = FakeBotAPI(), {}
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                badge = stats.create_custom_badge("chat", "🎯", "Меткий глаз", 10, "Admin")
                flow_id = await self._menu(api, flows)
                await self._tap(api, flows, flow_id, "listq")
                await self._tap(api, flows, flow_id, "give", badge.badge_id)
                self.assertEqual(flows[flow_id]["selected_badge_id"], badge.badge_id)
                self.assertTrue(flows[flow_id]["silent"])

                await self._tap(api, flows, flow_id, "menu")

                self.assertIsNone(flows[flow_id]["selected_badge_id"])
                self.assertIsNone(flows[flow_id]["awaiting"])
                self.assertFalse(flows[flow_id]["silent"])

    async def test_answering_a_text_step_with_nazad_returns_to_the_menu(self):
        """A force-reply message cannot carry an inline keyboard, so on the steps that ask
        for text the way back is the word -- and the flow survives it, unlike "отмена"."""
        api, flows = FakeBotAPI(), {}
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                flow_id = await self._menu(api, flows)
                prompt = (await self._tap(api, flows, flow_id, "create"))[0]
                self.assertIn(bot_listener.BADGE_BACK_HINT, prompt["text"])

                consumed = await bot_listener.handle_badge_text_input(
                    api, None,
                    {"message_id": 5, "chat": {"id": 10, "type": "private"},
                     "from": self.ADMIN, "text": "назад", "reply_to_message": prompt},
                    timezone.utc, flows,
                )

                self.assertTrue(consumed)
                self.assertIn(flow_id, flows)  # still alive, unlike "отмена"
                self.assertIsNone(flows[flow_id]["awaiting"])
                self.assertIn(bot_listener.BADGE_CREATE_BUTTON_TEXT,
                              [b["text"] for b in self._buttons(api.sent[-1])])
                self.assertEqual(stats.list_custom_badges("chat"), [])

    async def test_otmena_still_drops_the_flow_entirely(self):
        api, flows = FakeBotAPI(), {}
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                flow_id = await self._menu(api, flows)
                prompt = (await self._tap(api, flows, flow_id, "create"))[0]

                await bot_listener.handle_badge_text_input(
                    api, None,
                    {"message_id": 5, "chat": {"id": 10, "type": "private"},
                     "from": self.ADMIN, "text": "отмена", "reply_to_message": prompt},
                    timezone.utc, flows,
                )

                self.assertEqual(flows, {})


if __name__ == "__main__":
    unittest.main()
