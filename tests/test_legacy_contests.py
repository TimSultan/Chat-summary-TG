"""legacy_contests.py -- the poll contests from before the bot ran the vote.

Before /vote, the weekly contest was a Telegram poll -- often two or three -- posted by the
channel into its discussion chat, the works' pictures on its options or in the album
right before it, the authors credited by captions, @tags or t.me links. These tests drive
the whole path against a fake chat built the way the real one turned out to be: copies of
one poll, a question poll beside a contest, the channel's own reposts of the works, an
option nobody can be matched to. Nothing may be credited to the channel, a copy may not
double a field, and a week with an unknown author waits for an administrator to name it.
"""

import asyncio
import itertools
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
ANYA, BORYA, VIKA, GOSHA = 11, 12, 13, 14
CHANNEL, CHANNEL_PEER = 777, -1000000000777
ADMIN = {"id": 7, "username": "admin"}
STRANGER = {"id": 8, "username": "someone"}
_photo_ids = itertools.count(1000)


def _at(day: str, hour: int = 12, minute: int = 0) -> datetime:
    """A message time: local `day` at `hour`, as Telegram gives it -- in UTC."""
    return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00").replace(tzinfo=TZ).astimezone(timezone.utc)


def _picture(seed: int, size=(160, 120)) -> Image.Image:
    """A distinct picture per seed: a coarse random grid, scaled up. The same seed at
    another size and JPEG quality is the same photo recompressed, which is all Telegram
    does to a work posted once and put on a poll option again."""
    rng = random.Random(seed)
    grid = Image.new("L", (12, 9))
    grid.putdata([rng.randrange(256) for _ in range(12 * 9)])
    return grid.resize(size, Image.BICUBIC).convert("RGB")


def _media(seed, size=(160, 120), photo_id=None):
    return SimpleNamespace(photo=SimpleNamespace(id=photo_id or next(_photo_ids)), seed=seed, size=size)


def _user(user_id, first, username=None):
    return tl.User(id=user_id, first_name=first, username=username)


ENTITIES = {
    ANYA: _user(ANYA, "Аня", "anya"),
    BORYA: _user(BORYA, "Боря", None),
    VIKA: _user(VIKA, "Вика", "vika"),
    GOSHA: _user(GOSHA, "Гоша", "gosha"),
    CHANNEL_PEER: SimpleNamespace(id=CHANNEL, title="Единая Папка Художников | ЕПХ", username="papkahudojnicov"),
    "persykart": SimpleNamespace(id=888, title="Persyk Art", username="persykart"),
}


def _message(message_id, when, *, sender=CHANNEL_PEER, text="", media=None, grouped_id=None,
             forwarded_from=None, entities=None):
    async def get_sender():
        return ENTITIES.get(sender)

    fwd = SimpleNamespace(from_id=SimpleNamespace(user_id=forwarded_from), from_name=None) if forwarded_from else None
    return SimpleNamespace(
        id=message_id, date=when, text=text, media=media, sender_id=sender, action=None,
        photo=getattr(media, "photo", None), grouped_id=grouped_id, fwd_from=fwd,
        entities=entities or [], get_sender=get_sender,
    )


def _answer(index, text, *, seed=None, added_by=None, mention=None):
    entities = [SimpleNamespace(user_id=mention)] if mention else []
    return SimpleNamespace(
        text=SimpleNamespace(text=text, entities=entities), option=bytes([index]),
        media=_media(seed, (240, 180)) if seed is not None else None,
        added_by=SimpleNamespace(user_id=added_by) if added_by else None,
        date=None,
    )


def _poll_message(message_id, when, question, answers, votes, *, total=None, visible=True, poll_id=None,
                  sender=CHANNEL_PEER):
    results = SimpleNamespace(
        results=[SimpleNamespace(option=bytes([i]), voters=(v if visible else None)) for i, v in enumerate(votes)],
        total_voters=total if total is not None else sum(votes),
    )
    poll = SimpleNamespace(id=poll_id or message_id * 10, question=SimpleNamespace(text=question, entities=[]),
                           answers=answers, quiz=False, closed=True, close_date=None)
    return _message(message_id, when, sender=sender, media=SimpleNamespace(poll=poll, results=results))


class FakeClient:
    """The chat, as legacy_contests reads it: a poll listing, a hashtag search, messages by
    id and before an id, downloads that write real pictures, entities by id and username."""

    def __init__(self, messages, fresh_results=None):
        self.messages = {m.id: m for m in messages}
        self.fresh_results = fresh_results or {}
        self.requests = []
        self.lookups = []

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
        _picture(media.seed, media.size).save(file, "JPEG", quality=70)
        return file

    async def get_entity(self, key):
        self.lookups.append(key)
        if isinstance(key, str):
            for entity in ENTITIES.values():
                if (getattr(entity, "username", None) or "").lower() == key.lower():
                    return entity
            raise ValueError("no such username")
        if key in ENTITIES:
            return ENTITIES[key]
        raise ValueError("unknown id")

    async def __call__(self, request):
        self.requests.append(request)
        results = self.fresh_results.get(request.msg_id)
        return SimpleNamespace(updates=[tl.UpdateMessagePoll(poll_id=1, results=results)] if results else [])


def _chat():
    """Three contest weeks and the chat around them, the polls all posted by the channel.

    W27 (Friday 3 July): a numbered poll, "1".."3", its pictures the channel's album right
    before it -- the first credited "1. @anya", the second forwarded from Боря, the third
    captioned "Вика" -- and the same poll again later (a copy: one poll id).
    W28 (Sunday 12 July): pictures on the options. Аня's and Вика's are their own
    #итогинедели posts (the channel reposted Аня's too); Гоша's is in the channel's
    #итогинедели repost crediting @gosha; the fourth is nobody's.
    W29 (Monday 20 July): the authors as t.me links -- a member, a member's handle and an
    artist's own channel -- and ten minutes later the channel's question about the rules.
    Plus a question poll whose question mentions the contest.
    """
    return FakeClient([
        # W27
        _message(50, _at("2026-07-03", 17), media=_media(31), grouped_id=900, text="1. @anya"),
        _message(51, _at("2026-07-03", 17), media=_media(32), grouped_id=900, forwarded_from=BORYA),
        _message(52, _at("2026-07-03", 17), media=_media(33), grouped_id=900, text="Вика"),
        _message(53, _at("2026-07-03", 17), sender=BORYA, text="ого"),
        _message(54, _at("2026-07-03", 17), text="Голосуем!"),
        _poll_message(60, _at("2026-07-03", 18), "Итоги недели", [
            _answer(0, "1"), _answer(1, "2"), _answer(2, "3️⃣"),
        ], [5, 9, 2], total=16, poll_id=6000),
        _message(61, _at("2026-07-03", 19), sender=BORYA, text="проголосовал"),
        _poll_message(62, _at("2026-07-03", 20), "Итоги недели", [
            _answer(0, "1"), _answer(1, "2"), _answer(2, "3️⃣"),
        ], [5, 9, 2], total=16, poll_id=6000),
        # W28
        _message(100, _at("2026-07-08"), sender=ANYA, media=_media(1), grouped_id=500),
        _message(101, _at("2026-07-08"), sender=ANYA, text="#итогинедели дракон", media=_media(2), grouped_id=500),
        _message(102, _at("2026-07-09"), sender=VIKA, text="Рыцарь #итогинедели", media=_media(3)),
        _message(105, _at("2026-07-10"), text="#итогинедели орк от @gosha", media=_media(9)),
        _message(106, _at("2026-07-10"), text="#итогинедели лучшее за неделю", media=_media(1, (200, 150))),
        _poll_message(110, _at("2026-07-12", 18), "Итоги недели", [
            _answer(0, "1", seed=1), _answer(1, "2", seed=3), _answer(2, "3", seed=9), _answer(3, "4", seed=10),
        ], [6, 2, 8, 1]),
        # W29
        _poll_message(200, _at("2026-07-20", 18), "Итоги недели", [
            _answer(0, "https://t.me/anya"), _answer(1, "t.me/persykart"), _answer(2, "https://telegram.me/vika"),
        ], [3, 1, 7]),
        _poll_message(201, _at("2026-07-20", 18, 10), "Какие работы берём?", [
            _answer(0, "Закончены только на этой неделе"), _answer(1, "Старые тоже ОК"),
        ], [10, 30]),
        _poll_message(250, _at("2026-07-24"), "Итоги недели: делать промежуточные?", [
            _answer(0, "Да, делай промежуточные, интересен процесс"), _answer(1, "Нет, лучше готовую работу"),
        ], [12, 15]),
    ])


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
                                            members=MEMBERS, log=lambda *_: None))
        return client, scan

    def _plan(self, client=None, until=None):
        client, scan = self._scan(client, until)
        return client, scan, legacy.build_plan(CHAT, scan, MEMBERS)

    def _contest(self, contests, week):
        return next(c for c in contests if c.week == week)

    def _name_the_stranger(self):
        """Names the one W28 work nobody could be matched to: Боря's."""
        return legacy.set_author(CHAT, "110.3", {"id": BORYA, "name": "Боря", "username": None})


class TextTests(unittest.TestCase):
    def test_numbered_options(self):
        self.assertEqual(legacy.option_number("1"), 1)
        self.assertEqual(legacy.option_number("№ 2"), 2)
        self.assertEqual(legacy.option_number("3️⃣"), 3)
        self.assertEqual(legacy.option_number("Работа 4."), 4)
        self.assertIsNone(legacy.option_number("1. Аня"))
        self.assertIsNone(legacy.option_number("Аня"))

    def test_numbering_and_links_come_off_the_text(self):
        self.assertEqual(legacy.clean_option_text("1. Аня — дракон"), "Аня — дракон")
        self.assertEqual(legacy.clean_option_text("№2) Боря"), "Боря")
        self.assertEqual(legacy.clean_option_text("3 Вика"), "Вика")
        self.assertEqual(legacy.clean_option_text("Отряд 40k"), "Отряд 40k")
        self.assertEqual(legacy.clean_option_text("https://t.me/viva_la_liia"), "")

    def test_tags_and_links_name_usernames(self):
        self.assertEqual(legacy.usernames_in("https://t.me/viva_la_liia"), ["viva_la_liia"])
        self.assertEqual(legacy.usernames_in("telegram.me/ChuArt_Pokras и @anya"), ["anya", "ChuArt_Pokras"])
        self.assertEqual(legacy.usernames_in("t.me/joinchat/abcdef"), [])
        self.assertEqual(legacy.usernames_in("почта a@mail.com"), [])

    def test_a_numbered_list_of_authors(self):
        self.assertEqual(legacy.credit_lines("Работы:\n1. @anya\n2) Боря — танк\nспасибо"),
                         {1: "@anya", 2: "Боря — танк"})

    def test_name_scores(self):
        self.assertEqual(legacy.name_score("Аня Петрова", "Аня Петрова"), 3)
        self.assertEqual(legacy.name_score("1. Ёлка", "елка"), 3)
        self.assertEqual(legacy.name_score("anya", "Аня", "anya"), 3)
        self.assertEqual(legacy.name_score("Аня", "Аня Петрова"), 2)
        self.assertEqual(legacy.name_score("Петрова — дракон", "Аня Петрова"), 1)
        self.assertEqual(legacy.name_score("Дракон", "Аня Петрова"), 0)
        self.assertEqual(legacy.name_score("", "Аня"), 0)

    def test_a_poll_early_in_the_week_decides_the_week_before(self):
        self.assertEqual(legacy.contest_week(datetime(2026, 7, 26, 18)), "2026-W30")   # Sunday
        self.assertEqual(legacy.contest_week(datetime(2026, 7, 27, 9)), "2026-W30")    # Monday after
        self.assertEqual(legacy.contest_week(datetime(2026, 7, 30, 9)), "2026-W31")    # Thursday

    def test_a_question_with_its_own_hashtag_is_a_thematic_contest(self):
        self.assertEqual(legacy.poll_hashtag("Голосуем #итогинедели"), (voting.CONTEST_HASHTAG, ""))
        self.assertEqual(legacy.poll_hashtag("Лучший #МассПокрас"), ("#масспокрас", "МассПокрас"))

    def test_a_later_part_numbered_on_finds_its_photos_in_its_own_album(self):
        self.assertEqual(legacy.album_offset([1, 2, 3], 3), 0)
        self.assertEqual(legacy.album_offset([11, 12, 13, 14, 15], 5), 10)
        self.assertEqual(legacy.album_offset([11, 12, 13], 15), 0)
        self.assertEqual(legacy.album_offset([1, 9], 3), 0)

    def test_with_every_album_before_the_first_poll_the_second_takes_the_second_album(self):
        def item(message_id, album=None, photo=True, poll=False):
            return {"message_id": message_id, "grouped_id": album, "photo": f"c{message_id}.jpg" if photo else None,
                    "is_poll": poll, "text": ""}

        # Newest first, as the context is read: poll 1, then album 2, then album 1.
        context = [item(30, photo=False, poll=True), item(22, 2), item(21, 2), item(12, 1), item(11, 1)]
        self.assertEqual([i["message_id"] for i in legacy._album_before(context, [1, 2])], [21, 22])
        self.assertEqual([i["message_id"] for i in legacy._album_before(context, [3, 4])], [11, 12, 21, 22])
        self.assertEqual([i["message_id"] for i in legacy._album_before(context[1:], [1, 2])], [11, 12, 21, 22])

    def test_the_same_photo_recompressed_hashes_close_and_another_far(self):
        with tempfile.TemporaryDirectory() as folder:
            _picture(1, (400, 300)).save(Path(folder) / "a.jpg", quality=90)
            _picture(1, (120, 90)).save(Path(folder) / "b.jpg", quality=40)
            _picture(2, (400, 300)).save(Path(folder) / "c.jpg", quality=90)
            hashes = legacy.hash_photos(Path(folder), ["a.jpg", "b.jpg", "c.jpg", "missing.jpg"])
        self.assertEqual(set(hashes), {"a.jpg", "b.jpg", "c.jpg"})
        self.assertLessEqual(legacy._distance(hashes["a.jpg"], hashes["b.jpg"]), legacy.HASH_MATCH_BITS // 2)
        self.assertGreater(legacy._distance(hashes["a.jpg"], hashes["c.jpg"]), 3 * legacy.HASH_MATCH_BITS // 2)


class ScanTests(_Store):
    def test_a_copy_of_a_poll_and_question_polls_are_not_contests(self):
        _, scan = self._scan()
        self.assertEqual(scan["polls_seen"], 6)
        self.assertEqual(scan["copies"], 1)
        self.assertEqual(scan["questions"], 2)
        self.assertEqual([p["message_id"] for p in scan["polls"]], [60, 110, 200])

    def test_option_pictures_the_album_and_the_posts_are_all_downloaded(self):
        _, scan = self._scan()
        numbered, pictured, _ = scan["polls"]
        self.assertEqual([o["photo"] for o in numbered["options"]], ["c50.jpg", "c51.jpg", "c52.jpg"])
        self.assertEqual(numbered["options"][1]["forwarded_from"], BORYA)
        self.assertEqual(numbered["options"][0]["caption"], "1. @anya")
        self.assertEqual([o["photo"] for o in pictured["options"]], [f"110_o{i}.jpg" for i in range(4)])
        for name in ("c50.jpg", "110_o3.jpg", "100_0.jpg", "100_1.jpg", "102_0.jpg", "105_0.jpg"):
            self.assertTrue((legacy.cache_dir(CHAT) / name).is_file(), name)
        anya = next(p for p in scan["posts"] if p["entry"]["entry_id"] == "100")
        self.assertEqual((anya["is_user"], anya["entry"]["media"]), (True, ["100_0.jpg", "100_1.jpg"]))
        reposts = [p for p in scan["posts"] if not p["is_user"]]
        self.assertEqual(sorted(p["entry"]["entry_id"] for p in reposts), ["105", "106"])

    def test_links_nobody_in_the_chat_uses_are_looked_up(self):
        client, scan = self._scan()
        self.assertEqual(scan["usernames"], {"persykart": {"name": "Persyk Art", "username": "persykart",
                                                           "is_user": False, "id": None}})
        self.assertNotIn("anya", client.lookups)   # a member's handle needs no lookup

    def test_polls_after_the_automation_are_not_read(self):
        _, scan = self._scan(until=_at("2026-07-13", 0))
        self.assertEqual([p["message_id"] for p in scan["polls"]], [60, 110])

    def test_hidden_results_are_asked_for_again_and_marked_when_still_hidden(self):
        client = _chat()
        client.messages[200] = _poll_message(200, _at("2026-07-20", 18), "Итоги недели",
                                             [_answer(0, "@anya"), _answer(1, "@vika")], [0, 0], visible=False)
        client.messages[110] = _poll_message(110, _at("2026-07-12", 18), "Итоги недели",
                                             [_answer(0, "1", seed=1), _answer(1, "2", seed=3)], [0, 0], visible=False)
        client.fresh_results[110] = tl.PollResults(
            results=[tl.PollAnswerVoters(option=bytes([0]), voters=2), tl.PollAnswerVoters(option=bytes([1]), voters=9)],
            total_voters=11,
        )
        _, scan = self._scan(client)
        by_id = {p["message_id"]: p for p in scan["polls"]}
        self.assertFalse(by_id[200]["visible"])
        self.assertEqual([o["votes"] for o in by_id[110]["options"]], [2, 9])
        self.assertEqual({r.msg_id for r in client.requests}, {110, 200})


class PlanTests(_Store):
    def test_a_numbered_poll_is_credited_through_its_album(self):
        _, _, contests = self._plan()
        week = self._contest(contests, "2026-W27")
        self.assertEqual(week.status, legacy.READY)
        self.assertEqual(week.poll_message_ids, [60])   # the copy is not a second poll
        self.assertEqual([(w.entry.author_id, w.votes) for w in week.works], [(BORYA, 9), (ANYA, 5), (VIKA, 2)])
        self.assertEqual({w.how for w in week.works}, {legacy.HOW_CREDIT})
        self.assertEqual(week.voters, 16)

    def test_pictures_find_the_artists_posts_and_the_channel_is_never_an_author(self):
        _, _, contests = self._plan()
        week = self._contest(contests, "2026-W28")
        by_votes = {w.votes: w for w in week.works}
        gosha, anya, vika, stranger = by_votes[8], by_votes[6], by_votes[2], by_votes[1]
        self.assertEqual((anya.how, anya.entry.author_id, anya.entry.entry_id, anya.entry.media),
                         (legacy.HOW_PICTURE, ANYA, "100", ["100_0.jpg", "100_1.jpg"]))
        self.assertEqual((vika.how, vika.entry.entry_id), (legacy.HOW_PICTURE, "102"))
        # Гоша's work is in the channel's repost: its caption credits him, not the channel.
        self.assertEqual((gosha.how, gosha.entry.author_id, gosha.entry.author_username),
                         (legacy.HOW_CREDIT, GOSHA, "gosha"))
        self.assertEqual(stranger.how, legacy.HOW_NONE)
        self.assertNotIn(CHANNEL, [w.entry.author_id for w in week.works])
        self.assertEqual(week.status, legacy.INCOMPLETE)

    def test_links_name_members_and_an_artists_own_channel(self):
        _, _, contests = self._plan()
        week = self._contest(contests, "2026-W29")
        self.assertEqual(week.poll_message_ids, [200])   # the rules question is not a part of it
        self.assertEqual([(w.entry.author_id, w.entry.author_name, w.votes) for w in week.works],
                         [(VIKA, "Вика", 7), (ANYA, "Аня", 3), (None, "Persyk Art", 1)])
        self.assertEqual({w.how for w in week.works}, {legacy.HOW_USERNAME})
        self.assertEqual(week.status, legacy.READY)

    def test_naming_the_stranger_completes_the_week_and_survives_a_rescan(self):
        self._plan()
        contest, work = self._name_the_stranger()
        self.assertEqual((contest.status, work.how, work.entry.author_id), (legacy.READY, legacy.HOW_MANUAL, BORYA))
        _, _, contests = self._plan()
        week = self._contest(contests, "2026-W28")
        self.assertEqual(week.status, legacy.READY)
        self.assertIn((BORYA, legacy.HOW_MANUAL), [(w.entry.author_id, w.how) for w in week.works])

    def test_the_organiser_adding_every_option_is_not_their_author(self):
        def option(i, name):
            return {"index": i, "text": name, "number": None, "votes": 1, "photo": None, "photo_id": None,
                    "mentions": [], "usernames": [], "added_by": 1, "added_at": "", "caption": "",
                    "caption_mentions": [], "forwarded_from": None}

        polls = [{"message_id": 50, "posted_at": "2026-07-12T18:00:00+03:00", "question": "Итоги недели",
                  "sender_id": 1, "closed": True, "closed_at": "", "total_voters": 3, "visible": True,
                  "options": [option(i, n) for i, n in enumerate(["Аня", "Боря", "Вика"])],
                  "credits": {}, "reposts": []}]
        contests = legacy.plan_contests({"polls": polls, "posts": []}, members=MEMBERS)
        self.assertEqual([(w.entry.author_id, w.how) for w in contests[0].works],
                         [(ANYA, legacy.HOW_MEMBER), (BORYA, legacy.HOW_MEMBER), (VIKA, legacy.HOW_MEMBER)])

    def test_an_ambiguous_name_is_nobody(self):
        polls = [{"message_id": 50, "posted_at": "2026-07-12T18:00:00+03:00", "question": "Итоги недели",
                  "sender_id": 1, "closed": True, "closed_at": "", "total_voters": 1, "visible": True,
                  "options": [{"index": 0, "text": "Аня", "number": None, "votes": 1, "photo": None,
                               "photo_id": None, "mentions": [], "usernames": [], "added_by": None, "added_at": "",
                               "caption": "", "caption_mentions": [], "forwarded_from": None}],
                  "credits": {}, "reposts": []}]
        members = {"1": {"name": "Аня Петрова", "username": None}, "2": {"name": "Аня Смирнова", "username": None}}
        contest = legacy.plan_contests({"polls": polls, "posts": []}, members=members)[0]
        self.assertEqual((contest.works[0].how, contest.status), (legacy.HOW_NONE, legacy.INCOMPLETE))

    def test_a_second_part_posted_right_after_is_the_same_contest(self):
        client = _chat()
        client.messages[112] = _poll_message(112, _at("2026-07-12", 18, 5), "Часть 2", [
            _answer(0, "@vika"), _answer(1, "@gosha"),
        ], [3, 0])
        _, _, contests = self._plan(client)
        week = self._contest(contests, "2026-W28")
        self.assertEqual(week.poll_message_ids, [110, 112])
        self.assertEqual(len(week.works), 6)

    def test_a_week_already_recorded_or_the_bots_own_is_not_written_again(self):
        voting.save_imported_results(CHAT, "2026-W28", [], created_at="", announced_at="", voters=0,
                                     extra={"source": legacy.SOURCE})
        voting.save_poll(voting.Poll(poll_id="2026-W29", entry=CHAT, created_at="2026-07-26T10:00:00+00:00"))
        known, first = legacy.known_contests(CHAT)
        self.assertIn("2026-W28", known)
        self.assertEqual(first, "2026-W29")   # an imported week never moves the boundary
        _, _, contests = self._plan()
        self.assertEqual({c.week: c.status for c in contests},
                         {"2026-W27": legacy.READY, "2026-W28": legacy.KNOWN, "2026-W29": legacy.AUTOMATED})

    def test_hidden_results_are_not_written(self):
        client = _chat()
        client.messages[200] = _poll_message(200, _at("2026-07-20", 18), "Итоги недели",
                                             [_answer(0, "@anya"), _answer(1, "@vika")], [0, 0], visible=False)
        _, _, contests = self._plan(client)
        self.assertEqual(self._contest(contests, "2026-W29").status, legacy.HIDDEN)

    def test_the_preview_says_what_was_left_out_and_what_waits(self):
        _, scan, contests = self._plan()
        text = "\n".join(legacy.preview_messages(contests, scan["polls_seen"], scan["copies"], scan["questions"]))
        self.assertIn("Опросов в чате до автоматизации: 6.", text)
        self.assertIn("копий одного и того же опроса — 1, опросов-вопросов, не про работы — 2", text)
        self.assertIn("Конкурсов: 3. Готовы к записи: 2. Не узнал авторов у 1 работы — подпишешь по картинкам.", text)
        self.assertIn("🥇 Гоша (@gosha) — 8 голосов", text)
        self.assertIn("⏳ запишу, когда подпишешь неузнанных", text)
        self.assertNotIn("ЕПХ", text)

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
    def test_only_complete_weeks_are_written_with_their_winners(self):
        self._plan()
        result = legacy.apply_plan(CHAT, giver_id=ADMIN["id"], giver_name="@admin", log=lambda *_: None)
        self.assertEqual(result["written"], ["2026-W27", "2026-W29"])
        self.assertEqual(result["waiting"], 1)
        self.assertIsNone(hall_of_fame.load_contest(CHAT, "2026-W28"))

        record = voting.load_results(CHAT, "2026-W27")
        self.assertEqual((record["source"], record["poll_message_ids"], record["voters"]), (legacy.SOURCE, [60], 16))
        self.assertIn("1. Боря — 9 голосов", record["text"])
        self.assertTrue(record["announced_at"].endswith("+00:00"))
        contest = hall_of_fame.load_contest(CHAT, "2026-W27")
        self.assertEqual([w.author_id for w in contest.works], [BORYA, ANYA, VIKA])
        self.assertEqual(contest.works[0].photos, ["c51.jpg"])
        self.assertIsNotNone(contest.works[0].thumb)
        self.assertTrue((voting.archived_media_path(CHAT, "2026-W27") / "c51.jpg").is_file())

        winners = stats.weekly_contest_winners(CHAT)
        self.assertEqual({week: w["user_id"] for week, w in winners.items()}, {1: str(BORYA), 2: str(VIKA)})
        self.assertEqual(winners[1]["source"], legacy.SOURCE)
        report = legacy.apply_report(result)
        self.assertIn("• 2026-W27 · 03.07.2026 — 🥇 Боря — 9 голосов · №1", report)
        self.assertIn("• 2026-W29 · 20.07.2026 — 🥇 Вика (@vika) — 7 голосов · №2", report)
        self.assertIn("Ждут подписи авторов: 1 неделя.", report)

    def test_naming_the_stranger_then_writing_brings_the_last_week_in(self):
        self._plan()
        self._name_the_stranger()
        result = legacy.apply_plan(CHAT, log=lambda *_: None)
        self.assertEqual(result["written"], ["2026-W27", "2026-W28", "2026-W29"])
        contest = hall_of_fame.load_contest(CHAT, "2026-W28")
        self.assertEqual([(w.author_id, w.votes) for w in contest.works],
                         [(GOSHA, 8), (ANYA, 6), (VIKA, 2), (BORYA, 1)])
        self.assertEqual(contest.works[1].photos, ["100_0.jpg", "100_1.jpg"])
        self.assertEqual({w: r["user_id"] for w, r in stats.weekly_contest_winners(CHAT).items()},
                         {1: str(BORYA), 2: str(GOSHA), 3: str(VIKA)})

    def test_running_it_again_changes_nothing(self):
        self._plan()
        self._name_the_stranger()
        legacy.apply_plan(CHAT, log=lambda *_: None)
        before = voting.results_path(CHAT, "2026-W28").read_text(encoding="utf-8")
        _, _, contests = self._plan()
        self.assertEqual({c.status for c in contests}, {legacy.KNOWN})
        result = legacy.apply_plan(CHAT, log=lambda *_: None)
        self.assertEqual(result["written"], [])
        self.assertEqual(voting.results_path(CHAT, "2026-W28").read_text(encoding="utf-8"), before)
        self.assertIn("Нечего записывать", legacy.apply_report(result))

    def test_undo_takes_back_out_exactly_what_the_import_wrote(self):
        # The bot's own week, its winner assigned by hand: №3 once the two old weeks are in.
        stats.record_weekly_contest_winner(CHAT, 3, ANYA, "Аня", ADMIN["id"], "@admin")
        bots_own = voting.Poll(poll_id="2026-W40", entry=CHAT, created_at="t",
                               entries=[voting.Entry("9", 9, ANYA, "Аня", "anya", "", [])])
        voting.set_approved(bots_own, ["9"])
        voting.record_vote(bots_own, 1, ["9"])
        voting.save_results(bots_own, bots_own.tally(), "объявлено ботом")
        self._plan()
        legacy.apply_plan(CHAT, log=lambda *_: None)
        result = legacy.undo_import(CHAT)
        self.assertEqual(result, {"contests": ["2026-W27", "2026-W29"], "winners": [1, 2]})
        self.assertEqual(voting.results_poll_ids(CHAT), ["2026-W40"])
        self.assertIsNone(hall_of_fame.load_contest(CHAT, "2026-W27"))
        self.assertFalse(hall_of_fame.media_dir(CHAT, "2026-W27").exists())
        self.assertEqual(list(stats.weekly_contest_winners(CHAT)), [3])
        self.assertEqual(len(list(legacy.undone_dir(CHAT).glob("*/2026-W27.json"))), 1)
        _, _, contests = self._plan()
        self.assertEqual(self._contest(contests, "2026-W27").status, legacy.READY)

    def test_undo_finds_the_winners_of_an_import_that_did_not_mark_them(self):
        self._plan()
        legacy.apply_plan(CHAT, log=lambda *_: None)
        data = stats._load_weekly_contest_data(CHAT)
        for record in data["winners"].values():
            record.pop("source", None)
            record.pop("contest_id", None)
        stats._write_json_atomic(stats._weekly_contest_path(CHAT), data)
        self.assertEqual(legacy.undo_import(CHAT)["winners"], [1, 2])
        self.assertEqual(stats.weekly_contest_winners(CHAT), {})

    def test_an_announced_week_is_never_overwritten_by_an_import(self):
        poll = voting.Poll(poll_id="2026-W28", entry=CHAT, created_at="t",
                           entries=[voting.Entry("9", 9, 99, "Своя", None, "", ["9_0.jpg"])])
        voting.set_approved(poll, ["9"])
        voting.save_results(poll, poll.tally(), "объявлено ботом")
        self.assertIsNone(voting.save_imported_results(CHAT, "2026-W28", [], created_at="", announced_at="",
                                                       voters=0))
        self.assertEqual(voting.load_results(CHAT, "2026-W28")["text"], "объявлено ботом")

    def test_a_weekly_table_numbered_another_way_is_left_alone(self):
        stats.record_weekly_contest_winner(CHAT, 1, GOSHA, "Гоша", ADMIN["id"], "@admin")
        self._plan()
        before = stats._weekly_contest_path(CHAT).read_text(encoding="utf-8")
        result = legacy.apply_plan(CHAT, log=lambda *_: None)
        self.assertEqual(result["winners"]["status"], "mismatch")
        self.assertEqual(stats._weekly_contest_path(CHAT).read_text(encoding="utf-8"), before)
        self.assertIn("не трогал", legacy.apply_report(result))

    def test_a_table_that_agrees_is_filled_in_with_one_read_pair_and_one_write(self):
        stats.record_weekly_contest_winner(CHAT, 1, BORYA, "Боря", ADMIN["id"], "@admin")
        self._plan()
        loads, writes = [], []
        real_load, real_write = stats._load_weekly_contest_data, stats._write_json_atomic
        with patch.object(stats, "_load_weekly_contest_data", side_effect=lambda e: loads.append(e) or real_load(e)), \
                patch.object(stats, "_write_json_atomic", side_effect=lambda *a: writes.append(a[0]) or real_write(*a)):
            result = legacy.apply_plan(CHAT, log=lambda *_: None)
        self.assertEqual(result["winners"]["awarded"], [2])
        self.assertEqual(len(loads), 2)    # the check, then the batch -- not one per week
        self.assertEqual(len(writes), 1)

    def test_a_winner_without_an_account_is_not_put_in_the_table(self):
        client = _chat()
        client.messages[200] = _poll_message(200, _at("2026-07-20", 18), "Итоги недели",
                                             [_answer(0, "t.me/persykart"), _answer(1, "@anya")], [5, 1])
        self._plan(client)
        result = legacy.apply_plan(CHAT, log=lambda *_: None)
        self.assertEqual(result["winners"]["awarded"], [1])
        self.assertEqual(result["winners"]["unnamed"], 1)
        self.assertEqual(hall_of_fame.load_contest(CHAT, "2026-W29").winner().author_name, "Persyk Art")

    def test_without_a_plan_it_says_to_scan_first(self):
        self.assertIn("/hall опросы", legacy.apply_report(legacy.apply_plan(CHAT)))

    def test_a_lost_option_picture_comes_back_from_the_poll(self):
        client = _chat()
        names = asyncio.run(legacy.download_option_photo(client, "chat", 110, 2, self.root / "back"))
        self.assertEqual(names, ["110_o2.jpg"])
        self.assertEqual(asyncio.run(legacy.download_option_photo(client, "chat", 200, 0, self.root / "back")), [])
        self.assertIsNone(asyncio.run(legacy.download_option_photo(client, "chat", 999, 0, self.root / "back")))


class FakeApi:
    def __init__(self):
        self.sent = []
        self.photos = []
        self.edits = []

    async def send_message(self, chat_id, text, reply_to_message_id=None, reply_markup=None,
                           parse_mode=None, disable_notification=False):
        self.sent.append((text, reply_markup))
        return {"message_id": 100 + len(self.sent)}

    async def send_photo_file(self, chat_id, path, caption=None, reply_markup=None, **kwargs):
        self.photos.append((Path(path).name, caption, reply_markup))
        return {"message_id": 500 + len(self.photos)}

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None, parse_mode=None):
        self.edits.append(text)

    async def answer_callback_query(self, callback_id, text=None):
        pass


def _cfg():
    return SimpleNamespace(webapp_public_url="https://example.com", vote_miniapp_short_name=None,
                           vote_announce_extra_chat=None, backup_chat_id=None)


class CommandTests(_Store):
    def _patches(self):
        async def resolve_id(*args, **kwargs):
            return -100

        async def can_manage(api_, chat_id_, user_, entry=None):
            return user_.get("id") == ADMIN["id"]

        async def resolve(client_, entry):
            return SimpleNamespace(id=4242, username=None)

        return [patch.object(bot_listener, "_resolve_chat_id", resolve_id),
                patch.object(bot_listener, "_can_manage_chat", can_manage),
                patch.object(bot_listener, "resolve_chat", resolve),
                patch.object(bot_listener, "_legacy_members", lambda entry: MEMBERS)]

    def _run(self, coroutine_factory):
        async def scenario():
            patchers = self._patches()
            for patcher in patchers:
                patcher.start()
            try:
                await coroutine_factory()
            finally:
                for patcher in patchers:
                    patcher.stop()

        asyncio.run(scenario())

    def _type(self, text, client=None, user=ADMIN, chat_type="private"):
        api = FakeApi()
        message = {"message_id": 1, "chat": {"id": 5 if chat_type == "private" else -100, "type": chat_type},
                   "from": user, "text": text}
        self._run(lambda: bot_listener.handle_hall_command(api, client or _chat(), _cfg(), message, CHAT,
                                                           "testbot", log=lambda *_: None))
        return api

    def _reply(self, prompt_caption, text, user=ADMIN):
        api = FakeApi()
        message = {"message_id": 2, "chat": {"id": 5, "type": "private"}, "from": user, "text": text,
                   "reply_to_message": {"from": {"is_bot": True}, "caption": prompt_caption}}
        handled = []

        async def go():
            handled.append(await bot_listener.handle_hall_author_reply(api, _chat(), _cfg(), message, CHAT,
                                                                       log=lambda *_: None))

        self._run(go)
        return handled[0], api

    def test_only_an_administrator_in_the_dm(self):
        self.assertIn("только администраторы", self._type("/hall опросы", user=STRANGER).sent[0][0])
        self.assertIn("в личке", self._type("/hall опросы", chat_type="supergroup").sent[0][0])
        self.assertIsNone(legacy.load_plan(CHAT))

    def test_the_panel_offers_the_old_contests(self):
        buttons = [b for row in self._type("/hall").sent[0][1]["inline_keyboard"] for b in row]
        self.assertIn(f"hallaction:polls:{ADMIN['id']}", [b.get("callback_data") for b in buttons])

    def test_scan_name_the_stranger_by_reply_then_write(self):
        api = self._type("/hall опросы")
        text, markup = api.sent[-1]
        buttons = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
        self.assertEqual(buttons, [f"hallaction:pollsask:{ADMIN['id']}", f"hallaction:pollsyes:{ADMIN['id']}"])

        api = self._type(bot_listener.HALL_ACTIONS["pollsask"])
        self.assertEqual(len(api.photos), 1)
        name, caption, reply_markup = api.photos[0]
        self.assertEqual(name, "110_o3.jpg")
        self.assertTrue(caption.startswith(bot_listener.HALL_AUTHOR_PROMPT))
        self.assertIn("Опрос: https://t.me/c/4242/110", caption)
        self.assertTrue(caption.endswith("Код работы: 110.3"))
        self.assertEqual(reply_markup, {"force_reply": True, "selective": True})

        handled, stranger_api = self._reply(caption, "Боря", user=STRANGER)
        self.assertTrue(handled)
        self.assertIn("только администраторы", stranger_api.sent[0][0])
        handled, api = self._reply(caption, "Боря")
        self.assertIn("Записал: «4» (2026-W28) — Боря.", api.sent[0][0])
        self.assertIn("Неделя 2026-W28 готова", api.sent[0][0])

        api = self._type(bot_listener.HALL_ACTIONS["pollsyes"])
        self.assertIn("Записано конкурсов: 3", api.sent[-1][0])
        self.assertIn("🥇 Гоша (@gosha) — 8 голосов · №2", api.sent[-1][0])
        self.assertEqual(api.sent[-1][1]["inline_keyboard"][0][0]["url"], "https://example.com/hall")

    def test_an_author_can_be_typed_with_the_works_code(self):
        self._type("/hall опросы")
        api = self._type("/hall опросы автор 110.3 @gosha")
        self.assertIn("— Гоша (@gosha).", api.sent[-1][0])
        api = self._type("/hall опросы автор 999.9 @gosha")
        self.assertIn("Не нашёл такую работу", api.sent[-1][0])

    def test_an_author_nobody_in_the_chat_has_is_looked_up(self):
        self._type("/hall опросы")
        api = self._type("/hall опросы автор 110.3 t.me/persykart")
        self.assertIn("— Persyk Art (@persykart). Аккаунт не нашёл — будет под этим именем.", api.sent[-1][0])

    def test_a_reply_to_something_else_is_not_claimed(self):
        handled, api = self._reply("Какая-то другая подпись", "@gosha")
        self.assertFalse(handled)
        self.assertEqual(api.sent, [])

    def test_undo_asks_first_then_takes_it_out(self):
        self._type("/hall опросы")
        self._type(bot_listener.HALL_ACTIONS["pollsyes"])
        api = self._type(bot_listener.HALL_ACTIONS["pollsundo"])
        self.assertIn("2026-W27, 2026-W29", api.sent[-1][0])
        self.assertEqual(api.sent[-1][1]["inline_keyboard"][0][0]["callback_data"],
                         f"hallaction:pollsundoyes:{ADMIN['id']}")
        self.assertIsNotNone(hall_of_fame.load_contest(CHAT, "2026-W27"))
        api = self._type(bot_listener.HALL_ACTIONS["pollsundoyes"])
        self.assertIn("Убрал недели: 2026-W27, 2026-W29.", api.sent[-1][0])
        self.assertIsNone(hall_of_fame.load_contest(CHAT, "2026-W27"))
        self.assertIn("отменять нечего", self._type(bot_listener.HALL_ACTIONS["pollsundo"]).sent[-1][0])

    def test_the_hall_import_fetches_a_lost_option_picture_back_from_the_poll(self):
        self._plan()
        self._name_the_stranger()
        legacy.apply_plan(CHAT, log=lambda *_: None)
        for path in hall_of_fame.media_dir(CHAT, "2026-W28").glob("*110_o3.jpg"):
            path.unlink()
        contest = hall_of_fame.load_contest(CHAT, "2026-W28")
        for work in contest.works:
            if work.entry_id == "110o3":
                work.photos, work.thumb = [], None
        hall_of_fame.save_contest(contest)

        async def resolve(client_, entry):
            return "chat"

        with patch.object(bot_listener, "resolve_chat", resolve):
            restored, gone = asyncio.run(bot_listener._restore_hall_photos(_chat(), CHAT, log=lambda *_: None))
        self.assertEqual((restored, gone), (1, 0))
        work = next(w for w in hall_of_fame.load_contest(CHAT, "2026-W28").works if w.entry_id == "110o3")
        self.assertEqual(work.photos, ["110_o3.jpg"])


class SearchTests(_Store):
    def test_the_search_brings_in_the_whole_album(self):
        entries = asyncio.run(voting.search_entries(
            _chat(), "chat", TZ, self.root / "media", _at("2026-07-06", 0), _at("2026-07-10", 0), log=lambda *_: None,
        ))
        self.assertEqual([(e.entry_id, e.media) for e in entries],
                         [("102", ["102_0.jpg"]), ("100", ["100_0.jpg", "100_1.jpg"])])
        self.assertEqual(entries[1].text, "дракон")


if __name__ == "__main__":
    unittest.main()
