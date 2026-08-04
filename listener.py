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
import html
import re
import sys
import time
import traceback
from collections import deque
from datetime import date, datetime, timedelta

from telethon import TelegramClient, events, utils as tl_utils
from telethon.tl.functions.messages import GetMessageReactionsListRequest, SendReactionRequest
from telethon.tl.types import ReactionEmoji, UpdateMessageReactions

import history
import stats
import tree
from config import SUMMARY_COMMAND, build_session, load_config
from errors import ChatSummaryError
from intent import parse_summary_request, resolve_name_hint
from intent_v2 import route_request
from main import period_label, resolve_tz
from responder_v2 import answer_request
from summarizer import summarize_transcript
from telegram_fetch import (
    fetch_range_messages_cached,
    format_transcript_lines,
    is_image_message,
    is_video_message,
    resolve_chat,
    sender_display_name,
    sender_matches,
)

MENTION_RE = re.compile(r"@(\w{4,32})")
MAX_REPLY_CHARS = 4000  # stay under Telegram's ~4096 message limit
IMPRESSION_MIN_MESSAGES = 15
IMPRESSION_RE = re.compile(r"\bвпечатлен\w*", re.IGNORECASE)
EXPLICIT_TIME_RE = re.compile(
    r"\b(?:сегодня|вчера|позавчера|today|yesterday|последн\w*|last\s+\d+|"
    r"за\s+(?:\d+\s+)?(?:час\w*|дн\w*|день|сут\w*|недел\w*|месяц\w*))\b|"
    r"\b\d{4}-\d{2}-\d{2}\b",
    re.IGNORECASE,
)

# Appended to every successful summary reply so people re-discover the available commands
# without having to ask -- not shown on short rejection/error notices, which already
# explain themselves and self-delete fast.
COMMANDS_FOOTER = (
    "Список команд:\n"
    "/summary + время, юзер, вопрос\n"
    "/tree наше ЕЧХ дерево\n"
    "/stat @user - статистика пользователя\n"
    "/stat pokras - список лентяев"
)

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


# Reacted onto the triggering message itself as soon as a summary request is accepted,
# so the requester gets instant feedback that it was picked up while the LLM calls
# (which can take a few seconds) run.
SUMMARY_ACK_EMOJI = "✍"

# Reacted onto a #япокрасил post (with a photo or video attached) the instant it's seen -- see
# on_message's figurine-detection block and stats.record_figurine_live. Must be one of
# Telegram's own fixed "quick reaction" emoji set (core.telegram.org/api/reactions) --
# anything outside that set is rejected by the API. "🎨" (artist palette) is NOT in that
# set, which is why reactions were silently failing (see bot_api.set_message_reaction's
# now-added failure logging); "🔥" is a real quick-reaction emoji.
FIGURINE_ACK_EMOJI = "🔥"

# "/stat pokras" on-demand fallback when stats.format_procrastinators finds nobody to
# call out (no candidates at all, or everyone it walked through posted within the
# window) -- it returns None rather than an empty list in that case, so callers supply
# their own "all clear" message.
PROCRASTINATOR_NONE_FOUND_MESSAGE = "Все скидывали покрасы вовремя -- прокрастинаторов не найдено."

ERROR_DELETE_AFTER = 10  # short rejection notices (such as day limit) self-delete fast
STATS_DELETE_AFTER = 300  # /top and /stat replies (incl. their own errors) self-delete after 5 minutes

# "сохрани" (config.py, SAVE_TRIGGER_KEYWORD env var), sent by you as a reply to any
# message, asks (via a confirmation prompt + reaction) whether to
# repost that message -- photo, video, any media, or just text -- to your save channel
# (SAVE_CHANNEL), with any text after the trigger word appended as a caption. Your own
# trigger message is deleted immediately (it's always yours -- see msg.out check in
# on_message); the confirmation prompt gets a tick reaction and self-deletes once
# confirmed, or self-deletes unconfirmed after SAVE_CONFIRM_TIMEOUT. Unlike
# summary, this ignores LISTENER_ALLOWED_CHATS entirely, since it never touches
# the OpenAI budget.
SAVE_CONFIRM_TEXT = "Сохранить в t.me/papka_pokrasa?\nреакция для подтверждения."
SAVE_TICK_EMOJI = "✅"
SAVE_CONFIRM_TIMEOUT = 10  # seconds to wait for a confirming reaction before cancelling
SAVE_CONFIRM_DELETE_AFTER = 3  # seconds after a tick reaction before the prompt is deleted

# Reacting with a thumbs-up on ANY message this account or the bot sent -- not just one
# awaiting a specific confirmation, like the save flow above -- is a one-tap "get
# rid of this" shortcut, checked in on_reaction only once none of those more specific
# flows claim the reaction first. Deliberately scoped to messages we sent (msg.out or a
# bot sender): this account may well have delete rights over the whole chat, but a stray
# 👍 on someone else's message must never delete it.
DISMISS_EMOJI = "👍"
DISMISS_DELETE_AFTER = 1  # seconds -- meant to feel closer to instant than a courtesy pause

# Archives and 3D-model files are the one kind of attachment the chat doesn't host: they
# are deleted on sight and their sender is told to pass them around in a DM instead.
# Matched on the attachment's own filename extension rather than its mime type -- Telegram
# hands most of these over as a generic application/octet-stream, so the filename (carried
# in the document's DocumentAttributeFilename) is the only thing that tells a .stl apart
# from any other blob. Photos, videos and stickers have no filename attribute at all, so
# an ordinary #япокрасил post can never match here.
BLOCKED_FILE_EXTENSIONS = (".zip", ".7z", ".rar", ".stl", ".obj", ".glb")

# Sent into the chat after such a file is deleted, with {mention} replaced by the sender
# (see format_blocked_file_notice). Deliberately NOT scheduled for deletion, unlike the
# short rejection notices above: it's the only trace left of the removed message and the
# explanation for it, so it stays in the chat.
BLOCKED_FILE_NOTICE = "{mention}, пересылка файлов разрешена только в личке. Спасибо за понимание."


def blocked_file_name(msg) -> str | None:
    """The attachment's filename if this message carries a file whose extension is in
    BLOCKED_FILE_EXTENSIONS, else None -- returned rather than a bool purely so the log
    line can name what was removed.

    Only documents are inspected: a filename only ever reaches Telegram as a document
    attribute, so compressed photos/videos, stickers, voice notes and plain text all fall
    straight through."""
    document = getattr(msg, "document", None)
    if document is None:
        return None
    for attr in getattr(document, "attributes", None) or []:
        name = getattr(attr, "file_name", None)
        if name and name.lower().endswith(BLOCKED_FILE_EXTENSIONS):
            return name
    return None


def format_blocked_file_notice(sender) -> str:
    """BLOCKED_FILE_NOTICE addressed to `sender`, as HTML.

    An @username is used when there is one -- it's what people recognise, and it notifies.
    Without one (usernames are optional, and plenty of members have none) a
    tg://user?id= link over the display name is the only way to still address the right
    person, hence HTML rather than plain text; the name is escaped since it's somebody
    else's uncontrolled text."""
    username = getattr(sender, "username", None)
    if username:
        mention = f"@{username}"
    else:
        name = html.escape(sender_display_name(sender))
        user_id = getattr(sender, "id", None)
        mention = f'<a href="tg://user?id={user_id}">{name}</a>' if user_id else name
    return BLOCKED_FILE_NOTICE.format(mention=mention)


def extract_mentioned_usernames(text: str, exclude: str | None) -> list[str]:
    names = {m.group(1) for m in MENTION_RE.finditer(text or "")}
    if exclude:
        names = {n for n in names if n.lower() != exclude.lower()}
    return sorted(names)


def is_summary_command(text: str) -> bool:
    """Whether this message asks for a summary -- i.e. OPENS with "/summary".

    A summary costs an OpenAI call, so the invocation has to be deliberate. Merely
    containing the word somewhere is not enough: quoting what someone else asked ("я
    написал /summary а он молчит") used to fire a real request, and in a busy chat that
    is a bill nobody chose to run up.

    Right after the command there must be a boundary: end of message, whitespace, or "@"
    -- Telegram appends "@botname" to commands in groups, and "/summary@my_bot за вчера"
    is the same invocation as "/summary за вчера". Anything else ("/summarize") is a
    different command and does not match.
    """
    stripped = (text or "").lstrip()
    if not stripped.lower().startswith(SUMMARY_COMMAND):
        return False
    rest = stripped[len(SUMMARY_COMMAND):]
    return not rest or rest[0].isspace() or rest[0] == "@"


def strip_summary_command(text: str) -> str:
    """Removes the leading "/summary" (or "/summary@my_bot") from the request, so the LLM
    sees the actual question ("кто такой Степан") rather than the invocation itself.

    Only the opening invocation is removed, and only when the text actually starts with
    one -- the same word later in the sentence is part of the question the person asked
    and must survive into the prompt.
    """
    stripped = (text or "").lstrip()
    if not is_summary_command(stripped):
        return stripped.strip()
    rest = stripped[len(SUMMARY_COMMAND):]
    # The "@botname" Telegram tacks onto a group command belongs to the invocation too.
    rest = re.sub(r"^@\S+", "", rest)
    return rest.strip()


def _is_default_impression_request(text: str, routed: dict, ref_date: date) -> bool:
    """Whether this is a person-impression request with no explicit time period."""
    return (
        bool(routed.get("username"))
        and bool(IMPRESSION_RE.search(text or ""))
        and not EXPLICIT_TIME_RE.search(text or "")
        and routed.get("lookback_hours") is None
        and routed.get("start_date") == ref_date
        and routed.get("end_date") == ref_date
    )


async def _expand_sparse_impression_history(
    client,
    chat_ref,
    tz,
    text: str,
    routed: dict,
    ref_date: date,
    current_start_date: date,
    messages: list,
    log=print,
    log_prefix: str = "[listener]",
) -> tuple[date, list, bool]:
    """Prepends yesterday when today's target activity is below the impression floor.

    Explicit dates remain authoritative. The fallback only expands the otherwise-default
    current-day window, and stops after one additional Moscow calendar day.
    """
    if not _is_default_impression_request(text, routed, ref_date):
        return current_start_date, messages, False

    username_hint = routed["username"]
    today_count = sum(1 for message in messages if sender_matches(message, username_hint))
    if today_count >= IMPRESSION_MIN_MESSAGES:
        log(
            f"{log_prefix} impression target '{username_hint}' has {today_count} message(s) "
            f"today; using today only"
        )
        return current_start_date, messages, False

    previous_day = ref_date - timedelta(days=1)
    _, previous_messages = await fetch_range_messages_cached(
        client=client,
        chat_ref=chat_ref,
        start_day=previous_day,
        end_day=previous_day,
        tz=tz,
        log=log,
    )
    combined = previous_messages + messages
    combined_count = sum(1 for message in combined if sender_matches(message, username_hint))
    log(
        f"{log_prefix} impression target '{username_hint}' has only {today_count} message(s) "
        f"today (<{IMPRESSION_MIN_MESSAGES}); added {previous_day} "
        f"({combined_count} target message(s) across both days)"
    )
    return previous_day, combined, combined_count < IMPRESSION_MIN_MESSAGES


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
            model=cfg.openai_routing_model,
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
                resolve_name_hint, cfg.openai_api_key, cfg.openai_routing_model, name_hint, candidates
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
    original_question = strip_summary_command(text)

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
            model=cfg.openai_routing_model,
            text=text,
            reference_date=ref_date,
            mentioned_usernames=mentioned,
            my_username=my_username,
            requester_username=getattr(sender, "username", None),
            requester_name=requester,
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

    start_date, messages, impression_inactive = await _expand_sparse_impression_history(
        client=event.client,
        chat_ref=chat,
        tz=tz,
        text=text,
        routed=routed,
        ref_date=ref_date,
        current_start_date=start_date,
        messages=messages,
        log=log,
    )

    if impression_inactive:
        await respond(f"@{routed['username']} не был активным эти дни")
        return

    focus_user = None
    username_hint = routed["username"]
    requester_aliases = {
        value.strip().lstrip("@").lower()
        for value in (requester, getattr(sender, "username", None))
        if value and value.strip()
    }
    if username_hint and username_hint.strip().lstrip("@").lower() in requester_aliases:
        # The router interpreted the original request as being about its author. Use the
        # transcript's display name and verify the identity with Telegram sender_id.
        focus_user = requester
        requester_id = getattr(sender, "id", None)
        matched = sum(1 for m in messages if requester_id is not None and m.sender_id == requester_id)
        log(f"[listener] v2 focus_user(requester)={focus_user} matched={matched}/{len(messages)}")
    elif username_hint:
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
                    resolve_name_hint, cfg.openai_api_key, cfg.openai_routing_model, username_hint, candidates
                )
            except ChatSummaryError as e:
                log(f"[listener] v2 name resolution failed: {e}")
                focus_user = None
            if focus_user:
                log(f"[listener] v2 resolved name hint '{username_hint}' -> '{focus_user}'")
            else:
                log(f"[listener] v2 could not resolve name hint '{username_hint}' among participants")
                # The final responder has the untouched original request and requester
                # identity, so let it interpret the question instead of rejecting here.
                focus_user = None

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
        original_request=text,
        requester_name=requester,
    )

    await respond(f"{answer}\n\n{COMMANDS_FOOTER}")


async def _stats_catch_up(client, cfg, tz, stats_digest_queue=None, log=print) -> None:
    """For each chat in cfg.listener_allowed_chats, closes out every day in the last
    cfg.stats_catchup_days that isn't recorded yet (see stats.finalize_and_record) --
    oldest first. Cheap to call repeatedly: a day on the current stats schema costs only
    a local JSON read. A recorded day from before hashtag badges existed is read once
    from the normal transcript cache so those badge counters can be backfilled without
    changing XP. The badge-only scan covers the last 14 days (and skips days that do not
    already have stats), ensuring the preceding weekly contest is included. This covers both the very first run (backfill up to
    stats_catchup_days of history) and every subsequent midnight with the same path."""
    today = datetime.now(tz).date()
    for entry in cfg.listener_allowed_chats:
        try:
            chat_entity = await resolve_chat(client, entry)
        except Exception as e:
            log(f"[stats] could not resolve '{entry}' for catch-up: {e}")
            continue
        lookback_days = max(cfg.stats_catchup_days, stats.HASHTAG_BADGE_BACKFILL_DAYS)
        for delta in range(lookback_days, 0, -1):
            day = today - timedelta(days=delta)
            # Outside the configured general catch-up window, augment only a day that
            # already has stats. This reaches the previous contest week for hashtag
            # badges without creating extra historical XP days as a side effect.
            if delta > cfg.stats_catchup_days and not stats.is_recorded(entry, day):
                continue
            try:
                await stats.finalize_and_record(client, chat_entity, entry, day, tz, log=log)
            except Exception:
                log(f"[stats] failed to catch up '{entry}' for {day}:\n{traceback.format_exc()}")
        if stats_digest_queue is not None:
            try:
                announcements = await stats.collect_level_up_announcements(
                    client, chat_entity, entry, tz, log=log
                )
                for announcement in announcements:
                    # The queue's item is (entry, text, parse_mode, image) -- the same
                    # four-part shape the tree and procrastinator digests put on it. A
                    # level-up is plain text with no picture, but it still has to be
                    # spelled in full: the consumer unpacks four, and a short item took
                    # bot_listener's digest loop down with a ValueError, silently ending
                    # every digest until the process was restarted.
                    await stats_digest_queue.put((entry, announcement, None, None))
            except Exception:
                log(
                    f"[stats] failed to collect level-up announcements for "
                    f"'{entry}':\n{traceback.format_exc()}"
                )


async def _send_procrastinator_digests(client, cfg, tz, stats_digest_queue, log=print) -> None:
    """Builds and queues the "Топ покрастинаторов" call-out (stats.format_procrastinators)
    for every chat in LISTENER_ALLOWED_CHATS due for one today (stats.
    should_send_procrastinator_digest -- the every-other-day cadence, tracked per entry via
    a persisted last-sent date so it survives restarts) -- ambient, unprompted content
    posted to the group with nobody having asked for it, so it follows the SAME
    bot-account-only rule as every other post: `stats_digest_queue` is only ever non-None
    when a bot account is configured (see main()), and this is simply a no-op otherwise --
    no personal-account fallback.

    `client` is passed straight through to stats.format_procrastinators (as both `client`
    and, via the plain entry string, `chat_ref` -- resolve_chat can take either) so it can
    re-derive today's figurine posts the same reliable way /stat does, instead of trusting
    only the local live-counter file (see format_procrastinators's own docstring for the
    real production bug this fixed).

    Marks each due entry as sent (stats.mark_procrastinator_sent) right after attempting
    it, regardless of whether there was anything to report -- an empty result still counts
    as today's check-in, so it doesn't get retried before the next scheduled one. Only
    skipped (left for a future check-in) if building the digest itself raises."""
    if stats_digest_queue is None:
        return
    today = datetime.now(tz).date()
    for entry in cfg.listener_allowed_chats:
        if not stats.should_send_procrastinator_digest(entry, today):
            continue
        try:
            text = await stats.format_procrastinators(client, entry, entry, tz, log=log)
            if text:
                await stats_digest_queue.put((entry, text, None, None))
                log(f"[stats] queued procrastinator digest for '{entry}'")
            else:
                log(f"[stats] procrastinator digest for '{entry}' has nothing to report today")
            stats.mark_procrastinator_sent(entry, today)
        except Exception:
            log(f"[stats] failed to build procrastinator digest for '{entry}':\n{traceback.format_exc()}")


async def _stats_catchup_loop(client, cfg, tz, stats_digest_queue=None, log=print) -> None:
    """Runs _stats_catch_up once immediately (covers a restart that missed one or more
    midnights while down), then sleeps until the next local midnight and runs it again,
    forever. A few seconds of buffer after :00 avoids any edge-case race right at the
    rollover instant.

    The parameter is named for the queue it is actually given (run_stats_rollover passes
    stats_digest_queue positionally): it used to be called level_announcement_queue, and
    that name is what hid a mismatched item shape all the way into production.
    """
    await _stats_catch_up(client, cfg, tz, stats_digest_queue, log=log)
    while True:
        now = datetime.now(tz)
        next_run = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        await asyncio.sleep((next_run - now).total_seconds())
        await _stats_catch_up(client, cfg, tz, stats_digest_queue, log=log)


async def _send_tree_digests(client, cfg, tz, stats_digest_queue, log=print) -> None:
    """Builds and queues the morning ЕПХ-tree post for every tracked chat.

    Reports YESTERDAY: at 10:00 today's own numbers are three hours old and would make
    the growth figure meaningless. Yesterday is also a closed, recorded day, so the same
    morning re-run after a restart produces the same post rather than a moving one.

    A per-chat marker means a restart cannot post it twice, and a chat whose digest
    raises is simply left for tomorrow instead of blocking the others.
    """
    if stats_digest_queue is None:
        return
    now = datetime.now(tz)
    today = now.date()
    # Nothing to catch up on before the hour itself. Without this the startup check
    # would fire the moment the process comes up -- planting the tree at 05:00 on the
    # day it ships instead of at the 10:00 it was promised for.
    if now.hour < stats.TREE_DIGEST_HOUR:
        return
    yesterday = today - timedelta(days=1)
    for entry in cfg.listener_allowed_chats:
        if not stats.should_send_tree_digest(entry, today):
            continue
        try:
            # A ceremony opened with /посадить_семечко closes here: this is the "последняя
            # горсть земли" the invitation promised, and the roll call takes the place of
            # the morning digest for one day.
            ceremony = stats.planting_state(entry)
            if ceremony is not None:
                joined = stats.planters(entry)
                if not joined:
                    # Nobody pressed. The tree is deliberately NOT planted and the
                    # ceremony stays open: opening the whole thing on an empty roll call
                    # would be worse than waiting another day for a better one.
                    await stats_digest_queue.put(
                        (entry, tree.format_nobody_planted_message(), "HTML", None)
                    )
                    stats.mark_tree_digest_sent(entry, today)
                    log(f"[stats] planting for '{entry}' had no takers, still open")
                    continue
                awarded = stats.award_founder_badges(entry)
                stats.close_planting(entry)
                stats.mark_tree_planted(entry, today)
                # No picture on this one, nor on the planting post below: both run far past
                # Telegram's 1024-character caption limit, so a photo would only cost them
                # the text.
                await stats_digest_queue.put(
                    (entry, tree.format_planting_roll_call(joined), "HTML", None)
                )
                stats.mark_tree_digest_sent(entry, today)
                log(f"[stats] planted '{entry}' with {len(joined)} planters ({awarded} new badges)")
                continue

            # With no ceremony, the very first post plants the tree rather than reporting
            # on it: on that day there is nothing to report, and the height starts from
            # zero here.
            planting = stats.tree_planted_on(entry) is None
            image = None
            if planting:
                stats.mark_tree_planted(entry, today)
                text = tree.format_planting_message()
                day_xp = 0
            else:
                total_xp, day_xp, contributors = await stats.chat_tree_totals(
                    client, entry, entry, yesterday, tz, log=log
                )
                text = tree.format_morning_digest(total_xp, day_xp, contributors, today)
                # None until somebody drops a file in assets/tree_stages for this stage;
                # the post then goes out as text, exactly as it did before.
                image = tree.stage_image(total_xp)
            await stats_digest_queue.put((entry, text, "HTML", image))
            stats.mark_tree_digest_sent(entry, today)
            log(
                f"[stats] queued tree {'planting' if planting else 'digest'} for "
                f"'{entry}' (+{day_xp} XP yesterday, image: {image.name if image else 'none'})"
            )
        except Exception:
            log(f"[stats] failed to build tree digest for '{entry}':\n{traceback.format_exc()}")


async def _tree_digest_loop(client, cfg, tz, stats_digest_queue, log=print) -> None:
    """Wakes every day at 10:00 MOSCOW time -- pinned to Europe/Moscow rather than the
    app timezone, because the chat asked for a Moscow morning and the deployment's own
    timezone is a hosting detail that could move.

    Runs a check once on startup too, so a process that was down at 10:00 still posts
    when it comes back rather than skipping the day entirely; the per-chat marker keeps
    that from double-posting.
    """
    if stats_digest_queue is None:
        return
    # Said once, at startup, because a missing stage picture has no other symptom: the
    # post simply goes out as text and nobody knows a file was expected.
    missing = tree.missing_stage_images()
    if missing:
        log(f"[stats] tree stage images missing ({len(missing)}/{len(tree.TREE_STAGES)}): {', '.join(missing)}")
    else:
        log(f"[stats] all {len(tree.TREE_STAGES)} tree stage images present")
    await _send_tree_digests(client, cfg, stats.tree_digest_tz(), stats_digest_queue, log=log)
    while True:
        moscow = stats.tree_digest_tz()
        now = datetime.now(moscow)
        next_run = now.replace(
            hour=stats.TREE_DIGEST_HOUR, minute=0, second=10, microsecond=0
        )
        if next_run <= now:
            next_run += timedelta(days=1)
        await asyncio.sleep((next_run - now).total_seconds())
        await _send_tree_digests(client, cfg, moscow, stats_digest_queue, log=log)


async def _procrastinator_digest_loop(client, cfg, tz, stats_digest_queue, log=print) -> None:
    """Wakes every day at stats.PROCRASTINATOR_DIGEST_HOUR local time and hands off to
    _send_procrastinator_digests, which itself decides (per entry) whether today is
    actually a "send" day under the every-other-day cadence -- so this loop's job is only
    ever "check in at 19:00", not "send every time". No-ops immediately (never sleeps) if
    there's no bot account, same gating as the catch-up loop's caller checks for
    LISTENER_ALLOWED_CHATS."""
    if stats_digest_queue is None:
        return
    while True:
        now = datetime.now(tz)
        next_run = now.replace(hour=stats.PROCRASTINATOR_DIGEST_HOUR, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        await asyncio.sleep((next_run - now).total_seconds())
        await _send_procrastinator_digests(client, cfg, tz, stats_digest_queue, log=log)


async def run_stats_rollover(client, cfg, tz, stats_digest_queue=None, log=print) -> None:
    """Keeps stats.py's per-day-per-chat files up to date for every chat named in
    LISTENER_ALLOWED_CHATS (stats tracking needs specific chats named -- there's no
    "everywhere" fallback), and separately drives the automatic "Топ
    покрастинаторов" digest -- two independent cadences run concurrently
    (_stats_catchup_loop at midnight, _procrastinator_digest_loop at
    stats.PROCRASTINATOR_DIGEST_HOUR every stats.PROCRASTINATOR_DIGEST_INTERVAL_DAYS days)
    since they serve different purposes and shouldn't block on each other."""
    if not cfg.listener_allowed_chats:
        log("[stats] STATS_ENABLED is set but LISTENER_ALLOWED_CHATS is empty -- stats tracking needs specific chats named, so it's off.")
        return

    await asyncio.gather(
        _stats_catchup_loop(client, cfg, tz, stats_digest_queue, log=log),
        _procrastinator_digest_loop(client, cfg, tz, stats_digest_queue, log=log),
        _tree_digest_loop(client, cfg, tz, stats_digest_queue, log=log),
    )


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
    figurine_ack_queue: "asyncio.Queue | None" = None,
    stats_digest_queue: "asyncio.Queue | None" = None,
    dismiss_queue: "asyncio.Queue | None" = None,
    file_block_queue: "asyncio.Queue | None" = None,
):
    """Registers the mention-trigger handler on an already-connected & authorized
    `client` and blocks until it disconnects (call `client.disconnect()` to stop it).

    `figurine_ack_queue`, if given, is where (allowed_chats entry, message_id) goes for a
    #япокрасил+photo/video message this session has just seen -- see on_message's figurine-
    detection block. This session always does the counting itself (stats.
    record_figurine_live), since it's the only one that sees every message, and reacts
    itself too when there's no bot account to defer to -- but once a bot token is
    configured, the reaction has to come from the bot account instead, same bot-account-
    only rule as every reply, hence the hand-off (see bot_takeover below).

    `stats_digest_queue`, if given, is where (allowed_chats entry, text) goes every
    stats.PROCRASTINATOR_DIGEST_INTERVAL_DAYS days at stats.PROCRASTINATOR_DIGEST_HOUR
    local time for the "Топ покрастинаторов" call-out (see run_stats_rollover/
    stats.format_procrastinators) -- unprompted, ambient content, so same
    bot-account-only rule as every other post: passed through to run_stats_rollover, and
    simply never sent (no personal-account fallback) if there's no bot account.

    `dismiss_queue`, if given, is where (chat_id, message_id) goes from
    _maybe_dismiss_on_thumbs_up (see on_reaction) when the thumbs-up dismiss shortcut
    targets a message THIS session can't delete itself -- one sent by the bot account,
    which this personal account typically has no delete rights over unless it happens to
    be a chat admin (unlike a message this account sent itself, which it can always
    delete and does directly, no hand-off needed). bot_listener.py deletes it via the Bot
    API instead, which -- like every other reply -- can always delete its OWN messages
    without needing admin rights.

    `file_block_queue`, if given, is where (allowed_chats entry, message_id, notice text)
    goes when this session sees an archive/3D-model attachment (see BLOCKED_FILE_EXTENSIONS
    and on_message's blocked-file block). Same split as everything else here: this session
    is the only one that reliably sees every message and every filename, while the deletion
    and the notice are done by the bot account over in bot_listener.py -- which, unlike this
    personal account, is normally the chat admin holding the "delete messages" right."""
    assert cfg.summary_queue_delay_seconds >= 0, "internal bug: queue delay should have been validated by config"

    me = await client.get_me()
    my_username = me.username

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
    # *your own* account reposting to your own channel.
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

    # Save confirm/react flow state, keyed by (chat_id, confirm_message_id) --
    # keyed directly by the confirmation message itself since a save
    # only ever needs one pending confirmation per prompt (no "already pending for this
    # user" concept to track). Value carries what to repost once/if confirmed.
    save_pending: dict[tuple[int, int], dict] = {}

    # (chat_id, grouped_id) of albums already answered by a blocked-file notice. Telegram
    # delivers an album as one message per file, so ten .stl files dragged in at once would
    # otherwise be ten deletions AND ten identical notices -- every file still goes, but
    # the sender is told once. Bounded by maxlen; nothing here needs to outlive the burst
    # it belongs to.
    blocked_album_groups: deque = deque(maxlen=50)

    def is_chat_allowed(chat) -> bool:
        if not allowed_chats:
            return True
        username = (getattr(chat, "username", "") or "").lower()
        title = (getattr(chat, "title", "") or "").lower()
        chat_id = str(getattr(chat, "id", ""))
        return username in allowed_chats or title in allowed_chats or chat_id in allowed_chats

    def matched_allowed_chat(chat) -> str | None:
        """Like is_chat_allowed, but returns the actual LISTENER_ALLOWED_CHATS entry
        (original casing) that matched, instead of a bool -- this is the key everything
        handed to bot_listener.py is queued under, so its consumers can look up the
        matching Bot-API chat_id. Those only ever consider chats explicitly named here,
        never is_chat_allowed's "empty list = allow everywhere" fallback."""
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

        # An archive or a 3D model (BLOCKED_FILE_EXTENSIONS): removed from the chat and
        # answered with a one-line explanation addressed to whoever sent it. Checked before
        # everything below and returning immediately, so a blocked file never counts as a
        # figurine and never gets read as a command. Groups
        # only -- the notice tells people to send these in a DM, so deleting them in one
        # would be absurd. Only in chats named in LISTENER_ALLOWED_CHATS: this account sits
        # in other chats that aren't ours to moderate.
        blocked_name = blocked_file_name(msg)
        if blocked_name and not msg.is_private:
            chat = await event.get_chat()
            entry = matched_allowed_chat(chat)
            if entry is not None:
                sender = await event.get_sender()
                # One notice per album, not per file -- see blocked_album_groups. A
                # standalone file (no grouped_id) is always its own case.
                group_key = (event.chat_id, msg.grouped_id) if msg.grouped_id else None
                if group_key is not None and group_key in blocked_album_groups:
                    notice = None
                else:
                    notice = format_blocked_file_notice(sender)
                    if group_key is not None:
                        blocked_album_groups.append(group_key)
                log(
                    f"[listener] blocked file {blocked_name!r} from "
                    f"{sender_display_name(sender)} in '{entry}' -- deleting"
                )
                if bot_takeover:
                    if file_block_queue is not None:
                        await file_block_queue.put((entry, msg.id, notice))
                else:
                    # No bot account configured at all, so there is nobody to hand this to
                    # -- same fallback the figurine reaction takes. Needs this account to
                    # hold delete rights in the chat; if it doesn't, the delete fails and
                    # the notice is skipped rather than left standing over a message that
                    # is still there.
                    try:
                        await event.client.delete_messages(event.chat_id, [msg.id])
                    except Exception as e:
                        log(f"[listener] failed to delete blocked file: {e}")
                    else:
                        try:
                            if notice is not None:
                                sent = await client.send_message(event.chat_id, notice, parse_mode="html")
                                if sent is not None:
                                    sent_message_ids.add(sent.id)
                        except Exception as e:
                            log(f"[listener] failed to send blocked-file notice: {e}")
                return

        # #япокрасил + an attached photo OR video -- a "figurine painted" post (see
        # XP_PER_FIGURINE in stats.py). is_image_message/is_video_message (not just
        # msg.photo/msg.video) also catch media sent as an uncompressed file/document --
        # Telegram's own compressed-vs-document split is just a sender-side choice, and
        # artists posting full-resolution art or a painting timelapse routinely pick
        # "send without compression" (a real missed-post bug found in production for the
        # photo case: some users' #япокрасил images silently never counted because they'd
        # sent them as files -- see is_image_message's docstring). This session sees
        # every message as it arrives, so it's the one place that ever calls
        # record_figurine_live -- a plain local counter bump, not a re-fetch of anything
        # -- so /stat and /top pick it up immediately instead of waiting on the
        # transcript cache's own TTL. Reacting is the one part that has to defer to the
        # bot account once bot_takeover is on, same as every other reply (see
        # figurine_ack_queue).
        if cfg.stats_enabled and (is_image_message(msg) or is_video_message(msg)) and stats.is_figurine_caption(text):
            chat = await event.get_chat()
            entry = matched_allowed_chat(chat)
            if entry is not None:
                sender = await event.get_sender()
                count = stats.record_figurine_live(
                    entry, datetime.now(tz).date(), msg.sender_id,
                    getattr(sender, "username", None), sender_display_name(sender),
                    message_id=msg.id, log=log,
                )
                log(f"[listener] figurine painted by {sender_display_name(sender)} in '{entry}' (today: {count})")
                if bot_takeover:
                    if figurine_ack_queue is not None:
                        await figurine_ack_queue.put((entry, msg.id))
                else:
                    await react_emoji(event.chat_id, msg.id, FIGURINE_ACK_EMOJI)

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

        # "/summary" is the invocation itself, like any slash-command -- no need to also
        # @mention or reply to you. Works the same whether you type it yourself or someone
        # else does, in any allowed chat.
        #
        # It has to OPEN the message. There is deliberately no way to ask by writing the
        # bare word "summary" (naming this account alongside it, or replying to one of its
        # messages with it), because those fired on people merely talking ABOUT the bot.
        # See is_summary_command.
        if bot_takeover or not is_summary_command(text):
            return

        chat = await event.get_chat()
        if not is_chat_allowed(chat):
            return

        try:
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

    async def _my_reaction_emoticons(chat_id, update) -> set[str]:
        """Like _reactor_ids, but returns the emoticon(s) *this account* reacted with on
        update.msg_id, instead of every reactor's id -- lets the thumbs-up dismiss
        shortcut below gate on the specific emoji, not just "you reacted with something"."""

        def _mine_emoticons(reactions):
            found = set()
            for r in reactions:
                try:
                    if tl_utils.get_peer_id(r.peer_id) != me.id:
                        continue
                except Exception:
                    continue
                if isinstance(getattr(r, "reaction", None), ReactionEmoji):
                    found.add(r.reaction.emoticon)
            return found

        found = _mine_emoticons(update.reactions.recent_reactions or [])
        if found:
            return found
        # Same recent_reactions-not-always-populated fallback as _reactor_ids.
        try:
            result = await client(GetMessageReactionsListRequest(peer=chat_id, id=update.msg_id, limit=100))
            return _mine_emoticons(result.reactions)
        except Exception as e:
            log(f"[listener] failed to fetch reactor list for msg {update.msg_id}: {e}")
            return set()

    async def _maybe_dismiss_on_thumbs_up(chat_id, update):
        """Reacting DISMISS_EMOJI onto any message the bot (or this account) sent deletes
        it almost immediately -- a one-tap way to clean up a reply without hunting for a
        message-specific control. Only ever called once save_pending has already passed
        on the reaction (see on_reaction), so it never fights an in-progress confirm flow
        for the same message."""
        if DISMISS_EMOJI not in await _my_reaction_emoticons(chat_id, update):
            return
        try:
            msg = await client.get_messages(chat_id, ids=update.msg_id)
        except Exception as e:
            log(f"[listener] failed to fetch message {update.msg_id} for thumbs-up dismiss: {e}")
            return
        if msg is None:
            return
        if msg.out:
            log(f"[listener] thumbs-up dismiss: deleting own message {update.msg_id} in chat {chat_id}")
            schedule_delete(client, chat_id, [update.msg_id], DISMISS_DELETE_AFTER)
            return
        # .sender is a lazily-cached property -- not guaranteed populated just from
        # get_messages -- so get_sender() (which fetches on a cache miss) is what
        # reliably tells a bot-sent message apart from a fellow human's.
        try:
            sender = await msg.get_sender()
        except Exception as e:
            log(f"[listener] failed to resolve sender of message {update.msg_id} for thumbs-up dismiss: {e}")
            return
        if not (sender and getattr(sender, "bot", False)):
            return  # only ever our own/the bot's messages -- never someone else's
        if dismiss_queue is None:
            log(
                f"[listener] thumbs-up dismiss: message {update.msg_id} in chat {chat_id} was sent by a "
                "bot account, but no dismiss_queue is wired up (no TELEGRAM_BOT_TOKEN?) -- can't delete it "
                "without admin rights this account may not have"
            )
            return
        log(f"[listener] thumbs-up dismiss: queuing bot-sent message {update.msg_id} in chat {chat_id} for deletion")
        await dismiss_queue.put((chat_id, update.msg_id))

    # Reactions from a *user* account (not a bot) arrive as this raw update, carrying the
    # message's full new reaction state (not a per-reaction delta) -- used to confirm
    # the save flow (did *you* react to your own save confirmation).
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

        await _maybe_dismiss_on_thumbs_up(chat_id, update)

    if cfg.stats_enabled:
        # run_stats_rollover itself no-ops (with its own log line) if
        # LISTENER_ALLOWED_CHATS is empty -- stats tracking needs specific chats named
        # rather than defaulting to "everywhere".
        asyncio.create_task(run_stats_rollover(client, cfg, tz, stats_digest_queue=stats_digest_queue, log=log))

    stats_status = "on" if (cfg.stats_enabled and cfg.listener_allowed_chats) else "off"
    log(
        f"[listener] logged in as @{my_username or me.id}. Watching for messages STARTING WITH "
        f"'{SUMMARY_COMMAND}' (summary, pipeline {cfg.summary_pipeline_version}) "
        f"and your own '{cfg.save_trigger_keyword}' replies (save to {cfg.save_channel or 'disabled'}). "
        f"Summary queue: FIFO, {cfg.summary_queue_delay_seconds}s between completed jobs. "
        f"Stats (/top, /stat): {stats_status}. "
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
        # figurine_ack_queue carries (allowed_chats entry, message_id) from this session
        # -- the only one that sees every message, so it's the one that detects a
        # #япокрасил+photo/video post and bumps the counter (stats.record_figurine_live) -- to
        # bot_listener.py, so the *reaction* onto that message still comes from the bot
        # account, same bot-account-only rule as every other reply.
        figurine_ack_queue: asyncio.Queue = asyncio.Queue()
        # stats_digest_queue carries (allowed_chats entry, text) every other day for the
        # "Топ покрастинаторов" call-out (see run_stats_rollover/
        # stats.format_procrastinators) -- ambient, unprompted content, same
        # bot-account-only rule as every other post.
        stats_digest_queue: asyncio.Queue = asyncio.Queue()
        # dismiss_queue carries (chat_id, message_id) from this session's thumbs-up
        # dismiss shortcut (see _maybe_dismiss_on_thumbs_up in run_listener) whenever the
        # message to delete was sent by the bot account -- this account typically has no
        # delete rights over another account's message unless it happens to be a chat
        # admin, but the bot can always delete its own messages via the Bot API, same as
        # every other reply is bot-account-only.
        dismiss_queue: asyncio.Queue = asyncio.Queue()
        # file_block_queue carries (allowed_chats entry, message_id, notice text) for an
        # archive/3D-model attachment this session has just seen (BLOCKED_FILE_EXTENSIONS)
        # -- again, this is the only session that sees every message, but deleting somebody
        # ELSE's message needs the "delete messages" admin right, which the bot account is
        # the one that normally holds, and the notice is a chat post like any other.
        file_block_queue: asyncio.Queue = asyncio.Queue()
        await asyncio.gather(
            run_listener(
                client, cfg, tz,
                figurine_ack_queue=figurine_ack_queue, stats_digest_queue=stats_digest_queue,
                dismiss_queue=dismiss_queue, file_block_queue=file_block_queue,
            ),
            bot_listener.run_bot_listener(
                cfg.telegram_bot_token, cfg, tz, client,
                figurine_ack_queue=figurine_ack_queue, stats_digest_queue=stats_digest_queue,
                dismiss_queue=dismiss_queue, file_block_queue=file_block_queue,
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
