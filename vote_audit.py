"""An administrators-only record of how a vote was cast, for spotting a rigged contest.

The poll file (voting.py) keeps only each voter's FINAL ballot -- who currently has a
vote for what. That is enough to announce a winner and nothing more. When a work is
pulling a suspicious number of votes, the question is a different one: *where* are those
votes coming from, and *who* is casting them -- real chat members who take part, or a
burst of accounts that show up only to vote and leave. Answering that needs the things a
single final ballot throws away: when each vote landed, how many times it changed, whether
the voter was a member at the time, and how the page was opened (a normal Mini App launch
versus a link forwarded into some other chat).

So this writes one line per ballot submission to an append-only log, beside the polls but
never read by them -- voting.py does not import this module and cannot tell it exists. The
log holds only what the bot already handles in the course of taking a vote (the Telegram
id and name Telegram's signed initData carried, the works chosen, the membership snapshot
the ballot already computes, and the chat_type/start_param Telegram itself attaches to the
launch). It does not reach for anything else: no IP, no device, no profile scraping.

Reading it is for administrators alone. The report and CSV are built in bot_listener
behind the same "real chat admin or the owner, not a delegate" gate that guards the
history backup, for the same reason -- it says who voted for whom.
"""

import csv
import json
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl

import voting

# What Telegram itself attaches to a Mini App launch, telling us where the page was opened
# from without our asking for anything. A vote cast from a private chat with the bot looks
# different from one opened through a link forwarded into a third-party chat, and a sudden
# run of the latter is exactly what organised voting looks like. All three are optional in
# initData -- a plain launch carries none of them -- so a missing value is normal, not an
# error.
SOURCE_KEYS = ("chat_type", "chat_instance", "start_param")


def _audit_dir() -> Path:
    """A subdirectory of the voting store, so it travels with a contest's records and so a
    test that redirects the voting dir (patching voting._voting_dir) redirects this too.
    Its own directory rather than beside the polls: voting.latest_poll globs
    "<key>_*.json" out of the voting dir, and living one level down is never caught by it.
    """
    return voting._voting_dir() / "audit"


def _key(entry: str) -> str:
    """The same filesystem-safe chat key voting._poll_key builds -- reused directly so a
    chat's audit and its polls key the same way."""
    return voting._poll_key(entry)


def log_path(entry: str) -> Path:
    return _audit_dir() / f"{_key(entry)}.jsonl"


# One lock per process is enough: the only writer is this module, and an append is a
# single short write. It is a threading lock rather than an asyncio one because the web
# handlers run the append on a worker thread (nominations_web) or inline under the poll
# lock (vote_web), and a threading lock serialises both.
_write_lock = threading.Lock()


def parse_source(init_data: str | None) -> dict:
    """The launch-origin fields out of a Mini App's initData.

    These are read for the log only, never for a decision: the signature was already
    checked by voting.verify_init_data on the same string before a ballot was accepted, so
    re-parsing here to pull three descriptive fields trusts nothing new. A link ballot
    (voting.decode_ballot_link, no Mini App) has no initData and gets an empty dict."""
    if not init_data:
        return {}
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    return {key: fields[key] for key in SOURCE_KEYS if fields.get(key)}


def record_vote_event(
    entry: str,
    *,
    system: str,
    poll_id: str,
    voter_id,
    name: str,
    username: str | None,
    choices: list[str],
    is_member: bool | None,
    via: str,
    source: dict | None = None,
    now: datetime | None = None,
    log=print,
) -> None:
    """Append one ballot submission to the log. Best-effort: a vote that was already saved
    must never fail because its audit line could not be written, so every error here is
    logged and swallowed rather than raised back into the ballot path.

    `via` is how the ballot arrived ("miniapp", "link", "nomination") and `system` which
    contest it belongs to ("v1", "v3") -- both kept so one log can hold more than one
    voting system without the reader having to guess.
    """
    event = {
        "ts": (now or datetime.now(timezone.utc)).isoformat(),
        "system": system,
        "poll_id": poll_id,
        "voter_id": str(voter_id),
        "name": name or "",
        "username": (username or None),
        "via": via,
        "choices": [str(c) for c in (choices or [])],
        "is_member": (None if is_member is None else bool(is_member)),
        "source": source or {},
    }
    line = json.dumps(event, ensure_ascii=False)
    try:
        directory = _audit_dir()
        directory.mkdir(parents=True, exist_ok=True)
        with _write_lock, (directory / f"{_key(entry)}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as exc:  # pragma: no cover -- disk trouble, logged not raised
        log(f"[vote_audit] could not record a vote event: {exc}")


def read_events(entry: str, *, poll_id: str | None = None, system: str | None = None) -> list[dict]:
    """Every logged submission for this chat, oldest first, optionally narrowed to one
    poll or one system. A torn or malformed last line (a crash mid-append) is skipped, not
    fatal: the log is operational context, and the authoritative ballots are in the poll
    file regardless."""
    path = log_path(entry)
    if not path.exists():
        return []
    events: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if poll_id is not None and event.get("poll_id") != poll_id:
            continue
        if system is not None and event.get("system") != system:
            continue
        events.append(event)
    return events


def voter_rollup(events: list[dict]) -> dict[str, dict]:
    """Collapse a chat's submissions to one record per voter, in the order they first
    voted. The final ballot is the last one they sent (so this agrees with the poll's own
    votes), but the count of submissions, the first and last timestamps and the launch
    origin are the parts a single final ballot cannot show -- a voter who resubmitted ten
    times in a minute is visible here and nowhere else."""
    rollup: dict[str, dict] = {}
    for event in events:
        voter_id = str(event.get("voter_id") or "")
        if not voter_id:
            continue
        record = rollup.get(voter_id)
        if record is None:
            record = rollup[voter_id] = {
                "voter_id": voter_id,
                "first_ts": event.get("ts"),
                "submissions": 0,
            }
        record["submissions"] += 1
        record["last_ts"] = event.get("ts")
        record["name"] = event.get("name") or record.get("name") or ""
        record["username"] = event.get("username") or record.get("username")
        record["choices"] = list(event.get("choices") or [])
        record["via"] = event.get("via") or record.get("via")
        record["is_member_at_vote"] = event.get("is_member")
        record["source"] = event.get("source") or {}
    return rollup


def exports_dir() -> Path:
    return _audit_dir() / "exports"


def write_csv(entry: str, poll_id: str, header: list[str], rows: list[list]) -> Path:
    """One CSV of the detail behind a report, saved under the audit tree and returned for
    sending as a document. utf-8-sig so the Cyrillic opens straight in Excel without a
    manual encoding step."""
    directory = exports_dir()
    directory.mkdir(parents=True, exist_ok=True)
    safe_poll = unicodedata.normalize("NFKC", str(poll_id)).replace("/", "_").replace(":", "_")
    path = directory / f"{_key(entry)}_{safe_poll}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return path
