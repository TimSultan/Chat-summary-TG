"""Морской бой in the group chat: two members, a 10×10 field, fleets placed at random.

One member types "/seabattle @name" (or answers somebody's message with it). That posts
an invitation only the named member can accept; nobody accepting within
INVITE_TTL_SECONDS removes it. Once accepted, both fleets are placed at random and a coin
toss picks who fires first.

Everything here is pure: game state, the rules, and the text and buttons each screen is
drawn from. Every Telegram call -- sending, deleting, the timers -- lives in
bot_listener (handle_seabattle_command / handle_seabattle_callback), so the rules can be
played out in a test without a fake API.

WHY THE FIELD IS TEXT, NOT BUTTONS. Telegram caps an inline keyboard at 8 buttons per row
and 100 in total, so a 10×10 grid of buttons cannot exist at all, let alone leave room for
"Сдаться". The field is an emoji grid in the message, and a shot is two taps: a row
letter, then a cell in that row. Picking the row only edits the message (the row lights
up in the grid); the shot itself replaces it.

Classic rules: the fleet is one 4-deck, two 3-deck, three 2-deck and four 1-deck ships,
no two ships touch, not even at a corner. A hit earns another shot; a miss passes the
turn. Sinking a ship marks the water around it, since nothing can be there.
"""

from __future__ import annotations

import html
import random
import secrets
from dataclasses import dataclass, field
from typing import Final

SIZE: Final = 10
FLEET: Final = (4, 3, 3, 2, 2, 2, 1, 1, 1, 1)
# The traditional Russian letters: А to К, skipping Й.
ROWS: Final = "АБВГДЕЖЗИК"
INVITE_TTL_SECONDS: Final = 30
# Nothing stops a player from walking away mid-game, and a game nobody finishes would keep
# both players "busy" until a restart. Whoever is on the move and does nothing for this
# long loses.
TURN_TIMEOUT_SECONDS: Final = 3 * 60
# "Сдаться" sits under the field next to buttons pressed dozens of times a game, so one
# stray tap must not end it: the first press only asks, a second within this window does.
SURRENDER_CONFIRM_SECONDS: Final = 5
CALLBACK_PREFIX: Final = "sea"

PHASE_INVITE: Final = "invite"
PHASE_PLAYING: Final = "playing"
PHASE_FINISHED: Final = "finished"

MISS: Final = "miss"
HIT: Final = "hit"
SUNK: Final = "sunk"
REPEAT: Final = "repeat"

# How a cell of the board being fired at looks to everybody in the chat. Only what the
# shooter already knows is drawn; an unhit ship is fog like any other water.
_FOG: Final = "🟦"
_AIM: Final = "🟨"
_MISS: Final = "⚪"
_HIT: Final = "🔥"
_SUNK: Final = "🟥"
_LAST_HIT: Final = "💥"
_SHIP: Final = "⬛"
_KEYCAPS: Final = tuple(f"{n}️⃣" for n in range(1, 10)) + ("\U0001F51F",)
# The private "Мой флот" popup. answerCallbackQuery takes at most 200 characters, and an
# emoji outside the Basic Multilingual Plane costs two of them, so this palette is BMP only:
# ten rows of 🟦 alone would be 209 and the popup would never appear.
_POPUP_WATER: Final = "⬜"
_POPUP_SHIP: Final = "⬛"
_POPUP_HIT: Final = "❌"
_POPUP_MISS: Final = "⚪"
POPUP_LIMIT: Final = 200

_SHIP_NAMES: Final = {1: "однопалубный", 2: "двухпалубный", 3: "трёхпалубный", 4: "четырёхпалубный"}
_NAME_LIMIT: Final = 24

Cell = tuple[int, int]


def _neighbours(cell: Cell):
    """The cell itself and the up-to-eight around it, clipped to the field."""
    row, col = cell
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            r, c = row + dr, col + dc
            if 0 <= r < SIZE and 0 <= c < SIZE:
                yield r, c


def place_fleet(rng=None) -> list[tuple[Cell, ...]]:
    """A random legal fleet: every FLEET ship on the field, none touching another.

    Largest first, each ship drawn uniformly from every position still legal for it, so
    the loop never retries a spot it already knows is blocked. The whole fleet starts
    over only if a ship has nowhere left at all, which the classic fleet on a 10×10 field
    almost never runs into.
    """
    rng = rng or random
    while True:
        blocked: set[Cell] = set()
        ships: list[tuple[Cell, ...]] = []
        for size in FLEET:
            options = []
            for horizontal in ((True,) if size == 1 else (True, False)):
                for row in range(SIZE if horizontal else SIZE - size + 1):
                    for col in range(SIZE - size + 1 if horizontal else SIZE):
                        cells = tuple(
                            (row, col + i) if horizontal else (row + i, col) for i in range(size)
                        )
                        if not any(cell in blocked for cell in cells):
                            options.append(cells)
            if not options:
                break
            cells = rng.choice(options)
            ships.append(cells)
            for cell in cells:
                blocked.update(_neighbours(cell))
        else:
            return ships


@dataclass
class Board:
    """One player's fleet and every shot fired at it."""

    ships: list[tuple[Cell, ...]]
    shots: set = field(default_factory=set)
    # Water around a sunk ship: known to be empty without anybody having fired there.
    cleared: set = field(default_factory=set)

    def __post_init__(self) -> None:
        self._ship_of = {cell: index for index, ship in enumerate(self.ships) for cell in ship}

    def ship_index(self, cell: Cell) -> int | None:
        return self._ship_of.get(cell)

    def is_sunk(self, index: int) -> bool:
        return all(cell in self.shots for cell in self.ships[index])

    def afloat(self) -> int:
        return sum(1 for index in range(len(self.ships)) if not self.is_sunk(index))

    def defeated(self) -> bool:
        return self.afloat() == 0

    def fire(self, cell: Cell) -> str:
        if cell in self.shots or cell in self.cleared:
            return REPEAT
        self.shots.add(cell)
        index = self.ship_index(cell)
        if index is None:
            return MISS
        if not self.is_sunk(index):
            return HIT
        for deck in self.ships[index]:
            for near in _neighbours(deck):
                if near not in self._ship_of and near not in self.shots:
                    self.cleared.add(near)
        return SUNK

    def view(self, cell: Cell) -> str:
        """What the OTHER player knows about a cell: "fog", MISS, HIT or SUNK."""
        if cell in self.shots:
            index = self.ship_index(cell)
            if index is None:
                return MISS
            return SUNK if self.is_sunk(index) else HIT
        return MISS if cell in self.cleared else "fog"

    def row_open(self, row: int) -> bool:
        return any(self.view((row, col)) == "fog" for col in range(SIZE))


@dataclass
class Player:
    user_id: int | None
    name: str
    username: str | None = None

    def matches(self, user_id, username: str | None) -> bool:
        """Whether a Telegram user is this player. By id whenever the id is known; an
        invitation addressed by @username alone matches the username until accepted."""
        if self.user_id is not None:
            return user_id is not None and str(self.user_id) == str(user_id)
        return bool(
            self.username and username and self.username.casefold() == username.casefold()
        )


@dataclass
class Game:
    game_id: str
    chat_id: int
    # [challenger, invited]
    players: list[Player]
    phase: str = PHASE_INVITE
    # boards[i] is player i's own fleet -- the one the OTHER player fires at.
    boards: list[Board] = field(default_factory=list)
    # Index of the player firing now.
    turn: int = 0
    # Every shot fired so far. Also stamped into every field button, so a press on a field
    # that has since changed is recognised instead of firing at the wrong board.
    shots: int = 0
    shots_by: list[int] = field(default_factory=lambda: [0, 0])
    selected_row: int | None = None
    message_id: int | None = None
    command_message_id: int | None = None
    last_event: str = ""
    # Whether the shooter has just changed. Only then is their name a mention: a ping per
    # shot would be a ping per hit for somebody who is already looking at the field.
    turn_passed: bool = True
    # (board index, row, col) of the latest shot.
    last_shot: tuple[int, int, int] | None = None
    winner: int | None = None
    end_reason: str | None = None
    created_at: float = 0.0
    last_move_at: float = 0.0
    surrender_armed: dict = field(default_factory=dict)


def new_game(chat_id: int, challenger: Player, invited: Player, *, now: float,
             game_id: str | None = None) -> Game:
    return Game(
        game_id=game_id or secrets.token_hex(4), chat_id=chat_id,
        players=[challenger, invited], created_at=now, last_move_at=now,
    )


def is_live(game: Game) -> bool:
    return game.phase in (PHASE_INVITE, PHASE_PLAYING)


def player_index(game: Game, user_id, username: str | None) -> int | None:
    for index, player in enumerate(game.players):
        if player.matches(user_id, username):
            return index
    return None


def same_person(player: Player, user_id, username: str | None) -> bool:
    """Looser than Player.matches, for "is this person already playing": an id on either
    side that agrees, or a username on either side that agrees. An invitation addressed
    by @name has no id yet, and a challenger typing "@name" has no id for the target."""
    if player.user_id is not None and user_id is not None and str(player.user_id) == str(user_id):
        return True
    return bool(
        player.username and username and player.username.casefold() == username.casefold()
    )


def busy_game(games: dict, user_id, username: str | None, *, exclude: Game | None = None) -> Game | None:
    """The unfinished game or invitation this person is already in, if any."""
    for game in games.values():
        if game is exclude or not is_live(game):
            continue
        if any(same_person(player, user_id, username) for player in game.players):
            return game
    return None


def start(game: Game, *, now: float, rng=None) -> None:
    """The invitation was accepted: place both fleets and toss for the first shot."""
    rng = rng or random
    game.boards = [Board(place_fleet(rng)), Board(place_fleet(rng))]
    game.turn = rng.randrange(2)
    game.phase = PHASE_PLAYING
    game.last_move_at = now
    game.turn_passed = True
    game.last_event = f"🎲 Жребий: первым стреляет {_bold(game.players[game.turn])}"


def target_board(game: Game) -> Board:
    return game.boards[1 - game.turn]


def cell_name(row: int, col: int) -> str:
    return f"{ROWS[row]}{col + 1}"


def fire(game: Game, row: int, col: int, *, now: float) -> str:
    """The shooter fires at (row, col) on the other fleet. Returns MISS, HIT, SUNK, or
    REPEAT for a cell already known, which changes nothing at all."""
    board = target_board(game)
    result = board.fire((row, col))
    if result == REPEAT:
        return result
    shooter = game.players[game.turn]
    game.shots += 1
    game.shots_by[game.turn] += 1
    game.last_shot = (1 - game.turn, row, col)
    game.selected_row = None
    game.last_move_at = now
    game.surrender_armed.clear()
    where = f"{_bold(shooter)} → {cell_name(row, col)}"
    if result == MISS:
        game.last_event = f"💦 {where}: мимо"
        game.turn = 1 - game.turn
        game.turn_passed = True
    elif result == HIT:
        game.last_event = f"💥 {where}: попадание!"
        game.turn_passed = False
    else:
        size = len(board.ships[board.ship_index((row, col))])
        game.last_event = f"🟥 {where}: потоплен {_SHIP_NAMES[size]}!"
        game.turn_passed = False
        if board.defeated():
            finish(game, game.turn, "fleet")
    return result


def finish(game: Game, winner: int, reason: str) -> None:
    game.phase = PHASE_FINISHED
    game.winner = winner
    game.end_reason = reason
    game.selected_row = None


def request_surrender(game: Game, index: int, *, now: float) -> bool:
    """First press arms, a second within SURRENDER_CONFIRM_SECONDS gives up. True once
    the game is over."""
    armed_at = game.surrender_armed.get(index)
    if armed_at is not None and now - armed_at <= SURRENDER_CONFIRM_SECONDS:
        finish(game, 1 - index, "surrender")
        return True
    game.surrender_armed[index] = now
    return False


def time_out(game: Game) -> None:
    """Whoever is on the move has let the clock run out."""
    finish(game, 1 - game.turn, "timeout")


# --- callback data ---------------------------------------------------------------------


def callback_data(game: Game, action: str, *args: int) -> str:
    return ":".join([CALLBACK_PREFIX, game.game_id, str(game.shots), action, *map(str, args)])


def parse_callback(data: str) -> tuple[str, int, str, list[int]] | None:
    """(game id, shot count it was drawn at, action, integer arguments), or None."""
    parts = (data or "").split(":")
    if len(parts) < 4 or parts[0] != CALLBACK_PREFIX:
        return None
    try:
        seq = int(parts[2])
        args = [int(part) for part in parts[4:]]
    except ValueError:
        return None
    return parts[1], seq, parts[3], args


# --- screens ---------------------------------------------------------------------------


def _short(name: str) -> str:
    name = " ".join((name or "").split()) or "Игрок"
    return name if len(name) <= _NAME_LIMIT else name[: _NAME_LIMIT - 1] + "…"


def _bold(player: Player) -> str:
    return f"<b>{html.escape(_short(player.name))}</b>"


def _mention(player: Player) -> str:
    if player.user_id is None:
        # "@name" in plain text is a mention Telegram links and notifies by itself.
        return html.escape(_short(player.name))
    return f'<a href="tg://user?id={int(player.user_id)}">{html.escape(_short(player.name))}</a>'


def _title(game: Game) -> str:
    first, second = (html.escape(_short(p.name)) for p in game.players)
    return f"⚓ <b>Морской бой</b> · {first} ⚔️ {second}"


def _header() -> str:
    return "<code>~</code> " + "".join(_KEYCAPS)


def _row_label(row: int) -> str:
    # Monospace, so every label is the same width and the emoji columns line up under the
    # keycaps whatever the client's font does with Д next to Г.
    return f"<code>{ROWS[row]}</code> "


def _fleet_bar(board: Board) -> str:
    """Every ship, biggest first: ▰ afloat, ▱ sunk."""
    ships = sorted(
        ((len(ship), board.is_sunk(index)) for index, ship in enumerate(board.ships)),
        key=lambda item: (-item[0], item[1]),
    )
    return "🚢 " + " ".join(("▱" if sunk else "▰") * size for size, sunk in ships)


def _target_grid(game: Game) -> list[str]:
    index = 1 - game.turn
    board = game.boards[index]
    lines = [_header()]
    for row in range(SIZE):
        cells = []
        for col in range(SIZE):
            state = board.view((row, col))
            if state == "fog":
                glyph = _AIM if row == game.selected_row else _FOG
            elif state == MISS:
                glyph = _MISS
            elif state == HIT:
                glyph = _HIT
            else:
                glyph = _SUNK
            if state in (HIT, SUNK) and game.last_shot == (index, row, col):
                glyph = _LAST_HIT
            cells.append(glyph)
        lines.append(_row_label(row) + "".join(cells))
    return lines


def _revealed_grid(board: Board) -> list[str]:
    """A whole fleet, for the final message: ships nobody found are shown too."""
    lines = [_header()]
    for row in range(SIZE):
        cells = []
        for col in range(SIZE):
            cell = (row, col)
            index = board.ship_index(cell)
            if index is None:
                cells.append(_MISS if cell in board.shots else _FOG)
            elif cell in board.shots:
                cells.append(_SUNK if board.is_sunk(index) else _HIT)
            else:
                cells.append(_SHIP)
        lines.append(_row_label(row) + "".join(cells))
    return lines


def invite_text(game: Game) -> str:
    challenger, invited = game.players
    return (
        "⚓ <b>Морской бой</b>\n\n"
        f"{_bold(challenger)} бросает вызов!\n"
        f"{_mention(invited)}, принимаешь бой?\n"
        "Поле 10×10, флот расставлю сам.\n\n"
        f"⏳ {INVITE_TTL_SECONDS} сек на ответ"
    )


def invite_keyboard(game: Game) -> dict:
    return {"inline_keyboard": [[
        {"text": "⚓ Принять бой", "callback_data": callback_data(game, "acc")},
        {"text": "✖ Отказ", "callback_data": callback_data(game, "dec")},
    ]]}


def declined_text(game: Game, by_index: int) -> str:
    player = _bold(game.players[by_index])
    if by_index == 0:
        return f"⚓ <b>Морской бой</b>\n✖ {player} отменяет вызов."
    return f"⚓ <b>Морской бой</b>\n🏳 {player} отклоняет вызов."


def board_text(game: Game) -> str:
    shooter = game.players[game.turn]
    target = game.players[1 - game.turn]
    if game.selected_row is not None:
        hint = f"строка {ROWS[game.selected_row]}: выбери клетку"
    elif not game.turn_passed:
        hint = "ещё выстрел, выбери строку"
    else:
        hint = "выбери строку"
    name = _mention(shooter) if game.turn_passed else _bold(shooter)
    lines = [
        _title(game),
        f"🌊 Поле: {_bold(target)}",
        "",
        *_target_grid(game),
        _fleet_bar(target_board(game)),
        "",
    ]
    if game.last_event:
        lines.append(game.last_event)
    lines.append(f"🎯 Ход: {name} — {hint}")
    return "\n".join(lines)


def board_keyboard(game: Game) -> dict:
    board = target_board(game)
    fleet = {"text": "🚢 Мой флот", "callback_data": callback_data(game, "m")}
    give_up = {"text": "🏳 Сдаться", "callback_data": callback_data(game, "s")}
    row = game.selected_row
    if row is None:
        buttons = [
            {"text": ROWS[r] if board.row_open(r) else "·", "callback_data": callback_data(game, "r", r)}
            for r in range(SIZE)
        ]
        return {"inline_keyboard": [buttons[:5], buttons[5:], [fleet, give_up]]}
    marks = {MISS: _MISS, HIT: _HIT, SUNK: _SUNK}
    buttons = []
    for col in range(SIZE):
        state = board.view((row, col))
        buttons.append({
            "text": cell_name(row, col) if state == "fog" else marks[state],
            "callback_data": callback_data(game, "f", row, col),
        })
    back = {"text": "↩ Назад", "callback_data": callback_data(game, "b")}
    return {"inline_keyboard": [buttons[:5], buttons[5:], [back, fleet, give_up]]}


def final_text(game: Game) -> str:
    winner = game.players[game.winner]
    loser = game.players[1 - game.winner]
    lines = [_title(game), f"🏆 Победа: {_mention(winner)}!"]
    if game.end_reason == "fleet":
        if game.last_event:
            lines.append(game.last_event)
        lines.append("Весь флот соперника на дне.")
    elif game.end_reason == "surrender":
        lines.append(f"🏳 {_bold(loser)} сдаётся.")
    elif game.end_reason == "timeout":
        lines.append(
            f"⏰ {_bold(loser)}: время на ход вышло ({TURN_TIMEOUT_SECONDS // 60} мин)."
        )
    first, second = game.players
    lines.append(
        f"🎯 Выстрелов: {html.escape(_short(first.name))} {game.shots_by[0]}"
        f" · {html.escape(_short(second.name))} {game.shots_by[1]}"
    )
    board = game.boards[game.winner]
    lines += ["", f"🌊 Флот: {_bold(winner)}", *_revealed_grid(board), _fleet_bar(board)]
    return "\n".join(lines)


def fleet_popup(game: Game, index: int) -> str:
    """A player's own fleet, for the popup only they see: ships, and where the other
    side has fired."""
    board = game.boards[index]
    rows = []
    for row in range(SIZE):
        cells = []
        for col in range(SIZE):
            cell = (row, col)
            ship = board.ship_index(cell) is not None
            shot = cell in board.shots
            if ship:
                cells.append(_POPUP_HIT if shot else _POPUP_SHIP)
            else:
                cells.append(_POPUP_MISS if shot else _POPUP_WATER)
        rows.append("".join(cells))
    return f"Твой флот: на плаву {board.afloat()} из {len(FLEET)}\n" + "\n".join(rows)
