"""What the pet game's commands and buttons actually put in front of somebody.

The game logic is tested in test_pets*.py without a bot at all; what these pin is the
Telegram half that only exists in bot_listener -- where a screen is allowed to appear, who
is allowed to press a button, and the one ordering rule that has bitten this codebase
before: the tap must be answered BEFORE anything that can block, or the button spins on
the client until it times out.
"""

import asyncio
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app_time
import bot_listener
import economy
import pets
import pets_config as C
import pets_image
import pets_ui
import quests
import pets_updates
import stats

DM_CHAT_ID = 555
MAIN_CHAT_ID = -1001234567890
CHAT = "Chat"
BOT = "testbot"
PLAYER = {"id": 42, "username": "player", "first_name": "Player"}
STRANGER = {"id": 99, "username": "stranger", "first_name": "Stranger"}
RICH_XP = 400_000


def _run(coro):
    return asyncio.run(coro)


def _async_return(value):
    """Stand in for an awaitable stats lookup (words_per_point) with a fixed answer."""
    async def _call(*args, **kwargs):
        return value
    return _call


class FakeApi:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.photos = []
        self.photo_files = []
        self.media_groups = []
        self.answered = []
        self.deleted = []
        # Every call is appended here in order, which is how the "answer the tap first"
        # test can assert on ORDERING rather than just on the fact that both happened.
        self.calls = []

    async def send_message(self, chat_id, text, reply_to_message_id=None,
                           reply_markup=None, parse_mode=None, disable_notification=False):
        self.calls.append("send_message")
        item = {"message_id": 100 + len(self.sent), "chat_id": chat_id,
                "text": text, "reply_markup": reply_markup,
                "disable_notification": disable_notification}
        self.sent.append(item)
        return item

    async def send_photo(self, chat_id, photo, caption, reply_to_message_id=None,
                         reply_markup=None, parse_mode=None):
        self.calls.append("send_photo")
        item = {"message_id": 200 + len(self.photos), "chat_id": chat_id,
                "photo": photo, "caption": caption}
        self.photos.append(item)
        return item

    async def send_photo_file(self, chat_id, path, caption=None, reply_to_message_id=None,
                              reply_markup=None, parse_mode=None, disable_notification=False):
        self.calls.append("send_photo_file")
        item = {
            "message_id": 300 + len(self.photo_files), "chat_id": chat_id,
            "caption": caption, "reply_markup": reply_markup, "size": Path(path).stat().st_size,
            "disable_notification": disable_notification,
        }
        self.photo_files.append(item)
        return item

    async def send_media_group_files(self, chat_id, paths, caption=None, parse_mode=None,
                                     disable_notification=False):
        """One album is one receipt, so it lands in photo_files like a single photo.

        The per-album detail (how many pictures, which paths) is kept separately in
        media_groups for the tests that care that the log ships with the result.
        """
        self.calls.append("send_media_group_files")
        paths = list(paths)
        item = {
            "message_id": 300 + len(self.photo_files), "chat_id": chat_id,
            "caption": caption, "reply_markup": None,
            "size": sum(Path(path).stat().st_size for path in paths),
            "disable_notification": disable_notification,
        }
        self.photo_files.append(item)
        self.media_groups.append({
            "chat_id": chat_id, "paths": [str(path) for path in paths],
            "caption": caption, "count": len(paths),
        })
        return item

    async def edit_message_text(self, chat_id, message_id, text,
                                reply_markup=None, parse_mode=None):
        self.calls.append("edit_message_text")
        # reply_markup is recorded because a redraw's BUTTONS are half of what a screen
        # is: a test that only sees the text cannot tell a working shelf from a dead end.
        self.edits.append({
            "chat_id": chat_id, "message_id": message_id, "text": text,
            "reply_markup": reply_markup,
        })
        return {"message_id": message_id}

    async def answer_callback_query(self, callback_id, text=None):
        self.calls.append("answer_callback_query")
        self.answered.append(text)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class _Resolver:
    """Stands in for stats.resolve_stat_target, which in production reaches Telegram
    through Telethon. `blocking` records when it was called, so a test can prove the
    callback was answered before this point."""

    def __init__(self, api, user_id=PLAYER["id"], name="Player", found=True):
        self.api = api
        self.user_id = user_id
        self.name = name
        self.found = found

    async def __call__(self, client, chat_ref, entry, arg, username, display_name, tz,
                       log=print, frozen_days_for=None):
        self.api.calls.append("resolve_stat_target")
        if not self.found:
            return None, None, 0, None, None, None
        user = SimpleNamespace(user_id=self.user_id, display_name=self.name)
        return user, 1, 1, RICH_XP, 0, RICH_XP


def _cfg():
    return SimpleNamespace(webapp_public_url="https://example.com")


def _message(user, text="/arena", chat_type="private"):
    return {
        "message_id": 5,
        "chat": {"id": DM_CHAT_ID if chat_type == "private" else MAIN_CHAT_ID,
                 "type": chat_type},
        "from": user,
        "text": text,
    }


def _callback(user, action, argument="", owner=None):
    return {
        "id": "cb1",
        "from": user,
        "data": pets_ui.callback_data(owner if owner is not None else PLAYER["id"],
                                      action, argument),
        "message": {"message_id": 900, "chat": {"id": DM_CHAT_ID, "type": "private"}},
    }


def _buttons(message):
    markup = message.get("reply_markup") or {}
    return [b for row in markup.get("inline_keyboard", []) for b in row]


class PetsCommandTests(unittest.TestCase):
    def setUp(self):
        self._dir = TemporaryDirectory()
        path = Path(self._dir.name)
        real = stats._stats_dir
        stats._stats_dir = lambda: path
        self.addCleanup(self._dir.cleanup)
        self.addCleanup(lambda: setattr(stats, "_stats_dir", real))
        # No auto-delete timers in a test: they would leave pending tasks behind.
        patcher = patch.object(bot_listener, "schedule_bot_delete", lambda *a, **k: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _type(self, text="/arena", user=PLAYER, chat_type="private", found=True):
        api = FakeApi()
        with patch.object(stats, "resolve_stat_target", _Resolver(api, found=found)):
            _run(bot_listener.handle_pets_command(
                api, None, _cfg(), None, _message(user, text, chat_type), CHAT, BOT,
                set(), {}, log=lambda *_: None,
            ))
        return api

    def _tap(self, action, argument="", user=PLAYER, owner=None, flows=None):
        api = FakeApi()
        with patch.object(stats, "resolve_stat_target", _Resolver(api)):
            _run(bot_listener.handle_pets_callback(
                api, None, _cfg(), None, _callback(user, action, argument, owner),
                CHAT, flows if flows is not None else {}, set(), log=lambda *_: None,
            ))
        return api

    def test_accepted_quest_is_returned_to_the_player_with_photo_text_tag_and_praise(self):
        api = FakeApi()
        row = {
            "user_id": str(PLAYER["id"]), "photo_file_id": "submitted-photo",
            "title": "Покрась героя", "subject": "Покрась новую миниатюру.",
            "technique": "Добавь аккуратные высветления.", "hint": "Не спеши.",
            "proof": "Покажи готовую работу.", "hashtag": "#quest_hero",
        }
        _run(bot_listener._send_quest_completion(api, row, log=lambda *_: None))

        self.assertEqual(len(api.photos), 1)
        sent = api.photos[0]
        self.assertEqual(sent["chat_id"], str(PLAYER["id"]))
        self.assertEqual(sent["photo"], "submitted-photo")
        self.assertIn("Покрась новую миниатюру", sent["caption"])
        self.assertIn("#quest_hero", sent["caption"])
        self.assertIn("Отличная работа", sent["caption"])
        self.assertLessEqual(len(sent["caption"]), 1024)

    def test_every_menu_action_renders_instead_of_erroring(self):
        """A guard for the whole callback table, not one button.

        «Назад» is on nearly every screen and routes to "main", so a name that exists in
        one handler but not the other takes the entire menu down at once -- which is how
        it shipped: a plain redraw raised NameError, the blanket except turned it into
        "Что-то сломалось", and no test tapped a redraw through the CALLBACK path to
        notice. Walking the table costs nothing and covers every future addition to it.
        """
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Кабанчик", "file", "Player")

        redraws = [
            "main", "info", "cage", "farm", "train", "bag", "fight", "history", "mail",
            "updates", "leaderboard", "pet", "casino", "quests", "dailybonus", "store",
            "collection", "questmods",
        ]
        for action in redraws:
            with self.subTest(action=action):
                api = self._tap(action)
                self.assertTrue(
                    api.edits or api.sent,
                    f"'{action}' produced no message at all",
                )
                rendered = "".join(
                    str(call.get("text", "")) for call in (api.edits + api.sent)
                )
                self.assertNotIn("Что-то сломалось", rendered, f"'{action}' errored")

    def _empty_fight_bank(self, user_id):
        """Put one pet at an empty, freshly checkpointed hourly fight bank."""
        capacity = pets.fight_allowance_breakdown(CHAT, user_id, pets.today())["capacity"]
        data = pets._load(CHAT)
        record = data["pets"][str(user_id)]
        record["fight_bank"] = 0
        record["fight_bank_checkpoint"] = pets.app_now().isoformat()
        record["fight_bank_cap"] = capacity
        pets._save(CHAT, data)

    # ---------------------------------------------------------------------- commands

    def test_the_menu_opens_in_a_dm(self):
        api = self._type()
        self.assertEqual(len(api.sent), 1)
        self.assertIn("Арена", api.sent[0]["text"])
        # web_app buttons carry a url instead of callback data (the Mini App opener, see
        # pets_ui.main_view) -- they are not callbacks and have nothing to parse.
        actions = {
            pets_ui.parse_callback(b["callback_data"])[1]
            for b in _buttons(api.sent[0]) if "callback_data" in b
        }
        self.assertIn("cage", actions)
        self.assertIn("info", actions)

    def test_tamed_pet_menu_uses_two_button_rows_including_fight_notifications(self):
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Боец", "file", "Player")

        _, keyboard = pets_ui.main_view(CHAT, PLAYER["id"], RICH_XP)

        # Paired rows throughout, with one deliberate exception: the daily bonus is
        # reachable without a pet (see the no-pet test below) and so cannot share a row
        # with anything pet-gated, and gets a full-width row of its own instead. Casino
        # and Квесты -- two coming-soon placeholders announced the same way -- now pair
        # with each other rather than casino standing alone. Pinning the exception by
        # name keeps a THIRD lone button from appearing by accident.
        lone = [
            pets_ui.parse_callback(row[0]["callback_data"])[1]
            for row in keyboard["inline_keyboard"] if len(row) != 2
        ]
        self.assertEqual(lone, ["dailybonus"])
        actions = {
            pets_ui.parse_callback(button["callback_data"])[1]
            for row in keyboard["inline_keyboard"] for button in row
        }
        self.assertIn("fightnotify", actions)
        self.assertIn("casino", actions)
        self.assertIn("quests", actions)

    def test_daily_bonus_button_is_offered_without_a_pet(self):
        # The one row on this menu that must survive every `if pet:` gate: chat activity,
        # not the arena, is most members' only income.
        _, keyboard = pets_ui.main_view(CHAT, PLAYER["id"], 0)
        actions = {
            pets_ui.parse_callback(button["callback_data"])[1]
            for row in keyboard["inline_keyboard"] for button in row
        }
        self.assertIn("dailybonus", actions)
        self.assertNotIn("casino", actions)
        self.assertNotIn("quests", actions)

    def test_daily_bonus_claims_once_a_day_and_refuses_a_second_tap(self):
        before = economy.balance(CHAT, PLAYER["id"], RICH_XP)

        api = self._tap("dailybonusclaim")
        self.assertIn("+25", api.edits[0]["text"])
        self.assertEqual(economy.balance(CHAT, PLAYER["id"], RICH_XP), before + 25)
        # The claim button itself must be gone now that today is used up -- otherwise a
        # second tap of the same button would look like it ought to pay again.
        self.assertIn("Ежедневный бонус", api.edits[0]["text"])
        claim_actions = {
            pets_ui.parse_callback(b["callback_data"])[1]
            for row in api.edits[0]["reply_markup"]["inline_keyboard"] for b in row
        }
        self.assertNotIn("dailybonusclaim", claim_actions)

        api = self._tap("dailybonusclaim")
        self.assertIn("уже забран", api.edits[0]["text"])
        self.assertEqual(economy.balance(CHAT, PLAYER["id"], RICH_XP), before + 25)

    def test_the_quest_screen_hands_out_a_quest_and_tells_you_how_to_submit_it(self):
        """It stopped being a placeholder. The screen has to carry the four things a
        player leaves the app with: what the technique is, what small thing to paint, what
        it pays, and -- last, because it is the only part they have to copy -- the hashtag
        that turns a finished model into a submission."""
        text, keyboard = pets_ui.quests_view(CHAT, PLAYER["id"])
        board = quests.daily_quest(CHAT, PLAYER["id"])
        quest = board["quest"]

        self.assertIn(quest["title"], text)
        self.assertIn(quest["subject"], text)
        self.assertIn(quest["hashtag"], text)
        self.assertIn(str(quest["reward"]["gold"]), text)
        # Opening the screen twice is not two quests: an assignment is sticky.
        self.assertEqual(pets_ui.quests_view(CHAT, PLAYER["id"])[0], text)

        actions = {
            pets_ui.parse_callback(button["callback_data"])[1]
            for row in keyboard["inline_keyboard"] for button in row
        }
        # The two slots link to each other, so the paint card offers the real one.
        self.assertEqual(actions, {"main", "questreroll", "quests"})

    def test_rerolling_from_the_menu_swaps_the_quest_and_runs_out(self):
        first = quests.daily_quest(CHAT, PLAYER["id"])["quest"]["code"]
        seen = {first}
        for _ in range(quests.REROLLS_PER_QUEST):
            api = self._tap("questreroll")
            self.assertIn("Новый квест", api.edits[0]["text"])
            seen.add(quests.daily_quest(CHAT, PLAYER["id"])["quest"]["code"])

        # Two rerolls used, so the button is gone and the action refuses.
        _text, keyboard = pets_ui.quests_view(CHAT, PLAYER["id"])
        actions = {
            pets_ui.parse_callback(button["callback_data"])[1]
            for row in keyboard["inline_keyboard"] for button in row
        }
        self.assertEqual(actions, {"main", "quests"})
        api = self._tap("questreroll")
        self.assertIn("Реролов больше нет", api.edits[0]["text"])
        self.assertGreater(len(seen), 1)

    def test_fight_result_notifications_can_be_disabled_from_the_menu(self):
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Боец", "file", "Player")

        self._tap("fightnotify")

        self.assertFalse(pets.fight_result_notifications_enabled(CHAT, PLAYER["id"]))

    def test_private_arena_menu_is_never_scheduled_for_deletion(self):
        deletions = []
        with patch.object(
            bot_listener, "schedule_bot_delete",
            side_effect=lambda *args, **kwargs: deletions.append((args, kwargs)),
        ):
            api = self._type()
        self.assertEqual(len(api.sent), 1)
        self.assertEqual(deletions, [])

    def test_how_to_play_button_opens_the_arena_rules(self):
        api = self._tap("info")
        self.assertIn("Как играть", api.edits[0]["text"])
        self.assertIn("/arena в личке бота", api.edits[0]["text"])
        self.assertIn("своего покраса", api.edits[0]["text"])
        self.assertIn("/duel @user", api.edits[0]["text"])
        self.assertIn("Особые преимущества", api.edits[0]["text"])
        self.assertIn("30%", api.edits[0]["text"])

    def test_arena_screen_shows_empty_bank_capacity_and_hourly_countdown(self):
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Боец", "file", "Player")
        self._empty_fight_bank(PLAYER["id"])

        text, keyboard = pets_ui.fight_view(CHAT, PLAYER["id"], RICH_XP)

        self.assertIn("В запасе боёв: <b>0 из 5</b>", text)
        self.assertIn("Следующий +1 бой через:", text)
        self.assertIn(pets_ui.ARENA_NO_FIGHTS_NOTICE, text)
        actions = {
            pets_ui.parse_callback(button["callback_data"])[1]
            for button in _buttons({"reply_markup": keyboard})
            if button.get("callback_data")
        }
        self.assertNotIn("search", actions)

    def test_farm_menu_offers_eight_duration_buttons_then_starts_and_cancels(self):
        economy.grant(CHAT, PLAYER["id"], C.CAGE_PRICE + C.FARM_UPGRADE_COSTS[0], "test")
        self.assertTrue(pets.buy_cage(CHAT, PLAYER["id"], 0)[0])
        self.assertTrue(pets.tame(CHAT, PLAYER["id"], RICH_XP, "Фермер", "file", "Player")[0])

        rendered = pets_ui.farm_view(CHAT, PLAYER["id"], RICH_XP)
        initial_actions = {
            pets_ui.parse_callback(button["callback_data"])[1] for button in _buttons({"reply_markup": rendered[1]})
        }
        self.assertIn("upfarm", initial_actions)
        self.assertNotIn("farmstart", initial_actions)

        api = self._tap("upfarm")
        self.assertEqual(pets.farm_level(CHAT, PLAYER["id"]), 1)
        rendered = pets_ui.farm_view(CHAT, PLAYER["id"], RICH_XP)
        self.assertIn("Пассивно: +1 монет/ч", rendered[0])
        # One preview line and one button per selectable duration, 1-8 hours.
        self.assertIn("6 ч — 🪙", rendered[0])
        parsed_buttons = [
            pets_ui.parse_callback(button["callback_data"])
            for row in rendered[1]["inline_keyboard"] for button in row
        ]
        farmstart_hours = {argument for _, action, argument in parsed_buttons if action == "farmstart"}
        self.assertEqual(farmstart_hours, {str(hours) for hours in C.FARM_HOUR_CHOICES})
        self.assertNotIn("uphamsterator", {action for _, action, _ in parsed_buttons})
        # Four per row, two rows of four, as asked.
        hour_rows = [
            row for row in rendered[1]["inline_keyboard"]
            if {pets_ui.parse_callback(b["callback_data"])[1] for b in row} == {"farmstart"}
        ]
        self.assertEqual([len(row) for row in hour_rows], [4, 4])

        api = self._tap("farmstart", "3")
        self.assertTrue(pets.is_farming(CHAT, PLAYER["id"]))
        self.assertIn("Питомец отправлен на ферму на 3 ч", api.edits[0]["text"])
        rendered = pets_ui.farm_view(CHAT, PLAYER["id"], RICH_XP)
        actions = {pets_ui.parse_callback(button["callback_data"])[1] for button in _buttons({"reply_markup": rendered[1]})}
        self.assertNotIn("farmstart", actions)
        self.assertIn("farmcancel", actions)

        # Cancelling immediately (well under one hour worked) pays nothing but still ends
        # the shift -- the button and lock both disappear.
        api = self._tap("farmcancel")
        self.assertFalse(pets.is_farming(CHAT, PLAYER["id"]))
        self.assertIn("меньше часа", api.edits[0]["text"])
        rendered = pets_ui.farm_view(CHAT, PLAYER["id"], RICH_XP)
        actions = {pets_ui.parse_callback(button["callback_data"])[1] for button in _buttons({"reply_markup": rendered[1]})}
        self.assertNotIn("farmcancel", actions)
        self.assertIn("farmstart", actions)

    def test_farm_return_notification_marks_only_successful_dm(self):
        receipt = {
            "user_id": str(PLAYER["id"]), "run_id": "run-1", "pet_name": "Фермер", "hours": 3,
            "gold": 24, "xp": 75, "levels_gained": 1, "level": 3,
            "item_code": "amulet_red_button", "auto_equipped": True,
        }
        api = FakeApi()
        with patch.object(pets, "settle_completed_farms", return_value=[]), \
                patch.object(pets, "pending_farm_notifications", return_value=[receipt]), \
                patch.object(pets, "mark_farm_notified", return_value=True) as marked:
            _run(bot_listener._pets_deliver_farm_returns(api, [CHAT], log=lambda *_: None))

        self.assertEqual(api.sent[0]["chat_id"], PLAYER["id"])
        self.assertIn("Ваш питомец <b>Фермер</b> вернулся с фермы (3 ч)", api.sent[0]["text"])
        self.assertIn("Амулет красной кнопки", api.sent[0]["text"])
        self.assertIn("автоматически", api.sent[0]["text"])
        marked.assert_called_once_with(CHAT, str(PLAYER["id"]), "run-1")

    def test_failed_farm_return_dm_stays_pending_for_retry(self):
        receipt = {
            "user_id": str(PLAYER["id"]), "run_id": "retry-me", "pet_name": "Фермер",
            "gold": 14, "xp": 50,
        }

        class ClosedDmApi(FakeApi):
            async def send_message(self, *args, **kwargs):
                raise RuntimeError("user has not started the bot")

        api = ClosedDmApi()
        with patch.object(pets, "settle_completed_farms", return_value=[]), \
                patch.object(pets, "pending_farm_notifications", return_value=[receipt]), \
                patch.object(pets, "mark_farm_notified") as marked:
            _run(bot_listener._pets_deliver_farm_returns(api, [CHAT], log=lambda *_: None))

        marked.assert_not_called()

    def test_daily_chatter_prize_loop_pays_and_announces_once(self):
        # economy.daily_chatter_prizes is the other agent's territory and is idempotent by
        # construction (grant_once) -- this test is not re-proving that, it is proving the
        # loop body only announces when a payout actually happened, and does not re-announce
        # on the next pass once the (mocked) ledger call reports nothing new.
        paid = [
            {"user_id": "1", "username": "artist", "display_name": "Artist", "xp": 410, "messages": 40, "place": 1, "amount": 200},
            {"user_id": "2", "username": None, "display_name": "Второй", "xp": 260, "messages": 30, "place": 2, "amount": 100},
            {"user_id": "3", "username": "c", "display_name": "C", "xp": 95, "messages": 20, "place": 3, "amount": 50},
        ]
        api = FakeApi()
        known_chat_ids = {CHAT: MAIN_CHAT_ID}
        tz = app_time.resolve_timezone()
        with patch.object(economy, "daily_chatter_prizes", side_effect=[paid, []]) as mocked, \
             patch.object(stats, "words_per_point", new=_async_return(5.0)):
            _run(bot_listener._pets_announce_daily_chatter_prizes(
                api, None, [CHAT], known_chat_ids, tz, log=lambda *_: None,
            ))
            _run(bot_listener._pets_announce_daily_chatter_prizes(
                api, None, [CHAT], known_chat_ids, tz, log=lambda *_: None,
            ))

        self.assertEqual(mocked.call_count, 2)
        # Always yesterday IN THE APP TIMEZONE, never the day the loop happens to run on
        # and never a naive local date that could disagree with it near midnight. The
        # chat's words-per-point calibration is threaded through as the third argument --
        # ranking is by earned XP, exactly as the ЕПХ tree ranks the same day.
        yesterday = app_time.now(tz).date() - timedelta(days=1)
        for call in mocked.call_args_list:
            self.assertEqual(call.args, (CHAT, yesterday, 5.0))
        self.assertIn("410 XP", api.sent[0]["text"])
        self.assertEqual(len(api.sent), 1)
        self.assertEqual(api.sent[0]["chat_id"], MAIN_CHAT_ID)
        self.assertIn("@artist", api.sent[0]["text"])
        self.assertIn("200", api.sent[0]["text"])
        self.assertIn("Второй", api.sent[0]["text"])

    def test_daily_chatter_prize_loop_announces_nothing_when_nobody_is_newly_paid(self):
        api = FakeApi()
        with patch.object(economy, "daily_chatter_prizes", return_value=[]), \
             patch.object(stats, "words_per_point", new=_async_return(5.0)):
            _run(bot_listener._pets_announce_daily_chatter_prizes(
                api, None, [CHAT], {CHAT: MAIN_CHAT_ID}, app_time.resolve_timezone(),
                log=lambda *_: None,
            ))
        self.assertEqual(api.sent, [])

    def test_group_arena_command_points_to_the_private_bot_menu(self):
        deletions = []
        with patch.object(
            bot_listener, "schedule_bot_delete",
            side_effect=lambda *args, **kwargs: deletions.append((args, kwargs)),
        ):
            api = self._type(chat_type="group")
        self.assertEqual(api.sent[0]["text"], "Приручить и прокачать существо можно в личке бота.")
        button = _buttons(api.sent[0])[0]
        self.assertEqual(button["text"], "Открыть Арену")
        self.assertEqual(button["url"], f"https://t.me/{BOT}?start=pets")
        self.assertEqual(len(deletions), 2)
        self.assertTrue(all(args[3] == bot_listener.GROUP_PETS_DELETE_AFTER for args, _ in deletions))
        self.assertTrue(any(kwargs.get("trigger_message_id") == 5 for _, kwargs in deletions))

    def test_pet_commands_are_advertised_in_the_group_command_menu(self):
        commands = {command["command"] for command in bot_listener.GROUP_CHAT_COMMANDS}
        self.assertTrue({"arena", "pet", "duel", "testfight"} <= commands)

    def test_admin_testfight_posts_not_silent_result_without_mutating_game(self):
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Кабанчик", "file_a", "Player")
        pets.buy_cage(CHAT, 43, RICH_XP)
        pets.tame(CHAT, 43, RICH_XP, "Тумблер", "file_b", "Bob")
        before = pets._load(CHAT)
        api = FakeApi()

        async def allowed(*args, **kwargs):
            return True

        with patch.object(bot_listener, "_is_chat_admin_or_privileged", new=allowed), \
                patch.object(pets, "record_fight", side_effect=AssertionError("must not record")):
            _run(bot_listener.handle_test_fight_command(
                api, None, _message(PLAYER, "/testfight", "group"), CHAT,
                CHAT, {CHAT: MAIN_CHAT_ID}, log=lambda *_: None,
            ))

        self.assertEqual(pets._load(CHAT), before)
        self.assertEqual(len(api.photo_files), 1)
        result = api.photo_files[0]
        self.assertEqual(result["chat_id"], MAIN_CHAT_ID)
        self.assertFalse(result["disable_notification"])
        self.assertIn("Тестовый бой", result["caption"])
        self.assertIn("Золото, опыт, дроп и количество боёв не изменены", result["caption"])

    def test_non_admin_cannot_start_testfight(self):
        api = FakeApi()

        async def denied(*args, **kwargs):
            return False

        with patch.object(bot_listener, "_is_chat_admin_or_privileged", new=denied):
            _run(bot_listener.handle_test_fight_command(
                api, None, _message(STRANGER, "/testfight", "group"), CHAT,
                CHAT, {CHAT: MAIN_CHAT_ID}, log=lambda *_: None,
            ))

        self.assertEqual(api.photo_files, [])
        self.assertIn("только администраторам", api.sent[0]["text"])

    def test_private_testfight_posts_to_home_chat_and_confirms_in_dm(self):
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Кабанчик", "file_a", "Player")
        pets.buy_cage(CHAT, 43, RICH_XP)
        pets.tame(CHAT, 43, RICH_XP, "Тумблер", "file_b", "Bob")
        api = FakeApi()

        async def allowed(*args, **kwargs):
            return True

        with patch.object(bot_listener, "_is_chat_admin_or_privileged", new=allowed):
            _run(bot_listener.handle_test_fight_command(
                api, None, _message(PLAYER, "/testfight", "private"), CHAT,
                CHAT, {CHAT: MAIN_CHAT_ID}, log=lambda *_: None,
            ))

        self.assertEqual(api.photo_files[0]["chat_id"], MAIN_CHAT_ID)
        self.assertEqual(api.sent[-1]["chat_id"], DM_CHAT_ID)
        self.assertIn("отправлен в основной чат", api.sent[-1]["text"])

    def test_somebody_the_chat_has_never_seen_is_turned_away(self):
        api = self._type(found=False)
        self.assertIn("не отслеживаешься", api.sent[0]["text"])

    # --------------------------------------------------------------------- /pet card

    def test_pet_card_works_in_the_group_and_carries_the_photo(self):
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Кабанчик", "file_abc", "Player")
        api = FakeApi()
        with patch.object(stats, "resolve_stat_target", _Resolver(api)):
            _run(bot_listener.handle_pet_card_command(
                api, None, None, _message(PLAYER, "/pet", "group"), CHAT, "/pet",
                set(), log=lambda *_: None,
            ))
        self.assertEqual(len(api.photos), 1)
        self.assertEqual(api.photos[0]["photo"], "file_abc")
        self.assertIn("Кабанчик", api.photos[0]["caption"])

    def test_pet_card_without_a_creature_says_so_instead_of_failing(self):
        api = FakeApi()
        with patch.object(stats, "resolve_stat_target", _Resolver(api)):
            _run(bot_listener.handle_pet_card_command(
                api, None, None, _message(PLAYER, "/pet"), CHAT, "/pet",
                set(), log=lambda *_: None,
            ))
        self.assertEqual(api.photos, [])
        self.assertIn("нет существа", api.sent[0]["text"])

    def test_pet_card_offers_to_summon_when_the_owner_has_no_pet(self):
        api = FakeApi()
        with patch.object(stats, "resolve_stat_target", _Resolver(api)):
            _run(bot_listener.handle_pet_card_command(
                api, None, None, _message(PLAYER, "/pet", "group"), CHAT, "/pet",
                set(), bot_username=BOT, log=lambda *_: None,
            ))
        button = _buttons(api.sent[0])[0]
        self.assertEqual(button["text"], "Призвать Существо")
        self.assertEqual(button["url"], f"https://t.me/{BOT}?start=pets")

    # --------------------------------------------------------------------- callbacks

    def test_the_tap_is_answered_before_anything_that_can_block(self):
        """A Telethon call made before answerCallbackQuery leaves the button spinning on
        the client forever. Resolving the member goes through Telethon, so the answer must
        come first -- this asserts the ORDER, not merely that both happened."""
        api = self._tap("main")
        self.assertIn("answer_callback_query", api.calls)
        self.assertIn("resolve_stat_target", api.calls)
        self.assertLess(
            api.calls.index("answer_callback_query"),
            api.calls.index("resolve_stat_target"),
        )

    def test_a_forwarded_menu_cannot_spend_somebody_elses_coins(self):
        api = self._tap("buycage", user=STRANGER, owner=PLAYER["id"])
        self.assertEqual(api.answered, ["Это чужая арена."])
        self.assertEqual(api.edits, [])
        self.assertEqual(api.sent, [])
        # And nothing was bought.
        self.assertIsNone(pets.get_pet(CHAT, PLAYER["id"]))

    def test_buying_a_cage_through_the_button_actually_debits(self):
        before = economy.balance(CHAT, PLAYER["id"], RICH_XP)
        api = self._tap("buycage")
        self.assertEqual(
            economy.balance(CHAT, PLAYER["id"], RICH_XP), before - C.CAGE_PRICE
        )
        self.assertEqual(pets.cage_level(CHAT, PLAYER["id"]), 1)
        self.assertTrue(api.edits, "the screen should be redrawn in place")

    def test_an_unaffordable_purchase_is_refused_on_screen_not_silently(self):
        api = FakeApi()
        with patch.object(stats, "resolve_stat_target", _Resolver(api)), \
                patch.object(economy, "balance", return_value=0):
            _run(bot_listener.handle_pets_callback(
                api, None, _cfg(), None, _callback(PLAYER, "buycage"),
                CHAT, {}, set(), log=lambda *_: None,
            ))
        self.assertTrue(api.edits)
        self.assertIsNone(pets.get_pet(CHAT, PLAYER["id"]))

    def test_taming_asks_for_a_photo_and_opens_a_flow(self):
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        flows = {}
        api = self._tap("tame", flows=flows)
        self.assertEqual(len(flows), 1)
        flow = next(iter(flows.values()))
        self.assertEqual(flow["awaiting"], "photo_tame")
        self.assertTrue(api.sent[0]["reply_markup"]["force_reply"])
        self.assertIn("собственная раскрашенная фигурка", api.sent[0]["text"])

    def test_the_shop_accessory_tab_opens_a_shelf_with_a_working_buy_button(self):
        """End-to-end through the callback router, not just the view function: the tab
        was previously wired to the slot catalogue, where the buy button's page depended
        on how much the player already owned."""
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Бублик", "file_a", "Player")

        api = self._tap("shopslot", "amulet")

        self.assertTrue(api.edits)
        buttons = _buttons(api.edits[-1])
        self.assertNotIn("только из боёв", api.edits[-1]["text"])
        self.assertTrue(
            [b for b in buttons if pets_ui.parse_callback(b["callback_data"])[1] == "buy"],
            api.edits[-1]["text"],
        )

    def test_opening_updates_marks_the_latest_entry_read_and_redraws_the_log(self):
        self.assertTrue(pets_updates.has_unread(CHAT, PLAYER["id"]))

        api = self._tap("updates")

        self.assertFalse(pets_updates.has_unread(CHAT, PLAYER["id"]))
        self.assertTrue(api.edits)
        self.assertIn("Обновления", api.edits[-1]["text"])

    def test_taming_without_a_cage_shows_the_cage_screen_rather_than_a_prompt(self):
        flows = {}
        self._tap("tame", flows=flows)
        self.assertEqual(flows, {})

    def _tame_trade_pair(self):
        for user_id, name in ((PLAYER["id"], "Giver"), (43, "Receiver")):
            pets.buy_cage(CHAT, user_id, RICH_XP)
            pets.tame(CHAT, user_id, RICH_XP, name, f"file_{user_id}", name)
        data = pets._load(CHAT)
        data["pets"][str(PLAYER["id"])]["level"] = C.GIFT_MIN_PET_LEVEL
        pets._save(CHAT, data)
        # Use the live game wrapper so selection and purchase share the same app-clock
        # window; the pure catalogue helper intentionally uses the process clock.
        item = next(item for item in pets.daily_storefront_weapons(CHAT)
                    if item.rarity not in {"rare", "legendary"})
        economy.grant(CHAT, PLAYER["id"], item.price, "test")
        self.assertTrue(pets.buy_item(CHAT, PLAYER["id"], RICH_XP, item.code)[0])
        return item

    @staticmethod
    def _gift_reply(text, prompt_message_id):
        message = _message(PLAYER, text)
        message["message_id"] = 77
        message["reply_to_message"] = {"message_id": prompt_message_id}
        return message

    def test_gift_button_binds_item_to_owner_flow_and_valid_reply_transfers_once(self):
        item = self._tame_trade_pair()
        api = FakeApi()
        flows = {}
        with patch.object(stats, "resolve_stat_target", _Resolver(api)):
            _run(bot_listener.handle_pets_callback(
                api, None, _cfg(), None, _callback(PLAYER, "gift", item.code), CHAT,
                flows, set(), log=lambda *_: None,
            ))
        flow = next(iter(flows.values()))
        self.assertEqual(flow["awaiting"], "gift_target")
        self.assertEqual(flow["item_code"], item.code)
        self.assertEqual(flow["user_id"], PLAYER["id"])
        self.assertTrue(api.sent[0]["reply_markup"]["force_reply"])

        giver = SimpleNamespace(user_id=PLAYER["id"], display_name="Player")
        receiver = SimpleNamespace(user_id=43, display_name="Receiver")

        async def resolve(client, chat_ref, entry, arg, *args, **kwargs):
            return (giver, 1, 1, RICH_XP, 0, RICH_XP) if not arg else (
                receiver, 1, 1, RICH_XP, 0, RICH_XP
            )

        with patch.object(stats, "resolve_stat_target", resolve):
            handled = _run(bot_listener.maybe_handle_pets_flow_message(
                api, None, None, self._gift_reply("@receiver", flow["prompt_message_id"]),
                flows, BOT, set(), log=lambda *_: None,
            ))
        self.assertTrue(handled)
        self.assertEqual(flows, {})
        self.assertNotIn(item.code, pets.get_pet(CHAT, PLAYER["id"])["inventory"])
        self.assertIn(item.code, pets.get_pet(CHAT, 43)["inventory"])
        self.assertEqual(api.sent[-1]["chat_id"], 43)  # recipient DM attempt
        self.assertFalse(_run(bot_listener.maybe_handle_pets_flow_message(
            api, None, None, self._gift_reply("@receiver", flow["prompt_message_id"]),
            flows, BOT, set(), log=lambda *_: None,
        )))

    def test_invalid_or_petless_gift_recipient_never_moves_the_item(self):
        item = self._tame_trade_pair()
        api = FakeApi()
        flows = {}
        with patch.object(stats, "resolve_stat_target", _Resolver(api)):
            _run(bot_listener.handle_pets_callback(
                api, None, _cfg(), None, _callback(PLAYER, "gift", item.code), CHAT,
                flows, set(), log=lambda *_: None,
            ))
        flow = next(iter(flows.values()))
        with patch.object(stats, "resolve_stat_target", _Resolver(api)):
            self.assertTrue(_run(bot_listener.maybe_handle_pets_flow_message(
                api, None, None, self._gift_reply("not-a-user", flow["prompt_message_id"]),
                flows, BOT, set(), log=lambda *_: None,
            )))
        self.assertIn(item.code, pets.get_pet(CHAT, PLAYER["id"])["inventory"])

        # A syntactically valid name can resolve to a tracked member without a tamed pet.
        petless = SimpleNamespace(user_id=88, display_name="Petless")
        giver = SimpleNamespace(user_id=PLAYER["id"], display_name="Player")

        async def resolve(client, chat_ref, entry, arg, *args, **kwargs):
            return (giver, 1, 1, RICH_XP, 0, RICH_XP) if not arg else (
                petless, 1, 1, RICH_XP, 0, RICH_XP
            )

        with patch.object(stats, "resolve_stat_target", resolve):
            self.assertTrue(_run(bot_listener.maybe_handle_pets_flow_message(
                api, None, None, self._gift_reply("@petless", flow["prompt_message_id"]),
                flows, BOT, set(), log=lambda *_: None,
            )))
        self.assertEqual(flows, {})
        self.assertIn(item.code, pets.get_pet(CHAT, PLAYER["id"])["inventory"])

    def test_duplicate_receiver_rejects_gift_and_sell_callback_credits_redraw(self):
        item = self._tame_trade_pair()
        data = pets._load(CHAT)
        data["pets"]["43"]["inventory"].append(item.code)
        pets._save(CHAT, data)
        api = FakeApi()
        flows = {}
        with patch.object(stats, "resolve_stat_target", _Resolver(api)):
            _run(bot_listener.handle_pets_callback(
                api, None, _cfg(), None, _callback(PLAYER, "gift", item.code), CHAT,
                flows, set(), log=lambda *_: None,
            ))
        flow = next(iter(flows.values()))
        giver = SimpleNamespace(user_id=PLAYER["id"], display_name="Player")
        receiver = SimpleNamespace(user_id=43, display_name="Receiver")

        async def resolve(client, chat_ref, entry, arg, *args, **kwargs):
            return (giver, 1, 1, RICH_XP, 0, RICH_XP) if not arg else (
                receiver, 1, 1, RICH_XP, 0, RICH_XP
            )

        with patch.object(stats, "resolve_stat_target", resolve):
            _run(bot_listener.maybe_handle_pets_flow_message(
                api, None, None, self._gift_reply("@receiver", flow["prompt_message_id"]),
                flows, BOT, set(), log=lambda *_: None,
            ))
        self.assertIn(item.code, pets.get_pet(CHAT, PLAYER["id"])["inventory"])

        # Remove the test-only duplicate then sell through the actual callback.
        data = pets._load(CHAT)
        data["pets"]["43"]["inventory"].remove(item.code)
        pets._save(CHAT, data)
        before = economy.balance(CHAT, PLAYER["id"], RICH_XP)
        sell_api = self._tap("sell", item.code)
        self.assertEqual(economy.balance(CHAT, PLAYER["id"], RICH_XP), before + C.resale_value(item))
        self.assertNotIn(item.code, pets.get_pet(CHAT, PLAYER["id"])["inventory"])
        self.assertTrue(sell_api.edits)

    def test_every_menu_action_renders_without_blowing_up(self):
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Кабанчик", "file_abc", "Player")
        for action, argument in [
            ("main", ""), ("cage", ""), ("train", ""), ("bag", ""), ("bagitems", "weapon,0"), ("fight", ""),
            ("history", ""), ("pet", ""), ("slot", "weapon"), ("up", "strength"),
            ("up10", "luck"), ("search", ""),
        ]:
            api = self._tap(action, argument)
            self.assertTrue(
                api.edits or api.sent,
                f"action {action!r} drew nothing at all",
            )

    def test_result_image_item_receipt_contains_weapon_stats_and_amulet_effect(self):
        pet = {
            "equipped": {
                "weapon": "w001",
                "amulet": "amulet_left_sock",
            },
        }

        weapon = bot_listener._pets_image_item(pet, "weapon")
        amulet = bot_listener._pets_image_item(pet, "amulet")

        self.assertEqual(weapon["name"], C.find_item("w001").name)
        self.assertEqual(weapon["rarity"], C.find_item("w001").rarity)
        self.assertEqual(weapon["bonuses"], dict(C.find_item("w001").bonuses))
        self.assertTrue(amulet["effect"])
        self.assertEqual(amulet["bonuses"], dict(C.find_item("amulet_left_sock").bonuses))
        self.assertIsNone(bot_listener._pets_image_item(pet, "boots"))

    def test_fight_posts_one_composite_result_image(self):
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Кабанчик", "file_a", "Player")
        pets.buy_cage(CHAT, 43, RICH_XP)
        pets.tame(CHAT, 43, RICH_XP, "Тумблер", "file_b", "Bob")
        api = FakeApi()

        _run(bot_listener._pets_run_fight(
            api, DM_CHAT_ID, 900, CHAT, PLAYER["id"], "43", RICH_XP, log=lambda *_: None,
        ))

        self.assertEqual(len(api.photo_files), 2)
        self.assertEqual(api.photo_files[0]["chat_id"], PLAYER["id"])
        self.assertGreater(api.photo_files[0]["size"], 1_000)
        self.assertTrue(any(
            outcome in api.photo_files[0]["caption"]
            for outcome in ("Победа", "Поражение", "Ничья")
        ))
        defender_copy = api.photo_files[1]
        self.assertEqual(defender_copy["chat_id"], 43)
        self.assertIn("Вас атаковал Player", defender_copy["caption"])
        self.assertIsNone(defender_copy["reply_markup"])
        self.assertTrue(api.photo_files[0]["disable_notification"])
        self.assertTrue(defender_copy["disable_notification"])

    def test_battle_log_ships_in_the_same_album_as_the_result_board(self):
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Кабанчик", "file_a", "Player")
        pets.buy_cage(CHAT, 43, RICH_XP)
        pets.tame(CHAT, 43, RICH_XP, "Тумблер", "file_b", "Bob")
        api = FakeApi()

        _run(bot_listener._pets_run_fight(
            api, DM_CHAT_ID, 900, CHAT, PLAYER["id"], "43", RICH_XP, log=lambda *_: None,
        ))

        # Both participants get one album carrying the result board and the log, in that
        # order -- never the result alone and never the log as a stray second message.
        self.assertEqual([group["chat_id"] for group in api.media_groups], [PLAYER["id"], 43])
        for group in api.media_groups:
            self.assertEqual(group["count"], 2)
            self.assertNotEqual(group["paths"][0], group["paths"][1])
        self.assertNotIn("send_photo_file", api.calls)

    def test_the_attacker_keeps_action_buttons_that_an_album_cannot_carry(self):
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Кабанчик", "file_a", "Player")
        pets.buy_cage(CHAT, 43, RICH_XP)
        pets.tame(CHAT, 43, RICH_XP, "Тумблер", "file_b", "Bob")
        api = FakeApi()

        _run(bot_listener._pets_run_fight(
            api, DM_CHAT_ID, 900, CHAT, PLAYER["id"], "43", RICH_XP, log=lambda *_: None,
        ))

        # sendMediaGroup takes no reply_markup, so the "another fight" loop would quietly
        # disappear if the buttons were not sent separately to the attacker alone.
        follow_ups = [item for item in api.sent if item["reply_markup"]]
        self.assertEqual(len(follow_ups), 1)
        self.assertEqual(follow_ups[0]["chat_id"], PLAYER["id"])
        # Silent like the album it follows -- the arena never pings for a result.
        self.assertTrue(follow_ups[0]["disable_notification"])
        actions = {
            pets_ui.parse_callback(button["callback_data"])[1]
            for row in follow_ups[0]["reply_markup"]["inline_keyboard"] for button in row
        }
        self.assertIn("search", actions)

    def test_a_failed_log_render_still_delivers_the_result_board(self):
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Кабанчик", "file_a", "Player")
        pets.buy_cage(CHAT, 43, RICH_XP)
        pets.tame(CHAT, 43, RICH_XP, "Тумблер", "file_b", "Bob")
        api = FakeApi()

        with patch.object(pets_image, "render_fight_log", side_effect=RuntimeError("boom")):
            _run(bot_listener._pets_run_fight(
                api, DM_CHAT_ID, 900, CHAT, PLAYER["id"], "43", RICH_XP, log=lambda *_: None,
            ))

        # The transcript is a companion picture; losing it must not cost the receipt.
        self.assertEqual(api.media_groups, [])
        self.assertEqual([item["chat_id"] for item in api.photo_files], [PLAYER["id"], 43])

    def test_opted_out_player_does_not_receive_a_private_fight_result(self):
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Кабанчик", "file_a", "Player")
        pets.buy_cage(CHAT, 43, RICH_XP)
        pets.tame(CHAT, 43, RICH_XP, "Тумблер", "file_b", "Bob")
        pets.toggle_fight_result_notifications(CHAT, 43)
        api = FakeApi()

        _run(bot_listener._pets_run_fight(
            api, DM_CHAT_ID, 900, CHAT, PLAYER["id"], "43", RICH_XP, log=lambda *_: None,
        ))

        self.assertEqual([item["chat_id"] for item in api.photo_files], [PLAYER["id"]])

    def test_rare_weapon_drop_stays_in_the_winner_private_result(self):
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Attacker", "file_a", "Player", "player")
        pets.buy_cage(CHAT, 43, RICH_XP)
        pets.tame(CHAT, 43, RICH_XP, "Defender", "file_b", "Bob", "bob")
        rare = next(
            item for item in C.ITEMS
            if item.slot == "weapon" and item.source == "drop" and item.rarity == "rare"
        )
        reward = {
            "draw": False, "gold": 10, "loss_gold": 0, "xp": 25,
            "levels_gained": 0, "level": 1, "dropped_item": rare.code,
            "opponent_gold": 0, "opponent_loss_gold": 3, "opponent_xp": 5,
            "opponent_levels_gained": 0, "opponent_level": 1,
            "opponent_dropped_item": None,
        }
        api = FakeApi()

        with patch.object(pets, "record_fight", return_value=reward):
            _run(bot_listener._pets_run_fight(
                api, MAIN_CHAT_ID, 900, CHAT, PLAYER["id"], "43", RICH_XP,
                log=lambda *_: None,
            ))

        # The receipts are an album now, so the winner's action buttons follow in their
        # own DM message. What must stay true is that nothing reaches the group and that
        # the dropped item is never named outside the private receipt.
        self.assertFalse(any(item["chat_id"] == MAIN_CHAT_ID for item in api.sent))
        self.assertNotIn(rare.name, "".join(item["text"] for item in api.sent))
        self.assertEqual([item["chat_id"] for item in api.photo_files], [PLAYER["id"], 43])
        self.assertIn(rare.name, api.photo_files[0]["caption"])

    def test_private_arena_attack_keeps_results_with_the_two_players(self):
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Кабанчик", "file_a", "Player")
        pets.buy_cage(CHAT, 43, RICH_XP)
        pets.tame(CHAT, 43, RICH_XP, "Тумблер", "file_b", "Bob")
        api = FakeApi()
        deletions = []

        with patch.object(stats, "resolve_stat_target", _Resolver(api)):
            _run(bot_listener.handle_pets_callback(
                api, None, _cfg(), None, _callback(PLAYER, "attack", "43"), CHAT,
                {}, set(), bot_username=BOT, known_chat_ids={CHAT: MAIN_CHAT_ID}, log=lambda *_: None,
            ))

        self.assertEqual([item["chat_id"] for item in api.photo_files], [PLAYER["id"], 43])
        self.assertFalse(any(item["chat_id"] == MAIN_CHAT_ID for item in api.photo_files))

    def test_stale_daily_capped_arena_card_silently_deals_another_opponent(self):
        """A card can become stale between search and tap; never publish that refusal."""
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Attacker", "file_a", "Player")
        for opponent_id, name in ((43, "Capped"), (44, "Available")):
            pets.buy_cage(CHAT, opponent_id, RICH_XP)
            pets.tame(CHAT, opponent_id, RICH_XP, name, f"file_{opponent_id}", name)
        result = SimpleNamespace(winner=str(PLAYER["id"]), loser="43")
        with patch("random.random", return_value=1.0):
            for _ in range(C.ARENA_SAME_OPPONENT_DAILY_LIMIT):
                pets.record_fight(CHAT, PLAYER["id"], "43", result, pets.today())

        api = self._tap("attack", "43")

        self.assertEqual(api.sent, [])
        self.assertEqual(api.photo_files, [])
        self.assertTrue(api.edits)
        self.assertIn("Available", api.edits[-1]["text"])
        self.assertNotIn("можно атаковать", api.edits[-1]["text"])

    def test_exhausted_arena_card_shows_private_popup_and_never_posts_to_group(self):
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Attacker", "file_a", "Player")
        pets.buy_cage(CHAT, 43, RICH_XP)
        pets.tame(CHAT, 43, RICH_XP, "Opponent", "file_b", "Bob")
        self._empty_fight_bank(PLAYER["id"])
        api = FakeApi()

        async def must_not_resolve_group(*args, **kwargs):
            raise AssertionError("exhausted arena taps must not resolve the public chat")

        with patch.object(stats, "resolve_stat_target", _Resolver(api)), patch.object(
            bot_listener, "_resolve_chat_id", must_not_resolve_group,
        ):
            _run(bot_listener.handle_pets_callback(
                api, None, _cfg(), None, _callback(PLAYER, "attack", "43"), CHAT,
                {}, set(), bot_username=BOT, known_chat_ids={CHAT: MAIN_CHAT_ID},
                log=lambda *_: None,
            ))

        self.assertEqual(api.answered, [pets_ui.ARENA_NO_FIGHTS_NOTICE])
        self.assertEqual(api.sent, [])
        self.assertEqual(api.photo_files, [])
        self.assertEqual(api.edits[-1]["chat_id"], DM_CHAT_ID)
        self.assertIn(pets_ui.ARENA_NO_FIGHTS_NOTICE, api.edits[-1]["text"])

    def test_seven_level_advantage_is_a_normal_combat(self):
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Adult", "file_a", "Player")
        pets.buy_cage(CHAT, 43, RICH_XP)
        pets.tame(CHAT, 43, RICH_XP, "Child", "file_b", "Bob")
        data = pets._load(CHAT)
        data["pets"][str(PLAYER["id"])]["level"] = 8
        data["pets"]["43"]["level"] = 1
        pets._save(CHAT, data)
        api = FakeApi()
        with patch.object(bot_listener.pets_combat, "simulate", wraps=bot_listener.pets_combat.simulate) as simulate:
            _run(bot_listener._pets_run_fight(
                api, DM_CHAT_ID, 900, CHAT, PLAYER["id"], "43", RICH_XP,
                log=lambda *_: None,
            ))

        simulate.assert_called_once()
        self.assertEqual(len(api.photo_files), 2)

    def test_six_level_advantage_remains_a_normal_combat(self):
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Adult", "file_a", "Player")
        pets.buy_cage(CHAT, 43, RICH_XP)
        pets.tame(CHAT, 43, RICH_XP, "Child", "file_b", "Bob")
        data = pets._load(CHAT)
        data["pets"][str(PLAYER["id"])]["level"] = 7
        data["pets"]["43"]["level"] = 1
        pets._save(CHAT, data)
        api = FakeApi()

        with patch.object(bot_listener.pets_combat, "simulate", wraps=bot_listener.pets_combat.simulate) as simulate:
            _run(bot_listener._pets_run_fight(
                api, DM_CHAT_ID, 900, CHAT, PLAYER["id"], "43", RICH_XP,
                log=lambda *_: None,
            ))

        simulate.assert_called_once()
        self.assertEqual(len(api.photo_files), 2)

    def test_opponent_rerolls_are_unlimited_and_keep_only_the_current_card_in_callback(self):
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Кабанчик", "file_a", "Player")
        pets.buy_cage(CHAT, 43, RICH_XP)
        pets.tame(CHAT, 43, RICH_XP, "Тумблер", "file_b", "Bob")

        _, keyboard = pets_ui.opponent_view(CHAT, PLAYER["id"], "43", RICH_XP)
        search = next(b for b in _buttons({"reply_markup": keyboard}) if b["text"].startswith("🔍"))
        _, action, argument = pets_ui.parse_callback(search["callback_data"])
        self.assertEqual(action, "search")
        self.assertEqual(pets_ui.parse_search_argument(argument), "43")
        largest_callback = pets_ui.callback_data(
            9_223_372_036_854_775_807,
            "search",
            pets_ui.search_argument(9_223_372_036_854_775_807),
        )
        self.assertLessEqual(len(largest_callback.encode("utf-8")), pets_ui.MAX_CALLBACK_BYTES)

        # The next button has the same bounded shape regardless of how often it is used;
        # there is no counter to cap after the third reroll.
        _, rerolled = pets_ui.opponent_view(CHAT, PLAYER["id"], "43", RICH_XP)
        self.assertTrue(any(b["text"].startswith("🔍") for b in _buttons({"reply_markup": rerolled})))

    def test_opponent_card_does_not_reveal_power_rating(self):
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Кабанчик", "file_a", "Player")
        pets.buy_cage(CHAT, 43, RICH_XP)
        pets.tame(CHAT, 43, RICH_XP, "Тумблер", "file_b", "Bob")

        text, _ = pets_ui.opponent_view(CHAT, PLAYER["id"], "43", RICH_XP)

        self.assertNotIn("Боевой рейтинг", text)

    def test_reroll_never_immediately_repeats_when_another_valid_opponent_exists(self):
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Кабанчик", "file_a", "Player")
        for opponent_id, name in ((43, "Тумблер"), (44, "Лис")):
            pets.buy_cage(CHAT, opponent_id, RICH_XP)
            pets.tame(CHAT, opponent_id, RICH_XP, name, f"file_{opponent_id}", name)

        api = self._tap("search", "43")

        self.assertTrue(api.edits)
        self.assertIn("Лис", api.edits[-1]["text"])

    def test_bare_group_duel_starts_a_target_flow(self):
        api = FakeApi()
        flows = {}

        _run(bot_listener.handle_duel_command(
            api, None, None, _message(PLAYER, "/duel", "group"), CHAT,
            "/duel", BOT, set(), pets_flows=flows, log=lambda *_: None,
        ))

        self.assertEqual(api.sent[0]["text"], "@player, на кого нападаем?")
        flow = next(iter(flows.values()))
        self.assertEqual(flow["awaiting"], "duel_target")
        self.assertEqual(flow["command_message_id"], 5)
        self.assertEqual(flow["prompt_message_id"], api.sent[0]["message_id"])

    def test_invalid_duel_target_is_removed_with_the_prompt_after_five_seconds(self):
        api = FakeApi()
        flows = {
            "duel": {
                "awaiting": "duel_target", "chat_id": MAIN_CHAT_ID,
                "user_id": PLAYER["id"], "entry": CHAT,
                "command_message_id": 5, "prompt_message_id": 10,
                "created_at": 0,
            },
        }
        reply = _message(PLAYER, "not a user", "group")
        reply["message_id"] = 6
        deletions = []

        with patch.object(
            bot_listener, "schedule_bot_delete",
            side_effect=lambda *args, **kwargs: deletions.append((args, kwargs)),
        ), patch.object(bot_listener.time, "monotonic", return_value=1):
            handled = _run(bot_listener.maybe_handle_pets_flow_message(
                api, None, None, reply, flows, BOT, set(), log=lambda *_: None,
            ))

        self.assertTrue(handled)
        self.assertEqual(flows, {})
        self.assertEqual(api.sent[-1]["text"], "Пользователь не найден.")
        self.assertEqual(deletions[0][0][3], bot_listener.DUEL_TARGET_INVALID_DELETE_AFTER)
        # 100 is the id FakeApi hands the "not found" reply -- the notice goes with the
        # command, the prompt and the answer rather than being left behind on its own.
        self.assertEqual(set(deletions[0][0][2]), {5, 6, 10, 100})

    def test_direct_duel_refusal_is_removed_after_exactly_five_seconds(self):
        """A public /duel rejection must not linger in the group for the normal 30s."""
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Attacker", "file_a", "Player")
        pets.buy_cage(CHAT, 43, RICH_XP)
        pets.tame(CHAT, 43, RICH_XP, "Defender", "file_b", "Bob")
        api = FakeApi()
        challenger = SimpleNamespace(user_id=PLAYER["id"], display_name="Player")
        target = SimpleNamespace(user_id=43, display_name="Bob")
        deletions = []

        async def resolve(*args, **kwargs):
            return (challenger, 1, 1, RICH_XP, 0, RICH_XP) if args[3] == "" else (
                target, 1, 1, RICH_XP, 0, RICH_XP
            )

        with patch.object(stats, "resolve_stat_target", resolve), \
                patch.object(pets, "claim_duel", return_value=(False, "already fought")), \
                patch.object(bot_listener, "schedule_bot_delete",
                             side_effect=lambda *args, **kwargs: deletions.append((args, kwargs))):
            _run(bot_listener.handle_duel_command(
                api, None, None, _message(PLAYER, "/duel @bobby", "group"), CHAT,
                "/duel @bobby", BOT, set(), log=lambda *_: None,
            ))

        self.assertEqual(len(api.sent), 1)
        self.assertEqual(deletions[-1][0][3], 5)
        self.assertIn(
            api.sent[0]["message_id"],
            deletions[-1][0][2] + [deletions[-1][1].get("trigger_message_id")],
        )

    def test_force_reply_duel_refusal_is_removed_after_exactly_five_seconds(self):
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Attacker", "file_a", "Player")
        pets.buy_cage(CHAT, 43, RICH_XP)
        pets.tame(CHAT, 43, RICH_XP, "Defender", "file_b", "Bob")
        api = FakeApi()
        flows = {
            "duel": {
                "awaiting": "duel_target", "chat_id": MAIN_CHAT_ID,
                "user_id": PLAYER["id"], "entry": CHAT,
                "command_message_id": 5, "prompt_message_id": 10, "created_at": 0,
            },
        }
        reply = _message(PLAYER, "@bobby", "group")
        reply["message_id"] = 6
        challenger = SimpleNamespace(user_id=PLAYER["id"], display_name="Player")
        target = SimpleNamespace(user_id=43, display_name="Bob")
        deletions = []

        async def resolve(*args, **kwargs):
            return (challenger, 1, 1, RICH_XP, 0, RICH_XP) if args[3] == "" else (
                target, 1, 1, RICH_XP, 0, RICH_XP
            )

        with patch.object(bot_listener.time, "monotonic", return_value=1), \
                patch.object(stats, "resolve_stat_target", resolve), \
                patch.object(pets, "claim_duel", return_value=(False, "already fought")), \
                patch.object(bot_listener, "schedule_bot_delete",
                             side_effect=lambda *args, **kwargs: deletions.append((args, kwargs))):
            handled = _run(bot_listener.maybe_handle_pets_flow_message(
                api, None, None, reply, flows, BOT, set(), log=lambda *_: None,
            ))

        self.assertTrue(handled)
        self.assertEqual(len(api.sent), 1)
        self.assertEqual(deletions[-1][0][3], 5)
        self.assertIn(
            api.sent[0]["message_id"],
            deletions[-1][0][2] + [deletions[-1][1].get("trigger_message_id")],
        )

    def test_valid_duel_target_deletes_the_exchange_before_starting_the_duel(self):
        api = FakeApi()
        flows = {
            "duel": {
                "awaiting": "duel_target", "chat_id": MAIN_CHAT_ID,
                "user_id": PLAYER["id"], "entry": CHAT,
                "command_message_id": 5, "prompt_message_id": 100,
                "created_at": 0,
            },
        }
        # A real username, not "@bob": Telegram's own minimum is five characters and the
        # flow rejects anything shorter as "user not found".
        reply = _message(PLAYER, "@bobby", "group")
        reply["message_id"] = 6
        started = []

        async def start_duel(*args, **kwargs):
            started.append((args, kwargs))

        with patch.object(bot_listener.time, "monotonic", return_value=1), \
                patch.object(bot_listener, "handle_duel_command", side_effect=start_duel):
            handled = _run(bot_listener.maybe_handle_pets_flow_message(
                api, None, None, reply, flows, BOT, set(), log=lambda *_: None,
            ))

        self.assertTrue(handled)
        self.assertEqual(api.deleted, [(MAIN_CHAT_ID, 5), (MAIN_CHAT_ID, 100), (MAIN_CHAT_ID, 6)])
        self.assertEqual(started[0][0][5], "/duel @bobby")
        self.assertTrue(started[0][1]["target_from_followup"])

    def test_group_duel_keeps_results_with_both_players(self):
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Кабанчик", "file_a", "Player")
        pets.buy_cage(CHAT, 43, RICH_XP)
        pets.tame(CHAT, 43, RICH_XP, "Тумблер", "file_b", "Bob")
        api = FakeApi()
        challenger = SimpleNamespace(user_id=PLAYER["id"], display_name="Player")
        target = SimpleNamespace(user_id=43, display_name="Bob")
        deletions = []

        async def resolve(*args, **kwargs):
            return (challenger, 1, 1, RICH_XP, 0, RICH_XP) if args[3] == "" else (target, 1, 1, RICH_XP, 0, RICH_XP)

        with patch.object(stats, "resolve_stat_target", resolve), patch.object(
            bot_listener, "schedule_bot_delete",
            side_effect=lambda *args, **kwargs: deletions.append((args, kwargs)),
        ):
            _run(bot_listener.handle_duel_command(
                api, None, None, _message(PLAYER, "/duel @bob", "group"), CHAT,
                "/duel @bob", BOT, set(), log=lambda *_: None,
            ))

        self.assertEqual(len(api.photo_files), 2)
        self.assertEqual([item["chat_id"] for item in api.photo_files], [PLAYER["id"], target.user_id])
        self.assertEqual(api.sent, [])
        self.assertTrue(all(item["disable_notification"] for item in api.photo_files))
        defender_copy = api.photo_files[-1]
        self.assertIn("Вас атаковал @player", defender_copy["caption"])
        self.assertIsNone(defender_copy["reply_markup"])
        self.assertFalse(any(item["chat_id"] == MAIN_CHAT_ID for item in api.photo_files))
        self.assertEqual(pets._load(CHAT)["duels"][str(PLAYER["id"])]["uses"], 1)

    def test_exhausted_group_duel_sends_arena_to_dm_without_group_notice(self):
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Кабанчик", "file_a", "Player")
        pets.buy_cage(CHAT, 43, RICH_XP)
        pets.tame(CHAT, 43, RICH_XP, "Тумблер", "file_b", "Bob")
        self._empty_fight_bank(PLAYER["id"])
        api = FakeApi()
        challenger = SimpleNamespace(user_id=PLAYER["id"], display_name="Player")
        target = SimpleNamespace(user_id=43, display_name="Bob")
        deletions = []

        async def resolve(*args, **kwargs):
            return (challenger, 1, 1, RICH_XP, 0, RICH_XP) if args[3] == "" else (target, 1, 1, RICH_XP, 0, RICH_XP)

        with patch.object(stats, "resolve_stat_target", resolve), patch.object(
            bot_listener, "schedule_bot_delete",
            side_effect=lambda *args, **kwargs: deletions.append((args, kwargs)),
        ):
            _run(bot_listener.handle_duel_command(
                api, None, None, _message(PLAYER, "/duel @bob", "group"), CHAT,
                "/duel @bob", BOT, set(), log=lambda *_: None,
            ))

        self.assertEqual(len(api.sent), 1)
        self.assertEqual(api.sent[0]["chat_id"], PLAYER["id"])
        self.assertIn(pets_ui.ARENA_NO_FIGHTS_NOTICE, api.sent[0]["text"])
        # The original /duel command may still be cleaned up; no bot-authored group
        # notice exists, so there is no response message id to schedule for deletion.
        self.assertTrue(all(not args[2] for args, _ in deletions))
        self.assertNotIn(str(PLAYER["id"]), pets._load(CHAT)["duels"])

    def test_exhaustion_race_redraws_dm_instead_of_using_public_result_chat(self):
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Attacker", "file_a", "Player")
        pets.buy_cage(CHAT, 43, RICH_XP)
        pets.tame(CHAT, 43, RICH_XP, "Opponent", "file_b", "Bob")
        self._empty_fight_bank(PLAYER["id"])
        api = FakeApi()

        _run(bot_listener._pets_run_fight(
            api, MAIN_CHAT_ID, 900, CHAT, PLAYER["id"], "43", RICH_XP,
            log=lambda *_: None, arena_menu_chat_id=DM_CHAT_ID,
            arena_menu_message_id=900,
        ))

        self.assertEqual(api.sent, [])
        self.assertEqual(api.photo_files, [])
        self.assertEqual(api.edits[-1]["chat_id"], DM_CHAT_ID)
        self.assertIn(pets_ui.ARENA_NO_FIGHTS_NOTICE, api.edits[-1]["text"])

    def test_last_fight_race_from_authoritative_write_stays_in_arena_dm(self):
        """A concurrent tap may drain the bank after the UI precheck but before save."""
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Attacker", "file_a", "Player")
        pets.buy_cage(CHAT, 43, RICH_XP)
        pets.tame(CHAT, 43, RICH_XP, "Opponent", "file_b", "Bob")
        api = FakeApi()

        with patch.object(pets, "record_fight", side_effect=ValueError("bank was spent")):
            _run(bot_listener._pets_run_fight(
                api, MAIN_CHAT_ID, 900, CHAT, PLAYER["id"], "43", RICH_XP,
                log=lambda *_: None, arena_menu_chat_id=DM_CHAT_ID,
                arena_menu_message_id=900,
            ))

        self.assertEqual(api.sent, [])
        self.assertEqual(api.photo_files, [])
        self.assertEqual(api.edits[-1]["chat_id"], DM_CHAT_ID)

    def test_private_duel_posts_one_result_to_each_player(self):
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Кабанчик", "file_a", "Player")
        pets.buy_cage(CHAT, 43, RICH_XP)
        pets.tame(CHAT, 43, RICH_XP, "Тумблер", "file_b", "Bob")
        api = FakeApi()
        challenger = SimpleNamespace(user_id=PLAYER["id"], display_name="Player")
        target = SimpleNamespace(user_id=43, display_name="Bob")

        async def resolve(*args, **kwargs):
            return (challenger, 1, 1, RICH_XP, 0, RICH_XP) if args[3] == "" else (target, 1, 1, RICH_XP, 0, RICH_XP)

        with patch.object(stats, "resolve_stat_target", resolve):
            _run(bot_listener.handle_duel_command(
                api, None, None, _message(PLAYER, "/duel @bob", "private"), CHAT,
                "/duel @bob", BOT, set(), log=lambda *_: None,
            ))

        self.assertEqual([item["chat_id"] for item in api.photo_files], [PLAYER["id"], target.user_id])
        self.assertEqual(api.deleted, [])


class ArenaNewsCommandTests(unittest.TestCase):
    """"/arenanews" -- the only way to add a changelog entry without a deploy."""

    def setUp(self):
        self._dir = TemporaryDirectory()
        path = Path(self._dir.name)
        real = stats._stats_dir
        stats._stats_dir = lambda: path
        self.addCleanup(self._dir.cleanup)
        self.addCleanup(lambda: setattr(stats, "_stats_dir", real))

    def _write(self, text, user=PLAYER, admin=True, chat_type="group"):
        api = FakeApi()

        async def is_admin(*_args, **_kwargs):
            return admin

        with patch.object(bot_listener, "_is_chat_admin_or_privileged", is_admin):
            _run(bot_listener.handle_arena_news_command(
                api, None, _message(user, text, chat_type), CHAT, text, None, {},
                log=lambda *_: None,
            ))
        return api

    def test_an_admin_publishes_a_headline_and_a_body(self):
        before = len(pets_updates.all_updates(CHAT))
        api = self._write("/arenanews 🌾 Ферма\nСмена теперь от 1 до 8 часов.")

        published = pets_updates.latest(CHAT)
        self.assertEqual(published.title, "🌾 Ферма")
        self.assertEqual(published.text, "Смена теперь от 1 до 8 часов.")
        self.assertEqual(len(pets_updates.all_updates(CHAT)), before + 1)
        self.assertIn("Опубликовано", api.sent[-1]["text"])
        self.assertIn("Обновления", pets_ui.updates_view(CHAT, PLAYER["id"], 0)[0])

    def test_a_multi_line_body_keeps_its_line_breaks(self):
        self._write("/arenanews Итоги\nПервый абзац.\n\nВторой абзац.")
        self.assertEqual(pets_updates.latest(CHAT).text, "Первый абзац.\n\nВторой абзац.")

    def test_a_non_admin_publishes_nothing(self):
        before = len(pets_updates.all_updates(CHAT))
        api = self._write("/arenanews Что-нибудь", user=STRANGER, admin=False)

        self.assertEqual(len(pets_updates.all_updates(CHAT)), before)
        self.assertIn("только администраторы", api.sent[-1]["text"])

    def test_a_bare_command_explains_the_format_instead_of_publishing_a_blank(self):
        before = len(pets_updates.all_updates(CHAT))
        api = self._write("/arenanews")

        self.assertEqual(len(pets_updates.all_updates(CHAT)), before)
        self.assertIn("Формат:", api.sent[-1]["text"])

    def test_the_russian_spelling_reaches_the_same_handler(self):
        self._write("/аренановости Привет\nТекст.")
        self.assertEqual(pets_updates.latest(CHAT).title, "Привет")


if __name__ == "__main__":
    unittest.main()
