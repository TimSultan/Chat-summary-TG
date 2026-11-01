"""vote_monthly.py and "/vote месяц" -- «Лучшая работа месяца».

The month's three votes are built from the weeks' records in the Hall of Fame: every
week's third place goes into the bronze round, the second places into silver, the winners
into gold, photos, framing and names included. A week belongs to the month of the Sunday
its vote was held on; a week of the month before comes in when it ended after that
month's rounds began. The page runs the rounds one after another.
"""

import asyncio
import hashlib
import hmac
import json
import shutil
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_listener
import hall_of_fame
import nominations
import vote_monthly
import vote_web
import voting
from tests.async_case import AsyncTestCase

CHAT = "Chat"
UTC = timezone.utc
ADMIN = {"id": 7, "username": "admin"}
STRANGER = {"id": 8, "username": "someone"}
DM = 5
CROP = {"x": 10.0, "y": 20.0, "size": 300.0}


def _entry(entry_id: int) -> voting.Entry:
    return voting.Entry(
        entry_id=str(entry_id), message_id=entry_id, author_id=entry_id + 1000,
        author_name=f"Автор {entry_id}", author_username=f"user{entry_id}",
        text=f"работа {entry_id}", media=[f"{entry_id}_0.jpg", f"{entry_id}_1.jpg"],
        posted_at="2026-10-01T12:00:00+00:00",
    )


class _Store(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self._patchers = [
            patch("voting._voting_dir", return_value=self.root / "voting"),
            patch("hall_of_fame._hall_dir", return_value=self.root / "hall"),
        ]
        for patcher in self._patchers:
            patcher.start()
        hall_of_fame._cache.clear()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for patcher in self._patchers:
            patcher.stop()
        hall_of_fame._cache.clear()
        self._temporary.cleanup()

    def _week(self, poll_id, created_at, first_id, votes=(3, 2, 1), hashtag=voting.CONTEST_HASHTAG,
              title=""):
        """A closed weekly vote recorded in the hall, as closing it from the page does: its
        works are first_id, +1, +2 with `votes` each, the first one framed by hand."""
        entries = [_entry(first_id + offset) for offset in range(len(votes))]
        poll = voting.Poll(poll_id=poll_id, entry=CHAT, created_at=created_at, entries=entries,
                           hashtag=hashtag, title=title)
        media = voting.media_path(CHAT, poll_id)
        media.mkdir(parents=True, exist_ok=True)
        for entry in entries:
            for name in entry.media:
                Image.new("RGB", (640, 480), (first_id % 255, 90, 160)).save(media / name)
        voting.set_approved(poll, [e.entry_id for e in entries])
        for entry, count in zip(entries, votes):
            for voter in range(count):
                voting.record_vote(poll, f"{poll_id}-{entry.entry_id}-{voter}", [entry.entry_id])
        poll.crops = {entries[0].entry_id: dict(CROP)}
        voting.close_and_announce(poll)
        poll.open = False   # a week nobody voted in is over too
        voting.save_poll(poll)
        hall_of_fame.record_poll(poll, poll.tally())
        return poll

    def _september_rounds(self, created_at="2026-09-22T10:54:27+00:00"):
        """September's rounds, as the archive brought them in: run on 22 September."""
        for round_ in vote_monthly.ROUNDS:
            hall_of_fame.save_contest(hall_of_fame.Contest(
                contest_id=f"2026-09-{round_.key}", entry=CHAT, hashtag=voting.MONTHLY_HASHTAG,
                title=vote_monthly.round_title(9, round_), badge=round_.badge, tier=round_.tier,
                created_at=created_at, closed_at="2026-09-23T12:00:00+00:00",
            ))

    def _october(self):
        """Every kind of week around October 2026."""
        self._september_rounds()
        self._week("2026-W38", "2026-09-20T09:00:00+00:00", 380)   # before September's rounds
        self._week("2026-W40", "2026-09-28T09:00:00+00:00", 390)   # collected Mon 28.09: Sun 27.09
        self._week("2026-W40b", "2026-10-04T09:00:00+00:00", 400)  # Sunday 04.10
        self._week("2026-W42", "2026-10-12T09:00:00+00:00", 410)   # collected Mon 12.10: Sun 11.10
        self._week("2026-W42b", "2026-10-18T09:00:00+00:00", 420, votes=(0, 0, 0))  # nobody voted
        self._week("2026-W43", "2026-10-25T09:00:00+00:00", 430, votes=(5, 4))       # no third place
        self._week("2026-W44", "2026-11-01T09:00:00+00:00", 440)   # November's
        self._week("2026-W41-abcdef", "2026-10-10T09:00:00+00:00", 450, hashtag="#аниме", title="Аниме")


class WeekTests(unittest.TestCase):
    def test_a_vote_belongs_to_the_sunday_nearest_its_start(self):
        sunday = date(2026, 10, 25)
        for day in (date(2026, 10, 22), date(2026, 10, 25), date(2026, 10, 26), date(2026, 10, 28)):
            with self.subTest(day=day):
                self.assertEqual(vote_monthly.nearest_sunday(day), sunday)
        self.assertEqual(vote_monthly.nearest_sunday(date(2026, 10, 29)), date(2026, 11, 1))

    def test_a_week_collected_on_monday_is_the_sunday_before_whatever_its_id_says(self):
        contest = hall_of_fame.Contest(contest_id="2026-W44", entry=CHAT,
                                       created_at="2026-10-26T08:00:00+00:00")
        self.assertEqual(vote_monthly.week_sunday(contest, UTC), date(2026, 10, 25))

    def test_without_a_time_the_ids_own_sunday_is_used(self):
        contest = hall_of_fame.Contest(contest_id="2026-W43", entry=CHAT)
        self.assertEqual(vote_monthly.week_sunday(contest, UTC), date(2026, 10, 25))

    def test_round_ids_and_titles_match_the_archived_rounds(self):
        gold = vote_monthly.ROUNDS[2]
        self.assertEqual(vote_monthly.round_poll_id(2026, 7, gold), "2026-07-gold")
        self.assertEqual(vote_monthly.round_title(7, gold), "Лучшая работа месяца · июль · золото")
        self.assertEqual(vote_monthly.parse_round_id("2026-10-bronze")[2].tier, 3)
        self.assertIsNone(vote_monthly.parse_round_id("2026-W41"))


class PlanTests(_Store):
    def test_the_months_weeks_and_the_one_september_never_counted(self):
        self._october()
        plan = vote_monthly.plan_month(CHAT, 2026, 10, UTC)
        self.assertEqual([(w.contest.contest_id, w.sunday, w.selected, w.carried) for w in plan.weeks], [
            ("2026-W40", date(2026, 9, 27), True, True),
            ("2026-W40b", date(2026, 10, 4), True, False),
            ("2026-W42", date(2026, 10, 11), True, False),
            ("2026-W43", date(2026, 10, 25), True, False),
        ])
        self.assertEqual(plan.previous_started, date(2026, 9, 22))
        # Place by place: the first is gold, the second silver, the third bronze.
        medals = plan.weeks[1].medals
        self.assertEqual({tier: work.entry_id for tier, work in medals.items()},
                         {1: "400", 2: "401", 3: "402"})
        self.assertEqual(set(plan.weeks[3].medals), {1, 2})

    def test_with_no_rounds_the_month_before_its_weeks_are_offered_but_not_ticked(self):
        self._week("2026-W39", "2026-09-27T09:00:00+00:00", 390)
        self._week("2026-W40", "2026-10-04T09:00:00+00:00", 400)
        plan = vote_monthly.plan_month(CHAT, 2026, 10, UTC)
        self.assertEqual([(w.sunday, w.selected) for w in plan.weeks],
                         [(date(2026, 9, 27), False), (date(2026, 10, 4), True)])
        self.assertEqual([w.contest.contest_id for w in plan.selected()], ["2026-W40"])

    def test_a_weekly_vote_still_running_is_named(self):
        poll = voting.Poll(poll_id="2026-W44", entry=CHAT, created_at="2026-11-01T09:00:00+00:00",
                           entries=[_entry(1)])
        voting.set_approved(poll, ["1"])
        voting.save_poll(poll)
        self.assertEqual(vote_monthly.plan_month(CHAT, 2026, 11, UTC).running, [date(2026, 11, 1)])


class CreateTests(_Store):
    def _create(self, mask=None, now=datetime(2026, 11, 1, 12, 0, tzinfo=UTC)):
        plan = vote_monthly.plan_month(CHAT, 2026, 10, UTC)
        return vote_monthly.create_rounds(CHAT, plan, plan.selected(mask), now=now)

    def test_three_separate_votes_one_per_medal(self):
        self._october()
        results = self._create()
        self.assertEqual([(r.poll_id, r.outcome, r.works) for r in results], [
            ("2026-10-bronze", "created", 3), ("2026-10-silver", "created", 4),
            ("2026-10-gold", "created", 4),
        ])
        bronze = voting.load_poll(CHAT, "2026-10-bronze")
        self.assertEqual([e.entry_id for e in bronze.entries], ["392", "402", "412"])
        self.assertEqual(bronze.approved, ["392", "402", "412"])
        self.assertTrue(bronze.open)
        self.assertEqual((bronze.hashtag, bronze.tier, bronze.badge, bronze.max_choices),
                         (voting.MONTHLY_HASHTAG, 3, "🥉", 1))
        self.assertEqual(bronze.label(), "Лучшая работа месяца · октябрь · бронза")
        gold = voting.load_poll(CHAT, "2026-10-gold")
        self.assertEqual([e.entry_id for e in gold.entries], ["390", "400", "410", "430"])
        self.assertEqual(gold.tier, 1)
        self.assertEqual([e.entry_id for e in voting.load_poll(CHAT, "2026-10-silver").entries],
                         ["391", "401", "411", "431"])

    def test_pictures_names_and_framing_come_from_the_weeks_even_after_a_clear(self):
        self._october()
        # "/vote очистить", and the weeks' own photos gone too: the hall's copies remain.
        voting.archive_all_polls(CHAT)
        shutil.rmtree(self.root / "voting")
        self._create()
        gold = voting.load_poll(CHAT, "2026-10-gold")
        first = gold.entries[0]
        self.assertEqual((first.author_name, first.author_username, first.author_id, first.text),
                         ("Автор 390", "user390", 1390, "работа 390"))
        self.assertEqual(first.message_id, 390)
        self.assertEqual(first.media, ["390_0.jpg", "390_1.jpg"])
        media = voting.media_path(CHAT, "2026-10-gold")
        for entry in gold.entries:
            for name in entry.media:
                self.assertTrue((media / name).is_file(), name)
        # The winners were each framed by hand in their week.
        self.assertEqual(gold.crops["390"], CROP)

    def test_unticked_weeks_stay_out(self):
        self._october()
        self._create(mask=0b0110)  # only 04.10 and 11.10
        self.assertEqual([e.entry_id for e in voting.load_poll(CHAT, "2026-10-gold").entries],
                         ["400", "410"])

    def test_the_page_opens_bronze_then_moves_to_silver_then_gold(self):
        self._october()
        self._create()
        self.assertEqual(voting.latest_poll(CHAT).poll_id, "2026-10-bronze")
        for current, following in (("2026-10-bronze", "2026-10-silver"), ("2026-10-silver", "2026-10-gold")):
            poll = voting.load_poll(CHAT, current)
            voting.record_vote(poll, "voter", [poll.approved[0]])
            voting.close_and_announce(poll)
            voting.save_poll(poll)
            self.assertEqual(vote_monthly.start_next_round(CHAT, poll).poll_id, following)
            self.assertEqual(voting.latest_poll(CHAT).poll_id, following)
        gold = voting.load_poll(CHAT, "2026-10-gold")
        voting.record_vote(gold, "voter", [gold.approved[0]])
        voting.close_and_announce(gold)
        voting.save_poll(gold)
        self.assertIsNone(vote_monthly.start_next_round(CHAT, gold))
        # The last round decided keeps the page, with its result.
        self.assertEqual(voting.latest_poll(CHAT).poll_id, "2026-10-gold")

    def test_a_round_somebody_voted_in_is_never_rebuilt_an_untouched_one_is(self):
        self._october()
        self._create(mask=0b0010)
        bronze = voting.load_poll(CHAT, "2026-10-bronze")
        voting.record_vote(bronze, "voter", ["402"])
        voting.save_poll(bronze)
        silver_before = voting.load_poll(CHAT, "2026-10-silver")
        results = self._create(now=datetime(2026, 11, 2, tzinfo=UTC))
        self.assertEqual([r.outcome for r in results], ["kept", "rebuilt", "rebuilt"])
        self.assertEqual([e.entry_id for e in voting.load_poll(CHAT, "2026-10-bronze").entries], ["402"])
        silver = voting.load_poll(CHAT, "2026-10-silver")
        self.assertEqual(len(silver.entries), 4)
        self.assertEqual(silver.created_at, silver_before.created_at)  # its place in the queue

    def test_a_round_no_week_has_a_place_for_is_not_created(self):
        self._september_rounds()
        self._week("2026-W43", "2026-10-25T09:00:00+00:00", 430, votes=(5, 4))
        results = self._create()
        self.assertEqual([r.outcome for r in results], ["empty", "created", "created"])
        self.assertIsNone(voting.load_poll(CHAT, "2026-10-bronze"))

    def test_a_work_with_no_picture_anywhere_is_left_out_and_named(self):
        self._september_rounds()
        self._week("2026-W40", "2026-10-04T09:00:00+00:00", 400)
        shutil.rmtree(self.root / "voting")
        shutil.rmtree(hall_of_fame.media_dir(CHAT, "2026-W40"))
        (gold, *_) = [r for r in self._create() if r.round.key == "gold"]
        self.assertEqual(gold.outcome, "empty")
        self.assertEqual(gold.without_photos, ["Автор 400 (неделя 04.10)"])


class HallTests(_Store):
    def test_a_closed_round_is_recorded_with_its_medal(self):
        self._october()
        vote_monthly.create_rounds(CHAT, plan := vote_monthly.plan_month(CHAT, 2026, 10, UTC), plan.selected())
        bronze = voting.load_poll(CHAT, "2026-10-bronze")
        voting.record_vote(bronze, "a", ["412"])
        voting.record_vote(bronze, "b", ["412"])
        voting.record_vote(bronze, "c", ["402"])
        voting.close_and_announce(bronze)
        contest = hall_of_fame.record_poll(bronze, bronze.tally())
        self.assertTrue(contest.is_monthly)
        self.assertEqual(contest.tier, 3)
        winner, second = contest.works[0], contest.works[1]
        self.assertEqual(winner.entry_id, "412")
        self.assertEqual(contest.medal_for(winner), 3)   # the month's bronze, not a gold
        self.assertIsNone(contest.medal_for(second))
        self.assertTrue(winner.photos)

    def test_a_results_record_brings_the_medal_back_after_a_clear(self):
        self._october()
        vote_monthly.create_rounds(CHAT, plan := vote_monthly.plan_month(CHAT, 2026, 10, UTC), plan.selected())
        silver = voting.load_poll(CHAT, "2026-10-silver")
        voting.record_vote(silver, "a", ["401"])
        voting.close_and_announce(silver)
        voting.save_poll(silver)
        voting.save_results(silver, silver.tally(), "текст")
        self.assertEqual(voting.load_results(CHAT, "2026-10-silver")["tier"], 2)
        voting.archive_all_polls(CHAT)
        hall_of_fame.import_history(CHAT)
        self.assertEqual(hall_of_fame.load_contest(CHAT, "2026-10-silver").tier, 2)

    def test_a_poll_keeps_its_tier_on_disk(self):
        poll = voting.Poll(poll_id="2026-10-gold", entry=CHAT, created_at="x", hashtag=voting.MONTHLY_HASHTAG, tier=1)
        voting.save_poll(poll)
        self.assertEqual(voting.load_poll(CHAT, "2026-10-gold").tier, 1)
        self.assertEqual(voting.Poll.from_dict({"tier": 9}).tier, 0)

    def test_the_results_header_names_the_round(self):
        poll = voting.Poll(poll_id="2026-10-gold", entry=CHAT, created_at="", hashtag=voting.MONTHLY_HASHTAG,
                           title="Лучшая работа месяца · октябрь · золото")
        self.assertEqual(voting.results_header(poll),
                         "Результаты голосования «Лучшая работа месяца · октябрь · золото»:")

    def test_nominations_never_take_a_rounds_old_works(self):
        self._october()
        voting.archive_all_polls(CHAT)
        vote_monthly.create_rounds(CHAT, plan := vote_monthly.plan_month(CHAT, 2026, 10, UTC), plan.selected())
        works, _ = nominations.v1_collection(CHAT)
        self.assertEqual(works, [])


def _cfg():
    return SimpleNamespace(webapp_public_url="https://example.com",
                           vote_miniapp_short_name=None, vote_announce_extra_chat=None)


class FakeApi:
    def __init__(self):
        self.sent = []      # (chat id, text, reply_markup)
        self.edits = []     # (message_id, text, reply_markup)
        self.answered = []

    async def send_message(self, chat_id, text, reply_to_message_id=None, reply_markup=None,
                           parse_mode=None, disable_notification=False):
        self.sent.append((chat_id, text, reply_markup))
        return {"message_id": 100 + len(self.sent)}

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None, parse_mode=None):
        self.edits.append((message_id, text, reply_markup))

    async def answer_callback_query(self, callback_id, text=None):
        self.answered.append((callback_id, text))


def _buttons(markup):
    return [b for row in (markup or {}).get("inline_keyboard", []) for b in row]


def _by_text(markup):
    return {b["text"]: b.get("callback_data") for b in _buttons(markup)}


class CommandTests(_Store):
    def _run(self, coroutine_factory):
        async def resolve(*args, **kwargs):
            return -100

        async def can_manage(api, chat_id, user, entry=None):
            return user.get("id") == ADMIN["id"]

        class _Today(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 11, 1, 15, 0, tzinfo=tz or UTC)

        async def scenario():
            tasks = set()
            with patch.object(bot_listener, "_resolve_chat_id", resolve), \
                    patch.object(bot_listener, "_can_manage_chat", can_manage), \
                    patch.object(bot_listener, "datetime", _Today):
                await coroutine_factory(tasks)
                while tasks:
                    await asyncio.gather(*list(tasks))
        asyncio.run(scenario())

    def _type(self, text, user=ADMIN):
        api = FakeApi()
        message = {"message_id": 1, "chat": {"id": DM, "type": "private"}, "from": user, "text": text}
        self._run(lambda tasks: bot_listener.handle_vote_command(
            api, None, _cfg(), UTC, message, CHAT, "testbot", tasks, log=lambda *_: None,
        ))
        return api

    def _tap(self, data, user=ADMIN):
        api = FakeApi()
        callback = {"id": "cbq", "from": user, "data": data,
                    "message": {"message_id": 55, "chat": {"id": DM, "type": "private"}}}
        self._run(lambda tasks: bot_listener.handle_vote_month_callback(
            api, None, _cfg(), UTC, callback, CHAT, log=lambda *_: None,
        ))
        return api

    def test_the_screen_opens_on_the_month_just_ended_with_its_weeks(self):
        self._october()
        api = self._type("/vote месяц")
        _, text, markup = api.sent[0]
        self.assertIn("Итоги месяца · Октябрь 2026", text)
        self.assertIn("✅ 27.09 — неделя сентября, в итоги сентября не попала", text)
        self.assertIn("🥇 Автор 400 · 🥈 Автор 401 · 🥉 Автор 402", text)
        self.assertIn("🥉 бронза — 3 работы", text)
        self.assertIn("🥇 золото — 4 работы", text)
        buttons = _by_text(markup)
        self.assertIn("✓ Октябрь", buttons)
        self.assertIn("Ноябрь", buttons)
        self.assertIn("🏅 Создать голосования", buttons)
        self.assertEqual([t for t in buttons if t.startswith("✅")], ["✅ 27.09", "✅ 04.10", "✅ 11.10", "✅ 25.10"])
        self.assertIsNone(voting.load_poll(CHAT, "2026-10-bronze"))   # nothing created yet

    def test_a_month_can_be_typed(self):
        self._october()
        api = self._type("/vote месяц 11.2026")
        self.assertIn("Ноябрь 2026", api.sent[0][1])
        self.assertIn("Не понял месяц", self._type("/vote месяц 12.2026").sent[0][1])  # still to come

    def test_only_an_administrator_gets_the_screen(self):
        self._october()
        api = self._type("/vote месяц", user=STRANGER)
        self.assertIn("только администраторы", api.sent[0][1])
        self.assertIsNone(api.sent[0][2])

    def test_the_panel_has_the_button(self):
        self.assertEqual(bot_listener.VOTE_ACTIONS["month"], "/vote месяц")
        api = self._type("/vote")
        self.assertIn("🏅 Итоги месяца", _by_text(api.sent[0][2]))

    def test_ticking_a_week_off_redraws_the_screen(self):
        self._october()
        markup = self._type("/vote месяц").sent[0][2]
        api = self._tap(_by_text(markup)["✅ 27.09"])
        (_, text, redrawn), = api.edits
        self.assertIn("⬜ 27.09", text)
        self.assertIn("🥇 золото — 3 работы", text)
        self.assertIn("⬜ 27.09", _by_text(redrawn))

    def test_create_makes_the_three_rounds_and_hands_over_the_page(self):
        self._october()
        markup = self._type("/vote месяц").sent[0][2]
        api = self._tap(_by_text(markup)["🏅 Создать голосования"])
        self.assertEqual(api.edits[0][2], {"inline_keyboard": []})   # nothing left to tap twice
        summary = api.edits[-1][1]
        self.assertIn("🥉 бронза — создано, 3 работы", summary)
        self.assertIn("На странице сейчас: Лучшая работа месяца · октябрь · бронза", summary)
        for round_ in vote_monthly.ROUNDS:
            self.assertIsNotNone(voting.load_poll(CHAT, f"2026-10-{round_.key}"))
        _, _, buttons = api.sent[-1]
        self.assertEqual(_buttons(buttons)[0]["web_app"]["url"], "https://example.com/vote")

    def test_create_takes_the_weeks_as_ticked(self):
        self._october()
        markup = self._type("/vote месяц").sent[0][2]
        markup = self._tap(_by_text(markup)["✅ 27.09"]).edits[0][2]
        self._tap(_by_text(markup)["🏅 Создать голосования"])
        self.assertEqual([e.entry_id for e in voting.load_poll(CHAT, "2026-10-gold").entries],
                         ["400", "410", "430"])

    def test_create_rechecks_the_administrator(self):
        self._october()
        markup = self._type("/vote месяц").sent[0][2]
        data = _by_text(markup)["🏅 Создать голосования"]

        async def never(*args, **kwargs):
            return False

        with patch.object(bot_listener, "_can_manage_chat", never):
            api = FakeApi()
            callback = {"id": "cbq", "from": ADMIN, "data": data,
                        "message": {"message_id": 55, "chat": {"id": DM, "type": "private"}}}

            async def resolve(*args, **kwargs):
                return -100

            with patch.object(bot_listener, "_resolve_chat_id", resolve):
                asyncio.run(bot_listener.handle_vote_month_callback(
                    api, None, _cfg(), UTC, callback, CHAT, log=lambda *_: None))
        self.assertIn("только администраторы", api.edits[0][1])
        self.assertIsNone(voting.load_poll(CHAT, "2026-10-bronze"))

    def test_somebody_elses_tap_does_nothing(self):
        self._october()
        data = _by_text(self._type("/vote месяц").sent[0][2])["🏅 Создать голосования"]
        api = self._tap(data, user=STRANGER)
        self.assertEqual(api.answered, [("cbq", "Эта кнопка не для тебя.")])
        self.assertEqual(api.edits, [])
        self.assertIsNone(voting.load_poll(CHAT, "2026-10-bronze"))

    def test_a_week_that_closed_meanwhile_sends_the_admin_back_to_the_list(self):
        self._october()
        data = _by_text(self._type("/vote месяц").sent[0][2])["🏅 Создать голосования"]
        self._week("2026-W42c", "2026-10-18T09:00:00+00:00", 460)
        api = self._tap(data)
        self.assertIn("закрылась ещё одна неделя", api.edits[-1][1])
        self.assertIn("✅ 18.10", _by_text(api.edits[-1][2]))
        self.assertIsNone(voting.load_poll(CHAT, "2026-10-bronze"))

    def test_the_status_panel_names_the_round(self):
        self._october()
        voting.archive_all_polls(CHAT)
        vote_monthly.create_rounds(CHAT, plan := vote_monthly.plan_month(CHAT, 2026, 10, UTC), plan.selected())
        text = bot_listener._vote_status_text(CHAT)
        self.assertIn("Голосование: 🥉 Лучшая работа месяца · октябрь · бронза", text)
        self.assertIn("Раунд: 2026-10-bronze", text)

    def test_voters_are_told_which_round_it_is(self):
        self._october()
        self.assertEqual(bot_listener._vote_heading(CHAT), "Голосование за итоги недели")
        vote_monthly.create_rounds(CHAT, plan := vote_monthly.plan_month(CHAT, 2026, 10, UTC), plan.selected())
        api = self._type("/vote", user=STRANGER)
        self.assertTrue(api.sent[0][1].startswith(
            "Голосование «Лучшая работа месяца · октябрь · бронза» 🥉:"))

    def test_closing_a_round_hands_the_page_to_the_next(self):
        self._october()
        vote_monthly.create_rounds(CHAT, plan := vote_monthly.plan_month(CHAT, 2026, 10, UTC), plan.selected())
        bronze = voting.load_poll(CHAT, "2026-10-bronze")
        voting.record_vote(bronze, "a", ["402"])
        voting.close_and_announce(bronze)
        voting.save_poll(bronze)
        following = asyncio.run(bot_listener._start_next_monthly_round(bronze, log=lambda *_: None))
        self.assertEqual(following.poll_id, "2026-10-silver")
        self.assertEqual(voting.latest_poll(CHAT).poll_id, "2026-10-silver")
        weekly = voting.load_poll(CHAT, "2026-W43")
        self.assertIsNone(asyncio.run(bot_listener._start_next_monthly_round(weekly, log=lambda *_: None)))

    def test_add_new_with_a_round_on_the_page_tops_up_the_weekly_vote(self):
        self._october()
        voting.archive_all_polls(CHAT)
        vote_monthly.create_rounds(CHAT, plan := vote_monthly.plan_month(CHAT, 2026, 10, UTC), plan.selected())
        scans = []

        async def collect_entries(**kwargs):
            scans.append(kwargs)
            return []

        with patch.object(voting, "collect_entries", collect_entries):
            self._type("/vote добавить")
        self.assertEqual(scans[0]["hashtag"], voting.CONTEST_HASHTAG)

    def test_the_dispatcher_routes_the_screen_to_its_handler(self):
        reached = []

        async def handle(*args, **kwargs):
            reached.append(args[4]["data"])

        data = f"votemonth:x:202610:-:-:{ADMIN['id']}"

        async def go():
            callback = {"id": "cbq", "from": ADMIN, "data": data,
                        "message": {"message_id": 55, "chat": {"id": DM, "type": "private"}}}
            with patch.object(bot_listener, "handle_vote_month_callback", handle):
                await bot_listener._dispatch_update(
                    {"callback_query": callback}, FakeApi(), None, _cfg(), None, "testbot", 1, set(),
                    asyncio.Queue(), set(), CHAT, {}, {}, {}, {}, log=lambda *_: None,
                )

        asyncio.run(go())
        self.assertEqual(reached, [data])

    def test_callback_data_fits_telegrams_limit_and_round_trips(self):
        data = bot_listener._vote_month_callback_data("go", 2026, 10, 1234567890123, 0b1111111111, "ab12")
        self.assertLessEqual(len(data.encode("utf-8")), 64)
        self.assertEqual(bot_listener._parse_vote_month_callback(data),
                         ("go", 2026, 10, 0b1111111111, "ab12", 1234567890123))
        self.assertIsNone(bot_listener._parse_vote_month_callback("votemonth:rm:202610:-:-:7"))


BOT_TOKEN = "123456:FAKE-TOKEN-FOR-TESTS"


def _init_data(user_id: int) -> str:
    fields = {"auth_date": str(int(time.time())),
              "user": json.dumps({"id": user_id, "username": "voter", "first_name": "V"})}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    return urlencode(fields)


class PageTests(_Store, AsyncTestCase):
    """What a voter opening the Mini App gets once the rounds exist."""

    async def asyncSetUp(self):
        async def is_admin(user):
            return False

        app = vote_web.create_app(SimpleNamespace(telegram_bot_token=BOT_TOKEN), CHAT, is_admin,
                                  log=lambda *_: None)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_the_bronze_round_with_its_pictures_and_one_choice(self):
        self._october()
        voting.archive_all_polls(CHAT)
        vote_monthly.create_rounds(CHAT, plan := vote_monthly.plan_month(CHAT, 2026, 10, UTC), plan.selected())
        response = await self.client.get("/vote/api/poll", headers={"X-Telegram-Init-Data": _init_data(2)})
        poll = await response.json()
        self.assertEqual(poll["poll_id"], "2026-10-bronze")
        self.assertEqual(poll["title"], "Лучшая работа месяца · октябрь · бронза")
        self.assertEqual([e["author"] for e in poll["entries"]], ["Автор 392", "Автор 402", "Автор 412"])
        self.assertEqual(poll["max_choices"], 1)
        for entry in poll["entries"]:
            for url in entry["photos"]:
                picture = await self.client.get(url)
                self.assertEqual(picture.status, 200, url)
        two = await self.client.post("/vote/api/ballot", json={
            "init_data": _init_data(2), "choices": ["392", "402"]})
        self.assertEqual(two.status, 400)
        one = await self.client.post("/vote/api/ballot", json={"init_data": _init_data(2), "choices": ["402"]})
        self.assertEqual(one.status, 200)
        self.assertEqual(voting.load_poll(CHAT, "2026-10-bronze").votes, {"2": ["402"]})


if __name__ == "__main__":
    unittest.main()
