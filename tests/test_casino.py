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
        self.cards = cards or [
            (14, "♠"), (14, "♥"), (13, "♠"), (12, "♥"),
            (2, "♣"), (5, "♦"), (7, "♠"), (9, "♣"), (11, "♦"),
        ]
        self.number = number

    def sample(self, population, size):
        return list(self.cards)

    def randint(self, low, high):
        return self.number

    def choice(self, items):
        return items[0]


class CasinoTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self._patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.addCleanup(self._temporary.cleanup)
        economy.grant("chat", "1", 300, "test")

    def test_poker_reveals_three_four_five_cards_then_pays_twice_the_stake(self):
        started = casino.start_poker("chat", "1", 0, 25, rng=_FixedRng())
        self.assertTrue(started["ok"])
        self.assertEqual(casino.poker_snapshot(started["active"])["stage"], 3)
        self.assertEqual(economy.balance("chat", "1", 0), 275)

        turn = casino.advance_poker("chat", "1", 0, raise_by=25)
        self.assertEqual(casino.poker_snapshot(turn["active"])["stage"], 4)
        self.assertEqual(turn["active"]["stake"], 50)
        self.assertEqual(economy.balance("chat", "1", 0), 250)
        river = casino.advance_poker("chat", "1", 0)
        self.assertEqual(casino.poker_snapshot(river["active"])["stage"], 5)
        self.assertEqual(river["active"]["stake"], 50)
        result = casino.advance_poker("chat", "1", 0, raise_by=25)
        self.assertTrue(result["won"])
        self.assertEqual(result["payout"], 150)
        self.assertEqual(economy.balance("chat", "1", 0), 375)

    def test_poker_view_puts_the_pot_first_and_offers_call_or_raise(self):
        started = casino.start_poker("chat", "1", 0, 25, rng=_FixedRng())
        text, keyboard = pets_ui.casino_poker_view("chat", "1", 0, started["active"])
        self.assertLess(text.index("Общая ставка"), text.index("Стол"))
        self.assertLess(text.index("Стол"), text.index("Твои карты"))
        self.assertNotIn("Соперник коллирует", text)
        callbacks = [
            pets_ui.parse_callback(button["callback_data"])
            for row in keyboard["inline_keyboard"] for button in row
        ]
        self.assertIn(("1", "cpoker", ""), callbacks)
        self.assertEqual(
            {argument for _, action, argument in callbacks if action == "cpoker" and argument},
            {"raise:25"},
        )

    def test_poker_raise_must_equal_the_opening_stake(self):
        started = casino.start_poker("chat", "1", 0, 25, rng=_FixedRng())
        before = economy.balance("chat", "1", 0)
        rejected = casino.advance_poker("chat", "1", 0, raise_by=50)
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["error"], "invalid")
        self.assertEqual(rejected["active"], started["active"])
        self.assertEqual(economy.balance("chat", "1", 0), before)

    def test_available_stakes_are_ten_twenty_five_fifty_and_one_hundred(self):
        self.assertEqual(casino.POKER_BET_AMOUNTS, (10, 25, 50, 100))
        self.assertEqual(casino.BET_AMOUNTS, (1, 5, 10, 25))
        _text, keyboard = pets_ui.casino_bet_view("chat", "1", 0, "poker")
        labels = [
            button["text"] for row in keyboard["inline_keyboard"] for button in row
            if button["text"].startswith("🪙")
        ]
        self.assertEqual(labels, ["🪙 10", "🪙 25", "🪙 50", "🪙 100"])

    def test_shells_and_higher_lower_settle_the_chosen_outcome(self):
        lost = casino.play_shell("chat", "1", 0, 10, 1, rng=_FixedRng(number=2))
        self.assertTrue(lost["ok"])
        self.assertFalse(lost["won"])
        self.assertEqual(economy.balance("chat", "1", 0), 290)

        won = casino.play_highlow("chat", "1", 0, 10, "high", rng=_FixedRng(number=9))
        self.assertTrue(won["won"])
        self.assertEqual(won["card"], 9)
        self.assertEqual(economy.balance("chat", "1", 0), 300)

        shell_win = casino.play_shell("chat", "1", 0, 10, 1, rng=_FixedRng(number=1))
        self.assertEqual(shell_win["payout"], 30)
        self.assertEqual(economy.balance("chat", "1", 0), 320)
        # Only net profit counts: +10 from higher/lower and +20 from the shell.
        self.assertEqual(economy.casino_winnings_for_user("chat", "1"), 30)
        text, _ = pets_ui.casino_result_view("chat", "1", 0, shell_win)
        self.assertIn("🟢", text)
        self.assertNotIn("шарик был под", text)

    def test_retired_goat_game_is_refunded_and_cleared(self):
        data = economy._load("chat")
        record = economy._record(data, "1")
        record["spent"] = record.get("spent", 0) + 25
        economy._effects(record).setdefault("casino", {})["active"] = {
            "kind": "goat", "stake": 25, "choice": 1, "prize": 2, "opened": 3,
        }
        economy._save("chat", data)
        self.assertEqual(economy.balance("chat", "1", 0), 275)

        self.assertIsNone(casino.active_game("chat", "1"))
        self.assertEqual(economy.balance("chat", "1", 0), 300)
        self.assertIsNone(casino.active_game("chat", "1"))
        self.assertEqual(economy.balance("chat", "1", 0), 300)

    def test_invalid_or_unaffordable_bets_do_not_change_the_balance(self):
        self.assertFalse(casino.play_shell("chat", "1", 0, 3, 1)["ok"])
        self.assertEqual(economy.balance("chat", "1", 0), 300)

        self.assertFalse(casino.play_highlow("chat", "1", 0, 500, "low")["ok"])
        self.assertEqual(economy.balance("chat", "1", 0), 300)

    def test_lobby_shows_the_three_games_and_coin_stakes(self):
        text, keyboard = pets_ui.casino_view("chat", "1", 0)
        self.assertIn("покер", text.lower())
        self.assertIn("наперстки", text.lower().replace("ё", "е"))
        self.assertIn("больше / меньше", text.lower())
        self.assertNotIn("коза", text.lower())
        self.assertNotIn("🐐", " ".join(button["text"] for row in keyboard["inline_keyboard"] for button in row))
        actions = [
            pets_ui.parse_callback(button["callback_data"])[1]
            for row in keyboard["inline_keyboard"] for button in row
        ]
        self.assertIn("cgame", actions)


if __name__ == "__main__":
    unittest.main()
