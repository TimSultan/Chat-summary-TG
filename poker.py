"""Техасский холдем for the chat: one table at a time, up to ten players, opened by a
member holding the «Диллер» badge.

Everything here is pure except the small JSON store at the bottom: the rules, the hand
evaluator, the rendering and the callback payloads all operate on plain dicts and never
touch Telegram. bot_listener.py owns every send, edit and toast. That split is what makes
a card game testable at all -- the evaluator and the betting round are ordinary functions
with ordinary return values, and the tests play whole hands without a network.

Chips are SESSION chips. They are dealt equal at the table and vanish with it: nothing
here reads or writes economy.py, so no hand can move a real coin balance, and a table
abandoned mid-hand costs nobody anything. That is a deliberate choice, not an oversight --
see the README.

Betting is fixed-limit: one bet size per street (BET_STEP), so every decision is a button
and nobody has to type a number into a group chat. The four actions asked for -- check,
bet, all-in, fold -- are joined by call, which is not optional: without it a bet could
only ever be folded to.
"""

import json
import random
import uuid
from collections import Counter
from html import escape
from itertools import combinations
from pathlib import Path

import stats
from app_time import now as app_now

CALLBACK_PREFIX = "poker"
COMMAND = "/poker"

# The badge that lets somebody open a table. Created by the bot itself with a FIXED id,
# the same way the founder badge is (stats.ensure_founder_badge), so an administrator only
# ever has to GIVE it -- nothing about a card game can be recomputed from a member's
# stats, so there is nothing for the bot to award automatically.
DEALER_BADGE_ID = "dealer"
DEALER_BADGE_EMOJI = "🃏"
DEALER_BADGE_NAME = "Диллер"
DEALER_BADGE_DESCRIPTION = "может открыть покерный стол"

# Held rights are still checked by NAME as well as by id: a chat that created its own
# «Диллер» badge by hand before this existed keeps working, and both spellings are in use.
# A lenient match is a better failure than a table nobody can open.
DEALER_BADGE_NAMES = ("диллер", "дилер")

MIN_PLAYERS = 2
MAX_PLAYERS = 10

# 50 big blinds each. Enough that a hand is decided by cards rather than by whoever
# shoves first, and small enough that a session actually ends.
START_STACK = 1_000
SMALL_BLIND = 10
BIG_BLIND = 20
BET_STEP = BIG_BLIND

STREETS = ("preflop", "flop", "turn", "river")
STREET_NAMES = {
    "preflop": "Префлоп",
    "flop": "Флоп",
    "turn": "Тёрн",
    "river": "Ривер",
}
# How many board cards each street adds.
STREET_CARDS = {"preflop": 0, "flop": 3, "turn": 1, "river": 1}

PHASE_LOBBY = "lobby"
PHASE_HAND = "hand"
PHASE_SHOWDOWN = "showdown"

# --- Карты ----------------------------------------------------------------------------

RANKS = "23456789TJQKA"
SUITS = ("♠", "♥", "♦", "♣")
# T is stored as one character so a card is always exactly two, and rendered as "10" so
# nobody has to decode it at a glance.
RANK_LABELS = {"T": "10"}

HAND_NAMES = (
    "Старшая карта",
    "Пара",
    "Две пары",
    "Тройка",
    "Стрит",
    "Флеш",
    "Фулл-хаус",
    "Каре",
    "Стрит-флеш",
)


def new_deck(rng=None) -> list[str]:
    """A shuffled 52-card deck. `rng` is injectable so a test can deal a known hand;
    live play uses SystemRandom, since a predictable shuffle in a game people bet on is
    the one bug nobody would report but everybody would notice."""
    deck = [rank + suit for suit in SUITS for rank in RANKS]
    (rng or random.SystemRandom()).shuffle(deck)
    return deck


def card_value(card: str) -> int:
    """2..14, aces high. The wheel (A-2-3-4-5) is handled in _straight_high."""
    return RANKS.index(card[0]) + 2


def format_card(card: str) -> str:
    return f"{RANK_LABELS.get(card[0], card[0])}{card[1]}"


def format_cards(cards) -> str:
    return " ".join(format_card(card) for card in cards) if cards else "—"


def _straight_high(values: list[int]) -> int:
    """Top card of the best straight in `values`, or 0. Returns 5 for A-2-3-4-5, which is
    the only hand where an ace plays low."""
    unique = sorted(set(values), reverse=True)
    for index in range(len(unique) - 4):
        window = unique[index : index + 5]
        if window[0] - window[4] == 4:
            return window[0]
    if {14, 5, 4, 3, 2} <= set(unique):
        return 5
    return 0


def evaluate_five(cards) -> tuple:
    """A comparable score for exactly five cards: (category, tiebreakers...).

    Bigger is better, and two scores are comparable with plain tuple comparison because
    equal categories always produce equal-length tails.
    """
    values = sorted((card_value(card) for card in cards), reverse=True)
    counts = Counter(values)
    # Count first, then value: pairs and trips have to outrank their own kickers.
    ordered = sorted(counts.items(), key=lambda item: (item[1], item[0]), reverse=True)
    shape = [count for _, count in ordered]
    by_count = tuple(value for value, _ in ordered)
    flush = len({card[1] for card in cards}) == 1
    straight = _straight_high(values)

    if flush and straight:
        return (8, straight)
    if shape[0] == 4:
        return (7,) + by_count
    if shape[:2] == [3, 2]:
        return (6,) + by_count
    if flush:
        return (5,) + tuple(values)
    if straight:
        return (4, straight)
    if shape[0] == 3:
        return (3,) + by_count
    if shape[:2] == [2, 2]:
        return (2,) + by_count
    if shape[0] == 2:
        return (1,) + by_count
    return (0,) + tuple(values)


def best_hand(cards) -> tuple[tuple, list[str]]:
    """(score, the five cards that made it) out of five to seven cards."""
    best_score, best_five = None, None
    for five in combinations(cards, 5):
        score = evaluate_five(five)
        if best_score is None or score > best_score:
            best_score, best_five = score, list(five)
    return best_score, best_five


def hand_name(score: tuple) -> str:
    return HAND_NAMES[score[0]]


# --- Стол -----------------------------------------------------------------------------


def open_table(chat_id: int, dealer_id, dealer_name: str) -> dict:
    """A fresh table in the lobby phase. Chips are dealt when the hand starts, not here,
    so a table nobody joins costs nothing to abandon."""
    return {
        "table_id": uuid.uuid4().hex[:8],
        "chat_id": int(chat_id),
        "dealer_id": str(dealer_id),
        "dealer_name": dealer_name,
        "phase": PHASE_LOBBY,
        "lobby_message_id": None,
        "players": [],
        "button": 0,
        "hand_no": 0,
        "hand": None,
        "opened_at": app_now().isoformat(),
    }


def seat(table: dict, user_id, name: str, username: str | None) -> str:
    """Put one member at the table. Returns why it did or did not happen: "seated",
    "already", "full", or "closed" -- the caller turns each into a toast."""
    if table.get("phase") != PHASE_LOBBY:
        return "closed"
    if find_player(table, user_id) is not None:
        return "already"
    if len(table["players"]) >= MAX_PLAYERS:
        return "full"
    table["players"].append({
        "user_id": str(user_id),
        "name": name,
        "username": (username or "").lstrip("@") or None,
        "stack": START_STACK,
    })
    return "seated"


def find_player(table: dict, user_id) -> dict | None:
    return next((p for p in table["players"] if p["user_id"] == str(user_id)), None)


def seat_index(table: dict, user_id) -> int:
    return next(
        (index for index, p in enumerate(table["players"]) if p["user_id"] == str(user_id)),
        -1,
    )


def player_label(player: dict) -> str:
    """@handle where there is one, display name otherwise. HTML-escaped: display names
    are user-controlled and every message this module renders is sent as HTML."""
    if player.get("username"):
        return f"@{escape(player['username'])}"
    return escape(player.get("name") or "Игрок")


def ensure_dealer_badge(entry: str):
    """Create the «Диллер» badge if it isn't there yet, so an administrator only has to
    give it rather than invent it.

    Deliberately exempt from stats.MAX_CUSTOM_BADGES, like the founder badge: a chat that
    had already filled its badge budget would otherwise have a /poker command nobody in it
    could ever use. Idempotent -- it runs on every startup.
    """
    data = stats._load_custom_badge_data(entry)
    if data["badges"].get(DEALER_BADGE_ID) is None:
        data["badges"][DEALER_BADGE_ID] = {
            "id": DEALER_BADGE_ID,
            "emoji": DEALER_BADGE_EMOJI,
            "name": DEALER_BADGE_NAME,
            "created_at": app_now().isoformat(),
            "created_by_id": "bot",
            "created_by_name": "ЕПХ-бот",
        }
        stats._save_custom_badge_data(entry, data)
        return True
    return False


def is_dealer(entry: str, user_id) -> bool:
    """Whether this member holds the «Диллер» badge in this chat.

    Matched by id OR by name: the id covers the badge the bot creates, the name covers one
    an administrator made by hand before it did.
    """
    try:
        badges = stats.custom_badges_for_user(entry, user_id)
    except Exception:
        return False
    return any(
        badge.badge_id == DEALER_BADGE_ID
        or (badge.name or "").strip().casefold() in DEALER_BADGE_NAMES
        for badge in badges
    )


def is_table_dealer(table: dict, user_id) -> bool:
    """Whether this is the member who opened THIS table. A second badge holder must not
    be able to start or end somebody else's game from the same keyboard."""
    return str(user_id) == str(table.get("dealer_id"))


# --- Раздача --------------------------------------------------------------------------


def start_hand(table: dict, rng=None) -> None:
    """Deal a new hand: post blinds, deal two cards each, and put the action on the seat
    left of the big blind.

    Players who ran out of chips are dropped from the table first -- a session ends by
    people busting, and an empty stack that stays seated would be asked to act on every
    street with nothing to bet.
    """
    table["players"] = [p for p in table["players"] if int(p.get("stack", 0)) > 0]
    if len(table["players"]) < MIN_PLAYERS:
        raise ValueError("Для раздачи нужны хотя бы двое игроков с фишками.")

    count = len(table["players"])
    if table["hand_no"]:
        table["button"] = (table["button"] + 1) % count
    else:
        table["button"] %= count
    table["hand_no"] += 1
    table["phase"] = PHASE_HAND

    deck = new_deck(rng)
    hand = {
        "street": "preflop",
        "deck": deck,
        "board": [],
        "hole": {},
        "committed": {p["user_id"]: 0 for p in table["players"]},
        "round": {p["user_id"]: 0 for p in table["players"]},
        "folded": [],
        "all_in": [],
        "acted": [],
        "to_act": 0,
        "message_id": None,
        "log": [],
        "result": None,
    }
    table["hand"] = hand
    for player in table["players"]:
        hand["hole"][player["user_id"]] = [deck.pop(), deck.pop()]

    # Blinds. Posting one is not "acting": the big blind still gets to raise when the
    # action comes back around, which is why neither seat goes into `acted` here.
    small = _seat_at(table, 1)
    big = _seat_at(table, 2)
    _commit(table, small, SMALL_BLIND)
    _commit(table, big, BIG_BLIND)
    hand["log"] = [
        f"{player_label(table['players'][small])} — малый блайнд {SMALL_BLIND}",
        f"{player_label(table['players'][big])} — большой блайнд {BIG_BLIND}",
    ]
    hand["to_act"] = _next_to_act(table, big + 1)


def _seat_at(table: dict, offset: int) -> int:
    """Seat `offset` places left of the button."""
    return (table["button"] + offset) % len(table["players"])


def _commit(table: dict, index: int, amount: int) -> int:
    """Move chips from a stack into the pot, capped at what the player actually has.
    Returns what was really committed -- a short blind is an all-in, not a debt."""
    hand = table["hand"]
    player = table["players"][index]
    paid = max(0, min(int(amount), int(player["stack"])))
    player["stack"] -= paid
    hand["committed"][player["user_id"]] += paid
    hand["round"][player["user_id"]] += paid
    if player["stack"] == 0 and player["user_id"] not in hand["all_in"]:
        hand["all_in"].append(player["user_id"])
    return paid


def pot(table: dict) -> int:
    hand = table.get("hand") or {}
    return sum(int(value) for value in (hand.get("committed") or {}).values())


def _highest_round(table: dict) -> int:
    hand = table["hand"]
    return max((int(value) for value in hand["round"].values()), default=0)


def to_call(table: dict, user_id) -> int:
    hand = table["hand"]
    return max(0, _highest_round(table) - int(hand["round"].get(str(user_id), 0)))


def _in_hand(table: dict) -> list[int]:
    """Seat indexes of everybody who has not folded."""
    hand = table["hand"]
    return [
        index for index, p in enumerate(table["players"])
        if p["user_id"] not in hand["folded"]
    ]


def _can_act(table: dict) -> list[int]:
    """Seat indexes of everybody who still has a decision to make -- not folded, not
    already all-in."""
    hand = table["hand"]
    return [index for index in _in_hand(table) if table["players"][index]["user_id"] not in hand["all_in"]]


def _next_to_act(table: dict, start: int) -> int:
    """First seat from `start` (inclusive, wrapping) that can still act, or -1."""
    count = len(table["players"])
    allowed = set(_can_act(table))
    for offset in range(count):
        index = (start + offset) % count
        if index in allowed:
            return index
    return -1


def current_player(table: dict) -> dict | None:
    hand = table.get("hand")
    if not hand or table.get("phase") != PHASE_HAND:
        return None
    index = hand.get("to_act", -1)
    if index is None or index < 0 or index >= len(table["players"]):
        return None
    return table["players"][index]


def legal_actions(table: dict) -> list[str]:
    """What the player to act may do right now, in button order.

    "bet" covers a raise as well: at fixed limit they are the same decision (put one more
    BET_STEP in), and two buttons for it would only be two ways to press the same thing.
    """
    player = current_player(table)
    if player is None:
        return []
    owed = to_call(table, player["user_id"])
    stack = int(player["stack"])
    actions = []
    if owed == 0:
        actions.append("check")
    elif stack > owed:
        actions.append("call")
    if stack > owed and stack > owed + BET_STEP:
        # A raise that would take the whole stack is offered as all-in instead, so the
        # two buttons never mean the same thing.
        actions.append("bet")
    if stack > 0:
        actions.append("allin")
    actions.append("fold")
    return actions


def act(table: dict, user_id, action: str) -> tuple[bool, str]:
    """Apply one action from one player. (False, reason) when it is not theirs to make --
    the caller shows the reason as a toast on that person's own screen and changes
    nothing, which is what makes a mis-tap harmless.
    """
    if table.get("phase") != PHASE_HAND or not table.get("hand"):
        return False, "Сейчас нет активной раздачи."
    player = current_player(table)
    if player is None:
        return False, "Сейчас никто не ходит."
    if player["user_id"] != str(user_id):
        return False, f"Сейчас ход: {plain_label(player)}."
    if find_player(table, user_id) is None:
        return False, "Ты не за этим столом."
    if action not in legal_actions(table):
        return False, "Так сейчас нельзя."

    hand = table["hand"]
    index = seat_index(table, user_id)
    owed = to_call(table, user_id)
    label = player_label(player)

    if action == "fold":
        hand["folded"].append(player["user_id"])
        hand["log"].append(f"{label} — пас")
    elif action == "check":
        hand["log"].append(f"{label} — чек")
    elif action == "call":
        paid = _commit(table, index, owed)
        hand["log"].append(f"{label} — колл {paid}")
    elif action == "bet":
        paid = _commit(table, index, owed + BET_STEP)
        hand["log"].append(
            f"{label} — {'рейз' if owed else 'ставка'} {paid}"
        )
        # A raise reopens the betting: everybody who had already acted has to answer it.
        hand["acted"] = []
    elif action == "allin":
        paid = _commit(table, index, int(player["stack"]))
        if paid > owed:
            hand["acted"] = []
        hand["log"].append(f"{label} — ва-банк {paid}")

    if player["user_id"] not in hand["acted"]:
        hand["acted"].append(player["user_id"])
    _advance(table)
    return True, ""


def _round_is_closed(table: dict) -> bool:
    """Everybody who could act has acted, and nobody is still owed a call."""
    hand = table["hand"]
    highest = _highest_round(table)
    for index in _can_act(table):
        user_id = table["players"][index]["user_id"]
        if user_id not in hand["acted"] or int(hand["round"][user_id]) < highest:
            return False
    return True


def _advance(table: dict) -> None:
    """Move the action along: next player, next street, or showdown."""
    hand = table["hand"]

    if len(_in_hand(table)) == 1:
        _finish(table)
        return
    if not _round_is_closed(table):
        hand["to_act"] = _next_to_act(table, hand["to_act"] + 1)
        if hand["to_act"] != -1:
            return
    _next_street(table)


def _next_street(table: dict) -> None:
    """Deal the next street, or run the board out and settle when nobody can act.

    Running it out matters: once everybody is all-in there are no more decisions, but the
    remaining cards still decide the hand, so they have to be dealt rather than skipped.
    """
    hand = table["hand"]
    while True:
        position = STREETS.index(hand["street"])
        if position == len(STREETS) - 1:
            _finish(table)
            return
        hand["street"] = STREETS[position + 1]
        for _ in range(STREET_CARDS[hand["street"]]):
            hand["board"].append(hand["deck"].pop())
        hand["round"] = {p["user_id"]: 0 for p in table["players"]}
        hand["acted"] = []
        hand["log"].append(f"— {STREET_NAMES[hand['street']]}: {format_cards(hand['board'])}")
        if len(_can_act(table)) > 1:
            hand["to_act"] = _next_to_act(table, table["button"] + 1)
            if hand["to_act"] != -1:
                return
        # Nobody left to bet: keep dealing until the river, then show the cards.


def _side_pots(table: dict) -> list[tuple[int, list[str]]]:
    """[(chips, eligible user ids)] -- the standard side-pot split.

    Every all-in caps what its owner can win at what they put in, so the pot is sliced at
    each distinct contribution level and each slice is contested only by the players who
    reached it. Folded players' chips stay in the slices they paid into: they are lost,
    not refunded.
    """
    hand = table["hand"]
    contributions = {user_id: int(amount) for user_id, amount in hand["committed"].items() if amount > 0}
    if not contributions:
        return []
    still_in = {table["players"][index]["user_id"] for index in _in_hand(table)}
    pots = []
    previous = 0
    for level in sorted(set(contributions.values())):
        chips = sum(min(amount, level) - min(amount, previous) for amount in contributions.values())
        eligible = [
            user_id for user_id, amount in contributions.items()
            if amount >= level and user_id in still_in
        ]
        if chips > 0 and eligible:
            pots.append((chips, eligible))
        elif chips > 0 and pots:
            # Nobody eligible (everybody at this level folded): fold the orphaned chips
            # into the previous slice rather than letting them vanish.
            chips_before, eligible_before = pots[-1]
            pots[-1] = (chips_before + chips, eligible_before)
        previous = level
    return pots


def _finish(table: dict) -> None:
    """Settle the hand: everybody folded to one player, or a showdown."""
    hand = table["hand"]
    table["phase"] = PHASE_SHOWDOWN
    survivors = _in_hand(table)

    scores = {}
    if len(survivors) > 1:
        for index in survivors:
            user_id = table["players"][index]["user_id"]
            score, five = best_hand(hand["hole"][user_id] + hand["board"])
            scores[user_id] = {"score": list(score), "five": five, "name": hand_name(score)}

    winnings = {}
    for chips, eligible in _side_pots(table):
        if len(survivors) == 1:
            best = [table["players"][survivors[0]]["user_id"]]
        else:
            ranked = sorted(eligible, key=lambda user_id: tuple(scores[user_id]["score"]), reverse=True)
            top = tuple(scores[ranked[0]]["score"])
            best = [user_id for user_id in ranked if tuple(scores[user_id]["score"]) == top]
        share, remainder = divmod(chips, len(best))
        # Odd chip goes to the first winner left of the button, as at a real table: -1 so
        # the seat after the button sorts first and the button itself sorts last.
        count = len(table["players"])
        order = sorted(best, key=lambda user_id: (seat_index(table, user_id) - table["button"] - 1) % count)
        for position, user_id in enumerate(order):
            winnings[user_id] = winnings.get(user_id, 0) + share + (1 if position < remainder else 0)

    for user_id, amount in winnings.items():
        player = find_player(table, user_id)
        if player is not None:
            player["stack"] = int(player["stack"]) + amount

    hand["to_act"] = -1
    hand["result"] = {
        "winnings": winnings,
        "scores": scores,
        "showdown": len(survivors) > 1,
        "pot": pot(table),
    }


def hand_is_over(table: dict) -> bool:
    return table.get("phase") == PHASE_SHOWDOWN


def players_with_chips(table: dict) -> int:
    return sum(1 for player in table["players"] if int(player.get("stack", 0)) > 0)


def session_standings(table: dict) -> list[dict]:
    """Everybody still holding chips, richest first -- the closing scoreboard."""
    return sorted(table["players"], key=lambda p: int(p.get("stack", 0)), reverse=True)


# --- Тексты ---------------------------------------------------------------------------


def plain_label(player: dict) -> str:
    """Same as player_label but without HTML escaping -- for callback toasts, which
    Telegram renders as plain text."""
    if player.get("username"):
        return f"@{player['username']}"
    return player.get("name") or "Игрок"


LOBBY_TITLE = "🃏 <b>Кто играет?</b>"
LOBBY_JOIN_BUTTON = "🎲 Я в игре"
LOBBY_START_BUTTON = "▶️ Начать игру"
END_BUTTON = "🛑 Завершить стол"
NEXT_HAND_BUTTON = "🔄 Следующая раздача"

ACTION_LABELS = {
    "check": "Чек",
    "call": "Колл",
    "bet": "Ставка",
    "allin": "Ва-банк",
    "fold": "Пас",
}


def format_lobby(table: dict) -> str:
    lines = [
        LOBBY_TITLE,
        "",
        f"Диллер: {escape(table.get('dealer_name') or '')}",
        f"Стартовый стек: {START_STACK} фишек. Блайнды {SMALL_BLIND}/{BIG_BLIND}.",
        "",
    ]
    if table["players"]:
        lines.append(f"<b>За столом ({len(table['players'])}/{MAX_PLAYERS}):</b>")
        lines.extend(
            f"{index}. {player_label(player)}"
            for index, player in enumerate(table["players"], start=1)
        )
    else:
        lines.append("<i>Пока никто не сел за стол.</i>")
    lines.append("")
    lines.append("Нажми кнопку, чтобы сесть за стол. Карты придут в личку от бота.")
    return "\n".join(lines)


def format_hand(table: dict) -> str:
    """The street message: board, pot, stacks and whose turn it is."""
    hand = table["hand"]
    lines = [
        f"🃏 <b>Раздача {table['hand_no']} — {STREET_NAMES[hand['street']]}</b>",
        "",
        f"Стол: {format_cards(hand['board'])}",
        f"Банк: <b>{pot(table)}</b>",
        "",
    ]
    for index, player in enumerate(table["players"]):
        marks = []
        if index == table["button"]:
            marks.append("Д")
        if player["user_id"] in hand["folded"]:
            marks.append("пас")
        elif player["user_id"] in hand["all_in"]:
            marks.append("ва-банк")
        committed = int(hand["round"].get(player["user_id"], 0))
        suffix = f" · в круге {committed}" if committed else ""
        marker = f" ({', '.join(marks)})" if marks else ""
        lines.append(f"{player_label(player)}{marker} — {int(player['stack'])} фишек{suffix}")

    lines.append("")
    recent = hand["log"][-4:]
    if recent:
        lines.extend(recent)
        lines.append("")

    player = current_player(table)
    if player is not None:
        owed = to_call(table, player["user_id"])
        turn = f"Ход: <b>{player_label(player)}</b>"
        lines.append(turn + (f" — до колла {owed}" if owed else ""))
    return "\n".join(lines)


def format_hole_cards(table: dict, user_id) -> str:
    """The private DM every player gets when the hand is dealt."""
    hand = table["hand"]
    cards = hand["hole"].get(str(user_id), [])
    player = find_player(table, user_id)
    return "\n".join([
        f"🃏 <b>Раздача {table['hand_no']}</b>",
        "",
        f"Твои карты: <b>{format_cards(cards)}</b>",
        f"Стек: {int(player['stack']) if player else 0} фишек.",
        "",
        "Ходы — кнопками под сообщением в общем чате.",
    ])


def format_showdown(table: dict) -> str:
    """What happened, who won and with what."""
    hand = table["hand"]
    result = hand.get("result") or {}
    winnings = result.get("winnings") or {}
    scores = result.get("scores") or {}
    lines = [
        f"🏆 <b>Раздача {table['hand_no']} — итог</b>",
        "",
        f"Стол: {format_cards(hand['board'])}",
        f"Банк: <b>{result.get('pot', 0)}</b>",
        "",
    ]
    if result.get("showdown"):
        lines.append("<b>Вскрытие:</b>")
        for index in _in_hand(table):
            player = table["players"][index]
            score = scores.get(player["user_id"]) or {}
            lines.append(
                f"{player_label(player)} — {format_cards(hand['hole'][player['user_id']])}"
                f" · {escape(score.get('name', ''))}"
            )
        lines.append("")

    for user_id, amount in sorted(winnings.items(), key=lambda item: item[1], reverse=True):
        player = find_player(table, user_id)
        if player is None:
            continue
        score = scores.get(user_id) or {}
        with_what = f" ({escape(score['name'])})" if score.get("name") else ""
        lines.append(f"Выигрыш: {player_label(player)} +{amount}{with_what}")

    lines.append("")
    lines.append("<b>Стеки:</b>")
    for player in session_standings(table):
        lines.append(f"{player_label(player)} — {int(player['stack'])}")
    return "\n".join(lines)


def format_session_over(table: dict, reason: str = "") -> str:
    lines = ["🃏 <b>Стол закрыт.</b>", ""]
    if reason:
        lines.extend([reason, ""])
    standings = session_standings(table)
    if standings:
        lines.append("<b>Итог сессии:</b>")
        lines.extend(
            f"{index}. {player_label(player)} — {int(player['stack'])} фишек"
            for index, player in enumerate(standings, start=1)
        )
    return "\n".join(lines)


# --- Кнопки ---------------------------------------------------------------------------


def callback_data(action: str, table_id: str, hand_no: int = 0, street: str = "") -> str:
    """`poker:<action>:<table>:<hand>:<street>` -- comfortably inside Telegram's 64-byte
    limit. The hand number and street travel with every action button so a press on a
    scrolled-back message from an earlier street can be recognised and refused instead of
    being applied to the current one."""
    return ":".join([CALLBACK_PREFIX, action, table_id, str(hand_no), street])


def parse_callback(data: str) -> tuple[str, str, int, str] | None:
    """(action, table_id, hand_no, street), or None when this isn't a poker button."""
    parts = (data or "").split(":")
    if len(parts) != 5 or parts[0] != CALLBACK_PREFIX:
        return None
    try:
        hand_no = int(parts[3])
    except ValueError:
        return None
    return parts[1], parts[2], hand_no, parts[4]


def lobby_keyboard(table: dict) -> dict:
    return {"inline_keyboard": [
        [{"text": LOBBY_JOIN_BUTTON, "callback_data": callback_data("join", table["table_id"])}],
        [{"text": LOBBY_START_BUTTON, "callback_data": callback_data("start", table["table_id"])}],
        [{"text": END_BUTTON, "callback_data": callback_data("end", table["table_id"])}],
    ]}


def action_keyboard(table: dict) -> dict:
    """The four actions, two per row, plus the dealer's way out.

    Everybody sees the same keyboard -- Telegram has no per-viewer buttons in a group --
    so the wrong person pressing is expected, and answered with a toast rather than
    prevented. The labels carry the amounts so nobody has to compute a call.
    """
    hand = table["hand"]
    player = current_player(table)
    rows, current = [], []
    if player is not None:
        owed = to_call(table, player["user_id"])
        for action in legal_actions(table):
            text = ACTION_LABELS[action]
            if action == "call":
                text = f"{text} {owed}"
            elif action == "bet":
                text = f"{text} {owed + BET_STEP}"
            elif action == "allin":
                text = f"{text} {int(player['stack'])}"
            current.append({
                "text": text,
                "callback_data": callback_data(
                    action, table["table_id"], table["hand_no"], hand["street"]
                ),
            })
            if len(current) == 2:
                rows.append(current)
                current = []
    if current:
        rows.append(current)
    rows.append([{
        "text": END_BUTTON,
        "callback_data": callback_data("end", table["table_id"], table["hand_no"], hand["street"]),
    }])
    return {"inline_keyboard": rows}


def showdown_keyboard(table: dict) -> dict:
    rows = []
    if players_with_chips(table) >= MIN_PLAYERS:
        rows.append([{
            "text": NEXT_HAND_BUTTON,
            "callback_data": callback_data("next", table["table_id"], table["hand_no"]),
        }])
    rows.append([{
        "text": END_BUTTON,
        "callback_data": callback_data("end", table["table_id"], table["hand_no"]),
    }])
    return {"inline_keyboard": rows}


def no_keyboard() -> dict:
    """What an old street's message is edited down to. An empty list is required here:
    omitting reply_markup leaves the previous keyboard in place, which would leave live
    buttons on a finished street."""
    return {"inline_keyboard": []}


# --- Хранилище ------------------------------------------------------------------------
#
# One table per chat, persisted so a redeploy in the middle of a hand does not silently
# eat everybody's chips. Same store, key scheme and atomic write as economy.py.

STORE_VERSION = 1


def _path(entry: str) -> Path:
    return stats._stats_dir() / f"{stats._cache_key(entry)}_poker.json"


def load_table(entry: str) -> dict | None:
    path = _path(entry)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    table = data.get("table") if isinstance(data, dict) else None
    if not isinstance(table, dict) or not table.get("table_id"):
        return None
    return table


def save_table(entry: str, table: dict) -> None:
    stats._write_json_atomic(_path(entry), {"version": STORE_VERSION, "table": table})


def clear_table(entry: str) -> None:
    try:
        _path(entry).unlink(missing_ok=True)
    except OSError:
        pass
