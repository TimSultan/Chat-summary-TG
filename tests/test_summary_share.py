"""The "Отправить мне тоже" receipt a group /summary leaves behind.

What matters: the receipt stays, a press puts the answer in the presser's DM exactly once,
the counter only counts people who really received it, somebody who never opened the DM
is routed through the bot's Start button, and the answer text is gone after a day.
"""

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_listener
import summary_share
from errors import ChatSummaryError

GROUP = -1001234567890
REQUESTER = 4242
READER = 555
BOT = "summarybot"
RECEIPT_ID = 901


class FakeApi:
    def __init__(self, closed_dms=()):
        self.closed_dms = set(closed_dms)
        self.sent = []        # (chat_id, text, kwargs)
        self.edits = []       # (chat_id, message_id, text, reply_markup)
        self.markups = []     # (chat_id, message_id, reply_markup)
        self.answers = []     # (callback_id, text, url)
        self.deleted = []

    async def send_message(self, chat_id, text, **kwargs):
        if chat_id in self.closed_dms:
            raise ChatSummaryError("Forbidden: bot can't initiate conversation with a user")
        self.sent.append((chat_id, text, kwargs))
        return {"message_id": RECEIPT_ID if chat_id == GROUP else 1000 + len(self.sent)}

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None, parse_mode=None):
        self.edits.append((chat_id, message_id, text, reply_markup))

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.markups.append((chat_id, message_id, reply_markup))

    async def answer_callback_query(self, callback_query_id, text=None, url=None):
        self.answers.append((callback_query_id, text, url))

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


def _run(coro):
    return asyncio.run(coro)


def _quiet(*_):
    pass


class ShareTestCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def _post_receipt(self, api):
        _run(bot_listener._post_summary_share_receipt(
            api, GROUP, 77, "Саммари по запросу Аня\n\n**Тема** — ответ", REQUESTER, log=_quiet,
        ))
        _, _, kwargs = api.sent[-1]
        data = kwargs["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        return data.split(":", 1)[1]

    def _press(self, api, token, user_id):
        callback = {
            "id": f"cb-{user_id}-{len(api.answers)}",
            "data": f"{summary_share.CALLBACK_PREFIX}:{token}",
            "from": {"id": user_id},
            "message": {"message_id": RECEIPT_ID, "chat": {"id": GROUP}},
        }
        _run(bot_listener.handle_summary_share_callback(api, callback, BOT, log=_quiet))


class ReceiptTests(ShareTestCase):
    def test_the_receipt_replies_to_the_request_with_a_zero_counter_and_is_never_swept(self):
        api = FakeApi()
        self._post_receipt(api)
        chat_id, text, kwargs = api.sent[0]
        self.assertEqual(chat_id, GROUP)
        self.assertEqual(text, "Саммари отправлено в личку бота (👀 0)")
        self.assertEqual(kwargs["reply_to_message_id"], 77)
        button = kwargs["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(button["text"], "Отправить мне тоже")
        self.assertEqual(api.deleted, [])

    def test_the_shared_text_says_whose_request_it_answers(self):
        text = bot_listener._shared_summary_text("Аня", "/summary@bot за 6 часов", "ответ")
        self.assertEqual(text, "Саммари по запросу Аня: «за 6 часов»\n\nответ")
        self.assertEqual(
            bot_listener._shared_summary_text("Аня", "/summary", "ответ"),
            "Саммари по запросу Аня\n\nответ",
        )


class PressTests(ShareTestCase):
    def test_a_press_sends_the_answer_to_the_dm_and_bumps_the_counter_by_edit(self):
        api = FakeApi()
        token = self._post_receipt(api)
        self._press(api, token, READER)

        dm = [(chat_id, text) for chat_id, text, _ in api.sent if chat_id == READER]
        self.assertEqual(len(dm), 1)
        self.assertIn("Саммари по запросу Аня", dm[0][1])
        self.assertIn("<b>Тема</b>", dm[0][1])
        self.assertEqual(api.answers[-1][1], "Отправил в личку.")
        self.assertEqual(api.edits, [(
            GROUP, RECEIPT_ID, "Саммари отправлено в личку бота (👀 1)",
            summary_share.receipt_keyboard(token),
        )])

    def test_a_second_press_by_the_same_person_sends_and_counts_nothing(self):
        api = FakeApi()
        token = self._post_receipt(api)
        self._press(api, token, READER)
        self._press(api, token, READER)
        self.assertEqual(sum(1 for chat_id, _, _ in api.sent if chat_id == READER), 1)
        self.assertEqual(len(api.edits), 1)
        self.assertEqual(api.answers[-1][1], "Уже лежит у тебя в личке.")

    def test_the_requester_already_has_it_and_is_not_counted(self):
        api = FakeApi()
        token = self._post_receipt(api)
        self._press(api, token, REQUESTER)
        self.assertEqual(api.edits, [])
        self.assertEqual(api.answers[-1][1], "Уже лежит у тебя в личке.")

    def test_each_new_reader_raises_the_counter(self):
        api = FakeApi()
        token = self._post_receipt(api)
        for reader in (501, 502, 503):
            self._press(api, token, reader)
        self.assertEqual(api.edits[-1][2], "Саммари отправлено в личку бота (👀 3)")


class ClosedDmTests(ShareTestCase):
    def test_a_closed_dm_opens_the_bot_and_start_delivers_and_counts(self):
        api = FakeApi(closed_dms={READER})
        token = self._post_receipt(api)
        self._press(api, token, READER)

        # Nothing delivered, nothing counted, and the press opens the bot's Start screen.
        self.assertEqual(api.edits, [])
        self.assertEqual(api.answers[-1][2], f"https://t.me/{BOT}?start=sum-{token}")

        api.closed_dms.clear()  # they pressed Start
        _run(bot_listener.handle_summary_share_start(
            api, {"chat": {"id": READER, "type": "private"}, "from": {"id": READER}},
            token, log=_quiet,
        ))
        self.assertEqual(sum(1 for chat_id, _, _ in api.sent if chat_id == READER), 1)
        self.assertEqual(api.edits[-1][2], "Саммари отправлено в личку бота (👀 1)")

    def test_the_start_payload_survives_telegrams_lowercasing(self):
        token = summary_share.create("x", REQUESTER)
        self.assertEqual(token, token.lower())


class GroupRequestTests(ShareTestCase):
    def test_a_group_summary_lands_in_the_dm_and_leaves_a_lasting_share_receipt(self):
        api = FakeApi()
        message = {
            "message_id": 77, "date": 1_770_000_000, "text": "/summary за 6 часов",
            "chat": {"id": GROUP, "type": "supergroup", "title": "Home chat"},
            "from": {"id": REQUESTER, "first_name": "Аня", "username": "anya"},
        }
        routed = {
            "start_date": datetime(2026, 2, 2).date(), "end_date": datetime(2026, 2, 2).date(),
            "lookback_hours": 6, "username": None, "cleaned_question": "сводка за 6 часов",
        }
        cfg = type("Cfg", (), {
            "openai_api_key": "k", "openai_routing_model": "m", "openai_model": "m",
        })()

        async def fake_fetch(**_):
            return "Home chat", []

        async def go():
            api.send_chat_action = lambda *a, **k: asyncio.sleep(0)
            background_tasks = set()
            with patch.object(bot_listener, "route_request", return_value=routed), \
                    patch.object(bot_listener, "fetch_range_messages_cached", fake_fetch), \
                    patch.object(bot_listener, "answer_request", return_value="**Тема** — ответ"), \
                    patch.object(bot_listener.history, "record"):
                await bot_listener.handle_bot_summary_request(
                    api, None, cfg, timezone.utc, BOT, message, background_tasks, None, log=_quiet,
                )
            while background_tasks:
                await asyncio.gather(*list(background_tasks))

        _run(go())
        self.assertIn("<b>Тема</b>", api.sent[0][1])
        self.assertEqual(api.sent[0][0], REQUESTER)
        self.assertEqual(api.sent[1][0], GROUP)
        self.assertEqual(api.sent[1][1], summary_share.receipt_text(0))
        self.assertEqual(api.deleted, [])


class ExpiryTests(ShareTestCase):
    def test_after_a_day_a_press_is_told_it_is_stale_and_the_button_goes(self):
        api = FakeApi()
        token = self._post_receipt(api)
        later = datetime.now(timezone.utc) + summary_share.SHARE_TTL + timedelta(minutes=1)
        with patch.object(summary_share, "datetime") as fake_datetime:
            fake_datetime.now.return_value = later
            fake_datetime.fromisoformat = datetime.fromisoformat
            self._press(api, token, READER)
        self.assertEqual(api.answers[-1][1], summary_share.STALE_TEXT)
        self.assertEqual(api.markups, [(GROUP, RECEIPT_ID, None)])
        self.assertFalse(any(chat_id == READER for chat_id, _, _ in api.sent))

    def test_old_answers_are_dropped_from_disk_on_the_next_write(self):
        start = datetime(2026, 10, 13, 9, tzinfo=timezone.utc)
        old = summary_share.create("старое", REQUESTER, now=start)
        fresh = summary_share.create("новое", REQUESTER, now=start + summary_share.SHARE_TTL)
        store = summary_share._load()
        self.assertNotIn(old, store)
        self.assertIn(fresh, store)
        self.assertNotIn("старое", summary_share._path().read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
