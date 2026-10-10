"""A zip of the bot's whole voting history, to keep somewhere the server's disk is not.

What goes in: every JSON record under the voting stores -- /vote's polls (live, archived
and snapshotted), announced results and remembered contest hashtags; /vote2's tournaments;
/vote3's nominations; the Hall of Fame's contests. That is who voted for what, who took
part with which work, and how every contest ended. A year of it is a few megabytes.

What stays out: the photos. They are hundreds of megabytes a year and over Telegram's
50 MB upload limit within months -- and every one of them is a post in the chat, which the
Hall of Fame can fetch back from there (bot_listener._restore_hall_photos).

The archive's paths are DATA_DIR's own (voting/..., hall_of_fame/...), so restoring is
unzipping it into DATA_DIR. The bot sends it with "/backup", and after every closed vote
to BACKUP_CHAT_ID when one is set; a copy of each is also kept under DATA_DIR/backups.
"""

import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

# How many zips are kept on the server itself, newest first. The point of a backup is the
# copy that is NOT on this disk; these are only for "the one from last week, please".
BACKUPS_KEPT = 10
# Telegram's limit on a file a bot uploads.
TELEGRAM_UPLOAD_LIMIT = 50 * 1024 * 1024
BACKUP_PREFIX = "history-"


def build_backup(sources: dict[str, Path], destination_dir: Path) -> tuple[Path, int]:
    """Writes the zip and returns (its path, how many records are in it).

    `sources` maps the name a store has under DATA_DIR ("voting") to where it actually
    is; a store that does not exist yet is simply not in the archive. Only *.json files
    are taken -- never a half-written *.tmp, never a photo."""
    destination_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = destination_dir / f"{BACKUP_PREFIX}{stamp}.zip"
    temporary = path.with_suffix(".zip.tmp")
    count = 0
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, root in sorted(sources.items()):
            if root is None or not Path(root).is_dir():
                continue
            for record in sorted(Path(root).rglob("*.json")):
                if not record.is_file():
                    continue
                archive.write(record, f"{name}/{record.relative_to(root).as_posix()}")
                count += 1
    temporary.replace(path)
    _prune(destination_dir)
    return path, count


def _prune(destination_dir: Path) -> None:
    """Keeps the newest BACKUPS_KEPT zips this module wrote and removes the older ones --
    only files named like its own, so nothing else in the directory is ever touched."""
    ours = sorted(destination_dir.glob(f"{BACKUP_PREFIX}*.zip"), reverse=True)
    for old in ours[BACKUPS_KEPT:]:
        try:
            old.unlink()
        except OSError:
            continue


def too_big_to_send(path: Path) -> bool:
    return os.path.getsize(path) > TELEGRAM_UPLOAD_LIMIT
