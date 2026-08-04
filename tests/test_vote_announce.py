"""The two announcement flows that end with something being posted to a group.

1. "/vote chat" -- the force-reply that parks a draft announcement, and the destination
   buttons that decide which chat(s) it is finally posted into.
2. The weekly RESULTS draft: closing the vote in the Mini App no longer announces
   anything, it puts a draft of the results text in the closing admin's DM behind
   Редактировать/Отправить/Отмена, and only Отправить reaches the chat.

Neither touches voting.py's on-disk state here -- both flows live in their own in-memory
dict -- so there is no poll to set up, the admin check is stubbed rather than exercised
(it has its own tests elsewhere), and voting.save_results is stubbed so nothing in this
file needs a real poll directory.
"""

import asyncio
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_listener
import voting

DM_CHAT_ID = 555
MAIN_CHAT_ID = -1001234567890
EXTRA_CHAT = "@papkahudojnicov"
ADMIN = {"id": 42, "username": "admin"}
PROMPT_ID = 7
BOT = "testbot"


def _run(coro):
    return asyncio.run(coro)


def _cfg(extra=EXTRA_CHAT, short_name=None):
    return SimpleNamespace(
        webapp_public_url="https://example.com",
        vote_announce_extra_chat=extra,
        vote_miniapp_short_name=short_name,
    )


class FakeApi:
    """sendMessage plus answerCallbackQuery, with a per-chat failure injection so a
    destination that rejects the post can be tested next to one that accepts it.

    Deliberately has no send_photo_file: neither of these flows posts a picture any more,
    so a regression that reintroduced one would blow up here on the missing attribute
    instead of quietly passing a test that never looked."""

    def __init__(self, failing_chats=()):
        self.sent = []
        self.answered = []
        self.failing_chats = set(failing_chats)

    async def send_message(self, chat_id, text, reply_to_message_id=None,
                           reply_markup=None, parse_mode=None):
        if chat_id in self.failing_chats:
            raise RuntimeError("Bad Request: chat not found")
        item = {
            "message_id": 100 + len(self.sent),
            "chat_id": chat_id, "text": text, "reply_markup": reply_markup,
            "reply_to_message_id": reply_to_message_id,
        }
        self.sent.append(item)
        return item

    async def answer_callback_query(self, callback_id, text=None):
        self.answered.append({"id": callback_id, "text": text})

    def posts_to(self, chat_id):
        return [s for s in self.sent if s["chat_id"] == chat_id]


def _flow(text="Голосуем за итоги недели"):
    return {
        "chat_id": DM_CHAT_ID,
        "user_id": ADMIN["id"],
        "entry": "Chat",
        "admin_chat_id": MAIN_CHAT_ID,
        "prompt_message_id": PROMPT_ID,
        "created_at": time.monotonic(),
        "text": text,
    }


def _reply_message(text):
    return {
        "message_id": 8,
        "chat": {"id": DM_CHAT_ID, "type": "private"},
        "from": ADMIN,
        "text": text,
        "reply_to_message": {"message_id": PROMPT_ID},
    }


def _press(destination, flow_id):
    return {
        "id": "cbq1",
        "data": bot_listener._vote_chat_dest_callback_data(destination, flow_id),
        "from": ADMIN,
        "message": {"message_id": 9},
    }


class _AlwaysAdmin:
    """Stands in for _can_manage_chat, which would otherwise ask Telegram for the chat's
    administrator list (and read the badge-manager file) on every one of these cases."""

    def __init__(self, allowed=True):
        self.allowed = allowed

    async def __call__(self, api, chat_id, user, entry=None):
        return self.allowed


class DraftAsksWhereItGoesTests(unittest.TestCase):
    def _feed(self, text, cfg=None, flows=None, admin=True):
        api = FakeApi()
        flows = {"f1": _flow()} if flows is None else flows
        with patch.object(bot_listener, "_can_manage_chat", _AlwaysAdmin(admin)):
            handled = _run(bot_listener.handle_vote_chat_text_input(
                api, cfg or _cfg(), _reply_message(text), flows, log=lambda *_: None,
            ))
        return api, flows, handled

    def test_the_draft_is_not_posted_yet_and_the_flow_survives_the_reply(self):
        api, flows, handled = self._feed("Новое голосование")
        self.assertTrue(handled)
        self.assertEqual([s["chat_id"] for s in api.sent], [DM_CHAT_ID])
        self.assertIn("Куда отправить объявление", api.sent[0]["text"])
        self.assertEqual(flows["f1"]["text"], "Новое голосование")

    def test_all_three_destinations_are_offered_when_the_second_group_is_configured(self):
        api, _, _ = self._feed("Текст")
        labels = [b["text"] for row in api.sent[0]["reply_markup"]["inline_keyboard"] for b in row]
        self.assertEqual(labels, ["📣 В чат", "🎨 В Папку художников", "📣 В оба", "Отмена"])

    def test_without_a_second_group_only_the_main_chat_is_offered(self):
        api, _, _ = self._feed("Текст", cfg=_cfg(extra=None))
        labels = [b["text"] for row in api.sent[0]["reply_markup"]["inline_keyboard"] for b in row]
        self.assertEqual(labels, ["📣 В чат", "Отмена"])

    def test_cancelling_at_the_text_step_drops_the_draft(self):
        api, flows, _ = self._feed("отмена")
        self.assertEqual(flows, {})
        self.assertIn("отменён", api.sent[0]["text"])


class DestinationCallbackTests(unittest.TestCase):
    def _tap(self, destination, cfg=None, flows=None, failing_chats=(), admin=True,
             bot_username=BOT):
        api = FakeApi(failing_chats=failing_chats)
        flows = {"f1": _flow()} if flows is None else flows
        with patch.object(bot_listener, "_can_manage_chat", _AlwaysAdmin(admin)):
            _run(bot_listener.handle_vote_chat_destination_callback(
                api, cfg or _cfg(), _press(destination, "f1"), flows, bot_username,
                log=lambda *_: None,
            ))
        return api, flows

    def test_main_posts_into_the_tracked_chat_only(self):
        api, flows = self._tap("main")
        self.assertEqual(len(api.posts_to(MAIN_CHAT_ID)), 1)
        self.assertEqual(api.posts_to(EXTRA_CHAT), [])
        self.assertEqual(api.posts_to(MAIN_CHAT_ID)[0]["text"], _flow()["text"])
        self.assertEqual(flows, {})

    def test_extra_posts_into_the_second_group_only(self):
        api, _ = self._tap("extra")
        self.assertEqual(len(api.posts_to(EXTRA_CHAT)), 1)
        self.assertEqual(api.posts_to(MAIN_CHAT_ID), [])

    def test_both_posts_into_each_of_them_once(self):
        api, _ = self._tap("both")
        self.assertEqual(len(api.posts_to(MAIN_CHAT_ID)), 1)
        self.assertEqual(len(api.posts_to(EXTRA_CHAT)), 1)
        report = api.posts_to(DM_CHAT_ID)[-1]["text"]
        self.assertIn("основной чат", report)
        self.assertIn("Папка художников", report)

    def test_one_failing_destination_does_not_block_the_other(self):
        api, _ = self._tap("both", failing_chats=(MAIN_CHAT_ID,))
        self.assertEqual(len(api.posts_to(EXTRA_CHAT)), 1)
        report = api.posts_to(DM_CHAT_ID)[-1]["text"]
        self.assertIn("Папка художников", report.split("Не получилось")[0])
        self.assertIn("основной чат", report.split("Не получилось")[1])
        self.assertIn("chat not found", report)

    def test_every_destination_failing_is_still_reported_rather_than_silent(self):
        api, flows = self._tap("both", failing_chats=(MAIN_CHAT_ID, EXTRA_CHAT))
        self.assertIn("Не получилось отправить", api.posts_to(DM_CHAT_ID)[-1]["text"])
        self.assertEqual(flows, {})

    def test_the_button_is_a_plain_url_not_a_web_app_one(self):
        """A web_app button is private-chat only -- Telegram rejects one posted to a
        group, which is where every destination here is."""
        api, _ = self._tap("main")
        button = api.posts_to(MAIN_CHAT_ID)[0]["reply_markup"]["inline_keyboard"][0][0]
        self.assertNotIn("web_app", button)
        self.assertEqual(button["url"], f"https://t.me/{BOT}?start=vote")

    def test_a_direct_link_mini_app_short_name_opens_the_app_from_the_group(self):
        api, _ = self._tap("main", cfg=_cfg(short_name="vote"))
        button = api.posts_to(MAIN_CHAT_ID)[0]["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(button["url"], f"https://t.me/{BOT}/vote?startapp=vote")

    def test_the_announcement_is_never_scheduled_for_deletion(self):
        """The one message in this bot that has to still be there tomorrow -- no sweep
        path may touch it, on any destination."""
        scheduled = []
        with patch.object(bot_listener, "schedule_bot_delete", lambda *a, **k: scheduled.append(a)):
            api, _ = self._tap("both")
        self.assertEqual(scheduled, [])
        self.assertEqual(len(api.posts_to(MAIN_CHAT_ID)), 1)

    def test_cancel_posts_nothing_anywhere(self):
        api, flows = self._tap("cancel")
        self.assertEqual(api.posts_to(MAIN_CHAT_ID), [])
        self.assertEqual(api.posts_to(EXTRA_CHAT), [])
        self.assertIn("отменено", api.posts_to(DM_CHAT_ID)[-1]["text"])
        self.assertEqual(flows, {})

    def test_the_spinner_is_stopped_before_anything_is_posted(self):
        api, _ = self._tap("both")
        self.assertEqual(len(api.answered), 1)

    def test_somebody_else_pressing_it_posts_nothing(self):
        api = FakeApi()
        flows = {"f1": _flow()}
        press = _press("both", "f1")
        press["from"] = {"id": 99, "username": "someone"}
        with patch.object(bot_listener, "_can_manage_chat", _AlwaysAdmin(True)):
            _run(bot_listener.handle_vote_chat_destination_callback(
                api, _cfg(), press, flows, BOT, log=lambda *_: None,
            ))
        self.assertEqual(api.sent, [])
        self.assertIn("не для тебя", api.answered[0]["text"])
        self.assertIn("f1", flows)

    def test_an_expired_draft_posts_nothing(self):
        flow = _flow()
        flow["created_at"] -= bot_listener.VOTE_CHAT_FLOW_TTL_SECONDS + 1
        api, flows = self._tap("both", flows={"f1": flow})
        self.assertEqual(api.sent, [])
        self.assertEqual(flows, {})

    def test_losing_admin_rights_between_the_draft_and_the_button_posts_nothing(self):
        api, _ = self._tap("both", admin=False)
        self.assertEqual(api.posts_to(MAIN_CHAT_ID), [])
        self.assertIn("администратор", api.posts_to(DM_CHAT_ID)[-1]["text"])

    def test_an_unknown_bot_username_posts_nothing(self):
        api, _ = self._tap("main", bot_username=None)
        self.assertEqual(api.posts_to(MAIN_CHAT_ID), [])


class DestinationCallbackDataTests(unittest.TestCase):
    def test_data_round_trips(self):
        data = bot_listener._vote_chat_dest_callback_data("both", "abc123")
        self.assertEqual(bot_listener._parse_vote_chat_dest_callback(data), ("both", "abc123"))

    def test_an_unknown_destination_is_rejected(self):
        self.assertIsNone(bot_listener._parse_vote_chat_dest_callback("votechatdest:nowhere:f1"))

    def test_malformed_data_is_rejected(self):
        self.assertIsNone(bot_listener._parse_vote_chat_dest_callback("votechatdest:main"))
        self.assertIsNone(bot_listener._parse_vote_chat_dest_callback("votechatdest:main:"))
        self.assertIsNone(bot_listener._parse_vote_chat_dest_callback(""))
        self.assertIsNone(bot_listener._parse_vote_chat_dest_callback(None))


class DestinationDispatchTests(unittest.TestCase):
    """Same reason test_vote_dispatch.py pins the voteaction prefix: a callback that never
    reaches its handler looks exactly like a handler bug from the outside."""

    def test_a_destination_button_reaches_its_handler_through_update_dispatch(self):
        handled = []

        async def handle(*args, **kwargs):
            handled.append(args[2])  # (api, cfg, callback, flows, bot_username)

        async def go():
            with patch.object(bot_listener, "handle_vote_chat_destination_callback", handle):
                await bot_listener._dispatch_update(
                    {"callback_query": _press("both", "f1")},
                    FakeApi(), None, None, None, BOT, 1, set(), asyncio.Queue(),
                    set(), "chat", {}, {}, {}, {},
                    log=lambda *_: None,
                )

        _run(go())
        self.assertEqual(len(handled), 1)
        self.assertEqual(handled[0]["data"], "votechatdest:both:f1")


# ---- the weekly results draft ------------------------------------------------------------

# Not DM_CHAT_ID: this draft goes to whoever closed the vote, in their own DM with the bot,
# and in a DM Telegram's chat id IS the user id. The results flow relies on exactly that,
# so the fixture has to agree with it rather than invent a separate chat.
RESULT_DM = ADMIN["id"]
RESULTS_TEXT = "Итоги недельного голосования\n\nПервое место"


def _work(entry_id, name, votes):
    return SimpleNamespace(
        entry_id=entry_id, author_name=name, author_username=None, text="", media=[]
    ), votes


STANDINGS = [_work("a", "Аня", 3), _work("b", "Боря", 2), _work("c", "Вера", 0)]


def _poll(open_=False):
    return SimpleNamespace(entry="Chat", poll_id="2026-W31", open=open_)


def _result_flow(text=RESULTS_TEXT, poll=None):
    return {
        "chat_id": RESULT_DM,
        "user_id": ADMIN["id"],
        "entry": "Chat",
        "admin_chat_id": MAIN_CHAT_ID,
        "poll": poll or _poll(),
        "standings": STANDINGS,
        "text": text,
        "prompt_message_id": None,
        "created_at": time.monotonic(),
    }


def _press_result(action, flow_id):
    return {
        "id": "cbq2",
        "data": bot_listener._vote_result_callback_data(action, flow_id),
        "from": ADMIN,
        "message": {"message_id": 11},
    }


def _result_reply(text, prompt_id):
    return {
        "message_id": 12,
        "chat": {"id": RESULT_DM, "type": "private"},
        "from": ADMIN,
        "text": text,
        "reply_to_message": {"message_id": prompt_id},
    }


def _buttons(message):
    return [b["text"] for row in (message["reply_markup"] or {})["inline_keyboard"] for b in row]


class _SavedResults:
    """Stands in for voting.save_results, which is another agent's file and would write a
    real JSON record next to a real poll. Records the calls instead, since WHEN the record
    is written (on drafting, on every edit, after posting) is the interesting part."""

    def __init__(self):
        self.calls = []

    def __call__(self, poll, standings, text):
        self.calls.append({"poll_id": poll.poll_id, "standings": standings, "text": text})
        return Path("results.json")


class ResultsDraftTests(unittest.TestCase):
    """send_vote_results_draft -- what the admin gets the moment they close the vote."""

    def _draft(self, api=None, flows=None, standings=STANDINGS, text=RESULTS_TEXT):
        api = api or FakeApi()
        flows = {} if flows is None else flows
        saved = _SavedResults()

        with patch.object(voting, "save_results", saved, create=True), \
                patch.object(voting, "format_results_text", lambda s, p=3: text, create=True):
            _run(bot_listener.send_vote_results_draft(
                api, ADMIN, _poll(), standings, MAIN_CHAT_ID, flows, log=lambda *_: None,
            ))
        return api, flows, saved

    def test_the_draft_goes_to_the_admins_dm_and_nowhere_else(self):
        api, flows, _ = self._draft()
        self.assertEqual([s["chat_id"] for s in api.sent], [RESULT_DM])
        self.assertEqual(api.posts_to(MAIN_CHAT_ID), [])
        self.assertEqual(api.sent[0]["text"], RESULTS_TEXT)
        self.assertEqual(len(flows), 1)

    def test_the_draft_carries_exactly_the_three_review_buttons(self):
        api, _, _ = self._draft()
        self.assertEqual(_buttons(api.sent[0]), ["Редактировать", "Отправить", "Отмена"])

    def test_the_draft_is_never_scheduled_for_deletion(self):
        """The admin comes back to this message when they have thought of better wording,
        so no auto-delete sweep may touch it -- same rule as the /vote announcement."""
        scheduled = []
        with patch.object(bot_listener, "schedule_bot_delete", lambda *a, **k: scheduled.append(a)):
            api, _, _ = self._draft()
        self.assertEqual(scheduled, [])
        self.assertEqual(len(api.sent), 1)

    def test_the_results_are_recorded_before_any_button_is_pressed(self):
        """Closing the vote is what produces the results -- announcing them is not -- so
        Отмена can honestly promise the week's result is kept."""
        _, _, saved = self._draft()
        self.assertEqual(len(saved.calls), 1)
        self.assertEqual(saved.calls[0]["text"], RESULTS_TEXT)
        self.assertEqual(saved.calls[0]["standings"], STANDINGS)

    def test_nothing_is_drafted_when_there_are_no_standings_at_all(self):
        api, flows, saved = self._draft(standings=[])
        self.assertEqual(api.sent, [])
        self.assertEqual(flows, {})
        self.assertEqual(saved.calls, [])

    def test_a_second_close_replaces_the_first_draft_rather_than_stacking(self):
        api, flows, _ = self._draft()
        first = next(iter(flows))
        self._draft(api=api, flows=flows)
        self.assertEqual(len(flows), 1)
        self.assertNotIn(first, flows)

    def test_a_dm_the_bot_cannot_write_to_leaves_no_orphan_buttons(self):
        api = FakeApi(failing_chats=(RESULT_DM,))
        _, flows, _ = self._draft(api=api, flows={})
        self.assertEqual(flows, {})


class ResultsButtonTests(unittest.TestCase):
    """Редактировать / Отправить / Отмена."""

    def _tap(self, action, flows=None, admin=True, failing_chats=(), clicker=ADMIN):
        api = FakeApi(failing_chats=failing_chats)
        flows = {"r1": _result_flow()} if flows is None else flows
        press = _press_result(action, "r1")
        press["from"] = clicker
        saved = _SavedResults()
        with patch.object(bot_listener, "_can_manage_chat", _AlwaysAdmin(admin)), \
                patch.object(voting, "save_results", saved, create=True):
            _run(bot_listener.handle_vote_result_callback(
                api, press, flows, log=lambda *_: None,
            ))
        return api, flows, saved

    # ---- Отмена ----

    def test_cancel_posts_nothing_and_says_the_vote_stays_closed(self):
        flows = {"r1": _result_flow(poll=_poll(open_=False))}
        api, flows, _ = self._tap("cancel", flows=flows)
        self.assertEqual(api.posts_to(MAIN_CHAT_ID), [])
        self.assertEqual(flows, {})
        report = api.posts_to(RESULT_DM)[-1]["text"]
        self.assertIn("не опубликованы", report)
        self.assertIn("закрытым", report)

    def test_cancel_does_not_reopen_the_poll(self):
        poll = _poll(open_=False)
        self._tap("cancel", flows={"r1": _result_flow(poll=poll)})
        self.assertFalse(poll.open)

    # ---- Отправить ----

    def test_send_posts_the_results_text_into_the_main_chat(self):
        api, flows, _ = self._tap("send")
        posted = api.posts_to(MAIN_CHAT_ID)
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["text"], RESULTS_TEXT)
        self.assertIsNone(posted[0]["reply_markup"])  # the review buttons stay in the DM
        self.assertEqual(flows, {})

    def test_send_confirms_back_to_the_admin(self):
        api, _, _ = self._tap("send")
        self.assertIn("опубликованы", api.posts_to(RESULT_DM)[-1]["text"])

    def test_the_posted_results_are_never_scheduled_for_deletion(self):
        """The one message of the week people scroll back to."""
        scheduled = []
        with patch.object(bot_listener, "schedule_bot_delete", lambda *a, **k: scheduled.append(a)):
            api, _, _ = self._tap("send")
        self.assertEqual(scheduled, [])
        self.assertEqual(len(api.posts_to(MAIN_CHAT_ID)), 1)

    def test_the_record_is_refreshed_after_a_successful_post(self):
        _, _, saved = self._tap("send")
        self.assertEqual([c["text"] for c in saved.calls], [RESULTS_TEXT])

    def test_a_failed_post_keeps_the_draft_so_it_can_be_retried(self):
        api, flows, _ = self._tap("send", failing_chats=(MAIN_CHAT_ID,))
        self.assertEqual(api.posts_to(MAIN_CHAT_ID), [])
        self.assertIn("r1", flows)
        self.assertIn("Не получилось", api.posts_to(RESULT_DM)[-1]["text"])

    def test_an_unresolvable_main_chat_reports_rather_than_posting_into_the_void(self):
        flow = _result_flow()
        flow["admin_chat_id"] = None
        api, _, _ = self._tap("send", flows={"r1": flow})
        self.assertEqual(len(api.posts_to(RESULT_DM)), 1)
        self.assertIn("основной чат", api.posts_to(RESULT_DM)[0]["text"])

    # ---- who may press ----

    def test_somebody_elses_press_posts_nothing(self):
        api, flows, _ = self._tap("send", clicker={"id": 99, "username": "someone"})
        self.assertEqual(api.sent, [])
        self.assertIn("не для тебя", api.answered[0]["text"])
        self.assertIn("r1", flows)

    def test_losing_admin_rights_between_the_draft_and_the_button_posts_nothing(self):
        api, flows, _ = self._tap("send", admin=False)
        self.assertEqual(api.posts_to(MAIN_CHAT_ID), [])
        self.assertIn("администратор", api.posts_to(RESULT_DM)[-1]["text"])
        self.assertEqual(flows, {})

    def test_cancel_needs_no_admin_check_at_all(self):
        """Refusing to NOT post something would be an odd thing to guard -- and the flow's
        owner is already the only person who can see the button."""
        api, flows, _ = self._tap("cancel", admin=False)
        self.assertEqual(flows, {})
        self.assertIn("не опубликованы", api.posts_to(RESULT_DM)[-1]["text"])

    def test_an_expired_draft_posts_nothing(self):
        flow = _result_flow()
        flow["created_at"] -= bot_listener.VOTE_RESULT_FLOW_TTL_SECONDS + 1
        api, flows, _ = self._tap("send", flows={"r1": flow})
        self.assertEqual(api.sent, [])
        self.assertEqual(flows, {})
        self.assertIn("устарел", api.answered[0]["text"])

    def test_the_spinner_is_stopped_before_anything_is_posted(self):
        api, _, _ = self._tap("send")
        self.assertEqual(len(api.answered), 1)


class ResultsEditTests(unittest.TestCase):
    """Редактировать -> force-reply -> the draft comes back with the new text, as often as
    the admin wants."""

    def setUp(self):
        self.api = FakeApi()
        self.flows = {"r1": _result_flow()}
        self.saved = _SavedResults()

    def _press_edit(self):
        with patch.object(bot_listener, "_can_manage_chat", _AlwaysAdmin(True)), \
                patch.object(voting, "save_results", self.saved, create=True):
            _run(bot_listener.handle_vote_result_callback(
                self.api, _press_result("edit", "r1"), self.flows, log=lambda *_: None,
            ))
        return self.api.sent[-1]

    def _reply(self, text, prompt_id):
        with patch.object(bot_listener, "_can_manage_chat", _AlwaysAdmin(True)), \
                patch.object(voting, "save_results", self.saved, create=True):
            return _run(bot_listener.handle_vote_result_text_input(
                self.api, _result_reply(text, prompt_id), self.flows, log=lambda *_: None,
            ))

    def _round(self, text):
        prompt = self._press_edit()
        self.assertTrue(prompt["reply_markup"].get("force_reply"))
        handled = self._reply(text, prompt["message_id"])
        self.assertTrue(handled)
        return self.api.sent[-1]

    def test_editing_re_shows_the_draft_with_the_new_text_and_the_same_buttons(self):
        redrawn = self._round("Первый вариант")
        self.assertEqual(redrawn["chat_id"], RESULT_DM)
        self.assertEqual(redrawn["text"], "Первый вариант")
        self.assertEqual(_buttons(redrawn), ["Редактировать", "Отправить", "Отмена"])
        self.assertEqual(self.flows["r1"]["text"], "Первый вариант")

    def test_editing_twice_in_a_row_works(self):
        self._round("Первый вариант")
        redrawn = self._round("Второй вариант")
        self.assertEqual(redrawn["text"], "Второй вариант")
        self.assertEqual(self.flows["r1"]["text"], "Второй вариант")
        self.assertEqual(
            [c["text"] for c in self.saved.calls], ["Первый вариант", "Второй вариант"]
        )

    def test_the_edited_text_is_what_finally_reaches_the_chat(self):
        self._round("Окончательный текст")
        with patch.object(bot_listener, "_can_manage_chat", _AlwaysAdmin(True)), \
                patch.object(voting, "save_results", self.saved, create=True):
            _run(bot_listener.handle_vote_result_callback(
                self.api, _press_result("send", "r1"), self.flows, log=lambda *_: None,
            ))
        self.assertEqual(self.api.posts_to(MAIN_CHAT_ID)[0]["text"], "Окончательный текст")

    def test_a_stale_prompt_cannot_swallow_an_unrelated_reply_later(self):
        prompt = self._press_edit()
        self._reply("Новый текст", prompt["message_id"])
        self.assertFalse(self._reply("что-то другое", prompt["message_id"]))
        self.assertEqual(self.flows["r1"]["text"], "Новый текст")

    def test_a_reply_that_is_not_to_a_prompt_is_not_ours(self):
        self.assertFalse(self._reply("просто сообщение", 999))
        self.assertEqual(self.api.sent, [])

    def test_cancelling_at_the_edit_step_drops_the_draft_without_posting(self):
        prompt = self._press_edit()
        self._reply("отмена", prompt["message_id"])
        self.assertEqual(self.flows, {})
        self.assertEqual(self.api.posts_to(MAIN_CHAT_ID), [])
        self.assertIn("закрытым", self.api.posts_to(RESULT_DM)[-1]["text"])

    def test_an_empty_reply_re_shows_the_draft_rather_than_losing_it(self):
        prompt = self._press_edit()
        self._reply("   ", prompt["message_id"])
        self.assertIn("r1", self.flows)
        self.assertEqual(self.flows["r1"]["text"], RESULTS_TEXT)
        self.assertEqual(_buttons(self.api.sent[-1]), ["Редактировать", "Отправить", "Отмена"])

    def test_losing_admin_rights_mid_edit_drops_the_draft_silently(self):
        prompt = self._press_edit()
        before = len(self.api.sent)
        with patch.object(bot_listener, "_can_manage_chat", _AlwaysAdmin(False)), \
                patch.object(voting, "save_results", self.saved, create=True):
            handled = _run(bot_listener.handle_vote_result_text_input(
                self.api, _result_reply("Новый текст", prompt["message_id"]), self.flows,
                log=lambda *_: None,
            ))
        self.assertTrue(handled)
        self.assertEqual(self.flows, {})
        self.assertEqual(len(self.api.sent), before)


class ResultsTextFallbackTests(unittest.TestCase):
    def test_the_wording_comes_from_voting_when_it_is_available(self):
        with patch.object(voting, "format_results_text", lambda s, p=None: "из voting", create=True):
            self.assertEqual(bot_listener._vote_results_text(STANDINGS), "из voting")

    def test_the_whole_board_is_asked_for_not_a_podium(self):
        """The announcement names every entrant, so nothing on this side may cap the list
        -- a stray places=3 here would silently drop the tail of a big contest."""
        seen = {}

        def spy(standings, places=None):
            seen["places"] = places
            return "ok"

        with patch.object(voting, "format_results_text", spy, create=True):
            bot_listener._vote_results_text(STANDINGS)
        self.assertIsNone(seen["places"])

    def test_a_broken_formatter_still_produces_a_usable_text(self):
        """The poll is already closed by the time this runs, so there is no "try again
        later" -- something has to come out of it."""
        def boom(standings, places=None):
            raise RuntimeError("nope")

        with patch.object(voting, "format_results_text", boom, create=True):
            text = bot_listener._vote_results_text(STANDINGS)
        self.assertIn("Аня", text)
        self.assertIn("Боря", text)
        self.assertIn("Вера", text)  # every entrant, the zero-vote one included


class ResultsCallbackDataTests(unittest.TestCase):
    def test_data_round_trips(self):
        for action in bot_listener.VOTE_RESULT_ACTIONS:
            data = bot_listener._vote_result_callback_data(action, "abc123")
            self.assertEqual(bot_listener._parse_vote_result_callback(data), (action, "abc123"))

    def test_an_unknown_action_is_rejected(self):
        self.assertIsNone(bot_listener._parse_vote_result_callback("voteresult:delete:r1"))

    def test_malformed_data_is_rejected(self):
        self.assertIsNone(bot_listener._parse_vote_result_callback("voteresult:send"))
        self.assertIsNone(bot_listener._parse_vote_result_callback("voteresult:send:"))
        self.assertIsNone(bot_listener._parse_vote_result_callback(""))
        self.assertIsNone(bot_listener._parse_vote_result_callback(None))

    def test_the_prefix_does_not_collide_with_the_other_vote_callbacks(self):
        """"voteresult" and "voteclear" both start with "vote" -- the dispatcher matches on
        "<prefix>:", so the colon is what keeps them apart."""
        for other in (
            bot_listener.VOTE_CLEAR_CALLBACK_PREFIX,
            bot_listener.VOTE_ACTION_CALLBACK_PREFIX,
            bot_listener.VOTE_CHAT_DEST_CALLBACK_PREFIX,
        ):
            self.assertFalse(f"{other}:".startswith(f"{bot_listener.VOTE_RESULT_CALLBACK_PREFIX}:"))
            self.assertFalse(f"{bot_listener.VOTE_RESULT_CALLBACK_PREFIX}:".startswith(f"{other}:"))


class ResultsDispatchTests(unittest.TestCase):
    """A callback that never reaches its handler looks exactly like a handler bug from the
    outside -- same reason test_vote_dispatch.py pins the voteaction prefix."""

    def test_a_review_button_reaches_its_handler_through_update_dispatch(self):
        handled = []

        async def handle(api, callback, flows, log=print):
            handled.append(callback)

        async def go():
            with patch.object(bot_listener, "handle_vote_result_callback", handle):
                await bot_listener._dispatch_update(
                    {"callback_query": _press_result("send", "r1")},
                    FakeApi(), None, None, None, BOT, 1, set(), asyncio.Queue(),
                    set(), "chat", {}, {}, {}, {},
                    log=lambda *_: None,
                )

        _run(go())
        self.assertEqual(len(handled), 1)
        self.assertEqual(handled[0]["data"], "voteresult:send:r1")

    def test_the_edit_reply_reaches_its_handler_through_update_dispatch(self):
        handled = []

        async def handle(api, message, flows, log=print):
            handled.append(message)
            return True

        cfg = SimpleNamespace(listener_allowed_chats=[], stats_enabled=False)

        async def go():
            with patch.object(bot_listener, "handle_vote_result_text_input", handle):
                await bot_listener._dispatch_update(
                    {"message": _result_reply("Новый текст", 5)},
                    FakeApi(), None, cfg, None, BOT, 1, set(), asyncio.Queue(),
                    set(), "chat", {}, {}, {}, {},
                    log=lambda *_: None,
                )

        _run(go())
        self.assertEqual(len(handled), 1)
        self.assertEqual(handled[0]["text"], "Новый текст")


if __name__ == "__main__":
    unittest.main()
