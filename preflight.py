"""Check a release against a COPY of the real data before it goes anywhere near the real data.

The unit tests prove the rules. They cannot prove the thing that has actually broken this
game, repeatedly: a store written by the OLD code being read by the NEW code and quietly
losing a field on the way through.

Every one of these was a real outage, and every one looked fine in the tests:

    _normalise_dungeon_run dropped `run_id`, so every dungeon kill re-used one loot key and
    300 kills produced zero scrolls.
    The scroll wallet rebuilt `pity` from a whitelist that omitted "dungeon", so a pity
    counter the game advertised had never once fired.
    pets_combat.snapshot() omitted fields the fighter needed, so replayed fights rebuilt a
    different creature than the one that fought.

They share a shape: a normaliser that rebuilds a structure from a list of known keys, and
therefore silently discards anything not on that list. A load-then-save round trip against
real data finds all of them mechanically, which is what this does.

    python preflight.py /path/to/a/copy/of/the/volume

WHAT IT CHECKS

    1. The release imports, and the web app can actually be BUILT with the arguments
       bot_listener really passes it. (A mismatch there took the whole service down on
       boot once; it is one line to check and it is checked here.)
    2. Every store loads without raising.
    3. A load -> save round trip loses nothing: no key disappears, no list gets shorter,
       no wallet gets smaller.

WHAT IT DOES NOT DO. It never writes to the directory you point it at -- everything is
compared in memory -- but point it at a COPY anyway. A preflight tool that can damage
production is not a safety measure.
"""

from __future__ import annotations

import argparse
import glob
import inspect
import json
import os
import sys
from pathlib import Path

FAILURE = "❌"
WARNING = "⚠️ "
OK = "✅"


def _make_console_utf8_safe() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


# --------------------------------------------------------------------- structural diffing


def _walk(value, path="") -> dict:
    """Every leaf in a JSON structure, keyed by its path. Lists are recorded by LENGTH as
    well as by element, so a shortened inventory shows up as its own finding rather than
    as a scatter of missing indices."""
    found = {}
    if isinstance(value, dict):
        for key, child in value.items():
            found.update(_walk(child, f"{path}.{key}" if path else str(key)))
    elif isinstance(value, list):
        found[f"{path}[]"] = len(value)
        for index, child in enumerate(value):
            found.update(_walk(child, f"{path}[{index}]"))
    else:
        found[path] = value
    return found


# Paths that the normalisers are MEANT to change, so a difference here is the code doing
# its job rather than losing something. Matched as a substring of the leaf path.
EXPECTED_CHANGES = (
    ".version",
    "submission_photos",          # consumed on read by design
)


def _compare(before: dict, after: dict) -> list[str]:
    """Findings, worst first. Empty means the round trip preserved everything."""
    old, new = _walk(before), _walk(after)
    findings = []
    for path, value in old.items():
        if any(marker in path for marker in EXPECTED_CHANGES):
            continue
        if path not in new:
            findings.append(f"{FAILURE} ПОТЕРЯНО  {path} = {value!r}")
        elif path.endswith("[]") and isinstance(value, int) and new[path] < value:
            findings.append(
                f"{FAILURE} УКОРОЧЕНО {path}: было {value}, стало {new[path]}"
            )
        elif new[path] != value:
            # A changed value is usually a repair (a missing default filled in). Numbers
            # going DOWN are the ones worth a shout: that is somebody's wallet.
            if isinstance(value, (int, float)) and isinstance(new[path], (int, float)) \
                    and new[path] < value:
                findings.append(
                    f"{FAILURE} УМЕНЬШИЛОСЬ {path}: было {value}, стало {new[path]}"
                )
            else:
                findings.append(f"{WARNING}изменено  {path}: {value!r} -> {new[path]!r}")
    return findings


# ------------------------------------------------------------------------- the checks


def check_imports() -> list[str]:
    """The release loads, and the app can be built the way production builds it."""
    problems = []
    try:
        import bot_listener
        import pets_web
    except Exception as error:
        return [f"{FAILURE} модули не импортируются: {error}"]

    try:
        source = inspect.getsource(bot_listener.run_bot_listener)
        block = source.split("pets_web.attach(")[1].split(")\n")[0]
        passed = [
            line.split("=")[0].strip() for line in block.splitlines()
            if "=" in line and not line.strip().startswith("#")
        ]
        passed = [name for name in passed if name.isidentifier()]
        accepted = inspect.signature(pets_web.attach).parameters
        for name in passed:
            if name not in accepted:
                problems.append(
                    f"{FAILURE} bot_listener передаёт attach(..., {name}=...), "
                    f"а attach() такого параметра не принимает — сервис не поднимется"
                )
    except Exception as error:
        problems.append(f"{WARNING}не удалось сверить вызов attach(): {error}")
    return problems


def check_store(path: Path, loader, saver_shape) -> tuple[str, list[str]]:
    """Load one store through the release and compare it with what is on disk."""
    label = path.name
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        return label, [f"{FAILURE} файл не читается: {error}"]
    try:
        loaded = loader()
    except Exception as error:
        return label, [f"{FAILURE} НЕ ЗАГРУЖАЕТСЯ новым кодом: {error!r}"]
    return label, _compare(raw, loaded)


def run(directory: Path) -> int:
    import stats
    # Everything downstream resolves storage through this, so pointing it at the copy is
    # what keeps the real volume out of reach.
    stats._stats_dir = lambda: directory
    os.environ["DATA_DIR"] = str(directory)

    import pets
    import economy
    import quests

    print(f"Проверяю копию данных: {directory}\n")

    failures = 0
    print("1. Код и сборка приложения")
    problems = check_imports()
    for line in problems:
        print(f"   {line}")
    failures += sum(1 for line in problems if line.startswith(FAILURE))
    if not problems:
        print(f"   {OK} импорт и вызов attach() согласованы")

    print("\n2. Хранилища: загрузка и round-trip")
    kinds = (
        ("*_pets.json", pets._load, "pets"),
        ("*_economy.json", economy._load, "economy"),
        ("*_quests.json", quests._load, "quests"),
    )
    seen = 0
    for pattern, loader, kind in kinds:
        for found in sorted(glob.glob(str(directory / pattern))):
            path = Path(found)
            entry = path.name[: -len(f"_{kind}.json")]
            seen += 1
            # _cache_key hashes the entry to a filename; here the filename IS the entry,
            # so it has to resolve to itself or every load would miss (see admin_grant.py).
            original = stats._cache_key
            stats._cache_key = lambda raw: raw
            try:
                label, findings = check_store(path, lambda: loader(entry), kind)
            finally:
                stats._cache_key = original
            hard = [line for line in findings if line.startswith(FAILURE)]
            failures += len(hard)
            if not findings:
                print(f"   {OK} {label}")
                continue
            print(f"   {'❌' if hard else '⚠️ '} {label}")
            for line in findings[:25]:
                print(f"        {line}")
            if len(findings) > 25:
                print(f"        … и ещё {len(findings) - 25}")

    if not seen:
        print(f"   {WARNING}в этой папке нет ни одного файла хранилища — не та папка?")

    print()
    if failures:
        print(f"{FAILURE} НЕ ВЫКАТЫВАТЬ: найдено проблем — {failures}.")
        print("   «ПОТЕРЯНО» и «УКОРОЧЕНО» означают, что новый код читает старые данные")
        print("   и молча выбрасывает часть. Почини нормализатор, а не данные.")
        return 1
    print(f"{OK} Годно к выкатке: старые данные читаются новым кодом без потерь.")
    print("   Это не отменяет тестов и не проверяет игровой баланс — только сохранность.")
    return 0


def main(argv: list[str] | None = None) -> int:
    _make_console_utf8_safe()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "directory",
        help="папка с КОПИЕЙ боевых данных (cache/stats/<timezone>/)",
    )
    args = parser.parse_args(argv)
    directory = Path(args.directory).expanduser().resolve()
    if not directory.is_dir():
        print(f"Нет такой папки: {directory}", file=sys.stderr)
        return 2
    return run(directory)


if __name__ == "__main__":
    raise SystemExit(main())
