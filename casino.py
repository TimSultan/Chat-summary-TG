"""Small coin-only casino games.

Each function resolves a complete, server-side game and settles its wager through the
shared economy ledger.  Telegram buttons only describe a choice; neither a client nor a
screen render can decide the outcome or mint coins.
"""

from __future__ import annotations

import random
from typing import Final

import economy


BET_AMOUNTS: Final = (1, 5, 10, 25)
GAMES: Final = ("poker", "shell", "highlow")
_CARD_NAMES: Final = {11: "В", 12: "Д", 13: "К", 14: "Т"}


def valid_stake(value) -> int | None:
    try:
        stake = int(value)
    except (TypeError, ValueError):
        return None
    return stake if stake in BET_AMOUNTS else None


def _card(value: int) -> str:
    return _CARD_NAMES.get(value, str(value))


def _hand_score(cards: list[int]) -> tuple[int, ...]:
    ordered = sorted(cards, reverse=True)
    pairs = [card for card in set(cards) if cards.count(card) == 2]
    if pairs:
        pair = pairs[0]
        kicker = next(card for card in ordered if card != pair)
        return 2, pair, kicker
    return 1, *ordered


def _settle(entry, user_id, xp: int, stake: int, won: bool, details: dict, *, draw: bool = False) -> dict:
    ok, balance = economy.settle_wager(entry, user_id, xp, stake, won, "casino", draw=draw)
    if not ok:
        return {"ok": False, "balance": balance, "stake": stake, **details}
    outcome = "win" if won else "draw" if draw else "loss"
    return {
        "ok": True, "game": details.pop("game"), "stake": stake, "won": won,
        "draw": draw, "outcome": outcome, "payout": stake * 2 if won else stake if draw else 0,
        "balance": balance, **details,
    }


def play_poker(entry, user_id, xp: int, stake, rng=None) -> dict:
    """Three-card poker: a pair beats high cards; equal hands refund the stake."""
    stake = valid_stake(stake)
    if stake is None:
        return {"ok": False, "balance": economy.balance(entry, user_id, xp), "stake": 0}
    rng = rng or random.SystemRandom()
    cards = list(rng.sample(range(2, 15), 6))
    mine, dealer = cards[:3], cards[3:]
    mine_score, dealer_score = _hand_score(mine), _hand_score(dealer)
    return _settle(
        entry, user_id, xp, stake, mine_score > dealer_score,
        {"game": "poker", "player_cards": [_card(card) for card in mine],
         "dealer_cards": [_card(card) for card in dealer]},
        draw=mine_score == dealer_score,
    )


def play_shell(entry, user_id, xp: int, stake, choice, rng=None) -> dict:
    """Pick one of three cups. The ball is chosen after the server receives the tap."""
    stake = valid_stake(stake)
    try:
        choice = int(choice)
    except (TypeError, ValueError):
        choice = 0
    if stake is None or choice not in (1, 2, 3):
        return {"ok": False, "balance": economy.balance(entry, user_id, xp), "stake": stake or 0}
    rng = rng or random.SystemRandom()
    ball = rng.randint(1, 3)
    return _settle(entry, user_id, xp, stake, choice == ball, {
        "game": "shell", "choice": choice, "ball": ball,
    })


def play_highlow(entry, user_id, xp: int, stake, choice, rng=None) -> dict:
    """The visible card is seven; a matching next card loses, so each choice wins 5/11."""
    stake = valid_stake(stake)
    choice = str(choice or "").lower()
    if stake is None or choice not in {"high", "low"}:
        return {"ok": False, "balance": economy.balance(entry, user_id, xp), "stake": stake or 0}
    rng = rng or random.SystemRandom()
    card = rng.randint(2, 12)
    won = card > 7 if choice == "high" else card < 7
    return _settle(entry, user_id, xp, stake, won, {
        "game": "highlow", "choice": choice, "card": card,
    })


__all__ = ["BET_AMOUNTS", "GAMES", "valid_stake", "play_poker", "play_shell", "play_highlow"]
