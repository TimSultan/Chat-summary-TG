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


def main_view(entry: str, user: stats.UserStats, xp: int, rank: int, total: int, streak: int) -> tuple[str, dict]:
    """The landing screen: a compact identity card plus the section buttons."""
    level = stats.chat_level(xp)
    bar = stats.progress_bar(stats.chat_level_progress(xp))
    coins = economy.balance(entry, user.user_id, xp)
    title = economy.active_title(entry, user.user_id)
    freezes = economy.streak_freezes(entry, user.user_id)

    lines = [f"👤 <b>Личный кабинет</b>\n", f"{escape(user.display_name)}"]
    if title:
        lines.append(f"«{escape(title)}»")
    lines.append("")
    lines.append(f"🧩 {escape(level.label)}  {bar}")
    lines.append(f"🪙 Монеты: {_money(coins)}")
    lines.append(f"📈 Место в рейтинге: {rank} из {total}")
    if streak > 0:
        lines.append(f"🔥 Серия: {stats._ru_days(streak)}")
    if freezes:
        lines.append(f"❄️ Заморозок в запасе: {freezes}")

    keyboard = {
        "inline_keyboard": [
            [
                {"text": "📊 Статистика", "callback_data": callback_data(user.user_id, "stats")},
                {"text": "🏪 Магазин", "callback_data": callback_data(user.user_id, "shop")},
            ],
            [
                {"text": "🎨 Мои работы", "callback_data": callback_data(user.user_id, "works")},
                {"text": "🏅 Значки", "callback_data": callback_data(user.user_id, "badges")},
            ],
            [
                {"text": "✏️ Титул", "callback_data": callback_data(user.user_id, "title")},
                {"text": "💸 Перевод", "callback_data": callback_data(user.user_id, "send")},
            ],
            [{"text": "🔄 Обновить", "callback_data": callback_data(user.user_id, "main")}],
        ]
    }
    return "\n".join(lines), keyboard


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
) -> tuple[str, dict]:
    """The same /stat card the group sees -- deliberately identical, so the cabinet never
    becomes a second, subtly different source of truth for somebody's numbers."""
    text = stats.format_stat(
        user, rank, total, xp, streak,
        figurine_links=figurine_links,
        custom_badges=custom_badges,
        best_work_link=best_work_link,
        workplace_link=workplace_link,
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
    user: stats.UserStats,
    figurine_links: list[str],
    best_work_link: str | None,
    workplace_link: str | None,
) -> tuple[str, dict]:
    lines = ["🎨 <b>Мои работы</b>\n"]
    if workplace_link:
        lines.append(f'🛠️ Рабочее место: <a href="{escape(workplace_link, quote=True)}">ссылка</a>')
    if best_work_link:
        lines.append(f'💎 Моя лучшая: <a href="{escape(best_work_link, quote=True)}">ссылка</a>')
    if workplace_link or best_work_link:
        lines.append("")

    lines.append(f"Фигурок засчитано: {user.figurines_painted} ({stats.FIGURINE_HASHTAG})")
    if figurine_links:
        shown = figurine_links[:WORKS_SHOWN]
        numbered = " · ".join(
            f'<a href="{escape(link, quote=True)}">{index}</a>'
            for index, link in enumerate(shown, start=1)
        )
        lines.append(f"\n{numbered}")
        if len(figurine_links) > WORKS_SHOWN:
            lines.append(f"\n…и ещё {len(figurine_links) - WORKS_SHOWN}.")
    else:
        lines.append(f"\nПока пусто — выложи работу с {stats.FIGURINE_HASHTAG}.")
    return "\n".join(lines), {"inline_keyboard": [_back_row(user.user_id)]}


def badges_view(user: stats.UserStats, custom_badges: list) -> tuple[str, dict]:
    badges = stats.earned_badges(user) + list(custom_badges or [])
    lines = ["🏅 <b>Значки</b>\n"]
    if badges:
        for badge in badges:
            description = f" — {escape(badge.description)}" if badge.description else ""
            lines.append(f"{escape(badge.label)}{description}")
    else:
        lines.append("Пока ни одного.")
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


def send_view(entry: str, user: stats.UserStats, xp: int, notice: str = "") -> tuple[str, dict]:
    coins = economy.balance(entry, user.user_id, xp)
    lines = ["💸 <b>Перевод монет</b>\n"]
    if notice:
        lines.append(f"{notice}\n")
    lines.append(f"🪙 У тебя: {_money(coins)}\n")
    lines.append(
        f"Минимум {economy.MIN_TRANSFER} монет. "
        f"Комиссия {economy.TRANSFER_BURN_PERCENT}% сгорает — "
        "так монеты в чате не копятся бесконечно."
    )
    rows = []
    if coins >= economy.MIN_TRANSFER:
        rows.append([{
            "text": "💸 Отправить монеты",
            "callback_data": callback_data(user.user_id, "send_start"),
        }])
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


def parse_transfer_request(text: str) -> tuple[str, int] | None:
    """"@user 50" / "user 50" -> (target, amount). None when it isn't that shape."""
    parts = (text or "").split()
    if len(parts) < 2:
        return None
    target, raw_amount = parts[0], parts[-1]
    try:
        amount = int(raw_amount)
    except ValueError:
        return None
    if not target.strip():
        return None
    return target.strip(), amount
