"""Long-polls the Telegram Bot HTTP API for /summary requests and every other command in
the same chats listener.py's Telethon-based listener watches, and answers them as the bot
account instead of your personal account.

Why this exists alongside listener.py: a bot account lets people trigger this without it
coming from (or being confused with) your own account. The tradeoff is that the Bot API
gives a bot no retroactive access to chat history at all -- it only ever sees messages
sent after it's added to a chat. So message fetching here still goes through the already-
connected Telethon `client` passed into run_bot_listener() (same
fetch_range_messages_cached() listener.py itself uses); only trigger detection and
replying happen over the bot's HTTP API.

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
deletes it outright; "/vote chat" (DM, admin-only) drafts an announcement and posts it to
the chats the admin picks; "/vote картинка" (DM, admin-only) renders the standings as one
picture (vote_image.py) and sends it back as a file. See handle_vote_command's docstring.

Run with: python bot_listener.py (standalone, using load_config()'s own Telethon
session) -- or, more commonly, let listener.py's main() start this automatically
alongside its own Telethon listener when TELEGRAM_BOT_TOKEN is set.
"""

import asyncio
import html
import re
import secrets
import sys
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone

import aiohttp
from telethon import utils as tl_utils

import arena
import arena_core
import arena_web
import cabinet
import button_builder
import economy
import history
import pets
import pets_combat
import pets_config as C
import pets_image
import pets_ui
import poker
import preview
import stats
import vote_image
import vote_web
import voting
from bot_api import CAPTION_LIMIT, TelegramBotAPI
from config import SUMMARY_COMMAND, build_session, load_config
from critique import critique_work
from errors import ChatSummaryError
from intent import resolve_name_hint
from intent_v2 import route_request
from listener import (
    BLOCKED_FILE_NOTICE_DELETE_AFTER,
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
    is_summary_command,
    resolve_time_window,
)
from main import period_label, resolve_tz
from responder_v2 import answer_request
import tree
from telegram_fetch import (
    fetch_range_messages_cached,
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
# "/vote картинка" -- renders the standings as one tall picture (vote_image.py) and sends
# it back as a FILE. Admin-only like everything else that reads the whole poll, and
# deliberately not offered to voters: it shows every vote count at once, which is exactly
# what the page withholds from somebody who hasn't voted yet.
VOTE_IMAGE_WORDS = frozenset({"картинка", "картинку", "изображение", "image", "png"})
# "/vote перенос" -- shows what the next собрать would carry over from last week's poll
# and whether it would fire at all. Read-only by design: it is the "check before you
# commit to it" half of the carry-over, so it must never be able to cause one.
VOTE_CARRYOVER_WORDS = frozenset({"перенос", "перенести", "carryover", "carry"})
VOTE_CHAT_FLOW_TTL_SECONDS = 10 * 60
# The draft is written in the admin's DM but is almost never meant to stay there, so the
# finished text is not sent anywhere until they say where it goes: the main chat, the
# second group (VOTE_ANNOUNCE_EXTRA_CHAT), or both. That choice is what keeps a /vote chat
# flow alive past its own text step -- the text has to survive in memory until the button
# is pressed, so unlike every other force-reply flow here the entry is popped on the
# button, not on the reply.
VOTE_CHAT_DEST_CALLBACK_PREFIX = "votechatdest"
VOTE_CHAT_DESTINATIONS = ("main", "extra", "both", "cancel")
# Buttons on an administrator's bare-/vote status message for собрать/chat/картинка/
# очистить -- unlike "Открыть голосование"/"Модерация", those are bot ACTIONS, not Mini
# App pages, so they can't be a web_app button; tapping one runs the exact same code path
# as typing the command (see handle_vote_action_callback), just via a synthetic message.
# ---------------------------------------------------------------- the arena (v2)
#
# The second voting system: head-to-head duels instead of a grid ballot (arena.py,
# arena_core.py, arena_web.py). It shares this file's admin/membership checks and the web
# server's port, and NOTHING else -- separate storage, separate media, separate moderation,
# separate commands. Both can run in the same week; neither can break the other.
# Spelled "/vote2" and nothing else. "/arena" and "/арена" used to reach this system and
# now belong to the pet game (pets.py) -- the name was reassigned deliberately, and the
# voting system it used to open is unchanged underneath, only re-spelled. Old chat posts
# still carry a "?start=arena" deep link, which is why that payload keeps pointing here
# (see _dispatch_update): a button already sitting in the group must not start opening a
# different feature than the message around it describes.
ARENA_COMMANDS = ("/vote2", "/голосование2")
ARENA_COLLECT_WORDS = frozenset({"собрать", "обновить", "collect", "refresh"})
ARENA_MODERATE_WORDS = frozenset({"выбрать", "модерация", "moderate", "admin"})
ARENA_IMPORT_WORDS = frozenset({"импорт", "import", "изv1", "изv1"})
ARENA_RESULTS_WORDS = frozenset({"итоги", "результаты", "standings", "results"})
ARENA_CLEAR_WORDS = frozenset({"очистить", "сброс", "clear", "reset"})
# "/vote2 chat" -- the arena's own announcement, drafted and posted through the SAME flow
# v1's uses (vote_chat_flows, tagged with which system asked for it). The two systems keep
# their data apart; a composer that asks for a line of text and offers two groups to post
# it into is not either system's data, and two copies of it would drift.
ARENA_CHAT_WORDS = frozenset({"chat", "объявление", "announce"})
ARENA_OPEN_BUTTON_TEXT = "⚔️ Открыть арену"
# Buttons on the /vote2 status message, same synthetic-message trick as VOTE_ACTIONS.
ARENA_ACTION_CALLBACK_PREFIX = "arenaaction"
ARENA_ACTIONS = {
    "collect": "/vote2 собрать",
    "import": "/vote2 импорт",
    "chat": "/vote2 chat",
    "results": "/vote2 итоги",
    "clear": "/vote2 очистить",
}

VOTE_ACTION_CALLBACK_PREFIX = "voteaction"
VOTE_ACTIONS = {
    "collect": "/vote собрать",
    "chat": "/vote chat",
    "image": "/vote картинка",
    # Same command with the column count on the end -- see _vote_image_columns. A separate
    # button rather than a setting, because "how wide" is the only choice the picture has
    # and a button that renders it is shorter than a menu that asks first.
    "image4": "/vote картинка 4",
    "carryover": "/vote перенос",
    "clear": "/vote очистить",
}

# "Закрыть голосование и объявить победителя" in the Mini App no longer announces anything
# by itself. It closes the poll and records the winner exactly as before, but what the
# admin then gets is a DRAFT in their DM with the bot -- the results text on its own --
# and three buttons:
#
#   Редактировать -- a force-reply prompt whose answer replaces the text; repeatable
#   Отправить     -- posts the text straight into the main chat, no destination picker
#                    (unlike "/vote chat": the results always belong in the chat)
#   Отмена        -- posts nothing at all; the poll stays CLOSED and the results stay saved
#
# The results text is the one message a week whose wording the admin actually cares about,
# which is the whole reason nothing reaches the chat until they say so.
VOTE_RESULT_CALLBACK_PREFIX = "voteresult"
VOTE_RESULT_ACTIONS = ("edit", "send", "cancel")
# Deliberately far longer than VOTE_CHAT_FLOW_TTL_SECONDS: rewriting the week's results is
# a "let me think about the wording" job, not a one-line announcement, and nothing is
# racing it -- the poll is already closed and the results are already on disk by the time
# this draft appears, so an expired flow costs the admin their wording, never the result.
VOTE_RESULT_FLOW_TTL_SECONDS = 60 * 60

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
    {"command": "arena", "description": "Арена: клетка, существо, бои"},
    {"command": "pet", "description": "Моё существо"},
)
# Telegram appends @bot_username to group-menu suggestions. The bot still recognises all
# group commands when typed normally, but intentionally publishes no group menu entries
# so typing a slash does not fill the composer with that suffix.
GROUP_CHAT_COMMANDS = ()

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
    known_chat_ids (see run_bot_listener) so anything queued by listener.py under that
    same entry string (a figurine reaction, a stats digest) can be resolved back to this
    Bot-API chat_id. Deliberately does NOT special-case private chats the way
    _is_chat_allowed does: a DM isn't a postable target for chat-wide content."""
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
    -- commands, summary keywords, both force-reply flows -- gets to
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


def _vote_group_button_url(cfg, bot_username: str | None) -> str | None:
    """What the vote button links to in a message that lands in a GROUP. A web_app button
    is private-chat only -- Telegram rejects one posted to a group outright -- so a group
    can only carry a plain url, and which url depends on how the bot is registered.

    With a Direct Link Mini App (BotFather's /newapp short name) the url opens the Mini App
    in place, which is what the button promises. Without one the best available is the
    ?start=vote deep link into the DM, where the real web_app button is waiting: it still
    gets there, at the cost of one extra tap.
    """
    if not bot_username:
        return None
    if cfg.vote_miniapp_short_name:
        return f"https://t.me/{bot_username}/{cfg.vote_miniapp_short_name}?startapp=vote"
    return f"https://t.me/{bot_username}?start=vote"


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


def _vote_carryover_preview_text(entry: str, poll_id: str) -> str:
    """What the next "/vote собрать" would bring over from last week -- reported, not done.

    Reads polls off disk and computes the same carry-over the collect step would (see
    voting.carry_over_entries); it writes nothing, copies nothing and creates nothing, so
    checking can never change the answer it is describing. Which is the point: the one
    thing an administrator wants before a new week is to know what is about to appear,
    and the only way to find that out otherwise is to make it happen.

    It also says whether the carry-over would fire at all -- it only does for a week whose
    poll does not exist yet, and "already collected today" is the likeliest reason for it
    to look broken.
    """
    previous = voting.previous_poll(entry, poll_id)
    if previous is None:
        return (
            "Прошлого голосования нет -- переносить нечего. "
            "Первый /vote собрать соберёт заявки только из чата."
        )

    ranked = previous.tally()
    retiring = [(e, v) for e, v in ranked[:voting.CARRY_OVER_SKIP_TOP] if v > 0]
    carried = voting.carry_over_entries(previous)

    lines = [
        f"Прошлое голосование: {previous.poll_id} "
        f"(допущено {len(previous.approved)}, проголосовало {len(previous.votes)} чел.)",
        "",
    ]
    if retiring:
        lines.append(f"Не переносятся (топ-{voting.CARRY_OVER_SKIP_TOP}):")
        for medal, (e, v) in zip(_VOTE_MEDALS, retiring):
            lines.append(f"{medal} {_vote_who(e)} — {v} голосов")
    else:
        lines.append("В прошлый раз никто не набрал голосов -- переносятся все работы.")
    lines.append("")

    if carried:
        lines.append(f"Перенесётся работ: {len(carried)}")
        # Bounded: twenty names is already a wall of text in a chat, and the exact tail
        # matters less than the count and who is at the top of it.
        for e in carried[:15]:
            lines.append(f"• {_vote_who(e)}")
        if len(carried) > 15:
            lines.append(f"…и ещё {len(carried) - 15}")
    else:
        lines.append("Переносить нечего: кроме топа в прошлом голосовании работ не было.")
    lines.append("")

    current = voting.load_poll(entry, poll_id)
    if current is None:
        lines.append(f"Голосование на эту неделю ({poll_id}) ещё не создано -- перенос сработает при /vote собрать.")
    else:
        lines.append(
            f"Голосование на эту неделю ({poll_id}) уже создано: работ {len(current.entries)}, "
            f"допущено {len(current.approved)}. Перенос уже отработал и второй раз не повторится "
            "-- иначе он вернул бы то, что ты снял с голосования."
        )
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


def _vote_image_columns(argument: str) -> int | None:
    """How many works per row "/vote картинка" was asked for, or None if this isn't the
    картинка command at all.

    Accepts the bare word (the default board), the word with a number after it
    ("картинка 4", "image 4") and the two run together ("картинка4"), because all three
    get typed and none of them should land on the plain ballot by falling through the
    parser. The number itself is bounded by vote_image.clamp_columns, not here."""
    normalized = (argument or "").strip().lower()
    if normalized in VOTE_IMAGE_WORDS:
        return vote_image.COLUMNS
    match = re.match(r"^(?P<word>[^\s\d]+)\s*(?P<columns>\d{1,2})$", normalized)
    if match and match.group("word") in VOTE_IMAGE_WORDS:
        return vote_image.clamp_columns(match.group("columns"))
    return None


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
    (собрать/chat/картинка/очистить -- "Открыть голосование"/"Модерация" are plain web_app
    buttons, not callbacks, since those just open a page). Rather than duplicating
    those subcommands' logic here, this builds the same message shape
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


def _arena_page_url(cfg) -> str | None:
    return f"{cfg.webapp_public_url}{arena_web.ROUTE_PREFIX}" if cfg.webapp_public_url else None


def _arena_group_button_url(cfg, bot_username: str | None) -> str | None:
    """What the arena button links to in a message that lands in a GROUP -- the same
    constraint as v1's (a web_app button is private-chat only), and the same deep link the
    group /vote2 reply already leaves behind.

    No Direct Link Mini App branch, unlike _vote_group_button_url: a short name is
    registered against ONE url in BotFather, VOTE_MINIAPP_SHORT_NAME points at v1's page,
    and sending arena voters there with ?startapp=arena would open the wrong ballot.
    """
    return f"https://t.me/{bot_username}?start=vote2" if bot_username else None


def _announce_button(cfg, bot_username: str | None, system: str) -> dict | None:
    """The one button an announcement carries into a group, for whichever system drafted
    it. None when the link cannot be built (an unknown bot username), which every caller
    reports rather than posting a message whose only promise is a button that isn't there.
    """
    if system == "arena":
        url = _arena_group_button_url(cfg, bot_username)
        return {"text": ARENA_OPEN_BUTTON_TEXT, "url": url} if url else None
    url = _vote_group_button_url(cfg, bot_username)
    return {"text": VOTE_OPEN_BUTTON_TEXT, "url": url} if url else None


async def _start_announcement_draft(
    vote_chat_flows: dict[str, dict] | None,
    reply,
    telethon_client,
    chat_id,
    user: dict,
    entry: str,
    system: str,
    prompt_text: str,
    log=print,
) -> None:
    """Open the force-reply draft an announcement starts as -- for "/vote chat" and for
    "/vote2 chat" alike, tagged with which of them asked.

    One pending draft per (chat, admin), across BOTH systems on purpose: they share the
    prompt convention, so two live drafts would both be waiting on a reply to a message and
    the wrong one could swallow it. Starting a second abandons the first rather than
    accumulating flows nobody will ever answer.
    """
    if vote_chat_flows is None:
        await reply("Не получилось открыть черновик объявления -- попробуй ещё раз.")
        return
    for old_flow_id, old_flow in list(vote_chat_flows.items()):
        if old_flow.get("chat_id") == chat_id and old_flow.get("user_id") == user.get("id"):
            vote_chat_flows.pop(old_flow_id, None)
    prompt = await reply(prompt_text, reply_markup={"force_reply": True, "selective": True})
    if prompt is None:
        return
    vote_chat_flows[uuid.uuid4().hex[:10]] = {
        "chat_id": chat_id,
        "user_id": user.get("id"),
        "entry": entry,
        "system": system,
        # Resolved again (require_admin_in_dm's own lookup isn't exposed) so the consuming
        # side (handle_vote_chat_text_input) can re-check admin status without needing the
        # Telethon client at all -- the same "store what you'll need to re-verify"
        # convention badge_flows/cabinet_flows already follow.
        "admin_chat_id": await _resolve_chat_id(telethon_client, entry, {}, log=log),
        "prompt_message_id": prompt.get("message_id"),
        "created_at": time.monotonic(),
    }


def _arena_status_text(entry: str) -> str:
    """The arena's own status block for its /vote2 menu -- deliberately NOT v1's numbers.
    Two systems reporting one another's progress is how somebody ends up announcing the
    wrong result."""
    tournament = arena.latest_tournament(entry)
    if tournament is None:
        return "Арена ещё не создана. Собери работы: /vote2 собрать (или возьми их из v1: /vote2 импорт)."

    progress = tournament.progress()
    lines = [
        f"Работ: {len(tournament.approved)} допущено из {len(tournament.entries)} · "
        f"{'открыта' if tournament.open else 'закрыта'}",
        f"Проголосовало: {progress['completed']} чел. (в процессе {progress['in_progress']}) · "
        f"сравнений: {progress['judgements']}",
    ]
    rows = tournament.standings()["rows"]
    if progress["judgements"] and rows:
        lines.append("")
        lines.append("Топ по рейтингу:")
        for medal, row in zip(_VOTE_MEDALS, rows[:3]):
            margin = f" ±{round(row['margin'])}" if row["margin"] is not None else ""
            lines.append(
                f"{medal} {_vote_who(row['entry'])} — {round(row['rating'])}{margin} "
                f"({row['played']} дуэлей)"
            )
        # The sizing rule from import/CLAUDE.md: below ~4 judgements per possible pair the
        # top of the table is not separated, and reporting a winner from it would be
        # reporting noise.
        if progress["coverage"] < 4:
            lines.append("")
            lines.append(
                f"Голосов пока мало ({progress['coverage']:.1f} на пару из 4 нужных) -- "
                "верх таблицы ещё может перевернуться."
            )
    elif tournament.approved:
        lines.append("Пока никто не голосовал.")
    else:
        lines.append("Работы ещё не допущены -- /vote2 выбрать.")
    return "\n".join(lines)


def _arena_action_callback_data(action: str, chat_id, user_id) -> str:
    return f"{ARENA_ACTION_CALLBACK_PREFIX}:{action}:{chat_id}:{user_id}"


def _parse_arena_action_callback(data: str) -> tuple[str, int, int] | None:
    parts = (data or "").split(":")
    if len(parts) != 4 or parts[0] != ARENA_ACTION_CALLBACK_PREFIX or parts[1] not in ARENA_ACTIONS:
        return None
    try:
        return parts[1], int(parts[2]), int(parts[3])
    except ValueError:
        return None


async def handle_arena_action_callback(
    api: TelegramBotAPI,
    telethon_client,
    cfg,
    tz,
    callback: dict,
    entry: str | None,
    bot_username: str | None,
    background_tasks: set,
    vote_chat_flows: dict[str, dict] | None = None,
    log=print,
) -> None:
    """The /vote2 menu's buttons, built the same way v1's are: the tap is answered first,
    then a synthetic message goes through handle_arena_command so the admin/DM gate and
    the work itself live in exactly one place."""
    parsed = _parse_arena_action_callback(callback.get("data"))
    if parsed is None:
        await api.answer_callback_query(callback["id"])
        return
    action, chat_id, target_user_id = parsed

    clicker = callback.get("from") or {}
    if clicker.get("id") != target_user_id:
        await api.answer_callback_query(callback["id"], text="Эта кнопка не для тебя.")
        return
    # Answered before any of the slow work, or the button spins until Telegram times it out.
    await api.answer_callback_query(callback["id"])

    trigger = callback.get("message") or {}
    synthetic_message = {
        "message_id": trigger.get("message_id"),
        "chat": {"id": chat_id, "type": "private"},
        "from": clicker,
        "text": ARENA_ACTIONS[action],
    }
    task = asyncio.create_task(
        handle_arena_command(
            api, telethon_client, cfg, tz, synthetic_message, entry, bot_username,
            background_tasks, log=log, vote_chat_flows=vote_chat_flows,
        )
    )
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


async def handle_arena_command(
    api: TelegramBotAPI,
    telethon_client,
    cfg,
    tz,
    message: dict,
    entry: str | None,
    bot_username: str | None,
    background_tasks: set,
    log=print,
    vote_chat_flows: dict[str, dict] | None = None,
) -> None:
    """/vote2 -- the second voting system, with the same shape as /vote so an admin who
    knows one knows the other:

    - "/vote2 собрать" (DM, admin) scans #итогинедели into the ARENA's own store, with its
      own copy of the photos. It never reads or writes a poll.
    - "/vote2 импорт" (DM, admin) copies the works v1 has ADMITTED into the arena, so a
      week already moderated in v1 doesn't have to be moderated from scratch here. One
      way, on demand, by copy: v1 is not touched. They still arrive unadmitted -- this
      system's moderation is its own.
    - "/vote2 выбрать" (DM, admin) opens the arena's moderation screen: admit works, set
      pairs per voter and the pairing mode, open or close it.
    - "/vote2 chat" (DM, admin) drafts an announcement for the group with the arena's own
      button on it, through the same composer "/vote chat" uses (see
      _start_announcement_draft) -- tagged "arena", so the button leads here and not to v1.
    - "/vote2 итоги" (DM, admin) prints the fitted table.
    - "/vote2 очистить" (DM, admin) deletes the arena and nothing else.
    - bare "/vote2" opens the duels for everyone, and is the status/control panel for an
      administrator. A voter who is not one gets a single button and no panel: every other
      button here is an administrator's, and one they cannot use is one that only lies.
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
            log(f"[arena] failed to send the reply: {e}")
            return None

    page_url = _arena_page_url(cfg)
    if not page_url:
        await reply("Арена не настроена: не задан WEBAPP_PUBLIC_URL.")
        return
    if not entry:
        await reply("Не настроен основной чат (LISTENER_ALLOWED_CHATS).")
        return

    argument = stats.strip_command_bot_mention(message.get("text") or "", bot_username)
    for spelling in ARENA_COMMANDS:
        if argument.lower().startswith(spelling):
            argument = argument[len(spelling):]
            break
    normalized = argument.strip().lower()
    wants_collect = normalized in ARENA_COLLECT_WORDS
    wants_moderate = normalized in ARENA_MODERATE_WORDS
    wants_import = normalized in ARENA_IMPORT_WORDS
    wants_results = normalized in ARENA_RESULTS_WORDS
    wants_clear = normalized in ARENA_CLEAR_WORDS
    wants_chat = normalized in ARENA_CHAT_WORDS

    async def require_admin_in_dm(denial: str) -> bool:
        if not is_private:
            url = f"https://t.me/{bot_username}" if bot_username else None
            await reply(
                "Это только в личке с ботом.",
                reply_markup=({"inline_keyboard": [[{"text": "Открыть в личке", "url": url}]]} if url else None),
            )
            return False
        admin_chat_id = await _resolve_chat_id(telethon_client, entry, {}, log=log)
        if admin_chat_id is None or not await _can_manage_chat(api, admin_chat_id, user, entry):
            await reply(denial)
            return False
        return True

    tournament_id = _current_vote_poll_id(tz)  # same ISO-week key, its own file

    if wants_collect:
        if not await require_admin_in_dm("Собирать работы могут только администраторы."):
            return
        await reply("Собираю работы с #итогинедели в арену -- это займёт минуту.")
        existing = arena.load_tournament(entry, tournament_id)
        known = {e.entry_id for e in existing.entries} if existing else set()
        try:
            new_entries = await voting.collect_entries(
                client=telethon_client,
                chat_ref=entry,
                tz=tz,
                # The arena's OWN media directory: v1's photos stay v1's, and clearing
                # either system cannot delete the other's pictures.
                media_dir=arena.media_path(entry, tournament_id),
                skip_entry_ids=known,
                log=log,
            )
        except Exception:
            log(f"[arena] collecting failed:\n{traceback.format_exc()}")
            await reply("Не получилось собрать работы.")
            return

        all_entries = (existing.entries if existing else []) + new_entries
        tournament = arena.build_tournament(entry, tournament_id, all_entries, existing=existing)
        arena.save_tournament(tournament)
        arena.invalidate_standings(tournament_id)
        if not all_entries:
            await reply("Постов с #итогинедели не нашлось. Можно взять работы из v1: /vote2 импорт")
            return
        await reply(
            f"Новых работ: {len(new_entries)} (всего {len(all_entries)}). "
            "Открой модерацию и отметь, что допустить.",
            reply_markup={"inline_keyboard": [[
                {"text": "🛠 Модерация арены", "web_app": {"url": f"{page_url}?mode=admin"}}
            ]]},
        )
        return

    if wants_import:
        if not await require_admin_in_dm("Импортировать работы могут только администраторы."):
            return
        poll = voting.latest_poll(entry)
        if poll is None or not poll.approved:
            await reply("В v1 нет допущенных работ -- импортировать нечего.")
            return
        tournament = arena.load_tournament(entry, tournament_id) or arena.Tournament(
            tournament_id=tournament_id, entry=entry,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        added = arena.import_entries_from_poll(tournament, poll)
        arena.save_tournament(tournament)
        arena.invalidate_standings(tournament_id)
        log(f"[arena] imported {added} work(s) from poll {poll.poll_id}")
        await reply(
            (f"Взял из v1: {added} работ (всего в арене {len(tournament.entries)}). "
             "Голосование v1 не изменилось. Работы пока НЕ допущены -- отметь их в модерации."
             if added else
             "Все работы из v1 уже в арене -- ничего не добавилось."),
            reply_markup={"inline_keyboard": [[
                {"text": "🛠 Модерация арены", "web_app": {"url": f"{page_url}?mode=admin"}}
            ]]},
        )
        return

    if wants_moderate:
        if not await require_admin_in_dm("Модерировать арену могут только администраторы."):
            return
        await reply(
            "Модерация арены: отметь работы, задай число пар на голосующего и режим подбора.",
            reply_markup={"inline_keyboard": [[
                {"text": "🛠 Модерация арены", "web_app": {"url": f"{page_url}?mode=admin"}}
            ]]},
        )
        return

    if wants_results:
        if not await require_admin_in_dm("Смотреть итоги арены могут только администраторы."):
            return
        tournament = arena.latest_tournament(entry)
        if tournament is None:
            await reply("Арена ещё не создана.")
            return
        rows = tournament.standings()["rows"]
        if not rows:
            await reply("В арене пока нет допущенных работ.")
            return
        lines = ["Рейтинг арены (Bradley-Terry, 1500 — середина поля):"]
        for place, row in enumerate(rows, start=1):
            margin = f" ±{round(row['margin'])}" if row["margin"] is not None else ""
            lines.append(
                f"{place}. {_vote_who(row['entry'])} — {round(row['rating'])}{margin} "
                f"({row['played']} дуэлей, {row['score']:g} очк.)"
            )
        progress = tournament.progress()
        lines.append("")
        lines.append(
            f"Сравнений: {progress['judgements']} · покрытие {progress['coverage']:.1f} на пару"
        )
        if len(rows) > 1 and not arena_core.is_separated(rows[0], rows[1]):
            # Saying so is the whole point of carrying a margin around: two overlapping
            # error bars are not a first and a second place, whatever the order shows.
            lines.append("Первое и второе место статистически не разделены -- нужно больше голосов.")
        await reply("\n".join(lines))
        return

    if wants_chat:
        if not await require_admin_in_dm("Готовить объявление могут только администраторы."):
            return
        await _start_announcement_draft(
            vote_chat_flows, reply, telethon_client, chat_id, user, entry, "arena",
            "Какой текст написать в объявлении об арене? Ответь на это сообщение.",
            log=log,
        )
        return

    if wants_clear:
        if not await require_admin_in_dm("Очищать арену могут только администраторы."):
            return
        tournament = arena.latest_tournament(entry)
        if tournament is None:
            await reply("Арена ещё не создана -- нечего очищать.")
            return
        existed = arena.delete_tournament(entry, tournament.tournament_id)
        arena.invalidate_standings(tournament.tournament_id)
        await reply(
            "Арена очищена. Голосование v1 не тронуто." if existed else "Арена уже пуста."
        )
        return

    # Bare "/vote2": the duels for everyone, plus a control panel for an administrator.
    if is_private:
        admin_chat_id = await _resolve_chat_id(telethon_client, entry, {}, log=log)
        is_manager = admin_chat_id is not None and await _can_manage_chat(api, admin_chat_id, user, entry)
        if is_manager:
            admin_user_id = user.get("id")
            # Status and buttons, and no list of the commands the buttons already are:
            # every line of it was a slower way to press the button underneath it.
            await reply(
                f"{_arena_status_text(entry)}\n\n"
                "Это отдельная система: v1 (/vote) работает как работал.",
                reply_markup={"inline_keyboard": [
                    [
                        {"text": ARENA_OPEN_BUTTON_TEXT, "web_app": {"url": page_url}},
                        {"text": "🛠 Модерация", "web_app": {"url": f"{page_url}?mode=admin"}},
                    ],
                    [
                        {"text": "🔄 Собрать", "callback_data": _arena_action_callback_data("collect", chat_id, admin_user_id)},
                        {"text": "⬇️ Взять из v1", "callback_data": _arena_action_callback_data("import", chat_id, admin_user_id)},
                    ],
                    [
                        {"text": "📣 Объявление", "callback_data": _arena_action_callback_data("chat", chat_id, admin_user_id)},
                        {"text": "📊 Рейтинг", "callback_data": _arena_action_callback_data("results", chat_id, admin_user_id)},
                    ],
                    [
                        {"text": "🗑 Очистить", "callback_data": _arena_action_callback_data("clear", chat_id, admin_user_id)},
                    ],
                ]},
            )
        else:
            # Everything else on the panel above is an administrator's, and each one is
            # refused twice over (the callback is bound to one user id, and the command it
            # replays re-checks admin status). So a voter is shown none of them rather than
            # buttons that exist only to say no.
            await reply(
                "Арена: сравни работы попарно.",
                reply_markup={"inline_keyboard": [[{"text": ARENA_OPEN_BUTTON_TEXT, "web_app": {"url": page_url}}]]},
            )
        return

    # In a group a web_app button is not allowed, so the same deep link v1 uses.
    url = _arena_group_button_url(cfg, bot_username)
    await reply(
        "Арена открывается в личке с ботом:",
        reply_markup=({"inline_keyboard": [[{"text": ARENA_OPEN_BUTTON_TEXT, "url": url}]]} if url else None),
    )


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
    """Six distinct things live behind /vote, deliberately kept apart rather than one
    page that changes shape depending who opens it:

    - "/vote собрать" (DM, admin-only) adds newly posted #итогинедели entries to the
      list -- already-known ones are left alone, not re-fetched or re-processed.
    - "/vote выбрать" (DM, admin-only) opens the moderation screen -- admit toggles, live
      counts, ballot settings, and closing the vote.
    - "/vote очистить" (DM, admin-only, tap-to-confirm) deletes the current poll outright.
    - "/vote chat" (DM, admin-only) drafts an announcement -- asks for the text via a
      force-reply, then asks where it goes (main chat, the second group, or both) and
      posts that text plus the vote button there (see handle_vote_chat_text_input and
      handle_vote_chat_destination_callback).
    - "/vote картинка" (DM, admin-only) renders the standings as one tall picture
      (vote_image.py), saves it under the poll's own exports directory and sends it back
      as a file.
    - bare "/vote" opens the actual ballot, for EVERYONE including an administrator --
      an admin is never forced into moderation mode just to cast their own vote. For an
      administrator specifically, it's also a status/control panel: current standings
      plus the full command list, since remembering four subcommands is more friction
      than a menu.

    `forced_mode` ("moderate", "clear", "chat", or "image") is set by the /start deep-link
    an admin-only group message hands out for "выбрать"/"очистить"/"chat"/"картинка",
    bypassing the usual text parsing since a /start payload never carries the Russian word
    itself (see VOTE_MODERATE_WORDS/VOTE_CLEAR_WORDS/VOTE_CHAT_WORDS/VOTE_IMAGE_WORDS
    below). `vote_chat_flows` is
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

    wants_collect = wants_moderate = wants_clear = wants_chat = wants_image = False
    wants_carryover = False
    image_columns = vote_image.COLUMNS
    if forced_mode == "moderate":
        wants_moderate = True
    elif forced_mode == "clear":
        wants_clear = True
    elif forced_mode == "chat":
        wants_chat = True
    elif forced_mode == "image":
        wants_image = True
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
        wants_carryover = normalized in VOTE_CARRYOVER_WORDS
        requested_columns = _vote_image_columns(normalized)
        wants_image = requested_columns is not None
        if requested_columns is not None:
            image_columns = requested_columns

    async def require_admin_in_dm(denial: str) -> bool:
        """Common gate for собрать/выбрать/очистить/chat/картинка: DM only, admin only.
        Returns whether the caller passed; the group-vs-DM split lives here once instead
        of being repeated for all five admin-only subcommands."""
        if not is_private:
            if not bot_username:
                await reply("Открой в личке с ботом.")
                return False
            start_payload = (
                "vote_admin" if wants_moderate else
                "vote_clear" if wants_clear else
                "vote_chat" if wants_chat else
                "vote_image" if wants_image else None
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

        # Starting a NEW week: last week's admitted works, minus its top 3, run again --
        # the podium has had its week and everything below it deserves another (see
        # voting.seed_poll_from_previous). Only when this week's poll doesn't exist yet:
        # re-collecting an already-started week must not resurrect works the moderator
        # has since un-admitted, which a carry-over on every /vote собрать would do.
        carried = 0
        if existing_poll is None:
            last_week = voting.previous_poll(entry, poll_id)
            if last_week is not None:
                existing_poll = voting.seed_poll_from_previous(entry, poll_id, last_week)
                carried = len(existing_poll.entries)
                log(
                    f"[bot_listener] vote poll {poll_id}: carried {carried} work(s) over from "
                    f"{last_week.poll_id} (top {voting.CARRY_OVER_SKIP_TOP} retired)"
                )

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
        if carried:
            # Said out loud, and said first: the moderator is about to open a screen with
            # works in it they did not collect today, already admitted. Unexplained, that
            # reads as the bot having picked up something it shouldn't.
            summary = (
                f"Перенёс с прошлого голосования: {carried} работ "
                f"(топ-{voting.CARRY_OVER_SKIP_TOP} не переносится, они уже допущены). "
                + summary
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
        await _start_announcement_draft(
            vote_chat_flows, reply, telethon_client, chat_id, user, entry, "vote",
            "Какой текст написать в объявлении о голосовании? Ответь на это сообщение.",
            log=log,
        )
        return

    if wants_carryover:
        if not await require_admin_in_dm("Смотреть перенос могут только администраторы."):
            return
        # Nothing but a read: no poll is created, nothing is saved, no photo is copied.
        # Checking what will happen must not be a way of making it happen.
        await reply(_vote_carryover_preview_text(entry, _current_vote_poll_id(tz)))
        return

    if wants_image:
        if not await require_admin_in_dm("Рендерить картинку итогов могут только администраторы."):
            return
        poll = voting.latest_poll(entry)
        if poll is None:
            await reply("Голосование ещё не создано -- нечего рисовать. Сначала /vote собрать.")
            return
        standings = poll.tally()
        if not standings:
            await reply("К голосованию ещё не допущена ни одна работа -- рисовать нечего. /vote выбрать.")
            return

        await reply(
            f"Рисую картинку: {len(standings)} работ, {image_columns} в ряд. "
            "Это займёт несколько секунд."
        )
        subtitle = (
            f"Проголосовало: {len(poll.votes)} чел. · работ: {len(standings)} · "
            f"{'голосование открыто' if poll.open else 'голосование закрыто'}"
        )
        try:
            # In a thread: Pillow decodes, scales and re-encodes every photo in the poll,
            # which is seconds of straight CPU work -- on the event loop it would stall
            # every other chat the bot is serving for the whole render.
            path = await asyncio.to_thread(
                vote_image.render_poll_image,
                poll,
                voting.export_image_path(entry, poll.poll_id, image_columns),
                subtitle=subtitle,
                columns=image_columns,
            )
        except Exception:
            log(f"[bot_listener] rendering the vote image failed:\n{traceback.format_exc()}")
            await reply("Не получилось нарисовать картинку -- смотри логи.")
            return
        log(
            f"[bot_listener] rendered vote image for poll {poll.poll_id}: {path} "
            f"({image_columns} columns, {path.stat().st_size} bytes)"
        )

        # As a document, not a photo: Telegram re-encodes photos and refuses one past
        # 10000px of width+height or a 20:1 side ratio, and a board of a whole contest is
        # exactly that shape (see bot_api.send_document_file). The file stays on disk
        # either way -- the send is a copy, not the save.
        try:
            await api.send_document_file(
                chat_id, path,
                caption="Итоги голосования одной картинкой. Файлом, чтобы Telegram не сжимал.",
                reply_to_message_id=message["message_id"],
            )
        except Exception as e:
            log(f"[bot_listener] failed to send the vote image: {e}")
            await reply(f"Картинка отрисована ({path.name}), но отправить её не вышло -- смотри логи.")
        return

    # Bare "/vote": the actual ballot, for everyone -- an administrator gets this too,
    # unless they specifically asked for "выбрать". An administrator's bare /vote is also
    # a status/control panel: current standings and a button per command, rather than just
    # the vote button, since they're the one who has four subcommands to remember. The
    # written-out command list that used to sit here is gone: the buttons below say the
    # same thing and do it in one tap.
    if is_private:
        admin_chat_id = await _resolve_chat_id(telethon_client, entry, {}, log=log)
        is_manager = admin_chat_id is not None and await _can_manage_chat(api, admin_chat_id, user, entry)
        if is_manager:
            text = _vote_status_text(entry)
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
                        # Reads and reports; changes nothing. Sits next to "Собрать заявки"
                        # because it is a preview of exactly what that button would do.
                        {
                            "text": "🔁 Что перенесётся с прошлой недели",
                            "callback_data": _vote_action_callback_data("carryover", chat_id, admin_user_id),
                        },
                    ],
                    [
                        {
                            "text": "🖼 Картинка 3 в ряд",
                            "callback_data": _vote_action_callback_data("image", chat_id, admin_user_id),
                        },
                        {
                            "text": "🖼 4 в ряд",
                            "callback_data": _vote_action_callback_data("image4", chat_id, admin_user_id),
                        },
                    ],
                    [
                        # A web_app button, not a callback: cropping is a page, and this is
                        # a DM, which is the only place Telegram allows one (see the
                        # docstring). It renders and delivers the picture itself, so it is
                        # the картинка buttons with a framing step in front of them.
                        {"text": "✂️ Кадрировать и выгрузить", "web_app": {"url": f"{page_url}/board"}},
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

    # In a group: a plain url button, since a web_app button is private-chat only. Where
    # that url goes -- the Mini App itself or the DM -- is _vote_group_button_url's call,
    # so the wording follows it rather than promising a trip through the DM that a
    # configured Direct Link Mini App no longer needs.
    group_url = _vote_group_button_url(cfg, bot_username)
    if not group_url:
        await reply("Открой голосование в личке с ботом.")
        return
    # Deliberately kept in the chat, unlike the stats replies this codebase otherwise
    # sweeps away as noise -- people need to be able to find the vote announcement later,
    # so it is never scheduled for auto-delete.
    await reply(
        "Голосование за итоги недели:" if cfg.vote_miniapp_short_name
        else "Голосование за итоги недели -- открывается в личке с ботом:",
        reply_markup={"inline_keyboard": [[{"text": VOTE_OPEN_BUTTON_TEXT, "url": group_url}]]},
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
    """Consumes "/vote chat"'s force-reply and asks where the finished announcement should
    go -- the main chat, the second group (VOTE_ANNOUNCE_EXTRA_CHAT), or both. Nothing is
    posted here: the text is parked in its own flow and
    handle_vote_chat_destination_callback sends it once a destination button is pressed,
    which is why this is the one force-reply flow in this file that does NOT pop its entry
    on the reply.

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

    text = (message.get("text") or "").strip()
    if text.lower() in ("/cancel", "отмена"):
        vote_chat_flows.pop(flow_id, None)
        await api.send_message(
            chat_id, "Черновик отменён.", reply_to_message_id=message["message_id"], parse_mode=None
        )
        return True
    if not await _can_manage_chat(api, flow["admin_chat_id"], actor, flow.get("entry")):
        vote_chat_flows.pop(flow_id, None)
        return True  # silently dropped, same as badge_flows -- admin status changed mid-flow
    if not text:
        vote_chat_flows.pop(flow_id, None)
        await api.send_message(
            chat_id, "Пустой текст -- объявление не отправлено.",
            reply_to_message_id=message["message_id"], parse_mode=None,
        )
        return True

    # Which system asked for the draft decides where its button goes; "vote" is the default
    # so a flow written before the arena existed still means what it did.
    system = flow.get("system") or "vote"
    if not (_arena_page_url(cfg) if system == "arena" else _vote_page_url(cfg)):
        vote_chat_flows.pop(flow_id, None)
        await api.send_message(
            chat_id,
            "Арена не настроена -- некуда вести кнопку." if system == "arena"
            else "Голосование не настроено -- некуда вести кнопку.",
            reply_to_message_id=message["message_id"], parse_mode=None,
        )
        return True

    destinations = [[{
        "text": "📣 В чат", "callback_data": _vote_chat_dest_callback_data("main", flow_id),
    }]]
    # The second group only appears when it's configured -- offering "В оба" with nothing
    # on the other side would be a button whose only possible outcome is an error.
    if cfg.vote_announce_extra_chat:
        destinations[0].append({
            "text": "🎨 В Папку художников",
            "callback_data": _vote_chat_dest_callback_data("extra", flow_id),
        })
        destinations.append([{
            "text": "📣 В оба", "callback_data": _vote_chat_dest_callback_data("both", flow_id),
        }])
    destinations.append([{
        "text": "Отмена", "callback_data": _vote_chat_dest_callback_data("cancel", flow_id),
    }])

    flow["text"] = text
    # The destination step gets the full TTL of its own: the clock so far measured how long
    # the admin took to write the text, which says nothing about how long they need to
    # decide where it goes.
    flow["created_at"] = time.monotonic()
    try:
        await api.send_message(
            chat_id, "Куда отправить объявление?",
            reply_to_message_id=message["message_id"], parse_mode=None,
            reply_markup={"inline_keyboard": destinations},
        )
    except Exception as e:
        log(f"[bot_listener] failed to offer the vote announcement destinations: {e}")
        vote_chat_flows.pop(flow_id, None)
        return True
    log(f"[bot_listener] {actor.get('username') or actor.get('id')} drafted a vote announcement ({len(text)} chars)")
    return True


def _vote_chat_dest_callback_data(destination: str, flow_id: str) -> str:
    return f"{VOTE_CHAT_DEST_CALLBACK_PREFIX}:{destination}:{flow_id}"


def _parse_vote_chat_dest_callback(data: str) -> tuple[str, str] | None:
    parts = (data or "").split(":")
    if (
        len(parts) != 3
        or parts[0] != VOTE_CHAT_DEST_CALLBACK_PREFIX
        or parts[1] not in VOTE_CHAT_DESTINATIONS
        or not parts[2]
    ):
        return None
    return parts[1], parts[2]


async def handle_vote_chat_destination_callback(
    api: TelegramBotAPI,
    cfg,
    callback: dict,
    vote_chat_flows: dict[str, dict],
    bot_username: str | None,
    log=print,
) -> None:
    """Posts a drafted "/vote chat" announcement once the admin picks where it goes, or
    drops it on "Отмена". Either way the flow is finished with here -- this is where the
    entry handle_vote_chat_text_input deliberately left alive gets popped.

    Each destination is its own send, with its own try/except: two groups are two Bot API
    calls, and a bot that has been kicked from one of them must still get the announcement
    into the other rather than failing the whole thing. The report back to the admin names
    both sides so a silent half-delivery is impossible.
    """
    parsed = _parse_vote_chat_dest_callback(callback.get("data"))
    if parsed is None:
        await api.answer_callback_query(callback["id"])
        return
    destination, flow_id = parsed

    flow = vote_chat_flows.get(flow_id)
    if flow is None or time.monotonic() - flow["created_at"] > VOTE_CHAT_FLOW_TTL_SECONDS:
        vote_chat_flows.pop(flow_id, None)
        await api.answer_callback_query(
            callback["id"], text="Черновик устарел -- начни заново: /vote chat"
        )
        return
    clicker = callback.get("from") or {}
    if clicker.get("id") != flow.get("user_id"):
        await api.answer_callback_query(callback["id"], text="Эта кнопка не для тебя.")
        return
    # Answered before anything else that talks to Telegram: posting into two groups is two
    # round trips plus the re-verification below, and a button that waits for all of them
    # sits spinning on the presser's screen for the whole time.
    await api.answer_callback_query(callback["id"])
    vote_chat_flows.pop(flow_id, None)

    dm_chat_id = flow["chat_id"]

    async def report(text: str) -> None:
        try:
            await api.send_message(dm_chat_id, text, parse_mode=None)
        except Exception as e:
            log(f"[bot_listener] failed to report the vote announcement result: {e}")

    if destination == "cancel":
        await report("Объявление отменено.")
        return
    # Re-checked against the tapper even though only they could see the button, the same
    # belt-and-suspenders check every other confirm flow in this file does -- admin status
    # can have been taken away between writing the text and choosing a destination.
    if not await _can_manage_chat(api, flow["admin_chat_id"], clicker, flow.get("entry")):
        await report("Публиковать объявление могут только администраторы.")
        return

    # A url button, never web_app: every destination here is a group, and Telegram accepts
    # a web_app button only in a private chat (see _vote_group_button_url).
    button = _announce_button(cfg, bot_username, flow.get("system") or "vote")
    if button is None:
        await report("Не удалось собрать кнопку голосования -- неизвестно имя бота.")
        return
    keyboard = {"inline_keyboard": [[button]]}

    targets: list[tuple[str, object]] = []
    if destination in ("main", "both"):
        targets.append(("основной чат", flow.get("admin_chat_id")))
    if destination in ("extra", "both") and cfg.vote_announce_extra_chat:
        targets.append(("Папка художников", cfg.vote_announce_extra_chat))

    posted: list[str] = []
    failed: list[str] = []
    for label, target in targets:
        if target is None:
            failed.append(f"{label} (чат не определён)")
            continue
        try:
            # Never scheduled for auto-delete, unlike the stats replies this codebase
            # sweeps away as noise: an announcement whose whole purpose is to be come back
            # to and voted from is the one message that has to still be there tomorrow.
            await api.send_message(target, flow["text"], parse_mode=None, reply_markup=keyboard)
        except Exception as e:
            log(f"[bot_listener] failed to post the vote announcement to {label}: {e}")
            failed.append(f"{label} ({e})")
        else:
            posted.append(label)

    if not targets:
        await report("Некуда отправлять: второй чат не настроен (VOTE_ANNOUNCE_EXTRA_CHAT).")
        return
    lines = []
    if posted:
        lines.append("Объявление опубликовано: " + ", ".join(posted) + ".")
    if failed:
        lines.append("Не получилось отправить: " + "; ".join(failed))
    await report("\n".join(lines))
    log(
        f"[bot_listener] {clicker.get('username') or clicker.get('id')} posted a vote "
        f"announcement to {destination}: {len(posted)} ok, {len(failed)} failed"
    )


def _vote_result_callback_data(action: str, flow_id: str) -> str:
    return f"{VOTE_RESULT_CALLBACK_PREFIX}:{action}:{flow_id}"


def _parse_vote_result_callback(data: str) -> tuple[str, str] | None:
    parts = (data or "").split(":")
    if (
        len(parts) != 3
        or parts[0] != VOTE_RESULT_CALLBACK_PREFIX
        or parts[1] not in VOTE_RESULT_ACTIONS
        or not parts[2]
    ):
        return None
    return parts[1], parts[2]


def _vote_result_keyboard(flow_id: str) -> dict:
    """The three buttons every copy of the draft carries. Emoji-free like the results text
    itself: this whole flow speaks in the bot's plain voice."""
    return {"inline_keyboard": [[
        {"text": "Редактировать", "callback_data": _vote_result_callback_data("edit", flow_id)},
        {"text": "Отправить", "callback_data": _vote_result_callback_data("send", flow_id)},
        {"text": "Отмена", "callback_data": _vote_result_callback_data("cancel", flow_id)},
    ]]}


def _vote_results_text(standings: list, places: int | None = None) -> str:
    """voting.format_results_text owns the wording -- this only guarantees there IS one.

    `places` is passed straight through, and None (the default) means every entrant gets a
    line: the announcement is the whole board, not a podium, so nothing here may cap it.

    The poll is already closed by the time this is reached, so a formatting error would
    otherwise leave the admin with a finished vote and no way to announce it. Falling back
    to a bare list keeps the flow alive at the cost of the dictated wording.
    """
    try:
        return voting.format_results_text(standings, places)
    except Exception:
        # Not traceback-logged at error level on purpose: this also covers running against
        # an older voting.py that has no format_results_text at all.
        lines = ["Результаты недельного голосования:"]
        for index, (entry, votes) in enumerate(
            standings if places is None else standings[:places], start=1
        ):
            lines.append(f"{index}. {_vote_who(entry)} — {votes} голосов")
        return "\n".join(lines)


def _save_vote_results(poll, standings: list, text: str, log=print) -> None:
    """Writes the results record to disk.

    Called three times over the life of one draft -- when the results are first produced,
    after every edit, and after a successful post -- so that (a) the record exists even if
    the admin presses Отмена and never announces anything, which is precisely what the
    cancellation message promises them, and (b) what is on disk always matches the wording
    they last saw rather than the first machine-generated draft.

    Best-effort: this record is a convenience for later lookups, not the poll's own state
    (voting.save_poll already stored that), so losing it must not cost the admin the draft
    sitting in front of them.
    """
    try:
        voting.save_results(poll, standings, text)
    except Exception:
        log(f"[bot_listener] failed to save the vote results record:\n{traceback.format_exc()}")


async def _send_vote_result_draft(api: TelegramBotAPI, flow_id: str, flow: dict, log=print) -> bool:
    """Puts one copy of the draft in front of the admin: the results text with the three
    buttons under it.

    Sent as a NEW message on every edit rather than edited in place, so the earlier wording
    stays visible while the admin works on the next one -- an edit-in-place would make
    "what did I just replace?" unanswerable in the middle of a rewrite.

    Deliberately never handed to schedule_bot_delete, exactly like the /vote announcement
    in the group: the draft is something the admin comes back to when they have thought of
    better wording, so nothing in this bot's auto-delete sweep may touch it.

    Returns False when nothing could be delivered at all, which is the caller's cue to drop
    the flow rather than leave buttons nobody will ever see.
    """
    try:
        await api.send_message(
            flow["chat_id"], flow["text"], parse_mode=None,
            reply_markup=_vote_result_keyboard(flow_id),
        )
    except Exception as e:
        log(f"[bot_listener] failed to show the vote results draft: {e}")
        return False
    return True


async def send_vote_results_draft(
    api: TelegramBotAPI,
    user: dict,
    poll,
    standings: list,
    admin_chat_id,
    vote_result_flows: dict[str, dict],
    log=print,
) -> None:
    """What vote_web.handle_announce reaches when the vote is closed from the Mini App.

    `user` is whoever closed it, taken from the page's own verified identity rather than
    re-derived some other way, and their user id doubles as the DM chat id -- the draft
    goes to the person who pressed the button, in the chat they pressed it from.
    `standings` is poll.tally() IN FULL, best first: the text lists every admitted work, so
    a caller that had already sliced it to a podium could not be un-sliced from here.

    The results record is written here, before a single button has been pressed, so that
    closing the vote is what produces the results -- not announcing them. Отмена then
    genuinely means "don't post", not "throw the week away".
    """
    chat_id = user.get("id")
    if chat_id is None or not standings:
        return

    text = _vote_results_text(standings)
    _save_vote_results(poll, standings, text, log=log)

    # One live draft per admin, plus the usual TTL sweep -- a second "подведи итоги" in the
    # same DM abandons the first draft rather than leaving two sets of buttons that would
    # post the same results twice.
    for old_flow_id, old_flow in list(vote_result_flows.items()):
        if (
            old_flow.get("chat_id") == chat_id
            or time.monotonic() - old_flow["created_at"] > VOTE_RESULT_FLOW_TTL_SECONDS
        ):
            vote_result_flows.pop(old_flow_id, None)

    flow_id = uuid.uuid4().hex[:10]
    flow = {
        "chat_id": chat_id,
        "user_id": chat_id,
        "entry": poll.entry,
        # Resolved by the caller while the Telethon session was at hand and carried here so
        # neither the buttons nor the re-verification below ever waits on a chat lookup --
        # the same "store what you'll need" convention vote_chat_flows follows.
        "admin_chat_id": admin_chat_id,
        "poll": poll,
        "standings": standings,
        "text": text,
        "prompt_message_id": None,
        "created_at": time.monotonic(),
    }
    if not await _send_vote_result_draft(api, flow_id, flow, log=log):
        return
    vote_result_flows[flow_id] = flow
    log(
        f"[bot_listener] drafted the results of poll {poll.poll_id} for "
        f"{user.get('username') or chat_id}"
    )


async def handle_vote_result_callback(
    api: TelegramBotAPI,
    callback: dict,
    vote_result_flows: dict[str, dict],
    log=print,
) -> None:
    """Редактировать / Отправить / Отмена under a results draft.

    Отправить posts into the main chat and nowhere else -- there is no destination picker
    here on purpose, unlike "/vote chat": the results of the chat's own contest belong in
    the chat, and asking would only be a button whose answer is always the same.

    The flow is popped on Отмена and on a successful post, but deliberately NOT on a failed
    post: a Bot API hiccup should leave the draft (and its buttons) intact so the admin can
    simply press Отправить again, rather than having to close the vote a second time.
    """
    parsed = _parse_vote_result_callback(callback.get("data"))
    if parsed is None:
        await api.answer_callback_query(callback["id"])
        return
    action, flow_id = parsed

    flow = vote_result_flows.get(flow_id)
    if flow is None or time.monotonic() - flow["created_at"] > VOTE_RESULT_FLOW_TTL_SECONDS:
        vote_result_flows.pop(flow_id, None)
        await api.answer_callback_query(
            callback["id"], text="Черновик устарел -- подведи итоги заново."
        )
        return
    clicker = callback.get("from") or {}
    if clicker.get("id") != flow.get("user_id"):
        await api.answer_callback_query(callback["id"], text="Эта кнопка не для тебя.")
        return
    # Answered before anything else that talks to Telegram: the admin re-check, the post
    # into the chat and the confirmation back are three round trips, and a button that
    # waits for them sits spinning on the presser's screen for the whole time.
    await api.answer_callback_query(callback["id"])

    async def report(text: str) -> None:
        try:
            await api.send_message(flow["chat_id"], text, parse_mode=None)
        except Exception as e:
            log(f"[bot_listener] failed to report the vote results outcome: {e}")

    if action == "cancel":
        vote_result_flows.pop(flow_id, None)
        await report(
            "Итоги не опубликованы -- в чат ничего не отправлено. "
            "Голосование остаётся закрытым, результаты сохранены."
        )
        return

    # Re-checked against the tapper even though only they can see these buttons, the same
    # belt-and-suspenders check every other confirm flow in this file does -- admin rights
    # can have been taken away between closing the vote and pressing the button.
    if not await _can_manage_chat(api, flow.get("admin_chat_id"), clicker, flow.get("entry")):
        vote_result_flows.pop(flow_id, None)
        await report("Публиковать итоги могут только администраторы.")
        return

    if action == "edit":
        try:
            prompt = await api.send_message(
                flow["chat_id"],
                "Пришли новый текст итогов ответом на это сообщение.",
                parse_mode=None,
                reply_markup={"force_reply": True, "selective": True},
            )
        except Exception as e:
            log(f"[bot_listener] failed to open the vote results editor: {e}")
            await report("Не получилось открыть редактирование -- попробуй ещё раз.")
            return
        flow["prompt_message_id"] = (prompt or {}).get("message_id")
        # The clock restarts: what it measured so far was how long the admin looked at the
        # draft, which says nothing about how long they need to rewrite it.
        flow["created_at"] = time.monotonic()
        return

    target = flow.get("admin_chat_id")
    if target is None:
        await report("Не удалось определить основной чат -- итоги не отправлены.")
        return
    text = flow["text"]
    try:
        # Never scheduled for auto-delete, unlike the stats replies this codebase sweeps
        # away as noise (see schedule_bot_delete) and exactly like the /vote announcement:
        # the week's results are the message people scroll back to, so no sweep path may
        # ever touch them.
        await api.send_message(target, text, parse_mode=None)
    except Exception as e:
        log(f"[bot_listener] failed to post the vote results: {e}")
        await report(f"Не получилось отправить итоги в чат: {e}")
        return

    vote_result_flows.pop(flow_id, None)
    _save_vote_results(flow["poll"], flow["standings"], text, log=log)
    await report("Итоги опубликованы в основном чате.")
    log(
        f"[bot_listener] {clicker.get('username') or clicker.get('id')} posted the results "
        f"of poll {flow['poll'].poll_id} to the main chat"
    )


async def handle_vote_result_text_input(
    api: TelegramBotAPI,
    message: dict,
    vote_result_flows: dict[str, dict],
    log=print,
) -> bool:
    """Consumes the force-reply Редактировать opened: the reply becomes the draft text and
    the draft is shown again, with the same three buttons, so editing can be repeated as
    many times as the admin likes.

    Like handle_vote_chat_text_input this does NOT pop its flow on the reply -- the draft
    has to survive until Отправить or Отмена -- and unlike it, an empty reply is not fatal
    either: the results already exist, so a reply with no text at all re-shows what is
    there rather than throwing the week's announcement away.

    Returns True once this message belonged to a pending draft, so the caller stops
    treating it as ordinary chat input -- same contract as handle_badge_text_input/
    handle_cabinet_text_input/handle_vote_chat_text_input.
    """
    chat_id = message["chat"]["id"]
    actor = message.get("from") or {}
    replied_to = (message.get("reply_to_message") or {}).get("message_id")
    found = next(
        (
            (flow_id, flow)
            for flow_id, flow in vote_result_flows.items()
            if flow.get("chat_id") == chat_id
            and flow.get("user_id") == actor.get("id")
            and flow.get("prompt_message_id") is not None
            and flow.get("prompt_message_id") == replied_to
            and time.monotonic() - flow["created_at"] <= VOTE_RESULT_FLOW_TTL_SECONDS
        ),
        None,
    )
    if found is None:
        return False
    flow_id, flow = found

    text = (message.get("text") or "").strip()
    if text.lower() in ("/cancel", "отмена"):
        vote_result_flows.pop(flow_id, None)
        await api.send_message(
            chat_id,
            "Итоги не опубликованы -- в чат ничего не отправлено. "
            "Голосование остаётся закрытым, результаты сохранены.",
            reply_to_message_id=message["message_id"], parse_mode=None,
        )
        return True
    if not await _can_manage_chat(api, flow.get("admin_chat_id"), actor, flow.get("entry")):
        vote_result_flows.pop(flow_id, None)
        return True  # silently dropped, same as badge_flows -- admin status changed mid-flow

    if text:
        flow["text"] = text
        _save_vote_results(flow["poll"], flow["standings"], text, log=log)
        log(
            f"[bot_listener] {actor.get('username') or actor.get('id')} rewrote the vote "
            f"results text ({len(text)} chars)"
        )
    # Used up: the next Редактировать installs a prompt of its own, so a stale one can
    # never swallow an unrelated reply later in the same DM.
    flow["prompt_message_id"] = None
    flow["created_at"] = time.monotonic()
    if not await _send_vote_result_draft(api, flow_id, flow, log=log):
        vote_result_flows.pop(flow_id, None)
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


# ------------------------------------------------------------- the pet game (/arena)
#
# The third game in this bot, and the first one that spends coins on something permanent.
# Its pure halves live outside this file the way cabinet.py's and poker.py's do:
#
#   pets_config.py   every tunable number, and nothing else -- re-balancing is editing
#                    that file and only that file
#   pets.py          state, storage and the wallet (economy.py, NOT a second currency)
#   pets_combat.py   the fight, deterministic given a seeded rng
#   pets_flavor.py   the joke library the fight log is written out of
#   pets_ui.py       every screen, as pure (text, keyboard) functions
#
# What is left here is what can only live here: Telegram I/O, working out who pressed a
# button, and the two flows that need free text or a photo back.
#
# "/arena" is the private setup menu. Public pet interactions use /pet and /duel.
PETS_COMMANDS = ("/arena", "/арена")
# Works in the group as well as the DM -- it is the one screen meant to be shown off, so
# refusing it in front of everybody would defeat the point.
PET_CARD_COMMANDS = ("/pet", "/пет", "/питомец")
PETS_RENAME_COMMANDS = ("/переименовать", "/rename")
DUEL_COMMANDS = ("/duel", "/дуэль")
PETS_ARENA_DELETE_AFTER = 3 * 60
PET_NOTICE_DELETE_AFTER = PETS_ARENA_DELETE_AFTER
GROUP_PETS_DELETE_AFTER = 30
DUEL_RESULT_DELETE_AFTER = GROUP_PETS_DELETE_AFTER
DUEL_TARGET_PROMPT_DELETE_AFTER = GROUP_PETS_DELETE_AFTER
# Same ten-minute window the cabinet flows use, and for the same reason: only naming and
# re-photographing a creature need server-side state at all. Every button carries its own
# owner id, so navigation itself survives a restart.
PETS_FLOW_TTL_SECONDS = 10 * 60
PETS_DM_ONLY_NOTICE = (
    "Приручить и прокачать существо можно в личке бота."
)


async def _pets_context(telethon_client, entry: str, tz, actor: dict, log=print):
    """(user, xp) for whoever is playing, or (None, None) if the chat has never seen them.

    The pet game rides on the same coin ledger as /shop, and economy.balance derives the
    earned half from live XP (see its docstring), so nothing here can be priced without
    first resolving the member -- which is also, conveniently, the check that stops
    somebody who has never written in the chat from farming the arena.
    """
    try:
        user, _, _, xp, _, _ = await stats.resolve_stat_target(
            telethon_client, entry, entry, "",
            actor.get("username"), _display_name(actor), tz, log=log,
        )
    except Exception:
        log(f"[pets] failed to resolve the player:\n{traceback.format_exc()}")
        return None, None
    if user is None:
        return None, None
    return user, xp


async def _send_pets_view(
    api: TelegramBotAPI, chat_id, rendered, reply_to_message_id=None, message_id=None,
    background_tasks: set | None = None, delete_after: int | None = None, log=print,
):
    """Draw a screen, editing the message the button was on when there is one.

    Editing rather than sending is what keeps the menu to a single message in the DM
    instead of a growing column of near-identical screens. A failed edit is not an error
    worth telling the player about -- Telegram rejects an edit whose text and keyboard are
    both unchanged, which is exactly what pressing "Обновить" twice does.
    """
    text, keyboard = rendered
    if message_id is not None:
        try:
            await api.edit_message_text(
                chat_id, message_id, text, reply_markup=keyboard, parse_mode="HTML",
            )
            return
        except Exception:
            pass
    try:
        sent = await api.send_message(
            chat_id, text, reply_to_message_id=reply_to_message_id,
            reply_markup=keyboard, parse_mode="HTML",
        )
        if delete_after and background_tasks and sent and "message_id" in sent:
            schedule_bot_delete(
                api, chat_id, [sent["message_id"]], delete_after, log, background_tasks,
                trigger_message_id=reply_to_message_id,
            )
    except Exception:
        log(f"[pets] failed to send a view:\n{traceback.format_exc()}")


async def handle_pets_command(
    api: TelegramBotAPI,
    telethon_client,
    cfg,
    tz,
    message: dict,
    entry: str,
    bot_username: str | None,
    background_tasks: set,
    pets_flows: dict,
    log=print,
) -> None:
    """"/arena" -- the pet game's menu.

    DM-only, for the same reason /cabinet is: every button on it spends the presser's
    coins, and a menu posted in the group would put one member's wallet in front of 190
    people and offer buttons the other 189 cannot use. The group gets a pointer and a deep
    link instead (a web_app button is private-chat only; a url button is not).
    """
    chat = message["chat"]
    chat_id = chat["id"]
    actor = message.get("from") or {}

    if chat.get("type") != "private":
        schedule_bot_delete(
            api, chat_id, [], GROUP_PETS_DELETE_AFTER, log, background_tasks,
            trigger_message_id=message["message_id"],
        )
        try:
            sent = await api.send_message(
                chat_id, PETS_DM_ONLY_NOTICE,
                reply_to_message_id=message["message_id"], parse_mode=None,
                reply_markup=(
                    {"inline_keyboard": [[{
                        "text": "Открыть Арену",
                        "url": f"https://t.me/{bot_username}?start=pets",
                    }]]} if bot_username else None
                ),
            )
            if sent and "message_id" in sent:
                schedule_bot_delete(
                    api, chat_id, [sent["message_id"]], GROUP_PETS_DELETE_AFTER, log,
                    background_tasks,
                )
        except Exception:
            log(f"[pets] failed to point a group at the DM:\n{traceback.format_exc()}")
        return

    user, xp = await _pets_context(telethon_client, entry, tz, actor, log=log)
    if user is None:
        try:
            await api.send_message(
                chat_id,
                "Ты ещё не отслеживаешься -- напиши что-нибудь в чат и попробуй снова.",
                reply_to_message_id=message["message_id"], parse_mode=None,
            )
        except Exception:
            pass
        return

    await _send_pets_view(
        api, chat_id, pets_ui.main_view(entry, user.user_id, xp),
        reply_to_message_id=message["message_id"], background_tasks=background_tasks,
        delete_after=PETS_ARENA_DELETE_AFTER, log=log,
    )


async def handle_pet_card_command(
    api: TelegramBotAPI,
    telethon_client,
    tz,
    message: dict,
    entry: str,
    command_text: str,
    background_tasks: set,
    bot_username: str | None = None,
    log=print,
) -> None:
    """"/pet" -- the creature's card, with its picture, in a DM or in the group.

    Takes the same target argument /stat does (a @username or a name fragment) and falls
    back to a replied-to message, so "show me yours" works without anybody having to type
    a username exactly.
    """
    chat = message["chat"]
    chat_id = chat["id"]
    actor = message.get("from") or {}
    group_chat = chat.get("type") != "private"
    if group_chat:
        schedule_bot_delete(
            api, chat_id, [], GROUP_PETS_DELETE_AFTER, log, background_tasks,
            trigger_message_id=message["message_id"],
        )
    argument = ""
    for spelling in PET_CARD_COMMANDS:
        if command_text.lower().startswith(spelling):
            argument = command_text[len(spelling):].strip()
            break
    replied = (message.get("reply_to_message") or {}).get("from") or {}
    if not argument and replied and not replied.get("is_bot"):
        argument = replied.get("username") or _display_name(replied)

    async def deliver(text: str, photo_file_id: str | None, reply_markup=None) -> None:
        try:
            if photo_file_id:
                sent = await api.send_photo(
                    chat_id, photo_file_id, caption=text,
                    reply_to_message_id=message["message_id"], reply_markup=reply_markup, parse_mode="HTML",
                )
            else:
                sent = await api.send_message(
                    chat_id, text, reply_to_message_id=message["message_id"],
                    reply_markup=reply_markup, parse_mode="HTML",
                )
        except Exception:
            log(f"[pets] failed to send a pet card:\n{traceback.format_exc()}")
            return
        if sent and "message_id" in sent:
            schedule_bot_delete(
                api, chat_id, [sent["message_id"]],
                GROUP_PETS_DELETE_AFTER if group_chat else PET_NOTICE_DELETE_AFTER,
                log, background_tasks,
            )

    try:
        user, _, _, _, _, _ = await stats.resolve_stat_target(
            telethon_client, entry, entry, argument,
            actor.get("username"), _display_name(actor), tz, log=log,
        )
    except Exception:
        log(f"[pets] failed to resolve a /pet target:\n{traceback.format_exc()}")
        return
    if user is None:
        await deliver("Не нашёл такого участника.", None)
        return

    pet = pets.get_pet(entry, user.user_id)
    if not pet:
        if str(user.user_id) == str(actor.get("id")):
            button = ({"inline_keyboard": [[{
                "text": "Призвать Существо",
                "url": f"https://t.me/{bot_username}?start=pets",
            }]]} if bot_username else None)
            await deliver("У тебя ещё нет существа.", None, button)
        else:
            await deliver(f"У {html.escape(user.display_name)} пока нет существа.", None)
        return
    await deliver(pets_ui.pet_card(entry, user.user_id, pet), pet.get("photo_file_id"))


async def handle_duel_command(
    api: TelegramBotAPI, telethon_client, tz, message: dict, entry: str, command_text: str,
    bot_username: str | None, background_tasks: set, log=print,
) -> None:
    """Run a public duel. The challenger alone spends a duel use and cooldown."""
    chat = message["chat"]
    if chat.get("type") == "private":
        return
    chat_id = chat["id"]
    actor = message.get("from") or {}
    schedule_bot_delete(
        api, chat_id, [], GROUP_PETS_DELETE_AFTER, log, background_tasks,
        trigger_message_id=message["message_id"],
    )
    argument = command_text.split(maxsplit=1)[1].strip() if len(command_text.split(maxsplit=1)) == 2 else ""
    replied = (message.get("reply_to_message") or {}).get("from") or {}
    if not argument and replied and not replied.get("is_bot"):
        argument = replied.get("username") or _display_name(replied)

    async def notice(
        text: str, summon: bool = False, delete_after: int = GROUP_PETS_DELETE_AFTER,
        trigger_delete_after: int | None = None,
    ) -> None:
        markup = None
        if summon and bot_username:
            markup = {"inline_keyboard": [[{
                "text": "Призвать Существо", "url": f"https://t.me/{bot_username}?start=pets",
            }]]}
        sent = await api.send_message(
            chat_id, text, reply_to_message_id=message["message_id"],
            reply_markup=markup, parse_mode="HTML",
        )
        if sent and "message_id" in sent:
            schedule_bot_delete(
                api, chat_id, [sent["message_id"]], delete_after, log, background_tasks,
            )
            if trigger_delete_after is not None:
                schedule_bot_delete(
                    api, chat_id, [], trigger_delete_after, log, background_tasks,
                    trigger_message_id=message["message_id"],
                )

    if not argument:
        await notice(
            "Укажи соперника: /duel @user.",
            delete_after=DUEL_TARGET_PROMPT_DELETE_AFTER,
            trigger_delete_after=None,
        )
        return
    challenger, xp = await _pets_context(telethon_client, entry, tz, actor, log=log)
    if challenger is None:
        await notice("Ты ещё не отслеживаешься в этом чате.")
        return
    try:
        target, _, _, _, _, _ = await stats.resolve_stat_target(
            telethon_client, entry, entry, argument,
            actor.get("username"), _display_name(actor), tz, log=log,
        )
    except Exception:
        log(f"[pets] failed to resolve duel target:\n{traceback.format_exc()}")
        await notice("Не удалось найти соперника.")
        return
    if target is None:
        await notice("Не нашёл такого участника.")
        return
    if str(target.user_id) == str(challenger.user_id):
        await notice("С собой дуэлиться нельзя.")
        return
    if not pets.get_pet(entry, challenger.user_id):
        await notice("У тебя ещё нет существа.", summon=True)
        return
    if not pets.get_pet(entry, target.user_id):
        await notice(f"У {html.escape(target.display_name)} пока нет существа.")
        return
    ok, reason = pets.claim_duel(entry, challenger.user_id, target.user_id)
    if not ok:
        await notice(html.escape(reason))
        return
    await _pets_run_fight(
        api, chat_id, message["message_id"], entry, challenger.user_id, str(target.user_id),
        xp, log, background_tasks=background_tasks, delete_after=DUEL_RESULT_DELETE_AFTER,
        include_keyboard=False,
        persistent_recipient_ids=(challenger.user_id, target.user_id),
        enforce_arena_target_limit=False,
    )

def _pets_fighter(entry: str, user_id, pet: dict):
    """A pets_combat.Fighter built from EFFECTIVE stats -- purchased levels plus the pet's
    own level plus whatever it is wearing. Combat never reads the store itself, which is
    what lets a fight be replayed from a seed in a test."""
    effective = pets.effective_stats(entry, user_id)
    return pets_combat.Fighter(
        key=str(user_id),
        name=pet.get("name") or "Существо",
        strength=effective.get("strength", 1),
        health=effective.get("health", 1),
        agility=effective.get("agility", 1),
        luck=effective.get("luck", 1),
        armor=effective.get("armor", 0),
    )


async def _pets_start_flow(
    api: TelegramBotAPI, pets_flows: dict, chat_id, user_id, entry: str,
    awaiting: str, prompt_text: str, reply_to_message_id,
) -> None:
    prompt = await api.send_message(
        chat_id, prompt_text, reply_to_message_id=reply_to_message_id,
        reply_markup={"force_reply": True, "selective": True}, parse_mode=None,
    )
    pets_flows[uuid.uuid4().hex[:10]] = {
        "created_at": time.monotonic(),
        "chat_id": chat_id,
        "user_id": user_id,
        "entry": entry,
        "awaiting": awaiting,
        "photo_file_id": None,
        "prompt_message_id": prompt.get("message_id") if prompt else None,
    }


async def handle_pets_callback(
    api: TelegramBotAPI,
    telethon_client,
    cfg,
    tz,
    callback: dict,
    entry: str | None,
    pets_flows: dict,
    background_tasks: set,
    log=print,
) -> None:
    """Every button in the pet menu.

    The tap is answered BEFORE anything that can block. _pets_context goes through
    Telethon to resolve the member, and a Telethon call made before answerCallbackQuery
    leaves the button spinning on the client until it times out -- a bug this codebase has
    already been bitten by once.
    """
    parsed = pets_ui.parse_callback(callback.get("data"))
    if parsed is None:
        return
    owner_id, action, argument = parsed
    callback_id = callback.get("id")
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    actor = callback.get("from") or {}

    # The owner id rides inside the button, so a forwarded menu cannot be used to spend
    # somebody else's coins.
    if str(actor.get("id")) != str(owner_id):
        await api.answer_callback_query(callback_id, "Это чужая арена.")
        return
    if entry is None:
        await api.answer_callback_query(callback_id, "Основной чат не настроен.")
        return
    if action == "noop":
        await api.answer_callback_query(callback_id, "Уже максимум.")
        return
    await api.answer_callback_query(callback_id)

    user, xp = await _pets_context(telethon_client, entry, tz, actor, log=log)
    if user is None:
        await _send_pets_view(
            api, chat_id,
            ("Ты ещё не отслеживаешься -- напиши что-нибудь в чат и попробуй снова.",
             {"inline_keyboard": []}),
            message_id=message_id, log=log,
        )
        return
    user_id = user.user_id

    try:
        # --- the two flows that need something back from the player ------------------
        if action in ("tame", "rename", "photo"):
            if action == "tame" and not pets.has_cage(entry, user_id):
                await _send_pets_view(
                    api, chat_id, pets_ui.cage_view(entry, user_id, xp),
                    message_id=message_id, log=log,
                )
                return
            prompts = {
                "tame": "Пришли фото будущего существа (картинкой, не файлом).",
                "photo": "Пришли новое фото существа (картинкой, не файлом).",
                "rename": "Ответь на это сообщение новым именем существа.",
            }
            await _pets_start_flow(
                api, pets_flows, chat_id, actor.get("id"), entry,
                "name" if action == "rename" else f"photo_{action}",
                prompts[action], message_id,
            )
            return

        # --- purchases ---------------------------------------------------------------
        if action == "buycage":
            ok, note = pets.buy_cage(entry, user_id, xp)
            await _pets_toast_and_redraw(
                api, chat_id, message_id, note, pets_ui.cage_view(entry, user_id, xp), log
            )
            return
        if action == "upcage":
            ok, note = pets.upgrade_cage(entry, user_id, xp)
            await _pets_toast_and_redraw(
                api, chat_id, message_id, note, pets_ui.cage_view(entry, user_id, xp), log
            )
            return
        if action in ("up", "up10"):
            ok, note, _ = pets.upgrade_stat(
                entry, user_id, xp, argument, times=10 if action == "up10" else 1
            )
            await _pets_toast_and_redraw(
                api, chat_id, message_id, note, pets_ui.train_view(entry, user_id, xp), log
            )
            return
        if action == "buy":
            ok, note = pets.buy_item(entry, user_id, xp, argument)
            slot = pets_ui.slot_of(argument)
            await _pets_toast_and_redraw(
                api, chat_id, message_id, note,
                pets_ui.slot_view(entry, user_id, xp, slot), log
            )
            return
        if action == "equip":
            ok, note = pets.equip(entry, user_id, argument)
            slot = pets_ui.slot_of(argument)
            await _pets_toast_and_redraw(
                api, chat_id, message_id, note,
                pets_ui.slot_view(entry, user_id, xp, slot), log
            )
            return
        if action == "unequip":
            ok, note = pets.unequip(entry, user_id, argument)
            await _pets_toast_and_redraw(
                api, chat_id, message_id, note,
                pets_ui.slot_view(entry, user_id, xp, argument), log
            )
            return

        # --- the arena ---------------------------------------------------------------
        if action == "search":
            if pets.fights_left(entry, user_id, pets.today()) <= 0:
                await _send_pets_view(
                    api, chat_id, pets_ui.fight_view(entry, user_id, xp),
                    message_id=message_id, log=log,
                )
                return
            rerolls_used, search_seed = pets_ui.parse_search_argument(argument)
            search_seed = search_seed if search_seed is not None else secrets.randbits(32)
            if rerolls_used > C.MAX_OPPONENT_REROLLS:
                await _send_pets_view(
                    api, chat_id, pets_ui.fight_view(entry, user_id, xp),
                    message_id=message_id, log=log,
                )
                return
            candidates = pets.opponent_cycle(entry, user_id, search_seed)
            if rerolls_used >= len(candidates):
                await _send_pets_view(
                    api, chat_id,
                    pets_ui.notice_view(
                        user_id,
                        "Соперников пока нет: ты единственный, кто завёл существо. "
                        "Позови кого-нибудь в чат.",
                    ),
                    message_id=message_id, log=log,
                )
                return
            opponent_id = candidates[rerolls_used]
            rerolls_allowed = min(C.MAX_OPPONENT_REROLLS, len(candidates) - 1)
            await _send_pets_view(
                api, chat_id,
                pets_ui.opponent_view(
                    entry, user_id, opponent_id, xp, rerolls_used=rerolls_used,
                    rerolls_allowed=rerolls_allowed, search_seed=search_seed,
                ),
                message_id=message_id, log=log,
            )
            return

        if action == "attack":
            await _pets_run_fight(
                api, chat_id, message_id, entry, user_id, argument, xp, log
            )
            return

        # --- plain redraws -------------------------------------------------------------
        views = {
            "main": lambda: pets_ui.main_view(entry, user_id, xp),
            "info": lambda: pets_ui.info_view(user_id),
            "cage": lambda: pets_ui.cage_view(entry, user_id, xp),
            "train": lambda: pets_ui.train_view(entry, user_id, xp),
            "bag": lambda: pets_ui.bag_view(entry, user_id, xp),
            "fight": lambda: pets_ui.fight_view(entry, user_id, xp),
            "history": lambda: pets_ui.history_view(entry, user_id),
            "pet": lambda: pets_ui.pet_view(entry, user_id),
            "slot": lambda: pets_ui.slot_view(entry, user_id, xp, argument),
        }
        render = views.get(action)
        if render is None:
            return
        await _send_pets_view(api, chat_id, render(), message_id=message_id, log=log)
    except Exception:
        log(f"[pets] callback '{action}' failed:\n{traceback.format_exc()}")
        await _send_pets_view(
            api, chat_id,
            pets_ui.notice_view(user_id, "Что-то сломалось. Попробуй ещё раз: /arena"),
            message_id=message_id, log=log,
        )


async def _pets_toast_and_redraw(api, chat_id, message_id, note: str, rendered, log) -> None:
    """A purchase's answer belongs ON the screen it changed, not in a toast that vanishes:
    the note is prepended to the redrawn view so "не хватает 40 монет" is still readable a
    minute later, next to the price that caused it."""
    text, keyboard = rendered
    await _send_pets_view(
        api, chat_id, (f"{html.escape(note)}\n\n{text}", keyboard),
        message_id=message_id, log=log,
    )


async def _pets_run_fight(
    api: TelegramBotAPI, chat_id, message_id, entry: str, user_id, opponent_raw: str,
    xp: int, log, background_tasks: set | None = None, delete_after: int | None = None,
    include_keyboard: bool = True, persistent_recipient_ids=None,
    enforce_arena_target_limit: bool = True,
) -> None:
    """One duel, start to finish: simulate, record, print.

    The daily fight is spent inside pets.record_fight, together with the payout, so a
    crash between "fought" and "paid" cannot exist -- there is one write, not two.
    """
    mine = pets.get_pet(entry, user_id)
    opponent_id = (opponent_raw or "").strip()
    theirs = pets.get_pet(entry, opponent_id) if opponent_id else None
    if not mine or not theirs:
        await _send_pets_view(
            api, chat_id, pets_ui.fight_view(entry, user_id, xp),
            message_id=message_id, log=log,
        )
        return
    if enforce_arena_target_limit and not pets.can_attack_in_arena(entry, user_id, opponent_id):
        await _send_pets_view(
            api, chat_id,
            pets_ui.notice_view(
                user_id,
                f"Этого соперника можно атаковать не больше {C.ARENA_SAME_OPPONENT_DAILY_LIMIT} раз в день.",
            ),
            message_id=message_id, log=log,
        )
        return
    if pets.fights_left(entry, user_id, pets.today()) <= 0:
        await _send_pets_view(
            api, chat_id, pets_ui.fight_view(entry, user_id, xp),
            message_id=message_id, log=log,
        )
        return

    attacker_fighter = _pets_fighter(entry, user_id, mine)
    defender_fighter = _pets_fighter(entry, opponent_id, theirs)
    seed = secrets.randbits(63)
    result = pets_combat.simulate(attacker_fighter, defender_fighter, seed=seed)
    combat_snapshot = {
        "seed": seed,
        "fighters": {
            str(user_id): _pets_fighter_snapshot(attacker_fighter),
            str(opponent_id): _pets_fighter_snapshot(defender_fighter),
        },
    }
    reward = pets.record_fight(
        entry, user_id, opponent_id, result, pets.today(), attacker_xp=xp,
        combat_snapshot=combat_snapshot,
    )
    report = pets_ui.fight_report(
        result, str(user_id),
        {str(user_id): mine.get("name"), str(opponent_id): theirs.get("name")},
        reward,
    )
    image_path = None
    try:
        image_path = await _pets_render_result_image(
            api, result, entry, user_id, opponent_id, mine, theirs, log,
        )
        sent = None
        if image_path is not None:
            sent = await api.send_photo_file(
                chat_id, image_path, caption=report,
                reply_markup=pets_ui.fight_report_keyboard(user_id) if include_keyboard else None,
                parse_mode="HTML",
            )
        else:
            sent = await api.send_message(
                chat_id, report,
                reply_markup=pets_ui.fight_report_keyboard(user_id) if include_keyboard else None,
                parse_mode="HTML",
            )
    except Exception:
        log(f"[pets] failed to send a fight report:\n{traceback.format_exc()}")
    else:
        if delete_after and background_tasks and sent and "message_id" in sent:
            schedule_bot_delete(
                api, chat_id, [sent["message_id"]], delete_after, log, background_tasks,
                trigger_message_id=message_id,
            )
    finally:
        if image_path is not None:
            for recipient_id in dict.fromkeys(persistent_recipient_ids or ()):
                try:
                    await api.send_photo_file(
                        recipient_id, image_path, caption=report, parse_mode="HTML",
                    )
                except Exception:
                    # A bot cannot message a member who has not started it; their opponent
                    # should still receive the report.
                    log(f"[pets] could not deliver the duel image to {recipient_id}")
        if image_path is not None:
            try:
                image_path.unlink(missing_ok=True)
            except OSError:
                pass


def _pets_fighter_snapshot(fighter: pets_combat.Fighter) -> dict:
    return {
        key: getattr(fighter, key)
        for key in ("key", "name", "strength", "health", "agility", "luck", "armor")
    }


async def _pets_download_media(api, file_id, log) -> bytes | None:
    if not file_id or not hasattr(api, "download_file"):
        return None
    try:
        return await api.download_file(file_id)
    except Exception:
        log("[pets] could not fetch fight-result media")
        return None


async def _pets_owner_avatar(api, user_id, log) -> bytes | None:
    if not hasattr(api, "get_user_profile_photo"):
        return None
    try:
        file_id = await api.get_user_profile_photo(user_id)
    except Exception:
        log("[pets] could not fetch owner avatar")
        return None
    return await _pets_download_media(api, file_id, log)


async def _pets_render_result_image(
    api, result, entry: str, attacker_id, defender_id, attacker: dict, defender: dict, log,
):
    attacker_stats = pets.effective_stats(entry, attacker_id)
    defender_stats = pets.effective_stats(entry, defender_id)
    pet_a, pet_b, avatar_a, avatar_b = await asyncio.gather(
        _pets_download_media(api, attacker.get("photo_file_id"), log),
        _pets_download_media(api, defender.get("photo_file_id"), log),
        _pets_owner_avatar(api, attacker_id, log),
        _pets_owner_avatar(api, defender_id, log),
    )
    path = pets_image.temporary_result_path()
    try:
        return pets_image.render_fight_result(path, result, {
            "id": str(attacker_id),
            "pet_name": attacker.get("name"),
            "owner_name": attacker.get("owner_name"),
            "stats": attacker_stats,
            "power": pets.power_rating(entry, attacker_id),
            "pet_photo": pet_a,
            "owner_avatar": avatar_a,
        }, {
            "id": str(defender_id),
            "pet_name": defender.get("name"),
            "owner_name": defender.get("owner_name"),
            "stats": defender_stats,
            "power": pets.power_rating(entry, defender_id),
            "pet_photo": pet_b,
            "owner_avatar": avatar_b,
        })
    except Exception:
        path.unlink(missing_ok=True)
        raise


async def handle_pets_rename_command(
    api: TelegramBotAPI,
    telethon_client,
    tz,
    message: dict,
    entry: str,
    command_text: str,
    log=print,
) -> None:
    """"/переименовать <имя>" -- the typed spelling of the pet menu's rename button.

    Shares pets.rename with the button, so the name rules (length, duplicates, the angle
    brackets that would break the HTML the card is sent with) are enforced in exactly one
    place no matter which way somebody asks.
    """
    chat_id = message["chat"]["id"]
    actor = message.get("from") or {}
    argument = ""
    for spelling in PETS_RENAME_COMMANDS:
        if command_text.lower().startswith(spelling):
            argument = command_text[len(spelling):].strip()
            break

    async def reply(text: str) -> None:
        try:
            await api.send_message(
                chat_id, text, reply_to_message_id=message["message_id"], parse_mode=None,
            )
        except Exception:
            log(f"[pets] failed to answer a rename:\n{traceback.format_exc()}")

    if not argument:
        await reply("Формат: /переименовать <новое имя>")
        return
    user, _ = await _pets_context(telethon_client, entry, tz, actor, log=log)
    if user is None:
        await reply("Ты ещё не отслеживаешься -- напиши что-нибудь в чат и попробуй снова.")
        return
    _, note = pets.rename(entry, user.user_id, argument)
    await reply(note)


async def maybe_handle_pets_flow_message(
    api: TelegramBotAPI,
    telethon_client,
    tz,
    message: dict,
    pets_flows: dict,
    log=print,
) -> bool:
    """The photo or the name a pet flow is waiting for. True when this message was one.

    Matched on the force-reply it answers, exactly as the cabinet and button-builder flows
    are, so two people naming creatures in their own DMs at the same time cannot collide.
    """
    chat_id = message["chat"]["id"]
    actor = message.get("from") or {}
    replied_message_id = (message.get("reply_to_message") or {}).get("message_id")
    pair = next(
        (
            (flow_id, flow)
            for flow_id, flow in pets_flows.items()
            if flow.get("chat_id") == chat_id
            and flow.get("user_id") == actor.get("id")
            and flow.get("prompt_message_id") == replied_message_id
            and time.monotonic() - flow["created_at"] <= PETS_FLOW_TTL_SECONDS
        ),
        None,
    )
    if pair is None:
        return False
    flow_id, flow = pair
    entry = flow["entry"]
    raw = (message.get("text") or message.get("caption") or "").strip()
    if raw.lower() in ("/cancel", "отмена"):
        pets_flows.pop(flow_id, None)
        await api.send_message(chat_id, "Отменил.", parse_mode=None)
        return True

    user, xp = await _pets_context(telethon_client, entry, tz, actor, log=log)
    if user is None:
        pets_flows.pop(flow_id, None)
        return True

    awaiting = flow.get("awaiting")
    try:
        if awaiting in ("photo_tame", "photo_photo"):
            photos = message.get("photo") or []
            if not photos:
                await api.send_message(
                    chat_id, "Нужна именно картинка. Пришли фото ещё раз, ответом на тот же вопрос.",
                    parse_mode=None,
                )
                return True
            # The largest size Telegram offers. Only the file_id is stored -- the picture
            # itself stays on Telegram's servers, so a creature costs this bot no disk.
            file_id = photos[-1]["file_id"]
            if awaiting == "photo_photo":
                pets_flows.pop(flow_id, None)
                ok, note = pets.set_photo(entry, user.user_id, file_id)
                await _send_pets_view(
                    api, chat_id, pets_ui.notice_view(user.user_id, note), log=log
                )
                return True
            flow["photo_file_id"] = file_id
            flow["awaiting"] = "name_tame"
            prompt = await api.send_message(
                chat_id, "Отлично. Теперь ответь на это сообщение именем существа.",
                reply_markup={"force_reply": True, "selective": True}, parse_mode=None,
            )
            flow["prompt_message_id"] = prompt.get("message_id") if prompt else None
            return True

        if awaiting in ("name", "name_tame"):
            if not raw:
                await api.send_message(chat_id, "Пустое имя не подойдёт.", parse_mode=None)
                return True
            pets_flows.pop(flow_id, None)
            if awaiting == "name":
                ok, note = pets.rename(entry, user.user_id, raw)
            else:
                ok, note = pets.tame(
                    entry, user.user_id, xp, raw,
                    flow.get("photo_file_id"), _display_name(actor),
                )
            await _send_pets_view(
                api, chat_id,
                pets_ui.main_view(entry, user.user_id, xp) if ok
                else pets_ui.notice_view(user.user_id, note),
                log=log,
            )
            if ok:
                await api.send_message(chat_id, note, parse_mode=None)
            return True
    except Exception:
        log(f"[pets] flow step '{awaiting}' failed:\n{traceback.format_exc()}")
        pets_flows.pop(flow_id, None)
        await api.send_message(
            chat_id, "Не получилось. Начни заново: /arena", parse_mode=None,
        )
        return True
    return False


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
    badge_flows: dict[str, dict],
    cabinet_flows: dict[str, dict],
    menu_last_sent: dict,
    button_builder_flows: dict[str, dict] | None = None,
    vote_chat_flows: dict[str, dict] | None = None,
    vote_result_flows: dict[str, dict] | None = None,
    pets_flows: dict[str, dict] | None = None,
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
    vote_result_flows = vote_result_flows if vote_result_flows is not None else {}
    pets_flows = pets_flows if pets_flows is not None else {}
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
        elif callback_data.startswith(f"{VOTE_CLEAR_CALLBACK_PREFIX}:"):
            await handle_vote_clear_callback(api, cfg, tz, callback, home_chat_ref, log=log)
        elif callback_data.startswith(f"{ARENA_ACTION_CALLBACK_PREFIX}:"):
            await handle_arena_action_callback(
                api, telethon_client, cfg, tz, callback, home_chat_ref, bot_username,
                background_tasks, vote_chat_flows, log=log,
            )
        elif callback_data.startswith(f"{VOTE_ACTION_CALLBACK_PREFIX}:"):
            await handle_vote_action_callback(
                api, telethon_client, cfg, tz, callback, home_chat_ref, bot_username,
                background_tasks, vote_chat_flows, log=log,
            )
        elif callback_data.startswith(f"{VOTE_CHAT_DEST_CALLBACK_PREFIX}:"):
            # No chat resolution here either: the draft carries the ids it needs, so
            # choosing a destination never waits on the Telethon session.
            await handle_vote_chat_destination_callback(
                api, cfg, callback, vote_chat_flows, bot_username, log=log,
            )
        elif callback_data.startswith(f"{pets_ui.CALLBACK_PREFIX}:"):
            # The pet menu. No chat resolution in the argument list: every button carries
            # its owner's id, and the member lookup happens inside, AFTER the spinner is
            # stopped.
            await handle_pets_callback(
                api, telethon_client, cfg, tz, callback,
                _stats_entry_for(callback.get("message", {}).get("chat", {}), None, home_chat_ref),
                pets_flows, background_tasks, log=log,
            )
        elif callback_data.startswith(f"{VOTE_RESULT_CALLBACK_PREFIX}:"):
            # No chat resolution here either: the draft already carries the main chat's id
            # (resolved when the vote was closed), so pressing Отправить never waits on the
            # Telethon session.
            await handle_vote_result_callback(api, callback, vote_result_flows, log=log)
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
    # known_chat_ids (see run_bot_listener's queue consumers) finds out the Bot-API
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
        # ?start=vote_chat, ?start=vote_image) land, and the natural first thing a new
        # member does anyway.
        # Groups are ignored: a
        # /start there is somebody's fat finger, not a request. Any payload other than the
        # vote ones -- including none at all -- opens the cabinet, matching the old
        # unconditional behavior.
        if chat.get("type") != "private":
            return
        start_payload = (start_match.group(1) or "").lower()
        # The arena's own deep link, from the url button a group /vote2 leaves behind
        # (a web_app button is private-chat only, exactly as for v1). "arena" is still
        # accepted because announcements posted before the command was renamed are still
        # sitting in the group with that payload baked into their button.
        if start_payload == "pets":
            pets_entry = _stats_entry_for(chat, matched_entry, home_chat_ref)
            if pets_entry is not None:
                await handle_pets_command(
                    api, telethon_client, cfg, tz, message, pets_entry, bot_username,
                    background_tasks, pets_flows, log=log,
                )
            return
        if start_payload in ("vote2", "arena"):
            await handle_arena_command(
                api, telethon_client, cfg, tz, message,
                _stats_entry_for(chat, matched_entry, home_chat_ref), bot_username,
                background_tasks, log=log, vote_chat_flows=vote_chat_flows,
            )
            return
        if start_payload in ("vote", "vote_admin", "vote_clear", "vote_chat", "vote_image"):
            forced_mode = {
                "vote_admin": "moderate", "vote_clear": "clear",
                "vote_chat": "chat", "vote_image": "image",
            }.get(start_payload)
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

    if await handle_vote_result_text_input(
        api, message, vote_result_flows, log=log
    ):
        return

    # Before any command match: a creature's name is free text and could easily be
    # something starting with a slash.
    if await maybe_handle_pets_flow_message(
        api, telethon_client, tz, message, pets_flows, log=log
    ):
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

    # "/vote2" -- the second voting system. Checked BEFORE /vote so
    # "/vote2" reaches the arena rather than being swallowed by "/vote"'s prefix match and
    # opening v1's ballot with a stray "2" as its argument.
    # "/arena" -- the pet game. Nothing to do with ARENA_COMMANDS below, which is the
    # voting system that used to answer to this word and now answers to "/vote2".
    if any(
        re.match(rf"^{re.escape(spelling)}(?:\s|$)", command_text, re.IGNORECASE)
        for spelling in PETS_COMMANDS
    ):
        pets_entry = _stats_entry_for(chat, matched_entry, home_chat_ref)
        if pets_entry is None:
            return
        await handle_pets_command(
            api, telethon_client, cfg, tz, message, pets_entry, bot_username,
            background_tasks, pets_flows, log=log,
        )
        return

    # "/pet" works in the group as well as the DM -- it is the one screen meant to be
    # shown off.
    if any(
        re.match(rf"^{re.escape(spelling)}(?:\s|$)", command_text, re.IGNORECASE)
        for spelling in PET_CARD_COMMANDS
    ):
        pets_entry = _stats_entry_for(chat, matched_entry, home_chat_ref)
        if pets_entry is None:
            return
        await handle_pet_card_command(
            api, telethon_client, tz, message, pets_entry, command_text,
            background_tasks, bot_username=bot_username, log=log,
        )
        return

    if any(
        re.match(rf"^{re.escape(spelling)}(?:\s|$)", command_text, re.IGNORECASE)
        for spelling in DUEL_COMMANDS
    ):
        pets_entry = _stats_entry_for(chat, matched_entry, home_chat_ref)
        if pets_entry is None:
            return
        await handle_duel_command(
            api, telethon_client, tz, message, pets_entry, command_text, bot_username,
            background_tasks, log=log,
        )
        return

    # "/переименовать <имя>" -- the same rename the pet menu's button does, for somebody
    # who would rather type it. DM-only, like the rest of the menu.
    if any(
        re.match(rf"^{re.escape(spelling)}(?:\s|$)", command_text, re.IGNORECASE)
        for spelling in PETS_RENAME_COMMANDS
    ):
        if chat.get("type") != "private":
            return
        pets_entry = _stats_entry_for(chat, matched_entry, home_chat_ref)
        if pets_entry is None:
            return
        await handle_pets_rename_command(
            api, telethon_client, tz, message, pets_entry, command_text, log=log,
        )
        return

    if any(command_text.lower().startswith(c) for c in ARENA_COMMANDS):
        arena_entry = _stats_entry_for(chat, matched_entry, home_chat_ref)
        if arena_entry is None:
            return
        task = asyncio.create_task(
            handle_arena_command(
                api, telethon_client, cfg, tz, message, arena_entry, bot_username,
                background_tasks, log=log, vote_chat_flows=vote_chat_flows,
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

    # Only when the message OPENS with the command ("/summary ...", or "/summary@thisbot
    # ..." as Telegram spells a command in a group). Quoting it mid-sentence is talking
    # about it, not asking for one, and a summary is an OpenAI call -- see
    # listener.is_summary_command for the whole rule.
    has_summary = is_summary_command(message_text)

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
    figurine_ack_queue: "asyncio.Queue | None" = None,
    stats_digest_queue: "asyncio.Queue | None" = None,
    dismiss_queue: "asyncio.Queue | None" = None,
    file_block_queue: "asyncio.Queue | None" = None,
):
    """Runs until cancelled. Meant to be started as a sibling asyncio task alongside
    listener.py's Telethon client -- both share the same connected `telethon_client` for
    message fetching.

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

    `file_block_queue`, if given, carries (allowed_chats entry, message_id, notice text)
    pairs from listener.py's blocked-file check (see BLOCKED_FILE_EXTENSIONS there): an
    archive or 3D model somebody attached in the group. Both halves happen here -- the
    delete, because removing SOMEBODY ELSE's message needs the "delete messages" admin
    right that this bot account normally holds and that session's personal account normally
    doesn't, and the notice, because it's a chat post like every other one. The notice is
    HTML (it may carry a tg://user mention for a sender with no @username) and sweeps
    itself after BLOCKED_FILE_NOTICE_DELETE_AFTER, with no trigger message to take along:
    the file that prompted it is already gone. It is None for the second and later files
    of a single album -- those are still deleted, just not each answered separately.

    All queues are left None when run standalone (this module's own main()), which
    just means figurine reactions/digests/dismissals/file blocks never fire, matching
    that listener.py isn't running their detection either in that mode. Commands and
    /summary still work standalone: those come from the bot's own updates."""
    allowed_chats = set(c.lower().lstrip("@") for c in cfg.listener_allowed_chats)
    background_tasks: set[asyncio.Task] = set()
    summary_queue: asyncio.Queue = asyncio.Queue()
    # Maps a LISTENER_ALLOWED_CHATS entry to the Bot-API chat_id it corresponds to.
    # Populated passively by _dispatch_update as it observes live updates from that chat
    # (see the comment there) and, on a miss, actively by _resolve_chat_id via the
    # Telethon session -- see that function's docstring for why that's safe to do.
    known_chat_ids: dict[str, int] = {}
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
    # Short-lived results drafts, one per admin who has just closed a vote from the Mini
    # App (see send_vote_results_draft). The results THEMSELVES are already on disk by the
    # time an entry appears here -- voting.save_results is called when the draft is
    # produced, not when it is posted -- so losing this on a restart costs the admin their
    # wording and one "подведи итоги" again, never the week's result.
    vote_result_flows: dict[str, dict] = {}
    # Short-lived taming/renaming prompts for the pet game. Everything a creature IS lives
    # on disk (pets.py); only the half-finished "send me a photo, now send me a name"
    # conversation is here, so losing it on a restart costs one re-press.
    pets_flows: dict[str, dict] = {}
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
            f"[bot_listener] logged in as @{bot_username or me.get('id')}. Long-polling for messages "
            f"STARTING WITH '{SUMMARY_COMMAND}' (summary) and every other command. FIFO queue delay: "
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
                            known_chat_ids, badge_flows,
                            cabinet_flows, menu_last_sent,
                            button_builder_flows=button_builder_flows, vote_chat_flows=vote_chat_flows,
                            vote_result_flows=vote_result_flows, pets_flows=pets_flows, log=log,
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
                        sent = None
                        try:
                            sent = await api.send_message(
                                chat["id"], "Что-то пошло не так при генерации сводки.",
                                reply_to_message_id=message["message_id"],
                            )
                        except Exception:
                            pass
                        # Swept on the same clock as the receipt handle_bot_summary_request
                        # would have left (SUMMARY_RECEIPT_DELETE_AFTER): a crash was the
                        # one outcome that still stranded both a notice AND the request in
                        # the group for good. Groups only -- in a DM the exchange is the
                        # person's own and there is no clutter to clear.
                        if chat.get("type") != "private":
                            schedule_bot_delete(
                                api, chat["id"],
                                [sent["message_id"]] if sent and "message_id" in sent else [],
                                SUMMARY_RECEIPT_DELETE_AFTER, log, background_tasks,
                                trigger_message_id=message["message_id"],
                            )
                finally:
                    last_finished_at = time.monotonic()
                    summary_queue.task_done()

        async def _consume_figurine_acks():
            while True:
                entry, message_id = await figurine_ack_queue.get()
                chat_id = await _resolve_chat_id(telethon_client, entry, known_chat_ids, log=log)
                if chat_id is None:
                    log(f"[bot_listener] dropping figurine reaction for '{entry}': could not resolve a chat_id for it")
                    continue
                await api.set_message_reaction(chat_id, message_id, FIGURINE_ACK_EMOJI, log=log)

        async def _consume_file_blocks():
            while True:
                item = await file_block_queue.get()
                # Guarded like _consume_stats_digests: every consumer here shares one
                # asyncio.gather with the poll loop, so an exception escaping this loop
                # takes the whole bot down rather than costing one item.
                try:
                    entry, message_id, notice = item
                except (TypeError, ValueError):
                    log(f"[bot_listener] dropping malformed file-block item: {item!r}")
                    continue
                try:
                    chat_id = await _resolve_chat_id(telethon_client, entry, known_chat_ids, log=log)
                    if chat_id is None:
                        log(f"[bot_listener] dropping file deletion for '{entry}': could not resolve a chat_id for it")
                        continue
                    # api.delete_message is best-effort and swallows its own failure, so
                    # the notice can still go out over a message that survived (no delete
                    # rights, older than 48h) -- better a stated rule than silence.
                    await api.delete_message(chat_id, message_id)
                    # notice is None for the second and later files of one album: every
                    # file goes, the sender is told once (see blocked_album_groups in
                    # listener.py).
                    if notice is not None:
                        sent = await api.send_message(chat_id, notice, parse_mode="HTML")
                        # No trigger_message_id: the message that prompted this notice is
                        # the file, and it has already been deleted above.
                        if sent and "message_id" in sent:
                            schedule_bot_delete(
                                api, chat_id, [sent["message_id"]],
                                BLOCKED_FILE_NOTICE_DELETE_AFTER, log, background_tasks,
                            )
                    log(f"[bot_listener] deleted blocked file {message_id} in '{entry}'")
                except Exception:
                    log(f"[bot_listener] failed to handle a blocked file in '{entry}':\n{traceback.format_exc()}")

        async def _consume_stats_digests():
            while True:
                item = await stats_digest_queue.get()
                # Every consumer here runs under the same asyncio.gather as the polling
                # loop, so an exception raised out of one does not merely lose its own
                # item -- it takes the whole listener down. That is exactly what a
                # short item on this queue did in production (ValueError: not enough
                # values to unpack), killing the process for a level-up announcement.
                # The producers are fixed; this makes the loop survive the next one.
                try:
                    entry, text, parse_mode, photo = item
                except (TypeError, ValueError):
                    log(f"[bot_listener] dropping malformed stats digest item: {item!r}")
                    continue
                try:
                    chat_id = await _resolve_chat_id(telethon_client, entry, known_chat_ids, log=log)
                    if chat_id is None:
                        log(f"[bot_listener] dropping stats notification for '{entry}': could not resolve a chat_id for it")
                        continue
                    await send_stats_digest(api, chat_id, entry, text, parse_mode, photo, log=log)
                except Exception:
                    log(f"[bot_listener] failed to send a stats digest for '{entry}':\n{traceback.format_exc()}")

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

        async def _announce_vote_winner(user: dict, poll, standings: list) -> None:
            """Hands the admin who just closed the vote a DRAFT of the results in their own
            DM -- the results text with Редактировать/Отправить/Отмена under it -- instead
            of announcing anything. Nothing reaches the chat until they press Отправить
            (see send_vote_results_draft and handle_vote_result_callback).

            `user` is whoever closed the vote, taken from the page's own verified identity
            rather than re-deriving "the admin" some other way. `standings` is poll.tally()
            IN FULL, best first: the text lists every admitted work, so slicing it to a
            podium anywhere upstream would silently drop most of the week from the message.

            The main chat is resolved HERE, where the Telethon session is at hand, and
            carried in the flow -- so that pressing Отправить later is a single Bot API
            send with nothing to look up first.
            """
            admin_chat_id = (
                await _resolve_chat_id(telethon_client, home_chat_ref, known_chat_ids, log=log)
                if home_chat_ref
                else None
            )
            await send_vote_results_draft(
                api, user, poll, standings, admin_chat_id, vote_result_flows, log=log,
            )

        async def _deliver_vote_board(user: dict, poll, path) -> None:
            """Sends the picture the cropping page just rendered to the administrator who
            pressed "Выгрузить картинку", in their own DM.

            Their Telegram id comes from the page's verified initData, and a DM to a user
            id needs no lookup -- but it does need them to have started the bot, which any
            administrator who has ever used /vote has. A failure here is reported back to
            the page rather than swallowed: the file is on disk either way and the page
            offers a link to it, so "не отправилось" is information, not a dead end.
            """
            await api.send_document_file(
                user["id"], path,
                caption="Итоги голосования одной картинкой. Файлом, чтобы Telegram не сжимал.",
            )

        tasks = [
            _poll_loop(),
            _consume_summaries(),
            _button_counter_refresh_loop(api, home_chat_ref, log=log),
        ]
        if figurine_ack_queue is not None:
            tasks.append(_consume_figurine_acks())
        if stats_digest_queue is not None:
            tasks.append(_consume_stats_digests())
        if dismiss_queue is not None:
            tasks.append(_consume_dismissals())
        if file_block_queue is not None:
            tasks.append(_consume_file_blocks())
        if cfg.webapp_port:
            # PORT is set by the host (Railway does this automatically for any service
            # with public networking on); off when running locally without it, same as
            # every other optional piece here.
            tasks.append(
                vote_web.run_web_server(
                    cfg, home_chat_ref or "", _is_vote_admin, cfg.webapp_port,
                    announce=_announce_vote_winner, log=log, is_member=_is_vote_member,
                    export=_deliver_vote_board,
                    # The arena rides on the same server, under its own prefix. It reuses
                    # the two questions that need a Bot API client and shares nothing else
                    # -- v1 keeps its own routes, its own storage and its own rules.
                    attach=lambda app: arena_web.attach(
                        app, cfg, home_chat_ref or "", _is_vote_admin,
                        is_member=_is_vote_member, log=log,
                    ),
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
