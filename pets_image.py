"""A single shareable fight-result image, composed from Telegram-hosted pet media."""

from io import BytesIO
import os
from pathlib import Path
import tempfile
import unicodedata
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont, ImageOps


WIDTH = 1280
HEIGHT = 1020
PANEL_WIDTH = 550
PANEL_TOP = 100
PANEL_BOTTOM = 790
PANEL_PADDING_X = 40
PET_IMAGE_SIZE = (470, 365)
PET_IMAGE_TOP = PANEL_TOP + 25
AVATAR_TOP = PET_IMAGE_TOP + PET_IMAGE_SIZE[1] + 15
PET_NAME_TOP = AVATAR_TOP + 93
RATING_TOP = PET_NAME_TOP + 49
STATS_BOTTOM_PADDING = 18
STAT_ROW_HEIGHT = 20
STAT_LABEL_FONT_SIZE = 16
STAT_VALUE_FONT_SIZE = 18
_FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
)
_BOLD_FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/ARIALBD.TTF",
)
WINNER_NAME_COLOR = "#147a59"
LOSER_NAME_COLOR = "#b83e58"
_UNRENDERABLE_PROBE = "͸"


@lru_cache(maxsize=None)
def _font(size: int, bold: bool = False):
    paths = _BOLD_FONT_PATHS if bold else _FONT_PATHS
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


@lru_cache(maxsize=None)
def _notdef_bitmap(size: int, bold: bool) -> bytes | None:
    try:
        return bytes(_font(size, bold).getmask(_UNRENDERABLE_PROBE))
    except Exception:
        return None


def _renders(character: str, size: int, bold: bool) -> bool:
    notdef = _notdef_bitmap(size, bold)
    if notdef is None:
        return True
    try:
        return bytes(_font(size, bold).getmask(character)) != notdef
    except Exception:
        return False


def legible(value, size: int, bold: bool = False) -> str:
    """Normalize names and remove glyphs the result-image font cannot render."""
    normalized = unicodedata.normalize("NFKC", str(value or "").strip())
    return "".join(
        character
        for character in normalized
        if character.isspace() or _renders(character, size, bold)
    ).strip()


def _photo(
    data: bytes | None,
    size: tuple[int, int],
    fallback: tuple[int, int, int],
    *,
    crop: bool = True,
) -> Image.Image:
    """Decode media into a fixed box, optionally preserving its full composition."""
    if data:
        try:
            image = Image.open(BytesIO(data)).convert("RGB")
            if crop:
                return ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
            contained = ImageOps.contain(image, size, method=Image.Resampling.LANCZOS)
            fitted = Image.new("RGB", size, fallback)
            fitted.paste(
                contained,
                ((size[0] - contained.width) // 2, (size[1] - contained.height) // 2),
            )
            return fitted
        except Exception:
            pass
    return Image.new("RGB", size, fallback)


def _circle(image: Image.Image, diameter: int) -> Image.Image:
    image = ImageOps.fit(image, (diameter, diameter), method=Image.Resampling.LANCZOS)
    mask = Image.new("L", (diameter, diameter), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, diameter - 1, diameter - 1), fill=255)
    image.putalpha(mask)
    return image


def _short(value, limit: int = 19, size: int = 39, bold: bool = True) -> str:
    value = legible(value, size, bold=bold) or "Без имени"
    return value if len(value) <= limit else f"{value[:limit - 1]}..."


def _center(draw: ImageDraw.ImageDraw, y: int, text: str, font, fill) -> None:
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(((WIDTH - (box[2] - box[0])) / 2, y), text, font=font, fill=fill)


def _stats_top(row_count: int) -> int:
    return PANEL_BOTTOM - STATS_BOTTOM_PADDING - row_count * STAT_ROW_HEIGHT


def _fighter_panel(draw, image, x, fighter, side: str, winner: bool) -> None:
    panel_color = "#e4f2eb" if side == "left" else "#f8e6e8"
    border = "#0e553f" if winner else "#c1c9c5"
    name_color = WINNER_NAME_COLOR if winner else LOSER_NAME_COLOR
    draw.rounded_rectangle(
        (x, PANEL_TOP, x + PANEL_WIDTH, PANEL_BOTTOM),
        radius=8,
        fill=panel_color,
        outline=border,
        width=6,
    )

    pet = _photo(
        fighter.get("pet_photo"), PET_IMAGE_SIZE,
        (43, 111, 82) if side == "left" else (151, 57, 78), crop=False,
    )
    image.paste(pet, (x + PANEL_PADDING_X, PET_IMAGE_TOP))
    avatar = _circle(_photo(fighter.get("owner_avatar"), (90, 90), (82, 97, 108)), 78)
    image.paste(avatar, (x + PANEL_PADDING_X, AVATAR_TOP), avatar)
    draw.ellipse(
        (x + PANEL_PADDING_X - 2, AVATAR_TOP - 2, x + PANEL_PADDING_X + 80, AVATAR_TOP + 80),
        outline="#ffffff", width=4,
    )
    draw.text(
        (x + 135, AVATAR_TOP + 9), _short(fighter.get("owner_name"), 24, size=23, bold=False), font=_font(23),
        fill=name_color,
    )
    draw.text(
        (x + PANEL_PADDING_X, PET_NAME_TOP), _short(fighter.get("pet_name"), 24, size=36), font=_font(36, bold=True),
        fill=name_color,
    )
    draw.text(
        (x + PANEL_PADDING_X, RATING_TOP), f"РЕЙТИНГ  {fighter.get('power', 0)}",
        font=_font(20, bold=True), fill="#37434c",
    )

    rows = (
        ("СИЛА", "strength"), ("ЗДОРОВЬЕ", "health"), ("ЛОВКОСТЬ", "agility"),
        ("УДАЧА", "luck"), ("БРОНЯ", "armor"),
    )
    stats = fighter.get("stats") or {}
    stats_top = _stats_top(len(rows))
    for index, (label, key) in enumerate(rows):
        y = stats_top + index * STAT_ROW_HEIGHT
        draw.text((x + PANEL_PADDING_X, y), label, font=_font(STAT_LABEL_FONT_SIZE), fill="#53606a")
        value = str(stats.get(key, 0))
        box = draw.textbbox((0, 0), value, font=_font(STAT_VALUE_FONT_SIZE, bold=True))
        draw.text(
            (x + PANEL_WIDTH - 50 - (box[2] - box[0]), y - 2), value,
            font=_font(STAT_VALUE_FONT_SIZE, bold=True), fill="#20272c",
        )


def render_fight_result(path, result, attacker: dict, defender: dict) -> Path:
    """Write one JPEG result board and return its path. Inputs may omit any media bytes."""
    image = Image.new("RGB", (WIDTH, HEIGHT), "#f6f2ea")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, WIDTH, 84), fill="#17372f")
    _center(draw, 19, "АРЕНА", _font(34, bold=True), "#ffffff")

    winner = result.winner
    _fighter_panel(draw, image, 45, attacker, "left", winner == attacker.get("id"))
    _fighter_panel(draw, image, 685, defender, "right", winner == defender.get("id"))

    draw.ellipse((548, 348, 732, 532), fill="#f4b63f", outline="#ffffff", width=7)
    _center(draw, 394, "VS", _font(54, bold=True), "#26343a")

    damage = result.total_damage
    left_damage = damage.get(attacker.get("id"), 0)
    right_damage = damage.get(defender.get("id"), 0)
    outcome = "НИЧЬЯ" if result.is_draw else "ПОБЕДА"
    outcome_color = "#6c4f9b" if result.is_draw else "#147a59"
    _center(draw, 805, outcome, _font(36, bold=True), outcome_color)
    _center(draw, 850, "НАНЕСЕНО УРОНА", _font(18, bold=True), "#5c666c")
    draw.text((518, 875), str(left_damage), font=_font(43, bold=True), fill="#147a59")
    draw.text((684, 875), str(right_damage), font=_font(43, bold=True), fill="#b83e58")
    _center(draw, 933, "НОКАУТ" if not result.stopped_early and not result.is_draw else "10 АТАК", _font(18, bold=True), "#5c666c")

    if winner:
        winner_name = attacker.get("pet_name") if winner == attacker.get("id") else defender.get("pet_name")
        _center(
            draw, 965, f"ПОБЕДИТЕЛЬ: {_short(winner_name, 22, size=25).upper()}",
            _font(25, bold=True), WINNER_NAME_COLOR,
        )
    else:
        _center(draw, 965, "ОДИНАКОВЫЙ УРОН", _font(25, bold=True), "#26343a")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="JPEG", quality=91, optimize=True)
    return path


def temporary_result_path() -> Path:
    descriptor, path = tempfile.mkstemp(prefix="pet_fight_", suffix=".jpg")
    os.close(descriptor)
    return Path(path)
