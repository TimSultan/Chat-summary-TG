"""Mirrors /vote and the Hall of Fame to the Stranger Hands website -- strangerhands.com
/competition (the vote on now), /competition/hall and /competition/artists.

The bot stays the one place a contest is run. Once a minute this reads what the voting page
shows (voting.page_polls: the admitted works only, no ballots, no counts -- exactly what
vote_web's public browser page shows) and the Hall of Fame (hall_of_fame.snapshot), and when
either has changed since the last push, sends it to the site:

1. POST /api/competition/photos {hashes}: which pictures the site does not have yet.
2. POST /api/competition/photos {hash, data}: each missing one, base64, one per request. The
   site keeps it in its own public bucket under its SHA-256, so a photo shared by a vote, the
   Hall of Fame and an avatar is uploaded once, and nothing is ever sent twice.
3. POST /api/competition/hall and/or /api/competition/live: the records, naming pictures by
   hash. The site translates the names, titles and captions into English itself.

Nothing is pushed when nothing changed: the payloads are hashed and compared with the last
push that went through, so an idle week costs a directory listing a minute. A vote opening,
a work admitted, a vote closing or a week cleared reaches the site within the minute;
recording a closed vote also wakes the loop at once (wake()).

PRIVACY: no Telegram user id leaves the bot. A painter is named on the site by a salted hash
of the hall's own author key (artist_id), which keeps their page's identity stable across
renames without saying who they are on Telegram. Names, @tags and avatars are what the
chat and the hall already show publicly. No ballot is sent, and no vote count of a vote that
is still open.

Configured by STRANGERHANDS_MIRROR_URL (the site's origin) and STRANGERHANDS_MIRROR_TOKEN (the
site's COMPETITION_SYNC_TOKEN). Either unset, and the mirror is off.

Everything that touches the disk runs in a worker thread; the HTTP calls are aiohttp on the
bot's event loop, one at a time.
"""

import asyncio
import base64
import hashlib
import json
import os
import time
import traceback
from pathlib import Path

import hall_of_fame
import voting

# A painter's id on the site: this salt and the hall's author key, hashed. Changing the salt
# would give every painter a new identity there, and a new page address with it.
ARTIST_SALT = "strangerhands-competition:"
INTERVAL_SECONDS = 60
# After a failed push the next try waits longer, up to this, so a site that is down is not
# hammered once a minute.
MAX_BACKOFF_SECONDS = 15 * 60
# Avatars change rarely and each one is a Bot API round trip: once a day per painter, a few
# per cycle so a first run with a hundred painters does not hold up the push.
AVATAR_REFRESH_SECONDS = 24 * 60 * 60
AVATAR_RETRY_SECONDS = 60 * 60
AVATARS_PER_CYCLE = 40
# The site refuses a picture past 20 MB; a phone photo the bot downloaded is a few MB.
MAX_PHOTO_BYTES = 20 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 120


def mirror_settings() -> tuple[str | None, str | None]:
    url = (os.getenv("STRANGERHANDS_MIRROR_URL", "") or "").strip().rstrip("/") or None
    token = (os.getenv("STRANGERHANDS_MIRROR_TOKEN", "") or "").strip() or None
    return url, token


def artist_id(author_id, author_name: str) -> str:
    """The painter's id on the site: twenty hex characters, never the Telegram id."""
    key = hall_of_fame.author_key(author_id, author_name)
    return hashlib.sha256((ARTIST_SALT + key).encode("utf-8")).hexdigest()[:20]


def _kind(hashtag: str) -> str:
    if voting.is_weekly_hashtag(hashtag):
        return "weekly"
    return "monthly" if hashtag == voting.MONTHLY_HASHTAG else "theme"


def _post_url(link_base: str | None, message_id) -> str | None:
    try:
        message = int(message_id or 0)
    except (TypeError, ValueError):
        message = 0
    return f"{link_base}/{message}" if link_base and message else None


class FileHashes:
    """SHA-256 of each picture, remembered against its size and modification time: a
    collected photo is written once and never rewritten, so a minute's check re-reads
    nothing it has seen."""

    def __init__(self):
        self._known: dict[str, tuple[int, int, str]] = {}

    def digest(self, path: Path) -> str | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        if not path.is_file() or stat.st_size == 0 or stat.st_size > MAX_PHOTO_BYTES:
            return None
        cached = self._known.get(str(path))
        if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            return cached[2]
        sha = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    sha.update(block)
        except OSError:
            return None
        digest = sha.hexdigest()
        self._known[str(path)] = (stat.st_mtime_ns, stat.st_size, digest)
        return digest


def _photos(directory: Path, names, hashes: FileHashes, files: dict) -> list[str]:
    """The digests of `names` under `directory` that exist, recording where each one is."""
    digests = []
    for name in names or []:
        if not name or "/" in str(name) or "\\" in str(name):
            continue
        path = directory / str(name)
        digest = hashes.digest(path)
        if digest:
            files.setdefault(digest, path)
            digests.append(digest)
    return digests


def build_live(entry: str, hashes: FileHashes, link_base: str | None = None):
    """What the voting page shows now, as the site takes it. Returns (payload, files, people):
    files maps a photo digest to its path, people maps an artist id to (author id, name,
    @tag) for their avatar and the artist list."""
    files: dict[str, Path] = {}
    people: dict[str, tuple] = {}
    polls = []
    for poll in voting.page_polls(entry):
        directory = voting.media_path(entry, poll.poll_id)
        admitted = poll.approved_entries()
        entries = []
        for work in admitted:
            who = artist_id(work.author_id, work.author_name)
            people.setdefault(who, (work.author_id, work.author_name, work.author_username))
            entries.append({
                "entry_id": str(work.entry_id),
                "artist": who,
                "name": work.author_name or "Unknown",
                "username": work.author_username,
                "text": work.text or "",
                "posted_at": work.posted_at or None,
                "post_url": _post_url(link_base, work.message_id),
                "photos": _photos(directory, work.media, hashes, files),
                "crop": poll.crops.get(work.entry_id),
            })
        round_row = poll.round
        winner = poll.winner_entry_id if not poll.open and any(w.entry_id == poll.winner_entry_id for w in admitted) else None
        polls.append({
            "id": poll.poll_id,
            "kind": _kind(poll.hashtag),
            "hashtag": poll.hashtag,
            "title": poll.label(),
            "badge": poll.badge,
            "tier": poll.tier,
            "round": {"key": round_row[0], "medal": round_row[1]} if round_row else None,
            "open": bool(poll.open),
            "max_choices": poll.max_choices,
            "allow_revote": bool(poll.allow_revote),
            "voters": len(poll.votes),
            "created_at": poll.created_at or None,
            "entries": entries,
            "winner": winner,
        })
    return {"polls": polls}, files, people


def build_hall(entry: str, hashes: FileHashes, link_base: str | None = None):
    """Every contest in the Hall of Fame, with its places, votes and medals as the hall
    decides them (Contest.medal_for). Same return shape as build_live."""
    files: dict[str, Path] = {}
    people: dict[str, tuple] = {}
    hall = hall_of_fame.snapshot(entry)
    contests = []
    for contest in hall.contests:
        directory = hall_of_fame.media_dir(entry, contest.contest_id)
        works = []
        for work in contest.works:
            thumb = _photos(directory, [work.thumb] if work.thumb else [], hashes, files)
            works.append({
                "entry_id": str(work.entry_id),
                "place": int(work.place),
                "votes": int(work.votes),
                "medal": contest.medal_for(work),
                "artist": artist_id(work.author_id, work.author_name),
                "name": work.author_name or "Unknown",
                "username": work.author_username,
                "text": work.text or "",
                "posted_at": work.posted_at or None,
                "post_url": _post_url(link_base, work.message_id),
                "photos": _photos(directory, work.photos, hashes, files),
                "thumb": thumb[0] if thumb else None,
            })
        contests.append({
            "id": contest.contest_id,
            "kind": _kind(contest.hashtag),
            "hashtag": contest.hashtag,
            "title": contest.label(),
            "badge": contest.badge,
            "tier": contest.tier,
            "voters": int(contest.voters),
            "created_at": contest.created_at or None,
            "closed_at": contest.closed_at or None,
            "works": works,
        })
    for artist in hall.artists:
        people[artist_id(artist.author_id, artist.name)] = (artist.author_id, artist.name, artist.username)
    return {"contests": contests}, files, people


def signature(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


class MirrorError(Exception):
    pass


class Mirror:
    """The loop that keeps the site in step. `avatar` takes a Telegram user id and returns
    photo bytes or None (the ballot's own Bot API callable); `chat` returns (chat @username,
    marked chat id) for links to the original posts. Either may be left out."""

    def __init__(self, entry: str, base_url: str, token: str, avatar=None, chat=None, log=print,
                 interval: float = INTERVAL_SECONDS):
        self.entry = entry
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.avatar = avatar
        self.chat = chat
        self.log = log
        self.interval = interval
        self.hashes = FileHashes()
        self.pushed: dict[str, str] = {}
        # author id -> (next check, digest or None, bytes or None)
        self.avatars: dict[int, tuple[float, str | None, bytes | None]] = {}
        self._link_base: str | None = None
        self._link_resolved = False
        self._wake = asyncio.Event()
        self._session = None

    def wake(self) -> None:
        """Push now rather than at the next minute: a vote was just closed or changed."""
        self._wake.set()

    # ---------------------------------------------------------------- HTTP

    async def _post(self, path: str, payload: dict) -> dict:
        import aiohttp

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS))
        async with self._session.post(
            f"{self.base_url}{path}", json=payload,
            headers={"Authorization": f"Bearer {self.token}", "User-Agent": "chat-summary-tg site mirror"},
        ) as response:
            text = await response.text()
            if response.status >= 400:
                raise MirrorError(f"{path} answered {response.status}: {text[:300]}")
            try:
                return json.loads(text) if text else {}
            except json.JSONDecodeError:
                raise MirrorError(f"{path} did not answer with JSON: {text[:200]}")

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    # ---------------------------------------------------------------- pieces

    async def _link(self) -> str | None:
        if self._link_resolved or self.chat is None:
            return self._link_base
        try:
            username, chat_id = await self.chat()
        except Exception as e:  # noqa: BLE001 -- links are a nicety; try again next time
            self.log(f"[site_mirror] could not resolve the chat for post links: {e}")
            return None
        if username:
            self._link_base = f"https://t.me/{str(username).lstrip('@')}"
        elif chat_id is not None and str(chat_id).startswith("-100"):
            self._link_base = f"https://t.me/c/{str(chat_id)[4:]}"
        self._link_resolved = True
        return self._link_base

    async def _refresh_avatars(self, people: dict) -> None:
        """Fetches the avatars that are due: never fetched, fetched a day ago, or an hour
        after a failure. A failure keeps the avatar already known rather than blanking it."""
        if self.avatar is None:
            return
        now = time.monotonic()
        fetched = 0
        for author_id, _, _ in people.values():
            if author_id is None or fetched >= AVATARS_PER_CYCLE:
                continue
            key = int(author_id)
            known = self.avatars.get(key)
            if known is not None and now < known[0]:
                continue
            fetched += 1
            try:
                data = await self.avatar(key)
            except Exception as e:  # noqa: BLE001
                self.log(f"[site_mirror] could not fetch avatar {author_id}: {e}")
                self.avatars[key] = (now + AVATAR_RETRY_SECONDS, known[1] if known else None, known[2] if known else None)
                continue
            data = bytes(data) if data else None
            self.avatars[key] = (now + AVATAR_REFRESH_SECONDS, hashlib.sha256(data).hexdigest() if data else None, data)

    def _artists(self, people: dict) -> list[dict]:
        artists = []
        for who, (author_id, name, username) in sorted(people.items()):
            known = self.avatars.get(int(author_id)) if author_id is not None else None
            artists.append({"id": who, "name": name or "Unknown", "username": username,
                            "avatar": known[1] if known else None})
        return artists

    async def _upload_missing(self, files: dict, blobs: dict) -> bool:
        """Uploads every picture the site lacks. True when all of them are there now."""
        wanted = sorted(set(files) | set(blobs))
        if not wanted:
            return True
        missing: list[str] = []
        for start in range(0, len(wanted), 5000):
            answer = await self._post("/api/competition/photos", {"hashes": wanted[start:start + 5000]})
            missing.extend(str(h) for h in answer.get("missing") or [])
        complete = True
        for digest in missing:
            if digest in blobs:
                data = blobs[digest]
            else:
                try:
                    data = await asyncio.to_thread(files[digest].read_bytes)
                except (KeyError, OSError) as e:
                    self.log(f"[site_mirror] could not read photo {digest[:12]}: {e}")
                    complete = False
                    continue
            if hashlib.sha256(data).hexdigest() != digest:
                complete = False  # rewritten since it was hashed; the next cycle hashes it again
                continue
            try:
                await self._post("/api/competition/photos", {"hash": digest, "data": base64.b64encode(data).decode("ascii")})
            except MirrorError as e:
                # 415: not a JPEG, PNG or WebP. The site will never take it, so it is not
                # worth holding the push back for.
                if " 415:" not in str(e):
                    complete = False
                self.log(f"[site_mirror] photo {digest[:12]} was not uploaded: {e}")
        return complete

    # ---------------------------------------------------------------- one cycle

    async def sync_once(self) -> dict:
        """One look at the disk and, when anything changed, one push. Returns what it did."""
        link = await self._link()
        live, live_files, live_people = await asyncio.to_thread(build_live, self.entry, self.hashes, link)
        hall, hall_files, hall_people = await asyncio.to_thread(build_hall, self.entry, self.hashes, link)
        await self._refresh_avatars({**live_people, **hall_people})
        live["artists"] = self._artists(live_people)
        hall["artists"] = self._artists(hall_people)

        changed = {name: payload for name, payload in (("hall", hall), ("live", live))
                   if self.pushed.get(name) != signature(payload)}
        if not changed:
            return {"pushed": []}

        files: dict[str, Path] = {}
        if "hall" in changed:
            files.update(hall_files)
        if "live" in changed:
            files.update(live_files)
        blobs = {}
        for payload in changed.values():
            for artist in payload["artists"]:
                if artist["avatar"]:
                    blob = next((known[2] for known in self.avatars.values() if known[1] == artist["avatar"]), None)
                    if blob:
                        blobs[artist["avatar"]] = blob
        complete = await self._upload_missing(files, blobs)

        pushed = []
        # The hall first: a closed vote's results are on the site before the page stops
        # showing it as the vote on now.
        for name in ("hall", "live"):
            if name not in changed:
                continue
            answer = await self._post(f"/api/competition/{name}", changed[name])
            pushed.append(name)
            if complete:
                self.pushed[name] = signature(changed[name])
            self.log(f"[site_mirror] pushed the {name} to the site: {answer}")
        if not complete:
            # Pushed, but not remembered as pushed: the next cycle checks the pictures again,
            # after the loop's back-off, rather than once a minute.
            raise MirrorError("some photos did not reach the site; trying again")
        return {"pushed": pushed}

    async def run(self) -> None:
        """Runs until cancelled. Never raises: the bot's services share one supervisor, and
        a site that is down must not take the bot with it."""
        self.log(f"[site_mirror] mirroring the vote and the Hall of Fame to {self.base_url}")
        delay = self.interval
        try:
            while True:
                try:
                    await self.sync_once()
                    delay = self.interval
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    self.log(f"[site_mirror] the push failed, trying again in {int(delay)}s:\n{traceback.format_exc()}")
                    delay = min(max(delay * 2, self.interval), MAX_BACKOFF_SECONDS)
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.close()


_current: Mirror | None = None


def mirror_from_env(entry: str, avatar=None, chat=None, log=print) -> Mirror | None:
    """The mirror for `entry`, or None when the site's address or token is not set."""
    global _current
    url, token = mirror_settings()
    if not url or not token or not entry:
        return None
    if not url.startswith("https://") and not url.startswith("http://localhost") and not url.startswith("http://127.0.0.1"):
        log(f"[site_mirror] STRANGERHANDS_MIRROR_URL must be https:// -- got {url!r}; the mirror is off")
        return None
    _current = Mirror(entry, url, token, avatar=avatar, chat=chat, log=log)
    return _current


def wake() -> None:
    """Tells the running mirror to push now. Does nothing when there is none."""
    if _current is not None:
        _current.wake()
