"""Renders a finished (or in-progress) weekly-contest vote as ONE tall picture -- the same
three-column board the Mini App shows, drawn with Pillow so it can be saved and reposted
outside Telegram.

Deliberately the page's grid and not a prettier layout of its own: the point is that the
admin who has been looking at vote_web.PAGE_HTML all week recognises the export as the same
thing. Same colours, same three columns, same square thumbnail with the author underneath.
Two differences, both from the medium:

- No "выбрать" button on a card. Nothing in a picture is tappable, and a button drawn into
  one is just a lie about what it does.
- The photo is FITTED into its square (letterboxed), not cropped to fill it. On the page a
  cropped thumbnail is a link to the full picture one tap away; here there is no tap, so a
  crop would be the only thing anybody ever sees of that work.

Everything here is pure: it takes standings and a directory of photos and writes a file.
Who is admitted, how the votes were counted and where the file then goes are voting.py's
and bot_listener.py's business respectively.
"""

import math
import os
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

COLUMNS = 3
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

WIDTH = COLUMNS * CARD_WIDTH + (COLUMNS - 1) * GAP + 2 * MARGIN

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


def _load_photo(path: Path) -> Image.Image | None:
    """The work, fitted (never cropped) inside the thumbnail square. None if the file is
    missing or unreadable -- one lost photo leaves an empty card, it does not lose the
    board."""
    try:
        with Image.open(path) as opened:
            # Phone photos carry their rotation in EXIF rather than in the pixels; without
            # this a portrait shot lands on its side.
            rotated = ImageOps.exif_transpose(opened) or opened
            return ImageOps.contain(
                rotated.convert("RGB"), (CARD_WIDTH, THUMB_HEIGHT), Image.LANCZOS
            )
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def _who_lines(entry) -> tuple[str, str]:
    """The two caption lines: the author's name, and their @tag under it. An author with
    no username gets an empty second line rather than having their name moved down into
    it -- the name must sit at the same height on every card."""
    tag = f"@{entry.author_username}" if entry.author_username else ""
    return entry.author_name or "Unknown", tag


def _draw_card(entry, votes: int, media_dir: Path, show_votes: bool) -> Image.Image:
    """One cell: the fitted photo, its vote count, the author. Drawn on its own canvas and
    masked to rounded corners by the caller, which is the only way the corners stay round
    over a photo that happens to fill the square exactly."""
    card = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), CARD)
    draw = ImageDraw.Draw(card)
    draw.rectangle([0, 0, CARD_WIDTH, THUMB_HEIGHT], fill=THUMB_BG)

    photo = _load_photo(media_dir / entry.media[0]) if entry.media else None
    if photo is not None:
        card.paste(
            photo,
            ((CARD_WIDTH - photo.width) // 2, (THUMB_HEIGHT - photo.height) // 2),
        )

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


def image_size(count: int, header_height: int = 0) -> tuple[int, int]:
    """The canvas a board of `count` works needs. Fixed width, height grows a row at a
    time -- one picture however many entries there are, which is the whole request: no
    paging, no second file, just a longer one."""
    rows = max(1, math.ceil(count / COLUMNS))
    height = MARGIN + header_height + rows * CARD_HEIGHT + (rows - 1) * GAP + MARGIN
    return WIDTH, height


def render_standings_image(
    standings,
    media_dir,
    out_path,
    title: str = "Итоги недели",
    subtitle: str = "",
    show_votes: bool = True,
) -> Path:
    """Draws `standings` -- a voting.Poll.tally() result, i.e. (Entry, votes) pairs already
    ranked most-votes-first -- into one picture at `out_path`, and returns that path.

    The order is taken as given, never re-sorted here: tally() is what the page ranks by
    and what the announcement text numbers, so the export ranking anything for itself is
    how the three of them would start disagreeing.

    `media_dir` is the poll's own photo directory (voting.media_path); only each entry's
    FIRST photo is drawn, exactly as the page's grid does with an album.

    Raises ValueError on empty standings -- a board of nothing is not a picture worth
    writing, and the caller has something better to say about it than a blank file.
    """
    standings = list(standings)
    if not standings:
        raise ValueError("nothing to render -- no admitted entries")

    media_dir = Path(media_dir)
    out_path = Path(out_path)

    # The header is measured before the canvas exists (its height decides how tall that
    # canvas is), so it's measured against a throwaway one-pixel image.
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    title_font, subtitle_font = _font(TITLE_SIZE, bold=True), _font(SUBTITLE_SIZE)
    title_height = _text_size(probe, title, title_font)[1] if title else 0
    subtitle_height = _text_size(probe, subtitle, subtitle_font)[1] if subtitle else 0
    header_height = 0
    if title:
        header_height += title_height + 14
    if subtitle:
        header_height += subtitle_height + 10
    if header_height:
        header_height += 14  # breathing room between the header and the first row

    width, height = image_size(len(standings), header_height)
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

    mask = _rounded_mask()
    for index, (entry, votes) in enumerate(standings):
        column, row = index % COLUMNS, index // COLUMNS
        x = MARGIN + column * (CARD_WIDTH + GAP)
        top = y + row * (CARD_HEIGHT + GAP)
        canvas.paste(_draw_card(entry, votes, media_dir, show_votes), (x, top), mask)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.suffix.lower() in (".jpg", ".jpeg") and height > _JPEG_MAX_SIDE:
        out_path = out_path.with_suffix(".png")
    if out_path.suffix.lower() in (".jpg", ".jpeg"):
        canvas.save(out_path, "JPEG", quality=92, optimize=True, progressive=True)
    else:
        canvas.save(out_path)
    return out_path


def render_poll_image(poll, out_path, title: str = "Итоги недели", subtitle: str = "") -> Path:
    """render_standings_image for a whole voting.Poll: its own tally, its own photos.

    Only ADMITTED entries appear, because tally() only ranks those -- the export is a
    picture of the vote, not of everything that was ever nominated.
    """
    return render_standings_image(
        poll.tally(),
        voting.media_path(poll.entry, poll.poll_id),
        out_path,
        title=title,
        subtitle=subtitle,
    )
