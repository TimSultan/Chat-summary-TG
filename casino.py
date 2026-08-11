"""Small coin-only casino games, with server-owned outcomes and pending game state."""

from __future__ import annotations

from itertools import combinations
import random
from typing import Final

import economy


BET_AMOUNTS: Final = (1, 5, 10, 25)
GAMES: Final = ("poker", "shell", "highlow", "goat")
_RANKS: Final = tuple(range(2, 15))
_SUITS: Final = ("♠", "♥", "♦", "♣")
_DECK: Final = tuple((rank, suit) for rank in _RANKS for suit in _SUITS)
_CARD_NAMES: Final = {11: "В", 12: "Д", 13: "К", 14: "Т"}


def valid_stake(value) -> int | None:
    try:
        stake = int(value)
    except (TypeError, ValueError):
        return None
    return stake if stake in BET_AMOUNTS else None


def _card(card: tuple[int, str] | list) -> str:
    return f"{_CARD_NAMES.get(int(card[0]), str(card[0]))}{card[1]}"


def _active_state(record: dict) -> dict | None:
    state = economy._effects(record).get("casino", {}).get("active")
    return dict(state) if isinstance(state, dict) else None


def _record_winnings(record: dict, stake: int, payout: int) -> None:
    """Count only profit, never the player's returned stake, towards «Азартный»."""
    profit = max(0, int(payout) - int(stake))
    if profit:
        casino = economy._effects(record).setdefault("casino", {})
        casino["winnings"] = max(0, int(casino.get("winnings", 0) or 0)) + profit


def active_game(entry, user_id) -> dict | None:
    """Read a resumable poker/goat game without changing coins or its stage."""
    data = economy._load(entry)
    record = data.get("users", {}).get(str(user_id)) or {}
    return _active_state(record)


def _start(entry, user_id, xp: int, stake: int, state: dict) -> dict:
    data = economy._load(entry)
    record = economy._record(data, user_id)
    active = _active_state(record)
    if active:
        return {"ok": False, "error": "active", "active": active,
                "balance": economy._balance_from(data, user_id, xp), "stake": stake}
    balance = economy._balance_from(data, user_id, xp)
    if balance < stake:
        return {"ok": False, "error": "funds", "balance": balance, "stake": stake}
    record["spent"] = record.get("spent", 0) + stake
    economy._append_log(data, user_id, -stake, f"wager:casino:{state['kind']}")
    economy._effects(record).setdefault("casino", {})["active"] = state
    economy._save(entry, data)
    return {"ok": True, "active": dict(state), "balance": balance - stake, "stake": stake}


def _finish_active(
    entry, user_id, xp: int, state: dict, won: bool, details: dict, *, draw: bool = False,
    data: dict | None = None, record: dict | None = None,
) -> dict:
    """Pay a wager already debited by `_start`, and clear its saved state atomically."""
    data = data if data is not None else economy._load(entry)
    record = record if record is not None else economy._record(data, user_id)
    active = _active_state(record)
    if active != state:
        return {"ok": False, "error": "stale", "balance": economy._balance_from(data, user_id, xp), "stake": 0}
    stake = int(state["stake"])
    payout = stake * (2 if won else 1 if draw else 0)
    if payout:
        record["bonus"] = record.get("bonus", 0) + payout
        economy._append_log(data, user_id, payout, f"wager_payout:casino:{state['kind']}")
        _record_winnings(record, stake, payout)
    economy._effects(record).setdefault("casino", {}).pop("active", None)
    economy._save(entry, data)
    return {
        "ok": True, "game": state["kind"], "stake": stake, "won": won, "draw": draw,
        "payout": payout, "balance": economy._balance_from(data, user_id, xp), **details,
    }


def _settle(entry, user_id, xp: int, stake: int, won: bool, details: dict, *, multiplier: int = 2) -> dict:
    """Resolve a one-tap game through the shared atomic wager helper."""
    if stake <= 0:
        return {"ok": False, "balance": economy.balance(entry, user_id, xp), "stake": 0}
    data = economy._load(entry)
    balance = economy._balance_from(data, user_id, xp)
    if balance < stake:
        return {"ok": False, "error": "funds", "balance": balance, "stake": stake}
    record = economy._record(data, user_id)
    record["spent"] = record.get("spent", 0) + stake
    economy._append_log(data, user_id, -stake, f"wager:casino:{details['game']}")
    payout = stake * multiplier if won else 0
    if payout:
        record["bonus"] = record.get("bonus", 0) + payout
        economy._append_log(data, user_id, payout, f"wager_payout:casino:{details['game']}")
        _record_winnings(record, stake, payout)
    economy._save(entry, data)
    return {
        "ok": True, "game": details.pop("game"), "stake": stake, "won": won,
        "draw": False, "payout": payout, "balance": economy._balance_from(data, user_id, xp),
        **details,
    }


def start_poker(entry, user_id, xp: int, stake, rng=None) -> dict:
    """Open a five-card Texas Hold'em hand at the flop (three community cards)."""
    stake = valid_stake(stake)
    if stake is None:
        return {"ok": False, "error": "invalid", "balance": economy.balance(entry, user_id, xp), "stake": 0}
    rng = rng or random.SystemRandom()
    cards = list(rng.sample(_DECK, 9))
    state = {
        "kind": "poker", "stake": stake, "stage": 3,
        "player": [list(card) for card in cards[:2]],
        "dealer": [list(card) for card in cards[2:4]],
        "board": [list(card) for card in cards[4:]],
    }
    return _start(entry, user_id, xp, stake, state)


def poker_snapshot(state: dict) -> dict:
    """Presentation-safe state: opponent hole cards stay hidden until showdown."""
    stage = min(5, max(3, int(state.get("stage", 3))))
    return {
        "stage": stage,
        "stake": int(state.get("stake", 0)),
        "player_cards": [_card(card) for card in state.get("player", [])],
        "board_cards": [_card(card) for card in (state.get("board") or [])[:stage]],
    }


def _five_score(cards) -> tuple[int, ...]:
    ranks = sorted((int(card[0]) for card in cards), reverse=True)
    counts = {rank: ranks.count(rank) for rank in set(ranks)}
    ordered_groups = sorted(((count, rank) for rank, count in counts.items()), reverse=True)
    unique = sorted(set(ranks), reverse=True)
    straight_high = 5 if set(ranks) == {14, 2, 3, 4, 5} else (unique[0] if len(unique) == 5 and unique[0] - unique[-1] == 4 else 0)
    flush = len({card[1] for card in cards}) == 1
    if flush and straight_high:
        return 8, straight_high
    if ordered_groups[0][0] == 4:
        quad = ordered_groups[0][1]
        return 7, quad, next(rank for rank in ranks if rank != quad)
    if ordered_groups[0][0] == 3 and ordered_groups[1][0] == 2:
        return 6, ordered_groups[0][1], ordered_groups[1][1]
    if flush:
        return 5, *ranks
    if straight_high:
        return 4, straight_high
    if ordered_groups[0][0] == 3:
        triple = ordered_groups[0][1]
        return 3, triple, *(rank for rank in ranks if rank != triple)
    if ordered_groups[0][0] == 2 and ordered_groups[1][0] == 2:
        pairs = sorted((ordered_groups[0][1], ordered_groups[1][1]), reverse=True)
        return 2, *pairs, next(rank for rank in ranks if rank not in pairs)
    if ordered_groups[0][0] == 2:
        pair = ordered_groups[0][1]
        return 1, pair, *(rank for rank in ranks if rank != pair)
    return 0, *ranks


def _best_score(cards) -> tuple[int, ...]:
    return max(_five_score(hand) for hand in combinations(cards, 5))


def advance_poker(entry, user_id, xp: int, raise_by=0) -> dict:
    """Call or raise, then reveal the turn or settle the river atomically."""
    data = economy._load(entry)
    record = economy._record(data, user_id)
    state = _active_state(record)
    if not state or state.get("kind") != "poker":
        return {"ok": False, "error": "stale", "balance": economy._balance_from(data, user_id, xp), "stake": 0}
    if raise_by in (None, "", 0, "0"):
        raise_by = 0
    else:
        raise_by = valid_stake(raise_by)
        if raise_by is None:
            return {
                "ok": False, "error": "invalid", "active": state,
                "balance": economy._balance_from(data, user_id, xp), "stake": 0,
            }
    if raise_by:
        balance = economy._balance_from(data, user_id, xp)
        if balance < raise_by:
            return {
                "ok": False, "error": "funds", "active": state,
                "balance": balance, "stake": raise_by,
            }
        record["spent"] = record.get("spent", 0) + raise_by
        economy._append_log(data, user_id, -raise_by, "wager_raise:casino:poker")
        state["stake"] = int(state["stake"]) + raise_by
        economy._effects(record).setdefault("casino", {})["active"] = state
    stage = int(state.get("stage", 3))
    if stage < 4:
        state["stage"] = 4
        economy._effects(record).setdefault("casino", {})["active"] = state
        economy._save(entry, data)
        return {"ok": True, "active": dict(state), "balance": economy._balance_from(data, user_id, xp)}
    player = state["player"] + state["board"]
    dealer = state["dealer"] + state["board"]
    player_score, dealer_score = _best_score(player), _best_score(dealer)
    return _finish_active(entry, user_id, xp, state, player_score > dealer_score, {
        "player_cards": [_card(card) for card in state["player"]],
        "dealer_cards": [_card(card) for card in state["dealer"]],
        "board_cards": [_card(card) for card in state["board"]],
    }, draw=player_score == dealer_score, data=data, record=record)


def play_shell(entry, user_id, xp: int, stake, choice, rng=None) -> dict:
    """Pick one of three cups. A 1/3 win returns x3, not the ordinary x2."""
    stake = valid_stake(stake)
    try:
        choice = int(choice)
    except (TypeError, ValueError):
        choice = 0
    if stake is None or choice not in (1, 2, 3):
        return {"ok": False, "error": "invalid", "balance": economy.balance(entry, user_id, xp), "stake": stake or 0}
    rng = rng or random.SystemRandom()
    ball = rng.randint(1, 3)
    return _settle(entry, user_id, xp, stake, choice == ball, {
        "game": "shell", "choice": choice, "ball": ball,
    }, multiplier=3)


def play_highlow(entry, user_id, xp: int, stake, choice, rng=None) -> dict:
    """The visible card is seven; a matching next card loses, so each choice wins 5/11."""
    stake = valid_stake(stake)
    choice = str(choice or "").lower()
    if stake is None or choice not in {"high", "low"}:
        return {"ok": False, "error": "invalid", "balance": economy.balance(entry, user_id, xp), "stake": stake or 0}
    rng = rng or random.SystemRandom()
    card = rng.randint(2, 12)
    won = card > 7 if choice == "high" else card < 7
    return _settle(entry, user_id, xp, stake, won, {
        "game": "highlow", "choice": choice, "card": card,
    })


def choose_goat_door(entry, user_id, xp: int, stake, choice, rng=None) -> dict:
    """Pick one door, then save the prize and an opened goat door for the final choice."""
    stake = valid_stake(stake)
    try:
        choice = int(choice)
    except (TypeError, ValueError):
        choice = 0
    if stake is None or choice not in (1, 2, 3):
        return {"ok": False, "error": "invalid", "balance": economy.balance(entry, user_id, xp), "stake": stake or 0}
    rng = rng or random.SystemRandom()
    prize = rng.randint(1, 3)
    opened = rng.choice([door for door in (1, 2, 3) if door not in {choice, prize}])
    state = {"kind": "goat", "stake": stake, "choice": choice, "prize": prize, "opened": opened}
    return _start(entry, user_id, xp, stake, state)


def finish_goat(entry, user_id, xp: int, decision: str) -> dict:
    """Settle the Monty Hall choice: keep the first door or switch to the last closed one."""
    data = economy._load(entry)
    record = economy._record(data, user_id)
    state = _active_state(record)
    if not state or state.get("kind") != "goat":
        return {"ok": False, "error": "stale", "balance": economy._balance_from(data, user_id, xp), "stake": 0}
    decision = str(decision or "").lower()
    if decision not in {"keep", "switch"}:
        return {"ok": False, "error": "invalid", "balance": economy._balance_from(data, user_id, xp), "stake": int(state["stake"])}
    first, opened = int(state["choice"]), int(state["opened"])
    final = first if decision == "keep" else next(door for door in (1, 2, 3) if door not in {first, opened})
    return _finish_active(entry, user_id, xp, state, final == int(state["prize"]), {
        "choice": first, "opened": opened, "prize": int(state["prize"]), "final": final, "decision": decision,
    })


__all__ = [
    "BET_AMOUNTS", "GAMES", "valid_stake", "active_game", "start_poker", "poker_snapshot",
    "advance_poker", "play_shell", "play_highlow", "choose_goat_door", "finish_goat",
]
