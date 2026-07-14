"""v2 request routing: turns a short, free-form, possibly mixed-language chat request
like '/summary что там у Толи вчера' into just enough structure to fetch the right
messages -- a date range (or rolling lookback window) and, optionally, which person the
request is about -- plus a cleaned-up restatement of the question itself.

Unlike intent.py (v1), this does NOT try to extract scope/topic_hint/length_hint as
separate structured fields. Everything beyond "which messages do we need" (what's being
asked, how it should be phrased/shaped) is left to responder_v2.py's single freeform
answering step, which gets the transcript plus `cleaned_question` and decides the rest
itself. Username resolution against actual chat participants (for a plain name/nickname
rather than an exact @mention) still reuses intent.py's resolve_name_hint -- that's a
generic name-matching helper, not part of the v1 response-shaping logic being replaced.

All replies to the chat are always in Russian regardless of what language the request was
written in -- see responder_v2.py -- so there is no reply_language field here.
"""

import json
from datetime import date

from openai import OpenAI, OpenAIError

from errors import ChatSummaryError

SYSTEM_PROMPT = """\
Ты превращаешь короткое сообщение из чата в структурированный запрос на выборку истории \
переписки Telegram, плюс очищенную формулировку вопроса для второго ИИ-шага, который на него \
ответит. Сообщение может быть на смеси языков (например, русский и английский) и может как \
называть конкретного человека, так и не называть. Тебе даны сегодняшняя опорная дата и все \
@username, упомянутые в сообщении.

Верни ТОЛЬКО JSON-объект с этими полями:
- "start_date": дата в формате ISO (YYYY-MM-DD), с которой начинается запрошенный период.
- "end_date": дата в формате ISO (YYYY-MM-DD), которой запрошенный период заканчивается \
(включительно; равна start_date для одного дня). Толкуй относительные даты (сегодня, вчера, эта \
неделя, последние N дней и т.п.) исходя из опорной даты. Если период вообще не упомянут, по \
умолчанию бери только опорную дату.
- "lookback_hours": если запрос явно просит скользящее окно за последние N часов или минут \
(минуты переведи в часы, например "последние 90 минут" -> 1.5), а не календарный день -- \
например "за последние 10 часов", "за последние 3 часа", "за пару часов" -- укажи это число \
здесь (int или float). Это поле имеет приоритет над start_date/end_date, которые в коде потом \
вычисляются точно от момента запроса, а не тобой -- всё равно заполни start_date/end_date своей \
лучшей оценкой, но они будут переопределены. НЕ заполняй это поле для "сегодня"/"вчера" и прочих \
формулировок про календарный день. Иначе null.
- "username": если запрос конкретно про одного человека -- что он говорил, ситуация/тема, \
связанная с ним, или кто он такой/что про него известно (например "что писал @bob", "что там за \
ситуация с Анжеликой", "кто такой Степан") -- укажи здесь username или имя/прозвище ровно так, как \
оно написано в запросе (без ведущей @; оно может быть с опечаткой, сокращено или транслитерировано \
с другого алфавита). НИКОГДА не указывай сюда собственный username бота (дан ниже) -- сообщение \
часто *начинается* с "@такойюзернейм" просто чтобы обратиться к боту/вызвать его, а не потому что \
вопрос про него. Иначе null, если запрос про чат/переписку в целом, а не про конкретного человека.
- "cleaned_question": запрос пользователя, переформулированный в один ясный, самодостаточный \
вопрос или инструкцию на ТОМ ЖЕ языке, с удалённым триггерным словом и любым обращением к боту -- \
второй ИИ-шаг увидит ТОЛЬКО этот текст плюс переписку, без остального контекста, так что он \
должен быть однозначным сам по себе. Сохраняй верность исходному запросу; убирай только шум \
(триггерное слово, префикс "@бот"), не добавляй и не убирай смысл. Если исходный запрос уже \
представляет собой ясный самодостаточный вопрос, оставь его почти как есть.
"""

USER_PROMPT_TEMPLATE = """\
Опорная дата: {reference_date}
Собственный username бота (к нему обращаются/его вызывают этим сообщением -- НИКОГДА не тема \
вопроса, даже если он часто встречается как ведущее упоминание @username): {my_username}
Упомянутые в сообщении username (кроме бота): {mentioned}
Текст запроса: {text}
"""


def route_request(
    api_key: str,
    model: str,
    text: str,
    reference_date: date,
    mentioned_usernames: list[str],
    my_username: str | None = None,
) -> dict:
    if not text or not text.strip():
        raise ChatSummaryError("Cannot parse an empty summary request.")
    assert isinstance(mentioned_usernames, list), "internal bug: mentioned_usernames must be a list"

    client = OpenAI(api_key=api_key)
    prompt = USER_PROMPT_TEMPLATE.format(
        reference_date=reference_date.isoformat(),
        my_username=my_username or "(unknown)",
        mentioned=", ".join(mentioned_usernames) or "(none)",
        text=text,
    )
    try:
        response = client.chat.completions.create(
            model=model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
    except OpenAIError as e:
        raise ChatSummaryError(f"OpenAI API call failed while routing the request: {e}") from e

    content = response.choices[0].message.content
    if not content:
        raise ChatSummaryError("Router returned an empty response from the model.")
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise ChatSummaryError(f"Router returned invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ChatSummaryError(f"Router returned a JSON {type(data).__name__}, expected an object.")

    username = (data.get("username") or None)
    if isinstance(username, str):
        username = username.strip().lstrip("@") or None
    # Deterministic safeguard: the bot's own username is never a valid target, no matter
    # what the model returned -- it just means the bot was addressed, not asked about.
    if username and my_username and username.lower() == my_username.lower():
        username = None

    def _parse(value, fallback):
        try:
            return date.fromisoformat(value)
        except (TypeError, ValueError):
            return fallback

    start_date = _parse(data.get("start_date"), reference_date)
    end_date = _parse(data.get("end_date"), reference_date)
    if end_date < start_date:
        start_date, end_date = end_date, start_date

    lookback_hours = data.get("lookback_hours")
    if isinstance(lookback_hours, bool) or not isinstance(lookback_hours, (int, float)):
        lookback_hours = None
    elif lookback_hours <= 0:
        lookback_hours = None

    cleaned_question = (data.get("cleaned_question") or None)
    if isinstance(cleaned_question, str):
        cleaned_question = cleaned_question.strip() or None
    if not cleaned_question:
        cleaned_question = text.strip()  # fall back to the raw request rather than nothing

    return {
        "start_date": start_date,
        "end_date": end_date,
        "lookback_hours": lookback_hours,
        "username": username,
        "cleaned_question": cleaned_question,
    }
