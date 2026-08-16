"""Maintain the roll of honour, and read the pledges waiting to be answered.

The published list is edited BY HAND and only by the owner, because money arrives outside
the bot -- through whatever the two of you agreed -- and nothing in the game can verify
that it did. Somebody appears on the list because you put them there.

    python admin_donors.py list                      -- the published list
    python admin_donors.py pledges                   -- who has asked to contribute
    python admin_donors.py add "Кломбик" 25 --note "своё оружие: Молот зари"
    python admin_donors.py add "Кломбик" 10          -- tops up an existing entry
    python admin_donors.py remove "Кломбик"

Run it where the data lives -- the deployed volume, not a developer checkout. `--chat`
picks the store when more than one exists; with one it is found on its own.

Same `_cache_key` trap admin_grant.py documents: the entry a store is named with is the
RAW chat reference, and the filename is its hash. `_resolved_paths` makes the hash resolve
back to itself for the duration of a call, so nothing writes a sibling file nobody reads.
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import sys
from pathlib import Path

import donations
import stats


def _stores() -> list[str]:
    directory = stats._stats_dir()
    return [
        Path(path).name[: -len("_pets.json")]
        for path in sorted(glob.glob(str(directory / "*_pets.json")))
    ]


@contextlib.contextmanager
def _resolved_paths():
    original = stats._cache_key
    stats._cache_key = lambda raw: raw
    try:
        yield
    finally:
        stats._cache_key = original


def _entry(requested: str | None) -> str | None:
    if requested:
        return requested
    found = _stores()
    if len(found) == 1:
        return found[0]
    if not found:
        print(f"Хранилищ не найдено в {stats._stats_dir()}", file=sys.stderr)
        return None
    print("Хранилищ несколько — укажи --chat:", file=sys.stderr)
    for name in found:
        print(f"  {name}", file=sys.stderr)
    return None


def _make_console_utf8_safe() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _make_console_utf8_safe()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--chat", default=None, help="какое хранилище, если их несколько")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="показать опубликованный топ")
    pledge_command = commands.add_parser("pledges", help="кто оставил заявку")
    pledge_command.add_argument("--limit", type=int, default=30)
    add = commands.add_parser("add", help="добавить или пополнить запись")
    add.add_argument("name")
    add.add_argument("amount", type=float)
    add.add_argument("--note", default="", help="что вы ему пообещали: значок, оружие, титул")
    drop = commands.add_parser("remove", help="убрать из топа")
    drop.add_argument("name")
    args = parser.parse_args(argv)

    entry = _entry(args.chat)
    if entry is None:
        return 1

    with _resolved_paths():
        if args.command == "list":
            rows = donations.donors(entry, limit=1000)
            if not rows:
                print("Топ пока пуст.")
                return 0
            print(f"Топ поддержавших · всего ${donations.total_raised(entry)}")
            for place, row in enumerate(rows, start=1):
                note = f" — {row['note']}" if row["note"] else ""
                print(f"  {place}. {row['name']} · ${row['amount']}{note}")
            return 0

        if args.command == "pledges":
            rows = donations.pledges(entry, limit=args.limit)
            if not rows:
                print("Заявок пока нет.")
                return 0
            print(f"Заявки ({len(rows)}), новые сверху:")
            for row in rows:
                handle = f" @{row['username']}" if row.get("username") else ""
                print(f"  {row['at'][:16].replace('T', ' ')} · {row['name']}{handle} "
                      f"· ${row['amount']} · id {row['id']} · user {row['user_id']}")
            return 0

        if args.command == "add":
            row = donations.add_donor(entry, args.name, args.amount, args.note)
            print(f"В топе: {row['name']} · ${row['amount']}"
                  + (f" — {row['note']}" if row["note"] else ""))
            return 0

        if args.command == "remove":
            if donations.remove_donor(entry, args.name):
                print(f"Убран: {args.name}")
                return 0
            print(f"«{args.name}» в топе не найден.", file=sys.stderr)
            return 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
