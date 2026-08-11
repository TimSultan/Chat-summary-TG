"""Casino outcomes stay server-side and use only the shared coin ledger."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import casino
import economy
import pets_ui


class _FixedRng:
    def __init__(self, *, cards=None, number=1):
        self.cards = cards or [14, 14, 2, 13, 12, 3]
        self.number = number

    def sample(self, population, size):
        return list(self.cards)

    def randint(self, low, high):
        return self.number


class CasinoTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self._patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.addCleanup(self._temporary.cleanup)
        economy.grant("chat", "1", 10, "test")

    def test_poker_win_returns_twice_the_stake(self):
        result = casino.play_poker("chat", "1", 0, 5, rng=_FixedRng())

        self.assertTrue(result["ok"])
        self.assertTrue(result["won"])
        self.assertEqual(result["payout"], 10)
        self.assertEqual(economy.balance("chat", "1", 0), 15)

    def test_shells_and_higher_lower_settle_the_chosen_outcome(self):
        lost = casino.play_shell("chat", "1", 0, 5, 1, rng=_FixedRng(number=2))
        self.assertTrue(lost["ok"])
        self.assertFalse(lost["won"])
        self.assertEqual(economy.balance("chat", "1", 0), 5)

        won = casino.play_highlow("chat", "1", 0, 5, "high", rng=_FixedRng(number=9))
        self.assertTrue(won["won"])
        self.assertEqual(won["card"], 9)
        self.assertEqual(economy.balance("chat", "1", 0), 10)

    def test_invalid_or_unaffordable_bets_do_not_change_the_balance(self):
        self.assertFalse(casino.play_shell("chat", "1", 0, 3, 1)["ok"])
        self.assertEqual(economy.balance("chat", "1", 0), 10)

        self.assertFalse(casino.play_highlow("chat", "1", 0, 25, "low")["ok"])
        self.assertEqual(economy.balance("chat", "1", 0), 10)

    def test_lobby_shows_the_three_games_and_coin_stakes(self):
        text, keyboard = pets_ui.casino_view("chat", "1", 0)
        self.assertIn("покер", text.lower())
        self.assertIn("наперстки", text.lower().replace("ё", "е"))
        self.assertIn("больше / меньше", text.lower())
        actions = [
            pets_ui.parse_callback(button["callback_data"])[1]
            for row in keyboard["inline_keyboard"] for button in row
        ]
        self.assertIn("cgame", actions)


if __name__ == "__main__":
    unittest.main()
