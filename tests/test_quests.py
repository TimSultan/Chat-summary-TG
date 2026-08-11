import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import economy
import pets
import pets_config
import pets_quest_catalog as catalog
import quests


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
            quests.parse_hashtag("вот моя работа #quest-nmm, сделано за вечер"), "nmm"
        )

    def test_is_case_insensitive(self):
        self.assertEqual(quests.parse_hashtag("готово! #QUEST-NMM"), "nmm")

    def test_returns_none_for_an_unknown_code(self):
        self.assertIsNone(quests.parse_hashtag("#quest-not-a-real-code"))

    def test_does_not_read_a_versioned_tag_as_its_base_code(self):
        """The character class that lets a code survive being hand-typed ([a-z0-9-]) also
        swallows a trailing "-v2" into the same match, so a caption tagged for a different,
        unknown variant must not silently credit the base quest."""
        self.assertIsNone(quests.parse_hashtag("работа по технике #quest-nmm-v2"))


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
