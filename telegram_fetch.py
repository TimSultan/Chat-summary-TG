"""Fetches Telegram chat history for a date range via Telethon (user session)."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from telethon import TelegramClient
from telethon.errors import RPCError
from telethon.tl.types import Channel, Chat, User

import transcript_cache
from errors import ChatSummaryError


@dataclass
class ChatMessage:
    message_id: int
    dt_local: datetime
    sender_name: str
    sender_username: Optional[str]
    sender_id: Optional[int]
    text: str
    is_reply: bool


def describe_media(msg) -> str:
    if msg.photo:
        return "[Photo]"
    if msg.video_note:
        return "[Video note]"
    if msg.video:
        return "[Video]"
    if msg.voice:
        return "[Voice message]"
    if msg.gif:
        return "[GIF]"
    if msg.sticker:
        alt = ""
        for attr in getattr(msg.sticker, "attributes", []) or []:
            if hasattr(attr, "alt") and attr.alt:
                alt = attr.alt
                break
        return f"[Sticker {alt}]".strip()
    if msg.contact:
        return "[Contact shared]"
    if msg.geo:
        return "[Location shared]"
    if msg.poll:
        question = getattr(msg.poll.poll, "question", "") if msg.poll else ""
        return f"[Poll: {question}]" if question else "[Poll]"
    if msg.document:
        fname = None
        if msg.file:
            fname = msg.file.name
        return f"[File: {fname or 'document'}]"
    return ""


def sender_display_name(sender) -> str:
    if sender is None:
        return "Unknown"
    if isinstance(sender, (Channel, Chat)):
        return getattr(sender, "title", None) or "Unknown channel"
    if isinstance(sender, User):
        parts = [sender.first_name, sender.last_name]
        name = " ".join(p for p in parts if p)
        if name:
            return name
        if sender.username:
            return f"@{sender.username}"
        return f"id{sender.id}"
    return "Unknown"


async def resolve_chat(client: TelegramClient, chat_ref: str):
    """Resolve a chat by username/id/phone, falling back to a case-insensitive
    substring match against dialog titles for plain group names."""
    if not chat_ref or not chat_ref.strip():
        raise ChatSummaryError("Chat cannot be empty -- give a username, numeric ID, title, or invite link.")

    try:
        candidate: Optional[str] = chat_ref
        if candidate.lstrip("-").isdigit():
            candidate = int(candidate)
        return await client.get_entity(candidate)
    except ChatSummaryError:
        raise
    except ValueError as e:
        if "not part of" in str(e).lower():
            raise ChatSummaryError(
                f"You're not a member of '{chat_ref}' yet -- open the invite link in Telegram "
                "and join the chat first, then try again."
            ) from e
        # some other unresolvable string (bad username, etc.) -- fall through to title search
    except Exception:
        pass

    needle = chat_ref.strip().lower()
    matches = []
    async for dialog in client.iter_dialogs():
        if needle in dialog.name.lower():
            matches.append(dialog)

    if not matches:
        raise ChatSummaryError(
            f"Could not find any chat matching '{chat_ref}'. Make sure you're a member of it "
            "and the name/username/ID is correct."
        )
    if len(matches) > 1:
        names = "\n".join(f"  - {d.name}" for d in matches)
        raise ChatSummaryError(
            f"'{chat_ref}' matches multiple chats, be more specific:\n{names}"
        )
    return matches[0].entity


async def fetch_range_messages(
    client: TelegramClient,
    chat_ref,
    start_day: date,
    end_day: date,
    tz,
):
    """Returns (chat_title, list[ChatMessage]) for local calendar days
    [start_day, end_day] (inclusive) in timezone `tz`. `chat_ref` may be a
    resolvable string (username/id/title substring) or an already-resolved entity."""
    if start_day > end_day:
        raise ChatSummaryError(f"start_day ({start_day}) is after end_day ({end_day}).")

    entity = chat_ref if not isinstance(chat_ref, str) else await resolve_chat(client, chat_ref)
    chat_title = getattr(entity, "title", None) or sender_display_name(entity)

    start_local = datetime.combine(start_day, time.min, tzinfo=tz)
    end_local = datetime.combine(end_day, time.min, tzinfo=tz) + timedelta(days=1)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)
    assert start_utc < end_utc, "computed UTC range must be non-empty"

    messages: list[ChatMessage] = []
    try:
        async for msg in client.iter_messages(entity, offset_date=end_utc, reverse=False):
            if msg.date < start_utc:
                break
            if msg.date >= end_utc:
                continue
            if msg.action is not None:
                continue  # service message (join/leave/pin/etc.)

            sender = await msg.get_sender()
            if getattr(sender, "bot", False):
                continue  # a bot's own reply (e.g. bot_listener.py's) isn't chat content --
                # caching it would feed a summary its own earlier output as if it were a topic

            name = sender_display_name(sender)
            username = getattr(sender, "username", None)
            sender_id = getattr(sender, "id", None)

            text = (msg.text or "").replace("\n", " ").strip()
            media_tag = describe_media(msg)
            body = " ".join(p for p in (media_tag, text) if p)
            if not body:
                continue

            messages.append(
                ChatMessage(
                    message_id=msg.id,
                    dt_local=msg.date.astimezone(tz),
                    sender_name=name,
                    sender_username=username,
                    sender_id=sender_id,
                    text=body,
                    is_reply=bool(msg.is_reply),
                )
            )
    except RPCError as e:
        raise ChatSummaryError(f"Telegram rejected reading history for '{chat_title}': {e}") from e

    messages.reverse()  # chronological order
    return chat_title, messages


async def fetch_new_messages(client: TelegramClient, chat_ref, tz, min_id: int):
    """Fetches only messages with a Telegram message ID greater than `min_id`, in
    chronological order -- used by fetch_range_messages_cached to incrementally extend
    an already-cached transcript instead of re-fetching (and re-resolving every sender
    for) a whole day from scratch on every cache refresh. Message IDs are strictly
    increasing over time within a chat, so this is exact: nothing is missed or
    duplicated as long as `min_id` really is the highest ID already cached.

    Trade-off: this only picks up messages that didn't exist yet as of the previous
    fetch -- an edit or deletion of an already-cached message is NOT reflected (a full
    day re-fetch would happen to catch those as a side effect, at the cost of re-fetching
    everything to do it). For a chat summary tool, catching up on new activity cheaply
    matters far more than reflecting a same-day edit, so this is deliberate."""
    entity = chat_ref if not isinstance(chat_ref, str) else await resolve_chat(client, chat_ref)
    chat_title = getattr(entity, "title", None) or sender_display_name(entity)

    messages: list[ChatMessage] = []
    try:
        async for msg in client.iter_messages(entity, min_id=min_id, reverse=True):
            if msg.action is not None:
                continue  # service message (join/leave/pin/etc.)

            sender = await msg.get_sender()
            if getattr(sender, "bot", False):
                continue  # see fetch_range_messages -- a bot's own reply isn't chat content

            name = sender_display_name(sender)
            username = getattr(sender, "username", None)
            sender_id = getattr(sender, "id", None)

            text = (msg.text or "").replace("\n", " ").strip()
            media_tag = describe_media(msg)
            body = " ".join(p for p in (media_tag, text) if p)
            if not body:
                continue

            messages.append(
                ChatMessage(
                    message_id=msg.id,
                    dt_local=msg.date.astimezone(tz),
                    sender_name=name,
                    sender_username=username,
                    sender_id=sender_id,
                    text=body,
                    is_reply=bool(msg.is_reply),
                )
            )
    except RPCError as e:
        raise ChatSummaryError(f"Telegram rejected reading new messages for '{chat_title}': {e}") from e

    return chat_title, messages


def _message_to_dict(m: ChatMessage) -> dict:
    return {
        "message_id": m.message_id,
        "dt_local": m.dt_local.isoformat(),
        "sender_name": m.sender_name,
        "sender_username": m.sender_username,
        "sender_id": m.sender_id,
        "text": m.text,
        "is_reply": m.is_reply,
    }


def _message_from_dict(d: dict) -> ChatMessage:
    return ChatMessage(
        # Cache files written before incremental fetching (see fetch_new_messages) have
        # no "message_id" -- 0 is a safe placeholder for those since callers that need a
        # trustworthy id gate on has_message_ids() first rather than relying on this.
        message_id=d.get("message_id", 0),
        dt_local=datetime.fromisoformat(d["dt_local"]),
        sender_name=d["sender_name"],
        sender_username=d["sender_username"],
        sender_id=d["sender_id"],
        text=d["text"],
        is_reply=d["is_reply"],
    )


def _has_message_ids(dicts: list[dict]) -> bool:
    """False for cache entries written before message_id was tracked (or empty ones) --
    both cases must fall back to a full re-fetch instead of an incremental one, since
    there's no trustworthy id to resume from (see fetch_range_messages_cached)."""
    return bool(dicts) and all("message_id" in d for d in dicts)


async def fetch_range_messages_cached(
    client: TelegramClient,
    chat_ref,
    start_day: date,
    end_day: date,
    tz,
    log=print,
    force_refresh: bool = False,
):
    """Like `fetch_range_messages`, but fetches one calendar day at a time and caches
    each day's raw transcript to disk (transcript_cache.py). A day already cached and
    fresh is reused with no Telegram call at all -- permanently for closed-out past days,
    or within the last `transcript_cache.TODAY_TTL_SECONDS` for today. Different
    questions about the same day therefore share one Telegram fetch; only the
    (always-fresh) LLM call varies per question.

    A stale-but-present cache for today is NOT thrown away and re-fetched from scratch --
    only messages newer than the last cached one are fetched (fetch_new_messages) and
    appended, since re-resolving every sender for a whole active day on every refresh is
    the main cost here. `force_refresh=True` bypasses all of this and re-fetches every
    day clean, regardless of freshness."""
    if start_day > end_day:
        raise ChatSummaryError(f"start_day ({start_day}) is after end_day ({end_day}).")

    entity = chat_ref if not isinstance(chat_ref, str) else await resolve_chat(client, chat_ref)
    chat_title = getattr(entity, "title", None) or sender_display_name(entity)

    all_messages: list[ChatMessage] = []
    day = start_day
    while day <= end_day:
        is_final = transcript_cache.day_is_final(day, tz)
        cached = None if force_refresh else transcript_cache.load(entity.id, day, is_final)

        if cached is not None and cached.is_fresh:
            log(f"[cache] using saved transcript for {day}")
            all_messages.extend(_message_from_dict(d) for d in cached.messages)
        elif cached is not None and _has_message_ids(cached.messages):
            last_id = max(d["message_id"] for d in cached.messages)
            log(f"[cache] refreshing {day}: fetching messages newer than id={last_id}")
            title, new_messages = await fetch_new_messages(client, entity, tz, min_id=last_id)
            if title:
                chat_title = title
            merged = cached.messages + [_message_to_dict(m) for m in new_messages]
            transcript_cache.save(entity.id, day, merged)
            all_messages.extend(_message_from_dict(d) for d in merged)
            log(f"[cache] appended {len(new_messages)} new message(s) for {day} ({len(merged)} total)")
        else:
            # No cache at all yet, or a stale cache in the old pre-message_id format --
            # either way there's nothing trustworthy to resume from, so bootstrap with a
            # full fetch (which also upgrades the cache file to the new format).
            log(f"[cache] fetching {day} from Telegram...")
            title, day_messages = await fetch_range_messages(client, entity, day, day, tz)
            if title:
                chat_title = title
            transcript_cache.save(entity.id, day, [_message_to_dict(m) for m in day_messages])
            all_messages.extend(day_messages)
        day += timedelta(days=1)

    return chat_title, all_messages


def sender_matches(message: ChatMessage, user_filter: str) -> bool:
    """Case-insensitive match of a `--user`/target filter against a message's
    sender, by exact @username or substring of their display name."""
    needle = user_filter.strip().lstrip("@").lower()
    if not needle:
        return False
    if message.sender_username and needle == message.sender_username.lower():
        return True
    if message.sender_username and needle in message.sender_username.lower():
        return True
    if needle in message.sender_name.lower():
        return True
    return False


def format_transcript_lines(messages: list[ChatMessage], include_date: bool = False) -> list[str]:
    lines = []
    for m in messages:
        reply_tag = " (reply)" if m.is_reply else ""
        ts = m.dt_local.strftime("%Y-%m-%d %H:%M") if include_date else m.dt_local.strftime("%H:%M")
        lines.append(f"[{ts}] {m.sender_name}{reply_tag}: {m.text}")
    return lines
