"""Generates a short, unprompted joke/remark for the chat, based on a slice of the live
conversation. Unlike everything else in this project, nobody asks for this -- it's fired
autonomously by an activity-based trigger (see JOKE_* settings in config.py and the
tracking code in listener.py's on_message) when the chat has been active for a bit, on a
cooldown, with a random chance on top so it doesn't feel mechanical. Always sent via the
bot account (see joke_queue plumbing in listener.py/bot_listener.py), never the personal
account.

Because nothing reviews this before it posts, the model is explicitly given a way to
decline (SKIP_SENTINEL) for moments that aren't actually appropriate to joke about --
real arguments, heavy/personal topics, etc. -- rather than being forced to always produce
something.
"""

from openai import OpenAI, OpenAIError

from errors import ChatSummaryError

SKIP_SENTINEL = "SKIP"

JOKE_SYSTEM_PROMPT = """\
Ты -- один из участников дружеского группового чата. Тебе иногда, редко, разрешают \
вставить ОДНУ короткую смешную реплику по ходу разговора -- как будто просто проходил \
мимо и не удержался.

Правила:
- Пиши ТОЛЬКО на русском. Одна-две короткие фразы, не абзац и не монолог.
- Можно по-доброму подколоть конкретного человека по имени, если это реально вытекает из \
того, что он только что написал -- но никогда не за внешность, здоровье, вес, деньги, \
отношения, утраты и другие личные больные темы, и не по признакам вроде национальности, \
религии, ориентации.
- Никогда не выдумывай факты, которых нет в переписке.
- Не пиши шутку, если сейчас реальный конфликт, ссора, или тяжёлый/личный разговор -- в \
такие моменты уместнее промолчать.
- Без заголовков, без markdown, без имени в начале как подписи -- просто одна реплика, \
как обычное сообщение в чат.

Если по этим правилам сейчас шутить не стоит (неподходящий момент, не над чем пошутить \
необидно, тема слишком личная или острая) -- ответь ровно одним словом, без ничего \
больше: """ + SKIP_SENTINEL

JOKE_USER_PROMPT = """\
Последние сообщения в чате (формат "[HH:MM] Имя: текст"):
{transcript}

Если момент подходящий -- напиши одну короткую смешную реплику по мотивам этого \
разговора. Если нет -- ответь {skip}.
"""


def generate_joke(api_key: str, model: str, lines: list[str]) -> str | None:
    """Returns a short joke string, or None if the model decided this isn't a good moment
    to joke (see SKIP_SENTINEL) or the response was otherwise empty. `lines` should be
    recent chat messages formatted like telegram_fetch.format_transcript_lines."""
    if not api_key or not api_key.strip():
        raise ChatSummaryError("OpenAI API key is missing.")
    if not model or not model.strip():
        raise ChatSummaryError("OpenAI model name is missing.")
    if not lines:
        return None

    client = OpenAI(api_key=api_key)
    prompt = JOKE_USER_PROMPT.format(transcript="\n".join(lines), skip=SKIP_SENTINEL)

    try:
        response = client.chat.completions.create(
            model=model,
            temperature=0.9,
            messages=[
                {"role": "system", "content": JOKE_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
    except OpenAIError as e:
        raise ChatSummaryError(f"OpenAI API call failed while generating a joke: {e}") from e

    content = (response.choices[0].message.content or "").strip()
    if not content or content.strip(" .!").upper() == SKIP_SENTINEL:
        return None
    return content
