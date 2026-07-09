"""Records who asked the listener what, and what it answered. Each entry's full answer
is written to its own file (answers can be long), with a small JSON index -- so the
GUI's History tab can list entries cheaply and link to the answer instead of showing it
inline.
"""

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

# See transcript_cache.py's DATA_DIR comment -- same optional persistent-disk hook.
DATA_DIR = Path(os.getenv("DATA_DIR", "."))
HISTORY_DIR = DATA_DIR / "history"
INDEX_PATH = HISTORY_DIR / "index.json"


@dataclass
class HistoryEntry:
    timestamp: str  # ISO 8601, local time
    chat_title: str
    requester: str
    question: str
    answer_path: str


def _safe(value: str) -> str:
    cleaned = re.sub(r"[^\w\-.]+", "_", value).strip("_")
    return cleaned or "x"


def record(chat_title: str, requester: str, question: str, answer: str) -> HistoryEntry:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()

    stamp = now.strftime("%Y%m%d_%H%M%S_%f")
    answer_path = HISTORY_DIR / f"{stamp}_{_safe(chat_title)}_{_safe(requester)}.md"
    answer_path.write_text(
        f"# Question from {requester} in {chat_title}\n\n"
        f"*{now.strftime('%Y-%m-%d %H:%M')}*\n\n"
        f"**Question:** {question}\n\n"
        "---\n\n"
        f"{answer}\n",
        encoding="utf-8",
    )

    entry = HistoryEntry(
        timestamp=now.isoformat(),
        chat_title=chat_title,
        requester=requester,
        question=question,
        answer_path=str(answer_path),
    )

    entries = load_all()
    entries.append(entry)
    INDEX_PATH.write_text(
        json.dumps([asdict(e) for e in entries], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return entry


def load_all() -> list[HistoryEntry]:
    if not INDEX_PATH.exists():
        return []
    try:
        raw = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return [HistoryEntry(**e) for e in raw]
