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
    summary_queue_delay_seconds: int
    roast_trigger_keywords: list[str]
    roast_lookback_days: int
    roast_max_messages: int
    save_trigger_keyword: str
    save_channel: str | None
    summary_pipeline_version: str
    telegram_bot_token: str | None
    joke_enabled: bool
    joke_activity_min_messages: int
    joke_fire_probability: float
    joke_cooldown_min_seconds: int
    joke_cooldown_max_seconds: int
    joke_reaction_threshold: int
    joke_reaction_cooldown_seconds: int
    joke_manual_trigger_keyword: str
    joke_manual_preview_keyword: str
    joke_profile_lookback_days: int
    joke_profile_ttl_seconds: int
    joke_profile_max_messages: int
    followup_enabled: bool
    followup_window_messages: int
    followup_check_every_messages: int
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

    summary_pipeline_version = os.getenv("SUMMARY_PIPELINE_VERSION", "v2").strip().lower()
    if summary_pipeline_version not in ("v1", "v2"):
        raise ChatSummaryError(
            f"SUMMARY_PIPELINE_VERSION must be 'v1' or 'v2', got '{summary_pipeline_version}'."
        )

    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or None

    joke_enabled = os.getenv("JOKE_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")

    joke_min_messages_raw = os.getenv("JOKE_ACTIVITY_MIN_MESSAGES", "20")
    try:
        joke_activity_min_messages = int(joke_min_messages_raw)
    except ValueError:
        raise ChatSummaryError(f"JOKE_ACTIVITY_MIN_MESSAGES must be a number, got '{joke_min_messages_raw}'.")
    if joke_activity_min_messages < 1:
        raise ChatSummaryError(f"JOKE_ACTIVITY_MIN_MESSAGES must be >= 1, got {joke_activity_min_messages}.")

    joke_probability_raw = os.getenv("JOKE_FIRE_PROBABILITY", "0.35")
    try:
        joke_fire_probability = float(joke_probability_raw)
    except ValueError:
        raise ChatSummaryError(f"JOKE_FIRE_PROBABILITY must be a number, got '{joke_probability_raw}'.")
    if not (0.0 <= joke_fire_probability <= 1.0):
        raise ChatSummaryError(f"JOKE_FIRE_PROBABILITY must be between 0 and 1, got {joke_fire_probability}.")

    joke_cooldown_min_raw = os.getenv("JOKE_COOLDOWN_MIN_SECONDS", "1800")
    try:
        joke_cooldown_min_seconds = int(joke_cooldown_min_raw)
    except ValueError:
        raise ChatSummaryError(f"JOKE_COOLDOWN_MIN_SECONDS must be a number, got '{joke_cooldown_min_raw}'.")
    if joke_cooldown_min_seconds < 0:
        raise ChatSummaryError(f"JOKE_COOLDOWN_MIN_SECONDS must be >= 0, got {joke_cooldown_min_seconds}.")

    joke_cooldown_max_raw = os.getenv("JOKE_COOLDOWN_MAX_SECONDS", "3600")
    try:
        joke_cooldown_max_seconds = int(joke_cooldown_max_raw)
    except ValueError:
        raise ChatSummaryError(f"JOKE_COOLDOWN_MAX_SECONDS must be a number, got '{joke_cooldown_max_raw}'.")
    if joke_cooldown_max_seconds < joke_cooldown_min_seconds:
        raise ChatSummaryError(
            f"JOKE_COOLDOWN_MAX_SECONDS ({joke_cooldown_max_seconds}) must be >= "
            f"JOKE_COOLDOWN_MIN_SECONDS ({joke_cooldown_min_seconds})."
        )

    joke_reaction_threshold_raw = os.getenv("JOKE_REACTION_THRESHOLD", "3")
    try:
        joke_reaction_threshold = int(joke_reaction_threshold_raw)
    except ValueError:
        raise ChatSummaryError(f"JOKE_REACTION_THRESHOLD must be a number, got '{joke_reaction_threshold_raw}'.")
    if joke_reaction_threshold < 1:
        raise ChatSummaryError(f"JOKE_REACTION_THRESHOLD must be >= 1, got {joke_reaction_threshold}.")

    joke_reaction_cooldown_raw = os.getenv("JOKE_REACTION_COOLDOWN_SECONDS", "1800")
    try:
        joke_reaction_cooldown_seconds = int(joke_reaction_cooldown_raw)
    except ValueError:
        raise ChatSummaryError(
            f"JOKE_REACTION_COOLDOWN_SECONDS must be a number, got '{joke_reaction_cooldown_raw}'."
        )
    if joke_reaction_cooldown_seconds < 0:
        raise ChatSummaryError(
            f"JOKE_REACTION_COOLDOWN_SECONDS must be >= 0, got {joke_reaction_cooldown_seconds}."
        )

    joke_manual_trigger_keyword = os.getenv("JOKE_MANUAL_TRIGGER_KEYWORD", "пошути").strip().lower()
    if not joke_manual_trigger_keyword:
        raise ChatSummaryError("JOKE_MANUAL_TRIGGER_KEYWORD cannot be empty.")

    joke_manual_preview_keyword = os.getenv("JOKE_MANUAL_PREVIEW_KEYWORD", "пошути превью").strip().lower()
    if not joke_manual_preview_keyword:
        raise ChatSummaryError("JOKE_MANUAL_PREVIEW_KEYWORD cannot be empty.")

    joke_profile_lookback_raw = os.getenv("JOKE_PROFILE_LOOKBACK_DAYS", "3")
    try:
        joke_profile_lookback_days = int(joke_profile_lookback_raw)
    except ValueError:
        raise ChatSummaryError(f"JOKE_PROFILE_LOOKBACK_DAYS must be a number, got '{joke_profile_lookback_raw}'.")
    if joke_profile_lookback_days < 1:
        raise ChatSummaryError(f"JOKE_PROFILE_LOOKBACK_DAYS must be >= 1, got {joke_profile_lookback_days}.")

    joke_profile_ttl_raw = os.getenv("JOKE_PROFILE_TTL_SECONDS", "86400")
    try:
        joke_profile_ttl_seconds = int(joke_profile_ttl_raw)
    except ValueError:
        raise ChatSummaryError(f"JOKE_PROFILE_TTL_SECONDS must be a number, got '{joke_profile_ttl_raw}'.")
    if joke_profile_ttl_seconds < 1:
        raise ChatSummaryError(f"JOKE_PROFILE_TTL_SECONDS must be >= 1, got {joke_profile_ttl_seconds}.")

    joke_profile_max_raw = os.getenv("JOKE_PROFILE_MAX_MESSAGES", "1000")
    try:
        joke_profile_max_messages = int(joke_profile_max_raw)
    except ValueError:
        raise ChatSummaryError(f"JOKE_PROFILE_MAX_MESSAGES must be a number, got '{joke_profile_max_raw}'.")
    if joke_profile_max_messages < 1:
        raise ChatSummaryError(f"JOKE_PROFILE_MAX_MESSAGES must be >= 1, got {joke_profile_max_messages}.")

    followup_enabled = os.getenv("FOLLOWUP_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")

    followup_window_raw = os.getenv("FOLLOWUP_WINDOW_MESSAGES", "15")
    try:
        followup_window_messages = int(followup_window_raw)
    except ValueError:
        raise ChatSummaryError(f"FOLLOWUP_WINDOW_MESSAGES must be a number, got '{followup_window_raw}'.")
    if followup_window_messages < 1:
        raise ChatSummaryError(f"FOLLOWUP_WINDOW_MESSAGES must be >= 1, got {followup_window_messages}.")

    followup_check_every_raw = os.getenv("FOLLOWUP_CHECK_EVERY_MESSAGES", "5")
    try:
        followup_check_every_messages = int(followup_check_every_raw)
    except ValueError:
        raise ChatSummaryError(f"FOLLOWUP_CHECK_EVERY_MESSAGES must be a number, got '{followup_check_every_raw}'.")
    if followup_check_every_messages < 1:
        raise ChatSummaryError(f"FOLLOWUP_CHECK_EVERY_MESSAGES must be >= 1, got {followup_check_every_messages}.")

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
        listener_allowed_chats=[c.strip() for c in allowed_chats_raw.split(",") if c.strip()],
        listener_trigger_keywords=trigger_keywords,
        summary_queue_delay_seconds=summary_queue_delay_seconds,
        roast_trigger_keywords=roast_trigger_keywords,
        roast_lookback_days=roast_lookback_days,
        roast_max_messages=roast_max_messages,
        save_trigger_keyword=save_trigger_keyword,
        save_channel=save_channel,
        summary_pipeline_version=summary_pipeline_version,
        telegram_bot_token=telegram_bot_token,
        joke_enabled=joke_enabled,
        joke_activity_min_messages=joke_activity_min_messages,
        joke_fire_probability=joke_fire_probability,
        joke_cooldown_min_seconds=joke_cooldown_min_seconds,
        joke_cooldown_max_seconds=joke_cooldown_max_seconds,
        joke_reaction_threshold=joke_reaction_threshold,
        joke_reaction_cooldown_seconds=joke_reaction_cooldown_seconds,
        joke_manual_trigger_keyword=joke_manual_trigger_keyword,
        joke_manual_preview_keyword=joke_manual_preview_keyword,
        joke_profile_lookback_days=joke_profile_lookback_days,
        joke_profile_ttl_seconds=joke_profile_ttl_seconds,
        joke_profile_max_messages=joke_profile_max_messages,
        followup_enabled=followup_enabled,
        followup_window_messages=followup_window_messages,
        followup_check_every_messages=followup_check_every_messages,
        stats_enabled=stats_enabled,
        stats_top_limit=stats_top_limit,
        stats_catchup_days=stats_catchup_days,
    )
