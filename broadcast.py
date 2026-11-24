"""/broadcast -- anything at all through the bot, with the admin's own link buttons or none.

"/vote chat" can already post a text, a picture or an album to the chat, the second group
or everybody's DM -- but always under one "Открыть голосование" button, because that is
what it is for. A broadcast is the same draft (bot_listener's announcement flow, tagged
BROADCAST_SYSTEM) without that button: after the text the admin is asked whether to add
buttons, and answers with lines like

    Сайт - https://example.com
    Канал - https://t.me/channel | Чат - https://t.me/chat

one row per line, `|` between buttons in a row. Only link buttons: a callback button would
need a handler that outlives the draft, and a web_app button is refused in a group.

This module is the parsing alone -- pure, so the rules are testable without a bot.
"""

from __future__ import annotations

import re

COMMANDS = ("/broadcast", "/рассылка")

MAX_ROWS = 8
MAX_PER_ROW = 3
MAX_BUTTON_TEXT = 64

# A reply that means "no buttons after all", for whoever answered the buttons prompt
# instead of pressing «Без кнопок».
NO_BUTTONS_WORDS = frozenset({"нет", "без кнопок", "без", "no", "none", "-"})

_URL_TAIL = re.compile(
    r"^(?P<text>.*?)\s*[-—–:]?\s*(?P<url>(?:https?://|tg://|t\.me/|telegram\.me/|@)\S+)$",
    re.IGNORECASE,
)
_MARKDOWN_LINK = re.compile(r"^\[(?P<text>[^\]]+)\]\((?P<url>[^)\s]+)\)$")

FORMAT_HELP = (
    "Каждая кнопка с новой строки:\n"
    "Текст кнопки - https://ссылка\n\n"
    "Несколько кнопок в один ряд -- через |, например:\n"
    "Сайт - https://example.com | Канал - https://t.me/channel\n\n"
    "Вместо ссылки можно @username канала или бота."
)


class ButtonsError(ValueError):
    """What is wrong with the reply, in words for the admin."""


def _normalize_url(url: str) -> str:
    url = url.strip()
    if url.startswith("@"):
        return f"https://t.me/{url[1:]}"
    lowered = url.lower()
    if lowered.startswith(("t.me/", "telegram.me/")):
        return f"https://{url}"
    return url


def _button(chunk: str, line_number: int) -> dict:
    chunk = chunk.strip()
    match = _MARKDOWN_LINK.match(chunk) or _URL_TAIL.match(chunk)
    if match is None:
        raise ButtonsError(
            f"Строка {line_number}: не нашёл ссылку в «{chunk}». Нужно так: Текст - https://ссылка"
        )
    text = match.group("text").strip()
    url = _normalize_url(match.group("url"))
    if not text:
        raise ButtonsError(f"Строка {line_number}: у кнопки со ссылкой {url} нет текста.")
    if len(text) > MAX_BUTTON_TEXT:
        raise ButtonsError(
            f"Строка {line_number}: текст кнопки длиннее {MAX_BUTTON_TEXT} символов."
        )
    if not re.match(r"^(https?|tg)://\S+$", url, re.IGNORECASE):
        raise ButtonsError(f"Строка {line_number}: «{url}» не похоже на ссылку.")
    return {"text": text, "url": url}


def parse_buttons(reply: str) -> list[list[dict]]:
    """The keyboard rows a reply describes. Raises ButtonsError with the first problem."""
    rows: list[list[dict]] = []
    for line_number, line in enumerate((reply or "").splitlines(), start=1):
        if not line.strip():
            continue
        chunks = [chunk for chunk in line.split("|") if chunk.strip()]
        if len(chunks) > MAX_PER_ROW:
            raise ButtonsError(
                f"Строка {line_number}: в одном ряду не больше {MAX_PER_ROW} кнопок."
            )
        rows.append([_button(chunk, line_number) for chunk in chunks])
    if not rows:
        raise ButtonsError("Не нашёл ни одной кнопки.")
    if len(rows) > MAX_ROWS:
        raise ButtonsError(f"Не больше {MAX_ROWS} рядов кнопок.")
    return rows


def wants_no_buttons(reply: str) -> bool:
    return (reply or "").strip().lower() in NO_BUTTONS_WORDS
