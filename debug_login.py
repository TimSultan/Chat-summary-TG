"""Diagnostic for "I never received the login code": calls Telegram's
send_code_request directly, bypassing the normal interactive login flow, so any
error (bad phone format, invalid API credentials, rate limiting, etc.) is fully
visible instead of hiding behind a prompt that just looks like it's hanging.

Run locally: python debug_login.py +15551234567   (your number, with country code)
"""

import asyncio
import sys

from telethon import TelegramClient
from telethon.sessions import StringSession

from config import load_config


async def main():
    if len(sys.argv) < 2:
        print("Usage: python debug_login.py +<countrycode><number>")
        print("Example: python debug_login.py +15551234567")
        return

    phone = sys.argv[1]
    cfg = load_config()
    print(f"Using TELEGRAM_API_ID={cfg.api_id}")

    client = TelegramClient(StringSession(), cfg.api_id, cfg.api_hash)
    await client.connect()
    print("Connected to Telegram servers OK.")
    print(f"Requesting a login code for {phone} ...")

    try:
        result = await client.send_code_request(phone)
        print("\nsend_code_request SUCCEEDED. Telegram's response:")
        print(result)
        print(
            "\nIf a code was already sent successfully, check: the Telegram app "
            "itself (Settings > Devices, or a system chat 'Telegram') on any device "
            "you're logged into, not just SMS -- Telegram usually prefers sending "
            "the code there first."
        )
    except Exception as e:
        print(f"\nsend_code_request FAILED: {type(e).__name__}: {e}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
