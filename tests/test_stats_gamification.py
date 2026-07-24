import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot_listener
import stats


class GamificationTests(unittest.TestCase):
    def test_xp_coins_and_levels(self):
        user = stats.UserStats(
            user_id="1",
            legacy_message_points=900,
            media=20,
            replies=25,
            active_days=11,
        )
        self.assertEqual(user.xp(5.0), 1_000)
        self.assertEqual(stats.coins_for_xp(1_009), 100)
        self.assertEqual(stats.coins_for_xp(1_010), 101)

        self.assertEqual(stats.level_for_progress(2_500, 2)[0].label, "🩶 Серый новичок")
        self.assertEqual(stats.level_for_progress(2_499, 3)[0].label, "🩶 Серый новичок")
        self.assertEqual(stats.level_for_progress(2_500, 3)[0].label, "⚪ Ученик грунта")
        self.assertEqual(stats.level_for_progress(5_000, 5)[0].label, "🖌️ Подмастерье кисти")
        self.assertEqual(stats.level_for_progress(10_000, 10)[0].label, "💨 Укротитель аэрографа")
        self.assertEqual(stats.level_for_progress(20_000, 20)[0].label, "💧 Повелитель проливок")
        self.assertEqual(stats.level_for_progress(35_000, 35)[0].label, "🏛️ Мастер витрины")
        final_level, next_level = stats.level_for_progress(50_000, 50)
        self.assertEqual(final_level.label, "👑 Легенда покраса")
        self.assertIsNone(next_level)

    def test_automatic_badges_use_existing_counters(self):
        first_day = date(2026, 1, 1)
        active_dates = {(first_day + timedelta(days=offset)).isoformat() for offset in range(7)}
        user = stats.UserStats(
            user_id="1",
            figurines_painted=5,
            messages=1_000,
            media=25,
            replies=100,
            active_days=30,
            active_day_dates=active_dates,
            hours={str(hour): 10 for hour in range(6)},
        )

        self.assertEqual(
            {badge.badge_id for badge in stats.earned_badges(user)},
            {
                "painted_bronze",
                "chat_voice",
                "gallery",
                "conversation",
                "regular",
                "streak_7",
                "night_shift",
            },
        )

    def test_only_highest_painting_medal_is_shown(self):
        bronze = stats.earned_badges(stats.UserStats(user_id="1", figurines_painted=1))
        silver = stats.earned_badges(stats.UserStats(user_id="1", figurines_painted=10))
        gold = stats.earned_badges(stats.UserStats(user_id="1", figurines_painted=50))

        self.assertEqual([(badge.emoji, badge.name) for badge in bronze], [("🥉", "Я покрасил III")])
        self.assertEqual([(badge.emoji, badge.name) for badge in silver], [("🥈", "Я покрасил II")])
        self.assertEqual([(badge.emoji, badge.name) for badge in gold], [("🥇", "Я покрасил I")])

    def test_higher_message_badge_replaces_lower_tier(self):
        none = stats.earned_badges(stats.UserStats(user_id="1", messages=99))
        hundred = stats.earned_badges(stats.UserStats(user_id="1", messages=100))
        still_hundred = stats.earned_badges(stats.UserStats(user_id="1", messages=999))
        voice = stats.earned_badges(stats.UserStats(user_id="1", messages=1_000))

        self.assertEqual(none, [])
        self.assertEqual(
            [(badge.badge_id, badge.name) for badge in hundred],
            [("hundred_messages", "Сотня")],
        )
        self.assertEqual(
            [(badge.badge_id, badge.name) for badge in still_hundred],
            [("hundred_messages", "Сотня")],
        )
        self.assertEqual(
            [(badge.badge_id, badge.name) for badge in voice],
            [("chat_voice", "Голос чата")],
        )

    def test_streak_and_night_badges_upgrade_without_stacking(self):
        def user_for(streak_days, night_messages):
            first_day = date(2026, 1, 1)
            return stats.UserStats(
                user_id="1",
                active_day_dates={
                    (first_day + timedelta(days=offset)).isoformat()
                    for offset in range(streak_days)
                },
                hours={"0": night_messages},
            )

        expected = (
            (7, 50, "streak_7", "Не остановить III", "night_shift", "Ночная смена III"),
            (14, 250, "streak_14", "Не остановить II", "night_shift_250", "Ночная смена II"),
            (30, 1_000, "streak_30", "Не остановить I", "night_shift_1000", "Ночная смена I"),
        )
        for streak, night, streak_id, streak_name, night_id, night_name in expected:
            with self.subTest(streak=streak, night=night):
                badges = stats.earned_badges(user_for(streak, night))
                self.assertEqual(
                    [(badge.badge_id, badge.name) for badge in badges],
                    [(streak_id, streak_name), (night_id, night_name)],
                )

        almost = stats.earned_badges(user_for(6, 49))
        self.assertEqual(almost, [])

    def test_hashtag_badges_and_weekly_participation_are_derived_from_messages(self):
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
        labels = [badge.label for badge in stats.earned_badges(user)]
        self.assertIn("🦄 Я не пидор", labels)
        self.assertIn("🎪 Участник Недельного конкурса ×2", labels)

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

        self.assertIn("⭐ XP: 1.234", text)
        self.assertIn("🪙 Монеты: 123", text)
        self.assertIn("🧩 Уровень: 🩶 Серый новичок", text)
        self.assertNotIn("До уровня", text)
        self.assertLess(text.index("🏅 Значки:"), text.index("🎨 Все работы"))
        self.assertIn("🏹 Лучник", text)
        self.assertIn('<a href="https://t.me/example/1">1</a>', text)

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

        self.assertIn("Имя: &lt;Painter &amp; Friend&gt;", text)
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

        self.assertIn("Фигурок: 12 (#япокрасил)", text)
        self.assertIn("Активных дней: 96 (🔥 Серия: 11 дней)", text)
        self.assertIn("💬 Сообщений: 1.842 (19.2 в день)", text)
        self.assertNotIn("Среднее сообщений в день:", text)
        self.assertNotIn("+200 XP за фигурку", text)

        without_streak = stats.format_stat(user, rank=1, total=1, xp=12_480, streak=0)
        self.assertIn("Активных дней: 96\n", without_streak)
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
                self.assertEqual(stats.record_level_observations("chat", [(user, 0)]), [])

                user.figurines_painted = 2
                self.assertEqual(stats.record_level_observations("chat", [(user, 2_500)]), [])

                user.figurines_painted = 3
                first = stats.record_level_observations("chat", [(user, 2_500)])
                repeated = stats.record_level_observations("chat", [(user, 2_500)])

                user.figurines_painted = 5
                second = stats.record_level_observations("chat", [(user, 5_000)])

        self.assertEqual(
            first,
            ["@user получил новый уровень «⚪ Ученик грунта»! 🎉🎊🥳"],
        )
        self.assertEqual(repeated, [])
        self.assertEqual(
            second,
            ["@user получил новый уровень «🖌️ Подмастерье кисти»! 🎉🎊🥳"],
        )

    def test_custom_badges_persist_and_duplicate_awards_are_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                emoji, name = stats.parse_custom_badge_spec("🎯 Меткий глаз")
                badge = stats.create_custom_badge("chat", emoji, name, 10, "Admin")
                awarded, created = stats.give_custom_badge("chat", badge.badge_id, 20, "User", 10, "Admin")
                awarded_again, created_again = stats.give_custom_badge(
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

    def test_custom_badge_requires_an_emoji(self):
        with self.assertRaisesRegex(ValueError, "эмодзи"):
            stats.parse_custom_badge_spec("VIP Пользователь")

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
                self.assertIn("получает значок 🎯 Меткий глаз", api.sent[-1][0]["text"])

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
                    "🏆 Победитель Недельного Конкурса ×1",
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
                    "🏆 Победитель Недельного Конкурса ×1",
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


if __name__ == "__main__":
    unittest.main()
