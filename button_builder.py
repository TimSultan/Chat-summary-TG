"""Pure rendering and callback helpers for admin-created counter-button posts.

The conversation itself lives in bot_listener.py and the published state in stats.py.
Keeping all Telegram text/keyboard construction here gives the preview, initial post and
three-second counter edits one source of truth.
"""

CALLBACK_PREFIX = "btngen"
COMMAND = "/buttons"
FLOW_TTL_SECONDS = 15 * 60
COUNTER_REFRESH_SECONDS = 3
MAX_MESSAGE_CHARS = 3500
MAX_BUTTON_TEXT_CHARS = 64
TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024


def callback_data(action: str, item_id: str, argument: int | None = None) -> str:
    parts = [CALLBACK_PREFIX, action, item_id]
    if argument is not None:
        parts.append(str(argument))
    return ":".join(parts)


def parse_callback(data: str) -> tuple[str, str, int | None] | None:
    parts = (data or "").split(":")
    if len(parts) not in (3, 4) or parts[0] != CALLBACK_PREFIX:
        return None
    argument = None
    if len(parts) == 4:
        try:
            argument = int(parts[3])
        except ValueError:
            return None
    return parts[1], parts[2], argument


def choose_count_keyboard(flow_id: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "1 кнопка", "callback_data": callback_data("count", flow_id, 1)},
            {"text": "2 кнопки", "callback_data": callback_data("count", flow_id, 2)},
        ]]
    }


def choose_photo_keyboard(flow_id: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "🖼 Добавить картинку", "callback_data": callback_data("photo", flow_id, 1)}],
            [{"text": "Без картинки", "callback_data": callback_data("photo", flow_id, 0)}],
        ]
    }


def render_post(message_text: str, buttons: list[dict]) -> str:
    counter_lines = [f"• {button['text']} — {int(button.get('count', 0))}" for button in buttons]
    return "\n".join([message_text.strip(), "", "Нажатия:", *counter_lines])


def post_keyboard(post_id: str, buttons: list[dict]) -> dict:
    return {
        "inline_keyboard": [[
            {
                "text": button["text"],
                "callback_data": callback_data("press", post_id, index),
            }
        ] for index, button in enumerate(buttons)]
    }


def preview_keyboard(flow_id: str, buttons: list[dict]) -> dict:
    rows = [[
        {
            "text": button["text"],
            "callback_data": callback_data("sample", flow_id, index),
        }
    ] for index, button in enumerate(buttons)]
    rows.extend([
        [{"text": "✅ Отправить в чат", "callback_data": callback_data("send", flow_id)}],
        [{"text": "✖️ Отменить", "callback_data": callback_data("cancel", flow_id)}],
    ])
    return {"inline_keyboard": rows}


def delete_keyboard(post_id: str) -> dict:
    return {
        "inline_keyboard": [[{
            "text": "🗑 Удалить из чата",
            "callback_data": callback_data("delete", post_id),
        }]]
    }


def validate_message_text(text: str) -> str:
    clean = (text or "").strip()
    if not clean:
        raise ValueError("Текст сообщения не может быть пустым.")
    if len(clean) > MAX_MESSAGE_CHARS:
        raise ValueError(f"Текст слишком длинный. Максимум — {MAX_MESSAGE_CHARS} символов.")
    return clean


def validate_button_text(text: str) -> str:
    clean = (text or "").strip()
    if not clean:
        raise ValueError("Текст кнопки не может быть пустым.")
    if len(clean) > MAX_BUTTON_TEXT_CHARS:
        raise ValueError(
            f"Текст кнопки слишком длинный. Максимум — {MAX_BUTTON_TEXT_CHARS} символа."
        )
    return clean


def validate_rendered_length(message_text: str, buttons: list[dict], with_photo: bool) -> None:
    limit = TELEGRAM_CAPTION_LIMIT if with_photo else TELEGRAM_TEXT_LIMIT
    if len(render_post(message_text, buttons)) > limit:
        kind = "подписи к картинке" if with_photo else "сообщения"
        raise ValueError(f"Текст слишком длинный для {kind}. Сократите его и запустите /buttons заново.")
