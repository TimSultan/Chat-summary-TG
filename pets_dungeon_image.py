"""Cached floor illustrations for the Telegram dungeon screen."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

import pets_dungeon as D


WIDTH = 1200
HEIGHT = 720

_THEME_COLORS = (
    ((22, 51, 45), (70, 110, 81)),
    ((62, 22, 35), (125, 52, 57)),
    ((79, 58, 29), (161, 119, 55)),
    ((45, 32, 67), (98, 67, 126)),
    ((18, 57, 75), (44, 123, 139)),
)
_MONSTER_COLORS = ((157, 75, 65), (106, 148, 80), (113, 92, 159))


def _cache_dir() -> Path:
    return Path(__file__).resolve().parent / "cache" / "dungeon_floors"


def _gradient(image: Image.Image, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> None:
    pixels = image.load()
    for y in range(HEIGHT):
        progress = y / (HEIGHT - 1)
        color = tuple(round(top[index] + (bottom[index] - top[index]) * progress) for index in range(3))
        for x in range(WIDTH):
            pixels[x, y] = color


def _corridor(draw: ImageDraw.ImageDraw, floor: int) -> None:
    horizon = 240
    draw.polygon(((0, HEIGHT), (WIDTH, HEIGHT), (760, horizon), (440, horizon)), fill=(21, 26, 31))
    for offset in range(1, 7):
        y = horizon + (HEIGHT - horizon) * offset / 7
        draw.line((0, y, WIDTH, y), fill=(66, 72, 70), width=4)
    for x in range(0, WIDTH + 1, 120):
        draw.line((600, horizon, x, HEIGHT), fill=(52, 58, 59), width=3)
    for x in (90, 1110):
        draw.ellipse((x - 30, 110, x + 30, 170), fill=(237, 176, 72))
        draw.ellipse((x - 15, 125, x + 15, 155), fill=(255, 230, 139))
    draw.rectangle((0, 0, WIDTH, 12), fill=(13, 16, 19))
    draw.rectangle((0, HEIGHT - 12, WIDTH, HEIGHT), fill=(13, 16, 19))


def _enemy(draw: ImageDraw.ImageDraw, center_x: int, baseline: int, scale: float,
           color: tuple[int, int, int], variant: int) -> None:
    body_width = round(150 * scale)
    body_height = round(190 * scale)
    head_radius = round(58 * scale)
    head_y = baseline - body_height
    draw.ellipse((center_x - body_width // 2, baseline - body_height // 2,
                  center_x + body_width // 2, baseline + body_height // 3), fill=color, outline=(20, 18, 24), width=7)
    draw.ellipse((center_x - head_radius, head_y - head_radius,
                  center_x + head_radius, head_y + head_radius), fill=color, outline=(20, 18, 24), width=7)
    eye_y = head_y - round(6 * scale)
    eye_offset = round(22 * scale)
    eye_radius = max(5, round(10 * scale))
    for eye_x in (center_x - eye_offset, center_x + eye_offset):
        draw.ellipse((eye_x - eye_radius, eye_y - eye_radius, eye_x + eye_radius, eye_y + eye_radius), fill=(247, 230, 158))
        draw.ellipse((eye_x - 3, eye_y - 3, eye_x + 3, eye_y + 3), fill=(25, 20, 26))
    if variant == 0:
        draw.polygon(((center_x - 42, head_y - head_radius + 12), (center_x - 18, head_y - head_radius - 38),
                      (center_x + 3, head_y - head_radius + 4)), fill=(43, 35, 43))
        draw.polygon(((center_x + 12, head_y - head_radius + 4), (center_x + 38, head_y - head_radius - 35),
                      (center_x + 52, head_y - head_radius + 14)), fill=(43, 35, 43))
    elif variant == 1:
        for shift in (-40, 40):
            draw.line((center_x + shift, baseline - 60, center_x + shift * 2, baseline + 50), fill=(32, 49, 32), width=12)
    else:
        draw.polygon(((center_x - 65, baseline - 30), (center_x - 120, baseline + 65),
                      (center_x - 20, baseline + 20)), fill=(52, 39, 72))
        draw.polygon(((center_x + 65, baseline - 30), (center_x + 120, baseline + 65),
                      (center_x + 20, baseline + 20)), fill=(52, 39, 72))


def _regular_floor(image: Image.Image, floor: int) -> None:
    draw = ImageDraw.Draw(image)
    positions = ((270, 535, .86), (600, 555, 1.12), (930, 535, .86))
    for index, (x, y, scale) in enumerate(positions):
        _enemy(draw, x, y, scale, _MONSTER_COLORS[(floor + index) % len(_MONSTER_COLORS)], index)


def _boss_floor(image: Image.Image, floor: int) -> None:
    draw = ImageDraw.Draw(image)
    color = _MONSTER_COLORS[(floor // 5) % len(_MONSTER_COLORS)]
    _enemy(draw, 600, 590, 1.8, color, (floor // 5) % 3)
    draw.ellipse((270, 90, 930, 680), outline=(222, 177, 81), width=10)
    draw.ellipse((290, 110, 910, 660), outline=(83, 54, 83), width=5)


def render_floor(path: Path, floor: int) -> Path:
    """Render one stable scene. Regular floors have three enemies; boss floors one boss."""
    floor = max(1, int(floor))
    theme_index = ((floor - 1) // 3) % len(_THEME_COLORS)
    image = Image.new("RGB", (WIDTH, HEIGHT))
    _gradient(image, *_THEME_COLORS[theme_index])
    _corridor(ImageDraw.Draw(image), floor)
    if D.is_boss_floor(floor):
        _boss_floor(image, floor)
    else:
        _regular_floor(image, floor)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="JPEG", quality=88, optimize=True)
    return path


def floor_image(floor: int) -> Path:
    """Return the cached illustration for a floor, creating it on the first request."""
    floor = max(1, int(floor))
    path = _cache_dir() / f"floor_{floor}.jpg"
    return path if path.is_file() else render_floor(path, floor)