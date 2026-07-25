"""The member-facing "личный кабинет": an inline-button menu in the bot's DM where
somebody can read their own stats, browse their works and badges, and spend coins.

Everything here is PURE -- each view is a function returning `(text, keyboard)` and
touches nothing but the local stats/economy stores. All Telegram I/O, and the one async
step (resolving whose cabinet this is), lives in bot_listener.py. That split is what
makes the whole menu testable without a bot token, a network, or a fake Telegram.

Callback data is `cab:<owner_id>:<action>[:<argument>]` -- the owner id travels inside
the button rather than in a server-side session, so the menu keeps working across a
process restart (unlike the admin badge flows, which are short-lived by design). Only the
two steps that need free text back from the member -- setting a title, sending coins --
hold temporary state, because a force-reply prompt has nothing else to correlate against.

Every view is rendered with Telegram's HTML parse mode, so anything user-controlled (a
display name, a bought title, another member's name) must go through html.escape.
"""

from datetime import date
from html import escape

import economy
import stats

CALLBACK_PREFIX = "cab"
# Telegram caps callback_data at 64 bytes. "cab:" + a 19-digit id + ":" + the longest
# action + ":" + an item code stays comfortably inside that.
MAX_CALLBACK_BYTES = 64

BACK_BUTTON = "◀️ Назад"

# Works are linked as compact numbers (see stats.format_stat); a DM view can afford more
# of them than a group reply, but not an unbounded wall.
WORKS_SHOWN = 30


def callback_data(owner_id, action: str, argument: str = "") -> str:
    parts = [CALLBACK_PREFIX, str(owner_id), action]
    if argument:
        parts.append(argument)
    return ":".join(parts)


def parse_callback(data: str) -> tuple[str, str, str] | None:
    """(owner_id, action, argument) or None when this isn't a cabinet button."""
    parts = (data or "").split(":")
    if len(parts) < 3 or parts[0] != CALLBACK_PREFIX:
        return None
    return parts[1], parts[2], parts[3] if len(parts) > 3 else ""


def _back_row(owner_id) -> list:
    return [{"text": BACK_BUTTON, "callback_data": callback_data(owner_id, "main")}]


def _money(amount: int) -> str:
    return f"{amount:,}".replace(",", ".")


def main_view(
    entry: str, user: stats.UserStats, xp: int, rank: int, total: int, streak: int,
    season_xp: int | None = None, can_manage_badges: bool = False,
) -> tuple[str, dict]:
    """The landing screen: a compact identity card plus the section buttons.

    The level is scored on SEASON XP (see stats.season_bounds); `xp` remains the
    all-time total that rank and coins come from. `season_xp` defaults to `xp` so
    callers that have not been updated still render a coherent card.
    """
    level_xp = xp if season_xp is None else season_xp
    level = stats.chat_level(level_xp)
    bar = stats.progress_bar(stats.chat_level_progress(level_xp))
    coins = economy.balance(entry, user.user_id, xp)
    title = economy.active_title(entry, user.user_id)
    freezes = economy.streak_freezes(entry, user.user_id)

    lines = [f"👤 <b>Личный кабинет</b>\n", f"{escape(user.display_name)}"]
    if title:
        lines.append(f"«{escape(title)}»")
    lines.append("")
    lines.append(f"🧩 {escape(level.label)}  {bar}")
    lines.append(f"🗓️ {escape(stats.season_label(date.today()))}")
    lines.append(f"🪙 Монеты: {_money(coins)}")
    lines.append(f"📈 Место в рейтинге: {rank} из {total}")
    if streak > 0:
        lines.append(f"🔥 Серия: {stats._ru_days(streak)}")
    if freezes:
        lines.append(f"❄️ Заморозок в запасе: {freezes}")

    rows = [
        [
            {"text": "📊 Статистика", "callback_data": callback_data(user.user_id, "stats")},
            {"text": "🏪 Магазин", "callback_data": callback_data(user.user_id, "shop")},
        ],
        [
            {"text": "🎨 Мои работы", "callback_data": callback_data(user.user_id, "works")},
            {"text": "🏅 Значки", "callback_data": callback_data(user.user_id, "badges")},
        ],
        [{"text": "✏️ Титул", "callback_data": callback_data(user.user_id, "title")}],
    ]
    # Only drawn for somebody an administrator delegated with /badgeadmin (or a hardcoded
    # delegate). The button is NOT the permission check -- handle_cabinet_callback
    # re-verifies before acting, so a menu left open after a revoke cannot still act.
    if can_manage_badges:
        rows.append([{
            "text": "🛠️ Выдать значок участнику",
            "callback_data": callback_data(user.user_id, "badge_admin"),
        }])
    rows.append([{"text": "🔄 Обновить", "callback_data": callback_data(user.user_id, "main")}])
    return "\n".join(lines), {"inline_keyboard": rows}


def stats_view(
    entry: str,
    user: stats.UserStats,
    xp: int,
    rank: int,
    total: int,
    streak: int,
    figurine_links: list[str] | None,
    custom_badges: list,
    best_work_link: str | None,
    workplace_link: str | None,
    season_xp: int | None = None,
) -> tuple[str, dict]:
    """The same /stat card the group sees -- deliberately identical, so the cabinet never
    becomes a second, subtly different source of truth for somebody's numbers."""
    text = stats.format_stat(
        user, rank, total, xp, streak,
        figurine_links=figurine_links,
        custom_badges=custom_badges,
        best_work_link=best_work_link,
        workplace_link=workplace_link,
        season_xp=season_xp,
        **economy.stat_extras(entry, user.user_id, xp),
    )
    return text, {"inline_keyboard": [_back_row(user.user_id)]}


def shop_view(entry: str, user: stats.UserStats, xp: int, notice: str = "") -> tuple[str, dict]:
    """The catalogue, with one button per item. An item that cannot be bought right now
    still gets a button: pressing it explains why, which is more useful than a dead row
    the member cannot interrogate."""
    coins = economy.balance(entry, user.user_id, xp)
    lines = ["🏪 <b>Магазин</b>\n"]
    if notice:
        lines.append(f"{notice}\n")
    lines.append(f"🪙 У тебя: {_money(coins)}\n")

    rows = []
    for item in economy.SHOP_ITEMS:
        remaining = economy.cooldown_remaining(entry, user.user_id, item)
        if remaining is not None:
            mark = "⏳"
        elif coins >= item.price:
            mark = "✅"
        else:
            mark = "🔒"
        lines.append(f"{mark} <b>{escape(item.name)}</b> — {item.price}")
        lines.append(f"{escape(item.description)}\n")
        rows.append([{
            "text": f"{mark} {item.name} — {item.price}",
            "callback_data": callback_data(user.user_id, "buy", item.code),
        }])

    rows.append(_back_row(user.user_id))
    return "\n".join(lines), {"inline_keyboard": rows}


def works_view(
    entry: str,
    user: stats.UserStats,
    figurine_links: list[str],
    best_work_link: str | None,
    workplace_link: str | None,
    notice: str = "",
) -> tuple[str, dict]:
    """One line per work, so the position stays visible next to its name.

    A member renames by position ("3 Дредноут"), but the name is stored against that
    work's message_id (see stats.set_work_name) -- positions shift when a work is
    deleted, and a name must follow the work rather than the slot it happened to sit in.
    """
    names = stats.work_names_for_user(entry, user.user_id)
    lines = ["🎨 <b>Мои работы</b>\n"]
    if notice:
        lines.append(f"{notice}\n")
    if workplace_link:
        lines.append(f'🛠️ Рабочее место: <a href="{escape(workplace_link, quote=True)}">ссылка</a>')
    if best_work_link:
        lines.append(f'💎 Моя лучшая: <a href="{escape(best_work_link, quote=True)}">ссылка</a>')
    if workplace_link or best_work_link:
        lines.append("")

    lines.append(f"Фигурок засчитано: {user.figurines_painted} ({stats.FIGURINE_HASHTAG})\n")

    rows = []
    if figurine_links:
        for index, link in enumerate(figurine_links[:WORKS_SHOWN], start=1):
            message_id = message_id_for_position(user, index)
            name = names.get(str(message_id)) if message_id is not None else None
            label = escape(name) if name else "<i>без названия</i>"
            lines.append(f'{index}. <a href="{escape(link, quote=True)}">{label}</a>')
        if len(figurine_links) > WORKS_SHOWN:
            lines.append(f"\n…и ещё {len(figurine_links) - WORKS_SHOWN}.")
        lines.append(f"\nНазвание — до {stats.WORK_NAME_MAX_CHARS} символов.")
        rows.append([
            {"text": "✏️ Переименовать",
             "callback_data": callback_data(user.user_id, "work_rename")},
            {"text": "🗑 Удалить",
             "callback_data": callback_data(user.user_id, "work_delete")},
        ])
    else:
        lines.append(f"Пока пусто — выложи работу с {stats.FIGURINE_HASHTAG}.")

    rows.append(_back_row(user.user_id))
    return "\n".join(lines), {"inline_keyboard": rows}


def message_id_for_position(user: stats.UserStats, position: int) -> int | None:
    """The message_id behind the 1-based position this view and /stat display.

    recent_figurine_posts is newest-first and already has deleted works removed
    (_apply_deleted_figurines), so position N here is the same work /stat calls N.
    """
    posts = user.recent_figurine_posts
    if 1 <= position <= len(posts):
        return posts[position - 1][1]
    return None


def parse_rename_request(text: str) -> tuple[int, str] | None:
    """"3 Дредноут" -> (3, "Дредноут"). A bare number clears that work's name."""
    parts = (text or "").strip().split(maxsplit=1)
    if not parts:
        return None
    try:
        position = int(parts[0])
    except ValueError:
        return None
    if position < 1:
        return None
    return position, (parts[1] if len(parts) > 1 else "")


def confirm_work_delete_view(owner_id, position: int, name: str | None, message_id: int):
    """Ask before deleting. Removing a work is irreversible -- it writes a permanent
    tombstone so a stale transcript cannot restore it -- and costs the member 200 XP,
    which can drop their level and a painting badge with it. That is far too much to
    hang off a single mistaken tap, so the number they typed is read back to them first.

    The message_id, not the position, rides in the confirm button: positions renumber
    when a work is deleted, and by the time the second tap arrives this member may have
    deleted another one from a different screen.
    """
    label = f"«{escape(name)}»" if name else "без названия"
    text = (
        f"🗑 <b>Удалить работу №{position}</b> — {label}?\n\n"
        f"Это уберёт {stats.XP_PER_FIGURINE} XP и одну фигурку. Отменить будет нельзя."
    )
    keyboard = {"inline_keyboard": [
        [{"text": "🗑 Да, удалить",
          "callback_data": callback_data(owner_id, "work_delete_ok", str(message_id))}],
        [{"text": "◀️ Отмена", "callback_data": callback_data(owner_id, "works")}],
    ]}
    return text, keyboard


def badges_view(
    entry: str,
    user: stats.UserStats,
    custom_badges: list,
    chat_custom_badge_total: int = 0,
) -> tuple[str, dict]:
    """Hand-made badges first, then earned ones, then a completion counter.

    They lead because they are the only ones somebody chose to give this person; buried
    among a dozen automatic counters that is exactly what gets lost. The split is on
    Badge.custom, which is what that flag exists for -- a weekly-contest win is assigned
    by an administrator but is still earned, so it stays below.
    """
    given = [badge for badge in (custom_badges or []) if getattr(badge, "custom", False)]
    other_awarded = [badge for badge in (custom_badges or []) if not getattr(badge, "custom", False)]
    earned = stats.earned_badges(user) + other_awarded

    lines = ["🏅 <b>Значки</b>"]
    if given:
        lines.append("\n✨ <b>Уникальные значки</b>")
        for badge in given:
            lines.append(escape(badge.label))
    if earned:
        lines.append("\n<b>Заработанные</b>")
        for badge in earned:
            description = f" — {escape(badge.description)}" if badge.description else ""
            lines.append(f"{escape(badge.label)}{description}")
    if not given and not earned:
        lines.append("\nПока ни одного.")

    unlocked, total = stats.badge_collection_progress(
        user, custom_badges=custom_badges, chat_custom_badge_total=chat_custom_badge_total
    )
    lines.append(f"\n📦 Открыто: {unlocked} из {total}")
    lines.append("<i>считая уровни чата и звания художника</i>")
    return "\n".join(lines), {"inline_keyboard": [_back_row(user.user_id)]}


def title_view(entry: str, user: stats.UserStats, xp: int, notice: str = "") -> tuple[str, dict]:
    item = economy.find_item("title")
    current = economy.active_title(entry, user.user_id)
    coins = economy.balance(entry, user.user_id, xp)

    lines = ["✏️ <b>Титул</b>\n"]
    if notice:
        lines.append(f"{notice}\n")
    if current:
        lines.append(f"Сейчас: «{escape(current)}»\n")
    else:
        lines.append("Сейчас титула нет.\n")
    lines.append(
        f"Свой титул показывается в /stat и в кабинете {economy.TITLE_DAYS} дней.\n"
        f"Цена: {item.price} монет. У тебя: {_money(coins)}."
    )

    rows = []
    if coins >= item.price:
        label = "✏️ Сменить титул" if current else "✏️ Купить титул"
        rows.append([{"text": label, "callback_data": callback_data(user.user_id, "title_set")}])
    rows.append(_back_row(user.user_id))
    return "\n".join(lines), {"inline_keyboard": rows}


def welcome_view(user_id) -> tuple[str, dict]:
    """Shown to somebody the stats don't know yet.

    They have no balance, level or works to render, so offering the full board would be
    six buttons leading to six empty screens. One honest sentence and a retry is better.
    """
    text = (
        "👋 <b>Привет!</b>\n\n"
        "Я веду статистику чата: уровни, монеты, значки и работы.\n"
        "Тебя я пока не вижу — напиши что-нибудь в чат, и всё появится."
    )
    keyboard = {"inline_keyboard": [[
        {"text": "🔄 Проверить снова", "callback_data": callback_data(user_id, "main")}
    ]]}
    return text, keyboard


def result_view(owner_id, text: str) -> tuple[str, dict]:
    """A one-off outcome screen (a purchase, a transfer) that always offers the way back
    -- a member must never end up on a leaf with no route to the rest of the cabinet."""
    return text, {"inline_keyboard": [_back_row(owner_id)]}


