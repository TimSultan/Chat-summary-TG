"""Small coin-only casino games, with server-owned outcomes and pending game state."""

from __future__ import annotations

from itertools import combinations
import random
from typing import Final

import economy


BET_AMOUNTS: Final = (1, 5, 10, 25)
POKER_BET_AMOUNTS: Final = (10, 25, 50, 100)
POKER_MODES: Final = ("classic", "opponent")
GAMES: Final = ("poker", "poker_ai", "shell", "highlow")
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

# The conventional two-axis poker model: tight/loose controls which hands continue,
# aggressive/passive controls how often that continuing range raises.  Numeric knobs are
# intentionally kept in this one editable table.  ``fold_below`` and ``raise_above`` are
# estimated showdown-equity thresholds; the other values keep a style from becoming a
# perfectly readable robot.
POKER_STYLES: Final = (
    {
        "code": "tight_aggressive", "name": "Тайтово-агрессивный",
        "short": "Играет мало рук, но сильные разыгрывает напористо.",
        "fold_below": .43, "raise_above": .64, "bluff_chance": .08,
        "value_raise_chance": .78, "reraise_chance": .55,
    },
    {
        "code": "loose_aggressive", "name": "Лузово-агрессивный",
        "short": "Заходит широко, часто давит рейзами и иногда блефует.",
        "fold_below": .24, "raise_above": .50, "bluff_chance": .20,
        "value_raise_chance": .88, "reraise_chance": .72,
    },
    {
        "code": "tight_passive", "name": "Тайтово-пассивный",
        "short": "Слабые руки выбрасывает, со средними осторожно коллирует.",
        "fold_below": .50, "raise_above": .78, "bluff_chance": .01,
        "value_raise_chance": .34, "reraise_chance": .12,
    },
    {
        "code": "loose_passive", "name": "Лузово-пассивный",
        "short": "Часто остаётся в раздаче и любит колл больше рейза.",
        "fold_below": .18, "raise_above": .82, "bluff_chance": .02,
        "value_raise_chance": .24, "reraise_chance": .08,
    },
)
_POKER_STYLE_BY_CODE: Final = {row["code"]: row for row in POKER_STYLES}


def valid_stake(value, game: str | None = None) -> int | None:
    try:
        stake = int(value)
    except (TypeError, ValueError):
        return None
    allowed = POKER_BET_AMOUNTS if game in {"poker", "poker_ai"} else BET_AMOUNTS
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
    data: dict | None = None, record: dict | None = None, payout_override: int | None = None,
) -> dict:
    """Pay a wager already debited by `_start`, and clear its saved state atomically."""
    data = data if data is not None else economy._load(entry)
    record = record if record is not None else economy._record(data, user_id)
    active = _active_state(record)
    if active != state:
        return {"ok": False, "error": "stale", "balance": economy._balance_from(data, user_id, xp), "stake": 0}
    stake = int(state["stake"])
    payout = (
        max(0, int(payout_override)) if payout_override is not None
        else stake * (2 if won else 1 if draw else 0)
    )
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


def start_poker(entry, user_id, xp: int, stake, rng=None, *, mode: str = "classic") -> dict:
    """Open Hold'em at the flop in classic or behavioural-opponent mode."""
    stake = valid_stake(stake, "poker")
    if stake is None:
        return {"ok": False, "error": "invalid", "balance": economy.balance(entry, user_id, xp), "stake": 0}
    mode = mode if mode in POKER_MODES else "classic"
    rng = rng or random.SystemRandom()
    cards = list(rng.sample(_DECK, 9))
    state = {
        "kind": "poker", "stake": stake, "base_stake": stake, "stage": 3,
        "mode": mode, "dealer_stake": stake, "to_call": 0,
        "player": [list(card) for card in cards[:2]],
        "dealer": [list(card) for card in cards[2:4]],
        "board": [list(card) for card in cards[4:]],
    }
    if mode == "opponent":
        style = rng.choice(POKER_STYLES)
        state.update({
            "opponent_style": style["code"],
            "opponent_seed": rng.randint(0, 2_147_483_647),
            "opponent_decisions": 0,
            "last_action": "Соперник изучает стол.",
        })
    return _start(entry, user_id, xp, stake, state)


def poker_snapshot(state: dict) -> dict:
    """Presentation-safe state: opponent hole cards stay hidden until showdown."""
    stage = min(5, max(3, int(state.get("stage", 3))))
    return {
        "stage": stage,
        "stake": int(state.get("stake", 0)),
        "dealer_stake": int(state.get("dealer_stake", state.get("stake", 0))),
        "pot": int(state.get("stake", 0)) + int(state.get("dealer_stake", state.get("stake", 0))),
        "base_stake": int(state.get("base_stake", state.get("stake", 0))),
        "mode": state.get("mode") if state.get("mode") in POKER_MODES else "classic",
        "to_call": max(0, int(state.get("to_call", 0) or 0)),
        "last_action": str(state.get("last_action") or ""),
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


def poker_style(code: str) -> dict | None:
    """Public text for a behavioural opponent style; numeric tuning stays private."""
    row = _POKER_STYLE_BY_CODE.get(str(code or ""))
    return {key: row[key] for key in ("code", "name", "short")} if row else None


def _set_active(record: dict, state: dict) -> None:
    economy._effects(record).setdefault("casino", {})["active"] = state


def _poker_result_details(state: dict) -> dict:
    player_score = _best_score(state["player"] + state["board"])
    dealer_score = _best_score(state["dealer"] + state["board"])
    comparison = _poker_comparison(player_score, dealer_score)
    if state.get("mode") == "opponent":
        comparison = comparison.replace("дилера", "соперника")
    return {
        "mode": state.get("mode", "classic"),
        "opponent_style": poker_style(state.get("opponent_style")),
        "player_cards": [_card(card) for card in state["player"]],
        "dealer_cards": [_card(card) for card in state["dealer"]],
        "board_cards": [_card(card) for card in state["board"]],
        "player_combination": poker_combination(player_score),
        "dealer_combination": poker_combination(dealer_score),
        "comparison": comparison,
    }


def _showdown_poker(entry, user_id, xp: int, state: dict, data: dict, record: dict) -> dict:
    player_score = _best_score(state["player"] + state["board"])
    dealer_score = _best_score(state["dealer"] + state["board"])
    _set_active(record, state)
    return _finish_active(
        entry, user_id, xp, state, player_score > dealer_score,
        _poker_result_details(state), draw=player_score == dealer_score,
        data=data, record=record,
        payout_override=(
            int(state["stake"]) + int(state.get("dealer_stake", state["stake"]))
            if player_score > dealer_score else int(state["stake"]) if player_score == dealer_score else 0
        ),
    )


def _fold_poker(
    entry, user_id, xp: int, state: dict, data: dict, record: dict, folded_by: str,
) -> dict:
    """Settle without exposing the opponent's folded cards."""
    dealer_folded = folded_by == "dealer"
    stage = min(5, max(3, int(state.get("stage", 3))))
    _set_active(record, state)
    return _finish_active(
        entry, user_id, xp, state, dealer_folded,
        {
            "mode": state.get("mode", "classic"),
            "folded_by": folded_by,
            "opponent_style": poker_style(state.get("opponent_style")),
            "player_cards": [_card(card) for card in state.get("player", [])],
            "dealer_cards": [],
            "board_cards": [_card(card) for card in (state.get("board") or [])[:stage]],
            "comparison": (
                "Соперник сбросил карты — банк твой."
                if dealer_folded else "Ты сбросил карты и уступил банк."
            ),
        },
        data=data, record=record,
        payout_override=(
            int(state["stake"]) + int(state.get("dealer_stake", state["stake"]))
            if dealer_folded else 0
        ),
    )


def _opponent_rng(state: dict) -> random.Random:
    decision = max(0, int(state.get("opponent_decisions", 0) or 0))
    state["opponent_decisions"] = decision + 1
    return random.Random(
        f"poker-opponent:{state.get('opponent_seed', 0)}:{state.get('stage', 3)}:{decision}"
    )


def _opponent_equity(state: dict, rng: random.Random, trials: int = 72) -> float:
    """Estimate strength using only cards the opponent is allowed to know.

    The actual player's hole cards and unrevealed board are deliberately NOT excluded
    from the sampling pool: from the opponent's point of view they are unknown cards.
    This is the guard that keeps a server-side bot from quietly becoming a cheater.
    """
    stage = min(5, max(3, int(state.get("stage", 3))))
    dealer = [tuple(card) for card in state.get("dealer", [])]
    visible = [tuple(card) for card in (state.get("board") or [])[:stage]]
    known = set(dealer + visible)
    unseen = [card for card in _DECK if card not in known]
    board_needed = 5 - len(visible)
    wins = 0.0
    trials = min(160, max(24, int(trials or 72)))
    for _ in range(trials):
        drawn = rng.sample(unseen, 2 + board_needed)
        opponent_hole = drawn[:2]
        board = visible + drawn[2:]
        mine = _best_score(dealer + board)
        theirs = _best_score(opponent_hole + board)
        wins += 1.0 if mine > theirs else .5 if mine == theirs else 0.0
    return wins / trials


def _opponent_decision(state: dict, *, faced_raise: bool, can_reraise: bool = True) -> str:
    """Return fold/call/check/raise from the selected style and estimated equity."""
    style = _POKER_STYLE_BY_CODE.get(str(state.get("opponent_style") or "")) \
        or _POKER_STYLE_BY_CODE["tight_aggressive"]
    rng = _opponent_rng(state)
    equity = _opponent_equity(state, rng)
    perceived = max(0.0, min(1.0, equity + rng.uniform(-.045, .045)))
    if faced_raise and perceived < float(style["fold_below"]):
        return "fold"
    if can_reraise:
        value_raise = (
            perceived >= float(style["raise_above"])
            and rng.random() < float(style["value_raise_chance"])
        )
        bluff_raise = perceived < .50 and rng.random() < float(style["bluff_chance"])
        if value_raise or bluff_raise:
            if not faced_raise or rng.random() < float(style["reraise_chance"]):
                return "raise"
    return "call" if faced_raise else "check"


def _poker_charge(data: dict, record: dict, user_id, xp: int, state: dict, amount: int) -> dict | None:
    amount = max(0, int(amount or 0))
    balance = economy._balance_from(data, user_id, xp)
    if balance < amount:
        return {
            "ok": False, "error": "funds", "active": state,
            "balance": balance, "stake": amount,
        }
    if amount:
        record["spent"] = record.get("spent", 0) + amount
        economy._append_log(data, user_id, -amount, "wager_raise:casino:poker")
        state["stake"] = int(state["stake"]) + amount
    return None


def _next_poker_street(entry, user_id, xp: int, state: dict, data: dict, record: dict) -> dict:
    stage = int(state.get("stage", 3))
    state["to_call"] = 0
    if stage < 5:
        state["stage"] = stage + 1
        _set_active(record, state)
        economy._save(entry, data)
        return {"ok": True, "active": dict(state), "balance": economy._balance_from(data, user_id, xp)}
    return _showdown_poker(entry, user_id, xp, state, data, record)


def _advance_classic_poker(
    entry, user_id, xp: int, state: dict, data: dict, record: dict, raise_by: int,
) -> dict:
    if raise_by:
        refused = _poker_charge(data, record, user_id, xp, state, raise_by)
        if refused:
            return refused
        state["dealer_stake"] = int(state.get("dealer_stake", state["stake"] - raise_by)) + raise_by
    return _next_poker_street(entry, user_id, xp, state, data, record)


def _advance_opponent_poker(
    entry, user_id, xp: int, state: dict, data: dict, record: dict, raise_by: int,
) -> dict:
    base = int(state.get("base_stake", state.get("stake", 0)) or 0)
    to_call = max(0, int(state.get("to_call", 0) or 0))
    if raise_by:
        # A re-raise first calls the outstanding bet, then adds exactly the original
        # buy-in.  One bot re-raise per street caps the exchange and prevents loops.
        refused = _poker_charge(data, record, user_id, xp, state, to_call + base)
        if refused:
            return refused
        state["to_call"] = 0
        decision = _opponent_decision(state, faced_raise=True, can_reraise=not bool(to_call))
        if decision == "fold":
            state["last_action"] = "Соперник сбросил карты после твоего рейза."
            return _fold_poker(entry, user_id, xp, state, data, record, "dealer")
        if decision == "raise" and not to_call:
            state["dealer_stake"] = int(state.get("dealer_stake", 0)) + base * 2
            state["to_call"] = base
            state["last_action"] = f"Соперник переставил ещё на {base}."
            _set_active(record, state)
            economy._save(entry, data)
            return {"ok": True, "active": dict(state), "balance": economy._balance_from(data, user_id, xp)}
        state["dealer_stake"] = int(state.get("dealer_stake", 0)) + base
        state["last_action"] = "Соперник поддержал твой рейз."
        return _next_poker_street(entry, user_id, xp, state, data, record)

    if to_call:
        refused = _poker_charge(data, record, user_id, xp, state, to_call)
        if refused:
            return refused
        state["last_action"] = "Ты поддержал рейз соперника."
        return _next_poker_street(entry, user_id, xp, state, data, record)

    decision = _opponent_decision(state, faced_raise=False)
    if decision == "raise":
        state["dealer_stake"] = int(state.get("dealer_stake", state["stake"])) + base
        state["to_call"] = base
        state["last_action"] = f"После твоего чека соперник поднял на {base}."
        _set_active(record, state)
        economy._save(entry, data)
        return {"ok": True, "active": dict(state), "balance": economy._balance_from(data, user_id, xp)}
    state["last_action"] = "Соперник тоже сделал чек."
    return _next_poker_street(entry, user_id, xp, state, data, record)


def advance_poker(entry, user_id, xp: int, raise_by=0) -> dict:
    """Fold, check/call or raise, then let the selected opponent answer."""
    data = economy._load(entry)
    record = economy._record(data, user_id)
    state = _active_state(record)
    if not state or state.get("kind") != "poker":
        return {"ok": False, "error": "stale", "balance": economy._balance_from(data, user_id, xp), "stake": 0}
    if str(raise_by or "").strip().lower() == "fold":
        return _fold_poker(entry, user_id, xp, state, data, record, "player")
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
    mode = state.get("mode") if state.get("mode") in POKER_MODES else "classic"
    if mode == "opponent":
        return _advance_opponent_poker(entry, user_id, xp, state, data, record, raise_by)
    return _advance_classic_poker(entry, user_id, xp, state, data, record, raise_by)


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
    "BET_AMOUNTS", "POKER_BET_AMOUNTS", "POKER_COMBINATIONS", "POKER_STYLES",
    "POKER_MODES", "GAMES", "valid_stake", "active_game", "start_poker",
    "poker_snapshot", "poker_combination", "poker_style",
    "advance_poker", "play_shell", "play_highlow", "draw_highlow_open_card", "highlow_card_text",
]
