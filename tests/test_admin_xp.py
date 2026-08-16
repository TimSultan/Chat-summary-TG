"""Undoing an XP grant without taking the money it was standing in for.

XP is the wrong lever for handing somebody coins, because coins are DERIVED from it
(economy.balance) -- so a grant meant to top up a wallet also rewrites /top. Taking the
XP back therefore takes the coins with it unless they are put back deliberately, which is
the whole point of this tool and the thing these tests pin.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import admin_xp
import economy
import pets
import stats

WPP = 20.0


class RevokeXpGrantTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        patcher = patch("stats._stats_dir", return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The tools resolve a store's filename to itself; mirror that here so `find()`
        # and the grants agree on which file they mean.
        key = patch("stats._cache_key", side_effect=lambda raw: raw)
        key.start()
        self.addCleanup(key.stop)

        pets.buy_cage("chat", "1", 0)
        pets.tame("chat", "1", 0, "Кломбик", "file", "Кломбик")
        pets.buy_cage("chat", "2", 0)
        pets.tame("chat", "2", 0, "Обычный", "file", "Игрок2")
        for found in list(self.root.glob("*_pets.json")):
            if found.name != "chat_pets.json":
                shutil.move(str(found), self.root / "chat_pets.json")

    def _xp(self, user_id="1"):
        rows = stats.aggregate_all_time("chat")
        return rows.get(user_id, stats.UserStats(user_id=user_id)).xp(WPP)

    def _coins(self, user_id="1"):
        return economy.balance("chat", user_id, self._xp(user_id))

    def test_a_grant_inflates_xp_and_revoking_it_hands_the_money_back_as_coins(self):
        stats.grant_xp_once("chat", "1", 10_000_000, "money")
        inflated_xp, funded = self._xp(), self._coins()
        self.assertEqual(inflated_xp, 10_000_000)
        self.assertEqual(funded, 10_000_000 // stats.XP_PER_COIN)

        self.assertEqual(admin_xp.main(["revoke", "Кломбик", "--yes"]), 0)

        # The leaderboard is clean again...
        self.assertEqual(self._xp(), 0)
        # ...and the wallet is untouched, to the coin. This is the assertion that matters:
        # without the compensation step this number would drop to zero.
        self.assertEqual(self._coins(), funded)

    def test_a_dry_run_changes_nothing(self):
        stats.grant_xp_once("chat", "1", 5_000, "money")
        self.assertEqual(admin_xp.main(["revoke", "Кломбик"]), 0)
        self.assertEqual(self._xp(), 5_000)
        self.assertEqual(len(stats.xp_grants_for("chat", "1")), 1)

    def test_running_it_twice_does_not_pay_twice(self):
        stats.grant_xp_once("chat", "1", 1_000_000, "money")
        admin_xp.main(["revoke", "Кломбик", "--yes"])
        once = self._coins()
        admin_xp.main(["revoke", "Кломбик", "--yes"])
        self.assertEqual(self._coins(), once)

    def test_no_compensate_really_takes_it_all_back(self):
        """The other half of the choice, for a grant that was simply wrong."""
        stats.grant_xp_once("chat", "1", 1_000_000, "mistake")
        before = self._coins()
        admin_xp.main(["revoke", "Кломбик", "--yes", "--no-compensate"])
        self.assertEqual(self._xp(), 0)
        self.assertLess(self._coins(), before)

    def test_one_grant_can_be_revoked_by_key_leaving_the_others(self):
        stats.grant_xp_once("chat", "1", 100, "earned-a-prize")
        stats.grant_xp_once("chat", "1", 9_000_000, "oops")
        admin_xp.main(["revoke", "Кломбик", "--key", "oops", "--yes"])
        remaining = stats.xp_grants_for("chat", "1")
        self.assertEqual(list(remaining), ["earned-a-prize"])
        self.assertEqual(self._xp(), 100)

    def test_nobody_else_is_touched(self):
        stats.grant_xp_once("chat", "1", 10_000_000, "money")
        stats.grant_xp_once("chat", "2", 250, "earned")
        admin_xp.main(["revoke", "Кломбик", "--yes"])
        self.assertEqual(self._xp("2"), 250)
        self.assertEqual(len(stats.xp_grants_for("chat", "2")), 1)

    def test_revoking_nothing_is_not_an_error(self):
        self.assertEqual(admin_xp.main(["revoke", "Кломбик", "--yes"]), 0)
        self.assertEqual(stats.revoke_xp_grants("chat", "1"), 0)
        self.assertEqual(stats.revoke_xp_grants("chat", "1", "no-such-key"), 0)


if __name__ == "__main__":
    unittest.main()
