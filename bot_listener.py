"""Long-polls the Telegram Bot HTTP API for /summary requests and direct replies in the
same chats listener.py's Telethon-based listener watches, and answers them as the bot
account instead of your personal account.

Why this exists alongside listener.py: a bot account lets people trigger this without it
coming from (or being confused with) your own account. The tradeoff is that the Bot API
gives a bot no retroactive access to chat history at all -- it only ever sees messages
sent after it's added to a chat. So message fetching here still goes through the already-
connected Telethon `client` passed into run_bot_listener() (same
fetch_range_messages_cached() listener.py itself uses); only trigger detection and
replying happen over the bot's HTTP API.

Any human message sent with Telegram's Reply action against a message authored by this
bot gets a normal conversational response. This is unconditional once a bot token is
configured and is separate from JOKE_ENABLED, which controls only unprompted remarks.

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

The DM is also where every summary ANSWER goes, including for a request typed in a group
-- the group only gets a short receipt, which takes the request down with it ten seconds
later (see handle_bot_summary_request and SUMMARY_RECEIPT_DELETE_AFTER). Since
Telegram forbids a bot from writing first, somebody who has never opened the DM has
nowhere to receive an answer, and is told to open it instead of being answered.

Always uses the v2 pipeline (intent_v2 + responder_v2) regardless of
SUMMARY_PIPELINE_VERSION, which only governs the older Telethon-listener code path kept
for rollback/comparison -- see intent_v2.py's module docstring.

/vote opens the weekly-contest voting Mini App (voting.py / vote_web.py) -- a real web
page served by this same process, alongside the long-poll loop, whenever WEBAPP_PUBLIC_URL
and PORT are set (see run_bot_listener). Bare "/vote" is the plain ballot for everyone,
including an admin (also a status/control panel for one); "/vote выбрать" (DM, admin-only)
is the separate moderation screen; "/vote собрать" (DM, admin-only) (re-)scans
#итогинедели posts into the poll; "/vote очистить" (DM, admin-only, tap-to-confirm)
deletes it outright; "/vote chat" (DM, admin-only) drafts an announcement. See
handle_vote_command's docstring.

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
import uuid
from datetime import datetime, timedelta, timezone

import aiohttp
from telethon import utils as tl_utils

import cabinet
import button_builder
import chat_profile
import economy
import history
import poker
import preview
import stats
import vote_web
import voting
from bot_api import CAPTION_LIMIT, TelegramBotAPI
from config import build_session, load_config
from critique import critique_work
from errors import ChatSummaryError
from followup import generate_direct_reply
from intent import resolve_name_hint
from intent_v2 import route_request
from joke import CONTEXT_MESSAGE_COUNT, generate_joke
from listener import (
    COMMANDS_FOOTER,
    DAY_LIMIT_MESSAGE,
    DISMISS_DELETE_AFTER,
    ERROR_DELETE_AFTER,
    FIGURINE_ACK_EMOJI,
    PROCRASTINATOR_NONE_FOUND_MESSAGE,
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
import tree
from telegram_fetch import (
    fetch_range_messages_cached,
    fetch_recent_messages_fresh,
    format_transcript_lines,
    resolve_chat,
    sender_matches,
)

MAX_REPLY_CHARS = 4000
POLL_TIMEOUT_SECONDS = 30

# A summary asked for in a group is now ANSWERED IN THE REQUESTER'S DM with the bot; the
# group itself keeps only a short receipt pointing there. Both that receipt and the
# request that prompted it are swept after this long -- everything left in the group is
# bookkeeping, and bookkeeping shouldn't outlive being read. The same 10s
# ERROR_DELETE_AFTER gives its notices: long enough to read one line and tap a link.
SUMMARY_RECEIPT_DELETE_AFTER = 10
SUMMARY_RECEIPT_TEXT = "Сообщение отправлено 👍"
# Telegram forbids a bot from writing first, so somebody who has never opened the DM is
# simply unreachable -- there is no way to deliver their answer and nothing to do but say
# so. Same 10s sweep: the request is gone either way.
SUMMARY_DM_CLOSED_TEXT = "Активируй личку бота, чтобы получать ответы там."

# How long ONE update may take before the poll loop abandons it and moves on.
#
# Updates are handled one at a time (see _poll_loop), so this is what stands between a
# single stuck handler and a bot that stops answering anything at all -- which is not a
# hypothetical: a Telethon call on a session that cannot connect waits indefinitely rather
# than raising, and it took the whole bot down with it, the visible symptom being an
# unrelated menu that "stopped opening" long after the button that actually wedged it.
#
# Generous on purpose. The genuinely slow inline paths are the vision critique and the
# conversational reply, both offloaded to a thread and bounded by the OpenAI client's own
# timeout; three minutes is far beyond either, so this can only ever fire on something
# pathological. Note that cancelling an asyncio.to_thread does not stop the thread -- the
# call finishes and its result is discarded, which costs one wasted request and no
# correctness.
UPDATE_HANDLING_TIMEOUT_SECONDS = 180

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

BADGE_CALLBACK_PREFIX = "badge"
BADGE_FLOW_TTL_SECONDS = 10 * 60
BADGE_CREATE_BUTTON_TEXT = "➕ Создать значок"
BADGE_GIVE_BUTTON_TEXT = "🎁 Выдать значок"
# Same award, minus the group announcement (see _award_badge_from_flow). A separate
# button rather than a toggle on the menu: a toggle has a state the admin has to read
# back before tapping, and getting it wrong publishes something meant to stay quiet.
BADGE_GIVE_QUIET_BUTTON_TEXT = "🤫 Выдать без уведомления"
BADGE_REVOKE_BUTTON_TEXT = "➖ Забрать у участника"
BADGE_DELETE_BUTTON_TEXT = "🗑 Удалить значок совсем"
# One reply can name several recipients. Bounded so a pasted wall of text turns into one
# clear refusal instead of a few dozen resolve_stat_target lookups and a summary message
# too long for Telegram to accept.
BADGE_MAX_RECIPIENTS = 30
WEEK_WINNER_COMMAND = "/weekwinner"
DELETE_POKRAS_COMMAND = "/deletepokras"
BADGE_ADMIN_COMMAND = "/badgeadmin"
REPLANT_COMMAND = "/replant"
SEND_COMMAND = "/send"
PREVIEW_COMMAND = "/preview"
BUTTON_BUILDER_COMMAND = button_builder.COMMAND
BUTTON_BUILDER_FLOW_TTL_SECONDS = button_builder.FLOW_TTL_SECONDS

# Opens the planting ceremony. Two spellings for one action: Telegram only treats
# [a-zA-Z0-9_] after a slash as a command, so "/посадить_семечко" is never highlighted,
# never autocompletes, and -- if the bot's privacy mode is ever turned back on -- never
# reaches the bot in a group at all. "/plant" always works and can be registered in the
# menu; the Cyrillic spelling is kept because it is the one that was asked for and reads
# far better in the chat.
PLANT_COMMANDS = ("/посадить_семечко", "/plant")
PLANT_REMINDER_COMMANDS = ("/напомнить_посадку", "/plantreminder")
# The real planting button, as opposed to preview.SAMPLE_CALLBACK, which looks identical
# and does nothing.
PLANT_CALLBACK_PREFIX = "plant"

# Opens a poker table. Same two-spelling rule as the planting command; "/покер" is the one
# people actually type, "/poker" is the one Telegram can highlight.
POKER_COMMANDS = (poker.COMMAND, "/покер")
# "/poker стоп" closes a table whose buttons have scrolled out of reach.
POKER_STOP_WORDS = frozenset({"стоп", "закрыть", "заверши", "завершить", "stop", "close", "end"})

# Explicit bot-management delegates. These users may use the DM-only management
# commands even without Telegram administrator status in the configured home chat.
# Usernames are compared case-insensitively and without a leading @.
PRIVILEGED_MANAGEMENT_USERNAMES = frozenset({"sultan_kembayev"})


SHOP_COMMANDS = ("/shop", "/buy", "/coins")

CABINET_COMMAND = "/cabinet"

# Opens the weekly-contest voting Mini App (voting.py / vote_web.py). Two spellings for
# the same reason /plant has two: "/голосование" is what people type, "/vote" is what
# Telegram can highlight and register in the menu.
VOTE_COMMANDS = ("/vote", "/голосование")
# Rebuilds the entry list from the last two days of #итогинедели posts. Admin-only and
# separate from opening the page: collecting downloads every photo in every nomination,
# which is slow enough that it must be something somebody asks for, not something that
# happens each time a voter taps a button.
VOTE_COLLECT_WORDS = frozenset({"собрать", "обновить", "collect", "refresh"})
# Opens the moderation screen (admit toggles, live counts, closing the vote) explicitly,
# as opposed to bare "/vote" -- which now always opens the plain ballot, even for an
# administrator, so admitting entries never blocks an admin from casting their own vote.
VOTE_MODERATE_WORDS = frozenset({"выбрать", "модерация", "moderate", "admin"})
# Deletes the current poll outright -- entries, votes, admitted flags, downloaded photos
# -- so the next "/vote собрать" starts genuinely fresh. Destructive and irreversible, so
# it goes through the same tap-to-confirm inline button as everything else that deletes
# real data (see VOTE_CLEAR_CALLBACK_PREFIX below), rather than firing on the word alone.
VOTE_CLEAR_WORDS = frozenset({"очистить", "сброс", "clear", "reset"})
VOTE_CLEAR_CALLBACK_PREFIX = "voteclear"
VOTE_OPEN_BUTTON_TEXT = "🗳 Открыть голосование"
# "/vote chat" -- drafts an announcement (custom text + the vote button) for posting
# somewhere. Asks for the text via the same force-reply convention as every other
# short text-entry flow in this file (badge_flows, cabinet_flows), rather than trying to
# read a message-after-the-command, since that has no natural end and would swallow
# whatever the admin says next in the DM.
VOTE_CHAT_WORDS = frozenset({"chat", "объявление", "announce"})
VOTE_CHAT_FLOW_TTL_SECONDS = 10 * 60
# Buttons on an administrator's bare-/vote status message for собрать/chat/очистить --
# unlike "Открыть голосование"/"Модерация", those three are bot ACTIONS, not Mini App
# pages, so they can't be a web_app button; tapping one runs the exact same code path as
# typing the command (see handle_vote_action_callback), just via a synthetic message.
VOTE_ACTION_CALLBACK_PREFIX = "voteaction"
VOTE_ACTIONS = {"collect": "/vote собрать", "chat": "/vote chat", "clear": "/vote очистить"}

# Registered with Telegram so the client shows a tappable ☰ Menu next to the input field
# -- the point being that nobody has to know a command exists in order to use the bot.
# Deliberately excludes the admin-only DM commands (/badge, /weekwinner, /deletepokras):
# advertising them to all 190 members would invite a wave of "нужны права администратора".
PRIVATE_CHAT_COMMANDS = (
    {"command": "cabinet", "description": "Личный кабинет"},
    {"command": "stat", "description": "Моя статистика"},
    {"command": "top", "description": "Рейтинг чата"},
    {"command": "shop", "description": "Магазин"},
    {"command": "coins", "description": "Мой баланс"},
    {"command": "tree", "description": "Наше дерево ЕПХ"},
    {"command": "vote", "description": "Голосование за итоги недели"},
)
# Shorter in groups: the wallet actions belong in the DM, where a balance isn't public,
# and /cabinet is deliberately absent -- it only works in a DM, so offering it here would
# be a button that answers "напиши мне в личку".
#
# "/topall" and "/toppokras" are spelled without a space because Telegram only accepts
# [a-z0-9_] in a registered command name: "/top all" cannot be a menu entry at all. Both
# spellings work when typed (see parse_top_argument).
GROUP_CHAT_COMMANDS = (
    {"command": "stat", "description": "Статистика участника"},
    {"command": "topall", "description": "Рейтинг чата"},
    {"command": "toppokras", "description": "Топ прокрастинаторов"},
    {"command": "tree", "description": "Наше дерево ЕПХ"},
    {"command": "vote", "description": "Голосование за итоги недели"},
)

# An unhandled DM gets the menu back instead of silence -- see maybe_send_menu. The
# cooldown only stops a burst of messages producing a wall of identical menus; set it to
# 0 to answer literally every message.
MENU_FALLBACK_COOLDOWN_SECONDS = 60
# Same ten-minute window the badge flows use: only the two force-reply steps (setting a
# title, sending coins) need server-side state at all -- every button in the cabinet
# carries its own owner id, so navigation itself survives a restart.
CABINET_FLOW_TTL_SECONDS = 10 * 60
CABINET_DM_ONLY_NOTICE = "Личный кабинет работает в личке с ботом: напиши мне /cabinet."


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




def _badge_callback_data(action: str, flow_id: str, badge_id: str | None = None) -> str:
    parts = [BADGE_CALLBACK_PREFIX, action, flow_id]
    if badge_id:
        parts.append(badge_id)
    return ":".join(parts)


def _parse_badge_recipients(text: str) -> list[str]:
    """Split one force-reply into the names it lists.

    Separators are commas, semicolons and newlines -- deliberately NOT spaces: plenty of
    members are tracked under a two-word display name ("Алексей Белявский"), and splitting
    on whitespace would go looking for two people who do not exist. Repeats are dropped
    case-insensitively (and ignoring a leading @) so pasting a list twice still awards
    once, which matters because the confirmation reports a count.
    """
    seen = set()
    names = []
    for chunk in re.split(r"[,;\n]+", text or ""):
        name = chunk.strip()
        if not name:
            continue
        key = name.lower().lstrip("@")
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def _parse_badge_callback(data: str) -> tuple[str, str, str | None] | None:
    parts = (data or "").split(":")
    if len(parts) not in (3, 4) or parts[0] != BADGE_CALLBACK_PREFIX:
        return None
    return parts[1], parts[2], parts[3] if len(parts) == 4 else None


def _badge_flow(
    badge_flows: dict[str, dict],
    flow_id: str,
    chat_id: int,
    admin_id: int,
) -> dict | None:
    flow = badge_flows.get(flow_id)
    if not flow:
        return None
    if time.monotonic() - flow["created_at"] > BADGE_FLOW_TTL_SECONDS:
        badge_flows.pop(flow_id, None)
        return None
    if flow["chat_id"] != chat_id or flow["admin_id"] != admin_id:
        return None
    return flow


async def _is_chat_admin(api: TelegramBotAPI, chat_id: int, user_id: int) -> bool:
    try:
        administrators = await api.get_chat_administrators(chat_id)
    except ChatSummaryError:
        return False
    return any((member.get("user") or {}).get("id") == user_id for member in administrators)


_CHAT_MEMBER_STATUSES = frozenset({"creator", "administrator", "member"})


async def _is_chat_member(api: TelegramBotAPI, chat_id: int, user_id: int) -> bool:
    """Whether this user is currently a member of the chat -- the "подписчики" gate for
    /vote. "restricted" is special-cased because Telegram uses that single status for two
    different situations: a member under restrictions (still in the chat) and someone who
    was restricted and then left. Only the payload's `is_member` field tells them apart;
    trusting the status alone would let a departed user keep voting."""
    try:
        member = await api.get_chat_member(chat_id, user_id)
    except ChatSummaryError:
        # A user who never joined makes Telegram return an error here, same as a
        # transient API failure -- either way, failing closed beats letting a
        # non-member's vote through.
        return False
    status = member.get("status")
    if status == "restricted":
        return bool(member.get("is_member"))
    return status in _CHAT_MEMBER_STATUSES


async def _can_manage_chat(
    api: TelegramBotAPI, chat_id: int, user: dict | None, entry: str | None = None
) -> bool:
    """Who may create and award custom badges from a DM.

    Three routes: a Telegram administrator of the home chat, a hardcoded delegate
    (PRIVILEGED_MANAGEMENT_USERNAMES), or somebody an administrator delegated at runtime
    with /badgeadmin (stats.is_badge_manager). The last one is checked first because it
    is a local file read, while the admin check is a Telegram round trip.
    """
    user = user or {}
    user_id = user.get("id")
    if entry and user_id and stats.is_badge_manager(entry, user_id):
        return True
    username = (user.get("username") or "").strip().lstrip("@").lower()
    if username in PRIVILEGED_MANAGEMENT_USERNAMES:
        return True
    return bool(user_id and await _is_chat_admin(api, chat_id, user_id))


def _is_privileged_username(user: dict | None) -> bool:
    """Whether this person is a hardcoded delegate (PRIVILEGED_MANAGEMENT_USERNAMES).

    Split out so it can be checked ON ITS OWN, and FIRST, wherever an admin gate sits in
    front of work that needs no chat at all. The full gate below has to resolve the home
    chat's id before it can ask Telegram for the administrator list, and resolving goes
    through the Telethon session -- which, when unwell, waits instead of failing. For a
    button whose entire job is to write a message back into the DM it is already in, that
    round trip was the only thing that could go wrong, and it did.
    """
    username = ((user or {}).get("username") or "").strip().lstrip("@").lower()
    return username in PRIVILEGED_MANAGEMENT_USERNAMES


async def _is_chat_admin_or_privileged(
    api: TelegramBotAPI, chat_id: int, user: dict | None
) -> bool:
    """Who may DELEGATE badge management. Deliberately excludes the delegates themselves:
    a badge manager can hand out badges, not hand out the right to hand out badges."""
    if _is_privileged_username(user):
        return True
    user_id = (user or {}).get("id")
    return bool(user_id and await _is_chat_admin(api, chat_id, user_id))


async def handle_badge_command(
    api: TelegramBotAPI,
    message: dict,
    entry: str | None,
    admin_chat_id: int | None,
    badge_flows: dict[str, dict],
) -> None:
    """Start an admin-bound custom-badge flow in a private chat with the bot."""
    chat = message["chat"]
    dm_chat_id = chat["id"]
    admin = message.get("from") or {}
    admin_id = admin.get("id")
    # Management commands are deliberately silent in a group. Apart from keeping the
    # admin UI private, this prevents command/menu clutter in the public chat.
    if chat.get("type") != "private":
        return
    if entry is None or admin_chat_id is None:
        await api.send_message(
            dm_chat_id,
            "Не настроен единственный основной чат для управления значками.",
            reply_to_message_id=message["message_id"],
            parse_mode=None,
        )
        return
    if not admin_id or not await _can_manage_chat(api, admin_chat_id, admin, entry):
        await api.send_message(
            dm_chat_id,
            "Создавать и выдавать значки могут только администраторы чата.",
            reply_to_message_id=message["message_id"],
            parse_mode=None,
        )
        return

    flow_id = uuid.uuid4().hex[:10]
    badge_flows[flow_id] = {
        "created_at": time.monotonic(),
        "chat_id": dm_chat_id,
        "admin_chat_id": admin_chat_id,
        "entry": entry,
        "admin_id": admin_id,
        "admin_name": _display_name(admin),
        "target": None,
        "awaiting": None,
        "selected_badge_id": None,
        # Set when the admin enters the give step, by which button they used. Defaults to
        # announcing: a flow that somehow reached the award without passing through either
        # button behaves the way it always did.
        "silent": False,
    }
    await api.send_message(
        dm_chat_id,
        "🏅 Управление значками\nРаботает только в этой личной переписке.",
        reply_to_message_id=message["message_id"],
        reply_markup={
            "inline_keyboard": [
                [{"text": BADGE_CREATE_BUTTON_TEXT, "callback_data": _badge_callback_data("create", flow_id)}],
                [{"text": BADGE_GIVE_BUTTON_TEXT, "callback_data": _badge_callback_data("list", flow_id)}],
                [{"text": BADGE_GIVE_QUIET_BUTTON_TEXT, "callback_data": _badge_callback_data("listq", flow_id)}],
                [{"text": BADGE_REVOKE_BUTTON_TEXT, "callback_data": _badge_callback_data("revlist", flow_id)}],
                [{"text": BADGE_DELETE_BUTTON_TEXT, "callback_data": _badge_callback_data("dellist", flow_id)}],
            ]
        },
        parse_mode=None,
    )


async def _award_badge_from_flow(
    api: TelegramBotAPI,
    flow: dict,
    badge_id: str,
    targets: dict | list[dict],
    reply_to_message_id: int | None,
    log=print,
    missing: list[str] | None = None,
) -> None:
    """Award one badge to one or more members.

    However many recipients there are, the admin gets ONE summary and the group gets ONE
    announcement -- handing a badge to eight people must not post eight messages. A single
    dict is still accepted so the preset-target path reads unchanged.

    `flow["silent"]` skips the group announcement entirely (the 🤫 button). The admin's own
    confirmation is never silenced: it is the only thing telling them the award landed.
    `missing` are names that resolved to nobody; they are reported alongside rather than
    discarding the recipients that did resolve.
    """
    if isinstance(targets, dict):
        targets = [targets]
    badge = None
    awarded, already = [], []
    for target in targets:
        badge, newly_awarded = stats.give_custom_badge(
            flow["entry"],
            badge_id,
            target["user_id"],
            target["display_name"],
            flow["admin_id"],
            flow["admin_name"],
        )
        (awarded if newly_awarded else already).append(target)

    lines = []
    if awarded:
        who = ", ".join(target["display_name"] for target in awarded)
        lines.append(
            f"🎉 {who} получает значок {badge.label}!" if len(awarded) == 1
            else f"🎉 Значок {badge.label} получают ({len(awarded)}): {who}"
        )
    if already:
        who = ", ".join(target["display_name"] for target in already)
        lines.append(
            f"{who} уже имеет значок {badge.label}." if len(already) == 1
            else f"Уже имели значок ({len(already)}): {who}"
        )
    if missing:
        lines.append("Не нашёл в статистике: " + ", ".join(missing))
    if awarded and flow.get("silent"):
        lines.append("Без объявления в чате.")
    await api.send_message(
        flow["chat_id"],
        "\n".join(lines),
        reply_to_message_id=reply_to_message_id,
        parse_mode=None,
    )
    if awarded and not flow.get("silent"):
        await _announce_badge_in_chat(api, flow.get("admin_chat_id"), badge, awarded, log=log)


async def _announce_badge_in_chat(
    api: TelegramBotAPI, chat_id, badge, targets: dict | list[dict], log=print
) -> None:
    """Tell the group somebody was given a unique badge.

    Only for genuinely NEW awards -- give_custom_badge is idempotent, and re-running it
    must not post the same announcement again. Best-effort: the badge is already
    recorded by the time this runs, so a failed send costs the announcement, never the
    badge.

    Sent as plain text with the @username inline rather than an HTML mention: a display
    name is user-controlled and would have to be escaped, and a plain @username is what
    actually notifies the person. Several recipients share one message, which still
    notifies each of them, rather than one message apiece.
    """
    if chat_id is None:
        return
    if isinstance(targets, dict):
        targets = [targets]
    if not targets:
        return
    names = []
    for target in targets:
        username = (target.get("username") or "").lstrip("@")
        names.append(f"@{username}" if username else target.get("display_name", "Участник"))
    who = ", ".join(names)
    verb = "получил" if len(names) == 1 else "получили"
    try:
        await api.send_message(
            chat_id,
            f"{who} {verb} уникальный значок: {badge.label}",
            parse_mode=None,
        )
    except Exception:
        log(f"[bot_listener] failed to announce a badge in the chat:\n{traceback.format_exc()}")


async def handle_badge_callback(
    api: TelegramBotAPI,
    callback: dict,
    badge_flows: dict[str, dict],
) -> None:
    parsed = _parse_badge_callback(callback.get("data") or "")
    if parsed is None:
        return
    action, flow_id, badge_id = parsed
    callback_id = callback.get("id")
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    actor = callback.get("from") or {}
    flow = _badge_flow(badge_flows, flow_id, chat.get("id"), actor.get("id"))
    if flow is None:
        await api.answer_callback_query(callback_id, "Меню устарело или принадлежит другому администратору.")
        return
    if not await _can_manage_chat(api, flow["admin_chat_id"], actor, flow.get("entry")):
        await api.answer_callback_query(callback_id, "Нужны права администратора.")
        return

    await api.answer_callback_query(callback_id)
    if action == "create":
        flow["awaiting"] = "create_spec"
        prompt = await api.send_message(
            flow["chat_id"],
            "Ответьте на это сообщение: сначала эмодзи, затем название.\nНапример: 🎯 Меткий глаз",
            reply_to_message_id=message.get("message_id"),
            reply_markup={"force_reply": True, "selective": True},
            parse_mode=None,
        )
        flow["prompt_message_id"] = prompt.get("message_id") if prompt else None
        return

    # Both give buttons land here; they differ only in whether the award is announced in
    # the group, which the flow carries from this point on.
    if action in ("list", "listq"):
        flow["silent"] = action == "listq"
        badges = stats.list_custom_badges(flow["entry"])
        if not badges:
            await api.send_message(
                flow["chat_id"],
                "Пока нет пользовательских значков. Сначала создайте первый.",
                reply_to_message_id=message.get("message_id"),
                parse_mode=None,
            )
            return
        keyboard = [
            [{"text": badge.label, "callback_data": _badge_callback_data("give", flow_id, badge.badge_id)}]
            for badge in badges
        ]
        await api.send_message(
            flow["chat_id"],
            "Выберите значок (без объявления в чате):" if flow["silent"] else "Выберите значок:",
            reply_to_message_id=message.get("message_id"),
            reply_markup={"inline_keyboard": keyboard},
            parse_mode=None,
        )
        return

    if action == "give" and badge_id:
        if flow["target"]:
            await _award_badge_from_flow(
                api, flow, badge_id, flow["target"], message.get("message_id")
            )
            badge_flows.pop(flow_id, None)
        else:
            flow["selected_badge_id"] = badge_id
            flow["awaiting"] = "target"
            prompt = await api.send_message(
                flow["chat_id"],
                "Ответьте на это сообщение именем или @username получателя.\n"
                "Можно сразу несколько — через запятую или с новой строки.\n"
                "Участник должен уже присутствовать в статистике основного чата."
                + ("\n\n🤫 Объявления в чате не будет." if flow.get("silent") else ""),
                reply_to_message_id=message.get("message_id"),
                reply_markup={"force_reply": True, "selective": True},
                parse_mode=None,
            )
            flow["prompt_message_id"] = prompt.get("message_id") if prompt else None
        return

    # Pick a badge, for either of the two destructive actions.
    if action in ("dellist", "revlist"):
        badges = stats.list_custom_badges(flow["entry"])
        if not badges:
            await api.send_message(
                flow["chat_id"], "Пока нет пользовательских значков.",
                reply_to_message_id=message.get("message_id"), parse_mode=None,
            )
            return
        next_action = "del" if action == "dellist" else "rev"
        prompt_text = (
            "Какой значок удалить совсем?" if action == "dellist"
            else "Какой значок забрать у участника?"
        )
        await api.send_message(
            flow["chat_id"], prompt_text,
            reply_to_message_id=message.get("message_id"),
            reply_markup={"inline_keyboard": [
                [{"text": badge.label,
                  "callback_data": _badge_callback_data(next_action, flow_id, badge.badge_id)}]
                for badge in badges
            ]},
            parse_mode=None,
        )
        return

    if action == "del" and badge_id:
        # Deleting a definition takes the badge away from everybody holding it, so the
        # count is spelled out before the second tap rather than discovered afterwards.
        badge = next(
            (item for item in stats.list_custom_badges(flow["entry"]) if item.badge_id == badge_id),
            None,
        )
        if badge is None:
            await api.send_message(
                flow["chat_id"], "Этот значок уже удалён.",
                reply_to_message_id=message.get("message_id"), parse_mode=None,
            )
            return
        holders = stats.custom_badge_holder_count(flow["entry"], badge_id)
        note = f"\nСейчас он есть у {holders} чел. — у них он тоже пропадёт." if holders else ""
        await api.send_message(
            flow["chat_id"],
            f"Удалить значок {badge.label} совсем?{note}\nОтменить будет нельзя.",
            reply_to_message_id=message.get("message_id"),
            reply_markup={"inline_keyboard": [
                [{"text": "🗑 Да, удалить",
                  "callback_data": _badge_callback_data("delok", flow_id, badge_id)}],
            ]},
            parse_mode=None,
        )
        return

    if action == "delok" and badge_id:
        deleted = stats.delete_custom_badge(flow["entry"], badge_id)
        await api.send_message(
            flow["chat_id"],
            f"Значок {deleted.label} удалён." if deleted else "Этот значок уже удалён.",
            reply_to_message_id=message.get("message_id"), parse_mode=None,
        )
        badge_flows.pop(flow_id, None)
        return

    if action == "rev" and badge_id:
        flow["selected_badge_id"] = badge_id
        flow["awaiting"] = "revoke_target"
        prompt = await api.send_message(
            flow["chat_id"],
            "Ответьте на это сообщение именем или @username участника, у которого забрать значок.",
            reply_to_message_id=message.get("message_id"),
            reply_markup={"force_reply": True, "selective": True},
            parse_mode=None,
        )
        flow["prompt_message_id"] = prompt.get("message_id") if prompt else None
        return


async def handle_badge_text_input(
    api: TelegramBotAPI,
    telethon_client,
    message: dict,
    tz,
    badge_flows: dict[str, dict],
    log=print,
) -> bool:
    """Consumes the admin's force-reply after Create or after choosing a recipient."""
    chat_id = message["chat"]["id"]
    actor = message.get("from") or {}
    actor_id = actor.get("id")
    replied_message_id = (message.get("reply_to_message") or {}).get("message_id")
    flow_pair = next(
        (
            (flow_id, flow)
            for flow_id, flow in badge_flows.items()
            if flow.get("chat_id") == chat_id
            and flow.get("admin_id") == actor_id
            and flow.get("awaiting") in ("create_spec", "target", "revoke_target")
            and flow.get("prompt_message_id") == replied_message_id
            and time.monotonic() - flow["created_at"] <= BADGE_FLOW_TTL_SECONDS
        ),
        None,
    )
    if flow_pair is None:
        return False
    flow_id, flow = flow_pair
    text = (message.get("text") or "").strip()
    if text.lower() in ("/cancel", "отмена"):
        badge_flows.pop(flow_id, None)
        await api.send_message(
            chat_id, "Действие отменено.", reply_to_message_id=message["message_id"], parse_mode=None
        )
        return True
    if not await _can_manage_chat(api, flow["admin_chat_id"], actor, flow.get("entry")):
        badge_flows.pop(flow_id, None)
        return True

    if flow["awaiting"] == "create_spec":
        try:
            emoji, name = stats.parse_custom_badge_spec(text)
            badge = stats.create_custom_badge(
                flow["entry"], emoji, name, flow["admin_id"], flow["admin_name"]
            )
        except ValueError as e:
            prompt = await api.send_message(
                chat_id,
                str(e),
                reply_to_message_id=message["message_id"],
                reply_markup={"force_reply": True, "selective": True},
                parse_mode=None,
            )
            flow["prompt_message_id"] = prompt.get("message_id") if prompt else None
            return True
        badge_flows.pop(flow_id, None)
        await api.send_message(
            chat_id,
            f"Создан значок {badge.label}. Теперь его можно выдать через /badge.",
            reply_to_message_id=message["message_id"],
            parse_mode=None,
        )
        return True

    if flow["awaiting"] == "revoke_target":
        target, _, _, _, _, _ = await stats.resolve_stat_target(
            telethon_client, flow["entry"], flow["entry"], text, None, "", tz, log=log
        )
        if target is None:
            prompt = await api.send_message(
                chat_id,
                "Участник не найден в статистике. Попробуйте точный @username.",
                reply_to_message_id=message["message_id"],
                reply_markup={"force_reply": True, "selective": True},
                parse_mode=None,
            )
            flow["prompt_message_id"] = prompt.get("message_id") if prompt else None
            return True
        badge_flows.pop(flow_id, None)
        revoked = stats.revoke_custom_badge(
            flow["entry"], flow["selected_badge_id"], target.user_id
        )
        # Deliberately NOT announced in the group: an award is good news worth sharing,
        # having one taken away is not something to publish about somebody.
        await api.send_message(
            chat_id,
            f"Забрал значок {revoked.label} у {target.display_name}." if revoked
            else f"У {target.display_name} нет этого значка.",
            reply_to_message_id=message["message_id"],
            parse_mode=None,
        )
        return True

    try:
        names = _parse_badge_recipients(text)
        if not names:
            raise ValueError("Укажите имя или @username получателя.")
        if len(names) > BADGE_MAX_RECIPIENTS:
            raise ValueError(
                f"Слишком много получателей ({len(names)}), максимум {BADGE_MAX_RECIPIENTS} за раз."
            )
        resolved, missing = [], []
        seen_ids = set()
        for name in names:
            target, _, _, _, _, _ = await stats.resolve_stat_target(
                telethon_client,
                flow["entry"],
                flow["entry"],
                name,
                None,
                "",
                tz,
                log=log,
            )
            if target is None:
                missing.append(name)
                continue
            # Two spellings of the same member (a name and their @username) resolve to one
            # person; awarding twice is harmless but would be counted twice in the summary.
            if str(target.user_id) in seen_ids:
                continue
            seen_ids.add(str(target.user_id))
            resolved.append({
                "user_id": target.user_id,
                "display_name": target.display_name,
                "username": target.username,
            })
        if not resolved:
            # Nobody at all matched -- re-prompt, since there is nothing to report but the
            # failure and the admin most likely mistyped.
            raise ValueError(
                "Не нашёл в статистике: " + ", ".join(missing)
                + "\nПопробуйте точный @username."
            )
        await _award_badge_from_flow(
            api,
            flow,
            flow["selected_badge_id"],
            resolved,
            message["message_id"],
            log=log,
            missing=missing,
        )
        badge_flows.pop(flow_id, None)
    except ValueError as e:
        prompt = await api.send_message(
            chat_id,
            str(e),
            reply_to_message_id=message["message_id"],
            reply_markup={"force_reply": True, "selective": True},
            parse_mode=None,
        )
        flow["prompt_message_id"] = prompt.get("message_id") if prompt else None
    return True


async def handle_week_winner_command(
    api: TelegramBotAPI,
    telethon_client,
    message: dict,
    command_text: str,
    entry: str | None,
    admin_chat_id: int | None,
    tz,
    log=print,
) -> None:
    """DM-only admin command: /weekwinner <contest week> @username."""
    chat = message["chat"]
    dm_chat_id = chat["id"]
    admin = message.get("from") or {}
    admin_id = admin.get("id")
    if chat.get("type") != "private":
        return
    if entry is None or admin_chat_id is None:
        await api.send_message(
            dm_chat_id,
            "Не настроен единственный основной чат для назначения победителя.",
            reply_to_message_id=message["message_id"],
            parse_mode=None,
        )
        return
    if not admin_id or not await _can_manage_chat(api, admin_chat_id, admin, entry):
        await api.send_message(
            dm_chat_id,
            "Назначать победителя могут только администраторы чата.",
            reply_to_message_id=message["message_id"],
            parse_mode=None,
        )
        return

    parts = command_text.strip().split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit() or int(parts[1]) < 1:
        await api.send_message(
            dm_chat_id,
            "Использование: /weekwinner 1 @username",
            reply_to_message_id=message["message_id"],
            parse_mode=None,
        )
        return
    contest_week = int(parts[1])

    tracked, _, _, _, _, _ = await stats.resolve_stat_target(
        telethon_client,
        entry,
        entry,
        parts[2],
        None,
        "",
        tz,
        log=log,
    )
    target = (
        {"user_id": tracked.user_id, "display_name": tracked.display_name,
         "username": tracked.username}
        if tracked
        else None
    )

    if target is None:
        await api.send_message(
            dm_chat_id,
            "Не нашёл победителя. Укажите точный @username участника из статистики.",
            reply_to_message_id=message["message_id"],
            parse_mode=None,
        )
        return

    status, wins, existing_winner = stats.record_weekly_contest_winner(
        entry,
        contest_week,
        target["user_id"],
        target["display_name"],
        admin_id,
        _display_name(admin),
    )
    if status == "awarded":
        text = (
            f"🏆 {target['display_name']} — победитель Недельного Конкурса №{contest_week}!\n"
            f"Всего побед: {wins}"
        )
    elif status == "already":
        text = (
            f"{target['display_name']} уже записан(а) победителем недели №{contest_week}. "
            f"Всего побед: {wins}"
        )
    else:
        text = f"Неделя №{contest_week} уже записана за участником {existing_winner}."
    await api.send_message(
        dm_chat_id,
        text,
        reply_to_message_id=message["message_id"],
        parse_mode=None,
    )


async def handle_delete_pokras_command(
    api: TelegramBotAPI,
    telethon_client,
    message: dict,
    command_text: str,
    entry: str | None,
    admin_chat_id: int | None,
    tz,
    log=print,
) -> None:
    """DM-only admin command: /deletepokras @username <visible work number>."""
    chat = message["chat"]
    dm_chat_id = chat["id"]
    admin = message.get("from") or {}
    admin_id = admin.get("id")
    if chat.get("type") != "private":
        return
    if entry is None or admin_chat_id is None:
        await api.send_message(
            dm_chat_id,
            "Не настроен единственный основной чат для удаления покраса.",
            reply_to_message_id=message["message_id"],
            parse_mode=None,
        )
        return
    if not admin_id or not await _can_manage_chat(api, admin_chat_id, admin, entry):
        await api.send_message(
            dm_chat_id,
            "Удалять покрасы из статистики могут только администраторы чата.",
            reply_to_message_id=message["message_id"],
            parse_mode=None,
        )
        return

    parts = command_text.strip().split()
    if len(parts) != 3 or not parts[2].isdigit() or int(parts[2]) < 1:
        await api.send_message(
            dm_chat_id,
            "Использование: /deletepokras @username 1",
            reply_to_message_id=message["message_id"],
            parse_mode=None,
        )
        return
    work_number = int(parts[2])
    tracked, _, _, _, _, _ = await stats.resolve_stat_target(
        telethon_client,
        entry,
        entry,
        parts[1],
        None,
        "",
        tz,
        log=log,
    )
    if tracked is None:
        await api.send_message(
            dm_chat_id,
            "Не нашёл участника. Укажите точный @username из статистики.",
            reply_to_message_id=message["message_id"],
            parse_mode=None,
        )
        return

    # /stat numbers only posts that can produce a Telegram deep link. Use the same
    # newest-first filter here so N always selects the number the administrator sees.
    linked_posts = [
        post
        for post in tracked.recent_figurine_posts
        if len(post) >= 2
        and stats.figurine_message_link(None, admin_chat_id, post[1]) is not None
    ]
    if not linked_posts:
        await api.send_message(
            dm_chat_id,
            f"У {tracked.display_name} нет работ с доступными ссылками.",
            reply_to_message_id=message["message_id"],
            parse_mode=None,
        )
        return
    if work_number > len(linked_posts):
        await api.send_message(
            dm_chat_id,
            f"У {tracked.display_name} доступны номера работ от 1 до {len(linked_posts)}.",
            reply_to_message_id=message["message_id"],
            parse_mode=None,
        )
        return

    _, selected_message_id = linked_posts[work_number - 1]
    stats.delete_figurine_submission(
        entry,
        tracked.user_id,
        int(selected_message_id),
        admin_id,
        _display_name(admin),
    )
    remaining = max(0, tracked.figurines_painted - 1)
    await api.send_message(
        dm_chat_id,
        f"Удалил работу №{work_number} пользователя {tracked.display_name} из статистики.\n"
        f"Фигурок осталось: {remaining}. Номера оставшихся работ обновились.",
        reply_to_message_id=message["message_id"],
        parse_mode=None,
    )


async def handle_replant_command(
    api: TelegramBotAPI,
    telethon_client,
    message: dict,
    entry: str | None,
    admin_chat_id: int | None,
    tz,
    log=print,
) -> None:
    """/replant — post the planting announcement to the chat and start the tree over.

    Exists because the planting date lives in the stats directory, which on a deployed
    host is a volume nothing else here can reach: without a command there is no way to
    re-run the opening post at all.

    Both halves happen together on purpose. Re-posting "сегодня мы посадили семечко"
    while the tree is already a metre tall would be a lie, and re-planting without
    posting would silently reset the chat's progress with no announcement.

    The announcement is sent through the same path as the morning digest and, like it, is
    never scheduled for deletion -- this is the post the whole thing opens with.
    """
    dm_chat_id = message["chat"]["id"]
    reply_to = message["message_id"]
    actor = message.get("from") or {}

    async def reply(text: str) -> None:
        try:
            await api.send_message(dm_chat_id, text, reply_to_message_id=reply_to, parse_mode=None)
        except Exception:
            log(f"[bot_listener] failed to answer /replant:\n{traceback.format_exc()}")

    if entry is None or admin_chat_id is None:
        await reply("Основной чат не настроен.")
        return
    if not await _is_chat_admin_or_privileged(api, admin_chat_id, actor):
        await reply("Запустить дерево заново могут только администраторы чата.")
        return

    today = datetime.now(stats.tree_digest_tz()).date()
    try:
        await api.send_message(admin_chat_id, tree.format_planting_message(), parse_mode="HTML")
    except Exception:
        log(f"[bot_listener] failed to post the planting message:\n{traceback.format_exc()}")
        await reply("Не удалось отправить пост в чат. Данные дерева остались без изменений.")
        return

    # Only after the announcement actually landed: a reset with no post would leave the
    # chat's progress silently zeroed.
    stats.replant_tree(entry, today)
    await reply("Готово: пост отправлен, отсчёт дерева начат заново с сегодняшнего дня.")


async def handle_plant_command(
    api: TelegramBotAPI,
    message: dict,
    entry: str | None,
    admin_chat_id: int | None,
    log=print,
) -> None:
    """/посадить_семечко (or /plant) — post the invitation and start collecting presses.

    Works from the chat itself and from the bot's DM alike. The invitation always goes to
    the chat; only the acknowledgement follows the admin back to wherever they typed it.

    Nothing is pinned here. The admin pins the invitation by hand, which is why the bot
    never asks for can_pin_messages -- and why nothing has to remember to unpin at 10:00.
    """
    where = message["chat"]["id"]
    reply_to = message["message_id"]
    actor = message.get("from") or {}

    async def reply(text: str) -> None:
        try:
            await api.send_message(where, text, reply_to_message_id=reply_to, parse_mode=None)
        except Exception:
            log(f"[bot_listener] failed to answer the plant command:\n{traceback.format_exc()}")

    if entry is None or admin_chat_id is None:
        await reply("Основной чат не настроен.")
        return
    if not await _is_chat_admin_or_privileged(api, admin_chat_id, actor):
        await reply("Посадку может открыть только администратор чата.")
        return
    if stats.planting_is_open(entry):
        signed = len(stats.planters(entry))
        await reply(f"Посадка уже открыта. Сейчас в списке: {signed}. Перекличка в 10:00.")
        return
    if stats.tree_planted_on(entry) is not None:
        await reply("Дерево уже растёт. Чтобы начать заново, используй /replant в личке.")
        return

    now = datetime.now(stats.tree_digest_tz())
    # Opened before 10:00 and the roll call is today, otherwise tomorrow morning. No
    # minimum window is enforced: the admin chooses when to open it, and the invitation
    # says which morning it closes, so a short one is a decision rather than a surprise.
    same_day = now.hour < stats.TREE_DIGEST_HOUR
    try:
        sent = await api.send_message(
            admin_chat_id,
            tree.format_seed_ceremony_message(same_day=same_day),
            parse_mode="HTML",
            reply_markup=tree.seed_keyboard(f"{PLANT_CALLBACK_PREFIX}:join"),
        )
    except Exception:
        log(f"[bot_listener] failed to post the planting invitation:\n{traceback.format_exc()}")
        await reply("Приглашение не отправилось. Попробуй ещё раз.")
        return

    stats.open_planting(entry, admin_chat_id, sent["message_id"], now.date())
    await reply(
        "Посадка открыта. Закрепи приглашение — перекличка "
        + ("сегодня" if same_day else "завтра")
        + " в 10:00."
    )


async def handle_plant_callback(
    api: TelegramBotAPI, callback: dict, entry: str | None, log=print
) -> None:
    """The planting button. Answered as a toast on the presser's own screen and nowhere
    else: 190 members tapping a button must not become 190 messages in the chat."""
    callback_id = callback["id"]
    presser = callback.get("from") or {}
    if entry is None or not stats.planting_is_open(entry):
        await api.answer_callback_query(callback_id, "Эта посадка уже завершена.")
        return
    try:
        added = stats.add_planter(
            entry, presser.get("id"), _display_name(presser), presser.get("username"),
        )
    except Exception:
        log(f"[bot_listener] failed to record a planter:\n{traceback.format_exc()}")
        await api.answer_callback_query(callback_id, "Не удалось добавить тебя в список. Попробуй ещё раз.")
        return
    await api.answer_callback_query(
        callback_id, tree.SEED_BUTTON_ACK if added else tree.SEED_BUTTON_ALREADY
    )


async def handle_plant_reminder_command(
    api: TelegramBotAPI,
    message: dict,
    entry: str | None,
    admin_chat_id: int | None,
    log=print,
) -> None:
    """Post a count-only reminder for the currently open planting ceremony."""
    where = message["chat"]["id"]
    reply_to = message["message_id"]
    actor = message.get("from") or {}

    async def reply(text: str) -> None:
        try:
            await api.send_message(where, text, reply_to_message_id=reply_to, parse_mode=None)
        except Exception:
            log(f"[bot_listener] failed to answer the plant reminder command:\n{traceback.format_exc()}")

    if entry is None or admin_chat_id is None:
        await reply("Основной чат не настроен.")
        return
    if not await _is_chat_admin_or_privileged(api, admin_chat_id, actor):
        await reply("Напоминание о посадке может отправить только администратор чата.")
        return
    if not stats.planting_is_open(entry):
        await reply("Посадка сейчас не открыта. Сначала используй /plant.")
        return

    try:
        await api.send_message(
            admin_chat_id,
            tree.format_seed_reminder_message(len(stats.planters(entry))),
            parse_mode="HTML",
            reply_markup=tree.seed_keyboard(f"{PLANT_CALLBACK_PREFIX}:join"),
        )
    except Exception:
        log(f"[bot_listener] failed to post the planting reminder:\n{traceback.format_exc()}")
        await reply("Напоминание не отправилось. Попробуй ещё раз.")
        return

    await reply("Напоминание о посадке отправлено.")


# --- Покер ----------------------------------------------------------------------------
#
# The rules, the pot maths and every rendered string live in poker.py; everything here is
# Telegram. One table per chat, its state on disk, so a redeploy mid-hand does not eat
# anybody's chips.


def _poker_live_message(table: dict) -> int | None:
    """The id of the message that currently carries the table's buttons.

    Newest first: a finished hand's showdown message owns them, otherwise the street being
    played, otherwise the lobby. Getting this order wrong leaves a live keyboard on a
    message the game has already moved past.
    """
    hand = table.get("hand") or {}
    return (
        hand.get("showdown_message_id")
        or hand.get("message_id")
        or table.get("lobby_message_id")
    )


async def _poker_deal_cards(api: TelegramBotAPI, table: dict, log=print) -> list[dict]:
    """DM each player their two cards. Returns whoever could not be reached.

    A private chat's id IS the user id, so no lookup is needed -- but a bot cannot open a
    conversation the member has never started, which is why joining the table checks
    reachability up front. Getting here with an unreachable player means they blocked the
    bot mid-session; the group message names them rather than letting them play blind.
    """
    unreachable = []
    for player in table["players"]:
        try:
            await api.send_message(
                int(player["user_id"]),
                poker.format_hole_cards(table, player["user_id"]),
                parse_mode="HTML",
            )
        except Exception:
            unreachable.append(player)
            log(f"[bot_listener] could not deal cards to {player['user_id']}:\n{traceback.format_exc()}")
    return unreachable


async def _poker_post_street(api: TelegramBotAPI, entry: str, table: dict, log=print) -> None:
    """Send the message a street is played on and remember it for in-place edits.

    A new message per street rather than one edited all hand: an edit is silent, and a
    table where the flop arrives without anything appearing in the chat is a table nobody
    notices it is their turn at.
    """
    sent = await api.send_message(
        table["chat_id"],
        poker.format_hand(table),
        parse_mode="HTML",
        reply_markup=poker.action_keyboard(table),
    )
    table["hand"]["message_id"] = sent.get("message_id")
    poker.save_table(entry, table)


async def _poker_retire_message(api: TelegramBotAPI, chat_id, message_id, log=print) -> None:
    """Leave a finished round's message exactly as it is but take its buttons away, so a
    scroll back up cannot offer a live action on a hand that has moved on.

    Buttons only, never the text: rewriting it would mean reproducing what that message
    said at the time, and the state it was rendered from has already moved on.
    """
    if not message_id:
        return
    try:
        await api.edit_message_reply_markup(chat_id, message_id, poker.no_keyboard())
    except Exception:
        log(f"[bot_listener] failed to retire a poker message:\n{traceback.format_exc()}")


async def _poker_start_hand(api: TelegramBotAPI, entry: str, table: dict, log=print) -> None:
    """Deal, DM the cards, and open the first betting round in the chat."""
    poker.start_hand(table)
    poker.save_table(entry, table)
    unreachable = await _poker_deal_cards(api, table, log=log)
    if unreachable:
        names = ", ".join(poker.player_label(player) for player in unreachable)
        try:
            await api.send_message(
                table["chat_id"],
                f"Не удалось отправить карты в личку: {names}. Откройте чат с ботом и нажмите Start.",
                parse_mode="HTML",
            )
        except Exception:
            log(f"[bot_listener] failed to report undelivered poker cards:\n{traceback.format_exc()}")
    await _poker_post_street(api, entry, table, log=log)


async def _poker_close_table(api: TelegramBotAPI, entry: str, table: dict, note: str, log=print) -> None:
    """Forget the table, take the live keyboard away, and post the session's standings."""
    poker.clear_table(entry)
    await _poker_retire_message(api, table["chat_id"], _poker_live_message(table), log=log)
    try:
        await api.send_message(
            table["chat_id"], poker.format_session_over(table, note), parse_mode="HTML",
        )
    except Exception:
        log(f"[bot_listener] failed to post the poker session summary:\n{traceback.format_exc()}")


async def handle_poker_command(
    api: TelegramBotAPI,
    message: dict,
    command_text: str,
    entry: str | None,
    admin_chat_id: int | None,
    log=print,
) -> None:
    """/poker -- open a table. Only a holder of the «Диллер» badge may do it.

    "/poker стоп" closes the current one. That exists because the button that normally
    does it lives on a message in the chat, and a table opened yesterday is a table whose
    message has scrolled away -- without a command form, a chat could end up unable to
    open a new table and unable to close the old one.

    Like the planting command this works from the chat and from the bot's DM alike: the
    table always goes to the chat, and only the acknowledgement follows whoever typed it.
    """
    where = message["chat"]["id"]
    reply_to = message["message_id"]
    actor = message.get("from") or {}

    async def reply(text: str) -> None:
        try:
            await api.send_message(where, text, reply_to_message_id=reply_to, parse_mode=None)
        except Exception:
            log(f"[bot_listener] failed to answer the poker command:\n{traceback.format_exc()}")

    if entry is None or admin_chat_id is None:
        await reply("Основной чат не настроен.")
        return

    parts = (command_text or "").split(maxsplit=1)
    argument = parts[1].strip().casefold() if len(parts) > 1 else ""
    existing = poker.load_table(entry)

    if argument in POKER_STOP_WORDS:
        # Checked before the badge gate: an administrator clearing a stuck table may not
        # be a dealer at all, and requiring the badge to close would recreate the dead end
        # this branch exists to remove.
        if existing is None:
            await reply("Открытого стола нет.")
            return
        if not (
            poker.is_table_dealer(existing, actor.get("id"))
            or await _is_chat_admin_or_privileged(api, existing["chat_id"], actor)
        ):
            await reply("Закрыть стол может только его диллер или администратор чата.")
            return
        await _poker_close_table(api, entry, existing, "Стол закрыт командой.", log=log)
        await reply("Стол закрыт.")
        return

    if not poker.is_dealer(entry, actor.get("id")):
        await reply("Открыть стол может только участник со значком «Диллер».")
        return

    if existing is not None:
        await reply(
            "Стол уже открыт. Закрой его кнопкой «Завершить стол» "
            "или командой /poker стоп, и открывай новый."
        )
        return

    table = poker.open_table(admin_chat_id, actor.get("id"), _display_name(actor))
    try:
        sent = await api.send_message(
            admin_chat_id,
            poker.format_lobby(table),
            parse_mode="HTML",
            reply_markup=poker.lobby_keyboard(table),
        )
    except Exception:
        log(f"[bot_listener] failed to open a poker table:\n{traceback.format_exc()}")
        await reply("Стол не открылся. Попробуй ещё раз.")
        return

    table["lobby_message_id"] = sent.get("message_id")
    poker.save_table(entry, table)
    if where != admin_chat_id:
        await reply("Стол открыт в чате.")


async def _poker_join(api: TelegramBotAPI, entry: str, table: dict, callback_id: str, presser: dict, log=print) -> None:
    """One member takes a seat. Pressing twice is answered, not seated twice."""
    result = poker.seat(
        table, presser.get("id"), _display_name(presser), presser.get("username")
    )
    if result != "seated":
        await api.answer_callback_query(callback_id, {
            "already": "Ты уже за столом.",
            "full": f"За столом уже {poker.MAX_PLAYERS} игроков.",
            "closed": "Игра уже началась.",
        }[result])
        return

    # Reachability is checked HERE, before the spinner stops, and deliberately so: cards
    # are dealt privately, and a bot cannot write to somebody who has never started it.
    # Finding that out at the deal would mean a player sitting through a hand blind.
    try:
        await api.send_message(
            int(presser["id"]),
            "Ты за покерным столом. Карты придут сюда, как только диллер начнёт игру.",
            parse_mode=None,
        )
    except Exception:
        table["players"] = [p for p in table["players"] if p["user_id"] != str(presser.get("id"))]
        poker.save_table(entry, table)
        await api.answer_callback_query(
            callback_id, "Сначала открой чат со мной и нажми Start — туда придут карты.",
        )
        return

    poker.save_table(entry, table)
    await api.answer_callback_query(callback_id, "Ты в игре.")
    try:
        await api.edit_message_text(
            table["chat_id"], table["lobby_message_id"], poker.format_lobby(table),
            reply_markup=poker.lobby_keyboard(table), parse_mode="HTML",
        )
    except Exception:
        log(f"[bot_listener] failed to update the poker lobby:\n{traceback.format_exc()}")


async def handle_poker_callback(
    api: TelegramBotAPI,
    callback: dict,
    entry: str | None,
    log=print,
) -> None:
    """Every poker button in the group.

    Telegram has no per-viewer keyboards, so everybody sees everybody's buttons and the
    wrong person pressing is not an error case but the normal case: it is answered with a
    toast on that person's own screen and changes nothing at all.
    """
    parsed = poker.parse_callback(callback.get("data") or "")
    if parsed is None:
        return
    action, table_id, hand_no, street = parsed
    callback_id = callback["id"]
    presser = callback.get("from") or {}
    if presser.get("id") is None:
        await api.answer_callback_query(callback_id, "Не удалось определить, кто нажал.")
        return

    table = poker.load_table(entry) if entry else None
    if table is None or table.get("table_id") != table_id:
        await api.answer_callback_query(callback_id, "Этот стол уже закрыт.")
        return

    if action == "join":
        if table.get("phase") != poker.PHASE_LOBBY:
            await api.answer_callback_query(callback_id, "Игра уже началась.")
            return
        await _poker_join(api, entry, table, callback_id, presser, log=log)
        return

    if action == "end":
        # The one action a chat administrator can also take: without it a dealer who has
        # left, muted the chat or simply gone to bed would wedge the table forever, and
        # no new one can be opened while it exists.
        # The table carries its own chat id, so this works after a restart too -- unlike
        # known_chat_ids, which is only populated by messages the process has itself seen.
        allowed = poker.is_table_dealer(table, presser.get("id")) or (
            await _is_chat_admin_or_privileged(api, table["chat_id"], presser)
        )
        if not allowed:
            await api.answer_callback_query(callback_id, "Закрыть стол может только диллер.")
            return
        await api.answer_callback_query(callback_id, "Стол закрыт.")
        await _poker_close_table(api, entry, table, "Диллер закрыл стол.", log=log)
        return

    if action in ("start", "next"):
        if not poker.is_table_dealer(table, presser.get("id")):
            await api.answer_callback_query(callback_id, "Начать игру может только диллер.")
            return
        expected = poker.PHASE_LOBBY if action == "start" else poker.PHASE_SHOWDOWN
        if table.get("phase") != expected:
            await api.answer_callback_query(callback_id, "Сейчас это не нужно.")
            return
        if poker.players_with_chips(table) < poker.MIN_PLAYERS:
            await api.answer_callback_query(
                callback_id, f"Нужно хотя бы {poker.MIN_PLAYERS} игрока с фишками.",
            )
            return
        await api.answer_callback_query(callback_id)
        # Read before the deal: starting a hand replaces `hand`, and with it the id of the
        # message whose buttons have to be taken away.
        previous_id = _poker_live_message(table)
        try:
            await _poker_start_hand(api, entry, table, log=log)
        except ValueError as error:
            log(f"[bot_listener] refused to start a poker hand: {error}")
            return
        except Exception:
            log(f"[bot_listener] failed to start a poker hand:\n{traceback.format_exc()}")
            return
        await _poker_retire_message(api, table["chat_id"], previous_id, log=log)
        return

    # Everything left is a betting action, and every one of them is refused unless it is
    # this person's turn on this exact street of this exact hand.
    if table.get("phase") != poker.PHASE_HAND or not table.get("hand"):
        await api.answer_callback_query(callback_id, "Сейчас нет активной раздачи.")
        return
    if hand_no != table["hand_no"] or street != table["hand"]["street"]:
        await api.answer_callback_query(callback_id, "Эта раздача уже сыграна.")
        return

    street_before = table["hand"]["street"]
    message_before = table["hand"].get("message_id")
    ok, problem = poker.act(table, presser.get("id"), action)
    if not ok:
        await api.answer_callback_query(callback_id, problem)
        return

    poker.save_table(entry, table)
    await api.answer_callback_query(callback_id)

    if poker.hand_is_over(table):
        await _poker_retire_message(api, table["chat_id"], message_before, log=log)
        try:
            sent = await api.send_message(
                table["chat_id"], poker.format_showdown(table),
                parse_mode="HTML", reply_markup=poker.showdown_keyboard(table),
            )
            # Remembered so the next hand can take these buttons away: without it the
            # finished hand keeps a live "Следующая раздача" that would deal a second one.
            table["hand"]["showdown_message_id"] = sent.get("message_id")
            poker.save_table(entry, table)
        except Exception:
            log(f"[bot_listener] failed to post a poker showdown:\n{traceback.format_exc()}")
        return

    if table["hand"]["street"] != street_before:
        await _poker_retire_message(api, table["chat_id"], message_before, log=log)
        await _poker_post_street(api, entry, table, log=log)
        return

    try:
        await api.edit_message_text(
            table["chat_id"], message_before, poker.format_hand(table),
            reply_markup=poker.action_keyboard(table), parse_mode="HTML",
        )
    except Exception:
        log(f"[bot_listener] failed to update a poker hand:\n{traceback.format_exc()}")


async def handle_send_command(
    api: TelegramBotAPI,
    message: dict,
    command_text: str,
    entry: str | None,
    admin_chat_id: int | None,
    log=print,
) -> None:
    """DM-only admin command: /send <text> posts plain text to the home chat."""
    chat = message["chat"]
    if chat.get("type") != "private":
        return
    dm_chat_id = chat["id"]
    reply_to = message["message_id"]
    actor = message.get("from") or {}
    text = command_text[len(SEND_COMMAND):].strip()

    async def reply(text: str) -> None:
        try:
            await api.send_message(dm_chat_id, text, reply_to_message_id=reply_to, parse_mode=None)
        except Exception:
            log(f"[bot_listener] failed to answer /send:\n{traceback.format_exc()}")

    if entry is None or admin_chat_id is None:
        await reply("Основной чат не настроен.")
        return
    if not await _is_chat_admin_or_privileged(api, admin_chat_id, actor):
        await reply("Отправлять сообщения в чат могут только администраторы.")
        return
    if not text:
        await reply("Использование: /send текст сообщения")
        return

    try:
        await api.send_message(admin_chat_id, text, parse_mode=None)
    except Exception:
        log(f"[bot_listener] failed to send an admin message:\n{traceback.format_exc()}")
        await reply("Не удалось отправить сообщение в чат. Попробуй ещё раз.")
        return
    await reply("Сообщение отправлено в чат.")


async def _send_preview(
    api: TelegramBotAPI, dm_chat_id: int, preview_id: str, log=print
) -> bool:
    """Render one sample into the DM. True when it existed and went out."""
    text = preview.render(preview_id)
    if text is None:
        return False
    keyboard = preview.keyboard_for(preview_id)
    # Same rule the real post follows (see _consume_stats_digests): the picture is
    # optional, and a failure to send it must still leave the admin with the text.
    image = preview.image_for(preview_id)
    if image is not None and len(text) <= CAPTION_LIMIT:
        try:
            await api.send_photo_file(
                dm_chat_id, image, caption=text, parse_mode="HTML", reply_markup=keyboard,
            )
            return True
        except Exception:
            log(f"[bot_listener] failed to preview {image.name}, falling back to text:\n{traceback.format_exc()}")
    await api.send_message(dm_chat_id, text, parse_mode="HTML", reply_markup=keyboard)
    return True


async def _post_group_test(
    api: TelegramBotAPI, dm_chat_id: int, admin_chat_id: int, entry: str, log=print
) -> None:
    """Post a neutral button test and start a fresh, persistent participant list."""
    sent = await api.send_message(
        admin_chat_id,
        preview.GROUP_TEST_TEXT,
        parse_mode=None,
        reply_markup=preview.group_test_keyboard(),
    )
    try:
        stats.open_preview_button_test(entry, admin_chat_id, sent["message_id"])
    except Exception:
        # Do not leave a visible test whose button can never record anyone.
        await api.delete_message(admin_chat_id, sent["message_id"])
        raise
    text, keyboard = preview.group_test_sent_view(admin_chat_id, sent["message_id"])
    await api.send_message(dm_chat_id, text, parse_mode="HTML", reply_markup=keyboard)


async def handle_preview_command(
    api: TelegramBotAPI,
    telethon_client,
    message: dict,
    command_text: str,
    entry: str | None,
    known_chat_ids: dict[str, int],
    log=print,
) -> None:
    """/preview — look at the scheduled and one-off posts without waiting for them.

        /preview              the menu, one button per message
        /preview rollcall     one sample straight away
        /preview test_button  post the real thing to the chat, with an undo

    DM-only, and NOT gated on being an administrator. The gate was removed deliberately:
    it had to resolve the home chat before it could ask Telegram for the administrator
    list, and resolving goes through the Telethon session, which when unwell waits rather
    than failing -- so the check was the only part of this that could hang, on a command
    whose entire job is to write a message back into the DM it was typed in.

    Still DM-only, because a preview of the planting invitation loose in the group would
    be indistinguishable from the real invitation. The command is not registered in the
    menu and not advertised anywhere.
    """
    dm_chat_id = message["chat"]["id"]
    reply_to = message["message_id"]
    actor = message.get("from") or {}

    async def reply(text: str, parse_mode: str | None = None, reply_markup=None) -> None:
        try:
            await api.send_message(
                dm_chat_id, text, reply_to_message_id=reply_to,
                parse_mode=parse_mode, reply_markup=reply_markup,
            )
        except Exception:
            log(f"[bot_listener] failed to answer /preview:\n{traceback.format_exc()}")

    argument = command_text[len(PREVIEW_COMMAND):].strip().lower()
    if not argument:
        text, keyboard = preview.menu_view()
        await reply(text, parse_mode="HTML", reply_markup=keyboard)
        return

    try:
        if argument == preview.GROUP_TEST_ID:
            admin_chat_id = (
                await _resolve_chat_id(telethon_client, entry, known_chat_ids, log=log)
                if entry
                else None
            )
            if admin_chat_id is None:
                await reply("Не удалось найти основной чат. Проверь подключение бота.")
                return
            await _post_group_test(api, dm_chat_id, admin_chat_id, entry, log=log)
            return
        if not await _send_preview(api, dm_chat_id, argument, log=log):
            await reply(preview.unknown_preview_text(), parse_mode="HTML")
    except Exception:
        log(f"[bot_listener] /preview {argument} failed:\n{traceback.format_exc()}")
        await reply("Не удалось отправить превью.")


async def handle_preview_callback(
    api: TelegramBotAPI,
    telethon_client,
    callback: dict,
    entry: str | None,
    known_chat_ids: dict[str, int],
    log=print,
) -> None:
    """Buttons from the /preview menu, plus the button on the test post itself.

    The spinner is stopped FIRST, before anything that can be slow, and errors arrive as a
    DM rather than as a toast -- a fair trade for a button that always responds.

    There is no administrator check here, by decision. It was the only thing on this path
    that could hang: it had to resolve the home chat through the Telethon session before
    it could ask Telegram for the administrator list, and an unwell session waits rather
    than failing. What the buttons actually do is write into the DM they are already in,
    and the menu is only reachable from a DM command that is registered nowhere.
    """
    data = callback.get("data") or ""
    callback_id = callback["id"]
    presser = callback.get("from") or {}
    callback_message = callback.get("message") or {}
    dm_chat_id = callback_message.get("chat", {}).get("id")
    callback_message_id = callback_message.get("message_id")

    deletion = preview.parse_delete_callback(data)
    publication = preview.parse_publish_callback(data) if deletion is None else None
    preview_id = (
        preview.parse_callback(data)
        if deletion is None and publication is None
        else None
    )

    # This is the button on the neutral post in the group. Unlike the sample planting
    # buttons rendered inside the DM, it really records each member once.
    if preview_id == preview.GROUP_TEST_JOIN_ID:
        try:
            result = (
                stats.add_preview_button_tester(
                    entry,
                    dm_chat_id,
                    callback_message_id,
                    presser.get("id"),
                    _display_name(presser),
                    presser.get("username"),
                )
                if entry is not None
                and dm_chat_id is not None
                and callback_message_id is not None
                and presser.get("id") is not None
                else None
            )
        except Exception:
            log(f"[bot_listener] failed to record a test-button press:\n{traceback.format_exc()}")
            await api.answer_callback_query(
                callback_id, "Не удалось добавить тебя в тестовый список. Попробуй ещё раз."
            )
            return
        acknowledgement = (
            preview.GROUP_TEST_BUTTON_ACK
            if result is True
            else preview.GROUP_TEST_BUTTON_ALREADY
            if result is False
            else preview.GROUP_TEST_BUTTON_CLOSED
        )
        await api.answer_callback_query(callback_id, acknowledgement)
        return

    # A sample planting button from a DM preview: no rights needed, nothing is recorded,
    # and everybody who taps it gets told what it is.
    if preview_id == preview.SAMPLE_BUTTON_ID:
        await api.answer_callback_query(callback_id, tree.SEED_BUTTON_TEST_ACK)
        return

    await api.answer_callback_query(callback_id)
    if dm_chat_id is None:
        return

    async def say(text: str) -> None:
        try:
            await api.send_message(dm_chat_id, text, parse_mode=None)
        except Exception:
            log(f"[bot_listener] failed to answer a preview button:\n{traceback.format_exc()}")

    try:
        # Deleting the test post and rendering a sample into this DM both already know
        # every id they need -- the DM's from the callback, the test post's from the
        # button. Only posting a NEW test to the group needs the group resolved, so that
        # is the only branch that reaches for it.
        if publication is not None:
            chat_id, message_id = publication
            testers = (
                stats.preview_button_testers(entry, chat_id, message_id)
                if entry is not None
                else None
            )
            if testers is None:
                await say("Этот тест уже завершён.")
                return
            for chunk in preview.group_test_result_chunks(testers):
                await api.send_message(chat_id, chunk, parse_mode="HTML")
            await say("Список нажавших опубликован в общем чате.")
            return

        if deletion is not None:
            chat_id, message_id = deletion
            await api.delete_message(chat_id, message_id)
            if entry is not None:
                stats.close_preview_button_test(entry, chat_id, message_id)
            await say("Тестовый пост удалён из общего чата.")
            return

        if not preview_id:
            return

        if preview_id == preview.GROUP_TEST_ID:
            admin_chat_id = (
                await _resolve_chat_id(telethon_client, entry, known_chat_ids, log=log)
                if entry
                else None
            )
            if admin_chat_id is None:
                await say("Не удалось найти основной чат. Проверь подключение бота.")
                return
            await _post_group_test(api, dm_chat_id, admin_chat_id, entry, log=log)
            return

        if not await _send_preview(api, dm_chat_id, preview_id, log=log):
            await say("Нет такого превью.")
    except Exception:
        log(f"[bot_listener] preview button {preview_id} failed:\n{traceback.format_exc()}")
        await say("Не удалось показать превью. Попробуй ещё раз.")


def _button_builder_flow(
    flows: dict[str, dict],
    flow_id: str,
    chat_id,
    user_id,
) -> dict | None:
    flow = flows.get(flow_id)
    if flow is None:
        return None
    if time.monotonic() - flow["created_at"] > BUTTON_BUILDER_FLOW_TTL_SECONDS:
        flows.pop(flow_id, None)
        return None
    if flow.get("chat_id") != chat_id or flow.get("user_id") != user_id:
        return None
    return flow


def _button_builder_buttons(flow: dict) -> list[dict]:
    return [{"text": text, "count": 0} for text in flow.get("button_texts", [])]


async def _button_builder_force_reply(
    api: TelegramBotAPI,
    flow: dict,
    text: str,
    reply_to_message_id: int | None,
) -> None:
    prompt = await api.send_message(
        flow["chat_id"],
        text,
        reply_to_message_id=reply_to_message_id,
        reply_markup={"force_reply": True, "selective": True},
        parse_mode=None,
    )
    flow["prompt_message_id"] = prompt.get("message_id") if prompt else None


async def _send_button_builder_preview(
    api: TelegramBotAPI,
    flow_id: str,
    flow: dict,
    reply_to_message_id: int | None,
) -> None:
    buttons = _button_builder_buttons(flow)
    with_photo = bool(flow.get("photo_file_id"))
    button_builder.validate_rendered_length(flow["message_text"], buttons, with_photo)
    rendered = button_builder.render_post(flow["message_text"], buttons)
    keyboard = button_builder.preview_keyboard(flow_id, buttons)
    if with_photo:
        sent = await api.send_photo(
            flow["chat_id"],
            flow["photo_file_id"],
            rendered,
            reply_to_message_id=reply_to_message_id,
            reply_markup=keyboard,
            parse_mode=None,
        )
    else:
        sent = await api.send_message(
            flow["chat_id"],
            rendered,
            reply_to_message_id=reply_to_message_id,
            reply_markup=keyboard,
            parse_mode=None,
        )
    flow["awaiting"] = "ready"
    flow["preview_message_id"] = sent.get("message_id") if sent else None


async def handle_button_builder_command(
    api: TelegramBotAPI,
    message: dict,
    entry: str | None,
    admin_chat_id: int | None,
    flows: dict[str, dict],
) -> None:
    """/buttons — start an admin-only DM flow for a one-to-five-button counter post."""
    chat = message["chat"]
    if chat.get("type") != "private":
        return
    dm_chat_id = chat["id"]
    actor = message.get("from") or {}
    actor_id = actor.get("id")
    if entry is None or admin_chat_id is None:
        await api.send_message(
            dm_chat_id,
            "Основной чат не настроен.",
            reply_to_message_id=message["message_id"],
            parse_mode=None,
        )
        return
    if not actor_id or not await _is_chat_admin_or_privileged(api, admin_chat_id, actor):
        await api.send_message(
            dm_chat_id,
            "Публиковать сообщения с кнопками могут только администраторы чата.",
            reply_to_message_id=message["message_id"],
            parse_mode=None,
        )
        return

    for old_flow_id, old_flow in list(flows.items()):
        if old_flow.get("chat_id") == dm_chat_id and old_flow.get("user_id") == actor_id:
            flows.pop(old_flow_id, None)
    flow_id = uuid.uuid4().hex[:10]
    flow = {
        "created_at": time.monotonic(),
        "chat_id": dm_chat_id,
        "admin_chat_id": admin_chat_id,
        "entry": entry,
        "user_id": actor_id,
        "awaiting": "message",
        "message_text": None,
        "button_count": None,
        "button_texts": [],
        "photo_file_id": None,
    }
    flows[flow_id] = flow
    await _button_builder_force_reply(
        api,
        flow,
        "Отправь текст будущего сообщения.\n"
        "Картинку можно будет добавить отдельным шагом. Для отмены: /cancel",
        message["message_id"],
    )


async def handle_button_builder_text_input(
    api: TelegramBotAPI,
    message: dict,
    flows: dict[str, dict],
    log=print,
) -> bool:
    """Consume the text, button labels or optional photo requested by /buttons."""
    chat_id = message["chat"]["id"]
    actor = message.get("from") or {}
    actor_id = actor.get("id")
    replied_message_id = (message.get("reply_to_message") or {}).get("message_id")
    flow_pair = next(
        (
            (flow_id, flow)
            for flow_id, flow in flows.items()
            if flow.get("chat_id") == chat_id
            and flow.get("user_id") == actor_id
            and flow.get("awaiting") in ("message", "button_text", "photo")
            and flow.get("prompt_message_id") == replied_message_id
            and time.monotonic() - flow["created_at"] <= BUTTON_BUILDER_FLOW_TTL_SECONDS
        ),
        None,
    )
    if flow_pair is None:
        return False
    flow_id, flow = flow_pair
    raw_text = (message.get("text") or message.get("caption") or "").strip()
    if raw_text.lower() in ("/cancel", "отмена"):
        flows.pop(flow_id, None)
        await api.send_message(
            chat_id,
            "Конструктор закрыт.",
            reply_to_message_id=message["message_id"],
            parse_mode=None,
        )
        return True

    try:
        if flow["awaiting"] == "message":
            flow["message_text"] = button_builder.validate_message_text(raw_text)
            flow["awaiting"] = "count"
            await api.send_message(
                chat_id,
                "Сколько кнопок добавить?",
                reply_to_message_id=message["message_id"],
                reply_markup=button_builder.choose_count_keyboard(flow_id),
                parse_mode=None,
            )
            return True

        if flow["awaiting"] == "button_text":
            label = button_builder.validate_button_text(raw_text)
            flow["button_texts"].append(label)
            if len(flow["button_texts"]) < int(flow["button_count"]):
                next_number = len(flow["button_texts"]) + 1
                await _button_builder_force_reply(
                    api,
                    flow,
                    f"Отправь текст кнопки №{next_number}.",
                    message["message_id"],
                )
            else:
                flow["awaiting"] = "photo_choice"
                await api.send_message(
                    chat_id,
                    "Добавить картинку к сообщению?",
                    reply_to_message_id=message["message_id"],
                    reply_markup=button_builder.choose_photo_keyboard(flow_id),
                    parse_mode=None,
                )
            return True

        photos = message.get("photo") or []
        if not photos:
            await _button_builder_force_reply(
                api,
                flow,
                "Пришли картинку как фото. Для отмены: /cancel",
                message["message_id"],
            )
            return True
        button_builder.validate_rendered_length(
            flow["message_text"], _button_builder_buttons(flow), with_photo=True
        )
        flow["photo_file_id"] = photos[-1]["file_id"]
        await _send_button_builder_preview(api, flow_id, flow, message["message_id"])
        return True
    except ValueError as error:
        prompt = (
            "Пришли картинку как фото. Для отмены: /cancel"
            if flow.get("awaiting") == "photo"
            else str(error)
        )
        if flow.get("awaiting") == "photo":
            await api.send_message(
                chat_id, str(error), reply_to_message_id=message["message_id"], parse_mode=None
            )
        await _button_builder_force_reply(api, flow, prompt, message["message_id"])
        return True
    except Exception:
        log(f"[bot_listener] button-builder text step failed:\n{traceback.format_exc()}")
        await api.send_message(
            chat_id,
            "Не удалось продолжить конструктор. Запусти /buttons ещё раз.",
            reply_to_message_id=message["message_id"],
            parse_mode=None,
        )
        flows.pop(flow_id, None)
        return True


async def _edit_button_builder_control(
    api: TelegramBotAPI,
    callback_message: dict,
    text: str,
    reply_markup: dict | None,
) -> None:
    chat_id = (callback_message.get("chat") or {}).get("id")
    message_id = callback_message.get("message_id")
    if chat_id is None or message_id is None:
        return
    if callback_message.get("photo"):
        await api.edit_message_caption(
            chat_id, message_id, text, reply_markup=reply_markup, parse_mode=None
        )
    else:
        await api.edit_message_text(
            chat_id, message_id, text, reply_markup=reply_markup, parse_mode=None
        )


async def handle_button_builder_callback(
    api: TelegramBotAPI,
    callback: dict,
    entry: str | None,
    flows: dict[str, dict],
    log=print,
) -> None:
    parsed = button_builder.parse_callback(callback.get("data") or "")
    if parsed is None:
        return
    action, item_id, argument = parsed
    callback_id = callback.get("id")
    callback_message = callback.get("message") or {}
    chat = callback_message.get("chat") or {}
    actor = callback.get("from") or {}
    actor_id = actor.get("id")

    # Public buttons remain valid across restarts, so their state lives on disk rather
    # than in the short-lived builder flow.
    if action == "press":
        result = None
        try:
            if (
                entry is not None
                and argument is not None
                and chat.get("id") is not None
                and callback_message.get("message_id") is not None
                and actor_id is not None
            ):
                result = stats.record_button_post_vote(
                    entry,
                    item_id,
                    chat["id"],
                    callback_message["message_id"],
                    argument,
                    actor_id,
                )
        except Exception:
            log(f"[bot_listener] failed to count a generated-button press:\n{traceback.format_exc()}")
        answer = (
            "Голос учтён."
            if result is not None and result[0] == "added"
            else "Ты уже голосовал в этом сообщении."
            if result is not None
            else "Эта кнопка уже неактивна."
        )
        await api.answer_callback_query(callback_id, answer)
        return

    if action == "delete":
        post = stats.button_post(entry, item_id) if entry is not None else None
        if post is None:
            await api.answer_callback_query(callback_id, "Этот пост уже удалён.")
            return
        if str(post.get("created_by_id")) != str(actor_id):
            await api.answer_callback_query(callback_id, "Удалить пост может только его автор.")
            return
        await api.answer_callback_query(callback_id)
        await api.delete_message(post["chat_id"], post["message_id"])
        stats.delete_button_post(entry, item_id, post["chat_id"], post["message_id"])
        try:
            await _edit_button_builder_control(
                api, callback_message, "Пост удалён из общего чата.", reply_markup=None
            )
        except Exception:
            log(f"[bot_listener] failed to update generated-post delete control:\n{traceback.format_exc()}")
        return

    flow = _button_builder_flow(flows, item_id, chat.get("id"), actor_id)
    if flow is None:
        await api.answer_callback_query(callback_id, "Конструктор устарел или принадлежит другому человеку.")
        return
    if action == "sample":
        await api.answer_callback_query(callback_id, "Это предпросмотр — счётчик не изменился.")
        return
    await api.answer_callback_query(callback_id)

    try:
        if action == "cancel":
            flows.pop(item_id, None)
            await _edit_button_builder_control(
                api, callback_message, "Конструктор закрыт.", reply_markup=None
            )
            return
        if (
            action == "count"
            and argument is not None
            and 1 <= argument <= button_builder.MAX_BUTTONS
            and flow.get("awaiting") == "count"
        ):
            flow["button_count"] = argument
            flow["button_texts"] = []
            flow["awaiting"] = "button_text"
            await _button_builder_force_reply(
                api, flow, "Отправь текст кнопки №1.", callback_message.get("message_id")
            )
            return
        if action == "photo" and argument in (0, 1) and flow.get("awaiting") == "photo_choice":
            if argument == 1:
                button_builder.validate_rendered_length(
                    flow["message_text"], _button_builder_buttons(flow), with_photo=True
                )
                flow["awaiting"] = "photo"
                await _button_builder_force_reply(
                    api,
                    flow,
                    "Пришли картинку как фото. Для отмены: /cancel",
                    callback_message.get("message_id"),
                )
            else:
                await _send_button_builder_preview(
                    api, item_id, flow, callback_message.get("message_id")
                )
            return
        if action == "send" and flow.get("awaiting") == "ready":
            buttons = _button_builder_buttons(flow)
            with_photo = bool(flow.get("photo_file_id"))
            button_builder.validate_rendered_length(flow["message_text"], buttons, with_photo)
            post_id = uuid.uuid4().hex[:10]
            rendered = button_builder.render_post(flow["message_text"], buttons)
            keyboard = button_builder.post_keyboard(post_id, buttons)
            if with_photo:
                sent = await api.send_photo(
                    flow["admin_chat_id"],
                    flow["photo_file_id"],
                    rendered,
                    reply_markup=keyboard,
                    parse_mode=None,
                )
            else:
                sent = await api.send_message(
                    flow["admin_chat_id"],
                    rendered,
                    reply_markup=keyboard,
                    parse_mode=None,
                )
            try:
                stats.create_button_post(
                    flow["entry"],
                    post_id,
                    flow["admin_chat_id"],
                    sent["message_id"],
                    flow["message_text"],
                    [button["text"] for button in buttons],
                    flow["user_id"],
                    flow["chat_id"],
                    photo_file_id=flow.get("photo_file_id"),
                )
            except Exception:
                await api.delete_message(flow["admin_chat_id"], sent["message_id"])
                raise
            flows.pop(item_id, None)
            try:
                await _edit_button_builder_control(
                    api,
                    callback_message,
                    "Пост отправлен в общий чат.",
                    reply_markup=button_builder.delete_keyboard(post_id),
                )
            except Exception:
                # The public post and its persistent counters already exist. Losing the
                # in-place DM edit must not report the publish as failed or lose the only
                # delete control; send a fresh receipt instead.
                log(
                    "[bot_listener] failed to edit generated-post receipt; "
                    "sending a new control message"
                )
                await api.send_message(
                    flow["chat_id"],
                    "Пост отправлен в общий чат.",
                    reply_markup=button_builder.delete_keyboard(post_id),
                    parse_mode=None,
                )
            return
    except ValueError as error:
        await api.send_message(
            flow["chat_id"],
            str(error),
            reply_to_message_id=callback_message.get("message_id"),
            parse_mode=None,
        )
    except Exception:
        log(f"[bot_listener] button-builder callback failed:\n{traceback.format_exc()}")
        await api.send_message(
            flow["chat_id"],
            "Не удалось выполнить действие. Попробуй ещё раз.",
            reply_to_message_id=callback_message.get("message_id"),
            parse_mode=None,
        )


async def refresh_button_counters_once(
    api: TelegramBotAPI,
    entry: str | None,
    rendered_counts: dict[str, tuple[int, ...]],
    log=print,
) -> None:
    """Edit every changed generated post once; the caller supplies the in-memory cache."""
    if entry is None:
        return
    posts = stats.active_button_posts(entry)
    active_ids = set()
    for post in posts:
        post_id = post["post_id"]
        active_ids.add(post_id)
        counts = tuple(int(button.get("count", 0)) for button in post["buttons"])
        if rendered_counts.get(post_id) == counts:
            continue
        try:
            text = button_builder.render_post(post["message_text"], post["buttons"])
            keyboard = button_builder.post_keyboard(post_id, post["buttons"])
            if post.get("photo_file_id"):
                await api.edit_message_caption(
                    post["chat_id"],
                    post["message_id"],
                    text,
                    reply_markup=keyboard,
                    parse_mode=None,
                )
            else:
                await api.edit_message_text(
                    post["chat_id"],
                    post["message_id"],
                    text,
                    reply_markup=keyboard,
                    parse_mode=None,
                )
        except Exception as error:
            lowered = str(error).lower()
            permanently_gone = any(
                marker in lowered
                for marker in (
                    "message to edit not found",
                    "message can't be edited",
                    "message_id_invalid",
                )
            )
            if permanently_gone:
                # Somebody removed the post by hand. Drop the orphaned state so the loop
                # does not retry it forever.
                stats.delete_button_post(
                    entry, post_id, post["chat_id"], post["message_id"]
                )
            else:
                # A network or Telegram-side failure is temporary: leave the cache
                # unchanged so the same counts are retried three seconds later.
                log(
                    f"[bot_listener] failed to refresh button counters for {post_id}:\n"
                    f"{traceback.format_exc()}"
                )
            continue
        rendered_counts[post_id] = counts
    for stale_id in set(rendered_counts) - active_ids:
        rendered_counts.pop(stale_id, None)


async def _button_counter_refresh_loop(
    api: TelegramBotAPI,
    entry: str | None,
    log=print,
) -> None:
    rendered_counts: dict[str, tuple[int, ...]] = {}
    while True:
        await asyncio.sleep(button_builder.COUNTER_REFRESH_SECONDS)
        try:
            await refresh_button_counters_once(api, entry, rendered_counts, log=log)
        except Exception:
            # A corrupt store entry or one unexpected Telegram response must not kill the
            # permanent refresh task. The next cycle gets a fresh read three seconds later.
            log(f"[bot_listener] button-counter refresh cycle failed:\n{traceback.format_exc()}")


async def handle_badge_admin_command(
    api: TelegramBotAPI,
    telethon_client,
    message: dict,
    command_text: str,
    entry: str | None,
    admin_chat_id: int | None,
    tz,
    log=print,
) -> None:
    """/badgeadmin — delegate custom-badge management to another member.

        /badgeadmin                  list current delegates
        /badgeadmin @username        grant
        /badgeadmin - @username      revoke

    Only chat administrators (and the hardcoded delegates) may run this: a delegate can
    award badges, not appoint further delegates -- see _is_chat_admin_or_privileged.
    """
    dm_chat_id = message["chat"]["id"]
    reply_to = message["message_id"]
    actor = message.get("from") or {}

    async def reply(text: str) -> None:
        try:
            await api.send_message(dm_chat_id, text, reply_to_message_id=reply_to, parse_mode=None)
        except Exception:
            log(f"[bot_listener] failed to answer /badgeadmin:\n{traceback.format_exc()}")

    if entry is None or admin_chat_id is None:
        await reply("Не настроен основной чат.")
        return
    if not await _is_chat_admin_or_privileged(api, admin_chat_id, actor):
        await reply("Выдавать это право могут только администраторы чата.")
        return

    argument = command_text[len(BADGE_ADMIN_COMMAND):].strip()
    if not argument:
        managers = stats.list_badge_managers(entry)
        if not managers:
            await reply(
                "Пока никому не выдано.\n"
                f"Выдать: {BADGE_ADMIN_COMMAND} @username\n"
                f"Забрать: {BADGE_ADMIN_COMMAND} - @username"
            )
            return
        listed = "\n".join(
            f"• {record.get('display_name') or record['user_id']}"
            + (f" (@{record['username']})" if record.get("username") else "")
            for record in managers
        )
        await reply(f"Могут выдавать значки:\n{listed}")
        return

    revoking = argument.startswith("-")
    target_name = argument.lstrip("-").strip()
    if not target_name:
        await reply(f"Формат: {BADGE_ADMIN_COMMAND} - @username")
        return

    target, _, _, _, _, _ = await stats.resolve_stat_target(
        telethon_client, entry, entry, target_name, None, "", tz, log=log
    )
    if target is None:
        await reply("Не нашёл такого участника в статистике чата.")
        return

    if revoking:
        removed = stats.revoke_badge_manager(entry, target.user_id)
        await reply(
            f"{target.display_name} больше не может выдавать значки."
            if removed
            else f"У {target.display_name} и так не было этого права."
        )
        return

    granted = stats.grant_badge_manager(
        entry, target.user_id, target.username, target.display_name,
        actor.get("id"), _display_name(actor),
    )
    await reply(
        f"{target.display_name} теперь может создавать и выдавать значки.\n"
        "Кнопка появится у него в /cabinet."
        if granted
        else f"{target.display_name} уже мог это делать."
    )


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


# How long any interactive path will wait on the Telethon session before giving up. Ten
# seconds is longer than a healthy resolve (milliseconds) and shorter than a member's
# patience with a button that appears to do nothing.
CHAT_RESOLVE_TIMEOUT_SECONDS = 10


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
        # Bounded, because a Telethon session that cannot connect does not raise -- it
        # waits, indefinitely, retrying underneath. On an interactive path that is far
        # worse than failing: an inline button whose handler is stuck here never reaches
        # answerCallbackQuery, so the button spins forever with no error anywhere.
        entity = await asyncio.wait_for(
            resolve_chat(telethon_client, entry), timeout=CHAT_RESOLVE_TIMEOUT_SECONDS
        )
        chat_id = tl_utils.get_peer_id(entity)
    except asyncio.TimeoutError:
        log(f"[bot_listener] timed out resolving chat_id for '{entry}' -- is the Telethon session alive?")
        return None
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


def schedule_bot_delete(
    api: TelegramBotAPI,
    chat_id,
    message_ids: list[int],
    delay_seconds: int,
    log,
    background_tasks: set,
    trigger_message_id: int | None = None,
):
    """Fire-and-forget: deletes `message_ids` after `delay_seconds`.

    `trigger_message_id` is the user's command that asked for the reply -- it goes with
    the answer, so a self-deleting exchange leaves nothing behind on either side rather
    than a chat full of orphaned "/stat" lines replying to messages that no longer exist.
    Only pass it for replies a user actually prompted: a dismissal (a reaction on a
    message we sent) or a scheduled post has no such command. Deletion is best-effort in
    api.delete_message, so a bot without delete rights in the chat quietly removes only
    its own message, exactly as it did before.
    """

    async def _do():
        await asyncio.sleep(delay_seconds)
        for mid in message_ids:
            await api.delete_message(chat_id, mid)
        if trigger_message_id is not None and trigger_message_id not in message_ids:
            await api.delete_message(chat_id, trigger_message_id)

    task = asyncio.create_task(_do())
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


async def _can_dm(api: TelegramBotAPI, user_id: int | None, log=print) -> bool:
    """Whether the bot may write to this person's DM at all.

    Probed with sendChatAction rather than by just trying the real send and handling the
    failure: a summary answer is only ready after a minute of OpenAI work, and finding out
    THEN that there was nowhere to put it means having paid for an answer nobody can read.
    The probe costs nothing, delivers no message to someone who has opened the DM, and
    fails with exactly the 403 the real send would have.
    """
    if not user_id:
        return False
    try:
        await api.send_chat_action(user_id, "typing")
        return True
    except ChatSummaryError as e:
        log(f"[bot_listener] cannot DM {user_id}: {e}")
        return False


async def _post_summary_receipt(
    api: TelegramBotAPI,
    chat_id,
    trigger_message_id: int,
    text: str,
    bot_username: str | None,
    background_tasks: set,
    log=print,
) -> None:
    """Leaves the group a short receipt for a summary that was answered in the DM (or
    couldn't be), with a link into that DM, and schedules both it and the request that
    prompted it for deletion -- see SUMMARY_RECEIPT_DELETE_AFTER.

    The link is an inline button rather than a URL in the text so the receipt reads as one
    line whichever of the two things it is saying.
    """
    markup = (
        {"inline_keyboard": [[{"text": "Открыть чат с ботом", "url": f"https://t.me/{bot_username}"}]]}
        if bot_username else None
    )
    sent = None
    try:
        sent = await api.send_message(
            chat_id, text, reply_to_message_id=trigger_message_id,
            parse_mode=None, reply_markup=markup,
        )
    except Exception as e:
        log(f"[bot_listener] failed to post the summary receipt: {e}")
    # Scheduled even when the receipt itself failed to send: the request still has to go,
    # and schedule_bot_delete takes the trigger separately from the message list.
    schedule_bot_delete(
        api, chat_id, [sent["message_id"]] if sent and "message_id" in sent else [],
        SUMMARY_RECEIPT_DELETE_AFTER, log, background_tasks,
        trigger_message_id=trigger_message_id,
    )


async def _deliver_shop_item(
    api, telethon_client, cfg, tz, entry, chat_ref, item, user, xp, requester, log,
):
    """Produce the thing a purchase actually bought.

    Returns the reply text. Raising is the documented way to signal that delivery failed
    -- handle_shop_command catches it and refunds, so a member is never charged for an
    LLM call that errored or a work that turned out not to exist.
    """
    if item.code == "freeze":
        held = economy.add_streak_freeze(entry, user.user_id)
        return (
            f"Заморозка куплена. В запасе: {held}.\n"
            "Она сработает сама, когда ты пропустишь день -- серия не оборвётся."
        )

    if item.code == "critique":
        if not user.recent_figurine_posts:
            raise ChatSummaryError("no tracked work to critique")
        _, message_id = user.recent_figurine_posts[0]
        source = await telethon_client.get_messages(chat_ref, ids=message_id)
        if source is None:
            raise ChatSummaryError("the tracked work is no longer available")
        image_bytes = await telethon_client.download_media(source, file=bytes)
        if not image_bytes:
            raise ChatSummaryError("the tracked work has no downloadable image")
        critique = await asyncio.to_thread(
            critique_work,
            api_key=cfg.openai_api_key, model=cfg.openai_model,
            image_bytes=image_bytes, caption=getattr(source, "message", "") or "",
        )
        return f"🎨 Разбор твоей последней работы:\n\n{critique}"

    raise ChatSummaryError(f"unknown shop item {item.code}")


# A chat's id and @username never change while the process runs, but resolving them costs
# a Telethon get_entity round trip each time. The cabinet re-renders on every button
# press, so this is the difference between a menu that responds instantly and one that
# waits on Telegram twice per tap.
_CABINET_CHAT_REF_CACHE: dict[str, tuple] = {}

# (entry, user_id) -> (deadline, context). resolve_stat_target re-reads every recorded
# day file AND may refetch today's transcript; doing that again for each tap of a
# six-button menu is what made the cabinet feel slow. Balances, titles and freezes are
# deliberately NOT part of this -- every view reads those straight from the ledger -- so a
# purchase still shows up immediately while navigation stays cheap.
_CABINET_CONTEXT_CACHE: dict[tuple, tuple] = {}
CABINET_CONTEXT_TTL_SECONDS = 45
# Longer than CHAT_RESOLVE_TIMEOUT_SECONDS because this call legitimately re-reads every
# recorded day file and may refetch today's transcript, but still bounded -- see
# _cabinet_context.
CABINET_CONTEXT_TIMEOUT_SECONDS = 30


async def _cabinet_chat_ref(telethon_client, entry: str, known_chat_ids: dict, log=print):
    """(chat_id, username) for building t.me links into the group from a DM."""
    cached = _CABINET_CHAT_REF_CACHE.get(entry)
    if cached is not None:
        return cached
    chat_id = await _resolve_chat_id(telethon_client, entry, known_chat_ids, log=log)
    username = None
    try:
        # Bounded for the same reason as _resolve_chat_id: this is only here to build a
        # t.me link, and a Telethon session that cannot connect would otherwise stall the
        # whole cabinet redraw waiting for one.
        entity = await asyncio.wait_for(
            resolve_chat(telethon_client, entry), timeout=CHAT_RESOLVE_TIMEOUT_SECONDS
        )
        username = getattr(entity, "username", None)
    except Exception:
        log(f"[bot_listener] could not resolve '{entry}' for cabinet links")
    resolved = (chat_id, username)
    # Only cache a usable answer, so a transient failure doesn't permanently break links.
    if chat_id is not None or username:
        _CABINET_CHAT_REF_CACHE[entry] = resolved
    return resolved


async def _cabinet_context(telethon_client, entry: str, tz, from_user: dict, log=print):
    """(user, xp, rank, total, streak, season_xp) for whoever is using the cabinet, or None.

    Every cabinet view needs the same resolved identity, and resolve_stat_target is also
    what applies any bought streak freeze, so this is the one place that call is made.

    Cached for CABINET_CONTEXT_TTL_SECONDS per member: the underlying call re-reads every
    recorded day file and can refetch today's transcript from Telegram, which is far too
    much work to repeat for each tap of a menu. Skipping it also skips re-applying streak
    freezes, which is harmless -- consume_streak_freeze is idempotent per day and runs
    again on the next uncached read.
    """
    cache_key = (entry, from_user.get("id"))
    cached = _CABINET_CONTEXT_CACHE.get(cache_key)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]

    try:
        # Bounded: this call can refetch today's transcript through the Telethon session,
        # and a session that cannot connect waits rather than failing. Unbounded, that is
        # what made "магазин не открывается" a symptom -- the callback had already been
        # answered, so the spinner stopped and the screen simply never arrived.
        user, rank, total, xp, streak, season_xp = await asyncio.wait_for(
            stats.resolve_stat_target(
                telethon_client, entry, entry, "",
                from_user.get("username"), _display_name(from_user), tz, log=log,
                frozen_days_for=economy.streak_freeze_lookup(entry),
            ),
            timeout=CABINET_CONTEXT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        log(
            f"[bot_listener] cabinet context for '{entry}' timed out after "
            f"{CABINET_CONTEXT_TIMEOUT_SECONDS}s -- is the Telethon session alive?"
        )
        return None
    if user is None:
        return None
    context = (user, xp, rank, total, streak, season_xp)
    _CABINET_CONTEXT_CACHE[cache_key] = (time.monotonic() + CABINET_CONTEXT_TTL_SECONDS, context)
    return context


async def _render_cabinet_section(
    telethon_client, api, cfg, entry, tz, action, argument, from_user, chat_id,
    known_chat_ids: dict | None = None, log=print,
) -> tuple[str, dict] | None:
    """Build one cabinet screen. Returns None when the member isn't tracked yet."""
    context = await _cabinet_context(telethon_client, entry, tz, from_user, log=log)
    if context is None:
        return None
    user, xp, rank, total, streak, season_xp = context

    async def links():
        """Resolved lazily and only by the two screens that show links -- every other
        screen used to pay for two Telegram entity lookups it never read."""
        home_chat_id, home_username = await _cabinet_chat_ref(
            telethon_client, entry, known_chat_ids if known_chat_ids is not None else {}, log=log
        )
        figurines = stats.figurine_message_links(home_username, home_chat_id, user)
        best, workplace = stats.showcase_message_links(home_username, home_chat_id, user)
        return figurines, best, workplace

    def badges():
        return (
            stats.custom_badges_for_user(entry, user.user_id)
            + stats.weekly_winner_badges_for_user(entry, user.user_id)
        )

    if action == "stats":
        figurines, best, workplace = await links()
        return cabinet.stats_view(
            entry, user, xp, rank, total, streak, figurines, badges(), best, workplace,
            season_xp=season_xp,
        )
    if action == "shop":
        return cabinet.shop_view(entry, user, xp)
    if action == "works":
        figurines, best, workplace = await links()
        return cabinet.works_view(entry, user, figurines, best, workplace)
    if action == "badges":
        return cabinet.badges_view(
            entry, user, badges(),
            chat_custom_badge_total=len(stats.list_custom_badges(entry)),
        )
    if action == "title":
        return cabinet.title_view(entry, user, xp)
    if action == "buy":
        return await _cabinet_buy(
            api, telethon_client, cfg, entry, tz, user, xp, argument, from_user, chat_id, log=log
        )
    return cabinet.main_view(
        entry, user, xp, rank, total, streak, season_xp=season_xp,
        can_manage_badges=_shows_badge_admin_button(entry, user.user_id, user.username),
    )


async def _cabinet_buy(
    api, telethon_client, cfg, entry, tz, user, xp, code, from_user, chat_id, log=print,
) -> tuple[str, dict]:
    """Purchase from a shop button, then re-render the shop with the outcome on top.

    The title is not bought here: it needs text back from the member first, so its button
    opens a force-reply step instead and only debits once that arrives.
    """
    item = economy.find_item(code)
    if item is None:
        return cabinet.shop_view(entry, user, xp, notice="Не знаю такой товар.")
    if item.code == "title":
        return cabinet.title_view(entry, user, xp)

    ok, refusal, remaining = economy.purchase(entry, user.user_id, xp, item)
    if not ok:
        return cabinet.shop_view(entry, user, xp, notice=f"❌ {refusal}")

    try:
        delivered = await _deliver_shop_item(
            api, telethon_client, cfg, tz, entry, entry, item, user, xp,
            _display_name(from_user), log,
        )
    except Exception:
        log(f"[bot_listener] cabinet delivery failed for {item.code}:\n{traceback.format_exc()}")
        restored = economy.refund(entry, user.user_id, xp, item.price, item.code)
        return cabinet.shop_view(
            entry, user, xp,
            notice=f"❌ Не получилось выдать «{item.name}» — монеты вернул. Баланс: {restored}.",
        )

    return cabinet.result_view(
        user.user_id, f"{delivered}\n\n🪙 Осталось: {remaining}."
    )


def _shows_badge_admin_button(entry: str | None, user_id, username: str | None) -> bool:
    """Whether to draw the delegate button. Deliberately does NOT call Telegram to check
    chat-admin status: this runs on every cabinet render, and administrators already know
    about /badge. The button only decides what is DRAWN -- handle_cabinet_callback
    re-verifies with the full check before acting on it."""
    if not entry:
        return False
    if (username or "").strip().lstrip("@").lower() in PRIVILEGED_MANAGEMENT_USERNAMES:
        return True
    return stats.is_badge_manager(entry, user_id)


def _stats_entry_for(chat: dict, matched_entry: str | None, home_chat_ref: str | None) -> str | None:
    """Which tracked chat a stats/wallet command should read.

    In a group that is the group itself. In a DM there is no tracked chat to match --
    _match_allowed_chat deliberately never matches a private chat -- so it falls back to
    the single configured home chat, exactly as /cabinet and the summary pipeline already
    do. Without this, every command in the DM menu below would either answer
    "недоступна в этом чате" or do nothing at all, which is a poor advertisement for a
    menu the bot itself publishes.
    """
    if matched_entry is not None:
        return matched_entry
    if chat.get("type") == "private":
        return home_chat_ref
    return None


def _has_pending_flow(flows: dict[str, dict], chat_id, user_id, ttl_seconds: int) -> bool:
    """Whether this person is mid-way through a force-reply step in `flows`.

    The menu must not barge in on somebody who has been asked a question -- they may be
    typing the answer, or may have replied without using Telegram's reply UI, in which
    case the correlated handler already declined and silence is the right response.
    """
    return any(
        flow.get("chat_id") == chat_id
        and flow.get("user_id", flow.get("admin_id")) == user_id
        and time.monotonic() - flow["created_at"] <= ttl_seconds
        for flow in flows.values()
    )


async def maybe_send_menu(
    api: TelegramBotAPI,
    telethon_client,
    tz,
    message: dict,
    entry: str | None,
    cabinet_flows: dict[str, dict],
    badge_flows: dict[str, dict],
    menu_last_sent: dict,
    button_builder_flows: dict[str, dict] | None = None,
    log=print,
) -> None:
    """Answer an otherwise-unhandled DM with the cabinet menu.

    This is the last thing tried in a private chat, deliberately: every specific handler
    -- commands, summary keywords, the joke trigger, both force-reply flows -- gets to
    claim the message first, and only what none of them wanted lands here. Without it a
    DM the bot doesn't recognise produces total silence, which reads as "the bot is
    broken" rather than "that wasn't a request I understand".

    Never fires in a group: a menu posted in reply to ordinary chatter would be spam, and
    it renders one person's private balance in front of everybody.
    """
    chat = message.get("chat") or {}
    if chat.get("type") != "private":
        return
    chat_id = chat.get("id")
    actor = message.get("from") or {}
    user_id = actor.get("id")

    if _has_pending_flow(cabinet_flows, chat_id, user_id, CABINET_FLOW_TTL_SECONDS):
        return
    if _has_pending_flow(badge_flows, chat_id, user_id, BADGE_FLOW_TTL_SECONDS):
        return
    if _has_pending_flow(
        button_builder_flows or {}, chat_id, user_id, BUTTON_BUILDER_FLOW_TTL_SECONDS
    ):
        return

    now = time.monotonic()
    last = menu_last_sent.get(chat_id)
    if last is not None and now - last < MENU_FALLBACK_COOLDOWN_SECONDS:
        return
    menu_last_sent[chat_id] = now

    try:
        if entry is None:
            text, keyboard = cabinet.welcome_view(user_id)
        else:
            context = await _cabinet_context(telethon_client, entry, tz, actor, log=log)
            if context is None:
                text, keyboard = cabinet.welcome_view(user_id)
            else:
                user, xp, rank, total, streak, season_xp = context
                text, keyboard = cabinet.main_view(
        entry, user, xp, rank, total, streak, season_xp=season_xp,
        can_manage_badges=_shows_badge_admin_button(entry, user.user_id, user.username),
    )
        await api.send_message(
            chat_id, text, reply_to_message_id=message.get("message_id"),
            reply_markup=keyboard, parse_mode="HTML",
        )
    except Exception:
        log(f"[bot_listener] failed to send the fallback menu:\n{traceback.format_exc()}")


async def send_stats_digest(
    api: TelegramBotAPI,
    chat_id,
    entry: str,
    text: str,
    parse_mode: str | None,
    photo=None,
    log=print,
) -> None:
    """Post one scheduled digest, with its stage picture when there is one.

    The picture is the part that is allowed to fail, and every way it can fail ends in the
    same place -- the post goes out as plain text:

      * no file for the current stage (nobody has uploaded it yet, which is the normal
        state of a half-filled assets/tree_stages),
      * a caption over Telegram's 1024-character limit, which is rejected outright,
      * an upload Telegram refuses (odd dimensions, a file somebody replaced with
        something huge, a file that vanished between the check and the send).

    Lives out here rather than inside run_bot_listener's queue consumer so all of that is
    testable: it is the code path that decides whether the chat gets its morning post.
    """
    if photo is not None and len(text) > CAPTION_LIMIT:
        log(f"[bot_listener] text too long for a caption ({len(text)}), sending '{entry}' without {photo.name}")
        photo = None
    if photo is not None:
        try:
            await api.send_photo_file(chat_id, photo, caption=text, parse_mode=parse_mode)
            log(f"[bot_listener] sent stats notification to '{entry}' with {photo.name}")
            return
        except Exception:
            log(f"[bot_listener] failed to send {photo.name}, falling back to text:\n{traceback.format_exc()}")
    try:
        # The producer decides the mode: the procrastinator call-out is plain text because
        # it embeds raw display names, while the tree digest is HTML and escapes them
        # itself. Sending one with the other's mode either prints tags verbatim or has
        # Telegram reject the whole message.
        await api.send_message(chat_id, text, parse_mode=parse_mode)
        log(f"[bot_listener] sent stats notification to '{entry}'")
    except Exception:
        log(f"[bot_listener] failed to send stats notification:\n{traceback.format_exc()}")


async def register_bot_menu(api: TelegramBotAPI, log=print) -> None:
    """Publish the ☰ Menu command lists once at startup. Best-effort: the bot works
    without a menu, so a transient failure here must not stop it from starting."""
    for scope, commands in (
        ({"type": "all_private_chats"}, list(PRIVATE_CHAT_COMMANDS)),
        ({"type": "all_group_chats"}, list(GROUP_CHAT_COMMANDS)),
    ):
        try:
            await api.set_my_commands(commands, scope=scope)
        except ChatSummaryError as e:
            log(f"[bot_listener] could not register the {scope['type']} menu: {e}")


async def handle_cabinet_command(
    api: TelegramBotAPI,
    telethon_client,
    tz,
    message: dict,
    entry: str | None,
    log=print,
) -> None:
    """Open the cabinet. DM only: it shows one person's own balance and buttons that
    spend their coins, neither of which belongs in a group."""
    chat_id = message["chat"]["id"]
    reply_to = message["message_id"]
    if entry is None:
        await api.send_message(
            chat_id,
            "Не настроен основной чат — кабинет недоступен.",
            reply_to_message_id=reply_to, parse_mode=None,
        )
        return
    context = await _cabinet_context(telethon_client, entry, tz, message.get("from") or {}, log=log)
    if context is None:
        await api.send_message(
            chat_id,
            "Ты ещё не отслеживаешься — напиши что-нибудь в чат и попробуй снова.",
            reply_to_message_id=reply_to, parse_mode=None,
        )
        return
    user, xp, rank, total, streak, season_xp = context
    text, keyboard = cabinet.main_view(
        entry, user, xp, rank, total, streak, season_xp=season_xp,
        can_manage_badges=_shows_badge_admin_button(entry, user.user_id, user.username),
    )
    await api.send_message(
        chat_id, text, reply_to_message_id=reply_to, reply_markup=keyboard, parse_mode="HTML"
    )


async def handle_cabinet_callback(
    api: TelegramBotAPI,
    telethon_client,
    cfg,
    tz,
    callback: dict,
    entry: str | None,
    cabinet_flows: dict[str, dict],
    badge_flows: dict[str, dict] | None = None,
    known_chat_ids: dict | None = None,
    log=print,
) -> None:
    parsed = cabinet.parse_callback(callback.get("data") or "")
    if parsed is None:
        return
    owner_id, action, argument = parsed
    callback_id = callback.get("id")
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    actor = callback.get("from") or {}

    # The owner id rides inside the button, so somebody forwarded this menu -- or a
    # second person in the same DM, which cannot happen but is cheap to rule out --
    # cannot press buttons that spend another member's coins.
    if str(actor.get("id")) != str(owner_id):
        await api.answer_callback_query(callback_id, "Это чужой кабинет.")
        return
    if entry is None:
        await api.answer_callback_query(callback_id, "Основной чат не настроен.")
        return

    # The two text-entry actions answer with a force-reply prompt instead of redrawing.
    if action == "badge_admin":
        admin_chat_id = await _resolve_chat_id(
            telethon_client, entry, known_chat_ids if known_chat_ids is not None else {}, log=log
        )
        if not await _can_manage_chat(api, admin_chat_id, actor, entry):
            await api.answer_callback_query(callback_id, "Нет прав на управление значками.")
            return
        await api.answer_callback_query(callback_id)
        # Reuses the existing /badge menu wholesale -- same flow, same prompts, same
        # idempotent awarding -- so this button adds an entry point, not a second
        # implementation to keep in step.
        if badge_flows is None:
            # Only reachable from a caller that did not thread the flow state through;
            # starting the menu without it would draw buttons that can never resolve.
            log("[bot_listener] cabinet badge button pressed without badge_flows state")
            return
        await handle_badge_command(
            api,
            {"chat": {"id": chat_id, "type": "private"},
             "message_id": message.get("message_id"), "from": actor},
            entry, admin_chat_id, badge_flows,
        )
        return

    if action == "work_delete_ok":
        context = await _cabinet_context(telethon_client, entry, tz, actor, log=log)
        if context is None:
            await api.answer_callback_query(callback_id, "Статистика не найдена.")
            return
        user = context[0]
        # Only ever their OWN work: the message_id is checked against this member's own
        # tracked posts, so a hand-crafted callback cannot delete somebody else's.
        if argument not in {str(post[1]) for post in user.recent_figurine_posts}:
            await api.answer_callback_query(callback_id, "Эта работа уже удалена.")
            return
        await api.answer_callback_query(callback_id)
        stats.delete_figurine_submission(
            entry, user.user_id, int(argument), actor.get("id"), _display_name(actor)
        )
        # The name would otherwise outlive the work it belonged to.
        stats.set_work_name(entry, user.user_id, argument, "")
        _CABINET_CONTEXT_CACHE.pop((entry, actor.get("id")), None)
        rendered = await _render_cabinet_section(
            telethon_client, api, cfg, entry, tz, "works", "", actor, chat_id,
            known_chat_ids=known_chat_ids, log=log,
        )
        if rendered:
            text, keyboard = rendered
            try:
                await api.edit_message_text(
                    chat_id, message.get("message_id"), text,
                    reply_markup=keyboard, parse_mode="HTML",
                )
            except Exception:
                log(f"[bot_listener] redraw after delete failed:\n{traceback.format_exc()}")
        return

    if action in ("title_set", "work_rename", "work_delete"):
        await api.answer_callback_query(callback_id)
        flow_id = uuid.uuid4().hex[:10]
        prompt_text = (
            f"Ответь на это сообщение текстом титула (до {economy.TITLE_MAX_CHARS} символов)."
            if action == "title_set"
            else (
                "Ответь на это сообщение в формате: номер и название.\n"
                f"Например: 3 Дредноут (до {stats.WORK_NAME_MAX_CHARS} символов).\n"
                "Один только номер — убрать название."
            )
        )
        prompt = await api.send_message(
            chat_id, prompt_text,
            reply_to_message_id=message.get("message_id"),
            reply_markup={"force_reply": True, "selective": True},
            parse_mode=None,
        )
        cabinet_flows[flow_id] = {
            "created_at": time.monotonic(),
            "chat_id": chat_id,
            "user_id": actor.get("id"),
            "entry": entry,
            "awaiting": {"title_set": "title", "work_delete": "work_delete"}.get(
                action, "work_rename"
            ),
            "prompt_message_id": prompt.get("message_id") if prompt else None,
        }
        return

    await api.answer_callback_query(callback_id)
    try:
        rendered = await _render_cabinet_section(
            telethon_client, api, cfg, entry, tz, action, argument, actor, chat_id,
            known_chat_ids=known_chat_ids, log=log,
        )
    except Exception:
        log(f"[bot_listener] cabinet section '{action}' failed:\n{traceback.format_exc()}")
        return
    if rendered is None:
        return
    text, keyboard = rendered
    try:
        await api.edit_message_text(
            chat_id, message.get("message_id"), text, reply_markup=keyboard, parse_mode="HTML"
        )
    except Exception:
        log(f"[bot_listener] cabinet redraw failed:\n{traceback.format_exc()}")


async def _works_screen(telethon_client, entry: str, user, notice: str = "", log=print):
    """Re-render "Мои работы" after a rename, links and all.

    The chat ref is cached per process (_CABINET_CHAT_REF_CACHE), so by the time anybody
    can rename a work -- they had to open this screen to see the numbers -- this costs no
    Telegram round trip at all.
    """
    chat_id, username = await _cabinet_chat_ref(telethon_client, entry, {}, log=log)
    figurines = stats.figurine_message_links(username, chat_id, user)
    best, workplace = stats.showcase_message_links(username, chat_id, user)
    return cabinet.works_view(entry, user, figurines, best, workplace, notice=notice)


async def handle_cabinet_text_input(
    api: TelegramBotAPI,
    telethon_client,
    tz,
    message: dict,
    cabinet_flows: dict[str, dict],
    log=print,
) -> bool:
    """Consume the force-reply after "Сменить титул" or "Отправить монеты".

    Returns True once this message belonged to a cabinet flow, so the caller stops
    treating it as ordinary chat input."""
    chat_id = message["chat"]["id"]
    actor = message.get("from") or {}
    replied_to = (message.get("reply_to_message") or {}).get("message_id")
    found = next(
        (
            (flow_id, flow)
            for flow_id, flow in cabinet_flows.items()
            if flow.get("chat_id") == chat_id
            and flow.get("user_id") == actor.get("id")
            and flow.get("prompt_message_id") == replied_to
            and time.monotonic() - flow["created_at"] <= CABINET_FLOW_TTL_SECONDS
        ),
        None,
    )
    if found is None:
        return False
    flow_id, flow = found
    cabinet_flows.pop(flow_id, None)
    entry = flow["entry"]
    text = (message.get("text") or "").strip()

    async def answer(rendered: tuple[str, dict]) -> None:
        body, keyboard = rendered
        try:
            await api.send_message(
                chat_id, body, reply_to_message_id=message["message_id"],
                reply_markup=keyboard, parse_mode="HTML",
            )
        except Exception:
            log(f"[bot_listener] cabinet reply failed:\n{traceback.format_exc()}")

    if text.lower() in ("отмена", "/cancel"):
        return True

    context = await _cabinet_context(telethon_client, entry, tz, actor, log=log)
    if context is None:
        return True
    user, xp, _, _, _, _ = context

    if flow["awaiting"] == "title":
        item = economy.find_item("title")
        ok, refusal, remaining = economy.purchase(entry, user.user_id, xp, item)
        if not ok:
            await answer(cabinet.title_view(entry, user, xp, notice=f"❌ {refusal}"))
            return True
        saved = economy.set_title(entry, user.user_id, text)
        if not saved:
            economy.refund(entry, user.user_id, xp, item.price, "title")
            await answer(cabinet.title_view(entry, user, xp, notice="❌ Пустой титул — монеты вернул."))
            return True
        await answer(cabinet.result_view(
            user.user_id,
            f"✅ Титул «{html.escape(saved)}» активен {economy.TITLE_DAYS} дней.\n"
            f"🪙 Осталось: {remaining}.",
        ))
        return True

    if flow["awaiting"] == "work_delete":
        try:
            position = int(text.strip())
        except ValueError:
            await answer(await _works_screen(
                telethon_client, entry, user, notice="❌ Нужен номер работы.", log=log))
            return True
        message_id = cabinet.message_id_for_position(user, position)
        if message_id is None:
            await answer(await _works_screen(
                telethon_client, entry, user, notice=f"❌ Работы №{position} нет.", log=log))
            return True
        names = stats.work_names_for_user(entry, user.user_id)
        await answer(cabinet.confirm_work_delete_view(
            user.user_id, position, names.get(str(message_id)), message_id
        ))
        return True

    parsed = cabinet.parse_rename_request(text)
    if parsed is None:
        await answer(await _works_screen(telethon_client, entry, user, notice="❌ Формат: 3 Дредноут", log=log))
        return True
    position, name = parsed
    message_id = cabinet.message_id_for_position(user, position)
    if message_id is None:
        await answer(await _works_screen(telethon_client, entry, user, notice=f"❌ Работы №{position} нет.", log=log))
        return True
    saved = stats.set_work_name(entry, user.user_id, message_id, name)
    notice = f"✅ Работа №{position}: «{html.escape(saved)}»" if saved else f"✅ Название работы №{position} убрано."
    await answer(await _works_screen(telethon_client, entry, user, notice=notice, log=log))
    return True


async def handle_tree_command(
    api: TelegramBotAPI,
    telethon_client,
    tz,
    message: dict,
    entry: str,
    background_tasks: set,
    log=print,
) -> None:
    """/tree -- the chat's shared progression. Self-deletes like every other stats reply,
    since the standing announcement of the tree is the 10:00 morning post."""
    chat_id = message["chat"]["id"]
    try:
        # While a ceremony is open there is no tree yet, and the ordinary status would
        # answer "0 мм" -- which reads as a broken feature rather than as one that hasn't
        # started.
        if entry and stats.planting_is_open(entry):
            text = tree.format_awaiting_planting_status()
            parse_mode = "HTML"
        else:
            yesterday = datetime.now(tz).date() - timedelta(days=1)
            total_xp, day_xp, contributors = await stats.chat_tree_totals(
                telethon_client, entry, entry, yesterday, tz, log=log, live_total=True
            )
            text = tree.format_tree_status(total_xp, day_xp, contributors)
            parse_mode = "HTML"
    except Exception:
        log(f"[bot_listener] failed to build the tree status:\n{traceback.format_exc()}")
        text, parse_mode = "Не удалось посчитать дерево.", None
    try:
        sent = await api.send_message(
            chat_id, text, reply_to_message_id=message["message_id"], parse_mode=parse_mode
        )
        if sent and "message_id" in sent:
            schedule_bot_delete(
                api, chat_id, [sent["message_id"]], STATS_DELETE_AFTER, log, background_tasks,
                trigger_message_id=message["message_id"],
            )
    except Exception:
        log(f"[bot_listener] failed to send the tree status:\n{traceback.format_exc()}")


def _vote_page_url(cfg) -> str | None:
    return f"{cfg.webapp_public_url}{vote_web.ROUTE_PREFIX}" if cfg.webapp_public_url else None


_VOTE_MEDALS = ("🥇", "🥈", "🥉")


def _vote_who(entry) -> str:
    return f"{entry.author_name} (@{entry.author_username})" if entry.author_username else entry.author_name


def _vote_status_text(entry: str) -> str:
    """The "current standings" block shared by an administrator's bare /vote status
    message and (implicitly, via the same shape) the winner announcement -- top 3 with
    medals, or an explanation of why there's nothing to show yet."""
    poll = voting.latest_poll(entry)
    if poll is None:
        return "Голосование ещё не создано."
    lines = [f"Проголосовало: {len(poll.votes)} чел. · {'открыто' if poll.open else 'закрыто'}"]
    top = poll.tally()[:3]
    # poll.tally() lists every APPROVED entry, zero-vote ones included -- so "top" alone
    # doesn't mean anyone actually voted, only that something was admitted.
    if top and top[0][1] > 0:
        lines.append("")
        lines.append("Топ сейчас:" if poll.open else "Топ:")
        for medal, (e, v) in zip(_VOTE_MEDALS, top):
            if v <= 0:
                break  # don't pad the top with zero-vote entries just to reach 3
            lines.append(f"{medal} {_vote_who(e)} — {v} голосов")
    elif poll.approved:
        lines.append("Пока никто не проголосовал.")
    else:
        lines.append("Работы ещё не допущены к голосованию -- /vote выбрать.")
    return "\n".join(lines)


def _current_vote_poll_id(tz) -> str:
    """Keyed by ISO week, not by today's date: собрать/выбрать/очистить all need to agree
    on which poll "this week" refers to regardless of which day of the week they're run,
    and a date-keyed id would silently point at a different poll once the day rolls over
    mid-week."""
    iso_year, iso_week, _ = datetime.now(tz).isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _vote_clear_callback_data(chat_id, user_id) -> str:
    return f"{VOTE_CLEAR_CALLBACK_PREFIX}:{chat_id}:{user_id}"


def _parse_vote_clear_callback(data: str) -> tuple[int, int] | None:
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != VOTE_CLEAR_CALLBACK_PREFIX:
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


async def handle_vote_clear_callback(
    api: TelegramBotAPI, cfg, tz, callback: dict, entry: str | None, log=print,
) -> None:
    """Confirms and executes "/vote очистить"'s tap-to-confirm button. Re-checks admin
    status against the tapper (not just whoever the confirmation was originally sent to)
    even though a DM only that person can see the button in the first place -- the same
    belt-and-suspenders check every other confirm flow in this file does."""
    parsed = _parse_vote_clear_callback(callback.get("data"))
    if parsed is None:
        await api.answer_callback_query(callback["id"])
        return
    chat_id, target_user_id = parsed

    clicker = callback.get("from") or {}
    if clicker.get("id") != target_user_id:
        await api.answer_callback_query(callback["id"], text="Эта кнопка не для тебя.")
        return
    if not entry or not await _can_manage_chat(api, chat_id, clicker, entry):
        await api.answer_callback_query(callback["id"], text="Только администраторы.")
        return

    poll_id = _current_vote_poll_id(tz)
    existed = voting.delete_poll(entry, poll_id)
    await api.answer_callback_query(callback["id"], text="Очищено" if existed else "Уже пусто")
    log(f"[bot_listener] {clicker.get('username') or target_user_id} cleared vote poll {poll_id} (existed={existed})")
    try:
        await api.send_message(
            chat_id,
            "Голосование очищено. Собери заново: /vote собрать" if existed
            else "Голосование за эту неделю уже было пустым.",
            parse_mode=None,
        )
    except Exception as e:
        log(f"[bot_listener] failed to confirm the vote clear: {e}")


def _vote_action_callback_data(action: str, chat_id, user_id) -> str:
    return f"{VOTE_ACTION_CALLBACK_PREFIX}:{action}:{chat_id}:{user_id}"


def _parse_vote_action_callback(data: str) -> tuple[str, int, int] | None:
    parts = (data or "").split(":")
    if len(parts) != 4 or parts[0] != VOTE_ACTION_CALLBACK_PREFIX or parts[1] not in VOTE_ACTIONS:
        return None
    try:
        return parts[1], int(parts[2]), int(parts[3])
    except ValueError:
        return None


async def handle_vote_action_callback(
    api: TelegramBotAPI,
    telethon_client,
    cfg,
    tz,
    callback: dict,
    entry: str | None,
    bot_username: str | None,
    background_tasks: set,
    vote_chat_flows: dict[str, dict],
    log=print,
) -> None:
    """One button per subcommand on an administrator's bare-/vote status message
    (собрать/chat/очистить -- "Открыть голосование"/"Модерация" are plain web_app
    buttons, not callbacks, since those just open a page). Rather than duplicating
    собрать/chat/очистить's logic here, this builds the same message shape
    handle_vote_command already parses and hands it straight over -- the admin/DM gate,
    the actual work, all of it, from one place."""
    parsed = _parse_vote_action_callback(callback.get("data"))
    if parsed is None:
        await api.answer_callback_query(callback["id"])
        return
    action, chat_id, target_user_id = parsed

    clicker = callback.get("from") or {}
    if clicker.get("id") != target_user_id:
        await api.answer_callback_query(callback["id"], text="Эта кнопка не для тебя.")
        return
    # собрать can take close to a minute -- answered immediately regardless of action, so
    # the tap never sits spinning while handle_vote_command does the real (re-verified)
    # admin check and the actual work in the background.
    await api.answer_callback_query(callback["id"])

    trigger = callback.get("message") or {}
    synthetic_message = {
        "message_id": trigger.get("message_id"),
        "chat": {"id": chat_id, "type": "private"},
        "from": clicker,
        "text": VOTE_ACTIONS[action],
    }
    task = asyncio.create_task(
        handle_vote_command(
            api, telethon_client, cfg, tz, synthetic_message, entry, bot_username,
            background_tasks, log=log, vote_chat_flows=vote_chat_flows,
        )
    )
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


async def handle_vote_command(
    api: TelegramBotAPI,
    telethon_client,
    cfg,
    tz,
    message: dict,
    entry: str | None,
    bot_username: str | None,
    background_tasks: set,
    log=print,
    forced_mode: str | None = None,
    vote_chat_flows: dict[str, dict] | None = None,
) -> None:
    """Five distinct things live behind /vote, deliberately kept apart rather than one
    page that changes shape depending who opens it:

    - "/vote собрать" (DM, admin-only) adds newly posted #итогинедели entries to the
      list -- already-known ones are left alone, not re-fetched or re-processed.
    - "/vote выбрать" (DM, admin-only) opens the moderation screen -- admit toggles, live
      counts, ballot settings, and closing the vote.
    - "/vote очистить" (DM, admin-only, tap-to-confirm) deletes the current poll outright.
    - "/vote chat" (DM, admin-only) drafts an announcement -- asks for the text via a
      force-reply, then sends that text plus the vote button, for now into the same DM
      (see handle_vote_chat_text_input).
    - bare "/vote" opens the actual ballot, for EVERYONE including an administrator --
      an admin is never forced into moderation mode just to cast their own vote. For an
      administrator specifically, it's also a status/control panel: current standings
      plus the full command list, since remembering four subcommands is more friction
      than a menu.

    `forced_mode` ("moderate", "clear", or "chat") is set by the /start deep-link an
    admin-only group message hands out for "выбрать"/"очистить"/"chat", bypassing the
    usual text parsing since a /start payload never carries the Russian word itself (see
    VOTE_MODERATE_WORDS/VOTE_CLEAR_WORDS/VOTE_CHAT_WORDS below). `vote_chat_flows` is
    required for "/vote chat" to have anywhere to remember it's waiting for text; every
    other subcommand ignores it.

    Every web_app button differs by chat type because Telegram only allows one in a
    private chat. In a group the same command therefore offers a plain link into the DM,
    where the real Mini App button is waiting: the round trip is what buys a signed
    identity, which is the whole basis for one-vote-per-person.
    """
    chat = message["chat"]
    chat_id = chat["id"]
    is_private = chat.get("type") == "private"
    user = message.get("from") or {}

    async def reply(text: str, reply_markup=None):
        try:
            return await api.send_message(
                chat_id, text, reply_to_message_id=message["message_id"],
                parse_mode=None, reply_markup=reply_markup,
            )
        except Exception as e:
            log(f"[bot_listener] failed to send the vote reply: {e}")
            return None

    page_url = _vote_page_url(cfg)
    if not page_url:
        await reply(
            "Голосование не настроено: не задан WEBAPP_PUBLIC_URL (нужен https-адрес приложения)."
        )
        return
    if not entry:
        await reply("Не настроен основной чат (LISTENER_ALLOWED_CHATS) -- нечего выносить на голосование.")
        return

    if forced_mode == "moderate":
        wants_collect, wants_moderate, wants_clear, wants_chat = False, True, False, False
    elif forced_mode == "clear":
        wants_collect, wants_moderate, wants_clear, wants_chat = False, False, True, False
    elif forced_mode == "chat":
        wants_collect, wants_moderate, wants_clear, wants_chat = False, False, False, True
    else:
        argument = stats.strip_command_bot_mention(message.get("text") or "", bot_username)
        for spelling in VOTE_COMMANDS:
            if argument.lower().startswith(spelling):
                argument = argument[len(spelling):]
                break
        normalized = argument.strip().lower()
        wants_collect = normalized in VOTE_COLLECT_WORDS
        wants_moderate = normalized in VOTE_MODERATE_WORDS
        wants_clear = normalized in VOTE_CLEAR_WORDS
        wants_chat = normalized in VOTE_CHAT_WORDS

    async def require_admin_in_dm(denial: str) -> bool:
        """Common gate for собрать/выбрать/очистить/chat: DM only, admin only. Returns
        whether the caller passed; the group-vs-DM split lives here once instead of being
        repeated for all four admin-only subcommands."""
        if not is_private:
            if not bot_username:
                await reply("Открой в личке с ботом.")
                return False
            start_payload = (
                "vote_admin" if wants_moderate else
                "vote_clear" if wants_clear else
                "vote_chat" if wants_chat else None
            )
            url = f"https://t.me/{bot_username}" + (f"?start={start_payload}" if start_payload else "")
            await reply(
                "Это только в личке с ботом:",
                reply_markup={"inline_keyboard": [[{"text": "Открыть в личке", "url": url}]]},
            )
            return False
        admin_chat_id = await _resolve_chat_id(telethon_client, entry, {}, log=log)
        if admin_chat_id is None or not await _can_manage_chat(api, admin_chat_id, user, entry):
            await reply(denial)
            return False
        return True

    if wants_collect:
        if not await require_admin_in_dm("Собирать заявки могут только администраторы."):
            return

        await reply("Собираю новые заявки с #итогинедели за сегодня и вчера -- это займёт минуту.")
        poll_id = _current_vote_poll_id(tz)
        existing_poll = voting.load_poll(entry, poll_id)
        known_ids = {e.entry_id for e in existing_poll.entries} if existing_poll else set()
        try:
            new_entries = await voting.collect_entries(
                client=telethon_client,
                chat_ref=entry,
                tz=tz,
                media_dir=voting.media_path(entry, poll_id),
                skip_entry_ids=known_ids,
                log=log,
            )
        except Exception:
            log(f"[bot_listener] collecting vote entries failed:\n{traceback.format_exc()}")
            await reply("Не получилось собрать заявки.")
            return

        # Already-known entries are carried over as-is, not re-fetched -- collect_entries
        # only ever resolves and returns what's new (see its docstring). Concatenating
        # rather than replacing is what makes build_poll's "known" set include them, so
        # their admitted/vote state survives untouched.
        all_entries = (existing_poll.entries if existing_poll else []) + new_entries
        poll = voting.build_poll(entry, poll_id, all_entries, existing=existing_poll)
        voting.save_poll(poll)
        log(f"[bot_listener] vote poll {poll_id}: {len(all_entries)} entries ({len(new_entries)} new), {len(poll.approved)} admitted")
        if not all_entries:
            await reply("За сегодня и вчера постов с #итогинедели не нашлось.")
            return
        summary = (
            f"Новых заявок: {len(new_entries)} (всего {len(all_entries)})." if new_entries
            else f"Новых заявок нет (всего {len(all_entries)})."
        )
        await reply(
            f"{summary} Открой модерацию и отметь, какие работы допустить.",
            reply_markup={"inline_keyboard": [[
                {"text": "🛠 Модерация заявок", "web_app": {"url": f"{page_url}?mode=admin"}}
            ]]},
        )
        return

    if wants_moderate:
        if not await require_admin_in_dm("Модерировать заявки могут только администраторы."):
            return
        await reply(
            "Модерация: отметь, какие работы допустить к голосованию, и закрой голосование, когда пора подводить итоги.",
            reply_markup={"inline_keyboard": [[
                {"text": "🛠 Модерация заявок", "web_app": {"url": f"{page_url}?mode=admin"}}
            ]]},
        )
        return

    if wants_clear:
        if not await require_admin_in_dm("Очищать голосование могут только администраторы."):
            return
        poll_id = _current_vote_poll_id(tz)
        if voting.load_poll(entry, poll_id) is None:
            await reply("Голосование за эту неделю ещё не создано -- нечего очищать.")
            return
        await reply(
            "Точно очистить голосование за эту неделю? Все заявки, голоса и настройки удалятся безвозвратно.",
            reply_markup={"inline_keyboard": [[
                {"text": "🗑 Да, очистить", "callback_data": _vote_clear_callback_data(chat_id, user.get("id"))}
            ]]},
        )
        return

    if wants_chat:
        if not await require_admin_in_dm("Готовить объявление могут только администраторы."):
            return
        if vote_chat_flows is None:
            await reply("Не получилось открыть черновик объявления -- попробуй ещё раз.")
            return
        # One pending draft per (chat, admin) at a time -- starting a new one abandons
        # whatever text prompt was already waiting, rather than accumulating stale flows
        # nobody will ever reply to.
        for old_flow_id, old_flow in list(vote_chat_flows.items()):
            if old_flow.get("chat_id") == chat_id and old_flow.get("user_id") == user.get("id"):
                vote_chat_flows.pop(old_flow_id, None)
        flow_id = uuid.uuid4().hex[:10]
        prompt = await reply(
            "Какой текст написать в объявлении о голосовании? Ответь на это сообщение.",
            reply_markup={"force_reply": True, "selective": True},
        )
        if prompt is None:
            return
        vote_chat_flows[flow_id] = {
            "chat_id": chat_id,
            "user_id": user.get("id"),
            "entry": entry,
            # Resolved again (require_admin_in_dm's own lookup isn't exposed) so the
            # consuming side (handle_vote_chat_text_input) can re-check admin status
            # without needing the Telethon client at all -- same "store what you'll need
            # to re-verify" convention badge_flows/cabinet_flows already follow.
            "admin_chat_id": await _resolve_chat_id(telethon_client, entry, {}, log=log),
            "prompt_message_id": prompt.get("message_id") if prompt else None,
            "created_at": time.monotonic(),
        }
        return

    # Bare "/vote": the actual ballot, for everyone -- an administrator gets this too,
    # unless they specifically asked for "выбрать". An administrator's bare /vote is also
    # a status/control panel: current standings, the full command list, and a button per
    # command, rather than just the vote button, since they're the one who has four
    # subcommands to remember.
    if is_private:
        admin_chat_id = await _resolve_chat_id(telethon_client, entry, {}, log=log)
        is_manager = admin_chat_id is not None and await _can_manage_chat(api, admin_chat_id, user, entry)
        if is_manager:
            text = (
                f"{_vote_status_text(entry)}\n\n"
                "Команды:\n"
                "/vote — открыть бюллетень (проголосовать)\n"
                "/vote выбрать — модерация заявок\n"
                "/vote собрать — собрать новые заявки\n"
                "/vote chat — подготовить объявление с кнопкой\n"
                "/vote очистить — очистить голосование"
            )
            admin_user_id = user.get("id")
            await reply(
                text,
                reply_markup={"inline_keyboard": [
                    [
                        {"text": VOTE_OPEN_BUTTON_TEXT, "web_app": {"url": page_url}},
                        {"text": "🛠 Модерация", "web_app": {"url": f"{page_url}?mode=admin"}},
                    ],
                    [
                        {
                            "text": "🔄 Собрать заявки",
                            "callback_data": _vote_action_callback_data("collect", chat_id, admin_user_id),
                        },
                        {
                            "text": "📣 Объявление",
                            "callback_data": _vote_action_callback_data("chat", chat_id, admin_user_id),
                        },
                    ],
                    [
                        {
                            "text": "🗑 Очистить",
                            "callback_data": _vote_action_callback_data("clear", chat_id, admin_user_id),
                        },
                    ],
                ]},
            )
        else:
            await reply(
                "Голосование за итоги недели:",
                reply_markup={"inline_keyboard": [[{"text": VOTE_OPEN_BUTTON_TEXT, "web_app": {"url": page_url}}]]},
            )
        return

    # In a group: a link into the DM, since a web_app button is private-chat only.
    if not bot_username:
        await reply("Открой голосование в личке с ботом.")
        return
    # Deliberately kept in the chat, unlike the stats replies this codebase otherwise
    # sweeps away as noise -- people need to be able to find the vote announcement later,
    # so it is never scheduled for auto-delete.
    await reply(
        "Голосование за итоги недели -- открывается в личке с ботом:",
        reply_markup={"inline_keyboard": [[
            {"text": VOTE_OPEN_BUTTON_TEXT, "url": f"https://t.me/{bot_username}?start=vote"}
        ]]},
    )
    # background_tasks is now unused in this function, but stays a required parameter --
    # callers (handle_vote_action_callback, _dispatch_update) pass it positionally.


async def handle_vote_chat_text_input(
    api: TelegramBotAPI,
    cfg,
    message: dict,
    vote_chat_flows: dict[str, dict],
    log=print,
) -> bool:
    """Consumes "/vote chat"'s force-reply and sends the finished announcement -- the
    admin's own text, plus the vote button -- into the same DM the draft was started in
    (see handle_vote_command's docstring: posting it into the actual group chat is a
    manual copy-paste away until that's wired up directly).

    Returns True once this message belonged to a pending draft, so the caller stops
    treating it as ordinary chat input -- same contract as handle_badge_text_input/
    handle_cabinet_text_input/handle_button_builder_text_input.
    """
    chat_id = message["chat"]["id"]
    actor = message.get("from") or {}
    replied_to = (message.get("reply_to_message") or {}).get("message_id")
    found = next(
        (
            (flow_id, flow)
            for flow_id, flow in vote_chat_flows.items()
            if flow.get("chat_id") == chat_id
            and flow.get("user_id") == actor.get("id")
            and flow.get("prompt_message_id") == replied_to
            and time.monotonic() - flow["created_at"] <= VOTE_CHAT_FLOW_TTL_SECONDS
        ),
        None,
    )
    if found is None:
        return False
    flow_id, flow = found
    vote_chat_flows.pop(flow_id, None)

    text = (message.get("text") or "").strip()
    if text.lower() in ("/cancel", "отмена"):
        await api.send_message(
            chat_id, "Черновик отменён.", reply_to_message_id=message["message_id"], parse_mode=None
        )
        return True
    if not await _can_manage_chat(api, flow["admin_chat_id"], actor, flow.get("entry")):
        return True  # silently dropped, same as badge_flows -- admin status changed mid-flow
    if not text:
        await api.send_message(
            chat_id, "Пустой текст -- объявление не отправлено.",
            reply_to_message_id=message["message_id"], parse_mode=None,
        )
        return True

    page_url = _vote_page_url(cfg)
    if not page_url:
        await api.send_message(
            chat_id, "Голосование не настроено -- некуда вести кнопку.",
            reply_to_message_id=message["message_id"], parse_mode=None,
        )
        return True

    try:
        await api.send_message(
            chat_id, text, parse_mode=None,
            reply_markup={"inline_keyboard": [[{"text": VOTE_OPEN_BUTTON_TEXT, "web_app": {"url": page_url}}]]},
        )
    except Exception as e:
        log(f"[bot_listener] failed to send the vote chat announcement: {e}")
        await api.send_message(
            chat_id, "Не получилось отправить объявление.",
            reply_to_message_id=message["message_id"], parse_mode=None,
        )
        return True
    log(f"[bot_listener] {actor.get('username') or actor.get('id')} sent a vote announcement ({len(text)} chars)")
    return True


async def handle_shop_command(
    api: TelegramBotAPI,
    telethon_client,
    cfg,
    tz,
    message: dict,
    command_text: str,
    entry: str,
    background_tasks: set,
    log=print,
) -> None:
    """/shop, /coins and /buy <item> [args].

    Every path resolves the requester through stats.resolve_stat_target first, because a
    balance is only meaningful next to the XP it is derived from (see economy.balance)
    and because it is also how somebody who is not tracked yet gets a clear answer rather
    than a zero balance.
    """
    chat_id = message["chat"]["id"]
    reply_to = message["message_id"]
    from_user = message.get("from") or {}
    lowered = command_text.lower()

    async def reply(text: str, parse_mode: str | None = None) -> None:
        try:
            sent = await api.send_message(
                chat_id, text, reply_to_message_id=reply_to, parse_mode=parse_mode
            )
            if sent and "message_id" in sent:
                schedule_bot_delete(
                    api, chat_id, [sent["message_id"]], STATS_DELETE_AFTER, log, background_tasks,
                    trigger_message_id=reply_to,
                )
        except Exception:
            log(f"[bot_listener] failed to answer a shop command:\n{traceback.format_exc()}")

    user, _, _, xp, _, _ = await stats.resolve_stat_target(
        telethon_client, entry, entry, "",
        from_user.get("username"), _display_name(from_user), tz, log=log,
    )
    if user is None:
        await reply("Ты ещё не отслеживаешься -- напиши что-нибудь в чат и попробуй снова.")
        return

    if lowered.startswith("/coins"):
        current = economy.balance(entry, user.user_id, xp)
        held = economy.streak_freezes(entry, user.user_id)
        text = f"🪙 У тебя {current:,} монет.".replace(",", ".")
        if held:
            text += f"\n❄️ Заморозок серии: {held}."
        await reply(text + "\n\nМагазин: /shop")
        return

    if lowered.startswith("/shop"):
        await reply(economy.format_shop(entry, user.user_id, xp), parse_mode="HTML")
        return

    # /buy
    argument = command_text[len("/buy") :].strip()
    code, _, extra = argument.partition(" ")
    item = economy.find_item(code)
    if item is None:
        await reply("Не знаю такой товар. Посмотри /shop.")
        return
    if item.code == "title" and not extra.strip():
        await reply(f"Формат: /buy title {item.argument_hint}")
        return

    ok, refusal, remaining = economy.purchase(entry, user.user_id, xp, item)
    if not ok:
        await reply(refusal)
        return

    # The title is stored directly rather than through _deliver_shop_item: it cannot
    # fail, and set_title is what stamps its own purchase cooldown.
    if item.code == "title":
        saved = economy.set_title(entry, user.user_id, extra)
        await reply(
            f"Титул «{saved}» активен {economy.TITLE_DAYS} дней.\n"
            f"🪙 Осталось: {remaining}."
        )
        return

    try:
        delivered_text = await _deliver_shop_item(
            api, telethon_client, cfg, tz, entry, entry, item, user, xp,
            _display_name(from_user), log,
        )
    except Exception:
        log(f"[bot_listener] shop delivery failed for {item.code}:\n{traceback.format_exc()}")
        restored = economy.refund(entry, user.user_id, xp, item.price, item.code)
        await reply(
            f"Не получилось выдать «{item.name}» -- монеты вернул.\n"
            f"🪙 Баланс: {restored}."
        )
        return

    await reply(f"{delivered_text}\n\n🪙 Осталось: {remaining}.")




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
    """Answers one summary enquiry -- ALWAYS in the requester's DM with the bot.

    A summary is long, it is for the person who asked, and a group that produces a dozen a
    day drowns in them. So a request made in a group is answered in that person's DM and
    the group gets only a receipt (see _post_summary_receipt), which takes the request
    down with it ten seconds later. A request already made in a DM is simply answered
    where it was asked -- same destination, no receipt, nothing to clean up.

    That makes an unopened DM a hard blocker rather than a degraded case: Telegram won't
    let a bot write first, so there is nowhere to deliver to. It's checked BEFORE any
    OpenAI work for that reason -- see _can_dm.
    """
    chat = message["chat"]
    chat_id = chat["id"]
    message_id = message["message_id"]
    text = _message_content(message)
    sender = message.get("from") or {}
    requester = _display_name(sender)
    chat_title_for_history = chat.get("title") or chat.get("first_name") or "Unknown chat"
    request_dt = datetime.fromtimestamp(message["date"], tz=timezone.utc)

    is_private = chat.get("type") == "private"
    # Where the answer goes. In a DM that is this very chat; from a group it is the
    # requester's own DM, whose chat_id for a private chat IS their user id.
    answer_chat_id = chat_id if is_private else sender.get("id")

    async def respond(answer: str, delete_after: int | None = None, record: bool = True) -> list[int]:
        try:
            sent_ids = await send_long_bot_message(
                api, answer_chat_id, answer,
                # A group request's message_id means nothing in the DM the answer lands
                # in, so there is nothing to reply to there.
                reply_to_message_id=message_id if is_private else None,
            )
        except Exception as e:
            # _can_dm said this DM was reachable, so getting here means something else
            # went wrong -- but from the group's side the outcome is the same: no answer
            # arrived, and saying "отправлено" would be a lie.
            log(f"[bot_listener] failed to deliver the answer to {answer_chat_id}: {e}")
            if not is_private:
                await _post_summary_receipt(
                    api, chat_id, message_id, SUMMARY_DM_CLOSED_TEXT, bot_username,
                    background_tasks, log=log,
                )
            return []
        if record:
            try:
                history.record(chat_title_for_history, requester, text, answer)
            except Exception as e:
                log(f"[bot_listener] failed to record history: {e}")
        if not is_private:
            await _post_summary_receipt(
                api, chat_id, message_id, SUMMARY_RECEIPT_TEXT, bot_username,
                background_tasks, log=log,
            )
        elif delete_after and sent_ids:
            # Only ever the DM's own short notices (the day limit, say). `delete_after` is
            # meaningless for an answer sent to a group's requester: the group's receipt
            # has its own sweep, and the DM copy is the one thing meant to be kept.
            schedule_bot_delete(
                api, chat_id, sent_ids, delete_after, log, background_tasks,
                trigger_message_id=message_id,
            )
        return sent_ids

    # A DM has no group history of its own -- redirect data fetching to the configured
    # home group, but keep replying/recording history against the DM itself (chat_id,
    # requester above are untouched). See _home_chat_ref.
    if is_private:
        if not home_chat_ref:
            await respond("Не настроен основной чат для личных сообщений -- обратитесь в группе.")
            return
        data_chat_ref = home_chat_ref
    else:
        if not await _can_dm(api, answer_chat_id, log=log):
            await _post_summary_receipt(
                api, chat_id, message_id, SUMMARY_DM_CLOSED_TEXT, bot_username,
                background_tasks, log=log,
            )
            return
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
            model=cfg.openai_routing_model,
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
                    resolve_name_hint, cfg.openai_api_key, cfg.openai_routing_model, username_hint, candidates
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

    await respond(f"{answer}\n\n{COMMANDS_FOOTER}")


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


async def handle_joke_preview_callback(
    api: TelegramBotAPI,
    telethon_client,
    callback: dict,
    joke_preview_pending: dict[int, dict],
    known_chat_ids: dict[str, int],
    joke_posted_queue,
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
    except Exception:
        log(f"[bot_listener] failed to send previewed joke:\n{traceback.format_exc()}")
        await api.answer_callback_query(callback["id"], text="Не удалось отправить.")


def _message_content(message: dict | None) -> str:
    """Best-effort readable content for a Bot API message.

    Direct replies are normally text, but treating captions, stickers, and common media
    as content keeps "reply to the bot" behavior consistent with normal Telegram chat.
    """
    if not message:
        return ""
    text = (message.get("text") or message.get("caption") or "").strip()
    if text:
        return text
    if message.get("sticker"):
        emoji = message["sticker"].get("emoji") or ""
        return f"[Sticker {emoji}]".strip()
    for field, label in (
        ("photo", "[Photo]"),
        ("video", "[Video]"),
        ("animation", "[GIF]"),
        ("voice", "[Voice message]"),
        ("video_note", "[Video note]"),
        ("document", "[File]"),
        ("poll", "[Poll]"),
        ("location", "[Location shared]"),
        ("contact", "[Contact shared]"),
    ):
        if message.get(field):
            return label
    return ""


def _is_direct_reply_to_bot(message: dict, bot_user_id: int) -> bool:
    """True only for a human's explicit Telegram Reply to this bot account."""
    sender = message.get("from") or {}
    replied_to = message.get("reply_to_message") or {}
    replied_sender = replied_to.get("from") or {}
    return not sender.get("is_bot", False) and replied_sender.get("id") == bot_user_id


async def handle_direct_bot_reply(
    api: TelegramBotAPI,
    telethon_client,
    cfg,
    tz,
    message: dict,
    matched_entry: str | None,
    log=print,
) -> None:
    """Answer one explicit reply to any message authored by this bot.

    Unlike the old follow-up watcher, this has no time/message window and does not need
    to remember which bot messages were sent during the current process lifetime. The
    Bot API embeds the replied-to message in the update, which is the authoritative
    signal and also gives the model the exact bot text being answered.
    """
    chat = message["chat"]
    chat_id = chat["id"]
    user_message = _message_content(message)
    bot_message = _message_content(message.get("reply_to_message"))
    sender_name = _display_name(message.get("from"))

    try:
        lines: list[str] = []
        profile = None
        if chat.get("type") != "private":
            # A fresh tail matters here: the response should know what people said just
            # now, not the possibly 30-minute-old reusable daily-summary cache.
            chat_ref = matched_entry or str(chat_id)
            _, recent_messages = await fetch_recent_messages_fresh(
                client=telethon_client,
                chat_ref=chat_ref,
                tz=tz,
                limit=CONTEXT_MESSAGE_COUNT,
                log=log,
            )
            lines = format_transcript_lines(recent_messages, include_date=False)

            # Reuse the same multi-day room-style profile as natural remarks. It is
            # regenerated only when its configured TTL expires, so normal replies don't
            # add a profile-generation call every time.
            profile = await chat_profile.ensure_profile(
                telethon_client,
                chat_ref,
                matched_entry or str(chat_id),
                cfg.openai_api_key,
                cfg.openai_model,
                tz,
                cfg.joke_profile_ttl_seconds,
                cfg.joke_profile_lookback_days,
                cfg.joke_profile_max_messages,
                log=log,
            )

        reply_text = await asyncio.to_thread(
            generate_direct_reply,
            cfg.openai_api_key,
            cfg.openai_model,
            bot_message,
            user_message,
            sender_name,
            lines,
            profile,
        )
        await api.send_message(
            chat_id,
            reply_text,
            reply_to_message_id=message["message_id"],
            parse_mode=None,
        )
        log(
            f"[bot_listener] answered direct reply from {sender_name} in "
            f"'{chat.get('title', chat_id)}': {reply_text!r}"
        )
    except Exception:
        log(f"[bot_listener] error generating direct conversational reply:\n{traceback.format_exc()}")
        try:
            await api.send_message(
                chat_id,
                "Не получилось ответить — попробуй ещё раз.",
                reply_to_message_id=message["message_id"],
                parse_mode=None,
            )
        except Exception:
            pass


async def _handle_one_update(dispatch, update_id, log=print) -> None:
    """Await one update's handling, and never let it stop the poll loop.

    Updates are processed one at a time, so this is the only thing standing between a
    single stuck handler and a bot that answers nothing at all. That is not hypothetical:
    a Telethon call on a session that cannot connect waits indefinitely instead of
    raising, and it took the whole bot down with it -- the reported symptom being an
    unrelated menu that "stopped opening", long after whatever actually wedged it.

    Losing one update to a timeout is strictly better than losing every update after it.
    """
    try:
        await asyncio.wait_for(dispatch, timeout=UPDATE_HANDLING_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        log(
            f"[bot_listener] update {update_id} took longer than "
            f"{UPDATE_HANDLING_TIMEOUT_SECONDS}s and was abandoned so the bot keeps answering "
            f"-- something it awaited is not coming back"
        )
    except Exception:
        log(f"[bot_listener] unhandled error processing update {update_id}:\n{traceback.format_exc()}")


async def _dispatch_update(
    update: dict,
    api: TelegramBotAPI,
    telethon_client,
    cfg,
    tz,
    bot_username: str | None,
    bot_user_id: int,
    allowed_chats: set[str],
    summary_queue: asyncio.Queue,
    background_tasks: set,
    home_chat_ref: str | None,
    known_chat_ids: dict[str, int],
    joke_preview_pending: dict[int, dict],
    joke_posted_queue,
    badge_flows: dict[str, dict],
    cabinet_flows: dict[str, dict],
    menu_last_sent: dict,
    button_builder_flows: dict[str, dict] | None = None,
    vote_chat_flows: dict[str, dict] | None = None,
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
    button_builder_flows = button_builder_flows if button_builder_flows is not None else {}
    vote_chat_flows = vote_chat_flows if vote_chat_flows is not None else {}
    callback = update.get("callback_query")
    if callback is not None:
        callback_data = callback.get("data") or ""
        if callback_data.startswith(f"{cabinet.CALLBACK_PREFIX}:"):
            await handle_cabinet_callback(
                api, telethon_client, cfg, tz, callback, home_chat_ref, cabinet_flows,
                badge_flows=badge_flows, known_chat_ids=known_chat_ids, log=log,
            )
        elif callback_data.startswith(f"{BADGE_CALLBACK_PREFIX}:"):
            await handle_badge_callback(api, callback, badge_flows)
        elif callback_data.startswith(f"{PLANT_CALLBACK_PREFIX}:"):
            await handle_plant_callback(api, callback, home_chat_ref, log=log)
        elif callback_data.startswith(f"{poker.CALLBACK_PREFIX}:"):
            # No chat resolution here: the table carries its own chat id, so not one of
            # these buttons ever touches the Telethon session.
            await handle_poker_callback(api, callback, home_chat_ref, log=log)
        elif callback_data.startswith(f"{button_builder.CALLBACK_PREFIX}:"):
            await handle_button_builder_callback(
                api, callback, home_chat_ref, button_builder_flows, log=log
            )
        elif callback_data.startswith(f"{preview.CALLBACK_PREFIX}:"):
            # The chat is resolved inside, AFTER the spinner is stopped -- see the
            # docstring. Doing it here, in the argument list, is what made these hang.
            await handle_preview_callback(
                api, telethon_client, callback, home_chat_ref, known_chat_ids, log=log,
            )
        elif callback_data.startswith(f"{JOKE_PREVIEW_CALLBACK_PREFIX}:"):
            await handle_joke_preview_callback(
                api, telethon_client, callback, joke_preview_pending, known_chat_ids,
                joke_posted_queue, log=log,
            )
        elif callback_data.startswith(f"{VOTE_CLEAR_CALLBACK_PREFIX}:"):
            await handle_vote_clear_callback(api, cfg, tz, callback, home_chat_ref, log=log)
        elif callback_data.startswith(f"{VOTE_ACTION_CALLBACK_PREFIX}:"):
            await handle_vote_action_callback(
                api, telethon_client, cfg, tz, callback, home_chat_ref, bot_username,
                background_tasks, vote_chat_flows, log=log,
            )
        else:
            # Unrecognized callback_data -- answer it anyway so the tapped button's spinner
            # doesn't hang forever on the client.
            await api.answer_callback_query(callback["id"])
        return

    message = update.get("message")
    if not message:
        return
    message_text = message.get("text") or message.get("caption") or ""

    # Learned regardless of whether this message is a trigger -- this is how
    # known_chat_ids (see run_bot_listener's joke queue consumer) finds out the Bot-API
    # chat_id for a chat named in LISTENER_ALLOWED_CHATS, since there's no way to look
    # that up on demand (getChat needs an id/username we don't have yet either). Placed
    # before the has_summary early-return so it also learns from ordinary chat
    # messages whenever the bot's privacy mode is off, not just from /summary requests.
    chat = message["chat"]
    matched_entry = _match_allowed_chat(chat, cfg.listener_allowed_chats)
    if matched_entry is not None:
        known_chat_ids[matched_entry] = chat["id"]

    command_text = stats.strip_command_bot_mention(message_text, bot_username)
    start_match = re.match(r"^/start(?:\s+(\S+))?\s*$", command_text, re.IGNORECASE)
    if start_match:
        # Where /stat's "Открыть личный кабинет" link (t.me/<bot>?start=cabinet) and the
        # group /vote buttons' DM links (?start=vote, ?start=vote_admin, ?start=vote_clear,
        # ?start=vote_chat) land, and the natural first thing a new member does anyway.
        # Groups are ignored: a
        # /start there is somebody's fat finger, not a request. Any payload other than the
        # vote ones -- including none at all -- opens the cabinet, matching the old
        # unconditional behavior.
        if chat.get("type") != "private":
            return
        start_payload = (start_match.group(1) or "").lower()
        if start_payload in ("vote", "vote_admin", "vote_clear", "vote_chat"):
            forced_mode = {"vote_admin": "moderate", "vote_clear": "clear", "vote_chat": "chat"}.get(start_payload)
            await handle_vote_command(
                api, telethon_client, cfg, tz, message,
                _stats_entry_for(chat, matched_entry, home_chat_ref), bot_username,
                background_tasks, log=log,
                forced_mode=forced_mode, vote_chat_flows=vote_chat_flows,
            )
            return
        await handle_cabinet_command(api, telethon_client, tz, message, home_chat_ref, log=log)
        return
    if re.match(rf"^{re.escape(CABINET_COMMAND)}(?:\s|$)", command_text, re.IGNORECASE):
        # In a group this would print somebody's balance for everyone and offer buttons
        # that spend their coins, so it points them at the DM instead of refusing.
        if chat.get("type") != "private":
            try:
                sent = await api.send_message(
                    chat["id"], CABINET_DM_ONLY_NOTICE,
                    reply_to_message_id=message["message_id"], parse_mode=None,
                )
                if sent and "message_id" in sent:
                    schedule_bot_delete(
                        api, chat["id"], [sent["message_id"]], STATS_DELETE_AFTER, log, background_tasks,
                        trigger_message_id=message["message_id"],
                    )
            except Exception:
                pass
            return
        await handle_cabinet_command(api, telethon_client, tz, message, home_chat_ref, log=log)
        return
    if re.match(rf"^{re.escape(REPLANT_COMMAND)}(?:\s|$)", command_text, re.IGNORECASE):
        if chat.get("type") != "private":
            return
        admin_chat_id = (
            await _resolve_chat_id(telethon_client, home_chat_ref, known_chat_ids, log=log)
            if home_chat_ref
            else None
        )
        await handle_replant_command(
            api, telethon_client, message, home_chat_ref, admin_chat_id, tz, log=log,
        )
        return
    if any(
        re.match(rf"^{re.escape(spelling)}(?:\s|$)", command_text, re.IGNORECASE)
        for spelling in PLANT_COMMANDS
    ):
        # Unlike the other management commands this one is NOT DM-only: it was asked for
        # as something an admin types in the chat, in front of everybody, and the reply
        # follows them to wherever they typed it.
        admin_chat_id = (
            await _resolve_chat_id(telethon_client, home_chat_ref, known_chat_ids, log=log)
            if home_chat_ref
            else None
        )
        await handle_plant_command(api, message, home_chat_ref, admin_chat_id, log=log)
        return
    if any(
        re.match(rf"^{re.escape(spelling)}(?:\s|$)", command_text, re.IGNORECASE)
        for spelling in PLANT_REMINDER_COMMANDS
    ):
        admin_chat_id = (
            await _resolve_chat_id(telethon_client, home_chat_ref, known_chat_ids, log=log)
            if home_chat_ref
            else None
        )
        await handle_plant_reminder_command(
            api, message, home_chat_ref, admin_chat_id, log=log
        )
        return
    if any(
        re.match(rf"^{re.escape(spelling)}(?:\s|$)", command_text, re.IGNORECASE)
        for spelling in POKER_COMMANDS
    ):
        # Not DM-only: the table is a group event, and the dealer opens it in front of
        # everybody. Typed in the DM it still posts to the chat, like /plant.
        admin_chat_id = (
            await _resolve_chat_id(telethon_client, home_chat_ref, known_chat_ids, log=log)
            if home_chat_ref
            else None
        )
        await handle_poker_command(
            api, message, command_text, home_chat_ref, admin_chat_id, log=log
        )
        return
    if re.match(rf"^{re.escape(SEND_COMMAND)}(?:\s|$)", command_text, re.IGNORECASE):
        if chat.get("type") != "private":
            return
        admin_chat_id = (
            await _resolve_chat_id(telethon_client, home_chat_ref, known_chat_ids, log=log)
            if home_chat_ref
            else None
        )
        await handle_send_command(
            api, message, command_text, home_chat_ref, admin_chat_id, log=log
        )
        return
    if re.match(rf"^{re.escape(PREVIEW_COMMAND)}(?:\s|$)", command_text, re.IGNORECASE):
        if chat.get("type") != "private":
            return
        # The chat is NOT resolved here. Resolving goes through the Telethon session, and
        # doing it in the argument list means even the paths that never need it pay for
        # it -- which is how a menu that only writes back to this DM ended up hanging.
        await handle_preview_command(
            api, telethon_client, message, command_text, home_chat_ref, known_chat_ids, log=log,
        )
        return
    if re.match(rf"^{re.escape(BUTTON_BUILDER_COMMAND)}(?:\s|$)", command_text, re.IGNORECASE):
        if chat.get("type") != "private":
            return
        admin_chat_id = (
            await _resolve_chat_id(telethon_client, home_chat_ref, known_chat_ids, log=log)
            if home_chat_ref
            else None
        )
        await handle_button_builder_command(
            api, message, home_chat_ref, admin_chat_id, button_builder_flows
        )
        return
    if re.match(rf"^{re.escape(BADGE_ADMIN_COMMAND)}(?:\s|$)", command_text, re.IGNORECASE):
        if chat.get("type") != "private":
            return
        admin_chat_id = (
            await _resolve_chat_id(telethon_client, home_chat_ref, known_chat_ids, log=log)
            if home_chat_ref
            else None
        )
        await handle_badge_admin_command(
            api, telethon_client, message, command_text, home_chat_ref, admin_chat_id, tz, log=log,
        )
        return
    if re.match(r"^/badge(?:\s|$)", command_text, re.IGNORECASE):
        if chat.get("type") != "private":
            return
        admin_chat_id = (
            await _resolve_chat_id(telethon_client, home_chat_ref, known_chat_ids, log=log)
            if home_chat_ref
            else None
        )
        await handle_badge_command(
            api,
            message,
            home_chat_ref,
            admin_chat_id,
            badge_flows,
        )
        return
    if re.match(rf"^{re.escape(WEEK_WINNER_COMMAND)}(?:\s|$)", command_text, re.IGNORECASE):
        if chat.get("type") != "private":
            return
        admin_chat_id = (
            await _resolve_chat_id(telethon_client, home_chat_ref, known_chat_ids, log=log)
            if home_chat_ref
            else None
        )
        await handle_week_winner_command(
            api,
            telethon_client,
            message,
            command_text,
            home_chat_ref,
            admin_chat_id,
            tz,
            log=log,
        )
        return
    if re.match(rf"^{re.escape(DELETE_POKRAS_COMMAND)}(?:\s|$)", command_text, re.IGNORECASE):
        if chat.get("type") != "private":
            return
        admin_chat_id = (
            await _resolve_chat_id(telethon_client, home_chat_ref, known_chat_ids, log=log)
            if home_chat_ref
            else None
        )
        await handle_delete_pokras_command(
            api,
            telethon_client,
            message,
            command_text,
            home_chat_ref,
            admin_chat_id,
            tz,
            log=log,
        )
        return

    if await handle_button_builder_text_input(
        api, message, button_builder_flows, log=log
    ):
        return

    if await handle_cabinet_text_input(
        api, telethon_client, tz, message, cabinet_flows, log=log
    ):
        return

    if await handle_badge_text_input(
        api, telethon_client, message, tz, badge_flows, log=log
    ):
        return

    if await handle_vote_chat_text_input(
        api, cfg, message, vote_chat_flows, log=log
    ):
        return

    # "пошути"/"пошути превью" (see JOKE_PREVIEW_* constants) only ever fires from a DM to
    # the bot, per JOKE_MANUAL_TRIGGER_KEYWORD's own docs -- checked before has_summary
    # since it's a wholly separate trigger with its own keyword(s). The longer
    # "preview" phrase is checked first since it contains the plain trigger word too.
    if chat.get("type") == "private" and message_text:
        stripped = message_text.lower()
        wants_preview = cfg.joke_manual_preview_keyword in stripped
        if wants_preview or cfg.joke_manual_trigger_keyword in stripped:
            task = asyncio.create_task(
                handle_manual_joke(
                    api, telethon_client, cfg, tz, message, wants_preview, home_chat_ref,
                    known_chat_ids, joke_preview_pending, joke_posted_queue, log=log,
                )
            )
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)
            return

    text_lower = message_text.lower()

    # Shop and wallet commands (see economy.py). Same chat gating as /stat: they read and
    # spend against a tracked chat's ledger, so there is nothing meaningful to answer for
    # a chat that isn't tracked.
    if cfg.stats_enabled and text_lower.startswith(SHOP_COMMANDS):
        shop_entry = _stats_entry_for(chat, matched_entry, home_chat_ref)
        if shop_entry is None:
            return
        shop_text = stats.strip_command_bot_mention(message_text, bot_username)
        task = asyncio.create_task(
            handle_shop_command(
                api, telethon_client, cfg, tz, message, shop_text, shop_entry,
                background_tasks, log=log,
            )
        )
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return

    # "/vote" / "/голосование" -- the weekly-contest voting Mini App (voting.py /
    # vote_web.py). Same chat resolution as the stats commands.
    if any(command_text.lower().startswith(c) for c in VOTE_COMMANDS):
        vote_entry = _stats_entry_for(chat, matched_entry, home_chat_ref)
        if vote_entry is None:
            return
        task = asyncio.create_task(
            handle_vote_command(
                api, telethon_client, cfg, tz, message, vote_entry, bot_username,
                background_tasks, log=log, vote_chat_flows=vote_chat_flows,
            )
        )
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return

    # "/tree" -- the chat's shared ЕПХ tree (see tree.py). Same chat resolution as the
    # stats commands, so it answers in a DM about the home chat too.
    if cfg.stats_enabled and text_lower.startswith("/tree"):
        tree_entry = _stats_entry_for(chat, matched_entry, home_chat_ref)
        if tree_entry is None:
            return
        task = asyncio.create_task(
            handle_tree_command(
                api, telethon_client, tz, message, tree_entry, background_tasks, log=log
            )
        )
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return

    # "/top today|week|month|all" and "/stat [username]" (stats.py) -- plain lookups over
    # already-computed daily files, so they bypass the OpenAI summary queue. Reuses matched_entry
    # from the known_chat_ids learning above rather than re-matching the chat.
    if cfg.stats_enabled and (text_lower.startswith("/top") or text_lower.startswith("/stat")):
        chat_key = chat["id"]
        # In a DM this resolves to the configured home chat, so /stat and /top work from
        # the published menu instead of reporting themselves unavailable.
        matched_entry = _stats_entry_for(chat, matched_entry, home_chat_ref)
        if matched_entry is None:
            try:
                sent = await api.send_message(
                    chat_key, "Статистика недоступна в этом чате.",
                    reply_to_message_id=message["message_id"], parse_mode=None,
                )
                if sent and "message_id" in sent:
                    schedule_bot_delete(
                        api, chat_key, [sent["message_id"]], STATS_DELETE_AFTER, log, background_tasks,
                        trigger_message_id=message["message_id"],
                    )
            except Exception:
                pass
            return
        # Strips a same-account "@bot_username" mention Telegram tacks onto the command
        # with no space (e.g. "/stat@Trash_Modelist") before parsing the period/username
        # argument -- see strip_command_bot_mention in stats.py.
        stats_text = stats.strip_command_bot_mention(message_text, bot_username)
        try:
            level_announcements = []
            reply_parse_mode = None
            if text_lower.startswith("/top"):
                top_arg = stats_text[len("/top"):].strip()
                # "/top pokras" reads the same way "/stat pokras" always has, so both
                # spellings reach the procrastinator list instead of one of them
                # silently falling through to a leaderboard for "today".
                if stats.is_procrastinator_command(top_arg):
                    reply_text = await stats.format_procrastinators(
                        telethon_client, matched_entry, matched_entry, tz, log=log
                    ) or PROCRASTINATOR_NONE_FOUND_MESSAGE
                else:
                    period = stats.parse_top_argument(top_arg)
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
                    user, rank, total, xp, streak, season_xp = await stats.resolve_stat_target(
                        telethon_client, matched_entry, matched_entry, arg,
                        from_user.get("username"), _display_name(from_user), tz, log=log,
                        frozen_days_for=economy.streak_freeze_lookup(matched_entry),
                    )
                    if user:
                        figurine_links = stats.figurine_message_links(chat.get("username"), chat_key, user)
                        best_work_link, workplace_link = stats.showcase_message_links(
                            chat.get("username"), chat_key, user
                        )
                        custom_badges = (
                            stats.custom_badges_for_user(matched_entry, user.user_id)
                            + stats.weekly_winner_badges_for_user(matched_entry, user.user_id)
                        )
                        reply_text = stats.format_stat(
                            user, rank, total, xp, streak, figurine_links, custom_badges,
                            best_work_link=best_work_link, workplace_link=workplace_link,
                            season_xp=season_xp, bot_username=bot_username,
                            work_names=stats.work_name_list(matched_entry, user),
                            **economy.stat_extras(matched_entry, user.user_id, xp, user),
                        )
                        reply_parse_mode = "HTML"
                        level_announcements = stats.record_level_observations(
                            matched_entry, [(user, xp)]
                        )
                    else:
                        reply_text = "Статистика не найдена -- пользователь ещё не отслеживается."
            # Direct /stat output is safely HTML-escaped by stats.format_stat so its
            # numbered work links can be clickable. Leaderboards/digests remain plain
            # text because they can contain uncontrolled display names.
            sent = await api.send_message(
                chat_key,
                reply_text,
                reply_to_message_id=message["message_id"],
                parse_mode=reply_parse_mode,
            )
            if sent and "message_id" in sent:
                schedule_bot_delete(
                    api, chat_key, [sent["message_id"]], STATS_DELETE_AFTER, log, background_tasks,
                    trigger_message_id=message["message_id"],
                )
            for announcement in level_announcements:
                try:
                    await api.send_message(chat_key, announcement, parse_mode=None)
                except Exception as e:
                    log(f"[bot_listener] failed to send level-up announcement: {e}")
        except Exception:
            log(f"[bot_listener] error handling stats command:\n{traceback.format_exc()}")
            try:
                sent = await api.send_message(
                    chat_key, "Не удалось получить статистику.",
                    reply_to_message_id=message["message_id"], parse_mode=None,
                )
                if sent and "message_id" in sent:
                    schedule_bot_delete(
                        api, chat_key, [sent["message_id"]], STATS_DELETE_AFTER, log, background_tasks,
                        trigger_message_id=message["message_id"],
                    )
            except Exception:
                pass
        return

    has_summary = any(k in text_lower for k in cfg.listener_trigger_keywords)

    # A direct Telegram Reply to this bot is normal conversational input. It is handled
    # immediately and independently of JOKE_ENABLED: that flag only controls unprompted
    # ambient remarks. Explicit commands keep their existing specialized handlers.
    #
    # Turned off -- forced False rather than removing the handler below, so it stays a
    # one-line revert if it's ever turned back on.
    direct_reply_enabled = False
    if (
        direct_reply_enabled
        and not has_summary
        and _is_direct_reply_to_bot(message, bot_user_id)
        and _message_content(message)
    ):
        if not _is_chat_allowed(allowed_chats, chat):
            return
        task = asyncio.create_task(
            handle_direct_bot_reply(
                api, telethon_client, cfg, tz, message, matched_entry, log=log
            )
        )
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return

    if not has_summary:
        # Nothing above wanted this message. In a DM that means the person typed
        # something the bot has no specific answer for, so show them what it CAN do
        # rather than saying nothing at all. Groups fall through silently as before.
        await maybe_send_menu(
            api, telethon_client, tz, message, home_chat_ref,
            cabinet_flows, badge_flows, menu_last_sent,
            button_builder_flows=button_builder_flows, log=log,
        )
        return

    if not _is_chat_allowed(allowed_chats, chat):
        return

    chat_key = chat["id"]
    sender = message.get("from") or {}
    sender_id = sender.get("id")

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

    await api.set_message_reaction(chat_key, message["message_id"], SUMMARY_ACK_EMOJI, log=log)
    await summary_queue.put(message)
    log(
        f"[bot_listener] queued request #{summary_queue.qsize()} from "
        f"'{chat.get('title', chat_key)}': {message_text!r}"
    )


async def run_bot_listener(
    bot_token: str,
    cfg,
    tz,
    telethon_client,
    log=print,
    joke_queue: "asyncio.Queue | None" = None,
    joke_posted_queue: "asyncio.Queue | None" = None,
    figurine_ack_queue: "asyncio.Queue | None" = None,
    stats_digest_queue: "asyncio.Queue | None" = None,
    dismiss_queue: "asyncio.Queue | None" = None,
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

    `figurine_ack_queue`, if given, carries (allowed_chats entry, message_id) pairs put
    there by listener.py's on_message the instant it sees a #япокрасил+photo/video post and
    bumps the counter (stats.record_figurine_live) -- only the reaction itself is done
    here, via the bot account, same bot-account-only rule as every other reply.

    `stats_digest_queue`, if given, carries (allowed_chats entry, text, parse_mode, image
    path or None) tuples -- the image being the tree's current stage picture, posted as a
    photo with the text as its caption. Put there
    every stats.PROCRASTINATOR_DIGEST_INTERVAL_DAYS days by listener.py's
    run_stats_rollover -- the "Топ покрастинаторов" call-out (see
    stats.format_procrastinators) -- sent here as a plain message, same account as
    everything else, and deliberately never scheduled for deletion (unlike the on-demand
    "/stat pokras" reply, which self-deletes like every other /stat or /top reply): this
    is an ambient reminder meant to stay visible in the chat.

    `dismiss_queue`, if given, carries (chat_id, message_id) pairs from listener.py's
    thumbs-up dismiss shortcut (_maybe_dismiss_on_thumbs_up) whenever the reacted-to
    message was sent by this bot account -- that session's Telethon client typically has
    no delete rights over a message it didn't send itself, but this account can always
    delete its OWN messages via the Bot API regardless of admin status, so the deletion
    itself has to happen here.

    All queues are left None when run standalone (this module's own main()), which
    just means jokes/figurine reactions/digests/dismissals never fire, matching that
    listener.py isn't running their activity tracking either in that mode. Direct
    replies still work standalone because the Bot API update carries the replied-to
    message itself."""
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
    # Short-lived, admin-bound /badge conversations. Definitions and assignments are
    # persisted by stats.py; only the in-progress menu/prompt state lives in memory.
    badge_flows: dict[str, dict] = {}
    # Short-lived /cabinet force-reply steps (setting a title, sending coins). Cabinet
    # *navigation* deliberately keeps no state at all -- every button carries its own
    # owner id -- so only these two text-entry prompts need correlating, and losing them
    # on a restart costs the member one re-press, nothing more.
    cabinet_flows: dict[str, dict] = {}
    # Short-lived /buttons conversations. Published posts and their counters are
    # persisted separately by stats.py; only the unfinished constructor lives here.
    button_builder_flows: dict[str, dict] = {}
    # Short-lived "/vote chat" draft-text prompts. The finished announcement itself is
    # just sent, not persisted anywhere -- losing this on a restart costs the admin one
    # re-press, same as every other force-reply flow here.
    vote_chat_flows: dict[str, dict] = {}
    # Last time the fallback menu was sent per DM chat_id, so a burst of messages
    # gets one menu rather than one each (see MENU_FALLBACK_COOLDOWN_SECONDS).
    menu_last_sent: dict[int, float] = {}

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
        await register_bot_menu(api, log=log)
        if home_chat_ref:
            # So the «Диллер» badge is already in the /badgeadmin list waiting to be
            # given, rather than something an administrator has to know to create first.
            try:
                if poker.ensure_dealer_badge(home_chat_ref):
                    log(f"[bot_listener] created the {poker.DEALER_BADGE_EMOJI} {poker.DEALER_BADGE_NAME} badge")
            except Exception:
                log(f"[bot_listener] could not ensure the dealer badge:\n{traceback.format_exc()}")
        log(
            f"[bot_listener] logged in as @{bot_username or me.get('id')}. Long-polling for "
            f"{cfg.listener_trigger_keywords} (summary) and direct replies. FIFO queue delay: "
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
                    await _handle_one_update(
                        _dispatch_update(
                            update, api, telethon_client, cfg, tz, bot_username, me["id"], allowed_chats,
                            summary_queue, background_tasks, home_chat_ref,
                            known_chat_ids, joke_preview_pending, joke_posted_queue, badge_flows,
                            cabinet_flows, menu_last_sent,
                            button_builder_flows=button_builder_flows, vote_chat_flows=vote_chat_flows, log=log,
                        ),
                        update.get("update_id"),
                        log=log,
                    )

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
                        f"'{chat.get('title', chat['id'])}': {_message_content(message)!r}"
                    )
                    try:
                        await handle_bot_summary_request(
                            api, telethon_client, cfg, tz, bot_username, message,
                            background_tasks, home_chat_ref, log=log,
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
                except Exception:
                    log(f"[bot_listener] failed to send joke:\n{traceback.format_exc()}")

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
                entry, text, parse_mode, photo = await stats_digest_queue.get()
                chat_id = await _resolve_chat_id(telethon_client, entry, known_chat_ids, log=log)
                if chat_id is None:
                    log(f"[bot_listener] dropping stats notification for '{entry}': could not resolve a chat_id for it")
                    continue
                await send_stats_digest(api, chat_id, entry, text, parse_mode, photo, log=log)

        async def _consume_dismissals():
            while True:
                chat_id, message_id = await dismiss_queue.get()
                # schedule_bot_delete already does the DISMISS_DELETE_AFTER wait via a
                # background task, so this loop isn't blocked from picking up the next
                # dismissal while one is still pending.
                schedule_bot_delete(api, chat_id, [message_id], DISMISS_DELETE_AFTER, log, background_tasks)

        async def _is_vote_admin(user: dict) -> bool:
            """Who may admit nominations, see live counts, and close the vote on the
            voting page -- reuses the same "chat admin or delegate" rule as every other
            moderation surface (_can_manage_chat). Takes the FULL Telegram user dict
            Telegram's signed initData verified, not just the id: _can_manage_chat's
            PRIVILEGED_MANAGEMENT_USERNAMES check needs the username too."""
            if not home_chat_ref:
                return False
            admin_chat_id = await _resolve_chat_id(telethon_client, home_chat_ref, known_chat_ids, log=log)
            if admin_chat_id is None:
                return False
            return await _can_manage_chat(api, admin_chat_id, user, home_chat_ref)

        async def _is_vote_member(user: dict) -> bool:
            """The "голосовать могут только подписчики" gate: only members of the home
            chat may cast a ballot. Fails closed -- an unresolvable home chat blocks
            voting entirely rather than letting a stranger through, the same tradeoff
            _is_vote_admin makes above."""
            if not home_chat_ref:
                return False
            admin_chat_id = await _resolve_chat_id(telethon_client, home_chat_ref, known_chat_ids, log=log)
            if admin_chat_id is None:
                log("[bot_listener] /vote membership check: could not resolve the home chat -- denying the vote.")
                return False
            user_id = user.get("id")
            if user_id is None:
                return False
            return await _is_chat_member(api, admin_chat_id, user_id)

        async def _announce_vote_winner(user: dict, poll, top: list) -> None:
            """Sends the winner announcement -- for now into the admin's own DM with the
            bot (the same chat the Mini App was opened from), not the group. `user` is
            whoever closed the vote, taken from the page's own verified identity rather
            than re-deriving "the admin" some other way. `top` is poll.tally()'s ranked
            (Entry, votes) pairs, already sliced to at most 3, winner first.

            Reaches directly into voting's on-disk media rather than re-downloading:
            collect_entries already pulled every photo down when the poll was built."""
            chat_id = user.get("id")
            if not top or chat_id is None:
                return
            winner_entry, winner_votes = top[0]

            lines = [f"🏆 Итоги голосования за {poll.entry}"]
            for medal, (entry, votes) in zip(_VOTE_MEDALS, top):
                if votes <= 0:
                    break  # don't pad the runners-up with entries nobody voted for
                lines.append("")
                lines.append(f"{medal} {_vote_who(entry)} — {votes} голосов")
                if entry is winner_entry and entry.text:
                    lines.append(entry.text)
            text = "\n".join(lines)

            photo_path = None
            # Same rule as send_stats_digest: a caption over Telegram's limit is rejected
            # outright, so a long post text falls back to plain text rather than losing
            # the announcement entirely.
            if winner_entry.media and len(text) <= CAPTION_LIMIT:
                candidate = voting.media_path(poll.entry, poll.poll_id) / winner_entry.media[0]
                if candidate.is_file():
                    photo_path = candidate
            if photo_path is not None:
                await api.send_photo_file(chat_id, photo_path, caption=text, parse_mode=None)
            else:
                await api.send_message(chat_id, text, parse_mode=None)

        tasks = [
            _poll_loop(),
            _consume_summaries(),
            _button_counter_refresh_loop(api, home_chat_ref, log=log),
        ]
        if joke_queue is not None:
            tasks.append(_consume_jokes())
        if figurine_ack_queue is not None:
            tasks.append(_consume_figurine_acks())
        if stats_digest_queue is not None:
            tasks.append(_consume_stats_digests())
        if dismiss_queue is not None:
            tasks.append(_consume_dismissals())
        if cfg.webapp_port:
            # PORT is set by the host (Railway does this automatically for any service
            # with public networking on); off when running locally without it, same as
            # every other optional piece here.
            tasks.append(
                vote_web.run_web_server(
                    cfg, home_chat_ref or "", _is_vote_admin, cfg.webapp_port,
                    announce=_announce_vote_winner, log=log, is_member=_is_vote_member,
                )
            )
        else:
            log("[bot_listener] PORT is not set -- the voting page is not being served.")
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
