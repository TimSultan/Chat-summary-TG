"""Watches the chat right after the bot posts a response (a /summary answer or a joke --
see joke.py) for chat commentary ABOUT that response -- praise, mockery, whatever -- even
if it's not a direct reply or @mention, just the conversation flowing on and someone
remarking on it in passing. If that happens within a bounded window of messages (see
maybe_followup/FOLLOWUP_* settings in config.py and listener.py's on_message), the bot
gets to clap back once: pleased/smug/thankful for praise, funny-defensive right back for
criticism. Always sent via the bot account (see bot_response_queue/followup_queue
plumbing in listener.py/bot_listener.py), never the personal account -- same rule as
summary/roast/joke.

Same "let the model decline" shape as joke.py (FOLLOWUP_SKIP_SENTINEL): most messages
after a bot response are just the chat moving on to something else, not a reaction at
all, and nothing reviews this before it posts, so the model has to be able to say
"nothing to react to" rather than being forced to invent a comeback out of thin air.
"""

from openai import OpenAI, OpenAIError

from errors import ChatSummaryError

FOLLOWUP_SKIP_SENTINEL = "SKIP"

_KIND_LABELS = {
    "joke": "шутка",
    "summary": "сводка / ответ на вопрос",
}

FOLLOWUP_SYSTEM_PROMPT = """\
Ты -- бот в дружеском групповом чате. Только что ты отправил туда сообщение ({kind}). \
Ниже -- несколько сообщений, которые люди написали в чате СРАЗУ ПОСЛЕ этого. Они не \
обязаны отвечать тебе напрямую, упоминать тебя по имени или отвечать реплаем именно на \
твоё сообщение -- иногда это просто реплика в общем потоке ("бот жжёт", "опять эта \
дичь", "ору с этого") -- и это тоже считается комментарием о тебе.

Определи, комментируют ли тебя / твоё сообщение в этих репликах -- пусть даже косвенно.

- Если да, и это похвала, восторг или благодарность -- ответь ОДНОЙ короткой репликой: \
смешно, слегка хвастливо и с подколкой, но и искренне благодарно.
- Если да, и это критика, наезд или недовольство -- ответь ОДНОЙ короткой репликой: \
смешно, защищаясь, и по-доброму подкалывая в ответ того, кто это написал.
- Если в этих сообщениях тебя вообще не обсуждают (просто болтают о своём) -- ответь \
ровно одним словом, без ничего больше: {skip}

Правила для самого ответа (когда он есть):
- Пиши ТОЛЬКО на русском. Одна-две короткие фразы, не абзац и не монолог.
- Никогда не выдумывай факты, которых нет в переписке.
- По-доброму, как между своими -- никогда по-настоящему обидно и никогда по признакам \
вроде внешности, здоровья, денег, национальности, религии, ориентации.
- Без заголовков, без markdown, без имени в начале как подписи -- просто одна реплика, \
как обычное сообщение в чат.
- Без смайликов и эмодзи -- звучит неуверенно, а не уверенно и с подколкой.
"""

FOLLOWUP_USER_PROMPT = """\
{profile_section}Твоё последнее сообщение в чате ({kind}):
\"\"\"
{response_text}
\"\"\"

Сообщения чата сразу после этого (формат "[HH:MM] Имя: текст"):
{transcript}

Комментируют ли тебя в этих сообщениях? Если да -- ответь по инструкции. Если нет -- ответь {skip}.
"""

PROFILE_SECTION_TEMPLATE = "Атмосфера и юмор этого чата (используй как контекст, не пересказывай дословно):\n{profile}\n\n"


def generate_followup_reply(
    api_key: str,
    model: str,
    response_kind: str,
    response_text: str,
    lines: list[str],
    flavor_profile: str | None = None,
) -> str | None:
    """Returns a short clap-back string, or None if the model decided these messages
    aren't actually about the bot's last response (see FOLLOWUP_SKIP_SENTINEL) or the
    response was otherwise empty. `response_kind` is "joke" or "summary" -- whatever the
    bot itself just sent -- `response_text` is its actual content (so the comeback stays
    grounded in what was actually said, not generic), and `lines` are the chat messages
    that followed it, formatted like telegram_fetch.format_transcript_lines.
    `flavor_profile` (see chat_profile.py), if given, is the same cached chat-humor
    profile joke.py uses, so the comeback sounds like the same in-the-room persona."""
    if not api_key or not api_key.strip():
        raise ChatSummaryError("OpenAI API key is missing.")
    if not model or not model.strip():
        raise ChatSummaryError("OpenAI model name is missing.")
    if not lines:
        return None

    kind_label = _KIND_LABELS.get(response_kind, response_kind)
    client = OpenAI(api_key=api_key)
    profile_section = PROFILE_SECTION_TEMPLATE.format(profile=flavor_profile) if flavor_profile else ""
    system_prompt = FOLLOWUP_SYSTEM_PROMPT.format(kind=kind_label, skip=FOLLOWUP_SKIP_SENTINEL)
    prompt = FOLLOWUP_USER_PROMPT.format(
        profile_section=profile_section,
        kind=kind_label,
        response_text=response_text,
        transcript="\n".join(lines),
        skip=FOLLOWUP_SKIP_SENTINEL,
    )

    try:
        response = client.chat.completions.create(
            model=model,
            temperature=0.9,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
        )
    except OpenAIError as e:
        raise ChatSummaryError(f"OpenAI API call failed while generating a follow-up reply: {e}") from e

    content = (response.choices[0].message.content or "").strip()
    if not content or content.strip(" .!").upper() == FOLLOWUP_SKIP_SENTINEL:
        return None
    return content
