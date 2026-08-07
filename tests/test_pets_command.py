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
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_listener
import economy
import pets
import pets_config as C
import pets_ui
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


class FakeApi:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.photos = []
        self.answered = []
        # Every call is appended here in order, which is how the "answer the tap first"
        # test can assert on ORDERING rather than just on the fact that both happened.
        self.calls = []

    async def send_message(self, chat_id, text, reply_to_message_id=None,
                           reply_markup=None, parse_mode=None):
        self.calls.append("send_message")
        item = {"message_id": 100 + len(self.sent), "chat_id": chat_id,
                "text": text, "reply_markup": reply_markup}
        self.sent.append(item)
        return item

    async def send_photo(self, chat_id, photo, caption, reply_to_message_id=None,
                         reply_markup=None, parse_mode=None):
        self.calls.append("send_photo")
        item = {"message_id": 200 + len(self.photos), "chat_id": chat_id,
                "photo": photo, "caption": caption}
        self.photos.append(item)
        return item

    async def edit_message_text(self, chat_id, message_id, text,
                                reply_markup=None, parse_mode=None):
        self.calls.append("edit_message_text")
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text})
        return {"message_id": message_id}

    async def answer_callback_query(self, callback_id, text=None):
        self.calls.append("answer_callback_query")
        self.answered.append(text)


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

    # ---------------------------------------------------------------------- commands

    def test_the_menu_opens_in_a_dm(self):
        api = self._type()
        self.assertEqual(len(api.sent), 1)
        self.assertIn("Арена", api.sent[0]["text"])
        actions = {pets_ui.parse_callback(b["callback_data"])[1] for b in _buttons(api.sent[0])}
        self.assertIn("cage", actions)

    def test_a_group_gets_a_pointer_and_a_deep_link_not_the_menu(self):
        """The menu spends the presser's coins and would show one member's wallet to 190
        people, so the group gets a link into the DM instead -- a url button, because a
        web_app button is private-chat only."""
        api = self._type(chat_type="group")
        self.assertEqual(len(api.sent), 1)
        buttons = _buttons(api.sent[0])
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0]["url"], f"https://t.me/{BOT}?start=pets")
        self.assertNotIn("callback_data", buttons[0])

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

    def test_taming_without_a_cage_shows_the_cage_screen_rather_than_a_prompt(self):
        flows = {}
        self._tap("tame", flows=flows)
        self.assertEqual(flows, {})

    def test_every_menu_action_renders_without_blowing_up(self):
        pets.buy_cage(CHAT, PLAYER["id"], RICH_XP)
        pets.tame(CHAT, PLAYER["id"], RICH_XP, "Кабанчик", "file_abc", "Player")
        for action, argument in [
            ("main", ""), ("cage", ""), ("train", ""), ("bag", ""), ("fight", ""),
            ("history", ""), ("pet", ""), ("slot", "weapon"), ("up", "strength"),
            ("up10", "luck"), ("search", ""),
        ]:
            api = self._tap(action, argument)
            self.assertTrue(
                api.edits or api.sent,
                f"action {action!r} drew nothing at all",
            )


if __name__ == "__main__":
    unittest.main()
