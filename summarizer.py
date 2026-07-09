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
This will be sent directly as a Telegram chat message reply, not saved as a document, so:
- Do NOT use "## " headings. For each topic write one short paragraph starting with a **bold** \
title (Telethon markdown: **bold**, __italic__, `code`), e.g. "**Topic title** — summary sentence(s). \
Conclusion: ...".
- Keep the ENTIRE reply well under 3500 characters. If there are many topics, keep only the most \
substantial ones and drop minor ones rather than writing something too long.
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


def _build_system_prompt(style: str, focus_user: str | None, reply_language: str | None) -> str:
    assert style in VALID_STYLES, f"internal bug: unknown style {style!r}, expected one of {VALID_STYLES}"
    parts = [BASE_RULES]
    if focus_user:
        parts.append(FOCUS_USER_RULES.format(focus_user=focus_user))
    if style == "reply":
        parts.append(REPLY_STYLE_RULES.format(reply_language=reply_language or "the request's language"))
    else:
        parts.append(FILE_STYLE_RULES)
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
    system_prompt = _build_system_prompt(style, focus_user, reply_language)
    chunks = chunk_transcript(lines, max_chunk_tokens, model)

    if len(chunks) == 1:
        prompt = FINAL_FROM_TRANSCRIPT_PROMPT.format(
            chat_title=chat_title, period_label=period_label, transcript=chunks[0]
        )
        return _chat(client, model, system_prompt, prompt)

    focus_note = f"\nOnly keep notes relevant to {focus_user}.\n" if focus_user else ""
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
