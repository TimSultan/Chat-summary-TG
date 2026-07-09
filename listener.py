"""Live listener: when someone @mentions you (or replies to one of your messages) in a chat
you're in, with a request containing a trigger keyword (default "summary"), it parses what
they're asking for -- the whole chat's topics, or one participant's -- for whatever date/range
they meant, and replies in that chat (as you, via your own Telegram session) with the summary.

Examples it understands (mixed languages are fine):
    "@sultan_kembayev summary что обсуждали сегодня"   -> whole-chat summary, today
    "summary сообщения @some_user за сегодня"          -> @some_user's topics, today

Run with: python listener.py
Stop with Ctrl+C.

`run_listener()` below is also reused by gui.py, which supplies its own already-connected
client and a log callback that writes into the GUI's log pane instead of stdout.
"""

import asyncio
import re
import sys
import time

from telethon import TelegramClient, events

import history
from config import build_session, load_config
from errors import ChatSummaryError
from intent import parse_summary_request, resolve_name_hint
from main import period_label, resolve_tz
from summarizer import summarize_transcript
from telegram_fetch import (
    fetch_range_messages_cached,
    format_transcript_lines,
    sender_display_name,
    sender_matches,
)

MENTION_RE = re.compile(r"@(\w{4,32})")
MAX_REPLY_CHARS = 4000  # stay under Telegram's ~4096 message limit


def extract_mentioned_usernames(text: str, exclude: str | None) -> list[str]:
    names = {m.group(1) for m in MENTION_RE.finditer(text or "")}
    if exclude:
        names = {n for n in names if n.lower() != exclude.lower()}
    return sorted(names)


async def send_long_reply(event, text: str, sent_ids: set[int] | None = None):
    for i in range(0, len(text), MAX_REPLY_CHARS):
        chunk = text[i : i + MAX_REPLY_CHARS]
        if i == 0:
            sent = await event.reply(chunk, parse_mode="md", link_preview=False)
        else:
            sent = await event.respond(chunk, parse_mode="md", link_preview=False)
        # Track our own generated messages so the listener never re-triggers on them --
        # matters once outgoing messages are watched too (see run_listener), since a
        # summary reply can easily contain the trigger keyword itself.
        if sent_ids is not None and sent is not None:
            sent_ids.add(sent.id)


async def handle_request(event, cfg, tz, my_username: str, sent_ids: set[int], log=print):
    msg = event.message
    text = msg.raw_text or ""

    chat = await event.get_chat()
    chat_title_for_history = getattr(chat, "title", None) or "Unknown chat"
    sender = await event.get_sender()
    requester = sender_display_name(sender)

    async def respond(answer: str):
        await send_long_reply(event, answer, sent_ids=sent_ids)
        try:
            history.record(chat_title_for_history, requester, text, answer)
        except Exception as e:
            log(f"[listener] failed to record history: {e}")

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
    label = period_label(start_date, end_date)

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
    )

    await respond(summary)


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

    if not my_username:
        raise ChatSummaryError(
            "Your Telegram account has no @username set, so people can't @mention you. "
            "Set one in Telegram settings, or rely on replies to your messages instead."
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

    last_trigger: dict[int, float] = {}
    sent_message_ids: set[int] = set()

    def is_chat_allowed(chat) -> bool:
        if not allowed_chats:
            return True
        username = (getattr(chat, "username", "") or "").lower()
        title = (getattr(chat, "title", "") or "").lower()
        chat_id = str(getattr(chat, "id", ""))
        return username in allowed_chats or title in allowed_chats or chat_id in allowed_chats

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

        has_keyword = any(k in text_lower for k in cfg.listener_trigger_keywords)
        if not has_keyword:
            return

        if event.out:
            # You typing it yourself always counts, no @mention/reply needed.
            addressed_to_me = True
        else:
            addressed_to_me = bool(msg.mentioned) or f"@{my_username.lower()}" in text_lower
        if not addressed_to_me:
            return

        chat = await event.get_chat()
        if not is_chat_allowed(chat):
            return

        chat_key = event.chat_id
        now = time.monotonic()
        if now - last_trigger.get(chat_key, 0) < cfg.listener_cooldown_seconds:
            log(f"[listener] cooldown active for chat {chat_key}, skipping")
            return
        last_trigger[chat_key] = now

        log(f"[listener] handling request in '{getattr(chat, 'title', chat_key)}': {text!r}")
        try:
            await handle_request(event, cfg, tz, my_username, sent_message_ids, log=log)
        except Exception as e:
            log(f"[listener] error handling request: {e}")
            try:
                sent = await event.reply("Something went wrong generating that summary.")
                if sent is not None:
                    sent_message_ids.add(sent.id)
            except Exception:
                pass

    log(
        f"[listener] logged in as @{my_username}. Watching for mentions containing "
        f"{cfg.listener_trigger_keywords}. Ctrl+C to stop."
    )
    await client.run_until_disconnected()


async def main():
    cfg = load_config()
    tz = resolve_tz(None)
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
