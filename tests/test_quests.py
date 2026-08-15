import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import economy
import pets
import pets_config
import pets_ui
import pets_quest_catalog as catalog
import stats
import quests


def _a_real_quest(*, repeatable):
    """One real quest picked by the property under test, never by name.

    The catalogue is meant to be edited -- that is the whole point of it being data -- so
    a test that hard-codes "magnifier" fails the day somebody swaps that quest out, and
    the failure says nothing about the behaviour it was guarding.
    """
    for quest in catalog.REAL_QUESTS:
        if repeatable == (quest.cooldown_days > 0):
            return quest
    raise AssertionError(
        f"the catalogue has no {'repeatable' if repeatable else 'once-ever'} real quest"
    )


_ONCE = _a_real_quest(repeatable=False)
_REPEATABLE = _a_real_quest(repeatable=True)
_BADGED = next((q for q in catalog.REAL_QUESTS if q.badge), _REPEATABLE)


class QuestsTestCase(unittest.TestCase):
    """Base fixture: point stats._stats_dir (and therefore economy's, pets' and quests'
    storage) at a throwaway directory, the same way tests/test_pets.py does."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def _tame(self, entry, uid, name=None):
        """Fund and walk one member all the way to a named pet -- copied verbatim from
        tests/test_pets.py's PetsTestCase so both suites tame the same way."""
        name = name or f"Питомец{uid}"
        # The base cage and taming are free now. Funding the retired prices here leaves
        # 100 coins in every fixture and makes payout assertions measure the fixture
        # rather than the quest.
        ok, msg = pets.buy_cage(entry, uid, 0)
        self.assertTrue(ok, msg)
        ok, msg = pets.tame(entry, uid, 0, name, f"file{uid}", f"Owner{uid}")
        self.assertTrue(ok, msg)

    def _finish_quest(self, entry, user_id, now, accept=True):
        """Assign, submit and review one quest in a single call, returning what was
        assigned, the submission id, and the review receipt -- the sequence every
        payment or history test needs and none of them should have to spell out by hand.
        """
        payload = quests.daily_quest(entry, user_id, now=now)
        quest = payload["quest"]
        ok, msg = quests.submit(entry, user_id, quest["code"], now=now)
        self.assertTrue(ok, msg)
        live = quests.daily_quest(entry, user_id, now=now)
        submission_id = live["submission"]["id"]
        ok, msg, receipt = quests.review(
            entry, submission_id, "mod1", accept,
            note="Попробуй сфотографировать работу при дневном свете." if not accept else "",
            now=now,
        )
        self.assertTrue(ok, msg)
        return quest, submission_id, receipt


class AssignmentTests(QuestsTestCase):
    def test_paint_board_has_three_unique_cards_without_an_automatic_deadline(self):
        entry = "chat"
        first = quests.daily_quest(entry, "1", now=datetime(2026, 8, 9, 9, 0))
        codes = {card["code"] for card in first["quests"]}
        self.assertEqual(len(codes), 3)
        self.assertEqual(first["available_count"], 3)

        again_same_day = quests.daily_quest(entry, "1", now=datetime(2026, 8, 9, 20, 0))
        self.assertEqual({card["code"] for card in again_same_day["quests"]}, codes)

        next_board = quests.daily_quest(entry, "1", now=datetime(2026, 9, 10, 9, 0))
        next_codes = {card["code"] for card in next_board["quests"]}
        self.assertEqual(len(next_codes), 3)
        self.assertEqual(next_codes, codes)
        self.assertFalse(next_board["auto_refresh"])
        self.assertIsNone(next_board["refresh_at"])
        self.assertIsNone(next_board["seconds_until_refresh"])

    def test_finishing_one_card_leaves_the_other_two_available(self):
        entry = "chat"
        self._tame(entry, "1")
        day = datetime(2026, 8, 9, 9, 0)
        quest, _submission_id, _receipt = self._finish_quest(entry, "1", day)

        board = quests.daily_quest(entry, "1", now=day + timedelta(hours=6))
        self.assertEqual(board["status"], "open")
        self.assertEqual(board["available_count"], 2)
        completed = next(card for card in board["quests"] if card["code"] == quest["code"])
        self.assertEqual(completed["status"], "done")

    def test_empty_paint_board_waits_for_an_explicit_group_reroll(self):
        entry = "chat"
        self._tame(entry, "1")
        started = datetime(2026, 8, 9, 9, 0)
        old_codes = {card["code"] for card in quests.daily_quest(entry, "1", now=started)["quests"]}

        for minute in range(3):
            self._finish_quest(entry, "1", started + timedelta(minutes=minute))

        resting = quests.daily_quest(entry, "1", now=started + timedelta(hours=7, minutes=59))
        self.assertEqual(resting["status"], "resting")
        self.assertEqual(resting["available_count"], 0)

        still_resting = quests.daily_quest(entry, "1", now=started + timedelta(days=30))
        self.assertEqual(still_resting["available_count"], 0)

        rerolled_at = started + timedelta(days=30, minutes=1)
        self.assertTrue(quests.reroll(entry, "1", now=rerolled_at)[0])
        refreshed = quests.daily_quest(entry, "1", now=rerolled_at)
        new_codes = {card["code"] for card in refreshed["quests"]}
        self.assertEqual(refreshed["available_count"], 3)
        self.assertTrue(old_codes.isdisjoint(new_codes))

    def test_real_board_always_has_one_card(self):
        board = quests.real_quest("chat", "1", now=datetime(2026, 8, 9, 9, 0))
        self.assertEqual(len(board["quests"]), 1)
        self.assertEqual(board["available_count"], 1)

    def test_rune_board_has_five_unique_cards(self):
        board = quests.rune_quest("chat", "1", now=datetime(2026, 8, 9, 9, 0))
        self.assertEqual(len(board["quests"]), 5)
        self.assertEqual(len({card["code"] for card in board["quests"]}), 5)


class RunePaintCatalogTests(unittest.TestCase):
    def test_every_enchantable_painted_item_has_its_own_rune_quest(self):
        rows = {quest.code: quest for quest in catalog.RUNE_QUESTS}
        expected = {
            "rune_paint_weapon", "rune_paint_shield", "rune_paint_boots",
            "rune_paint_amulet", "rune_paint_pickaxe", "rune_paint_shovel",
            "rune_paint_vial", "rune_paint_scroll",
        }
        self.assertTrue(expected <= rows.keys())
        self.assertIn("NMM", rows["rune_paint_pickaxe"].technique)
        self.assertIn("NMM", rows["rune_paint_shovel"].technique)
        self.assertIn("кожу", rows["rune_paint_boots"].technique.lower())
        self.assertIn("потёртости", rows["rune_paint_boots"].technique.lower())


class RerollTests(QuestsTestCase):
    def test_reroll_swaps_the_whole_group_and_unlocks_twelve_hours_later(self):
        started = datetime(2026, 8, 9, 9, 0)
        first = quests.daily_quest("chat", "1", now=started)
        old_codes = {card["code"] for card in first["quests"]}

        used_at = started + timedelta(minutes=1)
        ok, msg = quests.reroll("chat", "1", now=used_at)
        self.assertTrue(ok, msg)
        payload = quests.daily_quest("chat", "1", now=used_at)
        self.assertTrue(old_codes.isdisjoint({card["code"] for card in payload["quests"]}))
        self.assertFalse(payload["reroll_available"])
        self.assertEqual(payload["reroll_at_label"], "21:01")

        ok, msg = quests.reroll("chat", "1", now=used_at + timedelta(hours=11, minutes=59))
        self.assertFalse(ok)
        self.assertIn("21:01", msg)
        self.assertTrue(quests.reroll("chat", "1", now=used_at + timedelta(hours=12))[0])

    def test_group_reroll_preserves_a_submission_under_review(self):
        entry = "chat"
        payload = quests.daily_quest(entry, "1", now=datetime(2026, 8, 9, 9, 0))
        code = payload["quest"]["code"]
        ok, msg = quests.submit(entry, "1", code, now=datetime(2026, 8, 9, 9, 5))
        self.assertTrue(ok, msg)

        ok, msg = quests.reroll(entry, "1", now=datetime(2026, 8, 9, 9, 10))
        self.assertTrue(ok, msg)
        board = quests.daily_quest(entry, "1", now=datetime(2026, 8, 9, 9, 10))
        preserved = next(card for card in board["quests"] if card["code"] == code)
        self.assertEqual(preserved["status"], "review")
        self.assertEqual(len(board["quests"]), 3)

    def test_each_group_has_its_own_twelve_hour_cooldown(self):
        entry = "chat"
        self._tame(entry, "1")
        day = datetime(2026, 8, 9, 9, 0)
        self.assertTrue(quests.reroll(entry, "1", now=day, kind="paint")[0])
        self.assertFalse(quests.daily_quest(entry, "1", now=day)["reroll_available"])
        self.assertTrue(quests.real_quest(entry, "1", now=day)["reroll_available"])
        self.assertTrue(quests.rune_quest(entry, "1", now=day)["reroll_available"])


class SubmissionTests(QuestsTestCase):
    def test_bot_photo_arriving_before_submission_is_attached_to_the_reward(self):
        entry = "chat"
        board = quests.rune_quest(entry, "1", now=datetime(2026, 8, 9, 9, 0))
        code = board["quests"][0]["code"]
        self.assertTrue(quests.attach_submission_photo(
            entry, -100123, 77, "telegram-photo", now=datetime(2026, 8, 9, 9, 1),
        ))
        self.assertTrue(quests.submit(
            entry, "1", code, chat_id=-100123, message_id=77,
            now=datetime(2026, 8, 9, 9, 2),
        )[0])
        self.assertEqual(quests.pending(entry)[0]["photo_file_id"], "telegram-photo")

    def test_bot_photo_arriving_after_submission_enriches_the_pending_row(self):
        entry = "chat"
        board = quests.rune_quest(entry, "1", now=datetime(2026, 8, 9, 9, 0))
        code = board["quests"][0]["code"]
        self.assertTrue(quests.submit(
            entry, "1", code, chat_id=-100123, message_id=78,
            now=datetime(2026, 8, 9, 9, 1),
        )[0])
        self.assertTrue(quests.attach_submission_photo(
            entry, -100123, 78, "telegram-photo", now=datetime(2026, 8, 9, 9, 2),
        ))
        self.assertEqual(quests.pending(entry)[0]["photo_file_id"], "telegram-photo")

    def test_submit_refuses_a_hashtag_that_is_not_the_players_own_live_quest(self):
        """Otherwise the hashtag becomes the whole game: paste #quest-nmm under anything
        and collect, with the daily assignment reduced to decoration."""
        entry = "chat"
        payload = quests.daily_quest(entry, "1", now=datetime(2026, 8, 9, 9, 0))
        live_code = payload["quest"]["code"]
        other = next(q for q in catalog.QUESTS if q.code != live_code)

        ok, msg = quests.submit(entry, "1", other.code, now=datetime(2026, 8, 9, 9, 5))
        self.assertFalse(ok)
        self.assertIn(catalog.hashtag(live_code), msg)

    def test_submit_refuses_a_second_submission_while_one_is_pending(self):
        entry = "chat"
        payload = quests.daily_quest(entry, "1", now=datetime(2026, 8, 9, 9, 0))
        code = payload["quest"]["code"]
        ok, msg = quests.submit(entry, "1", code, now=datetime(2026, 8, 9, 9, 5))
        self.assertTrue(ok, msg)

        ok, msg = quests.submit(entry, "1", code, now=datetime(2026, 8, 9, 9, 6))
        self.assertFalse(ok)

    def test_submit_refuses_an_unknown_code(self):
        entry = "chat"
        quests.daily_quest(entry, "1", now=datetime(2026, 8, 9, 9, 0))
        ok, msg = quests.submit(entry, "1", "not-a-real-code", now=datetime(2026, 8, 9, 9, 5))
        self.assertFalse(ok)


class ParseHashtagTests(unittest.TestCase):
    def test_finds_the_code_inside_a_longer_caption(self):
        self.assertEqual(
            quests.parse_hashtag("вот моя работа #quest_nmm, сделано за вечер"), "nmm"
        )

    def test_is_case_insensitive(self):
        self.assertEqual(quests.parse_hashtag("готово! #QUEST_NMM"), "nmm")

    def test_every_catalogue_tag_is_one_telegram_hashtag(self):
        """Telegram ends a hashtag at the first character that is not a letter, a digit
        or an underscore. A hyphenated code therefore posted as the tag `#quest` followed
        by loose text -- unclickable, and not grouped with anything in search. This is the
        reason the codes use underscores, so it is the thing worth pinning."""
        allowed = set("abcdefghijklmnopqrstuvwxyz0123456789_")
        for quest in catalog.QUESTS:
            with self.subTest(code=quest.code):
                tag = catalog.hashtag(quest.code)
                self.assertTrue(tag.startswith("#"))
                self.assertTrue(set(tag[1:]) <= allowed, tag)
                self.assertEqual(quests.parse_hashtag(f"готово {tag}"), quest.code)

    def test_a_hyphenated_tag_written_before_the_change_still_counts(self):
        """Captions posted under the old spelling -- or copied from an old message -- are
        still submissions. Folding them costs one replace and orphans nobody."""
        self.assertEqual(quests.parse_hashtag("старый пост #quest-anime-eyes"), "anime_eyes")
        self.assertEqual(quests.parse_hashtag("#quest-nmm"), "nmm")
        self.assertIs(catalog.find_quest("anime-eyes"), catalog.find_quest("anime_eyes"))

    def test_returns_none_for_an_unknown_code(self):
        self.assertIsNone(quests.parse_hashtag("#quest_not_a_real_code"))

    def test_a_store_written_before_the_rename_keeps_working(self):
        """Codes are canonicalised on READ rather than migrated on disk. Without it, a
        live assignment would be dealt away from under somebody, every cooldown would
        reset, and a once-ever quest like the loupe would quietly pay out twice."""
        import json

        with tempfile.TemporaryDirectory() as folder:
            with patch("stats._stats_dir", return_value=Path(folder)):
                day = datetime(2026, 8, 11, 9, 0)
                quests._path("chat").write_text(json.dumps({
                    "version": 1,
                    "assignments": {"1": {
                        "code": "anime-eyes", "day": "2026-08-11",
                        "assigned_at": day.isoformat(), "rerolls_used": 0,
                        "status": "open", "submission_id": None,
                    }},
                    "real_assignments": {}, "submissions": [], "history": [], "rewards": {},
                    "done": {"1": {_ONCE.code.replace("_", "-"): day.isoformat(),
                                   _REPEATABLE.code.replace("_", "-"): day.isoformat()}},
                    "disabled": ["nmm-gold"],
                }), encoding="utf-8")

                board = quests.daily_quest("chat", "1", now=day)
                self.assertEqual(board["quest"]["code"], "anime_eyes")
                self.assertEqual(board["quest"]["hashtag"], "#quest_anime_eyes")

                data = quests._load("chat")
                self.assertEqual(set(data["done"]["1"]), {_ONCE.code, _REPEATABLE.code})
                self.assertEqual(data["disabled"], ["nmm_gold"])
                # The cooldowns those stamps stand for are still being served.
                once = _ONCE
                self.assertFalse(quests._is_offerable(
                    once, data, "1", day + timedelta(days=999)))
                repeatable = _REPEATABLE
                self.assertFalse(quests._is_offerable(repeatable, data, "1", day))
                self.assertTrue(quests._is_offerable(
                    repeatable, data, "1", day + timedelta(days=repeatable.cooldown_days + 1)))

    def test_does_not_read_a_versioned_tag_as_its_base_code(self):
        """The character class that lets a code survive being hand-typed also swallows a
        trailing "_v2" into the same match, so a caption tagged for a different, unknown
        variant must not silently credit the base quest."""
        self.assertIsNone(quests.parse_hashtag("работа по технике #quest_nmm_v2"))


class GroupRerollTests(QuestsTestCase):
    def test_group_reroll_keeps_three_unique_painting_challenges(self):
        entry = "chat"
        day = datetime(2026, 8, 9, 9, 0)
        before = quests.daily_quest(entry, "1", now=day)
        old_codes = {card["code"] for card in before["quests"]}

        ok, message = quests.reroll(entry, "1", now=day)
        self.assertTrue(ok, message)
        after = quests.daily_quest(entry, "1", now=day)
        new_codes = {card["code"] for card in after["quests"]}
        self.assertEqual(len(new_codes), 3)
        self.assertTrue(old_codes.isdisjoint(new_codes))

    def test_old_single_card_callback_is_treated_as_a_safe_group_reroll(self):
        entry = "chat"
        day = datetime(2026, 8, 9, 9, 0)
        before = quests.daily_quest(entry, "1", now=day)
        old_codes = {card["code"] for card in before["quests"]}

        ok, message = quests.reroll(
            entry, "1", now=day, code=before["quests"][0]["code"],
        )
        self.assertTrue(ok, message)
        after_codes = {
            card["code"] for card in quests.daily_quest(entry, "1", now=day)["quests"]
        }
        self.assertTrue(old_codes.isdisjoint(after_codes))

    def test_a_reroll_never_deals_a_real_quest(self):
        """The two independently dealt boards must never mix their catalogues."""
        entry = "chat"
        day = datetime(2026, 8, 9, 9, 0)
        real_codes = {quest.code for quest in catalog.REAL_QUESTS}
        for _ in range(quests.REROLLS_PER_QUEST):
            quests.reroll(entry, "1", now=day)
        board = quests.daily_quest(entry, "1", now=day)
        self.assertNotIn(board["quest"]["code"], real_codes)


class RealQuestTests(QuestsTestCase):
    """The real-life board is independently dealt and always contains one card."""

    def test_the_real_slot_deals_one_real_quest_and_never_a_painting_one(self):
        entry = "chat"
        day = datetime(2026, 8, 9, 9, 0)
        real_codes = {quest.code for quest in catalog.REAL_QUESTS}
        board = quests.real_quest(entry, "1", now=day)
        self.assertEqual(board["kind"], "real")
        self.assertIn(board["quest"]["code"], real_codes)
        self.assertTrue(board["quest"]["proof"])

    def test_the_two_slots_are_independent(self):
        """Finishing one must not disturb the other: they are separate assignments with
        separate rerolls, and a player holds one of each."""
        entry = "chat"
        self._tame(entry, "1")
        day = datetime(2026, 8, 9, 9, 0)
        paint = quests.daily_quest(entry, "1", now=day)["quest"]["code"]
        real = quests.real_quest(entry, "1", now=day)["quest"]["code"]
        self.assertNotEqual(paint, real)

        # Finish the real one; the painting challenge is untouched.
        self.assertTrue(quests.submit(entry, "1", real, now=day)[0])
        row = quests.pending(entry)[0]
        quests.review(entry, row["id"], "mod1", True, now=day)
        self.assertEqual(quests.daily_quest(entry, "1", now=day)["quest"]["code"], paint)
        self.assertEqual(quests.real_quest(entry, "1", now=day)["status"], "resting")

    def test_a_real_quest_lasts_twenty_four_hours_and_has_its_own_rerolls(self):
        entry = "chat"
        day = datetime(2026, 8, 9, 9, 0)
        first = quests.real_quest(entry, "1", now=day)
        self.assertTrue(first["reroll_available"])
        self.assertEqual(
            quests.real_quest(entry, "1", now=day + timedelta(hours=12))["quest"]["code"],
            first["quest"]["code"],
        )
        self.assertNotEqual(
            quests.real_quest(entry, "1", now=day + timedelta(days=1))["quest"]["code"],
            first["quest"]["code"],
        )
        # Rerolling the real slot leaves the painting slot's allowance alone.
        next_day = day + timedelta(days=1)
        quests.daily_quest(entry, "1", now=next_day)
        self.assertTrue(quests.reroll(entry, "1", now=next_day, kind="real")[0])
        self.assertFalse(quests.real_quest(entry, "1", now=next_day)["reroll_available"])
        self.assertTrue(quests.daily_quest(entry, "1", now=next_day)["reroll_available"])
        self.assertTrue(quests.reroll(entry, "1", now=next_day, kind="paint")[0])
        self.assertFalse(quests.daily_quest(entry, "1", now=next_day)["reroll_available"])

    def test_a_hashtag_only_counts_for_the_slot_it_was_dealt_into(self):
        entry = "chat"
        day = datetime(2026, 8, 9, 9, 0)
        live = quests.real_quest(entry, "1", now=day)["quest"]["code"]
        other = next(q for q in catalog.REAL_QUESTS if q.code != live)

        ok, message = quests.submit(entry, "1", other.code, now=day)
        self.assertFalse(ok)
        self.assertIn(catalog.hashtag(live), message)
        self.assertTrue(quests.submit(entry, "1", live, now=day)[0])

    def test_a_once_ever_quest_is_never_dealt_twice_and_a_repeatable_one_returns(self):
        """A cooldown of 0 means once ever -- you cannot buy the same loupe twice. It is
        enforced at the DEAL now rather than on a shelf, so the slot simply never offers
        a quest that is resting."""
        entry = "chat"
        day = datetime(2026, 8, 9, 9, 0)
        once = _ONCE
        self.assertEqual(once.cooldown_days, 0)

        data = quests._load(entry)
        data.setdefault("real_assignments", {})["1"] = {
            "code": once.code, "day": day.date().isoformat(),
            "assigned_at": day.isoformat(), "rerolls_used": 0,
            "status": "open", "submission_id": None,
        }
        quests._save(entry, data)
        self.assertTrue(quests.submit(entry, "1", once.code, now=day)[0])
        quests.review(entry, quests.pending(entry)[0]["id"], "mod1", True, now=day)

        # Years later it is still never dealt again.
        for _ in range(30):
            board = quests.real_quest(entry, "1", now=day + timedelta(days=400))
            if board["quest"]:
                self.assertNotEqual(board["quest"]["code"], once.code)
                quests._save(entry, {**quests._load(entry), "real_assignments": {}})

    def test_a_repeatable_quest_comes_back_after_its_own_cooldown(self):
        entry = "chat"
        day = datetime(2026, 8, 9, 9, 0)
        quest = _REPEATABLE
        self.assertGreater(quest.cooldown_days, 0)
        data = quests._load(entry)
        data["done"] = {"1": {quest.code: day.isoformat()}}
        quests._save(entry, data)

        self.assertFalse(quests._is_offerable(quest, quests._load(entry), "1", day))
        later = day + timedelta(days=quest.cooldown_days + 1)
        self.assertTrue(quests._is_offerable(quest, quests._load(entry), "1", later))

    def test_two_players_who_finish_on_different_days_come_back_on_different_days(self):
        """The cooldown runs from each player's OWN completion, which is what spreads a
        fortnightly quest across the chat instead of handing it to everybody at once."""
        entry = "chat"
        start = datetime(2026, 8, 9, 9, 0)
        quest = _REPEATABLE
        data = quests._load(entry)
        data["done"] = {
            "1": {quest.code: start.isoformat()},
            "2": {quest.code: (start + timedelta(days=6)).isoformat()},
        }
        quests._save(entry, data)

        checked = start + timedelta(days=15)
        loaded = quests._load(entry)
        self.assertTrue(quests._is_offerable(quest, loaded, "1", checked))
        self.assertFalse(quests._is_offerable(quest, loaded, "2", checked))

    def test_finishing_a_real_quest_awards_its_badge_once(self):
        """A badge is the point of the quests that carry one -- the coins are the same as
        any other quest at that difficulty. Awarding it must survive being handed out
        twice, which is what the cooldown makes possible."""
        entry = "chat"
        day = datetime(2026, 8, 9, 9, 0)
        quest = _BADGED
        self.assertTrue(quest.badge)

        def deal_and_finish(when):
            data = quests._load(entry)
            data.setdefault("real_assignments", {})["1"] = {
                "code": quest.code, "day": when.date().isoformat(),
                "assigned_at": when.isoformat(), "rerolls_used": 0,
                "status": "open", "submission_id": None,
            }
            quests._save(entry, data)
            quests.submit(entry, "1", quest.code, now=when, author_name="Художник")
            row = quests.pending(entry)[0]
            return quests.review(entry, row["id"], "mod1", True, now=when)[2]

        receipt = deal_and_finish(day)
        self.assertEqual(receipt["badge"], quest.badge)
        self.assertTrue(receipt["badge_given"])

        later = day + timedelta(days=quest.cooldown_days + 1)
        second = deal_and_finish(later)
        self.assertFalse(second["badge_given"])
        names = [badge.name for badge in stats.custom_badges_for_user(entry, "1")]
        self.assertEqual(names.count(quest.badge), 1)


class ReviewPaymentTests(QuestsTestCase):
    def test_personal_paint_review_requires_and_then_mints_the_submission_photo(self):
        entry = "chat"
        self._tame(entry, "1")
        day = datetime(2026, 8, 9, 9, 0)
        quests.rune_quest(entry, "1", now=day)
        data = quests._load(entry)
        data["rune_assignments"]["1"]["quests"][0]["code"] = "rune_paint_weapon"
        quests._save(entry, data)
        self.assertTrue(quests.submit(
            entry, "1", "rune_paint_weapon", chat_id=-100123, message_id=90, now=day,
        )[0])
        submission_id = quests.pending(entry)[0]["id"]

        ok, message, receipt = quests.review(entry, submission_id, "mod1", True, now=day)
        self.assertFalse(ok)
        self.assertIn("фотографии", message)
        self.assertEqual(receipt, {})
        self.assertEqual(quests.pending(entry)[0]["status"], "pending")

        quests.attach_submission_photo(entry, -100123, 90, "telegram-photo", now=day)
        ok, message, receipt = quests.review(entry, submission_id, "mod1", True, now=day)
        self.assertTrue(ok, message)
        self.assertTrue(receipt["personal_paint_rune"]["granted"])
        self.assertEqual(receipt["personal_paint_rune"]["rune"]["photo_file_id"], "telegram-photo")

    def test_rune_quest_grants_random_rune_and_magic_after_acceptance(self):
        entry = "chat"
        self._tame(entry, "1")
        day = datetime(2026, 8, 9, 9, 0)
        board = quests.rune_quest(entry, "1", now=day)
        quest = next(
            (card for card in board["quests"] if not card["code"].startswith("rune_paint_")),
            None,
        )
        if quest is None:
            # The board is sampled from both ordinary and specialist tasks; make this
            # old reward-stream test explicitly exercise an ordinary rune task.
            ordinary = next(row for row in catalog.RUNE_QUESTS if not row.code.startswith("rune_paint_"))
            data = quests._load(entry)
            data["rune_assignments"]["1"]["quests"][0]["code"] = ordinary.code
            quests._save(entry, data)
            quest = next(card for card in quests.rune_quest(entry, "1", now=day)["quests"]
                         if card["code"] == ordinary.code)

        self.assertTrue(quests.submit(entry, "1", quest["code"], now=day)[0])
        submission_id = quests.pending(entry)[0]["id"]
        ok, message, receipt = quests.review(entry, submission_id, "mod1", True, now=day)

        self.assertTrue(ok, message)
        self.assertEqual(receipt["rubies"], 2)
        self.assertEqual(receipt["rune"]["granted"], 1)
        self.assertIn(receipt["rune"]["element"], pets.RUNE_ELEMENTS)
        self.assertTrue(receipt["scroll"])
        self.assertTrue(receipt["scroll_name"])

    def test_accepting_pays_gold_pet_xp_tickets_and_a_drop_exactly_once(self):
        """The most important behaviour in this module: review is a button pressed from a
        web page, and a moderator on a slow connection WILL double-tap it. All four
        payouts must land from the first press and stay put -- not multiply -- on the
        second."""
        entry = "chat"
        self._tame(entry, "1")
        day = datetime(2026, 8, 9, 9, 0)
        payload = quests.daily_quest(entry, "1", now=day)
        difficulty = payload["quest"]["difficulty"]
        # Force the drop roll to land, so the fourth payout is exercised too instead of
        # depending on luck.
        ok, msg = quests.set_reward(entry, difficulty, "drop_chance", 1.0)
        self.assertTrue(ok, msg)
        reward = quests.rewards_for(entry, difficulty)

        _quest, submission_id, receipt = self._finish_quest(entry, "1", day)

        self.assertEqual(receipt["gold"], reward["gold"])
        self.assertEqual(receipt["xp"], reward["xp"])
        self.assertEqual(receipt["tickets"], reward["tickets"])
        self.assertIsNotNone(receipt["item"])

        self.assertEqual(economy.balance(entry, "1", 0), reward["gold"])
        self.assertEqual(pets.farm_tickets(entry, "1"), reward["tickets"])
        self.assertEqual(len(pets.get_pet(entry, "1")["inventory"]), 1)

        # The double-tap: the row is already "accepted", so the second press must refuse
        # outright and touch none of the four payouts above.
        ok, msg, second_receipt = quests.review(entry, submission_id, "mod1", True, now=day)
        self.assertFalse(ok)
        self.assertEqual(second_receipt, {})
        self.assertEqual(economy.balance(entry, "1", 0), reward["gold"])
        self.assertEqual(pets.farm_tickets(entry, "1"), reward["tickets"])
        self.assertEqual(len(pets.get_pet(entry, "1")["inventory"]), 1)

    def test_a_painter_with_no_creature_is_paid_what_can_actually_reach_them(self):
        """Quests are a PAINTING task, so they deliberately do not require a pet -- the
        chat is full of people who never bought a cage, and the ticket wallet is at the
        top of pets' store for the same reason. But two of the four legs have nowhere to
        land without one: pets.award_xp silently no-ops and grant_random_drop declines,
        neither of which reports its own failure. The receipt must say what really
        happened, or a moderator reads out experience that went nowhere.
        """
        entry = "chat"
        day = datetime(2026, 8, 9, 9, 0)
        payload = quests.daily_quest(entry, "1", now=day)   # no _tame: no creature at all
        self.assertFalse(payload["has_pet"])
        quest = payload["quest"]
        quests.set_reward(entry, quest["difficulty"], "drop_chance", 1.0)
        reward = quests.rewards_for(entry, quest["difficulty"])

        self.assertTrue(quests.submit(entry, "1", quest["code"], now=day)[0])
        submission_id = quests.pending(entry)[0]["id"]
        ok, message, receipt = quests.review(entry, submission_id, "mod1", True, now=day)
        self.assertTrue(ok, message)

        # What lands, lands in full.
        self.assertEqual(economy.balance(entry, "1", 0), reward["gold"])
        self.assertEqual(pets.farm_tickets(entry, "1"), reward["tickets"])
        # What cannot land is reported as zero rather than as the nominal reward.
        self.assertEqual(receipt["xp"], 0)
        self.assertIsNone(receipt["item"])
        self.assertFalse(receipt["has_pet"])
        self.assertNotIn("опыта", message)
        self.assertIn("нет существа", message)

    def test_rejecting_pays_nothing_and_leaves_the_same_quest_open_for_a_fresh_try(self):
        """Rule two of quests.py: a rejection is feedback, not a punishment. Burning the
        day's quest on a bad photo would make moderators reluctant to ever reject one."""
        entry = "chat"
        self._tame(entry, "1")
        day = datetime(2026, 8, 9, 9, 0)
        quest, _submission_id, receipt = self._finish_quest(entry, "1", day, accept=False)

        self.assertEqual(receipt, {})
        self.assertEqual(economy.balance(entry, "1", 0), 0)
        self.assertEqual(pets.farm_tickets(entry, "1"), 0)
        self.assertEqual(pets.get_pet(entry, "1")["inventory"], [])

        again = quests.daily_quest(entry, "1", now=day + timedelta(minutes=5))
        self.assertEqual(again["status"], "open")
        self.assertEqual(again["quest"]["code"], quest["code"])
        self.assertIsNone(again["submission"])

        ok, msg = quests.submit(entry, "1", quest["code"], now=day + timedelta(minutes=6))
        self.assertTrue(ok, msg)

    def test_rejection_requires_feedback_for_the_player(self):
        entry = "chat"
        day = datetime(2026, 8, 9, 9, 0)
        quest = quests.daily_quest(entry, "1", now=day)["quest"]
        self.assertTrue(quests.submit(entry, "1", quest["code"], now=day)[0])
        submission_id = quests.pending(entry)[0]["id"]

        ok, message, _ = quests.review(entry, submission_id, "mod1", False, now=day)
        self.assertFalse(ok)
        self.assertIn("причин", message.lower())
        self.assertEqual(quests.pending(entry)[0]["id"], submission_id)


class QuestIdeaTests(QuestsTestCase):
    def test_ideas_are_kept_in_a_moderator_inbox_newest_first(self):
        self.assertTrue(quests.suggest_idea(
            "chat", "1", "Добавить квест на необычную подставку", author_name="Вася"
        )[0])
        self.assertTrue(quests.suggest_idea(
            "chat", "2", "Квест на покрас миниатюры", author_username="masha"
        )[0])

        rows = quests.ideas("chat")
        self.assertEqual([row["user_id"] for row in rows], ["2", "1"])
        self.assertEqual(rows[0]["author_username"], "masha")
        self.assertFalse(quests.suggest_idea("chat", "3", "   ")[0])


class QuestCatalogueEditTests(QuestsTestCase):
    def test_rotation_can_toggle_each_kind_without_falling_back_to_a_disabled_pool(self):
        entry = "chat"
        paint = catalog.PAINT_QUESTS[0]
        real = catalog.REAL_QUESTS[0]
        self.assertTrue(quests.set_quest_enabled(entry, paint.code, False)[0])
        self.assertTrue(quests.set_quest_enabled(entry, real.code, False)[0])
        states = {row["code"]: row["enabled"] for row in quests.catalog_entries(entry)}
        self.assertFalse(states[paint.code])
        self.assertFalse(states[real.code])
        self.assertTrue(quests.set_quest_enabled(entry, paint.code, True)[0])
        self.assertTrue({row["code"]: row["enabled"] for row in quests.catalog_entries(entry)}[paint.code])

    def test_moderator_text_edits_are_used_on_the_board_and_in_the_review_queue(self):
        entry = "chat"
        quest = catalog.PAINT_QUESTS[0]
        text = {
            "title": "Новый бриф", "subject": "Новую деталь", "technique": "Тонкие слои.",
            "hint": "Не используй старую работу.", "proof": "Фото новой детали.",
        }
        self.assertTrue(quests.set_quest_text(entry, quest.code, text)[0])
        self.assertEqual(quests._quest_payload(entry, quest, quests._load(entry))["title"], text["title"])
        with patch("random.choice", return_value=quest):
            quests.daily_quest(entry, "1")
        self.assertTrue(quests.submit(entry, "1", quest.code)[0])
        row = quests.pending(entry)[0]
        self.assertEqual(row["technique"], text["technique"])
        self.assertEqual(row["proof"], text["proof"])


class RewardTableTests(QuestsTestCase):
    def test_only_hard_quest_cards_advertise_the_fixed_scroll_chance(self):
        data = quests._load("chat")
        easy = next(row for row in catalog.QUESTS if row.difficulty == 3)
        hard = next(row for row in catalog.QUESTS if row.difficulty == 4)
        brutal = next(row for row in catalog.QUESTS if row.difficulty == 5)

        self.assertNotIn("scroll_chance", quests._quest_payload("chat", easy, data)["reward"])
        self.assertEqual(
            quests._quest_payload("chat", hard, data)["reward"]["scroll_chance"],
            pets.HARD_QUEST_SCROLL_CHANCES[4],
        )
        self.assertEqual(
            quests._quest_payload("chat", brutal, data)["reward"]["scroll_chance"],
            pets.HARD_QUEST_SCROLL_CHANCES[5],
        )

    def test_set_reward_clamps_out_of_range_values_to_the_configured_limits(self):
        """A moderator's fat-fingered 50000 must not become the chat's new gold reward --
        REWARD_LIMITS exists precisely because there is no undo for coins already spent."""
        entry = "chat"
        ok, msg = quests.set_reward(entry, 1, "gold", 50_000)
        self.assertTrue(ok, msg)
        self.assertEqual(quests.rewards_for(entry, 1)["gold"], quests.REWARD_LIMITS["gold"][1])

        ok, msg = quests.set_reward(entry, 1, "tickets", -5)
        self.assertTrue(ok, msg)
        self.assertEqual(
            quests.rewards_for(entry, 1)["tickets"], quests.REWARD_LIMITS["tickets"][0]
        )

        ok, msg = quests.set_reward(entry, 1, "drop_chance", 3)
        self.assertTrue(ok, msg)
        self.assertEqual(
            quests.rewards_for(entry, 1)["drop_chance"], quests.REWARD_LIMITS["drop_chance"][1]
        )

    def test_set_reward_rejects_an_unknown_field(self):
        entry = "chat"
        ok, msg = quests.set_reward(entry, 1, "luck", 10)
        self.assertFalse(ok)
        self.assertEqual(quests.rewards_for(entry, 1), quests.REWARDS_BY_DIFFICULTY[1])

    def test_set_reward_rejects_a_non_numeric_value(self):
        entry = "chat"
        ok, msg = quests.set_reward(entry, 2, "gold", "lots")
        self.assertFalse(ok)
        self.assertEqual(quests.rewards_for(entry, 2), quests.REWARDS_BY_DIFFICULTY[2])

    def test_set_reward_rejects_an_out_of_range_difficulty(self):
        ok, msg = quests.set_reward("chat", 9, "gold", 100)
        self.assertFalse(ok)


class QuestRotationTests(QuestsTestCase):
    def test_disabling_a_quest_removes_it_from_the_rotation(self):
        entry = "chat"
        code = catalog.QUESTS[0].code
        ok, msg = quests.set_quest_enabled(entry, code, False)
        self.assertTrue(ok, msg)
        self.assertNotIn(code, {q.code for q in quests.available_quests(entry)})

        ok, msg = quests.set_quest_enabled(entry, code, True)
        self.assertTrue(ok, msg)
        self.assertIn(code, {q.code for q in quests.available_quests(entry)})

    def test_set_quest_enabled_refuses_to_disable_every_quest_of_one_kind(self):
        """Each assignment pool must keep at least one quest of its own kind alive."""
        entry = "chat"
        codes = [quest.code for quest in catalog.PAINT_QUESTS]
        for code in codes[:-1]:
            ok, msg = quests.set_quest_enabled(entry, code, False)
            self.assertTrue(ok, msg)

        ok, msg = quests.set_quest_enabled(entry, codes[-1], False)
        self.assertFalse(ok, msg)
        self.assertTrue(quests.available_quests(entry))

    def test_disabling_a_quest_leaves_a_live_assignment_on_it_alone(self):
        entry = "chat"
        with patch("random.choice", return_value=catalog.QUESTS[0]):
            payload = quests.daily_quest(entry, "1", now=datetime(2026, 8, 9, 9, 0))
        code = payload["quest"]["code"]

        ok, msg = quests.set_quest_enabled(entry, code, False)
        self.assertTrue(ok, msg)

        still_live = quests.daily_quest(entry, "1", now=datetime(2026, 8, 9, 9, 5))
        self.assertEqual(still_live["quest"]["code"], code)


class HistoryAndStatsTests(QuestsTestCase):
    def test_history_and_submissions_are_newest_first_and_scoped_to_one_user(self):
        entry = "chat"
        self._tame(entry, "1")
        self._tame(entry, "2")

        quest1, _sub1, _r1 = self._finish_quest(entry, "1", datetime(2026, 8, 1, 9, 0))
        quest2, _sub2, _r2 = self._finish_quest(entry, "2", datetime(2026, 8, 1, 10, 0))
        quest3, _sub3, _r3 = self._finish_quest(entry, "1", datetime(2026, 8, 2, 9, 0))

        history_1 = quests.history(entry, user_id="1")
        self.assertEqual([row["code"] for row in history_1], [quest3["code"], quest1["code"]])
        self.assertTrue(all(row["outcome"] == "accepted" for row in history_1))

        history_2 = quests.history(entry, user_id="2")
        self.assertEqual([row["code"] for row in history_2], [quest2["code"]])

        submissions_1 = quests.submissions(entry, user_id="1")
        self.assertEqual([row["code"] for row in submissions_1], [quest3["code"], quest1["code"]])
        self.assertTrue(all(row["status"] == "accepted" for row in submissions_1))

    def test_stats_for_reports_finished_count_best_difficulty_and_total_gold(self):
        entry = "chat"
        self._tame(entry, "1")
        quest1, _sub1, receipt1 = self._finish_quest(entry, "1", datetime(2026, 8, 1, 9, 0))
        quest2, _sub2, receipt2 = self._finish_quest(entry, "1", datetime(2026, 8, 2, 9, 0))

        stats_row = quests.stats_for(entry, "1")

        self.assertEqual(stats_row["done"], 2)
        self.assertEqual(
            stats_row["best_difficulty"], max(quest1["difficulty"], quest2["difficulty"])
        )
        self.assertEqual(stats_row["gold"], receipt1["gold"] + receipt2["gold"])


class StorageRobustnessTests(QuestsTestCase):
    def test_a_missing_store_file_loads_as_empty_rather_than_raising(self):
        self.assertEqual(quests._load("chat"), quests._empty())

    def test_a_store_file_with_invalid_json_loads_as_empty_rather_than_raising(self):
        entry = "chat"
        quests._path(entry).write_text("{not valid json", encoding="utf-8")
        self.assertEqual(quests._load(entry), quests._empty())
        # And assignment keeps working afterwards -- a bad file must not wedge the module.
        payload = quests.daily_quest(entry, "1", now=datetime(2026, 8, 9, 9, 0))
        self.assertIsNotNone(payload["quest"])

    def test_a_store_file_holding_the_wrong_json_shape_loads_as_empty(self):
        entry = "chat"
        quests._path(entry).write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(quests._load(entry), quests._empty())


if __name__ == "__main__":
    unittest.main()


class QuestModeratorTests(QuestsTestCase):
    """Quest review is its own small delegation. It is NOT chat admin and NOT the badge
    manager list: judging a painting is "does this look like NMM", which any trusted
    painter can do, while the other two carry the ban button and the badge cupboard."""

    def test_a_moderator_is_matched_by_id_or_by_username(self):
        """Somebody can be appointed from a @username before they have ever opened the
        Mini App, and the signed initData the page verifies carries a username too. If
        only the id matched, an appointment would not take effect until their next visit
        -- which looks exactly like the button being broken."""
        entry = "chat"
        self.assertFalse(quests.is_moderator(entry, "77", "vasya"))

        ok, message = quests.add_moderator(entry, "77", "vasya", "Вася", "1", "Админ")
        self.assertTrue(ok, message)
        self.assertTrue(quests.is_moderator(entry, "77"))
        self.assertTrue(quests.is_moderator(entry, None, "@VASYA"))
        self.assertTrue(quests.is_moderator(entry, None, "vasya"))
        # And nobody else is swept in by either route.
        self.assertFalse(quests.is_moderator(entry, "99", "petya"))
        self.assertFalse(quests.is_moderator(entry, None, ""))
        self.assertFalse(quests.is_moderator(entry, None, None))

    def test_appointing_the_same_person_twice_says_so_and_changes_nothing(self):
        entry = "chat"
        self.assertTrue(quests.add_moderator(entry, "77", "vasya", "Вася", "1", "Админ")[0])
        again, message = quests.add_moderator(entry, "77", "vasya", "Вася", "1", "Админ")
        self.assertFalse(again)
        self.assertIn("и так", message)
        self.assertEqual(len(quests.moderators(entry)), 1)

    def test_removing_a_moderator_takes_the_permission_away_by_both_routes(self):
        entry = "chat"
        quests.add_moderator(entry, "77", "vasya", "Вася", "1", "Админ")
        ok, message = quests.remove_moderator(entry, "77")
        self.assertTrue(ok, message)
        self.assertFalse(quests.is_moderator(entry, "77"))
        self.assertFalse(quests.is_moderator(entry, None, "vasya"))
        self.assertFalse(quests.remove_moderator(entry, "77")[0])

    def test_moderators_are_per_chat(self):
        quests.add_moderator("chat-a", "77", "vasya", "Вася", "1", "Админ")
        self.assertTrue(quests.is_moderator("chat-a", "77"))
        self.assertFalse(quests.is_moderator("chat-b", "77"))

    def test_a_delegated_moderator_is_never_shown_the_appointment_buttons(self):
        """The line this screen exists to hold: a moderator can review, and can see who
        else can, but cannot widen the list. Otherwise one appointment quietly becomes the
        power to hand out the same appointment forever."""
        entry = "chat"
        quests.add_moderator(entry, "77", "vasya", "Вася", "1", "Админ")

        _text, keyboard = pets_ui.quest_mods_view(entry, "77", can_appoint=False)
        actions = {
            pets_ui.parse_callback(button["callback_data"])[1]
            for row in keyboard["inline_keyboard"] for button in row
        }
        self.assertEqual(actions, {"main", "questreview"})

        _text, keyboard = pets_ui.quest_mods_view(entry, "1", can_appoint=True)
        actions = {
            pets_ui.parse_callback(button["callback_data"])[1]
            for row in keyboard["inline_keyboard"] for button in row
        }
        self.assertEqual(actions, {"main", "questreview", "questmodadd", "questmoddel"})

    def test_the_arena_menu_only_offers_the_screen_to_somebody_who_can_use_it(self):
        entry = "chat"
        economy.grant(entry, "1", pets_config.CAGE_PRICE + pets_config.TAME_PRICE, "test")
        self._tame(entry, "1")
        for allowed in (False, True):
            with self.subTest(quest_mod=allowed):
                _text, keyboard = pets_ui.main_view(entry, "1", 0, quest_mod=allowed)
                actions = {
                    pets_ui.parse_callback(button["callback_data"])[1]
                    for row in keyboard["inline_keyboard"] for button in row
                    if "callback_data" in button
                }
                self.assertEqual("questmods" in actions, allowed)

    def test_the_chat_moderation_view_has_accept_and_reject_for_the_oldest_submission(self):
        entry = "chat"
        quest = quests.daily_quest(entry, "1")["quest"]
        self.assertTrue(quests.submit(entry, "1", quest["code"], author_name="Вася")[0])
        text, keyboard = pets_ui.quest_review_view(entry, "77")
        self.assertIn(quest["title"], text)
        actions = {
            pets_ui.parse_callback(button["callback_data"])[1]
            for row in keyboard["inline_keyboard"] for button in row
        }
        self.assertEqual(actions, {"main", "questaccept", "questreject"})
