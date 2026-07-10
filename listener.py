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
import math
import re
import sys
import time
from datetime import datetime, timedelta

from telethon import TelegramClient, events, utils as tl_utils
from telethon.tl.functions.messages import GetMessageReactionsListRequest, SendReactionRequest
from telethon.tl.types import ReactionEmoji, UpdateMessageReactions

import history
from config import build_session, load_config
from errors import ChatSummaryError
from intent import parse_summary_request, resolve_name_hint
from main import period_label, resolve_tz
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

# Appended to every successful summary/roast reply so people re-discover the available
# commands without having to ask -- not shown on rejection/error/cooldown notices, which
# already explain themselves and self-delete fast.
COMMANDS_FOOTER = "Список команд - прожарь меня, summary + время или юзер"

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

SUMMARY_DELETE_AFTER = 180  # successful replies self-delete after 3 minutes
ERROR_DELETE_AFTER = 10  # rejection notices (day limit, cooldown) self-delete fast
ROAST_DELETE_AFTER = 600  # roast replies self-delete after 10 minutes

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


def _ru_minutes(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} минуту"
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return f"{n} минуты"
    return f"{n} минут"


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


async def repost_saved_message(client, channel, replied_msg, added_text: str) -> None:
    """Reposts `replied_msg` (whatever it contains -- photo, video, any other media, or
    just text) to `channel` as a fresh message, not a forward (no "Forwarded from" tag).
    `added_text` (the text typed after the save trigger word, may be empty) is appended
    below whatever text/caption the original message already had."""
    original_text = replied_msg.raw_text or ""
    caption = "\n\n".join(p for p in (original_text, added_text) if p) or None

    if replied_msg.media:
        # Passing the original media object straight through re-uses Telegram's existing
        # file server-side (no download/re-upload through us), same as a forward would,
        # but as a brand-new message so it doesn't carry a "Forwarded from" tag.
        await client.send_file(channel, file=replied_msg.media, caption=caption)
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
        intent = parse_summary_request(
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

    # "last N hours" is a rolling window anchored to the exact moment the request was
    # sent, not a calendar day -- e.g. asked at 1am for "the last 10 hours" needs
    # messages back to 3pm *yesterday*, which a same-day-only range would miss
    # entirely. Computed here (not by the LLM) so it's exact, and allowed to span two
    # calendar days without tripping the single-day limit below, since it's still
    # bounded to at most MAX_LOOKBACK_HOURS regardless of where midnight falls.
    lookback_hours = intent.get("lookback_hours")
    window_start_dt = window_end_dt = None
    if lookback_hours:
        if lookback_hours > MAX_LOOKBACK_HOURS:
            log(f"[listener] clamping requested lookback of {lookback_hours}h to {MAX_LOOKBACK_HOURS}h")
            lookback_hours = MAX_LOOKBACK_HOURS
        window_end_dt = msg.date.astimezone(tz)
        window_start_dt = window_end_dt - timedelta(hours=lookback_hours)
        start_date, end_date = window_start_dt.date(), window_end_dt.date()
        log(f"[listener] lookback window: {window_start_dt} to {window_end_dt}")
    elif start_date != end_date:
        log(f"[listener] rejected multi-day request ({start_date}..{end_date})")
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
            focus_user = resolve_name_hint(cfg.openai_api_key, cfg.openai_model, name_hint, candidates)
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

    summary = summarize_transcript(
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

    await respond(f"{summary}\n\n{COMMANDS_FOOTER}", delete_after=SUMMARY_DELETE_AFTER)


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
    roast = roast_person(
        api_key=cfg.openai_api_key,
        model=cfg.openai_model,
        target_name=requester,
        lines=lines,
    )

    await respond(f"{roast}\n\n{COMMANDS_FOOTER}", delete_after=ROAST_DELETE_AFTER)


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


async def run_listener(client: TelegramClient, cfg, tz, log=print):
    """Registers the mention-trigger handler on an already-connected & authorized
    `client` and blocks until it disconnects (call `client.disconnect()` to stop it)."""
    assert cfg.listener_cooldown_seconds >= 0, "internal bug: cooldown should have been validated by config"

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

    last_trigger: dict[int, float] = {}
    sent_message_ids: set[int] = set()
    background_tasks: set[asyncio.Task] = set()

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

    def is_chat_allowed(chat) -> bool:
        if not allowed_chats:
            return True
        username = (getattr(chat, "username", "") or "").lower()
        title = (getattr(chat, "title", "") or "").lower()
        chat_id = str(getattr(chat, "id", ""))
        return username in allowed_chats or title in allowed_chats or chat_id in allowed_chats

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

        # "сохрани" (config.py SAVE_TRIGGER_KEYWORD), sent by you as a reply, asks for
        # confirmation before reposting whatever you replied to into your save channel
        # -- see save_pending handling in on_reaction below. Only ever fires for your
        # own messages (msg.out), and doesn't touch LISTENER_ALLOWED_CHATS/cooldown
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
                    sent = await event.reply(f"Couldn't save that: {e}")
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

        # The trigger keyword (default "/summary") is the invocation itself, like a
        # slash-command -- no need to also @mention or reply to you. Works the same
        # whether you type it yourself or someone else does, in any allowed chat.
        # "прожарь меня" is a second, separate trigger for the roast command.
        has_summary_keyword = any(k in text_lower for k in cfg.listener_trigger_keywords)
        has_roast_keyword = any(k in text_lower for k in cfg.roast_trigger_keywords)

        # Two more ways to ask for a summary without the exact trigger keyword: naming
        # you by first name alongside the word "summary" in one message, or replying to
        # one of your own messages and saying "summary". Both checks are gated on the
        # bare word "summary" being present at all, so plain chat never pays for the
        # extra (async, for the reply case) checks below.
        if not has_summary_keyword and not has_roast_keyword and "summary" in text_lower:
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

        chat_key = event.chat_id
        now = time.monotonic()
        elapsed = now - last_trigger.get(chat_key, 0)
        if elapsed < cfg.listener_cooldown_seconds:
            remaining_minutes = max(1, math.ceil((cfg.listener_cooldown_seconds - elapsed) / 60))
            log(f"[listener] cooldown active for chat {chat_key}, {remaining_minutes} min remaining")
            try:
                sent = await event.reply(f"Спросите через {_ru_minutes(remaining_minutes)}")
                if sent is not None:
                    sent_message_ids.add(sent.id)
                    schedule_delete(event.client, chat_key, [sent.id], ERROR_DELETE_AFTER)
            except Exception:
                pass
            return
        last_trigger[chat_key] = now

        log(f"[listener] handling request in '{getattr(chat, 'title', chat_key)}': {text!r}")
        try:
            if has_roast_keyword:
                confirm = await event.reply(ROAST_CONFIRM_TEXT)
                if confirm is not None:
                    sent_message_ids.add(confirm.id)
                    roast_pending[(event.chat_id, sender.id)] = (confirm.id, text)
                    log(f"[listener] sent roast confirmation to {sender_display_name(sender)} (msg {confirm.id})")
            else:
                await react_emoji(event.chat_id, msg.id, SUMMARY_ACK_EMOJI)
                await handle_request(event, cfg, tz, my_username, sent_message_ids, schedule_delete, log=log)
        except Exception as e:
            log(f"[listener] error handling request: {e}")
            try:
                sent = await event.reply("Something went wrong generating that summary.")
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
            except Exception as e:
                log(f"[listener] error generating confirmed roast: {e}")
                try:
                    sent = await client.send_message(
                        chat_id, "Something went wrong generating that roast.", reply_to=confirm_msg_id
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

    log(
        f"[listener] logged in as @{my_username or me.id}. Watching for messages containing "
        f"{cfg.listener_trigger_keywords} (summary) or {cfg.roast_trigger_keywords} (roast), "
        f"and your own '{cfg.save_trigger_keyword}' replies (save to {cfg.save_channel or 'disabled'}). "
        "Ctrl+C to stop."
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
