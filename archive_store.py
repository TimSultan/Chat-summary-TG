"""Taking a finished record out of the live set WITHOUT destroying it.

Every "очистить" in the bot -- /vote's, /vote2's, /vote3's -- used to move the record's
JSON into an archive directory and DELETE its photos, and the moderation screens' clear
buttons deleted both. The photos were the one thing that could not come back, and the
history of who took part with what is exactly what the Hall of Fame is built from. So
nothing is deleted any more: the record and its photo directory move into the archive
together, under one name, where everything that reads the live contest cannot see them.

Shared by voting.py, arena.py and nominations.py, which otherwise share nothing -- this
module knows only about files, never about polls.
"""

import shutil
from datetime import datetime, timezone
from pathlib import Path

# Photos of an archived record live here, in a directory named like the record's file.
MEDIA_SUBDIR = "media"
# Copies of a record taken before something rewrote it (see snapshot).
SNAPSHOT_SUBDIR = "snapshots"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def _free_stem(directory: Path, stem: str, suffix: str) -> str:
    """`stem`, or `stem` with a time stamp (and a counter, inside one second) when that
    name -- as a file or as a photo directory -- is already in the archive. Archiving the
    same week twice keeps both rather than letting the second overwrite the first."""
    def taken(candidate: str) -> bool:
        return (directory / f"{candidate}{suffix}").exists() or (directory / MEDIA_SUBDIR / candidate).exists()

    if not taken(stem):
        return stem
    base = f"{stem}_{_stamp()}"
    candidate, counter = base, 1
    while taken(candidate):
        counter += 1
        candidate = f"{base}{counter}"
    return candidate


def _move(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        source.replace(destination)
    except OSError:
        shutil.move(str(source), str(destination))  # another filesystem: copy, then remove


def move_to_archive(record: Path, media: Path | None, archive_dir: Path) -> Path | None:
    """Moves `record` into `archive_dir` and its photo directory `media` into
    `archive_dir/media/<the same name>`. Returns where the record went, or None when there
    was no record (its photos, if any, are archived all the same under its name)."""
    if not record.exists() and not (media is not None and media.exists()):
        return None
    stem = _free_stem(archive_dir, record.stem, record.suffix)
    destination = archive_dir / f"{stem}{record.suffix}"
    if record.exists():
        _move(record, destination)
    if media is not None and media.exists():
        _move(media, archive_dir / MEDIA_SUBDIR / stem)
    return destination if destination.exists() else None


def archived_media_dirs(archive_dir: Path, stem: str) -> list[Path]:
    """The photo directories archived for a record named `stem`, newest first: the exact
    name, then every time-stamped one (a record archived more than once)."""
    root = archive_dir / MEDIA_SUBDIR
    if not root.exists():
        return []
    stamped = sorted((path for path in root.glob(f"{stem}_*") if path.is_dir()), reverse=True)
    exact = root / stem
    return ([exact] if exact.is_dir() else []) + stamped


def snapshot(record: Path, archive_dir: Path) -> Path | None:
    """A copy of `record` as it is now, under archive_dir/snapshots/, before something
    rewrites it in a way that drops part of it. In a subdirectory of its own, so nothing
    that reads the archive mistakes a snapshot for a second cleared record."""
    if not record.exists():
        return None
    directory = archive_dir / SNAPSHOT_SUBDIR
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{record.stem}_{_stamp()}{record.suffix}"
    counter = 1
    while destination.exists():
        counter += 1
        destination = directory / f"{record.stem}_{_stamp()}{counter}{record.suffix}"
    shutil.copy2(record, destination)
    return destination
