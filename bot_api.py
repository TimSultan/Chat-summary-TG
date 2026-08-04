"""Thin async wrapper around Telegram's Bot HTTP API (https://core.telegram.org/bots/api)
-- used by bot_listener.py to run a bot account alongside the Telethon user session that
listener.py drives. Deliberately minimal: just the handful of methods bot_listener.py
needs (getMe, getUpdates via long polling, message/photo send and edit, deleteMessage,
setMessageReaction, sendChatAction, answerCallbackQuery, getChatAdministrators,
getChatMember), not a full SDK.

Confirmation flows (cabinet, badges, shop, etc.) use inline-keyboard buttons +
callback_query rather than reactions (like the Telethon listener uses for its own
confirmations): receiving *other users'* reactions via getUpdates (message_reaction
updates) requires the bot to be a chat admin, while callback_query from the bot's own
inline keyboard requires no special rights at all.
"""

import json
import mimetypes
from pathlib import Path

import aiohttp

from errors import ChatSummaryError

# Telegram's own cap on a photo caption (sendPhoto, "0-1024 characters after entities
# parsing"). A caption one character over is rejected outright, so a caller with a longer
# text sends it as a plain message instead of losing the post.
CAPTION_LIMIT = 1024


class TelegramBotAPI:
    def __init__(self, token: str, session: aiohttp.ClientSession):
        if not token or not token.strip():
            raise ChatSummaryError("Telegram bot token is missing.")
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._session = session

    async def _call(self, method: str, _http_timeout: float = 20.0, **params) -> object:
        params = {k: v for k, v in params.items() if v is not None}
        try:
            async with self._session.post(
                f"{self._base_url}/{method}", json=params, timeout=aiohttp.ClientTimeout(total=_http_timeout)
            ) as resp:
                data = await resp.json()
        except aiohttp.ClientError as e:
            raise ChatSummaryError(f"Telegram Bot API request failed ({method}): {e}") from e

        if not data.get("ok"):
            raise ChatSummaryError(
                f"Telegram Bot API {method} failed: {data.get('description', data)}"
            )
        return data["result"]

    async def _upload(self, method: str, form: aiohttp.FormData, _http_timeout: float = 60.0) -> object:
        """_call's twin for multipart requests -- same error handling, same result
        unwrapping. Separate because Telegram takes a file only as form data, never as
        JSON, and because an upload deserves a longer timeout than an API call."""
        try:
            async with self._session.post(
                f"{self._base_url}/{method}", data=form, timeout=aiohttp.ClientTimeout(total=_http_timeout)
            ) as resp:
                data = await resp.json()
        except aiohttp.ClientError as e:
            raise ChatSummaryError(f"Telegram Bot API upload failed ({method}): {e}") from e

        if not data.get("ok"):
            raise ChatSummaryError(
                f"Telegram Bot API {method} failed: {data.get('description', data)}"
            )
        return data["result"]

    async def get_me(self) -> dict:
        return await self._call("getMe")

    async def get_chat_administrators(self, chat_id) -> list[dict]:
        return await self._call("getChatAdministrators", chat_id=chat_id)

    async def get_chat_member(self, chat_id, user_id: int) -> dict:
        return await self._call("getChatMember", chat_id=chat_id, user_id=user_id)

    async def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[dict]:
        # HTTP read timeout must exceed Telegram's own long-poll `timeout` param below, or
        # every poll would spuriously time out client-side right as Telegram is about to
        # respond.
        return await self._call(
            "getUpdates",
            _http_timeout=timeout + 10,
            offset=offset,
            timeout=timeout,
            allowed_updates=["message", "callback_query"],
        )

    async def send_message(
        self,
        chat_id,
        text: str,
        reply_to_message_id: int | None = None,
        reply_markup: dict | None = None,
        parse_mode: str | None = "Markdown",
    ) -> dict:
        # parse_mode=None (plain text) is for callers echoing uncontrolled text (e.g.
        # someone's display name) straight into the message -- Telegram's legacy
        # Markdown mode rejects the WHOLE message outright if _/*/`/[ don't balance
        # (e.g. exactly one underscore in a name), which a real username can easily
        # trigger by accident. Nothing about that text is meant as formatting, so there's
        # no reason to risk the parse at all for those callers.
        params = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "link_preview_options": {"is_disabled": True},
            "reply_markup": reply_markup,
        }
        if reply_to_message_id is not None:
            params["reply_parameters"] = {"message_id": reply_to_message_id, "allow_sending_without_reply": True}
        return await self._call("sendMessage", **params)

    async def send_photo(
        self,
        chat_id,
        photo: str,
        caption: str,
        reply_to_message_id: int | None = None,
        reply_markup: dict | None = None,
        parse_mode: str | None = None,
    ) -> dict:
        """Send a Telegram photo file_id with an optional caption and inline keyboard."""
        params = {
            "chat_id": chat_id,
            "photo": photo,
            "caption": caption,
            "parse_mode": parse_mode,
            "reply_markup": reply_markup,
        }
        if reply_to_message_id is not None:
            params["reply_parameters"] = {
                "message_id": reply_to_message_id,
                "allow_sending_without_reply": True,
            }
        return await self._call("sendPhoto", **params)

    async def send_photo_file(
        self,
        chat_id,
        path,
        caption: str | None = None,
        reply_to_message_id: int | None = None,
        reply_markup: dict | None = None,
        parse_mode: str | None = None,
    ) -> dict:
        """Upload a picture from disk and post it with an optional caption.

        send_photo's counterpart for files Telegram has never seen: that one takes a
        file_id or a URL, which the stage pictures (committed alongside the code, never
        posted before) have neither of. Read into memory in one go -- these are a few
        hundred kilobytes, sent once a day.
        """
        path = Path(path)
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        if caption:
            form.add_field("caption", caption)
        if parse_mode:
            form.add_field("parse_mode", parse_mode)
        if reply_markup:
            form.add_field("reply_markup", json.dumps(reply_markup))
        if reply_to_message_id is not None:
            form.add_field("reply_parameters", json.dumps(
                {"message_id": reply_to_message_id, "allow_sending_without_reply": True}
            ))
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        form.add_field("photo", path.read_bytes(), filename=path.name, content_type=content_type)
        return await self._upload("sendPhoto", form)

    async def set_my_commands(self, commands: list[dict], scope: dict | None = None) -> None:
        """Populate the client's own ☰ Menu button next to the input field.

        This is what makes the bot usable without anybody memorising a command: Telegram
        renders the list as a tappable menu. Registered per scope, so the group list can
        stay short while the DM list carries the full set.

        Best-effort -- a failure here leaves the bot fully functional, just without the
        menu, which is never worth refusing to start over.
        """
        try:
            await self._call("setMyCommands", commands=commands, scope=scope)
        except ChatSummaryError as e:
            raise ChatSummaryError(f"setMyCommands failed: {e}") from e

    async def edit_message_text(
        self,
        chat_id,
        message_id: int,
        text: str,
        reply_markup: dict | None = None,
        parse_mode: str | None = None,
    ) -> None:
        """Replace an already-sent message in place -- how the cabinet menu navigates
        between sections without leaving a trail of dead menus in the DM.

        Telegram rejects an edit whose text AND markup both match what is already there
        ("message is not modified"). That is an expected outcome of double-tapping a
        button, not a failure, so it is swallowed like the other best-effort calls here.
        """
        try:
            await self._call(
                "editMessageText",
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=parse_mode,
                link_preview_options={"is_disabled": True},
                reply_markup=reply_markup,
            )
        except ChatSummaryError as e:
            if "not modified" not in str(e).lower():
                raise

    async def edit_message_reply_markup(
        self, chat_id, message_id: int, reply_markup: dict | None = None
    ) -> None:
        """Change only a message's buttons, leaving its text alone.

        What takes a finished round's keyboard away without having to reproduce the text
        it was attached to -- reproducing it is how an edit turns into a rewrite that
        loses whatever the message actually said. Pass {"inline_keyboard": []} to remove
        the keyboard: an omitted reply_markup means "leave it as it is".
        """
        try:
            await self._call(
                "editMessageReplyMarkup",
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=reply_markup if reply_markup is not None else {"inline_keyboard": []},
            )
        except ChatSummaryError as e:
            if "not modified" not in str(e).lower():
                raise

    async def edit_message_caption(
        self,
        chat_id,
        message_id: int,
        caption: str,
        reply_markup: dict | None = None,
        parse_mode: str | None = None,
    ) -> None:
        """Replace a photo caption while keeping its image and inline keyboard."""
        try:
            await self._call(
                "editMessageCaption",
                chat_id=chat_id,
                message_id=message_id,
                caption=caption,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
        except ChatSummaryError as e:
            if "not modified" not in str(e).lower():
                raise

    async def edit_message_media_photo(
        self,
        chat_id,
        message_id: int,
        file_id: str,
        caption: str | None = None,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
    ) -> None:
        """Swap the *picture* of an already-sent photo message, caption and all.

        This is what powers the vote carousel: stepping to the next nominee edits the one
        message in place instead of posting one photo message per nominee, so the chat
        keeps a single card the voter scrolls through rather than a wall of near-identical
        posts that would have to be cleaned up afterwards.

        `file_id` is always a file_id Telegram already knows -- never a fresh upload --
        which is what makes stepping through nominees cheap: no bytes leave this machine,
        the edit is a small JSON call whatever the picture weighs. (An upload would need
        multipart form data, i.e. _upload, not this method at all.)

        reply_markup is only sent when the caller passes one, and that omission is
        deliberate rather than a shortcut: Telegram leaves an omitted keyboard untouched,
        so plain navigation steps -- where the buttons are identical anyway -- skip it and
        the client has no reason to re-render them. caption/parse_mode are likewise left
        out of `media` when None: _call only strips None from its top-level params, so
        anything nested here has to be pruned by hand or Telegram receives explicit nulls.

        Editing to the picture that is already displayed raises "message is not modified",
        which is the expected result of double-tapping a navigation button, so it is
        swallowed exactly like the sibling edit_* methods do.
        """
        media = {"type": "photo", "media": file_id}
        if caption is not None:
            media["caption"] = caption
        if parse_mode is not None:
            media["parse_mode"] = parse_mode
        try:
            await self._call(
                "editMessageMedia",
                chat_id=chat_id,
                message_id=message_id,
                media=media,
                reply_markup=reply_markup,
            )
        except ChatSummaryError as e:
            if "not modified" not in str(e).lower():
                raise

    async def delete_message(self, chat_id, message_id: int) -> None:
        try:
            await self._call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except ChatSummaryError:
            pass  # best-effort: already deleted, too old (>48h), or lacking rights

    async def send_chat_action(self, chat_id, action: str = "typing") -> None:
        """Shows the "typing…" indicator -- and, deliberately, is the one call here that
        does NOT swallow its error. It doubles as a free reachability probe: Telegram
        forbids a bot from opening a conversation, so this fails with a 403 for anyone who
        has never pressed Start, without sending them anything if they have."""
        await self._call("sendChatAction", chat_id=chat_id, action=action)

    async def set_message_reaction(self, chat_id, message_id: int, emoji: str, log=print) -> None:
        try:
            await self._call(
                "setMessageReaction", chat_id=chat_id, message_id=message_id,
                reaction=[{"type": "emoji", "emoji": emoji}],
            )
        except ChatSummaryError as e:
            # Best-effort ack -- never worth failing the request over -- but still
            # logged: a wrong/non-standard emoji (Telegram only accepts a specific set
            # of "quick reaction" emoji, see core.telegram.org/api/reactions) fails
            # exactly like this, silently, with no other symptom at all.
            log(f"[bot_api] setMessageReaction({emoji!r}) failed for chat {chat_id}, message {message_id}: {e}")

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        try:
            await self._call("answerCallbackQuery", callback_query_id=callback_query_id, text=text)
        except ChatSummaryError:
            pass  # best-effort: just stops the client-side loading spinner
