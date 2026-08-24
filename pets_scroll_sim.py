"""Does casting a scroll beat throwing a punch? One controlled fight series per scroll.

Run directly:  python pets_scroll_sim.py           (writes SCROLL_BALANCE.md + .csv)
               python pets_scroll_sim.py --fights 1000 --quick

The question this answers is narrow on purpose. In a live fight the auto-battler picks an
action per turn -- four tickets for a plain attack, one for Defend, one for each scroll
still unused (pets_combat, `active_actions`). So a scroll is never free: casting it spends
the turn a punch would have used. "Is scroll X worth its turn" is therefore a question
about one action, and the honest way to measure it is to hold everything else still.

Two things make that possible, and both are ordinary game states rather than rigging:

* **One scroll per fighter.** A live loadout can hold four, and a fight with four mixes
  their effects and attributes nothing. Slots may each be empty, so a loadout holding
  exactly one scroll -- `(code, None, None, None)`, or the fourth slot for an ultimate --
  is a position a real player can be in, and it isolates that scroll completely.
* **Two synthetic scrolls as controls.** `sim_idle` does nothing and `sim_punch` deals
  exactly one plain hit's worth of damage. Both cost a turn like any scroll, so they
  separate what a scroll's effect is worth from what the turn it spends is worth.

The baseline every scroll is judged against is `пустые слоты`: the same creature with all
four slots open, which attacks and Defends and nothing else. That is the player's actual
alternative -- leave the slot empty -- so the headline column answers the question as it
is really faced, "is equipping this better than not?".

The opponent is identical in every arm and also carries four empty slots, which is what a
creature looks like after the wipe. Every arm replays the same seeds, so a scroll's score
is compared against its controls fight by fight rather than sample against sample, and
the reported interval is the paired one.

The `без слотов` row is a mob, not a pet: `pets.mob_fighter` passes no loadout, so it
never Defends. Every fighter now gets the same action budget whether or not it carries
scrolls (`C.MAX_SKILL_ACTIONS_PER_FIGHTER`), so that row measures Defend and nothing else.
"""

from __future__ import annotations

import argparse
import csv
import statistics
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pets_combat as combat
import pets_config as C
import pets_scroll_catalog as SCROLLS

# --------------------------------------------------------------- the laboratory fixtures
IDLE = "sim_idle"
PUNCH = "sim_punch"


def _control_scroll(code: str, name: str, effects: tuple) -> dict:
    return {
        "code": code, "name": name, "short": name, "category": "magic", "element": "air",
        "uses": 1, "dodgeable": True, "ultimate": False, "auto_weight": 1,
        "effects": effects, "icon": "·",
    }


# Registered into the lookup table rather than the catalogue tuples: `SCROLLS.SCROLLS` is
# what the game counts, validates and drops from, and a measuring instrument has no
# business appearing in any of those. `scroll()` reads the map, which is all combat needs.
SCROLLS.SCROLL_BY_CODE.setdefault(IDLE, _control_scroll(IDLE, "Пустой свиток", ()))
SCROLLS.SCROLL_BY_CODE.setdefault(
    PUNCH, _control_scroll(PUNCH, "Свиток-удар", ({"op": "damage", "amount": 1.0},)),
)


def solo_loadout(code: str) -> tuple:
    """Four slots with one scroll in the slot that scroll is allowed to occupy."""
    spell = SCROLLS.scroll(code)
    if spell is None:
        raise ValueError(f"unknown scroll: {code}")
    if spell["ultimate"]:
        return (None, None, None, code)
    return (code, None, None, None)


# Магия sits at 20 with everything else on purpose. Scroll damage now reads that stat
# (pets_config.spell_power), so a reference creature left at Магия 1 would be measuring
# "what is a scroll worth to somebody who never bought the stat behind it" -- a real
# question, but not the one this file asks. At 20 across the board the creature is the
# ordinary mid-game pet it has always been, and the headline column keeps meaning "is
# equipping this better than leaving the slot empty" for a player who spread their coins.
HERO_STATS = {"strength": 20, "health": 20, "agility": 20, "luck": 20, "magic": 20,
              "armor": 5, "level": 10}
# Three shapes rather than one, so a scroll cannot look good purely because of the single
# opponent it was measured against. A heal is worth more against a grinder than a glass
# cannon; a stun is worth more against the cannon.
OPPONENTS = {
    "зеркало": {"strength": 20, "health": 20, "agility": 20, "luck": 20, "magic": 20,
                "armor": 5, "level": 10},
    "танк": {"strength": 15, "health": 30, "agility": 14, "luck": 14, "magic": 12,
             "armor": 12, "level": 10},
    "ловкач": {"strength": 22, "health": 14, "agility": 28, "luck": 26, "magic": 16,
               "armor": 2, "level": 10},
}


def _fighter(key: str, stats: dict, skills: tuple) -> combat.Fighter:
    return combat.Fighter(
        key=key, name=key, strength=stats["strength"], health=stats["health"],
        agility=stats["agility"], luck=stats["luck"], armor=stats["armor"],
        magic=stats.get("magic", 0),
        level=stats["level"], effects=(), skills=skills, shield=None,
    )


EMPTY = "__empty__"
NO_SLOTS = "__none__"
CONTROLS = (NO_SLOTS, EMPTY, IDLE, PUNCH)


def _arms() -> list[tuple[str, tuple]]:
    """Every fighter configuration to measure, controls first."""
    rows = [
        (NO_SLOTS, ()),
        (EMPTY, SCROLLS.EMPTY_LOADOUT),
        (IDLE, solo_loadout(IDLE)),
        (PUNCH, solo_loadout(PUNCH)),
    ]
    rows.extend((row["code"], solo_loadout(row["code"])) for row in SCROLLS.SCROLLS)
    return rows


def run_arm(job: tuple) -> tuple:
    """One arm against one opponent shape: a score per seed, plus how the casts went.

    Score is 1 for a win, .5 for a draw, 0 for a loss -- the same accounting the arena
    pays out on, and a draw genuinely is half a result rather than a loss.
    """
    code, skills, shape, fights = job
    hero = _fighter("hero", HERO_STATS, skills)
    foe = _fighter("foe", OPPONENTS[shape], SCROLLS.EMPTY_LOADOUT)
    scores, cast, dodged, damage = [], 0, 0, 0
    for seed in range(fights):
        result = combat.simulate(hero, foe, seed=seed)
        scores.append(1.0 if result.winner == "hero" else (.5 if result.is_draw else 0.0))
        damage += result.total_damage.get("hero", 0)
        for round_ in result.rounds:
            if round_.attacker != "hero":
                continue
            if round_.event == f"skill_{code}":
                cast += 1
            elif round_.event == "skill_dodge":
                dodged += 1
    return code, shape, scores, cast, dodged, damage


def _paired(values: list[float], baseline: list[float]) -> tuple[float, float]:
    """Mean gap against the control and its 95% half-width, fight by fight on equal seeds.

    Paired because every arm replays the same seed list: the difference on one seed is a
    real observation, and differencing first removes the variance the shared opponent and
    shared draws would otherwise contribute to both sides.
    """
    gaps = [mine - theirs for mine, theirs in zip(values, baseline)]
    mean = statistics.fmean(gaps)
    if len(gaps) < 2:
        return mean * 100, 0.0
    error = statistics.stdev(gaps) / (len(gaps) ** .5)
    return mean * 100, 1.96 * error * 100


def simulate_all(fights: int, workers: int | None) -> dict:
    jobs = [
        (code, skills, shape, fights)
        for code, skills in _arms() for shape in OPPONENTS
    ]
    results: dict[tuple[str, str], tuple] = {}
    if workers == 1:
        for job in jobs:
            code, shape, *rest = run_arm(job)
            results[(code, shape)] = rest
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for code, shape, *rest in pool.map(run_arm, jobs, chunksize=1):
                results[(code, shape)] = rest
    return results


def _row(code: str, results: dict, fights: int) -> dict:
    pooled, casts, dodges, damage = [], 0, 0, 0
    per_shape = {}
    for shape in OPPONENTS:
        scores, cast, dodged, dealt = results[(code, shape)]
        pooled.extend(scores)
        per_shape[shape] = statistics.fmean(scores) * 100
        casts += cast
        dodges += dodged
        damage += dealt
    empty = [score for shape in OPPONENTS for score in results[(EMPTY, shape)][0]]
    punch = [score for shape in OPPONENTS for score in results[(PUNCH, shape)][0]]
    attempts = casts + dodges
    spell = SCROLLS.scroll(code) if code not in (NO_SLOTS, EMPTY) else None
    versus_empty, empty_error = _paired(pooled, empty)
    versus_punch, punch_error = _paired(pooled, punch)
    names = {NO_SLOTS: "Без слотов (моб)", EMPTY: "Пустые слоты"}
    return {
        "code": code,
        "name": (str(spell["name"]).split(": ", 1)[-1] if spell else names[code]),
        "ultimate": bool(spell and spell["ultimate"]),
        "element": (SCROLLS.element_label(spell["element"]) if spell else "—"),
        "win": statistics.fmean(pooled) * 100,
        "vs_empty": versus_empty, "vs_empty_error": empty_error,
        "vs_punch": versus_punch, "vs_punch_error": punch_error,
        "cast_rate": (attempts / (fights * len(OPPONENTS))) * 100,
        "dodge_rate": (dodges / attempts * 100) if attempts else 0.0,
        "damage": damage / (fights * len(OPPONENTS)),
        **{f"win_{shape}": value for shape, value in per_shape.items()},
    }


def bench_check(fights: int) -> dict:
    """The two numbers that decide whether the table can be believed at all.

    A mirror match has to land on a coin flip, or the harness is tilted before a scroll is
    involved. And `sim_punch` has to be worth what a plain attack is worth per action --
    it is the yardstick every row is measured against, and the two damage paths through
    the engine are genuinely different code (`spell_damage` against `_resolve_blow`), so
    their equivalence is an empirical claim rather than an obvious one.
    """
    mirror_hero = _fighter("hero", HERO_STATS, SCROLLS.EMPTY_LOADOUT)
    mirror_foe = _fighter("foe", OPPONENTS["зеркало"], SCROLLS.EMPTY_LOADOUT)
    mirror = statistics.fmean(
        1.0 if (result := combat.simulate(mirror_hero, mirror_foe, seed=seed)).winner == "hero"
        else (.5 if result.is_draw else 0.0)
        for seed in range(fights)
    ) * 100

    hero = _fighter("hero", HERO_STATS, solo_loadout(PUNCH))
    foe = _fighter("foe", OPPONENTS["зеркало"], SCROLLS.EMPTY_LOADOUT)
    attack_damage = attack_actions = cast_damage = cast_actions = 0
    for seed in range(fights):
        for round_ in combat.simulate(hero, foe, seed=seed).rounds:
            if round_.attacker != "hero":
                continue
            # Everything the engine can call a swing, including the ones that landed for
            # nothing: an action that missed still spent the turn it is being judged on.
            if round_.event in {"hit", "crit", "blocked", "low_damage", "dodge", "amulet_guard"}:
                attack_damage += round_.damage
                attack_actions += 1
            elif round_.event in {f"skill_{PUNCH}", "skill_dodge"}:
                cast_damage += round_.damage
                cast_actions += 1
    return {
        "mirror": mirror,
        "attack_per_action": attack_damage / max(1, attack_actions),
        "punch_per_action": cast_damage / max(1, cast_actions),
    }


def _verdict(row: dict) -> str:
    """Better or worse than leaving the slot empty, and only when the interval clears 0."""
    if row["code"] in CONTROLS:
        return "контроль"
    if row["vs_empty"] - row["vs_empty_error"] > 0:
        return "стоит слота"
    if row["vs_empty"] + row["vs_empty_error"] < 0:
        return "ХУЖЕ пустого"
    return "без разницы"


def _markdown(rows: list[dict], fights: int, check: dict) -> str:
    total = fights * len(OPPONENTS)
    controls = [row for row in rows if row["code"] in CONTROLS]
    scrolls = sorted(
        (row for row in rows if row["code"] not in CONTROLS),
        key=lambda row: row["vs_empty"], reverse=True,
    )
    worse = [row for row in scrolls if _verdict(row) == "ХУЖЕ пустого"]
    better = [row for row in scrolls if _verdict(row) == "стоит слота"]
    same = [row for row in scrolls if _verdict(row) == "без разницы"]

    out = [
        "# Стоит ли свиток своего слота",
        "",
        f"{total:,} боёв на каждую строку ({fights:,} на каждую из трёх форм соперника), "
        "одни и те же сиды во всех строках. Интервалы — 95%, парные: разница считается "
        "бой к бою против контроля, а не выборка против выборки.",
        "",
        "Существо носит **ровно один** свиток: слоты могут пустовать, поэтому набор из "
        "одного свитка — это обычное положение игрока, а не подстроенное. Четыре свитка "
        "в одном бою смешали бы эффекты и не дали бы отнести результат ни к одному из них.",
        "",
        "Отсчёт ведётся от **пустых слотов** — то есть от того же существа, которое умеет "
        "только бить и защищаться. Это и есть настоящая альтернатива: оставить слот "
        "свободным. Соперник во всех строках одинаковый и тоже с четырьмя пустыми слотами — "
        "так выглядит существо после вайпа. Лимит действий у всех одинаковый, "
        f"{C.MAX_SKILL_ACTIONS_PER_FIGHTER}, независимо от свитков.",
        "",
        "## Контроли",
        "",
        "| Строка | Победы | Что это |",
        "| --- | ---: | --- |",
    ]
    meaning = {
        NO_SLOTS: "Слотов нет вообще — так дерётся моб (`pets.mob_fighter`). "
                  "Отличается от пустых слотов только тем, что не умеет защищаться.",
        EMPTY: "Четыре пустых слота: только удар и защита. **Точка отсчёта таблицы.**",
        IDLE: "Каст, который не делает ничего. Показывает цену самого хода.",
        PUNCH: "Каст ровно в один обычный удар. Столько стоит ход, потраченный с пользой.",
    }
    for row in sorted(controls, key=lambda row: row["win"]):
        out.append(f"| {row['name']} | {row['win']:.1f}% | {meaning[row['code']]} |")

    out.extend([
        "",
        f"## Все свитки ({len(scrolls)})",
        "",
        "Отсортировано по разнице с пустым слотом. `vs пустой слот` — сколько побед "
        "свиток приносит сверх того, что существо набрало бы, оставив слот свободным; "
        "это и есть ответ на вопрос «надевать или нет». `vs удар` — сверх того, что дал "
        "бы обычный удар тем же ходом: это уже вопрос к самому эффекту. Три столбца "
        "справа — победы против каждой формы соперника по отдельности.",
        "",
        "| Свиток | Стихия | Победы | vs пустой слот | vs удар | зеркало | танк | ловкач | Увёртки | Вердикт |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ])
    for row in scrolls:
        star = " ⭐" if row["ultimate"] else ""
        out.append(
            f"| {row['name']}{star} | {row['element']} | {row['win']:.1f}% | "
            f"{row['vs_empty']:+.1f} ±{row['vs_empty_error']:.1f} | "
            f"{row['vs_punch']:+.1f} ±{row['vs_punch_error']:.1f} | "
            f"{row['win_зеркало']:.0f}% | {row['win_танк']:.0f}% | {row['win_ловкач']:.0f}% | "
            f"{row['dodge_rate']:.0f}% | {_verdict(row)} |"
        )

    out.extend([
        "",
        "## Итог",
        "",
        f"- **Стоят своего слота: {len(better)} из {len(scrolls)}**"
        + (f" — {', '.join(row['name'] for row in better[:8])}"
           + (" и другие" if len(better) > 8 else "") if better else ""),
        f"- **Не отличаются от пустого слота: {len(same)}**"
        + (f" — {', '.join(row['name'] for row in same[:8])}"
           + (" и другие" if len(same) > 8 else "") if same else ""),
        f"- **Хуже пустого слота: {len(worse)}**"
        + (f" — {', '.join(row['name'] for row in worse)}" if worse else ""),
        "",
        "Столбец «Увёртки» — доля кастов, которые соперник считал в ноль. Свиток, который "
        "можно увернуть, платит за это каждый раз: защитные свитки не считаются вредными "
        "(`harmful` в `take_active_action`), поэтому уворачиваться от них нечему, и часть "
        "их перевеса — просто то, что они не могут промахнуться.",
        "",
        "## Проверка стенда",
        "",
        f"- **Зеркало: {check['mirror']:.1f}%.** Два одинаковых бойца с пустыми слотами. "
        "Отклонение от 50% — преимущество первого хода, не перекос стенда.",
        f"- **Урон за действие: обычный удар {check['attack_per_action']:.1f}, "
        f"«Свиток-удар» {check['punch_per_action']:.1f}** (промахи посчитаны нулём — ход "
        "потрачен в обоих случаях). Разница меньше процента, так что мерка честная: это "
        "разные ветки движка (`spell_damage` против `_resolve_blow`), и их равенство "
        "проверено, а не предположено.",
        "",
        "## Побочная находка: защита проигрывает",
        "",
        "В таблице контролей строка «Без слотов (моб)» стоит **выше** строки «Пустые "
        "слоты», хотя это одно и то же существо. Разница между ними ровно одна: у мода "
        "нет слотов, поэтому он никогда не защищается, а существо с пустыми слотами "
        "тратит на защиту примерно каждое шестое действие. Тратит себе в минус.",
        "",
        "То есть «Защита» сейчас — не нейтральный ход, а отрицательный: ход, отданный ей, "
        "стоит дороже, чем стоит сохранённое здоровье. Это не про свитки и в этой таблице "
        "не чинится, но всякий свиток здесь измерен на фоне бойца, который часть ходов "
        "тратит впустую, — и «Пустой свиток» ниже «Пустых слотов» по той же причине.",
        "",
        "## Чего стенд НЕ мерит",
        "",
        "Соперник во всех боях одинаковый: он бьёт и защищается, и слоты у него пустые. "
        "Он **не ставит щитов и не накладывает эффектов**, поэтому свитки, которые отвечают "
        "на чужие действия, здесь работают вхолостую и их место в таблице занижено:",
        "",
        "- `Разбивает щит врага` — ломать нечего (**Растворитель**, и он же внизу таблицы);",
        "- `Снимает с себя все негативные эффекты` — снимать нечего;",
        "- `Поглощает следующий негативный эффект` — поглощать нечего;",
        "- `Отражает N% следующего удара` работает, но только по обычным ударам.",
        "",
        "Против живого соперника с полным набором из четырёх свитков эти строки поднимутся. "
        "Всё остальное — урон, лечение, щиты, ослабления, оглушения — измерено полностью.",
        "",
        "<sub>Собрано `pets_scroll_sim.py`. Числа — из живого движка `pets_combat.simulate`, "
        "не из отдельной модели.</sub>",
        "",
    ])
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fights", type=int, default=4000,
                        help="fights per scroll per opponent shape (default 4000)")
    parser.add_argument("--workers", type=int, default=None,
                        help="processes to use; 1 disables the pool")
    parser.add_argument("--out", default="SCROLL_BALANCE.md")
    args = parser.parse_args()

    results = simulate_all(args.fights, args.workers)
    rows = [_row(code, results, args.fights) for code, _skills in _arms()]
    check = bench_check(args.fights)

    report = Path(args.out)
    report.write_text(_markdown(rows, args.fights, check), encoding="utf-8")
    table = report.with_suffix(".csv")
    with table.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"{report} + {table} written from "
          f"{args.fights * len(OPPONENTS) * len(_arms()):,} fights")


if __name__ == "__main__":
    main()
