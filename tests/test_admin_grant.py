"""admin_grant.py: dry run is inert, each currency lands through its real library call,
and a replayed --reason never double-pays. See the module docstring in admin_grant.py for
why every grant has to round-trip through pets.py/economy.py rather than the JSON file --
these tests exercise the REAL hashed store (tests/test_pets.py's fixture pattern) rather
than mocking pets.py, specifically so a regression in _resolved_paths (which is what makes
admin_grant.py's already-hashed "entry" resolve to the right file at all) would show up as
a balance that never moves instead of passing silently against a mock.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import admin_grant
import economy
import pets
import pets_config


class AdminGrantTestCase(unittest.TestCase):
    """Same fixture as tests/test_pets.py's PetsTestCase: point stats._stats_dir at a
    throwaway directory so both pets.py and economy.py write there instead of the repo's
    real cache."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def _tame(self, entry, uid, name, owner=None):
        """Fund and walk one member all the way to a named pet, the real way (through
        pets.py), so the resulting file is hashed exactly like a real chat's."""
        economy.grant(entry, uid, pets_config.TAME_PRICE, "test")
        ok, msg = pets.buy_cage(entry, uid, 0)
        self.assertTrue(ok, msg)
        ok, msg = pets.tame(entry, uid, 0, name, f"file{uid}", owner or name)
        self.assertTrue(ok, msg)


class DryRunTests(AdminGrantTestCase):
    def test_dry_run_reports_but_grants_nothing(self):
        self._tame("chat", "1", "Кломбик")
        code = admin_grant.main([
            "Кломбик", "--gold", "500", "--rubies", "1000",
            "--farm-tickets", "2", "--dungeon-tickets", "1",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(pets.ruby_balance("chat", "1"), 0)
        self.assertEqual(economy.balance("chat", "1", 0), 0)
        self.assertEqual(pets.farm_tickets("chat", "1"), 0)
        self.assertEqual(pets.dungeon_tickets("chat", "1"), 0)

    def test_dry_run_is_the_default_even_with_a_reason(self):
        self._tame("chat", "1", "Кломбик")
        code = admin_grant.main(["Кломбик", "--rubies", "10000", "--reason", "comp"])
        self.assertEqual(code, 0)
        self.assertEqual(pets.ruby_balance("chat", "1"), 0)


class GrantLandingTests(AdminGrantTestCase):
    def test_all_four_currencies_land_through_the_real_wallets(self):
        self._tame("chat", "1", "Кломбик")
        code = admin_grant.main([
            "Кломбик", "--gold", "500", "--rubies", "1000",
            "--farm-tickets", "3", "--dungeon-tickets", "2", "--yes",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(pets.ruby_balance("chat", "1"), 1000)
        self.assertEqual(economy.balance("chat", "1", 0), 500)
        self.assertEqual(pets.farm_tickets("chat", "1"), 3)
        self.assertEqual(pets.dungeon_tickets("chat", "1"), 2)

    def test_a_single_currency_can_be_granted_alone(self):
        self._tame("chat", "1", "Кломбик")
        code = admin_grant.main(["Кломбик", "--rubies", "10000", "--yes"])
        self.assertEqual(code, 0)
        self.assertEqual(pets.ruby_balance("chat", "1"), 10000)
        self.assertEqual(economy.balance("chat", "1", 0), 0)
        self.assertEqual(pets.farm_tickets("chat", "1"), 0)
        self.assertEqual(pets.dungeon_tickets("chat", "1"), 0)

    def test_rubies_are_tracked_in_the_real_ruby_sources_ledger(self):
        # Confirms the grant went through pets.grant_rubies_once (ledger + metric kept
        # consistent) rather than a hand-rolled wallet edit.
        self._tame("chat", "1", "Кломбик")
        admin_grant.main(["Кломбик", "--rubies", "777", "--reason", "audit", "--yes"])
        data = pets._load("chat")
        self.assertIn("audit:rubies", data.get("ruby_sources", {}))
        self.assertEqual(data["ruby_sources"]["audit:rubies"]["amount"], 777)


class ReplayTests(AdminGrantTestCase):
    def test_replaying_the_same_reason_does_not_double_pay(self):
        self._tame("chat", "1", "Кломбик")
        argv = [
            "Кломбик", "--gold", "500", "--rubies", "1000",
            "--farm-tickets", "3", "--dungeon-tickets", "2",
            "--reason", "comp-2026-08", "--yes",
        ]
        self.assertEqual(admin_grant.main(argv), 0)
        self.assertEqual(admin_grant.main(argv), 0)
        self.assertEqual(pets.ruby_balance("chat", "1"), 1000)
        self.assertEqual(economy.balance("chat", "1", 0), 500)
        self.assertEqual(pets.farm_tickets("chat", "1"), 3)
        self.assertEqual(pets.dungeon_tickets("chat", "1"), 2)

    def test_replaying_dungeon_tickets_alone_does_not_double_pay(self):
        # Dungeon tickets are the one currency with no idempotency built into pets.py
        # itself (see admin_grant._grant_dungeon_tickets) -- worth its own test.
        self._tame("chat", "1", "Кломбик")
        argv = ["Кломбик", "--dungeon-tickets", "5", "--reason", "launch-gift", "--yes"]
        admin_grant.main(argv)
        admin_grant.main(argv)
        admin_grant.main(argv)
        self.assertEqual(pets.dungeon_tickets("chat", "1"), 5)


class LookupTests(AdminGrantTestCase):
    def test_ambiguous_name_exits_nonzero_and_grants_nothing(self):
        self._tame("chat1", "1", "Кломбик")
        self._tame("chat2", "2", "Кломбик")
        code = admin_grant.main(["Кломбик", "--rubies", "100", "--yes"])
        self.assertNotEqual(code, 0)
        self.assertEqual(pets.ruby_balance("chat1", "1"), 0)
        self.assertEqual(pets.ruby_balance("chat2", "2"), 0)

    def test_unknown_name_exits_nonzero(self):
        self._tame("chat", "1", "Кломбик")
        code = admin_grant.main(["Призрак", "--rubies", "100", "--yes"])
        self.assertNotEqual(code, 0)

    def test_case_insensitive_owner_match_still_finds_one(self):
        self._tame("chat", "1", "Кломбик", owner="Султан")
        code = admin_grant.main(["султан", "--rubies", "50", "--yes"])
        self.assertEqual(code, 0)
        self.assertEqual(pets.ruby_balance("chat", "1"), 50)


class ArgumentValidationTests(AdminGrantTestCase):
    def test_no_amount_flags_at_all_exits_nonzero(self):
        self._tame("chat", "1", "Кломбик")
        code = admin_grant.main(["Кломбик"])
        self.assertNotEqual(code, 0)
        self.assertEqual(pets.ruby_balance("chat", "1"), 0)

    def test_a_non_positive_amount_exits_nonzero(self):
        self._tame("chat", "1", "Кломбик")
        code = admin_grant.main(["Кломбик", "--rubies", "0", "--yes"])
        self.assertNotEqual(code, 0)
        code = admin_grant.main(["Кломбик", "--gold", "-5", "--yes"])
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
