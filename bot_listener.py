"""Long-polls the Telegram Bot HTTP API for /summary and roast ("прожарь меня") requests
in the same chats listener.py's Telethon-based listener watches, and answers them as the
bot account instead of your personal account.

Why this exists alongside listener.py: a bot account lets people trigger these without it
coming from (or being confused with) your own account. The tradeoff is that the Bot API
gives a bot no retroactive access to chat history at all -- it only ever sees messages
sent after it's added to a chat. So message fetching here still goes through the already-
connected Telethon `client` passed into run_bot_listener() (same
fetch_range_messages_cached() listener.py itself uses); only trigger detection and
replying happen over the bot's HTTP API.

Roast confirmation uses an inline-keyboard button + callback_query instead of the "react
to confirm" flow listener.py uses for the same command: receiving *other users'*
reactions via getUpdates (message_reaction updates) requires the bot to be a chat admin,
while callback_query from the bot's own inline keyboard needs no special rights.

Save ("сохрани") is NOT handled here -- it only makes sense as *your own* account
reposting to your own channel. See listener.py's on_message: it stops handling /summary
and roast itself once TELEGRAM_BOT_TOKEN is set, so only one of the two ever replies to a
given request.

A private chat (DM) with the bot is always accepted as a trigger source too, regardless
of LISTENER_ALLOWED_CHATS -- but since a DM has no group history of its own, data
fetching for a DM-originated request is redirected to a single "home" chat instead (see
_home_chat_ref): whichever chat LISTENER_ALLOWED_CHATS names, IF it names exactly one.
With zero or multiple entries there's no unambiguous default, and a DM request gets told
to ask in the group instead of guessing which one you meant.

Always uses the v2 pipeline (intent_v2 + responder_v2) regardless of
SUMMARY_PIPELINE_VERSION, which only governs the older Telethon-listener code path kept
for rollback/comparison -- see intent_v2.py's module docstring.

Run with: python bot_listener.py (standalone, using load_config()'s own Telethon
session) -- or, more commonly, let listener.py's main() start this automatically
alongside its own Telethon listener when TELEGRAM_BOT_TOKEN is set.
"""

import asyncio
import math
import sys
import time
from datetime import datetime, timedelta, timezone

import aiohttp

import history
from bot_api import TelegramBotAPI
from config import build_session, load_config
from errors import ChatSummaryError
from intent import resolve_name_hint
from intent_v2 import route_request
from listener import (
    COMMANDS_FOOTER,
    DAY_LIMIT_MESSAGE,
    ERROR_DELETE_AFTER,
    NO_ROAST_MATERIAL_MESSAGE,
    ROAST_BUSY_EMOJI,
    ROAST_DELETE_AFTER,
    SUMMARY_ACK_EMOJI,
    SUMMARY_DELETE_AFTER,
    DayLimitExceeded,
    _format_hours,
    _ru_minutes,
    extract_mentioned_usernames,
    resolve_time_window,
)
from main import period_label, resolve_tz
from responder_v2 import answer_request
from roast import roast_person
from telegram_fetch import fetch_range_messages_cached, format_transcript_lines, sender_matches

MAX_REPLY_CHARS = 4000
POLL_TIMEOUT_SECONDS = 30

BOT_ROAST_CONFIRM_TEXT = "Ты точно хочешь прожарку? Нажми кнопку, чтобы подтвердить."
ROAST_BUTTON_TEXT = "🔥 Жги"
ROAST_CALLBACK_PREFIX = "roast"


def _display_name(user: dict | None) -> str:
    if not user:
        return "Unknown"
    parts = [user.get("first_name"), user.get("last_name")]
    name = " ".join(p for p in parts if p)
    if name:
        return name
    if user.get("username"):
        return f"@{user['username']}"
    return f"id{user.get('id')}"


def _is_chat_allowed(allowed_chats: set[str], chat: dict) -> bool:
    # A private chat (DM) with the bot itself is always a legitimate input channel,
    # regardless of the group allowlist -- see _home_chat_ref: it's how you ask about the
    # group without posting in it.
    if chat.get("type") == "private":
        return True
    if not allowed_chats:
        return True
    username = (chat.get("username") or "").lower()
    title = (chat.get("title") or "").lower()
    chat_id = str(chat.get("id", ""))
    return username in allowed_chats or title in allowed_chats or chat_id in allowed_chats


def _home_chat_ref(cfg) -> str | None:
    """The one group chat a DM with the bot should be treated as being about, since a DM
    has no group history of its own to fetch. Only well-defined when LISTENER_ALLOWED_CHATS
    names exactly one chat -- with zero or multiple entries there's no unambiguous default."""
    if len(cfg.listener_allowed_chats) == 1:
        return cfg.listener_allowed_chats[0]
    return None


async def send_long_bot_message(api: TelegramBotAPI, chat_id, text: str, reply_to_message_id: int | None) -> list[int]:
    sent_ids = []
    for i in range(0, len(text), MAX_REPLY_CHARS):
        chunk = text[i : i + MAX_REPLY_CHARS]
        sent = await api.send_message(chat_id, chunk, reply_to_message_id=reply_to_message_id if i == 0 else None)
        if sent and "message_id" in sent:
            sent_ids.append(sent["message_id"])
    return sent_ids


def schedule_bot_delete(api: TelegramBotAPI, chat_id, message_ids: list[int], delay_seconds: int, log, background_tasks: set):
    async def _do():
        await asyncio.sleep(delay_seconds)
        for mid in message_ids:
            await api.delete_message(chat_id, mid)

    task = asyncio.create_task(_do())
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


def _roast_callback_data(chat_id, user_id) -> str:
    return f"{ROAST_CALLBACK_PREFIX}:{chat_id}:{user_id}"


def _parse_roast_callback(data: str) -> tuple[int, int] | None:
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != ROAST_CALLBACK_PREFIX:
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


async def run_bot_roast(
    api: TelegramBotAPI,
    telethon_client,
    cfg,
    tz,
    chat_id,
    chat_ref: str,
    target_user: dict,
    confirm_msg_id: int,
    original_text: str,
    background_tasks: set,
    log=print,
):
    """Actually generates and sends the roast, once the target user has confirmed by
    tapping the inline button on BOT_ROAST_CONFIRM_TEXT. Mirrors listener.py's run_roast,
    but message fetching goes through `chat_ref` (a username/title string, NOT `chat_id`
    -- the Bot API's chat id numbering differs from Telethon's, e.g. supergroups use a
    "-100" prefix over the Bot API, so only a resolvable name/username is safe to hand to
    the Telethon session)."""
    target_user_id = target_user.get("id")
    requester = _display_name(target_user)
    chat_title_for_history = chat_ref

    async def respond(answer: str, delete_after: int | None = None, record: bool = True):
        sent_ids = await send_long_bot_message(api, chat_id, answer, reply_to_message_id=confirm_msg_id)
        if record:
            try:
                history.record(chat_title_for_history, requester, original_text, answer)
            except Exception as e:
                log(f"[bot_listener] failed to record history: {e}")
        if delete_after and sent_ids:
            schedule_bot_delete(api, chat_id, sent_ids, delete_after, log, background_tasks)

    end_date = datetime.now(tz).date()
    start_date = end_date - timedelta(days=cfg.roast_lookback_days - 1)

    chat_title, messages = await fetch_range_messages_cached(
        client=telethon_client, chat_ref=chat_ref, start_day=start_date, end_day=end_date, tz=tz, log=log,
    )
    if chat_title:
        chat_title_for_history = chat_title

    own_messages = [m for m in messages if m.sender_id == target_user_id]
    if not own_messages:
        username = target_user.get("username")
        if username:
            own_messages = [m for m in messages if sender_matches(m, username)]

    log(f"[bot_listener] roast target={requester} matched={len(own_messages)}/{len(messages)}")
    if not own_messages:
        await respond(NO_ROAST_MATERIAL_MESSAGE, delete_after=ERROR_DELETE_AFTER, record=False)
        return

    if len(own_messages) > cfg.roast_max_messages:
        log(
            f"[bot_listener] capping roast input for {requester}: {len(own_messages)} -> "
            f"{cfg.roast_max_messages} most recent messages"
        )
        own_messages = own_messages[-cfg.roast_max_messages :]

    lines = format_transcript_lines(own_messages, include_date=True)
    roast = roast_person(api_key=cfg.openai_api_key, model=cfg.openai_model, target_name=requester, lines=lines)

    await respond(f"{roast}\n\n{COMMANDS_FOOTER}", delete_after=ROAST_DELETE_AFTER)


async def handle_bot_roast_callback(
    api: TelegramBotAPI,
    telethon_client,
    cfg,
    tz,
    callback: dict,
    roast_pending: dict,
    roast_in_progress: set,
    background_tasks: set,
    log=print,
):
    parsed = _parse_roast_callback(callback.get("data"))
    if parsed is None:
        await api.answer_callback_query(callback["id"])
        return
    chat_id, target_user_id = parsed

    clicker = callback.get("from") or {}
    if clicker.get("id") != target_user_id:
        await api.answer_callback_query(callback["id"], text="Эта кнопка не для тебя.")
        return

    key = (chat_id, target_user_id)
    pending = roast_pending.pop(key, None)
    if pending is None:
        await api.answer_callback_query(callback["id"])
        return  # already confirmed or this callback is stale -- ignore a stray second tap

    await api.answer_callback_query(callback["id"], text="Жарим...")
    roast_in_progress.add(key)
    log(f"[bot_listener] roast confirmed via button: chat={chat_id} user={target_user_id}")

    async def _run():
        try:
            await run_bot_roast(
                api, telethon_client, cfg, tz, chat_id, pending["chat_ref"], clicker,
                pending["confirm_msg_id"], pending["original_text"], background_tasks, log=log,
            )
        except Exception as e:
            log(f"[bot_listener] error generating confirmed roast: {e}")
            try:
                await api.send_message(
                    chat_id, "Что-то пошло не так при генерации прожарки.",
                    reply_to_message_id=pending["confirm_msg_id"],
                )
            except Exception:
                pass
        finally:
            roast_in_progress.discard(key)

    task = asyncio.create_task(_run())
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


async def handle_bot_summary_request(
    api: TelegramBotAPI,
    telethon_client,
    cfg,
    tz,
    bot_username: str,
    message: dict,
    background_tasks: set,
    home_chat_ref: str | None,
    log=print,
):
    chat = message["chat"]
    chat_id = chat["id"]
    message_id = message["message_id"]
    text = message.get("text") or ""
    requester = _display_name(message.get("from"))
    chat_title_for_history = chat.get("title") or chat.get("first_name") or "Unknown chat"
    request_dt = datetime.fromtimestamp(message["date"], tz=timezone.utc)

    async def respond(answer: str, delete_after: int | None = None, record: bool = True):
        sent_ids = await send_long_bot_message(api, chat_id, answer, reply_to_message_id=message_id)
        if record:
            try:
                history.record(chat_title_for_history, requester, text, answer)
            except Exception as e:
                log(f"[bot_listener] failed to record history: {e}")
        if delete_after and sent_ids:
            schedule_bot_delete(api, chat_id, sent_ids, delete_after, log, background_tasks)

    # A DM has no group history of its own -- redirect data fetching to the configured
    # home group, but keep replying/recording history against the DM itself (chat_id,
    # requester above are untouched). See _home_chat_ref.
    if chat.get("type") == "private":
        if not home_chat_ref:
            await respond("Не настроен основной чат для личных сообщений -- обратитесь в группе.")
            return
        data_chat_ref = home_chat_ref
    else:
        data_chat_ref = chat.get("username") or chat_title_for_history

    mentioned = extract_mentioned_usernames(text, exclude=bot_username)
    ref_date = request_dt.astimezone(tz).date()

    try:
        routed = route_request(
            api_key=cfg.openai_api_key,
            model=cfg.openai_model,
            text=text,
            reference_date=ref_date,
            mentioned_usernames=mentioned,
            my_username=bot_username,
        )
    except Exception as e:
        log(f"[bot_listener] intent_v2 routing failed: {e}")
        await respond("Не удалось разобрать запрос.")
        return

    try:
        start_date, end_date, window_start_dt, window_end_dt, lookback_hours = resolve_time_window(
            routed["start_date"], routed["end_date"], routed["lookback_hours"], request_dt, tz, log
        )
    except DayLimitExceeded:
        await respond(DAY_LIMIT_MESSAGE, delete_after=ERROR_DELETE_AFTER, record=False)
        return

    chat_title, messages = await fetch_range_messages_cached(
        client=telethon_client,
        chat_ref=data_chat_ref,
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
        from_explicit_mention = any(username_hint.lower() == m.lower() for m in mentioned)
        if from_explicit_mention:
            focus_user = username_hint
            matched = sum(1 for m in messages if sender_matches(m, focus_user))
            log(f"[bot_listener] focus_user(explicit)={focus_user} matched={matched}/{len(messages)}")
            if matched == 0:
                await respond(f"Сообщений от @{focus_user} за этот период не найдено.")
                return
        else:
            candidates = sorted({c for m in messages for c in (m.sender_username, m.sender_name) if c})
            log(f"[bot_listener] resolving name hint '{username_hint}' against {len(candidates)} candidates")
            try:
                focus_user = resolve_name_hint(cfg.openai_api_key, cfg.openai_model, username_hint, candidates)
            except ChatSummaryError as e:
                log(f"[bot_listener] name resolution failed: {e}")
                focus_user = None
            if focus_user:
                log(f"[bot_listener] resolved name hint '{username_hint}' -> '{focus_user}'")
            else:
                log(f"[bot_listener] could not resolve name hint '{username_hint}' among participants")
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

    answer = answer_request(
        api_key=cfg.openai_api_key,
        model=cfg.openai_model,
        chat_title=chat_title,
        period_label=label,
        lines=lines,
        question=routed["cleaned_question"],
        focus_user=focus_user,
        style="reply",
    )

    await respond(f"{answer}\n\n{COMMANDS_FOOTER}", delete_after=SUMMARY_DELETE_AFTER)


async def _dispatch_update(
    update: dict,
    api: TelegramBotAPI,
    telethon_client,
    cfg,
    tz,
    bot_username: str | None,
    allowed_chats: set[str],
    last_trigger: dict[int, float],
    roast_pending: dict,
    roast_in_progress: set,
    background_tasks: set,
    home_chat_ref: str | None,
    log=print,
) -> None:
    """Handles one update. Must never let an exception escape to the caller: an unhandled
    error here would crash the whole polling loop (see run_bot_listener), and since
    `offset` only lives in memory, a crash-restart would make Telegram redeliver this same
    still-unconfirmed update to the fresh process -- risking a crash/resend loop that
    looks like the bot spamming the same reply over and over. Every reply sent directly
    in this function is therefore individually try/except-guarded too (matching
    listener.py's pattern), so one failed send (rate limit, transient network error)
    can't take the rest of the process down with it -- run_bot_listener's own try/except
    around this call is strictly a last-resort backstop, not the primary safety net."""
    callback = update.get("callback_query")
    if callback is not None:
        await handle_bot_roast_callback(
            api, telethon_client, cfg, tz, callback, roast_pending, roast_in_progress, background_tasks, log=log,
        )
        return

    message = update.get("message")
    if not message or "text" not in message:
        return

    text_lower = message["text"].lower()
    has_summary = any(k in text_lower for k in cfg.listener_trigger_keywords)
    has_roast = any(k in text_lower for k in cfg.roast_trigger_keywords)
    # Convenience trigger: naming the bot directly (@its_username) alongside the bare
    # word "summary" also counts, so "/summary" isn't the only way to ask -- e.g.
    # "@echhchat_bot summary как дела". Mirrors listener.py's "first name + summary"
    # heuristic for the personal account.
    if not has_summary and not has_roast and "summary" in text_lower:
        if bot_username and f"@{bot_username.lower()}" in text_lower:
            has_summary = True
    if not has_summary and not has_roast:
        return

    chat = message["chat"]
    if not _is_chat_allowed(allowed_chats, chat):
        return

    chat_key = chat["id"]
    sender = message.get("from") or {}
    sender_id = sender.get("id")

    if has_roast:
        roast_key = (chat_key, sender_id)
        if roast_key in roast_pending or roast_key in roast_in_progress:
            log(f"[bot_listener] roast already pending/in-progress for {roast_key}, reacting instead")
            await api.set_message_reaction(chat_key, message["message_id"], ROAST_BUSY_EMOJI)
            return

    now = time.monotonic()
    # 0 is an unsafe "never triggered" sentinel against time.monotonic() -- use -inf so a
    # fresh process never spuriously treats its first request in a chat as in cooldown.
    elapsed = now - last_trigger.get(chat_key, float("-inf"))
    if elapsed < cfg.listener_cooldown_seconds:
        remaining_minutes = max(1, math.ceil((cfg.listener_cooldown_seconds - elapsed) / 60))
        log(f"[bot_listener] cooldown active for chat {chat_key}, {remaining_minutes} min remaining")
        try:
            sent = await api.send_message(
                chat_key, f"Спросите через {_ru_minutes(remaining_minutes)}",
                reply_to_message_id=message["message_id"],
            )
            if sent and "message_id" in sent:
                schedule_bot_delete(api, chat_key, [sent["message_id"]], ERROR_DELETE_AFTER, log, background_tasks)
        except Exception as e:
            log(f"[bot_listener] failed to send cooldown notice: {e}")
        return
    last_trigger[chat_key] = now

    is_private = chat.get("type") == "private"
    if is_private and not home_chat_ref:
        # A DM has no group history of its own -- without exactly one chat configured in
        # LISTENER_ALLOWED_CHATS there's no unambiguous default to pull from (see
        # _home_chat_ref), so say so instead of guessing or silently failing later.
        try:
            await api.send_message(
                chat_key, "Не настроен основной чат для личных сообщений -- обратитесь в группе.",
                reply_to_message_id=message["message_id"],
            )
        except Exception as e:
            log(f"[bot_listener] failed to send home-chat-not-configured notice: {e}")
        return

    if has_roast:
        log(f"[bot_listener] sending roast confirmation in '{chat.get('title', chat_key)}' to {_display_name(sender)}")
        try:
            sent = await api.send_message(
                chat_key, BOT_ROAST_CONFIRM_TEXT, reply_to_message_id=message["message_id"],
                reply_markup={
                    "inline_keyboard": [[
                        {"text": ROAST_BUTTON_TEXT, "callback_data": _roast_callback_data(chat_key, sender_id)}
                    ]]
                },
            )
            if sent and "message_id" in sent:
                roast_pending[(chat_key, sender_id)] = {
                    "confirm_msg_id": sent["message_id"],
                    "original_text": message["text"],
                    "chat_ref": home_chat_ref if is_private else (chat.get("username") or chat.get("title") or str(chat_key)),
                }
        except Exception as e:
            log(f"[bot_listener] failed to send roast confirmation: {e}")
        return

    log(f"[bot_listener] handling request in '{chat.get('title', chat_key)}': {message['text']!r}")
    await api.set_message_reaction(chat_key, message["message_id"], SUMMARY_ACK_EMOJI)

    async def _run(message=message):
        try:
            await handle_bot_summary_request(
                api, telethon_client, cfg, tz, bot_username, message, background_tasks, home_chat_ref, log=log
            )
        except Exception as e:
            log(f"[bot_listener] error handling request: {e}")
            try:
                await api.send_message(
                    message["chat"]["id"], "Что-то пошло не так при генерации сводки.",
                    reply_to_message_id=message["message_id"],
                )
            except Exception:
                pass

    task = asyncio.create_task(_run())
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


async def run_bot_listener(bot_token: str, cfg, tz, telethon_client, log=print):
    """Runs until cancelled. Meant to be started as a sibling asyncio task alongside
    listener.py's Telethon client -- both share the same connected `telethon_client` for
    message fetching."""
    allowed_chats = set(c.lower().lstrip("@") for c in cfg.listener_allowed_chats)
    background_tasks: set[asyncio.Task] = set()
    last_trigger: dict[int, float] = {}

    # Roast confirm/button flow state, keyed by (chat_id, target_user_id) -- mirrors
    # listener.py's roast_pending/roast_in_progress. Value: {"confirm_msg_id",
    # "original_text", "chat_ref"} -- chat_ref is a username/title string usable by the
    # Telethon session, since chat_id here is the Bot API's own numbering.
    roast_pending: dict[tuple[int, int], dict] = {}
    roast_in_progress: set[tuple[int, int]] = set()

    home_chat_ref = _home_chat_ref(cfg)
    if home_chat_ref:
        log(f"[bot_listener] home chat for DM requests: '{home_chat_ref}'")
    else:
        log(
            "[bot_listener] no single home chat configured (LISTENER_ALLOWED_CHATS doesn't "
            "name exactly one chat) -- DM requests to this bot will be told to ask in the group instead."
        )

    async with aiohttp.ClientSession() as session:
        api = TelegramBotAPI(bot_token, session)
        me = await api.get_me()
        bot_username = me.get("username")
        log(
            f"[bot_listener] logged in as @{bot_username or me.get('id')}. Long-polling for "
            f"{cfg.listener_trigger_keywords} (summary) and {cfg.roast_trigger_keywords} (roast)."
        )

        offset = None
        while True:
            try:
                updates = await api.get_updates(offset=offset, timeout=POLL_TIMEOUT_SECONDS)
            except ChatSummaryError as e:
                log(f"[bot_listener] getUpdates failed, retrying in 5s: {e}")
                await asyncio.sleep(5)
                continue

            for update in updates:
                # Offset must advance before processing, not after: if handling this
                # update throws, Telegram should still consider it delivered on the next
                # getUpdates call rather than resending the same update forever.
                offset = update["update_id"] + 1
                try:
                    await _dispatch_update(
                        update, api, telethon_client, cfg, tz, bot_username, allowed_chats,
                        last_trigger, roast_pending, roast_in_progress, background_tasks, home_chat_ref, log=log,
                    )
                except Exception as e:
                    log(f"[bot_listener] unhandled error processing update {update.get('update_id')}: {e}")


async def main():
    cfg = load_config()
    if not cfg.telegram_bot_token:
        raise ChatSummaryError("TELEGRAM_BOT_TOKEN is not set -- see .env.example.")
    tz = resolve_tz(None)

    from telethon import TelegramClient

    client = TelegramClient(build_session(cfg), cfg.api_id, cfg.api_hash)
    await client.start()
    try:
        await run_bot_listener(cfg.telegram_bot_token, cfg, tz, client, log=print)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ChatSummaryError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        sys.exit(130)
