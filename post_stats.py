"""Per-post stats (views, forwards, reactions, comments) for a Telegram channel/group
over a chosen date range -- the data layer for the standalone post-stats desktop app.
A second, separate report renderer + Tkinter GUI consumes PostStat/fetch_post_stats
directly, so the shapes here are a contract, not an implementation detail."""

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from telethon.errors import RPCError

from errors import ChatSummaryError
from telegram_fetch import describe_media, is_image_message, is_video_message, resolve_chat

_WHITESPACE_RE = re.compile(r"\s+")
_PREVIEW_MAX_LEN = 160


@dataclass
class PostStat:
    message_id: int
    date: datetime
    text_preview: str
    thumbnail_path: Path | None
    views: int
    forwards: int
    reactions_total: int
    reactions_breakdown: dict
    comments: int
    is_edited: bool
    media_type: str
    link: str


def post_link(entity, message_id: int) -> str:
    """Public-permalink form when entity.username is set/truthy; otherwise Telegram's
    private deep-link form using entity.id. Telethon already gives Channel.id as the
    bare internal id (no -100 prefix), which is exactly the form the /c/ link needs --
    do not strip or transform entity.id further.

    Pure function, no network calls -- must work against a stub object that only has
    .username and .id attributes."""
    if getattr(entity, "username", None):
        return f"https://t.me/{entity.username}/{message_id}"
    return f"https://t.me/c/{entity.id}/{message_id}"


def _build_text_preview(text) -> str:
    """First non-blank line of msg.text, whitespace-collapsed, truncated to 160 chars
    with a trailing "…" if truncated. "" if text is empty/None."""
    if not text:
        return ""
    first_line = ""
    for line in text.splitlines():
        if line.strip():
            first_line = line.strip()
            break
    if not first_line:
        return ""
    collapsed = _WHITESPACE_RE.sub(" ", first_line).strip()
    if len(collapsed) > _PREVIEW_MAX_LEN:
        return collapsed[:_PREVIEW_MAX_LEN] + "…"
    return collapsed


def _reaction_key(reaction) -> str:
    """A ReactionEmoji has a plain-emoji .emoticon string; a ReactionCustomEmoji
    (custom sticker-set reaction) has none -- key those on str(.document_id) if
    present, else fall back to "?" rather than crashing."""
    emoticon = getattr(reaction, "emoticon", None)
    if emoticon:
        return emoticon
    doc_id = getattr(reaction, "document_id", None)
    if doc_id is not None:
        return str(doc_id)
    return "?"


def _reactions_breakdown(reactions) -> dict:
    if reactions is None:
        return {}
    breakdown: dict = {}
    for result in getattr(reactions, "results", None) or []:
        key = _reaction_key(result.reaction)
        breakdown[key] = breakdown.get(key, 0) + (getattr(result, "count", 0) or 0)
    return breakdown


def _media_shim(msg):
    """telegram_fetch's is_image_message/is_video_message/describe_media all access
    msg.photo, msg.document, msg.sticker, etc. as plain attributes (no getattr) --
    real Telethon messages always have every one of those set, but a lightweight test
    stub may only set the couple of fields a given test cares about. Building a small
    stand-in with every field defensively defaulted keeps that reuse safe against a
    minimal stub without having to touch telegram_fetch.py itself."""
    return SimpleNamespace(
        photo=getattr(msg, "photo", None),
        video=getattr(msg, "video", None),
        document=getattr(msg, "document", None),
        sticker=getattr(msg, "sticker", None),
        gif=getattr(msg, "gif", None),
        voice=getattr(msg, "voice", None),
        video_note=getattr(msg, "video_note", None),
        contact=getattr(msg, "contact", None),
        geo=getattr(msg, "geo", None),
        poll=getattr(msg, "poll", None),
        file=getattr(msg, "file", None),
    )


def _media_type(msg) -> str:
    """"album" takes priority when msg.grouped_id is set (regardless of photo/video),
    else "photo"/"video" via telegram_fetch's own checks, else "other" if any other
    media attribute is set (describe_media(msg) non-empty), else "none"."""
    if getattr(msg, "grouped_id", None) is not None:
        return "album"
    shim = _media_shim(msg)
    if is_image_message(shim):
        return "photo"
    if is_video_message(shim):
        return "video"
    if describe_media(shim):
        return "other"
    return "none"


def _message_to_post_stat(msg, entity, thumbnail_path: Path | None) -> "PostStat":
    """Pure mapping from a Telethon Message (or a stand-in with the same attributes) to
    a PostStat. No network/IO here -- this is the seam tests exercise directly with a
    stub `msg` object. Every field is read defensively (getattr with a None/False
    default) so a minimal stub that only sets a few attributes doesn't crash."""
    reactions = getattr(msg, "reactions", None)
    replies = getattr(msg, "replies", None)

    return PostStat(
        message_id=msg.id,
        date=msg.date,
        text_preview=_build_text_preview(getattr(msg, "text", None)),
        thumbnail_path=thumbnail_path,
        views=getattr(msg, "views", None) or 0,
        forwards=getattr(msg, "forwards", None) or 0,
        reactions_total=sum(
            (getattr(r, "count", 0) or 0) for r in (getattr(reactions, "results", None) or [])
        ) if reactions is not None else 0,
        reactions_breakdown=_reactions_breakdown(reactions),
        comments=(getattr(replies, "replies", 0) or 0) if replies is not None else 0,
        is_edited=getattr(msg, "edit_date", None) is not None,
        media_type=_media_type(msg),
        link=post_link(entity, msg.id),
    )


async def fetch_post_stats(
    client, chat_ref, start: datetime, end: datetime, thumb_dir: Path, log=print,
) -> list["PostStat"]:
    """One PostStat per post in `chat_ref` with msg.date in [start, end) (both
    UTC-aware), for the resolved entity of `chat_ref` -- if `chat_ref` is a str,
    resolve it via telegram_fetch.resolve_chat, otherwise treat it as an
    already-resolved entity, exactly like telegram_fetch.fetch_range_messages does.

    Returns results in whatever order Telethon yields them (newest-first) -- sorting
    by whichever metric is the caller's job (the report renderer)."""
    entity = chat_ref if not isinstance(chat_ref, str) else await resolve_chat(client, chat_ref)

    thumb_dir.mkdir(parents=True, exist_ok=True)

    results: list[PostStat] = []
    try:
        async for msg in client.iter_messages(entity, offset_date=end, reverse=False):
            if msg.date < start:
                break
            if msg.date >= end:
                continue
            if msg.action is not None:
                continue  # service message (join/leave/pin/etc.)

            sender = await msg.get_sender()
            if getattr(sender, "bot", False):
                continue  # a bot's own post (e.g. this repo's own listener replies)
                # isn't a real "post" to analyze -- same skip as fetch_range_messages

            thumbnail_path = None
            if is_image_message(msg) or is_video_message(msg):
                dest = thumb_dir / f"{msg.id}.jpg"
                try:
                    await client.download_media(msg, thumb=-1, file=str(dest))
                    thumbnail_path = dest
                except Exception as e:
                    log(f"[post_stats] thumbnail download failed for message {msg.id}: {e}")
                    thumbnail_path = None

            results.append(_message_to_post_stat(msg, entity, thumbnail_path))
    except RPCError as e:
        raise ChatSummaryError(f"Telegram rejected reading posts for stats: {e}") from e

    return results
