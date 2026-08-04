import os
from dataclasses import dataclass

from dotenv import load_dotenv

from errors import ChatSummaryError

load_dotenv()

# The one and only way to ask for a summary. Hardcoded rather than configured: it is the
# bot's own slash-command, registered with BotFather and named in the menu.
#
# It used to come from LISTENER_TRIGGER_KEYWORDS, which was a knob nobody wanted and one
# that could take the command away -- production had it set to "sum", which meant every
# message merely OPENING with those three letters bought an OpenAI call, while "/summary"
# itself did nothing. The variable is gone; there is nothing to misconfigure.
SUMMARY_COMMAND = "/summary"

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
    openai_routing_model: str
    listener_allowed_chats: list[str]
    summary_queue_delay_seconds: int
    webapp_public_url: str | None
    webapp_port: int
    vote_announce_extra_chat: str | None
    vote_miniapp_short_name: str | None
    save_trigger_keyword: str
    save_channel: str | None
    summary_pipeline_version: str
    telegram_bot_token: str | None
    stats_enabled: bool
    stats_top_limit: int
    stats_catchup_days: int


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

    queue_delay_raw = os.getenv("SUMMARY_QUEUE_DELAY_SECONDS", "20")
    try:
        summary_queue_delay_seconds = int(queue_delay_raw)
    except ValueError:
        raise ChatSummaryError(f"SUMMARY_QUEUE_DELAY_SECONDS must be a number, got '{queue_delay_raw}'.")
    if summary_queue_delay_seconds < 0:
        raise ChatSummaryError(
            f"SUMMARY_QUEUE_DELAY_SECONDS must be >= 0, got {summary_queue_delay_seconds}."
        )

    allowed_chats_raw = os.getenv("LISTENER_ALLOWED_CHATS", "")

    # Public https:// origin this app is reachable at, used to build the Mini App link
    # for /vote. Telegram will not open a Mini App over plain http or from an IP, so
    # without a real domain here the voting button is simply not offered. PORT is set by
    # the host (Railway does it automatically); the web server is off when unset locally.
    webapp_public_url = (os.getenv("WEBAPP_PUBLIC_URL", "") or "").strip().rstrip("/") or None
    if webapp_public_url and not webapp_public_url.startswith("https://"):
        raise ChatSummaryError(
            f"WEBAPP_PUBLIC_URL must start with https:// (Telegram refuses anything else), "
            f"got '{webapp_public_url}'."
        )

    webapp_port_raw = os.getenv("PORT", "0")
    try:
        webapp_port = int(webapp_port_raw)
    except ValueError:
        raise ChatSummaryError(f"PORT must be a number, got '{webapp_port_raw}'.")
    if webapp_port < 0 or webapp_port > 65535:
        raise ChatSummaryError(f"PORT must be between 0 and 65535, got {webapp_port}.")

    # The second group "/vote chat" may post its announcement into, next to the tracked
    # chat itself. Telegram's sendMessage takes an "@username" straight as the chat id, so
    # this needs neither a numeric id nor a Telethon resolve -- the bot only has to be a
    # member there. An @ is added when it's missing because the bare username is the one
    # spelling sendMessage rejects, and it's the spelling people naturally type. Blank
    # removes the choice altogether: the announcement then only offers the main chat,
    # rather than offering a destination that could never work.
    vote_announce_extra_chat = os.getenv("VOTE_ANNOUNCE_EXTRA_CHAT", "@papkahudojnicov").strip() or None
    if vote_announce_extra_chat and not vote_announce_extra_chat.startswith("@") \
            and not vote_announce_extra_chat.lstrip("-").isdigit():
        vote_announce_extra_chat = f"@{vote_announce_extra_chat}"

    # BotFather's Direct Link Mini App short name (/newapp), which lets the vote button in
    # a GROUP be "https://t.me/<bot>/<short name>?startapp=vote" and open the Mini App
    # right there. A web_app button is private-chat only -- Telegram rejects one posted to
    # a group -- so without this short name the group button can only be the ?start=vote
    # deep link into the DM, which works but costs the voter an extra tap.
    vote_miniapp_short_name = os.getenv("VOTE_MINIAPP_SHORT_NAME", "").strip().strip("/") or None

    save_trigger_keyword = os.getenv("SAVE_TRIGGER_KEYWORD", "сохрани").strip().lower()
    if not save_trigger_keyword:
        raise ChatSummaryError("SAVE_TRIGGER_KEYWORD cannot be empty.")

    save_channel = os.getenv("SAVE_CHANNEL", "papka_pokrasa").strip() or None

    summary_pipeline_version = os.getenv("SUMMARY_PIPELINE_VERSION", "v2").strip().lower()
    if summary_pipeline_version not in ("v1", "v2"):
        raise ChatSummaryError(
            f"SUMMARY_PIPELINE_VERSION must be 'v1' or 'v2', got '{summary_pipeline_version}'."
        )

    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or None

    stats_enabled = os.getenv("STATS_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")

    stats_top_limit_raw = os.getenv("STATS_TOP_LIMIT", "10")
    try:
        stats_top_limit = int(stats_top_limit_raw)
    except ValueError:
        raise ChatSummaryError(f"STATS_TOP_LIMIT must be a number, got '{stats_top_limit_raw}'.")
    if stats_top_limit < 1:
        raise ChatSummaryError(f"STATS_TOP_LIMIT must be >= 1, got {stats_top_limit}.")

    stats_catchup_days_raw = os.getenv("STATS_CATCHUP_DAYS", "7")
    try:
        stats_catchup_days = int(stats_catchup_days_raw)
    except ValueError:
        raise ChatSummaryError(f"STATS_CATCHUP_DAYS must be a number, got '{stats_catchup_days_raw}'.")
    if stats_catchup_days < 1:
        raise ChatSummaryError(f"STATS_CATCHUP_DAYS must be >= 1, got {stats_catchup_days}.")

    return Config(
        api_id=api_id_int,
        api_hash=api_hash,
        session_name=os.getenv("TELEGRAM_SESSION", "tg_summary_session"),
        session_string=os.getenv("TELEGRAM_SESSION_STRING") or None,
        openai_api_key=openai_api_key,
        openai_model=os.getenv("OPENAI_MODEL", DEFAULT_MODEL),
        # Structured JSON routing/classification calls (intent parsing, name matching)
        # need little reasoning and have a tiny input, so they default to the cheapest
        # tier instead of paying for the same model used for full-transcript generation.
        openai_routing_model=os.getenv("OPENAI_ROUTING_MODEL", "gpt-5.4-nano"),
        listener_allowed_chats=[c.strip() for c in allowed_chats_raw.split(",") if c.strip()],
        summary_queue_delay_seconds=summary_queue_delay_seconds,
        webapp_public_url=webapp_public_url,
        webapp_port=webapp_port,
        vote_announce_extra_chat=vote_announce_extra_chat,
        vote_miniapp_short_name=vote_miniapp_short_name,
        save_trigger_keyword=save_trigger_keyword,
        save_channel=save_channel,
        summary_pipeline_version=summary_pipeline_version,
        telegram_bot_token=telegram_bot_token,
        stats_enabled=stats_enabled,
        stats_top_limit=stats_top_limit,
        stats_catchup_days=stats_catchup_days,
    )
