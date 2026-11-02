"""Who has the bot started (bot_users.py) and what /vote does with it:

1. the list itself -- fed by DM activity, block/unblock updates and what sends came back
   with, read once and written only on change, a whole run in one write;
2. deliver() -- paced, waits out "Too Many Requests", sorts errors into blocked / deleted /
   never started, never stops for one bad address;
3. the dispatcher writing people down (DMs only, never a group) and Telegram being asked
   for my_chat_member at all;
4. "📣 Объявление" → "📬 Всем в личку": asks first, then sends the text and a web_app
   vote button to each person, and records who turned out to have blocked the bot;
5. "/vote люди" and "/vote проверить": the list, and the check that fills it;
6. "/vote админам": a text or picture copied to the administrators, previewed first.
"""

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_api
import bot_listener
import bot_users
from errors import ChatSummaryError

DM = 555
MAIN_CHAT_ID = -1001234567890
ADMIN = {"id": 42, "first_name": "Админ", "username": "admin"}
BOT = "testbot"
CHAT = "Chat"

BLOCKED_ERROR = "Telegram Bot API sendMessage failed: Forbidden: bot was blocked by the user"
NEVER_ERROR = "Telegram Bot API sendMessage failed: Forbidden: bot can't initiate conversation with a user"
GONE_ERROR = "Telegram Bot API sendMessage failed: Forbidden: user is deactivated"


def _user(user_id, first="Имя", username=None):
    user = {"id": user_id, "first_name": first, "is_bot": False}
    if username:
        user["username"] = username
    return user


def _cfg():
    return SimpleNamespace(
        webapp_public_url="https://example.com", vote_announce_extra_chat=None,
        vote_miniapp_short_name=None, vote_member_chat=None, listener_allowed_chats=[CHAT],
        stats_enabled=False, stats_top_limit=10,
    )


class _Registry:
    """A fresh, empty list in a temporary directory for every test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        for patcher in (
            patch.object(bot_users, "_dir", lambda: self.dir),
            patch.dict(bot_users._REGISTRIES, clear=True),
            patch.object(bot_listener, "_BOT_USERS_JOBS", set()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def registry(self):
        return bot_users.registry()

    def seed(self, outcomes, names=None):
        """Put people on the list the way a run would, then forget the in-memory copy so
        the code under test reads the file like a fresh process."""
        bot_users.registry().record_outcomes(outcomes, names)
        bot_users._REGISTRIES.clear()

    def on_disk(self):
        return json.loads((self.dir / "users.json").read_text(encoding="utf-8"))


class FakeApi:
    def __init__(self, errors=None, admins=()):
        self.errors = dict(errors or {})  # chat_id -> error text for send/copy/chat action
        self.admins = list(admins)
        self.sent = []
        self.edits = []
        self.answered = []
        self.copies = []
        self.actions = []
        self.documents = []
        self.markups = []

    def _maybe_fail(self, chat_id):
        if chat_id in self.errors:
            raise ChatSummaryError(self.errors[chat_id])

    async def send_message(self, chat_id, text, reply_to_message_id=None, reply_markup=None,
                           parse_mode=None, disable_notification=False):
        self._maybe_fail(chat_id)
        item = {"message_id": 100 + len(self.sent), "chat_id": chat_id, "text": text,
                "reply_markup": reply_markup}
        self.sent.append(item)
        return item

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None, parse_mode=None):
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text,
                           "reply_markup": reply_markup})

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.markups.append((chat_id, message_id, reply_markup))

    async def answer_callback_query(self, callback_id, text=None, url=None, show_alert=False):
        self.answered.append(text)

    async def copy_message(self, chat_id, from_chat_id, message_id, reply_markup=None):
        self._maybe_fail(chat_id)
        self.copies.append({"chat_id": chat_id, "from_chat_id": from_chat_id,
                            "message_id": message_id, "reply_markup": reply_markup})
        return {"message_id": 900 + len(self.copies)}

    async def send_chat_action(self, chat_id, action="typing"):
        self._maybe_fail(chat_id)
        self.actions.append(chat_id)

    async def get_chat_administrators(self, chat_id):
        return [{"user": user, "status": "administrator"} for user in self.admins]

    async def send_document_file(self, chat_id, path, caption=None, **kwargs):
        self.documents.append({"chat_id": chat_id, "text": Path(path).read_text(encoding="utf-8"),
                               "caption": caption})

    def to(self, chat_id):
        return [item for item in self.sent if item["chat_id"] == chat_id]


def _buttons(markup):
    return [b for row in (markup or {}).get("inline_keyboard", []) for b in row]


async def _always_admin(*args, **kwargs):
    return True


async def _never_admin(*args, **kwargs):
    return False


# ------------------------------------------------------------------------ 1. the list


class RegistryTests(_Registry, unittest.TestCase):
    def test_somebody_writing_to_the_bot_is_on_the_list_as_active(self):
        self.assertTrue(self.registry().note_seen(_user(1, "Аня", "anya")))
        person = self.registry().people()[0]
        self.assertEqual((person.user_id, person.status, person.label()), ("1", "active", "Аня (@anya)"))
        self.assertEqual(self.on_disk()["users"]["1"]["status"], "active")

    def test_a_days_worth_of_messages_is_one_write(self):
        registry = self.registry()
        for _ in range(50):
            registry.note_seen(_user(1, "Аня"))
        self.assertEqual((registry.reads, registry.writes), (1, 1))
        self.assertTrue(registry.is_current(_user(1, "Аня")))

    def test_a_new_name_is_written_down(self):
        registry = self.registry()
        registry.note_seen(_user(1, "Аня"))
        self.assertFalse(registry.is_current(_user(1, "Анна")))
        self.assertTrue(registry.note_seen(_user(1, "Анна")))
        self.assertEqual(registry.people()[0].name, "Анна")

    def test_is_current_never_touches_the_disk(self):
        registry = self.registry()
        self.assertFalse(registry.is_current(_user(1)))
        self.assertEqual(registry.reads, 0)

    def test_bots_are_never_on_it(self):
        self.assertFalse(self.registry().note_seen({"id": 9, "is_bot": True, "first_name": "Bot"}))
        self.assertEqual(self.registry().people(), [])

    def test_blocking_and_unblocking_in_the_dm(self):
        registry = self.registry()
        update = {"chat": {"id": 1, "type": "private"}, "from": _user(1, "Аня"),
                  "old_chat_member": {"status": "member"}, "new_chat_member": {"status": "kicked"}}
        self.assertEqual(registry.note_member_update(update), "blocked")
        self.assertEqual(registry.counts(), {"active": 0, "blocked": 1, "gone": 0})
        update["new_chat_member"] = {"status": "member"}
        self.assertEqual(registry.note_member_update(update), "active")
        self.assertEqual(registry.counts()["active"], 1)

    def test_the_bot_leaving_a_group_is_not_somebody_blocking_it(self):
        update = {"chat": {"id": -100, "type": "supergroup"}, "from": _user(1),
                  "new_chat_member": {"status": "kicked"}}
        self.assertIsNone(self.registry().note_member_update(update))
        self.assertEqual(self.registry().people(), [])

    def test_a_whole_run_is_one_read_and_one_write(self):
        registry = self.registry()
        outcomes = {str(i): "active" for i in range(200)}
        outcomes.update({"500": "blocked", "501": "gone", "502": "never"})
        changed = registry.record_outcomes(outcomes, checked=True)
        self.assertEqual(changed, 202)
        self.assertEqual((registry.reads, registry.writes), (1, 1))
        self.assertEqual(registry.counts(), {"active": 200, "blocked": 1, "gone": 1})
        self.assertIsNotNone(registry.checked_at())

    def test_never_started_takes_somebody_off_the_list(self):
        registry = self.registry()
        registry.note_seen(_user(1))
        registry.record_outcomes({"1": "never"})
        self.assertEqual(registry.people(), [])

    def test_a_name_from_the_chat_only_fills_a_blank(self):
        registry = self.registry()
        registry.note_seen(_user(1, "Аня из лички", "anya"))
        registry.record_outcomes(
            {"1": "active", "2": "active"},
            {"1": {"name": "Старое имя", "username": "old"}, "2": {"name": "Боря", "username": "borya"}},
        )
        labels = {p.user_id: p.label() for p in registry.people()}
        self.assertEqual(labels, {"1": "Аня из лички (@anya)", "2": "Боря (@borya)"})

    def test_nothing_new_writes_nothing(self):
        registry = self.registry()
        registry.record_outcomes({"1": "active"})
        registry.record_outcomes({"1": "active"})
        self.assertEqual(registry.writes, 1)

    def test_a_restart_reads_the_list_back(self):
        self.seed({"1": "active", "2": "blocked"}, {"1": {"name": "Аня"}})
        self.assertEqual([(p.user_id, p.status) for p in self.registry().people()],
                         [("2", "blocked"), ("1", "active")])

    def test_a_corrupt_file_is_an_empty_list_not_a_crash(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "users.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.registry().people(), [])


# ------------------------------------------------------------------------ 2. delivery


class DeliverTests(unittest.TestCase):
    def _deliver(self, ids, send):
        sleeps = []

        async def sleep(seconds):
            sleeps.append(seconds)

        result = asyncio.run(bot_users.deliver(ids, send, sleep=sleep))
        return result, sleeps

    def test_errors_are_sorted_into_what_they_say_about_the_person(self):
        errors = {"2": BLOCKED_ERROR, "3": GONE_ERROR, "4": NEVER_ERROR, "5": "Bad Request: something"}

        async def send(user_id):
            if user_id in errors:
                raise ChatSummaryError(errors[user_id])

        result, _ = self._deliver(["1", "2", "3", "4", "5"], send)
        self.assertEqual(result.delivered, 1)
        self.assertEqual(result.outcomes, {"1": "active", "2": "blocked", "3": "gone", "4": "never"})
        self.assertEqual(list(result.errors), ["5"])

    def test_too_many_requests_waits_and_tries_the_same_person_again(self):
        calls = []

        async def send(user_id):
            calls.append(user_id)
            if len(calls) == 1:
                error = ChatSummaryError("Telegram Bot API sendMessage failed: Too Many Requests: retry after 7")
                error.retry_after = 7
                raise error

        result, sleeps = self._deliver(["1"], send)
        self.assertEqual(calls, ["1", "1"])
        self.assertEqual(result.outcomes, {"1": "active"})
        self.assertEqual(sleeps, [8])

    def test_the_wait_is_read_from_the_text_when_the_field_is_missing(self):
        self.assertEqual(bot_users.retry_after(ChatSummaryError("Too Many Requests: retry after 12")), 12)

    def test_it_is_paced_between_people(self):
        async def send(user_id):
            return None

        _, sleeps = self._deliver(["1", "2", "3"], send)
        self.assertEqual(sleeps, [bot_users.SEND_INTERVAL_SECONDS] * 2)

    def test_the_same_person_twice_is_sent_once(self):
        calls = []

        async def send(user_id):
            calls.append(user_id)

        self._deliver(["1", 1, "1"], send)
        self.assertEqual(calls, ["1"])

    def test_the_api_error_carries_telegrams_wait(self):
        error = bot_api._api_error("sendMessage", {
            "ok": False, "error_code": 429, "description": "Too Many Requests: retry after 3",
            "parameters": {"retry_after": 3},
        })
        self.assertEqual((error.error_code, error.retry_after), (429, 3))
        self.assertIn("Too Many Requests", str(error))

    def test_getupdates_asks_for_block_and_unblock(self):
        captured = {}

        async def fake_call(method, _http_timeout=20.0, **params):
            captured.update(params)
            return []

        client = bot_api.TelegramBotAPI("1:TOKEN", session=None)
        with patch.object(client, "_call", fake_call):
            asyncio.run(client.get_updates(offset=None, timeout=0))
        self.assertIn("my_chat_member", captured["allowed_updates"])
        self.assertIn("chat_member", captured["allowed_updates"])


# ------------------------------------------------------------------------ 3. dispatch


class DispatchTests(_Registry, unittest.TestCase):
    def _dispatch(self, update):
        async def go():
            await bot_listener._dispatch_update(
                update, FakeApi(), None, _cfg(), None, BOT, 1, set(), asyncio.Queue(),
                set(), CHAT, {}, {}, {}, {}, log=lambda *_: None,
            )

        asyncio.run(go())

    def test_a_dm_puts_the_sender_on_the_list(self):
        self._dispatch({"update_id": 1, "message": {
            "message_id": 1, "chat": {"id": 7, "type": "private"}, "from": _user(7, "Аня"),
            "text": "привет",
        }})
        self.assertEqual([p.user_id for p in self.registry().people()], ["7"])

    def test_a_group_message_does_not(self):
        self._dispatch({"update_id": 1, "message": {
            "message_id": 1, "chat": {"id": -100, "type": "supergroup"}, "from": _user(7),
            "text": "привет",
        }})
        self.assertEqual(self.registry().people(), [])

    def test_a_button_pressed_in_the_dm_counts_too(self):
        self._dispatch({"update_id": 1, "callback_query": {
            "id": "cb", "data": "nothing:here", "from": _user(8, "Боря"),
            "message": {"message_id": 3, "chat": {"id": 8, "type": "private"}},
        }})
        self.assertEqual([p.user_id for p in self.registry().people()], ["8"])

    def test_blocking_the_bot_reaches_the_list(self):
        self._dispatch({"update_id": 1, "my_chat_member": {
            "chat": {"id": 9, "type": "private"}, "from": _user(9, "Вера"),
            "old_chat_member": {"status": "member"}, "new_chat_member": {"status": "kicked"},
        }})
        self.assertEqual(self.registry().counts()["blocked"], 1)

    def test_the_admins_note_buttons_reach_their_handler(self):
        handled = []

        async def handle(api, callback, flows, log=print):
            handled.append(callback["data"])

        with patch.object(bot_listener, "handle_vote_admins_callback", handle):
            self._dispatch({"update_id": 1, "callback_query": {
                "id": "cb", "data": bot_listener._vote_admins_callback_data("send", "f1"),
                "from": ADMIN, "message": {"message_id": 3},
            }})
        self.assertEqual(handled, ["voteadmins:send:f1"])


# ------------------------------------------------------------------- 4. the DM mailing


def _flow(**extra):
    flow = {"chat_id": DM, "user_id": ADMIN["id"], "entry": CHAT, "admin_chat_id": MAIN_CHAT_ID,
            "prompt_message_id": 7, "created_at": time.monotonic(), "text": "Голосуем!"}
    flow.update(extra)
    return flow


def _press(destination, flow_id="f1", user=ADMIN):
    return {"id": "cbq", "from": user, "message": {"message_id": 9},
            "data": bot_listener._vote_chat_dest_callback_data(destination, flow_id)}


class MailingTests(_Registry, unittest.TestCase):
    def _tap(self, destination, flows, api=None, admin=True, background_tasks=None):
        api = api or FakeApi()

        async def go():
            with patch.object(bot_listener, "_can_manage_chat", _always_admin if admin else _never_admin), \
                    patch.object(bot_users, "SEND_INTERVAL_SECONDS", 0):
                await bot_listener.handle_vote_chat_destination_callback(
                    api, _cfg(), _press(destination), flows, BOT, log=lambda *_: None,
                    background_tasks=background_tasks,
                )
                if background_tasks:
                    await asyncio.gather(*background_tasks)

        asyncio.run(go())
        return api

    def test_the_text_step_offers_everybody_with_the_count(self):
        self.seed({"1": "active", "2": "active", "3": "blocked"})
        api = FakeApi()
        flows = {"f1": _flow()}
        flows["f1"].pop("text")
        message = {"message_id": 8, "chat": {"id": DM, "type": "private"}, "from": ADMIN,
                   "text": "Голосуем!", "reply_to_message": {"message_id": 7}}
        with patch.object(bot_listener, "_can_manage_chat", _always_admin):
            asyncio.run(bot_listener.handle_vote_chat_text_input(api, _cfg(), message, flows, log=lambda *_: None))
        labels = [b["text"] for b in _buttons(api.sent[-1]["reply_markup"])]
        self.assertIn("📬 Всем в личку (2)", labels)

    def test_the_first_tap_only_asks_and_keeps_the_draft(self):
        self.seed({"1": "active", "2": "active"})
        flows = {"f1": _flow()}
        api = self._tap("dm", flows)
        self.assertIn("f1", flows)
        self.assertEqual([item["chat_id"] for item in api.sent], [DM])
        self.assertIn("2 людям", api.sent[0]["text"])
        labels = [b["text"] for b in _buttons(api.sent[0]["reply_markup"])]
        self.assertEqual(labels, ["✅ Да, разослать (2)", "Отмена"])

    def test_an_empty_list_says_how_to_fill_it(self):
        flows = {"f1": _flow()}
        api = self._tap("dm", flows)
        self.assertIn("Проверить всех из чата", api.sent[0]["text"])
        self.assertIn("f1", flows)

    def test_yes_sends_the_text_and_a_web_app_button_to_each_person(self):
        self.seed({"1": "active", "2": "active", "3": "blocked"})
        flows = {"f1": _flow()}
        api = self._tap("dmyes", flows, background_tasks=set())
        self.assertEqual(flows, {})
        for user_id in (1, 2):
            [message] = api.to(user_id)
            self.assertEqual(message["text"], "Голосуем!")
            [button] = _buttons(message["reply_markup"])
            self.assertEqual(button["web_app"], {"url": "https://example.com" + bot_listener.vote_web.ROUTE_PREFIX})
        self.assertEqual(api.to(3), [], "somebody who blocked the bot is not written to")
        self.assertIn("доставлено 2 из 2", api.edits[-1]["text"])

    def test_the_arena_announcement_opens_the_arena(self):
        self.seed({"1": "active"})
        api = self._tap("dmyes", {"f1": _flow(system="arena")}, background_tasks=set())
        [button] = _buttons(api.to(1)[0]["reply_markup"])
        self.assertEqual(button["text"], bot_listener.ARENA_OPEN_BUTTON_TEXT)
        self.assertIn(bot_listener.arena_web.ROUTE_PREFIX, button["web_app"]["url"])

    def test_who_turned_out_to_have_blocked_it_is_recorded_in_one_write(self):
        self.seed({str(i): "active" for i in range(1, 31)})
        api = FakeApi(errors={5: BLOCKED_ERROR, 6: NEVER_ERROR, 7: GONE_ERROR})
        self._tap("dmyes", {"f1": _flow()}, api=api, background_tasks=set())
        registry = self.registry()
        self.assertEqual(registry.counts(), {"active": 27, "blocked": 1, "gone": 1})
        self.assertEqual((registry.reads, registry.writes), (1, 1))
        report = api.edits[-1]["text"]
        self.assertIn("доставлено 27 из 30", report)
        self.assertIn("Заблокировали бота: 1", report)

    def test_losing_admin_rights_sends_nothing(self):
        self.seed({"1": "active"})
        api = self._tap("dmyes", {"f1": _flow()}, admin=False, background_tasks=set())
        self.assertEqual(api.to(1), [])

    def test_somebody_else_pressing_yes_sends_nothing(self):
        self.seed({"1": "active"})
        flows = {"f1": _flow()}
        api = FakeApi()
        with patch.object(bot_listener, "_can_manage_chat", _always_admin):
            asyncio.run(bot_listener.handle_vote_chat_destination_callback(
                api, _cfg(), _press("dmyes", user=_user(99)), flows, BOT, log=lambda *_: None,
            ))
        self.assertEqual(api.sent, [])
        self.assertIn("f1", flows)

    def test_a_second_mailing_waits_for_the_first(self):
        self.seed({"1": "active"})
        bot_listener._BOT_USERS_JOBS.add("mailing")
        api = self._tap("dmyes", {"f1": _flow()}, background_tasks=set())
        self.assertEqual(api.to(1), [])
        self.assertIn("Уже идёт", api.to(DM)[-1]["text"])

    def test_the_mailing_runs_in_the_background(self):
        """Hundreds of messages at Telegram's pace must not hold up every other update:
        the handler returns with the job still queued."""
        self.seed({"1": "active"})
        tasks = set()

        async def go():
            with patch.object(bot_listener, "_can_manage_chat", _always_admin):
                await bot_listener.handle_vote_chat_destination_callback(
                    FakeApi(), _cfg(), _press("dmyes"), {"f1": _flow()}, BOT,
                    log=lambda *_: None, background_tasks=tasks,
                )
                self.assertEqual(len(tasks), 1)
                await asyncio.gather(*tasks)

        asyncio.run(go())


# ----------------------------------------------------------- 5. the list and the check


def _vote_message(text, chat_type="private"):
    return {"message_id": 5, "from": ADMIN, "text": text,
            "chat": {"id": DM if chat_type == "private" else MAIN_CHAT_ID, "type": chat_type}}


class VoteCommandTests(_Registry, unittest.TestCase):
    def _vote(self, text, api=None, admin=True, chat_type="private"):
        api = api or FakeApi()
        tasks = set()

        async def resolve(*args, **kwargs):
            return MAIN_CHAT_ID

        async def go():
            with patch.object(bot_listener, "_resolve_chat_id", resolve), \
                    patch.object(bot_listener, "_can_manage_chat", _always_admin if admin else _never_admin), \
                    patch.object(bot_users, "SEND_INTERVAL_SECONDS", 0):
                await bot_listener.handle_vote_command(
                    api, None, _cfg(), None, _vote_message(text, chat_type), CHAT, BOT, tasks,
                    log=lambda *_: None, vote_chat_flows={},
                )
                await asyncio.gather(*tasks)

        asyncio.run(go())
        return api

    def test_the_panel_has_the_list_and_the_admins_note(self):
        with patch.object(bot_listener.voting, "latest_poll", lambda entry: None):
            api = self._vote("/vote")
        data = {b["text"]: b.get("callback_data") for b in _buttons(api.sent[0]["reply_markup"])}
        self.assertEqual(data["👥 Кто активировал бота"],
                         bot_listener._vote_action_callback_data("people", DM, ADMIN["id"]))
        self.assertEqual(data["📢 Админам"],
                         bot_listener._vote_action_callback_data("admins", DM, ADMIN["id"]))

    def test_the_list_names_who_has_it_and_who_blocked_it(self):
        self.seed({"1": "active", "2": "blocked"},
                  {"1": {"name": "Аня", "username": "anya"}, "2": {"name": "Боря"}})
        api = self._vote("/vote люди")
        text = api.sent[0]["text"]
        self.assertIn("✅ Бот запущен: 1", text)
        self.assertIn("⛔ Заблокировали: 1", text)
        self.assertIn("1. Аня (@anya)", text)
        self.assertIn("1. Боря", text.split("⛔ Заблокировали (1)")[1])
        self.assertIn("замьютил", text)
        self.assertIn("Проверки ещё не было", text)
        labels = [b["text"] for b in _buttons(api.sent[0]["reply_markup"])]
        self.assertIn("🔍 Проверить всех из чата", labels)

    def test_a_long_list_goes_as_a_file(self):
        self.seed({str(i): "active" for i in range(1, 400)},
                  {str(i): {"name": f"Человек с длинным именем номер {i}"} for i in range(1, 400)})
        api = self._vote("/vote люди")
        self.assertLessEqual(len(api.sent[0]["text"]), 4096)
        self.assertIn("в файле", api.sent[0]["text"])
        [document] = api.documents
        self.assertIn("Человек с длинным именем номер 399 -- id 399", document["text"])

    def test_it_is_for_administrators_only(self):
        self.seed({"1": "active"}, {"1": {"name": "Аня"}})
        api = self._vote("/vote люди", admin=False)
        self.assertNotIn("Аня", api.sent[0]["text"])
        self.assertIn("администратор", api.sent[0]["text"])

    def test_in_a_group_it_points_to_the_dm(self):
        self.seed({"1": "active"}, {"1": {"name": "Аня"}})
        api = self._vote("/vote люди", chat_type="supergroup")
        self.assertNotIn("Аня", api.sent[0]["text"])
        self.assertIn("личке", api.sent[0]["text"])

    def test_the_check_asks_telegram_about_everybody_the_bot_has_seen(self):
        candidates = {"1": {"name": "Аня", "username": "anya"}, "2": {"name": "Боря"},
                      "3": {"name": "Вера"}, "4": {}}
        api = FakeApi(errors={2: BLOCKED_ERROR.replace("sendMessage", "sendChatAction"),
                              3: NEVER_ERROR.replace("sendMessage", "sendChatAction")})
        with patch.object(bot_listener, "_bot_user_candidates", lambda entry, log=print: candidates):
            self._vote("/vote проверить", api=api)
        self.assertEqual(sorted(api.actions), [1, 4])
        self.assertEqual(api.sent, [api.to(DM)[0]], "nothing is SENT to anybody but the admin")
        registry = self.registry()
        self.assertEqual({p.user_id: p.status for p in registry.people()},
                         {"1": "active", "2": "blocked", "4": "active"})
        self.assertEqual(registry.writes, 1)
        self.assertIsNotNone(registry.checked_at())
        report = api.edits[-1]["text"]
        self.assertIn("проверено 4", report)
        self.assertIn("Не запускали бота: 1", report)

    def test_the_check_runs_in_the_background(self):
        tasks = set()

        async def go():
            async def resolve(*args, **kwargs):
                return MAIN_CHAT_ID

            with patch.object(bot_listener, "_resolve_chat_id", resolve), \
                    patch.object(bot_listener, "_can_manage_chat", _always_admin), \
                    patch.object(bot_listener, "_bot_user_candidates", lambda entry, log=print: {"1": {}}):
                await bot_listener.handle_vote_command(
                    FakeApi(), None, _cfg(), None, _vote_message("/vote проверить"), CHAT, BOT, tasks,
                    log=lambda *_: None, vote_chat_flows={},
                )
                self.assertEqual(len(tasks), 1)
                await asyncio.gather(*tasks)

        asyncio.run(go())

    def test_candidates_come_from_the_chat_the_votes_and_the_list(self):
        self.seed({"30": "active"})
        users = {
            "10": SimpleNamespace(user_id="10", display_name="Аня", username="anya"),
            "-100": SimpleNamespace(user_id="-100", display_name="Канал", username=None),
            "11": SimpleNamespace(user_id="11", display_name="Unknown", username=None),
        }
        with patch.object(bot_listener.stats, "aggregate_all_time", lambda entry: users), \
                patch.object(bot_listener.voting, "weekly_vote_records",
                             lambda entry: [{"voter_ids": {"20", "10"}}]):
            found = bot_listener._bot_user_candidates(CHAT)
        self.assertEqual(set(found), {"10", "11", "20", "30"})
        self.assertEqual(found["10"], {"name": "Аня", "username": "anya"})
        self.assertEqual(found["11"]["name"], "")


# ---------------------------------------------------------------- 6. a note to admins


class AdminNoteTests(_Registry, unittest.TestCase):
    OTHER_ADMINS = [_user(2, "Боря", "borya"), _user(3, "Вера"), {"id": 77, "is_bot": True, "first_name": "Бот"}]

    def _draft(self, api, message_extra, flows=None):
        flows = flows if flows is not None else {"f1": _flow(system=bot_listener.ADMIN_NOTICE_SYSTEM)}
        flows["f1"].pop("text", None)
        message = {"message_id": 8, "chat": {"id": DM, "type": "private"}, "from": ADMIN,
                   "reply_to_message": {"message_id": 7}}
        message.update(message_extra)
        with patch.object(bot_listener, "_can_manage_chat", _always_admin), \
                patch.object(bot_listener.stats, "list_badge_managers", lambda entry: []):
            handled = asyncio.run(bot_listener.handle_vote_chat_text_input(
                api, _cfg(), message, flows, log=lambda *_: None,
            ))
        self.assertTrue(handled)
        return flows

    def _press(self, api, action, flows, user=ADMIN, admin=True):
        callback = {"id": "cb", "from": user, "message": {"message_id": 901},
                    "data": bot_listener._vote_admins_callback_data(action, "f1")}
        with patch.object(bot_listener, "_can_manage_chat", _always_admin if admin else _never_admin), \
                patch.object(bot_users, "SEND_INTERVAL_SECONDS", 0):
            asyncio.run(bot_listener.handle_vote_admins_callback(api, callback, flows, log=lambda *_: None))

    def test_text_is_previewed_as_a_copy_with_who_gets_it(self):
        api = FakeApi(admins=[ADMIN] + self.OTHER_ADMINS)
        flows = self._draft(api, {"text": "Завтра созвон"})
        self.assertIn("Боря (@borya), Вера", api.sent[0]["text"])
        self.assertNotIn("Бот", api.sent[0]["text"])
        self.assertNotIn("Админ", api.sent[0]["text"], "the sender sees the preview instead")
        [preview] = api.copies
        self.assertEqual((preview["chat_id"], preview["from_chat_id"], preview["message_id"]), (DM, DM, 8))
        self.assertEqual([b["text"] for b in _buttons(preview["reply_markup"])], ["✅ Отправить (2)", "Отмена"])
        self.assertEqual(flows["f1"]["recipients"], [{"id": "2", "label": "Боря (@borya)"},
                                                      {"id": "3", "label": "Вера"}])

    def test_a_picture_without_a_caption_is_taken_too(self):
        api = FakeApi(admins=self.OTHER_ADMINS)
        flows = self._draft(api, {"photo": [{"file_id": "small"}, {"file_id": "big"}]})
        self.assertEqual(flows["f1"]["source_message_id"], 8)
        self.assertEqual(len(api.copies), 1)

    def test_a_picture_reaches_the_draft_through_the_whole_dispatcher(self):
        """A photo has no text, and everything in front of the force-reply consumers reads
        text -- this pins that nothing on the way swallows it."""
        api = FakeApi(admins=self.OTHER_ADMINS)
        flows = {"f1": _flow(system=bot_listener.ADMIN_NOTICE_SYSTEM)}
        flows["f1"].pop("text")
        update = {"update_id": 1, "message": {
            "message_id": 8, "chat": {"id": DM, "type": "private"}, "from": ADMIN,
            "photo": [{"file_id": "small"}, {"file_id": "big"}],
            "reply_to_message": {"message_id": 7},
        }}

        async def go():
            await bot_listener._dispatch_update(
                update, api, None, _cfg(), None, BOT, 1, set(), asyncio.Queue(), set(), CHAT,
                {}, {}, {}, {}, vote_chat_flows=flows, log=lambda *_: None,
            )

        with patch.object(bot_listener, "_can_manage_chat", _always_admin), \
                patch.object(bot_listener.stats, "list_badge_managers", lambda entry: []):
            asyncio.run(go())
        self.assertEqual(flows["f1"].get("source_message_id"), 8)
        self.assertEqual(len(api.copies), 1)

    def test_send_copies_it_to_every_admin_and_names_who_it_missed(self):
        api = FakeApi(admins=self.OTHER_ADMINS, errors={3: NEVER_ERROR.replace("sendMessage", "copyMessage")})
        flows = self._draft(api, {"text": "Завтра созвон"})
        self._press(api, "send", flows)
        self.assertEqual(flows, {})
        delivered = [c for c in api.copies if c["chat_id"] != DM]
        self.assertEqual([(c["chat_id"], c["from_chat_id"], c["message_id"]) for c in delivered], [(2, DM, 8)])
        self.assertIsNone(delivered[0]["reply_markup"])
        report = api.to(DM)[-1]["text"]
        self.assertIn("Отправлено админам: 1 из 2", report)
        self.assertIn("Вера: не запускал бота", report)
        self.assertEqual(api.markups, [(DM, 901, None)], "the preview's buttons are taken away")

    def test_cancel_sends_nothing(self):
        api = FakeApi(admins=self.OTHER_ADMINS)
        flows = self._draft(api, {"text": "Завтра созвон"})
        self._press(api, "cancel", flows)
        self.assertEqual([c for c in api.copies if c["chat_id"] != DM], [])
        self.assertIn("Не отправлено", api.to(DM)[-1]["text"])

    def test_somebody_else_pressing_it_sends_nothing(self):
        api = FakeApi(admins=self.OTHER_ADMINS)
        flows = self._draft(api, {"text": "Завтра созвон"})
        self._press(api, "send", flows, user=_user(2))
        self.assertEqual([c for c in api.copies if c["chat_id"] != DM], [])
        self.assertIn("f1", flows)

    def test_losing_admin_rights_before_sending_sends_nothing(self):
        api = FakeApi(admins=self.OTHER_ADMINS)
        flows = self._draft(api, {"text": "Завтра созвон"})
        self._press(api, "send", flows, admin=False)
        self.assertEqual([c for c in api.copies if c["chat_id"] != DM], [])

    def test_an_expired_preview_sends_nothing(self):
        api = FakeApi(admins=self.OTHER_ADMINS)
        flows = self._draft(api, {"text": "Завтра созвон"})
        flows["f1"]["created_at"] -= bot_listener.VOTE_CHAT_FLOW_TTL_SECONDS + 1
        self._press(api, "send", flows)
        self.assertEqual([c for c in api.copies if c["chat_id"] != DM], [])
        self.assertIn("устарел", api.answered[-1])

    def test_nobody_else_to_send_to_is_said(self):
        api = FakeApi(admins=[ADMIN])
        flows = self._draft(api, {"text": "Завтра созвон"})
        self.assertEqual(flows, {})
        self.assertIn("Некому", api.sent[0]["text"])

    def test_cancelling_at_the_text_step(self):
        api = FakeApi(admins=self.OTHER_ADMINS)
        flows = self._draft(api, {"text": "отмена"})
        self.assertEqual(flows, {})
        self.assertEqual(api.copies, [])

    def test_delegated_managers_get_it_as_well(self):
        api = FakeApi(admins=[])
        managers = [{"user_id": "5", "display_name": "Гена", "username": "gena"}]
        recipients = asyncio.run(self._recipients(api, managers))
        self.assertEqual(recipients, [{"id": "5", "label": "Гена (@gena)"}])

    async def _recipients(self, api, managers):
        with patch.object(bot_listener.stats, "list_badge_managers", lambda entry: managers):
            return await bot_listener._admin_notice_recipients(api, MAIN_CHAT_ID, CHAT, ADMIN["id"])

    def test_the_draft_is_started_from_the_panel_word(self):
        flows = {}
        api = FakeApi()

        async def resolve(*args, **kwargs):
            return MAIN_CHAT_ID

        with patch.object(bot_listener, "_resolve_chat_id", resolve), \
                patch.object(bot_listener, "_can_manage_chat", _always_admin):
            asyncio.run(bot_listener.handle_vote_command(
                api, None, _cfg(), None, _vote_message("/vote админам"), CHAT, BOT, set(),
                log=lambda *_: None, vote_chat_flows=flows,
            ))
        [flow] = flows.values()
        self.assertEqual(flow["system"], bot_listener.ADMIN_NOTICE_SYSTEM)
        self.assertEqual(api.sent[0]["reply_markup"], {"force_reply": True, "selective": True})


if __name__ == "__main__":
    unittest.main()
