"""The contests from before the bot ran them: the Sunday polls in the chat.

Until /vote (2 August 2026) the weekly contest was run by hand. Works were posted with
#итогинедели during the week, and on Sunday a Telegram poll asked which was best, the
works' pictures on its options. None of that reached a record the bot keeps, so the Hall
of Fame, the results records and the numbered weekly winners all began where the
automation did. This module brings those weeks in, in three steps; the administrator sees
the first two before anything is written ("/hall опросы" in bot_listener).

SCAN (scan_chat, the only step that talks to Telegram). Telegram's own poll filter lists
every poll the chat ever had -- one cheap listing, not a read of the whole history. A poll
is a contest when at least two of its options carry a picture, or when its question names
the contest ("итоги", "конкурс", "голосование", "лучшая работа"). Each option's picture is
downloaded, and so is every #итогинедели post of the weeks the polls cover, found by
Telegram's search: a post is what a work is everywhere else in the bot -- its author, its
caption, its whole album, its link. Options that are bare numbers ("1", "№2") with no
picture of their own take theirs from the album the poll's author posted just before it.

PLAN (plan_contests, pure). Each option is matched to a person, strongest evidence first:
  1. the option's text names a user -- a mention, or an @username;
  2. its picture is one of the week's posts' pictures (a 64-bit difference hash, so the
     same photo recompressed by Telegram twice still matches);
  3. the user who added the option to an open poll, or whose photo the album forwarded --
     unless one person added most of the options, which makes them the organiser;
  4. its text is the name of a post's author that week;
  5. its text is the name of somebody the chat statistics know.
An option nothing matches keeps its text as the author's name: the work is recorded, with
its votes, under the name the chat voted for. A work is its post when one is found (an
option with no picture of its own takes its author's post of that week); otherwise it is
the option itself, with the option's picture. Polls of one week and hashtag (a field too
big for one poll, split in two) are one contest. A poll whose results Telegram does not
show this account -- open, and not voted in -- is listed and skipped: its numbers are not
known yet.

APPLY (apply_plan). Each contest is written the way every finished week is: its photos
into the voting archive, where a cleared week's photos live (voting.archived_media_path);
a results record beside the announced weeks' (voting.save_imported_results); then the
Hall of Fame imports it like any other week (hall_of_fame.import_history). Nothing that
exists is overwritten -- a week the hall or the results already have is skipped -- so
running it again is safe. The weekly contest's winners also go into /weekwinner's
numbered table (reputation and the 🏆 in /stat), but only while that table's numbering
agrees with the order of the weeks; otherwise it is left to an administrator.

Storage: the plan and the downloaded pictures under DATA_DIR/voting/legacy/<chat key>/,
a subdirectory nothing that lists polls reads. Everything here that touches the disk or
decodes an image is synchronous and runs in a worker thread; scan_chat is the only
coroutine.
"""

import json
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import voting

# What the results records of imported weeks say they came from.
SOURCE = "telegram_poll"

CONTEST, PLAIN = "contest", "plain"   # see poll_kind

READY = "ready"            # will be written
KNOWN = "known"            # the hall or the results already have this week
HIDDEN = "hidden"          # Telegram does not show this account the poll's results
AUTOMATED = "automated"    # the week is the bot's own, or later

# A question naming the contest makes a poll one even without pictures on its options.
CONTEST_QUESTION = re.compile(r"итог|конкурс|голосован|лучш", re.IGNORECASE)
MIN_OPTIONS = 2
MIN_PICTURED_OPTIONS = 2
# How long before its poll a work may have been posted -- the contest week and the one
# before it, the two weeks /vote collects -- and how long after: an open poll takes new
# options for a while after it is posted.
POST_LOOKBACK = timedelta(days=14)
POST_LOOKAHEAD = timedelta(days=2)
# At most this many of a difference hash's 64 bits may differ for two pictures to be the
# same work. Two unrelated photos differ in about 32.
HASH_MATCH_BITS = 10
# A poll that names no contest is still one when its author posted it this close to a
# contest poll: the second half of a field too big for one poll.
SPLIT_POLL_GAP = timedelta(hours=2)
# Somebody who added this many options to one open poll is its organiser, not an author.
ORGANISER_OPTIONS = 3
# A poll posted Monday to Wednesday is about the week before it (the nearest Sunday is
# behind it); Thursday to Sunday, about its own week.
LATE_WEEKDAYS = 3
# The presentation album before a numbered poll: how far back to look for it.
ALBUM_LOOKBACK_MESSAGES = 40
ALBUM_LOOKBACK = timedelta(hours=12)

# What each kind of evidence is called in the preview.
HOW_MENTION = "упоминание"
HOW_USERNAME = "@ник"
HOW_PICTURE = "картинка"
HOW_ADDED = "добавил вариант"
HOW_FORWARD = "пересланное фото"
HOW_NAME = "имя"
HOW_MEMBER = "участник чата"
HOW_NONE = "не найден"

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_USERNAME = re.compile(r"(?<![\w@])@([A-Za-z][A-Za-z0-9_]{3,31})")
_NUMBERED = re.compile(r"^\s*(?:#|работа|вариант|участник)?\s*(\d{1,2})\s*[.)]?\s*$", re.IGNORECASE)
_LEADING_NUMBER = re.compile(r"^\s*(?:#\s*)?\d{1,2}\s*[.)\-–—:]+\s*|^\s*(?:#\s*)?\d{1,2}\s+(?=\D)")
# An option's id inside the contest when the work is the option itself, not a post:
# "<poll message id>o<option index>". Restoring a lost photo reads it back.
OPTION_ENTRY = re.compile(r"^(\d+)o(\d+)$")


# ------------------------------------------------------------------------------ storage


def legacy_dir(entry: str) -> Path:
    """Under the voting directory, one level down: latest_poll globs the voting directory
    itself and never recurses, so nothing here is mistaken for a poll."""
    return voting._voting_dir() / "legacy" / voting._poll_key(entry)


def cache_dir(entry: str) -> Path:
    return legacy_dir(entry) / "cache"


def plan_path(entry: str) -> Path:
    return legacy_dir(entry) / "plan.json"


# ----------------------------------------------------------------------------- the text


def _text_of(value) -> str:
    """A poll's question and options are TextWithEntities in current layers, plain text
    in older ones."""
    if value is None:
        return ""
    return str(getattr(value, "text", value) or "")


def _plain(text: str) -> str:
    """Keycaps and variation selectors out ("1️⃣" -> "1"), compatibility forms folded --
    after "№" becomes "#", which NFKC would otherwise spell "No"."""
    text = unicodedata.normalize("NFKC", (text or "").replace("№", "#"))
    return "".join(ch for ch in text if ch not in "️︎⃣")


def option_number(text: str) -> int | None:
    """The number of an option that is nothing but a number: "1", "№2", "3️⃣", "Работа 4"."""
    match = _NUMBERED.match(_plain(text))
    return int(match.group(1)) if match else None


def album_offset(numbers: list[int], album_size: int) -> int:
    """What to subtract from an option's number to find its photo in the album before the
    poll. Nothing, normally; but the second part of a split poll may carry the numbering
    on ("11".."15") under an album of its own five photos, and then its first number is
    the album's first photo."""
    if not numbers or max(numbers) <= album_size:
        return 0
    if max(numbers) - min(numbers) + 1 <= album_size:
        return min(numbers) - 1
    return 0


def clean_option_text(text: str) -> str:
    """The option without its numbering: "1. Аня — дракон" -> "Аня — дракон"."""
    return " ".join(_LEADING_NUMBER.sub("", _plain(text)).split())


def _name_key(text: str) -> str:
    text = _plain(text).casefold().replace("ё", "е")
    text = _USERNAME.sub(" ", text)
    text = re.sub(r"[\W_\d]+", " ", text)
    return " ".join(text.split())


def name_score(option_text: str, name: str, username: str | None = None) -> int:
    """How well an option's text names somebody: 3 the very name (or the bare @tag),
    2 every word of one in the other ("Аня" / "Аня Петрова"), 1 one long word in common,
    0 nothing."""
    option = _name_key(clean_option_text(option_text))
    if not option:
        return 0
    if username and option.replace(" ", "") == username.casefold():
        return 3
    key = _name_key(name)
    if not key:
        return 0
    if option == key:
        return 3
    option_words, name_words = option.split(), key.split()
    if any(len(w) >= 3 for w in name_words) and (
        all(w in option_words for w in name_words) or all(w in name_words for w in option_words)
    ):
        return 2
    if any(len(w) >= 4 and w in name_words for w in option_words):
        return 1
    return 0


def contest_week(posted: datetime) -> str:
    """The ISO week a poll posted at `posted` (local time) decided: its own, unless it was
    posted early in the week, when it is the week before -- see LATE_WEEKDAYS."""
    day = posted.date()
    if day.weekday() < LATE_WEEKDAYS:
        day -= timedelta(days=day.weekday() + 1)
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def poll_hashtag(question: str) -> tuple[str, str]:
    """(hashtag, title) of the contest a poll's question names: a hashtag other than the
    weekly one makes it that thematic contest, titled as the tag was typed; anything
    else is the weekly contest."""
    for raw in re.findall(r"#\w+", unicodedata.normalize("NFKC", question or "")):
        tag = voting.normalize_hashtag(raw)
        if tag and not voting.is_weekly_hashtag(tag):
            return tag, raw.lstrip("#")[:voting.THEME_TITLE_MAX_LENGTH]
    return voting.CONTEST_HASHTAG, ""


# --------------------------------------------------------------------------- the pictures


def photo_hash(path: Path) -> int | None:
    """A 64-bit difference hash: the picture as 9x8 greys, one bit per neighbouring pair.
    It survives recompression and resizing, which is all Telegram does to a photo posted
    twice. None when the file is not a readable picture."""
    try:
        from PIL import Image, ImageOps

        with Image.open(path) as opened:
            image = (ImageOps.exif_transpose(opened) or opened).convert("L").resize((9, 8), Image.LANCZOS)
            pixels = list(image.tobytes())
    except Exception:  # noqa: BLE001 -- an unreadable picture just cannot be matched
        return None
    bits = 0
    for row in range(8):
        for column in range(8):
            bits = (bits << 1) | int(pixels[row * 9 + column] > pixels[row * 9 + column + 1])
    return bits


def hash_photos(directory: Path, names) -> dict[str, int]:
    hashes = {}
    for name in set(names):
        if name and _SAFE_NAME.match(name):
            value = photo_hash(directory / name)
            if value is not None:
                hashes[name] = value
    return hashes


def _distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _is_picture(media) -> bool:
    if media is None:
        return False
    if getattr(media, "photo", None) is not None:
        return True
    document = getattr(media, "document", None)
    return str(getattr(document, "mime_type", "") or "").startswith("image/")


# ------------------------------------------------------------------------------- the scan


def _user_id(peer) -> int | None:
    value = getattr(peer, "user_id", None)
    return int(value) if isinstance(value, int) else None


def _as_id(value) -> int | None:
    """A Telegram user id out of a stats key or a stored field; None for anything else
    (the statistics key a hidden sender as "None")."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _person(user) -> dict | None:
    if user is None:
        return None
    name = " ".join(part for part in (getattr(user, "first_name", None), getattr(user, "last_name", None)) if part)
    username = getattr(user, "username", None)
    return {"name": name or (f"@{username}" if username else f"id{getattr(user, 'id', '')}"),
            "username": username}


def _counts(results) -> dict[str, int] | None:
    """Votes per option (by its option bytes, hex), or None when Telegram withholds them."""
    rows = getattr(results, "results", None)
    if not rows:
        return None
    counts = {}
    for row in rows:
        voters = getattr(row, "voters", None)
        if voters is None:
            return None
        counts[_option_key(getattr(row, "option", b""))] = int(voters)
    return counts


def _option_key(option) -> str:
    if isinstance(option, (bytes, bytearray)):
        return bytes(option).hex()
    return str(option)


async def _fresh_results(client, entity, message_id: int, log):
    """Asks Telegram for a poll's results again: the copy that comes with a message in a
    listing can be the short form without the numbers."""
    try:
        from telethon.tl import functions, types
    except ImportError:
        return None
    try:
        try:
            request = functions.messages.GetPollResultsRequest(peer=entity, msg_id=message_id, poll_hash=0)
        except TypeError:  # an older layer, before the hash
            request = functions.messages.GetPollResultsRequest(peer=entity, msg_id=message_id)
        updates = await client(request)
    except Exception as e:
        log(f"[legacy] could not refresh the results of poll {message_id}: {e}")
        return None
    for update in getattr(updates, "updates", None) or []:
        if isinstance(update, types.UpdateMessagePoll) and getattr(update, "results", None) is not None:
            return update.results
    return None


async def _download(client, media, path: Path, log) -> bool:
    if path.exists():
        return True
    try:
        await client.download_media(media, file=str(path))
    except Exception as e:  # one picture lost costs that option its picture, not the poll
        log(f"[legacy] could not download {path.name}: {e}")
        return False
    return path.exists()


async def _presentation_album(client, entity, poll_message, directory: Path, log) -> list[dict]:
    """The pictures the poll's author posted just before it, oldest first -- the works a
    numbered poll's options stand for. Messages from other people in between are passed
    over; a message of the author's own that is not a picture ends the album, once it has
    begun (a "голосуем!" right before the poll is skipped)."""
    sender = getattr(poll_message, "sender_id", None)
    earliest = poll_message.date - ALBUM_LOOKBACK
    pictures = []
    try:
        async for message in client.iter_messages(entity, offset_id=poll_message.id, limit=ALBUM_LOOKBACK_MESSAGES):
            if message.date < earliest:
                break
            if sender is not None and getattr(message, "sender_id", None) != sender:
                continue
            if not getattr(message, "photo", None):
                if pictures:
                    break
                continue
            pictures.append(message)
    except Exception as e:
        log(f"[legacy] could not read the album before poll {poll_message.id}: {e}")
        return []
    album = []
    for message in sorted(pictures, key=lambda m: m.id):
        name = f"{message.id}_0.jpg"
        if not await _download(client, message, directory / name, log):
            continue
        forward = getattr(message, "fwd_from", None)
        # A forward from somebody who hides their account keeps only their name.
        caption = " ".join(part for part in (str(getattr(message, "text", "") or ""),
                                             str(getattr(forward, "from_name", "") or "")) if part)
        album.append({
            "message_id": message.id, "photo": name, "caption": caption,
            "forwarded_from": _user_id(getattr(forward, "from_id", None)),
        })
    return album


def poll_kind(message) -> str | None:
    """CONTEST for a poll that is a contest on its own (pictures on its options, or a
    question naming the contest); PLAIN for any other poll that could still be one part
    of a split contest; None for what never is (a quiz, a single option)."""
    poll = getattr(getattr(message, "media", None), "poll", None)
    if poll is None or getattr(poll, "quiz", False):
        return None
    answers = list(getattr(poll, "answers", None) or [])
    if len(answers) < MIN_OPTIONS:
        return None
    question = _text_of(getattr(poll, "question", ""))
    pictured = sum(1 for answer in answers if _is_picture(getattr(answer, "media", None)))
    if pictured >= MIN_PICTURED_OPTIONS or CONTEST_QUESTION.search(question):
        return CONTEST
    return PLAIN


def contest_polls(messages: list) -> list:
    """The polls among `messages` that are contests: every one that is a contest on its
    own, and every other poll its author posted within SPLIT_POLL_GAP of one -- the
    second half of a field too big for one poll, whose question ("Часть 2") need not
    name the contest. Oldest first."""
    kinds = {message.id: poll_kind(message) for message in messages}
    anchors = [m for m in messages if kinds[m.id] == CONTEST]
    kept = []
    for message in sorted(messages, key=lambda m: m.id):
        kind = kinds[message.id]
        if kind == CONTEST or (kind == PLAIN and any(
            getattr(anchor, "sender_id", None) == getattr(message, "sender_id", None)
            and abs(anchor.date - message.date) <= SPLIT_POLL_GAP
            for anchor in anchors
        )):
            kept.append(message)
    return kept


async def _read_poll(client, entity, message, tz, directory: Path, people: dict, log) -> dict:
    """One contest poll as a plain dict, its pictures downloaded."""
    media = getattr(message, "media", None)
    poll = media.poll
    answers = list(getattr(poll, "answers", None) or [])
    question = _text_of(getattr(poll, "question", ""))

    results = getattr(media, "results", None)
    counts = _counts(results)
    if counts is None:
        results = await _fresh_results(client, entity, message.id, log) or results
        counts = _counts(results)

    options = []
    for index, answer in enumerate(answers):
        text_value = getattr(answer, "text", "")
        text = _text_of(text_value)
        mentions = []
        for entity_ in getattr(text_value, "entities", None) or []:
            user_id = getattr(entity_, "user_id", None)
            if isinstance(user_id, int) and user_id not in mentions:
                mentions.append(user_id)
        photo = None
        answer_media = getattr(answer, "media", None)
        if _is_picture(answer_media):
            name = f"{message.id}_o{index}.jpg"
            if await _download(client, answer_media, directory / name, log):
                photo = name
        added_at = getattr(answer, "date", None)
        options.append({
            "index": index,
            "text": text,
            "votes": None if counts is None else counts.get(_option_key(getattr(answer, "option", b"")), 0),
            "photo": photo,
            "mentions": mentions,
            "usernames": [u for u in dict.fromkeys(_USERNAME.findall(text))],
            "added_by": _user_id(getattr(answer, "added_by", None)),
            "added_at": added_at.astimezone(tz).isoformat() if isinstance(added_at, datetime) else "",
            "caption": "",
            "forwarded_from": None,
        })

    # A numbered poll: its options are "1", "2", ... and the pictures are the album before it.
    numbers = [option_number(o["text"]) for o in options if o["photo"] is None and option_number(o["text"])]
    if numbers:
        album = await _presentation_album(client, entity, message, directory, log)
        offset = album_offset(numbers, len(album))
        for option in options:
            number = option_number(option["text"])
            if option["photo"] is None and number and 1 <= number - offset <= len(album):
                picture = album[number - offset - 1]
                option.update(photo=picture["photo"], caption=picture["caption"],
                              forwarded_from=picture["forwarded_from"])

    for user_id in {o["added_by"] for o in options} | {u for o in options for u in o["mentions"]} \
            | {o["forwarded_from"] for o in options}:
        if user_id is None or str(user_id) in people:
            continue
        try:
            people[str(user_id)] = _person(await client.get_entity(user_id))
        except Exception:
            continue  # named later from the posts or the chat statistics, if at all

    closed = bool(getattr(poll, "closed", False))
    close_date = getattr(poll, "close_date", None)
    total = getattr(results, "total_voters", None)
    return {
        "message_id": message.id,
        "posted_at": message.date.astimezone(tz).isoformat(),
        "question": question,
        "sender_id": getattr(message, "sender_id", None),
        "closed": closed,
        "closed_at": (close_date.astimezone(tz).isoformat()
                      if closed and isinstance(close_date, datetime) else ""),
        "total_voters": int(total) if isinstance(total, int) else None,
        "visible": counts is not None,
        "options": options,
    }


async def scan_chat(
    client, entity, tz, directory: Path, *, until: datetime | None = None,
    hashtag: str = voting.CONTEST_HASHTAG, progress=None, log=print,
) -> dict:
    """Everything the plan is made from, as plain JSON: {"polls_seen", "polls", "posts",
    "people"}. `until` (timezone-aware) stops the listing at the automation: a poll
    posted after it is the bot's era and not read at all. Pictures go into `directory`."""
    from telethon.tl import types

    async def say(text: str) -> None:
        if progress is None:
            return
        try:
            await progress(text)
        except Exception as e:
            log(f"[legacy] progress report failed: {e}")

    directory.mkdir(parents=True, exist_ok=True)
    listing = {"filter": types.InputMessagesFilterPoll()}
    if until is not None:
        listing["offset_date"] = until.astimezone(timezone.utc)
    people: dict[str, dict] = {}
    polls = []
    await say("Ищу опросы в чате…")
    listed = []
    async for message in client.iter_messages(entity, **listing):
        if getattr(getattr(message, "media", None), "poll", None) is None:
            continue
        if until is not None and message.date >= until:
            continue
        listed.append(message)
    seen = len(listed)
    chosen = contest_polls(listed)
    for position, message in enumerate(chosen, start=1):
        await say(f"Конкурсов-опросов: {len(chosen)} (всего опросов: {seen}). Качаю картинки: {position} из {len(chosen)}…")
        polls.append(await _read_poll(client, entity, message, tz, directory, people, log))

    posts: list[voting.Entry] = []
    if polls:
        since = min(datetime.fromisoformat(p["posted_at"]) for p in polls) - POST_LOOKBACK
        till = max(datetime.fromisoformat(p["posted_at"]) for p in polls) + POST_LOOKAHEAD
        if until is not None:
            till = min(till, until.astimezone(tz))

        async def post_progress(stage: str, done: int, total: int) -> None:
            if stage == "download" and total:
                await say(f"Скачиваю работы с {hashtag}: {done} из {total}…")

        await say(f"Ищу посты с {hashtag}…")
        posts = await voting.search_entries(
            client, entity, tz, directory, since, till, hashtag=hashtag, progress=post_progress, log=log,
        )
        if not posts:
            # The search index can miss a tag; reading the weeks message by message cannot.
            await say(f"Поиск не нашёл {hashtag} — читаю чат подряд, это дольше…")
            posts = await voting.collect_entries(
                client, entity, tz, directory, hashtag=hashtag, since=since, until=till,
                stop_at_known=False, progress=post_progress, log=log,
            )
    log(f"[legacy] {seen} poll(s) read, {len(polls)} contest(s), {len(posts)} post(s)")
    return {
        "polls_seen": seen,
        "polls": polls,
        "posts": [post.to_dict() for post in posts],
        "people": people,
    }


# ------------------------------------------------------------------------------- the plan


@dataclass
class PlannedWork:
    entry: voting.Entry
    votes: int
    how: str
    option_text: str = ""

    def to_dict(self) -> dict:
        return {"entry": self.entry.to_dict(), "votes": self.votes, "how": self.how,
                "option_text": self.option_text}

    @classmethod
    def from_dict(cls, raw: dict) -> "PlannedWork":
        return cls(entry=voting.Entry.from_dict(raw.get("entry") or {}), votes=int(raw.get("votes") or 0),
                   how=str(raw.get("how") or HOW_NONE), option_text=str(raw.get("option_text") or ""))


@dataclass
class PlannedContest:
    poll_id: str
    week: str
    hashtag: str
    title: str
    poll_message_ids: list[int]
    created_at: str
    closed_at: str
    voters: int
    status: str
    works: list[PlannedWork] = field(default_factory=list)   # best first

    @property
    def is_weekly(self) -> bool:
        return voting.is_weekly_hashtag(self.hashtag)

    def label(self) -> str:
        return voting.contest_title(self.hashtag, self.title)

    def to_dict(self) -> dict:
        return {
            "poll_id": self.poll_id, "week": self.week, "hashtag": self.hashtag, "title": self.title,
            "poll_message_ids": list(self.poll_message_ids), "created_at": self.created_at,
            "closed_at": self.closed_at, "voters": self.voters, "status": self.status,
            "works": [work.to_dict() for work in self.works],
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "PlannedContest":
        return cls(
            poll_id=str(raw.get("poll_id") or ""), week=str(raw.get("week") or ""),
            hashtag=voting.normalize_hashtag(raw.get("hashtag")) or voting.CONTEST_HASHTAG,
            title=str(raw.get("title") or ""),
            poll_message_ids=[int(i) for i in raw.get("poll_message_ids") or []],
            created_at=str(raw.get("created_at") or ""), closed_at=str(raw.get("closed_at") or ""),
            voters=int(raw.get("voters") or 0), status=str(raw.get("status") or ""),
            works=[PlannedWork.from_dict(w) for w in raw.get("works") or [] if isinstance(w, dict)],
        )


def _when(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _utc(text: str) -> str:
    """A local time stamp as UTC, the way the bot's own records keep created_at and
    announced_at -- so the hall's chronology compares like with like."""
    when = _when(text)
    if when is None or when.tzinfo is None:
        return text or ""
    return when.astimezone(timezone.utc).isoformat()


def _unique_best(scored: list[tuple[int, object, object]], minimum: int):
    """The candidate with the highest score of at least `minimum`, when nobody else shares
    that score -- two people called Аня are no answer to "Аня"."""
    good = [item for item in scored if item[0] >= minimum]
    if not good:
        return None
    best = max(score for score, _, _ in good)
    winners = {identity for score, identity, _ in good if score == best}
    if len(winners) != 1:
        return None
    return next(item for item in good if item[0] == best)


def _plan_works(polls: list[dict], posts: list[voting.Entry], people: dict, members: dict,
                hashes: dict) -> list[PlannedWork]:
    options = [(poll, option) for poll in polls for option in poll["options"]]
    # Each work once: the strongest picture matches are paired first.
    pairs = []
    for position, (_, option) in enumerate(options):
        mine = hashes.get(option.get("photo") or "")
        if mine is None:
            continue
        for index, post in enumerate(posts):
            distances = [_distance(mine, hashes[name]) for name in post.media if name in hashes]
            if distances and min(distances) <= HASH_MATCH_BITS:
                pairs.append((min(distances), position, index))
    by_picture: dict[int, int] = {}
    taken: set[int] = set()
    for _, position, index in sorted(pairs):
        if position not in by_picture and index not in taken:
            by_picture[position] = index
            taken.add(index)

    adders: dict[tuple[int, int], int] = {}
    for poll, option in options:
        if option.get("added_by") is not None:
            key = (poll["message_id"], option["added_by"])
            adders[key] = adders.get(key, 0) + 1

    by_username = {post.author_username.casefold(): post for post in posts if post.author_username}
    member_usernames = {(m.get("username") or "").casefold(): uid for uid, m in members.items() if m.get("username")}

    def person(user_id) -> tuple[str, str | None]:
        post = next((p for p in posts if p.author_id == user_id), None)
        if post is not None:
            return post.author_name, post.author_username
        known = people.get(str(user_id)) or members.get(str(user_id))
        if known:
            return known.get("name") or f"id{user_id}", known.get("username")
        return f"id{user_id}", None

    works: list[PlannedWork] = []
    used: set[int] = set(by_picture.values())
    for position, (poll, option) in enumerate(options):
        text = option.get("text") or ""
        hint = " ".join(part for part in (text, option.get("caption") or "") if part)
        author_id, author_name, author_username, how = None, None, None, HOW_NONE
        post_index = None

        if option.get("mentions"):
            author_id, how = option["mentions"][0], HOW_MENTION
        else:
            for username in option.get("usernames") or _USERNAME.findall(option.get("caption") or ""):
                post = by_username.get(username.casefold())
                if post is not None:
                    author_id, author_name, author_username = post.author_id, post.author_name, post.author_username
                elif _as_id(member_usernames.get(username.casefold())) is not None:
                    author_id = _as_id(member_usernames[username.casefold()])
                else:
                    author_name, author_username = f"@{username}", username
                how = HOW_USERNAME
                break
        if how == HOW_NONE and position in by_picture:
            post_index = by_picture[position]
            post = posts[post_index]
            author_id, author_name, author_username, how = (
                post.author_id, post.author_name, post.author_username, HOW_PICTURE)
        if how == HOW_NONE:
            adder = option.get("added_by")
            if adder is not None and adders.get((poll["message_id"], adder), 0) < ORGANISER_OPTIONS:
                author_id, how = adder, HOW_ADDED
            elif option.get("forwarded_from") is not None:
                author_id, how = option["forwarded_from"], HOW_FORWARD
        if how == HOW_NONE:
            authors = {}
            for post in posts:
                authors.setdefault(post.author_id if post.author_id is not None else post.author_name, post)
            scored = [(max(name_score(hint, post.author_name, post.author_username),
                           name_score(text, post.author_name, post.author_username)), identity, post)
                      for identity, post in authors.items()]
            found = _unique_best(scored, 1)
            if found is not None:
                post = found[2]
                author_id, author_name, author_username, how = (
                    post.author_id, post.author_name, post.author_username, HOW_NAME)
        if how == HOW_NONE:
            scored = [(max(name_score(hint, m.get("name") or "", m.get("username")),
                           name_score(text, m.get("name") or "", m.get("username"))), uid, m)
                      for uid, m in members.items()]
            found = _unique_best(scored, 2)
            if found is not None and _as_id(found[1]) is not None:
                author_id, how = _as_id(found[1]), HOW_MEMBER

        if author_id is not None and author_name is None:
            author_name, author_username = person(author_id)

        # Which work it is: the matched picture's post, when it is by this author; else,
        # for an option with no picture of its own, its author's post of the week (before
        # the poll, newest first).
        if post_index is None and position in by_picture and author_id is not None:
            post_index = by_picture[position]
        if post_index is not None and posts[post_index].author_id != author_id:
            post_index = None
        if post_index is None and not option.get("photo") and (author_id is not None or author_name):
            poll_time = _when(poll["posted_at"])
            mine = [i for i, p in enumerate(posts) if i not in used and (
                (author_id is not None and p.author_id == author_id)
                or (author_id is None and p.author_id is None and p.author_name == author_name))]
            before = [i for i in mine if poll_time is None or (_when(posts[i].posted_at) or poll_time) <= poll_time]
            pick = max(before, key=lambda i: posts[i].message_id) if before else (
                min(mine, key=lambda i: posts[i].message_id) if mine else None)
            if pick is not None:
                post_index = pick
                used.add(pick)

        votes = int(option.get("votes") or 0)
        cleaned = clean_option_text(text)
        if post_index is not None:
            post = posts[post_index]
            entry = voting.Entry.from_dict(post.to_dict())
        else:
            if author_name is None:
                author_name = cleaned or f"Вариант {option['index'] + 1}"
            entry = voting.Entry(
                entry_id=f"{poll['message_id']}o{option['index']}",
                message_id=int(poll["message_id"]),
                author_id=author_id,
                author_name=author_name,
                author_username=author_username,
                text="" if _name_key(cleaned) == _name_key(author_name) else cleaned,
                media=[option["photo"]] if option.get("photo") else [],
                posted_at=option.get("added_at") or poll["posted_at"],
            )
        works.append(PlannedWork(entry=entry, votes=votes, how=how, option_text=text))
    # Best first; a tie keeps the polls' own order, the way a tally keeps its entries'.
    order = {id(work): position for position, work in enumerate(works)}
    works.sort(key=lambda work: (-work.votes, order[id(work)]))
    return works


def plan_contests(
    scan: dict, *, members: dict | None = None, hashes: dict | None = None,
    known=frozenset(), first_automated_week: str | None = None,
) -> list[PlannedContest]:
    """The contests a scan holds, oldest first, each with its works matched and ranked
    and a status saying whether it will be written. Pure: everything it needs is passed
    in -- `members` {user id: {"name", "username"}} from the chat statistics, `hashes`
    {photo name: difference hash}, `known` the poll ids already recorded, and the first
    week the bot ran the vote itself."""
    members, hashes = members or {}, hashes or {}
    posts = [voting.Entry.from_dict(raw) for raw in scan.get("posts") or []]
    people = scan.get("people") or {}
    groups: dict[tuple[str, str], list[dict]] = {}
    titles: dict[tuple[str, str], str] = {}
    for poll in scan.get("polls") or []:
        posted = _when(poll.get("posted_at"))
        if posted is None:
            continue
        tag, title = poll_hashtag(poll.get("question") or "")
        key = (contest_week(posted), tag)
        groups.setdefault(key, []).append(poll)
        titles.setdefault(key, title)

    contests = []
    for (week, tag), polls in sorted(groups.items()):
        polls.sort(key=lambda p: p["message_id"])
        first, last = _when(polls[0]["posted_at"]), _when(polls[-1]["posted_at"])
        window = [post for post in posts if (when := _when(post.posted_at)) is not None
                  and first - POST_LOOKBACK <= when < last + POST_LOOKAHEAD]
        poll_id = voting.poll_id_for(week, tag)
        if first_automated_week and week >= first_automated_week:
            status = AUTOMATED
        elif poll_id in known:
            status = KNOWN
        elif not all(poll.get("visible") for poll in polls):
            status = HIDDEN
        else:
            status = READY
        works = _plan_works(polls, window, people, members, hashes) if status != AUTOMATED else []
        totals = [poll["total_voters"] for poll in polls if poll.get("total_voters") is not None]
        voters = max(totals) if totals else sum(work.votes for work in works)
        closed_at = max((poll.get("closed_at") or poll["posted_at"]) for poll in polls)
        contests.append(PlannedContest(
            poll_id=poll_id, week=week, hashtag=tag, title=titles[(week, tag)],
            poll_message_ids=[poll["message_id"] for poll in polls],
            created_at=polls[0]["posted_at"], closed_at=closed_at, voters=voters,
            status=status, works=works,
        ))
    return contests


def known_contests(entry: str) -> tuple[set[str], str | None]:
    """(every poll id the hall, the results records or the polls already have; the first
    week the bot ran the vote itself, or None). A week imported from a poll before is
    known, but is not the bot's own, so it never moves the boundary."""
    import hall_of_fame

    imported = set()
    results = set(voting.results_poll_ids(entry))
    for poll_id in results:
        record = voting.load_results(entry, poll_id) or {}
        if record.get("source") == SOURCE:
            imported.add(poll_id)
    hall = set()
    directory = hall_of_fame.contests_dir(entry)
    if directory.exists():
        hall = {path.stem for path in directory.glob("*.json")}
    polls = set(voting.poll_ids(entry)) | set(voting.archived_poll_ids(entry))
    known = results | hall | polls
    weeks = sorted(match.group(0) for poll_id in known - imported
                   if (match := re.match(r"^\d{4}-W\d{2}", poll_id)))
    return known, (weeks[0] if weeks else None)


def week_start(week: str, tz) -> datetime | None:
    """Monday 00:00 local of an ISO week id ("2026-W31")."""
    match = re.match(r"^(\d{4})-W(\d{2})", week or "")
    if not match:
        return None
    day = datetime.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
    return day.replace(tzinfo=tz)


def build_plan(entry: str, scan: dict, members: dict | None = None) -> list[PlannedContest]:
    """Hashes the scan's pictures, plans its contests against what is already on disk,
    and saves the plan for apply_plan. Disk and image work: run it in a worker thread."""
    names = [o.get("photo") for p in scan.get("polls") or [] for o in p["options"]]
    names += [name for post in scan.get("posts") or [] for name in post.get("media") or []]
    hashes = hash_photos(cache_dir(entry), names)
    known, first_automated = known_contests(entry)
    contests = plan_contests(scan, members=members, hashes=hashes, known=known,
                             first_automated_week=first_automated)
    path = plan_path(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({
        "planned_at": datetime.now(timezone.utc).isoformat(),
        "polls_seen": scan.get("polls_seen", 0),
        "first_automated_week": first_automated,
        "contests": [contest.to_dict() for contest in contests],
        "scan": scan,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return contests


def load_plan(entry: str) -> dict | None:
    path = plan_path(entry)
    if not path.exists():
        return None
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return plan if isinstance(plan, dict) else None


# ---------------------------------------------------------------------------- the preview

MESSAGE_BUDGET = 3500   # characters per message, well inside Telegram's 4096


def _short_date(text: str) -> str:
    when = _when(text)
    return when.strftime("%d.%m.%Y") if when else ""


def _count(number: int, one: str, few: str, many: str) -> str:
    """"2 опроса", "5 работ", "21 работа" -- Russian plural, teens included."""
    if 11 <= number % 100 <= 14 or number % 10 == 0 or number % 10 >= 5:
        word = many
    elif number % 10 == 1:
        word = one
    else:
        word = few
    return f"{number} {word}"


def _who(entry: voting.Entry) -> str:
    return f"{entry.author_name} (@{entry.author_username})" if entry.author_username else entry.author_name


def preview_messages(contests: list[PlannedContest], polls_seen: int) -> list[str]:
    """What the administrator reads before anything is written, as one or more messages:
    every contest found, its podium, how its works were matched and what will happen to
    it."""
    ready = [c for c in contests if c.status == READY]
    head = [
        f"Опросов в чате до автоматизации: {polls_seen}, из них конкурсов: {len(contests)}.",
        f"Запишу в Доску почёта: {len(ready)}." if ready else "Записывать нечего.",
    ]
    blocks = []
    for contest in contests:
        works = contest.works
        lines = [f"🗳 {contest.week} · {_short_date(contest.created_at)} · «{contest.label()}»"
                 + (f" · опросов: {len(contest.poll_message_ids)}" if len(contest.poll_message_ids) > 1 else "")]
        if contest.status == AUTOMATED:
            lines.append("   уже неделя бота — пропускаю")
            blocks.append("\n".join(lines))
            continue
        lines.append(f"   работ: {len(works)}, проголосовало: {contest.voters}")
        medals = ("🥇", "🥈", "🥉")
        podium = [f"{medal} {_who(work.entry)} — {voting.votes_label(work.votes)}"
                  for medal, work in zip(medals, works) if work.votes > 0]
        if podium:
            lines.extend(f"   {line}" for line in podium)
        counts: dict[str, int] = {}
        for work in works:
            counts[work.how] = counts.get(work.how, 0) + 1
        matched = len(works) - counts.get(HOW_NONE, 0)
        if works:
            detail = ", ".join(f"{how} {count}" for how, count in counts.items() if how != HOW_NONE)
            lines.append(f"   узнал авторов: {matched} из {len(works)}" + (f" ({detail})" if detail else ""))
        missing = [work for work in works if work.how == HOW_NONE]
        if missing:
            names = ", ".join(f"«{clean_option_text(w.option_text) or w.entry.author_name}»" for w in missing[:5])
            lines.append(f"   не узнал: {names}" + (" и др." if len(missing) > 5 else "") + " — запишу под этим именем")
        no_photo = sum(1 for work in works if not work.entry.media)
        if no_photo:
            lines.append(f"   без картинки: {no_photo}")
        lines.append({
            READY: "   ✅ запишу",
            KNOWN: "   уже на Доске почёта — пропускаю",
            HIDDEN: "   ⚠️ Telegram не показывает итоги этого опроса: останови его (это может автор опроса) "
                    "и запусти снова. Голосовать в нём не надо — это добавит голос",
        }.get(contest.status, ""))
        blocks.append("\n".join(line for line in lines if line))

    messages, current = [], "\n".join(head)
    for block in blocks:
        if len(current) + 2 + len(block) > MESSAGE_BUDGET:
            messages.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}"
    messages.append(current)
    return messages


# ------------------------------------------------------------------------------ the apply


def _place_photos(names: list[str], source: Path, target: Path) -> list[str]:
    """The names now in `target`, each a hardlink to the downloaded picture where the disk
    allows (a copy where it does not) -- the hall's own rule for its copies."""
    present = []
    for name in names:
        if not _SAFE_NAME.match(name or ""):
            continue
        destination = target / name
        if not destination.exists():
            origin = source / name
            if not origin.is_file():
                continue
            try:
                target.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(origin, destination)
                except OSError:
                    shutil.copy2(origin, destination)
            except OSError:
                continue
        present.append(name)
    return present


def _weekly_winners(entry: str, written: list[str], giver_id, giver_name: str) -> dict:
    """Writes the winners of the weekly contests just imported into /weekwinner's numbered
    table, numbered by the order of every weekly contest in the hall -- but only while the
    table agrees with that order. A table numbered some other way (its №1 is not the
    hall's first week) is left alone: guessing would hand somebody a second win for one
    week. Returns what happened, for the report."""
    import hall_of_fame
    import stats

    hall = hall_of_fame.snapshot(entry)
    weekly = sorted((contest for contest in hall.contests if contest.is_weekly), key=lambda c: c.contest_id)
    number_of = {contest.contest_id: number for number, contest in enumerate(weekly, start=1)}
    try:
        stored = stats.weekly_contest_winners(entry)
    except (json.JSONDecodeError, OSError, ValueError):
        return {"status": "unreadable"}
    for number, record in sorted(stored.items()):
        contest = weekly[number - 1] if 1 <= number <= len(weekly) else None
        winner = contest.winner() if contest else None
        if winner is None or str(winner.author_id) != str(record.get("user_id")):
            return {"status": "mismatch", "number": number,
                    "stored": record.get("user_display_name") or record.get("user_id"),
                    "week": contest.contest_id if contest else None}
    awards, unnamed = [], 0
    for contest_id in written:
        contest = hall.by_id.get(contest_id)
        if contest is None or contest_id not in number_of:
            continue
        winner = contest.winner()
        if winner is None:
            continue
        if winner.author_id is None:
            unnamed += 1
            continue
        awards.append((number_of[contest_id], winner.author_id, winner.author_name))
    statuses = stats.record_weekly_contest_winners(entry, awards, giver_id, giver_name) if awards else {}
    return {
        "status": "ok",
        "awarded": sorted(week for week, status in statuses.items() if status == "awarded"),
        "numbers": {contest_id: number_of[contest_id] for contest_id in written
                    if statuses.get(number_of.get(contest_id)) == "awarded"},
        "unnamed": unnamed,
        "next": len(weekly) + 1,
    }


def apply_plan(entry: str, *, giver_id=0, giver_name: str = "", log=print) -> dict:
    """Writes every ready contest of the saved plan and brings them into the Hall of
    Fame; returns {"written", "works", "hall", "winners"}, or {"error"} when there is no
    plan. Disk and image work: run it in a worker thread."""
    import hall_of_fame

    plan = load_plan(entry)
    if plan is None:
        return {"error": "no plan"}
    known, _ = known_contests(entry)
    source = cache_dir(entry)
    written, works, summaries = [], 0, []
    now = datetime.now(timezone.utc).isoformat()
    for raw in plan.get("contests") or []:
        contest = PlannedContest.from_dict(raw)
        if contest.status != READY or contest.poll_id in known or not contest.works:
            continue
        target = voting.archived_media_path(entry, contest.poll_id)
        standings = []
        for work in contest.works:
            work.entry.media = _place_photos(work.entry.media, source, target)
            standings.append((work.entry, work.votes))
        path = voting.save_imported_results(
            entry, contest.poll_id, standings, created_at=_utc(contest.created_at),
            announced_at=_utc(contest.closed_at), voters=contest.voters, hashtag=contest.hashtag,
            title=contest.title, extra={
                "source": SOURCE, "poll_message_ids": contest.poll_message_ids, "imported_at": now,
                "matched_by": [work.how for work in contest.works],
            },
        )
        if path is not None:
            written.append(contest.poll_id)
            works += len(standings)
            top = contest.works[0]
            summaries.append({
                "poll_id": contest.poll_id, "label": contest.label(), "date": contest.created_at,
                "polls": len(contest.poll_message_ids), "works": len(standings),
                "winner": _who(top.entry) if top.votes > 0 else None, "votes": top.votes,
            })
    if not written:
        return {"written": [], "works": 0, "contests": [], "hall": None, "winners": {"status": "none"}}
    hall = hall_of_fame.import_history(entry)
    winners = _weekly_winners(entry, written, giver_id, giver_name)
    log(f"[legacy] wrote {len(written)} contest(s), {works} work(s); hall {hall}; winners {winners}")
    return {"written": written, "works": works, "contests": summaries, "hall": hall, "winners": winners}


def apply_report(result: dict) -> str:
    """The reply to an apply, in one message."""
    if result.get("error"):
        return "Сначала найди конкурсы: /hall опросы — запишу то, что покажу."
    written = result.get("written") or []
    if not written:
        return "Нечего записывать: все найденные конкурсы уже на Доске почёта."
    winners = result.get("winners") or {}
    numbers = winners.get("numbers") or {}
    lines = [f"Готово. Записано конкурсов: {len(written)}, работ: {result.get('works', 0)}.", "", "Победители:"]
    for contest in result.get("contests") or []:
        head = f"{contest['poll_id']} · {_short_date(contest['date'])}"
        if contest["label"] != voting.WEEKLY_CONTEST_TITLE:
            head += f" · «{contest['label']}»"
        if contest["polls"] > 1:
            head += f" ({_count(contest['polls'], 'опрос', 'опроса', 'опросов')}, " \
                    f"{_count(contest['works'], 'работа', 'работы', 'работ')})"
        who = (f"🥇 {contest['winner']} — {voting.votes_label(contest['votes'])}"
               if contest["winner"] else "голосов никто не набрал")
        number = f" · №{numbers[contest['poll_id']]}" if contest["poll_id"] in numbers else ""
        lines.append(f"• {head} — {who}{number}")
    lines.append("")
    if winners.get("status") == "ok":
        awarded = winners.get("awarded") or []
        if awarded:
            lines.append("Победители недель (/weekwinner): записаны №" + ", №".join(map(str, awarded))
                         + f". Следующая неделя — №{winners.get('next')}.")
        if winners.get("unnamed"):
            lines.append(f"Победителей без аккаунта (не узнал, кто это): {winners['unnamed']} — "
                         "их в таблицу победителей не вписать.")
    elif winners.get("status") == "mismatch":
        lines.append(
            f"Таблицу победителей недель (/weekwinner) не трогал: там №{winners.get('number')} — "
            f"{winners.get('stored')}, а по порядку недель №{winners.get('number')} — "
            f"{winners.get('week') or 'другой конкурс'}. Победителей старых недель проставь вручную."
        )
    elif winners.get("status") == "unreadable":
        lines.append("Таблица победителей недель не читается — её не трогал.")
    return "\n".join(lines)


# --------------------------------------------------------------------- photos come back


async def download_option_photo(client, entity, message_id: int, index: int, directory: Path,
                                log=print) -> list[str] | None:
    """The picture of option `index` of the poll in message `message_id`, downloaded into
    `directory` under the name the scan gave it -- how the Hall of Fame gets back a work
    that was a poll option, the way voting.download_post_photos does for a post. None
    when the poll is gone; [] when the option has no picture."""
    message = await client.get_messages(entity, ids=int(message_id))
    poll = getattr(getattr(message, "media", None), "poll", None)
    if message is None or poll is None:
        return None
    answers = list(getattr(poll, "answers", None) or [])
    if not 0 <= index < len(answers) or not _is_picture(getattr(answers[index], "media", None)):
        return []
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{message_id}_o{index}.jpg"
    return [name] if await _download(client, answers[index].media, directory / name, log) else []
