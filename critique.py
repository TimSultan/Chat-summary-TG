"""Generates a Russian-language painting critique of one member's own miniature, from
the actual photo they posted under #япокрасил. Sold in the shop (see economy.py).

Unlike joke.py/roast.py, which reason over text, this one needs the image itself -- a
critique written from a caption alone would be generic filler, which is exactly what the
gamification plan warns cosmetic-feeling rewards degrade into. The photo is sent inline
as a base64 data URL rather than a public link, because the source chat is private and
the model has no way to fetch from it.

The caller is responsible for refunding the purchase if this raises: see the shop handler
in bot_listener.py.
"""

import base64

from openai import OpenAI, OpenAIError

from errors import ChatSummaryError

# Telegram photos are comfortably inside any request limit, but a full-resolution
# "sent as a document" upload (which #япокрасил posts legitimately use -- see
# is_image_message) can be many megabytes. Refuse rather than silently spend minutes
# uploading one.
MAX_IMAGE_BYTES = 8 * 1024 * 1024

CRITIQUE_SYSTEM_PROMPT = """\
Ты -- опытный миниатюрист и преподаватель покраса, который разбирает работу коллеги по \
чату. Тебя попросили именно о разборе, а не о похвале.

Дай честный, доброжелательный и КОНКРЕТНЫЙ разбор работы на фото. Пиши по-русски, \
одним-двумя абзацами, без заголовков, без нумерованных списков и без эмодзи. Опирайся \
ТОЛЬКО на то, что реально видно на фото -- не выдумывай деталей, которых не видно, и не \
угадывай, какая это миниатюра, если это не очевидно.

Структура по смыслу (но сплошным текстом, не списком): сначала что получилось хорошо и \
почему это работает, затем 2-3 конкретные вещи, которые стоит подтянуть, с объяснением \
КАК именно (какая техника, где именно на модели). Говори про базовые вещи честно: \
чистота нанесения, контраст, переходы, читаемость силуэта, акценты, глаза/лицо, \
металлики, подставка. Если фото не позволяет что-то оценить (засвет, размытие, \
далеко) -- так и скажи, вместо того чтобы придумывать.

Не сравнивай с "профессионалами" и не ставь оценок в баллах. Без вступлений вроде \
"Конечно, давайте разберём" -- сразу по делу.
"""

CRITIQUE_USER_PROMPT = """\
Разбери эту работу участника чата{caption_note}.
"""


def _data_url(image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    return f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"


def critique_work(
    api_key: str,
    model: str,
    image_bytes: bytes,
    caption: str = "",
    mime_type: str = "image/jpeg",
) -> str:
    """Blocking OpenAI call -- callers run it via asyncio.to_thread, same as roast_person.

    `caption` is the poster's own text under the photo, passed as context only: it may
    say what the model is or what they were attempting, which makes the critique less
    generic. It is explicitly NOT treated as instructions.
    """
    if not image_bytes:
        raise ChatSummaryError("no image to critique")
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ChatSummaryError(
            f"image is too large to critique ({len(image_bytes)} bytes, "
            f"limit {MAX_IMAGE_BYTES})"
        )

    clean_caption = " ".join((caption or "").split())[:400]
    caption_note = f'. Подпись автора: "{clean_caption}"' if clean_caption else ""
    client = OpenAI(api_key=api_key)
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": CRITIQUE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": CRITIQUE_USER_PROMPT.format(caption_note=caption_note),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": _data_url(image_bytes, mime_type)},
                        },
                    ],
                },
            ],
        )
    except OpenAIError as e:
        raise ChatSummaryError(f"OpenAI API call failed while generating a critique: {e}") from e

    content = response.choices[0].message.content
    if not content or not content.strip():
        raise ChatSummaryError("OpenAI returned an empty critique")
    return content.strip()
