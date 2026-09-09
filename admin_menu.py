"""/admin -- one panel holding every management command the bot has.

WHY IT EXISTS. The management commands were deliberately never registered in Telegram's
☰ menu: advertising /badge and /deletepokras to 190 members invites a wave of "нужны
права администратора". The cost of that decision is that they are invisible to the people
who ARE allowed to use them, who have to remember a dozen spellings and their argument
order. This panel is the other half: unadvertised, admin-gated, and listing all of them in
one place.

IT DOES NOT REIMPLEMENT ANYTHING. Every button ends in the same handler the typed command
runs, with the same permission check inside it. That is the point -- a panel that grew its
own copy of "post to the chat" would drift from the command within two releases, and the
gate is the last thing that should exist twice.

THREE KINDS OF BUTTON, and which one an action gets is not a style choice:

- `open` presses through immediately. Reserved for actions whose whole effect is a screen
  in the administrator's own DM. A misclick costs them one message on their own screen.
- `ask` sends a force-reply and runs the command with whatever comes back, because the
  command needs words that only a person can supply.
- `confirm` asks "точно?" first. Everything that writes into the group chat of 190 people,
  or resets something shared, is one of these. A single stray tap must not be able to post
  an invitation to the whole chat or start the tree over.
"""

from __future__ import annotations

COMMAND = "/admin"
CALLBACK_PREFIX = "adminmenu"

# Same ten minutes the badge and cabinet flows use. Only the `ask` steps need any
# server-side state at all; every button carries its own action id, so navigating the
# panel survives a restart and only a half-typed answer is ever lost.
FLOW_TTL_SECONDS = 10 * 60

BACK_BUTTON_TEXT = "◀️ Назад"
CANCEL_BUTTON_TEXT = "✖️ Отмена"
CANCEL_WORDS = frozenset({"отмена", "cancel", "/cancel", "назад", "back"})

# Ordered, and the single definition of what the panel contains: the keyboard, the legend
# above it and the callback router all read this one tuple, so an action added here shows
# up in all three or in none.
#
# `hint` is what the legend says the command does. `prompt` is the force-reply question
# for an `ask`, and `confirm` is the question for a `confirm`; each kind uses exactly one
# of them.
ACTIONS = (
    {
        "id": "send",
        "section": "Чат и посты",
        "label": "📣 Написать в чат",
        "command": "/send",
        "kind": "ask",
        "hint": "написать в чат от имени бота",
        "prompt": "Ответьте на это сообщение текстом — он уйдёт в чат как есть.",
    },
    {
        "id": "preview",
        "section": "Чат и посты",
        "label": "👀 Превью постов",
        "command": "/preview",
        "kind": "open",
        "hint": "посмотреть плановые посты, не дожидаясь их",
    },
    {
        "id": "buttons",
        "section": "Чат и посты",
        "label": "🔘 Пост с кнопками",
        "command": "/buttons",
        "kind": "open",
        "hint": "пост с кнопками-счётчиками",
    },
    {
        "id": "via",
        "section": "Чат и посты",
        "label": "🧹 ViaCleaner",
        "command": "/viacleaner",
        "kind": "open",
        "hint": "уборка сообщений от инлайн-ботов",
    },
    {
        "id": "vote",
        "section": "Голосования",
        "label": "🗳 Голосование",
        "command": "/vote",
        "kind": "open",
        "hint": "сбор работ, модерация, итоги недели",
    },
    {
        "id": "vote2",
        "section": "Голосования",
        "label": "⚔️ Голосование арены",
        "command": "/vote2",
        "kind": "open",
        "hint": "то же самое во второй системе",
    },
    {
        "id": "badge",
        "section": "Участники",
        "label": "🏅 Значки",
        "command": "/badge",
        "kind": "open",
        "hint": "создать, выдать, забрать значок",
    },
    {
        "id": "badgeadmin",
        "section": "Участники",
        "label": "🎖 Кто выдаёт значки",
        "command": "/badgeadmin",
        "kind": "open",
        "hint": "список тех, кому доверено выдавать значки",
    },
    {
        "id": "weekwinner",
        "section": "Участники",
        "label": "🏆 Победитель недели",
        "command": "/weekwinner",
        "kind": "ask",
        "hint": "записать победителя конкурсной недели",
        "prompt": "Ответьте номером недели и участником, например: 1 @username",
    },
    {
        "id": "deletepokras",
        "section": "Участники",
        "label": "🗑 Удалить покрас",
        "command": "/deletepokras",
        "kind": "ask",
        "hint": "убрать чужую работу из статистики",
        "prompt": "Ответьте участником и номером работы, например: @username 3",
    },
    {
        "id": "plant",
        "section": "Дерево",
        "label": "🌱 Открыть посадку",
        "command": "/plant",
        "kind": "confirm",
        "hint": "открыть сбор желающих посадить семечко",
        "confirm": "Открыть посадку? Приглашение с кнопкой уйдёт в чат сразу.",
    },
    {
        "id": "plantreminder",
        "section": "Дерево",
        "label": "🔔 Напомнить о посадке",
        "command": "/plantreminder",
        "kind": "confirm",
        "hint": "напоминание об уже открытой посадке",
        "confirm": "Отправить напоминание о посадке? Оно уйдёт в чат сразу.",
    },
    {
        "id": "replant",
        "section": "Дерево",
        "label": "🌳 Посадить заново",
        "command": "/replant",
        "kind": "confirm",
        "hint": "начать дерево с нуля",
        "confirm": (
            "Начать дерево заново?\n\n"
            "Весь накопленный рост обнулится, а в чат уйдёт пост «сегодня мы посадили "
            "семечко». Отменить это будет нечем."
        ),
    },
    {
        "id": "arenanews",
        "section": "Игра",
        "label": "📰 Новости арены",
        "command": "/arenanews",
        "kind": "ask",
        "hint": "запись в список обновлений игры",
        "prompt": (
            "Ответьте текстом новости. Первая строка — заголовок, остальное — пояснение."
        ),
    },
)

ACTIONS_BY_ID = {action["id"]: action for action in ACTIONS}


def action(action_id: str) -> dict | None:
    return ACTIONS_BY_ID.get(action_id)


def callback_data(step: str, action_id: str | None = None) -> str:
    parts = [CALLBACK_PREFIX, step]
    if action_id is not None:
        parts.append(action_id)
    return ":".join(parts)


def parse_callback(data: str) -> tuple[str, str] | None:
    """(step, action id) for one of this panel's buttons, or None if it isn't one."""
    parts = (data or "").split(":")
    if len(parts) < 2 or parts[0] != CALLBACK_PREFIX:
        return None
    return parts[1], parts[2] if len(parts) > 2 else ""


def menu_text() -> str:
    """The panel, with its legend.

    The legend names every command in full rather than only labelling the buttons: these
    commands all still work typed, and somebody who learns the spelling here stops needing
    the panel -- which is a better outcome than a panel nobody can work without.
    """
    lines = [
        "🛠 <b>Панель администратора</b>",
        "",
        "Всё управление чатом в одном месте. Каждая кнопка запускает ту же команду, "
        "что и текстом — панель нужна только чтобы их не помнить.",
    ]
    section = None
    for item in ACTIONS:
        if item["section"] != section:
            section = item["section"]
            lines.append("")
            lines.append(f"<b>{section}</b>")
        lines.append(f"{item['command']} — {item['hint']}")
    return "\n".join(lines)


def menu_keyboard() -> dict:
    """Two buttons per row, in the legend's order, never splitting a section across the
    boundary -- the rows and the paragraphs above them have to line up or the legend is
    just a wall of text next to an unrelated grid."""
    rows: list[list[dict]] = []
    section = None
    for item in ACTIONS:
        button = {"text": item["label"], "callback_data": callback_data("run", item["id"])}
        if item["section"] != section or not rows or len(rows[-1]) == 2:
            section = item["section"]
            rows.append([button])
        else:
            rows[-1].append(button)
    return {"inline_keyboard": rows}


def confirm_text(item: dict) -> str:
    return f"{item['label']}\n\n{item['confirm']}"


def confirm_keyboard(item: dict) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "✅ Да, продолжить", "callback_data": callback_data("go", item["id"])}],
            [{"text": BACK_BUTTON_TEXT, "callback_data": callback_data("menu")}],
        ]
    }


def prompt_text(item: dict) -> str:
    """The force-reply question. Says how to get out, because a force-reply cannot carry
    an inline keyboard -- Telegram allows one reply_markup per message -- so the way back
    has to be a word instead of a button."""
    return f"{item['label']}\n\n{item['prompt']}\n\nОтветьте «отмена», чтобы выйти."


def is_cancel(text: str) -> bool:
    return (text or "").strip().lower() in CANCEL_WORDS
