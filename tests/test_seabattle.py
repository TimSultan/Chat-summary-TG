"""Морской бой: the rules (seabattle.py) and the chat flow around them (bot_listener).

The flow tests drive the real handlers against a recording fake of the Bot API, because
what was asked for is chat behaviour: an invitation only the named member can accept,
gone after 30 seconds unanswered, and after every shot the old field deleted and a new
one posted saying whose turn it is.
"""

import asyncio
import random
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_listener
import seabattle
from errors import ChatSummaryError
from tests.async_case import AsyncTestCase

CHAT_ID = -1001234567890
ANYA = {"id": 11, "first_name": "Аня", "username": "anya"}
BORIS = {"id": 22, "first_name": "Борис", "username": "boris"}
VERA = {"id": 33, "first_name": "Вера", "username": "vera"}

# A fixed legal fleet, so a test can aim instead of guessing.
FLEET_CELLS = [
    ((0, 0), (0, 1), (0, 2), (0, 3)),
    ((2, 0), (2, 1), (2, 2)),
    ((2, 4), (2, 5), (2, 6)),
    ((4, 0), (4, 1)),
    ((4, 3), (4, 4)),
    ((4, 6), (4, 7)),
    ((6, 0),),
    ((6, 2),),
    ((6, 4),),
    ((6, 6),),
]
WATER = (9, 9)


def _assert_legal_fleet(test, ships):
    test.assertEqual(sorted(len(ship) for ship in ships), sorted(seabattle.FLEET))
    owner = {}
    for index, ship in enumerate(ships):
        rows = {r for r, _ in ship}
        cols = {c for _, c in ship}
        test.assertTrue(len(rows) == 1 or len(cols) == 1, f"ship {ship} is not straight")
        line = sorted(cols) if len(rows) == 1 else sorted(rows)
        test.assertEqual(line, list(range(line[0], line[0] + len(ship))), f"ship {ship} has a gap")
        for r, c in ship:
            test.assertTrue(0 <= r < seabattle.SIZE and 0 <= c < seabattle.SIZE)
            owner[(r, c)] = index
    for (r, c), index in owner.items():
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                other = owner.get((r + dr, c + dc))
                test.assertIn(other, (None, index), f"ships {index} and {other} touch at {(r, c)}")


def _buttons(markup):
    return [button for row in (markup or {}).get("inline_keyboard", []) for button in row]


def _game(turn=0):
    game = seabattle.new_game(
        CHAT_ID, seabattle.Player(11, "Аня", "anya"), seabattle.Player(22, "Борис", "boris"),
        now=0.0, game_id="g1",
    )
    seabattle.start(game, now=0.0, rng=random.Random(1))
    game.boards = [seabattle.Board(list(FLEET_CELLS)), seabattle.Board(list(FLEET_CELLS))]
    game.turn = turn
    return game


class FleetTests(unittest.TestCase):
    def test_every_random_fleet_is_legal(self):
        for seed in range(300):
            with self.subTest(seed=seed):
                _assert_legal_fleet(self, seabattle.place_fleet(random.Random(seed)))

    def test_the_test_fleet_is_legal_too(self):
        _assert_legal_fleet(self, FLEET_CELLS)

    def test_two_players_get_different_fleets(self):
        game = seabattle.new_game(
            CHAT_ID, seabattle.Player(1, "A"), seabattle.Player(2, "B"), now=0.0,
        )
        seabattle.start(game, now=0.0, rng=random.Random(7))
        self.assertNotEqual(game.boards[0].ships, game.boards[1].ships)
        self.assertEqual(game.phase, seabattle.PHASE_PLAYING)
        self.assertIn(game.turn, (0, 1))


class RuleTests(unittest.TestCase):
    def test_a_miss_passes_the_turn_and_a_hit_keeps_it(self):
        game = _game()
        self.assertEqual(seabattle.fire(game, *WATER, now=1.0), seabattle.MISS)
        self.assertEqual(game.turn, 1)
        self.assertTrue(game.turn_passed)
        self.assertEqual(seabattle.fire(game, 0, 0, now=2.0), seabattle.HIT)
        self.assertEqual(game.turn, 1)
        self.assertFalse(game.turn_passed)
        self.assertEqual(game.shots, 2)
        self.assertEqual(game.shots_by, [1, 1])

    def test_a_known_cell_changes_nothing(self):
        game = _game()
        seabattle.fire(game, 0, 0, now=1.0)
        before = (game.shots, game.turn, game.last_event)
        self.assertEqual(seabattle.fire(game, 0, 0, now=2.0), seabattle.REPEAT)
        self.assertEqual((game.shots, game.turn, game.last_event), before)

    def test_sinking_a_ship_clears_the_water_around_it(self):
        game = _game()
        self.assertEqual(seabattle.fire(game, 6, 0, now=1.0), seabattle.SUNK)
        board = game.boards[1]
        for cell in ((5, 0), (5, 1), (6, 1), (7, 0), (7, 1)):
            self.assertEqual(board.view(cell), seabattle.MISS, cell)
        # Cleared water is known: firing there is refused and costs no turn.
        self.assertEqual(seabattle.fire(game, 7, 1, now=2.0), seabattle.REPEAT)
        self.assertEqual(game.turn, 0)
        self.assertIn("однопалубный", game.last_event)

    def test_the_last_ship_wins_the_game(self):
        game = _game()
        for ship in FLEET_CELLS:
            for cell in ship:
                self.assertEqual(game.turn, 0)
                seabattle.fire(game, *cell, now=1.0)
        self.assertEqual(game.phase, seabattle.PHASE_FINISHED)
        self.assertEqual(game.winner, 0)
        self.assertEqual(game.end_reason, "fleet")
        self.assertEqual(game.shots_by, [20, 0])

    def test_surrender_needs_a_second_press(self):
        game = _game()
        self.assertFalse(seabattle.request_surrender(game, 1, now=10.0))
        self.assertEqual(game.phase, seabattle.PHASE_PLAYING)
        # Too late: the first press has lapsed and this one only asks again.
        self.assertFalse(seabattle.request_surrender(
            game, 1, now=10.0 + seabattle.SURRENDER_CONFIRM_SECONDS + 1,
        ))
        self.assertTrue(seabattle.request_surrender(
            game, 1, now=10.0 + seabattle.SURRENDER_CONFIRM_SECONDS + 2,
        ))
        self.assertEqual((game.winner, game.end_reason), (0, "surrender"))

    def test_a_timeout_hands_the_game_to_the_other_side(self):
        game = _game(turn=1)
        seabattle.time_out(game)
        self.assertEqual((game.phase, game.winner, game.end_reason),
                         (seabattle.PHASE_FINISHED, 0, "timeout"))

    def test_an_invitation_by_username_matches_by_username_until_accepted(self):
        invited = seabattle.Player(None, "@Boris", "Boris")
        self.assertTrue(invited.matches(999, "boris"))
        self.assertFalse(invited.matches(999, "vera"))
        known = seabattle.Player(22, "Борис", "boris")
        self.assertFalse(known.matches(999, "boris"), "an id, once known, is the identity")

    def test_busy_finds_a_player_by_id_or_by_username(self):
        game = _game()
        games = {game.game_id: game}
        self.assertIs(seabattle.busy_game(games, 11, None), game)
        self.assertIs(seabattle.busy_game(games, None, "BORIS"), game)
        self.assertIsNone(seabattle.busy_game(games, 33, "vera"))
        self.assertIsNone(seabattle.busy_game(games, 11, None, exclude=game))
        seabattle.finish(game, 0, "surrender")
        self.assertIsNone(seabattle.busy_game(games, 11, None))


class ScreenTests(unittest.TestCase):
    def test_callback_data_round_trips_and_fits_telegram(self):
        game = _game()
        game.shots = 199
        data = seabattle.callback_data(game, "f", 9, 9)
        self.assertLessEqual(len(data.encode("utf-8")), 64)
        self.assertEqual(seabattle.parse_callback(data), ("g1", 199, "f", [9, 9]))
        for bad in ("", "pet:g1:0:f", "sea:g1", "sea:g1:x:f", "sea:g1:0:f:a"):
            self.assertIsNone(seabattle.parse_callback(bad), bad)

    def test_keyboards_respect_telegrams_limits(self):
        """8 buttons a row, 100 a keyboard -- why the field itself is text."""
        game = _game()
        markups = [seabattle.invite_keyboard(game), seabattle.board_keyboard(game)]
        game.selected_row = 4
        markups.append(seabattle.board_keyboard(game))
        for markup in markups:
            rows = markup["inline_keyboard"]
            self.assertTrue(all(len(row) <= 8 for row in rows))
            self.assertLessEqual(sum(len(row) for row in rows), 100)
            for button in _buttons(markup):
                self.assertLessEqual(len(button["callback_data"].encode("utf-8")), 64)

    def test_the_field_is_ten_by_ten_and_hides_unhit_ships(self):
        game = _game()
        seabattle.fire(game, 0, 0, now=1.0)
        text = seabattle.board_text(game)
        grid = [line for line in text.splitlines() if line.startswith("<code>")]
        self.assertEqual(len(grid), 11, "a header and ten rows")
        self.assertEqual(grid[1].count("🟦") + grid[1].count("💥"), 10)
        self.assertNotIn("⬛", text, "an unhit ship must look like water")
        self.assertIn("💥", grid[1])
        self.assertIn("🎯 Ход:", text)
        self.assertIn("Аня", text.split("🎯 Ход:")[1])

    def test_picking_a_row_lights_it_up_and_offers_its_cells(self):
        game = _game()
        game.selected_row = 3
        text = seabattle.board_text(game)
        self.assertEqual([line for line in text.splitlines() if line.startswith("<code>Г</code>")][0].count("🟨"), 10)
        labels = [b["text"] for b in _buttons(seabattle.board_keyboard(game))]
        self.assertEqual(labels[:10], [f"Г{n}" for n in range(1, 11)])

    def test_the_turn_line_pings_only_when_the_turn_changes(self):
        game = _game()
        self.assertIn("tg://user?id=11", seabattle.board_text(game).split("🎯 Ход:")[1])
        seabattle.fire(game, 0, 0, now=1.0)
        self.assertNotIn("tg://user", seabattle.board_text(game).split("🎯 Ход:")[1])
        seabattle.fire(game, *WATER, now=2.0)
        self.assertIn("tg://user?id=22", seabattle.board_text(game).split("🎯 Ход:")[1])

    def test_the_fleet_popup_fits_an_alert(self):
        game = _game()
        for cell in ((0, 0), (9, 9), (5, 5)):
            game.boards[0].fire(cell)
        popup = seabattle.fleet_popup(game, 0)
        self.assertLessEqual(len(popup.encode("utf-16-le")) // 2, seabattle.POPUP_LIMIT)
        rows = popup.splitlines()[1:]
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0][0], "❌")
        self.assertEqual(rows[9][9], "⚪")

    def test_the_final_message_reveals_the_winners_fleet(self):
        game = _game()
        seabattle.request_surrender(game, 1, now=1.0)
        seabattle.request_surrender(game, 1, now=2.0)
        text = seabattle.final_text(game)
        self.assertIn("🏆 Победа:", text)
        self.assertIn("сдаётся", text)
        self.assertEqual(text.count("⬛"), 20)

    def test_names_are_escaped(self):
        game = seabattle.new_game(
            CHAT_ID, seabattle.Player(1, "<b>x</b>"), seabattle.Player(2, "a&b"), now=0.0,
        )
        self.assertNotIn("<b>x</b>", seabattle.invite_text(game))
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", seabattle.invite_text(game))
        self.assertIn("a&amp;b", seabattle.invite_text(game))


class FakeApi:
    def __init__(self):
        self.sent = []
        self.edited = []
        self.deleted = []
        self.answered = []
        self.markups = []
        self.fail_sends = 0
        self._next_id = 500

    async def send_message(self, chat_id, text, reply_to_message_id=None, reply_markup=None,
                           parse_mode=None, disable_notification=False):
        if self.fail_sends:
            self.fail_sends -= 1
            raise ChatSummaryError("Too Many Requests: retry after 5")
        self._next_id += 1
        item = {"message_id": self._next_id, "chat_id": chat_id, "text": text,
                "reply_markup": reply_markup, "reply_to": reply_to_message_id,
                "parse_mode": parse_mode}
        self.sent.append(item)
        return item

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None, parse_mode=None):
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text,
                            "reply_markup": reply_markup})

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.markups.append((chat_id, message_id, reply_markup))

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)

    async def answer_callback_query(self, callback_id, text=None, url=None, show_alert=False):
        self.answered.append({"id": callback_id, "text": text, "alert": show_alert})


def _command(text, user=ANYA, chat_type="supergroup", reply_to=None, entities=None, message_id=7):
    message = {
        "message_id": message_id,
        "chat": {"id": CHAT_ID if chat_type != "private" else user["id"], "type": chat_type},
        "from": user, "text": text,
    }
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": 3, "from": reply_to}
    if entities is not None:
        message["entities"] = entities
    return message


class ChatFlowTests(AsyncTestCase):
    def setUp(self):
        self.api = FakeApi()
        self.games = {}
        self.tasks = set()
        self.logged = []

    async def asyncTearDown(self):
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def log(self, line):
        self.logged.append(line)

    async def invite(self, text="/seabattle @boris", **kwargs):
        await bot_listener.handle_seabattle_command(
            self.api, None, None, _command(text, **kwargs), text, None, self.games,
            self.tasks, bot_username="testbot", log=self.log,
        )

    async def press(self, user, data, message_id=None):
        game = next(iter(self.games.values()), None)
        await bot_listener.handle_seabattle_callback(
            self.api,
            {"id": f"cb{len(self.api.answered)}", "from": user, "data": data,
             "message": {"message_id": message_id if message_id is not None
                         else (game.message_id if game else 1),
                         "chat": {"id": CHAT_ID}}},
            self.games, self.tasks, log=self.log,
        )
        return self.api.answered[-1]

    async def started_game(self):
        await self.invite()
        game = next(iter(self.games.values()))
        await self.press(BORIS, seabattle.callback_data(game, "acc"))
        game.boards = [seabattle.Board(list(FLEET_CELLS)), seabattle.Board(list(FLEET_CELLS))]
        game.turn = 0
        game.turn_passed = True
        self.api.sent.clear()
        self.api.deleted.clear()
        self.api.edited.clear()
        return game

    # --- the invitation --------------------------------------------------------------

    async def test_the_command_posts_an_invitation_with_accept_and_decline(self):
        await self.invite()
        self.assertEqual(len(self.games), 1)
        game = next(iter(self.games.values()))
        self.assertEqual(game.phase, seabattle.PHASE_INVITE)
        invite = self.api.sent[-1]
        self.assertEqual(invite["message_id"], game.message_id)
        self.assertIn("@boris", invite["text"])
        self.assertIn("30 сек", invite["text"])
        actions = [seabattle.parse_callback(b["callback_data"])[2] for b in _buttons(invite["reply_markup"])]
        self.assertEqual(actions, ["acc", "dec"])

    async def test_only_the_invited_member_can_accept(self):
        await self.invite()
        game = next(iter(self.games.values()))
        answer = await self.press(VERA, seabattle.callback_data(game, "acc"))
        self.assertIn("не тебе", answer["text"])
        answer = await self.press(ANYA, seabattle.callback_data(game, "acc"))
        self.assertIn("соперник", answer["text"])
        self.assertEqual(game.phase, seabattle.PHASE_INVITE)
        self.assertEqual(len(self.api.sent), 1, "nobody but the invited member starts it")

    async def test_accepting_replaces_the_invitation_with_the_field(self):
        await self.invite()
        game = next(iter(self.games.values()))
        invite_id = game.message_id
        await self.press(BORIS, seabattle.callback_data(game, "acc"))
        self.assertEqual(game.phase, seabattle.PHASE_PLAYING)
        self.assertEqual(game.players[1].user_id, BORIS["id"], "learned from the press")
        self.assertEqual(game.players[1].name, "Борис")
        field = self.api.sent[-1]
        self.assertEqual(game.message_id, field["message_id"])
        self.assertIn("🎯 Ход:", field["text"])
        self.assertIn(invite_id, self.api.deleted)
        self.assertIn(7, self.api.deleted, "the command goes with the invitation")
        for board in game.boards:
            _assert_legal_fleet(self, board.ships)

    async def test_an_unanswered_invitation_is_removed_after_30_seconds(self):
        with patch.object(seabattle, "INVITE_TTL_SECONDS", 0):
            await self.invite()
            game = next(iter(self.games.values()))
            invite_id = game.message_id
            await asyncio.gather(*self.tasks)
        self.assertEqual(self.games, {})
        self.assertIn(invite_id, self.api.deleted)
        self.assertIn(7, self.api.deleted)
        answer = await self.press(BORIS, seabattle.callback_data(game, "acc"), message_id=invite_id)
        self.assertIn("закончилась", answer["text"])

    async def test_the_clock_waits_exactly_the_invitation_ttl(self):
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        await self.invite()
        game = next(iter(self.games.values()))
        with patch.object(bot_listener.asyncio, "sleep", fake_sleep):
            await bot_listener._seabattle_invite_clock(self.api, self.games, game, log=self.log)
        self.assertEqual(slept, [30])
        self.assertEqual(self.games, {})

    async def test_an_accepted_invitation_survives_its_clock(self):
        await self.invite()
        game = next(iter(self.games.values()))
        await self.press(BORIS, seabattle.callback_data(game, "acc"))
        deleted_before = list(self.api.deleted)
        self.assertFalse(await bot_listener._seabattle_expire_invite(self.api, self.games, game))
        self.assertEqual(self.api.deleted, deleted_before)
        self.assertEqual(game.phase, seabattle.PHASE_PLAYING)

    async def test_declining_says_so_and_then_clears_away(self):
        await self.invite()
        game = next(iter(self.games.values()))
        invite_id = game.message_id
        await asyncio.sleep(0)  # the invitation's own clock starts its real 30 s first
        before = set(self.tasks)
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        with patch.object(bot_listener.asyncio, "sleep", fake_sleep):
            await self.press(BORIS, seabattle.callback_data(game, "dec"))
            self.assertEqual(self.games, {})
            self.assertIn("отклоняет", self.api.edited[-1]["text"])
            self.assertEqual(self.api.edited[-1]["reply_markup"], {"inline_keyboard": []})
            self.assertEqual(self.api.deleted, [], "the refusal stays readable for a moment")
            await asyncio.gather(*(self.tasks - before))
        self.assertEqual(slept, [bot_listener.SEABATTLE_DECLINED_DELETE_AFTER])
        self.assertEqual(sorted(self.api.deleted), sorted([invite_id, 7]))

    async def test_refusals_are_short_notices(self):
        cases = (
            ("/seabattle", {}, "Кого вызываем"),
            ("/seabattle @anya", {}, "С самим собой"),
            ("/seabattle @testbot", {}, "С ботом"),
            ("/seabattle", {"reply_to": {"id": 99, "first_name": "Bot", "is_bot": True}}, "С ботом"),
            ("/seabattle Борис", {}, "Не нашёл"),
            ("/seabattle @boris", {"chat_type": "private"}, "в общем чате"),
        )
        for text, kwargs, expected in cases:
            with self.subTest(text=text, kwargs=kwargs):
                self.api.sent.clear()
                await self.invite(text, **kwargs)
                self.assertEqual(self.games, {})
                self.assertIn(expected, self.api.sent[-1]["text"])

    async def test_a_reply_names_the_opponent(self):
        await self.invite("/seabattle", reply_to=BORIS)
        game = next(iter(self.games.values()))
        self.assertEqual(game.players[1].user_id, BORIS["id"])

    async def test_a_mention_without_a_username_names_the_opponent(self):
        user = {"id": 44, "first_name": "Гоша"}
        await self.invite("/seabattle Гоша", entities=[
            {"type": "bot_command", "offset": 0, "length": 10},
            {"type": "text_mention", "offset": 11, "length": 4, "user": user},
        ])
        game = next(iter(self.games.values()))
        self.assertEqual((game.players[1].user_id, game.players[1].name), (44, "Гоша"))

    async def test_a_display_name_is_looked_up_among_tracked_members(self):
        found = SimpleNamespace(user_id="22", display_name="Борис Б.", username=None)

        async def resolve(*args, **kwargs):
            return found, 1, 2, 3, 4

        with patch.object(bot_listener.stats, "resolve_stat_target", resolve):
            await bot_listener.handle_seabattle_command(
                self.api, object(), None, _command("/seabattle Борис"), "/seabattle Борис",
                "Chat", self.games, self.tasks, bot_username="testbot", log=self.log,
            )
        game = next(iter(self.games.values()))
        self.assertEqual((game.players[1].user_id, game.players[1].name), (22, "Борис Б."))

    async def test_nobody_plays_two_games_at_once(self):
        await self.invite()
        self.api.sent.clear()
        await self.invite("/seabattle @vera", message_id=8)
        self.assertIn("уже в партии", self.api.sent[-1]["text"])
        await bot_listener.handle_seabattle_command(
            self.api, None, None, _command("/seabattle @boris", user=VERA), "/seabattle @boris",
            None, self.games, self.tasks, bot_username="testbot", log=self.log,
        )
        self.assertIn("уже играет", self.api.sent[-1]["text"])
        self.assertEqual(len(self.games), 1)

    # --- the game ----------------------------------------------------------------------

    async def test_picking_a_row_edits_and_a_shot_replaces_the_message(self):
        game = await self.started_game()
        field_id = game.message_id
        await self.press(ANYA, seabattle.callback_data(game, "r", 9))
        self.assertEqual(game.selected_row, 9)
        self.assertEqual(self.api.sent, [], "picking a row is not a move")
        self.assertEqual(self.api.deleted, [])
        self.assertEqual(self.api.edited[-1]["message_id"], field_id)
        self.assertIn("строка К", self.api.edited[-1]["text"])

        answer = await self.press(ANYA, seabattle.callback_data(game, "f", 9, 9))
        self.assertIn("Мимо", answer["text"])
        self.assertEqual(len(self.api.sent), 1)
        new_field = self.api.sent[-1]
        self.assertEqual(self.api.deleted, [field_id], "the old field is deleted")
        self.assertEqual(game.message_id, new_field["message_id"])
        turn_line = new_field["text"].split("🎯 Ход:")[1]
        self.assertIn("Борис", turn_line, "a miss hands the turn over, and the field says so")
        self.assertIn("К10: мимо", new_field["text"])

    async def test_a_hit_keeps_the_turn_on_a_new_message(self):
        game = await self.started_game()
        await self.press(ANYA, seabattle.callback_data(game, "r", 0))
        answer = await self.press(ANYA, seabattle.callback_data(game, "f", 0, 0))
        self.assertIn("Попадание", answer["text"])
        self.assertIn("Аня", self.api.sent[-1]["text"].split("🎯 Ход:")[1])
        self.assertEqual(game.turn, 0)

    async def test_only_the_shooter_can_aim(self):
        game = await self.started_game()
        answer = await self.press(BORIS, seabattle.callback_data(game, "r", 0))
        self.assertIn("стреляет Аня", answer["text"])
        answer = await self.press(VERA, seabattle.callback_data(game, "r", 0))
        self.assertIn("чужая партия", answer["text"])
        self.assertIsNone(game.selected_row)
        self.assertEqual((self.api.sent, self.api.edited), ([], []))

    async def test_a_press_on_an_old_field_fires_nothing(self):
        game = await self.started_game()
        stale = seabattle.callback_data(game, "f", 0, 0)
        old_id = game.message_id
        await self.press(ANYA, seabattle.callback_data(game, "f", 1, 9))  # a miss: Борис's turn
        await self.press(BORIS, seabattle.callback_data(game, "f", 9, 9))  # a miss: Аня again
        shots = game.shots
        answer = await self.press(ANYA, stale, message_id=old_id)
        self.assertIn("обновилось", answer["text"])
        self.assertEqual(game.shots, shots)

    async def test_a_shot_into_known_water_is_refused(self):
        game = await self.started_game()
        await self.press(ANYA, seabattle.callback_data(game, "f", 6, 0))  # sinks a 1-decker
        sent = len(self.api.sent)
        answer = await self.press(ANYA, seabattle.callback_data(game, "f", 7, 1))
        self.assertIn("уже стреляли", answer["text"])
        self.assertEqual(len(self.api.sent), sent)

    async def test_my_fleet_is_a_private_alert(self):
        game = await self.started_game()
        answer = await self.press(BORIS, seabattle.callback_data(game, "m"))
        self.assertTrue(answer["alert"])
        self.assertIn("Твой флот", answer["text"])
        self.assertEqual(self.api.sent, [])

    async def test_surrender_takes_two_presses_and_posts_the_result(self):
        game = await self.started_game()
        field_id = game.message_id
        answer = await self.press(BORIS, seabattle.callback_data(game, "s"))
        self.assertIn("ещё раз", answer["text"])
        self.assertEqual(self.api.sent, [])
        await self.press(BORIS, seabattle.callback_data(game, "s"))
        self.assertEqual(self.games, {})
        final = self.api.sent[-1]
        self.assertIn("🏆 Победа:", final["text"])
        self.assertIn("Аня", final["text"].split("🏆 Победа:")[1].splitlines()[0])
        self.assertEqual(final["reply_markup"], {"inline_keyboard": []})
        self.assertIn(field_id, self.api.deleted)

    async def test_sinking_the_whole_fleet_ends_the_game(self):
        game = await self.started_game()
        for ship in FLEET_CELLS:
            for row, col in ship:
                await self.press(ANYA, seabattle.callback_data(game, "f", row, col))
        self.assertEqual(self.api.answered[-1]["text"], "Победа! 🏆")
        self.assertEqual(self.games, {})
        self.assertIn("Весь флот соперника на дне", self.api.sent[-1]["text"])
        self.assertEqual(len(self.api.sent), 20, "one new message per shot")
        self.assertEqual(len(self.api.deleted), 20, "and the one before it deleted")

    async def test_a_turn_left_too_long_loses_the_game(self):
        game = await self.started_game()
        game.last_move_at -= seabattle.TURN_TIMEOUT_SECONDS + 1
        await bot_listener._seabattle_turn_clock(self.api, self.games, game, log=self.log)
        self.assertEqual((game.winner, game.end_reason), (1, "timeout"))
        self.assertEqual(self.games, {})
        self.assertIn("время на ход вышло", self.api.sent[-1]["text"])

    async def test_the_turn_clock_sleeps_until_the_deadline(self):
        game = await self.started_game()
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)
            # Somebody fires while the clock sleeps: the deadline moves on.
            if len(slept) == 1:
                game.last_move_at = -seabattle.TURN_TIMEOUT_SECONDS - 1

        with patch.object(bot_listener.time, "monotonic", return_value=0.0), \
                patch.object(bot_listener.asyncio, "sleep", fake_sleep):
            game.last_move_at = 0.0
            await bot_listener._seabattle_turn_clock(self.api, self.games, game, log=self.log)
        self.assertEqual(slept, [seabattle.TURN_TIMEOUT_SECONDS])
        self.assertEqual(game.end_reason, "timeout")

    async def test_a_refused_send_edits_the_old_field_instead(self):
        game = await self.started_game()
        field_id = game.message_id
        self.api.fail_sends = 1
        await self.press(ANYA, seabattle.callback_data(game, "f", 9, 9))
        self.assertEqual(game.message_id, field_id)
        self.assertEqual(self.api.deleted, [], "the only field must not be deleted")
        self.assertEqual(self.api.edited[-1]["message_id"], field_id)
        self.assertIn("К10: мимо", self.api.edited[-1]["text"])

    async def test_a_dead_field_loses_its_buttons(self):
        answer = await self.press(ANYA, "sea:gone:3:r:1", message_id=41)
        self.assertIn("закончилась", answer["text"])
        self.assertEqual(self.api.markups, [(CHAT_ID, 41, {"inline_keyboard": []})])


class RoutingTests(AsyncTestCase):
    """Through _dispatch_update itself: a router bug is invisible to a test of the leaf."""

    async def test_every_spelling_reaches_the_command_and_the_buttons_their_handler(self):
        reached = []

        def recorder(name):
            async def handle(*args, **kwargs):
                reached.append(name)
            return handle

        cfg = SimpleNamespace(listener_allowed_chats=[], stats_enabled=True, stats_top_limit=10)
        tasks = set()
        with patch.object(bot_listener, "handle_seabattle_command", recorder("command")), \
                patch.object(bot_listener, "handle_seabattle_callback", recorder("button")):
            for text in ("/seabattle @boris", "/морскойбой @boris", "/морской_бой",
                         "/seabattle@testbot @boris"):
                update = {"message": _command(text)}
                await bot_listener._dispatch_update(
                    update, FakeApi(), None, cfg, None, "testbot", 1, set(), asyncio.Queue(),
                    tasks, None, {}, {}, {}, {}, log=lambda *_: None,
                )
            await asyncio.gather(*tasks)
            await bot_listener._dispatch_update(
                {"callback_query": {"id": "c", "from": BORIS, "data": "sea:g1:0:acc",
                                    "message": {"message_id": 1, "chat": {"id": CHAT_ID}}}},
                FakeApi(), None, cfg, None, "testbot", 1, set(), asyncio.Queue(),
                tasks, None, {}, {}, {}, {}, log=lambda *_: None,
            )
        self.assertEqual(reached, ["command"] * 4 + ["button"])

    async def test_games_persist_between_updates(self):
        """The dispatcher must hand every update the same game registry."""
        api = FakeApi()
        cfg = SimpleNamespace(listener_allowed_chats=[], stats_enabled=True, stats_top_limit=10)
        games, tasks = {}, set()

        async def dispatch(update):
            await bot_listener._dispatch_update(
                update, api, None, cfg, None, "testbot", 1, set(), asyncio.Queue(),
                tasks, None, {}, {}, {}, {}, seabattle_games=games, log=lambda *_: None,
            )

        await dispatch({"message": _command("/seabattle @boris")})
        # The command runs as a background task; let it post the invitation.
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertEqual(len(games), 1)
        game = next(iter(games.values()))
        await dispatch({"callback_query": {
            "id": "c", "from": BORIS, "data": seabattle.callback_data(game, "acc"),
            "message": {"message_id": game.message_id, "chat": {"id": CHAT_ID}},
        }})
        self.assertEqual(game.phase, seabattle.PHASE_PLAYING)
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
