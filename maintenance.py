"""The pause switch: stop the game changing state while an update lands.

WHY A FILE AND NOT AN ENVIRONMENT VARIABLE. The whole point of a pause is the order
pause -> deploy -> unpause. An env var is read at boot, so changing it IS a deploy, and
you would be restarting the process twice around the very restart you were trying to make
safe. This lives on the persistent volume instead: it survives a restart, it can be
flipped while the new code is booting, and it needs nobody's permission from the host.

WHAT A PAUSE IS FOR. Not "the game is broken" -- for that you fix forward. It is for the
few seconds around a restart when a request could be halfway through a multi-step write,
and for the longer window when a release changes the SHAPE of the stored data. Reads stay
open the whole time: somebody who opens the game during an update should see their
creature and a plain explanation, not an error.

WHAT IT DELIBERATELY DOES NOT DO. It does not roll anything back, it does not queue
actions to replay later, and it does not touch the farm and quarry clocks -- those are
settled from timestamps whenever they are next read, so a pause neither pays them early
nor loses them. It only refuses to START anything new.
"""

from __future__ import annotations

import json
import os

import stats
from app_time import now as app_now

MAINTENANCE_STORE_VERSION = 1

# What players are told when nothing more specific was given. Written to be read by
# somebody who was mid-game and is now confused: it says what is happening, that their
# progress is safe, and roughly how long -- in that order, because that is the order the
# questions arrive in.
DEFAULT_NOTICE = (
    "🛠 Идёт обновление игры. Бои и походы ненадолго закрыты — "
    "всё накопленное на месте, ферма и карьер продолжают идти. Загляните через несколько минут."
)


def _path():
    return stats._stats_dir() / "maintenance.json"


def _read() -> dict:
    path = _path()
    if not path.exists():
        # No file yet: the environment variable is the boot default, so a release that is
        # expected to be risky can come up already paused without a second restart.
        return {
            "paused": os.getenv("GAME_PAUSED", "").strip().lower() in {"1", "true", "yes", "on"},
            "notice": DEFAULT_NOTICE, "since": None, "by": "",
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A damaged flag must not decide the game is closed -- that would be an outage
        # caused by the thing meant to prevent one.
        return {"paused": False, "notice": DEFAULT_NOTICE, "since": None, "by": ""}
    if not isinstance(data, dict):
        return {"paused": False, "notice": DEFAULT_NOTICE, "since": None, "by": ""}
    return {
        "paused": bool(data.get("paused")),
        "notice": str(data.get("notice") or "").strip() or DEFAULT_NOTICE,
        "since": data.get("since"),
        "by": str(data.get("by") or ""),
    }


def status() -> dict:
    """Whether the game is paused, and what to tell anybody who asks."""
    return _read()


def is_paused() -> bool:
    return _read()["paused"]


def notice() -> str:
    return _read()["notice"]


def pause(notice_text: str = "", by: str = "") -> dict:
    """Close the game to state changes. Safe to call when already paused."""
    row = {
        "version": MAINTENANCE_STORE_VERSION,
        "paused": True,
        "notice": str(notice_text or "").strip() or DEFAULT_NOTICE,
        "since": app_now().isoformat(),
        "by": str(by or ""),
    }
    stats._write_json_atomic(_path(), row)
    return status()


def resume(by: str = "") -> dict:
    """Open it again. Safe to call when already open."""
    stats._write_json_atomic(_path(), {
        "version": MAINTENANCE_STORE_VERSION,
        "paused": False,
        "notice": DEFAULT_NOTICE,
        "since": None,
        "by": str(by or ""),
    })
    return status()
