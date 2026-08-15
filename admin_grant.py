"""Hand gold, rubies, farm tickets or dungeon tickets to one player, in any combination.

Supersedes grant_rubies.py, which could only do rubies. The four currencies are minted in
four different places (chat activity, mobs/quarry/quests, painting a figurine, a lucky PVE
roll) and none of them has an in-game admin button, so a one-off correction -- a
compensation, a prize, a bug refund -- still has to be made against the store directly.
This stays honest by going through the same library call each in-game path already uses,
never by writing the JSON:

    rubies          pets.grant_rubies_once   -- wallet + ruby_sources ledger + metric
    gold            economy.grant_once       -- bonus ledger, same as quests.py's payout
    farm tickets    pets.grant_farm_ticket   -- wallet + per-key replay guard, one call/ticket
    dungeon tickets pets.grant_dungeon_ticket -- wallet only, see _grant_dungeon_tickets below

Run it where the data is -- the deployed volume, not a developer checkout:

    python admin_grant.py "Кломбик" --gold 5000 --rubies 10000 --farm-tickets 3 --dungeon-tickets 2
    python admin_grant.py "Кломбик" --rubies 10000 --reason compensation-2026-08 --yes

Without ``--yes`` it only reports what it found and what it would grant, and changes
nothing -- keep it that way; it is the only thing standing between a typoed name and a
real player's wallet.

Two gotchas worth knowing before touching this file:

* pets.py and economy.py name every chat's file `{stats._cache_key(entry)}_pets.json` /
  `_economy.json`, where `entry` is meant to be the RAW chat reference (a title) and the
  hash is only how it is spelled on disk. `_stores()` below only ever sees the file, so
  the "entry" it hands back is already that hash -- feeding it straight into
  `pets.grant_rubies_once` et al. would hash it a SECOND time and silently write a sibling
  file nobody ever reads from again. `_resolved_paths()` makes `_cache_key` the identity
  for the duration of a call, so "entry" resolves back to the exact file it was read from.
  (This bit grant_rubies.py: `cache/stats/Europe_Moscow/172f36df233f74a1_pets.json` is an
  orphaned double-hashed store it left behind, `"pets": {}` with 10000 rubies sitting in
  nobody's wallet. Left in place -- it is evidence, and outside this file's remit to fix.)
* `pets.grant_dungeon_ticket` has no source/reason argument at all, unlike its ruby and
  farm-ticket siblings -- the game never needed one, since every real call site is a
  one-shot event (a PVE win, a launch gift flagged once per whole chat). A replay of this
  tool has no such guarantee, so `_grant_dungeon_tickets` keeps its own tiny receipt file
  next to the chat's other stores (`{entry}_dungeon_ticket_grants.json`) recording which
  (player, reason) pairs it has already paid. That file is this tool's own bookkeeping,
  not a second copy of the wallet -- it says only "did this reason already run", never how
  many tickets anyone holds, so it cannot itself get out of sync with pets.py's own count.
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import json
import sys
from collections import namedtuple
from pathlib import Path

import economy
import pets
import stats


def _stores() -> list[tuple[str, Path]]:
    """Every chat store on this machine, as (entry key, path).

    The entry key is the filename stem -- what `_matches`/`find` below need to read the
    right file, and, via `_resolved_paths`, what the grant/balance helpers need too.
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


@contextlib.contextmanager
def _resolved_paths():
    """Make `stats._cache_key` the identity for one call into pets.py/economy.py.

    See the module docstring's first gotcha. `entry` here is already the hash the file is
    named with; without this, every read and every grant below would quietly resolve to a
    different, usually nonexistent, file instead of the one `find()` just reported.
    """
    original = stats._cache_key
    stats._cache_key = lambda raw: raw
    try:
        yield
    finally:
        stats._cache_key = original


# --- per-currency balance/grant pairs ------------------------------------------------


def _rubies_balance(entry, user_id) -> int:
    with _resolved_paths():
        return pets.ruby_balance(entry, user_id)


def _grant_rubies(entry, user_id, amount: int, reason: str) -> None:
    with _resolved_paths():
        pets.grant_rubies_once(entry, user_id, amount, reason)


def _gold_balance(entry, user_id) -> int:
    """The bonus-ledger portion of gold, not the /stat balance.

    economy.balance(entry, user_id, xp) adds coins_for_xp(xp) on top of this, and xp needs
    the chat's live words_per_point -- a network round trip through Telethon this offline
    tool has no client for. Passing xp=0 reports exactly the part a grant can move (bonus
    + legacy received - spent, clamped at zero), which is also the only part that changes
    below, so before/after here stays honest even though it will read lower than /stat.
    """
    with _resolved_paths():
        return economy.balance(entry, user_id, 0)


def _grant_gold(entry, user_id, amount: int, reason: str) -> None:
    with _resolved_paths():
        economy.grant_once(entry, user_id, amount, reason)


def _farm_ticket_balance(entry, user_id) -> int:
    with _resolved_paths():
        return pets.farm_tickets(entry, user_id)


def _grant_farm_tickets(entry, user_id, amount: int, reason: str) -> None:
    """pets.grant_farm_ticket hands out exactly one ticket per call, keyed by `reason` for
    its own replay guard (see FARM_TICKET_GRANT_MEMORY in pets.py) -- so N tickets is N
    calls, each with its own numbered key. A replay with the same base reason regenerates
    the same N keys and every call is refused, in place, by pets.py itself."""
    with _resolved_paths():
        for unit in range(1, max(0, int(amount)) + 1):
            pets.grant_farm_ticket(entry, user_id, f"{reason}:{unit}")


def _dungeon_ticket_balance(entry, user_id) -> int:
    with _resolved_paths():
        return pets.dungeon_tickets(entry, user_id)


_DUNGEON_LEDGER_VERSION = 1


def _dungeon_ledger_path(entry: str) -> Path:
    return stats._stats_dir() / f"{entry}_dungeon_ticket_grants.json"


def _load_dungeon_ledger(entry: str) -> dict:
    path = _dungeon_ledger_path(entry)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    else:
        data = {}
    if not isinstance(data, dict) or not isinstance(data.get("grants"), dict):
        data = {"grants": {}}
    data["version"] = _DUNGEON_LEDGER_VERSION
    return data


def _grant_dungeon_tickets(entry, user_id, amount: int, reason: str) -> None:
    """See the module docstring's second gotcha: pets.grant_dungeon_ticket takes no source
    key, so the replay guard has to live here instead, as a receipt keyed by (player,
    reason) -- checked and written OUTSIDE _resolved_paths, since it is this tool's own
    file, not one pets.py's `entry` hashing scheme has any part in."""
    ledger = _load_dungeon_ledger(entry)
    key = f"{user_id}:{reason}"
    if key in ledger["grants"]:
        return
    with _resolved_paths():
        for _ in range(max(0, int(amount))):
            pets.grant_dungeon_ticket(entry, user_id)
    ledger["grants"][key] = max(0, int(amount))
    stats._write_json_atomic(_dungeon_ledger_path(entry), ledger)


Currency = namedtuple("Currency", "attr flag label emoji balance grant")

CURRENCIES = (
    Currency("gold", "--gold", "золото", "🪙", _gold_balance, _grant_gold),
    Currency("rubies", "--rubies", "рубины", "💎", _rubies_balance, _grant_rubies),
    Currency(
        "farm_tickets", "--farm-tickets", "фермерские билеты", "🎟️",
        _farm_ticket_balance, _grant_farm_tickets,
    ),
    Currency(
        "dungeon_tickets", "--dungeon-tickets", "билеты подземелья", "🎫",
        _dungeon_ticket_balance, _grant_dungeon_tickets,
    ),
)


def _default_reason(who: str, amounts: dict) -> str:
    parts = ":".join(f"{key}={amounts[key]}" for key in amounts)
    return f"manual:{who}:{parts}"


def _make_console_utf8_safe() -> None:
    """Best-effort UTF-8 stdout/stderr, so the Cyrillic + emoji output below survives a
    default Windows console (cp1251 etc.) instead of crashing on the first 💎. Best
    effort: a stream that is not a real TextIOWrapper is left alone rather than raising."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("who", help="имя существа, владельца или @username")
    parser.add_argument("--gold", type=int, default=None, help="сколько золота начислить")
    parser.add_argument("--rubies", type=int, default=None, help="сколько рубинов начислить")
    parser.add_argument("--farm-tickets", type=int, default=None, help="сколько фермерских билетов начислить")
    parser.add_argument("--dungeon-tickets", type=int, default=None, help="сколько билетов подземелья начислить")
    parser.add_argument(
        "--reason", default=None,
        help="ключ начисления; повторный запуск с тем же ключом и теми же суммами ничего не добавит",
    )
    parser.add_argument("--yes", action="store_true", help="действительно начислить")
    return parser


def main(argv: list[str] | None = None) -> int:
    _make_console_utf8_safe()
    parser = build_parser()
    args = parser.parse_args(argv)

    provided = [currency for currency in CURRENCIES if getattr(args, currency.attr) is not None]
    if not provided:
        print(
            "Укажи хотя бы одну сумму: --gold, --rubies, --farm-tickets или --dungeon-tickets.",
            file=sys.stderr,
        )
        return 2
    invalid = [currency for currency in provided if getattr(args, currency.attr) <= 0]
    if invalid:
        names = ", ".join(currency.flag for currency in invalid)
        print(f"Сумма должна быть больше нуля: {names}.", file=sys.stderr)
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
    print(f"Нашёл: {record.get('name')} (владелец {record.get('owner_name')})")
    print(f"  чат {entry} · игрок {user_id}")

    amounts = {currency.attr: int(getattr(args, currency.attr)) for currency in provided}
    reason = args.reason or _default_reason(args.who, amounts)

    befores = {currency.attr: currency.balance(entry, user_id) for currency in provided}
    for currency in provided:
        print(f"  {currency.emoji} {currency.label}: сейчас {befores[currency.attr]}")

    if not args.yes:
        want = ", ".join(
            f"{currency.emoji} +{amounts[currency.attr]} {currency.label}" for currency in provided
        )
        print(f"Начислить {want}? Запусти ещё раз с --yes (ключ «{reason}»).")
        if any(currency.attr == "gold" for currency in provided):
            print(
                "Золото выше — это бонусный остаток без XP-части /stat: у офлайн-скрипта "
                "нет живого чата, чтобы посчитать XP."
            )
        return 0

    for currency in provided:
        currency.grant(entry, user_id, amounts[currency.attr], f"{reason}:{currency.attr}")

    print(f"Готово (ключ «{reason}»):")
    for currency in provided:
        before = befores[currency.attr]
        after = currency.balance(entry, user_id)
        if after == before:
            print(f"  {currency.emoji} {currency.label}: ключ уже использован — без изменений ({after}).")
        else:
            print(f"  {currency.emoji} {currency.label}: {before} → {after}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
