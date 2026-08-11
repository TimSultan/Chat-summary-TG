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
        economy.grant(entry, uid, pets_config.CAGE_PRICE + pets_config.TAME_PRICE, "test")
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
        ok, msg, receipt = quests.review(entry, submission_id, "mod1", accept, now=now)
        self.assertTrue(ok, msg)
        return quest, submission_id, receipt


class AssignmentTests(QuestsTestCase):
    def test_an_unfinished_quest_is_never_replaced_by_a_new_day(self):
        """One technique can take days to actually paint. Rule one of quests.py's module
        docstring is that the day stamp only rate-limits handing out a NEW quest and never
        expires one that is still open -- if this broke, whoever is a few days into a hard
        NMM quest would find it swapped out from under them for something unrelated."""
        entry = "chat"
        first = quests.daily_quest(entry, "1", now=datetime(2026, 8, 9, 9, 0))
        code = first["quest"]["code"]

        again_same_day = quests.daily_quest(entry, "1", now=datetime(2026, 8, 9, 20, 0))
        self.assertEqual(again_same_day["quest"]["code"], code)

        four_days_later = quests.daily_quest(entry, "1", now=datetime(2026, 8, 13, 9, 0))
        self.assertEqual(four_days_later["quest"]["code"], code)
        self.assertEqual(four_days_later["status"], "open")

    def test_finishing_a_quest_rests_the_player_until_the_next_day(self):
        """Rule two: a fast painter who clears their quest at breakfast cannot just pull a
        second one that afternoon. `status` has to read "resting", not silently hand out a
        fresh code, or the one-a-day cadence the reward table is priced around breaks."""
        entry = "chat"
        self._tame(entry, "1")
        day = datetime(2026, 8, 9, 9, 0)
        quest, _submission_id, _receipt = self._finish_quest(entry, "1", day)

        resting = quests.daily_quest(entry, "1", now=day + timedelta(hours=6))
        self.assertEqual(resting["status"], "resting")
        self.assertIsNone(resting["quest"])
        self.assertEqual(resting["last"]["code"], quest["code"])

        next_day = quests.daily_quest(entry, "1", now=day + timedelta(days=1))
        self.assertEqual(next_day["status"], "open")
        self.assertIsNotNone(next_day["quest"])


class RerollTests(QuestsTestCase):
    def test_reroll_swaps_the_quest_and_is_capped_at_rerolls_per_quest(self):
        first = quests.daily_quest("chat", "1", now=datetime(2026, 8, 9, 9, 0))
        self.assertEqual(first["rerolls_left"], quests.REROLLS_PER_QUEST)

        for used in range(1, quests.REROLLS_PER_QUEST + 1):
            ok, msg = quests.reroll("chat", "1", now=datetime(2026, 8, 9, 9, used))
            self.assertTrue(ok, msg)
            payload = quests.daily_quest("chat", "1", now=datetime(2026, 8, 9, 9, used))
            self.assertEqual(payload["rerolls_left"], quests.REROLLS_PER_QUEST - used)

        ok, msg = quests.reroll("chat", "1", now=datetime(2026, 8, 9, 10, 0))
        self.assertFalse(ok)
        self.assertIn("Реролов", msg)

    def test_reroll_refuses_while_a_submission_is_under_review(self):
        entry = "chat"
        payload = quests.daily_quest(entry, "1", now=datetime(2026, 8, 9, 9, 0))
        code = payload["quest"]["code"]
        ok, msg = quests.submit(entry, "1", code, now=datetime(2026, 8, 9, 9, 5))
        self.assertTrue(ok, msg)

        ok, msg = quests.reroll(entry, "1", now=datetime(2026, 8, 9, 9, 10))
        self.assertFalse(ok)
        self.assertIn("проверке", msg)

    def test_finishing_a_quest_resets_the_reroll_allowance_for_the_next_one(self):
        """Rerolls are commissioned per ASSIGNMENT, not per day: burn both on a quest you
        end up finishing anyway, and the NEXT quest still has to start with two, not arrive
        already half-spent on account of a technique it has nothing to do with."""
        entry = "chat"
        self._tame(entry, "1")
        day = datetime(2026, 8, 9, 9, 0)
        quests.daily_quest(entry, "1", now=day)
        for _ in range(quests.REROLLS_PER_QUEST):
            ok, msg = quests.reroll(entry, "1", now=day)
            self.assertTrue(ok, msg)
        self._finish_quest(entry, "1", day)

        next_day = quests.daily_quest(entry, "1", now=day + timedelta(days=1))
        self.assertEqual(next_day["rerolls_left"], quests.REROLLS_PER_QUEST)


class SubmissionTests(QuestsTestCase):
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


class EscalatingRerollTests(QuestsTestCase):
    """A reroll climbs a difficulty. Without that it is a free respin, and since the
    reward table pays BY difficulty, a player would simply spin until the easiest quest
    came up and collect the same money for less work."""

    def test_each_reroll_hands_out_a_harder_challenge(self):
        entry = "chat"
        day = datetime(2026, 8, 9, 9, 0)
        first = quests.daily_quest(entry, "1", now=day)["quest"]
        levels = [first["difficulty"]]
        for _ in range(quests.REROLLS_PER_QUEST):
            ok, message = quests.reroll(entry, "1", now=day)
            self.assertTrue(ok, message)
            levels.append(quests.daily_quest(entry, "1", now=day)["quest"]["difficulty"])

        # Every step climbs -- unless it is already at the top rung, where there is
        # nowhere higher to go and another quest of the same difficulty is dealt instead.
        # Asserted as "never goes DOWN, and rises whenever it can" rather than as strictly
        # increasing: a first deal of difficulty 4 legitimately produces 4 -> 5 -> 5, and
        # this test used to fail roughly one run in four because of it.
        top = max(catalog.DIFFICULTIES)
        for lower, higher in zip(levels, levels[1:]):
            self.assertGreaterEqual(higher, lower, levels)
            if lower < top:
                self.assertGreater(higher, lower, levels)
        self.assertLessEqual(max(levels), top)

    def test_a_reroll_at_the_top_deals_another_hard_one_rather_than_refusing(self):
        """Somebody who cannot paint THIS brutal technique should still be able to trade
        it for a different brutal one -- the climb is a price, not a dead end."""
        entry = "chat"
        day = datetime(2026, 8, 9, 9, 0)
        hardest = catalog.quests_by_difficulty(max(catalog.DIFFICULTIES))[0]
        quests.daily_quest(entry, "1", now=day)
        data = quests._load(entry)
        data["assignments"]["1"]["code"] = hardest.code
        quests._save(entry, data)

        ok, message = quests.reroll(entry, "1", now=day)
        self.assertTrue(ok, message)
        swapped = quests.daily_quest(entry, "1", now=day)["quest"]
        self.assertNotEqual(swapped["code"], hardest.code)
        self.assertEqual(swapped["difficulty"], max(catalog.DIFFICULTIES))

    def test_a_reroll_never_deals_a_real_quest(self):
        """Квесты в реале are taken from a shelf, never dealt -- a reroll that handed
        somebody "buy a loupe" instead of a painting technique would be a category error
        and would also skip the taking rules entirely."""
        entry = "chat"
        day = datetime(2026, 8, 9, 9, 0)
        real_codes = {quest.code for quest in catalog.REAL_QUESTS}
        for _ in range(quests.REROLLS_PER_QUEST):
            quests.reroll(entry, "1", now=day)
        board = quests.daily_quest(entry, "1", now=day)
        self.assertNotIn(board["quest"]["code"], real_codes)


class RealQuestTests(QuestsTestCase):
    """Квесты в реале are DEALT now, exactly like painting challenges: one at a time, at
    random, into a slot of their own. They were briefly a browsable shelf; a list of 35 is
    a menu to shop the cheapest item off, and the reward table pays by difficulty."""

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

    def test_a_real_quest_is_sticky_and_rerolls_on_its_own_allowance(self):
        entry = "chat"
        day = datetime(2026, 8, 9, 9, 0)
        first = quests.real_quest(entry, "1", now=day)
        self.assertEqual(first["rerolls_left"], quests.REROLLS_PER_QUEST)
        self.assertEqual(
            quests.real_quest(entry, "1", now=day + timedelta(days=3))["quest"]["code"],
            first["quest"]["code"],
        )
        # Rerolling the real slot leaves the painting slot's allowance alone.
        quests.daily_quest(entry, "1", now=day)
        self.assertTrue(quests.reroll(entry, "1", now=day, kind="real")[0])
        self.assertEqual(
            quests.real_quest(entry, "1", now=day)["rerolls_left"],
            quests.REROLLS_PER_QUEST - 1,
        )
        self.assertEqual(
            quests.daily_quest(entry, "1", now=day)["rerolls_left"],
            quests.REROLLS_PER_QUEST,
        )

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


class RewardTableTests(QuestsTestCase):
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

    def test_set_quest_enabled_refuses_to_disable_every_quest_at_once(self):
        """The pool backing every assignment must never run dry. An empty pool would not
        fail loudly -- it would just leave the next player silently unassigned."""
        entry = "chat"
        codes = [quest.code for quest in catalog.QUESTS]
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
        self.assertEqual(actions, {"main"})

        _text, keyboard = pets_ui.quest_mods_view(entry, "1", can_appoint=True)
        actions = {
            pets_ui.parse_callback(button["callback_data"])[1]
            for row in keyboard["inline_keyboard"] for button in row
        }
        self.assertEqual(actions, {"main", "questmodadd", "questmoddel"})

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
