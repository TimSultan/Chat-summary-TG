"""If you already have a working local session file (tg_summary_session.session, from
an earlier login via main.py/gui.py), this converts it to a portable string -- no
phone number, no code, no fresh login. Use this instead of generate_session_string.py
whenever a valid local session already exists.

Run with: python convert_existing_session.py
"""

from telethon.sync import TelegramClient
from telethon.sessions import StringSession

from config import load_config


def main():
    cfg = load_config()
    with TelegramClient(cfg.session_name, cfg.api_id, cfg.api_hash) as client:
        me = client.get_me()
        if me is None:
            print(
                f"'{cfg.session_name}.session' exists but isn't authorized yet -- "
                "there's no existing login to convert. Run generate_session_string.py "
                "instead to log in fresh."
            )
            return

        print(f"Using existing session, logged in as @{me.username or me.id}.")
        session_string = StringSession.save(client.session)

    print(
        "\nAdd this to your deployment's environment variables as "
        "TELEGRAM_SESSION_STRING:\n"
    )
    print(session_string)
    print(
        "\nKeep it secret -- anyone with this string can act as your Telegram "
        "account, same as this session file."
    )


if __name__ == "__main__":
    main()
