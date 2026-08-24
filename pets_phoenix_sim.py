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

def spell(slot, name, icon, **effects):
    """One equipped scroll in the shape pets.phoenix_hero_profile hands over.

    The numbers are lifted straight out of pets_scroll_catalog, because a loadout the
    catalogue cannot actually produce would measure a fight nobody plays.
    """
    row = dict(slot=slot, code=f"scroll_{slot}", name=name, icon=icon, ultimate=False,
               damage=0.0, heal=0.0, shield=0.0, burn=0.0, lifesteal=0.0,
               cleanse=False, stun=False)
    row.update(effects)
    return row


# Four casts have to cover a fight that runs twenty-odd turns, so every budget carries a
# loadout of the shape a player at that budget would plausibly own: a striker, something
# that keeps the hero alive, and one scroll that buys an opening outright.
HEROES = {
    "слабый": dict(max_hp=1400, damage=120, spell_power=95, crit=.12, crit_power=2.4,
                   reduction=.10, guard=.40, has_magic=True, level=10, name="слабый",
                   spells=[
                       spell(1, "Искра эфира", "⚡", damage=1.45),
                       spell(2, "Цепная молния", "🌩", damage=1.25),
                       spell(3, "Полевая перевязка", "🩹", heal=.30, shield=.10),
                       spell(4, "Королевский барьер", "👑", shield=.22),
                   ]),
    "ровный": dict(max_hp=2400, damage=240, spell_power=210, crit=.18, crit_power=2.9,
                   reduction=.22, guard=.45, has_magic=True, level=14, name="ровный",
                   spells=[
                       spell(1, "Багровая комета", "☄️", damage=1.2, burn=.45),
                       spell(2, "Хищный укус", "🦷", damage=1.3, lifesteal=.7),
                       spell(3, "Нить гравитации", "🪐", damage=.8, stun=True),
                       spell(4, "Песок времени", "⌛", heal=.18, cleanse=True),
                   ]),
    "сильный": dict(max_hp=3800, damage=430, spell_power=380, crit=.26, crit_power=3.6,
                    reduction=.34, guard=.60, has_magic=True, level=20, name="сильный",
                    spells=[
                        spell(1, "Звездопад", "🌠", damage=2.35, ultimate=True),
                        spell(2, "Дыхание дракона", "🐉", damage=1.25, burn=.55,
                              ultimate=True),
                        spell(3, "Кракен из ведра", "🐙", damage=.9, stun=True,
                              ultimate=True),
                        spell(4, "Обратный ход", "⏪", heal=.32, cleanse=True,
                              ultimate=True),
                    ]),
}
BOSS = dict(name="Феникс пепельных залов", max_hp=2600, damage=210, level=13, floor=5)

# What each policy reaches for, in order of preference, out of whatever is on offer.
POLICIES = {
    "perfect": None,      # resolved from the engine's own grading, see best_action
    "average": None,
    "masher": [P.ATTACK],
}


def worth(before, after):
    """What a press bought: health kept, health taken off the boss, fire not caught."""
    return (after.get("hero_hp", 0) - before.get("hero_hp", 0)) * 3 \
        + (before.get("boss_hp", 0) - after.get("boss_hp", 0)) \
        - after.get("burn", 0) * 40


def options(state):
    """The presses worth weighing. Backing out of the scroll shelf never is one."""
    return [row["code"] for row in P.actions(state) if row["code"] != P.CANCEL]


def best_action(state, rng, mistakes=0.0):
    """The best button on offer, with `mistakes` chance of reaching for a wrong one."""
    offered = options(state)
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
        if after.get("picking"):
            # ✨ only opens the shelf and settles nothing, so it is worth exactly what the
            # best scroll standing behind it is worth.
            scores = []
            for inner in options(after):
                try:
                    scores.append(worth(state, P.take(dict(after), inner, seed=1234)))
                except ValueError:
                    continue
            if not scores:
                continue
            score = max(scores)
        else:
            score = worth(state, after)
        if best_score is None or score > best_score:
            best, best_score = code, score
    return best


def play(hero, policy, seed):
    rng = random.Random(seed)
    state = P.start(dict(hero), dict(BOSS), seed=seed)
    steps = 0
    while not P.is_over(state) and steps < 400:
        offered = options(state)
        if not offered:
            break
        if policy == "masher":
            code = P.ATTACK if P.ATTACK in offered else offered[0]
        else:
            code = best_action(state, rng, mistakes=0.0 if policy == "perfect" else .30)
        state = P.take(state, code, seed=rng.randrange(1 << 30))
        steps += 1
    # Turns the FIGHT took, not buttons the player pushed. Opening the spell shelf and
    # backing out of it are presses that resolve nothing, and counting them would report
    # a longer fight for the pet that owns more scrolls -- the opposite of the truth.
    return str(state.get("phase_state")) == P.VICTORY, int(state.get("actions_taken", steps))


print(f"{FIGHTS} боёв на клетку · победа% (ходов боя в среднем)\n")
print(f"{'герой':10}{'идеально':>18}{'середнячок':>18}{'жмёт атаку':>18}")
for name, hero in HEROES.items():
    cells = []
    for policy in ("perfect", "average", "masher"):
        results = [play(hero, policy, seed) for seed in range(FIGHTS)]
        wins = sum(1 for won, _ in results if won)
        steps = statistics.median(s for _, s in results)
        cells.append(f"{wins / FIGHTS * 100:5.1f}% ({steps:.0f})")
    print(f"{name:10}" + "".join(f"{c:>18}" for c in cells))
