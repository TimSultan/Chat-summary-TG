"""Turns chat transcript lines into a themed summary via an OpenAI chat model.

Two styles:
- "file": verbose markdown report with "## " headings, meant for a saved digest file.
- "reply": compact, meant to be sent back as a Telegram chat message (Telethon markdown,
  no headings, kept short).

Optionally focused on a single participant ("focus_user") rather than the whole chat.
"""

from openai import OpenAI, OpenAIError

from errors import ChatSummaryError

VALID_STYLES = ("file", "reply")

BASE_RULES = """\
You are summarizing messages from a Telegram group chat.

Your job: identify the main topics that were ACTUALLY discussed and turn each into ONE concise entry.

Rules:
- Merge everything related to the same topic into a single entry, even if dozens of people and \
dozens of messages were involved in that discussion.
- Give each topic a short title, then a couple of sentences summarizing what was discussed (the \
main points/arguments/positions people raised), then a brief conclusion if the discussion reached \
a decision, agreement, or resolution. Omit the conclusion if it just fizzled out with no resolution.
- Skip: greetings, small talk, one-off unanswered messages, stickers/reactions/memes with no real \
discussion attached, spam/ads, and any topic that only got a couple of low-content replies.
- Do not invent information that is not in the messages. If a topic is ambiguous, describe it \
briefly rather than guessing details.
"""

FOCUS_USER_RULES = """\
Focus on {focus_user}: cover topics they raised, replied to substantively, actively participated \
in, or that were substantively ABOUT them even if they weren't the one typing (e.g. a request for \
"the situation with {focus_user}" means a drama/event/decision involving them, not just their own \
messages). Summarize what happened/was said, who was involved, and how others responded or how it \
concluded. Ignore discussions {focus_user} had no real connection to. If nothing in this transcript \
substantively involves {focus_user}, say so briefly instead of inventing content.
"""

TOPIC_ONLY_RULES = """\
IMPORTANT: the user asked about ONE specific topic/event, not a general summary: {topic_hint}
Ignore the "cover every topic" instructions below -- ONLY find and describe that specific topic/\
event. If it happened more than once in this transcript, describe the most recent occurrence \
unless the request clearly asked for something else (e.g. "the first time" or "every time"). \
Give: what happened, who was involved, and the outcome/conclusion if there was one. Do not \
mention, list, or summarize any other unrelated topic from the transcript -- a one-topic question \
gets a one-topic answer, nothing padded on. If nothing in this transcript matches what was asked \
about, say so in one short sentence instead of describing something else.
"""

LENGTH_HINT_RULE = """\
The user explicitly asked for this length/level of detail: "{length_hint}". Follow that over any \
other length guidance above -- e.g. if they asked for something short/brief/quick/one sentence, \
answer in 1-2 sentences with no heading or title, even if that means dropping detail; if they \
asked for more detail, expand beyond the usual limit.
"""

DIRECT_QUESTION_RULE = """\
The user's exact original message was: "{original_question}"
Decide which shape fits this request better, based on what was actually asked:
- If it's phrased as a direct question (who/what/when/why/how/did-we/etc.) about something specific, \
answer THAT question directly and naturally, in your own words, using the transcript as your source \
of truth -- don't force it into the generic topic-by-topic format just because that's the default style.
- If it's a generic "summarize everything"/"what happened" request, or the question is broad enough \
that a topic-by-topic breakdown of the main themes discussed IS the natural answer, use the default \
topic-by-topic format instead.
Either way, if the transcript doesn't contain enough to answer it, say so plainly instead of guessing \
or padding with unrelated information.
"""

FILE_STYLE_RULES = """\
Order topics chronologically by when they started, and mention the rough time (and date, if the \
transcript spans multiple days) they started.
Write the summary in the same language the chat mostly used.
Output valid Markdown: one "## " heading per topic (short, descriptive title), nothing else at the \
top level. No preamble, no closing remarks, no meta-commentary about the summarization process itself.
If, after filtering, there is nothing substantive to report, output exactly: "No noteworthy topics \
were discussed in this period."
"""

REPLY_STYLE_RULES = """\
This will be sent directly as a Telegram chat message reply, not saved as a document, so DEFAULT \
TO CONCISE -- this is a quick chat answer, not a report, unless the user explicitly asked for more \
detail (see below):
- Do NOT use "## " headings. For each topic write ONE sentence: **bold** title, then the gist and \
the conclusion (if any) together, e.g. "**Topic title** — what happened and how it was resolved.". \
Don't pad with extra clauses or detail nobody asked for.
- Cover at most the 3-4 most substantial topics. Drop minor ones rather than listing everything --
a shorter reply covering what matters beats a longer one covering everything.
- Keep the ENTIRE reply under roughly 700 characters by default.
- No preamble ("Here's a summary..."), no closing remarks.
- Reply in this language: {reply_language}.
- If, after filtering, there is nothing substantive to report, say so briefly in that language \
(one short sentence, no heading).
"""

MAP_PROMPT = """\
Below is PART {part} of {total} of a chat transcript (chronological, format "[HH:MM] Name: text" \
or "[YYYY-MM-DD HH:MM] Name: text").
Extract candidate discussion topics from this part only, as compact notes for later merging with the \
other parts. For each candidate topic note: approximate start time/date, a short title, who weighed \
in (names or just "several people"), and the key points/positions raised. Drop obvious pure spam, \
single-word reactions, and greetings, but otherwise keep enough detail that a later step can judge \
significance and write conclusions -- do not aggressively filter yet, that happens later.
{focus_note}
Transcript part:
{transcript}
"""

FINAL_FROM_TRANSCRIPT_PROMPT = """\
Chat: {chat_title}
Period: {period_label}

Full transcript for this period (chronological, format "[HH:MM] Name: text" or \
"[YYYY-MM-DD HH:MM] Name: text"):
{transcript}
"""

FINAL_FROM_NOTES_PROMPT = """\
Chat: {chat_title}
Period: {period_label}

The transcript was too long to process in one pass, so it was split into {total} parts and each \
part was pre-processed into topic notes below. Some topics may continue across multiple parts \
(same subject discussed earlier and later) -- merge those into a single entry. Now apply the full \
filtering and formatting rules to produce the final summary.

Topic notes from each part:
{notes}
"""


def _build_system_prompt(
    style: str,
    focus_user: str | None,
    reply_language: str | None,
    topic_hint: str | None = None,
    length_hint: str | None = None,
    original_question: str | None = None,
) -> str:
    assert style in VALID_STYLES, f"internal bug: unknown style {style!r}, expected one of {VALID_STYLES}"
    parts = [TOPIC_ONLY_RULES.format(topic_hint=topic_hint) if topic_hint else BASE_RULES]
    if focus_user:
        parts.append(FOCUS_USER_RULES.format(focus_user=focus_user))
    if style == "reply":
        parts.append(REPLY_STYLE_RULES.format(reply_language=reply_language or "the request's language"))
    else:
        parts.append(FILE_STYLE_RULES)
    if length_hint:
        parts.append(LENGTH_HINT_RULE.format(length_hint=length_hint))
    if original_question:
        parts.append(DIRECT_QUESTION_RULE.format(original_question=original_question))
    return "\n".join(parts)


def _get_encoder(model: str):
    try:
        import tiktoken

        try:
            return tiktoken.encoding_for_model(model)
        except KeyError:
            return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def _count_tokens(text: str, encoder) -> int:
    if encoder is None:
        return max(1, len(text) // 4)
    return len(encoder.encode(text))


def chunk_transcript(lines: list[str], max_tokens: int, model: str) -> list[str]:
    assert max_tokens > 0, f"internal bug: max_tokens must be positive, got {max_tokens}"

    encoder = _get_encoder(model)
    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0

    for line in lines:
        t = _count_tokens(line, encoder)
        if current and current_tokens + t > max_tokens:
            chunks.append("\n".join(current))
            current = []
            current_tokens = 0
        current.append(line)
        current_tokens += t

    if current:
        chunks.append("\n".join(current))

    assert sum(c.count("\n") + 1 for c in chunks) == len(lines), (
        "internal bug: chunking lost or duplicated lines"
    )
    return chunks


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
        raise ChatSummaryError("OpenAI returned an empty summary -- try again or use a different model.")
    return content.strip()


def summarize_transcript(
    api_key: str,
    model: str,
    chat_title: str,
    period_label: str,
    lines: list[str],
    focus_user: str | None = None,
    style: str = "file",
    reply_language: str | None = None,
    topic_hint: str | None = None,
    length_hint: str | None = None,
    original_question: str | None = None,
    max_chunk_tokens: int = 6000,
) -> str:
    if style not in VALID_STYLES:
        raise ChatSummaryError(f"Unknown summary style '{style}', expected one of {VALID_STYLES}.")
    if not api_key or not api_key.strip():
        raise ChatSummaryError("OpenAI API key is missing.")
    if not model or not model.strip():
        raise ChatSummaryError("OpenAI model name is missing.")

    no_content_msg = (
        "No noteworthy topics were discussed in this period."
        if style == "file"
        else "Nothing noteworthy to summarize."
    )
    if not lines:
        return no_content_msg

    client = OpenAI(api_key=api_key)
    system_prompt = _build_system_prompt(style, focus_user, reply_language, topic_hint, length_hint, original_question)
    chunks = chunk_transcript(lines, max_chunk_tokens, model)

    if len(chunks) == 1:
        prompt = FINAL_FROM_TRANSCRIPT_PROMPT.format(
            chat_title=chat_title, period_label=period_label, transcript=chunks[0]
        )
        return _chat(client, model, system_prompt, prompt)

    focus_note = ""
    if focus_user:
        focus_note += f"\nOnly keep notes relevant to {focus_user}.\n"
    if topic_hint:
        focus_note += f"\nOnly keep notes relevant to this specific topic/event: {topic_hint}\n"
    if original_question:
        focus_note += f"\nThe user's actual question was: \"{original_question}\" -- keep any details that help answer it.\n"
    notes_parts = []
    for i, chunk in enumerate(chunks, start=1):
        prompt = MAP_PROMPT.format(part=i, total=len(chunks), transcript=chunk, focus_note=focus_note)
        notes = _chat(client, model, system_prompt, prompt)
        notes_parts.append(f"--- Part {i} notes ---\n{notes}")

    combined_notes = "\n\n".join(notes_parts)
    final_prompt = FINAL_FROM_NOTES_PROMPT.format(
        chat_title=chat_title,
        period_label=period_label,
        total=len(chunks),
        notes=combined_notes,
    )
    return _chat(client, model, system_prompt, final_prompt)
