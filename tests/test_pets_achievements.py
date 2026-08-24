"""Ачивки: the promises the catalogue makes to the save file and to the screen.

Two of these matter more than the rest. Codes are written into save files and can never
be renamed, and a brand-new pet must earn NOTHING -- a feature that opens half-completed
has nothing left to chase, which is the whole reason almost none of it is retroactive.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pets
import pets_achievements as achievements

CHAT = "ach-chat"
USER = 8800
RICH_XP = 10 ** 9

# A pet that has done everything the game offers, used to prove the catalogue is
# reachable. The rows it still misses are the deliberately exclusive ones -- you cannot
# wear a full cursed set and a full legendary set at once.
MAXED = {
    "level": 40, "cage_level": 6, "farm_level": 12, "power": 5_000,
    "stats": {"strength": 90, "health": 90, "agility": 90, "luck": 90, "magic": 90},
    "effective_stats": {"strength": 90, "health": 90, "agility": 90, "luck": 90,
                        "magic": 90, "armor": 60},
    "wins": 500, "fights": 900, "pet_wins": 300, "mob_wins": 400, "boss_wins": 40,
    "best_weapon_wins": 150, "deepest_floor": 45,
    "phoenix_wins": 5, "phoenix_perfect": True,
    "weapons_found": 632, "weapons_total": 632, "scrolls_owned": 40, "scrolls_total": 40,
    "equipped_slots": 5, "equipped_rarities": {"legendary": 5}, "equipped_cursed": 0,
    "equipped_magic": 1, "personal_paints": 9, "scroll_paints": 4, "runes": 30,
    "gold_earned": 500_000, "gold_spent": 90_000, "rubies": 40,
    "farm_tickets": 5, "dungeon_tickets": 5,
    "quests_done": 60, "quest_best_difficulty": 5,
    "figurines_painted": 44, "messages": 9_000, "active_days": 300, "best_work_posts": 2,
}


class CatalogueContractTests(unittest.TestCase):
    def test_codes_are_unique_and_shaped_to_live_in_a_save_file_for_ever(self):
        """A code is persisted the moment somebody earns the row.

        Renaming one later would silently un-earn it for everybody who has it, so the
        shape is pinned here rather than trusted to a future edit.
        """
        codes = [row.code for row in achievements.catalogue()]
        self.assertEqual(len(set(codes)), len(codes))
        for code in codes:
            with self.subTest(code=code):
                self.assertTrue(code.isascii())
                self.assertTrue(code.replace("_", "").isalnum())
                self.assertEqual(code, code.lower())

    def test_every_row_says_what_to_do_and_pays_for_it(self):
        """A row with no reward is a line in a list, not an achievement."""
        for row in achievements.catalogue():
            with self.subTest(row.code):
                self.assertTrue(row.icon and row.name and row.description)
                self.assertTrue(row.rubies or row.farm_tickets or row.dungeon_tickets)
                # The owner's ceiling. Diamonds are the scarce currency in this game and
                # a single achievement must never be a shortcut around earning them.
                self.assertLessEqual(row.rubies, 3)
                self.assertGreaterEqual(row.rubies, 0)

    def test_a_broken_predicate_can_never_take_the_screen_down(self):
        """The list is drawn from these; one bad row must not lose the other forty."""
        self.assertEqual(achievements.earned({}), ())
        self.assertEqual(achievements.earned({"stats": None, "level": "nonsense"}), ())

    def test_a_legacy_row_is_never_decided_by_the_ordinary_evaluation(self):
        """It is settled once at migration, from evidence that may later age out."""
        legacy = [row for row in achievements.catalogue() if row.legacy]
        self.assertTrue(legacy)
        for row in legacy:
            with self.subTest(row.code):
                self.assertNotIn(row.code, achievements.earned(MAXED))

    def test_the_old_phoenix_is_recognised_by_where_the_runner_stood(self):
        """Standing past floor five required clearing floor five, and floor five was the
        bird. The fight log cannot answer this -- it keeps the last five hundred fights
        chat-wide and deletes the rest."""
        row = achievements.by_code("old_phoenix")
        self.assertIsNotNone(row)
        self.assertTrue(row.legacy)
        self.assertTrue(row.check({"deepest_floor": 6}))
        self.assertFalse(row.check({"deepest_floor": 5}))

    def test_some_rows_are_hidden_because_naming_them_spoils_them(self):
        hidden = [row for row in achievements.catalogue() if row.hidden]
        self.assertTrue(hidden)
        self.assertTrue(all(row.rubies for row in hidden), "скрытая ачивка должна платить")

    def test_the_catalogue_is_actually_reachable(self):
        """A row nobody can earn is the same as no row at all."""
        earned = set(achievements.earned(MAXED))
        catalogue = [row for row in achievements.catalogue() if not row.legacy]
        self.assertGreaterEqual(len(earned), len(catalogue) * 3 // 4)
        # What a maxed pet misses is only ever a build it did not choose -- a full cursed
        # set excludes a full legendary one, and the hidden jokes ask for the opposite of
        # being maxed.
        missed = {row.code for row in catalogue} - earned
        self.assertTrue(missed <= {
            "all_cursed", "all_in_luck", "naked_hero", "pacifist", "one_stat",
        }, missed)


class LiveAchievementTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self._patch = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        self._patch.start()
        self.addCleanup(self._temporary.cleanup)
        self.addCleanup(self._patch.stop)
        # The chat aggregate is cached per chat and the cache outlives one test: every
        # case here uses the same chat name against a fresh directory, so a warm entry
        # from the previous test would answer for a store that no longer exists.
        pets._achievement_chat_cache.clear()
        self.assertTrue(pets.buy_cage(CHAT, USER, RICH_XP)[0])
        self.assertTrue(pets.tame(CHAT, USER, RICH_XP, "Пеструшка", "f", "Хозяин")[0])

    def test_a_brand_new_pet_has_earned_nothing_at_all(self):
        """The feature must not ship pre-completed.

        This is the whole reason nothing but the closed rows is retroactive: a veteran
        opening a wall of already-finished achievements has nothing left to chase, which
        is the opposite of what the screen is for.
        """
        view = pets.achievements_view(CHAT, USER)

        self.assertEqual(view["earned"], 0)
        self.assertEqual(view["claimable"]["count"], 0)
        self.assertTrue(view["rows"])
        self.assertFalse([row for row in view["rows"] if row["earned"]])
        # And a hidden row is not even listed until it is earned.
        self.assertFalse([row for row in view["rows"] if row["hidden"]])

    def test_opening_the_app_never_scans_the_chat_history(self):
        """The badge rides on every state payload; the evaluation must not.

        Working out what is newly earned reads the chat aggregate, which parses one file
        per recorded day, plus the whole quest history. On a chat that has been running
        for a year that is a page load the Mini App does not finish -- and it did not:
        putting the full evaluation on the payload stopped the app opening at all.
        """
        import pets_web
        import stats

        seen = []
        real = stats.aggregate_all_time
        with patch.object(stats, "aggregate_all_time",
                          side_effect=lambda *a, **k: (seen.append(1), real(*a, **k))[1]):
            payload = pets_web._state_payload(CHAT, USER, 0, "")
            self.assertEqual(seen, [], "загрузка приложения не должна читать историю чата")
            # And the summary it does carry is enough to draw the badge.
            self.assertIn("claimable", payload["achievements"])
            self.assertIn("earned", payload["achievements"])

            pets.achievements_view(CHAT, USER)
            self.assertEqual(len(seen), 1, "список считается ровно при открытии")

    def test_earning_and_claiming_are_two_separate_states(self):
        """Collapsing them would make the reward the achievement: a crash between the two
        would either lose the row or pay it twice, and the screen could not say
        "earned, go and collect"."""
        self._win_a_fight()

        view = pets.achievements_view(CHAT, USER)
        self.assertGreaterEqual(view["earned"], 1)
        self.assertGreaterEqual(view["claimable"]["count"], 1)
        row = next(row for row in view["rows"] if row["earned"])
        self.assertFalse(row["claimed"])

        ok, note, paid = pets.claim_achievements(CHAT, USER)

        self.assertTrue(ok, note)
        self.assertGreaterEqual(paid["count"], 1)
        after = pets.achievements_view(CHAT, USER)
        self.assertEqual(after["claimable"]["count"], 0)
        self.assertTrue(next(r for r in after["rows"] if r["code"] == row["code"])["claimed"])

    def test_a_second_press_pays_nothing_more(self):
        """The button is one press away from being pressed twice by a bad connection."""
        self._win_a_fight()
        self.assertTrue(pets.claim_achievements(CHAT, USER)[0])
        rubies = pets.ruby_balance(CHAT, USER)
        tickets = pets.farm_tickets(CHAT, USER)

        ok, _note, _paid = pets.claim_achievements(CHAT, USER)

        self.assertFalse(ok)
        self.assertEqual(pets.ruby_balance(CHAT, USER), rubies)
        self.assertEqual(pets.farm_tickets(CHAT, USER), tickets)

    def test_the_reward_actually_reaches_the_wallets(self):
        self._win_a_fight()
        before = (pets.ruby_balance(CHAT, USER), pets.farm_tickets(CHAT, USER),
                  pets.dungeon_tickets(CHAT, USER))

        _ok, _note, paid = pets.claim_achievements(CHAT, USER)

        after = (pets.ruby_balance(CHAT, USER), pets.farm_tickets(CHAT, USER),
                 pets.dungeon_tickets(CHAT, USER))
        self.assertEqual(after[0] - before[0], paid["rubies"])
        self.assertEqual(after[1] - before[1], paid["farm_tickets"])
        self.assertEqual(after[2] - before[2], paid["dungeon_tickets"])

    def test_the_reported_reward_is_what_was_actually_minted(self):
        """The note is read next to the wallet, so the two have to agree.

        The ruby granter answers with the resulting BALANCE rather than the amount it
        credited, which makes summing its answers report somebody's whole purse as the
        prize for one press.
        """
        data = pets._load(CHAT)
        data["pets"][str(USER)]["dungeon_deepest"] = 18
        pets._save(CHAT, data)
        pets.achievements_view(CHAT, USER)

        _ok, note, paid = pets.claim_achievements(CHAT, USER)

        self.assertEqual(pets.ruby_balance(CHAT, USER), paid["rubies"])
        self.assertIn(f"{paid['rubies']} 💎", note)

    def test_the_closed_row_is_credited_once_and_only_to_whoever_earned_it(self):
        """A pet that never got past the fifth floor never met the old Phoenix."""
        data = pets._load(CHAT)
        data["pets"][str(USER)]["dungeon_deepest"] = 12
        pets._save(CHAT, data)

        self.assertGreaterEqual(pets.backfill_legacy_achievements(CHAT), 1)
        unlocked = pets.get_pet(CHAT, USER)["achievements"]["unlocked"]
        self.assertIn("old_phoenix", unlocked)

        # Running again decides nothing further: the marker is what stops a later load
        # from re-asking a question whose evidence may by then have aged away.
        self.assertEqual(pets.backfill_legacy_achievements(CHAT), 0)

    def test_a_shallow_runner_is_not_handed_the_closed_row(self):
        pets.backfill_legacy_achievements(CHAT)
        self.assertNotIn(
            "old_phoenix", pets.get_pet(CHAT, USER)["achievements"]["unlocked"],
        )

    def _win_a_fight(self):
        """The cheapest real unlock there is, so the tests above are about the plumbing."""
        data = pets._load(CHAT)
        record = data["pets"][str(USER)]
        record.setdefault("weapon_records", {})["w001"] = {
            "pet_wins": 1, "mob_wins": 0, "boss_wins": 0,
        }
        pets._save(CHAT, data)


if __name__ == "__main__":
    unittest.main()
