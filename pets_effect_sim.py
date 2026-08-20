"""Is an item's passive worth anything at all? One mirror match per distinct passive.

Run directly:  python pets_effect_sim.py            (writes EFFECT_BALANCE.md + .csv)
               python pets_effect_sim.py --quick        (fast smoke run)

The question is narrower than "is this a good weapon". A rare weapon's stats are already
decided by its rarity band (`_bonus_tuple`), and the passive rides on top of them for
free -- so the honest thing to measure is the passive *alone*, holding the stats still.

Every arm is therefore the same reference creature carrying exactly one passive, against
an opponent carrying none, replaying the same seeds as the control arm, which is that
same creature with `effects=()`. The control lands on a coin flip by construction, so an
arm's score minus 50 is what the passive is worth in win percentage points.

Two columns matter and they answer different questions:

* **+победы** is the whole point -- what the passive does to the outcome.
* **HP за бой** is why. It totals every hit point the passive moved (damage dealt, damage
  healed, damage absorbed) from the fight transcript and divides it by the creature's
  maximum health. A passive worth 0.7% of a health bar per fight is not "slightly weak";
  it is decoration, and this column names it as such before anyone argues about it.

Four opponent shapes, because a passive can be strong against exactly one of them: a heal
outlasts a grinder and does nothing against a glass cannon, and `mob_hunter` is invisible
outside PVE. The mob shape uses a `mob:` key and no scroll slots -- what `pets.mob_fighter`
actually builds -- so PVE-only passives fire in the row where they are supposed to.
"""

from __future__ import annotations

import argparse
import csv
import statistics
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pets_amulet_catalog as AMULETS
import pets_combat as combat
import pets_gear_catalog as GEAR
import pets_scroll_catalog as SCROLLS
import pets_weapon_catalog as WEAPONS

CONTROL = "__none__"

# The reference creature. Level 10 with 20s across the board is an ordinary mid-game pet:
# 880 maximum health, 98 damage a swing, about ten swings each. Those two numbers are the
# yardstick every flat "heals 6 HP" value in the catalogue has to be read against.
HERO_STATS = {"strength": 20, "health": 20, "agility": 20, "luck": 20, "armor": 5, "level": 10}
OPPONENTS = {
    "зеркало": {"strength": 20, "health": 20, "agility": 20, "luck": 20, "armor": 5, "level": 10},
    "танк": {"strength": 15, "health": 30, "agility": 14, "luck": 14, "armor": 12, "level": 10},
    "ловкач": {"strength": 22, "health": 14, "agility": 28, "luck": 26, "armor": 2, "level": 10},
    "моб": {"strength": 20, "health": 22, "agility": 18, "luck": 16, "armor": 6, "level": 10},
    # A bigger opponent, so `giant_slayer` has somewhere to fire -- without this shape it
    # reads as an exact zero and looks broken when it is only situational. Deliberately
    # only one tier up: a hopeless fight contributes nothing to a paired difference and
    # would just dilute every other row's resolution.
    "старший": {"strength": 23, "health": 23, "agility": 22, "luck": 21, "armor": 7, "level": 13},
}
# Only this shape is a mob: the key prefix is the whole of what makes a fighter a mob to
# the engine (`hurt` checks `str(source_key).startswith("mob:")`), and mobs carry no slots.
MOB_SHAPE = "моб"
# Passives that pay in gold or loot rather than damage. The harness reads a fight
# transcript, so it is structurally blind to these and scores them an exact zero -- which
# is not the same finding as "does nothing" and must not be reported as one.
ECONOMY_CODES = frozenset({"coin_rake", "collector", "survivor", "trophy_compass"})


def _fighter(key: str, stats: dict, effects: tuple) -> combat.Fighter:
    mob = key.startswith("mob:")
    return combat.Fighter(
        key=key, name=key, strength=stats["strength"], health=stats["health"],
        agility=stats["agility"], luck=stats["luck"], armor=stats["armor"],
        level=stats["level"], effects=effects,
        skills=() if mob else SCROLLS.EMPTY_LOADOUT, shield=None,
    )


def _specs() -> list[dict]:
    """Every distinct passive in the equipment catalogues, with who carries it.

    Keyed by the effect payload rather than by item, because a passive repeated at the
    same value on a weapon and a glove is one rule to judge, not two. The slot only
    changes which shop the player finds it in.
    """
    seen: dict[tuple, dict] = {}
    sources = (
        ("оружие", WEAPONS.WEAPON_SPECS),
        ("амулет", AMULETS.AMULET_SPECS),
        ("экипировка", GEAR.GEAR_SPECS),
    )
    for slot, specs in sources:
        for item in specs:
            effect = item.effect_dict()
            if not effect:
                continue
            key = tuple(sorted((str(name), str(value)) for name, value in effect.items()))
            row = seen.setdefault(key, {
                "code": str(effect["code"]),
                "value": effect.get("value"),
                "effect": effect,
                "text": str(effect.get("text", "")),
                "carriers": [],
            })
            row["carriers"].append((slot, item.rarity, item.name))
    return sorted(seen.values(), key=lambda row: (row["code"], str(row["value"])))


def _arm_effects(row: dict | None) -> tuple:
    return () if row is None else (dict(row["effect"]),)


def run_arm(job: tuple) -> tuple:
    """One passive against one opponent shape: a score per seed, plus what it moved."""
    index, effect, code, shape, fights = job
    hero = _fighter("hero", HERO_STATS, effect)
    foe_key = "mob:sim" if shape == MOB_SHAPE else "foe"
    foe = _fighter(foe_key, OPPONENTS[shape], ())
    event = f"amulet_{code}" if code else None
    scores, procs, moved = [], 0, 0
    for seed in range(fights):
        result = combat.simulate(hero, foe, seed=seed)
        scores.append(1.0 if result.winner == "hero" else (.5 if result.is_draw else 0.0))
        if event is None:
            continue
        for round_ in result.rounds:
            # `effect_round` files a proc under the owner of the passive, whichever side
            # of the exchange it fired on, so a reflected or absorbed hit counts here too.
            if round_.event == event and round_.attacker == "hero":
                procs += 1
                moved += abs(round_.damage)
    return index, shape, scores, procs, moved


def simulate_all(rows: list[dict], fights: int, workers: int | None) -> dict:
    jobs = [(-1, (), None, shape, fights) for shape in OPPONENTS]
    jobs += [
        (index, _arm_effects(row), row["code"], shape, fights)
        for index, row in enumerate(rows) for shape in OPPONENTS
    ]
    results: dict[tuple[int, str], tuple] = {}
    if workers == 1:
        for job in jobs:
            index, shape, *rest = run_arm(job)
            results[(index, shape)] = rest
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for index, shape, *rest in pool.map(run_arm, jobs, chunksize=2):
                results[(index, shape)] = rest
    return results


def _paired(values: list[float], baseline: list[float]) -> tuple[float, float]:
    """Mean gap against the control and its 95% half-width, fight by fight on equal seeds.

    Paired because both arms replay the same seed list, so the difference on one seed is a
    real observation and the variance the shared opponent contributes to both sides drops
    out of the interval instead of drowning a small effect.
    """
    gaps = [mine - theirs for mine, theirs in zip(values, baseline)]
    mean = statistics.fmean(gaps)
    if len(gaps) < 2:
        return mean * 100, 0.0
    error = statistics.stdev(gaps) / (len(gaps) ** .5)
    return mean * 100, 1.96 * error * 100


def reference() -> tuple[float, float]:
    """The reference creature's health bar and its damage a swing, from the engine.

    Read rather than written down, because the numbers a flat catalogue value has to be
    judged against move whenever `pets_config` moves, and a stale constant in a balance
    report is worse than no report.
    """
    hero = _fighter("hero", HERO_STATS, ())
    foe = _fighter("foe", OPPONENTS["зеркало"], ())
    derived = combat.derive(hero, foe)
    return float(derived["max_hp"]), float(derived["damage"])


def _row(index: int, spec: dict, results: dict, fights: int, max_hp: float) -> dict:
    pooled, control, procs, moved = [], [], 0, 0
    per_shape = {}
    for shape in OPPONENTS:
        scores, shape_procs, shape_moved = results[(index, shape)]
        pooled.extend(scores)
        control.extend(results[(-1, shape)][0])
        per_shape[shape] = statistics.fmean(scores) * 100
        procs += shape_procs
        moved += shape_moved
    gap, error = _paired(pooled, control)
    total_fights = fights * len(OPPONENTS)
    return {
        "code": spec["code"],
        "value": spec["value"],
        "text": spec["text"],
        "slots": "/".join(sorted({slot for slot, _, _ in spec["carriers"]})),
        "rarities": "/".join(sorted({rarity for _, rarity, _ in spec["carriers"]})),
        "carriers": ", ".join(name for _, _, name in spec["carriers"]),
        "win": statistics.fmean(pooled) * 100,
        "gap": gap,
        "error": error,
        "procs": procs / total_fights,
        "moved": moved / total_fights,
        "moved_share": (moved / total_fights) / max_hp * 100,
        **{f"win_{shape}": value for shape, value in per_shape.items()},
    }


def _verdict(row: dict) -> str:
    """Four bands, and "мёртвый" is a finding rather than a shrug.

    A passive whose interval straddles zero has not been shown to do anything, and one
    that moves under 2% of a health bar a fight cannot be doing anything -- the two agree
    almost everywhere, and where they disagree the health-bar number is the honest one
    because a win rate on a coin-flip mirror is a noisy way to see a small edge.

    "вне боя" is separate on purpose: a gold or drop-rate passive scores zero here for the
    same reason a thermometer reads nothing about weight, and lumping it in with the dead
    ones would invite somebody to "fix" it by inflating the arena's payout.
    """
    if row["code"] in ECONOMY_CODES:
        return "вне боя"
    if row["gap"] - row["error"] > 0 and row["gap"] >= 1.5:
        return "работает"
    if row["gap"] + row["error"] < 0:
        return "ВРЕДИТ"
    return "мёртвый"


def _markdown(rows: list[dict], fights: int, max_hp: float, hit: float, mirror: float) -> str:
    total = fights * len(OPPONENTS)
    ranked = sorted(rows, key=lambda row: row["gap"], reverse=True)
    dead = [row for row in ranked if _verdict(row) == "мёртвый"]
    out = [
        "# Что реально делает пассивка предмета",
        "",
        f"{total:,} боёв на строку ({fights:,} против каждой из {len(OPPONENTS)} форм "
        "соперника), одни и те же сиды во всех строках. Интервалы 95%, парные: разница "
        "считается бой к бою против контроля, а не выборка против выборки.",
        "",
        f"Эталонное существо: 20/20/20/20, броня 5, уровень 10 — **{max_hp:,.0f} HP** и "
        f"**{hit:.0f} урона за удар**, около десяти ударов с каждой стороны. Контроль — "
        "то же существо вообще без пассивки против тех же пятерых соперников "
        f"({mirror:.1f}% побед; ниже 50%, потому что «старший» заведомо сильнее). "
        "«+победы» — разница с этим контролем, то есть цена самой пассивки.",
        "",
        "**HP за бой** — сколько хитпоинтов пассивка сдвинула за бой: нанесла, вылечила "
        "или поглотила, взято из протокола боя. Рядом та же величина в долях здоровья. "
        f"Один удар это {hit / max_hp * 100:.0f}% полоски — с этим и надо сравнивать.",
        "",
        f"Мёртвых пассивок: **{len(dead)} из {len(rows)}**.",
        "",
        "| Пассивка | Значение | Носители | +победы | HP за бой | % полоски | Срабатываний | Вердикт |",
        "| --- | ---: | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in ranked:
        out.append(
            f"| `{row['code']}` | {row['value']} | {row['carriers']} | "
            f"{row['gap']:+.1f} ± {row['error']:.1f} | {row['moved']:.0f} | "
            f"{row['moved_share']:.1f}% | {row['procs']:.1f} | {_verdict(row)} |"
        )
    out += [
        "",
        "## По формам соперника",
        "",
        "Победы в процентах. Пассивка, живая ровно в одном столбце, не сломана — она "
        "узкая; мёртвая во всех четырёх — сломана.",
        "",
        "| Пассивка | Значение | " + " | ".join(OPPONENTS) + " |",
        "| --- | ---: | " + " | ".join("---:" for _ in OPPONENTS) + " |",
    ]
    for row in ranked:
        cells = " | ".join(f"{row[f'win_{shape}']:.1f}" for shape in OPPONENTS)
        out.append(f"| `{row['code']}` | {row['value']} | {cells} |")
    out.append("")
    return "\n".join(out)


FIELDS = (
    "code", "value", "slots", "rarities", "carriers", "win", "gap", "error",
    "procs", "moved", "moved_share",
    *(f"win_{shape}" for shape in OPPONENTS),
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    # 600 a shape is 300,000 fights and about a minute, against the nine minutes 3,000 a
    # shape cost. The intervals widen from about +-0.8 to about +-1.8 win points, which is
    # still far tighter than any tuning decision this harness is used to make -- nobody
    # moves a value because it measured two points differently. Pass --fights for the rare
    # case that needs the precision (settling two items a point apart, say).
    parser.add_argument("--fights", type=int, default=600, help="боёв на форму соперника")
    parser.add_argument("--quick", action="store_true", help="200 боёв, для проверки прогона")
    parser.add_argument("--workers", type=int, default=None, help="1 отключает пул процессов")
    parser.add_argument("--out", default="EFFECT_BALANCE", help="имя файлов без расширения")
    args = parser.parse_args()
    fights = 200 if args.quick else args.fights

    specs = _specs()
    results = simulate_all(specs, fights, args.workers)
    max_hp, hit = reference()
    mirror = statistics.fmean(
        score for shape in OPPONENTS for score in results[(-1, shape)][0]
    ) * 100
    rows = [_row(index, spec, results, fights, max_hp) for index, spec in enumerate(specs)]

    Path(f"{args.out}.md").write_text(
        _markdown(rows, fights, max_hp, hit, mirror), encoding="utf-8",
    )
    with Path(f"{args.out}.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    dead = sum(1 for row in rows if _verdict(row) == "мёртвый")
    print(f"{len(rows)} пассивок, {fights * len(OPPONENTS):,} боёв на каждую; мёртвых: {dead}")
    print(f"контроль (зеркало без пассивок): {mirror:.1f}%   эталон HP: {max_hp:,.0f}")
    print(f"написано: {args.out}.md, {args.out}.csv")


if __name__ == "__main__":
    main()
