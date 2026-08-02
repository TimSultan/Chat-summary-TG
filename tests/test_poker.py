import asyncio
import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_listener
import poker
import stats

DEALER = {"id": 1, "username": "dealer_man", "first_name": "Диллер"}
# A chat administrator who is NOT the dealer: the only other person who may close a table.
ADMIN = {"id": 2, "username": "chat_admin", "first_name": "Админ"}
GROUP_CHAT = -1001234567890


def _table(count=3, stacks=None):
    """A table with `count` seated players and the hand not yet dealt."""
    table = poker.open_table(GROUP_CHAT, DEALER["id"], "Диллер")
    for index in range(count):
        poker.seat(table, 100 + index, f"Игрок {index}", f"player{index}")
    if stacks:
        for player, stack in zip(table["players"], stacks):
            player["stack"] = stack
    return table


def _act(table, seat_offset, action):
    """Act as whoever is at `seat_offset` in the players list."""
    return poker.act(table, table["players"][seat_offset]["user_id"], action)


def _act_current(table, action):
    return poker.act(table, poker.current_player(table)["user_id"], action)


class HandEvaluatorTests(unittest.TestCase):
    def test_the_categories_rank_in_the_right_order(self):
        hands = [
            ["2♠", "7♦", "9♣", "J♥", "K♠"],          # старшая карта
            ["2♠", "2♦", "9♣", "J♥", "K♠"],          # пара
            ["2♠", "2♦", "9♣", "9♥", "K♠"],          # две пары
            ["2♠", "2♦", "2♣", "9♥", "K♠"],          # тройка
            ["5♠", "6♦", "7♣", "8♥", "9♠"],          # стрит
            ["2♠", "5♠", "9♠", "J♠", "K♠"],          # флеш
            ["2♠", "2♦", "2♣", "9♥", "9♠"],          # фулл-хаус
            ["2♠", "2♦", "2♣", "2♥", "9♠"],          # каре
            ["5♠", "6♠", "7♠", "8♠", "9♠"],          # стрит-флеш
        ]
        scores = [poker.evaluate_five(hand) for hand in hands]
        self.assertEqual(scores, sorted(scores))
        self.assertEqual([poker.hand_name(score) for score in scores], list(poker.HAND_NAMES))

    def test_the_wheel_is_a_five_high_straight(self):
        wheel = poker.evaluate_five(["A♠", "2♦", "3♣", "4♥", "5♠"])
        six_high = poker.evaluate_five(["2♠", "3♦", "4♣", "5♥", "6♠"])

        self.assertEqual(poker.hand_name(wheel), "Стрит")
        self.assertLess(wheel, six_high, "A-2-3-4-5 must be the weakest straight")

    def test_an_ace_high_straight_beats_a_king_high_one(self):
        broadway = poker.evaluate_five(["T♠", "J♦", "Q♣", "K♥", "A♠"])
        lower = poker.evaluate_five(["9♠", "T♦", "J♣", "Q♥", "K♠"])
        self.assertGreater(broadway, lower)

    def test_kickers_break_a_tie_between_equal_pairs(self):
        stronger = poker.evaluate_five(["K♠", "K♦", "Q♣", "7♥", "3♠"])
        weaker = poker.evaluate_five(["K♣", "K♥", "J♣", "7♦", "3♦"])
        self.assertGreater(stronger, weaker)

    def test_best_hand_picks_five_out_of_seven(self):
        score, five = poker.best_hand(["A♠", "K♠", "Q♠", "J♠", "T♠", "2♦", "3♣"])
        self.assertEqual(poker.hand_name(score), "Стрит-флеш")
        self.assertEqual(len(five), 5)
        self.assertNotIn("2♦", five)

    def test_identical_hands_score_identically(self):
        # Split pots depend on this: two players playing the same board must tie exactly.
        self.assertEqual(
            poker.best_hand(["2♦", "3♣", "T♠", "J♠", "Q♠", "K♠", "A♠"])[0],
            poker.best_hand(["4♦", "5♣", "T♠", "J♠", "Q♠", "K♠", "A♠"])[0],
        )

    def test_a_full_deck_is_dealt_without_duplicates(self):
        deck = poker.new_deck(random.Random(7))
        self.assertEqual(len(deck), 52)
        self.assertEqual(len(set(deck)), 52)


class TableTests(unittest.TestCase):
    def test_players_take_one_seat_each(self):
        table = poker.open_table(GROUP_CHAT, DEALER["id"], "Диллер")

        self.assertEqual(poker.seat(table, 100, "Аня", "anya"), "seated")
        self.assertEqual(poker.seat(table, 100, "Аня", "anya"), "already")
        self.assertEqual(len(table["players"]), 1)

    def test_the_table_fills_up_at_ten(self):
        table = poker.open_table(GROUP_CHAT, DEALER["id"], "Диллер")
        for index in range(poker.MAX_PLAYERS):
            self.assertEqual(poker.seat(table, index, f"И{index}", None), "seated")

        self.assertEqual(poker.seat(table, 999, "Лишний", None), "full")
        self.assertEqual(len(table["players"]), poker.MAX_PLAYERS)

    def test_nobody_joins_a_table_that_is_already_playing(self):
        table = _table()
        poker.start_hand(table, random.Random(1))
        self.assertEqual(poker.seat(table, 777, "Опоздал", None), "closed")

    def test_a_hand_needs_two_players(self):
        table = _table(count=1)
        with self.assertRaises(ValueError):
            poker.start_hand(table, random.Random(1))

    def test_only_the_member_who_opened_the_table_runs_it(self):
        table = _table()
        self.assertTrue(poker.is_table_dealer(table, DEALER["id"]))
        self.assertFalse(poker.is_table_dealer(table, 100))


class DealTests(unittest.TestCase):
    def setUp(self):
        self.table = _table()
        poker.start_hand(self.table, random.Random(42))

    def test_everybody_gets_two_cards_nobody_else_has(self):
        hole = self.table["hand"]["hole"]
        self.assertEqual(len(hole), 3)
        dealt = [card for cards in hole.values() for card in cards]
        self.assertEqual(len(dealt), 6)
        self.assertEqual(len(set(dealt)), 6)

    def test_the_blinds_are_posted_and_come_out_of_the_stacks(self):
        players = self.table["players"]
        small = players[(self.table["button"] + 1) % 3]
        big = players[(self.table["button"] + 2) % 3]

        self.assertEqual(small["stack"], poker.START_STACK - poker.SMALL_BLIND)
        self.assertEqual(big["stack"], poker.START_STACK - poker.BIG_BLIND)
        self.assertEqual(poker.pot(self.table), poker.SMALL_BLIND + poker.BIG_BLIND)

    def test_the_action_starts_left_of_the_big_blind(self):
        expected = self.table["players"][(self.table["button"] + 3) % 3]
        self.assertEqual(poker.current_player(self.table)["user_id"], expected["user_id"])

    def test_the_big_blind_still_gets_to_act_after_everybody_calls(self):
        """The option. Without it the blind would be checked past on every hand."""
        big = self.table["players"][(self.table["button"] + 2) % 3]
        _act_current(self.table, "call")
        _act_current(self.table, "call")

        self.assertEqual(poker.current_player(self.table)["user_id"], big["user_id"])
        self.assertEqual(self.table["hand"]["street"], "preflop")

    def test_the_flop_arrives_once_the_round_closes(self):
        _act_current(self.table, "call")
        _act_current(self.table, "call")
        _act_current(self.table, "check")

        self.assertEqual(self.table["hand"]["street"], "flop")
        self.assertEqual(len(self.table["hand"]["board"]), 3)

    def test_the_whole_board_is_dealt_street_by_street(self):
        for expected in (3, 4, 5):
            while self.table["hand"]["street"] != "river" and len(self.table["hand"]["board"]) < expected:
                _act_current(self.table, poker.legal_actions(self.table)[0])
            self.assertGreaterEqual(len(self.table["hand"]["board"]), min(expected, 5))


class BettingTests(unittest.TestCase):
    def test_a_raise_reopens_the_betting_for_everybody(self):
        table = _table()
        poker.start_hand(table, random.Random(3))
        first = poker.current_player(table)

        _act_current(table, "bet")
        # The raiser has acted, so the action must come back around rather than closing.
        self.assertNotEqual(poker.current_player(table)["user_id"], first["user_id"])
        _act_current(table, "call")
        self.assertEqual(table["hand"]["street"], "preflop")
        _act_current(table, "call")
        self.assertEqual(table["hand"]["street"], "flop")

    def test_folding_leaves_the_hand_and_stops_the_asking(self):
        table = _table()
        poker.start_hand(table, random.Random(4))
        folder = poker.current_player(table)

        _act_current(table, "fold")
        self.assertIn(folder["user_id"], table["hand"]["folded"])
        for _ in range(6):
            if poker.hand_is_over(table):
                break
            self.assertNotEqual(poker.current_player(table)["user_id"], folder["user_id"])
            _act_current(table, poker.legal_actions(table)[0])

    def test_everybody_folding_hands_the_pot_over_without_a_showdown(self):
        table = _table()
        poker.start_hand(table, random.Random(5))
        big_blind = table["players"][(table["button"] + 2) % 3]

        _act_current(table, "fold")
        _act_current(table, "fold")

        self.assertTrue(poker.hand_is_over(table))
        self.assertFalse(table["hand"]["result"]["showdown"])
        self.assertEqual(
            big_blind["stack"],
            poker.START_STACK + poker.SMALL_BLIND,
            "the last player standing takes the blinds",
        )

    def test_a_short_stack_can_only_go_all_in(self):
        table = _table(count=2, stacks=[poker.START_STACK, 5])
        poker.start_hand(table, random.Random(6))
        short = next(p for p in table["players"] if p["user_id"] == "101")

        self.assertEqual(short["stack"], 0, "a blind bigger than the stack is an all-in")
        self.assertIn(short["user_id"], table["hand"]["all_in"])

    def test_an_all_in_player_is_never_asked_to_act_again(self):
        table = _table(count=3, stacks=[poker.START_STACK, poker.START_STACK, 40])
        poker.start_hand(table, random.Random(8))
        for _ in range(12):
            if poker.hand_is_over(table):
                break
            player = poker.current_player(table)
            self.assertNotIn(player["user_id"], table["hand"]["all_in"])
            _act_current(table, poker.legal_actions(table)[0])

    def test_the_board_is_run_out_when_everybody_is_all_in(self):
        table = _table(count=2)
        poker.start_hand(table, random.Random(9))
        _act_current(table, "allin")
        _act_current(table, "allin")

        self.assertTrue(poker.hand_is_over(table))
        self.assertEqual(len(table["hand"]["board"]), 5, "the cards still decide the hand")

    def test_chips_are_conserved_across_a_whole_hand(self):
        """The property that matters most: a bug in the pot maths is a bug that invents
        or destroys somebody's chips."""
        for seed in range(25):
            table = _table(count=4, stacks=[1_000, 700, 250, 90])
            before = sum(p["stack"] for p in table["players"])
            poker.start_hand(table, random.Random(seed))
            steps = 0
            while not poker.hand_is_over(table) and steps < 80:
                actions = poker.legal_actions(table)
                _act_current(table, actions[steps % len(actions)])
                steps += 1
            after = sum(p["stack"] for p in table["players"])
            self.assertEqual(before, after, f"chips leaked on seed {seed}")


class SidePotTests(unittest.TestCase):
    def test_an_all_in_wins_only_what_it_covered(self):
        table = _table(count=3, stacks=[1_000, 1_000, 100])
        poker.start_hand(table, random.Random(11))
        hand = table["hand"]
        # Hand-set the contributions: the short stack is all-in for 100, the other two
        # keep betting to 400 each.
        short, big_one, big_two = (p["user_id"] for p in table["players"])
        hand["committed"] = {short: 100, big_one: 400, big_two: 400}
        hand["all_in"] = [short]
        hand["folded"] = []

        pots = poker._side_pots(table)

        self.assertEqual(pots[0], (300, [short, big_one, big_two]))
        self.assertEqual(pots[1], (600, [big_one, big_two]))
        self.assertEqual(sum(chips for chips, _ in pots), 900)

    def test_a_folded_players_chips_stay_in_the_pot(self):
        table = _table(count=3)
        poker.start_hand(table, random.Random(12))
        hand = table["hand"]
        first, second, third = (p["user_id"] for p in table["players"])
        hand["committed"] = {first: 50, second: 200, third: 200}
        hand["folded"] = [first]

        pots = poker._side_pots(table)

        self.assertEqual(sum(chips for chips, _ in pots), 450)
        for _, eligible in pots:
            self.assertNotIn(first, eligible, "a folded player can win nothing")

    def test_a_split_pot_is_shared_and_the_odd_chip_goes_left_of_the_button(self):
        table = _table(count=3)
        poker.start_hand(table, random.Random(13))
        hand = table["hand"]
        first, second, third = (p["user_id"] for p in table["players"])
        # Two players tie on the board itself over an odd pot; the third folded a chip in.
        hand["board"] = ["A♠", "K♦", "Q♣", "J♥", "9♠"]
        hand["hole"] = {first: ["2♦", "3♣"], second: ["2♠", "3♦"], third: ["7♦", "8♣"]}
        hand["committed"] = {first: 100, second: 100, third: 1}
        hand["folded"] = [third]
        stacks = {p["user_id"]: p["stack"] for p in table["players"]}

        poker._finish(table)

        winnings = table["hand"]["result"]["winnings"]
        self.assertEqual(sum(winnings.values()), 201)
        self.assertEqual(abs(winnings[first] - winnings[second]), 1)
        left_of_button = table["players"][(table["button"] + 1) % 3]["user_id"]
        self.assertEqual(max(winnings, key=winnings.get), left_of_button)
        self.assertTrue(all(p["stack"] >= stacks[p["user_id"]] for p in table["players"]))

    def test_an_uncalled_bet_comes_back_to_the_player_who_made_it(self):
        """Not an odd-chip split: nobody covered the extra, so it was never contested."""
        table = _table(count=2)
        poker.start_hand(table, random.Random(13))
        hand = table["hand"]
        first, second = (p["user_id"] for p in table["players"])
        hand["board"] = ["A♠", "K♦", "Q♣", "J♥", "9♠"]
        hand["hole"] = {first: ["2♦", "3♣"], second: ["2♠", "3♦"]}
        hand["committed"] = {first: 140, second: 100}
        hand["folded"] = []

        poker._finish(table)

        winnings = table["hand"]["result"]["winnings"]
        self.assertEqual(sum(winnings.values()), 240)
        self.assertEqual(winnings[first] - winnings[second], 40)


class TurnTests(unittest.TestCase):
    def test_pressing_out_of_turn_changes_nothing_and_says_why(self):
        table = _table()
        poker.start_hand(table, random.Random(14))
        waiting = next(
            p for p in table["players"]
            if p["user_id"] != poker.current_player(table)["user_id"]
        )
        before = poker.pot(table)

        ok, message = poker.act(table, waiting["user_id"], "fold")

        self.assertFalse(ok)
        self.assertIn("Сейчас ход", message)
        self.assertEqual(poker.pot(table), before)
        self.assertEqual(table["hand"]["folded"], [])

    def test_a_stranger_cannot_act_at_all(self):
        table = _table()
        poker.start_hand(table, random.Random(15))
        ok, message = poker.act(table, 55_555, "check")
        self.assertFalse(ok)
        self.assertTrue(message)

    def test_an_illegal_action_is_refused_rather_than_applied(self):
        table = _table()
        poker.start_hand(table, random.Random(16))
        # Preflop there is a big blind to call, so a check is not on offer.
        self.assertNotIn("check", poker.legal_actions(table))
        ok, _ = _act_current(table, "check")
        self.assertFalse(ok)

    def test_nobody_can_act_after_the_hand_is_over(self):
        table = _table(count=2)
        poker.start_hand(table, random.Random(17))
        _act_current(table, "fold")

        self.assertTrue(poker.hand_is_over(table))
        ok, _ = poker.act(table, table["players"][0]["user_id"], "check")
        self.assertFalse(ok)


class SessionTests(unittest.TestCase):
    def test_the_button_moves_every_hand(self):
        table = _table()
        poker.start_hand(table, random.Random(18))
        first = table["button"]
        while not poker.hand_is_over(table):
            _act_current(table, poker.legal_actions(table)[-1])  # everybody folds out
        poker.start_hand(table, random.Random(19))

        self.assertEqual(table["button"], (first + 1) % 3)
        self.assertEqual(table["hand_no"], 2)

    def test_a_busted_player_leaves_the_table_before_the_next_hand(self):
        table = _table(count=3, stacks=[1_000, 1_000, 0])
        poker.start_hand(table, random.Random(20))

        self.assertEqual(len(table["players"]), 2)
        self.assertEqual(poker.players_with_chips(table), 2)

    def test_a_session_that_runs_down_to_one_player_cannot_deal_again(self):
        table = _table(count=3, stacks=[1_000, 0, 0])
        self.assertEqual(poker.players_with_chips(table), 1)
        with self.assertRaises(ValueError):
            poker.start_hand(table, random.Random(21))


class RenderTests(unittest.TestCase):
    def _check_html(self, text):
        """Telegram rejects the whole message on a stray tag, so every rendered string
        has to survive a strict parse."""
        from html.parser import HTMLParser

        stack = []

        class Strict(HTMLParser):
            def handle_starttag(self, tag, attrs):
                stack.append(tag)

            def handle_endtag(self, tag):
                if not stack or stack.pop() != tag:
                    raise AssertionError(f"unbalanced </{tag}> in: {text}")

        parser = Strict()
        parser.feed(text)
        parser.close()
        self.assertEqual(stack, [], f"unclosed tag in: {text}")

    def test_a_display_name_cannot_inject_html(self):
        table = poker.open_table(GROUP_CHAT, DEALER["id"], "Диллер")
        poker.seat(table, 100, "<b>Аня</b> & Ко", None)
        poker.seat(table, 101, "Боря", None)
        text = poker.format_lobby(table)

        self.assertIn("&lt;b&gt;Аня&lt;/b&gt; &amp; Ко", text)
        self._check_html(text)

    def test_every_screen_of_a_whole_hand_renders(self):
        table = _table(count=4)
        self._check_html(poker.format_lobby(table))
        poker.start_hand(table, random.Random(22))
        while not poker.hand_is_over(table):
            self._check_html(poker.format_hand(table))
            for player in table["players"]:
                self._check_html(poker.format_hole_cards(table, player["user_id"]))
            _act_current(table, poker.legal_actions(table)[0])
        self._check_html(poker.format_showdown(table))
        self._check_html(poker.format_session_over(table, "Диллер закрыл стол."))

    def test_the_hand_message_says_whose_turn_it_is(self):
        table = _table()
        poker.start_hand(table, random.Random(23))
        text = poker.format_hand(table)

        self.assertIn("Ход:", text)
        self.assertIn(poker.player_label(poker.current_player(table)), text)
        self.assertIn("Банк:", text)

    def test_hole_cards_never_appear_in_the_group_message(self):
        """The one leak that would end the game: the table message is public."""
        table = _table()
        poker.start_hand(table, random.Random(24))
        text = poker.format_hand(table)
        for cards in table["hand"]["hole"].values():
            for card in cards:
                self.assertNotIn(poker.format_card(card), text)


class KeyboardTests(unittest.TestCase):
    def test_callback_payloads_round_trip_and_fit(self):
        data = poker.callback_data("call", "abcd1234", 7, "river")
        self.assertLessEqual(len(data.encode()), 64)
        self.assertEqual(poker.parse_callback(data), ("call", "abcd1234", 7, "river"))

    def test_another_features_button_is_not_a_poker_button(self):
        self.assertIsNone(poker.parse_callback("plant:join"))
        self.assertIsNone(poker.parse_callback(""))

    def test_the_action_buttons_offer_exactly_what_is_legal(self):
        table = _table()
        poker.start_hand(table, random.Random(25))
        keyboard = poker.action_keyboard(table)
        actions = [
            poker.parse_callback(button["callback_data"])[0]
            for row in keyboard["inline_keyboard"] for button in row
        ]

        self.assertEqual([a for a in actions if a != "end"], poker.legal_actions(table))
        self.assertIn("end", actions, "the dealer always has a way to close the table")

    def test_the_amounts_are_on_the_buttons(self):
        table = _table()
        poker.start_hand(table, random.Random(26))
        labels = [
            button["text"]
            for row in poker.action_keyboard(table)["inline_keyboard"] for button in row
        ]
        self.assertTrue(any(str(poker.BIG_BLIND) in label for label in labels))

    def test_the_next_hand_button_disappears_when_the_session_is_over(self):
        table = _table(count=2, stacks=[2_000, 0])
        table["phase"] = poker.PHASE_SHOWDOWN
        actions = [
            poker.parse_callback(button["callback_data"])[0]
            for row in poker.showdown_keyboard(table)["inline_keyboard"] for button in row
        ]
        self.assertEqual(actions, ["end"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def test_a_table_survives_a_restart_mid_hand(self):
        table = _table()
        poker.start_hand(table, random.Random(27))
        _act_current(table, "call")
        poker.save_table("chat", table)

        restored = poker.load_table("chat")

        self.assertEqual(restored["table_id"], table["table_id"])
        self.assertEqual(restored["hand"]["hole"], table["hand"]["hole"])
        self.assertEqual(
            poker.current_player(restored)["user_id"],
            poker.current_player(table)["user_id"],
        )

    def test_tables_do_not_leak_between_chats(self):
        poker.save_table("one", _table())
        self.assertIsNone(poker.load_table("two"))

    def test_a_cleared_table_is_gone(self):
        poker.save_table("chat", _table())
        poker.clear_table("chat")
        self.assertIsNone(poker.load_table("chat"))
        poker.clear_table("chat")  # idempotent: closing a closed table must not raise

    def test_a_corrupt_file_reads_as_no_table(self):
        poker._path("chat").write_text("{ not json", encoding="utf-8")
        self.assertIsNone(poker.load_table("chat"))


class FakeAPI:
    """Records everything the handlers send. `unreachable` are user ids that have never
    started the bot -- writing to them raises, exactly as the real API does."""

    def __init__(self, unreachable=()):
        self.sent = []
        self.edits = []
        self.markup_edits = []
        self.answers = []
        self.unreachable = {int(user_id) for user_id in unreachable}
        self._next_id = 1000

    async def send_message(self, chat_id, text, reply_to_message_id=None,
                           reply_markup=None, parse_mode=None):
        if int(chat_id) in self.unreachable:
            raise RuntimeError("Forbidden: bot can't initiate conversation with a user")
        self._next_id += 1
        self.sent.append({
            "chat_id": int(chat_id), "text": text,
            "reply_markup": reply_markup, "parse_mode": parse_mode,
            "message_id": self._next_id,
        })
        return {"message_id": self._next_id}

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None, parse_mode=None):
        self.edits.append({
            "chat_id": int(chat_id), "message_id": message_id,
            "text": text, "reply_markup": reply_markup,
        })

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.markup_edits.append({
            "chat_id": int(chat_id), "message_id": message_id, "reply_markup": reply_markup,
        })

    async def answer_callback_query(self, callback_query_id, text=None):
        self.answers.append(text)

    async def get_chat_administrators(self, chat_id):
        return [{"user": ADMIN}]

    def to_group(self):
        return [item for item in self.sent if item["chat_id"] == GROUP_CHAT]

    def to_user(self, user_id):
        return [item for item in self.sent if item["chat_id"] == int(user_id)]


def _user(user_id, name=None, username=None):
    return {"id": user_id, "first_name": name or f"Игрок {user_id}", "username": username}


def _command(actor, text="/poker", chat_id=GROUP_CHAT):
    return {
        "message_id": 5,
        "chat": {"id": chat_id, "type": "supergroup"},
        "from": actor,
        "text": text,
    }


def _press(data, actor):
    return {"id": "cb1", "data": data, "from": actor,
            "message": {"message_id": 42, "chat": {"id": GROUP_CHAT, "type": "supergroup"}}}


class HandlerTests(unittest.TestCase):
    """Drives the real command and callback handlers: every one of these paths either
    posts to the whole chat or moves somebody's chips."""

    ENTRY = "chat"

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)
        self.api = FakeAPI()
        badge = stats.create_custom_badge(self.ENTRY, "🎲", "Диллер", DEALER["id"], "Диллер")
        stats.give_custom_badge(
            self.ENTRY, badge.badge_id, DEALER["id"], "Диллер", DEALER["id"], "Диллер"
        )

    def _open(self, actor=None, text="/poker"):
        asyncio.run(bot_listener.handle_poker_command(
            self.api, _command(actor or DEALER, text), text, self.ENTRY, GROUP_CHAT,
            log=lambda *_: None,
        ))
        return poker.load_table(self.ENTRY)

    def _callback(self, action, actor, hand_no=0, street=""):
        table = poker.load_table(self.ENTRY)
        data = poker.callback_data(action, table["table_id"], hand_no, street)
        asyncio.run(bot_listener.handle_poker_callback(
            self.api, _press(data, actor), self.ENTRY, log=lambda *_: None,
        ))
        return poker.load_table(self.ENTRY)

    def _seat(self, *user_ids):
        for user_id in user_ids:
            self._callback("join", _user(user_id, username=f"p{user_id}"))
        return poker.load_table(self.ENTRY)

    def _start(self):
        self._callback("start", DEALER)
        return poker.load_table(self.ENTRY)

    def _act_current(self, action):
        table = poker.load_table(self.ENTRY)
        current = poker.current_player(table)
        return self._callback(
            action, _user(int(current["user_id"])), table["hand_no"], table["hand"]["street"],
        )

    # --- открытие стола ---

    def test_a_member_without_the_badge_cannot_open_a_table(self):
        table = self._open(_user(500))

        self.assertIsNone(table)
        self.assertIn("Диллер", self.api.sent[0]["text"])

    def test_the_dealer_opens_a_table_and_it_lands_in_the_chat(self):
        table = self._open()

        self.assertIsNotNone(table)
        self.assertEqual(table["phase"], poker.PHASE_LOBBY)
        posted = self.api.to_group()[0]
        self.assertIn("Кто играет?", posted["text"])
        self.assertEqual(table["lobby_message_id"], posted["message_id"])

    def test_a_second_table_is_refused_while_one_is_open(self):
        first = self._open()
        self._open()

        self.assertEqual(poker.load_table(self.ENTRY)["table_id"], first["table_id"])
        self.assertIn("уже открыт", self.api.sent[-1]["text"])

    # --- посадка за стол ---

    def test_pressing_the_join_button_seats_you_and_updates_the_message(self):
        self._open()
        table = self._callback("join", _user(100, username="anya"))

        self.assertEqual(len(table["players"]), 1)
        self.assertEqual(self.api.answers[-1], "Ты в игре.")
        self.assertIn("@anya", self.api.edits[-1]["text"])
        self.assertEqual(self.api.edits[-1]["message_id"], table["lobby_message_id"])

    def test_pressing_twice_seats_you_once(self):
        self._open()
        self._callback("join", _user(100))
        table = self._callback("join", _user(100))

        self.assertEqual(len(table["players"]), 1)
        self.assertIn("уже за столом", self.api.answers[-1])

    def test_somebody_who_never_started_the_bot_is_not_seated(self):
        """Cards are private. Being seated without a DM would mean playing blind."""
        self.api = FakeAPI(unreachable=[100])
        self._open()
        table = self._callback("join", _user(100))

        self.assertEqual(table["players"], [])
        self.assertIn("нажми Start", self.api.answers[-1])

    def test_the_table_fills_up_at_ten(self):
        self._open()
        table = self._seat(*range(100, 111))

        self.assertEqual(len(table["players"]), poker.MAX_PLAYERS)
        self.assertIn(str(poker.MAX_PLAYERS), self.api.answers[-1])

    def test_nobody_joins_once_the_game_has_started(self):
        self._open()
        self._seat(100, 101)
        self._start()
        table = self._callback("join", _user(102))

        self.assertEqual(len(table["players"]), 2)
        self.assertIn("уже началась", self.api.answers[-1])

    # --- старт игры ---

    def test_only_the_dealer_starts_the_game(self):
        self._open()
        self._seat(100, 101)
        table = self._callback("start", _user(100))

        self.assertEqual(table["phase"], poker.PHASE_LOBBY)
        self.assertIn("только диллер", self.api.answers[-1])

    def test_a_game_needs_two_players(self):
        self._open()
        self._seat(100)
        table = self._callback("start", DEALER)

        self.assertEqual(table["phase"], poker.PHASE_LOBBY)
        self.assertIn("хотя бы", self.api.answers[-1])

    def test_starting_deals_private_cards_and_opens_the_betting_in_the_chat(self):
        self._open()
        self._seat(100, 101, 102)
        table = self._start()

        for user_id in (100, 101, 102):
            dealt = self.api.to_user(user_id)[-1]["text"]
            self.assertIn("Твои карты", dealt)
            self.assertIn(poker.format_cards(table["hand"]["hole"][str(user_id)]), dealt)

        street = self.api.to_group()[-1]
        self.assertIn("Префлоп", street["text"])
        self.assertTrue(street["reply_markup"]["inline_keyboard"])
        self.assertEqual(table["hand"]["message_id"], street["message_id"])

    def test_the_lobby_buttons_are_taken_away_when_the_game_starts(self):
        self._open()
        lobby_id = poker.load_table(self.ENTRY)["lobby_message_id"]
        self._seat(100, 101)
        self._start()

        retired = [edit for edit in self.api.markup_edits if edit["message_id"] == lobby_id]
        self.assertEqual(retired[-1]["reply_markup"], {"inline_keyboard": []})

    def test_no_hole_card_is_ever_written_to_the_group(self):
        self._open()
        self._seat(100, 101, 102)
        table = self._start()
        group_text = "\n".join(item["text"] for item in self.api.to_group())

        for cards in table["hand"]["hole"].values():
            for card in cards:
                self.assertNotIn(poker.format_card(card), group_text)

    # --- ходы ---

    def test_pressing_out_of_turn_changes_nothing(self):
        self._open()
        self._seat(100, 101, 102)
        table = self._start()
        waiting = next(
            p for p in table["players"] if p["user_id"] != poker.current_player(table)["user_id"]
        )
        before = poker.pot(table)

        after = self._callback(
            "fold", _user(int(waiting["user_id"])), table["hand_no"], table["hand"]["street"],
        )

        self.assertIn("Сейчас ход", self.api.answers[-1])
        self.assertEqual(poker.pot(after), before)
        self.assertEqual(after["hand"]["folded"], [])

    def test_a_button_from_an_earlier_street_no_longer_works(self):
        self._open()
        self._seat(100, 101, 102)
        self._start()
        stale = poker.load_table(self.ENTRY)
        for _ in range(3):  # everybody calls/checks the preflop away
            self._act_current(poker.legal_actions(poker.load_table(self.ENTRY))[0])

        table = poker.load_table(self.ENTRY)
        self.assertEqual(table["hand"]["street"], "flop")
        self._callback("fold", _user(int(poker.current_player(table)["user_id"])),
                       stale["hand_no"], "preflop")

        self.assertIn("уже сыграна", self.api.answers[-1])
        self.assertEqual(poker.load_table(self.ENTRY)["hand"]["folded"], [])

    def test_acting_in_turn_updates_the_message_in_place(self):
        self._open()
        self._seat(100, 101, 102)
        table = self._start()
        street_message = table["hand"]["message_id"]

        after = self._act_current("call")

        self.assertGreater(poker.pot(after), poker.pot(table))
        self.assertEqual(self.api.edits[-1]["message_id"], street_message)
        self.assertIn("Ход:", self.api.edits[-1]["text"])

    def test_a_new_street_gets_its_own_message(self):
        self._open()
        self._seat(100, 101, 102)
        table = self._start()
        preflop_message = table["hand"]["message_id"]
        for _ in range(3):
            self._act_current(poker.legal_actions(poker.load_table(self.ENTRY))[0])

        table = poker.load_table(self.ENTRY)
        self.assertEqual(table["hand"]["street"], "flop")
        self.assertNotEqual(table["hand"]["message_id"], preflop_message)
        self.assertIn("Флоп", self.api.to_group()[-1]["text"])
        retired = [e for e in self.api.markup_edits if e["message_id"] == preflop_message]
        self.assertTrue(retired, "the finished street kept its buttons")

    def test_a_whole_hand_reaches_a_showdown_with_the_next_hand_offered(self):
        self._open()
        self._seat(100, 101, 102)
        self._start()
        for _ in range(40):
            table = poker.load_table(self.ENTRY)
            if poker.hand_is_over(table):
                break
            self._act_current(poker.legal_actions(table)[0])

        table = poker.load_table(self.ENTRY)
        self.assertTrue(poker.hand_is_over(table))
        final = self.api.to_group()[-1]
        self.assertIn("итог", final["text"])
        actions = [
            poker.parse_callback(button["callback_data"])[0]
            for row in final["reply_markup"]["inline_keyboard"] for button in row
        ]
        self.assertEqual(actions, ["next", "end"])
        self.assertEqual(table["hand"]["showdown_message_id"], final["message_id"])

    def test_the_next_hand_deals_again_and_moves_the_button(self):
        self._open()
        self._seat(100, 101, 102)
        self._start()
        while not poker.hand_is_over(poker.load_table(self.ENTRY)):
            self._act_current(poker.legal_actions(poker.load_table(self.ENTRY))[0])
        first = poker.load_table(self.ENTRY)

        table = self._callback("next", DEALER, first["hand_no"])

        self.assertEqual(table["hand_no"], 2)
        self.assertEqual(table["button"], (first["button"] + 1) % 3)
        self.assertEqual(table["phase"], poker.PHASE_HAND)
        retired = [
            e for e in self.api.markup_edits
            if e["message_id"] == first["hand"]["showdown_message_id"]
        ]
        self.assertTrue(retired, "the finished hand kept a live «Следующая раздача»")

    def test_only_the_dealer_deals_the_next_hand(self):
        self._open()
        self._seat(100, 101)
        self._start()
        while not poker.hand_is_over(poker.load_table(self.ENTRY)):
            self._act_current(poker.legal_actions(poker.load_table(self.ENTRY))[0])

        table = self._callback("next", _user(100), poker.load_table(self.ENTRY)["hand_no"])

        self.assertEqual(table["hand_no"], 1)
        self.assertIn("только диллер", self.api.answers[-1])

    # --- закрытие стола ---

    def test_the_dealer_closes_the_table_and_the_session_is_summed_up(self):
        self._open()
        self._seat(100, 101)
        self._start()

        self.assertIsNone(self._callback("end", DEALER))
        self.assertIn("Стол закрыт", self.api.to_group()[-1]["text"])
        self.assertIn("Итог сессии", self.api.to_group()[-1]["text"])

    def test_a_chat_administrator_can_close_a_table_the_dealer_abandoned(self):
        """Without this the table is wedged forever -- and no new one can be opened while
        it exists."""
        self._open()
        self._seat(100, 101)
        self._start()

        self.assertIsNone(self._callback("end", ADMIN))
        self.assertIn("Стол закрыт", self.api.to_group()[-1]["text"])

    def test_the_command_closes_a_table_whose_buttons_have_scrolled_away(self):
        self._open()
        self._seat(100, 101)
        self._start()

        self._open(text="/poker стоп")

        self.assertIsNone(poker.load_table(self.ENTRY))
        # The acknowledgement follows the command; the standings go to the chat.
        self.assertIn("Итог сессии", "\n".join(item["text"] for item in self.api.to_group()))

    def test_closing_makes_room_for_a_new_table(self):
        first = self._open()
        self._open(text="/poker стоп")
        second = self._open()

        self.assertIsNotNone(second)
        self.assertNotEqual(second["table_id"], first["table_id"])

    def test_an_administrator_without_the_badge_can_close_a_stuck_table(self):
        self._open()
        self._open(actor=ADMIN, text="/poker стоп")

        self.assertIsNone(poker.load_table(self.ENTRY))

    def test_a_random_member_cannot_close_a_table_by_command(self):
        table = self._open()
        self._open(actor=_user(100), text="/poker закрыть")

        self.assertEqual(poker.load_table(self.ENTRY)["table_id"], table["table_id"])

    def test_closing_when_there_is_no_table_says_so(self):
        self._open(text="/poker стоп")

        self.assertIn("Открытого стола нет", self.api.sent[-1]["text"])

    def test_an_ordinary_player_cannot_close_the_table(self):
        self._open()
        self._seat(100, 101)
        self._start()

        self.assertIsNotNone(self._callback("end", _user(100)))
        self.assertIn("только диллер", self.api.answers[-1])

    def test_a_button_from_a_closed_table_answers_instead_of_crashing(self):
        self._open()
        table = poker.load_table(self.ENTRY)
        poker.clear_table(self.ENTRY)

        asyncio.run(bot_listener.handle_poker_callback(
            self.api,
            _press(poker.callback_data("join", table["table_id"]), _user(100)),
            self.ENTRY, log=lambda *_: None,
        ))

        self.assertIn("закрыт", self.api.answers[-1])

    def test_a_poker_button_reaches_its_handler_through_update_dispatch(self):
        """Pins the callback prefix wiring, not just the leaf handler."""
        handled = []

        async def handle(*args, **kwargs):
            handled.append(args[1])

        async def dispatch():
            with patch.object(bot_listener, "handle_poker_callback", handle):
                await bot_listener._dispatch_update(
                    {"callback_query": _press(poker.callback_data("join", "abcd1234"), DEALER)},
                    self.api, None, None, None, None, 1, set(), asyncio.Queue(),
                    set(), "chat", {}, {}, None, {}, {}, {},
                    log=lambda *_: None,
                )

        asyncio.run(dispatch())
        self.assertEqual(len(handled), 1)


class DealerBadgeTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def _give(self, name, user_id):
        badge = stats.create_custom_badge("chat", "🎲", name, DEALER["id"], "Диллер")
        stats.give_custom_badge("chat", badge.badge_id, user_id, "Кто-то", DEALER["id"], "Диллер")

    def test_the_bot_creates_the_badge_so_it_only_has_to_be_given(self):
        self.assertTrue(poker.ensure_dealer_badge("chat"))

        created = [b for b in stats.list_custom_badges("chat") if b.badge_id == poker.DEALER_BADGE_ID]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].name, poker.DEALER_BADGE_NAME)
        self.assertEqual(created[0].emoji, poker.DEALER_BADGE_EMOJI)

    def test_creating_it_again_changes_nothing(self):
        """It runs on every startup, so a second run must not add a second badge."""
        poker.ensure_dealer_badge("chat")
        self.assertFalse(poker.ensure_dealer_badge("chat"))
        self.assertEqual(len(stats.list_custom_badges("chat")), 1)

    def test_it_is_created_even_in_a_chat_that_filled_its_badge_budget(self):
        for index in range(stats.MAX_CUSTOM_BADGES):
            stats.create_custom_badge("chat", "🎯", f"Значок {index}", DEALER["id"], "Д")

        self.assertTrue(poker.ensure_dealer_badge("chat"))
        self.assertTrue(any(b.badge_id == poker.DEALER_BADGE_ID for b in stats.list_custom_badges("chat")))

    def test_the_badge_the_bot_made_can_be_given_and_opens_a_table(self):
        poker.ensure_dealer_badge("chat")
        stats.give_custom_badge("chat", poker.DEALER_BADGE_ID, 100, "Аня", DEALER["id"], "Д")

        self.assertTrue(poker.is_dealer("chat", 100))
        self.assertFalse(poker.is_dealer("chat", 101))

    def test_only_the_badge_holder_counts_as_a_dealer(self):
        self._give("Диллер", 100)
        self.assertTrue(poker.is_dealer("chat", 100))
        self.assertFalse(poker.is_dealer("chat", 101))

    def test_the_other_spelling_works_too(self):
        self._give("Дилер", 100)
        self.assertTrue(poker.is_dealer("chat", 100))

    def test_another_badge_does_not_make_a_dealer(self):
        self._give("Художник", 100)
        self.assertFalse(poker.is_dealer("chat", 100))

    def test_a_chat_with_no_badges_has_no_dealers(self):
        self.assertFalse(poker.is_dealer("chat", 100))
