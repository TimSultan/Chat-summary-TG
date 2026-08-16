"""Take back an XP grant, and keep the money it was standing in for.

XP IS THE WRONG LEVER FOR GIVING SOMEBODY MONEY, and this tool exists because that is an
easy mistake to make. Coins are DERIVED from XP (economy.balance: coins_for_xp(xp) + bonus
- spent), so granting XP does put coins in a wallet -- and also puts the person at the top
of /top and /stat, rewrites the chat's leaderboard, and cannot be told apart afterwards
from XP somebody earned by writing.

The right lever is `admin_grant.py --gold`, which moves the `bonus` half and touches no
ranking at all.

So this does two things together, and the second is the one that matters:

    1. removes the XP grant, restoring the leaderboard;
    2. pays the same value back as REAL coins, so the person keeps what was intended.

Skipping step 2 would quietly take their money away -- removing N XP removes N // 5 coins
(stats.XP_PER_COIN) from a balance they may already have spent from.

    python admin_xp.py show "Кломбик"
    python admin_xp.py revoke "Кломбик"                 # dry run: says what it would do
    python admin_xp.py revoke "Кломбик" --yes
    python admin_xp.py revoke "Кломбик" --yes --no-compensate   # really take it all back

Run it where the data lives -- the deployed volume, not a developer checkout.
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import json
import sys
from pathlib import Path

import economy
import stats


def _stores() -> list[tuple[str, Path]]:
    directory = stats._stats_dir()
    return [
        (Path(path).name[: -len("_pets.json")], Path(path))
        for path in sorted(glob.glob(str(directory / "*_pets.json")))
    ]


@contextlib.contextmanager
def _resolved_paths():
    """See admin_grant.py: the entry here is already the on-disk hash, so _cache_key has
    to resolve to itself or every read and write lands in a sibling file nobody uses."""
    original = stats._cache_key
    stats._cache_key = lambda raw: raw
    try:
        yield
    finally:
        stats._cache_key = original


def _matches(record: dict, needle: str) -> bool:
    needle = needle.casefold().strip()
    return needle in {
        str(record.get("name") or "").casefold().strip(),
        str(record.get("owner_name") or "").casefold().strip(),
        str(record.get("owner_username") or "").casefold().strip().lstrip("@"),
    }


def find(needle: str) -> list[tuple[str, str, dict]]:
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


def _num(value: int) -> str:
    """Spaced thousands. A helper rather than .replace(",", " ") on the finished line --
    doing it to the line also eats the commas in the sentence around the number."""
    return f"{int(value):,}".replace(",", " ")


def _make_console_utf8_safe() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _show(entry: str, user_id: str) -> dict:
    with _resolved_paths():
        grants = stats.xp_grants_for(entry, user_id)
    if not grants:
        print("  XP-начислений нет — этот игрок свой XP заработал.")
        return grants
    total = sum(row["amount"] for row in grants.values())
    print(f"  Выданный XP: {_num(total)}")
    for key, row in sorted(grants.items(), key=lambda item: -item[1]["amount"]):
        print(f"    {_num(row['amount']):>14} XP  ·  {row['granted_at'] or '?'}"
              f"  ·  ключ «{key}»")
    print(f"  Это даёт монет: {_num(total // stats.XP_PER_COIN)}")
    return grants


def main(argv: list[str] | None = None) -> int:
    _make_console_utf8_safe()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("show", help="какие XP-начисления есть у игрока")
    show.add_argument("who")
    revoke = commands.add_parser("revoke", help="забрать XP, вернув эквивалент монетами")
    revoke.add_argument("who")
    revoke.add_argument("--key", default=None, help="только одно начисление, по ключу")
    revoke.add_argument("--yes", action="store_true", help="действительно применить")
    revoke.add_argument(
        "--no-compensate", action="store_true",
        help="НЕ возвращать монеты — игрок потеряет и XP, и деньги",
    )
    args = parser.parse_args(argv)

    targets = find(args.who)
    if not targets:
        print(f"Никого по имени «{args.who}» не нашлось. Хранилища тут:")
        print(f"  {stats._stats_dir()}")
        return 1
    if len(targets) > 1:
        print(f"Под «{args.who}» подходит несколько — уточни имя:")
        for entry, user_id, record in targets:
            print(f"  {entry} / {user_id}: {record.get('name')} · {record.get('owner_name')}")
        return 1

    entry, user_id, record = targets[0]
    print(f"Нашёл: {record.get('name')} (владелец {record.get('owner_name')})")
    print(f"  чат {entry} · игрок {user_id}")
    grants = _show(entry, user_id)

    if args.command == "show":
        return 0
    if not grants:
        return 0

    if args.key is not None and args.key not in grants:
        print(f"Ключа «{args.key}» у этого игрока нет.", file=sys.stderr)
        return 1
    removing = grants[args.key]["amount"] if args.key else sum(r["amount"] for r in grants.values())
    coins = removing // stats.XP_PER_COIN

    if not args.yes:
        print(f"\nБудет снято: {_num(removing)} XP")
        if args.no_compensate:
            print(f"Монеты НЕ возвращаются — игрок потеряет ~{_num(coins)} монет.")
        else:
            print(f"И начислено обратно: {_num(coins)} монет "
                  "(уже как деньги, не как XP).")
        print("Ничего не изменено. Повтори с --yes.")
        return 0

    with _resolved_paths():
        removed = stats.revoke_xp_grants(entry, user_id, args.key)
        paid = 0
        if removed and not args.no_compensate:
            paid = removed // stats.XP_PER_COIN
            # Idempotent on the exact amount: re-running the same correction cannot pay
            # twice, the same guarantee every other grant in this codebase makes.
            economy.grant_once(
                entry, user_id, paid, f"xp_grant_revert:{user_id}:{removed}",
            )
        left = stats.xp_grants_for(entry, user_id)

    print(f"\nСнято XP: {_num(removed)}")
    if paid:
        print(f"Начислено монет: {_num(paid)}")
    print(f"Осталось XP-начислений: {len(left)}")
    print("\nТопы по XP пересчитываются на лету — проверь /top.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
