"""The date picker in front of "/vote собрать": which days a collect reads.

"Собрать все заявки" no longer runs on the press. It asks for a first and a last day (or a
ready-made period), says what the collect will do -- including how many works already
collected fall outside the dates and will leave the vote -- and only then scans. The poll
then holds exactly the works posted in those days. "Добавить новые" is untouched.
"""

import asyncio
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_listener
import voting
from tests.async_case import AsyncTestCase

CHAT = "Chat"
THIS_WEEK = "2026-W41"
ADMIN = {"id": 7, "username": "admin"}
STRANGER = {"id": 8, "username": "someone"}
DM = 5
TODAY = date(2026, 10, 5)  # a Monday


def _cfg():
    return SimpleNamespace(webapp_public_url="https://example.com",
                           vote_miniapp_short_name=None, vote_announce_extra_chat=None)


class FakeApi:
    def __init__(self):
        self.sent = []      # (text, reply_markup)
        self.edits = []     # (message_id, text, reply_markup)
        self.answered = []  # (callback id, text)

    async def send_message(self, chat_id, text, reply_to_message_id=None, reply_markup=None,
                           parse_mode=None, disable_notification=False):
        self.sent.append((text, reply_markup))
        return {"message_id": 100 + len(self.sent)}

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None, parse_mode=None):
        self.edits.append((message_id, text, reply_markup))

    async def answer_callback_query(self, callback_id, text=None):
        self.answered.append((callback_id, text))


def _buttons(markup):
    return [b for row in (markup or {}).get("inline_keyboard", []) for b in row]


def _by_text(markup):
    return {b["text"]: b["callback_data"] for b in _buttons(markup)}


def _entry(entry_id, posted: date | None = None):
    return voting.Entry(
        entry_id=entry_id, message_id=int(entry_id), author_id=int(entry_id),
        author_name=f"Автор {entry_id}", author_username=None, text="", media=[f"{entry_id}.jpg"],
        posted_at=datetime.combine(posted, datetime.min.time(), tzinfo=timezone.utc).replace(hour=12).isoformat()
        if posted else "",
    )


class ParseTypedRangeTests(unittest.TestCase):
    def parse(self, text, today=TODAY):
        return bot_listener._parse_vote_collect_range(text, today)

    def test_every_way_of_writing_two_dates(self):
        expected = (date(2026, 9, 28), date(2026, 10, 5))
        for text in ("28.09 05.10", "28.09-05.10", "28.09 по 05.10", "28.09.2026 – 05.10.2026",
                     "2026-09-28 2026-10-05", "28.09.26 5.10.26"):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text), expected)

    def test_one_date_means_from_then_until_today(self):
        self.assertEqual(self.parse("01.10"), (date(2026, 10, 1), TODAY))

    def test_a_day_without_a_year_is_the_latest_one_not_in_the_future(self):
        self.assertEqual(self.parse("28.12", today=date(2027, 1, 4)),
                         (date(2026, 12, 28), date(2027, 1, 4)))

    def test_what_cannot_be_collected_is_refused_with_the_reason(self):
        for text, reason in (
            ("05.10 28.09", "позже"),
            ("2026-10-01 2026-10-09", "не наступил"),
            ("01.08 05.10", "длинный"),
            ("31.02 05.10", "Такой даты нет"),
            ("что-нибудь", "Не понял"),
            ("01.10 02.10 03.10", "Не понял"),
        ):
            with self.subTest(text=text):
                self.assertIn(reason, self.parse(text))


class PickerScreenTests(unittest.TestCase):
    def _first_days(self, markup):
        picks = [b for b in _buttons(markup) if b["callback_data"].startswith("votedate:f:")]
        return [datetime.strptime(b["callback_data"].split(":")[2], "%Y%m%d").date() for b in picks]

    def test_step_one_offers_no_day_that_has_not_come_yet(self):
        mid_month = date(2026, 10, 20)
        _, markup = bot_listener._vote_date_picker(mid_month, ADMIN["id"])
        days = self._first_days(markup)
        self.assertEqual((min(days), max(days)), (date(2026, 10, 1), mid_month))
        texts = [b["text"] for b in _buttons(markup)]
        self.assertNotIn("›", texts)  # nothing to page forward to
        self.assertIn("‹", texts)

    def test_early_in_a_month_step_one_opens_on_last_weeks_monday(self):
        """On 5 October the usual first day, last week's Monday, is 28 September: the
        picker opens there instead of making every collect start with a page back."""
        _, markup = bot_listener._vote_date_picker(TODAY, ADMIN["id"])
        days = self._first_days(markup)
        self.assertIn(date(2026, 9, 28), days)
        self.assertEqual(max(days), date(2026, 9, 30))
        forward = _by_text(markup)["›"]
        self.assertEqual(forward, f"votedate:fm:202610:-:{ADMIN['id']}")

    def test_the_ready_made_periods_end_today(self):
        _, markup = bot_listener._vote_date_picker(TODAY, ADMIN["id"])
        buttons = _by_text(markup)
        uid = ADMIN["id"]
        self.assertEqual(buttons["Эта неделя"], f"votedate:t:20261005:20261005:{uid}")
        self.assertEqual(buttons["Прошлая и эта"], f"votedate:t:20260928:20261005:{uid}")
        self.assertEqual(buttons["7 дней"], f"votedate:t:20260929:20261005:{uid}")
        self.assertEqual(buttons["14 дней"], f"votedate:t:20260922:20261005:{uid}")

    def test_step_two_starts_at_the_chosen_day_and_marks_it(self):
        text, markup = bot_listener._vote_date_picker(TODAY, ADMIN["id"], since=date(2026, 9, 28))
        self.assertIn("С 28.09", text)
        picks = [b for b in _buttons(markup) if b["callback_data"].startswith("votedate:t:")]
        ends = {b["callback_data"].split(":")[3] for b in picks}
        # Opens on the month the last day most likely is in -- today's -- not on the
        # first day's, which would cost a page-forward on every collect across a month end.
        self.assertEqual((min(ends), max(ends)), ("20261001", "20261005"))
        self.assertIn("По сегодня (05.10)", _by_text(markup))
        september = bot_listener._vote_date_picker(TODAY, ADMIN["id"], since=date(2026, 9, 28),
                                                   month=date(2026, 9, 1))[1]
        texts = [b["text"] for b in _buttons(september)]
        self.assertIn("[28]", texts)
        self.assertNotIn("27", texts)  # before the first day: shown as "·", not offered

    def test_step_two_never_offers_a_span_past_the_cap(self):
        since = TODAY - timedelta(days=60)
        _, markup = bot_listener._vote_date_picker(TODAY, ADMIN["id"], since=since)
        ends = [datetime.strptime(b["callback_data"].split(":")[3], "%Y%m%d").date()
                for b in _buttons(markup) if b["callback_data"].startswith("votedate:t:")]
        self.assertEqual(max(ends), since + timedelta(days=bot_listener.VOTE_COLLECT_MAX_DAYS - 1))
        self.assertNotIn("По сегодня (05.10)", _by_text(markup))

    def test_every_button_fits_telegrams_limit_and_belongs_to_the_admin(self):
        screens = [bot_listener._vote_date_picker(TODAY, 1234567890),
                   bot_listener._vote_date_picker(TODAY, 1234567890, since=date(2026, 9, 28))]
        for _, markup in screens:
            for button in _buttons(markup):
                self.assertLessEqual(len(button["callback_data"].encode()), 64)
                self.assertTrue(button["text"])
                self.assertEqual(bot_listener._parse_vote_date_callback(button["callback_data"])[3], 1234567890)


class _Storage(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("voting._voting_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)
        self.scans = []

    def _seed(self):
        """This week's poll: works posted 20.09 (admitted, voted for), 29.09 (admitted) and
        03.10, plus one whose posting time was never recorded."""
        poll = voting.Poll(poll_id=THIS_WEEK, entry=CHAT, created_at="2026-10-01T00:00:00+00:00",
                           entries=[_entry("1", date(2026, 9, 20)), _entry("2", date(2026, 9, 29)),
                                    _entry("3", date(2026, 10, 3)), _entry("4")])
        voting.set_approved(poll, ["1", "2"])
        voting.record_vote(poll, 42, ["1", "2"])
        voting.save_poll(poll)

    def _patches(self, new_entries=()):
        async def collect_entries(**kwargs):
            self.scans.append(kwargs)
            return list(new_entries)

        async def resolve(*args, **kwargs):
            return -100

        async def can_manage(api, chat_id, user, entry=None):
            return user.get("id") == ADMIN["id"]

        class _Today(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 5, 15, 0, tzinfo=tz or timezone.utc)

        return [
            patch.object(bot_listener, "_resolve_chat_id", resolve),
            patch.object(bot_listener, "_can_manage_chat", can_manage),
            patch.object(voting, "collect_entries", collect_entries),
            patch.object(bot_listener, "_current_vote_poll_id", lambda tz: THIS_WEEK),
            patch.object(bot_listener, "datetime", _Today),
        ]

    def _run(self, coroutine_factory, new_entries=()):
        async def scenario():
            tasks = set()
            patches = self._patches(new_entries)
            for p in patches:
                p.start()
            try:
                await coroutine_factory(tasks)
                while tasks:
                    await asyncio.gather(*list(tasks))
            finally:
                for p in patches:
                    p.stop()
        asyncio.run(scenario())

    def _type(self, text, user=ADMIN, chat_type="private", new_entries=()):
        api = FakeApi()
        message = {"message_id": 1, "chat": {"id": DM, "type": chat_type}, "from": user, "text": text}
        self._run(lambda tasks: bot_listener.handle_vote_command(
            api, None, _cfg(), timezone.utc, message, CHAT, "testbot", tasks, log=lambda *_: None,
        ), new_entries)
        return api

    def _tap(self, data, user=ADMIN, new_entries=()):
        api = FakeApi()
        callback = {"id": "cbq", "from": user, "data": data,
                    "message": {"message_id": 55, "chat": {"id": DM, "type": "private"}}}
        self._run(lambda tasks: bot_listener.handle_vote_date_callback(
            api, None, _cfg(), timezone.utc, callback, CHAT, "testbot", tasks, {}, log=lambda *_: None,
        ), new_entries)
        return api


class CommandTests(_Storage):
    def test_collect_without_dates_opens_the_picker_and_scans_nothing(self):
        api = self._type("/vote собрать")
        text, markup = api.sent[0]
        self.assertIn("с какого дня", text)
        self.assertIn("Прошлая и эта", _by_text(markup))
        self.assertEqual(self.scans, [])
        self.assertIsNone(voting.load_poll(CHAT, THIS_WEEK))

    def test_the_panels_collect_button_reaches_the_picker(self):
        """The status panel's button replays "/vote собрать" -- which is now the picker."""
        self.assertEqual(bot_listener.VOTE_ACTIONS["collect"], "/vote собрать")

    def test_only_an_administrator_gets_the_picker(self):
        api = self._type("/vote собрать", user=STRANGER)
        self.assertIn("администратор", api.sent[0][0])
        self.assertIsNone(api.sent[0][1])

    def test_typed_dates_skip_the_picker(self):
        self._type("/vote собрать 28.09 05.10")
        (scan,) = self.scans
        self.assertEqual(scan["since"].date(), date(2026, 9, 28))
        self.assertEqual(scan["until"].date(), date(2026, 10, 6))
        self.assertIs(scan["stop_at_known"], False)

    def test_bad_typed_dates_are_refused_with_how_to_write_them(self):
        api = self._type("/vote собрать 05.10 28.09")
        self.assertIn("позже", api.sent[0][0])
        self.assertIn("/vote собрать 28.09 05.10", api.sent[0][0])
        self.assertEqual(self.scans, [])

    def test_add_new_keeps_its_own_fixed_window(self):
        self._type("/vote добавить")
        (scan,) = self.scans
        self.assertEqual(scan["weeks"], bot_listener.VOTE_COLLECT_WEEKS)
        self.assertIs(scan["stop_at_known"], True)
        self.assertNotIn("since", scan)

    def test_a_narrower_collect_keeps_exactly_those_days_works(self):
        self._seed()
        api = self._type("/vote собрать 28.09 05.10", new_entries=[_entry("9", date(2026, 10, 4))])
        poll = voting.load_poll(CHAT, THIS_WEEK)
        # 20.09 left, with its admission and its vote; 29.09 and 03.10 stayed, the undated
        # one is never removed on a guess, and the new find joined them.
        self.assertEqual([e.entry_id for e in poll.entries], ["2", "3", "4", "9"])
        self.assertEqual(poll.approved, ["2"])
        self.assertEqual(poll.votes, {"42": ["2"]})
        self.assertIn("Убрано работ вне этих дат: 1", " ".join(text for text, _ in api.sent))


class CallbackFlowTests(_Storage):
    def test_picking_the_first_day_moves_to_the_last(self):
        api = self._tap(f"votedate:f:20260928:-:{ADMIN['id']}")
        (message_id, text, markup), = api.edits
        self.assertEqual(message_id, 55)
        self.assertIn("по какой день", text)
        self.assertIn("По сегодня (05.10)", _by_text(markup))

    def test_paging_months_redraws_the_same_step(self):
        api = self._tap(f"votedate:fm:202609:-:{ADMIN['id']}")
        self.assertIn("Сентябрь 2026", [b["text"] for b in _buttons(api.edits[0][2])])

    def test_the_confirmation_names_the_dates_and_what_will_leave(self):
        self._seed()
        api = self._tap(f"votedate:t:20260928:20261005:{ADMIN['id']}")
        text, markup = api.edits[0][1], api.edits[0][2]
        self.assertIn("с 28.09 по 05.10 (8 дней)", text)
        self.assertIn("вне этих дат — 1 (допущено 1, с голосами 1)", text)
        self.assertEqual(_by_text(markup)["✅ Собрать"], f"votedate:go:20260928:20261005:{ADMIN['id']}")
        self.assertEqual(self.scans, [])
        self.assertEqual(len(voting.load_poll(CHAT, THIS_WEEK).entries), 4)  # nothing changed yet

    def test_with_nothing_outside_the_dates_there_is_no_warning(self):
        self._seed()
        api = self._tap(f"votedate:t:20260915:20261005:{ADMIN['id']}")
        self.assertNotIn("Внимание", api.edits[0][1])

    def test_collect_takes_its_buttons_away_then_scans_those_days(self):
        api = self._tap(f"votedate:go:20260928:20261005:{ADMIN['id']}", new_entries=[_entry("9", date(2026, 10, 4))])
        message_id, text, markup = api.edits[0]
        self.assertIn("Собираю заявки с 28.09 по 05.10", text)
        self.assertEqual(markup, {"inline_keyboard": []})
        (scan,) = self.scans
        self.assertEqual((scan["since"].date(), scan["until"].date()), (date(2026, 9, 28), date(2026, 10, 6)))
        self.assertEqual([e.entry_id for e in voting.load_poll(CHAT, THIS_WEEK).entries], ["9"])

    def test_the_replay_still_checks_the_administrator(self):
        """The button is bound to the admin who asked, but the collect itself re-checks:
        somebody who has lost the role since must not be able to finish a collect."""
        async def never(*args, **kwargs):
            return False

        with patch.object(bot_listener, "_can_manage_chat", never):
            api = FakeApi()
            callback = {"id": "cbq", "from": ADMIN, "data": f"votedate:go:20260928:20261005:{ADMIN['id']}",
                        "message": {"message_id": 55, "chat": {"id": DM, "type": "private"}}}

            async def scenario():
                tasks = set()
                await bot_listener.handle_vote_date_callback(
                    api, None, _cfg(), timezone.utc, callback, CHAT, "testbot", tasks, {}, log=lambda *_: None)
                await asyncio.gather(*tasks)

            async def resolve(*args, **kwargs):
                return -100

            async def collect_entries(**kwargs):
                self.scans.append(kwargs)
                return []

            with patch.object(bot_listener, "_resolve_chat_id", resolve), \
                    patch.object(voting, "collect_entries", collect_entries):
                asyncio.run(scenario())
        self.assertEqual(self.scans, [])
        self.assertIn("администратор", api.sent[0][0])

    def test_somebody_elses_tap_does_nothing(self):
        api = self._tap(f"votedate:go:20260928:20261005:{ADMIN['id']}", user=STRANGER)
        self.assertEqual(api.answered, [("cbq", "Эта кнопка не для тебя.")])
        self.assertEqual(api.edits, [])
        self.assertEqual(self.scans, [])

    def test_cancel_says_so_and_collects_nothing(self):
        api = self._tap(f"votedate:x:-:-:{ADMIN['id']}")
        self.assertIn("отменён", api.edits[0][1])
        self.assertEqual(api.edits[0][2], {"inline_keyboard": []})
        self.assertEqual(self.scans, [])

    def test_a_forged_range_goes_back_to_the_picker_instead_of_scanning(self):
        for data in (f"votedate:go:20261001:20261009:{ADMIN['id']}",   # ends in the future
                     f"votedate:go:20260101:20261005:{ADMIN['id']}",   # far past the cap
                     f"votedate:go:2026-bad:20261005:{ADMIN['id']}"):  # not a date at all
            with self.subTest(data=data):
                api = self._tap(data)
                self.assertIn("с какого дня", api.edits[0][1])
        self.assertEqual(self.scans, [])

    def test_the_spinner_is_stopped_on_every_tap(self):
        for data in (f"votedate:noop:-:-:{ADMIN['id']}", f"votedate:f:20260928:-:{ADMIN['id']}"):
            with self.subTest(data=data):
                self.assertEqual(len(self._tap(data).answered), 1)

    def test_the_dispatcher_routes_the_picker_to_its_handler(self):
        reached = []

        async def handle(*args, **kwargs):
            reached.append(args[4]["data"])

        async def go():
            callback = {"id": "cbq", "from": ADMIN, "data": f"votedate:x:-:-:{ADMIN['id']}",
                        "message": {"message_id": 55, "chat": {"id": DM, "type": "private"}}}
            with patch.object(bot_listener, "handle_vote_date_callback", handle):
                await bot_listener._dispatch_update(
                    {"callback_query": callback}, FakeApi(), None, _cfg(), None, "testbot", 1, set(),
                    asyncio.Queue(), set(), CHAT, {}, {}, {}, {}, log=lambda *_: None,
                )

        asyncio.run(go())
        self.assertEqual(reached, [f"votedate:x:-:-:{ADMIN['id']}"])


class _Message:
    def __init__(self, id, when):
        self.id, self.text, self.grouped_id, self.date = id, "работа #итогинедели", None, when
        self.photo, self.action = True, None

    async def get_sender(self):
        return SimpleNamespace(id=self.id, username=None, first_name="A", last_name=None)


class _Client:
    """Newest first, like Telethon. `honour_offset` False plays a client that ignores
    offset_date, to prove the scan's own bound holds regardless."""

    def __init__(self, messages, honour_offset=True):
        self.messages, self.honour_offset, self.listing = messages, honour_offset, None

    async def iter_messages(self, entity, reverse=False, offset_date=None):
        self.listing = {"reverse": reverse, "offset_date": offset_date}
        for message in self.messages:
            if self.honour_offset and offset_date is not None and message.date >= offset_date:
                continue
            yield message

    async def download_media(self, message, file=None):
        Path(file).write_bytes(b"photo")


class ScanBoundsTests(AsyncTestCase):
    async def test_the_scan_reads_only_the_days_it_was_given(self):
        since = datetime(2026, 9, 28, tzinfo=timezone.utc)
        until = datetime(2026, 10, 6, tzinfo=timezone.utc)
        messages = [
            _Message(4, until),                            # the next day: outside
            _Message(3, until - timedelta(seconds=1)),     # the last moment of the last day
            _Message(2, since),                            # the first moment of the first day
            _Message(1, since - timedelta(seconds=1)),     # the day before: outside
        ]
        for honour in (True, False):
            with self.subTest(client_honours_offset_date=honour), tempfile.TemporaryDirectory() as media:
                client = _Client(messages, honour_offset=honour)
                found = await voting.collect_entries(
                    client, object(), timezone.utc, Path(media), since=since, until=until,
                    stop_at_known=False, log=lambda *_: None,
                )
                self.assertEqual(sorted(e.entry_id for e in found), ["2", "3"])
                self.assertEqual(client.listing["offset_date"], until)

    async def test_without_dates_the_listing_starts_at_the_newest_message_as_before(self):
        with tempfile.TemporaryDirectory() as media:
            client = _Client([_Message(1, datetime.now(timezone.utc))])
            await voting.collect_entries(client, object(), timezone.utc, Path(media), log=lambda *_: None)
            self.assertIsNone(client.listing["offset_date"])


class PostedWithinTests(unittest.TestCase):
    def test_bounds_and_the_undated(self):
        since = datetime(2026, 9, 28, tzinfo=timezone.utc)
        until = datetime(2026, 10, 6, tzinfo=timezone.utc)
        self.assertTrue(voting.posted_within(_entry("1", date(2026, 9, 28)), since, until))
        self.assertTrue(voting.posted_within(_entry("1", date(2026, 10, 5)), since, until))
        self.assertFalse(voting.posted_within(_entry("1", date(2026, 10, 6)), since, until))
        self.assertFalse(voting.posted_within(_entry("1", date(2026, 9, 27)), since, until))
        self.assertTrue(voting.posted_within(_entry("1"), since, until))  # unknown: kept


if __name__ == "__main__":
    unittest.main()
