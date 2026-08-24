"""Is the Phoenix a fight you learn, or a fight you out-level?

Run directly:  python pets_phoenix_sim.py            (150 fights a cell)
               python pets_phoenix_sim.py 500        (slower, tighter)


Three heroes crossed with three players. The heroes are the same pet at three stat
budgets; the players are three policies:

  * `perfect`  -- always the best answer. What a veteran does.
  * `average`  -- knows most telegraphs, misreads some. What a second run looks like.
  * `masher`   -- presses ⚔️ every turn. The build the rework exists to defeat.

What the numbers have to say, or the design failed:
  masher loses at EVERY stat budget, perfect wins at every budget, and a strong hero
  needs materially fewer actions than a weak one to win.
"""
import random
import statistics
import sys

import pets_phoenix as P

FIGHTS = int(sys.argv[1]) if len(sys.argv) > 1 else 200

HEROES = {
    "слабый": dict(max_hp=1400, damage=120, spell_power=95, crit=.12, crit_power=2.4,
                   reduction=.10, guard=.40, has_magic=True, level=10, name="слабый"),
    "ровный": dict(max_hp=2400, damage=240, spell_power=210, crit=.18, crit_power=2.9,
                   reduction=.22, guard=.45, has_magic=True, level=14, name="ровный"),
    "сильный": dict(max_hp=3800, damage=430, spell_power=380, crit=.26, crit_power=3.6,
                    reduction=.34, guard=.60, has_magic=True, level=20, name="сильный"),
}
BOSS = dict(name="Феникс пепельных залов", max_hp=2600, damage=210, level=13, floor=5)

# What each policy reaches for, in order of preference, out of whatever is on offer.
POLICIES = {
    "perfect": None,      # resolved from the engine's own grading, see best_action
    "average": None,
    "masher": [P.ATTACK],
}


def best_action(state, rng, mistakes=0.0):
    """The best button on offer, with `mistakes` chance of reaching for a wrong one."""
    offered = [row["code"] for row in P.actions(state)]
    if not offered:
        return None
    if rng.random() < mistakes:
        return rng.choice(offered)
    # The engine knows the grading; ask it by trying each and keeping the one that costs
    # the least health and does the most damage. That is a harness privilege, not a hint
    # the player ever gets.
    best, best_score = offered[0], None
    for code in offered:
        try:
            after = P.take(dict(state), code, seed=1234)
        except ValueError:
            continue
        score = (after.get("hero_hp", 0) - state.get("hero_hp", 0)) * 3 \
            + (state.get("boss_hp", 0) - after.get("boss_hp", 0)) \
            - after.get("burn", 0) * 40
        if best_score is None or score > best_score:
            best, best_score = code, score
    return best


def play(hero, policy, seed):
    rng = random.Random(seed)
    state = P.start(dict(hero), dict(BOSS), seed=seed)
    steps = 0
    while not P.is_over(state) and steps < 400:
        offered = [row["code"] for row in P.actions(state)]
        if not offered:
            break
        if policy == "masher":
            code = P.ATTACK if P.ATTACK in offered else offered[0]
        else:
            code = best_action(state, rng, mistakes=0.0 if policy == "perfect" else .30)
        state = P.take(state, code, seed=rng.randrange(1 << 30))
        steps += 1
    return str(state.get("phase_state")) == P.VICTORY, steps


print(f"{FIGHTS} боёв на клетку · победа% (ходов в среднем)\n")
print(f"{'герой':10}{'идеально':>18}{'середнячок':>18}{'жмёт атаку':>18}")
for name, hero in HEROES.items():
    cells = []
    for policy in ("perfect", "average", "masher"):
        results = [play(hero, policy, seed) for seed in range(FIGHTS)]
        wins = sum(1 for won, _ in results if won)
        steps = statistics.median(s for _, s in results)
        cells.append(f"{wins / FIGHTS * 100:5.1f}% ({steps:.0f})")
    print(f"{name:10}" + "".join(f"{c:>18}" for c in cells))
