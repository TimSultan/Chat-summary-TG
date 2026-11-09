"""Turns what an administrator wrote to the bot into text + entities the bot can post.

A message reaches the bot as plain text plus a list of entities -- the bold, the links,
the spoilers the sender's app put on it. Reading only `text` (which the announcement flow
used to do) keeps the words and loses everything else: a hidden link becomes its bare
label, the address gone. So the entities are kept and handed back to sendMessage.

On top of that, Markdown typed out by hand is converted, because not every Telegram app
turns it into formatting on send: `[текст](https://…)` (or `[текст](t.me/…)`, `[текст](@user)`)
becomes a link, `**жирный**`, `__курсив__`, `~~зачёркнутый~~`, `||спойлер||` and `` `код` ``
become what they say. Single `*` and `_` are left alone on purpose -- they turn up in
usernames and asterisk footnotes far more often than as formatting.

Entity offsets are in UTF-16 code units, as Telegram counts them, so an emoji before a
link must not shift the link by a character. Everything here is pure.
"""

import bisect
import re

# Telegram re-detects these on its own; kept anyway so the post looks like the draft.
# custom_emoji is the one dropped: a bot without a Fragment username may not send it, and
# the emoji character it decorates stays in the text.
_DROPPED_TYPES = frozenset({"custom_emoji"})
# Markdown inside these is shown as typed, never converted.
_LITERAL_TYPES = frozenset({"code", "pre"})
_MAX_PASSES = 4  # each pass converts the outermost layer: **[link](url)** takes two

_SINGLE_LINE = r"(?!\s)[^\n]+?(?<!\s)"
_MARKDOWN = re.compile(
    r"```(?P<pre>[^`][\s\S]*?)```"
    r"|`(?P<code>[^`\n]+)`"
    r"|\[(?P<label>[^\[\]\n]+?)\]\((?P<url>[^\s()]+)\)"
    rf"|\*\*(?P<bold>{_SINGLE_LINE})\*\*"
    rf"|__(?P<italic>{_SINGLE_LINE})__"
    rf"|~~(?P<strikethrough>{_SINGLE_LINE})~~"
    rf"|\|\|(?P<spoiler>{_SINGLE_LINE})\|\|"
)
_PLAIN_KINDS = ("pre", "code", "bold", "italic", "strikethrough", "spoiler")

_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):")
_ALLOWED_SCHEMES = frozenset({"http", "https", "tg", "mailto"})
_DOMAIN = re.compile(r"^(?:[\w-]+\.)+[a-zA-Z]{2,}(?:[/?#]\S*)?$")
_USERNAME = re.compile(r"^@([A-Za-z]\w{3,31})$")


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def link_target(raw: str) -> str | None:
    """The address `[label](raw)` should open, or None when `raw` is not one -- the
    brackets then stay as typed rather than turning into a link Telegram would refuse.
    A bare domain gets https://, an @username its t.me page; javascript: and the like
    are never links."""
    raw = (raw or "").strip()
    scheme = _SCHEME.match(raw)
    if scheme:
        return raw if scheme.group(1).lower() in _ALLOWED_SCHEMES and len(raw) > len(scheme.group(0)) else None
    username = _USERNAME.match(raw)
    if username:
        return f"https://t.me/{username.group(1)}"
    if _DOMAIN.match(raw):
        return f"https://{raw}"
    return None


def _utf16_starts(text: str) -> list[int]:
    """starts[i] is where character i begins in UTF-16; the last item is the length."""
    starts = [0]
    for character in text:
        starts.append(starts[-1] + (2 if ord(character) > 0xFFFF else 1))
    return starts


def _to_spans(text: str, entities) -> list[tuple[int, int, dict]]:
    """Entities as (start, end, the rest of the entity) in Python string indices."""
    starts = _utf16_starts(text)
    spans = []
    for entity in entities or ():
        if not isinstance(entity, dict) or entity.get("type") in _DROPPED_TYPES:
            continue
        try:
            offset, length = int(entity["offset"]), int(entity["length"])
        except (KeyError, TypeError, ValueError):
            continue
        start = bisect.bisect_left(starts, offset)
        end = bisect.bisect_left(starts, offset + length)
        rest = {k: v for k, v in entity.items() if k not in ("offset", "length")}
        if end > start and rest.get("type"):
            spans.append((start, min(end, len(text)), rest))
    return spans


def _to_entities(text: str, spans) -> list[dict]:
    starts = _utf16_starts(text)
    entities = []
    for start, end, rest in sorted(spans, key=lambda span: (span[0], -span[1])):
        entity = dict(rest)
        entity["offset"] = starts[start]
        entity["length"] = starts[end] - starts[start]
        entities.append(entity)
    return entities


def _cut(text: str, spans, deletions, additions=()) -> tuple[str, list]:
    """Removes the `deletions` ranges from `text` and moves every span to match. A span
    reaching into a removed range is shortened; one left with nothing is dropped."""
    deletions = sorted((s, e) for s, e in deletions if e > s)

    def moved(position: int) -> int:
        removed = 0
        for start, end in deletions:
            if end <= position:
                removed += end - start
            elif start < position:
                removed += position - start
            else:
                break
        return position - removed

    pieces, cursor = [], 0
    for start, end in deletions:
        pieces.append(text[cursor:start])
        cursor = max(cursor, end)
    pieces.append(text[cursor:])
    kept = []
    for start, end, rest in list(spans) + list(additions):
        new_start, new_end = moved(start), moved(end)
        if new_end > new_start:
            kept.append((new_start, new_end, rest))
    return "".join(pieces), kept


def _markdown_pass(text: str, spans) -> tuple[str, list, bool]:
    literal = [(s, e) for s, e, rest in spans if rest.get("type") in _LITERAL_TYPES]
    deletions, additions = [], []
    for match in _MARKDOWN.finditer(text):
        start, end = match.span()
        if any(s < end and start < e for s, e in literal):
            continue
        if match.group("label") is not None:
            url = link_target(match.group("url"))
            if url is None:
                continue
            inner_start, inner_end = match.span("label")
            additions.append((inner_start, inner_end, {"type": "text_link", "url": url}))
        else:
            kind = next(k for k in _PLAIN_KINDS if match.group(k) is not None)
            inner_start, inner_end = match.span(kind)
            additions.append((inner_start, inner_end, {"type": kind}))
            if kind in _LITERAL_TYPES:
                literal.append((start, end))
        deletions += [(start, inner_start), (inner_end, end)]
    if not additions:
        return text, spans, False
    text, spans = _cut(text, spans, deletions, additions)
    return text, spans, True


def prepare(text: str | None, entities=None) -> tuple[str, list[dict]]:
    """(text, entities) ready for sendMessage's `text`/`entities` or sendPhoto's
    `caption`/`caption_entities`: trimmed, the sender's own formatting kept, typed
    Markdown converted. Empty text comes back as ("", [])."""
    text = text or ""
    spans = _to_spans(text, entities)
    stripped = text.strip()
    if not stripped:
        return "", []
    lead = len(text) - len(text.lstrip())
    trail = len(text) - len(text.rstrip())
    text, spans = _cut(text, spans, [(0, lead), (len(text) - trail, len(text))])
    for _ in range(_MAX_PASSES):
        text, spans, changed = _markdown_pass(text, spans)
        if not changed:
            break
    return text, _to_entities(text, spans)
