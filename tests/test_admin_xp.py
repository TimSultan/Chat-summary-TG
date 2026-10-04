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


class _GrantStoreCase(unittest.TestCase):
    """A temporary stats directory with two tamed players, "1" (Кломбик) and "2"."""

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


class RevokeXpGrantTests(_GrantStoreCase):
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

    def test_revoking_everything_nets_a_negative_adjustment_instead_of_paying_for_it(self):
        """The resource panel undoes XP with a NEGATIVE running adjustment. Clearing it
        together with the grant it undid removes nothing, so nothing is paid -- it used to
        be read as 0 and the grant's full value was handed out a second time."""
        stats.grant_xp_once("chat", "1", 1_000_000, "money")
        stats.adjust_bonus_xp("chat", "1", -1_000_000, by="panel")
        xp_before, coins_before = self._xp(), self._coins()

        admin_xp.main(["revoke", "Кломбик", "--yes"])

        self.assertEqual(self._xp(), xp_before)
        self.assertEqual(self._coins(), coins_before)


class RetireXpGrantTests(_GrantStoreCase):
    """economy.retire_xp_grant: the startup conversion of a payment made as XP."""

    def test_the_grant_becomes_coins_and_leaves_the_leaderboard(self):
        stats.grant_xp_once("chat", "1", 10_000_000, "paid-as-xp")
        funded = self._coins()

        paid = economy.retire_xp_grant("chat", "1", "paid-as-xp")

        self.assertEqual(paid, 10_000_000 // stats.XP_PER_COIN)
        self.assertEqual(self._xp(), 0)
        self.assertEqual(self._coins(), funded)
        self.assertEqual(stats.xp_grants_for("chat", "1"), {})

    def test_running_it_on_every_start_pays_once(self):
        stats.grant_xp_once("chat", "1", 10_000_000, "paid-as-xp")
        economy.retire_xp_grant("chat", "1", "paid-as-xp")
        once = self._coins()
        self.assertEqual(economy.retire_xp_grant("chat", "1", "paid-as-xp"), 0)
        self.assertEqual(self._coins(), once)

    def test_a_grant_already_compensated_by_hand_is_not_paid_again(self):
        """admin_xp.py revoke, then a restart re-granted it: the money is already out."""
        stats.grant_xp_once("chat", "1", 10_000_000, "paid-as-xp")
        admin_xp.main(["revoke", "Кломбик", "--yes"])
        stats.grant_xp_once("chat", "1", 10_000_000, "paid-as-xp")
        compensated = economy.balance("chat", "1", 0)

        self.assertEqual(economy.retire_xp_grant("chat", "1", "paid-as-xp"), 0)
        self.assertEqual(self._xp(), 0)
        self.assertEqual(self._coins(), compensated)

    def test_a_grant_the_panel_already_cancelled_pays_nothing_and_changes_nothing(self):
        stats.grant_xp_once("chat", "1", 10_000_000, "paid-as-xp")
        stats.grant_xp_once("chat", "1", 300, "earned-prize")
        stats.adjust_bonus_xp("chat", "1", -10_000_000, by="panel")
        xp_before, coins_before = self._xp(), self._coins()

        self.assertEqual(economy.retire_xp_grant("chat", "1", "paid-as-xp"), 0)

        self.assertEqual(self._xp(), xp_before)
        self.assertEqual(self._coins(), coins_before)
        # Both halves of the cancelled pair are gone; the unrelated grant stays.
        self.assertEqual(list(stats.xp_grants_for("chat", "1")), ["earned-prize"])

    def test_a_partly_cancelled_grant_pays_only_what_was_still_in_effect(self):
        stats.grant_xp_once("chat", "1", 1_000_000, "paid-as-xp")
        stats.adjust_bonus_xp("chat", "1", -400_000, by="panel")
        coins_before = self._coins()

        paid = economy.retire_xp_grant("chat", "1", "paid-as-xp")

        self.assertEqual(paid, 600_000 // stats.XP_PER_COIN)
        self.assertEqual(self._xp(), 0)
        self.assertEqual(self._coins(), coins_before)

    def test_nothing_to_retire_is_not_an_error(self):
        self.assertEqual(economy.retire_xp_grant("chat", "1", "no-such-grant"), 0)


class StartupGrantTests(unittest.TestCase):
    def test_the_bot_no_longer_pays_anybody_in_xp_when_it_starts(self):
        """The 10M grant used to be re-applied on every start, so even admin_xp.py could
        not make its removal stick. The start-up now converts it to coins instead."""
        source = (Path(__file__).resolve().parents[1] / "bot_listener.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("grant_xp_once(", source)
        self.assertIn("economy.retire_xp_grant(", source)


if __name__ == "__main__":
    unittest.main()
