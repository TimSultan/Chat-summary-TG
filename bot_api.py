"""Thin async wrapper around Telegram's Bot HTTP API (https://core.telegram.org/bots/api)
-- used by bot_listener.py to run a bot account alongside the Telethon user session that
listener.py drives. Deliberately minimal: just the handful of methods bot_listener.py
needs (getMe, getUpdates via long polling, sendMessage, deleteMessage,
setMessageReaction), not a full SDK.
"""

import aiohttp

from errors import ChatSummaryError


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

    async def get_me(self) -> dict:
        return await self._call("getMe")

    async def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[dict]:
        # HTTP read timeout must exceed Telegram's own long-poll `timeout` param below, or
        # every poll would spuriously time out client-side right as Telegram is about to
        # respond.
        return await self._call(
            "getUpdates",
            _http_timeout=timeout + 10,
            offset=offset,
            timeout=timeout,
            allowed_updates=["message"],
        )

    async def send_message(self, chat_id, text: str, reply_to_message_id: int | None = None) -> dict:
        params = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "link_preview_options": {"is_disabled": True},
        }
        if reply_to_message_id is not None:
            params["reply_parameters"] = {"message_id": reply_to_message_id, "allow_sending_without_reply": True}
        return await self._call("sendMessage", **params)

    async def delete_message(self, chat_id, message_id: int) -> None:
        try:
            await self._call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except ChatSummaryError:
            pass  # best-effort: already deleted, too old (>48h), or lacking rights

    async def set_message_reaction(self, chat_id, message_id: int, emoji: str) -> None:
        try:
            await self._call(
                "setMessageReaction", chat_id=chat_id, message_id=message_id,
                reaction=[{"type": "emoji", "emoji": emoji}],
            )
        except ChatSummaryError:
            pass  # best-effort ack -- never worth failing the request over
