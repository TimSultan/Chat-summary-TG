"""The pet game as a Mini App -- a real game client instead of a stack of chat buttons.

pets_ui.py drives the same game through Telegram inline keyboards, and the shape of that
interface is dictated by the medium rather than by the game: one message at a time, six
items to a page because a keyboard is narrow, three separate screens for "what is in this
equipment slot", and a hub you must return to before you can go anywhere else. This module
serves the same game as a page, so the layout can follow the game instead:

  * ONE STATE CALL. GET /api/state returns everything the client draws -- pet, wallet,
    stats, equipment, bag, farm, arena allowance. Every mutation returns the SAME payload,
    freshly computed, so a screen can never drift out of step with the server. There is no
    client-side model of the rules; the client renders what it is told.
  * ONE ACTION ENDPOINT. POST /api/action with {"action": ..., ...} rather than thirty
    routes. The game's mutations all have the same shape in pets.py -- (ok, message) plus
    some extra -- and the same shape here.
  * SCROLLING, NOT PAGING. The bag, the shop, the collection and the ranking are sent
    whole. Their pagination in the chat interface exists because a Telegram keyboard is
    small, not because the data is page-shaped.
  * A LIST OF OPPONENTS, not one at a time. The chat interface rerolls a single candidate
    per tap because it has one message to draw in; a page can show the field and let you
    pick.

WHAT STAYS IN THE CHAT. Taming and changing the pet's photo need a photo, and a Mini App
cannot hand the bot a Telegram file_id -- those two send you back to the DM. Everything
else in the game is here, renaming included.

ITEM ART. There is none yet, so every item is drawn at 210x210 by /img/<code>.svg, which
serves a real file from DATA_DIR/pets/items/<code>.<ext> when one exists and otherwise
generates a placeholder keyed on the item's own code -- rarity colour, slot glyph, a stable
pattern. Dropping real art into that directory replaces a placeholder with no code change.

Mounted onto the same aiohttp application vote_web.py builds (see bot_listener's
_attach_extra), with its own AppKey namespace, the same signed-initData authentication and
the same "photos need no header" exception for the art route.
"""

import asyncio
import hashlib
import json
import os
import re
import secrets
import time
import traceback
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from typing import Awaitable, Callable

from aiohttp import web

import economy
import pets
import pets_combat
import pets_config as C
import pets_updates
import voting
from pets_ui import valuable_item  # the "needs confirming" rarity rule, defined once

ROUTE_PREFIX = "/pets"

_CFG_KEY = web.AppKey("pets_cfg")
_ENTRY_KEY = web.AppKey("pets_entry", str)
_IS_MEMBER_KEY = web.AppKey("pets_is_member", Callable[[dict], Awaitable[bool]])
_RESOLVE_KEY = web.AppKey("pets_resolve_player")
_FETCH_PHOTO_KEY = web.AppKey("pets_fetch_photo")
_SAVE_PHOTO_KEY = web.AppKey("pets_save_photo")
_PREFIX_KEY = web.AppKey("pets_prefix", str)
_LOG_KEY = web.AppKey("pets_log", Callable[..., None])

# Item codes are ASCII and short by construction (w001, amulet_red_button, bt01). Anything
# else never reaches the filesystem -- same two-step guard vote_web.handle_media uses.
_SAFE_CODE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ART_EXTENSIONS = (".png", ".webp", ".jpg", ".jpeg")

# The art tile is square and small on purpose: a bag is a grid of these, and a grid reads
# as a grid only while a row holds several of them on a phone.
ART_SIZE = 210

# Rarity is the game's loudest signal -- it decides a card's border, its glow and its
# placeholder. Kept here rather than read from RARITY_LABELS so the page has real colours
# and not emoji, and ordered worst-to-best so sorting can use the index.
RARITY_ORDER = ("cursed", "common", "uncommon", "rare", "legendary")
RARITY_COLOURS = {
    "cursed": "#6b5b7b",
    "common": "#8a9aa9",
    "uncommon": "#4caf72",
    "rare": "#3390ec",
    "legendary": "#b06be0",
}
# The game's own slot emoji (pets_config.SLOT_EMOJI), not lookalike typographic symbols:
# ⚔ ◈ ▲ render as flat text glyphs in the same tile where 🧤 renders in colour, which read
# as three broken icons next to one working one.
SLOT_GLYPHS = dict(C.SLOT_EMOJI)


def _json_error(message: str, status: int = 400, code: str = "ERROR") -> web.Response:
    """arena_web's error shape -- a machine-readable code plus a line for the player."""
    return web.json_response({"error": code, "message": message}, status=status)


def _ok(payload) -> web.Response:
    """The ONE way a handler here returns data. Everything goes out through _jsonable, so
    a live datetime arriving from the game cannot 500 a route that forgot to convert it."""
    return web.json_response(_jsonable(payload))


def _init_data_from(request: web.Request, body: dict | None = None) -> str:
    if body and isinstance(body.get("init_data"), str):
        return body["init_data"]
    return request.headers.get("X-Telegram-Init-Data", "")


async def _authenticate(request: web.Request, body: dict | None = None) -> dict:
    cfg = request.app[_CFG_KEY]
    try:
        return voting.verify_init_data(_init_data_from(request, body), cfg.telegram_bot_token)
    except voting.InitDataError as e:
        raise web.HTTPUnauthorized(
            text=json.dumps({"error": "UNAUTHORIZED", "message": str(e)}, ensure_ascii=False),
            content_type="application/json",
        )


async def _default_is_member(user: dict) -> bool:
    """Permissive, for the same reason vote_web's default is: it keeps the module
    constructible without a Bot API client. Production always passes a real one."""
    return True


async def _default_resolve_player(user: dict):
    return None, None


async def _player(request: web.Request, body: dict | None = None):
    """(telegram user, chat-activity xp) or an HTTP error.

    The pet game rides the same coin ledger as /shop, and economy.balance derives the
    earned half from live chat XP -- so nothing can be priced, bought or sold before the
    player has been resolved against the chat's statistics. That resolution is also the
    check that stops somebody who has never written in the chat from farming the arena,
    which is why it is not optional and not cached.
    """
    user = await _authenticate(request, body)
    resolve = request.app[_RESOLVE_KEY]
    _, xp = await resolve(user)
    if xp is None:
        raise web.HTTPForbidden(
            text=json.dumps(
                {"error": "NOT_TRACKED",
                 "message": "Ты ещё не отслеживаешься -- напиши что-нибудь в чат и попробуй снова."},
                ensure_ascii=False,
            ),
            content_type="application/json",
        )
    return user, int(xp)


# --------------------------------------------------------------------------- item art


def art_dir() -> Path:
    """Where real item pictures go, when there are any. One file per item code.

    Under DATA_DIR, the same persistent-volume root voting.py uses -- art dropped in here
    survives a redeploy, and the placeholder is what shows until it exists.
    """
    return Path(os.getenv("DATA_DIR", ".")) / "pets" / "items"


def _art_file(code: str) -> Path | None:
    directory = art_dir()
    for extension in _ART_EXTENSIONS:
        candidate = directory / f"{code}{extension}"
        if candidate.is_file():
            return candidate
    return None


def placeholder_svg(code: str, rarity: str = "common", slot: str = "weapon") -> str:
    """A stand-in tile for an item that has no picture yet.

    Deterministic on the code, so an item looks the same everywhere it appears and the
    player can learn it by sight before any real art exists: the rarity decides the colour,
    the slot decides the glyph, and the code's own hash decides the angle and the speckle
    pattern. Generated rather than shipped as files because there are 596 items.
    """
    digest = hashlib.sha256(code.encode("utf-8")).digest()
    colour = RARITY_COLOURS.get(rarity, RARITY_COLOURS["common"])
    angle = digest[0] * 360 // 256
    glyph = SLOT_GLYPHS.get(slot, "◆")
    # Three dots placed from the hash: enough to make two grey commons distinguishable at
    # a glance in a full bag, which is the whole job of a placeholder.
    dots = "".join(
        f'<circle cx="{30 + digest[i] % 150}" cy="{30 + digest[i + 3] % 150}" '
        f'r="{2 + digest[i + 6] % 5}" fill="{colour}" opacity="0.35"/>'
        for i in range(3)
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{ART_SIZE}" height="{ART_SIZE}" '
        f'viewBox="0 0 {ART_SIZE} {ART_SIZE}" role="img" aria-label="{code}">'
        f'<defs><linearGradient id="g" gradientTransform="rotate({angle} 0.5 0.5)">'
        f'<stop offset="0%" stop-color="{colour}" stop-opacity="0.30"/>'
        f'<stop offset="100%" stop-color="{colour}" stop-opacity="0.05"/>'
        f"</linearGradient></defs>"
        f'<rect width="{ART_SIZE}" height="{ART_SIZE}" rx="18" fill="#1a2532"/>'
        f'<rect width="{ART_SIZE}" height="{ART_SIZE}" rx="18" fill="url(#g)"/>'
        f"{dots}"
        f'<text x="50%" y="52%" text-anchor="middle" dominant-baseline="middle" '
        f'font-size="76" fill="{colour}" opacity="0.85">{glyph}</text>'
        f'<text x="50%" y="86%" text-anchor="middle" font-family="monospace" '
        f'font-size="15" fill="{colour}" opacity="0.5">{code}</text>'
        f"</svg>"
    )


# A pet's picture lives on Telegram's servers as a file_id and nowhere else, so the page
# cannot point an <img> at it. It is fetched once through the Bot API and kept here, keyed
# on the FILE ID rather than on the player: a new photo is a new id, so the cache never
# needs invalidating and two pets that somehow share a picture share one file.
PORTRAIT_MAX_BYTES = 8 * 1024 * 1024
PORTRAIT_MAX_EDGE = 1280        # what gets stored; the crop is applied on top of it


def portrait_dir() -> Path:
    return Path(os.getenv("DATA_DIR", ".")) / "pets" / "portraits"


def portrait_cache_path(file_id: str) -> Path:
    return portrait_dir() / f"{hashlib.sha256(file_id.encode('utf-8')).hexdigest()[:32]}.jpg"


def portrait_placeholder_svg(seed: str, letter: str = "") -> str:
    """What stands in for a pet with no photo -- a coloured tile with its initial.

    Keyed on the pet's own id so the colour is stable: in a list of opponents, a consistent
    colour per creature is most of what "recognising" one is, before any photo exists.
    """
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    hue = digest[0] * 360 // 256
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{ART_SIZE}" height="{ART_SIZE}" '
        f'viewBox="0 0 {ART_SIZE} {ART_SIZE}">'
        f'<rect width="{ART_SIZE}" height="{ART_SIZE}" fill="hsl({hue} 42% 26%)"/>'
        f'<text x="50%" y="54%" text-anchor="middle" dominant-baseline="middle" '
        f'font-size="96" fill="hsl({hue} 55% 72%)" font-family="sans-serif">'
        f'{esc_xml(letter) or "🐾"}</text></svg>'
    )


def esc_xml(text: str) -> str:
    return (str(text or "")[:1]
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


async def handle_portrait(request: web.Request) -> web.Response:
    """A pet's photo, by owner id. Unauthenticated, like every other picture route here:
    an <img> cannot carry the initData header, and this photo was posted to the chat.

    Downloaded from Telegram at most once per file_id, then served off disk. A pet with no
    photo (or a download that fails) gets the placeholder rather than a broken image --
    the roster of opponents must render even when one player's picture is unavailable.
    """
    raw = request.match_info["user_id"]
    log = request.app[_LOG_KEY]
    if not _SAFE_CODE.match(raw or ""):
        raise web.HTTPNotFound()
    entry = request.app[_ENTRY_KEY]
    record = pets.get_pet(entry, raw) or {}
    file_id = record.get("photo_file_id")

    def placeholder(why: str) -> web.Response:
        # Logged with the REASON, because every one of these looks identical on screen: a
        # pet nobody photographed, a chat the lookup missed, and a download that failed all
        # render the same grey tile. Without the reason in the log there is nothing to tell
        # a working game from a broken one.
        log(f"[pets_web] portrait {raw}: placeholder ({why})")
        return web.Response(
            text=portrait_placeholder_svg(raw, (record.get("name") or "")[:1]),
            content_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=300"},
        )

    if not record:
        return placeholder(f"no pet under entry {entry!r}")
    if not file_id:
        return placeholder("pet has no photo_file_id")

    cached = portrait_cache_path(file_id)
    if not cached.is_file():
        fetch = request.app[_FETCH_PHOTO_KEY]
        started = time.monotonic()
        try:
            data = await fetch(file_id)
        except Exception:
            log(f"[pets_web] portrait {raw}: fetch raised:\n{traceback.format_exc()}")
            data = None
        if not data:
            return placeholder("download returned nothing")
        try:
            # Decoding and re-encoding a photo is real CPU work, and this process also
            # serves the ballot and answers the bot. Off the loop it goes, the same way
            # vote_image's renders do -- a burst of portraits opening the arena must not
            # stall everything else the server is in the middle of.
            await asyncio.to_thread(_write_portrait, cached, data)
        except Exception:
            log(f"[pets_web] portrait {raw}: could not store:\n{traceback.format_exc()}")
            return placeholder("could not store")
        log(
            f"[pets_web] portrait {raw}: fetched {len(data)} bytes -> "
            f"{cached.stat().st_size} on disk in {time.monotonic() - started:.1f}s"
        )
    # Immutable: the name is a hash of the file id, so this exact URL can never point at
    # different pixels. A changed photo is a changed file_id and therefore a changed URL.
    return web.FileResponse(cached, headers={"Cache-Control": "public, max-age=604800"})


def _normalise_photo(data: bytes) -> bytes | None:
    """Prove the bytes are an image, then bound and re-encode them. None if they are not.

    Everything a picture goes through here passes this: whatever arrives is decoded before
    it is stored or forwarded, EXIF (the orientation a browser would ignore and the
    location the owner did not mean to publish) is dropped, and an enormous original is
    brought down to something a phone can load. It also keeps the bot from being used to
    push arbitrary bytes at Telegram's servers -- the upload path calls this BEFORE
    handing anything over, not after.
    """
    from PIL import Image, ImageOps

    try:
        image = Image.open(BytesIO(data))
        image = ImageOps.exif_transpose(image).convert("RGB")
    except Exception:
        return None
    image.thumbnail((PORTRAIT_MAX_EDGE, PORTRAIT_MAX_EDGE), Image.Resampling.LANCZOS)
    buffer = BytesIO()
    image.save(buffer, "JPEG", quality=88, optimize=True)
    return buffer.getvalue()


def _write_portrait(path: Path, data: bytes) -> None:
    normalised = _normalise_photo(data)
    if normalised is None:
        raise ValueError("not an image")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(normalised)
    temporary.replace(path)


async def _read_bounded(request: web.Request, limit: int) -> bytes | None:
    """The request body, or None if it runs past `limit`.

    Read off the stream rather than through request.read(), which enforces the
    APPLICATION's client_max_size -- 1 MB by default, set once for the whole server by
    vote_web.create_app. A photo route needs its own ceiling and its own refusal: at the
    shared default an ordinary phone picture dies on aiohttp's generic 413 before this
    handler runs, and the player is told nothing useful. Counting the bytes here keeps
    both the limit and the message where the rule actually lives, and still refuses early
    rather than buffering something enormous.
    """
    chunks, total = [], 0
    async for chunk in request.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def handle_portrait_upload(request: web.Request) -> web.Response:
    """Replace the pet's photo from the page: raw image bytes in, new file_id out.

    The Mini App CAN produce a picture -- it is a web page, and a file input plus a canvas
    is all it takes. What it cannot produce is a Telegram file_id, so the bytes are handed
    to Telegram here (as a photo sent to the player's own chat, which doubles as their
    receipt) and the id that comes back is what gets stored. One picture, one id, and the
    chat menu's pet card shows exactly what the page does.
    """
    user, xp = await _player(request)
    entry = request.app[_ENTRY_KEY]
    if not await request.app[_IS_MEMBER_KEY](user):
        return _json_error("Только участники чата.", status=403, code="NOT_A_MEMBER")
    if pets.get_pet(entry, user["id"]) is None:
        return _json_error("Сначала приручи существо.", status=409, code="NO_PET")

    body = await _read_bounded(request, PORTRAIT_MAX_BYTES)
    if body is None:
        return _json_error("Файл слишком большой.", status=413, code="TOO_BIG")
    if not body:
        return _json_error("Пустой файл.", code="EMPTY")

    # Decoded here, before Telegram ever sees it: a refusal should say "это не картинка"
    # rather than arriving as a failed upload two round trips later, and nothing should be
    # able to use this route to push arbitrary bytes through the bot.
    log = request.app[_LOG_KEY]
    photo = await asyncio.to_thread(_normalise_photo, body)
    if photo is None:
        log(f"[pets_web] portrait upload from {user['id']}: not an image ({len(body)} bytes)")
        return _json_error("Это не картинка.", code="NOT_AN_IMAGE")

    save = request.app[_SAVE_PHOTO_KEY]
    try:
        file_id = await save(user["id"], photo)
    except Exception:
        log(f"[pets_web] portrait upload failed:\n{traceback.format_exc()}")
        file_id = None
    if not file_id:
        log(f"[pets_web] portrait upload from {user['id']}: Telegram gave no file_id")
        return _json_error("Не получилось сохранить фото.", status=502, code="UPLOAD_FAILED")

    ok, message = pets.set_photo(entry, user["id"], file_id)
    log(
        f"[pets_web] portrait upload from {user['id']}: {len(body)} -> {len(photo)} bytes, "
        f"file_id {file_id[:16]}…, stored={ok}"
    )
    return _ok({
        "ok": ok,
        "message": message,
        "state": _state_payload(entry, user["id"], xp, request.app[_PREFIX_KEY]),
    })


async def handle_item_art(request: web.Request) -> web.Response:
    """An item's picture: the real file when one has been dropped in, else a placeholder.

    Unauthenticated, like vote_web's media route and for the same reason -- an <img> tag
    cannot carry the initData header, and there is nothing secret about a picture of a
    sword every player in the chat can already buy.
    """
    code = request.match_info["code"]
    if not _SAFE_CODE.match(code or ""):
        raise web.HTTPNotFound()

    real = _art_file(code)
    if real is not None:
        return web.FileResponse(real, headers={"Cache-Control": "public, max-age=86400"})

    item = C.find_item(code)
    body = placeholder_svg(
        code,
        rarity=getattr(item, "rarity", "common"),
        slot=getattr(item, "slot", "weapon"),
    )
    return web.Response(
        text=body,
        content_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )


# ------------------------------------------------------------------------ serialisation


def _jsonable(value):
    """Everything the game hands back, made safe for json.dumps.

    The state is assembled by forwarding pets.py's own return values, and some of them
    carry a live datetime -- passive_income_status's "next_hour" does, on every branch
    including the zero-rate one a freshly tamed pet takes. json.dumps refuses it, and
    since every action re-embeds the state, one such field turns the entire Mini App into
    a 500 for anybody who owns a pet. Converting here rather than at each call site is
    what makes that a property of the boundary instead of a thing to remember.
    """
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _item_payload(item, prefix: str, record: dict | None = None) -> dict:
    """One item, with everything a card needs and nothing it doesn't.

    `record` is the viewer's pet: it decides the owned/equipped/locked flags, which are a
    property of the pair rather than of the item, and are what let the client render a bag
    card and a shop card from one shape.
    """
    equipped_codes = set((record or {}).get("equipped", {}).values())
    owned = item.code in set((record or {}).get("inventory", []))
    return {
        "code": item.code,
        "name": item.name,
        "slot": item.slot,
        "slot_name": C.SLOT_NAMES.get(item.slot, item.slot),
        "rarity": item.rarity,
        "rarity_name": C.RARITY_LABELS.get(item.rarity, item.rarity),
        "rarity_rank": RARITY_ORDER.index(item.rarity) if item.rarity in RARITY_ORDER else 1,
        "price": item.price,
        "resale": C.resale_value(item),
        "source": item.source,
        "bonuses": dict(item.bonuses),
        "description": item.description,
        "effect": dict(item.effect or {}),
        "art": f"{prefix}/img/{item.code}.svg",
        "owned": owned,
        "equipped": item.code in equipped_codes,
        "locked": item.code in set((record or {}).get("locked_items", [])),
    }


def _stat_payload(entry: str, user_id, record: dict) -> list[dict]:
    """A stat row: what it cost, what it is now, and what the gear adds on top.

    Purchased and effective are kept apart because they are spent differently -- coins
    raise the first, equipment raises the second, and a player deciding between a stat
    point and a new amulet is comparing exactly those two numbers.
    """
    effective = pets.effective_stats(entry, user_id)
    purchased = record.get("stats", {})
    rows = []
    for key in C.STAT_KEYS:
        base = int(purchased.get(key, 1))
        level = int(effective.get(key, base))
        rows.append({
            "key": key,
            "name": C.STAT_NAMES.get(key, key),
            "emoji": C.STAT_EMOJI.get(key, ""),
            "purchased": base,
            "effective": level,
            "bonus": level - base,
            "max": C.STAT_MAX_LEVEL,
            "cost_1": C.stat_upgrade_cost(base) if base < C.STAT_MAX_LEVEL else None,
            "cost_10": (
                C.total_stat_cost(min(base + 10, C.STAT_MAX_LEVEL), base)
                if base < C.STAT_MAX_LEVEL else None
            ),
        })
    rows.append({
        "key": "armor",
        "name": "Броня",
        "emoji": "🛡",
        "purchased": 0,
        "effective": int(effective.get("armor", 0)),
        "bonus": int(effective.get("armor", 0)),
        "max": None,
        "cost_1": None,
        "cost_10": None,
        "gear_only": True,
    })
    return rows


def _combat_payload(entry: str, user_id, record: dict) -> dict:
    """The numbers a fight actually uses, derived the way the fight derives them.

    A player choosing between +1 strength and +1 agility cannot read that choice off the
    stat levels -- dodge and crit saturate, armour reduces by a curve. Deriving against a
    neutral mirror of themselves shows the honest current value of each.
    """
    effective = pets.effective_stats(entry, user_id)
    mirror = pets_combat.Fighter(
        key="mirror", name="mirror",
        strength=effective["strength"], health=effective["health"],
        agility=effective["agility"], luck=effective["luck"],
        armor=effective.get("armor", 0), level=int(record.get("level", 1)),
    )
    derived = pets_combat.derive(mirror, mirror)
    return {
        "max_hp": int(derived.get("max_hp", 0)),
        "damage": round(float(derived.get("damage", 0)), 1),
        "dodge": round(float(derived.get("dodge", 0)) * 100, 1),
        "crit": round(float(derived.get("crit", 0)) * 100, 1),
        "reduction": round(float(derived.get("reduction", 0)) * 100, 1),
        "power": pets.power_rating(entry, user_id),
    }


def _portrait_url(prefix: str, user_id) -> str:
    return f"{prefix}/img/pet/{user_id}.jpg"


def _pet_payload(entry: str, user_id, record: dict, prefix: str) -> dict:
    level = int(record.get("level", 1))
    return {
        "name": record.get("name"),
        "level": level,
        "xp": int(record.get("xp", 0)),
        "xp_needed": C.pet_xp_for_next_level(level),
        "max_level": C.PET_MAX_LEVEL,
        "fights": int(record.get("fights", 0)),
        "wins": int(record.get("wins", 0)),
        "cage_level": int(record.get("cage_level", 0)),
        "cage_max": C.CAGE_MAX_LEVEL,
        "owner_name": record.get("owner_name"),
        "owner_username": record.get("owner_username"),
        "created_at": record.get("created_at"),
        "notifications": bool(record.get("fight_result_notifications", True)),
        "portrait": _portrait_url(prefix, user_id),
        "has_photo": bool(record.get("photo_file_id")),
        # The framing square, in the photo's own pixels, or null for "fit the whole thing".
        # Applied as CSS by the page and stored as numbers (see pets.set_portrait_crop).
        "crop": record.get("portrait_crop"),
    }


def _equipment_payload(record: dict, prefix: str) -> list[dict]:
    """The four slots, always all four -- an empty slot is a thing to fill, so it is drawn
    rather than omitted (which is what makes a paperdoll a paperdoll)."""
    slots = []
    for slot in C.SLOT_KEYS:
        code = record.get("equipped", {}).get(slot)
        item = C.find_item(code) if code else None
        slots.append({
            "slot": slot,
            "name": C.SLOT_NAMES.get(slot, slot),
            "emoji": C.SLOT_EMOJI.get(slot, ""),
            "item": _item_payload(item, prefix, record) if item else None,
        })
    return slots


def _state_payload(entry: str, user_id, xp: int, prefix: str) -> dict:
    """Everything the client draws, in one object.

    Assembled fresh on every call, including after a mutation, so no screen can show a
    number the server has already moved on from. It is a few milliseconds of pure reads
    against one JSON file -- cheaper than the bugs a client-side model would cost.
    """
    # A finished shift pays out on being looked at. The chat interface has a poller for
    # this; a page that did not settle would show "готово" next to a reward that has not
    # been credited, which reads as the game having eaten it.
    receipts = pets.settle_completed_farms(entry)
    mine = [r for r in receipts if str(r.get("user_id")) == str(user_id)]

    record = pets.get_pet(entry, user_id)
    balance = pets.balance_for(entry, user_id, xp)
    cage = pets.cage_level(entry, user_id)
    daily = economy.daily_bonus_status(entry, user_id)

    state = {
        "coins": balance,
        "has_cage": cage > 0,
        "cage": {
            "level": cage,
            "max": C.CAGE_MAX_LEVEL,
            "price": C.CAGE_PRICE,
            "upgrade_cost": (
                C.CAGE_UPGRADE_COSTS[cage] if 0 < cage < len(C.CAGE_UPGRADE_COSTS) else None
            ),
            "tame_price": C.TAME_PRICE,
        },
        "daily_bonus": {
            "can_claim": bool(daily.get("can_claim")),
            "amount": int(daily.get("amount", 0)),
            "streak": int(daily.get("streak", 0)),
            "tomorrow": int(daily.get("tomorrow", 0) or 0),
            "table": list(economy.DAILY_BONUS_BY_STREAK),
        },
        "farm_receipts": mine,
        "unread_updates": pets_updates.has_unread(entry, user_id),
    }

    if not record:
        state["pet"] = None
        return state

    state["pet"] = _pet_payload(entry, user_id, record, prefix)
    state["stats"] = _stat_payload(entry, user_id, record)
    state["combat"] = _combat_payload(entry, user_id, record)
    state["equipment"] = _equipment_payload(record, prefix)
    state["bag"] = [
        _item_payload(item, prefix, record)
        for item in (C.find_item(code) for code in record.get("inventory", []))
        if item is not None
    ]
    state["arena"] = pets.fight_allowance_breakdown(entry, user_id)
    state["arena"]["farming"] = pets.is_farming(entry, user_id)
    state["arena"]["pity"] = pets.legendary_pity_progress(entry, user_id)
    state["farm"] = pets.farm_status(entry, user_id)
    state["farm"]["passive"] = pets.passive_income_status(entry, user_id)
    return state


def _shop_payload(entry: str, user_id, prefix: str) -> dict:
    """One shop, not two. The chat interface splits the daily weapon rotation from the
    permanent accessory shelf across separate screens reached by different buttons; they
    are the same act of spending coins and belong on one page, tabbed by slot."""
    record = pets.get_pet(entry, user_id) or {}
    weapons = [_item_payload(item, prefix, record) for item in pets.daily_storefront_weapons(entry)]
    accessories = [
        _item_payload(item, prefix, record)
        for slot in C.SLOT_KEYS if slot != "weapon"
        for item in C.items_for_slot(slot, source="shop")
    ]
    return {"weapons": weapons, "accessories": accessories, "rotates_daily": True}


# ------------------------------------------------------------------------------ actions
#
# Every action is (entry, user_id, xp, payload) -> (ok, message). The state that follows is
# assembled by the caller from scratch, so an action never has to describe what it changed.


def _action_upgrade_stat(entry, user_id, xp, payload):
    stat = str(payload.get("stat") or "")
    if stat not in C.STAT_KEYS:
        return False, "Неизвестная характеристика."
    times = 10 if int(payload.get("times") or 1) > 1 else 1
    ok, message, _spent = pets.upgrade_stat(entry, user_id, xp, stat, times=times)
    return ok, message


def _action_equip(entry, user_id, xp, payload):
    return pets.equip(entry, user_id, str(payload.get("code") or ""))


def _action_unequip(entry, user_id, xp, payload):
    return pets.unequip(entry, user_id, str(payload.get("slot") or ""))


def _action_lock(entry, user_id, xp, payload):
    ok, message, _locked = pets.toggle_item_lock(entry, user_id, str(payload.get("code") or ""))
    return ok, message


def _action_buy(entry, user_id, xp, payload):
    return pets.buy_item(entry, user_id, xp, str(payload.get("code") or ""))


def _action_sell(entry, user_id, xp, payload):
    """Sell, with the confirmation the game asks for on a rare or legendary item.

    The page shows the confirmation as a dialog rather than a separate screen, so the token
    is minted and spent inside one action: the client sends `confirm: true` from the dialog
    it has already shown. The server still mints and consumes a real one-time token -- the
    dialog is the UI, not the safeguard.
    """
    code = str(payload.get("code") or "")
    item = C.find_item(code)
    token = None
    if item is not None and valuable_item(item):
        if not payload.get("confirm"):
            return False, "Продажа редкой вещи требует подтверждения."
        ok, message, token = pets.begin_item_confirmation(entry, user_id, "sell", code)
        if not ok:
            return False, message
    ok, message, gold = pets.sell_item(entry, user_id, code, confirmation_token=token)
    return ok, message


def _action_gift(entry, user_id, xp, payload):
    """Gift to somebody picked from a list, not typed as an @username.

    pets.gift_item takes a receiver id, and the page already knows every player in the chat
    from the ranking -- so the text prompt the chat interface needs (and its regex, and its
    "не нашёл такого" failure) has nothing to do here.
    """
    code = str(payload.get("code") or "")
    receiver = payload.get("receiver_id")
    if receiver in (None, ""):
        return False, "Не выбран получатель."
    item = C.find_item(code)
    token = None
    if item is not None and valuable_item(item):
        if not payload.get("confirm"):
            return False, "Подарок редкой вещи требует подтверждения."
        ok, message, token = pets.begin_item_confirmation(entry, user_id, "gift", code)
        if not ok:
            return False, message
    return pets.gift_item(entry, user_id, receiver, code, confirmation_token=token)


def _action_buy_cage(entry, user_id, xp, payload):
    return pets.buy_cage(entry, user_id, xp)


def _action_upgrade_cage(entry, user_id, xp, payload):
    return pets.upgrade_cage(entry, user_id, xp)


def _action_rename(entry, user_id, xp, payload):
    return pets.rename(entry, user_id, str(payload.get("name") or ""))


def _action_farm_start(entry, user_id, xp, payload):
    try:
        hours = int(payload.get("hours") or C.FARM_DURATION_HOURS)
    except (TypeError, ValueError):
        hours = C.FARM_DURATION_HOURS
    return pets.start_farm(entry, user_id, hours=hours)


def _action_farm_cancel(entry, user_id, xp, payload):
    return pets.cancel_farm(entry, user_id)


def _action_farm_upgrade(entry, user_id, xp, payload):
    return pets.upgrade_farm(entry, user_id, xp)


def _action_farm_feature(entry, user_id, xp, payload):
    return pets.upgrade_farm_feature(entry, user_id, xp, str(payload.get("feature") or ""))


def _action_daily_bonus(entry, user_id, xp, payload):
    claimed, amount, streak = economy.claim_daily_bonus(entry, user_id)
    if not claimed:
        return False, "Сегодняшний бонус уже забран."
    return True, f"+{amount} монет. Серия: {streak}."


def _action_portrait_crop(entry, user_id, xp, payload):
    crop = payload.get("crop")
    return pets.set_portrait_crop(entry, user_id, crop if isinstance(crop, dict) else None)


def _action_notifications(entry, user_id, xp, payload):
    enabled = pets.toggle_fight_result_notifications(entry, user_id)
    return True, "Отчёты о боях включены." if enabled else "Отчёты о боях выключены."


_ACTIONS = {
    "upgrade_stat": _action_upgrade_stat,
    "equip": _action_equip,
    "unequip": _action_unequip,
    "lock": _action_lock,
    "buy": _action_buy,
    "sell": _action_sell,
    "gift": _action_gift,
    "buy_cage": _action_buy_cage,
    "upgrade_cage": _action_upgrade_cage,
    "rename": _action_rename,
    "farm_start": _action_farm_start,
    "farm_cancel": _action_farm_cancel,
    "farm_upgrade": _action_farm_upgrade,
    "farm_feature": _action_farm_feature,
    "daily_bonus": _action_daily_bonus,
    "notifications": _action_notifications,
    "portrait_crop": _action_portrait_crop,
}


# ------------------------------------------------------------------------------- routes


async def handle_state(request: web.Request) -> web.Response:
    user, xp = await _player(request)
    entry = request.app[_ENTRY_KEY]
    return _ok(_state_payload(entry, user["id"], xp, request.app[_PREFIX_KEY]))


async def handle_action(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_error("malformed request body")
    user, xp = await _player(request, body)
    entry = request.app[_ENTRY_KEY]

    if not await request.app[_IS_MEMBER_KEY](user):
        return _json_error("Играть могут только участники чата.", status=403, code="NOT_A_MEMBER")

    action = _ACTIONS.get(str(body.get("action") or ""))
    if action is None:
        return _json_error("Неизвестное действие.", status=400, code="UNKNOWN_ACTION")

    try:
        ok, message = action(entry, user["id"], xp, body)
        # Every state change the game makes, with who made it and whether it took. This is
        # the record that says what a player actually did when they report that something
        # went wrong -- the alternative is asking them to remember.
        request.app[_LOG_KEY](
            f"[pets_web] {user['id']} {body.get('action')}"
            f"{' ' + str(body.get('code') or body.get('stat') or body.get('feature') or '')}".rstrip()
            + f" -> {'ok' if ok else 'refused'}: {message}"
        )
    except ValueError as e:
        # pets.py raises this for a race the UI already gated on -- a stale tab pressing a
        # button whose precondition has since gone. It is a refusal, not a crash.
        ok, message = False, str(e)
        request.app[_LOG_KEY](f"[pets_web] {user['id']} {body.get('action')} -> race: {e}")
    except Exception:
        # log is print-shaped here (see attach), so the traceback is formatted in rather
        # than passed as a keyword -- an exc_info= would raise inside the error handler.
        request.app[_LOG_KEY](
            f"[pets_web] action {body.get('action')} failed:\n{traceback.format_exc()}"
        )
        return _json_error("Что-то сломалось. Попробуй ещё раз.", status=500, code="FAILED")

    return _ok({
        "ok": bool(ok),
        "message": message,
        "state": _state_payload(entry, user["id"], xp, request.app[_PREFIX_KEY]),
    })


async def handle_opponents(request: web.Request) -> web.Response:
    """The field, not one candidate.

    Everyone with a pet, marked with whether you may still attack them today, sorted by how
    close their power is to yours -- an even fight is the interesting one, and a list makes
    that choice visible in a way rerolling a single card never could.
    """
    user, _xp = await _player(request)
    entry = request.app[_ENTRY_KEY]
    me = str(user["id"])
    mine = pets.power_rating(entry, me)

    opponents = []
    for row in pets.pet_leaderboard(entry):
        if str(row["user_id"]) == me:
            continue
        record = pets.get_pet(entry, row["user_id"]) or {}
        opponents.append({
            "user_id": str(row["user_id"]),
            "portrait": _portrait_url(request.app[_PREFIX_KEY], row["user_id"]),
            "crop": record.get("portrait_crop"),
            "name": row.get("name"),
            "owner_name": row.get("owner_name"),
            "owner_username": row.get("owner_username"),
            "power": row.get("power", 0),
            "level": int(record.get("level", 1)),
            "fights": int(record.get("fights", 0)),
            "wins": int(record.get("wins", 0)),
            "stats": pets.effective_stats(entry, row["user_id"]),
            "attackable": pets.can_attack_in_arena(entry, me, row["user_id"]),
            "attacks_today": pets.arena_attacks_against(entry, me, row["user_id"], pets.today()),
            "gap": abs(int(row.get("power", 0)) - mine),
        })
    opponents.sort(key=lambda o: (not o["attackable"], o["gap"]))
    return _ok({"me_power": mine, "opponents": opponents})


async def handle_attack(request: web.Request) -> web.Response:
    """Run a fight and hand back the whole thing to be replayed.

    The rounds go out with the result so the page can play the fight out blow by blow
    instead of printing a verdict -- the chat interface sends two pictures and a caption
    because that is what a chat can do, but the interesting part of a fight is the order it
    happened in.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_error("malformed request body")
    user, xp = await _player(request, body)
    entry = request.app[_ENTRY_KEY]

    if not await request.app[_IS_MEMBER_KEY](user):
        return _json_error("Драться могут только участники чата.", status=403, code="NOT_A_MEMBER")

    me = str(user["id"])
    opponent_id = str(body.get("opponent_id") or "")
    mine = pets.get_pet(entry, me)
    theirs = pets.get_pet(entry, opponent_id) if opponent_id else None
    if mine is None or theirs is None:
        return _json_error("Соперник больше не доступен.", status=409, code="NO_OPPONENT")
    if not pets.can_attack_in_arena(entry, me, opponent_id):
        return _json_error("Сегодня с этим соперником уже хватит.", status=409, code="LIMIT")

    def fighter(key, record):
        effective = pets.effective_stats(entry, key)
        return pets_combat.Fighter(
            key=str(key), name=record.get("name") or "Существо",
            strength=effective["strength"], health=effective["health"],
            agility=effective["agility"], luck=effective["luck"],
            armor=effective.get("armor", 0),
            effects=pets.equipped_combat_effects(entry, key),
            level=int(record.get("level", 1)),
        )

    result = pets_combat.simulate(fighter(me, mine), fighter(opponent_id, theirs),
                                  seed=secrets.randbits(63))
    try:
        reward = pets.record_fight(entry, me, opponent_id, result, pets.today(), attacker_xp=xp)
    except ValueError as e:
        # The bank emptied, or the pet went to the farm, between drawing the page and
        # pressing the button. Nothing has been recorded -- say so and let the client
        # refresh rather than showing a fight that did not count.
        return _json_error(str(e), status=409, code="CANNOT_FIGHT")

    dropped = C.find_item(reward.get("dropped_item")) if reward.get("dropped_item") else None
    prefix = request.app[_PREFIX_KEY]
    request.app[_LOG_KEY](
        f"[pets_web] fight {me} vs {opponent_id}: "
        f"{'draw' if result.is_draw else ('win' if result.winner == me else 'loss')}, "
        f"{len(result.rounds)} rounds, gold {reward.get('gold') or -reward.get('loss_gold', 0)}, "
        f"xp {reward.get('xp')}, drop {reward.get('dropped_item')}"
    )
    return _ok({
        "ok": True,
        "you": me,
        "opponent": {"user_id": opponent_id, "name": theirs.get("name")},
        "winner": result.winner,
        "draw": result.is_draw,
        "stopped_early": result.stopped_early,
        "opening": result.opening,
        "closing": result.closing,
        "rounds": [
            {"number": r.number, "attacker": r.attacker, "event": r.event, "damage": r.damage,
             "attacker_hp": r.attacker_hp, "defender_hp": r.defender_hp, "text": r.text}
            for r in result.rounds
        ],
        "reward": reward,
        "dropped": _item_payload(dropped, prefix, pets.get_pet(entry, me)) if dropped else None,
        "state": _state_payload(entry, me, xp, prefix),
    })


async def handle_shop(request: web.Request) -> web.Response:
    user, _xp = await _player(request)
    entry = request.app[_ENTRY_KEY]
    return _ok(_shop_payload(entry, user["id"], request.app[_PREFIX_KEY]))


async def handle_leaderboard(request: web.Request) -> web.Response:
    user, _xp = await _player(request)
    entry = request.app[_ENTRY_KEY]
    rows = pets.pet_leaderboard(entry)
    return _ok({
        "me": str(user["id"]),
        "rows": [
            {"rank": index, "user_id": str(row["user_id"]), "name": row.get("name"),
             "owner_name": row.get("owner_name"), "owner_username": row.get("owner_username"),
             "power": row.get("power", 0),
             "portrait": _portrait_url(request.app[_PREFIX_KEY], row["user_id"])}
            for index, row in enumerate(rows, start=1)
        ],
    })


async def handle_history(request: web.Request) -> web.Response:
    """Recent fights, already phrased from the reader's side.

    pets.history returns a fight row with both participants and money rewritten for the
    reader; turning that into "ты напал" or "на тебя напали" is a rule, not decoration, so
    it is applied here rather than reimplemented in the client.
    """
    user, _xp = await _player(request)
    entry = request.app[_ENTRY_KEY]
    me = str(user["id"])
    rows = []
    for fight in pets.history(entry, me):
        attacked = str(fight.get("attacker_id")) == me
        other = fight.get("defender_name") if attacked else fight.get("attacker_name")
        won = fight.get("winner_id") == me
        outcome = "Ничья" if not fight.get("winner_id") else ("Победа" if won else "Поражение")
        coins = (
            fight.get("gold", 0)
            or -fight.get("loss_gold", 0)
            or fight.get("consolation_gold", 0)
        )
        rows.append({
            "attacked": attacked,
            "opponent": other or "соперник",
            "outcome": outcome,
            "won": won,
            "coins": coins,
            "at": fight.get("at") or fight.get("created_at"),
        })
    return _ok({"rows": rows})


async def handle_collection(request: web.Request) -> web.Response:
    """The codex: every weapon anybody in the chat has ever found, and who holds it now."""
    user, _xp = await _player(request)
    entry = request.app[_ENTRY_KEY]
    prefix = request.app[_PREFIX_KEY]
    record = pets.get_pet(entry, user["id"]) or {}
    rows = []
    for found in pets.discovered_weapon_collection(entry):
        item = C.find_item(found["code"])
        if item is None:
            continue
        payload = _item_payload(item, prefix, record)
        payload["owners"] = found.get("owners", [])
        rows.append(payload)
    return _ok({
        "rows": rows,
        "total": len([i for i in C.ITEMS if i.slot == "weapon"]),
    })


async def handle_updates(request: web.Request) -> web.Response:
    """The changelog, whole. It is one note per screen-tap in the chat interface because a
    message can hold one; here it is a feed, and opening it marks it read."""
    user, _xp = await _player(request)
    entry = request.app[_ENTRY_KEY]
    updates = pets_updates.all_updates(entry)
    pets_updates.mark_latest_read(entry, user["id"])
    return _ok({
        "rows": [{"id": u.id, "title": u.title, "text": u.text} for u in reversed(updates)]
    })


async def handle_page(request: web.Request) -> web.Response:
    # Unauthenticated (the page itself is just markup; every route it calls is gated), so
    # this is the only place that records that somebody opened the game at all -- which is
    # the first thing worth knowing when a player says nothing works.
    request.app[_LOG_KEY]("[pets_web] page opened")
    return web.Response(
        text=PAGE_HTML.replace("__PREFIX__", request.app[_PREFIX_KEY]),
        content_type="text/html",
    )


async def _default_fetch_photo(file_id: str):
    return None


async def _default_save_photo(user_id, data: bytes):
    return None


def attach(
    app: web.Application,
    cfg,
    entry: str,
    is_member=None,
    resolve_player=None,
    fetch_photo=None,
    save_photo=None,
    log=print,
    route_prefix: str = ROUTE_PREFIX,
) -> web.Application:
    """Mount the pet game onto an existing application (see bot_listener's _attach_extra).

    Three injected callables, all async, all taking what the listener has and this module
    does not -- the same convention `is_member` follows:

      resolve_player(user) -> (member, xp)   the player's live chat XP. Pricing needs it,
          and resolving it needs the Telethon client and the timezone.
      fetch_photo(file_id) -> bytes | None   a pet's picture, for the portrait route. Needs
          a Bot API client.
      save_photo(user_id, bytes) -> file_id | None   the reverse, for an upload from the
          page: hands the bytes to Telegram and reports the id it assigned.

    Each has a default that simply declines, so the module stays constructible (and
    testable) without a bot -- a missing photo shows a placeholder rather than an error.
    """
    prefix = route_prefix.rstrip("/")
    app[_CFG_KEY] = cfg
    app[_ENTRY_KEY] = entry
    app[_IS_MEMBER_KEY] = is_member or _default_is_member
    app[_RESOLVE_KEY] = resolve_player or _default_resolve_player
    app[_FETCH_PHOTO_KEY] = fetch_photo or _default_fetch_photo
    app[_SAVE_PHOTO_KEY] = save_photo or _default_save_photo
    app[_PREFIX_KEY] = prefix
    app[_LOG_KEY] = log
    app.add_routes([
        web.get(prefix, handle_page),
        web.get(prefix + "/", handle_page),
        web.get(prefix + "/api/state", handle_state),
        web.post(prefix + "/api/action", handle_action),
        web.get(prefix + "/api/opponents", handle_opponents),
        web.post(prefix + "/api/attack", handle_attack),
        web.get(prefix + "/api/shop", handle_shop),
        web.get(prefix + "/api/leaderboard", handle_leaderboard),
        web.get(prefix + "/api/history", handle_history),
        web.get(prefix + "/api/collection", handle_collection),
        web.get(prefix + "/api/updates", handle_updates),
        web.post(prefix + "/api/portrait", handle_portrait_upload),
        # Before the item route: "pet/12.jpg" must not be read as an item code.
        web.get(prefix + "/img/pet/{user_id}.jpg", handle_portrait),
        web.get(prefix + "/img/{code}.svg", handle_item_art),
    ])
    log(f"[pets_web] pet game mounted at {prefix}")
    return app


PAGE_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Арена</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  /* Telegram hands the page its own palette; every colour falls back to a dark default so
     the game still looks like itself in a client that sends nothing. */
  :root {
    --bg: var(--tg-theme-bg-color, #17212b);
    --fg: var(--tg-theme-text-color, #f5f5f5);
    --muted: var(--tg-theme-hint-color, #8a9aa9);
    --card: var(--tg-theme-secondary-bg-color, #232e3c);
    --accent: var(--tg-theme-button-color, #3390ec);
    --accent-fg: var(--tg-theme-button-text-color, #fff);
    --line: rgba(128,128,128,.22);
    --sunken: rgba(0,0,0,.22);
    --gold: #e8b923;
    --hp: #e05260;
    --xp: #4caf72;
    --r-cursed: #6b5b7b;
    --r-common: #8a9aa9;
    --r-uncommon: #4caf72;
    --r-rare: #3390ec;
    --r-legendary: #b06be0;
    --hud: 118px;
    --tabs: 62px;
  }
  /* [hidden] is a low-specificity UA rule that a component's own display:flex/grid would
     silently beat -- every panel here sets one, so it has to be said louder. */
  [hidden] { display: none !important; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding: var(--hud) 0 calc(var(--tabs) + env(safe-area-inset-bottom)) 0;
    overscroll-behavior-y: contain;
  }
  button { font: inherit; color: inherit; cursor: pointer; }

  /* ------------------------------------------------------------------ the HUD
     Pinned, because the three numbers on it -- coins, fights, level -- are the ones every
     screen spends. Scrolling a shop while the wallet is off-screen is how you end up
     tapping a price you cannot pay. */
  .hud {
    position: fixed; top: 0; left: 0; right: 0; z-index: 20;
    background: var(--card); border-bottom: 1px solid var(--line);
    padding: 10px 12px calc(10px + env(safe-area-inset-top)) 12px;
    padding-top: calc(10px + env(safe-area-inset-top));
    display: flex; gap: 11px; align-items: center;
  }
  .hud .face {
    width: 54px; height: 54px; border-radius: 14px; flex: none; overflow: hidden;
    background: var(--sunken); display: flex; align-items: center; justify-content: center;
    font-size: 26px; border: 2px solid var(--accent);
  }
  .hud .who { flex: 1; min-width: 0; }
  .hud .nm { font-weight: 700; font-size: 16px; display: flex; align-items: center; gap: 7px; }
  .hud .lv {
    font-size: 11px; font-weight: 700; padding: 1px 7px; border-radius: 999px;
    background: var(--accent); color: var(--accent-fg); flex: none;
  }
  .hud .purse { display: flex; gap: 12px; font-size: 13px; margin-top: 3px; }
  .hud .purse b { font-weight: 700; }
  .bar { height: 5px; border-radius: 3px; background: var(--sunken); overflow: hidden; margin-top: 5px; }
  .bar > i { display: block; height: 100%; background: var(--xp); border-radius: 3px; transition: width .35s; }

  /* --------------------------------------------------------------- the tab bar */
  .tabs {
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 20;
    display: grid; grid-template-columns: repeat(6, 1fr);
    background: var(--card); border-top: 1px solid var(--line);
    padding-bottom: env(safe-area-inset-bottom);
  }
  .tabs button {
    border: 0; background: none; padding: 7px 0 8px; color: var(--muted);
    display: flex; flex-direction: column; align-items: center; gap: 2px;
    font-size: 10px; position: relative;
  }
  .tabs button .ic { font-size: 19px; line-height: 1; }
  .tabs button.on { color: var(--accent); }
  .tabs button .dot {
    position: absolute; top: 5px; right: calc(50% - 16px);
    width: 8px; height: 8px; border-radius: 50%; background: var(--hp);
  }

  /* ------------------------------------------------------------------ layout bits */
  .screen { padding: 12px; display: flex; flex-direction: column; gap: 12px; }
  .panel { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 12px; }
  .panel > h2 {
    margin: 0 0 10px; font-size: 12px; letter-spacing: .08em; text-transform: uppercase;
    color: var(--muted); font-weight: 700;
  }
  .row { display: flex; align-items: center; gap: 10px; }
  .row + .row { margin-top: 9px; }
  .spread { justify-content: space-between; }
  .muted { color: var(--muted); }
  .small { font-size: 13px; }
  .tiny { font-size: 11px; }
  .go {
    border: 0; border-radius: 10px; padding: 11px 14px; font-weight: 600;
    background: var(--accent); color: var(--accent-fg); width: 100%;
  }
  .go.sec { background: transparent; border: 1px solid var(--line); color: var(--fg); }
  .go.warn { background: var(--hp); color: #fff; }
  .go:disabled { opacity: .4; }
  .chip {
    border: 1px solid var(--line); background: transparent; border-radius: 999px;
    padding: 6px 12px; font-size: 13px; white-space: nowrap;
  }
  .chip.on { background: var(--accent); color: var(--accent-fg); border-color: var(--accent); }
  .chiprow { display: flex; gap: 7px; overflow-x: auto; padding-bottom: 2px; scrollbar-width: none; }
  .chiprow::-webkit-scrollbar { display: none; }

  /* ------------------------------------------------------------- the paperdoll
     Four slots around the portrait, in the arrangement they are worn: weapon and amulet at
     the hands, gloves and boots below. A list of four rows would say the same thing and
     teach nothing -- the point of a doll is that an empty slot is a shaped hole. */
  .doll { display: grid; grid-template-columns: 1fr 1.15fr 1fr; gap: 10px; align-items: center; }
  .doll .portrait {
    aspect-ratio: 1; border-radius: 18px; background: var(--sunken);
    display: flex; align-items: center; justify-content: center; font-size: 46px;
    border: 2px solid var(--accent); position: relative; overflow: hidden; padding: 0;
  }
  .doll .portrait .pw {
    position: absolute; bottom: 0; left: 0; right: 0; text-align: center;
    font-size: 11px; font-weight: 700; color: var(--gold);
    background: linear-gradient(transparent, rgba(0,0,0,.75)); padding: 12px 0 5px;
  }
  .doll .portrait .edit {
    position: absolute; top: 5px; right: 5px; font-size: 12px; background: rgba(0,0,0,.55);
    color: #fff; border-radius: 8px; padding: 2px 6px;
  }
  /* A framed photo is positioned by its crop, not by object-fit -- the square can hang off
     the edge of the picture (that is how "fit the whole thing" is expressed), and
     object-position cannot say that. Same model as vote_web's applyFrame. */
  .shot { position: relative; width: 100%; height: 100%; overflow: hidden; }
  .shot img { position: absolute; left: 0; top: 0; max-width: none; display: block; }
  .shot img.cover { width: 100%; height: 100%; object-fit: cover; }
  .slot {
    aspect-ratio: 1; border-radius: 14px; border: 1.5px dashed var(--line);
    background: var(--sunken); display: flex; align-items: center; justify-content: center;
    position: relative; overflow: hidden; padding: 0;
  }
  .slot img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .slot .ph { font-size: 22px; opacity: .45; }
  .slot .tag {
    position: absolute; left: 0; right: 0; bottom: 0; font-size: 9px; padding: 2px 3px;
    background: rgba(0,0,0,.55); color: #fff; text-align: center;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .slot.filled { border-style: solid; }

  /* --------------------------------------------------------------- stats + combat */
  .grid4 { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; }
  .stat-tile { background: var(--sunken); border-radius: 10px; padding: 8px 10px; }
  .stat-tile .k { font-size: 11px; color: var(--muted); }
  .stat-tile .v { font-size: 18px; font-weight: 700; }
  .statrow { display: grid; grid-template-columns: 1fr auto auto; gap: 7px; align-items: center; }
  .statrow + .statrow { margin-top: 8px; }
  .statrow .lbl { min-width: 0; }
  .statrow .lbl b { font-weight: 600; }
  .statrow .plus {
    border: 1px solid var(--line); background: transparent; border-radius: 9px;
    padding: 7px 9px; font-size: 12px; font-weight: 600; white-space: nowrap;
  }
  .statrow .plus:disabled { opacity: .35; }
  .gain { color: var(--xp); font-weight: 600; }
  .loss { color: var(--hp); font-weight: 600; }

  /* -------------------------------------------------------------------- item grid
     The art is a SQUARE, always: 210x210 is what the placeholder draws and what real art
     is expected to be, and `aspect-ratio: 1` on the tile holds that whatever the source
     turns out to be, so a grid of items reads as a grid rather than a ragged column.
     Rarity is the border -- in a bag of forty things it is the only property you scan by.
     Cards are a touch wider than the minimum a square needs, because the NAME has to fit:
     "Ржавая вилка прадеда" told as "Ржавая вил…" is not an item you can choose between. */
  .items { display: grid; grid-template-columns: repeat(auto-fill, minmax(108px, 1fr)); gap: 9px; }
  .item {
    border: 1.5px solid var(--line); background: var(--sunken); border-radius: 12px;
    padding: 0; overflow: hidden; text-align: left; position: relative;
    display: flex; flex-direction: column;
  }
  .item .art { width: 100%; aspect-ratio: 1; display: block; position: relative;
               background: var(--card); overflow: hidden; }
  .item .art img { width: 100%; height: 100%; object-fit: cover; display: block; }
  /* Wrapped, not clipped: up to three lines, and the row stretches to the tallest card so
     the grid still lines up. An ellipsis here hides the one thing the card is for. */
  .item .nm {
    display: block; font-size: 11px; line-height: 1.25; padding: 5px 6px 2px;
    overflow-wrap: anywhere; hyphens: auto;
  }
  .item .meta { display: block; font-size: 10px; padding: 0 6px 6px; color: var(--muted);
                margin-top: auto; }
  .item .flag {
    position: absolute; top: 5px; left: 5px; font-size: 10px; font-weight: 700;
    background: rgba(0,0,0,.6); color: #fff; border-radius: 6px; padding: 2px 5px;
  }
  .item .lockmark { position: absolute; top: 5px; right: 5px; font-size: 12px; }
  .item.dim { opacity: .45; }
  .r-cursed { border-color: var(--r-cursed); }
  .r-common { border-color: var(--r-common); }
  .r-uncommon { border-color: var(--r-uncommon); }
  .r-rare { border-color: var(--r-rare); box-shadow: 0 0 0 1px rgba(51,144,236,.25); }
  .r-legendary { border-color: var(--r-legendary); box-shadow: 0 0 14px rgba(176,107,224,.32); }
  .empty { text-align: center; color: var(--muted); padding: 26px 10px; font-size: 14px; }

  /* ----------------------------------------------------------------- opponent rows */
  .foe { display: grid; grid-template-columns: auto 1fr auto; gap: 10px; align-items: center;
         background: var(--sunken); border-radius: 12px; padding: 10px; border: 0; width: 100%;
         text-align: left; }
  .foe + .foe { margin-top: 8px; }
  .foe .av {
    width: 46px; height: 46px; border-radius: 11px; background: var(--card);
    overflow: hidden; flex: none; position: relative;
  }
  .foe .av img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .foe.out { opacity: .45; }
  .pw { color: var(--gold); font-weight: 700; }

  /* ----------------------------------------------------------- sheets and overlays */
  .veil {
    position: fixed; inset: 0; z-index: 40; background: rgba(0,0,0,.6);
    display: flex; align-items: flex-end; justify-content: center;
  }
  .sheet {
    background: var(--bg); width: 100%; max-width: 560px; max-height: 88vh; overflow-y: auto;
    border-radius: 18px 18px 0 0; padding: 14px 14px calc(18px + env(safe-area-inset-bottom));
    animation: rise .18s ease-out;
  }
  @keyframes rise { from { transform: translateY(14px); opacity: .5; } to { transform: none; opacity: 1; } }
  .sheet .hd { display: flex; gap: 12px; margin-bottom: 12px; }
  .sheet .hd img { width: 96px; height: 96px; border-radius: 12px; flex: none; }
  .sheet h3 { margin: 0 0 3px; font-size: 17px; }
  .sheet .acts { display: flex; flex-direction: column; gap: 8px; margin-top: 12px; }
  .sheet .pair { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .toast {
    position: fixed; left: 50%; transform: translateX(-50%); bottom: calc(var(--tabs) + 16px);
    z-index: 60; background: rgba(0,0,0,.86); color: #fff; padding: 10px 16px;
    border-radius: 10px; font-size: 14px; max-width: 88vw; text-align: center;
    animation: rise .16s ease-out;
  }

  /* ---------------------------------------------------------------- the fight view
     A fight is a sequence, so it is played as one: two HP bars and the blows arriving in
     order. The chat interface can only post the verdict; this is the part that was missing
     rather than a prettier version of what was there. */
  .duel { position: fixed; inset: 0; z-index: 50; background: var(--bg); display: flex;
          flex-direction: column; padding: calc(14px + env(safe-area-inset-top)) 14px
          calc(14px + env(safe-area-inset-bottom)); }
  .duel .side { margin-bottom: 10px; }
  .duel .hpbar { height: 12px; border-radius: 6px; background: var(--sunken); overflow: hidden; }
  .duel .hpbar > i { display: block; height: 100%; background: var(--hp); transition: width .28s; }
  .duel .log { flex: 1; overflow-y: auto; margin: 12px 0; display: flex; flex-direction: column; gap: 7px; }
  .duel .blow { background: var(--card); border-radius: 10px; padding: 9px 11px; font-size: 14px;
                animation: rise .16s ease-out; border-left: 3px solid var(--line); }
  .duel .blow.crit { border-left-color: var(--gold); }
  .duel .blow.dodge { border-left-color: var(--muted); opacity: .8; }
  .duel .blow.mine { border-left-color: var(--accent); }
  .duel .verdict { text-align: center; font-size: 20px; font-weight: 700; margin: 6px 0; }
  .loot { display: flex; gap: 10px; align-items: center; background: var(--card);
          border-radius: 12px; padding: 10px; margin-bottom: 10px; }
  .loot img { width: 62px; height: 62px; border-radius: 10px; }

  /* ------------------------------------------------------------- the portrait editor
     The same square-frame model the vote board's cropper uses: the stage is a fixed
     square viewport, the photo is absolutely positioned inside it, and the crop is
     {x, y, size} in the photo's own pixels. touch-action: none, or the first drag is
     taken by the page as a scroll and the photo never moves -- which reads as broken. */
  .stage { position: relative; width: 100%; aspect-ratio: 1; overflow: hidden;
           background: var(--sunken); border-radius: 14px; touch-action: none;
           cursor: grab; user-select: none; }
  .stage img { position: absolute; left: 0; top: 0; max-width: none; display: block;
               pointer-events: none; -webkit-user-drag: none; visibility: hidden; }
  .stage img.ready { visibility: visible; }
  .stage::after {
    content: ""; position: absolute; inset: 0; pointer-events: none; border-radius: 14px;
    box-shadow: inset 0 0 0 2px rgba(255,255,255,.28);
  }
  .zoomrow { display: flex; align-items: center; gap: 10px; margin-top: 10px; }
  .zoomrow input[type="range"] { flex: 1; accent-color: var(--accent); }
  .zoomrow button { border: 1px solid var(--line); background: transparent; color: var(--fg);
                    border-radius: 9px; width: 38px; height: 34px; font-size: 17px; }
</style>
</head>
<body>

<header class="hud" id="hud">
  <div class="face" id="hudFace">🥚</div>
  <div class="who">
    <div class="nm"><span id="hudName">…</span><span class="lv" id="hudLevel">1</span></div>
    <div class="purse">
      <span>💰 <b id="hudCoins">0</b></span>
      <span>⚔ <b id="hudFights">0</b><span class="muted" id="hudRecharge"></span></span>
    </div>
    <div class="bar"><i id="hudXp" style="width:0%"></i></div>
  </div>
</header>

<main id="main">
  <section class="screen" id="scr-hero"></section>
  <section class="screen" id="scr-bag" hidden></section>
  <section class="screen" id="scr-shop" hidden></section>
  <section class="screen" id="scr-arena" hidden></section>
  <section class="screen" id="scr-farm" hidden></section>
  <section class="screen" id="scr-more" hidden></section>
</main>

<nav class="tabs" id="tabs">
  <button data-tab="hero" class="on"><span class="ic">🛡</span>Герой</button>
  <button data-tab="bag"><span class="ic">🎒</span>Сумка</button>
  <button data-tab="shop"><span class="ic">🛒</span>Лавка</button>
  <button data-tab="arena"><span class="ic">⚔️</span>Арена</button>
  <button data-tab="farm"><span class="ic">🌾</span>Ферма</button>
  <button data-tab="more"><span class="ic">☰</span>Ещё</button>
</nav>

<script>
const PREFIX = "__PREFIX__";
const tg = window.Telegram && window.Telegram.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const initData = (tg && tg.initData) || "";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let S = null;            // the server's state, verbatim -- never edited on the client
let TAB = "hero";
let SHOP = null, FOES = null, ROSTER = null;
let ticker = null;

// --------------------------------------------------------------------------- transport
async function api(path, body) {
  const options = body
    ? { method: "POST", headers: { "Content-Type": "application/json",
        "X-Telegram-Init-Data": initData },
        body: JSON.stringify(Object.assign({ init_data: initData }, body)) }
    : { headers: { "X-Telegram-Init-Data": initData } };
  const response = await fetch(PREFIX + path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.message || "Не получилось");
  return data;
}

function toast(text) {
  const old = document.querySelector(".toast");
  if (old) old.remove();
  const node = document.createElement("div");
  node.className = "toast";
  node.textContent = text;
  document.body.appendChild(node);
  setTimeout(() => node.remove(), 2600);
}

function haptic(kind) {
  if (!tg || !tg.HapticFeedback) return;
  if (kind === "ok") tg.HapticFeedback.notificationOccurred("success");
  else if (kind === "no") tg.HapticFeedback.notificationOccurred("error");
  else tg.HapticFeedback.selectionChanged();
}

// Every mutation goes through here, and every mutation comes back with the whole state --
// so there is exactly one place where the screen is allowed to change, and no path where
// the client guesses what a rule did.
async function act(action, payload) {
  try {
    const data = await api("/api/action", Object.assign({ action }, payload || {}));
    S = data.state;
    haptic(data.ok ? "ok" : "no");
    if (data.message) toast(data.message);
    render();
    return data.ok;
  } catch (e) {
    haptic("no");
    toast(e.message);
    return false;
  }
}

// ------------------------------------------------------------------------------ helpers
const money = (n) => (n || 0).toLocaleString("ru-RU");
function clock(seconds) {
  seconds = Math.max(0, Math.floor(seconds || 0));
  const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
  const pad = (v) => (v < 10 ? "0" + v : "" + v);
  return h ? h + ":" + pad(m) + ":" + pad(s) : pad(m) + ":" + pad(s);
}
const STAT_ICON = { strength: "⚔️", health: "❤️", agility: "💨", luck: "🍀", armor: "🛡" };
const STAT_NAME = { strength: "Сила", health: "Здоровье", agility: "Ловкость",
                    luck: "Удача", armor: "Броня" };

function bonusText(bonuses) {
  const parts = [];
  for (const key of ["strength", "health", "agility", "luck", "armor"]) {
    const value = bonuses[key];
    if (!value) continue;
    parts.push((STAT_ICON[key] || key) + (value > 0 ? "+" : "") + value);
  }
  return parts.join(" ") || "—";
}

function affordable(price) { return S && S.coins >= price; }

// ------------------------------------------------------------------------- portraits
// A crop is {x, y, size} in the photo's own pixels and may hang off its edges -- that is
// how "show the whole thing, letterboxed" is expressed, and it is why this cannot be
// object-fit. Identical model and formula to vote_web's applyFrame, deliberately: the two
// croppers must agree, or a square framed in one would sit differently in the other.
function applyCrop(img, crop, framePx) {
  if (!img || !crop || !framePx || !img.naturalWidth || !Number(crop.size)) return false;
  const scale = framePx / Number(crop.size);
  if (!isFinite(scale) || scale <= 0) return false;
  img.classList.remove("cover");
  img.style.width = (img.naturalWidth * scale) + "px";
  img.style.height = (img.naturalHeight * scale) + "px";
  img.style.left = (-Number(crop.x) * scale) + "px";
  img.style.top = (-Number(crop.y) * scale) + "px";
  return true;
}

// Framed photos are painted after layout, because the crop needs both the photo's natural
// size (which arrives with the load event) and the frame's width (which needs a layout).
function paintShots(root) {
  for (const box of (root || document).querySelectorAll(".shot[data-crop]")) {
    const img = box.querySelector("img");
    if (!img) continue;
    let crop = null;
    try { crop = JSON.parse(box.dataset.crop || "null"); } catch (e) { crop = null; }
    const draw = () => {
      // No crop, or a photo that has not loaded: cover is the honest default -- it fills
      // the square and never leaves a hole, which is what an un-framed pet had before.
      if (!crop || !applyCrop(img, crop, box.clientWidth)) img.classList.add("cover");
    };
    if (img.complete && img.naturalWidth) draw();
    else img.addEventListener("load", draw, { once: true });
  }
}

function shot(url, crop, extra) {
  return '<span class="shot"' + (crop ? " data-crop='" + esc(JSON.stringify(crop)) + "'" : "") +
    '><img src="' + esc(url) + '" alt="" class="cover" loading="lazy">' + (extra || "") + "</span>";
}

// ---------------------------------------------------------------------------- the HUD
function renderHud() {
  const pet = S && S.pet;
  $("hudName").textContent = pet ? pet.name : "Без существа";
  $("hudLevel").textContent = pet ? pet.level : "—";
  $("hudCoins").textContent = money(S ? S.coins : 0);
  $("hudFace").innerHTML = pet ? shot(pet.portrait, pet.crop) : "🥚";
  if (pet) paintShots($("hudFace"));
  const arena = (S && S.arena) || {};
  $("hudFights").textContent = (arena.available != null ? arena.available : 0) +
    "/" + (arena.capacity != null ? arena.capacity : 0);
  const left = arena.seconds_until_next;
  $("hudRecharge").textContent = left ? " · " + clock(left) : "";
  const xpNeed = pet ? Math.max(1, pet.xp_needed) : 1;
  $("hudXp").style.width = (pet ? Math.min(100, (pet.xp / xpNeed) * 100) : 0) + "%";
}

// ------------------------------------------------------------------------ hero screen
function renderHero() {
  const box = $("scr-hero");
  if (!S.pet) { box.innerHTML = renderOnboarding(); return; }
  const pet = S.pet, combat = S.combat;
  const slot = (s) => {
    const item = s.item;
    return '<button class="slot ' + (item ? "filled r-" + item.rarity : "") + '" ' +
      'data-slot="' + s.slot + '" data-code="' + (item ? esc(item.code) : "") + '">' +
      (item ? '<img src="' + esc(item.art) + '" alt="" loading="lazy">' +
              '<span class="tag">' + esc(item.name) + "</span>"
            : '<span class="ph">' + s.emoji + "</span>" +
              '<span class="tag">' + esc(s.name) + "</span>") +
      "</button>";
  };
  const worn = {};
  for (const s of S.equipment) worn[s.slot] = s;

  box.innerHTML =
    '<div class="panel">' +
      '<div class="doll">' +
        "<div>" + slot(worn.weapon) + "</div>" +
        // Tapping the portrait is how you change and frame the photo. It is the one thing
        // on this screen that is a picture, so it is where a hand goes looking.
        '<button class="portrait" data-do="portrait">' +
          shot(pet.portrait, pet.crop) +
          '<span class="edit">✏️</span>' +
          '<span class="pw">⚡ ' + money(combat.power) + "</span></button>" +
        "<div>" + slot(worn.amulet) + "</div>" +
        "<div>" + slot(worn.gloves) + "</div>" +
        '<div class="tiny muted" style="text-align:center">' +
          esc(pet.name) + " · ур. " + pet.level + "<br>" +
          pet.xp + " / " + pet.xp_needed + " опыта<br>" +
          "боёв " + pet.fights + " · побед " + pet.wins +
        "</div>" +
        "<div>" + slot(worn.boots) + "</div>" +
      "</div>" +
    "</div>" +

    '<div class="panel"><h2>В бою</h2><div class="grid4">' +
      tile("❤️ Здоровье", combat.max_hp) +
      tile("⚔️ Урон", combat.damage) +
      tile("💨 Уклонение", combat.dodge + "%") +
      tile("🎯 Крит", combat.crit + "%") +
      tile("🛡 Поглощение", combat.reduction + "%") +
      tile("⚡ Сила героя", money(combat.power)) +
    "</div></div>" +

    '<div class="panel"><h2>Характеристики</h2>' +
      S.stats.map(statRow).join("") +
    "</div>" +

    cagePanel() + dailyPanel() +
    '<button class="go sec" data-do="rename">✏️ Переименовать</button>';
  paintShots(box);
}

function tile(label, value) {
  return '<div class="stat-tile"><div class="k">' + label + '</div><div class="v">' +
         value + "</div></div>";
}

function statRow(stat) {
  const bonus = stat.bonus ? ' <span class="gain">+' + stat.bonus + "</span>" : "";
  const maxed = stat.max != null && stat.purchased >= stat.max;
  const buttons = stat.gear_only
    ? '<span class="tiny muted">только с вещей</span>'
    : (maxed
        ? '<span class="tiny muted">максимум</span>'
        : '<button class="plus" data-up="' + stat.key + '" data-times="1"' +
            (affordable(stat.cost_1) ? "" : " disabled") + ">+1 · " + money(stat.cost_1) + "</button>" +
          '<button class="plus" data-up="' + stat.key + '" data-times="10"' +
            (affordable(stat.cost_10) ? "" : " disabled") + ">+10 · " + money(stat.cost_10) + "</button>");
  return '<div class="statrow"><div class="lbl">' +
    (STAT_ICON[stat.key] || "") + " <b>" + esc(stat.name) + "</b> " +
    '<span class="muted small">' + stat.purchased + "</span> → <b>" + stat.effective + "</b>" + bonus +
    "</div>" + buttons + "</div>";
}

function cagePanel() {
  const cage = S.cage;
  if (!S.has_cage) {
    return '<div class="panel"><h2>Клетка</h2>' +
      '<p class="small muted" style="margin:0 0 10px">Без клетки существо негде держать. ' +
      "Это первая покупка в игре.</p>" +
      '<button class="go" data-do="buycage"' + (affordable(cage.price) ? "" : " disabled") +
      ">Купить клетку · " + money(cage.price) + "</button></div>";
  }
  const top = cage.level >= cage.max;
  return '<div class="panel"><h2>Клетка</h2>' +
    '<div class="row spread"><span>Уровень ' + cage.level + " из " + cage.max + "</span>" +
    (top ? '<span class="muted small">максимум</span>' : "") + "</div>" +
    (top ? "" : '<button class="go sec" style="margin-top:10px" data-do="upcage"' +
      (affordable(cage.upgrade_cost) ? "" : " disabled") +
      ">Улучшить · " + money(cage.upgrade_cost) + "</button>") + "</div>";
}

function dailyPanel() {
  const daily = S.daily_bonus;
  return '<div class="panel"><h2>Ежедневный бонус</h2>' +
    '<div class="row spread"><span class="small">Серия: <b>' + daily.streak + "</b> дн.</span>" +
    '<span class="small muted">завтра ' + money(daily.tomorrow) + "</span></div>" +
    '<button class="go" style="margin-top:10px" data-do="daily"' +
      (daily.can_claim ? "" : " disabled") + ">" +
      (daily.can_claim ? "🎁 Забрать " + money(daily.amount) : "Сегодня уже забрано") +
    "</button></div>";
}

function renderOnboarding() {
  const cage = S.cage;
  if (!S.has_cage) {
    return '<div class="panel"><h2>Начало</h2>' +
      "<p>Тут живут существа: их растят, одевают и стравливают на арене.</p>" +
      "<p class='small muted'>Сначала нужна клетка, потом — своя раскрашенная фигурка.</p>" +
      '<button class="go" data-do="buycage"' + (affordable(cage.price) ? "" : " disabled") +
      ">Купить клетку · " + money(cage.price) + "</button></div>" + dailyPanel();
  }
  return '<div class="panel"><h2>Клетка готова</h2>' +
    "<p>Осталось приручить существо — для этого нужна фотография твоей фигурки.</p>" +
    "<p class='small muted'>Фото принимает бот в переписке: приручение и смена фото " +
    "живут там, потому что картинку сюда не передать.</p>" +
    '<button class="go" data-do="tobot">Открыть чат с ботом</button></div>' + dailyPanel();
}

// ------------------------------------------------------------------------- bag screen
let bagSlot = "all", bagRarity = "all", bagSort = "rarity";

function renderBag() {
  const box = $("scr-bag");
  if (!S.pet) { box.innerHTML = '<div class="empty">Сначала нужно существо.</div>'; return; }
  let items = S.bag.slice();
  if (bagSlot !== "all") items = items.filter((i) => i.slot === bagSlot);
  if (bagRarity !== "all") items = items.filter((i) => i.rarity === bagRarity);
  items.sort((a, b) => bagSort === "rarity"
    ? b.rarity_rank - a.rarity_rank || a.name.localeCompare(b.name)
    : b.resale - a.resale);

  box.innerHTML =
    '<div class="chiprow">' + slotChips(bagSlot, "bagslot") + "</div>" +
    '<div class="chiprow">' + rarityChips(bagRarity, "bagrarity") +
      '<button class="chip' + (bagSort === "price" ? " on" : "") + '" data-bagsort="1">💰 по цене</button>' +
    "</div>" +
    '<div class="panel"><h2>Надето</h2><div class="items">' +
      S.equipment.map((s) => s.item ? itemCard(s.item, "равно") : "").join("") +
      (S.equipment.every((s) => !s.item) ? '<div class="empty">Пока ничего не надето.</div>' : "") +
    "</div></div>" +
    '<div class="panel"><h2>В сумке · ' + items.length + "</h2>" +
      (items.length ? '<div class="items">' + items.map((i) => itemCard(i)).join("") + "</div>"
                    : '<div class="empty">Пусто. Загляни в лавку или выиграй в арене.</div>') +
    "</div>";
}

// `skipAll` for the shop, where "everything" is not a shelf you can stand at -- the daily
// weapon rotation and the permanent accessories are priced and stocked differently.
function slotChips(active, key, skipAll) {
  const slots = [["all", "Всё"], ["weapon", "🗡 Оружие"], ["amulet", "📿 Амулеты"],
                 ["gloves", "🧤 Перчатки"], ["boots", "👢 Сапоги"]];
  return slots.filter(([value]) => !(skipAll && value === "all")).map(([value, label]) =>
    '<button class="chip' + (active === value ? " on" : "") + '" data-' + key + '="' + value + '">' +
    label + "</button>").join("");
}

function rarityChips(active, key) {
  const rarities = [["all", "Любая"], ["legendary", "🟣"], ["rare", "🔵"],
                    ["uncommon", "🟢"], ["common", "⚪"], ["cursed", "☠️"]];
  return rarities.map(([value, label]) =>
    '<button class="chip' + (active === value ? " on" : "") + '" data-' + key + '="' + value + '">' +
    label + "</button>").join("");
}

function itemArt(item, marks) {
  return '<span class="art"><img src="' + esc(item.art) +
    '" alt="" width="210" height="210" loading="lazy">' + (marks || "") + "</span>";
}

function itemCard(item, flag) {
  const marks = (item.equipped ? '<span class="flag">надето</span>'
                               : (flag ? '<span class="flag">' + flag + "</span>" : "")) +
                (item.locked ? '<span class="lockmark">🔒</span>' : "");
  return '<button class="item r-' + item.rarity + '" data-item="' + esc(item.code) + '">' +
    itemArt(item, marks) +
    '<span class="nm">' + esc(item.name) + "</span>" +
    '<span class="meta">' + bonusText(item.bonuses) + "</span></button>";
}

function shopCard(item) {
  const owned = item.owned;
  const can = affordable(item.price) && !owned;
  return '<button class="item r-' + item.rarity + (can || owned ? "" : " dim") +
    '" data-item="' + esc(item.code) + '">' +
    itemArt(item, owned ? '<span class="flag">есть</span>' : "") +
    '<span class="nm">' + esc(item.name) + "</span>" +
    '<span class="meta">' + bonusText(item.bonuses) + " · 💰" + money(item.price) + "</span></button>";
}

// ------------------------------------------------------------------------ shop screen
let shopSlot = "weapon";

async function renderShop() {
  const box = $("scr-shop");
  if (!S.pet) { box.innerHTML = '<div class="empty">Сначала нужно существо.</div>'; return; }
  if (!SHOP) { box.innerHTML = '<div class="empty">Загружаю…</div>'; SHOP = await api("/api/shop"); }
  const items = shopSlot === "weapon"
    ? SHOP.weapons
    : SHOP.accessories.filter((i) => i.slot === shopSlot);
  box.innerHTML =
    '<div class="chiprow">' + slotChips(shopSlot, "shopslot", true) + "</div>" +
    '<div class="panel"><h2>' +
      (shopSlot === "weapon" ? "Витрина дня · меняется каждый день" : "Всегда в продаже") +
    "</h2>" +
    (items.length ? '<div class="items">' + items.map(shopCard).join("") + "</div>"
                  : '<div class="empty">Сегодня тут пусто.</div>') +
    "</div>";
}

// ----------------------------------------------------------------------- arena screen
async function renderArena() {
  const box = $("scr-arena");
  if (!S.pet) { box.innerHTML = '<div class="empty">Сначала нужно существо.</div>'; return; }
  const arena = S.arena;
  if (!FOES) { box.innerHTML = '<div class="empty">Ищу соперников…</div>';
               FOES = await api("/api/opponents"); }

  const blocked = arena.farming
    ? "Существо на ферме — оттуда не дерутся."
    : (arena.available > 0 ? null : "Бои кончились. Восстановление: " +
        clock(arena.seconds_until_next) + ".");

  box.innerHTML =
    '<div class="panel"><div class="row spread">' +
      "<div><div class='tiny muted'>Твоя сила</div><div class='pw' style='font-size:22px'>⚡ " +
        money(FOES.me_power) + "</div></div>" +
      "<div style='text-align:right'><div class='tiny muted'>Бои</div><div style='font-size:22px;font-weight:700'>" +
        arena.available + " / " + arena.capacity + "</div></div>" +
    "</div>" +
    (arena.seconds_until_next ? '<div class="tiny muted" style="margin-top:6px">Следующий бой через ' +
      clock(arena.seconds_until_next) + "</div>" : "") +
    "</div>" +
    (blocked ? '<div class="panel"><div class="small">' + esc(blocked) + "</div></div>" : "") +
    '<div class="panel"><h2>Соперники · ' + FOES.opponents.length + "</h2>" +
      (FOES.opponents.length
        ? FOES.opponents.map((foe) => foeRow(foe, !blocked)).join("")
        : '<div class="empty">Больше ни у кого нет существа.</div>') +
    "</div>";
  paintShots(box);
}

function foeRow(foe, canFight) {
  const usable = canFight && foe.attackable;
  return '<button class="foe' + (usable ? "" : " out") + '" data-foe="' + esc(foe.user_id) + '"' +
    (usable ? "" : " disabled") + '>' +
    '<span class="av">' + shot(foe.portrait, foe.crop) + "</span>" +
    "<span><b>" + esc(foe.name || "Существо") + "</b> <span class='muted small'>ур. " + foe.level +
      "</span><br><span class='tiny muted'>" + esc(foe.owner_name || "") +
      " · побед " + foe.wins + " из " + foe.fights +
      (foe.attackable ? "" : " · сегодня уже хватит") + "</span></span>" +
    "<span class='pw'>⚡ " + money(foe.power) + "</span></button>";
}

// ------------------------------------------------------------------------ farm screen
function renderFarm() {
  const box = $("scr-farm");
  if (!S.pet) { box.innerHTML = '<div class="empty">Сначала нужно существо.</div>'; return; }
  const farm = S.farm;
  if (!farm.level) {
    box.innerHTML = '<div class="panel"><h2>Ферма</h2>' +
      "<p class='small'>Ферма приносит монеты и опыт, пока ты занят чем-то ещё.</p>" +
      '<button class="go" data-do="farmup"' + (affordable(farm.next_level_cost) ? "" : " disabled") +
      ">🏡 Построить · " + money(farm.next_level_cost) + "</button></div>";
    return;
  }

  let shift;
  if (farm.running && !farm.ready) {
    const total = Math.max(1, (farm.planned_hours || 1) * 3600);
    const done = Math.max(0, total - (farm.seconds_left || 0));
    shift = '<div class="panel"><h2>Смена идёт</h2>' +
      '<div class="row spread small"><span>' + (farm.planned_hours || 0) + " ч</span><span>осталось " +
        clock(farm.seconds_left) + "</span></div>" +
      '<div class="bar" style="height:9px;margin-top:8px"><i style="width:' +
        Math.min(100, (done / total) * 100) + '%"></i></div>' +
      (farm.reward ? '<div class="small muted" style="margin-top:8px">Ожидается: 💰' +
        money(farm.reward.gold) + " · ✨" + money(farm.reward.xp) + "</div>" : "") +
      '<button class="go sec" style="margin-top:10px" data-do="farmcancel">❌ Забрать сейчас</button></div>';
  } else if (farm.ready) {
    shift = '<div class="panel"><h2>Смена готова</h2>' +
      '<button class="go" data-do="farmcancel">Забрать награду</button></div>';
  } else {
    shift = '<div class="panel"><h2>Отправить на смену</h2>' +
      '<div class="items" style="grid-template-columns:repeat(auto-fill,minmax(74px,1fr))">' +
      (farm.hour_previews || []).map((preview) =>
        '<button class="chip" style="border-radius:12px;padding:9px 4px;text-align:center;display:block" ' +
        'data-farmstart="' + preview.hours + '">' +
        "<b>" + preview.hours + " ч</b><br><span class='tiny muted'>💰" + money(preview.gold) +
        "<br>✨" + money(preview.xp) + "<br>🎁" + Math.round(preview.drop_chance * 100) + "%</span></button>"
      ).join("") + "</div></div>";
  }

  const passive = farm.passive || {};
  box.innerHTML = shift +
    '<div class="panel"><h2>Ферма · уровень ' + farm.level + " из " + farm.max_level + "</h2>" +
      '<div class="small muted">Пассивный доход: ' + money(passive.rate || 0) + " монет/час, накоплено " +
        money(passive.stored || 0) + " из " + money(passive.cap || 0) + "</div>" +
      (farm.next_level_cost != null
        ? '<button class="go sec" style="margin-top:10px" data-do="farmup"' +
          (affordable(farm.next_level_cost) ? "" : " disabled") +
          ">⬆️ Улучшить · " + money(farm.next_level_cost) + "</button>"
        : '<div class="small muted" style="margin-top:8px">Максимальный уровень.</div>') +
    "</div>" +
    '<div class="panel"><h2>Постройки</h2>' +
      Object.entries(farm.features || {}).map(([key, feature]) =>
        '<div class="row spread" style="margin-bottom:8px"><span class="small"><b>' +
        esc(featureName(key)) + "</b><br><span class='tiny muted'>" + esc(feature.effect || "") +
        "</span></span>" +
        (feature.level >= feature.max_level
          ? '<span class="tiny muted">есть</span>'
          : '<button class="plus" data-feature="' + key + '"' +
            (affordable(feature.next_cost) ? "" : " disabled") + ">💰" + money(feature.next_cost) +
            "</button>") + "</div>").join("") +
    "</div>";
}

const FEATURE_NAMES = { well: "Колодец", sprinkler: "Поливалка", beds: "Грядка", tractor: "Трактор" };
function featureName(key) { return FEATURE_NAMES[key] || key; }

// ------------------------------------------------------------------------ more screen
let moreView = "menu";

async function renderMore() {
  const box = $("scr-more");
  if (moreView === "menu") {
    box.innerHTML = '<div class="panel"><h2>Ещё</h2>' +
      ["ranking:🏆 Рейтинг существ", "collection:📚 Коллекция оружия",
       "history:📜 История боёв", "updates:📰 Обновления"].map((entry) => {
        const [key, label] = entry.split(":");
        return '<button class="go sec" style="margin-bottom:8px" data-more="' + key + '">' +
               label + "</button>";
      }).join("") + "</div>" +
      '<div class="panel"><h2>Настройки</h2><div class="row spread">' +
        "<span class='small'>Отчёты о боях в личку</span>" +
        '<button class="chip' + (S.pet && S.pet.notifications ? " on" : "") +
          '" data-do="notify">' + (S.pet && S.pet.notifications ? "включены" : "выключены") +
        "</button></div></div>";
    return;
  }
  box.innerHTML = '<button class="go sec" data-more="menu">◀️ Назад</button>' +
                  '<div class="empty">Загружаю…</div>';
  let body = "";
  if (moreView === "ranking") {
    const data = await api("/api/leaderboard");
    body = '<div class="panel"><h2>Рейтинг</h2>' + (data.rows.length
      ? data.rows.map((row) =>
          '<div class="foe" style="cursor:default">' +
          '<span class="av">' + shot(row.portrait, null) + "</span>" +
          "<span class='small'><b>" + row.rank + ".</b> " + esc(row.name || "—") +
          (row.user_id === data.me ? " <span class='tiny muted'>(ты)</span>" : "") +
          "<br><span class='tiny muted'>" + esc(row.owner_name || "") + "</span></span>" +
          "<span class='pw'>⚡ " + money(row.power) + "</span></div>").join("")
      : '<div class="empty">Пока пусто.</div>') + "</div>";
  } else if (moreView === "collection") {
    const data = await api("/api/collection");
    body = '<div class="panel"><h2>Найдено оружия · ' + data.rows.length + " из " + data.total +
      "</h2>" + (data.rows.length
        ? '<div class="items">' + data.rows.map((i) => itemCard(i)).join("") + "</div>"
        : '<div class="empty">Ещё ничего не найдено.</div>') + "</div>";
  } else if (moreView === "history") {
    const data = await api("/api/history");
    body = '<div class="panel"><h2>Последние бои</h2>' + (data.rows.length
      ? data.rows.map(historyRow).join("")
      : '<div class="empty">Боёв ещё не было.</div>') + "</div>";
  } else if (moreView === "updates") {
    const data = await api("/api/updates");
    body = (data.rows || []).map((row) =>
      '<div class="panel"><h2>' + esc(row.title || "Обновление") + "</h2>" +
      '<div class="small" style="white-space:pre-wrap">' + esc(row.text || "") +
      "</div></div>").join("") || '<div class="empty">Пока тихо.</div>';
  }
  box.innerHTML = '<button class="go sec" data-more="menu">◀️ Назад</button>' + body;
  paintShots(box);
}

function historyRow(row) {
  const coins = row.coins
    ? " · <span class='" + (row.coins > 0 ? "gain" : "loss") + "'>💰" +
      (row.coins > 0 ? "+" : "") + row.coins + "</span>"
    : "";
  return '<div class="row spread small" style="margin-bottom:7px"><span>' +
    (row.attacked ? "Ты напал на " : "На тебя напал ") + "<b>" + esc(row.opponent) + "</b>" +
    "</span><span class='tiny'>" + esc(row.outcome) + coins + "</span></div>";
}

// -------------------------------------------------------------------------- item sheet
function openItem(code) {
  const pool = (S.bag || []).concat(
    (S.equipment || []).map((s) => s.item).filter(Boolean),
    SHOP ? SHOP.weapons.concat(SHOP.accessories) : []);
  const item = pool.find((i) => i && i.code === code);
  if (!item) return;

  const wornHere = (S.equipment.find((s) => s.slot === item.slot) || {}).item;
  const deltas = [];
  for (const key of ["strength", "health", "agility", "luck", "armor"]) {
    const change = (item.bonuses[key] || 0) - ((wornHere && wornHere.bonuses[key]) || 0);
    if (change) deltas.push('<span class="' + (change > 0 ? "gain" : "loss") + '">' +
      (STAT_ICON[key] || key) + " " + (change > 0 ? "+" : "") + change + "</span>");
  }

  const actions = [];
  if (item.owned && !item.equipped) actions.push(btn("Надеть", "equip", item.code));
  if (item.equipped) actions.push(btn("Снять", "unequip", item.slot, "sec"));
  if (item.owned) {
    actions.push('<div class="pair">' +
      btn(item.locked ? "🔓 Разблокировать" : "🔒 Заблокировать", "lock", item.code, "sec") +
      (item.locked || item.equipped ? ""
        : btn("💰 Продать · " + money(item.resale), "sell", item.code, "sec")) + "</div>");
    if (!item.locked && !item.equipped) actions.push(btn("🎁 Подарить", "gift", item.code, "sec"));
  }
  if (!item.owned && item.source === "shop") {
    actions.push('<button class="go" data-act="buy" data-code="' + esc(item.code) + '"' +
      (affordable(item.price) ? "" : " disabled") + ">Купить · " + money(item.price) + "</button>");
  }
  if (!item.owned && item.source === "drop") {
    actions.push('<div class="small muted">Такое не продаётся — только выпадает в бою или на ферме.</div>');
  }

  sheet(
    '<div class="hd"><img src="' + esc(item.art) + '" alt="">' +
    "<div><h3>" + esc(item.name) + "</h3>" +
    '<div class="small" style="color:var(--r-' + item.rarity + ')">' + esc(item.rarity_name) +
      " · " + esc(item.slot_name) + "</div>" +
    '<div class="small" style="margin-top:5px">' + bonusText(item.bonuses) + "</div></div></div>" +
    (item.description ? '<div class="small muted">' + esc(item.description) + "</div>" : "") +
    (item.effect && item.effect.text
      ? '<div class="small" style="margin-top:8px">✨ ' + esc(item.effect.text) + "</div>" : "") +
    (deltas.length && !item.equipped
      ? '<div class="panel" style="margin-top:10px"><h2>Если надеть вместо ' +
        (wornHere ? esc(wornHere.name) : "пустого слота") + "</h2>" + deltas.join(" ") + "</div>"
      : "") +
    '<div class="acts">' + actions.join("") + "</div>"
  );
}

// Tapping a slot on the paperdoll asks "what can go here?", not "what is here?" -- so it
// opens everything that FITS, with what is worn at the top and every alternative one tap
// from being worn instead. Opening only the equipped item's card (which is what it used to
// do) answered a question nobody standing in front of an equipment screen is asking.
function openSlot(slotKey) {
  const slot = (S.equipment || []).find((s) => s.slot === slotKey);
  if (!slot) return;
  const worn = slot.item;
  const others = (S.bag || []).filter((i) => i.slot === slotKey && !i.equipped);

  const delta = (item) => {
    // Against what is worn right now, because that is the actual trade being considered.
    const parts = [];
    for (const key of ["strength", "health", "agility", "luck", "armor"]) {
      const change = (item.bonuses[key] || 0) - ((worn && worn.bonuses[key]) || 0);
      if (change) parts.push('<span class="' + (change > 0 ? "gain" : "loss") + '">' +
        (STAT_ICON[key] || key) + (change > 0 ? "+" : "") + change + "</span>");
    }
    return parts.join(" ") || '<span class="muted">без изменений</span>';
  };

  sheet(
    "<h3>" + slot.emoji + " " + esc(slot.name) + "</h3>" +
    (worn
      ? '<div class="hd"><img src="' + esc(worn.art) + '" alt="">' +
        "<div><div class='tiny muted'>надето</div><b>" + esc(worn.name) + "</b>" +
        '<div class="small" style="color:var(--r-' + worn.rarity + ')">' + esc(worn.rarity_name) + "</div>" +
        '<div class="small">' + bonusText(worn.bonuses) + "</div></div></div>" +
        '<button class="go sec" data-act="unequip" data-code="' + esc(slot.slot) + '">Снять</button>'
      : '<div class="small muted">Слот пустой.</div>') +
    (others.length
      ? '<div class="panel" style="margin-top:12px"><h2>Надеть вместо ' +
        (worn ? "этого" : "пустого") + " · " + others.length + "</h2>" +
        '<div class="items">' + others.map((item) =>
          '<button class="item r-' + item.rarity + '" data-equipnow="' + esc(item.code) + '">' +
          itemArt(item, item.locked ? '<span class="lockmark">🔒</span>' : "") +
          '<span class="nm">' + esc(item.name) + "</span>" +
          '<span class="meta">' + delta(item) + "</span></button>").join("") +
        "</div></div>"
      : '<div class="panel" style="margin-top:12px"><div class="empty">' +
        "Больше ничего для этого слота нет.</div>" +
        '<button class="go sec" data-shoptab="' + esc(slotKey) +
        '">🛒 Посмотреть в лавке</button></div>')
  );
}

function btn(label, action, argument, kind) {
  return '<button class="go ' + (kind || "") + '" data-act="' + action + '" data-code="' +
    esc(argument) + '">' + label + "</button>";
}

function sheet(html) {
  closeSheet();
  const veil = document.createElement("div");
  veil.className = "veil";
  veil.id = "veil";
  veil.innerHTML = '<div class="sheet">' + html + "</div>";
  veil.addEventListener("click", (event) => { if (event.target === veil) closeSheet(); });
  document.body.appendChild(veil);
}
function closeSheet() { const v = $("veil"); if (v) v.remove(); }

// A rare item is worth a second look before it is gone -- the same rule pets.py enforces
// with its one-time token, shown as a dialog rather than a screen you navigate to.
function confirmThen(text, run) {
  sheet("<h3>" + esc(text) + "</h3><div class='acts'>" +
    '<button class="go warn" id="yes">Да</button>' +
    '<button class="go sec" id="no">Отмена</button></div>');
  $("yes").onclick = () => { closeSheet(); run(); };
  $("no").onclick = closeSheet;
}

async function giftPicker(code) {
  if (!ROSTER) ROSTER = await api("/api/leaderboard");
  const others = ROSTER.rows.filter((row) => row.user_id !== ROSTER.me);
  if (!others.length) { toast("Некому дарить — больше ни у кого нет существа."); return; }
  sheet("<h3>Кому подарить?</h3>" + others.map((row) =>
    '<button class="go sec" style="margin-bottom:8px" data-gift="' + esc(row.user_id) + '" ' +
    'data-code="' + esc(code) + '">' + esc(row.owner_name || row.name) + "</button>").join(""));
}

// -------------------------------------------------------------------- portrait editor
//
// Change the picture, then frame it. The crop is stored as {x, y, size} in the photo's own
// pixels rather than cut out of it, which is this codebase's convention (see
// pets.set_portrait_crop): the picture itself lives on Telegram's servers and is
// re-rendered from a file_id every time, so pixels cut here could never be un-cut.
//
// A Mini App CAN produce a picture -- a file input and a canvas is all it takes. What it
// cannot produce is a file_id, so the upload posts bytes and the server does that half.
let crop = null, natural = null, cropDirty = false;

function minSize() { return Math.max(16, Math.min(natural.w, natural.h) / 8); }
function maxSize() { return Math.max(natural.w, natural.h) * 1.8; }

function fitCrop(size) {
  // The smallest square that holds the whole photo -- letterboxed on the short side, and
  // the same default an un-framed pet has always rendered with.
  const side = Math.max(size.w, size.h);
  return { x: (size.w - side) / 2, y: (size.h - side) / 2, size: side };
}

function clampCrop() {
  crop.size = Math.min(maxSize(), Math.max(minSize(), crop.size));
  // Keep a fifth of the frame on the picture whichever way it is dragged, or the photo can
  // be shoved off its own square and the portrait renders as an empty box.
  const keepX = Math.min(crop.size, natural.w) * 0.2;
  const keepY = Math.min(crop.size, natural.h) * 0.2;
  crop.x = Math.min(Math.max(crop.x, -crop.size + keepX), natural.w - keepX);
  crop.y = Math.min(Math.max(crop.y, -crop.size + keepY), natural.h - keepY);
}

function paintCrop() {
  const stage = $("cropStage"), img = $("cropImg");
  if (!stage || !img || !natural) return;
  clampCrop();
  applyCrop(img, crop, stage.clientWidth);
  img.classList.add("ready");
  const low = minSize(), high = maxSize();
  $("cropZoom").value = String(Math.round(1000 * Math.log(high / crop.size) / Math.log(high / low)));
}

// Zooms to `size` photo-pixels across while keeping whatever is under (ax, ay) where it
// is -- what makes a pinch feel like pulling the photo rather than re-centring it.
function cropZoomTo(size, ax, ay) {
  const stage = $("cropStage");
  const px = stage.clientWidth || 1;
  const bounded = Math.min(maxSize(), Math.max(minSize(), size));
  crop.x = crop.x + (ax / px) * crop.size - (ax / px) * bounded;
  crop.y = crop.y + (ay / px) * crop.size - (ay / px) * bounded;
  crop.size = bounded;
  cropDirty = true;
  paintCrop();
}

function openPortrait() {
  const pet = S.pet;
  cropDirty = false;
  sheet(
    "<h3>Фото существа</h3>" +
    '<div class="stage" id="cropStage"><img id="cropImg" alt=""></div>' +
    '<div class="zoomrow"><button id="cropOut">−</button>' +
      '<input type="range" id="cropZoom" min="0" max="1000" value="0">' +
      '<button id="cropIn">+</button></div>' +
    '<div class="small muted" style="margin-top:8px">Тяни, чтобы двигать. Щипок или ползунок — приблизить.</div>' +
    '<input type="file" id="cropFile" accept="image/*" hidden>' +
    '<div class="acts">' +
      '<button class="go" id="cropSave">Сохранить кадр</button>' +
      '<button class="go sec" id="cropPick">🖼 Выбрать другое фото</button>' +
      (pet.crop ? '<button class="go sec" id="cropReset">Показать фото целиком</button>' : "") +
    "</div>"
  );

  const img = $("cropImg");
  img.src = pet.portrait;
  const ready = () => {
    natural = { w: img.naturalWidth, h: img.naturalHeight };
    crop = pet.crop ? { x: +pet.crop.x, y: +pet.crop.y, size: +pet.crop.size } : fitCrop(natural);
    paintCrop();
  };
  if (img.complete && img.naturalWidth) ready(); else img.addEventListener("load", ready, { once: true });

  wireCropGestures();
  $("cropSave").onclick = async () => {
    const value = crop && { x: crop.x, y: crop.y, size: crop.size };
    closeSheet();
    await act("portrait_crop", { crop: value });
  };
  $("cropPick").onclick = () => $("cropFile").click();
  $("cropFile").onchange = () => uploadPortrait($("cropFile").files[0]);
  if ($("cropReset")) $("cropReset").onclick = async () => {
    closeSheet();
    await act("portrait_crop", { crop: null });
  };
}

function wireCropGestures() {
  const stage = $("cropStage");
  const pointers = new Map();
  let pinch = null;
  const at = (event) => {
    const box = stage.getBoundingClientRect();
    return { x: event.clientX - box.left, y: event.clientY - box.top };
  };
  stage.addEventListener("pointerdown", (event) => {
    if (!natural) return;
    stage.setPointerCapture(event.pointerId);
    pointers.set(event.pointerId, at(event));
    if (pointers.size === 2) {
      const two = [...pointers.values()];
      pinch = { distance: Math.hypot(two[0].x - two[1].x, two[0].y - two[1].y), size: crop.size };
    }
  });
  stage.addEventListener("pointermove", (event) => {
    if (!natural || !pointers.has(event.pointerId)) return;
    const previous = pointers.get(event.pointerId);
    const current = at(event);
    pointers.set(event.pointerId, current);
    if (pointers.size >= 2 && pinch) {
      const two = [...pointers.values()];
      const distance = Math.hypot(two[0].x - two[1].x, two[0].y - two[1].y);
      // Fingers apart => a smaller square of the photo fills the frame => zoomed in.
      if (distance > 0 && pinch.distance > 0) {
        cropZoomTo(pinch.size * (pinch.distance / distance),
                   (two[0].x + two[1].x) / 2, (two[0].y + two[1].y) / 2);
      }
      return;
    }
    const perPixel = crop.size / (stage.clientWidth || 1);
    crop.x -= (current.x - previous.x) * perPixel;
    crop.y -= (current.y - previous.y) * perPixel;
    cropDirty = true;
    paintCrop();
  });
  const done = (event) => {
    pointers.delete(event.pointerId);
    // The pinch ends the moment either finger leaves: measuring the next one-finger drag
    // against a stale two-finger distance is how a crop jumps for no reason.
    if (pointers.size < 2) pinch = null;
  };
  stage.addEventListener("pointerup", done);
  stage.addEventListener("pointercancel", done);
  const nudge = (factor) => {
    const centre = (stage.clientWidth || 1) / 2;
    cropZoomTo(crop.size * factor, centre, centre);
  };
  $("cropIn").onclick = () => nudge(1 / 1.2);
  $("cropOut").onclick = () => nudge(1.2);
  $("cropZoom").addEventListener("input", () => {
    if (!natural) return;
    const low = minSize(), high = maxSize(), centre = (stage.clientWidth || 1) / 2;
    cropZoomTo(high * Math.pow(low / high, Number($("cropZoom").value) / 1000), centre, centre);
  });
}

// The picked file is re-encoded through a canvas before it leaves the phone: a modern
// camera photo is several megabytes of detail nobody will ever see at 210 pixels, and the
// upload is the slowest thing in this whole page over a mobile connection.
const UPLOAD_EDGE = 1280;

async function uploadPortrait(file) {
  if (!file) return;
  toast("Загружаю фото…");
  try {
    const bitmap = await loadImage(file);
    const scale = Math.min(1, UPLOAD_EDGE / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.9));
    if (!blob) throw new Error("Не получилось прочитать картинку");

    const response = await fetch(PREFIX + "/api/portrait", {
      method: "POST",
      headers: { "Content-Type": "image/jpeg", "X-Telegram-Init-Data": initData },
      body: blob,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.message || "Не получилось загрузить");
    S = data.state;
    closeSheet();
    haptic("ok");
    toast(data.message || "Фото обновлено");
    render();
  } catch (e) {
    haptic("no");
    toast(e.message);
  }
}

function loadImage(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const image = new Image();
    image.onload = () => { URL.revokeObjectURL(url); resolve(image); };
    image.onerror = () => { URL.revokeObjectURL(url); reject(new Error("Это не картинка")); };
    image.src = url;
  });
}

// ------------------------------------------------------------------------- the duel
async function fight(opponentId) {
  let data;
  try {
    data = await api("/api/attack", { opponent_id: opponentId });
  } catch (e) { haptic("no"); toast(e.message); FOES = null; render(); return; }

  S = data.state;
  FOES = null;
  const me = data.you;
  const mineName = (S.pet && S.pet.name) || "Ты";
  const theirName = data.opponent.name || "Соперник";
  const startHp = S.combat ? S.combat.max_hp : 100;

  const view = document.createElement("div");
  view.className = "duel";
  view.id = "duel";
  view.innerHTML =
    '<div class="side"><div class="row spread small"><b>' + esc(mineName) +
      '</b><span id="hpMine"></span></div><div class="hpbar"><i id="barMine" style="width:100%"></i></div></div>' +
    '<div class="side"><div class="row spread small"><b>' + esc(theirName) +
      '</b><span id="hpTheirs"></span></div><div class="hpbar"><i id="barTheirs" style="width:100%"></i></div></div>' +
    '<div class="small muted">' + esc(data.opening || "") + "</div>" +
    '<div class="log" id="duelLog"></div>' +
    '<button class="go" id="duelDone">Пропустить</button>';
  document.body.appendChild(view);

  let index = 0, done = false;
  const finish = () => {
    if (done) return;
    done = true;
    const won = data.draw ? null : data.winner === me;
    const reward = data.reward || {};
    const bits = [];
    if (reward.gold) bits.push("💰 +" + reward.gold);
    if (reward.loss_gold) bits.push("💰 −" + reward.loss_gold);
    if (reward.consolation_gold) bits.push("💰 +" + reward.consolation_gold);
    if (reward.xp) bits.push("✨ +" + reward.xp);
    if (reward.levels_gained) bits.push("⬆️ уровень " + reward.level);
    $("duelLog").insertAdjacentHTML("beforeend",
      '<div class="verdict">' + (data.draw ? "Ничья" : (won ? "Победа" : "Поражение")) + "</div>" +
      '<div class="small muted" style="text-align:center">' + esc(data.closing || "") + "</div>" +
      (bits.length ? '<div class="small" style="text-align:center;margin-top:8px">' +
        bits.join(" · ") + "</div>" : "") +
      (data.dropped ? '<div class="loot" style="margin-top:12px"><img src="' +
        esc(data.dropped.art) + '" alt=""><div><b>' + esc(data.dropped.name) + "</b><br>" +
        "<span class='small muted'>" + (reward.auto_equipped ? "надето сразу" : "лежит в сумке") +
        "</span></div></div>" : ""));
    $("duelLog").scrollTop = $("duelLog").scrollHeight;
    $("duelDone").textContent = "Закрыть";
    $("duelDone").onclick = () => { view.remove(); render(); };
    haptic(won ? "ok" : "no");
  };

  const step = () => {
    if (index >= data.rounds.length) { finish(); return; }
    const round = data.rounds[index++];
    const mineTurn = String(round.attacker) === String(me);
    const attackerHp = Math.max(0, round.attacker_hp), defenderHp = Math.max(0, round.defender_hp);
    const mineHp = mineTurn ? attackerHp : defenderHp;
    const theirsHp = mineTurn ? defenderHp : attackerHp;
    $("barMine").style.width = Math.min(100, (mineHp / Math.max(1, startHp)) * 100) + "%";
    $("barTheirs").style.width = Math.min(100, (theirsHp / Math.max(1, startHp)) * 100) + "%";
    $("hpMine").textContent = mineHp;
    $("hpTheirs").textContent = theirsHp;
    const kind = round.event.indexOf("crit") >= 0 ? "crit"
      : (round.event === "dodge" ? "dodge" : (mineTurn ? "mine" : ""));
    $("duelLog").insertAdjacentHTML("beforeend",
      '<div class="blow ' + kind + '">' + esc(round.text) + "</div>");
    $("duelLog").scrollTop = $("duelLog").scrollHeight;
    setTimeout(step, 520);
  };
  $("duelDone").onclick = () => {
    while (index < data.rounds.length) { const r = data.rounds[index++];
      $("duelLog").insertAdjacentHTML("beforeend", '<div class="blow">' + esc(r.text) + "</div>"); }
    finish();
  };
  step();
}

// ------------------------------------------------------------------------ the router
function render() {
  renderHud();
  for (const name of ["hero", "bag", "shop", "arena", "farm", "more"]) {
    $("scr-" + name).hidden = name !== TAB;
  }
  for (const button of $("tabs").children) button.classList.toggle("on", button.dataset.tab === TAB);
  if (TAB === "hero") renderHero();
  else if (TAB === "bag") renderBag();
  else if (TAB === "shop") renderShop();
  else if (TAB === "arena") renderArena();
  else if (TAB === "farm") renderFarm();
  else if (TAB === "more") renderMore();
}

$("tabs").addEventListener("click", (event) => {
  const button = event.target.closest("[data-tab]");
  if (!button) return;
  TAB = button.dataset.tab;
  if (TAB === "more") moreView = "menu";
  haptic();
  render();
});

// One delegated handler for the whole game. Every control is a data- attribute rather than
// a bound listener, so a re-render (which replaces all of it) cannot leave a dead button
// or a duplicated one behind.
document.addEventListener("click", async (event) => {
  const target = event.target.closest("[data-item],[data-slot],[data-up],[data-do],[data-act]," +
    "[data-bagslot],[data-bagrarity],[data-bagsort],[data-shopslot],[data-foe],[data-more]," +
    "[data-farmstart],[data-feature],[data-gift],[data-equipnow],[data-shoptab]");
  if (!target) return;
  const d = target.dataset;

  // Equipping straight from the slot sheet: one tap, no detour through the item's own
  // card. Choosing between two swords is the whole point of that screen.
  if (d.equipnow) { closeSheet(); await act("equip", { code: d.equipnow }); return; }
  if (d.shoptab) { closeSheet(); TAB = "shop"; shopSlot = d.shoptab; render(); return; }
  if (d.item) { openItem(d.item); return; }
  if (d.slot !== undefined && d.slot && !d.act) { openSlot(d.slot); return; }
  if (d.bagslot) { bagSlot = d.bagslot; render(); return; }
  if (d.bagrarity) { bagRarity = d.bagrarity; render(); return; }
  if (d.bagsort) { bagSort = bagSort === "price" ? "rarity" : "price"; render(); return; }
  if (d.shopslot) { shopSlot = d.shopslot; render(); return; }
  if (d.more) { moreView = d.more; render(); return; }
  if (d.foe) { haptic(); fight(d.foe); return; }
  if (d.farmstart) { await act("farm_start", { hours: Number(d.farmstart) }); return; }
  if (d.feature) { await act("farm_feature", { feature: d.feature }); return; }
  if (d.gift) { closeSheet(); await act("gift", { code: d.code, receiver_id: d.gift, confirm: true }); return; }

  if (d.up) { await act("upgrade_stat", { stat: d.up, times: Number(d.times) }); return; }

  if (d.act) {
    const code = d.code;
    if (d.act === "equip") { closeSheet(); await act("equip", { code }); SHOP = null; }
    else if (d.act === "unequip") { closeSheet(); await act("unequip", { slot: code }); }
    else if (d.act === "lock") { closeSheet(); await act("lock", { code }); }
    else if (d.act === "buy") { closeSheet(); const ok = await act("buy", { code });
                                if (ok) SHOP = null; render(); }
    else if (d.act === "sell") {
      closeSheet();
      confirmThen("Продать вещь? Вернуть будет нельзя.",
                  () => act("sell", { code, confirm: true }));
    }
    else if (d.act === "gift") { closeSheet(); giftPicker(code); }
    return;
  }

  if (d.do === "buycage") { await act("buy_cage"); }
  else if (d.do === "upcage") { await act("upgrade_cage"); }
  else if (d.do === "daily") { await act("daily_bonus"); }
  else if (d.do === "notify") { await act("notifications"); }
  else if (d.do === "farmup") { await act("farm_upgrade"); }
  else if (d.do === "farmcancel") { await act("farm_cancel"); }
  else if (d.do === "portrait") { openPortrait(); }
  else if (d.do === "tobot") { if (tg) tg.close(); }
  else if (d.do === "rename") {
    sheet("<h3>Новое имя</h3><input id='newName' class='go sec' style='text-align:left' " +
      "maxlength='32' placeholder='Имя существа'><div class='acts'>" +
      "<button class='go' id='saveName'>Сохранить</button></div>");
    $("saveName").onclick = async () => {
      const name = $("newName").value.trim();
      closeSheet();
      if (name) await act("rename", { name });
    };
  }
});

// The two countdowns tick locally between refreshes -- a timer that only moves when you
// pull to refresh does not read as a timer. The numbers are still the server's; this only
// counts down the gap, and a reload puts them right.
function tick() {
  if (!S) return;
  let dirty = false;
  if (S.arena && S.arena.seconds_until_next) {
    S.arena.seconds_until_next = Math.max(0, S.arena.seconds_until_next - 1);
    if (!S.arena.seconds_until_next) dirty = true;
    renderHud();
  }
  if (S.farm && S.farm.seconds_left) {
    S.farm.seconds_left = Math.max(0, S.farm.seconds_left - 1);
    if (!S.farm.seconds_left) dirty = true;
    else if (TAB === "farm") renderFarm();
  }
  if (dirty) refresh();     // something finished -- ask the server what it actually became
}

async function refresh() {
  try {
    S = await api("/api/state");
    FOES = null;
    render();
    for (const receipt of S.farm_receipts || []) {
      toast("Ферма: 💰" + (receipt.gold || 0) + " · ✨" + (receipt.xp || 0));
    }
  } catch (e) {
    document.getElementById("main").innerHTML =
      '<div class="screen"><div class="panel"><h2>Не открылось</h2><div class="small">' +
      esc(e.message) + "</div></div></div>";
  }
}

refresh().then(() => { ticker = setInterval(tick, 1000); });
</script>
</body>
</html>
"""
