"""Whether what the bot keeps on disk -- votes, entrants, photos, the Hall of Fame --
survives a redeploy.

Everything lives under DATA_DIR, which defaults to the working directory. On Railway
that directory is rebuilt from the image on every deploy, so without a Volume each deploy
silently starts the bot's history over. Two things here:

- use_railway_volume() points DATA_DIR at the Volume when one is attached and DATA_DIR
  was never set -- the commonest way to lose everything is to attach the Volume and
  forget the variable. It must run before any module reads DATA_DIR (they all do so at
  import), so the entrypoints call it first thing.
- persistence_warning() says, in the administrators' words, when the history is still
  on a disk the next deploy will wipe. Shown in the startup log, the /vote panel and
  /hall, so it cannot go unnoticed for a season.

Away from Railway there is nothing to tell: a local disk keeps its files.
"""

import os
from pathlib import Path

# Variables Railway sets in every service; any one of them means "this runs on Railway".
_RAILWAY_MARKERS = ("RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID")
# Set by Railway when a Volume is attached to the service: where it is mounted.
_VOLUME_PATH = "RAILWAY_VOLUME_MOUNT_PATH"


def use_railway_volume() -> str | None:
    """DATA_DIR := the attached Volume, when DATA_DIR is not set. Returns the path it
    chose, or None when it changed nothing (no Volume, or DATA_DIR already set -- an
    explicit setting always wins)."""
    volume = (os.getenv(_VOLUME_PATH) or "").strip()
    if volume and not (os.getenv("DATA_DIR") or "").strip():
        os.environ["DATA_DIR"] = volume
        return volume
    return None


def data_dir() -> Path:
    return Path((os.getenv("DATA_DIR") or "").strip() or ".").resolve()


def persistence_warning() -> str | None:
    """Why the history may not survive the next deploy, or None when it will."""
    if not any(os.getenv(name) for name in _RAILWAY_MARKERS):
        return None
    volume = (os.getenv(_VOLUME_PATH) or "").strip()
    if not volume:
        return (
            "⚠️ История голосований, участники, фото и Доска почёта лежат не на постоянном "
            "диске: к сервису на Railway не подключён Volume. Следующий деплой всё это сотрёт. "
            "Подключите Volume (сервис → Settings → Volumes) — бот сам начнёт хранить данные на нём."
        )
    mount, current = Path(volume).resolve(), data_dir()
    if current == mount or mount in current.parents:
        return None
    return (
        f"⚠️ DATA_DIR ({current}) не на Volume ({mount}): история голосований, участники, фото "
        "и Доска почёта пропадут при следующем деплое. Уберите DATA_DIR или укажите путь "
        "внутри Volume."
    )
