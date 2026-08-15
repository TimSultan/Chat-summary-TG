"""Hand rubies to one player, by creature or owner name, wherever the store lives.

Rubies are minted by mobs, the quarry and quests -- there is deliberately no in-game
admin button for them, so a one-off correction (a compensation, a prize, a bug refund)
has to be made against the store directly. This is that tool, and it is kept honest by
going through ``pets.grant_rubies_once`` rather than editing JSON: the wallet, the
``ruby_sources`` ledger and the ``rubies_minted`` metric all stay consistent, and the
same source key can be replayed without paying twice.

Run it where the data is -- the deployed volume, not a developer checkout:

    python grant_rubies.py "Кломбик" 10000
    python grant_rubies.py "Кломбик" 10000 --reason compensation-2026-08 --yes

Without ``--yes`` it only reports what it found and changes nothing.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

import pets
import stats


def _stores() -> list[tuple[str, Path]]:
    """Every chat store on this machine, as (entry key, path).

    The entry key is the filename stem -- exactly what `pets._load` expects -- so this
    works without knowing which chats the bot is configured for.
    """
    directory = stats._stats_dir()
    return [
        (Path(path).name[: -len("_pets.json")], Path(path))
        for path in sorted(glob.glob(str(directory / "*_pets.json")))
    ]


def _matches(record: dict, needle: str) -> bool:
    needle = needle.casefold().strip()
    return needle in {
        str(record.get("name") or "").casefold().strip(),
        str(record.get("owner_name") or "").casefold().strip(),
        str(record.get("owner_username") or "").casefold().strip().lstrip("@"),
    }


def find(needle: str) -> list[tuple[str, str, dict]]:
    """Every (entry, user_id, record) whose creature or owner answers to `needle`."""
    found = []
    for entry, path in _stores():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            print(f"! не читается {path}: {error}", file=sys.stderr)
            continue
        for user_id, record in (data.get("pets") or {}).items():
            if isinstance(record, dict) and record.get("name") and _matches(record, needle):
                found.append((entry, str(user_id), record))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("who", help="имя существа, владельца или @username")
    parser.add_argument("amount", type=int, help="сколько рубинов начислить")
    parser.add_argument(
        "--reason", default=None,
        help="ключ начисления; повторный запуск с тем же ключом ничего не добавит",
    )
    parser.add_argument("--yes", action="store_true", help="действительно начислить")
    args = parser.parse_args()

    if args.amount <= 0:
        print("Сумма должна быть больше нуля.", file=sys.stderr)
        return 2

    targets = find(args.who)
    if not targets:
        print(f"Никого по имени «{args.who}» не нашлось. Проверь, где лежит хранилище:")
        print(f"  {stats._stats_dir()}")
        return 1
    if len(targets) > 1:
        print(f"Под «{args.who}» подходит несколько — уточни имя:")
        for entry, user_id, record in targets:
            print(f"  {entry} / {user_id}: {record.get('name')} · {record.get('owner_name')}")
        return 1

    entry, user_id, record = targets[0]
    before = pets.ruby_balance(entry, user_id)
    print(f"Нашёл: {record.get('name')} (владелец {record.get('owner_name')})")
    print(f"  чат {entry} · игрок {user_id} · сейчас 💎 {before}")
    if not args.yes:
        print(f"Начислить 💎 {args.amount}? Запусти ещё раз с --yes.")
        return 0

    source = args.reason or f"manual:{args.who}:{args.amount}"
    after = pets.grant_rubies_once(entry, user_id, args.amount, source)
    if after == before:
        print(f"Ключ «{source}» уже был использован — ничего не начислено.")
        return 0
    print(f"Готово: 💎 {before} → {after} (ключ «{source}»)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
