"""legacy_contests.py -- the Sunday-poll contests from before the bot ran the vote.

Before /vote, the weekly contest was a Telegram poll, its options carrying the works'
pictures. These tests drive the whole path against a fake chat: finding the contest polls
among all polls, downloading every picture, matching each option to a person (and to the
person's post), reading the votes, and writing the weeks into the results records, the
Hall of Fame and the numbered weekly winners -- once, never over anything already there.
"""

import asyncio
import json
import random
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from PIL import Image
from telethon.tl import types as tl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_listener
import hall_of_fame
import legacy_contests as legacy
import stats
import voting

CHAT = "Chat"
TZ = ZoneInfo("Europe/Moscow")
ORGANISER = 1
ANYA, BORYA, VIKA, GOSHA = 11, 12, 13, 14
ADMIN = {"id": 7, "username": "admin"}
STRANGER = {"id": 8, "username": "someone"}


def _at(day: str, hour: int = 12) -> datetime:
    """A message time: local `day` at `hour`, as Telegram gives it -- in UTC."""
    return datetime.fromisoformat(f"{day}T{hour:02d}:00:00").replace(tzinfo=TZ).astimezone(timezone.utc)


def _picture(seed: int, size=(160, 120), quality=85) -> Image.Image:
    """A distinct picture per seed: a coarse random grid, scaled up. The same seed at
    another size and JPEG quality is the same photo recompressed, which is all Telegram
    does to a work posted once and put on a poll option again."""
    rng = random.Random(seed)
    grid = Image.new("L", (12, 9))
    grid.putdata([rng.randrange(256) for _ in range(12 * 9)])
    return grid.resize(size, Image.BICUBIC).convert("RGB")


def _media(seed, size=(160, 120)):
    return SimpleNamespace(photo=SimpleNamespace(id=seed), seed=seed, size=size)


def _user(user_id, first, username=None):
    return tl.User(id=user_id, first_name=first, username=username)


USERS = {
    ORGANISER: _user(ORGANISER, "Орг", "org"),
    ANYA: _user(ANYA, "Аня", "anya"),
    BORYA: _user(BORYA, "Боря", None),
    VIKA: _user(VIKA, "Вика", "vika"),
    GOSHA: _user(GOSHA, "Гоша", "gosha"),
}


def _message(message_id, when, *, sender=ORGANISER, text="", media=None, grouped_id=None, fwd_from=None):
    async def get_sender():
        return USERS.get(sender)

    return SimpleNamespace(
        id=message_id, date=when, text=text, media=media, sender_id=sender, action=None,
        photo=getattr(media, "photo", None), grouped_id=grouped_id, fwd_from=fwd_from,
        get_sender=get_sender,
    )


def _answer(index, text, *, seed=None, added_by=None, mention=None):
    entities = [SimpleNamespace(user_id=mention)] if mention else []
    return SimpleNamespace(
        text=SimpleNamespace(text=text, entities=entities), option=bytes([index]),
        media=_media(seed, (240, 180)) if seed is not None else None,
        added_by=SimpleNamespace(user_id=added_by) if added_by else None,
        date=None,
    )


def _poll_message(message_id, when, question, answers, votes, *, total=None, visible=True, closed=True,
                  sender=ORGANISER):
    results = SimpleNamespace(
        results=[SimpleNamespace(option=bytes([i]), voters=(v if visible else None)) for i, v in enumerate(votes)],
        total_voters=total if total is not None else sum(votes),
    )
    poll = SimpleNamespace(question=SimpleNamespace(text=question, entities=[]), answers=answers,
                           quiz=False, closed=closed, close_date=None)
    return _message(message_id, when, sender=sender, media=SimpleNamespace(poll=poll, results=results))


class FakeClient:
    """The chat, as legacy_contests reads it: a poll listing, a hashtag search, messages by
    id, downloads that write real pictures, and users by id."""

    def __init__(self, messages, fresh_results=None):
        self.messages = {m.id: m for m in messages}
        self.fresh_results = fresh_results or {}
        self.downloads = []
        self.requests = []

    def _newest_first(self, offset_date=None, offset_id=None):
        for message in sorted(self.messages.values(), key=lambda m: m.id, reverse=True):
            if offset_date is not None and message.date >= offset_date:
                continue
            if offset_id is not None and message.id >= offset_id:
                continue
            yield message

    async def iter_messages(self, entity, filter=None, search=None, offset_date=None, offset_id=None,
                            limit=None, **kwargs):
        count = 0
        for message in self._newest_first(offset_date, offset_id):
            if filter is not None and getattr(message.media, "poll", None) is None:
                continue
            if search is not None and search.lower() not in (message.text or "").lower():
                continue
            yield message
            count += 1
            if limit is not None and count >= limit:
                return

    async def get_messages(self, entity, ids):
        if isinstance(ids, int):
            return self.messages.get(ids)
        return [self.messages.get(i) for i in ids]

    async def download_media(self, media, file):
        media = getattr(media, "media", media)
        self.downloads.append(Path(file).name)
        _picture(media.seed, media.size).save(file, "JPEG", quality=70)
        return file

    async def get_entity(self, user_id):
        if user_id not in USERS:
            raise ValueError("unknown user")
        return USERS[user_id]

    async def __call__(self, request):
        self.requests.append(request)
        results = self.fresh_results.get(request.msg_id)
        return SimpleNamespace(updates=[tl.UpdateMessagePoll(poll_id=1, results=results)] if results else [])


def _chat():
    """Three Sunday contests and the chat around them.

    Week 28 (Sunday 12 July): an open poll where each artist added their own work with its
    picture; Аня's is also her #итогинедели post, Гоша's picture is nobody's post.
    Week 29 (Sunday 19 July): the organiser's poll, options named by @tag, by plain name,
    and one nobody can be found for.
    Week 30 (Sunday 26 July): a numbered poll, "1".."3", the pictures in the album the
    organiser posted right before it.
    Plus a poll that is not a contest at all.
    """
    messages = [
        # Week 28's posts: Аня's album (the caption on its second photo), Вика's single.
        _message(100, _at("2026-07-08"), sender=ANYA, media=_media(1), grouped_id=500),
        _message(101, _at("2026-07-08"), sender=ANYA, text="#итогинедели дракон", media=_media(2), grouped_id=500),
        _message(102, _at("2026-07-09"), sender=VIKA, text="Рыцарь #итогинедели", media=_media(3)),
        _message(103, _at("2026-07-09"), sender=BORYA, text="просто болтаю"),
        _poll_message(110, _at("2026-07-12", 18), "Итоги недели! Добавляйте свои работы", [
            _answer(0, "Дракон", seed=1, added_by=ANYA),
            _answer(1, "Рыцарь", seed=3, added_by=VIKA),
            _answer(2, "Гоша — орк", seed=4, added_by=GOSHA),
        ], [7, 3, 5], total=14),
        _poll_message(111, _at("2026-07-13"), "Что заказываем на стрим?", [
            _answer(0, "Пиццу"), _answer(1, "Суши"),
        ], [4, 2]),
        # Week 29: Боря posts (no @username), Вика posts; the poll names them.
        _message(200, _at("2026-07-15"), sender=BORYA, text="#итогинедели танк", media=_media(5)),
        _message(201, _at("2026-07-16"), sender=VIKA, text="#итогинедели эльф", media=_media(6)),
        _poll_message(210, _at("2026-07-19", 18), "Голосование: лучшая работа недели", [
            _answer(0, "1. Боря"), _answer(1, "2. @vika"), _answer(2, "3. Неизвестный Художник"),
        ], [2, 6, 1]),
        # Week 30: the album, then "голосуем!", then the numbered poll.
        _message(300, _at("2026-07-22"), sender=GOSHA, text="#итогинедели гоблин", media=_media(7)),
        _message(301, _at("2026-07-23"), sender=ANYA, text="#итогинедели фея", media=_media(8)),
        _message(305, _at("2026-07-26", 17), media=_media(8, (90, 70)), grouped_id=900),
        _message(306, _at("2026-07-26", 17), media=_media(7, (90, 70)), grouped_id=900),
        _message(307, _at("2026-07-26", 17), sender=BORYA, text="ого"),
        _message(308, _at("2026-07-26", 17), text="Голосуем!"),
        _poll_message(310, _at("2026-07-26", 18), "Итоги недели", [
            _answer(0, "1"), _answer(1, "2️⃣"),
        ], [3, 4]),
    ]
    return FakeClient(messages)


MEMBERS = {
    str(ANYA): {"name": "Аня", "username": "anya"},
    str(BORYA): {"name": "Боря", "username": None},
    str(VIKA): {"name": "Вика", "username": "vika"},
    str(GOSHA): {"name": "Гоша", "username": "gosha"},
}


class _Store(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self._patchers = [
            patch("voting._voting_dir", return_value=self.root / "voting"),
            patch("hall_of_fame._hall_dir", return_value=self.root / "hall"),
            patch("stats._stats_dir", return_value=self.root / "stats"),
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

    def _scan(self, client=None, until=None):
        client = client or _chat()
        scan = asyncio.run(legacy.scan_chat(client, "chat", TZ, legacy.cache_dir(CHAT), until=until,
                                            log=lambda *_: None))
        return client, scan

    def _plan(self, client=None, until=None):
        client, scan = self._scan(client, until)
        return client, scan, legacy.build_plan(CHAT, scan, MEMBERS)


class TextTests(unittest.TestCase):
    def test_numbered_options(self):
        self.assertEqual(legacy.option_number("1"), 1)
        self.assertEqual(legacy.option_number("№ 2"), 2)
        self.assertEqual(legacy.option_number("3️⃣"), 3)
        self.assertEqual(legacy.option_number("Работа 4."), 4)
        self.assertIsNone(legacy.option_number("1. Аня"))
        self.assertIsNone(legacy.option_number("Аня"))

    def test_numbering_comes_off_the_text(self):
        self.assertEqual(legacy.clean_option_text("1. Аня — дракон"), "Аня — дракон")
        self.assertEqual(legacy.clean_option_text("№2) Боря"), "Боря")
        self.assertEqual(legacy.clean_option_text("3 Вика"), "Вика")
        self.assertEqual(legacy.clean_option_text("Отряд 40k"), "Отряд 40k")

    def test_name_scores(self):
        self.assertEqual(legacy.name_score("Аня Петрова", "Аня Петрова"), 3)
        self.assertEqual(legacy.name_score("1. Ёлка", "елка"), 3)
        self.assertEqual(legacy.name_score("anya", "Аня", "anya"), 3)
        self.assertEqual(legacy.name_score("Аня", "Аня Петрова"), 2)
        self.assertEqual(legacy.name_score("Петрова — дракон", "Аня Петрова"), 1)
        self.assertEqual(legacy.name_score("Дракон", "Аня Петрова"), 0)
        self.assertEqual(legacy.name_score("", "Аня"), 0)

    def test_a_sunday_poll_decides_its_own_week_and_a_monday_one_the_week_before(self):
        self.assertEqual(legacy.contest_week(datetime(2026, 7, 26, 18)), "2026-W30")   # Sunday
        self.assertEqual(legacy.contest_week(datetime(2026, 7, 25, 18)), "2026-W30")   # Saturday
        self.assertEqual(legacy.contest_week(datetime(2026, 7, 27, 9)), "2026-W30")    # Monday after
        self.assertEqual(legacy.contest_week(datetime(2026, 7, 30, 9)), "2026-W31")    # Thursday

    def test_a_second_part_numbered_on_finds_its_photos_in_its_own_album(self):
        self.assertEqual(legacy.album_offset([1, 2, 3], 3), 0)
        self.assertEqual(legacy.album_offset([11, 12, 13, 14, 15], 5), 10)
        self.assertEqual(legacy.album_offset([11, 12, 13], 15), 0)    # one album for both parts
        self.assertEqual(legacy.album_offset([1, 9], 3), 0)           # does not fit either way

    def test_a_question_with_its_own_hashtag_is_a_thematic_contest(self):
        self.assertEqual(legacy.poll_hashtag("Голосуем #итогинедели"), (voting.CONTEST_HASHTAG, ""))
        self.assertEqual(legacy.poll_hashtag("Лучший #МассПокрас"), ("#масспокрас", "МассПокрас"))
        self.assertEqual(legacy.poll_hashtag("Итоги недели"), (voting.CONTEST_HASHTAG, ""))

    def test_the_same_photo_recompressed_hashes_close_and_another_far(self):
        with tempfile.TemporaryDirectory() as folder:
            _picture(1, (400, 300)).save(Path(folder) / "a.jpg", quality=90)
            _picture(1, (120, 90)).save(Path(folder) / "b.jpg", quality=40)
            _picture(2, (400, 300)).save(Path(folder) / "c.jpg", quality=90)
            hashes = legacy.hash_photos(Path(folder), ["a.jpg", "b.jpg", "c.jpg", "missing.jpg"])
        self.assertEqual(set(hashes), {"a.jpg", "b.jpg", "c.jpg"})
        self.assertLessEqual(legacy._distance(hashes["a.jpg"], hashes["b.jpg"]), legacy.HASH_MATCH_BITS)
        self.assertGreater(legacy._distance(hashes["a.jpg"], hashes["c.jpg"]), legacy.HASH_MATCH_BITS)


class ScanTests(_Store):
    def test_only_contest_polls_are_kept_and_every_picture_is_downloaded(self):
        client, scan = self._scan()
        self.assertEqual(scan["polls_seen"], 4)
        self.assertEqual([p["message_id"] for p in scan["polls"]], [110, 210, 310])
        first = scan["polls"][0]
        self.assertEqual([o["photo"] for o in first["options"]], ["110_o0.jpg", "110_o1.jpg", "110_o2.jpg"])
        self.assertEqual([o["votes"] for o in first["options"]], [7, 3, 5])
        self.assertEqual([o["added_by"] for o in first["options"]], [ANYA, VIKA, GOSHA])
        self.assertEqual(first["total_voters"], 14)
        for name in ("110_o0.jpg", "100_0.jpg", "100_1.jpg", "102_0.jpg", "305_0.jpg", "306_0.jpg"):
            self.assertTrue((legacy.cache_dir(CHAT) / name).is_file(), name)
        # Аня's album came in whole, though only its second photo carries the hashtag.
        anya = next(p for p in scan["posts"] if p["author_id"] == ANYA and p["entry_id"] == "100")
        self.assertEqual(anya["media"], ["100_0.jpg", "100_1.jpg"])
        self.assertEqual(anya["author_name"], "Аня")

    def test_a_numbered_poll_takes_its_pictures_from_the_album_before_it(self):
        _, scan = self._scan()
        numbered = scan["polls"][2]
        self.assertEqual([o["photo"] for o in numbered["options"]], ["305_0.jpg", "306_0.jpg"])

    def test_polls_after_the_automation_are_not_read(self):
        _, scan = self._scan(until=_at("2026-07-20", 0))
        self.assertEqual([p["message_id"] for p in scan["polls"]], [110, 210])

    def test_hidden_results_are_asked_for_again_and_marked_when_still_hidden(self):
        client = _chat()
        client.messages[210] = _poll_message(210, _at("2026-07-19", 18), "Итоги недели",
                                             [_answer(0, "Боря"), _answer(1, "Вика")], [0, 0], visible=False)
        client.messages[310] = _poll_message(310, _at("2026-07-26", 18), "Итоги недели",
                                             [_answer(0, "1"), _answer(1, "2")], [0, 0], visible=False)
        client.fresh_results[310] = tl.PollResults(
            results=[tl.PollAnswerVoters(option=bytes([0]), voters=2), tl.PollAnswerVoters(option=bytes([1]), voters=9)],
            total_voters=11,
        )
        _, scan = self._scan(client)
        by_id = {p["message_id"]: p for p in scan["polls"]}
        self.assertFalse(by_id[210]["visible"])
        self.assertTrue(by_id[310]["visible"])
        self.assertEqual([o["votes"] for o in by_id[310]["options"]], [2, 9])
        self.assertEqual({r.msg_id for r in client.requests}, {210, 310})


class PlanTests(_Store):
    def _contest(self, contests, week):
        return next(c for c in contests if c.week == week)

    def test_open_poll_authors_are_the_people_who_added_their_work(self):
        _, _, contests = self._plan()
        week = self._contest(contests, "2026-W28")
        self.assertEqual(week.status, legacy.READY)
        self.assertEqual([w.entry.author_id for w in week.works], [ANYA, GOSHA, VIKA])
        self.assertEqual([w.votes for w in week.works], [7, 5, 3])
        anya, gosha, vika = week.works
        # Аня's option is the first photo of her album post: the work is the post, whole.
        self.assertEqual((anya.how, anya.entry.entry_id, anya.entry.media), (legacy.HOW_PICTURE, "100", ["100_0.jpg", "100_1.jpg"]))
        self.assertEqual((vika.how, vika.entry.entry_id), (legacy.HOW_PICTURE, "102"))
        # Гоша's picture is nobody's post: the work is the option, by the man who added it.
        self.assertEqual((gosha.how, gosha.entry.entry_id, gosha.entry.media), (legacy.HOW_ADDED, "110o2", ["110_o2.jpg"]))
        self.assertEqual((gosha.entry.author_name, gosha.entry.author_username), ("Гоша", "gosha"))
        self.assertEqual(gosha.entry.text, "Гоша — орк")
        self.assertEqual(week.voters, 14)

    def test_named_options_find_the_post_by_tag_and_by_name(self):
        _, _, contests = self._plan()
        week = self._contest(contests, "2026-W29")
        vika, borya, nobody = week.works
        self.assertEqual((vika.how, vika.entry.entry_id, vika.votes), (legacy.HOW_USERNAME, "201", 6))
        self.assertEqual((borya.how, borya.entry.entry_id, borya.entry.author_id), (legacy.HOW_NAME, "200", BORYA))
        self.assertEqual(nobody.how, legacy.HOW_NONE)
        self.assertEqual((nobody.entry.author_id, nobody.entry.author_name), (None, "Неизвестный Художник"))
        self.assertEqual(nobody.entry.media, [])
        self.assertEqual(nobody.entry.entry_id, "210o2")

    def test_a_numbered_poll_matches_through_the_album_pictures(self):
        _, _, contests = self._plan()
        week = self._contest(contests, "2026-W30")
        self.assertEqual([(w.entry.author_id, w.votes, w.how) for w in week.works],
                         [(GOSHA, 4, legacy.HOW_PICTURE), (ANYA, 3, legacy.HOW_PICTURE)])
        self.assertEqual([w.entry.entry_id for w in week.works], ["300", "301"])

    def test_the_organiser_adding_every_option_is_not_their_author(self):
        polls = [{
            "message_id": 50, "posted_at": "2026-07-12T18:00:00+03:00", "question": "Итоги недели",
            "sender_id": ORGANISER, "closed": True, "closed_at": "", "total_voters": 3, "visible": True,
            "options": [{"index": i, "text": name, "votes": 1, "photo": None, "mentions": [], "usernames": [],
                         "added_by": ORGANISER, "added_at": "", "caption": "", "forwarded_from": None}
                        for i, name in enumerate(["Аня", "Боря", "Вика"])],
        }]
        contests = legacy.plan_contests({"polls": polls, "posts": [], "people": {}}, members=MEMBERS)
        self.assertEqual([(w.entry.author_id, w.how) for w in contests[0].works],
                         [(ANYA, legacy.HOW_MEMBER), (BORYA, legacy.HOW_MEMBER), (VIKA, legacy.HOW_MEMBER)])

    def test_an_ambiguous_name_is_left_unmatched(self):
        polls = [{
            "message_id": 50, "posted_at": "2026-07-12T18:00:00+03:00", "question": "Итоги недели",
            "sender_id": ORGANISER, "closed": True, "closed_at": "", "total_voters": 1, "visible": True,
            "options": [{"index": 0, "text": "Аня", "votes": 1, "photo": None, "mentions": [], "usernames": [],
                         "added_by": None, "added_at": "", "caption": "", "forwarded_from": None},
                        {"index": 1, "text": "Боря", "votes": 0, "photo": None, "mentions": [], "usernames": [],
                         "added_by": None, "added_at": "", "caption": "", "forwarded_from": None}],
        }]
        members = {"1": {"name": "Аня Петрова", "username": None}, "2": {"name": "Аня Смирнова", "username": None}}
        works = legacy.plan_contests({"polls": polls, "posts": [], "people": {}}, members=members)[0].works
        self.assertEqual([w.how for w in works], [legacy.HOW_NONE, legacy.HOW_NONE])

    def test_two_polls_in_one_week_are_one_contest(self):
        client = _chat()
        client.messages[120] = _poll_message(120, _at("2026-07-12", 19), "Итоги недели, часть 2", [
            _answer(0, "Боря"), _answer(1, "Аня Петрова"),
        ], [9, 0], total=9)
        _, _, contests = self._plan(client)
        week = self._contest(contests, "2026-W28")
        self.assertEqual(week.poll_message_ids, [110, 120])
        self.assertEqual([w.votes for w in week.works], [9, 7, 5, 3, 0])
        self.assertEqual(week.voters, 14)

    def test_a_second_poll_that_names_no_contest_is_still_part_of_it(self):
        client = _chat()
        # Posted five minutes after week 28's poll by the same organiser: "Часть 2", no pictures.
        client.messages[112] = _poll_message(112, _at("2026-07-12", 18) + timedelta(minutes=5), "Часть 2", [
            _answer(0, "Боря"), _answer(1, "Аня Петрова"),
        ], [8, 1], total=9)
        _, scan, contests = self._plan(client)
        self.assertEqual([p["message_id"] for p in scan["polls"]], [110, 112, 210, 310])
        week = self._contest(contests, "2026-W28")
        self.assertEqual(week.poll_message_ids, [110, 112])
        self.assertEqual([(w.entry.author_id, w.votes) for w in week.works][:2], [(BORYA, 8), (ANYA, 7)])
        # The organiser's stream poll the next day is still nobody's contest.
        self.assertNotIn(111, [p["message_id"] for p in scan["polls"]])

    def test_a_numbered_field_split_over_two_polls_and_two_albums(self):
        client = FakeClient([
            _message(400, _at("2026-08-01"), sender=ANYA, text="#итогинедели а", media=_media(21)),
            _message(401, _at("2026-08-01"), sender=BORYA, text="#итогинедели б", media=_media(22)),
            _message(402, _at("2026-08-01"), sender=VIKA, text="#итогинедели в", media=_media(23)),
            _message(403, _at("2026-08-01"), sender=GOSHA, text="#итогинедели г", media=_media(24)),
            _message(410, _at("2026-08-02", 17), media=_media(21, (90, 70)), grouped_id=1),
            _message(411, _at("2026-08-02", 17), media=_media(22, (90, 70)), grouped_id=1),
            _poll_message(412, _at("2026-08-02", 18), "Итоги недели, часть 1", [_answer(0, "1"), _answer(1, "2")], [1, 2]),
            _message(420, _at("2026-08-02", 18), media=_media(23, (90, 70)), grouped_id=2),
            _message(421, _at("2026-08-02", 18), media=_media(24, (90, 70)), grouped_id=2),
            _poll_message(422, _at("2026-08-02", 19), "Часть 2", [_answer(0, "3"), _answer(1, "4")], [5, 0]),
        ])
        _, _, contests = self._plan(client)
        self.assertEqual(len(contests), 1)
        self.assertEqual(contests[0].poll_message_ids, [412, 422])
        self.assertEqual([(w.entry.author_id, w.votes, w.how) for w in contests[0].works],
                         [(VIKA, 5, legacy.HOW_PICTURE), (BORYA, 2, legacy.HOW_PICTURE),
                          (ANYA, 1, legacy.HOW_PICTURE), (GOSHA, 0, legacy.HOW_PICTURE)])

    def test_a_week_already_recorded_or_the_bots_own_is_not_written_again(self):
        voting.save_imported_results(CHAT, "2026-W29", [], created_at="", announced_at="", voters=0,
                                     extra={"source": legacy.SOURCE})
        poll = voting.Poll(poll_id="2026-W30", entry=CHAT, created_at="2026-07-26T10:00:00+00:00")
        voting.save_poll(poll)
        known, first = legacy.known_contests(CHAT)
        self.assertIn("2026-W29", known)
        self.assertEqual(first, "2026-W30")   # an imported week never moves the boundary
        _, _, contests = self._plan()
        self.assertEqual({c.week: c.status for c in contests},
                         {"2026-W28": legacy.READY, "2026-W29": legacy.KNOWN, "2026-W30": legacy.AUTOMATED})

    def test_hidden_results_are_not_written(self):
        client = _chat()
        client.messages[210] = _poll_message(210, _at("2026-07-19", 18), "Итоги недели",
                                             [_answer(0, "Боря"), _answer(1, "Вика")], [0, 0], visible=False)
        _, _, contests = self._plan(client)
        self.assertEqual(self._contest(contests, "2026-W29").status, legacy.HIDDEN)

    def test_the_preview_names_the_podium_and_what_will_happen(self):
        client = _chat()
        client.messages[210] = _poll_message(210, _at("2026-07-19", 18), "Итоги недели",
                                             [_answer(0, "Боря"), _answer(1, "Вика")], [0, 0], visible=False)
        _, scan, contests = self._plan(client)
        text = "\n".join(legacy.preview_messages(contests, scan["polls_seen"]))
        self.assertIn("Опросов в чате до автоматизации: 4, из них конкурсов: 3.", text)
        self.assertIn("Запишу в Доску почёта: 2.", text)
        self.assertIn("🥇 Аня (@anya) — 7 голосов", text)
        self.assertIn("Telegram не показывает итоги", text)
        self.assertIn("узнал авторов: 3 из 3", text)

    def test_a_long_preview_is_split_inside_telegrams_limit(self):
        contest = legacy.PlannedContest(
            poll_id="2026-W01", week="2026-W01", hashtag=voting.CONTEST_HASHTAG, title="",
            poll_message_ids=[1], created_at="2026-01-04T18:00:00+03:00", closed_at="", voters=1,
            status=legacy.READY,
        )
        messages = legacy.preview_messages([contest] * 80, 80)
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(m) <= legacy.MESSAGE_BUDGET for m in messages))


class ApplyTests(_Store):
    def test_contests_land_in_the_results_the_hall_and_the_weekly_winners(self):
        self._plan()
        result = legacy.apply_plan(CHAT, giver_id=ADMIN["id"], giver_name="@admin", log=lambda *_: None)
        self.assertEqual(result["written"], ["2026-W28", "2026-W29", "2026-W30"])
        self.assertEqual(result["works"], 8)

        record = voting.load_results(CHAT, "2026-W28")
        self.assertEqual(record["source"], legacy.SOURCE)
        self.assertEqual(record["poll_message_ids"], [110])
        self.assertEqual(record["voters"], 14)
        self.assertEqual([row["author_id"] for row in record["standings"]], [ANYA, GOSHA, VIKA])
        self.assertIn("Результаты недельного голосования:\n1. Аня (@anya) — 7 голосов", record["text"])
        self.assertTrue(record["announced_at"].endswith("+00:00"))

        contest = hall_of_fame.load_contest(CHAT, "2026-W28")
        self.assertEqual([w.author_id for w in contest.works], [ANYA, GOSHA, VIKA])
        self.assertEqual(contest.works[0].photos, ["100_0.jpg", "100_1.jpg"])
        self.assertIsNotNone(contest.works[0].thumb)
        self.assertTrue((hall_of_fame.media_dir(CHAT, "2026-W28") / "110_o2.jpg").is_file())
        self.assertTrue((voting.archived_media_path(CHAT, "2026-W28") / "110_o2.jpg").is_file())
        self.assertEqual(contest.voters, 14)

        winners = stats.weekly_contest_winners(CHAT)
        self.assertEqual({week: w["user_id"] for week, w in winners.items()},
                         {1: str(ANYA), 2: str(VIKA), 3: str(GOSHA)})
        self.assertEqual(result["winners"]["awarded"], [1, 2, 3])
        self.assertEqual(result["winners"]["next"], 4)
        self.assertEqual(stats.weekly_wins_for_user(CHAT, ANYA), 1)

    def test_the_report_lists_every_new_winner(self):
        client = _chat()
        client.messages[112] = _poll_message(112, _at("2026-07-12", 18) + timedelta(minutes=5), "Часть 2", [
            _answer(0, "Боря"), _answer(1, "Аня Петрова"),
        ], [8, 1], total=9)
        self._plan(client)
        report = legacy.apply_report(legacy.apply_plan(CHAT, log=lambda *_: None))
        self.assertIn("Победители:", report)
        self.assertIn("• 2026-W28 · 12.07.2026 (2 опроса, 5 работ) — 🥇 Боря — 8 голосов · №1", report)
        self.assertIn("• 2026-W29 · 19.07.2026 — 🥇 Вика (@vika) — 6 голосов · №2", report)
        self.assertIn("• 2026-W30 · 26.07.2026 — 🥇 Гоша (@gosha) — 4 голоса · №3", report)

    def test_running_it_again_changes_nothing(self):
        self._plan()
        legacy.apply_plan(CHAT, log=lambda *_: None)
        before = voting.results_path(CHAT, "2026-W28").read_text(encoding="utf-8")
        _, _, contests = self._plan()
        self.assertEqual({c.status for c in contests}, {legacy.KNOWN})
        result = legacy.apply_plan(CHAT, log=lambda *_: None)
        self.assertEqual(result["written"], [])
        self.assertEqual(voting.results_path(CHAT, "2026-W28").read_text(encoding="utf-8"), before)
        self.assertIn("Нечего записывать", legacy.apply_report(result))

    def test_an_announced_week_is_never_overwritten_by_an_import(self):
        poll = voting.Poll(poll_id="2026-W28", entry=CHAT, created_at="t",
                           entries=[voting.Entry("9", 9, 99, "Своя", None, "", ["9_0.jpg"])])
        voting.set_approved(poll, ["9"])
        voting.save_results(poll, poll.tally(), "объявлено ботом")
        self.assertIsNone(voting.save_imported_results(CHAT, "2026-W28", [], created_at="", announced_at="",
                                                       voters=0))
        self.assertEqual(voting.load_results(CHAT, "2026-W28")["text"], "объявлено ботом")

    def test_a_weekly_table_numbered_another_way_is_left_alone(self):
        stats.record_weekly_contest_winner(CHAT, 1, BORYA, "Боря", ADMIN["id"], "@admin")
        self._plan()
        before = stats._weekly_contest_path(CHAT).read_text(encoding="utf-8")
        result = legacy.apply_plan(CHAT, log=lambda *_: None)
        self.assertEqual(result["winners"]["status"], "mismatch")
        self.assertEqual(stats._weekly_contest_path(CHAT).read_text(encoding="utf-8"), before)
        self.assertIn("не трогал", legacy.apply_report(result))
        self.assertIsNotNone(hall_of_fame.load_contest(CHAT, "2026-W28"))

    def test_a_table_that_agrees_is_filled_in_with_one_read_pair_and_one_write(self):
        stats.record_weekly_contest_winner(CHAT, 1, ANYA, "Аня", ADMIN["id"], "@admin")
        self._plan()
        loads, writes = [], []
        real_load, real_write = stats._load_weekly_contest_data, stats._write_json_atomic
        with patch.object(stats, "_load_weekly_contest_data", side_effect=lambda e: loads.append(e) or real_load(e)), \
                patch.object(stats, "_write_json_atomic", side_effect=lambda *a: writes.append(a[0]) or real_write(*a)):
            result = legacy.apply_plan(CHAT, log=lambda *_: None)
        self.assertEqual(result["winners"]["awarded"], [2, 3])
        self.assertEqual(len(loads), 2)    # the check, then the batch -- not one per week
        self.assertEqual(len(writes), 1)

    def test_a_winner_without_an_account_is_not_put_in_the_table(self):
        client = _chat()
        client.messages[210] = _poll_message(210, _at("2026-07-19", 18), "Итоги недели",
                                             [_answer(0, "Неизвестный Художник"), _answer(1, "Боря")], [5, 1])
        self._plan(client)
        result = legacy.apply_plan(CHAT, log=lambda *_: None)
        self.assertEqual(result["winners"]["awarded"], [1, 3])
        self.assertEqual(result["winners"]["unnamed"], 1)
        self.assertEqual(hall_of_fame.load_contest(CHAT, "2026-W29").winner().author_name, "Неизвестный Художник")

    def test_without_a_plan_it_says_to_scan_first(self):
        self.assertIn("/hall опросы", legacy.apply_report(legacy.apply_plan(CHAT)))

    def test_a_lost_option_picture_comes_back_from_the_poll(self):
        client = _chat()
        names = asyncio.run(legacy.download_option_photo(client, "chat", 110, 2, self.root / "back"))
        self.assertEqual(names, ["110_o2.jpg"])
        self.assertTrue((self.root / "back" / "110_o2.jpg").is_file())
        self.assertEqual(asyncio.run(legacy.download_option_photo(client, "chat", 210, 0, self.root / "back")), [])
        self.assertIsNone(asyncio.run(legacy.download_option_photo(client, "chat", 999, 0, self.root / "back")))


class FakeApi:
    def __init__(self):
        self.sent = []
        self.edits = []

    async def send_message(self, chat_id, text, reply_to_message_id=None, reply_markup=None,
                           parse_mode=None, disable_notification=False):
        self.sent.append((text, reply_markup))
        return {"message_id": 100 + len(self.sent)}

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None, parse_mode=None):
        self.edits.append(text)

    async def answer_callback_query(self, callback_id, text=None):
        pass


def _cfg():
    return SimpleNamespace(webapp_public_url="https://example.com", vote_miniapp_short_name=None,
                           vote_announce_extra_chat=None, backup_chat_id=None)


class CommandTests(_Store):
    def _type(self, text, client=None, user=ADMIN, chat_type="private"):
        api = FakeApi()
        message = {"message_id": 1, "chat": {"id": 5 if chat_type == "private" else -100, "type": chat_type},
                   "from": user, "text": text}

        async def resolve_id(*args, **kwargs):
            return -100

        async def can_manage(api_, chat_id_, user_, entry=None):
            return user_.get("id") == ADMIN["id"]

        async def resolve(client_, entry):
            return "chat"

        async def scenario():
            with patch.object(bot_listener, "_resolve_chat_id", resolve_id), \
                    patch.object(bot_listener, "_can_manage_chat", can_manage), \
                    patch.object(bot_listener, "resolve_chat", resolve), \
                    patch.object(bot_listener, "_legacy_members", lambda entry: MEMBERS):
                await bot_listener.handle_hall_command(api, client, _cfg(), message, CHAT, "testbot",
                                                       log=lambda *_: None)

        asyncio.run(scenario())
        return api

    def test_only_an_administrator_in_the_dm(self):
        self.assertIn("только администраторы", self._type("/hall опросы", _chat(), user=STRANGER).sent[0][0])
        self.assertIn("в личке", self._type("/hall опросы", _chat(), chat_type="supergroup").sent[0][0])
        self.assertIsNone(legacy.load_plan(CHAT))

    def test_the_panel_offers_the_old_contests(self):
        buttons = [b for row in self._type("/hall").sent[0][1]["inline_keyboard"] for b in row]
        self.assertIn(f"hallaction:polls:{ADMIN['id']}", [b.get("callback_data") for b in buttons])

    def test_scan_shows_the_plan_then_the_button_writes_it(self):
        api = self._type("/hall опросы", _chat())
        text, markup = api.sent[-1]
        self.assertIn("Запишу в Доску почёта: 3.", text)
        button = markup["inline_keyboard"][0][0]
        self.assertEqual(button["callback_data"], f"hallaction:pollsyes:{ADMIN['id']}")
        self.assertIsNone(hall_of_fame.load_contest(CHAT, "2026-W28"))   # nothing written yet

        api = self._type(bot_listener.HALL_ACTIONS["pollsyes"], _chat())
        self.assertIn("Записано конкурсов: 3", api.sent[-1][0])
        self.assertIn("записаны №1, №2, №3", api.sent[-1][0])
        self.assertEqual(api.sent[-1][1]["inline_keyboard"][0][0]["url"], "https://example.com/hall")
        self.assertIsNotNone(hall_of_fame.load_contest(CHAT, "2026-W30"))

    def test_the_hall_import_fetches_a_lost_option_picture_back_from_the_poll(self):
        self._plan()
        legacy.apply_plan(CHAT, log=lambda *_: None)
        for path in hall_of_fame.media_dir(CHAT, "2026-W28").glob("*110_o2.jpg"):
            path.unlink()
        contest = hall_of_fame.load_contest(CHAT, "2026-W28")
        for work in contest.works:
            if work.entry_id == "110o2":
                work.photos, work.thumb = [], None
        hall_of_fame.save_contest(contest)

        async def resolve(client_, entry):
            return "chat"

        with patch.object(bot_listener, "resolve_chat", resolve):
            restored, gone = asyncio.run(bot_listener._restore_hall_photos(_chat(), CHAT, log=lambda *_: None))
        self.assertEqual(restored, 1)
        work = next(w for w in hall_of_fame.load_contest(CHAT, "2026-W28").works if w.entry_id == "110o2")
        self.assertEqual(work.photos, ["110_o2.jpg"])


class SearchTests(_Store):
    def test_the_search_brings_in_the_whole_album(self):
        client = _chat()
        entries = asyncio.run(voting.search_entries(
            client, "chat", TZ, self.root / "media", _at("2026-07-06", 0), _at("2026-07-11", 0), log=lambda *_: None,
        ))
        self.assertEqual([(e.entry_id, e.media) for e in entries],
                         [("102", ["102_0.jpg"]), ("100", ["100_0.jpg", "100_1.jpg"])])
        self.assertEqual(entries[1].text, "дракон")


if __name__ == "__main__":
    unittest.main()
