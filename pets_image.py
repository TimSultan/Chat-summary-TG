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


def _photo(data: bytes | None, size: tuple[int, int], fallback: tuple[int, int, int]) -> Image.Image:
    if data:
        try:
            image = Image.open(BytesIO(data)).convert("RGB")
            return ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
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


def _fighter_panel(draw, image, x, fighter, side: str, winner: bool) -> None:
    panel_width = 550
    panel_color = "#e4f2eb" if side == "left" else "#f8e6e8"
    accent = "#14805e" if side == "left" else "#b83e58"
    border = "#0e553f" if winner else "#c1c9c5"
    draw.rounded_rectangle((x, 110, x + panel_width, 780), radius=8, fill=panel_color, outline=border, width=6)

    pet = _photo(fighter.get("pet_photo"), (470, 270), (43, 111, 82) if side == "left" else (151, 57, 78))
    image.paste(pet, (x + 40, 145))
    avatar = _circle(_photo(fighter.get("owner_avatar"), (90, 90), (82, 97, 108)), 78)
    image.paste(avatar, (x + 40, 435), avatar)
    draw.ellipse((x + 38, 433, x + 120, 515), outline="#ffffff", width=4)
    draw.text(
        (x + 135, 444), _short(fighter.get("owner_name"), 24, size=25, bold=False), font=_font(25),
        fill=WINNER_NAME_COLOR if winner else "#243039",
    )
    draw.text(
        (x + 40, 525), _short(fighter.get("pet_name"), 24), font=_font(39, bold=True),
        fill=WINNER_NAME_COLOR if winner else accent,
    )
    draw.text((x + 40, 580), f"РЕЙТИНГ  {fighter.get('power', 0)}", font=_font(22, bold=True), fill="#37434c")

    rows = (
        ("СИЛА", "strength"), ("ЗДОРОВЬЕ", "health"), ("ЛОВКОСТЬ", "agility"),
        ("УДАЧА", "luck"), ("БРОНЯ", "armor"),
    )
    stats = fighter.get("stats") or {}
    for index, (label, key) in enumerate(rows):
        y = 620 + index * 27
        draw.text((x + 40, y), label, font=_font(18), fill="#53606a")
        value = str(stats.get(key, 0))
        box = draw.textbbox((0, 0), value, font=_font(20, bold=True))
        draw.text((x + 500 - (box[2] - box[0]), y - 2), value, font=_font(20, bold=True), fill="#20272c")


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
