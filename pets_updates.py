"""Small, append-only release notes for the pet arena.

Shipped notes live in code so a released entry can never be silently rewritten per chat.
Alongside them, an admin can append an entry from Telegram with "/arenanews" -- those are
persisted per chat, because they are written after the deploy that would otherwise be
their only home.  Both kinds share one chronological numbering, and the last release
opened by each member is persisted next to them.  That keeps the red dot personal while
keeping the shipped part of the changelog the same for everyone.
"""

from __future__ import annotations

import json
import secrets
import threading
from dataclasses import dataclass

import app_time
import stats


# v1 stored only read checkpoints. v2 adds chat-authored entries; a v1 file simply loads
# with none, so no migration step is needed.
STORE_VERSION = 2
_lock = threading.RLock()

# A note is one Telegram screen, not an article: the view sends title and body in a single
# message, and Telegram hard-caps that at 4096 characters.
MAX_TITLE_LENGTH = 100
MAX_TEXT_LENGTH = 2000


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
    Update(
        "202608-battle-log-image",
        "📜 Картинка с ходом боя",
        "К результату боя теперь прикладывается вторая картинка с раскладом по раундам: удары атакующего красные, защищающегося — синие, видно урон, эффекты и остаток HP.",
    ),
    Update(
        "202608-farm-shifts",
        "🌾 Смена на ферме — от 1 до 8 часов",
        "Теперь ты сам выбираешь, сколько питомец пробудет на ферме: восемь кнопок от 1 до 8 часов. "
        "Чем длиннее смена, тем больше монет и опыта за час, тем выше шанс находки и тем ценнее она может оказаться. "
        "Легендарное оружие с фермы выпадает только со смен в 7 и 8 часов.\n\n"
        "Смену можно прервать в любой момент кнопкой «Забрать сейчас»: заплатят за целые отработанные часы — "
        "по ставке короткой смены, так что уходить раньше срока невыгодно. Меньше часа работы награды не даёт.\n\n"
        "И главное: ферма больше не убежище. Пока питомец работает, сам он в бой не пойдёт, "
        "но напасть на него теперь можно.",
    ),
    Update(
        "202608-shop-prices",
        "🛒 Честные цены на снаряжение",
        "Амулеты, перчатки и сапоги остались с прошлой экономики и стоили до 1100 монет — теперь они стоят "
        "столько же, сколько оружие сопоставимой силы: от 10 до 170 монет.\n\n"
        "Заодно починили витрину: раньше на первых страницах амулетов, перчаток и сапог были одни трофеи "
        "«только из боёв», и казалось, что в магазине продаётся только оружие. Теперь то, что можно купить, "
        "всегда стоит первым.",
    ),
)


def _path(entry: str):
    return stats._stats_dir() / f"{stats._cache_key(entry)}_pets_updates.json"


def _empty() -> dict:
    return {"version": STORE_VERSION, "read": {}, "custom": []}


def _clean_custom(raw) -> list[dict]:
    """Keep only rows that can actually be rendered as an entry.

    A row missing its id would break the read checkpoint it is supposed to anchor, so a
    damaged one is dropped rather than shown under an invented identity.
    """
    rows = []
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("id") or "").strip()
        title = str(row.get("title") or "").strip()
        text = str(row.get("text") or "").strip()
        if not code or not (title or text):
            continue
        rows.append({
            "id": code,
            "title": title,
            "text": text,
            "created_at": str(row.get("created_at") or ""),
            "author_id": str(row.get("author_id") or ""),
        })
    return rows


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
    return {
        "version": STORE_VERSION,
        "read": {str(key): str(value) for key, value in read.items()},
        "custom": _clean_custom(data.get("custom")),
    }


def _save(entry: str, data: dict) -> None:
    data["version"] = STORE_VERSION
    stats._write_json_atomic(_path(entry), data)


def _as_update(row: dict) -> Update:
    return Update(row["id"], row["title"], row["text"])


def custom(entry: str) -> tuple[Update, ...]:
    """This chat's own "/arenanews" entries, oldest first."""
    with _lock:
        return tuple(_as_update(row) for row in _load(entry)["custom"])


def all_updates(entry: str) -> tuple[Update, ...]:
    """The whole log, oldest first: everything shipped, then everything written in chat.

    Chat-authored entries always sort after the shipped ones rather than by timestamp.
    They can only be written after the deploy that shipped the last code entry, so this IS
    chronological -- and it keeps a deploy from silently reordering a log players have
    already read up to.
    """
    return UPDATES + custom(entry)


def add(entry: str, title: str, text: str, author_id=None) -> Update:
    """Append one chat-authored entry and return it.

    Raises ValueError on empty input; the caller is a Telegram command, so the message is
    the user-facing complaint. Overlong input is truncated rather than rejected -- losing
    a tail is friendlier than losing the whole note somebody just typed out.
    """
    title = " ".join(str(title or "").split())[:MAX_TITLE_LENGTH].strip()
    text = str(text or "").strip()[:MAX_TEXT_LENGTH].strip()
    if not title and not text:
        raise ValueError("an update needs a title or a body")
    # Timestamped rather than sequential: an id must stay unique even if a row is ever
    # hand-removed from the store, because it is a durable per-member read checkpoint.
    created = app_time.now()
    row = {
        "id": f"chat-{created.strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(2)}",
        "title": title,
        "text": text,
        "created_at": created.isoformat(),
        "author_id": str(author_id or ""),
    }
    with _lock:
        data = _load(entry)
        data["custom"].append(row)
        _save(entry, data)
    return _as_update(row)


def latest(entry: str) -> Update | None:
    log = all_updates(entry)
    return log[-1] if log else None


def page(entry: str, page: int) -> tuple[Update | None, int, int]:
    """Newest-first entry at a safely clamped page, plus page and total."""
    log = all_updates(entry)
    total = len(log)
    if not total:
        return None, 0, 0
    index = min(max(0, int(page)), total - 1)
    return log[-1 - index], index, total


def has_unread(entry: str, user_id) -> bool:
    newest = latest(entry)
    if newest is None:
        return False
    with _lock:
        return _load(entry)["read"].get(str(user_id)) != newest.id


def mark_latest_read(entry: str, user_id) -> None:
    """Persist that this member has opened the log, regardless of page navigated to."""
    newest = latest(entry)
    if newest is None:
        return
    with _lock:
        data = _load(entry)
        if data["read"].get(str(user_id)) == newest.id:
            return
        data["read"][str(user_id)] = newest.id
        _save(entry, data)
