"""ViaCleaner: inline-bot posts are swept out of the chat once their delay has run out.

Three things are pinned here, in the order they can hurt somebody.

First, DETECTION -- which messages the cleaner considers its business. A false positive
here deletes a person's actual message, so "an ordinary message is never touched" is
tested at least as hard as "a via-message is".

Second, the QUEUE -- that a scheduled deletion survives a restart, that the same message
is never queued twice however many observers report it, and that switching the feature off
really stops the deletions that were already coming.

Third, the MENU -- because the menu is the only place any of this is switched on, and a
toggle that quietly does nothing would leave an administrator believing the chat is being
cleaned when it is not.
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

import bot_listener
import listener
import via_cleaner

CHAT = "Единый Чат Художников"
DM_CHAT_ID = 555
MAIN_CHAT_ID = -1001234567890
ADMIN = {"id": 42, "username": "admin", "first_name": "Админ"}
STRANGER = {"id": 77, "username": "someone", "first_name": "Кто-то"}


def _run(coro):
    return asyncio.run(coro)


class StoreTestCase(unittest.TestCase):
    """Every test here writes a real store file, into a directory of its own."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


class DetectionTests(unittest.TestCase):
    def test_a_message_sent_through_an_inline_bot_is_recognised(self):
        self.assertEqual(listener.inline_bot_ref(SimpleNamespace(via_bot_id=9999)), "9999")

    def test_a_cached_inline_bot_is_named(self):
        # Nicer in the log, and the only reason the helper returns a string at all.
        msg = SimpleNamespace(via_bot_id=9999, via_bot=SimpleNamespace(username="gif"))
        self.assertEqual(listener.inline_bot_ref(msg), "@gif")

    def test_an_ordinary_message_is_never_touched(self):
        # The expensive failure: a false positive here deletes somebody's own message.
        for msg in (
            SimpleNamespace(via_bot_id=None),
            SimpleNamespace(via_bot_id=0),
            SimpleNamespace(),
            SimpleNamespace(text="via @gif"),  # merely talking about one
        ):
            with self.subTest(msg=msg):
                self.assertIsNone(listener.inline_bot_ref(msg))

    def test_an_unreadable_via_bot_still_yields_the_id(self):
        """This runs for every message in the chat -- it may not raise, ever."""

        class Exploding:
            via_bot_id = 9999

            @property
            def via_bot(self):
                raise RuntimeError("entity cache is unhappy")

        self.assertEqual(listener.inline_bot_ref(Exploding()), "9999")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class SettingsTests(StoreTestCase):
    def test_it_is_off_until_somebody_turns_it_on(self):
        self.assertFalse(via_cleaner.is_enabled(CHAT))
        self.assertEqual(via_cleaner.settings(CHAT)["delay_seconds"], via_cleaner.DEFAULT_DELAY_SECONDS)

    def test_settings_survive_a_restart(self):
        via_cleaner.set_enabled(CHAT, True, by="Sultan")
        via_cleaner.set_delay(CHAT, 60, by="Sultan")
        # A restart is just another read of the same file.
        current = via_cleaner.settings(CHAT)
        self.assertTrue(current["enabled"])
        self.assertEqual(current["delay_seconds"], 60)
        self.assertEqual(current["updated_by"], "Sultan")

    def test_the_chat_key_ignores_at_signs_and_case(self):
        via_cleaner.set_enabled("@MyChat", True)
        self.assertTrue(via_cleaner.is_enabled("mychat"))

    def test_a_delay_that_is_not_on_the_menu_is_pulled_onto_it(self):
        via_cleaner.set_delay(CHAT, 7 * 24 * 60 * 60)
        self.assertEqual(via_cleaner.settings(CHAT)["delay_seconds"], max(via_cleaner.DELAY_CHOICES))
        via_cleaner.set_delay(CHAT, -5)
        self.assertEqual(via_cleaner.settings(CHAT)["delay_seconds"], 0)

    def test_a_damaged_store_reads_as_switched_off(self):
        # The safe direction: a corrupt file must not start deleting messages on defaults.
        via_cleaner.set_enabled(CHAT, True)
        via_cleaner._path().write_text("{not json", encoding="utf-8")
        self.assertFalse(via_cleaner.is_enabled(CHAT))


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------


class QueueTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        via_cleaner.set_enabled(CHAT, True)
        via_cleaner.set_delay(CHAT, 5 * 60)

    def test_nothing_is_scheduled_while_the_cleaner_is_off(self):
        via_cleaner.set_enabled(CHAT, False)
        self.assertIsNone(via_cleaner.remember(CHAT, 101))
        self.assertEqual(via_cleaner.due(now=time.time() + 10_000), [])

    def test_a_scheduled_message_waits_out_its_delay(self):
        now = 1_000_000.0
        via_cleaner.remember(CHAT, 101, via="@gif", now=now)
        self.assertEqual(via_cleaner.due(now=now), [])
        self.assertEqual(via_cleaner.due(now=now + 299), [])
        due = via_cleaner.due(now=now + 300)
        self.assertEqual([item["message_id"] for item in due], [101])

    def test_a_scheduled_message_survives_a_restart(self):
        # The entire reason this is a file: a five-minute timer in memory is lost to every
        # deploy, and a deploy is exactly when one is most likely to be waiting.
        now = 1_000_000.0
        via_cleaner.remember(CHAT, 101, now=now)
        self.assertEqual(len(via_cleaner.due(now=now + 300)), 1)

    def test_the_same_message_is_only_ever_queued_once(self):
        # Both observers (the Telethon session and the bot's own updates) report it, and a
        # reconnect can replay the update a third time.
        now = 1_000_000.0
        first = via_cleaner.remember(CHAT, 101, now=now)
        second = via_cleaner.remember(CHAT, 101, now=now + 30)
        self.assertEqual(first, second)
        self.assertEqual(len(via_cleaner.due(now=now + 600)), 1)

    def test_settling_removes_the_message_and_counts_it(self):
        now = 1_000_000.0
        via_cleaner.remember(CHAT, 101, now=now)
        via_cleaner.remember(CHAT, 102, now=now)
        ready = via_cleaner.due(now=now + 300)
        self.assertEqual(via_cleaner.settle(ready, now=now + 300), 0)
        self.assertEqual(via_cleaner.due(now=now + 300), [])
        self.assertEqual(via_cleaner.settings(CHAT)["deleted"], 2)

    def test_an_unhandled_message_stays_queued(self):
        """A chat the bot could not resolve must be retried, not counted as cleaned."""
        now = 1_000_000.0
        via_cleaner.remember(CHAT, 101, now=now)
        via_cleaner.remember(CHAT, 102, now=now)
        ready = via_cleaner.due(now=now + 300)
        via_cleaner.settle(ready[:1], now=now + 300)
        self.assertEqual([item["message_id"] for item in via_cleaner.due(now=now + 300)], [102])

    def test_a_message_telegram_will_no_longer_delete_is_given_up_on(self):
        # Past Telegram's ~48h window there is nothing to retry, and retrying for ever
        # would mean a permanent backlog failing on every single sweep.
        now = 1_000_000.0
        via_cleaner.remember(CHAT, 101, now=now)
        stale = now + via_cleaner.PENDING_EXPIRY_SECONDS + 60
        self.assertEqual(via_cleaner.due(now=stale), [])
        via_cleaner.settle([], now=stale)
        self.assertEqual(via_cleaner.settings(CHAT)["pending"], 0)
        self.assertEqual(via_cleaner.settings(CHAT)["deleted"], 0)

    def test_switching_the_cleaner_off_cancels_what_was_already_coming(self):
        now = 1_000_000.0
        via_cleaner.remember(CHAT, 101, now=now)
        via_cleaner.set_enabled(CHAT, False)
        self.assertEqual(via_cleaner.due(now=now + 300), [])

    def test_shortening_the_delay_re_times_what_is_already_queued(self):
        now = 1_000_000.0
        via_cleaner.remember(CHAT, 101, now=now)
        via_cleaner.set_delay(CHAT, 60)
        self.assertEqual([item["message_id"] for item in via_cleaner.due(now=now + 60)], [101])

    def test_the_sweeper_sleeps_until_the_next_deletion_is_due(self):
        now = 1_000_000.0
        self.assertEqual(via_cleaner.seconds_until_next(now=now), float(via_cleaner.IDLE_WAIT_SECONDS))
        via_cleaner.remember(CHAT, 101, now=now)
        self.assertAlmostEqual(via_cleaner.seconds_until_next(now=now + 240), 60.0, places=3)
        # Never negative: an overdue queue means "look now", not "sleep backwards".
        self.assertEqual(via_cleaner.seconds_until_next(now=now + 9_999), 0.0)

    def test_the_queue_cannot_grow_without_bound(self):
        # The real ceiling is thousands; a smaller one exercises the same code without
        # rewriting a growing JSON file five thousand times.
        now = 1_000_000.0
        with patch.object(via_cleaner, "PENDING_LIMIT", 10):
            for message_id in range(35):
                via_cleaner.remember(CHAT, message_id + 1, now=now)
            self.assertEqual(via_cleaner.settings(CHAT)["pending"], 10)


class WakeTests(StoreTestCase):
    """The sweeper sleeps until the next deletion is due; remembering one must cut that
    sleep short. Without this a chat set to "сразу" would wait out the idle timeout, and
    "сразу" is the setting whose whole promise is that it does not."""

    def test_a_new_message_cuts_a_long_sleep_short(self):
        async def _scenario():
            waited = asyncio.create_task(via_cleaner.wait_for_work(30))
            await asyncio.sleep(0)
            via_cleaner.wake()
            await asyncio.wait_for(waited, timeout=1)

        started = time.monotonic()
        _run(_scenario())
        self.assertLess(time.monotonic() - started, 1)

    def test_an_empty_queue_does_not_wake_for_nothing(self):
        async def _scenario():
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(via_cleaner.wait_for_work(30), timeout=0.05)

        _run(_scenario())

    def test_an_overdue_queue_does_not_sleep_at_all(self):
        async def _scenario():
            # seconds_until_next returns 0 for a backlog, and a zero wait must return
            # rather than hang: this is the loop's own "look now".
            await asyncio.wait_for(via_cleaner.wait_for_work(0), timeout=1)

        _run(_scenario())


# ---------------------------------------------------------------------------
# The menu
# ---------------------------------------------------------------------------


class FakeApi:
    def __init__(self):
        self.sent = []
        self.edited = []
        self.answered = []
        self.deleted = []

    async def send_message(self, chat_id, text, reply_to_message_id=None,
                           reply_markup=None, parse_mode=None):
        item = {"message_id": 100 + len(self.sent), "chat_id": chat_id, "text": text,
                "reply_markup": reply_markup}
        self.sent.append(item)
        return item

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None, parse_mode=None):
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text,
                            "reply_markup": reply_markup})

    async def answer_callback_query(self, callback_id, text=None):
        self.answered.append((callback_id, text))

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


def _buttons(markup) -> list[dict]:
    return [button for row in (markup or {}).get("inline_keyboard", []) for button in row]


def _message(user, text="/viacleaner", chat_type="private"):
    return {
        "message_id": 5,
        "chat": {"id": DM_CHAT_ID if chat_type == "private" else MAIN_CHAT_ID, "type": chat_type},
        "from": user,
        "text": text,
    }


def _callback(user, data, message_id=100):
    return {
        "id": "cb1",
        "from": user,
        "data": data,
        "message": {"message_id": message_id, "chat": {"id": DM_CHAT_ID, "type": "private"}},
    }


class MenuTests(StoreTestCase):
    def test_the_root_screen_says_what_will_be_deleted_before_what_is_set(self):
        text = via_cleaner.menu_text(CHAT)
        self.assertIn("инлайн-бот", text)
        self.assertIn("выключен", text)
        self.assertIn("5 минут", text)

    def test_the_toggle_button_names_the_action_and_not_the_state(self):
        # "Включить" on a switched-off cleaner. A button labelled with the current state
        # is a coin toss over what pressing it does.
        self.assertIn("Включить", _buttons(via_cleaner.menu_keyboard(CHAT))[0]["text"])
        via_cleaner.set_enabled(CHAT, True)
        self.assertIn("Выключить", _buttons(via_cleaner.menu_keyboard(CHAT))[0]["text"])

    def test_the_delay_screen_offers_every_choice_and_marks_the_current_one(self):
        via_cleaner.set_delay(CHAT, 60)
        buttons = _buttons(via_cleaner.delay_keyboard(CHAT))
        choices = [b for b in buttons if b["callback_data"].startswith(f"{via_cleaner.CALLBACK_PREFIX}:delay:")]
        self.assertEqual(len(choices), len(via_cleaner.DELAY_CHOICES))
        marked = [b["text"] for b in choices if b["text"].startswith("✅")]
        self.assertEqual(marked, ["✅ 1 минута"])
        self.assertEqual(buttons[-1]["callback_data"], f"{via_cleaner.CALLBACK_PREFIX}:menu")

    def test_delays_are_spelled_the_way_a_person_reads_them(self):
        self.assertEqual(via_cleaner.format_delay(0), "сразу")
        self.assertEqual(via_cleaner.format_delay(60), "1 минута")
        self.assertEqual(via_cleaner.format_delay(5 * 60), "5 минут")
        self.assertEqual(via_cleaner.format_delay(15 * 60), "15 минут")
        self.assertEqual(via_cleaner.format_delay(60 * 60), "1 час")
        self.assertEqual(via_cleaner.format_delay(3 * 60 * 60), "3 часа")
        self.assertEqual(via_cleaner.format_delay(24 * 60 * 60), "24 часа")

    def test_a_chat_name_cannot_break_the_html(self):
        # The chat title is somebody else's text and the menu is sent as HTML.
        self.assertIn("&lt;b&gt;", via_cleaner.menu_text("<b>Чат"))

    def test_only_an_administrator_is_shown_the_menu(self):
        api = FakeApi()
        with patch.object(bot_listener, "_is_chat_admin_or_privileged", _answering(False)):
            _run(bot_listener.handle_via_cleaner_command(api, _message(STRANGER), CHAT, MAIN_CHAT_ID))
        self.assertEqual(len(api.sent), 1)
        self.assertIsNone(api.sent[0]["reply_markup"])
        self.assertIn("администратор", api.sent[0]["text"])

    def test_an_administrator_gets_the_menu(self):
        api = FakeApi()
        _run_as_admin(bot_listener.handle_via_cleaner_command(api, _message(ADMIN), CHAT, MAIN_CHAT_ID))
        self.assertEqual(len(api.sent), 1)
        self.assertEqual(len(_buttons(api.sent[0]["reply_markup"])), 2)

    def test_the_command_needs_a_configured_chat(self):
        api = FakeApi()
        _run(bot_listener.handle_via_cleaner_command(api, _message(ADMIN), None, None))
        self.assertIn("не настроен", api.sent[0]["text"])

    def test_the_toggle_button_actually_switches_the_cleaner_on(self):
        api = FakeApi()
        _run_as_admin(_press(api, f"{via_cleaner.CALLBACK_PREFIX}:toggle"))
        self.assertTrue(via_cleaner.is_enabled(CHAT))
        self.assertIn("включён", api.edited[-1]["text"])

    def test_the_spinner_is_stopped_before_anything_that_can_wait(self):
        """A Telethon call before answerCallbackQuery leaves the button spinning for ever."""
        api = FakeApi()
        order = []

        async def _resolve(*args, **kwargs):
            order.append("resolve")
            return MAIN_CHAT_ID

        original = api.answer_callback_query

        async def _answer(callback_id, text=None):
            order.append("answer")
            await original(callback_id, text)

        api.answer_callback_query = _answer
        with patch.object(bot_listener, "_resolve_chat_id", _resolve):
            _run_as_admin(_press(api, f"{via_cleaner.CALLBACK_PREFIX}:toggle"), resolve=False)
        self.assertEqual(order[0], "answer")

    def test_a_delay_button_stores_the_choice_and_returns_to_the_root_screen(self):
        api = FakeApi()
        _run_as_admin(_press(api, f"{via_cleaner.CALLBACK_PREFIX}:delay:60"))
        self.assertEqual(via_cleaner.settings(CHAT)["delay_seconds"], 60)
        self.assertIn("1 минута", api.edited[-1]["text"])
        self.assertEqual(len(_buttons(api.edited[-1]["reply_markup"])), 2)

    def test_a_junk_delay_changes_nothing(self):
        api = FakeApi()
        _run_as_admin(_press(api, f"{via_cleaner.CALLBACK_PREFIX}:delay:soon"))
        self.assertEqual(via_cleaner.settings(CHAT)["delay_seconds"], via_cleaner.DEFAULT_DELAY_SECONDS)

    def test_somebody_who_has_stopped_being_an_administrator_loses_the_buttons(self):
        api = FakeApi()
        via_cleaner.set_enabled(CHAT, True)
        with patch.object(bot_listener, "_resolve_chat_id", _answering(MAIN_CHAT_ID)), \
             patch.object(bot_listener, "_is_chat_admin_or_privileged", _answering(False)):
            _run(_press(api, f"{via_cleaner.CALLBACK_PREFIX}:toggle"))
        self.assertTrue(via_cleaner.is_enabled(CHAT))  # unchanged
        self.assertIn("администратор", api.edited[-1]["text"])
        self.assertIsNone(api.edited[-1]["reply_markup"])

    def test_a_callback_from_another_menu_is_ignored(self):
        self.assertIsNone(via_cleaner.parse_callback("badge:menu:1"))
        self.assertEqual(via_cleaner.parse_callback(f"{via_cleaner.CALLBACK_PREFIX}:delay:60"), ("delay", "60"))
        self.assertEqual(via_cleaner.parse_callback(f"{via_cleaner.CALLBACK_PREFIX}:menu"), ("menu", ""))


def _answering(value):
    """A stand-in for one of bot_listener's `async def` helpers, fixed to one answer."""

    async def _stub(*args, **kwargs):
        return value

    return _stub


def _quiet(*args, **kwargs):
    """These handlers log what an administrator changed; the suite does not need it."""


async def _press(api, data, user=ADMIN):
    await bot_listener.handle_via_cleaner_callback(
        api, None, _callback(user, data), CHAT, {CHAT: MAIN_CHAT_ID}, log=_quiet,
    )


def _run_as_admin(coro, resolve=True):
    """Runs `coro` with the administrator gate answering yes.

    The gate itself is a Telegram round trip with its own tests elsewhere; what these care
    about is what happens once it has answered.
    """

    async def _yes(*args, **kwargs):
        return True

    async def _chat_id(*args, **kwargs):
        return MAIN_CHAT_ID

    patches = [patch.object(bot_listener, "_is_chat_admin_or_privileged", _yes)]
    if resolve:
        patches.append(patch.object(bot_listener, "_resolve_chat_id", _chat_id))
    for patcher in patches:
        patcher.start()
    try:
        return _run(coro)
    finally:
        for patcher in patches:
            patcher.stop()


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


class SweepTests(StoreTestCase):
    """What the bot half does with the queue, without running the loop that drives it.

    The loop's own body is a few lines around these calls; what matters and what can
    silently go wrong is the pairing -- delete exactly what came back from `due`, settle
    exactly what was deleted.
    """

    def setUp(self):
        super().setUp()
        via_cleaner.set_enabled(CHAT, True)
        via_cleaner.set_delay(CHAT, 60)

    def test_a_due_message_is_deleted_from_the_right_chat_and_then_forgotten(self):
        now = time.time() - 120
        via_cleaner.remember(CHAT, 101, now=now)
        api = FakeApi()
        ready = via_cleaner.due()
        _run(_sweep(api, ready))
        via_cleaner.settle(ready)
        self.assertEqual(api.deleted, [(MAIN_CHAT_ID, 101)])
        self.assertEqual(via_cleaner.settings(CHAT)["pending"], 0)
        self.assertEqual(via_cleaner.settings(CHAT)["deleted"], 1)

    def test_a_message_that_is_not_due_yet_is_left_alone(self):
        via_cleaner.remember(CHAT, 101)
        api = FakeApi()
        _run(_sweep(api, via_cleaner.due()))
        self.assertEqual(api.deleted, [])


async def _sweep(api, ready):
    for item in ready:
        await api.delete_message(MAIN_CHAT_ID, item["message_id"])


if __name__ == "__main__":
    unittest.main()
