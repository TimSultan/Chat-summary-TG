"""Pairwise voting, the pure half: pairing and ranking. No I/O, no Telegram, no storage.

A Python port of import/voting-core.js -- deliberately a port and not an improvement, so
the reference implementation, its documentation (import/CLAUDE.md) and this stay one
system. Same vocabulary throughout:

    entry   one thing being judged (voting.Entry -- only .entry_id is used here)
    pair    (entry_id_a, entry_id_b), in display order
    pick    entry_id_a | entry_id_b | TIE
    ballot  one voter's session: the pairs they were dealt and what they picked

This is the second voting system (the "arena"), running beside the grid ballot in
voting.py rather than replacing it. Nothing here touches v1.

The rules that must not regress, per CLAUDE.md:

- Ranking is order-independent. compute_standings refits from the whole vote table every
  time. It must never become incremental Elo -- that would make the result depend on the
  order votes arrived in, and two runs over the same data would disagree.
- Even exposure. Pairing deals in rounds, so every work appears a similar number of times.
  An under-exposed work gets a misleadingly wide margin.
"""

import math
import random

TIE = "tie"

# Chess convention: the field averages 1500 and +400 is roughly ten times more likely to
# win a head-to-head. Ratings are only ever compared with each other, so the scale is
# presentation -- but it is the scale everybody already reads fluently.
RATING_BASE = 1500.0
RATING_SCALE = 400.0
# One standard error, converted to rating points. 400/ln(10) is the derivative of the
# rating scale, so an error bar in strength-space lands in rating-space unchanged.
_SE_SCALE = RATING_SCALE / math.log(10)


def build_pairs_random(entry_ids, wanted, rng=None):
    """Random pairing, dealt in rounds.

    Each round shuffles the whole field and deals it off two at a time, so a work appears
    at most once per round and exposure stays even. A voter never sees the same matchup
    twice, whichever way round it was shown.
    """
    rng = rng or random
    pairs, seen = [], set()
    guard = 0
    while len(pairs) < wanted and guard < 200:
        guard += 1
        deck = list(entry_ids)
        rng.shuffle(deck)
        for index in range(0, len(deck) - 1, 2):
            if len(pairs) >= wanted:
                break
            a, b = deck[index], deck[index + 1]
            key = tuple(sorted((a, b)))
            if key in seen:
                continue
            seen.add(key)
            pairs.append((a, b) if rng.random() < 0.5 else (b, a))
    return pairs


def build_pairs_adaptive(
    entry_ids, wanted, standings=None, ballots_so_far=0,
    random_share=0.3, warmup_ballots=15, rng=None,
):
    """Pairing that spends votes where the ranking is least certain: each pair is seeded on
    the work with the widest error bar and matched against a similarly rated opponent.

    `random_share` of the pairs stay purely random, so a work that started unlucky can
    climb out of a low bracket instead of being locked into it. Below `warmup_ballots`
    finished ballots the ratings are mostly prior, so this falls back to pure random --
    pairing on noise is worse than not pairing at all.
    """
    rng = rng or random
    rows = (standings or {}).get("rows") or []
    if not rows or ballots_so_far < warmup_ballots:
        return build_pairs_random(entry_ids, wanted, rng)

    by_id = {row["entry_id"]: row for row in rows}
    rating = lambda entry_id: by_id.get(entry_id, {}).get("rating", RATING_BASE)
    margin = lambda entry_id: by_id.get(entry_id, {}).get("margin") or RATING_SCALE

    targeted = max(0, wanted - round(wanted * random_share))
    pairs, seen = [], set()
    used = {}
    uses = lambda entry_id: used.get(entry_id, 0)

    guard = 0
    while len(pairs) < targeted and guard < wanted * 60:
        guard += 1
        # Seed: widest error bar, least used so far in this session.
        seed = sorted(entry_ids, key=lambda e: (uses(e), -margin(e)))[0]
        candidates = [
            e for e in entry_ids
            if e != seed and tuple(sorted((e, seed))) not in seen
        ]
        if not candidates:
            break
        opponent = sorted(candidates, key=lambda e: (uses(e), abs(rating(e) - rating(seed))))[0]
        seen.add(tuple(sorted((seed, opponent))))
        used[seed] = uses(seed) + 1
        used[opponent] = uses(opponent) + 1
        pairs.append((seed, opponent) if rng.random() < 0.5 else (opponent, seed))

    # Top up with random pairs this voter hasn't already been given.
    for pair in build_pairs_random(entry_ids, wanted * 2, rng):
        if len(pairs) >= wanted:
            break
        key = tuple(sorted(pair))
        if key in seen:
            continue
        seen.add(key)
        pairs.append(pair)
    return pairs[:wanted]


def compute_standings(entries, ballots, iterations=250, tolerance=1e-9):
    """Fit a strength for every work from every pick, and report it as a rating.

    Bradley-Terry by MM iteration: a win counts for more when the opponent is strong, so
    the table accounts for luck of the draw and can rank two works that were never shown
    against each other. A weak prior (half a draw against a phantom of average strength)
    keeps an undefeated or winless work finite instead of running off to infinity.

    ORDER-INDEPENDENT, and that is a requirement rather than a happy accident: the whole
    table is refitted from every vote each time, so the same votes in any order give
    byte-identical output.

    `ballots` are anything with .pairs and .picks. A pick naming a work that is no longer
    in `entries` is skipped rather than dropped from the ballot -- a work removed after
    the fact must not invalidate the other nine judgements a voter made.

    Returns {"rows": [...], "judgements": int, "iterations": int}, rows best-first, each:
        entry_id, entry, strength, rating, margin (1 s.e. in rating points, or None),
        played, score (wins + half a point per tie), win_rate
    """
    entries = list(entries)
    size = len(entries)
    if size == 0:
        return {"rows": [], "judgements": 0, "iterations": 0}

    index_of = {entry.entry_id: i for i, entry in enumerate(entries)}
    meetings = [[0] * size for _ in range(size)]
    wins = [0.0] * size
    played = [0] * size
    judgements = 0

    for ballot in ballots or []:
        pairs = getattr(ballot, "pairs", None) or []
        picks = getattr(ballot, "picks", None) or []
        for position, pair in enumerate(pairs):
            pick = picks[position] if position < len(picks) else None
            if not pick:
                continue
            i = index_of.get(pair[0])
            j = index_of.get(pair[1])
            if i is None or j is None:
                continue  # a work that has been removed since the vote
            meetings[i][j] += 1
            meetings[j][i] += 1
            played[i] += 1
            played[j] += 1
            judgements += 1
            if pick == TIE:
                wins[i] += 0.5
                wins[j] += 0.5
            elif pick == pair[0]:
                wins[i] += 1
            elif pick == pair[1]:
                wins[j] += 1

    strength = [1.0] * size
    used = 0
    for iteration in range(iterations):
        used = iteration + 1
        nxt = [1.0] * size
        for i in range(size):
            denominator = 1.0 / (strength[i] + 1.0)  # the prior: one virtual draw
            for j in range(size):
                if i == j or meetings[i][j] == 0:
                    continue
                denominator += meetings[i][j] / (strength[i] + strength[j])
            nxt[i] = (wins[i] + 0.5) / denominator if denominator > 0 else strength[i]
            if not math.isfinite(nxt[i]) or nxt[i] <= 0:
                nxt[i] = 1e-6
        # Normalise to geometric mean 1, or the whole vector drifts and the ratings with it.
        scale = math.exp(sum(math.log(value) for value in nxt) / size)
        nxt = [value / scale for value in nxt]
        delta = max(abs(math.log(nxt[i] / strength[i])) for i in range(size))
        strength = nxt
        if delta < tolerance:
            break

    rows = []
    for i, entry in enumerate(entries):
        information = strength[i] / (strength[i] + 1.0) ** 2  # the prior's contribution
        for j in range(size):
            if i == j or meetings[i][j] == 0:
                continue
            information += (
                meetings[i][j] * strength[i] * strength[j] / (strength[i] + strength[j]) ** 2
            )
        rows.append({
            "entry_id": entry.entry_id,
            "entry": entry,
            "strength": strength[i],
            "rating": RATING_BASE + RATING_SCALE * math.log10(strength[i]),
            "margin": (_SE_SCALE / math.sqrt(information)) if information > 0 else None,
            "played": played[i],
            "score": wins[i],
            "win_rate": (wins[i] / played[i]) if played[i] else None,
        })

    rows.sort(key=lambda row: (-row["rating"], -row["played"], row["entry_id"]))
    return {"rows": rows, "judgements": judgements, "iterations": used}


def win_probability(rating_a, rating_b):
    """The modelled chance `a` beats `b`, from two ratings."""
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / RATING_SCALE))


def is_separated(row_a, row_b, sigmas=2):
    """Are these two rows further apart than noise? What "the vote has decided" means --
    two works whose margins overlap are not meaningfully ranked against each other."""
    if row_a.get("margin") is None or row_b.get("margin") is None:
        return False
    combined = math.hypot(row_a["margin"], row_b["margin"])
    return abs(row_a["rating"] - row_b["rating"]) > sigmas * combined


def coverage(entry_count, judgements):
    """Judgements per possible pair. CLAUDE.md's sizing rule of thumb: aim for 4 or more,
    below which the top of the table will not separate."""
    possible = entry_count * (entry_count - 1) / 2
    return (judgements / possible) if possible else 0.0
