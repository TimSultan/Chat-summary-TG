"""Choosing which hashtag "/vote собрать" collects: #итогинедели, or a thematic contest's own.

A thematic contest ("Лучший аниме-покрас", #аниме) runs beside the weekly vote, so its works
go into a poll of their own for the week -- the weekly vote's works, ballots, results record
and board picture are keyed by poll id and must never be touched by it.
"""

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_listener
import voting

CHAT = "Chat"
WEEK = "2026-W41"
ADMIN = {"id": 7, "username": "admin"}
STRANGER = {"id": 8, "username": "someone"}
DM = 5
ANIME = "#аниме"


def _cfg():
    return SimpleNamespace(webapp_public_url="https://example.com",
                           vote_miniapp_short_name=None, vote_announce_extra_chat=None)


class FakeApi:
    def __init__(self):
        self.sent = []      # (text, reply_markup)
        self.edits = []     # (message_id, text, reply_markup)
        self.answered = []

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
    return {b["text"]: b.get("callback_data") for b in _buttons(markup)}


def _entry(entry_id, posted: date = date(2026, 10, 1)):
    return voting.Entry(
        entry_id=entry_id, message_id=int(entry_id), author_id=int(entry_id),
        author_name=f"Автор {entry_id}", author_username=None, text="", media=[f"{entry_id}.jpg"],
        posted_at=datetime.combine(posted, datetime.min.time(), tzinfo=timezone.utc).replace(hour=12).isoformat(),
    )


class _Storage(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("voting._voting_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)


class HashtagRulesTests(_Storage):
    def test_every_spelling_of_a_tag_is_one_tag(self):
        for raw in ("#аниме", "аниме", " #Аниме ", "#АНИМЕ"):
            with self.subTest(raw=raw):
                self.assertEqual(voting.normalize_hashtag(raw), ANIME)
        self.assertEqual(voting.normalize_hashtag("#mini_32mm"), "#mini_32mm")

    def test_what_is_not_a_hashtag_is_refused(self):
        for raw in ("", "#", "# аниме", "#аниме покрас", "#аниме!", "#" + "а" * 60, None):
            with self.subTest(raw=raw):
                self.assertIsNone(voting.normalize_hashtag(raw))

    def test_the_weekly_tag_keeps_the_bare_week_and_any_other_gets_its_own_poll(self):
        self.assertEqual(voting.poll_id_for(WEEK, voting.CONTEST_HASHTAG), WEEK)
        self.assertEqual(voting.poll_id_for(WEEK, "#ИтогиНедели"), WEEK)
        anime = voting.poll_id_for(WEEK, ANIME)
        self.assertNotEqual(anime, WEEK)
        self.assertTrue(anime.startswith(WEEK + "-"))
        # Same tag, same poll -- however it was spelled; and a name the photo URLs allow.
        self.assertEqual(anime, voting.poll_id_for(WEEK, "Аниме"))
        self.assertRegex(anime, r"^[A-Za-z0-9_.-]+$")
        self.assertNotEqual(anime, voting.poll_id_for(WEEK, "#скульпт"))

    def test_a_typed_theme_splits_into_tag_badge_and_title(self):
        self.assertEqual(voting.parse_theme_text("#аниме 🌸 Лучший аниме-покрас"),
                         (ANIME, "Лучший аниме-покрас", "🌸"))
        self.assertEqual(voting.parse_theme_text("аниме"), (ANIME, "", ""))
        self.assertIsNone(voting.parse_theme_text("аниме Лучший аниме-покрас"))  # needs the #
        self.assertEqual(voting.parse_theme_text("#мини Лучшая миниатюра 32 мм"),
                         ("#мини", "Лучшая миниатюра 32 мм", ""))
        self.assertEqual(voting.parse_theme_text("#аниме"), (ANIME, "", ""))
        self.assertIsNone(voting.parse_theme_text("просто текст!"))

    def test_contest_names(self):
        self.assertEqual(voting.contest_title(voting.CONTEST_HASHTAG), "Итоги недели")
        self.assertEqual(voting.contest_title(ANIME), ANIME)
        self.assertEqual(voting.contest_title(ANIME, "Лучший аниме-покрас"), "Лучший аниме-покрас")


class ThemeRegistryTests(_Storage):
    def test_a_theme_is_remembered_and_found_by_slug_or_tag(self):
        theme = voting.remember_theme(CHAT, "Аниме", "Лучший аниме-покрас", "🌸")
        self.assertEqual((theme.hashtag, theme.title, theme.badge), (ANIME, "Лучший аниме-покрас", "🌸"))
        self.assertEqual(voting.find_theme(CHAT, theme.slug).title, "Лучший аниме-покрас")
        self.assertEqual(voting.find_theme(CHAT, ANIME).badge, "🌸")
        self.assertIsNone(voting.find_theme(CHAT, "abcdef"))
        self.assertEqual(voting.find_theme(CHAT, "").hashtag, voting.CONTEST_HASHTAG)

    def test_picking_a_theme_again_keeps_its_name(self):
        voting.remember_theme(CHAT, ANIME, "Лучший аниме-покрас", "🌸")
        theme = voting.remember_theme(CHAT, ANIME)
        self.assertEqual((theme.title, theme.badge), ("Лучший аниме-покрас", "🌸"))
        self.assertEqual(len(voting.load_themes(CHAT)), 1)

    def test_the_most_recently_used_theme_comes_first(self):
        voting.remember_theme(CHAT, ANIME)
        voting.remember_theme(CHAT, "#скульпт")
        self.assertEqual([t.hashtag for t in voting.load_themes(CHAT)], ["#скульпт", ANIME])
        voting.remember_theme(CHAT, ANIME)
        self.assertEqual([t.hashtag for t in voting.load_themes(CHAT)], [ANIME, "#скульпт"])

    def test_the_weekly_tag_is_never_written_down(self):
        voting.remember_theme(CHAT, voting.CONTEST_HASHTAG, "что угодно")
        self.assertEqual(voting.load_themes(CHAT), [])
        self.assertFalse(voting.themes_path(CHAT).exists())

    def test_the_themes_file_is_never_read_as_a_poll(self):
        voting.remember_theme(CHAT, ANIME)
        self.assertIsNone(voting.latest_poll(CHAT))
        self.assertEqual(voting.poll_ids(CHAT), [])


class PollHashtagTests(_Storage):
    def test_a_poll_keeps_its_contest_through_a_save(self):
        poll = voting.Poll(poll_id=voting.poll_id_for(WEEK, ANIME), entry=CHAT, created_at="t",
                           hashtag=ANIME, title="Лучший аниме-покрас", badge="🌸")
        voting.save_poll(poll)
        loaded = voting.load_poll(CHAT, poll.poll_id)
        self.assertEqual((loaded.hashtag, loaded.title, loaded.badge), (ANIME, "Лучший аниме-покрас", "🌸"))
        self.assertFalse(loaded.is_weekly)
        self.assertEqual(loaded.label(), "Лучший аниме-покрас")

    def test_a_poll_written_before_themes_is_the_weekly_contest(self):
        path = voting.poll_path(CHAT, WEEK)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"poll_id": WEEK, "entry": CHAT, "created_at": "t"}), encoding="utf-8")
        loaded = voting.load_poll(CHAT, WEEK)
        self.assertEqual(loaded.hashtag, voting.CONTEST_HASHTAG)
        self.assertTrue(loaded.is_weekly)
        self.assertEqual(loaded.label(), "Итоги недели")

    def test_a_re_collect_keeps_the_contest_and_a_theme_overrides_it(self):
        existing = voting.Poll(poll_id="p", entry=CHAT, created_at="t", hashtag=ANIME, title="Старое")
        self.assertEqual(voting.build_poll(CHAT, "p", [], existing=existing).title, "Старое")
        theme = voting.Theme(hashtag=ANIME, title="Новое", badge="🌸")
        rebuilt = voting.build_poll(CHAT, "p", [], existing=existing, theme=theme)
        self.assertEqual((rebuilt.title, rebuilt.badge), ("Новое", "🌸"))

    def test_the_results_record_says_which_contest_it_was(self):
        poll = voting.Poll(poll_id="p", entry=CHAT, created_at="t", entries=[_entry("1")],
                           hashtag=ANIME, title="Лучший аниме-покрас", badge="🌸")
        voting.save_results(poll, [(poll.entries[0], 3)], "текст")
        record = voting.load_results(CHAT, "p")
        self.assertEqual((record["hashtag"], record["title"], record["badge"]),
                         (ANIME, "Лучший аниме-покрас", "🌸"))
        self.assertEqual(record["standings"][0]["posted_at"], poll.entries[0].posted_at)

    def test_a_thematic_announcement_names_the_contest_and_the_weekly_one_is_unchanged(self):
        standings = [(_entry("1"), 2)]
        weekly = voting.Poll(poll_id=WEEK, entry=CHAT, created_at="t")
        themed = voting.Poll(poll_id="p", entry=CHAT, created_at="t", hashtag=ANIME, title="Лучший аниме-покрас")
        self.assertTrue(voting.format_results_text(standings, header=voting.results_header(weekly))
                        .startswith("Результаты недельного голосования:"))
        self.assertTrue(voting.format_results_text(standings, header=voting.results_header(themed))
                        .startswith("Результаты конкурса «Лучший аниме-покрас»:"))


class ClosedVoteYieldsTests(_Storage):
    def _poll(self, poll_id, created_at, admitted, open_=True, hashtag=voting.CONTEST_HASHTAG):
        poll = voting.Poll(poll_id=poll_id, entry=CHAT, created_at=created_at, hashtag=hashtag,
                           entries=[_entry(f"{len(poll_id)}{i}") for i in range(3)], open=open_)
        voting.set_approved(poll, [e.entry_id for e in poll.entries][:admitted])
        voting.save_poll(poll)

    def test_a_running_vote_still_keeps_the_page_from_a_thematic_collect(self):
        self._poll(WEEK, "2026-10-05T08:00:00+00:00", admitted=3)
        self._poll(voting.poll_id_for(WEEK, ANIME), "2026-10-06T08:00:00+00:00", admitted=0, hashtag=ANIME)
        self.assertEqual(voting.latest_poll(CHAT).poll_id, WEEK)

    def test_once_the_vote_is_closed_the_newer_collect_takes_the_page(self):
        """Without this a thematic contest collected beside the weekly one could only be
        reached by clearing -- which archives every poll, the new one with it."""
        self._poll(WEEK, "2026-10-05T08:00:00+00:00", admitted=3, open_=False)
        self._poll(voting.poll_id_for(WEEK, ANIME), "2026-10-06T08:00:00+00:00", admitted=0, hashtag=ANIME)
        self.assertEqual(voting.latest_poll(CHAT).hashtag, ANIME)

    def test_a_closed_vote_still_shows_its_result_while_it_is_the_newest(self):
        self._poll("2026-W40", "2026-09-28T08:00:00+00:00", admitted=0)
        self._poll(WEEK, "2026-10-05T08:00:00+00:00", admitted=3, open_=False)
        self.assertEqual(voting.latest_poll(CHAT).poll_id, WEEK)


class _Bot(_Storage):
    def setUp(self):
        super().setUp()
        self.scans = []

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
            patch.object(bot_listener, "_current_vote_poll_id", lambda tz: WEEK),
            patch.object(bot_listener, "datetime", _Today),
        ]

    def _run(self, factory, new_entries=()):
        async def scenario():
            tasks = set()
            patches = self._patches(new_entries)
            for p in patches:
                p.start()
            try:
                result = await factory(tasks)
                while tasks:
                    await asyncio.gather(*list(tasks))
                return result
            finally:
                for p in patches:
                    p.stop()
        return asyncio.run(scenario())

    def _type(self, text, user=ADMIN, new_entries=()):
        api = FakeApi()
        message = {"message_id": 1, "chat": {"id": DM, "type": "private"}, "from": user, "text": text}
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

    def _reply(self, text, prompt=bot_listener.VOTE_HASHTAG_PROMPT, from_bot=True, user=ADMIN):
        api = FakeApi()
        message = {"message_id": 2, "chat": {"id": DM, "type": "private"}, "from": user, "text": text,
                   "reply_to_message": {"message_id": 1, "text": prompt, "from": {"id": 1, "is_bot": from_bot}}}
        handled = self._run(lambda tasks: bot_listener.handle_vote_hashtag_reply(
            api, None, _cfg(), timezone.utc, message, CHAT, "testbot", tasks, {}, log=lambda *_: None,
        ))
        return api, handled

    def _seed_weekly_vote(self):
        poll = voting.Poll(poll_id=WEEK, entry=CHAT, created_at="2026-10-05T08:00:00+00:00",
                           entries=[_entry("1"), _entry("2")])
        voting.set_approved(poll, ["1", "2"])
        voting.record_vote(poll, 42, ["1"])
        voting.save_poll(poll)
        return poll


class CommandTests(_Bot):
    def test_a_typed_theme_opens_the_picker_for_it_and_is_remembered(self):
        api = self._type("/vote собрать #Аниме 🌸 Лучший аниме-покрас")
        text, markup = api.sent[0]
        self.assertIn("с какого дня", text)
        self.assertIn("Собрать заявки с #аниме", text)
        self.assertIn("«Лучший аниме-покрас»", text)
        slug = voting.hashtag_slug(ANIME)
        for button in _buttons(markup):
            self.assertLessEqual(len(button["callback_data"].encode()), 64)
        # Every date button carries the tag; cancelling needs none.
        dated = [b["callback_data"] for b in _buttons(markup) if b["callback_data"].startswith("votedate:t:")]
        self.assertTrue(dated and all(d.endswith(":" + slug) for d in dated))
        self.assertEqual(self.scans, [])
        theme = voting.find_theme(CHAT, ANIME)
        self.assertEqual((theme.title, theme.badge), ("Лучший аниме-покрас", "🌸"))

    def test_a_stranger_cannot_remember_a_theme(self):
        api = self._type("/vote собрать #аниме Лучший", user=STRANGER)
        self.assertIn("администратор", api.sent[0][0])
        self.assertEqual(voting.load_themes(CHAT), [])

    def test_a_tag_that_is_not_one_is_refused_before_anything_happens(self):
        api = self._type("/vote собрать #аниме! 28.09")
        self.assertIn("Не понял хэштег", api.sent[0][0])
        self.assertEqual(self.scans, [])

    def test_a_thematic_collect_reads_its_tag_into_its_own_poll(self):
        weekly = self._seed_weekly_vote()
        api = self._type("/vote собрать #аниме Лучший аниме-покрас 28.09 05.10",
                         new_entries=[_entry("9")])
        (scan,) = self.scans
        self.assertEqual(scan["hashtag"], ANIME)
        themed = voting.load_poll(CHAT, voting.poll_id_for(WEEK, ANIME))
        self.assertEqual([e.entry_id for e in themed.entries], ["9"])
        self.assertEqual((themed.hashtag, themed.title), (ANIME, "Лучший аниме-покрас"))
        # The weekly vote is exactly as it was: works, admissions and ballots.
        untouched = voting.load_poll(CHAT, WEEK)
        self.assertEqual(untouched.to_dict(), weekly.to_dict())
        replies = " ".join(text for text, _ in api.sent)
        self.assertIn("Собираю все заявки с #аниме", replies)
        self.assertIn("открыт пока другой конкурс", replies)  # the weekly vote is running

    def test_a_weekly_collect_still_reads_the_weekly_tag(self):
        self._type("/vote собрать 28.09 05.10")
        (scan,) = self.scans
        self.assertEqual(scan["hashtag"], voting.CONTEST_HASHTAG)

    def test_add_new_tops_up_the_contest_the_page_is_showing(self):
        poll = voting.Poll(poll_id=voting.poll_id_for(WEEK, ANIME), entry=CHAT, created_at="t",
                           entries=[_entry("3")], hashtag=ANIME, title="Лучший аниме-покрас")
        voting.save_poll(poll)
        self._type("/vote добавить", new_entries=[_entry("4")])
        (scan,) = self.scans
        self.assertEqual(scan["hashtag"], ANIME)
        topped = voting.load_poll(CHAT, poll.poll_id)
        self.assertEqual(sorted(e.entry_id for e in topped.entries), ["3", "4"])
        self.assertEqual(topped.title, "Лучший аниме-покрас")


class PickerTests(_Bot):
    def test_step_one_offers_to_change_the_tag(self):
        _, markup = bot_listener._vote_date_picker(date(2026, 10, 5), ADMIN["id"])
        self.assertEqual(_by_text(markup)["🏷 Хэштег: #итогинедели — сменить"], f"votedate:h:-:-:{ADMIN['id']}")

    def test_the_hashtag_screen_lists_the_weekly_tag_and_every_remembered_theme(self):
        voting.remember_theme(CHAT, ANIME, "Лучший аниме-покрас", "🌸")
        api = self._tap(f"votedate:h:-:-:{ADMIN['id']}")
        text, markup = api.edits[0][1], api.edits[0][2]
        self.assertIn("Какой хэштег собрать", text)
        buttons = _by_text(markup)
        self.assertEqual(buttons["✓ #итогинедели"], f"votedate:hs:-:-:{ADMIN['id']}")
        self.assertEqual(buttons["🌸 #аниме — Лучший аниме-покрас"],
                         f"votedate:hs:-:-:{ADMIN['id']}:{voting.hashtag_slug(ANIME)}")
        self.assertIn("✏️ Новый хэштег", buttons)

    def test_choosing_a_theme_goes_back_to_the_first_day_with_it(self):
        theme = voting.remember_theme(CHAT, ANIME, "Лучший аниме-покрас")
        api = self._tap(f"votedate:hs:-:-:{ADMIN['id']}:{theme.slug}")
        text, markup = api.edits[0][1], api.edits[0][2]
        self.assertIn("Собрать заявки с #аниме", text)
        self.assertIn(f"🏷 Хэштег: {ANIME} — сменить", _by_text(markup))

    def test_the_confirmation_names_the_contest_and_collecting_reads_its_tag(self):
        theme = voting.remember_theme(CHAT, ANIME, "Лучший аниме-покрас")
        api = self._tap(f"votedate:t:20260928:20261005:{ADMIN['id']}:{theme.slug}")
        text, markup = api.edits[0][1], api.edits[0][2]
        self.assertIn("работы с #аниме", text)
        self.assertIn("«Лучший аниме-покрас»", text)
        go = _by_text(markup)["✅ Собрать"]
        self.assertEqual(go, f"votedate:go:20260928:20261005:{ADMIN['id']}:{theme.slug}")
        self.assertEqual(self.scans, [])

        self._tap(go, new_entries=[_entry("9")])
        (scan,) = self.scans
        self.assertEqual(scan["hashtag"], ANIME)
        self.assertEqual(voting.load_poll(CHAT, voting.poll_id_for(WEEK, ANIME)).title, "Лучший аниме-покрас")

    def test_an_unknown_tag_never_falls_back_to_another_one(self):
        api = self._tap(f"votedate:go:20260928:20261005:{ADMIN['id']}:abcdef")
        self.assertIn("Не знаю такого хэштега", api.edits[0][1])
        self.assertEqual(self.scans, [])

    def test_a_malformed_slug_is_not_a_button_of_ours(self):
        self.assertIsNone(bot_listener._parse_vote_date_callback(f"votedate:go:1:2:{ADMIN['id']}:../x"))

    def test_new_hashtag_asks_for_it_with_a_force_reply(self):
        api = self._tap(f"votedate:hn:-:-:{ADMIN['id']}")
        text, markup = api.sent[0]
        self.assertTrue(text.startswith(bot_listener.VOTE_HASHTAG_PROMPT))
        self.assertTrue(markup["force_reply"])


class HashtagReplyTests(_Bot):
    def test_the_answer_opens_the_picker_for_the_new_theme(self):
        api, handled = self._reply("#аниме 🌸 Лучший аниме-покрас")
        self.assertTrue(handled)
        self.assertIn("Собрать заявки с #аниме", api.sent[0][0])
        self.assertEqual(voting.find_theme(CHAT, ANIME).title, "Лучший аниме-покрас")
        self.assertEqual(self.scans, [])

    def test_a_tag_without_the_hash_is_accepted(self):
        api, handled = self._reply("аниме")
        self.assertTrue(handled)
        self.assertIn("Собрать заявки с #аниме", api.sent[0][0])

    def test_only_an_answer_to_the_prompt_is_claimed(self):
        _, handled = self._reply("#аниме", prompt="Какой текст написать в объявлении?")
        self.assertFalse(handled)
        _, handled = self._reply("#аниме", from_bot=False)
        self.assertFalse(handled)

    def test_a_bad_answer_is_explained_and_nothing_is_remembered(self):
        api, handled = self._reply("аниме покрас!")
        self.assertTrue(handled)
        self.assertIn("Это не хэштег", api.sent[0][0])
        self.assertEqual(voting.load_themes(CHAT), [])

    def test_a_stranger_answering_gets_refused(self):
        api, handled = self._reply("#аниме", user=STRANGER)
        self.assertTrue(handled)
        self.assertIn("администратор", api.sent[0][0])
        self.assertEqual(voting.load_themes(CHAT), [])


if __name__ == "__main__":
    unittest.main()
