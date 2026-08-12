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


def _draw_item_title(
    draw, left: int, y: int, label: str, item: dict | None, max_width: int | None = None,
) -> None:
    rarity = str((item or {}).get("rarity") or "common")
    symbol, color = RARITY_SYMBOLS.get(rarity, RARITY_SYMBOLS["common"])
    symbol_font = _font(16, bold=True)
    draw.text((left, y - 1), symbol, font=symbol_font, fill=color)
    name = (item or {}).get("name") or "не надето"
    font = _font(15, bold=True)
    text = _fit_text(draw, f"{label}  {name}", font, (max_width or PET_IMAGE_SIZE[0]) - 25)
    draw.text((left + 23, y), text, font=font, fill="#273137")


def _draw_equipment(draw, x: int, fighter: dict) -> None:
    left = x + PANEL_PADDING_X
    right = left + PET_IMAGE_SIZE[0]
    weapon = fighter.get("weapon")
    amulet = fighter.get("amulet")
    shield = fighter.get("shield")

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

    # Amulet and shield share one compact row. The round log carries the complete prose;
    # the receipt only needs to make both equipped effects visible at a glance.
    column = PET_IMAGE_SIZE[0] // 2
    shield_left = left + column
    _draw_item_title(draw, left, AMULET_NAME_TOP, "ТАЛИСМАН", amulet, column - 6)
    _draw_item_title(draw, shield_left, AMULET_NAME_TOP, "ЩИТ", shield, column)
    effect = (amulet or {}).get("effect") or "без эффекта"
    effect_text = _fit_text(draw, f"♦ {effect}", _font(13), column - 8)
    draw.text((left, AMULET_EFFECT_TOP), effect_text, font=_font(13), fill="#53606a")
    shield_effect = (shield or {}).get("effect") or "без эффекта"
    shield_text = _fit_text(draw, f"♦ {shield_effect}", _font(13), column)
    draw.text((shield_left, AMULET_EFFECT_TOP), shield_text, font=_font(13), fill="#53606a")
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
    _center(
        draw, 933,
        "НОКАУТ" if not result.stopped_early and not result.is_draw else "ПО ЛИМИТУ",
        _font(18, bold=True), "#5c666c",
    )

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


# ------------------------------------------------------------------- the battle log

# The transcript board is a second picture posted alongside the result. Its only job is
# to make "who hit whom" readable at a glance, so the two fighters are told apart by
# colour rather than by reading names: the attacker (whoever opened the fight) is red
# and the defender blue -- stripe, name and damage all agree. The header repeats both
# names in those colours, which doubles as the legend.
LOG_ATTACKER_COLOR = "#b3382c"
LOG_DEFENDER_COLOR = "#2f5d9e"
LOG_ATTACKER_TINT = "#fbeeec"
LOG_DEFENDER_TINT = "#ecf1fa"
LOG_MARGIN_X = 60
LOG_HEADER_HEIGHT = 84
LOG_TITLE_TOP = 19
LOG_LEGEND_TOP = LOG_HEADER_HEIGHT + 26
LOG_OPENING_TOP = LOG_LEGEND_TOP + 40
LOG_ROWS_TOP = LOG_OPENING_TOP + 40
# The gap the rows are separated by, so consecutive blows never read as one block.
LOG_ROW_GAP = 14
LOG_ROW_PADDING = 13
LOG_STRIPE_WIDTH = 6
LOG_TEXT_LINE_HEIGHT = 21
LOG_RIGHT_COLUMN = 190
LOG_BOTTOM_PADDING = 34
# A ten-round fight where both pets carry a per-turn passive can produce fifty-odd
# transcript rows, and a contrived one nearly a hundred -- enough to push the board past
# Telegram's 10000px sum-of-sides limit, which would fail the send and cost the player
# the result board too. Keeping the opening and the finish and eliding the middle bounds
# the height while preserving both parts anyone actually reads.
LOG_MAX_ROWS = 26


def _wrap(draw: ImageDraw.ImageDraw, value, font, max_width: int) -> list[str]:
    """Break one transcript line into as many rows as it needs, on word boundaries."""
    text = legible(value, getattr(font, "size", 16))
    if not text:
        return []
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and draw.textbbox((0, 0), candidate, font=font)[2] > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _log_entries(result, attacker: dict, defender: dict) -> list[dict]:
    """Flatten the combat transcript into rows ready to draw, in fight order."""
    attacker_key = str(attacker.get("id"))
    entries = []
    for round_ in result.rounds:
        key = str(round_.attacker)
        is_attacker = key == attacker_key
        owner = attacker if is_attacker else defender
        # A passive/self-only skill belongs to whoever triggered it, so its remaining
        # health is the interesting number; an attack or damaging spell shows the target.
        event = str(round_.event or "")
        passive = (
            event.startswith("amulet_") or event == "defend"
            or (event.startswith("skill_") and event != "skill_dodge" and round_.damage <= 0)
        )
        entries.append({
            "color": LOG_ATTACKER_COLOR if is_attacker else LOG_DEFENDER_COLOR,
            "tint": LOG_ATTACKER_TINT if is_attacker else LOG_DEFENDER_TINT,
            "name": _short(owner.get("pet_name"), 22, size=17),
            "round": round_.number,
            "damage": int(round_.damage or 0),
            "health": round_.attacker_hp if passive else round_.defender_hp,
            "passive": passive,
            "text": round_.text,
        })
    if len(entries) > LOG_MAX_ROWS:
        head, tail = LOG_MAX_ROWS // 2, LOG_MAX_ROWS - LOG_MAX_ROWS // 2
        hidden = len(entries) - LOG_MAX_ROWS
        entries = [
            *entries[:head],
            {"elision": f"пропущено событий: {hidden}"},
            *entries[-tail:],
        ]
    return entries


def render_fight_log(path, result, attacker: dict, defender: dict) -> Path:
    """Write the round-by-round transcript board and return its path.

    Sized to its content rather than to a fixed canvas: a fight runs anywhere from one
    blow to twenty plus procs, and a fixed height would either clip the long ones or
    leave the short ones mostly empty.
    """
    ruler = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    name_font, text_font = _font(17, bold=True), _font(16)
    meta_font, damage_font = _font(13), _font(20, bold=True)
    content_width = WIDTH - 2 * LOG_MARGIN_X
    text_width = content_width - LOG_STRIPE_WIDTH - 2 * LOG_ROW_PADDING - LOG_RIGHT_COLUMN

    entries = _log_entries(result, attacker, defender)
    for entry in entries:
        if entry.get("elision"):
            entry["height"] = LOG_TEXT_LINE_HEIGHT + 8
            continue
        entry["lines"] = _wrap(ruler, entry["text"], text_font, text_width) or ["—"]
        entry["height"] = (
            2 * LOG_ROW_PADDING + 24 + len(entry["lines"]) * LOG_TEXT_LINE_HEIGHT
        )

    closing = _wrap(ruler, result.closing, text_font, content_width - 2 * LOG_ROW_PADDING)
    rows_height = sum(entry["height"] + LOG_ROW_GAP for entry in entries)
    height = (
        LOG_ROWS_TOP + rows_height
        + (len(closing) * LOG_TEXT_LINE_HEIGHT + 20 if closing else 0)
        + LOG_BOTTOM_PADDING
    )

    image = Image.new("RGB", (WIDTH, int(height)), "#f6f2ea")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, WIDTH, LOG_HEADER_HEIGHT), fill="#17372f")
    _center(draw, LOG_TITLE_TOP, "ХОД БОЯ", _font(34, bold=True), "#ffffff")

    # Both names in their own colour: the legend and the first data point at once.
    left_name = _short(attacker.get("pet_name"), 22, size=20)
    right_name = _short(defender.get("pet_name"), 22, size=20)
    legend_font = _font(20, bold=True)
    separator = "  против  "
    widths = [
        draw.textbbox((0, 0), part, font=legend_font)[2]
        for part in (left_name, separator, right_name)
    ]
    cursor = (WIDTH - sum(widths)) / 2
    for part, width, colour in zip(
        (left_name, separator, right_name), widths,
        (LOG_ATTACKER_COLOR, "#53606a", LOG_DEFENDER_COLOR),
    ):
        draw.text((cursor, LOG_LEGEND_TOP), part, font=legend_font, fill=colour)
        cursor += width

    opening = _fit_text(draw, result.opening, _font(16), content_width)
    _center(draw, LOG_OPENING_TOP, opening, _font(16), "#53606a")

    y = LOG_ROWS_TOP
    for entry in entries:
        left, right = LOG_MARGIN_X, WIDTH - LOG_MARGIN_X
        bottom = y + entry["height"]
        if entry.get("elision"):
            _center(draw, y + 4, entry["elision"], meta_font, "#8b959a")
            y = bottom + LOG_ROW_GAP
            continue
        draw.rectangle((left, y, right, bottom), fill=entry["tint"])
        draw.rectangle((left, y, left + LOG_STRIPE_WIDTH, bottom), fill=entry["color"])

        text_left = left + LOG_STRIPE_WIDTH + LOG_ROW_PADDING
        heading = f"РАУНД {entry['round']}  ·  {entry['name']}" if entry["round"] else entry["name"]
        draw.text(
            (text_left, y + LOG_ROW_PADDING),
            f"♦ {heading}" if entry["passive"] else heading,
            font=name_font, fill=entry["color"],
        )
        for index, line in enumerate(entry["lines"]):
            draw.text(
                (text_left, y + LOG_ROW_PADDING + 24 + index * LOG_TEXT_LINE_HEIGHT),
                line, font=text_font, fill="#3a464d",
            )

        # Only a blow gets the big damage figure. A proc's number is a shield, a heal or
        # a reflect just as often as damage, so rendering it as "-164" would say the
        # opposite of what happened; its own line already carries the signed amount.
        if not entry["passive"]:
            damage = f"-{entry['damage']}" if entry["damage"] > 0 else "—"
            box = draw.textbbox((0, 0), damage, font=damage_font)
            draw.text(
                (right - LOG_ROW_PADDING - (box[2] - box[0]), y + LOG_ROW_PADDING),
                damage, font=damage_font, fill=entry["color"],
            )
        health = f"HP {max(0, int(entry['health']))}"
        box = draw.textbbox((0, 0), health, font=meta_font)
        draw.text(
            (right - LOG_ROW_PADDING - (box[2] - box[0]), y + LOG_ROW_PADDING + 27),
            health, font=meta_font, fill="#53606a",
        )
        y = bottom + LOG_ROW_GAP

    for index, line in enumerate(closing):
        _center(draw, y + 8 + index * LOG_TEXT_LINE_HEIGHT, line, text_font, "#53606a")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="JPEG", quality=91, optimize=True)
    return path


def temporary_log_path() -> Path:
    descriptor, path = tempfile.mkstemp(prefix="pet_fight_log_", suffix=".jpg")
    os.close(descriptor)
    return Path(path)
