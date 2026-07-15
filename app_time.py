"""Application-wide timezone helpers.

Calendar days, displayed timestamps, and newly written cache metadata use the configured
IANA timezone.  Keeping this independent of the host prevents a deployment in London
from silently changing the meaning of "today".
"""

import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from errors import ChatSummaryError

load_dotenv()


DEFAULT_TIMEZONE_NAME = "Europe/Moscow"


def timezone_name() -> str:
    return os.getenv("APP_TIMEZONE", DEFAULT_TIMEZONE_NAME).strip() or DEFAULT_TIMEZONE_NAME


def resolve_timezone(name: str | None = None) -> ZoneInfo:
    requested = name.strip() if name and name.strip() else timezone_name()
    try:
        return ZoneInfo(requested)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ChatSummaryError(f"Unknown timezone '{requested}': {e}") from e


def now(tz=None) -> datetime:
    return datetime.now(tz or resolve_timezone())


def cache_namespace(tz=None) -> str:
    """A filesystem-safe namespace so caches made under different day boundaries
    can coexist without overwriting or mixing with one another."""
    zone = tz or resolve_timezone()
    label = getattr(zone, "key", None) or str(zone)
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("_") or "timezone"
