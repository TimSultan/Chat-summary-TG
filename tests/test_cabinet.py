import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot_listener
import cabinet
import economy
import stats


def _user(**kwargs):
    defaults = dict(user_id="20", username="user", display_name="Tester", messages=500)
    defaults.update(kwargs)
    return stats.UserStats(**defaults)


class ViewTests(unittest.TestCase):
    """The views are pure, so they can be checked without a bot token or a network."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def test_main_view_shows_the_card_and_every_section_button(self):
        text, keyboard = cabinet.main_view("chat", _user(), 5_000, rank=3, total=190, streak=4)

        self.assertIn("Личный кабинет", text)
        self.assertIn("🪙 Монеты: 500", text)
        self.assertIn("📈 Место в рейтинге: 3 из 190", text)
        self.assertIn("🔥 Серия: 4 дня", text)

        actions = {
            cabinet.parse_callback(button["callback_data"])[1]
            for row in keyboard["inline_keyboard"]
            for button in row
        }
        self.assertEqual(
            actions, {"stats", "shop", "works", "badges", "title", "main"}
        )

    def test_every_button_stays_inside_telegrams_callback_data_limit(self):
        _, keyboard = cabinet.main_view("chat", _user(user_id="1" * 19), 5_000, 1, 1, 0)
        _, shop = cabinet.shop_view("chat", _user(user_id="1" * 19), 5_000)
        for board in (keyboard, shop):
            for row in board["inline_keyboard"]:
                for button in row:
                    self.assertLessEqual(
                        len(button["callback_data"].encode("utf-8")),
                        cabinet.MAX_CALLBACK_BYTES,
                        button,
                    )

    def test_callback_round_trip(self):
        data = cabinet.callback_data(20, "buy", "freeze")
        self.assertEqual(cabinet.parse_callback(data), ("20", "buy", "freeze"))
        self.assertEqual(cabinet.parse_callback(cabinet.callback_data(20, "main")), ("20", "main", ""))
        self.assertIsNone(cabinet.parse_callback("roast:20:x"))
        self.assertIsNone(cabinet.parse_callback(""))

    def test_shop_view_marks_affordable_and_locked(self):
        user = _user()
        # 1_500 XP -> 150 coins: the 400-coin title is out of reach.
        locked, _ = cabinet.shop_view("chat", user, 1_500)
        self.assertIn("🔒", locked)
        self.assertNotIn("✅", locked)

        # 5_000 XP -> 500 coins: affordable.
        affordable, keyboard = cabinet.shop_view("chat", user, 5_000)
        self.assertIn("✅", affordable)
        self.assertEqual(
            [cabinet.parse_callback(b["callback_data"])[2]
             for row in keyboard["inline_keyboard"] for b in row
             if cabinet.parse_callback(b["callback_data"])[1] == "buy"],
            ["title"],
        )

    def test_every_leaf_view_offers_a_way_back(self):
        user = _user()
        views = [
            cabinet.shop_view("chat", user, 5_000),
            cabinet.works_view("chat", user, [], None, None),
            cabinet.badges_view("chat", user, []),
            cabinet.title_view("chat", user, 5_000),
            cabinet.result_view(user.user_id, "готово"),
        ]
        for text, keyboard in views:
            last_row = keyboard["inline_keyboard"][-1]
            self.assertEqual(last_row[0]["text"], cabinet.BACK_BUTTON)
            self.assertEqual(cabinet.parse_callback(last_row[0]["callback_data"])[1], "main")

    def test_user_controlled_text_is_escaped_into_html(self):
        economy.set_title("chat", "20", "<b>hax</b>")
        text, _ = cabinet.main_view(
            "chat", _user(display_name="<script>alert(1)</script>"), 5_000, 1, 1, 0
        )
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("&lt;b&gt;hax", text)

    def test_works_view_handles_empty_and_capped_histories(self):
        empty, keyboard = cabinet.works_view("chat", _user(), [], None, None)
        self.assertIn("Пока пусто", empty)
        # Nothing to rename, so no rename button.
        self.assertEqual(len(keyboard["inline_keyboard"]), 1)

        many = [f"https://t.me/c/1/{n}" for n in range(cabinet.WORKS_SHOWN + 5)]
        capped, keyboard = cabinet.works_view(
            "chat", _user(figurines_painted=35), many, None, None
        )
        self.assertIn("и ещё 5", capped)
        self.assertIn("✏️ Переименовать", capped + str(keyboard))

    def test_rename_request_parsing(self):
        self.assertEqual(cabinet.parse_rename_request("3 Дредноут"), (3, "Дредноут"))
        # Everything after the number is the name, spaces and all.
        self.assertEqual(
            cabinet.parse_rename_request("1  Космодесантник  Ультрамаринов"),
            (1, "Космодесантник  Ультрамаринов"),
        )
        # A bare number clears the name.
        self.assertEqual(cabinet.parse_rename_request("2"), (2, ""))
        self.assertIsNone(cabinet.parse_rename_request("Дредноут"))
        self.assertIsNone(cabinet.parse_rename_request("0 Ноль"))
        self.assertIsNone(cabinet.parse_rename_request(""))


class WorkNameTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    @staticmethod
    def _painter():
        return _user(
            figurines_painted=3,
            recent_figurine_posts=[
                ["2026-07-05T12:00:00", 105],
                ["2026-07-04T12:00:00", 104],
                ["2026-07-03T12:00:00", 103],
            ],
        )

    def test_named_works_render_by_name_and_unnamed_stay_numbered(self):
        user = self._painter()
        stats.set_work_name("chat", user.user_id, 104, "Дредноут")
        links = [f"https://t.me/c/1/{n}" for n in (105, 104, 103)]

        text, _ = cabinet.works_view("chat", user, links, None, None)

        self.assertIn("2. <a", text)
        self.assertIn("Дредноут</a>", text)
        self.assertIn("без названия", text)

    def test_a_name_follows_its_work_when_positions_shift(self):
        """Deleting a work compacts the numbering, so names keyed by position would
        silently jump onto somebody else's model."""
        user = self._painter()
        stats.set_work_name("chat", user.user_id, 103, "Дредноут")  # position 3

        # The newest work is deleted; the dreadnought is now position 2.
        user.recent_figurine_posts = user.recent_figurine_posts[1:]
        links = [f"https://t.me/c/1/{n}" for n in (104, 103)]
        text, _ = cabinet.works_view("chat", user, links, None, None)

        self.assertIn("2. <a", text)
        self.assertIn("Дредноут</a>", text)
        self.assertEqual(cabinet.message_id_for_position(user, 2), 103)

    def test_names_are_trimmed_bounded_and_clearable(self):
        user = self._painter()
        saved = stats.set_work_name("chat", user.user_id, 105, "  Очень   длинный  " + "я" * 80)
        self.assertLessEqual(len(saved), stats.WORK_NAME_MAX_CHARS)
        self.assertNotIn("  ", saved)

        self.assertEqual(stats.set_work_name("chat", user.user_id, 105, ""), "")
        self.assertNotIn("105", stats.work_names_for_user("chat", user.user_id))

    def test_a_name_is_escaped_into_the_html_view(self):
        user = self._painter()
        stats.set_work_name("chat", user.user_id, 105, "<b>hax</b>")
        text, _ = cabinet.works_view("chat", user, ["https://t.me/c/1/105"], None, None)
        self.assertNotIn("<b>hax", text)
        self.assertIn("&lt;b&gt;hax", text)

    def test_position_lookup_rejects_out_of_range(self):
        user = self._painter()
        self.assertEqual(cabinet.message_id_for_position(user, 1), 105)
        self.assertIsNone(cabinet.message_id_for_position(user, 0))
        self.assertIsNone(cabinet.message_id_for_position(user, 99))


class BadgeSectionTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def test_admin_badges_get_their_own_section_above_earned_ones(self):
        user = _user(messages=1_000, media=25)
        given = stats.Badge("custom", "🏹", "Лучник", custom=True)

        text, _ = cabinet.badges_view("chat", user, [given])

        self.assertIn("От администрации", text)
        self.assertIn("Заработанные", text)
        self.assertLess(text.index("От администрации"), text.index("Заработанные"))
        self.assertLess(text.index("🏹 Лучник"), text.index("Заработанные"))

    def test_the_admin_section_is_omitted_when_there_are_none(self):
        text, _ = cabinet.badges_view("chat", _user(messages=1_000), [])
        self.assertNotIn("От администрации", text)

    def test_a_weekly_win_is_earned_not_admin_granted(self):
        # It is assigned by an administrator, but it is won -- Badge.custom is the
        # discriminator, and the contest badge does not set it.
        won = stats.Badge("weekly_contest_winner", "🏆", "Победитель ×1")
        text, _ = cabinet.badges_view("chat", _user(), [won])
        self.assertNotIn("От администрации", text)
        self.assertIn("🏆 Победитель ×1", text)

    def test_collection_counter_counts_tiers_levels_and_ranks(self):
        # A genuinely blank member -- _user() has 500 messages, which already earns one.
        empty = stats.UserStats(user_id="20")
        unlocked, total = stats.badge_collection_progress(empty)
        # A brand-new member already holds the base painting rank and the first chat tier.
        self.assertEqual(unlocked, 2)
        self.assertEqual(
            total,
            len(stats.PAINTING_BADGE_TIERS) + len(stats.MESSAGE_BADGE_TIERS)
            + len(stats.STREAK_BADGE_TIERS) + len(stats.NIGHT_BADGE_TIERS)
            + 5 + len(stats.CHAT_LEVEL_TIERS) + len(stats.XP_LEVELS),
        )

        # Every painting tier below the current one counts, not just the shown one.
        painter = stats.UserStats(user_id="20", figurines_painted=50)
        more_unlocked, same_total = stats.badge_collection_progress(painter)
        self.assertEqual(same_total, total)
        self.assertEqual(
            more_unlocked - unlocked,
            len(stats.PAINTING_BADGE_TIERS)          # every step, not just the top one
            + (len(stats.XP_LEVELS) - 1)             # every painting rank above the base
            + (len(stats.CHAT_LEVEL_TIERS) - 1),     # 50 figurines is also 10k XP
        )

    def test_defined_custom_badges_widen_the_denominator(self):
        user = stats.UserStats(user_id="20")
        _, base_total = stats.badge_collection_progress(user)
        given = stats.Badge("custom", "🏹", "Лучник", custom=True)

        unlocked, total = stats.badge_collection_progress(
            user, custom_badges=[given], chat_custom_badge_total=4
        )

        self.assertEqual(total, base_total + 4)
        self.assertEqual(unlocked, 2 + 1)

    def test_the_counter_is_rendered_at_the_bottom(self):
        text, _ = cabinet.badges_view("chat", _user(messages=1_000), [])
        self.assertIn("📦 Открыто:", text)
        self.assertGreater(text.index("📦 Открыто:"), text.index("🏅 <b>Значки</b>"))


class FakeAPI:
    """Records what the handler would have sent, so the callback flow can be driven
    without Telegram."""

    def __init__(self):
        self.sent = []
        self.edits = []
        self.answers = []
        self._next_id = 500

    async def send_message(self, chat_id, text, reply_to_message_id=None,
                           reply_markup=None, parse_mode=None):
        self._next_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"message_id": self._next_id}

    async def edit_message_text(self, chat_id, message_id, text,
                                reply_markup=None, parse_mode=None):
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text,
                           "reply_markup": reply_markup})

    async def answer_callback_query(self, callback_query_id, text=None):
        self.answers.append(text)


class CallbackTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)
        self.api = FakeAPI()

    @staticmethod
    def _callback(owner_id, action, actor_id, argument=""):
        return {
            "id": "cb1",
            "data": cabinet.callback_data(owner_id, action, argument),
            "from": {"id": actor_id, "username": "user", "first_name": "Tester"},
            "message": {"message_id": 42, "chat": {"id": 999, "type": "private"}},
        }

    def _run(self, coro):
        return asyncio.run(coro)

    def test_another_member_cannot_press_your_buttons(self):
        # The owner id travels inside the button, so a forwarded menu is inert.
        self._run(
            bot_listener.handle_cabinet_callback(
                self.api, None, None, None,
                self._callback(owner_id=20, action="shop", actor_id=99),
                "chat", {}, log=lambda *_: None,
            )
        )
        self.assertEqual(self.api.answers, ["Это чужой кабинет."])
        self.assertEqual(self.api.edits, [])

    def test_a_missing_home_chat_is_reported_not_crashed(self):
        self._run(
            bot_listener.handle_cabinet_callback(
                self.api, None, None, None,
                self._callback(owner_id=20, action="shop", actor_id=20),
                None, {}, log=lambda *_: None,
            )
        )
        self.assertEqual(self.api.answers, ["Основной чат не настроен."])

    def test_text_entry_actions_open_a_force_reply_and_register_a_flow(self):
        flows = {}
        self._run(
            bot_listener.handle_cabinet_callback(
                self.api, None, None, None,
                self._callback(owner_id=20, action="title_set", actor_id=20),
                "chat", flows, log=lambda *_: None,
            )
        )
        self.assertEqual(len(flows), 1)
        flow = next(iter(flows.values()))
        self.assertEqual(flow["awaiting"], "title")
        self.assertEqual(flow["user_id"], 20)
        self.assertTrue(self.api.sent[0]["reply_markup"]["force_reply"])

    def test_a_reply_that_matches_no_flow_is_left_alone(self):
        handled = self._run(
            bot_listener.handle_cabinet_text_input(
                self.api, None, None,
                {"chat": {"id": 999}, "from": {"id": 20}, "message_id": 7,
                 "text": "просто сообщение"},
                {}, log=lambda *_: None,
            )
        )
        self.assertFalse(handled)


class MenuFallbackTests(unittest.TestCase):
    """The fallback menu's whole risk is firing where it shouldn't, so that is what
    these pin down."""

    def setUp(self):
        self.api = FakeAPI()

    @staticmethod
    def _message(chat_type="private", chat_id=999, user_id=20, text="привет"):
        return {
            "message_id": 7,
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": user_id, "username": "user", "first_name": "Tester"},
            "text": text,
        }

    def _send(self, message, entry=None, cabinet_flows=None, badge_flows=None, last_sent=None):
        return asyncio.run(
            bot_listener.maybe_send_menu(
                self.api, None, None, message, entry,
                cabinet_flows if cabinet_flows is not None else {},
                badge_flows if badge_flows is not None else {},
                last_sent if last_sent is not None else {},
                log=lambda *_: None,
            )
        )

    def test_never_fires_in_a_group(self):
        # A menu posted under ordinary group chatter is spam, and it would print one
        # person's balance in front of everybody.
        self._send(self._message(chat_type="supergroup"))
        self.assertEqual(self.api.sent, [])

    def test_an_unknown_dm_gets_the_menu(self):
        self._send(self._message())
        self.assertEqual(len(self.api.sent), 1)
        self.assertIn("inline_keyboard", self.api.sent[0]["reply_markup"])

    def test_no_home_chat_configured_still_answers_with_something_useful(self):
        self._send(self._message(), entry=None)
        self.assertIn("пока не вижу", self.api.sent[0]["text"])

    def test_a_tracked_member_gets_their_own_cabinet(self):
        async def fake_context(telethon_client, entry, tz, from_user, log=print):
            return _user(), 5_000, 3, 190, 4, 5_000

        with tempfile.TemporaryDirectory() as temporary:
            with patch("stats._stats_dir", return_value=Path(temporary)):
                with patch("bot_listener._cabinet_context", fake_context):
                    self._send(self._message(), entry="chat")

        self.assertIn("Личный кабинет", self.api.sent[0]["text"])
        self.assertIn("🪙 Монеты: 500", self.api.sent[0]["text"])

    def test_it_stays_quiet_while_a_cabinet_answer_is_awaited(self):
        flows = {
            "f1": {
                "created_at": 10**9,  # far in the future of monotonic(), so never expired
                "chat_id": 999,
                "user_id": 20,
                "entry": "chat",
                "awaiting": "title",
                "prompt_message_id": 5,
            }
        }
        self._send(self._message(), cabinet_flows=flows)
        self.assertEqual(self.api.sent, [])

    def test_it_stays_quiet_while_a_badge_answer_is_awaited(self):
        flows = {
            "f1": {
                "created_at": 10**9,
                "chat_id": 999,
                "admin_id": 20,
                "awaiting": "target",
                "prompt_message_id": 5,
            }
        }
        self._send(self._message(), badge_flows=flows)
        self.assertEqual(self.api.sent, [])

    def test_another_members_pending_flow_does_not_mute_you(self):
        flows = {
            "f1": {
                "created_at": 10**9, "chat_id": 999, "user_id": 77,
                "awaiting": "title", "prompt_message_id": 5,
            }
        }
        self._send(self._message(user_id=20), cabinet_flows=flows)
        self.assertEqual(len(self.api.sent), 1)

    def test_a_burst_of_messages_produces_one_menu(self):
        last_sent = {}
        for _ in range(4):
            self._send(self._message(), last_sent=last_sent)
        self.assertEqual(len(self.api.sent), 1)

    def test_separate_dms_do_not_share_the_cooldown(self):
        last_sent = {}
        self._send(self._message(chat_id=111), last_sent=last_sent)
        self._send(self._message(chat_id=222), last_sent=last_sent)
        self.assertEqual(len(self.api.sent), 2)


class RenderCostTests(unittest.TestCase):
    """Every screen used to cost two Telegram entity lookups and a full re-aggregation,
    which is what made the menu feel slow. These pin the fix."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)
        bot_listener._CABINET_CHAT_REF_CACHE.clear()
        bot_listener._CABINET_CONTEXT_CACHE.clear()
        self.addCleanup(bot_listener._CABINET_CHAT_REF_CACHE.clear)
        self.addCleanup(bot_listener._CABINET_CONTEXT_CACHE.clear)

    def _render(self, action, resolutions, contexts):
        async def fake_chat_id(client, entry, cache, log=print):
            resolutions.append(entry)
            return -1001234567890

        async def fake_resolve(client, entry):
            resolutions.append(entry)
            return type("E", (), {"username": "testchat"})()

        async def fake_context(telethon_client, entry, tz, from_user, log=print):
            contexts.append(entry)
            return _user(), 5_000, 3, 190, 4, 5_000

        with patch("bot_listener._resolve_chat_id", fake_chat_id), \
             patch("bot_listener.resolve_chat", fake_resolve), \
             patch("bot_listener._cabinet_context", fake_context):
            return asyncio.run(
                bot_listener._render_cabinet_section(
                    None, FakeAPI(), None, "chat", None, action, "",
                    {"id": 20, "username": "user"}, 999, log=lambda *_: None,
                )
            )

    def test_screens_without_links_never_touch_telegram(self):
        for action in ("main", "shop", "title", "send", "badges"):
            with self.subTest(action=action):
                resolutions = []
                self._render(action, resolutions, [])
                self.assertEqual(resolutions, [], f"{action} resolved a chat entity")

    def test_link_screens_resolve_once_then_reuse_the_cache(self):
        resolutions = []
        self._render("works", resolutions, [])
        first = len(resolutions)
        self.assertGreater(first, 0)

        self._render("works", resolutions, [])
        self._render("stats", resolutions, [])
        self.assertEqual(len(resolutions), first, "chat ref was resolved more than once")

    def test_a_failed_resolution_is_not_cached_permanently(self):
        async def failing_chat_id(client, entry, cache, log=print):
            return None

        async def failing_resolve(client, entry):
            raise RuntimeError("telegram down")

        with patch("bot_listener._resolve_chat_id", failing_chat_id), \
             patch("bot_listener.resolve_chat", failing_resolve):
            asyncio.run(bot_listener._cabinet_chat_ref(None, "chat", {}, log=lambda *_: None))

        self.assertNotIn("chat", bot_listener._CABINET_CHAT_REF_CACHE)

    def test_context_is_reused_across_rapid_navigation(self):
        calls = []

        async def counting_resolve(*args, **kwargs):
            calls.append(1)
            return _user(), 3, 190, 5_000, 4, 5_000

        with patch("stats.resolve_stat_target", counting_resolve):
            for _ in range(5):
                asyncio.run(
                    bot_listener._cabinet_context(
                        None, "chat", None, {"id": 20, "username": "user"}, log=lambda *_: None
                    )
                )

        self.assertEqual(len(calls), 1, "the heavy aggregate ran more than once")

    def test_different_members_do_not_share_a_cached_context(self):
        calls = []

        async def counting_resolve(*args, **kwargs):
            calls.append(1)
            return _user(), 3, 190, 5_000, 4, 5_000

        with patch("stats.resolve_stat_target", counting_resolve):
            for user_id in (20, 21):
                asyncio.run(
                    bot_listener._cabinet_context(
                        None, "chat", None, {"id": user_id, "username": "user"},
                        log=lambda *_: None,
                    )
                )

        self.assertEqual(len(calls), 2)

    def test_a_purchase_is_visible_immediately_despite_the_cached_context(self):
        # Balances are deliberately read from the ledger by each view, never cached with
        # the context -- otherwise buying something would appear to do nothing for 45s.
        user = _user()
        before, _ = cabinet.main_view("chat", user, 5_000, 1, 1, 0)
        economy.purchase("chat", user.user_id, 5_000, economy.find_item("title"))
        after, _ = cabinet.main_view("chat", user, 5_000, 1, 1, 0)

        self.assertIn("🪙 Монеты: 500", before)
        self.assertIn("🪙 Монеты: 100", after)


class MenuRegistrationTests(unittest.TestCase):
    def test_every_advertised_dm_command_resolves_to_a_chat_in_a_dm(self):
        """A published menu must not contain commands that do nothing where they are
        published. _match_allowed_chat never matches a DM, so without the home-chat
        fallback these would all be silent no-ops."""
        private = {"type": "private", "id": 999}
        self.assertEqual(bot_listener._stats_entry_for(private, None, "mychat"), "mychat")
        # A group still reads its own stats, never the home chat's.
        group = {"type": "supergroup", "id": -100123}
        self.assertEqual(bot_listener._stats_entry_for(group, "othergroup", "mychat"), "othergroup")
        # An untracked group resolves to nothing at all, as before.
        self.assertIsNone(bot_listener._stats_entry_for(group, None, "mychat"))

    def test_admin_only_commands_are_not_advertised(self):
        published = {
            command["command"]
            for command in bot_listener.PRIVATE_CHAT_COMMANDS + bot_listener.GROUP_CHAT_COMMANDS
        }
        self.assertFalse(published & {"badge", "weekwinner", "deletepokras"})
        self.assertIn("cabinet", published)

    def test_registration_survives_a_telegram_failure(self):
        class FailingAPI:
            async def set_my_commands(self, commands, scope=None):
                raise bot_listener.ChatSummaryError("boom")

        # The bot must still start without a menu rather than refusing to boot.
        asyncio.run(bot_listener.register_bot_menu(FailingAPI(), log=lambda *_: None))


if __name__ == "__main__":
    unittest.main()
