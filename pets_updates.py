"""Small, append-only release notes for the pet arena.

The notes themselves live in code so a shipped entry can never be silently rewritten
per chat.  Only the last release opened by each member is persisted, next to the other
per-chat game data.  This keeps the red dot personal while keeping the actual changelog
the same for everyone.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass

import stats


STORE_VERSION = 1
_lock = threading.RLock()


@dataclass(frozen=True)
class Update:
    """One immutable, chronological release-note entry.

    Add new entries at the end.  IDs must remain stable: they are the durable read
    checkpoints saved for players who have already opened the log.
    """

    id: str
    title: str
    text: str


UPDATES: tuple[Update, ...] = (
    Update(
        "202608-farm",
        "🌾 Ферма",
        "Отправляй существо на шестичасовую смену за монетами и находками.",
    ),
    Update(
        "202608-gear",
        "🎒 Снаряжение",
        "В магазине, инвентаре и коллекции появились оружие и экипировка.",
    ),
    Update(
        "202608-fights",
        "⚔️ Запас боёв",
        "Каждый час в запас приходит 1 бой. Вместимость увеличивают клетка, ферма и свежие покрасы.",
    ),
    Update(
        "202608-private-arena",
        "🔒 Личные результаты и доступный магазин",
        "Результаты боёв теперь приходят только участникам в личку. Ограничение боя по разнице уровней убрано, а дешёвое оружие теперь стоит 10–20 монет.",
    ),
    Update(
        "202608-fight-notify-toggle",
        "🔕 Отключение уведомлений о боях",
        "В меню /arena появилась кнопка «Результаты»: ей можно выключить личные уведомления о результатах боёв.",
    ),
    Update(
        "202608-loot-rebalance",
        "🎁 Больше добычи и сильные трофеи",
        "Предметы выпадают почти вдвое чаще. Редкое и легендарное оружие стало заметно сильнее, а у всех легендарок и половины редких появились особые эффекты — они видны в бою и на карточке предмета.",
    ),
)


def _path(entry: str):
    return stats._stats_dir() / f"{stats._cache_key(entry)}_pets_updates.json"


def _empty() -> dict:
    return {"version": STORE_VERSION, "read": {}}


def _load(entry: str) -> dict:
    try:
        data = json.loads(_path(entry).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    read = data.get("read")
    if not isinstance(read, dict):
        read = {}
    return {"version": STORE_VERSION, "read": {str(key): str(value) for key, value in read.items()}}


def _save(entry: str, data: dict) -> None:
    data["version"] = STORE_VERSION
    stats._write_json_atomic(_path(entry), data)


def latest() -> Update | None:
    return UPDATES[-1] if UPDATES else None


def page(page: int) -> tuple[Update | None, int, int]:
    """Newest-first entry at a safely clamped page, plus page and total."""
    total = len(UPDATES)
    if not total:
        return None, 0, 0
    index = min(max(0, int(page)), total - 1)
    return UPDATES[-1 - index], index, total


def has_unread(entry: str, user_id) -> bool:
    newest = latest()
    if newest is None:
        return False
    with _lock:
        return _load(entry)["read"].get(str(user_id)) != newest.id


def mark_latest_read(entry: str, user_id) -> None:
    """Persist that this member has opened the log, regardless of page navigated to."""
    newest = latest()
    if newest is None:
        return
    with _lock:
        data = _load(entry)
        if data["read"].get(str(user_id)) == newest.id:
            return
        data["read"][str(user_id)] = newest.id
        _save(entry, data)
