"""Rendered samples of every message the bot posts on a schedule or exactly once, so an
admin can look at all of them in a DM without waiting for 10:00, without collecting a
single reaction, and without planting anything.

Only messages that are HARD to trigger live here. /stat, /top, /shop and the cabinet are
already one command away, so previewing them would just be a second way to run them.
What is in here is the tree: posts that fire from a scheduler, or once in the lifetime of
the chat, or only when nobody reacted -- the ones that are otherwise impossible to look at
before the chat does.

Everything is PURE: fixed sample cast, fixed sample numbers, no stats store, no Telegram,
no clock beyond the day passed in. That matters because a preview built on live state
would be a second code path that could quietly drift from the real one, and the entire
point of this module is that what an admin sees here is what the chat gets. Every builder
calls the same formatter the scheduler calls.

Rendered previews go to the admin's DM. Nothing here can post to the chat.
"""

from datetime import date

import tree

CALLBACK_PREFIX = "prev"

# The one preview that does NOT render into the DM: it posts the ceremony message, button
# and all, into the real chat, because a button can only really be judged where it will
# live. Handled separately in bot_listener (it needs the chat, and an undo) rather than
# through PREVIEWS, which is pure by construction.
GROUP_TEST_ID = "test_button"
GROUP_TEST_TITLE = "🧨 Отправить тест в общий чат"

# Carried by every sample planting button -- the one on a DM preview and the one on the
# test post in the chat alike. It is deliberately NOT the same payload as the menu's
# "send to the chat" button: those two look identical on screen, and reusing one payload
# meant an admin tapping the button on a DM sample, to see what it does, would fire a real
# post at 190 people. This one never does anything but say "это тест".
SAMPLE_BUTTON_ID = "sample"
SAMPLE_CALLBACK = f"{CALLBACK_PREFIX}:{SAMPLE_BUTTON_ID}"

# A fixed cast, so a preview looks the same every time and two admins comparing notes are
# looking at the same thing. The first entry is a real handle (the bot's owner) and the
# rest are display names without one -- that mix is deliberate, it exercises both branches
# of _planter_names, and it means no preview ever links to a stranger's profile.
SAMPLE_PLANTERS = (
    ("Sultan", "sultan_kembayev"),
    ("Дзура Кацура", None),
    ("Кирилл", None),
    ("Мария", None),
    ("Антон", None),
    ("Вера", None),
    ("Пётр", None),
    ("Елена", None),
    ("Никита", None),
    ("Ольга", None),
    ("Роман", None),
    ("Светлана", None),
    ("Дмитрий", None),
    ("Юлия", None),
    ("Сергей", None),
    ("Екатерина", None),
    ("Михаил", None),
    ("Настя", None),
    ("Павел", None),
    ("Алина", None),
    ("Глеб", None),
    ("Даша", None),
    ("Артём", None),
    ("Полина", None),
)

SAMPLE_CONTRIBUTORS = (
    ("Sultan", "sultan_kembayev", 420),
    ("Дзура Кацура", None, 305),
    ("Кирилл", None, 288),
    ("Мария", None, 140),
)

# ~21 см of tree: a few weeks in, still small enough that a day's growth is visible.
SAMPLE_TOTAL_XP = 42_000
# 7 м: far enough along to show how format_length switches to metres and how a stage name
# reads once the tree is no longer a sprout.
SAMPLE_GROWN_TOTAL_XP = 1_400_000
# The chat's own measured output, ~3,600 XP/day -- so the growth number in a preview is
# the one an ordinary day actually produces.
SAMPLE_DAY_XP = 3_600


def _sample_day(day: date | None) -> date:
    return day or date.today()


# (id, menu title, builder, carries the planting button). The id travels in callback_data
# and in "/preview <id>", so it stays short and ASCII.
PREVIEWS = (
    (
        "seed",
        "🌰 Посадка — приглашение",
        lambda day: tree.format_seed_ceremony_message(),
        True,
    ),
    (
        "seedtoday",
        "🌰 Посадка — приглашение (сегодня в 10:00)",
        lambda day: tree.format_seed_ceremony_message(same_day=True),
        True,
    ),
    (
        "rollcall",
        "🌱 Посадка — перекличка в 10:00",
        lambda day: tree.format_planting_roll_call(list(SAMPLE_PLANTERS)),
        False,
    ),
    (
        "rollcallone",
        "🌱 Посадка — перекличка, сажал один",
        lambda day: tree.format_planting_roll_call(list(SAMPLE_PLANTERS[:1])),
        False,
    ),
    (
        "empty",
        "🌰 Посадка — никто не нажал",
        lambda day: tree.format_nobody_planted_message(),
        False,
    ),
    (
        "waiting",
        "🌳 /tree во время посадки",
        lambda day: tree.format_awaiting_planting_status(),
        False,
    ),
    (
        "morning",
        "☀️ Утренний пост (молодое дерево)",
        lambda day: tree.format_morning_digest(
            SAMPLE_TOTAL_XP, SAMPLE_DAY_XP, list(SAMPLE_CONTRIBUTORS), _sample_day(day)
        ),
        False,
    ),
    (
        "morningbig",
        "☀️ Утренний пост (выросшее дерево)",
        lambda day: tree.format_morning_digest(
            SAMPLE_GROWN_TOTAL_XP, SAMPLE_DAY_XP, list(SAMPLE_CONTRIBUTORS), _sample_day(day)
        ),
        False,
    ),
    (
        "quiet",
        "☀️ Утренний пост (тихий день, никто не писал)",
        lambda day: tree.format_morning_digest(
            SAMPLE_TOTAL_XP, 0, [], _sample_day(day)
        ),
        False,
    ),
    (
        "status",
        "🌳 /tree обычный",
        lambda day: tree.format_tree_status(
            SAMPLE_TOTAL_XP, SAMPLE_DAY_XP, list(SAMPLE_CONTRIBUTORS)
        ),
        False,
    ),
    (
        "planting",
        "🌱 Старый пост посадки (/replant)",
        lambda day: tree.format_planting_message(),
        False,
    ),
    (
        "founder",
        "🌱 Значок Основателя в /stat",
        lambda day: FOUNDER_BADGE_SAMPLE,
        False,
    ),
)

# What a planter's badge block looks like afterwards. Rendered here as a fixed sample
# rather than through stats.format_stat, which would need a real member with real history
# behind it; the point of this one is only to show where the badge lands.
FOUNDER_BADGE_SAMPLE = "\n".join([
    "Так значок будет выглядеть в <code>/stat</code> у того, кто сажал:",
    "",
    "✨ <b>Уникальные значки:</b>",
    "🌱 Основатель — посадил дерево ЕПХ",
    "",
    "🏅 <b>Значки:</b>",
    "🎨 Я покрасил 2 — покрасить 5 фигурок",
    "🖼️ Галерея — отправить 25 фото или видео",
])


def preview_ids() -> tuple:
    return tuple(preview_id for preview_id, _, _, _ in PREVIEWS)


def title_for(preview_id: str) -> str | None:
    if preview_id == GROUP_TEST_ID:
        return GROUP_TEST_TITLE
    for candidate, title, _, _ in PREVIEWS:
        if candidate == preview_id:
            return title
    return None


def render(preview_id: str, day: date | None = None) -> str | None:
    """The sample message for `preview_id`, rendered by the very same formatter the
    scheduler uses, or None when there is no such preview."""
    for candidate, _, builder, _ in PREVIEWS:
        if candidate == preview_id:
            return builder(day)
    return None


def keyboard_for(preview_id: str) -> dict | None:
    """The planting button, for the previews that carry one.

    Wired to SAMPLE_CALLBACK, so a curious tap answers "это тест" instead of falling
    through to an unhandled button -- or, worse, doing something.
    """
    for candidate, _, _, with_button in PREVIEWS:
        if candidate == preview_id:
            return tree.seed_keyboard(SAMPLE_CALLBACK) if with_button else None
    return None


def callback_data(preview_id: str) -> str:
    return f"{CALLBACK_PREFIX}:{preview_id}"


def parse_callback(data: str) -> str | None:
    """The preview id behind a button, or None when this isn't a preview button."""
    parts = (data or "").split(":")
    if len(parts) != 2 or parts[0] != CALLBACK_PREFIX:
        return None
    return parts[1] or None


def menu_view() -> tuple:
    """(text, inline keyboard) listing every preview, one per row.

    One per row rather than two because the titles are long enough in Russian that a
    two-column layout truncates them on a phone, which would defeat a menu whose only job
    is to say what each button will show.
    """
    text = "\n".join([
        "🧪 <b>Предпросмотр сообщений</b>",
        "",
        "Каждая кнопка присылает сюда, в личку, готовое сообщение — ровно в том виде,",
        "в котором его получит чат. Данные в примерах вымышленные, в чат ничего не уходит.",
        "",
        f"Последняя кнопка — исключение: она отправляет пост <b>в общий чат</b>,",
        "чтобы посмотреть кнопку там, где она будет жить. Удалить его можно одной кнопкой.",
        "",
        "Можно и командой: <code>/preview rollcall</code>",
    ])
    rows = [
        [{"text": title, "callback_data": callback_data(preview_id)}]
        for preview_id, title, _, _ in PREVIEWS
    ]
    rows.append([{"text": GROUP_TEST_TITLE, "callback_data": callback_data(GROUP_TEST_ID)}])
    return text, {"inline_keyboard": rows}


def group_test_sent_view(chat_id, message_id: int) -> tuple:
    """The DM receipt for a test post, with its own undo.

    The undo matters more than it looks: this is the only preview that lands in front of
    190 people, and hunting for the post to delete it by hand is exactly the friction
    that would stop somebody testing the button at all.
    """
    text = "\n".join([
        "Отправил тестовый пост в общий чат.",
        "",
        "Кнопка под ним рабочая, но никого не записывает — на нажатие отвечает,",
        "что это тест.",
    ])
    keyboard = {
        "inline_keyboard": [[{
            "text": "🗑 Удалить из чата",
            "callback_data": f"{CALLBACK_PREFIX}:del:{chat_id}:{message_id}",
        }]]
    }
    return text, keyboard


def parse_delete_callback(data: str) -> tuple | None:
    """(chat_id, message_id) behind the undo button, or None."""
    parts = (data or "").split(":")
    if len(parts) != 4 or parts[0] != CALLBACK_PREFIX or parts[1] != "del":
        return None
    try:
        return int(parts[2]), int(parts[3])
    except ValueError:
        return None


def unknown_preview_text() -> str:
    listed = "\n".join(f"• <code>{preview_id}</code> — {title}" for preview_id, title, _, _ in PREVIEWS)
    listed += f"\n• <code>{GROUP_TEST_ID}</code> — {GROUP_TEST_TITLE}"
    return f"Нет такого превью. Доступные:\n{listed}"
