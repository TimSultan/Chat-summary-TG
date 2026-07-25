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
import uuid
from datetime import datetime, timedelta, timezone

import aiohttp
from telethon import utils as tl_utils

import cabinet
import chat_profile
import economy
import history
import stats
from bot_api import TelegramBotAPI
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

BADGE_CALLBACK_PREFIX = "badge"
BADGE_FLOW_TTL_SECONDS = 10 * 60
BADGE_CREATE_BUTTON_TEXT = "➕ Создать значок"
BADGE_GIVE_BUTTON_TEXT = "🎁 Выдать значок"
BADGE_REVOKE_BUTTON_TEXT = "➖ Забрать у участника"
BADGE_DELETE_BUTTON_TEXT = "🗑 Удалить значок совсем"
WEEK_WINNER_COMMAND = "/weekwinner"
DELETE_POKRAS_COMMAND = "/deletepokras"
BADGE_ADMIN_COMMAND = "/badgeadmin"

# Explicit bot-management delegates. These users may use the DM-only management
# commands even without Telegram administrator status in the configured home chat.
# Usernames are compared case-insensitively and without a leading @.
PRIVILEGED_MANAGEMENT_USERNAMES = frozenset({"sultan_kembayev"})


SHOP_COMMANDS = ("/shop", "/buy", "/coins")

CABINET_COMMAND = "/cabinet"

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


async def _is_chat_admin_or_privileged(
    api: TelegramBotAPI, chat_id: int, user: dict | None
) -> bool:
    """Who may DELEGATE badge management. Deliberately excludes the delegates themselves:
    a badge manager can hand out badges, not hand out the right to hand out badges."""
    user = user or {}
    username = (user.get("username") or "").strip().lstrip("@").lower()
    if username in PRIVILEGED_MANAGEMENT_USERNAMES:
        return True
    user_id = user.get("id")
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
    }
    await api.send_message(
        dm_chat_id,
        "🏅 Управление значками\nРаботает только в этой личной переписке.",
        reply_to_message_id=message["message_id"],
        reply_markup={
            "inline_keyboard": [
                [{"text": BADGE_CREATE_BUTTON_TEXT, "callback_data": _badge_callback_data("create", flow_id)}],
                [{"text": BADGE_GIVE_BUTTON_TEXT, "callback_data": _badge_callback_data("list", flow_id)}],
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
    target: dict,
    reply_to_message_id: int | None,
    log=print,
) -> None:
    badge, newly_awarded = stats.give_custom_badge(
        flow["entry"],
        badge_id,
        target["user_id"],
        target["display_name"],
        flow["admin_id"],
        flow["admin_name"],
    )
    if newly_awarded:
        text = f"🎉 {target['display_name']} получает значок {badge.label}!"
    else:
        text = f"{target['display_name']} уже имеет значок {badge.label}."
    await api.send_message(
        flow["chat_id"],
        text,
        reply_to_message_id=reply_to_message_id,
        parse_mode=None,
    )
    if newly_awarded:
        await _announce_badge_in_chat(api, flow.get("admin_chat_id"), badge, target, log=log)


async def _announce_badge_in_chat(
    api: TelegramBotAPI, chat_id, badge, target: dict, log=print
) -> None:
    """Tell the group somebody was given a unique badge.

    Only on a genuinely NEW award -- give_custom_badge is idempotent, and re-running it
    must not post the same announcement again. Best-effort: the badge is already
    recorded by the time this runs, so a failed send costs the announcement, never the
    badge.

    Sent as plain text with the @username inline rather than an HTML mention: a display
    name is user-controlled and would have to be escaped, and a plain @username is what
    actually notifies the person.
    """
    if chat_id is None:
        return
    username = (target.get("username") or "").lstrip("@")
    who = f"@{username}" if username else target.get("display_name", "Участник")
    try:
        await api.send_message(
            chat_id,
            f"{who} получил уникальный значок: {badge.label}",
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

    if action == "list":
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
            "Выберите значок:",
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
                "Участник должен уже присутствовать в статистике основного чата.",
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
        target, _, _, _, _, _ = await stats.resolve_stat_target(
            telethon_client,
            flow["entry"],
            flow["entry"],
            text,
            None,
            "",
            tz,
            log=log,
        )
        if target is None:
            raise ValueError("Участник не найден в статистике. Попробуйте точный @username.")
        await _award_badge_from_flow(
            api,
            flow,
            flow["selected_badge_id"],
            {"user_id": target.user_id, "display_name": target.display_name,
             "username": target.username},
            message["message_id"],
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

    if item.code == "roast":
        end_date = datetime.now(tz).date()
        start_date = end_date - timedelta(days=cfg.roast_lookback_days - 1)
        _, messages = await fetch_range_messages_cached(
            client=telethon_client, chat_ref=chat_ref,
            start_day=start_date, end_day=end_date, tz=tz, log=log,
        )
        own = [m for m in messages if str(m.sender_id) == str(user.user_id)]
        if not own:
            raise ChatSummaryError("no messages to roast")
        own = own[-cfg.roast_max_messages :]
        return await asyncio.to_thread(
            roast_person,
            api_key=cfg.openai_api_key, model=cfg.openai_model,
            target_name=requester, lines=format_transcript_lines(own, include_date=True),
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


async def _cabinet_chat_ref(telethon_client, entry: str, known_chat_ids: dict, log=print):
    """(chat_id, username) for building t.me links into the group from a DM."""
    cached = _CABINET_CHAT_REF_CACHE.get(entry)
    if cached is not None:
        return cached
    chat_id = await _resolve_chat_id(telethon_client, entry, known_chat_ids, log=log)
    username = None
    try:
        entity = await resolve_chat(telethon_client, entry)
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

    user, rank, total, xp, streak, season_xp = await stats.resolve_stat_target(
        telethon_client, entry, entry, "",
        from_user.get("username"), _display_name(from_user), tz, log=log,
        frozen_days_for=economy.streak_freeze_lookup(entry),
    )
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
                    api, chat_id, [sent["message_id"]], STATS_DELETE_AFTER, log, background_tasks
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
    log=print,
):
    chat = message["chat"]
    chat_id = chat["id"]
    message_id = message["message_id"]
    text = _message_content(message)
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
    roast_pending: dict,
    roast_in_progress: set,
    background_tasks: set,
    home_chat_ref: str | None,
    known_chat_ids: dict[str, int],
    joke_preview_pending: dict[int, dict],
    joke_posted_queue,
    badge_flows: dict[str, dict],
    cabinet_flows: dict[str, dict],
    menu_last_sent: dict,
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
        callback_data = callback.get("data") or ""
        if callback_data.startswith(f"{cabinet.CALLBACK_PREFIX}:"):
            await handle_cabinet_callback(
                api, telethon_client, cfg, tz, callback, home_chat_ref, cabinet_flows,
                badge_flows=badge_flows, known_chat_ids=known_chat_ids, log=log,
            )
        elif callback_data.startswith(f"{BADGE_CALLBACK_PREFIX}:"):
            await handle_badge_callback(api, callback, badge_flows)
        elif callback_data.startswith(f"{JOKE_PREVIEW_CALLBACK_PREFIX}:"):
            await handle_joke_preview_callback(
                api, telethon_client, callback, joke_preview_pending, known_chat_ids,
                joke_posted_queue, log=log,
            )
        else:
            await handle_bot_roast_callback(
                api, telethon_client, cfg, tz, callback, roast_pending, roast_in_progress, background_tasks, log=log,
            )
        return

    message = update.get("message")
    if not message:
        return
    message_text = message.get("text") or message.get("caption") or ""

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

    command_text = stats.strip_command_bot_mention(message_text, bot_username)
    if re.match(r"^/start(?:\s|$)", command_text, re.IGNORECASE):
        # Where /stat's "Открыть личный кабинет" link lands (t.me/<bot>?start=cabinet),
        # and the natural first thing a new member does anyway. Groups are ignored: a
        # /start there is somebody's fat finger, not a request.
        if chat.get("type") != "private":
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
                        api, chat["id"], [sent["message_id"]], STATS_DELETE_AFTER, log, background_tasks
                    )
            except Exception:
                pass
            return
        await handle_cabinet_command(api, telethon_client, tz, message, home_chat_ref, log=log)
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

    if await handle_cabinet_text_input(
        api, telethon_client, tz, message, cabinet_flows, log=log
    ):
        return

    if await handle_badge_text_input(
        api, telethon_client, message, tz, badge_flows, log=log
    ):
        return

    # "пошути"/"пошути превью" (see JOKE_PREVIEW_* constants) only ever fires from a DM to
    # the bot, per JOKE_MANUAL_TRIGGER_KEYWORD's own docs -- checked before has_summary/
    # has_roast since it's a wholly separate trigger with its own keyword(s). The longer
    # "preview" phrase is checked first since it contains the plain trigger word too.
    if chat.get("type") == "private" and message_text:
        stripped = message_text.lower()
        preview = cfg.joke_manual_preview_keyword in stripped
        if preview or cfg.joke_manual_trigger_keyword in stripped:
            task = asyncio.create_task(
                handle_manual_joke(
                    api, telethon_client, cfg, tz, message, preview, home_chat_ref,
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
                    schedule_bot_delete(api, chat_key, [sent["message_id"]], STATS_DELETE_AFTER, log, background_tasks)
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
                            **economy.stat_extras(matched_entry, user.user_id, xp),
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
                schedule_bot_delete(api, chat_key, [sent["message_id"]], STATS_DELETE_AFTER, log, background_tasks)
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
                    schedule_bot_delete(api, chat_key, [sent["message_id"]], STATS_DELETE_AFTER, log, background_tasks)
            except Exception:
                pass
        return

    has_summary = any(k in text_lower for k in cfg.listener_trigger_keywords)
    # Roast ("прожарь меня") is turned off -- forced False rather than removing the
    # surrounding roast_pending/callback machinery below, so it stays a one-line revert
    # if it's ever turned back on.
    has_roast = False

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
        and not has_roast
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

    if not has_summary and not has_roast:
        # Nothing above wanted this message. In a DM that means the person typed
        # something the bot has no specific answer for, so show them what it CAN do
        # rather than saying nothing at all. Groups fall through silently as before.
        await maybe_send_menu(
            api, telethon_client, tz, message, home_chat_ref,
            cabinet_flows, badge_flows, menu_last_sent, log=log,
        )
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
                    "original_text": message_text,
                    "chat_ref": home_chat_ref if is_private else (chat.get("username") or chat.get("title") or str(chat_key)),
                }
        except Exception as e:
            log(f"[bot_listener] failed to send roast confirmation: {e}")
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

    `stats_digest_queue`, if given, carries (allowed_chats entry, text) pairs put there
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
    # Last time the fallback menu was sent per DM chat_id, so a burst of messages
    # gets one menu rather than one each (see MENU_FALLBACK_COOLDOWN_SECONDS).
    menu_last_sent: dict[int, float] = {}

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
        await register_bot_menu(api, log=log)
        log(
            f"[bot_listener] logged in as @{bot_username or me.get('id')}. Long-polling for "
            f"{cfg.listener_trigger_keywords} (summary; roast is off) and direct replies. FIFO queue delay: "
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
                            update, api, telethon_client, cfg, tz, bot_username, me["id"], allowed_chats,
                            summary_queue, roast_pending, roast_in_progress, background_tasks, home_chat_ref,
                            known_chat_ids, joke_preview_pending, joke_posted_queue, badge_flows,
                            cabinet_flows, menu_last_sent, log=log,
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
                entry, text = await stats_digest_queue.get()
                chat_id = await _resolve_chat_id(telethon_client, entry, known_chat_ids, log=log)
                if chat_id is None:
                    log(f"[bot_listener] dropping stats notification for '{entry}': could not resolve a chat_id for it")
                    continue
                try:
                    # parse_mode=None: the digest embeds raw display names, same reasoning
                    # as every other stats reply -- see the send_message call in the
                    # /top and /stat handling above.
                    await api.send_message(chat_id, text, parse_mode=None)
                    log(f"[bot_listener] sent stats notification to '{entry}'")
                except Exception:
                    log(f"[bot_listener] failed to send stats notification:\n{traceback.format_exc()}")

        async def _consume_dismissals():
            while True:
                chat_id, message_id = await dismiss_queue.get()
                # schedule_bot_delete already does the DISMISS_DELETE_AFTER wait via a
                # background task, so this loop isn't blocked from picking up the next
                # dismissal while one is still pending.
                schedule_bot_delete(api, chat_id, [message_id], DISMISS_DELETE_AFTER, log, background_tasks)

        tasks = [_poll_loop(), _consume_summaries()]
        if joke_queue is not None:
            tasks.append(_consume_jokes())
        if figurine_ack_queue is not None:
            tasks.append(_consume_figurine_acks())
        if stats_digest_queue is not None:
            tasks.append(_consume_stats_digests())
        if dismiss_queue is not None:
            tasks.append(_consume_dismissals())
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
