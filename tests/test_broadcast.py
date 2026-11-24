"""/broadcast -- anything through the bot, without the vote button (broadcast.py).

1. the button lines an admin types, parsed into link-button rows, and what is refused;
2. the draft: text (or pictures), then «кнопки или без», then the preview and the
   destinations -- the same flow as "/vote chat", only never carrying the vote button;
3. where it goes: the chat with the admin's own buttons, everybody's DM after one more
   question; a badge delegate (who may announce a vote) may not mail anything at all;
4. the ways in: /broadcast itself, «📬 Рассылка» in /admin, and «🛠 Админка» on the /vote
   panel and in an administrator's cabinet, which opens the panel as a new message.
"""

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import admin_menu
import bot_listener
import bot_users
import broadcast
import cabinet
import stats

DM = 555
MAIN_CHAT_ID = -1001234567890
EXTRA_CHAT = "@papkahudojnicov"
ADMIN = {"id": 42, "first_name": "Админ", "username": "admin"}
DELEGATE = {"id": 43, "first_name": "Делегат", "username": "delegate"}
OWNER = {"id": 44, "first_name": "Владелец", "username": "sultan_kembayev"}
BOT = "testbot"
CHAT = "Chat"
QUIET = lambda *_: None  # noqa: E731


def _cfg(extra=None):
    return SimpleNamespace(
        webapp_public_url="https://example.com", vote_announce_extra_chat=extra,
        vote_miniapp_short_name=None,
    )


class FakeApi:
    def __init__(self, admins=(ADMIN,)):
        self.sent = []
        self.edits = []
        self.answered = []
        self.admins = list(admins)
        self.admin_lookups = 0

    def _record(self, chat_id, **item):
        item.update(message_id=100 + len(self.sent), chat_id=chat_id)
        item.setdefault("reply_markup", None)
        self.sent.append(item)
        return item

    async def send_message(self, chat_id, text, reply_to_message_id=None, reply_markup=None,
                           parse_mode=None, entities=None, disable_notification=False):
        return self._record(chat_id, kind="text", text=text, entities=entities,
                            reply_markup=reply_markup, reply_to_message_id=reply_to_message_id)

    async def send_photo(self, chat_id, photo, caption, reply_to_message_id=None,
                         reply_markup=None, parse_mode=None, caption_entities=None):
        return self._record(chat_id, kind="photo", photo=photo, text=caption,
                            entities=caption_entities, reply_markup=reply_markup)

    async def send_media_group(self, chat_id, media):
        return [self._record(chat_id, kind="album", text=None,
                             photos=[item["media"] for item in media])]

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None, parse_mode=None):
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text,
                           "reply_markup": reply_markup})

    async def answer_callback_query(self, callback_id, text=None, url=None, show_alert=False):
        self.answered.append(text)

    async def get_chat_administrators(self, chat_id):
        self.admin_lookups += 1
        return [{"user": user, "status": "administrator"} for user in self.admins]

    def to(self, chat_id):
        return [item for item in self.sent if item["chat_id"] == chat_id]


def _buttons(markup):
    return [b for row in (markup or {}).get("inline_keyboard", []) for b in row]


def _reply(text=None, message_id=8, reply_to=7, user=ADMIN, **extra):
    message = {"message_id": message_id, "chat": {"id": DM, "type": "private"}, "from": user,
               "reply_to_message": {"message_id": reply_to}}
    if text is not None:
        message["text"] = text
    message.update(extra)
    return message


def _press(destination, flow_id="f1", user=ADMIN):
    return {"id": "cbq", "from": user, "message": {"message_id": 9},
            "data": bot_listener._vote_chat_dest_callback_data(destination, flow_id)}


def _draft(**extra):
    flow = {"chat_id": DM, "user_id": ADMIN["id"], "entry": CHAT, "admin_chat_id": MAIN_CHAT_ID,
            "system": bot_listener.BROADCAST_SYSTEM, "prompt_message_id": 7,
            "created_at": time.monotonic()}
    flow.update(extra)
    return flow


class _Registry:
    """An empty list of who has the bot started, in a temporary directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        for patcher in (
            patch.object(bot_users, "_dir", lambda: Path(self._tmp.name)),
            patch.dict(bot_users._REGISTRIES, clear=True),
            patch.object(bot_listener, "_BOT_USERS_JOBS", set()),
            patch.object(bot_users, "SEND_INTERVAL_SECONDS", 0),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def seed(self, outcomes):
        bot_users.registry().record_outcomes(outcomes)
        bot_users._REGISTRIES.clear()


async def _admin_gate(api, chat_id, user):
    """_is_chat_admin_or_privileged with the delegate on the wrong side of it."""
    return (user or {}).get("id") in (ADMIN["id"], OWNER["id"])


async def _manage_gate(api, chat_id, user, entry=None):
    """_can_manage_chat: badge delegates pass it, which is the point of these tests."""
    return (user or {}).get("id") in (ADMIN["id"], OWNER["id"], DELEGATE["id"])


def _gates():
    return (
        patch.object(bot_listener, "_is_chat_admin_or_privileged", _admin_gate),
        patch.object(bot_listener, "_can_manage_chat", _manage_gate),
    )


def _run(coro):
    first, second = _gates()
    with first, second:
        return asyncio.run(coro)


def _feed(api, flows, message, cfg=None):
    return _run(bot_listener.handle_vote_chat_text_input(
        api, cfg or _cfg(), message, flows, log=QUIET,
    ))


def _tap(api, flows, destination, user=ADMIN, cfg=None, background_tasks=None):
    async def go():
        await bot_listener.handle_vote_chat_destination_callback(
            api, cfg or _cfg(), _press(destination, user=user), flows, BOT, log=QUIET,
            background_tasks=background_tasks,
        )
        if background_tasks:
            await asyncio.gather(*background_tasks)

    return _run(go())


# ------------------------------------------------------------------- 1. button lines


class ParseButtonsTests(unittest.TestCase):
    def test_one_button_per_line(self):
        rows = broadcast.parse_buttons("Сайт - https://example.com\nКанал — https://t.me/channel")
        self.assertEqual(rows, [
            [{"text": "Сайт", "url": "https://example.com"}],
            [{"text": "Канал", "url": "https://t.me/channel"}],
        ])

    def test_a_bar_puts_buttons_side_by_side(self):
        rows = broadcast.parse_buttons("Сайт - https://a.ru | Чат - https://t.me/chat")
        self.assertEqual([[b["text"] for b in row] for row in rows], [["Сайт", "Чат"]])

    def test_a_username_and_a_bare_t_me_become_links(self):
        rows = broadcast.parse_buttons("Бот - @some_bot\nКанал t.me/papka")
        self.assertEqual([b["url"] for row in rows for b in row],
                         ["https://t.me/some_bot", "https://t.me/papka"])

    def test_a_dash_inside_the_text_stays_in_the_text(self):
        [[button]] = broadcast.parse_buttons("Итоги - неделя 5 - https://a.ru/x")
        self.assertEqual(button, {"text": "Итоги - неделя 5", "url": "https://a.ru/x"})

    def test_markdown_shaped_links_work_too(self):
        [[button]] = broadcast.parse_buttons("[Наш сайт](https://example.com)")
        self.assertEqual(button, {"text": "Наш сайт", "url": "https://example.com"})

    def test_blank_lines_are_skipped(self):
        self.assertEqual(len(broadcast.parse_buttons("\nА - https://a.ru\n\n")), 1)

    def test_refusals_name_the_line(self):
        cases = {
            "А - https://a.ru\nпросто текст": "Строка 2",
            "https://a.ru": "нет текста",
            "А - https://a.ru | Б - https://b.ru | В - https://c.ru | Г - https://d.ru": "не больше",
            "\n".join(f"К{i} - https://a.ru/{i}" for i in range(broadcast.MAX_ROWS + 1)): "рядов",
            ("Я" * (broadcast.MAX_BUTTON_TEXT + 1)) + " - https://a.ru": "длиннее",
            "  ": "ни одной",
        }
        for reply, words in cases.items():
            with self.subTest(reply=reply[:30]):
                with self.assertRaises(broadcast.ButtonsError) as caught:
                    broadcast.parse_buttons(reply)
                self.assertIn(words, str(caught.exception))

    def test_no_means_no_buttons(self):
        for word in ("нет", "Без кнопок", " - "):
            self.assertTrue(broadcast.wants_no_buttons(word), word)
        self.assertFalse(broadcast.wants_no_buttons("Сайт - https://a.ru"))


class StepsTests(unittest.TestCase):
    KEYBOARD = {"inline_keyboard": [[{"text": "Сайт", "url": "https://a.ru"}]]}

    def test_text_with_buttons_is_one_message(self):
        steps = bot_listener._announcement_steps(_draft(text="Привет"), self.KEYBOARD)
        self.assertEqual(steps, [("send_message", {"text": "Привет", "parse_mode": None,
                                                   "reply_markup": self.KEYBOARD})])

    def test_pictures_alone_are_just_the_album(self):
        steps = bot_listener._announcement_steps(_draft(text="", photos=["p1", "p2"]), None)
        self.assertEqual([method for method, _ in steps], ["send_media_group"])

    def test_pictures_with_buttons_get_a_line_to_hang_them_on(self):
        steps = bot_listener._announcement_steps(_draft(text="", photos=["p1", "p2"]), self.KEYBOARD)
        self.assertEqual([method for method, _ in steps], ["send_media_group", "send_message"])
        self.assertEqual(steps[1][1]["reply_markup"], self.KEYBOARD)
        self.assertNotIn("Голосуем", steps[1][1]["text"])

    def test_a_picture_with_no_caption_and_no_buttons_is_the_picture(self):
        steps = bot_listener._announcement_steps(_draft(text="", photos=["p1"]), None)
        self.assertEqual(steps, [("send_photo", {"photo": "p1", "caption": None, "reply_markup": None})])


# ------------------------------------------------------------------------- 2. the draft


class CommandTests(unittest.TestCase):
    def _command(self, user=ADMIN, entry=CHAT, admin_chat_id=MAIN_CHAT_ID):
        api, flows = FakeApi(), {}
        message = {"message_id": 5, "chat": {"id": DM, "type": "private"}, "from": user,
                   "text": "/broadcast"}
        with patch.object(bot_listener, "_resolve_chat_id", _answer(MAIN_CHAT_ID)):
            _run(bot_listener.handle_broadcast_command(
                api, None, message, entry, admin_chat_id, flows, log=QUIET,
            ))
        return api, flows

    def test_an_administrator_is_asked_for_the_text(self):
        api, flows = self._command()
        [flow] = flows.values()
        self.assertEqual(flow["system"], bot_listener.BROADCAST_SYSTEM)
        self.assertEqual(flow["admin_chat_id"], MAIN_CHAT_ID)
        self.assertTrue(api.sent[-1]["reply_markup"]["force_reply"])
        self.assertEqual(flow["prompt_message_id"], api.sent[-1]["message_id"])
        self.assertIn("Кнопку голосования не добавлю", api.sent[-1]["text"])

    def test_a_badge_delegate_is_refused(self):
        api, flows = self._command(user=DELEGATE)
        self.assertEqual(flows, {})
        self.assertIn("только администраторы", api.sent[-1]["text"])

    def test_it_needs_the_main_chat(self):
        api, flows = self._command(entry=None, admin_chat_id=None)
        self.assertEqual(flows, {})
        self.assertIn("не настроен", api.sent[-1]["text"])


def _answer(value):
    async def stub(*args, **kwargs):
        return value
    return stub


class DraftTests(_Registry, unittest.TestCase):
    def test_the_text_asks_about_buttons_before_any_preview(self):
        api, flows = FakeApi(), {"f1": _draft()}
        self.assertTrue(_feed(api, flows, _reply("Всем привет!")))
        [question] = api.sent
        self.assertIn("кнопки", question["text"])
        self.assertEqual(
            [b["callback_data"] for b in _buttons(question["reply_markup"])],
            [bot_listener._vote_chat_dest_callback_data(d, "f1") for d in ("buttons", "nobuttons", "cancel")],
        )
        self.assertEqual(flows["f1"]["text"], "Всем привет!")
        self.assertEqual(flows["f1"]["awaiting"], "choice")

    def test_no_vote_page_is_needed(self):
        api, flows = FakeApi(), {"f1": _draft()}
        _feed(api, flows, _reply("Привет"), cfg=SimpleNamespace(
            webapp_public_url=None, vote_announce_extra_chat=None, vote_miniapp_short_name=None,
        ))
        self.assertIn("f1", flows)
        self.assertIn("кнопки", api.sent[-1]["text"])

    def test_without_buttons_the_preview_has_none_and_no_vote_button(self):
        api, flows = FakeApi(), {"f1": _draft()}
        _feed(api, flows, _reply("Всем привет!"))
        _tap(api, flows, "nobuttons")
        preview, question = api.sent[-2:]
        self.assertEqual((preview["text"], preview["reply_markup"]), ("Всем привет!", None))
        self.assertIn("Куда отправить рассылку", question["text"])
        self.assertIn("📬 Всем в личку (0)", [b["text"] for b in _buttons(question["reply_markup"])])

    def test_typed_buttons_go_under_the_preview(self):
        api, flows = FakeApi(), {"f1": _draft()}
        _feed(api, flows, _reply("Всем привет!"))
        _tap(api, flows, "buttons")
        prompt = api.sent[-1]
        self.assertTrue(prompt["reply_markup"]["force_reply"])
        self.assertEqual(flows["f1"]["prompt_message_id"], prompt["message_id"])

        _feed(api, flows, _reply("Сайт - https://a.ru | Канал - @papka", message_id=20,
                                 reply_to=prompt["message_id"]))
        preview, question = api.sent[-2:]
        self.assertEqual(preview["text"], "Всем привет!")
        self.assertEqual(preview["reply_markup"], {"inline_keyboard": [[
            {"text": "Сайт", "url": "https://a.ru"}, {"text": "Канал", "url": "https://t.me/papka"},
        ]]})
        self.assertIn("Куда отправить рассылку", question["text"])
        self.assertNotIn("awaiting", flows["f1"])

    def test_a_bad_line_asks_again_and_keeps_the_text(self):
        api, flows = FakeApi(), {"f1": _draft()}
        _feed(api, flows, _reply("Всем привет!"))
        _tap(api, flows, "buttons")
        prompt_id = api.sent[-1]["message_id"]
        _feed(api, flows, _reply("просто слова", message_id=20, reply_to=prompt_id))
        again = api.sent[-1]
        self.assertIn("Строка 1", again["text"])
        self.assertTrue(again["reply_markup"]["force_reply"])
        self.assertEqual(flows["f1"]["prompt_message_id"], again["message_id"])
        self.assertEqual(flows["f1"]["text"], "Всем привет!")

        _feed(api, flows, _reply("нет", message_id=21, reply_to=again["message_id"]))
        preview = api.sent[-2]
        self.assertEqual((preview["text"], preview["reply_markup"]), ("Всем привет!", None))

    def test_a_second_press_does_not_draw_a_second_preview(self):
        api, flows = FakeApi(), {"f1": _draft()}
        _feed(api, flows, _reply("Привет"))
        _tap(api, flows, "nobuttons")
        count = len(api.sent)
        _tap(api, flows, "nobuttons")
        _tap(api, flows, "buttons")
        self.assertEqual(len(api.sent), count)

    def test_an_album_without_text_is_previewed_as_the_album(self):
        api, flows = FakeApi(), {"f1": _draft()}
        sizes = lambda name: [{"file_id": name + "-small"}, {"file_id": name}]  # noqa: E731
        message = _reply(photo=sizes("p1"))

        async def go():
            first, second = _gates()
            with first, second, patch.object(bot_listener, "ANNOUNCE_ALBUM_SETTLE_SECONDS", 0):
                tasks = set()
                await bot_listener.handle_vote_chat_text_input(
                    api, _cfg(), {**message, "media_group_id": "g"}, flows, log=QUIET,
                    background_tasks=tasks,
                )
                await bot_listener.handle_vote_chat_text_input(
                    api, _cfg(), {**_reply(message_id=9, photo=sizes("p2")), "media_group_id": "g"},
                    flows, log=QUIET, background_tasks=tasks,
                )
                await asyncio.gather(*tasks)
                await bot_listener.handle_vote_chat_destination_callback(
                    api, _cfg(), _press("nobuttons"), flows, BOT, log=QUIET,
                )

        asyncio.run(go())
        kinds = [item["kind"] for item in api.sent]
        self.assertEqual(kinds, ["text", "album", "text"])  # question, preview, destinations
        self.assertEqual(api.sent[1]["photos"], ["p1", "p2"])

    def test_cancel_drops_the_draft(self):
        api, flows = FakeApi(), {"f1": _draft()}
        _feed(api, flows, _reply("Привет"))
        _tap(api, flows, "cancel")
        self.assertEqual(flows, {})
        self.assertIn("Рассылка отменена", api.sent[-1]["text"])


# ------------------------------------------------------------------------ 3. destinations


class DestinationTests(_Registry, unittest.TestCase):
    KEYBOARD = {"inline_keyboard": [[{"text": "Сайт", "url": "https://a.ru"}]]}

    def test_the_chat_gets_the_admins_buttons_and_nothing_else(self):
        api, flows = FakeApi(), {"f1": _draft(text="Привет", keyboard=self.KEYBOARD)}
        _tap(api, flows, "main")
        [post] = api.to(MAIN_CHAT_ID)
        self.assertEqual((post["text"], post["reply_markup"]), ("Привет", self.KEYBOARD))
        self.assertIn("Рассылка отправлена", api.to(DM)[-1]["text"])
        self.assertEqual(flows, {})

    def test_no_buttons_means_no_keyboard_in_the_chat_either(self):
        api, flows = FakeApi(), {"f1": _draft(text="Привет", keyboard=None)}
        _tap(api, flows, "both", cfg=_cfg(extra=EXTRA_CHAT))
        for chat in (MAIN_CHAT_ID, EXTRA_CHAT):
            [post] = api.to(chat)
            self.assertIsNone(post["reply_markup"])

    def test_everybody_in_the_dm_after_one_more_question(self):
        self.seed({"1": "active", "2": "active", "3": "blocked"})
        api, flows = FakeApi(), {"f1": _draft(text="Привет", keyboard=self.KEYBOARD)}
        _tap(api, flows, "dm")
        ask = api.to(DM)[-1]
        self.assertIn("2 людям", ask["text"])
        self.assertNotIn("голосования", ask["text"])
        self.assertEqual(api.to(1), [])

        _tap(api, flows, "dmyes", background_tasks=set())
        for user_id in (1, 2):
            [message] = api.to(user_id)
            self.assertEqual((message["text"], message["reply_markup"]), ("Привет", self.KEYBOARD))
        self.assertEqual(api.to(3), [])
        self.assertIn("доставлено 2 из 2", api.edits[-1]["text"])

    def test_a_badge_delegate_cannot_send_a_broadcast(self):
        api, flows = FakeApi(), {"f1": _draft(text="Привет", user_id=DELEGATE["id"])}
        _tap(api, flows, "main", user=DELEGATE)
        self.assertEqual(api.to(MAIN_CHAT_ID), [])
        self.assertIn("только администраторы", api.to(DM)[-1]["text"])

    def test_but_may_still_announce_a_vote(self):
        flow = _draft(text="Голосуем", user_id=DELEGATE["id"])
        flow.pop("system")
        api, flows = FakeApi(), {"f1": flow}
        _tap(api, flows, "main", user=DELEGATE)
        [post] = api.to(MAIN_CHAT_ID)
        self.assertEqual(_buttons(post["reply_markup"])[0]["text"], bot_listener.VOTE_OPEN_BUTTON_TEXT)

    def test_an_expired_draft_names_the_command_to_start_again(self):
        api, flows = FakeApi(), {"f1": _draft(text="Привет", created_at=time.monotonic() - 10**6)}
        _tap(api, flows, "main")
        self.assertIn("/broadcast", api.answered[-1])


# ------------------------------------------------------------------------- 4. ways in


class WaysInTests(unittest.TestCase):
    def test_the_panel_has_the_broadcast(self):
        item = admin_menu.action("broadcast")
        self.assertEqual(item["label"], "📬 Рассылка")
        self.assertIn(admin_menu.callback_data("run", "broadcast"),
                      [b["callback_data"] for b in _buttons(admin_menu.menu_keyboard())])

    def test_the_entry_button_opens_the_panel_as_a_new_message(self):
        self.assertEqual(admin_menu.parse_callback(admin_menu.entry_button()["callback_data"]), ("open", ""))
        api = FakeApi()
        callback = {"id": "cb", "from": ADMIN, "data": admin_menu.entry_button()["callback_data"],
                    "message": {"message_id": 70, "chat": {"id": DM, "type": "private"}}}
        with patch.object(bot_listener, "_resolve_chat_id", _answer(MAIN_CHAT_ID)):
            _run(bot_listener.handle_admin_callback(
                api, None, SimpleNamespace(), None, callback, CHAT, {}, BOT, set(),
                {CHAT: MAIN_CHAT_ID}, {}, {}, {}, log=QUIET,
            ))
        self.assertEqual(api.edits, [], "the /vote panel it sat on was overwritten")
        [panel] = api.sent
        self.assertEqual(panel["reply_to_message_id"], 70)
        self.assertEqual(len(_buttons(panel["reply_markup"])), len(admin_menu.ACTIONS))

    def test_a_refused_entry_leaves_its_screen_alone(self):
        api = FakeApi()
        callback = {"id": "cb", "from": DELEGATE, "data": admin_menu.entry_button()["callback_data"],
                    "message": {"message_id": 70, "chat": {"id": DM, "type": "private"}}}
        with patch.object(bot_listener, "_resolve_chat_id", _answer(MAIN_CHAT_ID)):
            _run(bot_listener.handle_admin_callback(
                api, None, SimpleNamespace(), None, callback, CHAT, {}, BOT, set(),
                {CHAT: MAIN_CHAT_ID}, {}, {}, {}, log=QUIET,
            ))
        self.assertEqual(api.edits, [])
        self.assertIn("администратор", api.sent[-1]["text"])

    def test_the_vote_panel_carries_the_entry_button(self):
        api = FakeApi()
        message = {"message_id": 5, "chat": {"id": DM, "type": "private"}, "from": ADMIN, "text": "/vote"}
        with patch.object(bot_listener, "_resolve_chat_id", _answer(MAIN_CHAT_ID)), \
                patch.object(bot_listener.voting, "latest_poll", lambda entry: None):
            _run(bot_listener.handle_vote_command(
                api, None, _cfg(), None, message, CHAT, BOT, set(), log=QUIET, vote_chat_flows={},
            ))
        self.assertIn(admin_menu.entry_button(), _buttons(api.sent[-1]["reply_markup"]))


class CabinetTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(bot_listener._SEEN_CHAT_ADMIN_IDS, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _keyboard(self, **flags):
        user = stats.UserStats(user_id="42", username="admin", display_name="Админ")
        with patch("economy.balance", return_value=0), \
                patch("economy.active_title", return_value=None), \
                patch("economy.streak_freezes", return_value=0):
            _, keyboard = cabinet.main_view("chat", user, 0, 1, 1, 0, **flags)
        return keyboard

    def test_the_button_is_drawn_only_when_asked(self):
        self.assertIn(admin_menu.entry_button(), _buttons(self._keyboard(can_open_admin_panel=True)))
        self.assertNotIn(admin_menu.entry_button(), _buttons(self._keyboard()))

    def test_the_owner_always_sees_it(self):
        self.assertTrue(bot_listener._shows_admin_panel_button(OWNER["id"], "@Sultan_Kembayev"))

    def test_an_administrator_once_a_list_has_named_them(self):
        self.assertFalse(bot_listener._shows_admin_panel_button("42", "admin"))
        api = FakeApi(admins=(ADMIN,))
        self.assertTrue(asyncio.run(bot_listener._is_chat_admin(api, MAIN_CHAT_ID, ADMIN["id"])))
        self.assertTrue(bot_listener._shows_admin_panel_button("42", "admin"))
        self.assertFalse(bot_listener._shows_admin_panel_button("77", "someone"))

    def test_drawing_it_asks_telegram_nothing(self):
        api = FakeApi()
        bot_listener._shows_admin_panel_button("42", "admin")
        self.assertEqual(api.admin_lookups, 0)


class DispatchTests(unittest.TestCase):
    def test_broadcast_in_the_dm_reaches_its_handler(self):
        handled = []

        async def handle(api, telethon_client, message, entry, admin_chat_id, flows, log=print):
            handled.append((message["text"], entry, admin_chat_id))

        async def go():
            with patch.object(bot_listener, "handle_broadcast_command", handle), \
                    patch.object(bot_listener, "_note_bot_user", _answer(None)):
                for text in ("/broadcast", "/рассылка", "/broadcast@testbot"):
                    await bot_listener._dispatch_update(
                        {"update_id": 1, "message": {
                            "message_id": 1, "chat": {"id": DM, "type": "private"},
                            "from": ADMIN, "text": text,
                        }},
                        FakeApi(), None, SimpleNamespace(
                            listener_allowed_chats=[CHAT], stats_enabled=False, stats_top_limit=10,
                        ),
                        None, BOT, 1, set(), asyncio.Queue(), set(), CHAT,
                        {CHAT: MAIN_CHAT_ID}, {}, {}, {}, log=QUIET,
                    )

        asyncio.run(go())
        self.assertEqual([h[0] for h in handled], ["/broadcast", "/рассылка", "/broadcast@testbot"])
        self.assertEqual({h[1:] for h in handled}, {(CHAT, MAIN_CHAT_ID)})


if __name__ == "__main__":
    unittest.main()
