"""v2 answering step: given a transcript, the user's original request, and the router's
cleaned interpretation, produces one direct answer in a single prompt -- no separate
topic-shape/length-hint rule stack like summarizer.py's
TOPIC_ONLY_RULES/LENGTH_HINT_RULE/DIRECT_QUESTION_RULE; the model decides the right shape
itself (direct answer vs. topic-by-topic rundown) based on what's actually being asked.

Two styles, same as summarizer.py:
- "reply": compact, meant to be sent back as a Telegram chat message.
- "file": verbose markdown report with "## " headings, meant for a saved digest file.

Reuses chunk_transcript from summarizer.py -- pure token-chunking utility, unrelated to
the v1 response-shaping logic this module replaces.

Every answer is written in Russian, regardless of what language the question/transcript
are in -- there is no reply_language parameter.
"""

from openai import OpenAI, OpenAIError

from errors import ChatSummaryError
from summarizer import chunk_transcript

VALID_STYLES = ("file", "reply")

SYSTEM_PROMPT_BASE = """\
Ты отвечаешь на вопросы про групповой чат в Telegram, опираясь ТОЛЬКО на предоставленную \
переписку как на источник истины. Переписка идёт в хронологическом порядке, формат \
"[HH:MM] Имя: текст" или "[YYYY-MM-DD HH:MM] Имя: текст".

Отвечай ВСЕГДА на русском языке, даже если вопрос или сама переписка на другом языке.

Выбирай форму ответа исходя из того, что реально спрашивают:
- Конкретный вопрос (кто/что/когда/почему/как/обсуждали ли и т.п.) о чём-то определённом -- \
отвечай на него прямо и естественно, своими словами.
- Общий запрос вида "что происходило"/"перескажи чат", или вопрос настолько широкий, что \
естественным ответом будет обзор основных тем -- вместо этого дай разбивку по темам. Объединяй всё, \
что относится к одной теме, в одну запись, даже если в обсуждении участвовало много людей/сообщений. \
Пропускай приветствия, светскую болтовню, разовые сообщения без ответа, спам и темы, которые получили \
лишь пару малосодержательных реплик. Упоминай итог только если обсуждение реально к чему-то пришло.

Будь КОНКРЕТНЫМ, а не общим. Называй, кто именно что сказал/предложил/решил -- по имени, как оно дано \
в переписке -- а не безлико "кто-то предложил" или "было решено". Указывай реальные детали: конкретные \
позиции и аргументы сторон, цифры, даты, договорённости, а не абстрактное "обсуждали разные темы" или \
"было живое обсуждение". Если тема поднимается не первый день, самое ценное в ответе -- это именно то, \
что изменилось/добавилось/решилось СЕГОДНЯ по сравнению с уже известным, а не повтор общей формулировки \
темы -- вникай в детали конкретно этого периода, а не пересказывай тему абстрактно.

Никогда не выдумывай информацию, которой нет в переписке. Если в переписке недостаточно данных, \
чтобы ответить на вопрос, прямо скажи об этом одной короткой фразой вместо того, чтобы гадать или \
добавлять несвязанное содержимое.
"""

FOCUS_USER_RULE = """\
Маршрутизатор предположил, что вероятный фокус вопроса -- {focus_user}. Используй это как подсказку, \
если она согласуется с исходным сообщением пользователя. Если исходное сообщение указывает на \
другого человека или ставит вопрос иначе, игнорируй эту подсказку и следуй исходному сообщению. \
Когда подсказка верна, сосредоточься на том, что {focus_user} писал(а), в каких темах участвовал(а), \
или темах, которые касались его/её по существу, даже если он(а) сам(а) не писал(а) (например, вопрос \
"что там за ситуация с {focus_user}" означает драму/событие/решение, связанное с этим человеком, а \
не только его/её собственные сообщения). Игнорируй обсуждения, к которым он(а) не имеет реального \
отношения. Если в переписке нет ничего существенного про этого человека, коротко скажи об этом \
вместо того, чтобы выдумывать содержимое.
"""

REQUESTER_RULE = """\
Автор исходного запроса -- {requester_name}. Учитывай, кто задал вопрос: местоимения первого \
лица ("я", "мне", "меня", "мой", "I", "me", "my") в исходном сообщении относятся к этому \
человеку. Отвечай именно на вопрос автора, сохраняя его смысл и акценты.
"""

REQUEST_CONTEXT_TEMPLATE = """\
Автор запроса: {requester_name}
Исходное сообщение пользователя (главный источник смысла вопроса):
---
{original_request}
---
Очищенная интерпретация маршрутизатора (только вспомогательная подсказка):
---
{question}
---
Если интерпретация потеряла нюанс или расходится с исходным сообщением, следуй исходному сообщению.
"""

REPLY_STYLE_RULE = """\
Это отправляется прямо как ответное сообщение в чат Telegram -- должно почти всегда укладываться в \
одно сообщение Telegram, а не растягиваться на несколько. Разделяй темы по значимости на два уровня, \
а не расписывай все одинаково подробно:

- САМОЕ ИНТЕРЕСНОЕ (обычно 2-5 тем): реальная развёрнутая дискуссия, спор, драма, решение, что-то \
неожиданное или смешное -- раскрывай эти темы первыми и подробно: **жирным** короткое название темы, \
затем 1-3 предложения с конкретикой -- кто что сказал/предложил, какие позиции столкнулись, чем \
закончилось. Не сжимай такую тему до одной обтекаемой фразы.
- ОСТАЛЬНОЕ: темы, где было хоть какое-то содержательное обсуждение, но не тянущее на "самое \
интересное" -- упомяни их одной короткой фразой каждую, без отдельного жирного заголовка и без \
разбора позиций, просто чтобы было понятно, что это вообще поднималось.
- Пропускай только то, что и так велено пропускать выше (приветствия, малосодержательные обмены и т.п.).

Именно этот выбор -- что раскрыть подробно, а что упомянуть вскользь -- и держит ответ компактным на \
насыщенный день, а не искусственное урезание содержания или пропуск тем целиком. Тихий день -- \
короткий ответ из одних кратких упоминаний, активный день -- несколько подробных тем плюс короткий \
список остального.

Никаких заголовков "## ". Никаких вступлений ("Вот сводка...") и заключительных фраз.
"""

FILE_STYLE_RULE = """\
Располагай темы в хронологическом порядке по времени начала и указывай примерное время (и дату, \
если переписка охватывает несколько дней) начала. Выведи валидный Markdown: один заголовок "## " на \
тему (короткое, содержательное название), больше ничего на верхнем уровне -- никаких вступлений, \
заключительных фраз, мета-комментариев про сам процесс составления сводки.
"""

MAP_PROMPT = """\
Ниже -- ЧАСТЬ {part} из {total} переписки чата (в хронологическом порядке, формат \
"[HH:MM] Имя: текст" или "[YYYY-MM-DD HH:MM] Имя: текст").
{request_context}
Выпиши из этой части черновые заметки, релевантные для ответа на этот вопрос -- примерное время/\
дату, кто участвовал, и ключевые тезисы/позиции. Отбрось явный чистый спам, однословные реакции и \
приветствия, но в остальном сохрани достаточно деталей, чтобы на следующем шаге можно было оценить \
релевантность и написать настоящий ответ -- сильно фильтровать пока не нужно, это будет дальше.
{focus_note}
Часть переписки:
{transcript}
"""

FINAL_FROM_TRANSCRIPT_PROMPT = """\
Чат: {chat_title}
Период: {period_label}
{request_context}

Полная переписка за этот период (в хронологическом порядке, формат "[HH:MM] Имя: текст" или \
"[YYYY-MM-DD HH:MM] Имя: текст"):
{transcript}
"""

FINAL_FROM_NOTES_PROMPT = """\
Чат: {chat_title}
Период: {period_label}
{request_context}

Переписка оказалась слишком длинной для одного прохода, поэтому её разбили на {total} частей, и \
по каждой части ниже сделаны черновые заметки. Некоторые темы могут продолжаться в нескольких \
частях -- объедини их в одну запись. Теперь ответь на вопрос, используя эти заметки.

Заметки по каждой части:
{notes}
"""


def _build_system_prompt(style: str, focus_user: str | None, requester_name: str | None = None) -> str:
    assert style in VALID_STYLES, f"internal bug: unknown style {style!r}, expected one of {VALID_STYLES}"
    parts = [SYSTEM_PROMPT_BASE]
    if requester_name:
        parts.append(REQUESTER_RULE.format(requester_name=requester_name))
    if focus_user:
        parts.append(FOCUS_USER_RULE.format(focus_user=focus_user))
    parts.append(REPLY_STYLE_RULE if style == "reply" else FILE_STYLE_RULE)
    return "\n".join(parts)


def _chat(client: OpenAI, model: str, system_prompt: str, user_prompt: str) -> str:
    try:
        response = client.chat.completions.create(
            model=model,
            temperature=0.2,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
    except OpenAIError as e:
        raise ChatSummaryError(f"OpenAI API call failed: {e}") from e

    content = response.choices[0].message.content
    if not content or not content.strip():
        raise ChatSummaryError("OpenAI returned an empty answer -- try again or use a different model.")
    return content.strip()


def answer_request(
    api_key: str,
    model: str,
    chat_title: str,
    period_label: str,
    lines: list[str],
    question: str,
    focus_user: str | None = None,
    style: str = "reply",
    # gpt-5.4-mini has a 400k-token context window (verified against OpenAI's docs, July
    # 2026) -- 200k comfortably covers even this chat's busiest observed real day
    # (~167k tokens) as a SINGLE chunk, skipping map-reduce entirely rather than just
    # shrinking it, while leaving well over half the window free for the system prompt,
    # output, and headroom as the chat grows. Raising this doesn't cost more tokens on a
    # quiet day (the ceiling only matters once a day's content exceeds it) and actually
    # costs FEWER tokens overall on a busy one: each extra chunk under the old 6000-token
    # default paid for another full copy of the system prompt plus a "notes" step that
    # then got fed back in as input to the final call.
    max_chunk_tokens: int = 200_000,
    original_request: str | None = None,
    requester_name: str | None = None,
) -> str:
    if style not in VALID_STYLES:
        raise ChatSummaryError(f"Unknown summary style '{style}', expected one of {VALID_STYLES}.")
    if not api_key or not api_key.strip():
        raise ChatSummaryError("OpenAI API key is missing.")
    if not model or not model.strip():
        raise ChatSummaryError("OpenAI model name is missing.")
    if not question or not question.strip():
        raise ChatSummaryError("Cannot answer an empty question.")

    no_content_msg = (
        "За этот период не нашлось ничего существенного для сводки."
        if style == "file"
        else "Нечего было содержательного обсудить."
    )
    if not lines:
        return no_content_msg

    client = OpenAI(api_key=api_key)
    original_request = (original_request or question).strip()
    requester_label = (requester_name or "неизвестный пользователь").strip()
    request_context = REQUEST_CONTEXT_TEMPLATE.format(
        requester_name=requester_label,
        original_request=original_request,
        question=question.strip(),
    )

    system_prompt = _build_system_prompt(style, focus_user, requester_name)
    chunks = chunk_transcript(lines, max_chunk_tokens, model)

    if len(chunks) == 1:
        prompt = FINAL_FROM_TRANSCRIPT_PROMPT.format(
            chat_title=chat_title,
            period_label=period_label,
            request_context=request_context,
            transcript=chunks[0],
        )
        return _chat(client, model, system_prompt, prompt)

    focus_note = (
        f"\nПредполагаемый фокус -- {focus_user}; используй его только если он согласуется "
        "с исходным сообщением пользователя.\n"
        if focus_user
        else ""
    )
    notes_parts = []
    for i, chunk in enumerate(chunks, start=1):
        prompt = MAP_PROMPT.format(
            part=i,
            total=len(chunks),
            request_context=request_context,
            transcript=chunk,
            focus_note=focus_note,
        )
        notes = _chat(client, model, system_prompt, prompt)
        notes_parts.append(f"--- Заметки по части {i} ---\n{notes}")

    combined_notes = "\n\n".join(notes_parts)
    final_prompt = FINAL_FROM_NOTES_PROMPT.format(
        chat_title=chat_title,
        period_label=period_label,
        request_context=request_context,
        total=len(chunks),
        notes=combined_notes,
    )
    return _chat(client, model, system_prompt, final_prompt)
