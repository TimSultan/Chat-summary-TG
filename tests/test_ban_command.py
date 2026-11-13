"""/banuser and /unbanuser: the owner's ban, with one confirmation before it lands."""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_listener
import stats

OWNER = {"id": 1, "username": "Sultan_Kembayev", "first_name": "Султан"}
STRANGER = {"id": 2, "username": "someone", "first_name": "Кто-то"}
DM = 1
MAIN_CHAT = -1001234567890
GROUP = -1009876543210
TARGET_ID = 555


class FakeApi:
    def __init__(self, member=None, ban_error=None):
        self.sent, self.edits, self.answered = [], [], []
        self.banned, self.unbanned = [], []
        self.member = member if member is not None else {
            "status": "member", "user": {"id": TARGET_ID, "first_name": "Павел", "username": "Alecsiel"},
        }
        self.ban_error = ban_error

    async def send_message(self, chat_id, text, reply_to_message_id=None, reply_markup=None, parse_mode=None):
        item = {"chat_id": chat_id, "text": text, "reply_markup": reply_markup, "message_id": 100 + len(self.sent)}
        self.sent.append(item)
        return item

    async def get_chat_member(self, chat_id, user_id):
        return self.member

    async def ban_chat_member(self, chat_id, user_id):
        if self.ban_error:
            raise RuntimeError(self.ban_error)
        self.banned.append((chat_id, user_id))

    async def unban_chat_member(self, chat_id, user_id):
        self.unbanned.append((chat_id, user_id))

    async def answer_callback_query(self, callback_id, text=None):
        self.answered.append(text)

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None, parse_mode=None):
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text, "reply_markup": reply_markup})


class FakeRegistry:
    def __init__(self, people=()):
        self._people = list(people)

    def people(self, status=None):
        return self._people


class FakeTelethon:
    def __init__(self, entity=None):
        self.entity = entity

    async def get_entity(self, username):
        if self.entity is None:
            raise ValueError("No user has that username")
        return self.entity


def _message(text, sender=OWNER, chat_id=DM, reply_to=None):
    message = {"message_id": 7, "from": sender, "text": text,
               "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"}}
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": 6, "from": reply_to}
    return message


class BanCommandTests(unittest.TestCase):
    def setUp(self):
        async def resolve(*args, **kwargs):
            return MAIN_CHAT

        for patcher in (
            patch.object(bot_listener, "_resolve_chat_id", resolve),
            patch.object(bot_listener.bot_users, "registry", lambda: FakeRegistry()),
            patch.object(stats, "aggregate_all_time", lambda entry: {}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _command(self, text, api=None, telethon=None, **message):
        api = api or FakeApi()
        asyncio.run(bot_listener.handle_ban_command(
            api, telethon, _message(text, **message), text, "Chat", {},
            unban=text.startswith(bot_listener.UNBAN_COMMAND), log=lambda *_: None,
        ))
        return api

    def _tap(self, api, button, sender=OWNER):
        question = api.sent[-1]
        asyncio.run(bot_listener.handle_ban_callback(api, {
            "id": "cb", "from": sender, "data": button["callback_data"],
            "message": {"message_id": question["message_id"], "chat": {"id": question["chat_id"]},
                        "text": question["text"]},
        }, log=lambda *_: None))

    def _buttons(self, api):
        return [b for row in (api.sent[-1]["reply_markup"] or {}).get("inline_keyboard", []) for b in row]

    def test_the_owner_is_asked_first_and_nothing_is_banned_yet(self):
        telethon = FakeTelethon(SimpleNamespace(id=TARGET_ID, first_name="Павел", last_name=None, username="Alecsiel"))
        api = self._command("/banuser @Alecsiel", telethon=telethon)
        self.assertEqual(api.banned, [])
        self.assertIn("Забанить Павел (@Alecsiel) в основном чате?", api.sent[-1]["text"])
        self.assertEqual([b["text"] for b in self._buttons(api)], ["🚫 Да, забанить", "Отмена"])

    def test_confirming_bans_in_the_main_chat(self):
        telethon = FakeTelethon(SimpleNamespace(id=TARGET_ID, first_name="Павел", last_name=None, username="Alecsiel"))
        api = self._command("/banuser @Alecsiel", telethon=telethon)
        self._tap(api, self._buttons(api)[0])
        self.assertEqual(api.banned, [(MAIN_CHAT, TARGET_ID)])
        self.assertIn("🚫 Бан: Павел (@Alecsiel)", api.edits[-1]["text"])
        self.assertIn(f"/unbanuser {TARGET_ID}", api.edits[-1]["text"])
        self.assertIsNone(api.edits[-1]["reply_markup"])

    def test_cancelling_bans_nobody(self):
        api = self._command(f"/banuser {TARGET_ID}")
        self._tap(api, self._buttons(api)[1])
        self.assertEqual(api.banned, [])
        self.assertIn("отменён", api.edits[-1]["text"])

    def test_somebody_else_pressing_the_button_bans_nobody(self):
        api = self._command(f"/banuser {TARGET_ID}")
        self._tap(api, self._buttons(api)[0], sender=STRANGER)
        self.assertEqual(api.banned, [])
        self.assertIn("только для владельца", api.answered[-1])

    def test_anybody_else_typing_it_in_a_group_gets_silence(self):
        api = self._command("/banuser @Alecsiel", sender=STRANGER, chat_id=GROUP)
        self.assertEqual(api.sent, [])

    def test_anybody_else_typing_it_in_the_dm_is_told_no(self):
        api = self._command("/banuser @Alecsiel", sender=STRANGER, chat_id=2)
        self.assertIn("только для владельца", api.sent[-1]["text"])

    def test_in_reply_to_a_message_in_a_group_it_bans_that_author_there(self):
        target = {"id": TARGET_ID, "first_name": "Павел", "username": "Alecsiel"}
        api = self._command("/banuser", chat_id=GROUP, reply_to=target)
        self.assertIn("в этом чате", api.sent[-1]["text"])
        self._tap(api, self._buttons(api)[0])
        self.assertEqual(api.banned, [(GROUP, TARGET_ID)])

    def test_a_username_is_found_among_the_people_who_started_the_bot(self):
        person = bot_listener.bot_users.Person(
            user_id=str(TARGET_ID), name="Павел", username="Alecsiel", status="active", changed_at="", seen="",
        )
        with patch.object(bot_listener.bot_users, "registry", lambda: FakeRegistry([person])):
            api = self._command("/banuser alecsiel")
        self._tap(api, self._buttons(api)[0])
        self.assertEqual(api.banned, [(MAIN_CHAT, TARGET_ID)])

    def test_a_username_is_found_in_the_chats_stats(self):
        users = {"9": stats.UserStats(user_id=str(TARGET_ID), username="Alecsiel", display_name="Павел")}
        with patch.object(stats, "aggregate_all_time", lambda entry: users):
            api = self._command("/banuser @Alecsiel")
        self.assertTrue(self._buttons(api))

    def test_an_unknown_username_is_said_plainly(self):
        api = self._command("/banuser @nobody_here")
        self.assertIn("Не нашёл @nobody_here", api.sent[-1]["text"])
        self.assertIsNone(api.sent[-1]["reply_markup"])

    def test_a_username_now_held_by_somebody_else_is_flagged(self):
        users = {"9": stats.UserStats(user_id=str(TARGET_ID), username="old_nick", display_name="Павел")}
        with patch.object(stats, "aggregate_all_time", lambda entry: users):
            api = self._command("/banuser @old_nick")
        self.assertIn("⚠️ Сейчас у этого человека ник @Alecsiel, не @old_nick", api.sent[-1]["text"])

    def test_an_administrator_cannot_be_banned(self):
        api = FakeApi(member={"status": "administrator", "user": {"id": TARGET_ID, "first_name": "Админ"}})
        self._command(f"/banuser {TARGET_ID}", api=api)
        self.assertIn("администратор", api.sent[-1]["text"])
        self.assertIsNone(api.sent[-1]["reply_markup"])

    def test_somebody_already_banned_is_not_asked_about_again(self):
        api = FakeApi(member={"status": "kicked", "user": {"id": TARGET_ID, "first_name": "Павел"}})
        self._command(f"/banuser {TARGET_ID}", api=api)
        self.assertIn("уже в бане", api.sent[-1]["text"])

    def test_a_ban_telegram_refuses_is_reported(self):
        api = self._command(f"/banuser {TARGET_ID}", api=FakeApi(ban_error="not enough rights to restrict/unrestrict chat member"))
        self._tap(api, self._buttons(api)[0])
        self.assertIn("Не получилось забанить: not enough rights", api.edits[-1]["text"])

    def test_no_target_at_all_explains_how(self):
        api = self._command("/banuser")
        self.assertIn("Как пользоваться", api.sent[-1]["text"])

    def test_unban_lifts_it_without_asking(self):
        api = FakeApi(member={"status": "kicked", "user": {"id": TARGET_ID, "first_name": "Павел"}})
        self._command(f"/unbanuser {TARGET_ID}", api=api)
        self.assertEqual(api.unbanned, [(MAIN_CHAT, TARGET_ID)])
        self.assertIn("Бан снят", api.sent[-1]["text"])


class BanDispatchTests(unittest.TestCase):
    def _dispatch(self, update):
        calls = []

        async def command(*args, **kwargs):
            calls.append(("command", kwargs.get("unban")))

        async def callback(*args, **kwargs):
            calls.append(("callback", None))

        cfg = SimpleNamespace(listener_allowed_chats=[], stats_enabled=False)

        async def go():
            with patch.object(bot_listener, "handle_ban_command", command), \
                    patch.object(bot_listener, "handle_ban_callback", callback):
                await bot_listener._dispatch_update(
                    update, FakeApi(), None, cfg, None, "testbot", 1, set(), asyncio.Queue(),
                    set(), "chat", {}, {}, {}, {}, log=lambda *_: None,
                )

        asyncio.run(go())
        return calls

    def test_both_commands_and_the_button_reach_their_handlers(self):
        self.assertEqual(self._dispatch({"message": _message("/banuser @x")}), [("command", False)])
        self.assertEqual(self._dispatch({"message": _message("/unbanuser @x")}), [("command", True)])
        self.assertEqual(self._dispatch({"message": _message("/banuser@testbot", chat_id=GROUP)}), [("command", False)])
        self.assertEqual(self._dispatch({"callback_query": {
            "id": "cb", "from": OWNER, "data": bot_listener._ban_callback_data("ban", MAIN_CHAT, TARGET_ID),
            "message": {"message_id": 1, "chat": {"id": DM, "type": "private"}},
        }}), [("callback", None)])


if __name__ == "__main__":
    unittest.main()
