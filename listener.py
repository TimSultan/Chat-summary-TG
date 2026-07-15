"""Live listener: works like a slash-command -- any message in a chat you're in that
contains a trigger keyword (default "/summary") is treated as a summary request, from
anyone, no @mention or reply-to-you needed. It parses what's being asked for -- the whole
chat's topics, or one participant's -- for one specific day, and replies in that chat (as
you, via your own Telegram session) with the summary.

Examples it understands (mixed languages are fine):
    "/summary что обсуждали сегодня"          -> whole-chat summary, today
    "/summary сообщения @some_user за сегодня" -> @some_user's topics, today

Run with: python listener.py
Stop with Ctrl+C.

`run_listener()` below is also reused by gui.py, which supplies its own already-connected
client and a log callback that writes into the GUI's log pane instead of stdout.
"""

import asyncio
import random
import re
import sys
import time
import traceback
from collections import deque
from datetime import datetime, timedelta

from telethon import TelegramClient, events, utils as tl_utils
from telethon.tl.functions.messages import GetMessageReactionsListRequest, SendReactionRequest
from telethon.tl.types import ReactionEmoji, UpdateMessageReactions

import chat_profile
import history
import stats
from config import build_session, load_config
from errors import ChatSummaryError
from followup import generate_followup_reply
from intent import parse_summary_request, resolve_name_hint
from intent_v2 import route_request
from joke import generate_joke
from main import period_label, resolve_tz
from responder_v2 import answer_request
from roast import roast_person
from summarizer import summarize_transcript
from telegram_fetch import (
    fetch_range_messages_cached,
    format_transcript_lines,
    resolve_chat,
    sender_display_name,
    sender_matches,
)

MENTION_RE = re.compile(r"@(\w{4,32})")
MAX_REPLY_CHARS = 4000  # stay under Telegram's ~4096 message limit

# Appended to every successful summary reply so people re-discover the available commands
# without having to ask -- not shown on short rejection/error notices, which already
# explain themselves and self-delete fast.
COMMANDS_FOOTER = "Список команд - summary + время или юзер"

# Only ever answer about one specific day at a time -- multi-day ranges (a whole week,
# etc.) are refused outright rather than processed, to keep replies cheap and the chat
# from getting a wall of text. Applies regardless of whether it's a whole-chat or
# per-user request.
DAY_LIMIT_MESSAGE = "Сводка выдается Только за 1 конкретный день и юзера"

# Caps a "last N hours" request (see lookback_hours in intent.py) to roughly the same
# amount of history the single-calendar-day limit above already allows, even though the
# window is anchored to the request time rather than midnight.
MAX_LOOKBACK_HOURS = 24


def _format_hours(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else f"{n:g}"


class DayLimitExceeded(Exception):
    """Raised by resolve_time_window when a multi-day range was requested without an
    explicit lookback-hours window -- see DAY_LIMIT_MESSAGE."""


def resolve_time_window(start_date, end_date, lookback_hours, request_dt, tz, log=print):
    """Turns a parsed date range plus optional lookback_hours into what should actually
    be fetched, enforcing the single-day / MAX_LOOKBACK_HOURS safety caps shared by both
    the v1 and v2 request pipelines. Returns (start_date, end_date, window_start_dt,
    window_end_dt, lookback_hours) -- the last two are None unless lookback_hours was
    given, and lookback_hours is returned back out already clamped (if it was).

    "last N hours" is a rolling window anchored to the exact moment the request was sent,
    not a calendar day -- e.g. asked at 1am for "the last 10 hours" needs messages back to
    3pm *yesterday*, which a same-day-only range would miss entirely. Computed here (not
    by the LLM) so it's exact, and allowed to span two calendar days without tripping the
    single-day limit below, since it's still bounded to at most MAX_LOOKBACK_HOURS
    regardless of where midnight falls."""
    window_start_dt = window_end_dt = None
    if lookback_hours:
        if lookback_hours > MAX_LOOKBACK_HOURS:
            log(f"[listener] clamping requested lookback of {lookback_hours}h to {MAX_LOOKBACK_HOURS}h")
            lookback_hours = MAX_LOOKBACK_HOURS
        window_end_dt = request_dt.astimezone(tz)
        window_start_dt = window_end_dt - timedelta(hours=lookback_hours)
        start_date, end_date = window_start_dt.date(), window_end_dt.date()
        log(f"[listener] lookback window: {window_start_dt} to {window_end_dt}")
    elif start_date != end_date:
        log(f"[listener] rejected multi-day request ({start_date}..{end_date})")
        raise DayLimitExceeded()
    return start_date, end_date, window_start_dt, window_end_dt, lookback_hours


# "прожарь меня" roasts the requester using their own messages from the last
# ROAST_LOOKBACK_DAYS days (config.py, ROAST_LOOKBACK_DAYS env var, default 30). It's a
# two-step flow: the trigger message gets a confirmation prompt, and only an actual
# *reaction* from that same person on that prompt (any emoji) starts generation -- see
# roast_pending/roast_in_progress in run_listener.
ROAST_CONFIRM_TEXT = "Ты точно хочешь прожарку? поставь реакцию для подтверждения"
ROAST_BUSY_EMOJI = "⏳"
NO_ROAST_MATERIAL_MESSAGE = "За последний месяц твоих сообщений тут не нашлось -- нечем прожаривать."

# Reacted onto the triggering message itself as soon as a summary request is accepted,
# so the requester gets instant feedback that it was picked up while the LLM calls
# (which can take a few seconds) run.
SUMMARY_ACK_EMOJI = "✍"

ERROR_DELETE_AFTER = 10  # short rejection notices (such as day limit) self-delete fast
ROAST_DELETE_AFTER = 600  # roast replies self-delete after 10 minutes
STATS_DELETE_AFTER = 300  # /top and /stat replies (incl. their own errors) self-delete after 5 minutes

# "сохрани" (config.py, SAVE_TRIGGER_KEYWORD env var), sent by you as a reply to any
# message, asks (via a confirmation prompt + reaction, like the roast flow) whether to
# repost that message -- photo, video, any media, or just text -- to your save channel
# (SAVE_CHANNEL), with any text after the trigger word appended as a caption. Your own
# trigger message is deleted immediately (it's always yours -- see msg.out check in
# on_message); the confirmation prompt gets a tick reaction and self-deletes once
# confirmed, or self-deletes unconfirmed after SAVE_CONFIRM_TIMEOUT. Unlike
# summary/roast, this ignores LISTENER_ALLOWED_CHATS entirely, since it never touches
# the OpenAI budget.
SAVE_CONFIRM_TEXT = "Сохранить в t.me/papka_pokrasa?\nреакция для подтверждения."
SAVE_TICK_EMOJI = "✅"
SAVE_CONFIRM_TIMEOUT = 10  # seconds to wait for a confirming reaction before cancelling
SAVE_CONFIRM_DELETE_AFTER = 3  # seconds after a tick reaction before the prompt is deleted


def extract_mentioned_usernames(text: str, exclude: str | None) -> list[str]:
    names = {m.group(1) for m in MENTION_RE.finditer(text or "")}
    if exclude:
        names = {n for n in names if n.lower() != exclude.lower()}
    return sorted(names)


def strip_trigger_keywords(text: str, keywords: list[str]) -> str:
    """Removes the trigger keyword(s) (e.g. "/summary") from the request text, so the
    LLM sees the actual question ("кто такой Степан") rather than the invocation itself."""
    result = text
    for kw in keywords:
        result = re.sub(re.escape(kw), "", result, flags=re.IGNORECASE)
    return result.strip()


async def _fetch_album(client, chat_id: int, message) -> list:
    """Returns every message that's part of the same Telegram album (multiple
    photos/videos sent together as one grouped post) as `message`, in original order --
    just `[message]` if it isn't grouped at all. Albums are capped at 10 items by
    Telegram and always contiguous in message id, so a window of the 9 ids on either
    side is enough to find every sibling regardless of which one in the group was
    replied to."""
    if not message.grouped_id:
        return [message]
    ids = [i for i in range(message.id - 9, message.id + 10) if i > 0]
    fetched = await client.get_messages(chat_id, ids=ids)
    album = [m for m in fetched if m is not None and m.grouped_id == message.grouped_id]
    album.sort(key=lambda m: m.id)
    return album


async def repost_saved_message(client, channel, replied_msg, added_text: str) -> None:
    """Reposts `replied_msg` to `channel` as a fresh message, not a forward (no
    "Forwarded from" tag). If it's part of a Telegram album (see _fetch_album), the
    WHOLE album is reposted together, not just the one message that was replied to --
    fixes a bug where only the single replied-to photo/video went out instead of the
    full multi-photo/video post. `added_text` (the text typed after the save trigger
    word, may be empty) is appended below whatever caption the original post already
    had."""
    album = await _fetch_album(client, replied_msg.chat_id, replied_msg)
    original_text = next((m.raw_text for m in album if m.raw_text), "")
    caption = "\n\n".join(p for p in (original_text, added_text) if p) or None

    media_items = [m.media for m in album if m.media]
    if media_items:
        # Passing the original media objects straight through re-uses Telegram's existing
        # files server-side (no download/re-upload through us), same as a forward would,
        # but as brand-new message(s) so it doesn't carry a "Forwarded from" tag. More
        # than one item is sent as a single new album, same shape as the original post.
        await client.send_file(
            channel, file=media_items if len(media_items) > 1 else media_items[0], caption=caption
        )
    elif caption:
        await client.send_message(channel, caption)
    else:
        raise ChatSummaryError("The message you replied to has no text or media to save.")


async def send_long_message(
    client, chat, text: str, reply_to: int | None = None, sent_ids: set[int] | None = None
) -> list[int]:
    """Sends `text` to `chat` as one or more messages (Telegram's ~4096 char limit),
    replying to `reply_to` for the first chunk only -- later chunks are plain follow-ups,
    same as event.reply() + event.respond() do."""
    sent_message_ids = []
    for i in range(0, len(text), MAX_REPLY_CHARS):
        chunk = text[i : i + MAX_REPLY_CHARS]
        sent = await client.send_message(
            chat, chunk, reply_to=reply_to if i == 0 else None, parse_mode="md", link_preview=False
        )
        if sent is not None:
            sent_message_ids.append(sent.id)
            # Track our own generated messages so the listener never re-triggers on them
            # -- matters once outgoing messages are watched too (see run_listener), since
            # a summary reply can easily contain the trigger keyword itself.
            if sent_ids is not None:
                sent_ids.add(sent.id)
    return sent_message_ids


async def send_long_reply(event, text: str, sent_ids: set[int] | None = None) -> list[int]:
    chat = await event.get_chat()
    return await send_long_message(event.client, chat, text, reply_to=event.message.id, sent_ids=sent_ids)


async def handle_request(event, cfg, tz, my_username: str, sent_ids: set[int], schedule_delete, log=print):
    msg = event.message
    text = msg.raw_text or ""

    chat = await event.get_chat()
    chat_title_for_history = getattr(chat, "title", None) or "Unknown chat"
    sender = await event.get_sender()
    requester = sender_display_name(sender)

    async def respond(answer: str, delete_after: int | None = None, record: bool = True):
        message_ids = await send_long_reply(event, answer, sent_ids=sent_ids)
        if record:
            try:
                history.record(chat_title_for_history, requester, text, answer)
            except Exception as e:
                log(f"[listener] failed to record history: {e}")
        if delete_after and message_ids:
            schedule_delete(event.client, chat.id, message_ids, delete_after)

    mentioned = extract_mentioned_usernames(text, exclude=my_username)
    ref_date = msg.date.astimezone(tz).date()

    try:
        # to_thread: these OpenAI helpers use the synchronous client, which would
        # otherwise block this whole process's event loop (shared with bot_listener.py's
        # poll loop when a bot token is configured) for the entire network round trip.
        intent = await asyncio.to_thread(
            parse_summary_request,
            api_key=cfg.openai_api_key,
            model=cfg.openai_model,
            text=text,
            reference_date=ref_date,
            mentioned_usernames=mentioned,
            my_username=my_username,
        )
    except Exception as e:
        log(f"[listener] intent parse failed: {e}")
        await respond("Couldn't parse that request.")
        return

    focus_user = intent["target_username"] if intent["scope"] == "user" else None
    start_date, end_date = intent["start_date"], intent["end_date"]

    try:
        start_date, end_date, window_start_dt, window_end_dt, lookback_hours = resolve_time_window(
            start_date, end_date, intent.get("lookback_hours"), msg.date, tz, log
        )
    except DayLimitExceeded:
        await respond(DAY_LIMIT_MESSAGE, delete_after=ERROR_DELETE_AFTER, record=False)
        return

    # The raw transcript is cached per day (see transcript_cache.py) so repeated or
    # differently-scoped questions about the same day don't each re-fetch from
    # Telegram. Every question still gets its own fresh OpenAI call below, against
    # whatever transcript (cached or just-fetched) came back.
    chat_title, messages = await fetch_range_messages_cached(
        client=event.client,
        chat_ref=chat,
        start_day=start_date,
        end_day=end_date,
        tz=tz,
        log=log,
    )

    if window_start_dt is not None:
        messages = [m for m in messages if window_start_dt <= m.dt_local <= window_end_dt]

    from_explicit_mention = bool(focus_user)
    name_hint = intent.get("target_name_hint")
    if intent["scope"] == "user" and not focus_user and name_hint:
        # Include BOTH each sender's @username and display name as separate candidates --
        # someone's actual nickname (what people call them, e.g. a chosen display name in
        # a different script) is often not their @username, and picking only one per
        # person can silently drop the exact string the request actually used.
        candidates = sorted({c for m in messages for c in (m.sender_username, m.sender_name) if c})
        shown = candidates if len(candidates) <= 30 else candidates[:30] + [f"... +{len(candidates) - 30} more"]
        log(f"[listener] resolving name hint '{name_hint}' against {len(candidates)} candidates: {shown}")
        try:
            focus_user = await asyncio.to_thread(
                resolve_name_hint, cfg.openai_api_key, cfg.openai_model, name_hint, candidates
            )
        except ChatSummaryError as e:
            log(f"[listener] name resolution failed: {e}")
            focus_user = None
        if focus_user:
            log(f"[listener] resolved name hint '{name_hint}' -> '{focus_user}'")
        else:
            log(f"[listener] could not resolve name hint '{name_hint}' among participants")
            await respond(f"Couldn't figure out who \"{name_hint}\" refers to in this chat.")
            return

    if focus_user and from_explicit_mention:
        # An @mention is a literal request about that account's own messages, so it's
        # safe (and cheap) to bail out early if they posted nothing at all. A
        # name-hint match (e.g. "the situation with Anzhelika") can be about a topic
        # others discussed without her posting, so that path always goes to the LLM.
        matched = sum(1 for m in messages if sender_matches(m, focus_user))
        log(f"[listener] scope=user target={focus_user} matched={matched}/{len(messages)}")
        if matched == 0:
            await respond(f"No messages from @{focus_user} found in that period.")
            return

    lines = format_transcript_lines(messages, include_date=(start_date != end_date))
    if window_start_dt is not None:
        label = (
            f"last {_format_hours(lookback_hours)} hours "
            f"({window_start_dt.strftime('%Y-%m-%d %H:%M')} to {window_end_dt.strftime('%Y-%m-%d %H:%M')})"
        )
    else:
        label = period_label(start_date, end_date)
    original_question = strip_trigger_keywords(text, cfg.listener_trigger_keywords)

    summary = await asyncio.to_thread(
        summarize_transcript,
        api_key=cfg.openai_api_key,
        model=cfg.openai_model,
        chat_title=chat_title,
        period_label=label,
        lines=lines,
        focus_user=focus_user,
        style="reply",
        reply_language=intent["reply_language"],
        topic_hint=intent.get("topic_hint"),
        length_hint=intent.get("length_hint"),
        original_question=original_question,
    )

    await respond(f"{summary}\n\n{COMMANDS_FOOTER}")


async def handle_request_v2(event, cfg, tz, my_username: str, sent_ids: set[int], schedule_delete, log=print):
    """v2 pipeline: intent_v2.route_request extracts only a date range/lookback window
    and an optional focus username, plus a cleaned-up restatement of the question.
    responder_v2.answer_request then answers that question against the fetched
    transcript in one freeform step -- no separate topic/length-hint extraction, the
    model decides the answer's shape itself. See intent_v2.py / responder_v2.py."""
    msg = event.message
    text = msg.raw_text or ""

    chat = await event.get_chat()
    chat_title_for_history = getattr(chat, "title", None) or "Unknown chat"
    sender = await event.get_sender()
    requester = sender_display_name(sender)

    async def respond(answer: str, delete_after: int | None = None, record: bool = True):
        message_ids = await send_long_reply(event, answer, sent_ids=sent_ids)
        if record:
            try:
                history.record(chat_title_for_history, requester, text, answer)
            except Exception as e:
                log(f"[listener] failed to record history: {e}")
        if delete_after and message_ids:
            schedule_delete(event.client, chat.id, message_ids, delete_after)

    mentioned = extract_mentioned_usernames(text, exclude=my_username)
    ref_date = msg.date.astimezone(tz).date()

    try:
        routed = await asyncio.to_thread(
            route_request,
            api_key=cfg.openai_api_key,
            model=cfg.openai_model,
            text=text,
            reference_date=ref_date,
            mentioned_usernames=mentioned,
            my_username=my_username,
        )
    except Exception as e:
        log(f"[listener] intent_v2 routing failed: {e}")
        await respond("Не удалось разобрать запрос.")
        return

    try:
        start_date, end_date, window_start_dt, window_end_dt, lookback_hours = resolve_time_window(
            routed["start_date"], routed["end_date"], routed["lookback_hours"], msg.date, tz, log
        )
    except DayLimitExceeded:
        await respond(DAY_LIMIT_MESSAGE, delete_after=ERROR_DELETE_AFTER, record=False)
        return

    chat_title, messages = await fetch_range_messages_cached(
        client=event.client,
        chat_ref=chat,
        start_day=start_date,
        end_day=end_date,
        tz=tz,
        log=log,
    )

    if window_start_dt is not None:
        messages = [m for m in messages if window_start_dt <= m.dt_local <= window_end_dt]

    focus_user = None
    username_hint = routed["username"]
    if username_hint:
        # An exact match against an @mention actually present in the message is a
        # literal request about that account's own messages -- safe (and cheap) to bail
        # out early if they posted nothing at all. Anything else is a plain name/nickname
        # that needs resolving against actual participants (same as v1's name-hint path),
        # since it can be about a topic others discussed without that person posting
        # (e.g. "the situation with Anzhelika").
        from_explicit_mention = any(username_hint.lower() == m.lower() for m in mentioned)
        if from_explicit_mention:
            focus_user = username_hint
            matched = sum(1 for m in messages if sender_matches(m, focus_user))
            log(f"[listener] v2 focus_user(explicit)={focus_user} matched={matched}/{len(messages)}")
            if matched == 0:
                await respond(f"Сообщений от @{focus_user} за этот период не найдено.")
                return
        else:
            candidates = sorted({c for m in messages for c in (m.sender_username, m.sender_name) if c})
            shown = candidates if len(candidates) <= 30 else candidates[:30] + [f"... +{len(candidates) - 30} more"]
            log(f"[listener] v2 resolving name hint '{username_hint}' against {len(candidates)} candidates: {shown}")
            try:
                focus_user = await asyncio.to_thread(
                    resolve_name_hint, cfg.openai_api_key, cfg.openai_model, username_hint, candidates
                )
            except ChatSummaryError as e:
                log(f"[listener] v2 name resolution failed: {e}")
                focus_user = None
            if focus_user:
                log(f"[listener] v2 resolved name hint '{username_hint}' -> '{focus_user}'")
            else:
                log(f"[listener] v2 could not resolve name hint '{username_hint}' among participants")
                await respond(f"Не понял, о ком речь: \"{username_hint}\".")
                return

    lines = format_transcript_lines(messages, include_date=(start_date != end_date))
    if window_start_dt is not None:
        label = (
            f"last {_format_hours(lookback_hours)} hours "
            f"({window_start_dt.strftime('%Y-%m-%d %H:%M')} to {window_end_dt.strftime('%Y-%m-%d %H:%M')})"
        )
    else:
        label = period_label(start_date, end_date)

    answer = await asyncio.to_thread(
        answer_request,
        api_key=cfg.openai_api_key,
        model=cfg.openai_model,
        chat_title=chat_title,
        period_label=label,
        lines=lines,
        question=routed["cleaned_question"],
        focus_user=focus_user,
        style="reply",
    )

    await respond(f"{answer}\n\n{COMMANDS_FOOTER}")


async def run_roast(
    client,
    chat,
    target_user,
    confirm_msg_id: int,
    original_text: str,
    cfg,
    tz,
    sent_ids: set[int],
    schedule_delete,
    log=print,
):
    """Actually generates and sends the roast, once the target user has confirmed by
    reacting to the ROAST_CONFIRM_TEXT prompt. Uses their own messages from the last
    cfg.roast_lookback_days days (reusing the same per-day transcript cache as the
    summary path)."""
    chat_title_for_history = getattr(chat, "title", None) or "Unknown chat"
    requester = sender_display_name(target_user)

    async def respond(answer: str, delete_after: int | None = None, record: bool = True):
        message_ids = await send_long_message(client, chat, answer, reply_to=confirm_msg_id, sent_ids=sent_ids)
        if record:
            try:
                history.record(chat_title_for_history, requester, original_text, answer)
            except Exception as e:
                log(f"[listener] failed to record history: {e}")
        if delete_after and message_ids:
            schedule_delete(client, chat.id, message_ids, delete_after)

    end_date = datetime.now(tz).date()
    start_date = end_date - timedelta(days=cfg.roast_lookback_days - 1)

    _, messages = await fetch_range_messages_cached(
        client=client,
        chat_ref=chat,
        start_day=start_date,
        end_day=end_date,
        tz=tz,
        log=log,
    )

    target_id = getattr(target_user, "id", None)
    own_messages = [m for m in messages if m.sender_id == target_id] if target_id is not None else []
    if not own_messages:
        username = getattr(target_user, "username", None)
        if username:
            own_messages = [m for m in messages if sender_matches(m, username)]

    log(f"[listener] roast target={requester} matched={len(own_messages)}/{len(messages)}")
    if not own_messages:
        await respond(NO_ROAST_MATERIAL_MESSAGE, delete_after=ERROR_DELETE_AFTER, record=False)
        return

    if len(own_messages) > cfg.roast_max_messages:
        # A very active poster can have thousands of messages in the lookback window --
        # roast_person map-reduces the transcript into 6000-token chunks with one
        # *sequential* OpenAI call per chunk, so an uncapped input can mean dozens of
        # blocking calls (minutes of silence before anything is sent). Capping to the
        # most recent N keeps generation fast; a roast doesn't need the whole month, just
        # enough material.
        log(
            f"[listener] capping roast input for {requester}: {len(own_messages)} -> "
            f"{cfg.roast_max_messages} most recent messages"
        )
        own_messages = own_messages[-cfg.roast_max_messages :]

    lines = format_transcript_lines(own_messages, include_date=True)
    roast = await asyncio.to_thread(
        roast_person,
        api_key=cfg.openai_api_key,
        model=cfg.openai_model,
        target_name=requester,
        lines=lines,
    )

    await respond(f"{roast}\n\n{COMMANDS_FOOTER}", delete_after=ROAST_DELETE_AFTER)


async def _stats_catch_up(client, cfg, tz, log=print) -> None:
    """For each chat in cfg.listener_allowed_chats, closes out every day in the last
    cfg.stats_catchup_days that isn't recorded yet (see stats.finalize_and_record) --
    oldest first. Cheap to call repeatedly: stats.finalize_and_record's own idempotency
    check (stats.is_recorded) means any day already recorded costs nothing but a file
    existence check, no Telegram calls -- so this covers both the very first run (backfill
    up to stats_catchup_days of history) and every subsequent midnight (where only the
    single just-closed day is actually new work) with the same code path."""
    today = datetime.now(tz).date()
    for entry in cfg.listener_allowed_chats:
        try:
            chat_entity = await resolve_chat(client, entry)
        except Exception as e:
            log(f"[stats] could not resolve '{entry}' for catch-up: {e}")
            continue
        for delta in range(cfg.stats_catchup_days, 0, -1):
            day = today - timedelta(days=delta)
            try:
                await stats.finalize_and_record(client, chat_entity, entry, day, tz, log=log)
            except Exception:
                log(f"[stats] failed to catch up '{entry}' for {day}:\n{traceback.format_exc()}")


async def run_stats_rollover(client, cfg, tz, log=print) -> None:
    """Keeps stats.py's per-day-per-chat files up to date for every chat named in
    LISTENER_ALLOWED_CHATS (stats tracking, like jokes, needs specific chats named --
    there's no "everywhere" fallback): runs _stats_catch_up once immediately (covers a
    restart that missed one or more midnights while down), then sleeps until the next
    local midnight and runs it again, forever. A few seconds of buffer after :00 avoids
    any edge-case race right at the rollover instant."""
    if not cfg.listener_allowed_chats:
        log("[stats] STATS_ENABLED is set but LISTENER_ALLOWED_CHATS is empty -- stats tracking needs specific chats named, so it's off.")
        return

    await _stats_catch_up(client, cfg, tz, log=log)
    while True:
        now = datetime.now(tz)
        next_run = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        await asyncio.sleep((next_run - now).total_seconds())
        await _stats_catch_up(client, cfg, tz, log=log)


def build_client(cfg) -> TelegramClient:
    try:
        return TelegramClient(build_session(cfg), cfg.api_id, cfg.api_hash)
    except Exception as e:
        if not cfg.session_string:
            raise ChatSummaryError(
                "Could not create a session file, and TELEGRAM_SESSION_STRING is not set. "
                "On a host with no writable/persistent disk for a session file (Railway, "
                "etc.), you must set TELEGRAM_SESSION_STRING instead -- see "
                "generate_session_string.py or convert_existing_session.py. "
                f"Underlying error: {e}"
            ) from e
        raise


async def run_listener(
    client: TelegramClient,
    cfg,
    tz,
    log=print,
    joke_queue: "asyncio.Queue | None" = None,
    joke_posted_queue: "asyncio.Queue | None" = None,
    bot_response_queue: "asyncio.Queue | None" = None,
    followup_queue: "asyncio.Queue | None" = None,
):
    """Registers the mention-trigger handler on an already-connected & authorized
    `client` and blocks until it disconnects (call `client.disconnect()` to stop it).

    `joke_queue`, if given, is where a generated joke (see joke.py) is put once the
    activity trigger fires -- bot_listener.py's run_bot_listener consumes it and sends the
    joke via the bot account, same as every other reply once a bot token is configured.
    `joke_posted_queue` carries the reply the other way: bot_listener.py puts
    (allowed_chats entry, sent message_id) on it once a queued joke is actually sent, so
    this process can start the post-send cooldown and watch that specific message for
    reactions (see the reaction-count cooldown reduction in on_reaction below) -- reactions
    can only be reliably observed from this Telethon session, not the bot account (which
    would need admin rights to see other users' reactions via the Bot API).

    `bot_response_queue`/`followup_queue` are the same kind of hand-off, for a different
    feature (see followup.py): bot_listener.py puts (chat_id, sent_message_ids, kind,
    response_text) on `bot_response_queue` right after it posts ANY summary answer or
    joke to a group chat, which starts a per-chat watch here (see maybe_followup) over
    the next cfg.followup_window_messages messages for chat commentary about that
    response -- praise or criticism, not necessarily a reply/mention (though a direct
    Telegram reply to one of sent_message_ids is recognized with certainty, not left for
    the model to guess) -- re-checked every cfg.followup_check_every_messages messages
    rather than on every single one, to keep the OpenAI call count down. If the model
    decides someone's actually reacting, the generated clap-back is put on
    `followup_queue` for bot_listener.py to send, same bot-account-only rule as
    everything else. All four queues are passed in (not created here) so main() can
    share them between both tasks."""
    assert cfg.summary_queue_delay_seconds >= 0, "internal bug: queue delay should have been validated by config"

    me = await client.get_me()
    my_username = me.username
    # Lets people trigger a summary without the exact keyword, either by naming you
    # directly (e.g. "sultan summary" in one message) or by replying to one of your
    # messages and saying "summary" -- see the two extra checks in on_message below.
    # Require a few characters so a short/generic first name doesn't match constantly.
    my_first_name = (me.first_name or "").strip().lower()
    if len(my_first_name) < 3:
        my_first_name = ""

    if not my_username:
        # Not fatal -- triggering no longer needs an @mention of this account, just the
        # keyword itself. Only a couple of minor safety checks (excluding your own
        # username from name resolution) are skipped without one.
        log(
            "[listener] WARNING: your Telegram account has no @username set. The "
            "trigger keyword still works fine; only the 'never target myself' name "
            "safety checks are skipped."
        )

    # When a bot account (bot_listener.py) is configured, it takes over /summary entirely
    # -- this Telethon listener would otherwise also see and answer the same trigger
    # message, producing two replies. Save is unaffected: it only ever makes sense as
    # *your own* account reposting to your own channel. Roast is off everywhere (see
    # has_roast_keyword below), so it's not part of this handoff.
    bot_takeover = bool(cfg.telegram_bot_token)
    if bot_takeover:
        log("[listener] TELEGRAM_BOT_TOKEN is set -- /summary is handled by bot_listener.py instead of this account.")

    allowed_chats = set(c.lower().lstrip("@") for c in cfg.listener_allowed_chats)
    if allowed_chats:
        log(f"[listener] restricting to allowed chats: {sorted(allowed_chats)}")
    else:
        log(
            "[listener] WARNING: LISTENER_ALLOWED_CHATS is not set -- this will respond to "
            "summary requests from ANYONE in ANY chat you're in, spending your OpenAI budget "
            "on their behalf. Set LISTENER_ALLOWED_CHATS in .env to restrict this."
        )

    save_channel_entity = None
    if cfg.save_channel:
        try:
            save_channel_entity = await resolve_chat(client, cfg.save_channel)
            log(f"[listener] save channel resolved: {getattr(save_channel_entity, 'title', cfg.save_channel)}")
        except Exception as e:
            log(
                f"[listener] WARNING: could not resolve save channel '{cfg.save_channel}': {e}. "
                f"\"{cfg.save_trigger_keyword}\" trigger will fail until this is fixed."
            )
    else:
        log("[listener] SAVE_CHANNEL is not set -- the save trigger is disabled.")

    summary_queue: asyncio.Queue = asyncio.Queue()
    sent_message_ids: set[int] = set()
    background_tasks: set[asyncio.Task] = set()

    # "joke" (joke.py) is the one feature nobody asks for -- it's fired autonomously off
    # recent chat activity instead of a keyword. Kept off unless explicitly enabled AND a
    # bot account is available to actually post it (jokes always go out via the bot, never
    # this personal account, matching how summary/roast were moved over), AND at least one
    # chat is named in LISTENER_ALLOWED_CHATS -- unlike summary's "empty = respond
    # anywhere" fallback, something nobody asked for should never default to "everywhere".
    joke_enabled = joke_queue is not None and cfg.joke_enabled and bool(cfg.listener_allowed_chats)
    if cfg.joke_enabled and not joke_enabled:
        log(
            "[listener] JOKE_ENABLED is set but jokes need both TELEGRAM_BOT_TOKEN and a "
            "non-empty LISTENER_ALLOWED_CHATS -- jokes are off."
        )
    # Per chat: a fixed-size ring buffer of the last cfg.joke_activity_min_messages
    # qualifying messages -- once it's full, a joke attempt is considered (see
    # maybe_joke). Using maxlen means this can never grow unbounded even if a chat stays
    # busy through an entire cooldown, and "full" is exactly the trigger condition, with
    # no separate counter to keep in sync.
    joke_activity: dict[int, deque] = {}
    # Monotonic deadline before which a chat won't be considered again -- only set after
    # an actual joke is sent (never after a SKIP, see maybe_joke), so a decline just costs
    # another full buffer of messages, not a timer. May be pulled earlier by a reaction
    # burst on that joke (see on_reaction).
    joke_cooldown_until: dict[int, float] = {}
    # Lets the joke_posted_queue consumer (below) and on_reaction map a
    # LISTENER_ALLOWED_CHATS entry / a (chat, message) pair back to state kept here,
    # since the actual send happens over in bot_listener.py.
    joke_entry_to_chat: dict[str, int] = {}
    joke_reaction_watch: dict[tuple[int, int], float] = {}

    # Roast confirm/react flow state, keyed by (chat_id, target_user_id):
    # - roast_pending: confirmation prompt sent, awaiting a reaction from that user.
    #   Value is (confirmation_message_id, original_trigger_text).
    # - roast_in_progress: they reacted, generation is under way (until the reply is sent
    #   or it errors out).
    # Re-triggering "прожарь меня" while either is true doesn't restart anything -- it
    # just reacts to the new message with ROAST_BUSY_EMOJI.
    roast_pending: dict[tuple[int, int], tuple[int, str]] = {}
    roast_in_progress: set[tuple[int, int]] = set()

    # Save confirm/react flow state, keyed by (chat_id, confirm_message_id) -- unlike
    # roast_pending, keyed directly by the confirmation message itself since a save
    # only ever needs one pending confirmation per prompt (no "already pending for this
    # user" concept to track). Value carries what to repost once/if confirmed.
    save_pending: dict[tuple[int, int], dict] = {}

    # "follow-up" (followup.py) reacts to chat commentary about the bot's OWN last
    # response (a summary answer or a joke) -- see maybe_followup below. Needs both
    # queues (they're only created in main() alongside a bot token) since, like every
    # other reply, the actual send has to go out via the bot account, never this personal
    # one -- this process only watches and decides.
    followup_enabled = (
        bot_response_queue is not None and followup_queue is not None and cfg.followup_enabled
    )
    if cfg.followup_enabled and not followup_enabled:
        log("[listener] FOLLOWUP_ENABLED is set but reacting to chat feedback needs TELEGRAM_BOT_TOKEN -- it's off.")
    # Per chat: at most one active watch, keyed by chat_id (the SAME numbering Telethon's
    # event.chat_id and the Bot API's chat.id already share -- see bot_listener.py's
    # _resolve_chat_id docstring). Set (and any previous entry for that chat replaced
    # wholesale) by the bot_response_queue consumer below whenever bot_listener.py posts a
    # summary answer or joke; cleared by maybe_followup once it either fires or the
    # window (cfg.followup_window_messages messages) runs out with nothing to react to.
    # "in_flight" guards against two messages arriving back to back both launching their
    # own classification call for the same watch (see maybe_followup).
    followup_watch: dict[int, dict] = {}

    def is_chat_allowed(chat) -> bool:
        if not allowed_chats:
            return True
        username = (getattr(chat, "username", "") or "").lower()
        title = (getattr(chat, "title", "") or "").lower()
        chat_id = str(getattr(chat, "id", ""))
        return username in allowed_chats or title in allowed_chats or chat_id in allowed_chats

    def matched_allowed_chat(chat) -> str | None:
        """Like is_chat_allowed, but returns the actual LISTENER_ALLOWED_CHATS entry
        (original casing) that matched, instead of a bool -- this is the key jokes are
        queued under, so bot_listener.py's consumer can look up the matching Bot-API
        chat_id. Jokes only ever consider chats explicitly named here, never
        is_chat_allowed's "empty list = allow everywhere" fallback."""
        username = (getattr(chat, "username", "") or "").lower()
        title = (getattr(chat, "title", "") or "").lower()
        chat_id = str(getattr(chat, "id", ""))
        for entry in cfg.listener_allowed_chats:
            e = entry.lower().lstrip("@")
            if e in (username, title, chat_id):
                return entry
        return None

    def schedule_delete(delete_client, chat_id, message_ids, delay_seconds):
        """Fire-and-forget: deletes `message_ids` after `delay_seconds`, without
        blocking whatever's currently handling the request."""

        async def _do():
            await asyncio.sleep(delay_seconds)
            try:
                await delete_client.delete_messages(chat_id, message_ids)
            except Exception as e:
                log(f"[listener] failed to auto-delete message(s): {e}")

        task = asyncio.create_task(_do())
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    async def react_emoji(chat_id, msg_id, emoji):
        try:
            await client(
                SendReactionRequest(
                    peer=chat_id,
                    msg_id=msg_id,
                    reaction=[ReactionEmoji(emoticon=emoji)],
                    add_to_recent=True,
                )
            )
        except Exception as e:
            log(f"[listener] failed to react with emoji: {e}")

    async def _consume_summaries():
        """Processes accepted summary enquiries in FIFO order without dropping bursts."""
        last_finished_at: float | None = None
        while True:
            event, chat, text = await summary_queue.get()
            try:
                if last_finished_at is not None:
                    elapsed = time.monotonic() - last_finished_at
                    wait_for = max(0.0, cfg.summary_queue_delay_seconds - elapsed)
                    if wait_for:
                        log(
                            f"[listener] waiting {wait_for:.1f}s before next queued request "
                            f"({summary_queue.qsize()} still waiting)"
                        )
                        await asyncio.sleep(wait_for)
                chat_key = event.chat_id
                log(f"[listener] handling queued request in '{getattr(chat, 'title', chat_key)}': {text!r}")
                try:
                    handler = handle_request_v2 if cfg.summary_pipeline_version == "v2" else handle_request
                    await handler(event, cfg, tz, my_username, sent_message_ids, schedule_delete, log=log)
                except Exception:
                    log(f"[listener] error handling queued request:\n{traceback.format_exc()}")
                    try:
                        sent = await event.reply("Что-то пошло не так при генерации сводки.")
                        if sent is not None:
                            sent_message_ids.add(sent.id)
                    except Exception:
                        pass
            finally:
                last_finished_at = time.monotonic()
                summary_queue.task_done()

    summary_worker = asyncio.create_task(_consume_summaries())
    background_tasks.add(summary_worker)
    summary_worker.add_done_callback(background_tasks.discard)

    async def maybe_joke(event, msg, text):
        """Tracks this message in its chat's joke_activity ring buffer and, once that
        buffer is full, considers firing. Called for every plain-text message in an
        allowed chat, not just ones containing a keyword -- this is the only way jokes get
        their "is the chat active right now" signal, and it's pure message count, not a
        time window: a silent/sleeping chat simply never fills the buffer, so this can
        never fire there, no matter how long it waits.

        Once full (cfg.joke_activity_min_messages messages), a fire is considered if the
        per-chat cooldown (joke_cooldown_until, unset until the first joke is ever sent)
        has passed and a random roll under JOKE_FIRE_PROBABILITY hits. The buffer is only
        cleared on an actual attempt (roll passed, OpenAI gets called) -- a roll *miss*
        leaves it full so the very next qualifying message re-rolls immediately, instead of
        waiting for another whole buffer's worth. Everything from the buffer-full check
        through clearing it happens in one synchronous stretch with no `await` in between
        (the two awaits below, for chat/sender, already happened by this point), so two
        messages arriving close together can't both pass the check and double-fire.

        If the model then declines (SKIP, see joke.py), that's it for this attempt -- no
        cooldown is set, so it costs exactly "wait for another full buffer", not a timer.
        Only an actual sent joke sets joke_cooldown_until (via the joke_posted_queue
        consumer below), which a reaction burst on that joke can later pull earlier."""
        chat = await event.get_chat()
        entry = matched_allowed_chat(chat)
        if entry is None:
            return
        sender = await event.get_sender()
        if getattr(sender, "bot", False):
            return  # never let either account's own messages (incl. past jokes) count as activity

        now = time.monotonic()
        chat_key = event.chat_id
        joke_entry_to_chat[entry] = chat_key
        bucket = joke_activity.setdefault(chat_key, deque(maxlen=cfg.joke_activity_min_messages))
        bucket.append((msg.date.astimezone(tz).strftime("%H:%M"), sender_display_name(sender), text))

        if len(bucket) < cfg.joke_activity_min_messages:
            return
        if now < joke_cooldown_until.get(chat_key, float("-inf")):
            return
        if random.random() >= cfg.joke_fire_probability:
            return

        lines = [f"[{hhmm}] {name}: {t}" for hhmm, name, t in bucket]
        bucket.clear()
        log(f"[listener] joke conditions met in '{getattr(chat, 'title', chat_key)}' ({len(lines)} msgs) -- generating")

        async def _run():
            try:
                profile = await chat_profile.ensure_profile(
                    client, chat, entry, cfg.openai_api_key, cfg.openai_model, tz,
                    cfg.joke_profile_ttl_seconds, cfg.joke_profile_lookback_days, cfg.joke_profile_max_messages,
                    log=log,
                )
                joke_text = await asyncio.to_thread(
                    generate_joke, cfg.openai_api_key, cfg.openai_model, lines, profile
                )
                if joke_text:
                    await joke_queue.put((entry, joke_text))
                    log(f"[listener] queued joke for '{entry}': {joke_text!r}")
                else:
                    log(f"[listener] joke generation skipped itself for '{entry}' (not a good moment) -- next attempt after another {cfg.joke_activity_min_messages} messages")
            except Exception:
                log(f"[listener] error generating joke:\n{traceback.format_exc()}")

        task = asyncio.create_task(_run())
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    async def maybe_followup(event, msg, text):
        """Feeds this message into its chat's active follow-up watch (if any -- see
        followup_watch, populated by the bot_response_queue consumer below whenever
        bot_listener.py posts a summary answer or a joke) and, every
        cfg.followup_check_every_messages messages (or right at the window boundary,
        whichever comes first), runs one classification pass over the buffer collected
        so far.

        Mirrors maybe_joke's care around not double-firing: the only await before the
        synchronous append + in-flight check is get_sender (already true by the time
        that block runs), so two messages arriving back to back can't both slip past the
        in-flight guard and launch overlapping checks for the same watch. Unlike
        maybe_joke's buffer, this doesn't wait for the full window to fill before the
        FIRST check -- a reaction is only worth reacting to while it's still fresh -- but
        it also doesn't check on literally every message, to keep the OpenAI call count
        down: cfg.followup_check_every_messages (default 5) messages have to land between
        checks, except the window boundary itself (cfg.followup_window_messages, default
        15) is always evaluated regardless of where that falls, so "gave up, nothing
        found" is still decided off the full window, not a partial one. The watch closes
        the moment a check actually fires or the window runs out with nothing found.

        A message that's a direct Telegram reply to one of the bot's own sent_message_ids
        (see watch["message_ids"]) is tagged in the buffer -- it's certainly about this
        response, no need to make the model guess -- but that's purely a content signal:
        it still just gets buffered like anything else and waits for the next checkpoint,
        same as above, rather than forcing an immediate check."""
        chat_key = event.chat_id
        watch = followup_watch.get(chat_key)
        if watch is None:
            return
        sender = await event.get_sender()
        if getattr(sender, "bot", False):
            return  # never count either account's own messages (incl. a just-sent reply) as chat commentary

        if len(watch["buffer"]) < cfg.followup_window_messages:
            is_reply_to_bot = msg.reply_to_msg_id in watch["message_ids"]
            watch["buffer"].append(
                (msg.date.astimezone(tz).strftime("%H:%M"), sender_display_name(sender), text, is_reply_to_bot)
            )
        buffer_len = len(watch["buffer"])
        window_full = buffer_len >= cfg.followup_window_messages
        at_checkpoint = buffer_len % cfg.followup_check_every_messages == 0
        if watch["in_flight"] or not (at_checkpoint or window_full):
            return  # not a check checkpoint yet, or a check for this watch is already running
        watch["in_flight"] = True
        lines = [
            f"[{hhmm}] {name}{' (ответ на сообщение бота)' if was_reply_to_bot else ''}: {t}"
            for hhmm, name, t, was_reply_to_bot in watch["buffer"]
        ]

        async def _run():
            try:
                chat = await event.get_chat()
                entry = matched_allowed_chat(chat)
                profile = chat_profile.load_cached_profile(entry) if entry else None
                reply = await asyncio.to_thread(
                    generate_followup_reply,
                    cfg.openai_api_key, cfg.openai_model, watch["kind"], watch["response_text"], lines, profile,
                )
                if followup_watch.get(chat_key) is not watch:
                    return  # superseded by a newer bot response while this check was running
                if reply:
                    await followup_queue.put((chat_key, reply))
                    log(f"[listener] queued follow-up reply for chat {chat_key}: {reply!r}")
                    followup_watch.pop(chat_key, None)
                elif len(watch["buffer"]) >= cfg.followup_window_messages:
                    log(f"[listener] no reaction to its last {watch['kind']} found in chat {chat_key} within the watch window -- giving up")
                    followup_watch.pop(chat_key, None)
                else:
                    watch["in_flight"] = False
            except Exception:
                log(f"[listener] error generating follow-up reply:\n{traceback.format_exc()}")
                if followup_watch.get(chat_key) is watch:
                    if len(watch["buffer"]) >= cfg.followup_window_messages:
                        followup_watch.pop(chat_key, None)
                    else:
                        watch["in_flight"] = False

        task = asyncio.create_task(_run())
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    # No incoming=True filter: watching outgoing messages too is what lets *you*
    # trigger a summary by typing "summary ..." yourself, not just other people
    # @mentioning you. The sent_message_ids/addressed_to_me logic below keeps this
    # from re-triggering on the listener's own generated replies.
    @client.on(events.NewMessage())
    async def on_message(event):
        msg = event.message
        if msg.id in sent_message_ids:
            return  # our own generated reply -- never treat it as a new request

        text = msg.raw_text or ""
        text_lower = text.lower()

        if joke_enabled and text:
            await maybe_joke(event, msg, text)
        if followup_enabled and text:
            await maybe_followup(event, msg, text)

        # "сохрани" (config.py SAVE_TRIGGER_KEYWORD), sent by you as a reply, asks for
        # confirmation before reposting whatever you replied to into your save channel
        # -- see save_pending handling in on_reaction below. Only ever fires for your
        # own messages (msg.out), and doesn't touch LISTENER_ALLOWED_CHATS/the summary queue
        # since it never calls OpenAI.
        if msg.out and msg.is_reply and text_lower.startswith(cfg.save_trigger_keyword):
            added_text = text[len(cfg.save_trigger_keyword) :].strip(" :,-–—\t\n")
            try:
                if save_channel_entity is None:
                    raise ChatSummaryError(f"Save channel '{cfg.save_channel}' isn't set up -- check SAVE_CHANNEL.")
                replied = await msg.get_reply_message()
                if replied is None:
                    raise ChatSummaryError("Couldn't find the message you replied to.")

                confirm = await client.send_message(event.chat_id, SAVE_CONFIRM_TEXT, reply_to=replied.id)
                if confirm is not None:
                    sent_message_ids.add(confirm.id)
                    key = (event.chat_id, confirm.id)
                    save_pending[key] = {"replied": replied, "added_text": added_text}
                    log(f"[listener] sent save confirmation for message {replied.id} (confirm msg {confirm.id})")

                    async def _expire_save_confirm(key=key, confirm_id=confirm.id):
                        await asyncio.sleep(SAVE_CONFIRM_TIMEOUT)
                        if save_pending.pop(key, None) is not None:
                            try:
                                await client.delete_messages(event.chat_id, [confirm_id])
                            except Exception as e:
                                log(f"[listener] failed to delete unconfirmed save prompt: {e}")

                    task = asyncio.create_task(_expire_save_confirm())
                    background_tasks.add(task)
                    task.add_done_callback(background_tasks.discard)
            except Exception as e:
                log(f"[listener] failed to start save flow: {e}")
                try:
                    sent = await event.reply(f"Не удалось сохранить: {e}")
                    if sent is not None:
                        sent_message_ids.add(sent.id)
                        schedule_delete(event.client, event.chat_id, [sent.id], ERROR_DELETE_AFTER)
                except Exception:
                    pass
            finally:
                # Always yours (msg.out) -- clean it up now, its job is done once the
                # confirmation prompt (or error notice) is out.
                try:
                    await event.client.delete_messages(event.chat_id, [msg.id])
                except Exception as e:
                    log(f"[listener] failed to delete save trigger message: {e}")
            return

        # "/top today|week|month|all" and "/stat [username]" (stats.py) -- plain lookups over
        # already-computed daily files, with no OpenAI summary generation involved, so
        # these always work immediately rather than entering the summary queue. Skipped once a bot account has
        # taken over (bot_takeover), same as /summary -- bot_listener.py handles both
        # there instead, to avoid two replies to the same command.
        if not bot_takeover and cfg.stats_enabled and (text_lower.startswith("/top") or text_lower.startswith("/stat")):
            chat = await event.get_chat()
            entry = matched_allowed_chat(chat)
            if entry is None:
                sent = await event.reply("Статистика недоступна в этом чате.")
                if sent is not None:
                    sent_message_ids.add(sent.id)
                    schedule_delete(event.client, event.chat_id, [sent.id], STATS_DELETE_AFTER)
                return
            # Strips a same-account "@my_username" mention Telegram tacks onto the
            # command with no space (e.g. "/stat@Trash_Modelist") before parsing the
            # period/username argument, so that alone means bare "/stat", not a lookup
            # for a user literally named after the bot -- see strip_command_bot_mention.
            stats_text = stats.strip_command_bot_mention(text, my_username)
            try:
                if text_lower.startswith("/top"):
                    period = stats.parse_top_command(stats_text)
                    reply_text = await stats.format_top(client, chat, entry, period, tz, cfg.stats_top_limit, log=log)
                else:
                    sender = await event.get_sender()
                    arg = stats_text[len("/stat") :].strip()
                    user, rank, total = await stats.resolve_stat_target(
                        client, chat, entry, arg, getattr(sender, "username", None), sender_display_name(sender), tz, log=log
                    )
                    reply_text = (
                        stats.format_stat(user, rank, total)
                        if user
                        else "Статистика не найдена -- пользователь ещё не отслеживается."
                    )
                sent = await event.reply(reply_text)
                if sent is not None:
                    sent_message_ids.add(sent.id)
                    schedule_delete(event.client, event.chat_id, [sent.id], STATS_DELETE_AFTER)
            except Exception:
                log(f"[listener] error handling stats command:\n{traceback.format_exc()}")
                try:
                    sent = await event.reply("Не удалось получить статистику.")
                    if sent is not None:
                        sent_message_ids.add(sent.id)
                        schedule_delete(event.client, event.chat_id, [sent.id], STATS_DELETE_AFTER)
                except Exception:
                    pass
            return

        # The trigger keyword (default "/summary") is the invocation itself, like a
        # slash-command -- no need to also @mention or reply to you. Works the same
        # whether you type it yourself or someone else does, in any allowed chat.
        has_summary_keyword = not bot_takeover and any(k in text_lower for k in cfg.listener_trigger_keywords)
        # Roast ("прожарь меня") is turned off -- forced False rather than removing the
        # surrounding roast_pending/on_reaction machinery below, so it stays a one-line
        # revert if it's ever turned back on.
        has_roast_keyword = False

        # Two more ways to ask for a summary without the exact trigger keyword: naming
        # you by first name alongside the word "summary" in one message, or replying to
        # one of your own messages and saying "summary". Both checks are gated on the
        # bare word "summary" being present at all, so plain chat never pays for the
        # extra (async, for the reply case) checks below. Skipped entirely once the bot
        # account has taken over (see bot_takeover above).
        if not bot_takeover and not has_summary_keyword and not has_roast_keyword and "summary" in text_lower:
            if my_first_name and my_first_name in text_lower:
                has_summary_keyword = True
            elif msg.is_reply:
                replied = await msg.get_reply_message()
                if replied is not None and replied.sender_id == me.id:
                    has_summary_keyword = True

        if not has_summary_keyword and not has_roast_keyword:
            return

        chat = await event.get_chat()
        if not is_chat_allowed(chat):
            return

        sender = await event.get_sender()
        if has_roast_keyword:
            roast_key = (event.chat_id, sender.id)
            if roast_key in roast_pending or roast_key in roast_in_progress:
                log(f"[listener] roast already pending/in-progress for {roast_key}, reacting instead of re-asking")
                await react_emoji(event.chat_id, msg.id, ROAST_BUSY_EMOJI)
                return

        try:
            if has_roast_keyword:
                confirm = await event.reply(ROAST_CONFIRM_TEXT)
                if confirm is not None:
                    sent_message_ids.add(confirm.id)
                    roast_pending[(event.chat_id, sender.id)] = (confirm.id, text)
                    log(f"[listener] sent roast confirmation to {sender_display_name(sender)} (msg {confirm.id})")
            else:
                await react_emoji(event.chat_id, msg.id, SUMMARY_ACK_EMOJI)
                await summary_queue.put((event, chat, text))
                log(
                    f"[listener] queued request #{summary_queue.qsize()} from "
                    f"'{getattr(chat, 'title', event.chat_id)}': {text!r}"
                )
        except Exception:
            log(f"[listener] error handling request:\n{traceback.format_exc()}")
            try:
                sent = await event.reply("Что-то пошло не так при генерации сводки.")
                if sent is not None:
                    sent_message_ids.add(sent.id)
            except Exception:
                pass

    async def _reactor_ids(chat_id, update):
        reactor_ids = set()
        for r in update.reactions.recent_reactions or []:
            try:
                reactor_ids.add(tl_utils.get_peer_id(r.peer_id))
            except Exception:
                continue
        if reactor_ids:
            return reactor_ids
        # recent_reactions isn't always populated (depends on chat size/settings) --
        # fall back to explicitly listing this message's reactors.
        try:
            result = await client(GetMessageReactionsListRequest(peer=chat_id, id=update.msg_id, limit=100))
            return {tl_utils.get_peer_id(r.peer_id) for r in result.reactions}
        except Exception as e:
            log(f"[listener] failed to fetch reactor list for msg {update.msg_id}: {e}")
            return set()

    # Reactions from a *user* account (not a bot) arrive as this raw update, carrying the
    # message's full new reaction state (not a per-reaction delta) -- used to confirm
    # both the roast flow (did the person asked "точно хочешь прожарку?" react to that
    # exact prompt) and the save flow (did *you* react to your own save confirmation).
    @client.on(events.Raw(types=UpdateMessageReactions))
    async def on_reaction(update):
        try:
            chat_id = tl_utils.get_peer_id(update.peer)
        except Exception:
            return

        save_key = (chat_id, update.msg_id)
        if save_key in save_pending:
            reactor_ids = await _reactor_ids(chat_id, update)
            if me.id not in reactor_ids:
                return  # not confirmed yet (or the confirming account itself didn't react)

            pending = save_pending.pop(save_key, None)
            if pending is None:
                return  # already handled (race with the unconfirmed-timeout cleanup)

            log(f"[listener] save confirmed via reaction: chat={chat_id} confirm_msg={update.msg_id}")

            async def _run_save():
                try:
                    await repost_saved_message(
                        client, save_channel_entity, pending["replied"], pending["added_text"]
                    )
                    log(f"[listener] saved message {pending['replied'].id} from chat {chat_id} to '{cfg.save_channel}'")
                    await react_emoji(chat_id, update.msg_id, SAVE_TICK_EMOJI)
                except Exception as e:
                    log(f"[listener] failed to save message: {e}")
                finally:
                    schedule_delete(client, chat_id, [update.msg_id], SAVE_CONFIRM_DELETE_AFTER)

            task = asyncio.create_task(_run_save())
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)
            return

        joke_watch_key = (chat_id, update.msg_id)
        if joke_watch_key in joke_reaction_watch:
            # Keyed by the exact (chat, message) pair, so this can never fire off
            # reactions to some other/older message -- and it's popped the instant it
            # crosses the threshold, so a message that keeps collecting reactions after
            # that (or this handler firing again for the same message, which it will, since
            # every additional reaction re-sends the full state) can't re-trigger the
            # reduction a second time.
            posted_at = joke_reaction_watch[joke_watch_key]
            reactor_ids = await _reactor_ids(chat_id, update)
            if len(reactor_ids) >= cfg.joke_reaction_threshold:
                del joke_reaction_watch[joke_watch_key]
                reduced_until = posted_at + cfg.joke_reaction_cooldown_seconds
                if reduced_until < joke_cooldown_until.get(chat_id, float("inf")):
                    joke_cooldown_until[chat_id] = reduced_until
                    log(
                        f"[listener] joke in chat {chat_id} (msg {update.msg_id}) got "
                        f"{len(reactor_ids)} reactions -- cooldown reduced"
                    )
            return

        key = next(
            (k for k, (mid, _) in roast_pending.items() if k[0] == chat_id and mid == update.msg_id), None
        )
        if key is None:
            return  # not a reaction on a pending roast-confirmation message

        _, target_user_id = key
        reactor_ids = await _reactor_ids(chat_id, update)
        if target_user_id not in reactor_ids:
            return  # someone else reacted -- not the person who was actually asked

        confirm_msg_id, original_text = roast_pending.pop(key)
        roast_in_progress.add(key)
        log(f"[listener] roast confirmed via reaction: chat={chat_id} user={target_user_id}")

        async def _run():
            try:
                chat_entity = await client.get_entity(chat_id)
                target_user = await client.get_entity(target_user_id)
                await run_roast(
                    client,
                    chat_entity,
                    target_user,
                    confirm_msg_id,
                    original_text,
                    cfg,
                    tz,
                    sent_message_ids,
                    schedule_delete,
                    log=log,
                )
            except Exception:
                log(f"[listener] error generating confirmed roast:\n{traceback.format_exc()}")
                try:
                    sent = await client.send_message(
                        chat_id, "Что-то пошло не так при генерации прожарки.", reply_to=confirm_msg_id
                    )
                    if sent is not None:
                        sent_message_ids.add(sent.id)
                except Exception:
                    pass
            finally:
                roast_in_progress.discard(key)

        task = asyncio.create_task(_run())
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    if joke_enabled:
        # Drains joke_posted_queue for the life of the process: bot_listener.py puts
        # (allowed_chats entry, sent message_id) here once a queued joke is actually sent,
        # which is this process's only way to learn that -- it's what starts the post-send
        # cooldown and what makes that specific message eligible for the reaction-count
        # cooldown reduction in on_reaction above.
        async def _consume_joke_posted():
            while True:
                entry, message_id = await joke_posted_queue.get()
                chat_key = joke_entry_to_chat.get(entry)
                if chat_key is None:
                    log(f"[listener] joke was posted for '{entry}' but its chat_id isn't known here -- skipping cooldown/reaction tracking")
                    continue
                now = time.monotonic()
                joke_cooldown_until[chat_key] = now + random.uniform(
                    cfg.joke_cooldown_min_seconds, cfg.joke_cooldown_max_seconds
                )
                # Lazy GC: an entry only leaves this dict early if it actually crosses the
                # reaction threshold (see on_reaction) -- anything that never does would
                # otherwise sit here forever, so drop anything old enough that it's no
                # longer worth watching (well past any realistic cooldown).
                stale_cutoff = now - 4 * cfg.joke_cooldown_max_seconds
                for k in [k for k, posted_at in joke_reaction_watch.items() if posted_at < stale_cutoff]:
                    del joke_reaction_watch[k]
                joke_reaction_watch[(chat_key, message_id)] = now

        asyncio.create_task(_consume_joke_posted())

    if followup_enabled:
        # Drains bot_response_queue for the life of the process: bot_listener.py puts
        # (chat_id, sent_message_ids, kind, response_text) here right after it posts a
        # summary answer or a joke to a group chat. Replaces any watch already active for
        # that chat wholesale -- only the most recent bot response is worth reacting to,
        # so a still-open window from an earlier one is simply abandoned.
        async def _consume_bot_responses():
            while True:
                chat_id, sent_message_ids, kind, response_text = await bot_response_queue.get()
                followup_watch[chat_id] = {
                    "kind": kind,
                    "response_text": response_text,
                    "message_ids": set(sent_message_ids),
                    "buffer": [],
                    "in_flight": False,
                }
                log(
                    f"[listener] watching chat {chat_id} for reactions to its last {kind} "
                    f"(window: {cfg.followup_window_messages} messages)"
                )

        asyncio.create_task(_consume_bot_responses())

    if cfg.stats_enabled:
        # run_stats_rollover itself no-ops (with its own log line) if
        # LISTENER_ALLOWED_CHATS is empty -- stats tracking, like jokes, needs specific
        # chats named rather than defaulting to "everywhere".
        asyncio.create_task(run_stats_rollover(client, cfg, tz, log=log))

    joke_status = (
        f"on ({cfg.joke_activity_min_messages} msgs to fill the buffer, "
        f"{cfg.joke_fire_probability:.0%} chance, {cfg.joke_cooldown_min_seconds}-{cfg.joke_cooldown_max_seconds}s "
        f"cooldown, reduced to {cfg.joke_reaction_cooldown_seconds}s on {cfg.joke_reaction_threshold}+ reactions)"
        if joke_enabled
        else "off"
    )
    followup_status = (
        f"on (window: {cfg.followup_window_messages} messages, checked every {cfg.followup_check_every_messages})"
        if followup_enabled
        else "off"
    )
    stats_status = "on" if (cfg.stats_enabled and cfg.listener_allowed_chats) else "off"
    log(
        f"[listener] logged in as @{my_username or me.id}. Watching for messages containing "
        f"{cfg.listener_trigger_keywords} (summary, pipeline {cfg.summary_pipeline_version}; roast is off) "
        f"and your own '{cfg.save_trigger_keyword}' replies (save to {cfg.save_channel or 'disabled'}). "
        f"Summary queue: FIFO, {cfg.summary_queue_delay_seconds}s between completed jobs. "
        f"Joke: {joke_status}. Follow-up reactions: {followup_status}. Stats (/top, /stat): {stats_status}. "
        f"Timezone: {tz}. Ctrl+C to stop."
    )
    await client.run_until_disconnected()


async def main():
    cfg = load_config()
    tz = resolve_tz(None)

    # Diagnostic only -- never prints the secret itself, just whether the process
    # actually received it, to distinguish "not set on this host" from "set but wrong"
    # without needing to inspect the deployment platform's UI by eye.
    if cfg.session_string:
        print(f"[listener] TELEGRAM_SESSION_STRING: set ({len(cfg.session_string)} chars)")
    else:
        print("[listener] TELEGRAM_SESSION_STRING: NOT SET in this process's environment")

    client = build_client(cfg)
    await client.start()

    if cfg.telegram_bot_token:
        # Local import: bot_listener.py imports several helpers back from this module
        # (resolve_time_window, DayLimitExceeded, etc.), so importing it at module level
        # here would be a circular import. By the time main() runs, this module has
        # already finished executing top-level code, so the cycle resolves fine.
        import bot_listener

        print("[listener] TELEGRAM_BOT_TOKEN is set -- also starting bot_listener.py for /summary.")
        # joke_queue hands a generated joke (see maybe_joke in run_listener) from this
        # Telethon session, which is the only one that sees every plain-text message, to
        # the bot account, which is the only one that should ever post one.
        # joke_posted_queue carries the reply the other way: the sent message_id, once
        # bot_listener.py actually posts it, so this session can start its cooldown and
        # watch that message for reactions (only this session can reliably see other
        # users' reactions without the bot needing admin rights).
        joke_queue: asyncio.Queue = asyncio.Queue()
        joke_posted_queue: asyncio.Queue = asyncio.Queue()
        # bot_response_queue/followup_queue are the same hand-off shape for a different
        # feature (see followup.py): bot_listener.py puts (chat_id, sent_message_ids,
        # kind, response_text) on bot_response_queue right after ANY summary answer or
        # joke is posted to a group chat, so this session can watch the next few messages
        # for chat commentary about it; followup_queue carries a generated clap-back back
        # to bot_listener.py to actually send, same bot-account-only rule as every other
        # reply.
        bot_response_queue: asyncio.Queue = asyncio.Queue()
        followup_queue: asyncio.Queue = asyncio.Queue()
        await asyncio.gather(
            run_listener(
                client, cfg, tz,
                joke_queue=joke_queue, joke_posted_queue=joke_posted_queue,
                bot_response_queue=bot_response_queue, followup_queue=followup_queue,
            ),
            bot_listener.run_bot_listener(
                cfg.telegram_bot_token, cfg, tz, client,
                joke_queue=joke_queue, joke_posted_queue=joke_posted_queue,
                bot_response_queue=bot_response_queue, followup_queue=followup_queue,
            ),
        )
    else:
        await run_listener(client, cfg, tz)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ChatSummaryError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        sys.exit(130)
