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
# The HP strip sits directly below the un-cropped pet image.  The compact profile and
# stat rows below it deliberately reserve the lower part of the panel for readability.
HP_BAR_TOP = PET_IMAGE_TOP + PET_IMAGE_SIZE[1] + 8
HP_BAR_HEIGHT = 24
AVATAR_DIAMETER = 58
AVATAR_TOP = PET_IMAGE_TOP + PET_IMAGE_SIZE[1] - AVATAR_DIAMETER - 10
# Rare and legendary weapons carry a passive just like an amulet does, so the weapon
# block needs its own effect line -- otherwise the receipt shows flat stats only and the
# passive that decided the fight is invisible.  The panel cannot simply grow: the stat
# rows are anchored up from PANEL_BOTTOM and the outcome caption sits right below it.
# The extra line is paid for by trimming a few points from each gap in this block, so
# STATS_DIVIDER_TOP lands exactly where it did before.
WEAPON_NAME_TOP = HP_BAR_TOP + HP_BAR_HEIGHT + 4
WEAPON_STATS_TOP = WEAPON_NAME_TOP + 18
WEAPON_EFFECT_TOP = WEAPON_STATS_TOP + 18
WEAPON_DIVIDER_TOP = WEAPON_EFFECT_TOP + 18
AMULET_NAME_TOP = WEAPON_DIVIDER_TOP + 5
AMULET_EFFECT_TOP = AMULET_NAME_TOP + 18
EQUIPMENT_DIVIDER_TOP = AMULET_EFFECT_TOP + 19
PET_NAME_TOP = EQUIPMENT_DIVIDER_TOP + 7
OWNER_NAME_TOP = PET_NAME_TOP + 27
STATS_DIVIDER_TOP = OWNER_NAME_TOP + 19
STATS_BOTTOM_PADDING = 17
STAT_ROW_HEIGHT = 18
STAT_LABEL_FONT_SIZE = 14
STAT_VALUE_FONT_SIZE = 16
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
RARITY_SYMBOLS = {
    "cursed": ("♠", "#5e5367"),
    "common": ("○", "#7b8589"),
    "uncommon": ("●", "#3a8b58"),
    "rare": ("♦", "#3179b8"),
    "legendary": ("▲", "#d19a24"),
}
STAT_SYMBOLS = {
    "strength": "†",
    "health": "♥",
    "agility": "→",
    "luck": "♣",
    "armor": "■",
}


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


def _fit_text(draw: ImageDraw.ImageDraw, value, font, max_width: int) -> str:
    """Keep one equipment line inside its panel without wrapping into the next block."""
    text = legible(value, getattr(font, "size", 14)) or "—"
    if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
        return text
    suffix = "..."
    while text and draw.textbbox((0, 0), text + suffix, font=font)[2] > max_width:
        text = text[:-1]
    return (text.rstrip() + suffix) if text else suffix


def _equipment_bonus_text(item: dict | None) -> str:
    if not item:
        return "—"
    bonuses = item.get("bonuses") or {}
    parts = [
        f"{STAT_SYMBOLS.get(key, '•')} {int(value):+d}"
        for key, value in bonuses.items()
        if key in STAT_SYMBOLS
    ]
    return "   ".join(parts) or "—"


def _draw_item_title(draw, left: int, y: int, label: str, item: dict | None) -> None:
    rarity = str((item or {}).get("rarity") or "common")
    symbol, color = RARITY_SYMBOLS.get(rarity, RARITY_SYMBOLS["common"])
    symbol_font = _font(16, bold=True)
    draw.text((left, y - 1), symbol, font=symbol_font, fill=color)
    name = (item or {}).get("name") or "не надето"
    font = _font(15, bold=True)
    text = _fit_text(draw, f"{label}  {name}", font, PET_IMAGE_SIZE[0] - 25)
    draw.text((left + 23, y), text, font=font, fill="#273137")


def _draw_equipment(draw, x: int, fighter: dict) -> None:
    left = x + PANEL_PADDING_X
    right = left + PET_IMAGE_SIZE[0]
    weapon = fighter.get("weapon")
    amulet = fighter.get("amulet")

    _draw_item_title(draw, left, WEAPON_NAME_TOP, "ОРУЖИЕ", weapon)
    weapon_stats = _fit_text(
        draw, _equipment_bonus_text(weapon), _font(14, bold=True), PET_IMAGE_SIZE[0],
    )
    draw.text((left, WEAPON_STATS_TOP), weapon_stats, font=_font(14, bold=True), fill="#53606a")
    weapon_effect = (weapon or {}).get("effect")
    if weapon_effect:
        effect_line = _fit_text(draw, f"♦ {weapon_effect}", _font(13), PET_IMAGE_SIZE[0])
        draw.text((left, WEAPON_EFFECT_TOP), effect_line, font=_font(13), fill="#53606a")
    draw.line((left, WEAPON_DIVIDER_TOP, right, WEAPON_DIVIDER_TOP), fill="#c4cbc8", width=1)

    _draw_item_title(draw, left, AMULET_NAME_TOP, "ТАЛИСМАН", amulet)
    effect = (amulet or {}).get("effect") or "без эффекта"
    effect_text = _fit_text(draw, f"♦ {effect}", _font(13), PET_IMAGE_SIZE[0])
    draw.text((left, AMULET_EFFECT_TOP), effect_text, font=_font(13), fill="#53606a")
    draw.line((left, EQUIPMENT_DIVIDER_TOP, right, EQUIPMENT_DIVIDER_TOP), fill="#9ca8a4", width=2)


def _hp_values(fighter: dict) -> tuple[int, int] | None:
    """Return safe display HP when the combat receipt supplied it.

    Older receipts and image unit tests may not carry a health snapshot; leaving the
    strip out is more honest than inventing a value from potentially post-level-up
    stats.
    """
    try:
        remaining = round(float(fighter["remaining_hp"]))
        maximum = round(float(fighter["max_hp"]))
    except (KeyError, TypeError, ValueError):
        return None
    if maximum <= 0:
        return None
    return max(0, min(remaining, maximum)), maximum


def _draw_hp_bar(draw: ImageDraw.ImageDraw, x: int, fighter: dict) -> None:
    values = _hp_values(fighter)
    if values is None:
        return
    remaining, maximum = values
    left = x + PANEL_PADDING_X
    right = left + PET_IMAGE_SIZE[0]
    top = HP_BAR_TOP
    bottom = top + HP_BAR_HEIGHT
    draw.rounded_rectangle((left, top, right, bottom), radius=7, fill="#aeb6b5")
    filled_right = left + round((right - left) * remaining / maximum)
    if filled_right > left:
        draw.rounded_rectangle((left, top, filled_right, bottom), radius=7, fill="#c8444b")
    draw.rounded_rectangle((left, top, right, bottom), radius=7, outline="#7f8987", width=1)
    label = f"{remaining} / {maximum} HP"
    # The result is usually viewed at roughly half its source resolution in Telegram;
    # 16 px here remains readable after that downscale.
    font = _font(16, bold=True)
    box = draw.textbbox((0, 0), label, font=font)
    draw.text(
        (left + ((right - left) - (box[2] - box[0])) / 2, top + 1),
        label, font=font, fill="#ffffff",
    )


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
    _draw_hp_bar(draw, x, fighter)
    avatar_left = x + PANEL_PADDING_X + PET_IMAGE_SIZE[0] - AVATAR_DIAMETER - 10
    avatar = _circle(
        _photo(fighter.get("owner_avatar"), (AVATAR_DIAMETER, AVATAR_DIAMETER), (82, 97, 108)),
        AVATAR_DIAMETER,
    )
    image.paste(avatar, (avatar_left, AVATAR_TOP), avatar)
    draw.ellipse(
        (avatar_left - 2, AVATAR_TOP - 2, avatar_left + AVATAR_DIAMETER + 2,
         AVATAR_TOP + AVATAR_DIAMETER + 2), outline="#ffffff", width=3,
    )
    _draw_equipment(draw, x, fighter)
    draw.text(
        (x + PANEL_PADDING_X, PET_NAME_TOP), _short(fighter.get("pet_name"), 27, size=23),
        font=_font(23, bold=True),
        fill=name_color,
    )
    draw.text(
        (x + PANEL_PADDING_X, OWNER_NAME_TOP),
        f"ХОЗЯИН  {_short(fighter.get('owner_name'), 28, size=14, bold=False)}",
        font=_font(14), fill="#53606a",
    )
    draw.line(
        (x + PANEL_PADDING_X, STATS_DIVIDER_TOP,
         x + PANEL_PADDING_X + PET_IMAGE_SIZE[0], STATS_DIVIDER_TOP),
        fill="#c4cbc8", width=1,
    )

    rows = (
        ("СИЛА", "strength"), ("ЗДОРОВЬЕ", "health"), ("ЛОВКОСТЬ", "agility"),
        ("УДАЧА", "luck"), ("БРОНЯ", "armor"),
    )
    stats = fighter.get("stats") or {}
    stats_top = _stats_top(len(rows))
    for index, (label, key) in enumerate(rows):
        y = stats_top + index * STAT_ROW_HEIGHT
        icon = STAT_SYMBOLS.get(key, "•")
        draw.text(
            (x + PANEL_PADDING_X, y), f"{icon}  {label}",
            font=_font(STAT_LABEL_FONT_SIZE), fill="#53606a",
        )
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
