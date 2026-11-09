"""Renders a finished (or in-progress) weekly-contest vote as ONE tall picture -- the same
three-column board the Mini App shows, drawn with Pillow so it can be saved and reposted
outside Telegram.

Deliberately the page's grid and not a prettier layout of its own: the point is that the
admin who has been looking at vote_web.PAGE_HTML all week recognises the export as the same
thing. Same colours, same three columns, same square thumbnail with the author underneath.
Two differences, both from the medium:

- No "выбрать" button on a card. Nothing in a picture is tappable, and a button drawn into
  one is just a lie about what it does.
- The photo is FITTED into its square (letterboxed) by default, not cropped to fill it. On
  the page a cropped thumbnail is a link to the full picture one tap away; here there is no
  tap, so an automatic crop would be the only thing anybody ever sees of that work. An
  administrator can still frame any card by hand on the cropping page
  (vote_web.BOARD_HTML) -- what they draw arrives here as voting.Poll.crops, and a card
  nobody framed stays fitted.

Everything here is pure: it takes standings and a directory of photos and writes a file.
Who is admitted, how the votes were counted and where the file then goes are voting.py's
and bot_listener.py's business respectively.
"""

import math
import os
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

import voting

# ------------------------------------------------------------------------------ the look
#
# Straight out of vote_web.PAGE_HTML's :root, so the export and the page are the same
# board. The page's colours are Telegram-theme variables with these as their fallbacks --
# a picture has no theme to follow, so the fallbacks (the dark scheme everyone actually
# sees in Telegram) are what it draws.
BG = "#17212b"
CARD = "#232e3c"
FG = "#f5f5f5"
MUTED = "#8a9aa9"
ACCENT = "#3390ec"
ACCENT_FG = "#ffffff"
# The letterbox behind a fitted photo -- a touch darker than the card, so a portrait shot
# reads as a picture on a card rather than as a card of an odd shape.
THUMB_BG = "#1a2532"

# How many works to a row by default. Every layout figure below is per-CARD rather than
# per-image, so asking for another column widens the picture instead of shrinking the
# works in it: at 4 across each one is still drawn at exactly the same size, which is the
# whole reason to want the wider board (more of the week on one screenful, none of it
# smaller). MIN/MAX only bound what a request may ask for -- two columns is barely a grid
# and past six the picture is wider than anything renders it.
COLUMNS = 3
MIN_COLUMNS = 2
MAX_COLUMNS = 6
CARD_WIDTH = 360
# Square, exactly like the page's `.thumb { aspect-ratio: 1 }`: it is what keeps rows
# aligned when the works themselves are a mix of portrait and landscape.
THUMB_HEIGHT = CARD_WIDTH
# Fixed, and the same for a card whose author has no @username as for one who has: the row
# below would otherwise sit at a different height per card and the grid would go ragged.
CAPTION_HEIGHT = 92
CARD_HEIGHT = THUMB_HEIGHT + CAPTION_HEIGHT
GAP = 24
MARGIN = 36
CARD_RADIUS = 16
TEXT_PADDING = 14

def clamp_columns(columns) -> int:
    """Whatever a caller asked for, reduced to a column count this can actually draw.
    Takes the request from an HTTP body and a chat command alike, so the bounds live in
    one place rather than at each door."""
    try:
        return max(MIN_COLUMNS, min(MAX_COLUMNS, int(columns)))
    except (TypeError, ValueError):
        return COLUMNS


def width_for(columns: int = COLUMNS) -> int:
    columns = clamp_columns(columns)
    return columns * CARD_WIDTH + (columns - 1) * GAP + 2 * MARGIN


# The default board's width, kept as a constant because most of the code (and every test
# that isn't about column counts) only ever means this one.
WIDTH = width_for(COLUMNS)

TITLE_SIZE = 42
SUBTITLE_SIZE = 26
NAME_SIZE = 25
TAG_SIZE = 22
BADGE_SIZE = 23

# JPEG cannot address a side past this, and a board of a few hundred works would get
# there. PNG has no such ceiling, so an absurdly long export silently becomes one rather
# than failing on save.
_JPEG_MAX_SIDE = 65500

# Fonts Pillow must be handed explicitly -- it ships no Cyrillic-capable one of its own,
# and half the text here is Russian. Tried in order; the first that exists wins. Set
# VOTE_IMAGE_FONT (and optionally VOTE_IMAGE_FONT_BOLD) to override on a host whose fonts
# live somewhere else. The Docker image installs fonts-dejavu-core for this.
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]
_BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


def _font_path(bold: bool) -> str | None:
    override = os.getenv("VOTE_IMAGE_FONT_BOLD" if bold else "VOTE_IMAGE_FONT")
    candidates = ([override] if override else []) + (
        _BOLD_CANDIDATES if bold else []
    ) + _FONT_CANDIDATES
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


@lru_cache(maxsize=None)
def _font(size: int, bold: bool = False):
    """A truetype face at `size`, or Pillow's built-in as a last resort.

    Cached because every card asks for the same three sizes and reopening a face per card
    is the slowest thing in this module by a distance. The fallback renders Cyrillic as
    boxes, which is ugly but is still a picture -- better than a host with no fonts
    installed turning the whole export into an exception.
    """
    path = _font_path(bold)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


# Permanently unassigned in Unicode, so no font anywhere maps it: whatever the current
# face draws for THIS is what it draws for every character it doesn't have -- the .notdef
# box. Comparing against it is how a missing glyph is recognised without parsing the font.
_UNRENDERABLE_PROBE = "͸"


@lru_cache(maxsize=None)
def _notdef_bitmap(size: int, bold: bool) -> bytes | None:
    """What a character this font does NOT have looks like, or None if the question can't
    be asked of this font (the bitmap fallback, mainly)."""
    try:
        return bytes(_font(size, bold).getmask(_UNRENDERABLE_PROBE))
    except Exception:
        return None


@lru_cache(maxsize=4096)
def _renders(character: str, size: int, bold: bool) -> bool:
    notdef = _notdef_bitmap(size, bold)
    if notdef is None:
        return True  # can't tell -- draw it and hope, rather than silently deleting text
    try:
        return bytes(_font(size, bold).getmask(character)) != notdef
    except Exception:
        return False  # a character the font can't even be asked about would kill the draw


def legible(text: str, size: int = NAME_SIZE, bold: bool = False) -> str:
    """`text` with everything the current font would draw as an empty box removed.

    Telegram display names are full of things no ordinary font has: emoji, and the
    "fancy" alphabets (𝓐𝓷𝓷𝓪, 𝔸𝕟𝕟𝕒) people set as their name. FreeType does not fail on
    those -- it quietly draws .notdef, a hollow rectangle, so a whole name can come out as
    a row of squares while every other card looks fine.

    Two steps, in this order. NFKC normalisation first, because it turns most of those
    fancy alphabets back into the ordinary letters they imitate (𝓐 -> A) -- a rescue, not
    a deletion. Then whatever STILL has no glyph is dropped, along with the invisible
    formatting characters (zero-width joiners, variation selectors) that emoji leave
    behind. What's left is what the font can actually draw.
    """
    normalized = unicodedata.normalize("NFKC", text or "")
    kept = []
    for character in normalized:
        if character.isspace():
            kept.append(character)
            continue
        if unicodedata.category(character) in ("Cc", "Cf", "Cs", "Co", "Cn"):
            continue
        # Variation selectors are category Mn, not Cf, so the check above misses them --
        # and a font that happens to have a glyph for one draws a visible artefact where
        # the emoji it was modifying used to be.
        if 0xFE00 <= ord(character) <= 0xFE0F or 0xE0100 <= ord(character) <= 0xE01EF:
            continue
        if _renders(character, size, bold):
            kept.append(character)
    return " ".join("".join(kept).split())


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    """Measured with textbbox rather than the font's own metrics, so it works for the
    bitmap fallback too (which supports neither anchors nor getlength)."""
    left, top, right, bottom = draw.textbbox((0, 0), text or "", font=font)
    return right - left, bottom - top


def _ellipsize(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
    """text-overflow: ellipsis, by hand. A name that doesn't fit is cut and given a "…"
    rather than being allowed to run into the next column."""
    text = text or ""
    if _text_size(draw, text, font)[0] <= max_width:
        return text
    ellipsis = "…"
    cut = text
    while cut and _text_size(draw, cut + ellipsis, font)[0] > max_width:
        cut = cut[:-1]
    return (cut + ellipsis) if cut else ellipsis


def default_crop(width: int, height: int) -> dict:
    """The framing an entry gets when nobody has touched it: the smallest square that
    contains the WHOLE photo, centred -- i.e. fitted and letterboxed, which is what this
    export did before cropping existed. Shared with the cropping page (it computes the
    same square in JavaScript for a card it is showing for the first time), so "не тронуто"
    means the same thing in both places."""
    size = float(max(width, height))
    return {"x": (width - size) / 2, "y": (height - size) / 2, "size": size}


def _crop_to_square(image: Image.Image, crop: dict | None) -> Image.Image:
    """Draws `image` into the thumbnail square through `crop` -- a square in the image's
    OWN pixel coordinates, which may hang off its edges. Whatever the square covers of the
    photo is scaled to fill the thumbnail; whatever it covers of nothing becomes letterbox.

    Only the part of the photo actually inside the square is ever resized, so a deep zoom
    costs no more memory than a wide one: the region shrinks exactly as fast as the scale
    factor grows, and the result is a THUMB-sized tile either way.
    """
    if crop is None:
        crop = default_crop(image.width, image.height)
    x, y, size = float(crop["x"]), float(crop["y"]), float(crop["size"])
    scale = THUMB_HEIGHT / size

    square = Image.new("RGB", (CARD_WIDTH, THUMB_HEIGHT), THUMB_BG)
    left, top = max(0, math.floor(x)), max(0, math.floor(y))
    right = min(image.width, math.ceil(x + size))
    bottom = min(image.height, math.ceil(y + size))
    if right <= left or bottom <= top:
        return square  # framed entirely off the photo: all letterbox, nothing to draw

    region = image.crop((left, top, right, bottom))
    target = (max(1, round(region.width * scale)), max(1, round(region.height * scale)))
    square.paste(
        region.resize(target, Image.LANCZOS),
        (round((left - x) * scale), round((top - y) * scale)),
    )
    return square


def _load_photo(path: Path, crop: dict | None = None) -> Image.Image | None:
    """The work as it should appear in its cell, already the size of the thumbnail square.
    None if the file is missing or unreadable -- one lost photo leaves an empty card, it
    does not lose the board."""
    try:
        with Image.open(path) as opened:
            # Phone photos carry their rotation in EXIF rather than in the pixels; without
            # this a portrait shot lands on its side. Applied before the crop is read,
            # because the browser applies it too -- the square the editor drew is in
            # rotated coordinates, so this is what makes the two agree.
            rotated = ImageOps.exif_transpose(opened) or opened
            return _crop_to_square(rotated.convert("RGB"), crop)
    except (OSError, ValueError, KeyError, TypeError, Image.DecompressionBombError):
        return None


def _who_lines(entry) -> tuple[str, str]:
    """The two caption lines: the author's name, and their @tag under it. An author with
    no username gets an empty second line rather than having their name moved down into
    it -- the name must sit at the same height on every card.

    A name that is ENTIRELY unrenderable (all emoji, say -- see legible) leaves nothing to
    print, so the @tag is promoted to the name line rather than drawing a blank card, and
    the tag line is cleared so it isn't printed twice. A user with neither is named as
    plainly as possible instead of as a row of boxes.
    """
    name = legible(entry.author_name, NAME_SIZE)
    tag = legible(f"@{entry.author_username}", TAG_SIZE) if entry.author_username else ""
    if not name:
        return (tag or "Без имени"), ""
    return name, tag


def _draw_card(
    entry, votes: int, media_dir: Path, show_votes: bool, crop: dict | None = None
) -> Image.Image:
    """One cell: the framed photo, its vote count, the author. Drawn on its own canvas and
    masked to rounded corners by the caller, which is the only way the corners stay round
    over a photo that happens to fill the square exactly."""
    card = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), CARD)
    draw = ImageDraw.Draw(card)
    draw.rectangle([0, 0, CARD_WIDTH, THUMB_HEIGHT], fill=THUMB_BG)

    photo = _load_photo(media_dir / entry.media[0], crop) if entry.media else None
    if photo is not None:
        card.paste(photo, (0, 0))  # already exactly the thumbnail square, letterbox and all

    if show_votes:
        # Top-left pill, where the page puts its own `.votes` badge. Drawn last of the
        # thumbnail's parts so it stays legible over a light photo.
        label = str(votes)
        font = _font(BADGE_SIZE, bold=True)
        text_width, text_height = _text_size(draw, label, font)
        pad_x, pad_y = 12, 7
        box = [10, 10, 10 + text_width + 2 * pad_x, 10 + text_height + 2 * pad_y]
        draw.rounded_rectangle(box, radius=(box[3] - box[1]) // 2, fill=ACCENT)
        draw.text((box[0] + pad_x, box[1] + pad_y - 2), label, font=font, fill=ACCENT_FG)

    name, tag = _who_lines(entry)
    inner = CARD_WIDTH - 2 * TEXT_PADDING
    name_font, tag_font = _font(NAME_SIZE), _font(TAG_SIZE)
    draw.text(
        (TEXT_PADDING, THUMB_HEIGHT + 16),
        _ellipsize(draw, name, name_font, inner), font=name_font, fill=FG,
    )
    if tag:
        draw.text(
            (TEXT_PADDING, THUMB_HEIGHT + 54),
            _ellipsize(draw, tag, tag_font, inner), font=tag_font, fill=MUTED,
        )
    return card


def _rounded_mask() -> Image.Image:
    mask = Image.new("L", (CARD_WIDTH, CARD_HEIGHT), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, CARD_WIDTH - 1, CARD_HEIGHT - 1], radius=CARD_RADIUS, fill=255
    )
    return mask


def image_size(count: int, header_height: int = 0, columns: int = COLUMNS) -> tuple[int, int]:
    """The canvas a board of `count` works needs. Width follows the column count; height
    grows a row at a time -- one picture however many entries there are, which is the whole
    request: no paging, no second file, just a longer one."""
    columns = clamp_columns(columns)
    rows = max(1, math.ceil(count / columns))
    height = MARGIN + header_height + rows * CARD_HEIGHT + (rows - 1) * GAP + MARGIN
    return width_for(columns), height


def render_standings_image(
    standings,
    media_dir,
    out_path,
    title: str = "Итоги недели",
    subtitle: str = "",
    show_votes: bool = True,
    crops: dict | None = None,
    columns: int = COLUMNS,
) -> Path:
    """Draws `standings` -- a voting.Poll.tally() result, i.e. (Entry, votes) pairs already
    ranked most-votes-first -- into one picture at `out_path`, and returns that path.

    The order is taken as given, never re-sorted here: tally() is what the page ranks by
    and what the announcement text numbers, so the export ranking anything for itself is
    how the three of them would start disagreeing.

    `media_dir` is the poll's own photo directory (voting.media_path); only each entry's
    FIRST photo is drawn, exactly as the page's grid does with an album.

    `crops` is voting.Poll.crops -- entry_id -> the square that entry's photo is framed
    through, as drawn on the cropping page (vote_web.BOARD_HTML). An entry that isn't in
    it (or a None `crops` altogether) is fitted whole, letterboxed: untouched photos look
    exactly as they did before anybody could crop anything.

    `columns` is how many works go in a row. The cards themselves do not change size with
    it -- a wider board is a wider picture, not smaller works (see COLUMNS).

    Raises ValueError on empty standings -- a board of nothing is not a picture worth
    writing, and the caller has something better to say about it than a blank file.
    """
    standings = list(standings)
    if not standings:
        raise ValueError("nothing to render -- no admitted entries")
    columns = clamp_columns(columns)

    media_dir = Path(media_dir)
    out_path = Path(out_path)

    # The header is measured before the canvas exists (its height decides how tall that
    # canvas is), so it's measured against a throwaway one-pixel image.
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    title_font, subtitle_font = _font(TITLE_SIZE, bold=True), _font(SUBTITLE_SIZE)
    if title:
        # A contest's own title can be longer than "Итоги недели": cut, never cropped.
        title = _ellipsize(probe, title, title_font, width_for(columns) - 2 * MARGIN)
    title_height = _text_size(probe, title, title_font)[1] if title else 0
    subtitle_height = _text_size(probe, subtitle, subtitle_font)[1] if subtitle else 0
    header_height = 0
    if title:
        header_height += title_height + 14
    if subtitle:
        header_height += subtitle_height + 10
    if header_height:
        header_height += 14  # breathing room between the header and the first row

    width, height = image_size(len(standings), header_height, columns)
    canvas = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(canvas)

    y = MARGIN
    if title:
        draw.text((MARGIN, y), title, font=title_font, fill=FG)
        y += title_height + 14
    if subtitle:
        draw.text((MARGIN, y), subtitle, font=subtitle_font, fill=MUTED)
        y += subtitle_height + 10
    if header_height:
        y = MARGIN + header_height

    _paste_grid(canvas, standings, media_dir, show_votes, crops, columns, y)
    return _save(canvas, out_path)


def _grid_height(count: int, columns: int) -> int:
    rows = max(1, math.ceil(count / columns))
    return rows * CARD_HEIGHT + (rows - 1) * GAP


def _paste_grid(canvas, standings, media_dir: Path, show_votes: bool, crops, columns: int, top: int) -> None:
    """The cards of `standings`, `columns` to a row, the first row's top at `top`."""
    crops = crops or {}
    mask = _rounded_mask()
    for index, (entry, votes) in enumerate(standings):
        column, row = index % columns, index // columns
        x = MARGIN + column * (CARD_WIDTH + GAP)
        card = _draw_card(entry, votes, media_dir, show_votes, crops.get(entry.entry_id))
        canvas.paste(card, (x, top + row * (CARD_HEIGHT + GAP)), mask)


def _save(canvas, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.suffix.lower() in (".jpg", ".jpeg") and canvas.height > _JPEG_MAX_SIDE:
        out_path = out_path.with_suffix(".png")
    if out_path.suffix.lower() in (".jpg", ".jpeg"):
        canvas.save(out_path, "JPEG", quality=92, optimize=True, progressive=True)
    else:
        canvas.save(out_path)
    return out_path


def render_poll_image(
    poll, out_path, title: str | None = None, subtitle: str = "", columns: int = COLUMNS,
) -> Path:
    """render_standings_image for a whole voting.Poll: its own tally, its own photos.

    Only ADMITTED entries appear, because tally() only ranks those -- the export is a
    picture of the vote, not of everything that was ever nominated. The title is the
    contest's own name unless one is given: "Итоги недели" for the weekly vote, the
    round's for a monthly one.
    """
    return render_standings_image(
        poll.tally(),
        voting.media_path(poll.entry, poll.poll_id),
        out_path,
        title=poll.label() if title is None else title,
        subtitle=subtitle,
        crops=poll.crops,
        columns=columns,
    )


# ------------------------------------------------------------- the month's rounds at once
#
# The month's bronze, silver and gold rounds are three polls, but one contest: they are
# exported as ONE picture -- the month's title, then each round under a heading of its own
# medal's colour, its works ranked as on its tab of the page.

MEDAL_COLOURS = {3: "#cd7f32", 2: "#c0c6cc", 1: "#f2c230"}
MEDAL_FG = "#17212b"
SECTION_SIZE = 28
SECTION_GAP = 56          # between one round's last row and the next round's heading
SECTION_HEADING_GAP = 18  # between a heading and its first row
_PILL_PAD_X, _PILL_PAD_Y = 18, 9


@dataclass
class Section:
    """One round of a combined picture: its heading, the line beside it, and its board."""
    heading: str
    note: str
    standings: list
    media_dir: Path
    crops: dict | None = None
    colour: str = ACCENT


def _section_heading_height(probe, section_font) -> int:
    return _text_size(probe, "Бр", section_font)[1] + 2 * _PILL_PAD_Y


def render_sections_image(
    sections, out_path, title: str = "", subtitle: str = "", columns: int = COLUMNS,
    show_votes: bool = True,
) -> Path:
    """Several boards stacked in one picture, each under a coloured heading -- the month's
    rounds (render_month_image). A round with nothing admitted says so under its heading
    rather than being left out: three medals are promised, and a missing one would read as
    a mistake. Raises ValueError when no section has anything to draw."""
    sections = list(sections)
    if not any(section.standings for section in sections):
        raise ValueError("nothing to render -- no admitted entries in any section")
    columns = clamp_columns(columns)
    out_path = Path(out_path)
    width = width_for(columns)

    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    title_font, subtitle_font = _font(TITLE_SIZE, bold=True), _font(SUBTITLE_SIZE)
    section_font, note_font = _font(SECTION_SIZE, bold=True), _font(SUBTITLE_SIZE)
    if title:
        title = _ellipsize(probe, title, title_font, width - 2 * MARGIN)
    title_height = _text_size(probe, title, title_font)[1] if title else 0
    subtitle_height = _text_size(probe, subtitle, subtitle_font)[1] if subtitle else 0
    heading_height = _section_heading_height(probe, section_font)
    empty_height = _text_size(probe, "Нет", note_font)[1] + 8

    height = MARGIN
    if title:
        height += title_height + 14
    if subtitle:
        height += subtitle_height + 10
    if title or subtitle:
        height += 14 + SECTION_GAP // 2
    for index, section in enumerate(sections):
        if index:
            height += SECTION_GAP
        height += heading_height + SECTION_HEADING_GAP
        height += _grid_height(len(section.standings), columns) if section.standings else empty_height
    height += MARGIN

    canvas = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(canvas)
    y = MARGIN
    if title:
        draw.text((MARGIN, y), title, font=title_font, fill=FG)
        y += title_height + 14
    if subtitle:
        draw.text((MARGIN, y), subtitle, font=subtitle_font, fill=MUTED)
        y += subtitle_height + 10
    if title or subtitle:
        y += 14 + SECTION_GAP // 2

    for index, section in enumerate(sections):
        if index:
            y += SECTION_GAP
        heading = legible(section.heading, SECTION_SIZE, bold=True) or section.heading
        heading_width = _text_size(draw, heading, section_font)[0]
        pill = [MARGIN, y, MARGIN + heading_width + 2 * _PILL_PAD_X, y + heading_height]
        draw.rounded_rectangle(pill, radius=heading_height // 2, fill=section.colour)
        draw.text((MARGIN + _PILL_PAD_X, y + _PILL_PAD_Y - 3), heading, font=section_font, fill=MEDAL_FG)
        if section.note:
            note_x = pill[2] + 18
            note = _ellipsize(draw, legible(section.note, SUBTITLE_SIZE), note_font, width - MARGIN - note_x)
            note_height = _text_size(draw, note, note_font)[1]
            draw.text((note_x, y + (heading_height - note_height) // 2 - 3), note, font=note_font, fill=MUTED)
        y += heading_height + SECTION_HEADING_GAP
        if section.standings:
            _paste_grid(canvas, section.standings, Path(section.media_dir), show_votes,
                        section.crops, columns, y)
            y += _grid_height(len(section.standings), columns)
        else:
            draw.text((MARGIN, y), "Ни одной допущенной работы.", font=note_font, fill=MUTED)
            y += empty_height
    return _save(canvas, out_path)


def poll_note(poll, standings=None) -> str:
    """"проголосовало 12 · работ 9 · закрыто" -- the line beside a round's heading."""
    standings = poll.tally() if standings is None else standings
    return (
        f"проголосовало {len(poll.votes)} · работ {len(standings)} · "
        f"{'открыто' if poll.open else 'закрыто'}"
    )


def render_month_image(polls, out_path, title: str | None = None, subtitle: str | None = None,
                       columns: int = COLUMNS) -> Path:
    """The month's rounds (voting.page_polls' monthly polls, bronze first) as one picture,
    each round's admitted works ranked by its own tally()."""
    polls = list(polls)
    sections = []
    for poll in polls:
        standings = poll.tally()
        name = poll.round[2] if poll.round else poll.label()
        sections.append(Section(
            heading=name[:1].upper() + name[1:],
            note=poll_note(poll, standings),
            standings=standings,
            media_dir=voting.media_path(poll.entry, poll.poll_id),
            crops=poll.crops,
            colour=MEDAL_COLOURS.get(poll.round[1] if poll.round else poll.tier, ACCENT),
        ))
    if title is None:
        title = polls[0].group_label() if polls else ""
    if subtitle is None:
        voters = set()
        for poll in polls:
            voters.update(poll.votes)
        subtitle = f"Раундов: {len(polls)} · проголосовало: {len(voters)} чел."
    return render_sections_image(sections, out_path, title=title, subtitle=subtitle, columns=columns)
