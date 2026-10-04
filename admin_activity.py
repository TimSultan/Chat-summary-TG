"""Who actually played, and when. Read-only.

WHY THIS EXISTS. "Is the game dead?" is a question the bot cannot answer about itself:
every surface it has shows one player their own progress, and the /audit page shows money
rather than people. Deciding whether to keep serving a Mini App that costs ~17 MB of the
process (see GAME_ENABLED in bot_listener.py) needs a headcount, and a headcount needs the
store.

    python admin_activity.py                      last 7 days, against the 7 before
    python admin_activity.py --days 14            a fortnight per bucket instead
    python admin_activity.py --chat "Some Chat"   one chat rather than every tracked one

RUN IT WHERE THE DATA LIVES -- the deployed volume, not a developer checkout, which holds
nothing but whatever was typed into it during testing. It opens the stores read-only and
writes nothing, so it is safe to run against a live bot.

WHAT IT COUNTS, and what each thing is worth:

  economy events  Every gold movement in the game passes through economy's log -- fights,
                  quests, purchases, farm income, the daily bonus. It is the broadest
                  "somebody did something" signal there is, and the `reason` on each row
                  says what. Capped at economy.LOG_LIMIT rows, which the report flags if
                  it is ever reached, because a full log silently hides the older bucket.
  arena fights    PvP only, from the fight log's own sidecar file.
  new creatures   Taming, from each pet's created_at. The clearest "new player" signal.
  quest work      Submissions and reviews, which is the half that survives GAME_ENABLED=0
                  and therefore the half worth watching after the game is closed.

The stores are read as plain JSON rather than through pets.py, deliberately: importing
that module pulls in the entire game (~68 MB) for a report that needs four dictionaries.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import datetime, timedelta

import economy
import stats
from app_time import now as app_now


def _store(entry: str, suffix: str) -> dict:
    """One of the game's JSON stores, or an empty dict if it was never written."""
    path = stats._stats_dir() / f"{stats._cache_key(entry)}_{suffix}.json"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _fight_rows(entry: str) -> list[dict]:
    path = stats._stats_dir() / f"{stats._cache_key(entry)}_pets_fights.json"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        loaded = []
    rows = loaded if isinstance(loaded, list) else []
    # A store written before the sidecar move still carries its fights inline.
    inline = _store(entry, "pets").get("fights")
    if isinstance(inline, list):
        rows = inline + rows
    return [row for row in rows if isinstance(row, dict)]


def _moment(row: dict, *fields: str) -> datetime | None:
    """The first parseable timestamp among `fields`.

    Several of these records grew a timestamp at different times under different names
    (`at`, `ts`, `created_at`), and a row whose stamp cannot be read is dropped rather
    than counted into the wrong week.
    """
    for field in fields:
        raw = row.get(field)
        if not isinstance(raw, str) or not raw:
            continue
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=app_now().tzinfo)
    return None


def _bucket(moment: datetime | None, recent_from: datetime, previous_from: datetime) -> int:
    """0 for the recent window, 1 for the one before it, -1 for older or unreadable."""
    if moment is None:
        return -1
    if moment >= recent_from:
        return 0
    if moment >= previous_from:
        return 1
    return -1


def report(entry: str, days: int) -> str:
    now = app_now()
    recent_from = now - timedelta(days=days)
    previous_from = now - timedelta(days=days * 2)

    def counters():
        return [Counter(), Counter()], [set(), set()]

    economy_events, economy_people = counters()
    reasons = Counter()
    fights, fighters = counters()
    tamed, _unused_tamers = counters()
    submissions, submitters = counters()
    reviews, _unused_reviewers = counters()

    economy_store = _store(entry, "economy")
    log = [row for row in (economy_store.get("log") or []) if isinstance(row, dict)]
    for row in log:
        slot = _bucket(_moment(row, "ts"), recent_from, previous_from)
        if slot < 0:
            continue
        economy_events[slot]["n"] += 1
        economy_people[slot].add(str(row.get("user_id") or ""))
        if slot == 0:
            reasons[str(row.get("reason") or "?")] += 1

    for row in _fight_rows(entry):
        slot = _bucket(_moment(row, "at", "ts"), recent_from, previous_from)
        if slot < 0:
            continue
        fights[slot]["n"] += 1
        for side in ("attacker_id", "defender_id"):
            if row.get(side):
                fighters[slot].add(str(row[side]))

    pets_store = _store(entry, "pets")
    for pet in (pets_store.get("pets") or {}).values():
        if not isinstance(pet, dict):
            continue
        slot = _bucket(_moment(pet, "created_at"), recent_from, previous_from)
        if slot >= 0:
            tamed[slot]["n"] += 1

    quest_store = _store(entry, "quests")
    rows = [row for row in (quest_store.get("submissions") or []) if isinstance(row, dict)]
    pending = sum(1 for row in rows if row.get("status") == "pending")
    for row in rows:
        slot = _bucket(_moment(row, "ts"), recent_from, previous_from)
        if slot >= 0:
            submissions[slot]["n"] += 1
            submitters[slot].add(str(row.get("user_id") or ""))
        slot = _bucket(_moment(row, "reviewed_at"), recent_from, previous_from)
        if slot >= 0:
            reviews[slot]["n"] += 1

    # Anybody who showed up in any of the surfaces above, which is the closest thing to a
    # player count the stores can give.
    active = [
        economy_people[i] | fighters[i] | submitters[i] for i in (0, 1)
    ]

    label_recent = f"last {days}d"
    label_previous = f"prev {days}d"
    lines = [
        f"{entry}",
        f"  {'':30s}{label_recent:>10s}{label_previous:>10s}",
        f"  {'people active (any of below)':30s}{len(active[0]):>10d}{len(active[1]):>10d}",
        f"  {'economy events':30s}{economy_events[0]['n']:>10d}{economy_events[1]['n']:>10d}",
        f"  {'arena fights':30s}{fights[0]['n']:>10d}{fights[1]['n']:>10d}",
        f"  {'creatures tamed':30s}{tamed[0]['n']:>10d}{tamed[1]['n']:>10d}",
        f"  {'quest submissions':30s}{submissions[0]['n']:>10d}{submissions[1]['n']:>10d}",
        f"  {'quest reviews':30s}{reviews[0]['n']:>10d}{reviews[1]['n']:>10d}",
    ]
    if pending:
        lines.append(f"  ({pending} submission(s) still waiting for a moderator)")
    if not log:
        lines.append("  no economy store here at all -- wrong DATA_DIR, or nothing ever happened")
    elif len(log) >= economy.LOG_LIMIT:
        lines.append("  WARNING: the economy log is at its cap, so the older bucket is understated")
    if reasons:
        lines.append(f"  what they did ({label_recent}):")
        for reason, count in reasons.most_common(8):
            lines.append(f"    {reason:40s}{count:>6d}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=7, help="days per bucket (default 7)")
    parser.add_argument("--chat", action="append", default=None,
                        help="a LISTENER_ALLOWED_CHATS entry; repeatable, defaults to all")
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be at least 1")

    chats = args.chat or [
        chat.strip() for chat in os.getenv("LISTENER_ALLOWED_CHATS", "").split(",")
        if chat.strip()
    ]
    if not chats:
        parser.error(
            "no chats: set LISTENER_ALLOWED_CHATS or pass --chat. Run this on the "
            "deployed volume, where DATA_DIR points at the real stores."
        )

    print(f"data directory: {stats._stats_dir()}")
    print(f"now: {app_now().isoformat(timespec='seconds')}\n")
    for entry in chats:
        print(report(entry, args.days))
        print()


if __name__ == "__main__":
    main()
