"""Long-polls the Telegram Bot HTTP API for /summary requests in the same chats
listener.py's Telethon-based listener watches, and answers them as the bot account
instead of your personal account.

Why this exists alongside listener.py: a bot account lets people trigger this without it
coming from (or being confused with) your own account. The tradeoff is that the Bot API
gives a bot no retroactive access to chat history at all -- it only ever sees messages
sent after it's added to a chat. So message fetching here still goes through the already-
connected Telethon `client` passed into run_bot_listener() (same
fetch_range_messages_cached() listener.py itself uses); only trigger detection and
replying happen over the bot's HTTP API.

Roast ("прожарь меня") is turned off -- see `has_roast` in _dispatch_update, forced False
rather than deleted, along with the rest of the roast_pending/callback_query confirmation
machinery below it (an inline-keyboard button + callback_query, instead of listener.py's
"react to confirm" flow, since receiving *other users'* reactions via getUpdates requires
the bot to be a chat admin while callback_query from its own inline keyboard doesn't).
Left in place rather than removed so re-enabling it later is a one-line change.

Save ("сохрани") is NOT handled here -- it only makes sense as *your own* account
reposting to your own channel. See listener.py's on_message: it stops handling /summary
itself once TELEGRAM_BOT_TOKEN is set, so only one of the two ever replies to a given
request.

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
import html
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

import aiohttp
from telethon import utils as tl_utils

import chat_profile
import history
import stats
from bot_api import TelegramBotAPI
from config import build_session, load_config
from errors import ChatSummaryError
from intent import resolve_name_hint
from intent_v2 import route_request
from joke import CONTEXT_MESSAGE_COUNT, generate_joke
from listener import (
    COMMANDS_FOOTER,
    DAY_LIMIT_MESSAGE,
    ERROR_DELETE_AFTER,
    FIGURINE_ACK_EMOJI,
    NO_ROAST_MATERIAL_MESSAGE,
    PROCRASTINATOR_NONE_FOUND_MESSAGE,
    ROAST_BUSY_EMOJI,
    ROAST_DELETE_AFTER,
    STATS_DELETE_AFTER,
    SUMMARY_ACK_EMOJI,
    DayLimitExceeded,
    _expand_sparse_impression_history,
    _format_hours,
    extract_mentioned_usernames,
    resolve_time_window,
)
from main import period_label, resolve_tz
from responder_v2 import answer_request
from roast import roast_person
from telegram_fetch import (
    fetch_range_messages_cached,
    fetch_recent_messages_fresh,
    format_transcript_lines,
    resolve_chat,
    sender_matches,
)

MAX_REPLY_CHARS = 4000
POLL_TIMEOUT_SECONDS = 30

BOT_ROAST_CONFIRM_TEXT = "Ты точно хочешь прожарку? Нажми кнопку, чтобы подтвердить."
ROAST_BUTTON_TEXT = "🔥 Жги"
ROAST_CALLBACK_PREFIX = "roast"

# "пошути"/"пошути превью" (config.py JOKE_MANUAL_TRIGGER_KEYWORD/JOKE_MANUAL_PREVIEW_KEYWORD),
# sent as a DM to the bot, manually fires a joke (see joke.py) into the configured home
# chat -- unlike the automatic buffer-triggered one in listener.py, this bypasses the
# activity/cooldown/probability gates entirely (it's an explicit ask), but still goes
# through the same model-level decline check and, once actually posted, feeds the same
# cooldown/reaction-tracking machinery via joke_posted_queue so it doesn't stack
# independently of the automatic path. "пошути" posts straight to the chat; "пошути
# превью" sends it back to the DM first with a confirm button instead.
JOKE_PREVIEW_BUTTON_TEXT = "✅ Отправить в чат"
JOKE_PREVIEW_CALLBACK_PREFIX = "jokeprev"


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


def _match_allowed_chat(chat: dict, allowed_chats_original: list[str]) -> str | None:
    """Like _is_chat_allowed, but returns the actual LISTENER_ALLOWED_CHATS entry
    (original casing) that matched a group chat, instead of a bool -- used to key
    known_chat_ids (see run_bot_listener) so a joke queued by listener.py under that same
    entry string can be resolved back to this Bot-API chat_id. Deliberately does NOT
    special-case private chats the way _is_chat_allowed does: a DM isn't a postable
    target for a joke."""
    username = (chat.get("username") or "").lower()
    title = (chat.get("title") or "").lower()
    chat_id = str(chat.get("id", ""))
    for entry in allowed_chats_original:
        e = entry.lower().lstrip("@")
        if e in (username, title, chat_id):
            return entry
    return None


async def _resolve_chat_id(telethon_client, entry: str, known_chat_ids: dict[str, int], log=print) -> int | None:
    """Bot-API chat_id for `entry` (a LISTENER_ALLOWED_CHATS string). known_chat_ids
    (learned passively as _dispatch_update observes live updates from that chat -- see
    the comment there) is checked first since it's free; on a miss, this actively resolves
    `entry` via the Telethon session instead of waiting for a future update to teach it.

    This works because Telethon's default "marked" peer ids (telethon.utils.get_peer_id,
    what event.chat_id etc. already use throughout this project) use exactly the same
    numbering the Bot API uses for chat_id -- -100<channel_id> for supergroups/channels,
    -chat_id for basic groups -- a stable, documented Telegram-wide convention, not
    something specific to this bot. Without this fallback, a chat the bot hasn't
    happened to see a live message from yet (e.g. right after a restart, or one whose
    only traffic is manual "пошути" DMs) would be permanently unreachable by chat_id."""
    chat_id = known_chat_ids.get(entry)
    if chat_id is not None:
        return chat_id
    try:
        entity = await resolve_chat(telethon_client, entry)
        chat_id = tl_utils.get_peer_id(entity)
    except Exception:
        log(f"[bot_listener] failed to resolve chat_id for '{entry}':\n{traceback.format_exc()}")
        return None
    known_chat_ids[entry] = chat_id
    return chat_id


def _home_chat_ref(cfg) -> str | None:
    """The one group chat a DM with the bot should be treated as being about, since a DM
    has no group history of its own to fetch. Only well-defined when LISTENER_ALLOWED_CHATS
    names exactly one chat -- with zero or multiple entries there's no unambiguous default."""
    if len(cfg.listener_allowed_chats) == 1:
        return cfg.listener_allowed_chats[0]
    return None


def _telegram_html(text: str) -> str:
    """Escapes arbitrary model output for Telegram HTML, then restores the one bit of
    formatting the summary prompt deliberately asks for: **bold topic headings**.

    Escaping first means usernames containing underscores and literal <, >, or & can
    never become malformed Telegram entities. Any unmatched ** remains harmless text.
    """
    escaped = html.escape(text, quote=False)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped, flags=re.DOTALL)


async def send_long_bot_message(api: TelegramBotAPI, chat_id, text: str, reply_to_message_id: int | None) -> list[int]:
    sent_ids = []
    for i in range(0, len(text), MAX_REPLY_CHARS):
        chunk = text[i : i + MAX_REPLY_CHARS]
        sent = await api.send_message(
            chat_id,
            _telegram_html(chunk),
            reply_to_message_id=reply_to_message_id if i == 0 else None,
            parse_mode="HTML",
        )
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
    roast = await asyncio.to_thread(
        roast_person, api_key=cfg.openai_api_key, model=cfg.openai_model, target_name=requester, lines=lines
    )

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
        except Exception:
            log(f"[bot_listener] error generating confirmed roast:\n{traceback.format_exc()}")
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
    bot_response_queue,
    log=print,
):
    chat = message["chat"]
    chat_id = chat["id"]
    message_id = message["message_id"]
    text = message.get("text") or ""
    sender = message.get("from") or {}
    requester = _display_name(sender)
    chat_title_for_history = chat.get("title") or chat.get("first_name") or "Unknown chat"
    request_dt = datetime.fromtimestamp(message["date"], tz=timezone.utc)

    async def respond(answer: str, delete_after: int | None = None, record: bool = True) -> list[int]:
        sent_ids = await send_long_bot_message(api, chat_id, answer, reply_to_message_id=message_id)
        if record:
            try:
                history.record(chat_title_for_history, requester, text, answer)
            except Exception as e:
                log(f"[bot_listener] failed to record history: {e}")
        if delete_after and sent_ids:
            schedule_bot_delete(api, chat_id, sent_ids, delete_after, log, background_tasks)
        return sent_ids

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
        # to_thread: route_request uses the synchronous OpenAI client, which would
        # otherwise block this whole process's event loop (both this poll loop AND
        # listener.py's Telethon connection share it) for the entire network round trip.
        routed = await asyncio.to_thread(
            route_request,
            api_key=cfg.openai_api_key,
            model=cfg.openai_model,
            text=text,
            reference_date=ref_date,
            mentioned_usernames=mentioned,
            my_username=bot_username,
            requester_username=sender.get("username"),
            requester_name=requester,
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

    start_date, messages, impression_inactive = await _expand_sparse_impression_history(
        client=telethon_client,
        chat_ref=data_chat_ref,
        tz=tz,
        text=text,
        routed=routed,
        ref_date=ref_date,
        current_start_date=start_date,
        messages=messages,
        log=log,
        log_prefix="[bot_listener]",
    )

    if impression_inactive:
        await respond(f"@{routed['username']} не был активным эти дни")
        return

    focus_user = None
    username_hint = routed["username"]
    requester_aliases = {
        value.strip().lstrip("@").lower()
        for value in (requester, sender.get("username"))
        if value and value.strip()
    }
    if username_hint and username_hint.strip().lstrip("@").lower() in requester_aliases:
        # The router interpreted the original request as being about its author. Use the
        # transcript's display name and verify the identity with Telegram sender_id.
        focus_user = requester
        requester_id = sender.get("id")
        matched = sum(1 for m in messages if requester_id is not None and m.sender_id == requester_id)
        log(f"[bot_listener] focus_user(requester)={focus_user} matched={matched}/{len(messages)}")
    elif username_hint:
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
                focus_user = await asyncio.to_thread(
                    resolve_name_hint, cfg.openai_api_key, cfg.openai_model, username_hint, candidates
                )
            except ChatSummaryError as e:
                log(f"[bot_listener] name resolution failed: {e}")
                focus_user = None
            if focus_user:
                log(f"[bot_listener] resolved name hint '{username_hint}' -> '{focus_user}'")
            else:
                log(f"[bot_listener] could not resolve name hint '{username_hint}' among participants")
                # The final responder sees the original request and requester identity;
                # let it decide rather than stopping on an uncertain name match.
                focus_user = None

    lines = format_transcript_lines(messages, include_date=(start_date != end_date))
    if window_start_dt is not None:
        label = (
            f"last {_format_hours(lookback_hours)} hours "
            f"({window_start_dt.strftime('%Y-%m-%d %H:%M')} to {window_end_dt.strftime('%Y-%m-%d %H:%M')})"
        )
    else:
        label = period_label(start_date, end_date)

    # to_thread: answer_request can make several SEQUENTIAL blocking OpenAI calls for a
    # long transcript (map-reduce chunking) -- without offloading, each one would freeze
    # the whole process for its entire duration, one after another.
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

    sent_ids = await respond(f"{answer}\n\n{COMMANDS_FOOTER}")

    if bot_response_queue is not None and chat.get("type") != "private":
        # A DM reply has no group commentary to watch -- only a group-posted answer is a
        # candidate for the follow-up feature (see followup.py). sent_ids (plural: a long
        # summary can be split across several Telegram messages) lets listener.py
        # recognize a direct reply to ANY of those chunks as certainly being about this
        # response, not just plain chat commentary it has to guess at.
        await bot_response_queue.put((chat_id, sent_ids, "summary", answer))


def _joke_preview_callback_data(dm_chat_id) -> str:
    return f"{JOKE_PREVIEW_CALLBACK_PREFIX}:{dm_chat_id}"


def _parse_joke_preview_callback(data: str) -> int | None:
    parts = (data or "").split(":")
    if len(parts) != 2 or parts[0] != JOKE_PREVIEW_CALLBACK_PREFIX:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


async def handle_manual_joke(
    api: TelegramBotAPI,
    telethon_client,
    cfg,
    tz,
    message: dict,
    preview: bool,
    home_chat_ref: str | None,
    known_chat_ids: dict[str, int],
    joke_preview_pending: dict[int, dict],
    joke_posted_queue,
    bot_response_queue,
    log=print,
):
    """Handles a manual "пошути"/"пошути превью" DM (see the JOKE_PREVIEW_* constants
    above). Unlike the automatic buffer-triggered joke in listener.py, this bypasses the
    activity/cooldown/probability gates entirely -- it's an explicit ask -- but still goes
    through the same model-level decline check in joke.py, and once actually posted feeds
    the same cooldown/reaction-tracking machinery as an automatic joke (via
    joke_posted_queue), so a manual joke doesn't let someone dodge the cooldown that
    follows any joke, automatic or not.

    `preview=True` sends the generated joke back to the DM with a confirm button instead
    of posting it straight to the group -- see handle_joke_preview_callback for what
    tapping it does."""
    dm_chat_id = message["chat"]["id"]
    message_id = message["message_id"]

    if not home_chat_ref:
        await api.send_message(
            dm_chat_id, "Не настроен основной чат (LISTENER_ALLOWED_CHATS) -- некуда отправить сообщение.",
            reply_to_message_id=message_id,
        )
        return
    entry = home_chat_ref

    try:
        # fetch_recent_messages_fresh, not fetch_range_messages_cached: a manual "пошути"
        # is a deliberate, in-the-moment ask, so it needs to see whatever was *just*
        # typed -- fetch_range_messages_cached would happily reuse today's cache as-is
        # for up to TODAY_TTL_SECONDS (30 min), which is exactly why repeated tests
        # within that window kept getting the same joke off the same stale tail.
        chat_title, recent_messages = await fetch_recent_messages_fresh(
            client=telethon_client, chat_ref=home_chat_ref, tz=tz, limit=CONTEXT_MESSAGE_COUNT, log=log,
        )
        lines = format_transcript_lines(recent_messages, include_date=False)
        if not lines:
            await api.send_message(
                dm_chat_id, "Пока нет контекста -- в чате сегодня было пусто.", reply_to_message_id=message_id
            )
            return

        profile = await chat_profile.ensure_profile(
            telethon_client, home_chat_ref, entry, cfg.openai_api_key, cfg.openai_model, tz,
            cfg.joke_profile_ttl_seconds, cfg.joke_profile_lookback_days, cfg.joke_profile_max_messages, log=log,
        )
        joke_text = await asyncio.to_thread(generate_joke, cfg.openai_api_key, cfg.openai_model, lines, profile)
    except Exception:
        log(f"[bot_listener] error generating manual joke:\n{traceback.format_exc()}")
        await api.send_message(dm_chat_id, "Что-то пошло не так при генерации сообщения.", reply_to_message_id=message_id)
        return

    if not joke_text:
        await api.send_message(
            dm_chat_id, "Сейчас нечего естественно добавить к разговору.",
            reply_to_message_id=message_id,
        )
        return

    if preview:
        # Keyed by the DM's own chat_id, not a per-message id -- a DM only ever has one
        # bot conversation thread, so there's no need to track which specific message
        # this confirmation belongs to; a second "пошути превью" before confirming just
        # overwrites the pending one.
        joke_preview_pending[dm_chat_id] = {"entry": entry, "joke_text": joke_text}
        await api.send_message(
            dm_chat_id, joke_text, reply_to_message_id=message_id,
            reply_markup={"inline_keyboard": [[
                {"text": JOKE_PREVIEW_BUTTON_TEXT, "callback_data": _joke_preview_callback_data(dm_chat_id)}
            ]]},
        )
        return

    chat_id = await _resolve_chat_id(telethon_client, entry, known_chat_ids, log=log)
    if chat_id is None:
        await api.send_message(
            dm_chat_id, f"Не удалось найти чат '{entry}' -- проверь LISTENER_ALLOWED_CHATS.",
            reply_to_message_id=message_id,
        )
        return
    sent = await api.send_message(chat_id, joke_text)
    await api.send_message(dm_chat_id, "Отправлено в чат.", reply_to_message_id=message_id)
    log(f"[bot_listener] manual joke sent to '{entry}': {joke_text!r}")
    if joke_posted_queue is not None and sent and "message_id" in sent:
        await joke_posted_queue.put((entry, sent["message_id"]))
    if bot_response_queue is not None:
        sent_ids = [sent["message_id"]] if sent and "message_id" in sent else []
        await bot_response_queue.put((chat_id, sent_ids, "joke", joke_text))


async def handle_joke_preview_callback(
    api: TelegramBotAPI,
    telethon_client,
    callback: dict,
    joke_preview_pending: dict[int, dict],
    known_chat_ids: dict[str, int],
    joke_posted_queue,
    bot_response_queue,
    log=print,
):
    parsed = _parse_joke_preview_callback(callback.get("data"))
    if parsed is None:
        await api.answer_callback_query(callback["id"])
        return
    dm_chat_id = parsed

    pending = joke_preview_pending.pop(dm_chat_id, None)
    if pending is None:
        await api.answer_callback_query(callback["id"], text="Это предложение уже неактуально.")
        return

    entry = pending["entry"]
    joke_text = pending["joke_text"]
    chat_id = await _resolve_chat_id(telethon_client, entry, known_chat_ids, log=log)
    if chat_id is None:
        await api.answer_callback_query(callback["id"], text="Не удалось найти чат -- проверь LISTENER_ALLOWED_CHATS.")
        return

    try:
        sent = await api.send_message(chat_id, joke_text)
        await api.answer_callback_query(callback["id"], text="Отправлено!")
        log(f"[bot_listener] manual joke (previewed) sent to '{entry}': {joke_text!r}")
        if joke_posted_queue is not None and sent and "message_id" in sent:
            await joke_posted_queue.put((entry, sent["message_id"]))
        if bot_response_queue is not None:
            sent_ids = [sent["message_id"]] if sent and "message_id" in sent else []
            await bot_response_queue.put((chat_id, sent_ids, "joke", joke_text))
    except Exception:
        log(f"[bot_listener] failed to send previewed joke:\n{traceback.format_exc()}")
        await api.answer_callback_query(callback["id"], text="Не удалось отправить.")


async def _dispatch_update(
    update: dict,
    api: TelegramBotAPI,
    telethon_client,
    cfg,
    tz,
    bot_username: str | None,
    allowed_chats: set[str],
    summary_queue: asyncio.Queue,
    roast_pending: dict,
    roast_in_progress: set,
    background_tasks: set,
    home_chat_ref: str | None,
    known_chat_ids: dict[str, int],
    joke_preview_pending: dict[int, dict],
    joke_posted_queue,
    bot_response_queue,
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
        if (callback.get("data") or "").startswith(f"{JOKE_PREVIEW_CALLBACK_PREFIX}:"):
            await handle_joke_preview_callback(
                api, telethon_client, callback, joke_preview_pending, known_chat_ids,
                joke_posted_queue, bot_response_queue, log=log,
            )
        else:
            await handle_bot_roast_callback(
                api, telethon_client, cfg, tz, callback, roast_pending, roast_in_progress, background_tasks, log=log,
            )
        return

    message = update.get("message")
    if not message or "text" not in message:
        return

    # Learned regardless of whether this message is a trigger -- this is how
    # known_chat_ids (see run_bot_listener's joke queue consumer) finds out the Bot-API
    # chat_id for a chat named in LISTENER_ALLOWED_CHATS, since there's no way to look
    # that up on demand (getChat needs an id/username we don't have yet either). Placed
    # before the has_summary/has_roast early-return so it also learns from ordinary chat
    # messages whenever the bot's privacy mode is off, not just from /summary requests.
    chat = message["chat"]
    matched_entry = _match_allowed_chat(chat, cfg.listener_allowed_chats)
    if matched_entry is not None:
        known_chat_ids[matched_entry] = chat["id"]

    # "пошути"/"пошути превью" (see JOKE_PREVIEW_* constants) only ever fires from a DM to
    # the bot, per JOKE_MANUAL_TRIGGER_KEYWORD's own docs -- checked before has_summary/
    # has_roast since it's a wholly separate trigger with its own keyword(s). The longer
    # "preview" phrase is checked first since it contains the plain trigger word too.
    if chat.get("type") == "private":
        stripped = message["text"].lower()
        preview = cfg.joke_manual_preview_keyword in stripped
        if preview or cfg.joke_manual_trigger_keyword in stripped:
            task = asyncio.create_task(
                handle_manual_joke(
                    api, telethon_client, cfg, tz, message, preview, home_chat_ref,
                    known_chat_ids, joke_preview_pending, joke_posted_queue, bot_response_queue, log=log,
                )
            )
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)
            return

    text_lower = message["text"].lower()

    # "/top today|week|month|all" and "/stat [username]" (stats.py) -- plain lookups over
    # already-computed daily files, so they bypass the OpenAI summary queue. Reuses matched_entry
    # from the known_chat_ids learning above rather than re-matching the chat.
    if cfg.stats_enabled and (text_lower.startswith("/top") or text_lower.startswith("/stat")):
        chat_key = chat["id"]
        if matched_entry is None:
            try:
                sent = await api.send_message(
                    chat_key, "Статистика недоступна в этом чате.",
                    reply_to_message_id=message["message_id"], parse_mode=None,
                )
                if sent and "message_id" in sent:
                    schedule_bot_delete(api, chat_key, [sent["message_id"]], STATS_DELETE_AFTER, log, background_tasks)
            except Exception:
                pass
            return
        # Strips a same-account "@bot_username" mention Telegram tacks onto the command
        # with no space (e.g. "/stat@Trash_Modelist") before parsing the period/username
        # argument -- see strip_command_bot_mention in stats.py.
        stats_text = stats.strip_command_bot_mention(message["text"], bot_username)
        try:
            if text_lower.startswith("/top"):
                period = stats.parse_top_command(stats_text)
                reply_text = await stats.format_top(
                    telethon_client, matched_entry, matched_entry, period, tz, cfg.stats_top_limit, log=log
                )
            else:
                arg = stats_text[len("/stat") :].strip()
                if stats.is_procrastinator_command(arg):
                    reply_text = await stats.format_procrastinators(
                        telethon_client, matched_entry, matched_entry, tz, log=log
                    ) or PROCRASTINATOR_NONE_FOUND_MESSAGE
                elif (period := stats.parse_stat_period(arg)):
                    reply_text = await stats.format_top(
                        telethon_client, matched_entry, matched_entry, period, tz, cfg.stats_top_limit, log=log
                    )
                else:
                    from_user = message.get("from") or {}
                    user, rank, total = await stats.resolve_stat_target(
                        telethon_client, matched_entry, matched_entry, arg,
                        from_user.get("username"), _display_name(from_user), tz, log=log,
                    )
                    if user:
                        figurine_links = stats.figurine_message_links(chat.get("username"), chat_key, user)
                        reply_text = stats.format_stat(user, rank, total, figurine_links)
                    else:
                        reply_text = "Статистика не найдена -- пользователь ещё не отслеживается."
            # parse_mode=None: reply_text can embed a raw display name (leaderboard
            # entries, /stat's "Имя:" line) -- Telegram's Markdown mode would reject the
            # whole message if that name has an unbalanced _/*/`/[ (a real username with
            # a single underscore is enough), so these are always sent as plain text.
            sent = await api.send_message(
                chat_key, reply_text, reply_to_message_id=message["message_id"], parse_mode=None
            )
            if sent and "message_id" in sent:
                schedule_bot_delete(api, chat_key, [sent["message_id"]], STATS_DELETE_AFTER, log, background_tasks)
        except Exception:
            log(f"[bot_listener] error handling stats command:\n{traceback.format_exc()}")
            try:
                sent = await api.send_message(
                    chat_key, "Не удалось получить статистику.",
                    reply_to_message_id=message["message_id"], parse_mode=None,
                )
                if sent and "message_id" in sent:
                    schedule_bot_delete(api, chat_key, [sent["message_id"]], STATS_DELETE_AFTER, log, background_tasks)
            except Exception:
                pass
        return

    has_summary = any(k in text_lower for k in cfg.listener_trigger_keywords)
    # Roast ("прожарь меня") is turned off -- forced False rather than removing the
    # surrounding roast_pending/callback machinery below, so it stays a one-line revert
    # if it's ever turned back on.
    has_roast = False
    if not has_summary and not has_roast:
        return

    if not _is_chat_allowed(allowed_chats, chat):
        return

    chat_key = chat["id"]
    sender = message.get("from") or {}
    sender_id = sender.get("id")

    if has_roast:
        roast_key = (chat_key, sender_id)
        if roast_key in roast_pending or roast_key in roast_in_progress:
            log(f"[bot_listener] roast already pending/in-progress for {roast_key}, reacting instead")
            await api.set_message_reaction(chat_key, message["message_id"], ROAST_BUSY_EMOJI, log=log)
            return

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

    await api.set_message_reaction(chat_key, message["message_id"], SUMMARY_ACK_EMOJI, log=log)
    await summary_queue.put(message)
    log(
        f"[bot_listener] queued request #{summary_queue.qsize()} from "
        f"'{chat.get('title', chat_key)}': {message['text']!r}"
    )


async def run_bot_listener(
    bot_token: str,
    cfg,
    tz,
    telethon_client,
    log=print,
    joke_queue: "asyncio.Queue | None" = None,
    joke_posted_queue: "asyncio.Queue | None" = None,
    bot_response_queue: "asyncio.Queue | None" = None,
    followup_queue: "asyncio.Queue | None" = None,
    figurine_ack_queue: "asyncio.Queue | None" = None,
    stats_digest_queue: "asyncio.Queue | None" = None,
):
    """Runs until cancelled. Meant to be started as a sibling asyncio task alongside
    listener.py's Telethon client -- both share the same connected `telethon_client` for
    message fetching.

    `joke_queue`, if given, carries (allowed_chats entry, joke text) pairs put there by
    listener.py's activity trigger (see maybe_joke) -- this function drains it in a task
    running alongside the usual getUpdates poll loop and sends each one via `api`, the
    same account everything else replies from. `joke_posted_queue`, if given, is where
    (entry, sent message_id) goes right after a successful send, so listener.py -- the
    only side that can reliably watch reactions -- knows to start that chat's cooldown and
    watch that specific message.

    `bot_response_queue`/`followup_queue`, if given, are the same shape of hand-off for a
    different feature (see followup.py): (chat_id, sent_message_ids, kind, response_text)
    is put on `bot_response_queue` right after ANY summary answer or joke is posted to a
    group chat (kind is "summary" or "joke"; sent_message_ids is a list since a long
    summary can be split across several Telegram messages), so listener.py can watch the
    next few messages for chat commentary about it -- praise or criticism, not
    necessarily a reply/mention, though a direct Telegram reply to one of
    sent_message_ids is recognized with certainty rather than left for the model to
    guess. If it decides someone's actually reacting, the clap-back it generates comes
    back on `followup_queue` for this function to send, same as every other reply.

    `figurine_ack_queue`, if given, carries (allowed_chats entry, message_id) pairs put
    there by listener.py's on_message the instant it sees a #япокрасил+photo/video post and
    bumps the counter (stats.record_figurine_live) -- only the reaction itself is done
    here, via the bot account, same bot-account-only rule as every other reply.

    `stats_digest_queue`, if given, carries (allowed_chats entry, text) pairs put there
    every stats.PROCRASTINATOR_DIGEST_INTERVAL_DAYS days by listener.py's
    run_stats_rollover -- the "Топ покрастинаторов" call-out (see
    stats.format_procrastinators) -- sent here as a plain message, same account as
    everything else, and deliberately never scheduled for deletion (unlike the on-demand
    "/stat pokras" reply, which self-deletes like every other /stat or /top reply): this
    is an ambient reminder meant to stay visible in the chat.

    All queues are left None when run standalone (this module's own main()), which
    just means jokes/follow-ups/figurine reactions/digests never fire, matching that
    listener.py isn't running its activity tracking either in that mode."""
    allowed_chats = set(c.lower().lstrip("@") for c in cfg.listener_allowed_chats)
    background_tasks: set[asyncio.Task] = set()
    summary_queue: asyncio.Queue = asyncio.Queue()
    # Maps a LISTENER_ALLOWED_CHATS entry to the Bot-API chat_id it corresponds to.
    # Populated passively by _dispatch_update as it observes live updates from that chat
    # (see the comment there) and, on a miss, actively by _resolve_chat_id via the
    # Telethon session -- see that function's docstring for why that's safe to do.
    known_chat_ids: dict[str, int] = {}
    # "пошути превью" confirm-button state, keyed by the DM's own chat_id (see
    # handle_manual_joke) -- value: {"entry", "joke_text"}.
    joke_preview_pending: dict[int, dict] = {}

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
            f"{cfg.listener_trigger_keywords} (summary; roast is off). FIFO queue delay: "
            f"{cfg.summary_queue_delay_seconds}s. Timezone: {tz}."
        )

        async def _poll_loop():
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
                    # update throws, Telegram should still consider it delivered on the
                    # next getUpdates call rather than resending the same update forever.
                    offset = update["update_id"] + 1
                    try:
                        await _dispatch_update(
                            update, api, telethon_client, cfg, tz, bot_username, allowed_chats,
                            summary_queue, roast_pending, roast_in_progress, background_tasks, home_chat_ref,
                            known_chat_ids, joke_preview_pending, joke_posted_queue, bot_response_queue, log=log,
                        )
                    except Exception:
                        log(f"[bot_listener] unhandled error processing update {update.get('update_id')}:\n{traceback.format_exc()}")

        async def _consume_summaries():
            """Processes every accepted summary enquiry in FIFO order. The queue is
            intentionally unbounded: bursts are delayed, never rejected or dropped."""
            last_finished_at: float | None = None
            while True:
                message = await summary_queue.get()
                try:
                    if last_finished_at is not None:
                        elapsed = time.monotonic() - last_finished_at
                        wait_for = max(0.0, cfg.summary_queue_delay_seconds - elapsed)
                        if wait_for:
                            log(
                                f"[bot_listener] waiting {wait_for:.1f}s before next queued request "
                                f"({summary_queue.qsize()} still waiting)"
                            )
                            await asyncio.sleep(wait_for)
                    chat = message["chat"]
                    log(
                        f"[bot_listener] handling queued request in "
                        f"'{chat.get('title', chat['id'])}': {message['text']!r}"
                    )
                    try:
                        await handle_bot_summary_request(
                            api, telethon_client, cfg, tz, bot_username, message,
                            background_tasks, home_chat_ref, bot_response_queue, log=log,
                        )
                    except Exception:
                        log(f"[bot_listener] error handling queued request:\n{traceback.format_exc()}")
                        try:
                            await api.send_message(
                                chat["id"], "Что-то пошло не так при генерации сводки.",
                                reply_to_message_id=message["message_id"],
                            )
                        except Exception:
                            pass
                finally:
                    last_finished_at = time.monotonic()
                    summary_queue.task_done()

        async def _consume_jokes():
            while True:
                entry, joke_text = await joke_queue.get()
                chat_id = await _resolve_chat_id(telethon_client, entry, known_chat_ids, log=log)
                if chat_id is None:
                    log(f"[bot_listener] dropping joke for '{entry}': could not resolve a chat_id for it")
                    continue
                try:
                    sent = await api.send_message(chat_id, joke_text)
                    log(f"[bot_listener] sent joke to '{entry}': {joke_text!r}")
                    if joke_posted_queue is not None and sent and "message_id" in sent:
                        await joke_posted_queue.put((entry, sent["message_id"]))
                    if bot_response_queue is not None:
                        sent_ids = [sent["message_id"]] if sent and "message_id" in sent else []
                        await bot_response_queue.put((chat_id, sent_ids, "joke", joke_text))
                except Exception:
                    log(f"[bot_listener] failed to send joke:\n{traceback.format_exc()}")

        async def _consume_followups():
            while True:
                chat_id, reply_text = await followup_queue.get()
                try:
                    await api.send_message(chat_id, reply_text)
                    log(f"[bot_listener] sent follow-up reply to chat {chat_id}: {reply_text!r}")
                except Exception:
                    log(f"[bot_listener] failed to send follow-up reply:\n{traceback.format_exc()}")

        async def _consume_figurine_acks():
            while True:
                entry, message_id = await figurine_ack_queue.get()
                chat_id = await _resolve_chat_id(telethon_client, entry, known_chat_ids, log=log)
                if chat_id is None:
                    log(f"[bot_listener] dropping figurine reaction for '{entry}': could not resolve a chat_id for it")
                    continue
                await api.set_message_reaction(chat_id, message_id, FIGURINE_ACK_EMOJI, log=log)

        async def _consume_stats_digests():
            while True:
                entry, text = await stats_digest_queue.get()
                chat_id = await _resolve_chat_id(telethon_client, entry, known_chat_ids, log=log)
                if chat_id is None:
                    log(f"[bot_listener] dropping stats digest for '{entry}': could not resolve a chat_id for it")
                    continue
                try:
                    # parse_mode=None: the digest embeds raw display names, same reasoning
                    # as every other stats reply -- see the send_message call in the
                    # /top and /stat handling above.
                    await api.send_message(chat_id, text, parse_mode=None)
                    log(f"[bot_listener] sent procrastinator digest to '{entry}'")
                except Exception:
                    log(f"[bot_listener] failed to send stats digest:\n{traceback.format_exc()}")

        tasks = [_poll_loop(), _consume_summaries()]
        if joke_queue is not None:
            tasks.append(_consume_jokes())
        if followup_queue is not None:
            tasks.append(_consume_followups())
        if figurine_ack_queue is not None:
            tasks.append(_consume_figurine_acks())
        if stats_digest_queue is not None:
            tasks.append(_consume_stats_digests())
        await asyncio.gather(*tasks)


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
