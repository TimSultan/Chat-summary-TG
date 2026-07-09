"""Parses a short, free-form, possibly mixed-language chat request like
'@sultan_kembayev summary что обсуждали сегодня' or 'summary сообщения @user за сегодня'
into structured parameters (scope, target user, date range, reply language) via the LLM."""

import json
from datetime import date

from openai import OpenAI, OpenAIError

from errors import ChatSummaryError

SYSTEM_PROMPT = """\
You turn a short chat message requesting a conversation summary into structured parameters.
The message may mix languages (e.g. Russian and English) and may or may not explicitly name \
a target user. You are given today's reference date and any usernames mentioned in the message.

Return ONLY a JSON object with these fields:
- "scope": "chat" if it's asking to summarize the whole conversation/what everyone discussed, or \
"user" if it's specifically asking about one particular person -- what they said, or a situation/\
drama/topic involving them (e.g. "the situation with Anzhelika", "what happened with @bob").
- "target_username": the username (without @) to focus on if scope is "user" and the person was \
referenced with an @mention, else null. Prefer one of the given mentioned_usernames if the request \
names a person that way.
- "target_name_hint": if scope is "user" but the person was referenced by a plain name/nickname \
instead of (or in addition to) an @mention -- possibly misspelled, abbreviated, or transliterated \
from another script/language (e.g. "Anzhelika" for a Cyrillic "Анжелика") -- put that literal name \
text here exactly as written in the request. Else null.
- "topic_hint": if the request is about ONE specific topic/event/situation rather than a general \
"what did everyone discuss" summary -- e.g. "the last conflict", "the argument about the venue", \
"what got decided about the trip", "the drama earlier" -- put a short description of that topic \
here (your own paraphrase is fine). This can be combined with a person (e.g. "the conflict with \
Anzhelika"). Else null. Do NOT set this for a plain "what happened today"/"summarize the chat" \
request -- that should get every notable topic, not just one.
- "start_date": ISO date (YYYY-MM-DD) the summary period starts.
- "end_date": ISO date (YYYY-MM-DD) the summary period ends (inclusive). Equal to start_date for \
a single day.
- "length_hint": if the request explicitly asks for a particular length or level of detail (e.g. \
"very short", "brief", "one sentence", "just the highlights", "in detail", "подробнее"), put that \
instruction here (verbatim or paraphrased). Else null.
- "reply_language": your best-guess ISO 639-1 language code for how the reply should be written, \
based on the language of the request message (e.g. "ru", "en").

Interpret relative dates (today/сегодня, yesterday/вчера, this week/за неделю/на этой неделе, \
last N days, etc.) using the given reference date. If no time period is mentioned at all, default \
to just the reference date (today).
"""

USER_PROMPT_TEMPLATE = """\
Reference date: {reference_date}
Mentioned usernames in the message (excluding whoever is being asked to summarize): {mentioned}
Request message: {text}
"""


def parse_summary_request(
    api_key: str,
    model: str,
    text: str,
    reference_date: date,
    mentioned_usernames: list[str],
) -> dict:
    if not text or not text.strip():
        raise ChatSummaryError("Cannot parse an empty summary request.")
    assert isinstance(mentioned_usernames, list), "internal bug: mentioned_usernames must be a list"

    client = OpenAI(api_key=api_key)
    prompt = USER_PROMPT_TEMPLATE.format(
        reference_date=reference_date.isoformat(),
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
        raise ChatSummaryError(f"OpenAI API call failed while parsing the request: {e}") from e

    content = response.choices[0].message.content
    if not content:
        raise ChatSummaryError("Intent parser returned an empty response from the model.")
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise ChatSummaryError(f"Intent parser returned invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ChatSummaryError(f"Intent parser returned a JSON {type(data).__name__}, expected an object.")

    scope = data.get("scope") if data.get("scope") in ("chat", "user") else "chat"
    target_username = (data.get("target_username") or None)
    if target_username:
        target_username = target_username.lstrip("@")
    if scope == "user" and not target_username and len(mentioned_usernames) == 1:
        target_username = mentioned_usernames[0]

    target_name_hint = (data.get("target_name_hint") or None)
    if isinstance(target_name_hint, str):
        target_name_hint = target_name_hint.strip() or None

    if scope == "user" and not target_username and not target_name_hint:
        scope = "chat"  # nothing to focus on, fall back to whole-chat summary

    topic_hint = (data.get("topic_hint") or None)
    if isinstance(topic_hint, str):
        topic_hint = topic_hint.strip() or None

    length_hint = (data.get("length_hint") or None)
    if isinstance(length_hint, str):
        length_hint = length_hint.strip() or None

    def _parse(value, fallback):
        try:
            return date.fromisoformat(value)
        except (TypeError, ValueError):
            return fallback

    start_date = _parse(data.get("start_date"), reference_date)
    end_date = _parse(data.get("end_date"), reference_date)
    if end_date < start_date:
        start_date, end_date = end_date, start_date

    return {
        "scope": scope,
        "target_username": target_username,
        "target_name_hint": target_name_hint,
        "topic_hint": topic_hint,
        "length_hint": length_hint,
        "start_date": start_date,
        "end_date": end_date,
        "reply_language": data.get("reply_language") or "en",
    }


RESOLVE_SYSTEM_PROMPT = """\
You match a person's name (possibly misspelled, abbreviated, a nickname, or written in a \
different script/transliteration -- e.g. "Anzhelika" for a Cyrillic "Анжелика") against a list \
of actual chat participant names/usernames, and return which one it refers to.

Return ONLY a JSON object: {"match": "<exact candidate string, copied verbatim>"} if you're \
reasonably confident one candidate is who's meant, or {"match": null} if none plausibly match.
"""

RESOLVE_USER_TEMPLATE = """\
Name reference: {hint}
Candidates: {candidates}
"""


def resolve_name_hint(api_key: str, model: str, hint: str, candidates: list[str]) -> str | None:
    """Best-effort match of a free-text (possibly misspelled/transliterated) person
    reference against a chat's actual participants. Used when a request names someone
    without an @mention, e.g. "the situation with Anzhelika". Returns the matching
    candidate string verbatim, or None if nothing matched confidently."""
    if not hint or not candidates:
        return None

    client = OpenAI(api_key=api_key)
    prompt = RESOLVE_USER_TEMPLATE.format(hint=hint, candidates=", ".join(candidates))
    try:
        response = client.chat.completions.create(
            model=model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": RESOLVE_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
    except OpenAIError as e:
        raise ChatSummaryError(f"OpenAI API call failed while matching a participant name: {e}") from e

    content = response.choices[0].message.content
    if not content:
        raise ChatSummaryError("Name resolver returned an empty response from the model.")
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise ChatSummaryError(f"Name resolver returned invalid JSON: {e}") from e

    match = data.get("match") if isinstance(data, dict) else None
    # Guard against a hallucinated name that isn't actually one of the candidates.
    return match if match in candidates else None
