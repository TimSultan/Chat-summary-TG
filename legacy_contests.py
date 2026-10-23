"""The contests from before the bot ran them: the polls in the chat.

Until /vote (2 August 2026) the weekly contest was run by hand: works were posted with
#итогинедели, and a Telegram poll -- often two or three, a field too big for one -- asked
which was best, the works' pictures on its options. None of that reached a record the bot
keeps, so the Hall of Fame, the results records and the numbered weekly winners all began
where the automation did. This module brings those weeks in. The administrator sees what
it found before anything is written, names whoever it could not recognise, and can take
the whole import back out ("/hall опросы" in bot_listener).

SCAN (scan_chat, the only step that talks to Telegram).
  * Telegram's poll filter lists every poll the chat had before the bot's first week --
    one cheap listing, not a read of the whole history. The same poll can be in the chat
    more than once (the channel's post forwarded into its discussion group, and again by
    hand); copies share Telegram's poll id and are read once.
  * A poll is a contest when its options are works: pictures on them, bare numbers ("6",
    "№2") standing for the pictures posted just before it, or authors named by a mention,
    an @tag or a t.me link. A poll whose options are sentences ("Да, делай промежуточные")
    is a question to the chat, whatever its question says. A second part posted by the same
    author within SPLIT_POLL_GAP of a contest poll is part of it.
  * For each poll, the messages its author posted in the two days before it are read too:
    the album a numbered poll's options stand for, the captions crediting the works, and
    any numbered list of authors ("6. @user").
  * Every #итогинедели post of the weeks around the polls is downloaded (Telegram's search,
    each album whole): a post is what a work is everywhere else in the bot -- its author,
    its caption, its whole album, its link. A post by a CHANNEL, or by the poll's own
    author, is a repost: the work in it belongs to whoever its caption credits, never to
    the channel or the organiser.
  * Usernames nobody in the chat statistics has (an artist's t.me link) are looked up.

PLAN (plan_contests, pure). Each option is matched to a person, strongest evidence first:
  1. a mention, an @tag or a t.me link in the option itself;
  2. its picture is one of the artists' posts (the very photo, or a 256-bit difference
     hash close enough and clearly closer than any other post);
  3. its picture is one of the reposts, whose caption -- or the album's numbered list --
     credits the author;
  4. whoever added the option to an open poll (not the organiser, who adds three or more),
     or whose photo the album forwarded;
  5. a numbered option's line in the organiser's numbered list of authors;
  6. its text is the name of a post's author that week, or of somebody /stat knows -- an
     ambiguous name ("Аня" when there are two) is nobody.
A matched work is its post when there is one; otherwise it is the option, with the
option's picture. An option nothing matches leaves its week INCOMPLETE: the week is not
written until an administrator names the author (set_author) -- the bot sends the
picture and the answer is an @tag or a name. A week whose results Telegram does not show
this account (open, and not voted in) is HIDDEN until the poll is stopped.

APPLY (apply_plan). Each complete week is written the way every finished week is: its
photos into the voting archive, where a cleared week's photos live; a results record
beside the announced weeks' (voting.save_imported_results, marked SOURCE); then the Hall
of Fame imports it like any other week. Nothing that exists is overwritten, so running it
again is safe. The weekly contest's winners also go into /weekwinner's numbered table
(reputation and the 🏆 in /stat), but only while that table's numbering agrees with the
order of the weeks. undo_import takes all of it back out.

Storage: the plan and the downloaded pictures under DATA_DIR/voting/legacy/<chat key>/,
a subdirectory nothing that lists polls reads. Everything here that touches the disk or
decodes an image is synchronous and runs in a worker thread; the coroutines are the ones
that talk to Telegram.
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

# What the records an import writes say they came from.
SOURCE = "telegram_poll"
SCAN_VERSION = 2

CONTEST, PLAIN = "contest", "plain"   # see poll_kind

READY = "ready"              # every author known: will be written
INCOMPLETE = "incomplete"    # some authors unknown: waits for an administrator
KNOWN = "known"              # the hall or the results already have this week
HIDDEN = "hidden"            # Telegram does not show this account the poll's results
AUTOMATED = "automated"      # the week is the bot's own, or later

# A question naming the contest makes a poll a candidate even without pictures on its
# options -- whether it is one is then up to its options (see is_work_poll).
CONTEST_QUESTION = re.compile(
    r"итоги\s+недели|#итогинедели|конкурс|лучш\w*\s+работ|голосуем|голосование\s+за", re.IGNORECASE,
)
MIN_OPTIONS = 2
MIN_PICTURED_OPTIONS = 2
# How long before its poll a work may have been posted -- the contest week and the one
# before it, the two weeks /vote collects -- and how long after: an open poll takes new
# options for a while after it is posted.
POST_LOOKBACK = timedelta(days=14)
POST_LOOKAHEAD = timedelta(days=2)
# The organiser's own messages before a poll that are read with it: its album, its
# captions, its list of authors.
CONTEXT_LOOKBACK = timedelta(hours=48)
CONTEXT_SCAN_MESSAGES = 400
CONTEXT_KEEP = 80
# A 256-bit difference hash: at most this many bits may differ for two pictures to be the
# same work (the same photo recompressed differs by a handful; two unrelated photos by
# about half), and the best match must beat the second best by MATCH_MARGIN -- a dark
# figure on a dark background resembles many others, and a near tie is no match.
HASH_SIDE = 16
HASH_MATCH_BITS = 40
MATCH_MARGIN = 12
# A poll that names no contest is still one when its author posted it this close to a
# contest poll: the second part of a field too big for one poll.
SPLIT_POLL_GAP = timedelta(hours=2)
# Somebody who added this many options to one open poll is its organiser, not an author.
ORGANISER_OPTIONS = 3
# A poll posted Monday to Wednesday is about the week before it (the nearest Sunday is
# behind it); Thursday to Sunday, about its own week.
LATE_WEEKDAYS = 3
# Usernames looked up per scan, at most: each is a request Telegram rate-limits.
MAX_USERNAME_LOOKUPS = 150

# What each kind of evidence is called in the preview.
HOW_MENTION = "упоминание"
HOW_USERNAME = "ник или ссылка"
HOW_PICTURE = "картинка"
HOW_CREDIT = "подпись к репосту"
HOW_ADDED = "добавил вариант"
HOW_FORWARD = "пересланное фото"
HOW_LIST = "список авторов"
HOW_NAME = "имя"
HOW_MEMBER = "участник чата"
HOW_MANUAL = "вручную"
HOW_NONE = "не найден"

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_USERNAME = re.compile(r"(?<![\w@/])@([A-Za-z][A-Za-z0-9_]{3,31})\b")
_LINK = re.compile(r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/([A-Za-z][A-Za-z0-9_]{3,31})\b",
                   re.IGNORECASE)
_LINK_ANY = re.compile(r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/\S*", re.IGNORECASE)
_NOT_USERNAMES = frozenset({"joinchat", "addstickers", "share", "proxy", "socks", "iv", "c", "s", "addlist"})
_NUMBERED = re.compile(r"^\s*(?:#|работа|вариант|участник)?\s*(\d{1,2})\s*[.)]?\s*$", re.IGNORECASE)
_LEADING_NUMBER = re.compile(r"^\s*(?:#\s*)?\d{1,2}\s*[.)\-–—:]+\s*|^\s*(?:#\s*)?\d{1,2}\s+(?=\D)")
_CREDIT_LINE = re.compile(r"^\s*(?:#\s*)?(\d{1,2})\s*[.)\-–—:]\s*(\S.*)$")
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


def undone_dir(entry: str) -> Path:
    """Where undo_import puts what it takes out -- moved, not deleted."""
    return legacy_dir(entry) / "undone"


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


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


def usernames_in(text: str) -> list[str]:
    """Every @tag and t.me/<name> link in `text`, in order, once each."""
    found = [m.group(1) for m in _USERNAME.finditer(text or "")]
    found += [m.group(1) for m in _LINK.finditer(text or "") if m.group(1).lower() not in _NOT_USERNAMES]
    return list(dict.fromkeys(found))


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
    """The option without its numbering or links: "1. Аня — дракон" -> "Аня — дракон"."""
    text = _LINK_ANY.sub(" ", _plain(text))
    return " ".join(_LEADING_NUMBER.sub("", text).split())


def credit_lines(text: str) -> dict[int, str]:
    """A numbered list of authors -- "1. @anya", "2) Боря — дракон" -- as {number: line}."""
    lines = {}
    for line in _plain(text).splitlines():
        match = _CREDIT_LINE.match(line)
        if match:
            lines.setdefault(int(match.group(1)), match.group(2).strip())
    return lines


def _name_key(text: str) -> str:
    text = _LINK_ANY.sub(" ", _plain(text)).casefold().replace("ё", "е")
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
    """A 256-bit difference hash: the picture as 17x16 greys, one bit per neighbouring
    pair. It survives recompression and resizing, which is all Telegram does to a photo
    posted twice. None when the file is not a readable picture."""
    try:
        from PIL import Image, ImageOps

        with Image.open(path) as opened:
            image = (ImageOps.exif_transpose(opened) or opened).convert("L")
            image = image.resize((HASH_SIDE + 1, HASH_SIDE), Image.LANCZOS)
            pixels = list(image.tobytes())
    except Exception:  # noqa: BLE001 -- an unreadable picture just cannot be matched
        return None
    width = HASH_SIDE + 1
    bits = 0
    for row in range(HASH_SIDE):
        for column in range(HASH_SIDE):
            bits = (bits << 1) | int(pixels[row * width + column] > pixels[row * width + column + 1])
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


def _photo_id(media) -> int | None:
    photo = getattr(media, "photo", None)
    value = getattr(photo, "id", None)
    return int(value) if isinstance(value, int) else None


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


def _is_user(sender) -> bool:
    """A person, not a channel or a group posting as itself."""
    return sender is not None and getattr(sender, "title", None) is None


def _person(entity) -> dict:
    if entity is None:
        return {"name": "Unknown", "username": None, "is_user": False}
    username = getattr(entity, "username", None)
    if not _is_user(entity):
        return {"name": getattr(entity, "title", None) or (f"@{username}" if username else "канал"),
                "username": username, "is_user": False}
    name = " ".join(part for part in (getattr(entity, "first_name", None), getattr(entity, "last_name", None)) if part)
    return {"name": name or (f"@{username}" if username else f"id{getattr(entity, 'id', '')}"),
            "username": username, "is_user": True}


def _mention_ids(entities) -> list[int]:
    ids = []
    for entity in entities or []:
        user_id = getattr(entity, "user_id", None)
        if isinstance(user_id, int) and user_id not in ids:
            ids.append(user_id)
    return ids


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


def poll_kind(message) -> str | None:
    """CONTEST for a poll that may be a contest on its own (pictures on its options, or a
    question naming the contest); PLAIN for any other poll that could still be one part of
    a split contest; None for what never is (a quiz, a single option)."""
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


def unique_polls(messages: list) -> tuple[list, int]:
    """Each poll once, at its first appearance -- a forward of a poll is the same poll
    (Telegram's poll id, the same votes), and counting it twice doubles a week's field.
    Returns (the polls, how many copies were dropped)."""
    first: dict[str, object] = {}
    for message in sorted(messages, key=lambda m: m.id):
        poll = getattr(getattr(message, "media", None), "poll", None)
        key = str(getattr(poll, "id", None) or f"message-{message.id}")
        first.setdefault(key, message)
    kept = sorted(first.values(), key=lambda m: m.id)
    return kept, len(messages) - len(kept)


def contest_polls(messages: list) -> list:
    """The candidate polls among `messages`: every one that may be a contest on its own,
    and every other poll its author posted within SPLIT_POLL_GAP of one -- the second
    part of a field too big for one poll, whose question ("Часть 2") need not name the
    contest. Oldest first. Whether each IS a contest is up to its options (is_work_poll)."""
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


def is_work_poll(poll: dict) -> bool:
    """Whether a read poll's options are works: at least two of them, and at least half,
    carry a picture, a number standing for one, or an author's mention, @tag or link. A
    poll of sentences is a question to the chat."""
    options = poll.get("options") or []
    worklike = sum(1 for o in options if o.get("photo") or o.get("number") or o.get("mentions") or o.get("usernames"))
    return worklike >= 2 and worklike * 2 >= len(options)


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
    except Exception as e:  # one picture lost costs that picture, not the poll
        log(f"[legacy] could not download {path.name}: {e}")
        return False
    return path.exists()


async def _context(client, entity, poll_message, directory: Path, log) -> list[dict]:
    """The poll author's own messages in the two days before it, newest first, their
    photos downloaded: the album a numbered poll stands for, the captions crediting each
    work, a numbered list of authors. Messages from anybody else are passed over."""
    sender = getattr(poll_message, "sender_id", None)
    earliest = poll_message.date - CONTEXT_LOOKBACK
    items = []
    try:
        async for message in client.iter_messages(entity, offset_id=poll_message.id, limit=CONTEXT_SCAN_MESSAGES):
            if message.date < earliest:
                break
            if sender is None or getattr(message, "sender_id", None) != sender:
                continue
            forward = getattr(message, "fwd_from", None)
            item = {
                "message_id": message.id,
                "grouped_id": getattr(message, "grouped_id", None),
                "text": str(getattr(message, "text", "") or ""),
                "mentions": _mention_ids(getattr(message, "entities", None)),
                "forwarded_from": _user_id(getattr(forward, "from_id", None)),
                "forwarded_name": str(getattr(forward, "from_name", "") or ""),
                "is_poll": getattr(getattr(message, "media", None), "poll", None) is not None,
                "photo": None,
                "photo_id": None,
            }
            if getattr(message, "photo", None):
                name = f"c{message.id}.jpg"
                if await _download(client, message, directory / name, log):
                    item["photo"] = name
                    item["photo_id"] = _photo_id(message)
            items.append(item)
            if len(items) >= CONTEXT_KEEP:
                break
    except Exception as e:
        log(f"[legacy] could not read the messages before poll {poll_message.id}: {e}")
    return items


def _album_before(context: list[dict], numbers: list[int]) -> list[dict]:
    """The pictures a numbered poll's options stand for, oldest first: the author's photos
    from the newest back to the first message of theirs that is not one (a "голосуем!"
    right before the album is passed over).

    The second part of a split poll may come after the first with every album posted
    before both: "album 1, album 2, poll 1, poll 2". Then the photos run on past the poll
    it passed, and when its numbering starts again at 1 its pictures are the album that
    many places along -- the second album for the second poll."""
    block, passed = [], 0
    for item in context:
        if item["photo"]:
            block.append(item)
        elif block:
            break
        elif item["is_poll"]:
            passed += 1
    block.reverse()
    if passed and numbers and min(numbers) == 1:
        runs: list[list[dict]] = []
        for item in block:
            if runs and item["grouped_id"] is not None and runs[-1][-1]["grouped_id"] == item["grouped_id"]:
                runs[-1].append(item)
            else:
                runs.append([item])
        if len(runs) > passed:
            return [item for run in runs[passed:] for item in run]
    return block


def _reposts(context: list[dict]) -> list[dict]:
    """The poll author's photos before it, as works someone else made: each with its own
    caption, its album's caption and its place in that album -- what credits it."""
    albums: dict[object, list[dict]] = {}
    for item in context:
        if item["photo"]:
            albums.setdefault(item["grouped_id"] or ("single", item["message_id"]), []).append(item)
    reposts = []
    for items in albums.values():
        items.sort(key=lambda i: i["message_id"])
        album_caption = next((i["text"] for i in items if i["text"]), "")
        for index, item in enumerate(items, start=1):
            reposts.append({
                "name": item["photo"], "photo_id": item["photo_id"], "message_id": item["message_id"],
                "caption": item["text"], "mentions": item["mentions"],
                "forwarded_from": item["forwarded_from"], "forwarded_name": item["forwarded_name"],
                "album_caption": album_caption, "album_index": index,
            })
    return reposts


async def _read_poll(client, entity, message, tz, directory: Path, log) -> dict:
    """One candidate poll as a plain dict: its options, their pictures downloaded, and
    what its author posted before it."""
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
        photo, photo_id = None, None
        answer_media = getattr(answer, "media", None)
        if _is_picture(answer_media):
            name = f"{message.id}_o{index}.jpg"
            if await _download(client, answer_media, directory / name, log):
                photo, photo_id = name, _photo_id(answer_media)
        added_at = getattr(answer, "date", None)
        options.append({
            "index": index,
            "text": text,
            "number": option_number(text),
            "votes": None if counts is None else counts.get(_option_key(getattr(answer, "option", b"")), 0),
            "photo": photo,
            "photo_id": photo_id,
            "mentions": _mention_ids(getattr(text_value, "entities", None)),
            "usernames": usernames_in(text),
            "added_by": _user_id(getattr(answer, "added_by", None)),
            "added_at": added_at.astimezone(tz).isoformat() if isinstance(added_at, datetime) else "",
            "caption": "",
            "caption_mentions": [],
            "forwarded_from": None,
        })

    context = await _context(client, entity, message, directory, log)
    # A numbered poll: its options are "1", "2", ... and the pictures are the album before it.
    numbers = [o["number"] for o in options if o["photo"] is None and o["number"]]
    if numbers:
        album = _album_before(context, numbers)
        offset = album_offset(numbers, len(album))
        for option in options:
            number = option["number"]
            if option["photo"] is None and number and 1 <= number - offset <= len(album):
                picture = album[number - offset - 1]
                caption = " ".join(part for part in (picture["text"], picture["forwarded_name"]) if part)
                option.update(photo=picture["photo"], photo_id=picture["photo_id"], caption=caption,
                              caption_mentions=picture["mentions"], forwarded_from=picture["forwarded_from"])
    # The organiser's numbered list of authors, if there is one: the newest of their
    # messages before the poll that holds two or more numbered lines.
    credits = next((lines for item in context if len(lines := credit_lines(item["text"])) >= 2), {})

    sender = None
    try:
        sender = await message.get_sender()
    except Exception:
        pass
    closed = bool(getattr(poll, "closed", False))
    close_date = getattr(poll, "close_date", None)
    total = getattr(results, "total_voters", None)
    return {
        "message_id": message.id,
        "poll_id": str(getattr(poll, "id", "") or ""),
        "posted_at": message.date.astimezone(tz).isoformat(),
        "question": question,
        "sender_id": getattr(message, "sender_id", None),
        "sender_name": _person(sender)["name"] if sender is not None else "",
        "sender_is_user": _is_user(sender),
        "closed": closed,
        "closed_at": (close_date.astimezone(tz).isoformat()
                      if closed and isinstance(close_date, datetime) else ""),
        "total_voters": int(total) if isinstance(total, int) else None,
        "visible": counts is not None,
        "options": options,
        "credits": {str(k): v for k, v in credits.items()},
        "reposts": _reposts(context),
    }


async def _find_posts(client, entity, tz, directory: Path, since: datetime, until: datetime,
                      hashtag: str, say, log) -> list[dict]:
    """Every #итогинедели post between `since` and `until`, each album whole, its photos
    downloaded, with who sent it and every photo's own caption. Telegram's search first;
    when that finds nothing, the window read message by message (its index can miss a
    tag)."""
    since_utc, until_utc = since.astimezone(timezone.utc), until.astimezone(timezone.utc)
    found: dict[int, object] = {}
    async for message in client.iter_messages(entity, search=hashtag, offset_date=until_utc):
        if message.date < since_utc:
            break
        if message.date < until_utc and getattr(message, "action", None) is None:
            found[message.id] = message
    if not found:
        await say(f"Поиск не нашёл {hashtag} — читаю чат подряд, это дольше…")
        async for message in client.iter_messages(entity, offset_date=until_utc):
            if message.date < since_utc:
                break
            if getattr(message, "action", None) is None and voting._has_hashtag(getattr(message, "text", "") or "", hashtag):
                found[message.id] = message
    albums = {m.grouped_id for m in found.values() if getattr(m, "grouped_id", None)}
    for hit in [m for m in found.values() if getattr(m, "grouped_id", None)]:
        ids = [i for i in range(hit.id - voting.MAX_ALBUM_ITEMS + 1, hit.id + voting.MAX_ALBUM_ITEMS) if i not in found]
        try:
            around = await client.get_messages(entity, ids=ids)
        except Exception as e:  # the captioned photo is still a work on its own
            log(f"[legacy] could not read the album around {hit.id}: {e}")
            continue
        for message in around or []:
            if message is not None and getattr(message, "grouped_id", None) in albums:
                found[message.id] = message

    groups = voting.group_into_entries(list(found.values()), hashtag)
    posts = []
    for position, group in enumerate(groups, start=1):
        if position % 5 == 1:
            await say(f"Скачиваю работы с {hashtag}: {position} из {len(groups)}…")
        head = group[0]
        try:
            sender = await head.get_sender()
        except Exception:
            sender = None
        person = _person(sender)
        photos = []
        for index, message in enumerate(group):
            if not getattr(message, "photo", None):
                continue
            name = f"{head.id}_{index}.jpg"
            if await _download(client, message, directory / name, log):
                photos.append({
                    "name": name, "photo_id": _photo_id(message), "message_id": message.id, "index": len(photos) + 1,
                    "caption": str(getattr(message, "text", "") or ""),
                    "mentions": _mention_ids(getattr(message, "entities", None)),
                })
        if not photos:
            continue
        entry = voting.Entry(
            entry_id=str(head.id), message_id=head.id, author_id=getattr(sender, "id", None),
            author_name=person["name"], author_username=person["username"],
            text=voting._clean_caption(group, hashtag), media=[p["name"] for p in photos],
            posted_at=head.date.astimezone(tz).isoformat(),
        )
        posts.append({
            "entry": entry.to_dict(), "sender_id": getattr(head, "sender_id", None),
            "is_user": person["is_user"], "photos": photos,
            "caption": next((p["caption"] for p in photos if p["caption"]), ""),
        })
    log(f"[legacy] {hashtag} {since.date()}..{until.date()}: {len(found)} message(s), {len(posts)} post(s)")
    return posts


async def _resolve_usernames(client, wanted: list[str], known: set[str], log) -> dict[str, dict]:
    """{username, lower-cased: person} for every username in `wanted` nobody in `known`
    has -- an artist's link to their own channel, a handle nobody in /stat uses. At most
    MAX_USERNAME_LOOKUPS, and none after Telegram asks to wait."""
    resolved = {}
    for username in dict.fromkeys(u.lower() for u in wanted):
        if username in known or username in resolved:
            continue
        if len(resolved) >= MAX_USERNAME_LOOKUPS:
            break
        try:
            entity = await client.get_entity(username)
        except Exception as e:
            if "wait" in type(e).__name__.lower() or "flood" in str(e).lower():
                log(f"[legacy] Telegram asks to wait; stopped looking up usernames: {e}")
                break
            resolved[username] = None
            continue
        person = _person(entity)
        person["id"] = getattr(entity, "id", None) if person["is_user"] else None
        resolved[username] = person
    return {k: v for k, v in resolved.items() if v is not None}


async def scan_chat(
    client, entity, tz, directory: Path, *, until: datetime | None = None,
    hashtag: str = voting.CONTEST_HASHTAG, members: dict | None = None, progress=None, log=print,
) -> dict:
    """Everything the plan is made from, as plain JSON. `until` (timezone-aware) stops the
    listing at the automation: a poll posted after it is the bot's era and not read at
    all. `members` ({user id: {"name", "username"}}) saves looking up usernames the chat
    statistics already know. Pictures go into `directory`."""
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
    await say("Ищу опросы в чате…")
    listed = []
    async for message in client.iter_messages(entity, **listing):
        if getattr(getattr(message, "media", None), "poll", None) is None:
            continue
        if until is not None and message.date >= until:
            continue
        listed.append(message)
    distinct, copies = unique_polls(listed)
    candidates = contest_polls(distinct)
    polls, questions = [], 0
    for position, message in enumerate(candidates, start=1):
        await say(f"Читаю опросы: {position} из {len(candidates)}, качаю картинки…")
        read = await _read_poll(client, entity, message, tz, directory, log)
        if is_work_poll(read):
            polls.append(read)
        else:
            questions += 1

    posts: list[dict] = []
    if polls:
        since = min(datetime.fromisoformat(p["posted_at"]) for p in polls) - POST_LOOKBACK
        till = max(datetime.fromisoformat(p["posted_at"]) for p in polls) + POST_LOOKAHEAD
        if until is not None:
            till = min(till, until.astimezone(tz))
        await say(f"Ищу посты с {hashtag}…")
        posts = await _find_posts(client, entity, tz, directory, since, till, hashtag, say, log)

    wanted = [u for p in polls for o in p["options"] for u in o["usernames"] + usernames_in(o["caption"])]
    wanted += [u for p in polls for line in p["credits"].values() for u in usernames_in(line)]
    wanted += [u for p in polls for r in p["reposts"] for u in usernames_in(r["caption"] + "\n" + r["album_caption"])]
    wanted += [u for post in posts if not post["is_user"] for u in usernames_in(post["caption"])]
    known = {(m.get("username") or "").lower() for m in (members or {}).values() if m.get("username")}
    known |= {(post["entry"].get("author_username") or "").lower() for post in posts if post["is_user"]}
    if wanted:
        await say("Узнаю, чьи это ники и ссылки…")
    usernames = await _resolve_usernames(client, wanted, known - {""}, log)

    people: dict[str, dict] = {}
    ids = {o["added_by"] for p in polls for o in p["options"]} | {o["forwarded_from"] for p in polls for o in p["options"]}
    ids |= {u for p in polls for o in p["options"] for u in o["mentions"] + o["caption_mentions"]}
    ids |= {u for p in polls for r in p["reposts"] for u in r["mentions"] + [r["forwarded_from"]]}
    ids |= {u for post in posts for photo in post["photos"] for u in photo["mentions"]}
    for user_id in ids:
        if user_id is None or str(user_id) in (members or {}):
            continue
        try:
            people[str(user_id)] = _person(await client.get_entity(user_id))
        except Exception:
            continue  # named later from the posts or the chat statistics, if at all
    log(f"[legacy] {len(listed)} poll(s) listed, {copies} copies, {len(polls)} contest poll(s), "
        f"{questions} question poll(s), {len(posts)} post(s), {len(usernames)} username(s) looked up")
    return {
        "version": SCAN_VERSION,
        "polls_seen": len(listed),
        "copies": copies,
        "questions": questions,
        "polls": polls,
        "posts": posts,
        "people": people,
        "usernames": usernames,
    }


# ------------------------------------------------------------------------------- the plan


@dataclass
class PlannedWork:
    entry: voting.Entry
    votes: int
    how: str
    option_text: str = ""
    key: str = ""              # "<poll message id>.<option index>": names the option
    photo: str | None = None   # the option's picture (its own, or its album's), for asking

    @property
    def known(self) -> bool:
        return self.how != HOW_NONE

    def to_dict(self) -> dict:
        return {"entry": self.entry.to_dict(), "votes": self.votes, "how": self.how,
                "option_text": self.option_text, "key": self.key, "photo": self.photo}

    @classmethod
    def from_dict(cls, raw: dict) -> "PlannedWork":
        return cls(entry=voting.Entry.from_dict(raw.get("entry") or {}), votes=int(raw.get("votes") or 0),
                   how=str(raw.get("how") or HOW_NONE), option_text=str(raw.get("option_text") or ""),
                   key=str(raw.get("key") or ""), photo=raw.get("photo") or None)


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

    def unknown(self) -> list[PlannedWork]:
        return [work for work in self.works if not work.known]

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


class _People:
    """Who a username, an id or a name is, from everything the scan and /stat know."""

    def __init__(self, artist_posts: list[voting.Entry], people: dict, members: dict, usernames: dict):
        self.posts = artist_posts
        self.people = people
        self.members = members
        self.usernames = usernames
        self.by_username = {}
        for uid, member in members.items():
            if member.get("username") and _as_id(uid) is not None:
                self.by_username.setdefault(member["username"].lower(), (_as_id(uid), member.get("name"), member["username"]))
        for post in artist_posts:
            if post.author_username and post.author_id is not None:
                self.by_username[post.author_username.lower()] = (post.author_id, post.author_name, post.author_username)

    def named(self, user_id) -> tuple[str, str | None]:
        post = next((p for p in self.posts if p.author_id == user_id), None)
        if post is not None:
            return post.author_name, post.author_username
        known = self.members.get(str(user_id)) or self.people.get(str(user_id))
        if known:
            return known.get("name") or f"id{user_id}", known.get("username")
        return f"id{user_id}", None

    def username(self, username: str) -> tuple[int | None, str, str | None] | None:
        """(id, name, username) of an @tag or a t.me link: a member or an artist, a user
        looked up, or a channel -- an artist's own, whose title is the best name there is
        unless it names a member."""
        found = self.by_username.get(username.lower())
        if found is not None:
            return found
        person = self.usernames.get(username.lower())
        if person is None:
            return None
        if person.get("is_user") and person.get("id") is not None:
            return int(person["id"]), person.get("name") or f"@{username}", person.get("username") or username
        member = self.member_named(person.get("name") or "")
        if member is not None:
            return member
        return None, person.get("name") or f"@{username}", person.get("username") or username

    def member_named(self, text: str, minimum: int = 2):
        scored = [(name_score(text, m.get("name") or "", m.get("username")), uid, m)
                  for uid, m in self.members.items() if _as_id(uid) is not None]
        found = _unique_best(scored, minimum)
        if found is None:
            return None
        return _as_id(found[1]), found[2].get("name") or f"id{found[1]}", found[2].get("username")

    def artist_named(self, text: str, minimum: int = 1):
        authors = {}
        for post in self.posts:
            authors.setdefault(post.author_id if post.author_id is not None else post.author_name, post)
        scored = [(name_score(text, post.author_name, post.author_username), identity, post)
                  for identity, post in authors.items()]
        found = _unique_best(scored, minimum)
        if found is None:
            return None
        return found[2].author_id, found[2].author_name, found[2].author_username

    def credit(self, text: str, mentions: list[int] = (), forwarded_from=None):
        """Who a caption or a list line credits: a mention, an @tag or a link, a forward's
        sender, a name an artist or a member goes by. None when it names nobody."""
        for user_id in mentions or []:
            return (user_id, *self.named(user_id))
        for username in usernames_in(text):
            found = self.username(username)
            if found is not None:
                return found
        if forwarded_from is not None:
            return (forwarded_from, *self.named(forwarded_from))
        if clean_option_text(text):
            return self.artist_named(text, 2) or self.member_named(text, 2)
        return None


def _picture_matches(options: list[tuple[dict, dict]], candidates: list[tuple[object, str, int | None]],
                     hashes: dict, identity) -> dict[int, object]:
    """{option position: candidate} for every option whose picture is clearly one of the
    candidates' -- the very photo (same Telegram photo id), or the nearest by hash, close
    enough and clearly nearer than any candidate of another identity (`identity(unit)`:
    the same artist posting a work twice is no rival to themselves). Each candidate goes
    to one option, the closest pairs first."""
    pairs = []
    for position, (_, option) in enumerate(options):
        mine, mine_id = hashes.get(option.get("photo") or ""), option.get("photo_id")
        distances: dict[int, tuple[int, object]] = {}
        for unit, name, photo_id in candidates:
            if mine_id is not None and photo_id == mine_id:
                distance = 0
            elif mine is not None and name in hashes:
                distance = _distance(mine, hashes[name])
            else:
                continue
            if id(unit) not in distances or distance < distances[id(unit)][0]:
                distances[id(unit)] = (distance, unit)
        ranked = sorted(distances.values(), key=lambda pair: pair[0])
        if not ranked or ranked[0][0] > HASH_MATCH_BITS:
            continue
        best, best_who = ranked[0], identity(ranked[0][1])
        rival = next((pair for pair in ranked[1:] if best_who is None or identity(pair[1]) != best_who), None)
        if best_who is not None and rival is not None and best[0] > 0 and rival[0] - best[0] < MATCH_MARGIN:
            continue
        pairs.append((best[0], position, best[1]))
    matched: dict[int, object] = {}
    taken: set[int] = set()
    for _, position, unit in sorted(pairs, key=lambda pair: (pair[0], pair[1])):
        if position not in matched and id(unit) not in taken:
            matched[position] = unit
            taken.add(id(unit))
    return matched


def _plan_works(polls: list[dict], artist_posts: list[voting.Entry], reposts: list[dict],
                who: _People, hashes: dict, photo_ids: dict) -> list[PlannedWork]:
    options = [(poll, option) for poll in polls for option in poll["options"]]
    # The artists' own posts first; a repost of the same photo is no rival to its original.
    by_picture = _picture_matches(
        options, [(post, name, photo_ids.get(name)) for post in artist_posts for name in post.media],
        hashes, lambda post: post.author_id if post.author_id is not None else post.author_name,
    )
    rest = [(poll, option) for position, (poll, option) in enumerate(options) if position not in by_picture]
    positions = [position for position in range(len(options)) if position not in by_picture]
    reposted = _picture_matches(rest, [(r, r["name"], r.get("photo_id")) for r in reposts], hashes, lambda r: None)
    by_picture.update({positions[i]: unit for i, unit in reposted.items()})

    adders: dict[tuple[int, int], int] = {}
    for poll, option in options:
        if option.get("added_by") is not None:
            key = (poll["message_id"], option["added_by"])
            adders[key] = adders.get(key, 0) + 1

    works: list[PlannedWork] = []
    used = {id(unit) for unit in by_picture.values() if isinstance(unit, voting.Entry)}
    for position, (poll, option) in enumerate(options):
        text = option.get("text") or ""
        author, how = None, HOW_NONE      # author: (id or None, name, username)
        post = None
        matched = by_picture.get(position)

        if option.get("mentions"):
            user_id = option["mentions"][0]
            author, how = (user_id, *who.named(user_id)), HOW_MENTION
        if author is None:
            for username in option.get("usernames") or []:
                author = who.username(username)
                if author is not None:
                    how = HOW_USERNAME
                    break
        if author is None and isinstance(matched, voting.Entry):
            post, how = matched, HOW_PICTURE
            author = (post.author_id, post.author_name, post.author_username)
        if author is None and isinstance(matched, dict):
            author = (who.credit(matched["caption"], matched["mentions"], matched["forwarded_from"])
                      or who.credit(credit_lines(matched["album_caption"]).get(matched["album_index"], ""))
                      or (who.credit(matched["forwarded_name"]) if matched.get("forwarded_name") else None))
            if author is not None:
                how = HOW_CREDIT
        if author is None:
            adder = option.get("added_by")
            if adder is not None and adders.get((poll["message_id"], adder), 0) < ORGANISER_OPTIONS:
                author, how = (adder, *who.named(adder)), HOW_ADDED
            elif option.get("forwarded_from") is not None:
                author, how = (option["forwarded_from"], *who.named(option["forwarded_from"])), HOW_FORWARD
            elif option.get("caption") or option.get("caption_mentions"):
                author = who.credit(option.get("caption") or "", option.get("caption_mentions") or [])
                if author is not None:
                    how = HOW_CREDIT
        if author is None and option.get("number"):
            line = (poll.get("credits") or {}).get(str(option["number"]))
            if line:
                author = who.credit(line)
                if author is not None:
                    how = HOW_LIST
        if author is None and not option.get("number") and clean_option_text(text):
            author = who.artist_named(text, 1)
            how = HOW_NAME if author is not None else how
            if author is None:
                author = who.member_named(text, 2)
                how = HOW_MEMBER if author is not None else how

        # Which work it is: the matched picture's post; else, for an option with no picture
        # of its own, its author's post of the week (before the poll, newest first).
        if post is None and author is not None and author[0] is not None and not option.get("photo"):
            poll_time = _when(poll["posted_at"])
            mine = [p for p in artist_posts if id(p) not in used and p.author_id == author[0]]
            before = [p for p in mine if poll_time is None or (_when(p.posted_at) or poll_time) <= poll_time]
            post = max(before, key=lambda p: p.message_id) if before else (
                min(mine, key=lambda p: p.message_id) if mine else None)
        if post is not None:
            used.add(id(post))

        votes = int(option.get("votes") or 0)
        cleaned = clean_option_text(text)
        if post is not None:
            entry = voting.Entry.from_dict(post.to_dict())
            if author is not None and author[0] is not None and post.author_id != author[0]:
                entry.author_id, entry.author_name, entry.author_username = author
        else:
            author_id, author_name, author_username = author if author is not None else (
                None, cleaned or text.strip() or f"Вариант {option['index'] + 1}", None)
            entry = voting.Entry(
                entry_id=f"{poll['message_id']}o{option['index']}",
                message_id=int(poll["message_id"]),
                author_id=author_id, author_name=author_name, author_username=author_username,
                text="" if option.get("number") or _name_key(cleaned) == _name_key(author_name) else cleaned,
                media=[option["photo"]] if option.get("photo") else [],
                posted_at=option.get("added_at") or poll["posted_at"],
            )
        works.append(PlannedWork(entry=entry, votes=votes, how=how, option_text=text,
                                 key=f"{poll['message_id']}.{option['index']}", photo=option.get("photo")))
    # Best first; a tie keeps the polls' own order, the way a tally keeps its entries'.
    order = {id(work): position for position, work in enumerate(works)}
    works.sort(key=lambda work: (-work.votes, order[id(work)]))
    return works


def _status(contest: PlannedContest, polls: list[dict], known, first_automated_week) -> str:
    if first_automated_week and contest.week >= first_automated_week:
        return AUTOMATED
    if contest.poll_id in known:
        return KNOWN
    if not all(poll.get("visible") for poll in polls):
        return HIDDEN
    if contest.unknown():
        return INCOMPLETE
    return READY


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
    polls_all = scan.get("polls") or []
    organisers = {poll.get("sender_id") for poll in polls_all if poll.get("sender_id") is not None}
    artist_posts, repost_photos, photo_ids = [], [], {}
    for raw in scan.get("posts") or []:
        entry = voting.Entry.from_dict(raw.get("entry") or {})
        photos = raw.get("photos") or []
        if raw.get("is_user", True) and raw.get("sender_id") not in organisers:
            photo_ids.update({p["name"]: p.get("photo_id") for p in photos})
            artist_posts.append(entry)
        else:
            caption = raw.get("caption") or ""
            for photo in photos:
                repost_photos.append({
                    "name": photo["name"], "photo_id": photo.get("photo_id"), "message_id": photo.get("message_id"),
                    "caption": photo.get("caption") or "", "mentions": photo.get("mentions") or [],
                    "forwarded_from": None, "forwarded_name": "", "album_caption": caption,
                    "album_index": photo.get("index") or 1, "posted_at": entry.posted_at,
                })

    groups: dict[tuple[str, str], list[dict]] = {}
    titles: dict[tuple[str, str], str] = {}
    for poll in polls_all:
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

        def in_window(when_text: str) -> bool:
            when = _when(when_text)
            return when is not None and first - POST_LOOKBACK <= when < last + POST_LOOKAHEAD

        window = [post for post in artist_posts if in_window(post.posted_at)]
        reposts = [r for poll in polls for r in poll.get("reposts") or []]
        reposts += [r for r in repost_photos if in_window(r["posted_at"])]
        who = _People(window, scan.get("people") or {}, members, scan.get("usernames") or {})
        contest = PlannedContest(
            poll_id=voting.poll_id_for(week, tag), week=week, hashtag=tag, title=titles[(week, tag)],
            poll_message_ids=[poll["message_id"] for poll in polls],
            created_at=polls[0]["posted_at"],
            closed_at=max((poll.get("closed_at") or poll["posted_at"]) for poll in polls),
            voters=0, status="",
        )
        if not (first_automated_week and week >= first_automated_week):
            contest.works = _plan_works(polls, window, reposts, who, hashes, photo_ids)
        totals = [poll["total_voters"] for poll in polls if poll.get("total_voters") is not None]
        contest.voters = max(totals) if totals else sum(work.votes for work in contest.works)
        contest.status = _status(contest, polls, known, first_automated_week)
        contests.append(contest)
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


def _photo_names(scan: dict) -> list[str]:
    names = [o.get("photo") for p in scan.get("polls") or [] for o in p["options"]]
    names += [r["name"] for p in scan.get("polls") or [] for r in p.get("reposts") or []]
    names += [photo["name"] for post in scan.get("posts") or [] for photo in post.get("photos") or []]
    return [name for name in names if name]


def build_plan(entry: str, scan: dict, members: dict | None = None) -> list[PlannedContest]:
    """Hashes the scan's pictures, plans its contests against what is already on disk,
    and saves the plan for apply_plan. Disk and image work: run it in a worker thread.
    Authors an administrator named for an earlier plan are kept (set_author)."""
    hashes = hash_photos(cache_dir(entry), _photo_names(scan))
    known, first_automated = known_contests(entry)
    contests = plan_contests(scan, members=members, hashes=hashes, known=known,
                             first_automated_week=first_automated)
    previous = load_plan(entry) or {}
    manual = previous.get("manual") or {}
    for contest in contests:
        for work in contest.works:
            if work.key in manual:
                _apply_manual(work, manual[work.key])
        contest.status = _restatus(contest)
    _write_json(plan_path(entry), {
        "planned_at": datetime.now(timezone.utc).isoformat(),
        "polls_seen": scan.get("polls_seen", 0),
        "copies": scan.get("copies", 0),
        "questions": scan.get("questions", 0),
        "first_automated_week": first_automated,
        "members": members or {},
        "manual": manual,
        "contests": [contest.to_dict() for contest in contests],
        "scan": scan,
    })
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


def plan_contests_of(plan: dict | None) -> list[PlannedContest]:
    return [PlannedContest.from_dict(raw) for raw in (plan or {}).get("contests") or []]


# ------------------------------------------------------------- naming who was not found


def _restatus(contest: PlannedContest) -> str:
    """READY and INCOMPLETE follow the authors; the other statuses are about the week."""
    if contest.status in (READY, INCOMPLETE):
        return INCOMPLETE if contest.unknown() else READY
    return contest.status


def _apply_manual(work: PlannedWork, author: dict) -> None:
    work.entry.author_id = author.get("id")
    work.entry.author_name = author.get("name") or work.entry.author_name
    work.entry.author_username = author.get("username")
    work.how = HOW_MANUAL


def unknown_works(entry: str) -> list[tuple[PlannedContest, PlannedWork]]:
    """Every work of a week still to be written whose author nobody has named, oldest
    week first, most votes first within it."""
    return [(contest, work) for contest in plan_contests_of(load_plan(entry))
            if contest.status in (READY, INCOMPLETE) for work in contest.unknown()]


def parse_author(text: str) -> tuple[str | None, str] | None:
    """An administrator's answer to "чья это работа?": ("username", "") for an @tag or a
    t.me link, (None, "Имя Фамилия") for a plain name, None for nothing usable."""
    text = " ".join((text or "").split())
    if not text:
        return None
    found = usernames_in(text)
    if found:
        return found[0], ""
    if len(text) > 64 or not re.search(r"\w", text):
        return None
    return None, text


def set_author(entry: str, key: str, author: dict) -> tuple[PlannedContest, PlannedWork] | None:
    """Names the author of the work `key` ("<poll message id>.<option index>") in the
    saved plan -- {"id", "name", "username"}, the id None for somebody without an account
    the bot knows. An option with no picture of its own then becomes that author's post of
    the week, when they have one. Kept across a re-scan. Returns the contest and the work
    as they now are, or None when the plan has no such work. Disk: a worker thread."""
    plan = load_plan(entry)
    if plan is None:
        return None
    contests = plan_contests_of(plan)
    for contest in contests:
        for work in contest.works:
            if work.key != key:
                continue
            _apply_manual(work, author)
            if not work.entry.media and author.get("id") is not None:
                post = _post_by(plan.get("scan") or {}, author["id"], contest)
                if post is not None:
                    work.entry = voting.Entry.from_dict(post.to_dict())
            contest.status = _restatus(contest)
            plan.setdefault("manual", {})[key] = {
                "id": author.get("id"), "name": author.get("name"), "username": author.get("username"),
            }
            plan["contests"] = [c.to_dict() for c in contests]
            _write_json(plan_path(entry), plan)
            return contest, work
    return None


def _post_by(scan: dict, user_id, contest: PlannedContest) -> voting.Entry | None:
    first = _when(contest.created_at)
    used = {work.entry.entry_id for work in contest.works}
    best = None
    for raw in scan.get("posts") or []:
        entry = voting.Entry.from_dict(raw.get("entry") or {})
        when = _when(entry.posted_at)
        if entry.author_id != user_id or entry.entry_id in used or when is None or first is None:
            continue
        if first - POST_LOOKBACK <= when <= first and (best is None or entry.message_id > best.message_id):
            best = entry
    return best


def find_member(plan: dict | None, username: str) -> dict | None:
    """{"id", "name", "username"} of a member or an artist of the saved plan going by
    `username`, or None -- the bot then asks Telegram."""
    wanted = username.lower()
    for uid, member in ((plan or {}).get("members") or {}).items():
        if (member.get("username") or "").lower() == wanted and _as_id(uid) is not None:
            return {"id": _as_id(uid), "name": member.get("name") or f"@{username}", "username": member["username"]}
    for raw in ((plan or {}).get("scan") or {}).get("posts") or []:
        entry = raw.get("entry") or {}
        if (entry.get("author_username") or "").lower() == wanted and raw.get("is_user", True):
            return {"id": entry.get("author_id"), "name": entry.get("author_name"), "username": entry.get("author_username")}
    return None


# ---------------------------------------------------------------------------- the preview

MESSAGE_BUDGET = 3500   # characters per message, well inside Telegram's 4096


def _short_date(text: str) -> str:
    when = _when(text)
    return when.strftime("%d.%m.%Y") if when else ""


def _who(entry: voting.Entry) -> str:
    return f"{entry.author_name} (@{entry.author_username})" if entry.author_username else entry.author_name


def _count(number: int, one: str, few: str, many: str) -> str:
    """"2 опроса", "5 работ", "21 работа" -- Russian plural, teens included."""
    if 11 <= number % 100 <= 14 or number % 10 == 0 or number % 10 >= 5:
        word = many
    elif number % 10 == 1:
        word = one
    else:
        word = few
    return f"{number} {word}"


def _work_label(work: PlannedWork) -> str:
    if work.known:
        return _who(work.entry)
    return f"❓ «{clean_option_text(work.option_text) or work.option_text}»"


def preview_messages(contests: list[PlannedContest], polls_seen: int, copies: int = 0, questions: int = 0) -> list[str]:
    """What the administrator reads before anything is written, as one or more messages:
    every contest found, its podium, how its works were matched and what will happen to
    it."""
    ready = [c for c in contests if c.status == READY]
    unknown = sum(len(c.unknown()) for c in contests if c.status in (READY, INCOMPLETE))
    head = [f"Опросов в чате до автоматизации: {polls_seen}."]
    skipped = []
    if copies:
        skipped.append(f"копий одного и того же опроса — {copies}")
    if questions:
        skipped.append(f"опросов-вопросов, не про работы — {questions}")
    if skipped:
        head.append("Не считаю: " + ", ".join(skipped) + ".")
    head.append(f"Конкурсов: {len(contests)}. Готовы к записи: {len(ready)}."
                + (f" Не узнал авторов у {_count(unknown, 'работы', 'работ', 'работ')} — подпишешь по картинкам."
                   if unknown else ""))
    blocks = []
    for contest in contests:
        works = contest.works
        lines = [f"🗳 {contest.week} · {_short_date(contest.created_at)} · «{contest.label()}»"
                 + (f" · {_count(len(contest.poll_message_ids), 'опрос', 'опроса', 'опросов')}"
                    if len(contest.poll_message_ids) > 1 else "")]
        if contest.status == AUTOMATED:
            lines.append("   уже неделя бота — пропускаю")
            blocks.append("\n".join(lines))
            continue
        lines.append(f"   работ: {len(works)}, проголосовало: {contest.voters}")
        medals = ("🥇", "🥈", "🥉")
        lines.extend(f"   {medal} {_work_label(work)} — {voting.votes_label(work.votes)}"
                     for medal, work in zip(medals, works) if work.votes > 0)
        counts: dict[str, int] = {}
        for work in works:
            counts[work.how] = counts.get(work.how, 0) + 1
        missing = contest.unknown()
        if works:
            detail = ", ".join(f"{how} {count}" for how, count in counts.items() if how != HOW_NONE)
            lines.append(f"   узнал авторов: {len(works) - len(missing)} из {len(works)}" + (f" ({detail})" if detail else ""))
        if missing:
            names = ", ".join(f"«{clean_option_text(w.option_text) or w.option_text}»" for w in missing[:6])
            lines.append(f"   не узнал: {names}" + (" и др." if len(missing) > 6 else ""))
        no_photo = sum(1 for work in works if not work.entry.media)
        if no_photo:
            lines.append(f"   без картинки: {no_photo}")
        lines.append({
            READY: "   ✅ запишу",
            INCOMPLETE: "   ⏳ запишу, когда подпишешь неузнанных",
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
        awards.append((number_of[contest_id], winner.author_id, winner.author_name,
                       {"source": SOURCE, "contest_id": contest_id}))
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
    """Writes every complete week of the saved plan and brings them into the Hall of Fame;
    returns {"written", "works", "contests", "hall", "winners", "waiting"}, or {"error"}
    when there is no plan. Disk and image work: run it in a worker thread."""
    import hall_of_fame

    plan = load_plan(entry)
    if plan is None:
        return {"error": "no plan"}
    known, _ = known_contests(entry)
    source = cache_dir(entry)
    written, works, summaries = [], 0, []
    contests = plan_contests_of(plan)
    waiting = sum(1 for c in contests if c.status == INCOMPLETE and c.poll_id not in known)
    now = datetime.now(timezone.utc).isoformat()
    for contest in contests:
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
        return {"written": [], "works": 0, "contests": [], "hall": None, "winners": {"status": "none"},
                "waiting": waiting}
    hall = hall_of_fame.import_history(entry)
    winners = _weekly_winners(entry, written, giver_id, giver_name)
    log(f"[legacy] wrote {len(written)} contest(s), {works} work(s); hall {hall}; winners {winners}")
    return {"written": written, "works": works, "contests": summaries, "hall": hall, "winners": winners,
            "waiting": waiting}


def apply_report(result: dict) -> str:
    """The reply to an apply, in one message."""
    if result.get("error"):
        return "Сначала найди конкурсы: /hall опросы — запишу то, что покажу."
    written = result.get("written") or []
    waiting = result.get("waiting") or 0
    if not written:
        if waiting:
            return f"Пока записывать нечего: в {_count(waiting, 'неделе', 'неделях', 'неделях')} не все авторы узнаны — подпиши их."
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
            lines.append(f"Победителей без аккаунта: {winners['unnamed']} — их в таблицу победителей не вписать.")
    elif winners.get("status") == "mismatch":
        lines.append(
            f"Таблицу победителей недель (/weekwinner) не трогал: там №{winners.get('number')} — "
            f"{winners.get('stored')}, а по порядку недель №{winners.get('number')} — "
            f"{winners.get('week') or 'другой конкурс'}. Победителей старых недель проставь вручную."
        )
    elif winners.get("status") == "unreadable":
        lines.append("Таблица победителей недель не читается — её не трогал.")
    if waiting:
        lines.append(f"Ждут подписи авторов: {_count(waiting, 'неделя', 'недели', 'недель')}.")
    lines.append("Отменить всё записанное отсюда: /hall опросы отменить")
    return "\n".join(lines).strip()


# ------------------------------------------------------------------------------- the undo


def undo_import(entry: str) -> dict:
    """Takes everything an import wrote back out: the results records marked SOURCE and
    the photos beside them (moved into undone_dir, not deleted), the Hall of Fame's copies
    of those weeks, and the weekly winners the import recorded. Returns {"contests",
    "winners"}. Disk: a worker thread."""
    import hall_of_fame
    import stats

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    target = undone_dir(entry) / stamp
    removed, imported_at = [], []
    winners_of: dict[str, str] = {}
    hall = hall_of_fame.snapshot(entry)
    for poll_id in voting.results_poll_ids(entry):
        record = voting.load_results(entry, poll_id) or {}
        if record.get("source") != SOURCE:
            continue
        imported_at.append(record.get("imported_at") or "")
        contest = hall.by_id.get(poll_id)
        winner = contest.winner() if contest else None
        if winner is not None and winner.author_id is not None:
            winners_of[poll_id] = str(winner.author_id)
        target.mkdir(parents=True, exist_ok=True)
        shutil.move(str(voting.results_path(entry, poll_id)), str(target / f"{poll_id}.json"))
        media = voting.archived_media_path(entry, poll_id)
        if media.exists():
            shutil.move(str(media), str(target / f"media_{poll_id}"))
        hall_file = hall_of_fame.contest_path(entry, poll_id)
        if hall_file.exists():
            hall_file.unlink()
        hall_media = hall_of_fame.media_dir(entry, poll_id)
        if hall_media.exists():
            shutil.rmtree(hall_media, ignore_errors=True)
        removed.append(poll_id)
    weeks = []
    if removed:
        try:
            stored = stats.weekly_contest_winners(entry)
        except (json.JSONDecodeError, OSError, ValueError):
            stored = {}
        stamps = sorted(stamp for stamp in imported_at if stamp)
        for number, record in stored.items():
            if record.get("source") == SOURCE and record.get("contest_id") in removed:
                weeks.append(number)
            elif not record.get("source") and stamps and str(record.get("user_id")) in winners_of.values():
                # Written by an import that did not mark its winners yet: same person, in
                # the minutes the import ran.
                given = _when(record.get("given_at") or "")
                start, end = _when(stamps[0]), _when(stamps[-1])
                if given and start and end and start - timedelta(minutes=1) <= given <= end + timedelta(minutes=30):
                    weeks.append(number)
        if weeks:
            stats.remove_weekly_contest_winners(entry, weeks)
    return {"contests": removed, "winners": sorted(weeks)}


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
