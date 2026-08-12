"""Small coin-only casino games, with server-owned outcomes and pending game state."""

from __future__ import annotations

from itertools import combinations
import random
from typing import Final

import economy


BET_AMOUNTS: Final = (1, 5, 10, 25)
POKER_BET_AMOUNTS: Final = (10, 25, 50, 100)
GAMES: Final = ("poker", "shell", "highlow")
_RANKS: Final = tuple(range(2, 15))
_SUITS: Final = ("♠", "♥", "♦", "♣")
_DECK: Final = tuple((rank, suit) for rank in _RANKS for suit in _SUITS)
_CARD_NAMES: Final = {11: "В", 12: "Д", 13: "К", 14: "Т"}

# Strongest first.  This single table drives both the result explanation and the
# in-game help page, so their terminology cannot drift apart.
POKER_COMBINATIONS: Final = (
    {
        "code": "royal_flush", "name": "Роял-флеш", "genitive": "роял-флеша",
        "description": "10, В, Д, К и Т одной масти.",
    },
    {
        "code": "straight_flush", "name": "Стрит-флеш", "genitive": "стрит-флеша",
        "description": "Пять карт подряд одной масти.",
    },
    {
        "code": "four_kind", "name": "Каре", "genitive": "каре",
        "description": "Четыре карты одного достоинства.",
    },
    {
        "code": "full_house", "name": "Фул-хаус", "genitive": "фул-хауса",
        "description": "Три одинаковые карты и пара.",
    },
    {
        "code": "flush", "name": "Флеш", "genitive": "флеша",
        "description": "Пять карт одной масти в любом порядке.",
    },
    {
        "code": "straight", "name": "Стрит", "genitive": "стрита",
        "description": "Пять карт подряд любых мастей.",
    },
    {
        "code": "three_kind", "name": "Сет", "genitive": "сета",
        "description": "Три карты одного достоинства.",
    },
    {
        "code": "two_pair", "name": "Две пары", "genitive": "двух пар",
        "description": "Две разные пары карт.",
    },
    {
        "code": "pair", "name": "Пара", "genitive": "пары",
        "description": "Две карты одного достоинства.",
    },
    {
        "code": "high_card", "name": "Старшая карта", "genitive": "старшей карты",
        "description": "Комбинации нет — сравниваются самые старшие карты.",
    },
)

_POKER_COMBINATION_BY_CODE: Final = {
    row["code"]: row for row in POKER_COMBINATIONS
}
_POKER_SCORE_CODES: Final = {
    8: "straight_flush", 7: "four_kind", 6: "full_house", 5: "flush",
    4: "straight", 3: "three_kind", 2: "two_pair", 1: "pair", 0: "high_card",
}


def valid_stake(value, game: str | None = None) -> int | None:
    try:
        stake = int(value)
    except (TypeError, ValueError):
        return None
    allowed = POKER_BET_AMOUNTS if game == "poker" else BET_AMOUNTS
    return stake if stake in allowed else None


def _card(card: tuple[int, str] | list) -> str:
    return f"{_CARD_NAMES.get(int(card[0]), str(card[0]))}{card[1]}"


def highlow_card_text(rank) -> str:
    """Render a rank without a suit for the higher/lower table."""
    try:
        value = int(rank)
    except (TypeError, ValueError):
        value = 7
    return str(_CARD_NAMES.get(value, value))


def draw_highlow_open_card(rng=None) -> int:
    """Deal a varied opening rank while leaving room on both sides of it."""
    rng = rng or random.SystemRandom()
    return rng.randint(3, 13)


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
    """Read a resumable poker game and refund a retired-game wager if one remains."""
    data = economy._load(entry)
    record = data.get("users", {}).get(str(user_id)) or {}
    active = _active_state(record)
    if not active or active.get("kind") == "poker":
        return active
    # «Коза» was removed while games could be persisted between button taps. Returning
    # the debited stake is the only safe migration: otherwise opening the new casino
    # would either strand the player forever or silently eat an old wager.
    try:
        stake = max(0, int(active.get("stake", 0) or 0))
    except (TypeError, ValueError):
        stake = 0
    if stake:
        record["bonus"] = record.get("bonus", 0) + stake
        economy._append_log(data, user_id, stake, f"wager_refund:casino:{active.get('kind', 'retired')}")
    economy._effects(record).setdefault("casino", {}).pop("active", None)
    economy._save(entry, data)
    return None


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
    stake = valid_stake(stake, "poker")
    if stake is None:
        return {"ok": False, "error": "invalid", "balance": economy.balance(entry, user_id, xp), "stake": 0}
    rng = rng or random.SystemRandom()
    cards = list(rng.sample(_DECK, 9))
    state = {
        "kind": "poker", "stake": stake, "base_stake": stake, "stage": 3,
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
        "base_stake": int(state.get("base_stake", state.get("stake", 0))),
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


def poker_combination(score: tuple[int, ...]) -> dict:
    """Player-facing metadata for a private evaluator score."""
    code = (
        "royal_flush"
        if int(score[0]) == 8 and len(score) > 1 and int(score[1]) == 14
        else _POKER_SCORE_CODES[int(score[0])]
    )
    row = _POKER_COMBINATION_BY_CODE[code]
    return {"code": code, "name": row["name"], "description": row["description"]}


def _poker_comparison(player_score: tuple[int, ...], dealer_score: tuple[int, ...]) -> str:
    """Explain not only who won, but which comparison decided the showdown."""
    player_row = _POKER_COMBINATION_BY_CODE[poker_combination(player_score)["code"]]
    dealer_row = _POKER_COMBINATION_BY_CODE[poker_combination(dealer_score)["code"]]
    if player_score == dealer_score:
        return (
            f"Ничья: у обоих {player_row['name'].lower()} и одинаковые решающие карты."
        )
    player_won = player_score > dealer_score
    if player_row["code"] != dealer_row["code"]:
        if player_won:
            return f"Ты победил: {player_row['name']} сильнее {dealer_row['genitive']}."
        return f"Ты проиграл: {player_row['name']} слабее {dealer_row['genitive']}."

    # Same kind of hand: Python's tuple comparison uses the first differing rank, which
    # is exactly poker's pair/set/kicker ordering. Show those two decisive ranks rather
    # than an opaque "dealer won".
    differing = next(
        index for index, (mine, theirs) in enumerate(zip(player_score, dealer_score))
        if mine != theirs
    )
    mine = _CARD_NAMES.get(int(player_score[differing]), str(player_score[differing]))
    theirs = _CARD_NAMES.get(int(dealer_score[differing]), str(dealer_score[differing]))
    if player_won:
        return (
            f"Ты победил: у обоих {player_row['name'].lower()}, но твоя решающая карта "
            f"старше — {mine} против {theirs}."
        )
    return (
        f"Ты проиграл: у обоих {player_row['name'].lower()}, но решающая карта дилера "
        f"старше — {theirs} против {mine}."
    )


def advance_poker(entry, user_id, xp: int, raise_by=0) -> dict:
    """Call or raise, then reveal the turn or settle the river atomically."""
    data = economy._load(entry)
    record = economy._record(data, user_id)
    state = _active_state(record)
    if not state or state.get("kind") != "poker":
        return {"ok": False, "error": "stale", "balance": economy._balance_from(data, user_id, xp), "stake": 0}
    base_stake = int(state.get("base_stake", state.get("stake", 0)) or 0)
    if raise_by in (None, "", 0, "0"):
        raise_by = 0
    else:
        try:
            raise_by = int(raise_by)
        except (TypeError, ValueError):
            raise_by = -1
        if raise_by != base_stake:
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
    if stage < 5:
        state["stage"] = stage + 1
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
        "player_combination": poker_combination(player_score),
        "dealer_combination": poker_combination(dealer_score),
        "comparison": _poker_comparison(player_score, dealer_score),
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


def play_highlow(entry, user_id, xp: int, stake, choice, open_card=7, rng=None) -> dict:
    """Deal around the shown rank with equal 5/11 odds for either direction."""
    stake = valid_stake(stake)
    choice = str(choice or "").lower()
    try:
        open_card = int(open_card)
    except (TypeError, ValueError):
        open_card = 0
    if stake is None or choice not in {"high", "low"} or open_card not in range(3, 14):
        return {"ok": False, "error": "invalid", "balance": economy.balance(entry, user_id, xp), "stake": stake or 0}
    rng = rng or random.SystemRandom()
    # Five lower outcomes, one tie and five higher outcomes keep both choices fair,
    # including when the visible rank is close to an edge.
    outcome = rng.randint(1, 11)
    if outcome <= 5:
        card = rng.randint(2, open_card - 1)
    elif outcome == 6:
        card = open_card
    else:
        card = rng.randint(open_card + 1, 14)
    won = card > open_card if choice == "high" else card < open_card
    return _settle(entry, user_id, xp, stake, won, {
        "game": "highlow", "choice": choice, "open_card": open_card, "card": card,
    })


__all__ = [
    "BET_AMOUNTS", "POKER_BET_AMOUNTS", "POKER_COMBINATIONS", "GAMES", "valid_stake", "active_game", "start_poker", "poker_snapshot", "poker_combination",
    "advance_poker", "play_shell", "play_highlow", "draw_highlow_open_card", "highlow_card_text",
]
