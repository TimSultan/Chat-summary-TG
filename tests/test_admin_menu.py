"""/admin -- one panel holding every management command the bot has.

The panel is an index, not a new permission, and the two things that could go wrong with
it are the ones pinned hardest here.

First, DRIFT. Every button ends in the handler its typed command already runs, so a button
wired to nothing -- or an action added to the catalogue and forgotten -- is the failure
this file exists to catch.

Second, the WEIGHT of a stray tap. Some of these commands post to a group of 190 people or
reset the chat's shared tree. Those may never be one press away, and which ones they are
is asserted by name rather than left to whoever adds the next action.
"""

import asyncio
import inspect
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import admin_menu
import bot_listener

CHAT = "Единый Чат Художников"
DM_CHAT_ID = 555
MAIN_CHAT_ID = -1001234567890
PANEL_MESSAGE_ID = 100
ADMIN = {"id": 42, "username": "admin", "first_name": "Админ"}
STRANGER = {"id": 77, "username": "someone", "first_name": "Кто-то"}


def _run(coro):
    return asyncio.run(coro)


def _quiet(*args, **kwargs):
    """The panel logs who ran what; the suite does not need it."""


def _answering(value):
    """A stand-in for one of bot_listener's `async def` helpers, fixed to one answer."""

    async def _stub(*args, **kwargs):
        return value

    return _stub


class FakeApi:
    def __init__(self):
        self.sent = []
        self.edited = []
        self.answered = []

    async def send_message(self, chat_id, text, reply_to_message_id=None,
                           reply_markup=None, parse_mode=None):
        item = {"message_id": 200 + len(self.sent), "chat_id": chat_id, "text": text,
                "reply_markup": reply_markup, "reply_to_message_id": reply_to_message_id}
        self.sent.append(item)
        return item

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None, parse_mode=None):
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text,
                            "reply_markup": reply_markup})

    async def answer_callback_query(self, callback_id, text=None):
        self.answered.append((callback_id, text))


def _buttons(markup) -> list[dict]:
    return [button for row in (markup or {}).get("inline_keyboard", []) for button in row]


def _message(user, text=admin_menu.COMMAND, chat_type="private", message_id=5,
             reply_to_message_id=None):
    message = {
        "message_id": message_id,
        "chat": {"id": DM_CHAT_ID if chat_type == "private" else MAIN_CHAT_ID, "type": chat_type},
        "from": user,
        "text": text,
    }
    if reply_to_message_id is not None:
        message["reply_to_message"] = {"message_id": reply_to_message_id}
    return message


def _callback(user, data):
    return {
        "id": "cb1",
        "from": user,
        "data": data,
        "message": {"message_id": PANEL_MESSAGE_ID,
                    "chat": {"id": DM_CHAT_ID, "type": "private"}},
    }


class Recorder:
    """Stands in for one of the real command handlers and remembers being called."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))

        async def _noop():
            return None

        return _noop()

    @property
    def ran(self) -> bool:
        return bool(self.calls)


# The handler each action must reach. Kept as a table because it is also the assertion
# that the catalogue and the router agree -- a new action with nothing wired to it fails
# test_every_action_reaches_a_handler below.
HANDLER_FOR_ACTION = {
    "send": "handle_send_command",
    "preview": "handle_preview_command",
    "buttons": "handle_button_builder_command",
    "via": "handle_via_cleaner_command",
    "vote": "handle_vote_command",
    "vote2": "handle_arena_command",
    "badge": "handle_badge_command",
    "badgeadmin": "handle_badge_admin_command",
    "weekwinner": "handle_week_winner_command",
    "deletepokras": "handle_delete_pokras_command",
    "plant": "handle_plant_command",
    "plantreminder": "handle_plant_reminder_command",
    "replant": "handle_replant_command",
    "arenanews": "handle_arena_news_command",
}


class CatalogueTests(unittest.TestCase):
    def test_every_action_is_complete(self):
        for item in admin_menu.ACTIONS:
            with self.subTest(action=item["id"]):
                self.assertTrue(item["command"].startswith("/"))
                self.assertTrue(item["label"] and item["hint"] and item["section"])
                self.assertIn(item["kind"], ("open", "ask", "confirm"))
                if item["kind"] == "ask":
                    self.assertTrue(item.get("prompt"), "an ask must have a question")
                if item["kind"] == "confirm":
                    self.assertTrue(item.get("confirm"), "a confirm must have a question")

    def test_action_ids_are_unique(self):
        ids = [item["id"] for item in admin_menu.ACTIONS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_action_reaches_a_handler(self):
        """The drift this file exists to catch: a button wired to nothing."""
        source = inspect.getsource(bot_listener._run_admin_action)
        for item in admin_menu.ACTIONS:
            with self.subTest(action=item["id"]):
                self.assertIn(f'"{item["id"]}"', source)
                self.assertIn(HANDLER_FOR_ACTION[item["id"]], source)

    def test_the_router_has_no_actions_the_catalogue_lacks(self):
        self.assertEqual(set(HANDLER_FOR_ACTION), {item["id"] for item in admin_menu.ACTIONS})

    def test_anything_that_writes_to_the_group_asks_first(self):
        """Named one by one on purpose.

        These three post into a chat of 190 people or start the shared tree over, and
        "which button is dangerous" is not a judgement the next person to add an action
        should have to make from scratch.
        """
        for action_id in ("plant", "plantreminder", "replant"):
            with self.subTest(action=action_id):
                self.assertEqual(admin_menu.action(action_id)["kind"], "confirm")

    def test_the_commands_are_the_ones_the_bot_actually_answers(self):
        # A legend naming a command that no longer exists is worse than no legend.
        self.assertEqual(admin_menu.action("via")["command"], bot_listener.VIA_CLEANER_COMMANDS[0])
        self.assertEqual(admin_menu.action("buttons")["command"], bot_listener.BUTTON_BUILDER_COMMAND)
        self.assertEqual(admin_menu.action("badgeadmin")["command"], bot_listener.BADGE_ADMIN_COMMAND)
        self.assertEqual(admin_menu.action("weekwinner")["command"], bot_listener.WEEK_WINNER_COMMAND)
        self.assertEqual(admin_menu.action("deletepokras")["command"], bot_listener.DELETE_POKRAS_COMMAND)
        self.assertEqual(admin_menu.action("send")["command"], bot_listener.SEND_COMMAND)
        self.assertEqual(admin_menu.action("preview")["command"], bot_listener.PREVIEW_COMMAND)
        self.assertEqual(admin_menu.action("replant")["command"], bot_listener.REPLANT_COMMAND)
        self.assertIn(admin_menu.action("plant")["command"], bot_listener.PLANT_COMMANDS)
        self.assertIn(admin_menu.action("plantreminder")["command"], bot_listener.PLANT_REMINDER_COMMANDS)
        self.assertIn(admin_menu.action("arenanews")["command"], bot_listener.ARENA_NEWS_COMMANDS)
        self.assertIn(admin_menu.action("vote")["command"], bot_listener.VOTE_COMMANDS)
        self.assertIn(admin_menu.action("vote2")["command"], bot_listener.ARENA_COMMANDS)


class RenderingTests(unittest.TestCase):
    def test_the_keyboard_has_one_button_per_action(self):
        buttons = _buttons(admin_menu.menu_keyboard())
        self.assertEqual(len(buttons), len(admin_menu.ACTIONS))
        self.assertEqual(
            [b["callback_data"] for b in buttons],
            [admin_menu.callback_data("run", item["id"]) for item in admin_menu.ACTIONS],
        )

    def test_a_row_never_mixes_two_sections(self):
        # The legend above the keyboard is grouped; rows that straddle a group turn it
        # into a wall of text beside an unrelated grid.
        by_id = {item["id"]: item for item in admin_menu.ACTIONS}
        for row in admin_menu.menu_keyboard()["inline_keyboard"]:
            sections = {by_id[b["callback_data"].split(":")[2]]["section"] for b in row}
            self.assertEqual(len(sections), 1, row)
            self.assertLessEqual(len(row), 2)

    def test_the_legend_names_every_command(self):
        text = admin_menu.menu_text()
        for item in admin_menu.ACTIONS:
            with self.subTest(action=item["id"]):
                self.assertIn(item["command"], text)
                self.assertIn(item["hint"], text)

    def test_the_panel_fits_in_one_telegram_message(self):
        self.assertLess(len(admin_menu.menu_text()), 4000)

    def test_callback_data_stays_inside_telegram_s_limit(self):
        for button in _buttons(admin_menu.menu_keyboard()):
            self.assertLessEqual(len(button["callback_data"].encode("utf-8")), 64)

    def test_callback_data_round_trips(self):
        self.assertEqual(admin_menu.parse_callback("adminmenu:run:send"), ("run", "send"))
        self.assertEqual(admin_menu.parse_callback("adminmenu:menu"), ("menu", ""))
        self.assertIsNone(admin_menu.parse_callback("viaclean:toggle"))
        self.assertIsNone(admin_menu.parse_callback(""))

    def test_a_confirmation_repeats_what_is_about_to_happen(self):
        item = admin_menu.action("replant")
        text = admin_menu.confirm_text(item)
        self.assertIn(item["confirm"], text)
        buttons = _buttons(admin_menu.confirm_keyboard(item))
        self.assertEqual(buttons[0]["callback_data"], admin_menu.callback_data("go", "replant"))
        self.assertEqual(buttons[-1]["callback_data"], admin_menu.callback_data("menu"))

    def test_a_prompt_says_how_to_get_out(self):
        # A force-reply cannot carry an inline keyboard, so the way back has to be a word.
        text = admin_menu.prompt_text(admin_menu.action("send"))
        self.assertIn("отмена", text.lower())
        self.assertTrue(admin_menu.is_cancel("Отмена"))
        self.assertTrue(admin_menu.is_cancel("/cancel"))
        self.assertFalse(admin_menu.is_cancel("Привет, чат"))


def _open_panel(api, user=ADMIN, entry=CHAT, admin_chat_id=MAIN_CHAT_ID, admin=True):
    with patch.object(bot_listener, "_is_chat_admin_or_privileged", _answering(admin)):
        _run(bot_listener.handle_admin_command(
            api, _message(user), entry, admin_chat_id, log=_quiet,
        ))


async def _press(api, data, user=ADMIN, admin_flows=None):
    await bot_listener.handle_admin_callback(
        api, None, SimpleNamespace(), None, _callback(user, data), CHAT,
        admin_flows if admin_flows is not None else {}, "testbot", set(),
        {CHAT: MAIN_CHAT_ID}, {}, {}, {}, log=_quiet,
    )


async def _reply(api, message, admin_flows):
    return await bot_listener.handle_admin_text_input(
        api, None, SimpleNamespace(), None, message, CHAT, admin_flows,
        "testbot", set(), {CHAT: MAIN_CHAT_ID}, {}, {}, {}, log=_quiet,
    )


def _as_admin(coro, admin=True):
    """Runs `coro` with the administrator gate answering, and no Telethon lookup."""
    with patch.object(bot_listener, "_is_chat_admin_or_privileged", _answering(admin)), \
         patch.object(bot_listener, "_resolve_chat_id", _answering(MAIN_CHAT_ID)):
        return _run(coro)


class GateTests(unittest.TestCase):
    def test_an_administrator_gets_the_panel(self):
        api = FakeApi()
        _open_panel(api)
        self.assertEqual(len(api.sent), 1)
        self.assertEqual(len(_buttons(api.sent[0]["reply_markup"])), len(admin_menu.ACTIONS))

    def test_a_stranger_gets_a_refusal_and_no_buttons(self):
        api = FakeApi()
        _open_panel(api, user=STRANGER, admin=False)
        self.assertEqual(len(api.sent), 1)
        self.assertIsNone(api.sent[0]["reply_markup"])
        self.assertIn("администратор", api.sent[0]["text"])

    def test_the_panel_needs_a_configured_chat(self):
        api = FakeApi()
        _open_panel(api, entry=None, admin_chat_id=None)
        self.assertIn("не настроен", api.sent[0]["text"])

    def test_somebody_who_has_stopped_being_an_administrator_loses_the_buttons(self):
        api = FakeApi()
        recorder = Recorder()
        with patch.object(bot_listener, "handle_via_cleaner_command", recorder):
            _as_admin(_press(api, "adminmenu:run:via"), admin=False)
        self.assertFalse(recorder.ran)
        self.assertIn("администратор", api.edited[-1]["text"])
        self.assertIsNone(api.edited[-1]["reply_markup"])

    def test_the_spinner_is_stopped_before_anything_that_can_wait(self):
        """A Telethon call before answerCallbackQuery leaves the button spinning for ever."""
        api = FakeApi()
        order = []
        original = api.answer_callback_query

        async def _answer(callback_id, text=None):
            order.append("answer")
            await original(callback_id, text)

        async def _resolve(*args, **kwargs):
            order.append("resolve")
            return MAIN_CHAT_ID

        api.answer_callback_query = _answer
        with patch.object(bot_listener, "_is_chat_admin_or_privileged", _answering(True)), \
             patch.object(bot_listener, "_resolve_chat_id", _resolve), \
             patch.object(bot_listener, "handle_via_cleaner_command", Recorder()):
            _run(_press(api, "adminmenu:run:via"))
        self.assertEqual(order[0], "answer")


class PressTests(unittest.TestCase):
    def test_an_open_action_presses_straight_through(self):
        api = FakeApi()
        recorder = Recorder()
        with patch.object(bot_listener, "handle_via_cleaner_command", recorder):
            _as_admin(_press(api, "adminmenu:run:via"))
        self.assertTrue(recorder.ran)
        # The handler is given the panel's own chat and message, so its reply threads onto
        # the screen the administrator is looking at.
        message = recorder.calls[0][0][1]
        self.assertEqual(message["chat"]["id"], DM_CHAT_ID)
        self.assertEqual(message["message_id"], PANEL_MESSAGE_ID)
        self.assertEqual(message["from"], ADMIN)
        # /vote and /vote2 read the text to find their sub-command; a stand-in without it
        # would open their root panel by luck rather than by construction.
        self.assertEqual(message["text"], admin_menu.action("via")["command"])

    def test_every_open_action_reaches_its_own_handler(self):
        for item in admin_menu.ACTIONS:
            if item["kind"] != "open":
                continue
            with self.subTest(action=item["id"]):
                api = FakeApi()
                recorder = Recorder()
                with patch.object(bot_listener, HANDLER_FOR_ACTION[item["id"]], recorder):
                    _as_admin(_press(api, admin_menu.callback_data("run", item["id"])))
                self.assertTrue(recorder.ran)

    def test_a_confirm_action_does_not_run_on_the_first_press(self):
        api = FakeApi()
        recorder = Recorder()
        with patch.object(bot_listener, "handle_replant_command", recorder):
            _as_admin(_press(api, "adminmenu:run:replant"))
        self.assertFalse(recorder.ran, "one stray tap started the tree over")
        self.assertIn(admin_menu.action("replant")["confirm"], api.edited[-1]["text"])

    def test_a_confirmed_action_runs_and_gives_the_panel_back(self):
        api = FakeApi()
        recorder = Recorder()
        with patch.object(bot_listener, "handle_replant_command", recorder):
            _as_admin(_press(api, "adminmenu:go:replant"))
        self.assertTrue(recorder.ran)
        self.assertEqual(len(_buttons(api.edited[-1]["reply_markup"])), len(admin_menu.ACTIONS))

    def test_back_returns_to_the_panel(self):
        api = FakeApi()
        _as_admin(_press(api, "adminmenu:menu"))
        self.assertEqual(len(_buttons(api.edited[-1]["reply_markup"])), len(admin_menu.ACTIONS))

    def test_an_unknown_action_falls_back_to_the_panel(self):
        api = FakeApi()
        _as_admin(_press(api, "adminmenu:run:nothing_like_this"))
        self.assertEqual(len(_buttons(api.edited[-1]["reply_markup"])), len(admin_menu.ACTIONS))


class AskTests(unittest.TestCase):
    def _ask(self, api, action_id="send"):
        flows: dict[str, dict] = {}
        recorder = Recorder()
        with patch.object(bot_listener, HANDLER_FOR_ACTION[action_id], recorder):
            _as_admin(_press(api, admin_menu.callback_data("run", action_id), admin_flows=flows))
        return flows, recorder

    def test_it_asks_before_it_acts(self):
        api = FakeApi()
        flows, recorder = self._ask(api)
        self.assertFalse(recorder.ran)
        self.assertEqual(len(flows), 1)
        self.assertTrue(api.sent[-1]["reply_markup"]["force_reply"])

    def test_the_answer_runs_the_command_with_it(self):
        api = FakeApi()
        flows, _ = self._ask(api)
        prompt_id = api.sent[-1]["message_id"]
        recorder = Recorder()
        with patch.object(bot_listener, "handle_send_command", recorder):
            claimed = _as_admin(_reply(
                api, _message(ADMIN, text="Привет, чат", message_id=9,
                              reply_to_message_id=prompt_id),
                flows,
            ))
        self.assertTrue(claimed)
        self.assertTrue(recorder.ran)
        # The composed command is what the typed one would have been, so the handler's own
        # parsing and permission check are the only ones that ever run.
        self.assertEqual(recorder.calls[0][0][2], "/send Привет, чат")

    def test_the_prompt_is_answered_once_and_then_gone(self):
        # A second reply to the same prompt would be a second post to the chat.
        api = FakeApi()
        flows, _ = self._ask(api)
        prompt_id = api.sent[-1]["message_id"]
        reply = _message(ADMIN, text="Привет, чат", message_id=9, reply_to_message_id=prompt_id)
        recorder = Recorder()
        with patch.object(bot_listener, "handle_send_command", recorder):
            _as_admin(_reply(api, reply, flows))
            claimed_again = _as_admin(_reply(api, reply, flows))
        self.assertEqual(len(recorder.calls), 1)
        self.assertFalse(claimed_again)
        self.assertEqual(flows, {})

    def test_a_reply_to_something_else_is_left_alone(self):
        api = FakeApi()
        flows, _ = self._ask(api)
        recorder = Recorder()
        with patch.object(bot_listener, "handle_send_command", recorder):
            claimed = _as_admin(_reply(
                api, _message(ADMIN, text="не сюда", message_id=9, reply_to_message_id=12345),
                flows,
            ))
        self.assertFalse(claimed)
        self.assertFalse(recorder.ran)
        self.assertEqual(len(flows), 1, "an unrelated reply consumed the prompt")

    def test_somebody_else_cannot_answer_an_open_prompt(self):
        api = FakeApi()
        flows, _ = self._ask(api)
        prompt_id = api.sent[-1]["message_id"]
        recorder = Recorder()
        with patch.object(bot_listener, "handle_send_command", recorder):
            claimed = _as_admin(_reply(
                api, _message(STRANGER, text="я тоже хочу", message_id=9,
                              reply_to_message_id=prompt_id),
                flows,
            ))
        self.assertFalse(claimed)
        self.assertFalse(recorder.ran)

    def test_cancelling_runs_nothing(self):
        api = FakeApi()
        flows, _ = self._ask(api)
        prompt_id = api.sent[-1]["message_id"]
        recorder = Recorder()
        with patch.object(bot_listener, "handle_send_command", recorder):
            claimed = _as_admin(_reply(
                api, _message(ADMIN, text="отмена", message_id=9, reply_to_message_id=prompt_id),
                flows,
            ))
        self.assertTrue(claimed)
        self.assertFalse(recorder.ran)
        self.assertEqual(flows, {})

    def test_an_expired_prompt_is_not_answered(self):
        api = FakeApi()
        flows, _ = self._ask(api)
        prompt_id = api.sent[-1]["message_id"]
        for flow in flows.values():
            flow["created_at"] -= admin_menu.FLOW_TTL_SECONDS + 1
        recorder = Recorder()
        with patch.object(bot_listener, "handle_send_command", recorder):
            claimed = _as_admin(_reply(
                api, _message(ADMIN, text="Привет", message_id=9, reply_to_message_id=prompt_id),
                flows,
            ))
        self.assertFalse(claimed)
        self.assertFalse(recorder.ran)

    def test_every_ask_action_composes_its_own_command(self):
        for item in admin_menu.ACTIONS:
            if item["kind"] != "ask":
                continue
            with self.subTest(action=item["id"]):
                api = FakeApi()
                flows, _ = self._ask(api, item["id"])
                prompt_id = api.sent[-1]["message_id"]
                recorder = Recorder()
                with patch.object(bot_listener, HANDLER_FOR_ACTION[item["id"]], recorder):
                    _as_admin(_reply(
                        api, _message(ADMIN, text="аргумент", message_id=9,
                                      reply_to_message_id=prompt_id),
                        flows,
                    ))
                self.assertTrue(recorder.ran)
                composed = [a for a in recorder.calls[0][0] if isinstance(a, str)]
                self.assertIn(f"{item['command']} аргумент", composed)


if __name__ == "__main__":
    unittest.main()
