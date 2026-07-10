import os
from dataclasses import dataclass

from dotenv import load_dotenv

from errors import ChatSummaryError

load_dotenv()

# Curated for this tool as of July 2026 -- fastest/cheapest first within each tier.
# gpt-4o / gpt-4o-mini are kept for anyone pinned to them, but are the older, slower tier.
RECOMMENDED_MODELS = [
    "gpt-5.4-mini",  # default: big quality jump over gpt-4o-mini, >2x faster than gpt-5-mini
    "gpt-5.5",       # flagship: latency-matched to 5.4 but noticeably smarter -- best quality
    "gpt-5.4-nano",  # fastest/cheapest -- fine for quiet chats or tight budgets
    "gpt-5",
    "gpt-5-mini",
    "gpt-5-nano",
    "gpt-4o-mini",
    "gpt-4o",
]
DEFAULT_MODEL = RECOMMENDED_MODELS[0]


@dataclass
class Config:
    api_id: int
    api_hash: str
    session_name: str
    session_string: str | None
    openai_api_key: str
    openai_model: str
    listener_allowed_chats: list[str]
    listener_trigger_keywords: list[str]
    listener_cooldown_seconds: int
    roast_trigger_keywords: list[str]
    roast_lookback_days: int
    roast_max_messages: int
    save_trigger_keyword: str
    save_channel: str | None


def build_session(cfg: "Config"):
    """A file-based session (`cfg.session_name`) needs its own writable disk and an
    interactive login on first use -- fine locally, awkward on a host like Railway.
    If TELEGRAM_SESSION_STRING is set instead (see generate_session_string.py), use a
    portable StringSession so the deployed listener can start already logged in, with
    no volume or interactive step required."""
    if cfg.session_string:
        from telethon.sessions import StringSession

        return StringSession(cfg.session_string)
    return cfg.session_name


def load_config() -> Config:
    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise ChatSummaryError(
            "Missing TELEGRAM_API_ID / TELEGRAM_API_HASH.\n"
            "Get them from https://my.telegram.org/apps and put them in a .env file "
            "(copy .env.example to .env and fill it in)."
        )

    openai_api_key = os.getenv("OPENAI_API_KEY")
    if not openai_api_key:
        raise ChatSummaryError(
            "Missing OPENAI_API_KEY.\n"
            "Put it in a .env file (copy .env.example to .env and fill it in)."
        )

    try:
        api_id_int = int(api_id)
    except ValueError:
        raise ChatSummaryError(f"TELEGRAM_API_ID must be a number, got '{api_id}'.")

    cooldown_raw = os.getenv("LISTENER_COOLDOWN_SECONDS", "180")
    try:
        cooldown_seconds = int(cooldown_raw)
    except ValueError:
        raise ChatSummaryError(f"LISTENER_COOLDOWN_SECONDS must be a number, got '{cooldown_raw}'.")
    if cooldown_seconds < 0:
        raise ChatSummaryError(f"LISTENER_COOLDOWN_SECONDS must be >= 0, got {cooldown_seconds}.")

    allowed_chats_raw = os.getenv("LISTENER_ALLOWED_CHATS", "")
    trigger_keywords_raw = os.getenv("LISTENER_TRIGGER_KEYWORDS", "/summary")
    trigger_keywords = [k.strip().lower() for k in trigger_keywords_raw.split(",") if k.strip()]
    if not trigger_keywords:
        raise ChatSummaryError("LISTENER_TRIGGER_KEYWORDS must contain at least one keyword.")

    roast_trigger_keywords_raw = os.getenv("ROAST_TRIGGER_KEYWORDS", "прожарь меня")
    roast_trigger_keywords = [k.strip().lower() for k in roast_trigger_keywords_raw.split(",") if k.strip()]
    if not roast_trigger_keywords:
        raise ChatSummaryError("ROAST_TRIGGER_KEYWORDS must contain at least one keyword.")

    roast_lookback_raw = os.getenv("ROAST_LOOKBACK_DAYS", "30")
    try:
        roast_lookback_days = int(roast_lookback_raw)
    except ValueError:
        raise ChatSummaryError(f"ROAST_LOOKBACK_DAYS must be a number, got '{roast_lookback_raw}'.")
    if roast_lookback_days < 1:
        raise ChatSummaryError(f"ROAST_LOOKBACK_DAYS must be >= 1, got {roast_lookback_days}.")

    roast_max_messages_raw = os.getenv("ROAST_MAX_MESSAGES", "400")
    try:
        roast_max_messages = int(roast_max_messages_raw)
    except ValueError:
        raise ChatSummaryError(f"ROAST_MAX_MESSAGES must be a number, got '{roast_max_messages_raw}'.")
    if roast_max_messages < 1:
        raise ChatSummaryError(f"ROAST_MAX_MESSAGES must be >= 1, got {roast_max_messages}.")

    save_trigger_keyword = os.getenv("SAVE_TRIGGER_KEYWORD", "сохрани").strip().lower()
    if not save_trigger_keyword:
        raise ChatSummaryError("SAVE_TRIGGER_KEYWORD cannot be empty.")

    save_channel = os.getenv("SAVE_CHANNEL", "papka_pokrasa").strip() or None

    return Config(
        api_id=api_id_int,
        api_hash=api_hash,
        session_name=os.getenv("TELEGRAM_SESSION", "tg_summary_session"),
        session_string=os.getenv("TELEGRAM_SESSION_STRING") or None,
        openai_api_key=openai_api_key,
        openai_model=os.getenv("OPENAI_MODEL", DEFAULT_MODEL),
        listener_allowed_chats=[c.strip() for c in allowed_chats_raw.split(",") if c.strip()],
        listener_trigger_keywords=trigger_keywords,
        listener_cooldown_seconds=cooldown_seconds,
        roast_trigger_keywords=roast_trigger_keywords,
        roast_lookback_days=roast_lookback_days,
        roast_max_messages=roast_max_messages,
        save_trigger_keyword=save_trigger_keyword,
        save_channel=save_channel,
    )
