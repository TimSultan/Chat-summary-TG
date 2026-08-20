"""Поляна: the ticket-gated diamond lotto that sits under the farm and the quarry.

This module owns the *rules and the board*, not the store. It is deliberately pure and
seedable so a round is reproducible from what is written down about it -- the same
property `pets_combat.simulate` has, and for the same reason: a prize a player disputes
has to be checkable after the fact, not re-rolled.

The board is generated ONCE, when the round starts, and lives in the store from then on.
It is never regenerated on read. That is the whole anti-cheat design: if the layout were
rolled at pick time, "did I guess right" would mean nothing, and if it were rolled on
every open, closing the screen would be a reroll.

The second half of that design lives in the callers: `public_state` is the only view a
client may see, and it reveals a cell's contents only after it has been picked. Never send
`cells` to a browser.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Final


# Cell contents. Strings rather than an enum so a stored round is plain JSON that survives
# a deploy without a migration.
EMPTY: Final = "empty"
DIAMOND: Final = "diamond"
JACKPOT: Final = "jackpot"       # take every diamond still on the board, at once
REFILL: Final = "refill"         # top the arena fight bank back up to its capacity

SMALL: Final = "small"
BIG: Final = "big"
SIZES: Final = (SMALL, BIG)


@dataclass(frozen=True, slots=True)
class Meadow:
    """One meadow's rules. Immutable: a round in flight reads its own stored copy."""

    size: str
    title: str
    side: int          # the board is side x side
    diamonds: int      # cells holding one diamond each
    picks: int         # how many cells a player may open
    tickets: int       # what one entry costs
    jackpot: int = 0   # cells that pay out every diamond on the board
    refill: int = 0    # cells that refill the arena fight bank

    @property
    def cells(self) -> int:
        return self.side * self.side

    @property
    def prize_cells(self) -> int:
        return self.diamonds + self.jackpot + self.refill


# The small meadow is the one a player meets first, so it is pure and legible: five of nine
# cells pay, three picks, no special squares to explain. The big one costs three times as
# much to enter and is where the two rare squares live -- a jackpot that hands over all
# twelve diamonds at once, and a square that refills the whole arena fight bank.
MEADOWS: Final = {
    SMALL: Meadow(
        size=SMALL, title="Малая поляна", side=3,
        diamonds=5, picks=3, tickets=1,
    ),
    BIG: Meadow(
        size=BIG, title="Большая поляна", side=5,
        diamonds=12, picks=5, tickets=3, jackpot=1, refill=1,
    ),
}


def meadow(size: str) -> Meadow | None:
    return MEADOWS.get(str(size or ""))


def build_board(size: str, seed: str) -> list[str]:
    """Lay out one board. Same seed, same board, forever.

    Seeded from the round id rather than left to a fresh SystemRandom so a finished round
    can be re-derived from what the store kept about it -- which is what makes a payout
    auditable instead of merely asserted.
    """
    rules = meadow(size)
    if rules is None:
        raise ValueError(f"unknown meadow size: {size!r}")
    cells = (
        [DIAMOND] * rules.diamonds
        + [JACKPOT] * rules.jackpot
        + [REFILL] * rules.refill
        + [EMPTY] * (rules.cells - rules.prize_cells)
    )
    random.Random(f"meadow:{size}:{seed}").shuffle(cells)
    return cells


def prize_for(cell: str, rules: Meadow, diamonds_left: int) -> tuple[int, bool]:
    """What one revealed cell pays: (rubies, refills_the_fight_bank).

    The jackpot is worth what is still BURIED, not the meadow's nominal twelve: a player
    who has already dug up three diamonds and then finds the jackpot has not lost those
    three, and must not be paid for them twice either.
    """
    if cell == DIAMOND:
        return 1, False
    if cell == JACKPOT:
        return max(0, int(diamonds_left)), False
    if cell == REFILL:
        return 0, True
    return 0, False


def public_state(round_row: dict) -> dict:
    """The ONLY shape a client may be shown. Unpicked cells are not in it.

    A browser receives this; it never receives `cells`. Sending the whole board and hiding
    it in CSS would put the answer in the page source, which for a game paying real
    currency is the same as publishing it.
    """
    rules = meadow(str(round_row.get("size") or ""))
    if rules is None:
        return {}
    picked = [int(index) for index in round_row.get("picked", []) if isinstance(index, int)]
    cells = [str(value) for value in round_row.get("cells", [])]
    revealed = {
        str(index): cells[index]
        for index in picked
        if 0 <= index < len(cells)
    }
    picks_left = max(0, rules.picks - len(picked))
    return {
        "size": rules.size,
        "title": rules.title,
        "side": rules.side,
        "cells": rules.cells,
        "diamonds": rules.diamonds,
        "picks": rules.picks,
        "picks_left": picks_left,
        "revealed": revealed,
        "rubies_won": max(0, int(round_row.get("rubies_won", 0) or 0)),
        "refilled": bool(round_row.get("refilled")),
        "finished": picks_left <= 0,
        "has_jackpot": bool(rules.jackpot),
        "has_refill": bool(rules.refill),
    }


def final_board(round_row: dict) -> list[str]:
    """Everything, for the reveal after the last pick. Never sent before `finished`."""
    return [str(value) for value in round_row.get("cells", [])]


__all__ = [
    "EMPTY", "DIAMOND", "JACKPOT", "REFILL", "SMALL", "BIG", "SIZES",
    "Meadow", "MEADOWS", "meadow", "build_board", "prize_for", "public_state",
    "final_board",
]
