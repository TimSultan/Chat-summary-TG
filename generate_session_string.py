"""Run this once, locally, to log in interactively and print a portable session
string. Paste the result into your deployment host's environment variables (Railway,
etc.) as TELEGRAM_SESSION_STRING -- the deployed listener will start already logged
in, with no volume for a session file and no interactive login step on the host.

Run with: python generate_session_string.py
"""

import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession

from config import load_config


async def main():
    cfg = load_config()
    async with TelegramClient(StringSession(), cfg.api_id, cfg.api_hash) as client:
        me = await client.get_me()
        print(f"Logged in as @{me.username or me.id}.")
        session_string = client.session.save()

    print(
        "\nAdd this to your deployment's environment variables as "
        "TELEGRAM_SESSION_STRING:\n"
    )
    print(session_string)
    print(
        "\nKeep it secret -- anyone with this string can act as your Telegram "
        "account, same as your phone/session file."
    )


if __name__ == "__main__":
    asyncio.run(main())
