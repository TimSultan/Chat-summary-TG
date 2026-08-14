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
import pets_mobs
import pets_gemini
import pets_scroll_catalog
import pets_sprite
import pets_sprite_store
import pets_test_combat
import pets_updates
import quests
import stats
import voting
from pets_ui import mail_day_label, valuable_item  # rules defined once, used by both UIs

ROUTE_PREFIX = "/pets"

_CFG_KEY = web.AppKey("pets_cfg")
_ENTRY_KEY = web.AppKey("pets_entry", str)
_IS_MEMBER_KEY = web.AppKey("pets_is_member", Callable[[dict], Awaitable[bool]])
# Quest moderation and financial audit deliberately use separate gates: reviewing a
# painting can be delegated, while reading everybody's coin ledger cannot.
_IS_ADMIN_KEY = web.AppKey("pets_is_admin", Callable[[dict], Awaitable[bool]])
_IS_ECONOMY_ADMIN_KEY = web.AppKey(
    "pets_is_economy_admin", Callable[[dict], Awaitable[bool]],
)
_BIRTHDAY_NOTIFY_KEY = web.AppKey(
    "pets_birthday_notify", Callable[[str, str, int, int], Awaitable[None]],
)
_RESOLVE_KEY = web.AppKey("pets_resolve_player")
_FETCH_PHOTO_KEY = web.AppKey("pets_fetch_photo")
_SAVE_PHOTO_KEY = web.AppKey("pets_save_photo")
_QUEST_FEEDBACK_KEY = web.AppKey("pets_quest_feedback")
_QUEST_COMPLETION_KEY = web.AppKey("pets_quest_completion")
_PREFIX_KEY = web.AppKey("pets_prefix", str)
_LOG_KEY = web.AppKey("pets_log", Callable[..., None])
_TEST_BATTLE_SESSIONS_KEY = web.AppKey("pets_test_battle_sessions", dict)
_SPRITE_JOBS_KEY = web.AppKey("pets_sprite_jobs", set)

TEST_BATTLE_SESSION_TTL = 30 * 60
TEST_BATTLE_SESSION_LIMIT = 1000

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
RARITY_ORDER = ("cursed", "common", "rare", "legendary")
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


async def _default_is_admin(user: dict) -> bool:
    return False


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
    # The DISK path is a hash of the file id, but the URL is not -- it is keyed on the
    # owner, so /img/pet/42.jpg does point at different pixels once 42 changes their
    # photo. It was served as immutable for a week on the strength of the disk name,
    # which meant a new portrait stayed invisible to everybody who had already loaded the
    # old one. Short freshness plus revalidation instead: FileResponse answers a
    # conditional request with a 304, so the usual case still costs no image bytes, and a
    # replaced photo (a new file_id, hence a new file with a new mtime) shows up at once.
    return web.FileResponse(cached, headers={"Cache-Control": "public, max-age=300"})


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


def _stat_payload(entry: str, user_id, record: dict, stat_points: int = 0) -> list[dict]:
    """A stat row: what it cost, what it is now, and what the gear adds on top.

    Purchased and effective are kept apart because they are spent differently -- coins
    raise the first, equipment raises the second, and a player deciding between a stat
    point and a new amulet is comparing exactly those two numbers.
    """
    effective = pets.effective_stats(entry, user_id)
    purchased = record.get("stats", {})
    stat_points = max(0, int(stat_points or 0))
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
            "cost_1": 0 if stat_points else C.stat_upgrade_cost(base),
            "cost_10": C.total_stat_cost(base + 10, base + min(stat_points, 10)),
            "pending_effect": key == "endurance",
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
        skills=pets._skill_loadout_for(record),
        shield=pets._combat_shield_for(record),
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
    """All equipment slots, including the live Defend shield, always represented.

    An empty slot is a thing to fill, so it is drawn
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


def _skills_payload(record: dict) -> dict:
    loadout = pets._skill_loadout_for(record)
    selected = []
    for index, code in enumerate(loadout, start=1):
        spell = pets_scroll_catalog.scroll(code) if code else None
        if spell is None:
            # An empty slot still ships, so the panel can draw four slots and show which
            # of them are open rather than silently rendering a shorter list.
            selected.append({"slot": index, "code": None, "empty": True,
                             "ultimate": index == 4, "name": "", "icon": "",
                             "short": "", "element": "", "effects_text": []})
            continue
        row = pets_scroll_catalog.public_scroll(spell)
        row["slot"] = index
        row["empty"] = False
        selected.append(row)
    owned = set(pets._owned_scroll_codes_for(record))
    return {
        "slots": selected,
        "owned_count": len(owned),
        "catalogue_count": len(pets_scroll_catalog.SCROLLS),
        "rewards": {
            "paint_chance": pets.PAINT_SCROLL_CHANCE,
            "paint_pity": pets.PAINT_SCROLL_PITY,
            "hard_quest_chances": dict(pets.HARD_QUEST_SCROLL_CHANCES),
            "hard_quest_pity": pets.HARD_QUEST_SCROLL_PITY,
        },
        "regular": [
            pets_scroll_catalog.public_scroll(row) for row in pets_scroll_catalog.REGULAR_SCROLLS
            if row["code"] in owned
        ],
        "ultimate": [
            pets_scroll_catalog.public_scroll(row) for row in pets_scroll_catalog.ULTIMATE_SCROLLS
            if row["code"] in owned
        ],
    }


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
        "quest_attention": quests.has_available_quests(entry, user_id),
    }

    if not record:
        state["pet"] = None
        return state

    state["pet"] = _pet_payload(entry, user_id, record, prefix)
    # Top level rather than inside `pet`, because several screens draw it and none of them
    # is "the pet screen": it belongs to the creature but it explains the stats, the power
    # rating and the arena all at once.
    state["debuff"] = pets.debuff_for(record)
    state["stat_points"] = pets.available_stat_points(record)
    state["stat_respec_ruby_cost"] = C.STAT_RESPEC_RUBY_COST
    state["stats"] = _stat_payload(entry, user_id, record, state["stat_points"])
    state["combat"] = _combat_payload(entry, user_id, record)
    state["equipment"] = _equipment_payload(record, prefix)
    state["skills"] = _skills_payload(record)
    state["bag"] = [
        _item_payload(item, prefix, record)
        for item in (C.find_item(code) for code in record.get("inventory", []))
        if item is not None
    ]
    state["arena"] = pets.fight_allowance_breakdown(entry, user_id)
    state["arena"]["farming"] = pets.is_farming(entry, user_id)
    state["arena"]["pity"] = pets.legendary_pity_progress(entry, user_id)
    state["rubies"] = pets.ruby_balance(entry, user_id)
    state["runes"] = pets.rune_status(entry, user_id)
    state["dungeon"] = pets.dungeon_status(entry, user_id)
    state["pve"] = pets.pve_allowance(entry, user_id)
    state["farm"] = pets.farm_status(entry, user_id)
    state["farm"]["passive"] = pets.passive_income_status(entry, user_id)
    state["forge"] = pets.forge_status(entry, user_id)
    return state


def _shop_payload(entry: str, user_id, prefix: str) -> dict:
    """The personal 12-hour storefront for every equipment slot."""
    record = pets.get_pet(entry, user_id) or {}
    weapons = [_item_payload(item, prefix, record)
           for item in pets.daily_storefront_weapons(entry, user_id=user_id)]
    accessories = [
        _item_payload(item, prefix, record)
        for slot in C.SLOT_KEYS if slot != "weapon"
        for item in pets.daily_storefront_items(entry, slot, user_id=user_id)
    ]
    return {
        "weapons": weapons, "accessories": accessories,
      "rotates_daily": True, "rotation_hours": C.STOREFRONT_ROTATION_HOURS,
    }


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


def _action_respec_stats(entry, user_id, xp, payload):
    ok, message, _points = pets.respec_stats(entry, user_id)
    return ok, message


def _action_equip(entry, user_id, xp, payload):
    return pets.equip(entry, user_id, str(payload.get("code") or ""))


def _action_unequip(entry, user_id, xp, payload):
    return pets.unequip(entry, user_id, str(payload.get("slot") or ""))


def _action_set_skill(entry, user_id, xp, payload):
    return pets.set_skill_slot(
        entry, user_id, payload.get("slot"), str(payload.get("code") or ""),
    )


def _action_lock(entry, user_id, xp, payload):
    ok, message, _locked = pets.toggle_item_lock(entry, user_id, str(payload.get("code") or ""))
    return ok, message


def _action_buy(entry, user_id, xp, payload):
    return pets.buy_item(entry, user_id, xp, str(payload.get("code") or ""))


def _action_reforge(entry, user_id, xp, payload):
    ok, message, _code = pets.reforge_items(
        entry, user_id, str(payload.get("rarity") or ""),
    )
    return ok, message


def _action_enchant_weapon(entry, user_id, xp, payload):
    return pets.enchant_weapon(
      entry, user_id, str(payload.get("code") or ""), str(payload.get("element") or ""),
    )


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


def _action_farm_ticket(entry, user_id, xp, payload):
    return pets.use_farm_ticket(entry, user_id)


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


def _action_dungeon_enter(entry, user_id, xp, payload):
    return pets.enter_dungeon(entry, user_id)


def _action_dungeon_escalator(entry, user_id, xp, payload):
    return pets.enter_dungeon(entry, user_id, escalator=True)


def _action_dungeon_fight(entry, user_id, xp, payload):
  ok, message, result = pets.dungeon_fight(entry, user_id, int(payload.get("index") or 0))
  return ok, message, result


def _action_dungeon_rest(entry, user_id, xp, payload):
    return pets.dungeon_rest(entry, user_id, xp)


def _action_dungeon_descend(entry, user_id, xp, payload):
    return pets.dungeon_descend(entry, user_id)


def _action_dungeon_quit(entry, user_id, xp, payload):
    return pets.quit_dungeon(entry, user_id)


_ACTIONS = {
    "upgrade_stat": _action_upgrade_stat,
  "respec_stats": _action_respec_stats,
    "equip": _action_equip,
    "unequip": _action_unequip,
    "set_skill": _action_set_skill,
    "lock": _action_lock,
    "buy": _action_buy,
    "reforge": _action_reforge,
    "enchant_weapon": _action_enchant_weapon,
    "sell": _action_sell,
    "gift": _action_gift,
    "buy_cage": _action_buy_cage,
    "upgrade_cage": _action_upgrade_cage,
    "rename": _action_rename,
    "farm_start": _action_farm_start,
    "farm_cancel": _action_farm_cancel,
    "farm_ticket": _action_farm_ticket,
    "farm_upgrade": _action_farm_upgrade,
    "farm_feature": _action_farm_feature,
    "daily_bonus": _action_daily_bonus,
    "notifications": _action_notifications,
    "portrait_crop": _action_portrait_crop,
    "dungeon_enter": _action_dungeon_enter,
    "dungeon_escalator": _action_dungeon_escalator,
    "dungeon_fight": _action_dungeon_fight,
    "dungeon_rest": _action_dungeon_rest,
    "dungeon_descend": _action_dungeon_descend,
    "dungeon_quit": _action_dungeon_quit,
}


# ------------------------------------------------------------------------------- routes


async def handle_state(request: web.Request) -> web.Response:
    user, xp = await _player(request)
    entry = request.app[_ENTRY_KEY]
    state = _state_payload(entry, user["id"], xp, request.app[_PREFIX_KEY])
    # Whether the moderation entry is even drawn. A convenience for the menu, never a
    # permission: every route behind it asks the same gate again for itself.
    is_admin, is_economy_admin = await asyncio.gather(
        request.app[_IS_ADMIN_KEY](user),
        request.app[_IS_ECONOMY_ADMIN_KEY](user),
    )
    state["is_admin"] = is_admin
    # Financial history is more sensitive than quest review and uses its own, narrower
    # production gate. The flag only draws a button; the endpoint re-checks it.
    state["is_economy_admin"] = is_economy_admin
    # The queue is intentionally part of the ordinary state, so both moderation entries
    # can light up from the same server-side fact rather than drifting apart.
    state["pending_quests"] = quests.pending_count(entry) if state["is_admin"] else 0
    return _ok(state)


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
        outcome = action(entry, user["id"], xp, body)
        ok, message, extra = (*outcome, None)[:3]
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

    response = {
        "ok": bool(ok),
        "message": message,
        "state": _state_payload(entry, user["id"], xp, request.app[_PREFIX_KEY]),
    }
    if str(body.get("action") or "") == "dungeon_fight" and isinstance(extra, dict):
      result, hero, enemy = extra.get("result"), extra.get("hero"), extra.get("enemy")
      encounter = extra.get("encounter") or {}
      if encounter.get("boss") and result is not None and hero is not None and enemy is not None:
        dropped = extra.get("dropped") or {}
        dropped_item = C.find_item(dropped.get("code")) if isinstance(dropped, dict) else None
        response["battle"] = {
          **_playback_payload(result, str(user["id"]), hero, enemy.key, enemy, encounter.get("name")),
          "dungeon": True, "enemy_art": {"boss": bool(encounter.get("boss"))},
          "reward": extra.get("reward") or {},
          "dropped": _item_payload(dropped_item, request.app[_PREFIX_KEY], pets.get_pet(entry, user["id"])) if dropped_item else None,
        }
    return _ok(response)


async def handle_opponents(request: web.Request) -> web.Response:
    """The field, not one candidate.

    Everyone with a pet, marked with whether you may still attack them today, sorted by how
    close their power is to yours -- an even fight is the interesting one, and a list makes
    that choice visible in a way rerolling a single card never could.
    """
    user, _xp = await _player(request)
    entry = request.app[_ENTRY_KEY]
    me = str(user["id"])
    prefix = request.app[_PREFIX_KEY]

    # This remains blocking work, but _opponents_payload reads the chat store once and
    # derives every row from that one snapshot. It must stay off the event loop because a
    # large fight log is still real disk/JSON work, just no longer multiplied per opponent.
    return _ok(await asyncio.to_thread(_opponents_payload, entry, me, prefix))


# ---------------------------------------------------------------- test turn-based battle

def _test_fighter_snapshot(record: dict, key: str, prefix: str) -> dict:
    return {
        "name": record.get("name") or "Существо",
        "portrait": _portrait_url(prefix, key) if key != "dummy" else None,
        "crop": record.get("portrait_crop"),
        "stats": pets._effective_stats_for(record),
        # Whatever has already been worked out about this photograph. Never classified
        # here: the battle has to start now, and /api/sprite upgrades it a moment later.
        "kind": pets_sprite.cached_archetype(record) or pets_sprite.DEFAULT_ARCHETYPE,
        "owner_id": None if key == "dummy" else str(key),
    }


def _test_dummy_snapshot(mine: dict) -> dict:
    stats = dict(pets._effective_stats_for(mine))
    return {
        "name": "Тренировочный голем", "portrait": None, "crop": None,
        "stats": stats,
        # The golem is a lump with no photograph, and reads as one.
        "kind": "blob",
    }


def _test_battle_setup_payload(entry: str, me: str, prefix: str) -> dict:
    """Read-only setup data. No live arena limits or counters participate."""
    data = pets._load(entry)
    mine = pets._tamed_record(data, me)
    opponents = []
    if mine is not None:
        opponents.append({
            "user_id": "dummy", "name": "Тренировочный голем", "owner_name": "Песочница",
            "portrait": None, "crop": None, "stats": pets._effective_stats_for(mine),
        })
        for opponent_id, record in data.get("pets", {}).items():
            if str(opponent_id) == str(me) or not isinstance(record, dict) or not record.get("name"):
                continue
            opponents.append({
                "user_id": str(opponent_id), "name": record.get("name"),
                "owner_name": record.get("owner_name") or "кто-то",
                "portrait": _portrait_url(prefix, opponent_id), "crop": record.get("portrait_crop"),
                "stats": pets._effective_stats_for(record),
                "kind": pets_sprite.cached_archetype(record) or pets_sprite.DEFAULT_ARCHETYPE,
            })
    return {
        "test_only": True,
        "rules": {
            "base_hp": pets_test_combat.TEST_BASE_HP,
            "hp_per_health": pets_test_combat.TEST_HP_PER_HEALTH,
            "hp_per_strength": pets_test_combat.TEST_HP_PER_STRENGTH,
            "turn_limit": pets_test_combat.TEST_TURN_LIMIT,
            "guard_percent": round(pets_test_combat.BASE_GUARD * 100),
        },
        "defaults": {
            "skills": list(pets_scroll_catalog.SAMPLE_LOADOUT),
            "shield": pets_scroll_catalog.DEFAULT_SHIELD,
        },
        "regular_scrolls": [
            pets_scroll_catalog.public_scroll(row) for row in pets_scroll_catalog.REGULAR_SCROLLS
        ],
        "ultimate_scrolls": [
            pets_scroll_catalog.public_scroll(row) for row in pets_scroll_catalog.ULTIMATE_SCROLLS
        ],
        "shields": [pets_scroll_catalog.public_shield(row) for row in pets_scroll_catalog.SHIELDS],
        "opponents": opponents,
    }


def _prune_test_battles(app: web.Application) -> None:
    sessions = app[_TEST_BATTLE_SESSIONS_KEY]
    now = time.monotonic()
    expired = [token for token, row in sessions.items()
               if now - float(row.get("last_used", 0)) > TEST_BATTLE_SESSION_TTL]
    for token in expired:
        sessions.pop(token, None)
    if len(sessions) > TEST_BATTLE_SESSION_LIMIT:
        oldest = sorted(sessions, key=lambda token: sessions[token].get("last_used", 0))
        for token in oldest[:len(sessions) - TEST_BATTLE_SESSION_LIMIT]:
            sessions.pop(token, None)


async def handle_test_battle_setup(request: web.Request) -> web.Response:
    user, _xp = await _player(request)
    if not await request.app[_IS_MEMBER_KEY](user):
        return _json_error("Тестировать бой могут только участники чата.", status=403,
                           code="NOT_A_MEMBER")
    entry = request.app[_ENTRY_KEY]
    me = str(user["id"])
    if pets.get_pet(entry, me) is None:
        return _json_error("Сначала приручи существо.", status=409, code="NO_PET")
    return _ok(await asyncio.to_thread(
        _test_battle_setup_payload, entry, me, request.app[_PREFIX_KEY],
    ))


async def handle_test_battle_start(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_error("malformed request body")
    user, _xp = await _player(request, body)
    if not await request.app[_IS_MEMBER_KEY](user):
        return _json_error("Тестировать бой могут только участники чата.", status=403,
                           code="NOT_A_MEMBER")
    mode = str(body.get("mode") or "manual")
    if mode == "multiplayer":
        return _ok({
            "ok": False, "test_only": True, "mode": "multiplayer", "status": "coming_soon",
            "message": "Парный режим появится позже — сейчас это только место для будущего матчмейкинга.",
        })
    if mode not in ("manual", "auto"):
        return _json_error("Неизвестный режим.", code="BAD_MODE")

    entry = request.app[_ENTRY_KEY]
    me = str(user["id"])
    data = await asyncio.to_thread(pets._load, entry)
    mine = pets._tamed_record(data, me)
    if mine is None:
        return _json_error("Сначала приручи существо.", status=409, code="NO_PET")
    opponent_id = str(body.get("opponent_id") or "dummy")
    theirs = None if opponent_id == "dummy" else pets._tamed_record(data, opponent_id)
    if opponent_id != "dummy" and theirs is None:
        return _json_error("Соперник больше не доступен.", status=409, code="NO_OPPONENT")
    prefix = request.app[_PREFIX_KEY]
    player = _test_fighter_snapshot(mine, me, prefix)
    enemy = _test_dummy_snapshot(mine) if theirs is None else _test_fighter_snapshot(
        theirs, opponent_id, prefix,
    )
    try:
        loadout = pets_scroll_catalog.validate_loadout(
            body.get("skills") or pets_scroll_catalog.SAMPLE_LOADOUT
        )
        shield_code = str(body.get("shield") or pets_scroll_catalog.DEFAULT_SHIELD)
        if pets_scroll_catalog.shield(shield_code) is None:
            raise ValueError("Неизвестный щит.")
        battle = pets_test_combat.start_battle(
            player, enemy, loadout, pets_scroll_catalog.SAMPLE_LOADOUT,
            shield_code, pets_scroll_catalog.DEFAULT_SHIELD,
        )
    except ValueError as error:
        return _json_error(str(error), code="BAD_LOADOUT")

    if mode == "auto":
        battle = pets_test_combat.run_auto(battle)
        return _ok({
            "ok": True, "mode": mode, "session": None,
            "message": "Автобой завершён. Награды и счётчики не менялись.",
            "battle": pets_test_combat.public_state(battle),
        })

    _prune_test_battles(request.app)
    token = secrets.token_urlsafe(18)
    request.app[_TEST_BATTLE_SESSIONS_KEY][token] = {
        "owner": me, "entry": entry, "mode": mode,
        "last_used": time.monotonic(), "battle": battle,
    }
    return _ok({
        "ok": True, "mode": mode, "session": token,
        "message": "Тестовый бой начался. Результат нигде не запишется.",
        "battle": pets_test_combat.public_state(battle),
    })


async def handle_test_battle_action(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_error("malformed request body")
    user, _xp = await _player(request, body)
    if not await request.app[_IS_MEMBER_KEY](user):
        return _json_error("Тестировать бой могут только участники чата.", status=403,
                           code="NOT_A_MEMBER")
    _prune_test_battles(request.app)
    token = str(body.get("session") or "")
    session = request.app[_TEST_BATTLE_SESSIONS_KEY].get(token)
    if session is None:
        return _json_error("Тестовый бой закончился или устарел.", status=404,
                           code="NO_TEST_SESSION")
    if session.get("owner") != str(user["id"]) or session.get("entry") != request.app[_ENTRY_KEY]:
        return _json_error("Это чужой тестовый бой.", status=403, code="WRONG_TEST_OWNER")
    try:
        action = str(body.get("action") or "")
        if action == "auto":
            battle = pets_test_combat.run_auto(session["battle"])
            session["mode"] = "auto"
        else:
            battle = pets_test_combat.take_turn(session["battle"], "player", action)
            while not battle.get("finished") and battle.get("actor") == "enemy":
                battle = pets_test_combat.auto_turn(battle)
    except ValueError as error:
        return _json_error(str(error), status=409, code="BAD_TEST_ACTION")
    session["battle"] = battle
    session["last_used"] = time.monotonic()
    return _ok({
        "ok": True, "mode": session.get("mode", "manual"), "session": token,
        "message": "Тест завершён — ничего не записано." if battle.get("finished") else "",
        "battle": pets_test_combat.public_state(battle),
    })


def _opponents_payload(entry: str, me: str, prefix: str) -> dict:
    data = pets._load(entry)
    mine_record = pets._tamed_record(data, me)
    mine = pets._power_rating_for(mine_record) if mine_record else 0
    today = pets.today()
    today_key = today.isoformat()
    attacks_today = {}
    for fight in data.get("fights", []):
        if fight.get("date") != today_key or str(fight.get("attacker_id")) != me:
            continue
        defender_id = str(fight.get("defender_id"))
        attacks_today[defender_id] = attacks_today.get(defender_id, 0) + 1
    attacker_can_fight = bool(mine_record) and not pets._is_farming_record(mine_record)
    opponents = []
    for opponent_id, record in data.get("pets", {}).items():
        if opponent_id == me or not isinstance(record, dict) or not record.get("name"):
            continue
        power = pets._power_rating_for(record)
        used = attacks_today.get(str(opponent_id), 0)
        opponents.append({
            "user_id": str(opponent_id),
            "portrait": _portrait_url(prefix, opponent_id),
            "crop": record.get("portrait_crop"),
            "name": record.get("name"),
            "owner_name": record.get("owner_name") or "кто-то",
            "owner_username": record.get("owner_username"),
            "power": power,
            "level": int(record.get("level", 1)),
            "fights": int(record.get("fights", 0)),
            "wins": int(record.get("wins", 0)),
            "stats": pets._effective_stats_for(record),
            # Carried per row so the roster can mark somebody quietly in place. The power
            # beside it is already the reduced one -- `_effective_stats_for` applied the
            # scale -- so the badge explains a number the player can otherwise only wonder
            # about.
            "debuff": pets.debuff_for(record),
            "attackable": attacker_can_fight and used < C.ARENA_SAME_OPPONENT_DAILY_LIMIT,
            "attacks_today": used,
            "gap": abs(power - mine),
        })
    # An even fight first: the roster is sorted by how near each opponent's power is to
    # yours, with everyone you have already fought out today pushed to the bottom.
    opponents.sort(key=lambda o: (not o["attackable"], o["gap"]))

    # The celebrant goes to the very top and stops being a target for the day: the card
    # in their place offers a greeting, not an attack. Marked on the row rather than
    # pulled out of the list so a client that has not been updated still shows the
    # person, merely without the party.
    celebration = pets.birthday(entry, viewer=me)
    if celebration:
        celebrant = celebration["user_id"]
        for index, row in enumerate(opponents):
            if row["user_id"] == celebrant:
                row["birthday"] = True
                row["attackable"] = False
                opponents.insert(0, opponents.pop(index))
                break
    # `my_debuff` rides along with the roster rather than being read off the state, so
    # the arena can show the player their own mark on the screen where it costs them
    # something, next to the power rating it has already reduced.
    return {
        "me_power": mine, "opponents": opponents, "birthday": celebration,
        "my_debuff": pets.debuff_for(mine_record),
    }


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
            skills=pets.skill_loadout(entry, key),
            shield=pets.combat_shield(entry, key),
        )

    # Зеркало души goes on BEFORE the fighters are built, or it would not be among the
    # effects they carry. See pets.auto_equip_mirror: only fires on a big level gap, only
    # for somebody who owns it, and is put back straight after the fight is recorded.
    mirrored = pets.auto_equip_mirror(entry, me, opponent_id)
    attacker, defender = fighter(me, mine), fighter(opponent_id, theirs)
    seed = secrets.randbits(63)
    result = pets_combat.simulate(attacker, defender, seed=seed)
    # The seed plus both fighters as they stood is the entire fight -- simulate() reads
    # nothing else. Recorded so /api/replay can play this one back later; without it a
    # fight fought from the page would be the one kind nobody could watch again.
    combat_snapshot = {
        "seed": seed,
        "fighters": {
            me: pets_combat.snapshot(attacker),
            opponent_id: pets_combat.snapshot(defender),
        },
    }
    try:
        reward = pets.record_fight(entry, me, opponent_id, result, pets.today(),
                                   attacker_xp=xp, combat_snapshot=combat_snapshot)
    except ValueError as e:
        # The bank emptied, or the pet went to the farm, between drawing the page and
        # pressing the button. Nothing has been recorded -- say so and let the client
        # refresh rather than showing a fight that did not count.
        return _json_error(str(e), status=409, code="CANNOT_FIGHT")

    if mirrored:
        pets.restore_after_mirror(entry, me)
    dropped = C.find_item(reward.get("dropped_item")) if reward.get("dropped_item") else None
    prefix = request.app[_PREFIX_KEY]
    request.app[_LOG_KEY](
        f"[pets_web] fight {me} vs {opponent_id}"
        + (" (зеркало души)" if mirrored else "") + ": "
        f"{'draw' if result.is_draw else ('win' if result.winner == me else 'loss')}, "
        f"{len(result.rounds)} rounds, gold {reward.get('gold') or -reward.get('loss_gold', 0)}, "
        f"xp {reward.get('xp')}, drop {reward.get('dropped_item')}"
    )
    return _ok({
        "ok": True,
        **_playback_payload(result, me, attacker, opponent_id, defender, theirs.get("name")),
        "reward": reward,
        "dropped": _item_payload(dropped, prefix, pets.get_pet(entry, me)) if dropped else None,
        "state": _state_payload(entry, me, xp, prefix),
    })


def _playback_payload(result, me: str, mine, opponent_id: str, theirs, opponent_name) -> dict:
    """The part of a fight the page animates, blow by blow.

    Shared by the live fight and by /api/replay, so a replay is not a second, subtly
    different rendering of the same thing -- it is the same payload, and the client
    cannot tell them apart except by what it is told.

    `max_hp` is per side. The bars used to divide both fighters' HP by the READER's
    maximum, which is only right when the two pets happen to be equally tough: against a
    frailer opponent their bar started full and then fell off a cliff, and against a
    tougher one it never emptied.
    """
    return {
        "you": me,
        # The name as it was WHEN THE FIGHT HAPPENED, straight off the fighter the
        # transcript was written from. The client used to read this off the live pet,
        # which is the same string right up until somebody renames -- and then every
        # round of every replay talks about a creature whose name is nowhere on screen,
        # and none of the highlighting below can find it.
        "you_name": getattr(mine, "name", "") or "",
        "opponent": {"user_id": opponent_id, "name": opponent_name},
        "winner": result.winner,
        "draw": result.is_draw,
        "stopped_early": result.stopped_early,
        "opening": result.opening,
        "closing": result.closing,
        "max_hp": {
            me: round(pets_combat.derive(mine, theirs)["max_hp"]),
            opponent_id: round(pets_combat.derive(theirs, mine)["max_hp"]),
        },
        "rounds": [
            {"number": r.number, "attacker": r.attacker, "event": r.event, "damage": r.damage,
             "attacker_hp": r.attacker_hp, "defender_hp": r.defender_hp, "text": r.text}
            for r in result.rounds
        ],
    }


async def handle_replay(request: web.Request) -> web.Response:
    """Play a recorded fight again, exactly as it happened.

    Nothing about the fight is re-rolled and nothing is re-decided: the stored seed and
    the two stored fighters go back into the same pure simulate() that produced the
    original, and it returns the identical transcript. The money shown is what was
    actually paid at the time, read off the recorded row from this reader's side -- never
    recomputed, because prices change.

    Two fights cannot be replayed and say so plainly rather than inventing something. One
    recorded before this shipped has no snapshot to replay from. One whose re-simulation
    disagrees with the recorded winner was fought under rules that have since changed --
    showing that transcript would be showing a fight that never took place.
    """
    user, _xp = await _player(request)
    entry = request.app[_ENTRY_KEY]
    log = request.app[_LOG_KEY]
    me = str(user["id"])
    fight = pets.find_fight(entry, me, request.query.get("id"))
    if fight is None:
        return _json_error("Этот бой не найден.", status=404, code="NO_FIGHT")

    snapshot = fight.get("combat_snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    fighters = snapshot.get("fighters") if isinstance(snapshot.get("fighters"), dict) else {}
    seed = snapshot.get("seed")
    attacker_id, defender_id = str(fight.get("attacker_id")), str(fight.get("defender_id"))
    attacker = pets_combat.restore(fighters.get(attacker_id))
    defender = pets_combat.restore(fighters.get(defender_id))
    if attacker is None or defender is None or not isinstance(seed, int):
        log(f"[pets_web] replay {me} {fight.get('ts')}: no snapshot to replay from")
        return _json_error(
            "Этот бой прошёл до того, как появились повторы.", status=409, code="NO_REPLAY",
        )

    # Attacker first, exactly as the original call ordered them: simulate() picks the
    # first mover with the rng's very first roll, and swapping the arguments would swap
    # who wins the initiative and replay a different fight from the same seed.
    result = pets_combat.simulate(attacker, defender, seed=seed)
    if str(result.winner or "") != str(fight.get("winner_id") or ""):
        log(
            f"[pets_web] replay {me} {fight.get('ts')}: drifted -- recorded "
            f"{fight.get('winner_id')!r}, replays as {result.winner!r}"
        )
        return _json_error(
            "Бой шёл по прежним правилам — точный повтор уже не воспроизвести.",
            status=409, code="RULES_CHANGED",
        )

    attacked = attacker_id == me
    opponent_id = defender_id if attacked else attacker_id
    opponent_name = fight.get("defender_name") if attacked else fight.get("attacker_name")
    won = str(fight.get("winner_id") or "") == me
    # Money as recorded, from this reader's side -- the same rewrite pets.history does.
    # No xp or level-ups: a fight row has never stored them per side, and a plausible
    # number invented here would be indistinguishable from a real one.
    dropped = C.find_item(fight.get("dropped_item")) if won and fight.get("dropped_item") else None
    prefix = request.app[_PREFIX_KEY]
    return _ok({
        "ok": True,
        "replay": True,
        "at": fight.get("ts"),
        **_playback_payload(
            result, me,
            attacker if attacked else defender, opponent_id,
            defender if attacked else attacker, opponent_name,
        ),
        "reward": {
            "gold": fight.get("gold", 0) if won else 0,
            "loss_gold": 0 if won else fight.get("loss_gold", 0),
            "consolation_gold": 0 if won else fight.get("consolation_gold", 0),
            "auto_equipped": bool(fight.get("auto_equipped")) if won else False,
        },
        "dropped": _item_payload(dropped, prefix, pets.get_pet(entry, me)) if dropped else None,
    })


async def handle_mob(request: web.Request) -> web.Response:
    """Deal one mob, scaled against this player. Nothing is stored -- see pets.roll_mob."""
    user, _xp = await _player(request)
    entry = request.app[_ENTRY_KEY]
    block = await asyncio.to_thread(pets.roll_mob, entry, str(user["id"]))
    if block is None:
        return _json_error("Сначала приручи существо.", status=409, code="NO_PET")
    return _ok({"mob": _jsonable(block), "pve": _jsonable(pets.pve_allowance(entry, str(user["id"])))})


async def handle_mob_attack(request: web.Request) -> web.Response:
    """Fight a mob. Same simulator and same playback as a duel, but its OWN bank.

    Nothing here reads the arena allowance: a mob fight spends `pve_used` and leaves the
    duel bank alone (pets.record_mob_fight). A player out of arena fights can still farm
    mobs, and any screen that greys these buttons out because the arena is empty is wrong.

    The mob block comes back from the client, but nothing in it is trusted: `roll_mob`
    is called again server-side to rebuild the stats. Otherwise the block would be an
    open invitation to post a mob with one hit point and full rewards.
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
    mine = pets.get_pet(entry, me)
    if mine is None:
        return _json_error("Сначала приручи существо.", status=409, code="NO_PET")

    mob = pets_mobs.find_mob(str(body.get("code") or ""))
    tier = str(body.get("tier") or "")
    if mob is None or tier not in pets_mobs.TIERS:
        return _json_error("Соперник больше не доступен.", status=409, code="NO_MOB")
    # Re-rolled here, from the tier the client asked for and nothing else it sent.
    block = pets.mob_block(entry, me, mob.code, tier)
    if block is None:
        return _json_error("Сначала приручи существо.", status=409, code="NO_PET")

    effective = pets.effective_stats(entry, me)
    hero = pets_combat.Fighter(
        key=me, name=mine.get("name") or "Существо",
        strength=effective["strength"], health=effective["health"],
        agility=effective["agility"], luck=effective["luck"],
        armor=effective.get("armor", 0),
        effects=pets.equipped_combat_effects(entry, me),
        level=int(mine.get("level", 1)),
        skills=pets.skill_loadout(entry, me),
        shield=pets.combat_shield(entry, me),
    )
    enemy = pets.mob_fighter(block)
    seed = secrets.randbits(63)
    result = pets_combat.simulate(hero, enemy, seed=seed)
    try:
        reward = await asyncio.to_thread(pets.record_mob_fight, entry, me, block, result)
    except ValueError as e:
        return _json_error(str(e), status=409, code="CANNOT_FIGHT")

    dropped = C.find_item(reward.get("dropped_item")) if reward.get("dropped_item") else None
    prefix = request.app[_PREFIX_KEY]
    request.app[_LOG_KEY](
        f"[pets_web] mob {me} vs {mob.code} ({tier}): "
        f"{'win' if reward['won'] else 'loss'}, {len(result.rounds)} rounds, "
        f"gold {reward['gold']}, xp {reward['xp']}, rubies {reward['rubies']}, "
        f"drop {reward.get('dropped_item')}"
    )
    return _ok({
        "ok": True,
        **_playback_payload(result, me, hero, enemy.key, enemy, block["name"]),
        "mob": _jsonable(block),
        "reward": _jsonable(reward),
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
             "power": row.get("power", 0), "debuff": row.get("debuff"),
             "portrait": _portrait_url(request.app[_PREFIX_KEY], row["user_id"])}
            for index, row in enumerate(rows, start=1)
        ],
    })


async def handle_loadout(request: web.Request) -> web.Response:
    """What one creature is wearing right now, for the leaderboard's peek panel.

    Read-only and about somebody else, so the ownership flags `_item_payload` normally
    derives from the viewer are derived from the OWNER instead: on this panel "equipped"
    means "they are wearing it", which is the only question the panel answers. Nothing
    here is buyable or sellable, so no viewer-relative flag would mean anything.
    """
    await _player(request)
    entry = request.app[_ENTRY_KEY]
    prefix = request.app[_PREFIX_KEY]
    who = str(request.query.get("user_id") or "").strip()
    if not who:
        return _json_error("Не указан игрок.", status=400, code="NO_USER")

    record = pets.get_pet(entry, who)
    if record is None:
        return _json_error("У этого игрока нет существа.", status=404, code="NO_PET")

    slots = await asyncio.to_thread(_equipment_payload, record, prefix)
    effective = await asyncio.to_thread(pets.effective_stats, entry, who)
    return _ok({
        "user_id": who,
        "name": record.get("name"),
        "owner_name": record.get("owner_name"),
        "level": int(record.get("level", 1)),
        "fights": int(record.get("fights", 0)),
        "wins": int(record.get("wins", 0)),
        "power": pets._power_rating_for(record),
        "stats": effective,
        # The stats above are already scaled down by any mark, so the panel has to say
        # why -- otherwise it silently misreports somebody's creature as weaker.
        "debuff": pets.debuff_for(record),
        "slots": slots,
        # The scrolls are half of what a build is, so a panel that showed only the five
        # equipment slots would be describing half a creature.
        "skills": [
            {"slot": index, "code": code,
             **({"empty": True} if not code else {
                 "empty": False,
                 "name": str(pets_scroll_catalog.scroll(code)["name"]).split(": ", 1)[-1],
                 "icon": pets_scroll_catalog.scroll(code)["icon"],
                 "effects_text": list(pets_scroll_catalog.effect_lines(
                     pets_scroll_catalog.scroll(code))),
             })}
            for index, code in enumerate(pets._skill_loadout_for(record), start=1)
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
            "at": fight.get("ts"),
            # The timestamp is also the replay key (see pets.find_fight). `replayable`
            # spares the client a request that can only fail: a fight recorded before
            # snapshots existed has nothing to replay from, and a row that cannot be
            # watched should not look like a button.
            "id": pets.fight_id(fight),
            "replayable": bool(fight.get("combat_snapshot")),
        })
    return _ok({"rows": rows})


# --------------------------------------------------------------------------- quests


async def _quest_admin(request: web.Request, body: dict | None = None):
    """(user, xp) for a quest moderator, or an HTTPException. Fails closed.

    Moderation is the same "chat admin or delegate" rule every other management surface
    in this bot uses (_can_manage_chat, injected as is_admin) -- there is no separate
    quest-reviewer list to keep in sync, and /badgeadmin already delegates it at runtime.
    """
    user, xp = await _player(request, body)
    if not await request.app[_IS_ADMIN_KEY](user):
        raise web.HTTPForbidden(
            text=json.dumps({"error": "NOT_AN_ADMIN", "message": "Только для модераторов."}),
            content_type="application/json",
        )
    return user, xp


async def handle_congratulate(request: web.Request) -> web.Response:
    """Wish today's celebrant a happy birthday. Pays both, once per well-wisher.

    A route of its own rather than an entry in _ACTIONS because it has to await the DM
    to the celebrant, and _ACTIONS is a table of synchronous store edits. The payment is
    committed BEFORE the message is attempted: a celebrant whose DM is closed must still
    be paid, and `pets.congratulate` has already stored its own copy of the news.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        body = {}
    user, _xp = await _player(request, body)
    entry = request.app[_ENTRY_KEY]
    if not await request.app[_IS_MEMBER_KEY](user):
        return _json_error("Поздравлять могут только участники чата.", status=403, code="NOT_A_MEMBER")

    me = str(user["id"])
    try:
        receipt = await asyncio.to_thread(pets.congratulate, entry, me)
    except ValueError as e:
        return _json_error(str(e), status=409, code="CANNOT_CONGRATULATE")

    if not receipt.get("already"):
        # Failure here is logged and swallowed: the money is already banked, and a bot
        # cannot write to somebody who has never opened its DM. Losing the notification
        # must not turn a successful greeting into an error the greeter has to retry.
        try:
            await request.app[_BIRTHDAY_NOTIFY_KEY](
                receipt["celebrant"], receipt.get("greeter_name") or "кто-то",
                int(receipt.get("celebrant_gold") or 0), int(receipt.get("celebrant_xp") or 0),
            )
        except Exception as e:  # noqa: BLE001 -- see above
            request.app[_LOG_KEY](f"[pets_web] birthday DM to {receipt['celebrant']} failed: {e}")

    request.app[_LOG_KEY](
        f"[pets_web] {me} congratulated {receipt.get('celebrant')}"
        f"{' (repeat)' if receipt.get('already') else ''}"
    )
    return _ok({
        "receipt": _jsonable(receipt),
        "state": await asyncio.to_thread(_state_payload, entry, user["id"], _xp,
                                         request.app[_PREFIX_KEY]),
    })


async def handle_birthday_admin(request: web.Request) -> web.Response:
    """Who is celebrating, and everybody who could be. Chat admins only."""
    await _economy_admin(request)
    entry = request.app[_ENTRY_KEY]
    return _ok(await asyncio.to_thread(_birthday_admin_payload, entry))


def _birthday_admin_payload(entry: str) -> dict:
    data = pets._load(entry)
    candidates = [
        {
            "user_id": str(user_id),
            "owner_name": record.get("owner_name") or "кто-то",
            "owner_username": record.get("owner_username"),
            "pet_name": record.get("name"),
        }
        for user_id, record in (data.get("pets") or {}).items()
        if isinstance(record, dict) and record.get("name")
    ]
    candidates.sort(key=lambda row: str(row["owner_name"]).lower())
    return {"birthday": pets.birthday(entry), "candidates": candidates, "today": pets.today().isoformat()}


async def handle_birthday_set(request: web.Request) -> web.Response:
    """Set or clear the celebration. Chat admins only."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_error("malformed request body")
    user, _xp = await _economy_admin(request, body)
    entry = request.app[_ENTRY_KEY]

    if body.get("clear"):
        cleared = await asyncio.to_thread(pets.clear_birthday, entry)
        request.app[_LOG_KEY](f"[pets_web] {user['id']} cleared the birthday ({cleared})")
        return _ok({
            "message": "Праздник снят." if cleared else "Никто и не праздновал.",
            **await asyncio.to_thread(_birthday_admin_payload, entry),
        })

    try:
        await asyncio.to_thread(
            pets.set_birthday, entry, str(body.get("user_id") or ""), set_by=user["id"],
        )
    except ValueError as e:
        return _json_error(str(e), status=400, code="BAD_BIRTHDAY")
    request.app[_LOG_KEY](f"[pets_web] {user['id']} set the birthday to {body.get('user_id')}")
    payload = await asyncio.to_thread(_birthday_admin_payload, entry)
    return _ok({"message": "Праздник назначен на сегодня.", **payload})


async def _portrait_bytes(request: web.Request, record: dict) -> bytes | None:
    """The creature's photo as bytes, off the same disk cache the portrait route fills.

    Reuses that cache rather than downloading again: by the time anybody opens a battle
    the picture has almost always been rendered at least once, so this usually costs one
    file read. The download path is kept as a fallback for the case where it has not.
    """
    file_id = record.get("photo_file_id")
    if not file_id:
        return None
    cached = portrait_cache_path(file_id)
    if cached.is_file():
        try:
            return await asyncio.to_thread(cached.read_bytes)
        except OSError:
            return None
    try:
        data = await request.app[_FETCH_PHOTO_KEY](file_id)
    except Exception:
        request.app[_LOG_KEY](f"[pets_web] sprite fetch raised:\n{traceback.format_exc()}")
        return None
    if data:
        try:
            await asyncio.to_thread(_write_portrait, cached, data)
        except Exception:
            pass                      # the classification does not need it on disk
    return data


async def handle_sprite(request: web.Request) -> web.Response:
    """Everything the battle screen needs to animate one player's creature.

    Answers with three things: which of the twelve archetypes the photograph shows, the
    URLs of any generated frames that are ready, and whether generation is still running.

    It never waits for a model. Working out what a picture shows is one round trip and
    generating frames is four, so a battle screen that blocked on either would feel
    broken. The screen opens on whatever exists -- at worst the raw photograph with the
    neutral idle, which is exactly what it did before this feature -- and improves itself
    on a later look. That is why every failure path here is a usable answer with HTTP 200
    rather than an error the client would have to render.
    """
    await _player(request)
    entry = request.app[_ENTRY_KEY]
    prefix = request.app[_PREFIX_KEY]
    cfg = request.app[_CFG_KEY]
    who = str(request.query.get("user_id") or "").strip()
    record = pets.get_pet(entry, who) if who else None
    if record is None:
        return _ok({"user_id": who, "kind": pets_sprite.DEFAULT_ARCHETYPE,
                    "frames": [], "status": "none"})

    file_id = str(record.get("photo_file_id") or "")
    manifest = await asyncio.to_thread(pets_sprite_store.read_manifest, file_id) if file_id else {}
    if manifest.get("frames"):
        # Frames on disk are the finished article; nothing else needs asking.
        return _ok({
            "user_id": who,
            "kind": str(manifest.get("archetype") or pets_sprite.DEFAULT_ARCHETYPE),
            "frames": [
                {"name": name, "url": f"{prefix}/img/sprite/{who}/{name}.png"}
                for name in manifest["frames"]
            ],
            "status": "ready",
        })

    kind = pets_sprite.cached_archetype(record) or pets_sprite.DEFAULT_ARCHETYPE
    gemini_key = getattr(cfg, "gemini_api_key", "") or ""
    status = "none"

    if file_id and gemini_key and pets_sprite_store.claim(file_id):
        # Fire and forget, one job per photograph per process. The task holds a reference
        # on the app so a garbage collector cannot cancel it halfway through, which is the
        # documented way to lose a bare asyncio.create_task.
        async def build():
            image = await _portrait_bytes(request, record)
            if not image:
                return
            await asyncio.to_thread(
                pets_sprite_store.generate, image, file_id,
                api_key=gemini_key,
                vision_model=getattr(cfg, "gemini_vision_model", "gemini-2.5-flash"),
                image_model=getattr(cfg, "gemini_image_model", "gemini-2.5-flash-image"),
                log=request.app[_LOG_KEY],
            )

        task = asyncio.create_task(_guarded(build(), request.app[_LOG_KEY], f"sprite {who}"))
        request.app[_SPRITE_JOBS_KEY].add(task)
        task.add_done_callback(request.app[_SPRITE_JOBS_KEY].discard)
        status = "pending"
    elif file_id and gemini_key:
        status = "pending"                # somebody else's job is already on it

    # No Gemini configured: fall back to the archetype alone, which still picks a matching
    # CSS idle for the raw photograph. Classified with OpenAI, which every deployment has.
    if kind == pets_sprite.DEFAULT_ARCHETYPE and not gemini_key:
        openai_key = getattr(cfg, "openai_api_key", "")
        image = await _portrait_bytes(request, record) if openai_key else None
        if image:
            kind = await asyncio.to_thread(
                pets_sprite.classify, image,
                api_key=openai_key, model=getattr(cfg, "openai_model", "") or "gpt-4o-mini",
            )
            if kind != pets_sprite.DEFAULT_ARCHETYPE:
                await asyncio.to_thread(pets.remember_sprite, entry, who, kind)

    return _ok({"user_id": who, "kind": kind, "frames": [], "status": status})


async def _guarded(coroutine, log, label: str) -> None:
    """Run a background job and log what it did instead of letting it vanish.

    A bare create_task that raises reports nothing until the loop shuts down, and this one
    is a paid model call: silence is the difference between a feature that is off and one
    that is broken.
    """
    try:
        await coroutine
    except Exception:
        log(f"[pets_web] {label} background job failed:")
        log(traceback.format_exc())


async def handle_sprite_frame(request: web.Request) -> web.Response:
    """One generated frame, by owner id. Unauthenticated for the same reason the portrait
    route is: an <img> cannot carry the initData header, and this is derived from a photo
    that was posted to the chat anyway.

    Served immutable and long-lived, which the portrait route could not do: that URL is
    keyed on the OWNER, so it has to stay fresh across a photo change, whereas a frame's
    content is fixed by the photograph it came from and a new photograph produces a new
    set at a new time. A stale frame is still corrected by the manifest, not by the cache.
    """
    raw = request.match_info["user_id"]
    frame = request.match_info["frame"]
    if not _SAFE_CODE.match(raw or "") or frame not in pets_gemini.FRAMES:
        raise web.HTTPNotFound()
    record = pets.get_pet(request.app[_ENTRY_KEY], raw) or {}
    file_id = str(record.get("photo_file_id") or "")
    path = pets_sprite_store.frame_path(file_id, frame) if file_id else None
    if path is None or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=604800"})


async def handle_debuff_admin(request: web.Request) -> web.Response:
    """Who is marked, what marks exist, and everybody who could be given one."""
    await _economy_admin(request)
    entry = request.app[_ENTRY_KEY]
    return _ok(await asyncio.to_thread(_debuff_admin_payload, entry))


def _debuff_admin_payload(entry: str) -> dict:
    data = pets._load(entry)
    candidates = [
        {
            "user_id": str(user_id),
            "owner_name": record.get("owner_name") or "кто-то",
            "owner_username": record.get("owner_username"),
            "pet_name": record.get("name"),
            "has_photo": bool(record.get("photo_file_id")),
        }
        for user_id, record in (data.get("pets") or {}).items()
        if isinstance(record, dict) and record.get("name")
    ]
    candidates.sort(key=lambda row: str(row["owner_name"]).lower())
    # The catalogue travels to the client so the admin picks a mark by its real copy --
    # the same title, joke and get-out line the player will read -- rather than by a code.
    return {
        "debuffs": [
            {"code": code, **{key: spec[key] for key in
                              ("emoji", "title", "line", "description", "hint")}}
            for code, spec in C.DEBUFFS.items()
        ],
        "holders": pets.debuff_holders(entry),
        "candidates": candidates,
    }


async def handle_debuff_set(request: web.Request) -> web.Response:
    """Give or take away one player's mark. Chat admins only."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_error("malformed request body")
    user, _xp = await _economy_admin(request, body)
    entry = request.app[_ENTRY_KEY]
    target = str(body.get("user_id") or "")

    if body.get("clear"):
        cleared = await asyncio.to_thread(pets.clear_debuff, entry, target)
        request.app[_LOG_KEY](f"[pets_web] {user['id']} cleared the debuff on {target} ({cleared})")
        return _ok({
            "message": "Эффект снят." if cleared else "На этом игроке ничего не было.",
            **await asyncio.to_thread(_debuff_admin_payload, entry),
        })

    try:
        mark = await asyncio.to_thread(
            pets.set_debuff, entry, target, str(body.get("code") or ""), set_by=user["id"],
        )
    except ValueError as e:
        return _json_error(str(e), status=400, code="BAD_DEBUFF")
    request.app[_LOG_KEY](
        f"[pets_web] {user['id']} gave {body.get('code')} to {target}"
    )
    return _ok({
        "message": f"{mark.get('emoji', '')} {mark.get('title', 'Эффект')} выдан.".strip(),
        **await asyncio.to_thread(_debuff_admin_payload, entry),
    })


async def _economy_admin(request: web.Request, body: dict | None = None):
    # `body` for the POST callers: a GET carries initData in the header, but a mutation
    # sends it in the JSON like every other action, and reading only the header would
    # turn an authorised admin into an anonymous caller.
    user, xp = await _player(request, body)
    if not await request.app[_IS_ECONOMY_ADMIN_KEY](user):
        raise web.HTTPForbidden(
            text=json.dumps({
                "error": "NOT_AN_ECONOMY_ADMIN",
                "message": "Денежный аудит доступен только администраторам чата.",
            }, ensure_ascii=False),
            content_type="application/json",
        )
    return user, xp


def _economy_audit_users(entry: str) -> tuple[list[dict], dict[str, stats.UserStats]]:
    """Union of tracked chat users, pet owners and ledger-only accounts."""
    tracked = stats.aggregate_all_time(entry)
    pet_rows = {str(row["user_id"]): row for row in pets.pet_leaderboard(entry)}
    ids = set(tracked) | set(pet_rows) | economy.audit_user_ids(entry)
    rows = []
    for user_id in ids:
        stat = tracked.get(user_id)
        pet = pet_rows.get(user_id) or {}
        display_name = (
            (stat.display_name if stat else None)
            or pet.get("owner_name") or f"ID {user_id}"
        )
        username = (stat.username if stat else None) or pet.get("owner_username")
        rows.append({
            "user_id": user_id,
            "name": display_name,
            "username": username,
            "pet_name": pet.get("name"),
        })
    rows.sort(key=lambda row: (str(row["name"]).casefold(), row["user_id"]))
    return rows, tracked


async def handle_economy_audit(request: web.Request) -> web.Response:
    """Admin-only hourly source graph over the real coin audit trail."""
    caller, _xp = await _economy_admin(request)
    entry = request.app[_ENTRY_KEY]
    users, tracked = _economy_audit_users(entry)
    requested = str(request.query.get("user_id") or "")
    known = {row["user_id"] for row in users}
    selected = requested if requested in known else (
        str(caller["id"]) if str(caller["id"]) in known else (users[0]["user_id"] if users else "")
    )
    try:
        window = int(request.query.get("hours", 24))
    except (TypeError, ValueError):
        window = 24
    report = economy.audit_report(entry, selected, window) if selected else None
    selected_stats = tracked.get(selected)
    if report is not None:
        baseline = stats._load_words_per_point(entry) or stats.DEFAULT_WORDS_PER_POINT
        report["xp_coins_archived"] = (
            stats.coins_for_xp(selected_stats.xp(baseline)) if selected_stats else None
        )
        report["xp_coins_note"] = (
            "Монеты из активности чата считаются из XP и не создают почасовых операций. "
            "Показана оценка по сохранённым дням; сегодняшний незакрытый день может в неё не входить."
        )
    return _ok({
        "users": users,
        "selected": selected,
        "windows": list(economy.AUDIT_WINDOW_HOURS),
        "report": report,
    })


def _submission_link(row: dict) -> str | None:
    """A t.me link to the post itself.

    The photo is not re-hosted: it lives in the chat, the moderator is a member of that
    chat, and one tap there gives them the full-size image, the whole album and a reply
    box -- all of which a thumbnail rehosted here would take away. It also keeps a
    Telethon media download out of a request that a moderator is waiting on.
    """
    return stats.figurine_message_link(None, row.get("chat_id"), row.get("message_id"))


async def handle_quests(request: web.Request) -> web.Response:
    """The player's own quest board: what they were given, and what it pays."""
    user, _xp = await _player(request)
    entry = request.app[_ENTRY_KEY]
    me = str(user["id"])
    # Four store reads. Off the loop together rather than one of them at a time: this
    # process also serves the ballot and answers the bot, and a blocking JSON read is a
    # blocking JSON read whether or not it is the slowest one in the handler.
    payload = await asyncio.to_thread(_quest_board_payload, entry, me)
    payload["is_admin"] = await request.app[_IS_ADMIN_KEY](user)
    return _ok(payload)


def _quest_board_payload(entry: str, me: str) -> dict:
    return _jsonable({
        **quests.daily_quest(entry, me),
        "rerolls_total": quests.REROLLS_PER_QUEST,
        # The second slot. Dealt exactly like the painting challenge, and rendered by
        # the same card -- the two differ in what they ask for, not in how they work.
        "real": quests.real_quest(entry, me),
        "rune": quests.rune_quest(entry, me),
        "stats": quests.stats_for(entry, me),
        "history": quests.history(entry, me, limit=20),
    })





async def handle_quest_reroll(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        body = {}
    user, _xp = await _player(request, body)
    entry = request.app[_ENTRY_KEY]
    me = str(user["id"])
    kind = str(body.get("kind") or "paint")
    kind = kind if kind in {"paint", "real", "rune"} else "paint"
    ok, message = quests.reroll(
        entry, me, kind=kind, code=str(body.get("code") or "") or None,
    )
    request.app[_LOG_KEY](
        f"[pets_web] quest reroll {me} ({kind}): {'ok' if ok else 'refused'} -- {message}"
    )
    return _ok({"ok": ok, "message": message,
                "board": await asyncio.to_thread(_quest_board_payload, entry, me)})


async def handle_quest_idea(request: web.Request) -> web.Response:
    """Accept a free-text quest idea from any current chat member."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_error("malformed request body")
    user, _xp = await _player(request, body)
    if not await request.app[_IS_MEMBER_KEY](user):
        return _json_error("Играть могут только участники чата.", status=403, code="NOT_A_MEMBER")
    ok, message = await asyncio.to_thread(
        quests.suggest_idea, request.app[_ENTRY_KEY], user["id"], body.get("text") or "",
        author_name=user.get("first_name") or user.get("username") or "",
        author_username=user.get("username") or "",
    )
    return _ok({"ok": ok, "message": message})


async def handle_quest_review_queue(request: web.Request) -> web.Response:
    """Everything waiting on a moderator, plus the knobs they are allowed to turn."""
    user, _xp = await _quest_admin(request)
    entry = request.app[_ENTRY_KEY]
    rows = []
    for row in quests.pending(entry):
        rows.append({
            "id": row["id"], "user_id": row["user_id"],
            "author": row.get("author_name") or row.get("author_username") or row["user_id"],
            "username": row.get("author_username") or "",
            "code": row["code"], "title": row["title"], "subject": row["subject"],
            "technique": row["technique"], "hint": row["hint"], "proof": row["proof"],
            "difficulty": row["difficulty"], "reward": row["reward"],
            "at": row.get("ts"), "link": _submission_link(row),
            "portrait": _portrait_url(request.app[_PREFIX_KEY], row["user_id"]),
        })
    return _ok({
        "rows": _jsonable(rows),
        "rewards": quests.reward_table(entry),
        "catalog": quests.catalog_entries(entry),
        "recent": _jsonable(quests.submissions(entry, limit=40)),
        "ideas": _jsonable(quests.ideas(entry)),
    })


async def handle_quest_review(request: web.Request) -> web.Response:
    """Accept or reject one submission. The only place a quest ever pays."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_error("malformed request body")
    user, _xp = await _quest_admin(request, body)
    entry = request.app[_ENTRY_KEY]
    accept = bool(body.get("accept"))
    # Keep the recipient before review changes the queue. The notification itself is
    # deliberately best-effort: Telegram may forbid a DM until the player starts the bot,
    # but that must never undo a moderator's verdict.
    queued = next((row for row in quests.pending(entry)
                   if str(row.get("id")) == str(body.get("id") or "")), None)
    ok, message, receipt = await asyncio.to_thread(
        quests.review, entry, str(body.get("id") or ""), str(user["id"]), accept,
        reviewer_name=user.get("first_name") or user.get("username") or "",
        note=str(body.get("note") or ""),
    )
    request.app[_LOG_KEY](
        f"[pets_web] quest review by {user['id']}: {body.get('id')} -> "
        f"{'accepted' if accept else 'rejected'} ({'ok' if ok else message})"
        + (f", paid {receipt.get('gold')} gold / {receipt.get('xp')} xp"
           f" / {receipt.get('tickets')} ticket(s), drop {receipt.get('item')}" if receipt else "")
    )
    if ok and queued is not None:
        try:
            if accept:
                queued["paid"] = dict(receipt)
                await request.app[_QUEST_COMPLETION_KEY](queued)
            else:
                await request.app[_QUEST_FEEDBACK_KEY](
                    queued["user_id"], queued.get("title") or queued.get("code"),
                    str(body.get("note") or "").strip(),
                )
        except Exception:
            request.app[_LOG_KEY](
                "[pets_web] failed to send quest verdict DM:\n" + traceback.format_exc()
            )
    return _ok({"ok": ok, "message": message, "receipt": _jsonable(receipt)})


async def handle_quest_config(request: web.Request) -> web.Response:
    """Edit the reward table, or take a quest out of rotation."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_error("malformed request body")
    user, _xp = await _quest_admin(request, body)
    entry = request.app[_ENTRY_KEY]
    if isinstance(body.get("text"), dict):
        ok, message = quests.set_quest_text(
            entry, str(body.get("code") or ""), body["text"],
        )
    elif "code" in body:
        ok, message = quests.set_quest_enabled(
            entry, str(body.get("code") or ""), bool(body.get("enabled")),
        )
    else:
        ok, message = quests.set_reward(
            entry, body.get("difficulty"), str(body.get("field") or ""), body.get("value"),
        )
    request.app[_LOG_KEY](
        f"[pets_web] quest config by {user['id']}: {body.get('code') or body.get('field')} -> "
        f"{'ok' if ok else 'refused'} -- {message}"
    )
    return _ok({"ok": ok, "message": message, "rewards": quests.reward_table(entry)})


async def handle_mail(request: web.Request) -> web.Response:
    """The mailbox: fights, farm shifts and gifts, with new events at the bottom.

    Forwarded verbatim from pets.mail -- including the HH.MM it already formatted, and
    the day heading. The page must not re-derive either from the ISO timestamp: the
    browser would use the *device's* timezone, so a player travelling would see times
    that disagree with the ones the bot sent them for the very same events, and a phone
    an hour ahead would file this evening's fights under "Завтра".
    """
    user, _xp = await _player(request)
    entry = request.app[_ENTRY_KEY]
    rows = []
    for event in pets.mail(entry, user["id"], extra=quests.mail_events(entry, user["id"])):
        rows.append({**event, "day_label": mail_day_label(event.get("day") or "")})
    return _ok({"rows": _jsonable(rows)})


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


async def _default_quest_feedback(user_id, title: str, note: str):
    return None


async def _default_quest_completion(row: dict):
    return None


async def _default_birthday_notify(celebrant, greeter_name: str, gold: int, xp: int):
    return None


def attach(
    app: web.Application,
    cfg,
    entry: str,
    is_member=None,
    is_admin=None,
    is_economy_admin=None,
    resolve_player=None,
    fetch_photo=None,
    save_photo=None,
    quest_feedback=None,
    quest_completion=None,
    birthday_notify=None,
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
    # Fails closed: with no gate injected, nobody is a quest moderator and the review
    # tab simply never appears -- rather than appearing for everyone.
    app[_IS_ADMIN_KEY] = is_admin or _default_is_admin
    # Deliberately separate from quest moderators. Production injects the narrower
    # chat-admin gate; omitted means nobody can read financial history.
    app[_IS_ECONOMY_ADMIN_KEY] = is_economy_admin or _default_is_admin
    app[_RESOLVE_KEY] = resolve_player or _default_resolve_player
    app[_FETCH_PHOTO_KEY] = fetch_photo or _default_fetch_photo
    app[_SAVE_PHOTO_KEY] = save_photo or _default_save_photo
    app[_QUEST_FEEDBACK_KEY] = quest_feedback or _default_quest_feedback
    app[_QUEST_COMPLETION_KEY] = quest_completion or _default_quest_completion
    # Declines silently when absent: without a bot to send it, a greeting still pays and
    # still lands in the celebrant's stored notifications.
    app[_BIRTHDAY_NOTIFY_KEY] = birthday_notify or _default_birthday_notify
    app[_PREFIX_KEY] = prefix
    app[_LOG_KEY] = log
    # Ephemeral by design: a restart ends prototypes instead of ever writing a result to
    # the real pet store. Opaque tokens and ownership checks isolate simultaneous users.
    app[_TEST_BATTLE_SESSIONS_KEY] = {}
    # Strong references to in-flight sprite jobs. The event loop only holds a task
    # weakly, so without this the garbage collector is free to cancel a generation
    # halfway through and nothing would ever say why.
    app[_SPRITE_JOBS_KEY] = set()
    app.add_routes([
        web.get(prefix, handle_page),
        web.get(prefix + "/", handle_page),
        web.get(prefix + "/api/state", handle_state),
        web.post(prefix + "/api/action", handle_action),
        web.get(prefix + "/api/opponents", handle_opponents),
        web.post(prefix + "/api/attack", handle_attack),
        web.post(prefix + "/api/congratulate", handle_congratulate),
        # Chat admins only, both of them (see _economy_admin).
        web.get(prefix + "/api/birthday", handle_birthday_admin),
        web.post(prefix + "/api/birthday", handle_birthday_set),
        web.get(prefix + "/api/sprite", handle_sprite),
        web.get(prefix + "/img/sprite/{user_id}/{frame}.png", handle_sprite_frame),
        web.get(prefix + "/api/debuff", handle_debuff_admin),
        web.post(prefix + "/api/debuff", handle_debuff_set),
        web.get(prefix + "/api/test-battle", handle_test_battle_setup),
        web.post(prefix + "/api/test-battle/start", handle_test_battle_start),
        web.post(prefix + "/api/test-battle/action", handle_test_battle_action),
        web.get(prefix + "/api/mob", handle_mob),
        web.post(prefix + "/api/mob", handle_mob_attack),
        web.get(prefix + "/api/shop", handle_shop),
        web.get(prefix + "/api/leaderboard", handle_leaderboard),
        web.get(prefix + "/api/loadout", handle_loadout),
        web.get(prefix + "/api/history", handle_history),
        web.get(prefix + "/api/economy/audit", handle_economy_audit),
        web.get(prefix + "/api/mail", handle_mail),
        web.get(prefix + "/api/replay", handle_replay),
        web.get(prefix + "/api/quests", handle_quests),
        web.post(prefix + "/api/quests/reroll", handle_quest_reroll),
        web.post(prefix + "/api/quests/ideas", handle_quest_idea),
        # Moderator-only, all three (see _quest_admin).
        web.get(prefix + "/api/quests/review", handle_quest_review_queue),
        web.post(prefix + "/api/quests/review", handle_quest_review),
        web.post(prefix + "/api/quests/config", handle_quest_config),
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
  /* Dark on every client, on purpose -- Telegram's themeParams are read and ignored.
     Only six of the colours below could ever have come from the client; the rest of the
     game is drawn for a dark ground and cannot follow it: the gold, the rarity colours,
     the fight-log sides, and the item art, whose tiles have #1a2532 painted into the SVG
     the same way vote_image bakes a letterbox into a photo. Half a palette following an
     Android client into light mode is what put grey hint text on white cards next to
     navy item tiles and made the cabinet unreadable. */
  :root {
    color-scheme: dark;
    --bg: #17212b;
    --fg: #f5f5f5;
    --muted: #8a9aa9;
    --card: #232e3c;
    --accent: #3390ec;
    --accent-fg: #fff;
    --line: rgba(128,128,128,.22);
    --sunken: rgba(0,0,0,.22);
    --gold: #e8b923;
    --hp: #e05260;
    --xp: #4caf72;
    /* The two sides of a fight. Deliberately NOT --xp/--hp: those two mean "your money
     * went up" and "your money went down" everywhere else in the game, and a fight log
     * needs the numbers to keep saying that while the names say something different. */
    --mine: #62aef0;
    --foe: #d98a5a;
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
  /* The mailbox is the one screen you check rather than play, so it gets a fixed corner
     instead of a seventh tab -- six is already the width a phone can label. */
  /* A button centres its label using its own text layout: default padding, a baseline,
     and a line box. That is close enough for a word and visibly off for a single 20px
     emoji in a 42px square. Flex centring with the padding zeroed puts the glyph in the
     middle of the box instead of on a baseline inside it. align-self keeps the square
     level with the portrait rather than stretched to the height of the name column. */
  .hud .post {
    flex: none; align-self: center; width: 42px; height: 42px; padding: 0;
    display: flex; align-items: center; justify-content: center;
    border-radius: 12px; font-size: 20px; line-height: 1;
    border: 1px solid var(--line); background: var(--sunken);
  }
  .bar { height: 5px; border-radius: 3px; background: var(--sunken); overflow: hidden; margin-top: 5px; }
  .bar > i { display: block; height: 100%; background: var(--xp); border-radius: 3px; transition: width .35s; }

  /* --------------------------------------------------------------- the tab bar */
  .tabs {
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 20;
    display: grid; grid-template-columns: repeat(6, 1fr);
    background: var(--card); border-top: 1px solid var(--line);
    padding-bottom: env(safe-area-inset-bottom);
  }
  .tabs.has-review { grid-template-columns: repeat(7, 1fr); }
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
  .dungeon { overflow: hidden; padding: 0; border-color: rgba(232,185,35,.45); }
  .dungeon-head { min-height: 142px; position: relative; padding: 15px; display: flex; align-items: flex-end; background: repeating-linear-gradient(135deg, #1d3c3e 0 18px, #162a34 18px 36px); }
  .dungeon-head.boss { background: repeating-linear-gradient(135deg, #502331 0 18px, #201e31 18px 36px); }
  .dungeon-title { font-family: Georgia, serif; font-size: 24px; font-weight: 700; color: #ffe8a3; text-shadow: 0 2px 0 #121820; }
  .dungeon-title small { display: block; margin-top: 3px; font: 12px/1.3 "Segoe UI", sans-serif; color: #d4e7df; }
  .dungeon-stat { position: absolute; top: 12px; right: 13px; text-align: right; font-size: 12px; color: #fff2c0; }
  .dungeon-body { padding: 12px; }
  .dungeon-enemies { display: grid; gap: 8px; }
  .dungeon-enemy { text-align: left; display: grid; grid-template-columns: 48px 1fr auto; align-items: center; gap: 9px; border: 1px solid rgba(255,255,255,.12); border-radius: 8px; background: rgba(0,0,0,.16); padding: 7px; color: var(--fg); }
  .dungeon-enemy.done { opacity: .48; filter: saturate(.25); }
  .dungeon-art { width: 48px; height: 48px; border-radius: 7px; overflow: hidden; background: #10171c; }
  .dungeon-art svg { width: 100%; height: 100%; display: block; }
  .dungeon-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 11px; }
  .dungeon-actions .go { padding: 9px 8px; font-size: 13px; }
  .chip {
    border: 1px solid var(--line); background: transparent; border-radius: 999px;
    padding: 6px 12px; font-size: 13px; white-space: nowrap;
  }
  .chip.on { background: var(--accent); color: var(--accent-fg); border-color: var(--accent); }
  .chiprow { display: flex; gap: 7px; overflow-x: auto; padding-bottom: 2px; scrollbar-width: none; }
  .chiprow::-webkit-scrollbar { display: none; }

  /* ------------------------------------------------------------- the paperdoll
     Five slots around the portrait, in the arrangement they are worn. A plain list would
     say the same thing and
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
  /* display:block is load-bearing, not tidiness. `shot()` emits a <span>, and width and
     height do not apply to a non-replaced INLINE box -- so an un-blockified .shot is a
     zero-sized containing block, and the absolutely positioned photo inside it resolves
     width:100% to nothing. It looked like it worked because the first two callers put it
     in a flex container (.hud .face, .doll .portrait), which blockifies its items for
     free. The opponent roster and the ranking put it in a plain block instead -- so those
     two, and only those two, rendered every portrait at zero pixels. */
  .shot { display: block; position: relative; width: 100%; height: 100%; overflow: hidden; }
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
  .pet-equipment-summary { grid-column: 1 / -1; text-align: center; }

  .live-skills { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .live-skill { text-align: left; min-height: 76px; padding: 9px; }
  .live-skill b, .live-skill small { display: block; }
  .live-skill small { margin-top: 4px; color: var(--muted); font-weight: 400; }
  .live-skill.ultimate { border-color: var(--r-legendary); }
  /* The one card on the arena that is not a fight. Gold border rather than the accent,
     so it reads as an occasion and not as another button to press. */
  .panel.birthday { border: 1px solid var(--gold); }
  .panel.birthday h2 { color: var(--gold); }
  /* A granted mark. Deliberately the quietest thing on any screen it lands on: no accent
     colour, no border on the inline form, and the same muted grey the secondary lines
     already use. It has to be findable, not loud -- see debuffTag/debuffNote. */
  .dbf {
    display: inline-block; font-size: 11px; font-weight: 600; letter-spacing: .01em;
    padding: 1px 6px; border-radius: 999px; white-space: nowrap; vertical-align: middle;
    color: var(--muted); background: rgba(128, 128, 128, .16);
  }
  .dbfnote {
    margin-top: 9px; padding: 8px 10px; border-radius: 10px;
    background: rgba(128, 128, 128, .12); border: 1px solid rgba(128, 128, 128, .22);
  }
  .dbfhead { font-size: 13px; margin-bottom: 3px; }
  .dbfcost { color: var(--muted); font-size: 12px; font-weight: 600; }
  /* An open slot, drawn like one: dashed, quiet, and obviously something you can fill,
     rather than a card that looks broken because its contents failed to load. */
  .live-skill.empty { border-style: dashed; color: var(--muted); background: none; }
  .live-skill.empty b { font-weight: 600; }
  /* What the scroll actually does, in numbers. Brighter than the flavour line under it
     on purpose: comparing two scrolls means comparing these, and the description above
     them is atmosphere. */
  .fx { margin-top: 5px; display: grid; gap: 2px; font-size: 11px; color: var(--fg); }
  .fx > span { display: block; }
  .fx > span::before { content: "▸ "; color: var(--accent); }

  /* Financial audit: a horizontal hour strip keeps a 24h/7d timeline readable on a
     phone. Each bar is stacked by income source; expenses stay in the exact table below
     so a returned casino stake can never masquerade as profit. */
  .audit-graph { display:flex; align-items:flex-end; gap:4px; min-height:170px;
                 overflow-x:auto; padding:12px 2px 5px; }
  .audit-hour { flex:1 0 20px; min-width:20px; height:145px; display:flex;
                flex-direction:column; justify-content:flex-end; align-items:stretch; }
  .audit-stack { height:112px; display:flex; flex-direction:column-reverse;
                 justify-content:flex-start; border-bottom:1px solid var(--line); }
  .audit-segment { width:100%; min-height:2px; }
  .audit-label { font-size:8px; color:var(--muted); text-align:center; margin-top:5px;
                 writing-mode:vertical-rl; transform:rotate(180deg); height:25px; }
  .audit-legend { display:flex; gap:8px; flex-wrap:wrap; margin-top:8px; }
  .audit-dot { width:8px; height:8px; border-radius:50%; display:inline-block; margin-right:4px; }
  .audit-table { display:grid; grid-template-columns:minmax(0,1fr) auto auto auto;
                 gap:7px 9px; align-items:center; }
  .audit-table .head { color:var(--muted); font-size:10px; }
  .audit-positive { color:var(--xp); }
  .audit-negative { color:var(--hp); }
  .audit-select { width:100%; border:1px solid var(--line); border-radius:12px;
                  background:var(--card); color:var(--text); padding:11px; font:inherit; }
  .audit-summary { display:grid; grid-template-columns:repeat(3,1fr); gap:7px; }

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
  /* The leaderboard peek: one row per equipment slot, opened under the name that was
     tapped. Indented and tinted so it reads as belonging to the row above rather than
     as the next entry in the table. */
  .peek { margin: 8px 0 12px 12px; padding-left: 10px; border-left: 2px solid var(--line); }
  .peek + .foe { margin-top: 8px; }
  .peekrow { background: var(--sunken); border-radius: 10px; padding: 8px 10px; margin-bottom: 6px; }
  .peekrow.empty-slot { display: flex; justify-content: space-between; align-items: center;
                        background: none; border: 1px dashed var(--line); color: var(--muted); }
  .peekrow .stats { margin-top: 3px; }
  .peekrow .fxline { margin-top: 3px; color: var(--accent); }
  .peekrow .desc { margin-top: 4px; font-style: italic; }
  .peekrow.r-rare { box-shadow: inset 3px 0 0 var(--r-rare); }
  .peekrow.r-legendary { box-shadow: inset 3px 0 0 var(--r-legendary); }
  .peekrow.r-uncommon { box-shadow: inset 3px 0 0 var(--r-uncommon); }
  .peekrow.r-cursed { box-shadow: inset 3px 0 0 var(--r-cursed); }
  .foe .av {
    width: 46px; height: 46px; border-radius: 11px; background: var(--card);
    overflow: hidden; flex: none; position: relative;
  }
  .foe .av img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .foe.out { opacity: .45; }
  /* The mob card. A single opponent rather than a row in a list, so it gets a card of
     its own -- and the tier chip carries the whole decision: easy is a payday, hard is
     a gamble, and the colour says which before the numbers are read. */
  .mobcard .mobtaunt { margin-top: 8px; font-size: 13px; font-style: italic;
                       color: var(--muted); border-left: 2px solid var(--line);
                       padding-left: 9px; }
  .tierchip { font-size: 11px; font-weight: 700; border-radius: 999px; padding: 2px 10px;
              border: 1px solid currentColor; white-space: nowrap; }
  .tierchip.win { color: var(--xp); }
  .tierchip.gold { color: var(--gold); }
  .tierchip.loss { color: var(--hp); }
  .pw { color: var(--gold); font-weight: 700; }

  /* ----------------------------------------------------------------------- quests
     A quest card is read once and then acted on in the real world hours later, so it is
     laid out as a brief rather than a list row: what the technique is, what to paint,
     what it pays, and the hashtag that submits it -- in that order, largest first. */
  .qtitle { font-size: 17px; font-weight: 700; }
  .pips { letter-spacing: 1px; font-size: 11px; }
  .pips.d1, .pips.d2 { color: var(--xp); }
  .pips.d3 { color: var(--gold); }
  .pips.d4, .pips.d5 { color: var(--hp); }
  .qreward { background: var(--sunken); border-radius: 10px; padding: 8px 10px;
             font-size: 12px; text-align: center; }
  .qtag { margin-top: 9px; border: 1px dashed var(--accent); border-radius: 10px;
          padding: 8px 10px; font-size: 12px; text-align: center; }
  .qtag b { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .qtag.review { border-style: solid; border-color: var(--gold); color: var(--gold); }
  /* The reroll trade-off, said next to the button rather than discovered after it: a
     reroll climbs a difficulty, so it is a choice and not a free respin. */
  .warn-note { margin-top: 7px; font-size: 11px; line-height: 1.4; color: var(--gold);
               text-align: center; }
  .qbadge { flex: none; font-size: 10px; font-weight: 700; color: var(--r-legendary);
            border: 1px solid var(--r-legendary); border-radius: 999px; padding: 2px 8px;
            white-space: nowrap; align-self: flex-start; }

  /* One pending submission. Three columns so the verdict buttons keep a fixed home no
     matter how long a title or a painter's name turns out to be. */
  .claim { display: grid; grid-template-columns: 46px 1fr; gap: 10px;
           background: var(--sunken); border-radius: 12px; padding: 10px; margin-bottom: 9px; }
  .claim .av { width: 46px; height: 46px; border-radius: 11px; overflow: hidden;
               background: var(--card); }
  .claim .cbody { min-width: 0; }
  .claim .cbody a { color: var(--accent); }
  .claim .cacts { grid-column: 1 / -1; display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .rwrow { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; flex-wrap: wrap; }
  .rwrow label { display: flex; align-items: center; gap: 4px; }
  .rwin { width: 64px; background: var(--sunken); color: var(--fg); font: inherit;
          font-size: 13px; border: 1px solid var(--line); border-radius: 8px; padding: 5px 6px; }

  /* --------------------------------------------------------------------- the mailbox
     Colour-coded by what HAPPENED, not by which subsystem wrote the row: green won, red
     lost, gold earned on the farm, purple changed hands. The left stripe is the only
     thing the eye needs for that, so the icon is free to say which kind of event it was
     and the text is free to say nothing twice. Times are tabular so thirty of them line
     up into a column you can read down. */
  .mday { display: flex; align-items: center; gap: 9px; margin: 15px 0 8px; }
  /* The feed is dropped straight after the panel's heading, so the first day must not
     open a second gap under it. */
  .mday:first-child, h2 + .mday { margin-top: 2px; }
  .mday b { font-size: 11px; letter-spacing: .08em; text-transform: uppercase;
            color: var(--muted); font-weight: 700; }
  .mday i { flex: 1; height: 1px; background: var(--line); }
  .mail {
    --k: var(--muted);
    display: grid; grid-template-columns: 40px 26px 1fr; align-items: start; gap: 8px;
    background: var(--sunken); border-left: 3px solid var(--k); border-radius: 10px;
    padding: 9px 10px; margin-bottom: 7px;
  }
  .mail.win { --k: var(--xp); }
  .mail.loss { --k: var(--hp); }
  .mail.gold { --k: var(--gold); }
  .mail.give { --k: var(--r-legendary); }
  .mail .mt { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
  .mail .mi { font-size: 16px; line-height: 1.3; text-align: center; }
  .mail .mb { min-width: 0; font-size: 13px; }
  .mail .meta { margin-top: 4px; display: flex; flex-wrap: wrap; gap: 7px; font-size: 11px;
                align-items: center; }
  .mail .verdict { color: var(--k); font-weight: 700; }
  .mail .find { border: 1px solid currentColor; border-radius: 999px; padding: 1px 8px;
                font-weight: 600; }

  /* A row that replays a fight. It is a <button>, so it has to be talked back down to
     being a row: a button brings its own font, background, border and centred text, none
     of which a feed line wants. The ▶ is the whole affordance -- these rows are dense,
     and a frame around each one would turn a feed into a grid of boxes. */
  .rerunable { width: 100%; text-align: left; font: inherit; color: inherit;
               background: none; border: 0; padding: 0; margin-bottom: 7px;
               cursor: pointer; }
  .rerunable:active { opacity: .55; }
  /* A mail row is a .rerunable too when its fight can be replayed, and the reset above
     would strip the card and the coloured stripe that carry its meaning. */
  button.mail { background: var(--sunken); border-left: 3px solid var(--k);
                padding: 9px 10px; }
  .play { color: var(--accent); font-weight: 700; }

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

  /* ---------------------------------------------------- turn-based combat prototype */
  .test-banner { border: 1px solid var(--r-legendary); background: rgba(176,107,224,.10);
                 border-radius: 12px; padding: 10px; font-size: 12px; }
  .test-select { width: 100%; background: var(--sunken); color: var(--fg); border: 1px solid var(--line);
                 border-radius: 10px; padding: 10px; font: inherit; margin-top: 4px; }
  .test-loadout { display: grid; grid-template-columns: 1fr; gap: 9px; }
  /* The stage. A fixed aspect ratio rather than a fixed height, so the two creatures keep
     their relative size on every phone instead of being squashed on a short screen. The
     health bars sit ON it rather than under it: the whole point of the redesign is that
     the fight is one thing you look at, with the controls beneath, and a screen that
     stacked banner + two cards + six tall buttons did not fit a phone at all. */
  .test-stage {
    position: relative; aspect-ratio: 16 / 10; max-height: 46vh; border-radius: 14px;
    overflow: hidden; background: var(--sunken); border: 1px solid var(--line);
    touch-action: manipulation;
  }
  /* A floor to stand on. Without something reading as ground, a bobbing cut-out photo
     looks like it is falling rather than idling. */
  .test-stage::after {
    content: ""; position: absolute; left: 6%; right: 6%; bottom: 11%; height: 12px;
    border-radius: 50%; background: radial-gradient(ellipse at center,
      rgba(0,0,0,.30) 0%, rgba(0,0,0,.12) 45%, transparent 72%);
  }
  .test-spot { position: absolute; bottom: 12%; width: 42%; height: 68%; }
  .test-spot[data-side="player"] { left: 4%; }
  .test-spot[data-side="enemy"] { right: 4%; }
  .test-hud { position: absolute; top: 0; left: 0; right: 0; display: grid;
              grid-template-columns: 1fr 1fr; gap: 8px; padding: 8px; }
  .test-hud > div { min-width: 0; }
  .test-hud .who { font-size: 11px; font-weight: 700; white-space: nowrap;
                   overflow: hidden; text-overflow: ellipsis; }
  .test-hud .num { font-size: 10px; color: var(--muted); }
  .test-hud [data-side="enemy"] { text-align: right; }
  .test-hud .hpbar { height: 7px; border-radius: 5px; background: rgba(0,0,0,.35);
                     overflow: hidden; margin-top: 3px; }
  .test-hud .hpbar > i { display: block; height: 100%; background: var(--hp);
                         transition: width .28s; }
  .test-hud .hpbar.barrier > i { background: var(--xp); }
  .test-turn { position: absolute; top: 8px; left: 50%; transform: translateX(-50%);
               font-size: 10px; font-weight: 700; color: var(--muted);
               background: rgba(0,0,0,.30); border-radius: 999px; padding: 2px 9px; }
  /* Four across, so the six fight actions plus auto and exit are two rows on a phone --
     the brief was "all in one or two rows". Compact and icon-led: the scroll's full name
     and rules text live in the catalogue sheet, not on a button somebody taps in a hurry. */
  .test-actions { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; }
  .test-action {
    /* Fixed height rather than auto: a two-line scroll name ("Огненный вал") next to a
       one-line one ("Атака") gives the grid two rows of different heights, which reads as
       a layout bug. Two lines is the ceiling, and the name is clipped rather than allowed
       to make a third. */
    min-height: 68px; padding: 6px 3px; line-height: 1.15; font-size: 11px;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    gap: 3px; text-align: center; overflow: hidden;
  }
  .test-action .ic { font-size: 19px; line-height: 1; }
  .test-action small { display: block; font-size: 9px; opacity: .7; overflow: hidden;
                       text-overflow: ellipsis; white-space: nowrap; max-width: 100%; }
  .test-action.ultimate { border-color: var(--r-legendary); color: var(--r-legendary); }
  .test-action.spent { opacity: .38; }
  /* Roomier once there is room -- a tablet or a desktop browser has no reason to keep
     phone-sized targets. */
  @media (min-width: 520px) {
    .test-action { min-height: 74px; font-size: 12px; }
    .test-action .ic { font-size: 22px; }
  }
  /* ---------------------------------------------------------------------- battle sprites
     A "sprite" here is not a sprite sheet -- it is one photograph of a painted miniature,
     animated entirely with CSS transforms on a single <img>. The idle loop lives on the
     <img> (or the .sprite-fallback glyph) so it keeps running underneath combat moves; the
     combat moves themselves (lunge/hurt/ko) live on the .sprite wrapper, because they need
     to travel the whole picture across the battlefield, not just wobble the artwork in
     place. Nesting the two means they compose for free: a quadruped can still be mid-bounce
     while its wrapper lunges forward.
  */

  .sprite {
    position: relative;
    display: block;
    width: 100%;
    height: 100%;
    /* The photo stands on an implied ground line at its own bottom edge. Pivoting there
       (instead of the default 50% 50%) is what makes breathing/bouncing/tipping-over read
       as the creature moving while its feet stay planted, rather than the whole picture
       swelling or spinning around its own center. */
    transform-origin: 50% 100%;
    will-change: transform;
  }

  .sprite img {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: contain;
    transform-origin: 50% 100%;
    will-change: transform;
  }

  /* Shown instead of an <img> when the fighter has no portrait yet. Sized and centered like
     the glyph placeholder already used elsewhere in this page (see .test-fighter .av), so a
     fighter without a photo still fits the same box other fighters render into. */
  .sprite .sprite-fallback {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 100%;
    height: 100%;
    font-size: 2.4em;
    line-height: 1;
    transform-origin: 50% 100%;
    will-change: transform;
  }

  /* ------------------------------------------------------------------- generated frames
     When Gemini has drawn the creature, the wrapper holds three stacked cut-outs instead
     of one photograph. The two idle frames cross-fade into each other on a loop, which is
     what "breathing" means once the drawing itself changes rather than the transform; the
     attack pose is hidden until the wrapper is mid-lunge.

     The per-archetype transform underneath keeps running regardless, so a flipbooked dog
     still bobs like a dog. The frames carry the breath and the transform carries the
     bounce, and neither has to know about the other. */
  .sprite .frame {
    position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain;
    object-position: 50% 100%; opacity: 0;
  }
  .sprite .frame[data-frame="idle_a"], .sprite .frame[data-frame="idle_b"] {
    animation: sprite-breathe 2.2s ease-in-out infinite;
  }
  /* The two idle frames run the same keyframes half a cycle apart (the delay is set
     inline per frame), so one is always fading up while the other fades down and the
     creature is never briefly invisible. */
  @keyframes sprite-breathe {
    0%, 44% { opacity: 1; }
    56%, 100% { opacity: 0; }
  }
  /* Mid-lunge the drawing itself changes to the attack pose. Held for the whole travel
     rather than flashed, so the moment of impact is a pose and not a flicker. */
  .sprite.lunge .frame[data-frame="idle_a"],
  .sprite.lunge .frame[data-frame="idle_b"] { animation: none; opacity: 0; }
  .sprite.lunge .frame[data-frame="attack"] { opacity: 1; }
  /* A creature whose attack frame never generated: the idle frames must not be blanked by
     the rule above, or it would vanish every time it swung. */
  .sprite[data-framed="1"]:not(:has(.frame[data-frame="attack"])).lunge
    .frame[data-frame="idle_a"] { animation: none; opacity: 1; }
  @media (prefers-reduced-motion: reduce) {
    .sprite .frame[data-frame="idle_a"] { animation: none; opacity: 1; }
    .sprite .frame[data-frame="idle_b"] { animation: none; opacity: 0; }
  }

  /* ------------------------------------------------------------------------- idle loops
     Every archetype gets its own duration and easing on purpose: these sit side by side on
     the same screen (player vs. enemy) and need to be tellable apart at a glance, the same
     way you can tell a dog from a fish from a robot from across a room without looking hard.
     Amplitudes are kept small (a few percent of the sprite's own box) because this plays on
     a loop for minutes at a time -- anything bigger reads as distracting rather than alive.
  */

  /* humanoid: two breaths per cycle (chest rises via scaleY, pivoting from the feet) plus
     one full weight-shift left-then-right over the same span. Slow and even, like someone
     standing at ease. Too fast here and it reads as nervous fidgeting instead of idle rest. */
  @keyframes sprite-idle-humanoid {
    0%   { transform: translateX(0) translateY(0) scaleY(1); }
    25%  { transform: translateX(1.2%) translateY(-1.4%) scaleY(1.018); }
    50%  { transform: translateX(0) translateY(0) scaleY(1); }
    75%  { transform: translateX(-1.2%) translateY(-1.4%) scaleY(1.018); }
    100% { transform: translateX(0) translateY(0) scaleY(1); }
  }
  .sprite[data-kind="humanoid"] img,
  .sprite[data-kind="humanoid"] .sprite-fallback {
    animation: sprite-idle-humanoid 4.8s ease-in-out infinite;
  }

  /* quadruped: a vertical head-bob with a slight rotational sway, noticeably quicker than
     the humanoid breath -- a four-legged animal's resting shift of weight is a livelier,
     choppier motion than a biped's. Too slow and it stops reading as an animal at all. */
  @keyframes sprite-idle-quadruped {
    0%   { transform: translateY(0) rotate(0deg); }
    50%  { transform: translateY(-3.2%) rotate(-1deg); }
    100% { transform: translateY(0) rotate(0deg); }
  }
  .sprite[data-kind="quadruped"] img,
  .sprite[data-kind="quadruped"] .sprite-fallback {
    animation: sprite-idle-quadruped 1.8s cubic-bezier(.42, 0, .58, 1) infinite;
  }

  /* bird: quick vertical bob paired with a horizontal squash pulse that reads as folded
     wings resettling against the body. The overshoot in the easing curve (values above 1)
     gives a light springy snap rather than a smooth glide -- a bird's small movements are
     sharp, not fluid, and a plain ease-in-out here would look aquatic instead. */
  @keyframes sprite-idle-bird {
    0%   { transform: translateY(0) scaleX(1); }
    38%  { transform: translateY(-4.5%) scaleX(1.045); }
    55%  { transform: translateY(.5%) scaleX(.97); }
    100% { transform: translateY(0) scaleX(1); }
  }
  .sprite[data-kind="bird"] img,
  .sprite[data-kind="bird"] .sprite-fallback {
    animation: sprite-idle-bird 1.05s cubic-bezier(.33, 1.4, .6, 1) infinite;
  }

  /* insect: small, fast, deliberately IRREGULAR steps. The stops below are not evenly
     spaced and the timing is linear (no easing curve smoothing the travel between them) --
     that combination is what produces a jitter instead of a wobble. Evenly-spaced stops
     with ease-in-out would just look like a tiny, oddly slow version of the bird idle. */
  @keyframes sprite-idle-insect {
    0%   { transform: translate(0, 0); }
    9%   { transform: translate(.8%, -.6%); }
    18%  { transform: translate(-.5%, .9%); }
    30%  { transform: translate(.9%, .4%); }
    44%  { transform: translate(-.9%, -.7%); }
    58%  { transform: translate(.4%, .8%); }
    71%  { transform: translate(-.6%, -.4%); }
    85%  { transform: translate(.7%, -.9%); }
    100% { transform: translate(0, 0); }
  }
  .sprite[data-kind="insect"] img,
  .sprite[data-kind="insect"] .sprite-fallback {
    animation: sprite-idle-insect .62s linear infinite;
  }

  /* aquatic: a vertical float and a roll (rotate), deliberately a quarter-cycle OUT OF
     PHASE with each other -- when the float crosses its resting height the roll is at an
     extreme, and vice versa, so the two never bottom out into a neutral pose at the same
     instant. That is what "never fully still" means here in practice: an in-phase version
     of this (both peaking together) has a visible dead moment at 0%/50%/100%. */
  @keyframes sprite-idle-aquatic {
    0%   { transform: translateY(0) rotate(1deg); }
    25%  { transform: translateY(-4%) rotate(0deg); }
    50%  { transform: translateY(0) rotate(-1deg); }
    75%  { transform: translateY(4%) rotate(0deg); }
    100% { transform: translateY(0) rotate(1deg); }
  }
  .sprite[data-kind="aquatic"] img,
  .sprite[data-kind="aquatic"] .sprite-fallback {
    animation: sprite-idle-aquatic 3.6s ease-in-out infinite;
  }

  /* reptile: a big, slow breath (larger scaleY than humanoid) that PAUSES at the top for a
     tenth of the cycle before releasing, plus a small head tilt that arrives with that
     pause. The hold is the important part -- without it this is just a slower version of
     the humanoid breath; with it, it reads as an animal that is in no hurry at all. */
  @keyframes sprite-idle-reptile {
    0%   { transform: scaleY(1) translateY(0) rotate(0deg); }
    45%  { transform: scaleY(1.035) translateY(-1.3%) rotate(-1.2deg); }
    55%  { transform: scaleY(1.035) translateY(-1.3%) rotate(-1.2deg); }
    100% { transform: scaleY(1) translateY(0) rotate(0deg); }
  }
  .sprite[data-kind="reptile"] img,
  .sprite[data-kind="reptile"] .sprite-fallback {
    animation: sprite-idle-reptile 5.6s cubic-bezier(.45, 0, .55, 1) infinite;
  }

  /* blob: classic squash-and-stretch. scaleX and scaleY move in opposite directions and
     roughly cancel out (1.09 x .90 and .93 x 1.09 both land near 1.0), so the shape changes
     without visibly changing size -- that near-cancellation is "volume roughly preserved". */
  @keyframes sprite-idle-blob {
    0%   { transform: scale(1, 1) translateY(0); }
    30%  { transform: scale(1.09, .90) translateY(1.5%); }
    65%  { transform: scale(.93, 1.09) translateY(-2.2%); }
    100% { transform: scale(1, 1) translateY(0); }
  }
  .sprite[data-kind="blob"] img,
  .sprite[data-kind="blob"] .sprite-fallback {
    animation: sprite-idle-blob 1.5s ease-in-out infinite;
  }

  /* machine: rigid, discrete motion. steps(1, jump-end) turns every segment between two
     keyframes into a HOLD of the earlier value followed by an instant snap to the next --
     no easing curve can produce that, only a stepped timing function can. The pose reads as
     a servo ticking into position rather than settling there. Most of the 5.2s loop is a
     dead hold (the machine is simply idling); the pair of stops at 88%/92% is a brief,
     sharp misalignment nudge -- a "glitch" -- that only occupies about a tenth of the loop,
     which is what keeps it reading as rare rather than as a constant twitch. */
  @keyframes sprite-idle-machine {
    0%   { transform: translate(0, 0); }
    22%  { transform: translate(0, -2%); }
    47%  { transform: translate(0, 0); }
    86%  { transform: translate(0, 0); }
    88%  { transform: translate(1.6%, -1%); }
    92%  { transform: translate(-.8%, .4%); }
    96%  { transform: translate(0, 0); }
    100% { transform: translate(0, 0); }
  }
  .sprite[data-kind="machine"] img,
  .sprite[data-kind="machine"] .sprite-fallback {
    animation: sprite-idle-machine 5.2s steps(1, jump-end) infinite;
  }

  /* vehicle: a tight, high-frequency buzz -- sub-1% amplitude, linear timing (an eased
     vibration looks like gentle rocking, not an idling engine), and a period fast enough
     (under 200ms) to blur into a hum rather than read as discrete little hops. */
  @keyframes sprite-idle-vehicle {
    0%   { transform: translate(0, 0); }
    25%  { transform: translate(.35%, -.25%); }
    50%  { transform: translate(-.3%, .3%); }
    75%  { transform: translate(.25%, .2%); }
    100% { transform: translate(0, 0); }
  }
  .sprite[data-kind="vehicle"] img,
  .sprite[data-kind="vehicle"] .sprite-fallback {
    animation: sprite-idle-vehicle .16s linear infinite;
  }

  /* plant: very slow rotation from the base, like a stalk bending in wind. The keyframe
     stops (32%/68%) are deliberately NOT symmetric around 50% -- an evenly-spaced sway
     reads as a metronome, an unevenly-spaced one reads as actual gusts arriving at their
     own pace. This is the slowest idle of the twelve; anything quicker stops looking lazy. */
  @keyframes sprite-idle-plant {
    0%   { transform: rotate(0deg) translateX(0); }
    32%  { transform: rotate(2.6deg) translateX(.8%); }
    68%  { transform: rotate(-2.1deg) translateX(-.6%); }
    100% { transform: rotate(0deg) translateX(0); }
  }
  .sprite[data-kind="plant"] img,
  .sprite[data-kind="plant"] .sprite-fallback {
    animation: sprite-idle-plant 6.4s ease-in-out infinite;
  }

  /* spirit: rises and fades together rather than just sliding up and down -- the opacity
     dip at the peak (down to .78, never fully transparent) plus a faint scale-up is what
     sells "weightless" instead of "a solid object on a slow elevator". */
  @keyframes sprite-idle-spirit {
    0%   { transform: translateY(0) scale(1); opacity: 1; }
    50%  { transform: translateY(-5%) scale(1.015); opacity: .78; }
    100% { transform: translateY(0) scale(1); opacity: 1; }
  }
  .sprite[data-kind="spirit"] img,
  .sprite[data-kind="spirit"] .sprite-fallback {
    animation: sprite-idle-spirit 3.4s ease-in-out infinite;
  }

  /* creature: the fallback for "we don't know what this is". Deliberately the plainest
     animation here -- a single breathing cycle, no sway, no tilt, nothing that commits to a
     body plan the photo might not have. It should look calm and alive, never wrong. */
  @keyframes sprite-idle-creature {
    0%   { transform: translateY(0) scaleY(1); }
    50%  { transform: translateY(-1.1%) scaleY(1.014); }
    100% { transform: translateY(0) scaleY(1); }
  }
  .sprite[data-kind="creature"] img,
  .sprite[data-kind="creature"] .sprite-fallback {
    animation: sprite-idle-creature 4s ease-in-out infinite;
  }

  /* -------------------------------------------------------------------------- combat moves
     These animate the .sprite WRAPPER, not the img -- the idle keyframes above keep running
     on the img underneath, so a lunge or a hit lands on top of whatever the idle is doing
     instead of interrupting it.

     --dir carries which way this fighter's own attacks travel: 1 for the player (posted on
     the left, attacks rightward toward the enemy) and -1 for the enemy (posted on the
     right, attacks leftward toward the player). One keyframe set drives both because every
     distance in it is written as var(--dir) times a fixed length; sprite.js is responsible
     for setting --dir when it builds the markup. The var(--dir, 1) fallback just means a
     sprite that somehow renders without the inline custom property lunges toward the right
     instead of not moving at all. */

  @keyframes sprite-lunge {
    0%   { transform: translateX(0) scale(1); }
    38%  { transform: translateX(calc(var(--dir, 1) * 22px)) scale(1.045); }
    100% { transform: translateX(0) scale(1); }
  }
  /* 420ms total, but the keyframe stop that marks "fully lunged" sits at 38% of it (about
     160ms) -- reaching the strike quickly and then taking the remaining ~260ms to settle
     back is what makes an impact feel thrown rather than just slid back and forth. */
  .sprite.lunge {
    animation: sprite-lunge 420ms cubic-bezier(.2, .8, .3, 1) 1;
  }

  /* Recoils AWAY from the attacker -- the opposite sign of this fighter's own --dir, since
     getting hit pushes you back from where you throw your own attacks -- while a brightness
     spike and a brief drop-shadow tinted with the existing damage colour (--hp) read as an
     impact flash. Kept short (300ms) and small (9px, half the lunge distance) on purpose:
     this fires every time a hit lands, so anything bigger would turn into a constant strobe
     over a multi-turn fight instead of a single readable "ow". */
  @keyframes sprite-hurt {
    0%   { transform: translateX(0); filter: brightness(1) saturate(1) drop-shadow(0 0 0 transparent); }
    30%  { transform: translateX(calc(var(--dir, 1) * -9px)); filter: brightness(1.55) saturate(.5) drop-shadow(0 0 5px var(--hp)); }
    100% { transform: translateX(0); filter: brightness(1) saturate(1) drop-shadow(0 0 0 transparent); }
  }
  .sprite.hurt {
    animation: sprite-hurt 300ms ease-out 1;
  }

  /* A settled end state, not a loop -- entered once via transition and then left alone.
     Tips the sprite backward (away from its own attack direction, same convention as the
     hurt recoil) and desaturates/dims it so a knocked-out fighter reads as out of the fight
     at a glance next to one that is merely idling. The !important on the idle animations is
     deliberate: idle rules and this one share the same selector specificity
     (.sprite[data-kind] vs .sprite.ko), so without it, source order alone would decide the
     winner and a KO'd fighter could keep visibly breathing underneath its own dimmed, tipped
     pose depending on where this rule happens to sit in the file. */
  .sprite.ko {
    transform: rotate(calc(var(--dir, 1) * -14deg)) translateY(1.5%);
    filter: grayscale(.65) brightness(.55);
    transition: transform .4s ease-out, filter .4s ease-out;
  }
  .sprite.ko img,
  .sprite.ko .sprite-fallback {
    animation: none !important;
  }

  /* Idle motion is constant and ambient -- exactly the category prefers-reduced-motion
     exists to suppress. Combat moves are brief, meaningful, and rare enough to keep, but
     still get compressed toward "instant" out of respect for the same preference: a snap to
     the new pose rather than a 420ms slide still tells the player a hit happened. */
  @media (prefers-reduced-motion: reduce) {
    .sprite img,
    .sprite .sprite-fallback {
      animation: none !important;
    }
    .sprite.lunge {
      animation-duration: 70ms !important;
    }
    .sprite.hurt {
      animation-duration: 120ms !important;
    }
    .sprite.ko {
      transition-duration: 120ms !important;
    }
  }

  .test-log { max-height: 270px; overflow-y: auto; display: flex; flex-direction: column; gap: 6px; }
  .test-event { background: var(--sunken); border-radius: 9px; padding: 7px 9px; font-size: 12px;
                border-left: 3px solid var(--line); }
  .test-event.damage, .test-event.burn { border-left-color: var(--hp); }
  .test-event.heal, .test-event.shield { border-left-color: var(--xp); }
  .test-event.spell { border-left-color: var(--r-legendary); }

  /* ---------------------------------------------------------------- the fight view
     A fight is a sequence, so it is played as one: two HP bars and the blows arriving in
     order. The chat interface can only post the verdict; this is the part that was missing
     rather than a prettier version of what was there. */
  .duel { position: fixed; inset: 0; z-index: 50; background: var(--bg); display: flex;
          flex-direction: column; padding: calc(14px + env(safe-area-inset-top)) 14px
          calc(14px + env(safe-area-inset-bottom)); }
  .duel .side { margin-bottom: 10px; }
  .duel .fighters { display:flex; align-items:center; justify-content:center; gap:12px; margin-bottom:10px; }
  .duel .fighter-art { width:58px; height:58px; border-radius:8px; overflow:hidden; border:2px solid var(--line); display:grid; place-items:center; background:var(--card); }
  .duel .fighter-art img { width:100%; height:100%; object-fit:cover; }
  .duel .fighter-art .dungeon-art { width:100%; height:100%; }
  .duel .versus { font-weight:700; color:var(--muted); }
  .duel .hpbar { height: 12px; border-radius: 6px; background: var(--sunken); overflow: hidden; }
  .duel .hpbar > i { display: block; height: 100%; background: var(--hp); transition: width .28s; }
  .duel .log { flex: 1; overflow-y: auto; margin: 12px 0; display: flex; flex-direction: column; gap: 7px; }
  .duel .blow { background: var(--card); border-radius: 10px; padding: 9px 11px; font-size: 14px;
                animation: rise .16s ease-out; border-left: 3px solid var(--line); }
  .duel .blow.crit { border-left-color: var(--gold); }
  .duel .blow.dodge { border-left-color: var(--muted); opacity: .8; }
  .duel .blow.mine { border-left-color: var(--accent); }
  .duel .blow.amulet { border-left-color: var(--r-legendary); }
  .duel .blow.skill { border-left-color: var(--r-rare); }
  .duel .blow.defend { border-left-color: #4c82b8; }
  /* The flavour text is prose with three things buried in it -- who acted, who was hit,
     and how much. They are the only parts anybody actually reads at one line per half
     second, so they are the only parts coloured: two sides, and a number whose colour
     says which direction it moved HP. Everything else stays body text on purpose; a line
     where six words are highlighted is a line where nothing is. */
  .duel .nm { font-weight: 700; }
  .duel .nm.mine { color: var(--mine); }
  .duel .nm.them { color: var(--foe); }
  .duel .amount { font-weight: 700; }
  .duel .amount.harm { color: var(--hp); }
  .duel .amount.heal { color: var(--xp); }
  .duel .amount.soak { color: var(--muted); }
  .duel .verdict { text-align: center; font-size: 20px; font-weight: 700; margin: 6px 0; }
  /* A replay is pixel-for-pixel the live fight, which is the point -- and exactly why it
     has to say so somewhere, or a rerun of an old defeat reads as a fresh one. */
  .duel .rerun {
    align-self: center; font-size: 11px; font-weight: 700; letter-spacing: .06em;
    text-transform: uppercase; color: var(--muted); border: 1px solid var(--line);
    border-radius: 999px; padding: 3px 11px; margin-bottom: 8px;
  }
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
      <span id="hudRubyBox" hidden>💎 <b id="hudRuby">0</b></span>
    </div>
    <div class="bar"><i id="hudXp" style="width:0%"></i></div>
  </div>
  <button class="post" id="hudMail" title="Почта">📬</button>
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
  <button id="questReviewTab" data-tab="review" hidden><span class="ic">🛡</span>Проверка</button>
  <button data-tab="more"><span class="ic">☰</span>Ещё</button>
</nav>

<script>
const PREFIX = "__PREFIX__";
/* CSS reaches the page but not the frame around it: the header carrying the bot's name,
   and the strip a rubber-band scroll pulls into view. Those are the client's, and on a
   phone set to a light theme they stay white around our dark page unless asked. Each
   setter arrived in a different Bot API version, so each is asked for separately -- a
   client too old to answer keeps its own chrome, which is no worse than today. */
function paintChrome(tg) {
  const ask = (method, colour, since) => {
    try { if (tg.isVersionAtLeast(since)) tg[method](colour); } catch (e) {}
  };
  ask("setBackgroundColor", "#17212b", "6.1");
  ask("setHeaderColor", "#17212b", "6.9");
  ask("setBottomBarColor", "#232e3c", "7.10");
}
const tg = window.Telegram && window.Telegram.WebApp;
if (tg) { tg.ready(); tg.expand(); paintChrome(tg); }
const initData = (tg && tg.initData) || "";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let S = null;            // the server's state, verbatim -- never edited on the client
let TAB = "hero";
let SHOP = null, FOES = null, ROSTER = null;
let TEST_SETUP = null, TEST_BATTLE = null, TEST_SESSION = null, TEST_MODE = null, TEST_BUSY = false;
let ticker = null;
const START_VIEW = new URLSearchParams(window.location.search).get("view");

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
    if (data.battle) playDuel(data.battle);
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
  const minutes = Math.ceil(Math.max(0, Number(seconds) || 0) / 60);
  const h = Math.floor(minutes / 60), m = minutes % 60;
  return h ? h + " ч " + m + " мин" : m + " мин";
}
const STAT_ICON = { strength: "⚔️", health: "❤️", agility: "💨", luck: "🍀", endurance: "🫁", armor: "🛡" };
const STAT_NAME = { strength: "Сила", health: "Здоровье", agility: "Ловкость",
                    luck: "Удача", endurance: "Выносливость", armor: "Броня" };

function bonusText(bonuses) {
  const parts = [];
  for (const key of ["strength", "health", "agility", "luck", "endurance", "armor"]) {
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
// What the HUD portrait currently shows. renderHud runs once a SECOND while the fight
// bank is recharging (see tick), and rewriting innerHTML hands the browser a brand new
// <img> every time -- which is a fresh load, a fresh decode and a visible flicker once a
// second, forever. The picture only ever changes when the URL or the crop does, so that
// pair is the repaint condition; everything else in the HUD is text and cheap to rewrite.
let hudFaceKey = null;

function renderHud() {
  const pet = S && S.pet;
  $("hudName").textContent = pet ? pet.name : "Без существа";
  $("hudLevel").textContent = pet ? pet.level : "—";
  $("hudCoins").textContent = money(S ? S.coins : 0);
  const faceKey = pet ? (pet.portrait || "") + "|" + JSON.stringify(pet.crop || null) : "";
  if (faceKey !== hudFaceKey) {
    hudFaceKey = faceKey;
    $("hudFace").innerHTML = pet ? shot(pet.portrait, pet.crop) : "🥚";
    if (pet) paintShots($("hudFace"));
  }
  const rubies = (S && S.rubies) || 0;
  $("hudRubyBox").hidden = !rubies;
  $("hudRuby").textContent = money(rubies);
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
        "<div>" + slot(worn.shield) + "</div>" +
        "<div>" + slot(worn.gloves) + "</div>" +
        "<div>" + slot(worn.amulet) + "</div>" +
        "<div>" + slot(worn.boots) + "</div>" +
        '<div class="tiny muted pet-equipment-summary">' +
          esc(pet.name) + " · ур. " + pet.level + "<br>" +
          pet.xp + " / " + pet.xp_needed + " опыта<br>" +
          "боёв " + pet.fights + " · побед " + pet.wins +
        "</div>" +
      "</div>" +
    "</div>" + liveSkillsPanel() + dungeonPanel() +

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
      statRespec() +
      // Under the stats it is subtracting from. Tapping the portrait right above is how
      // the picture gets changed, so the way out is one screen away from the explanation.
      debuffNote(S.debuff) +
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
  // Signed both ways: the gap between purchased and effective used to be gear alone and
  // therefore always positive, but a granted debuff scales the effective number down, and
  // a hardcoded "+" turned that into "+-1".
  const bonus = stat.bonus
    ? ' <span class="' + (stat.bonus > 0 ? "gain" : "loss") + '">' +
      (stat.bonus > 0 ? "+" : "−") + Math.abs(stat.bonus) + "</span>"
    : "";
  const maxed = stat.max != null && stat.purchased >= stat.max;
  const buttons = stat.gear_only
    ? '<span class="tiny muted">только с вещей</span>'
    : (maxed
        ? '<span class="tiny muted">максимум</span>'
        : '<button class="plus" data-up="' + stat.key + '" data-times="1"' +
            (affordable(stat.cost_1) ? "" : " disabled") + ">+1 · " +
            (stat.cost_1 ? money(stat.cost_1) : "бесплатно") + "</button>" +
          '<button class="plus" data-up="' + stat.key + '" data-times="10"' +
            (affordable(stat.cost_10) ? "" : " disabled") + ">+10 · " +
            (stat.cost_10 ? money(stat.cost_10) : "бесплатно") + "</button>");
  const pending = stat.pending_effect ? ' <span class="tiny muted">эффект позже</span>' : "";
  return '<div class="statrow"><div class="lbl">' +
    (STAT_ICON[stat.key] || "") + " <b>" + esc(stat.name) + "</b> " +
    '<span class="muted small">' + stat.purchased + "</span> → <b>" + stat.effective + "</b>" + bonus + pending +
    "</div>" + buttons + "</div>";
}

function statRespec() {
  if (!S.pet) return "";
  const invested = (S.stats || []).some((stat) => !stat.gear_only && stat.purchased > 1);
  const points = Number(S.stat_points || 0);
  const cost = Number(S.stat_respec_ruby_cost || 15);
  const disabled = !invested || Number(S.rubies || 0) < cost;
  return '<div class="stat-respec">' +
    (points ? '<span class="tiny">🎯 Свободные очки: <b>' + points + '</b></span>' : "") +
    '<p class="tiny muted">Стат ниже половины от соперника открывает слабость в бою.</p>' +
    '<button class="go sec" data-do="respec"' + (disabled ? " disabled" : "") +
    '>🔄 Перераспределить статы · ' + cost + ' 💎</button></div>';
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

function dungeonArt(enemy) {
  const fill = enemy.boss ? "#e05a5a" : "#76b87b";
  const eye = enemy.boss ? "#ffe99a" : "#d8f4d3";
  return '<span class="dungeon-art"><svg viewBox="0 0 64 64" aria-hidden="true"><rect width="64" height="64" fill="' + (enemy.boss ? '#301d2c' : '#17312c') + '"/><path d="M5 55L19 35 29 42 40 20 59 55Z" fill="#0e1b24"/><path d="M17 52c0-17 7-29 16-29s16 12 16 29" fill="' + fill + '"/><circle cx="27" cy="34" r="3" fill="' + eye + '"/><circle cx="39" cy="34" r="3" fill="' + eye + '"/>' + (enemy.boss ? '<path d="M24 24l-7-12 13 8 4-14 4 14 13-8-7 12" fill="#f0c85a"/>' : '') + '</svg></span>';
}

function dungeonPanel() {
  const dungeon = S.dungeon || {};
  if (!S.pet) return "";
  if (!dungeon.available) {
    return '<div class="panel dungeon"><div class="dungeon-head"><div class="dungeon-title">Подземелье закрыто<small>Экспедиция ведёт расследование</small></div></div><div class="dungeon-body"><p class="small muted" style="margin:0">' + esc(dungeon.closed_notice || 'Подземелье временно закрыто.') + '</p>' + (dungeon.active ? '<button class="go warn" style="margin-top:10px" data-dungeon="quit">Вернуться</button>' : '') + '</div></div>';
  }
  if (!dungeon.active) {
    const eligible = Number(dungeon.power || 0) >= Number(dungeon.min_power || 1000);
    return '<div class="panel dungeon"><div class="dungeon-head"><div class="dungeon-title">Подземелье<small>Ниже этаж - опаснее добыча</small></div><div class="dungeon-stat">⚡ ' + money(dungeon.power) + ' / ' + money(dungeon.min_power) + '</div></div><div class="dungeon-body"><p class="small muted" style="margin:0 0 10px">Три врага на этаж. Здоровье не восстанавливается после боя; отдых доступен после зачистки.</p><button class="go" data-dungeon="enter"' + (eligible ? '' : ' disabled') + '>⚔️ Войти · ' + dungeon.entry_cost + ' 💎</button>' + (Number(dungeon.deepest || 1) > 1 ? '<button class="go sec" style="margin-top:8px" data-dungeon="escalator">🪜 Эскалатор до ' + dungeon.deepest + ' · ' + (Number(dungeon.entry_cost || 0) + Number(dungeon.escalator_cost || 0)) + ' 💎</button>' : '') + '</div></div>';
  }
  const boss = dungeon.encounters && dungeon.encounters[0] && dungeon.encounters[0].boss;
  const enemies = (dungeon.encounters || []).map((enemy) => '<button class="dungeon-enemy' + (enemy.cleared ? ' done' : '') + '" data-dungeon="fight" data-index="' + enemy.index + '"' + (enemy.cleared ? ' disabled' : '') + '>' + dungeonArt(enemy) + '<span><b>' + esc(enemy.name) + '</b><br><span class="tiny muted">ур. ' + enemy.level + (enemy.hint ? ' · ' + esc(enemy.hint) : '') + '</span></span><span>' + (enemy.cleared ? '✓' : '⚔️') + '</span></button>').join('');
  return '<div class="panel dungeon"><div class="dungeon-head' + (boss ? ' boss' : '') + '"><div class="dungeon-title">' + esc(dungeon.theme) + '<small>Этаж ' + dungeon.floor + (boss ? ' · БОСС' : '') + '</small></div><div class="dungeon-stat">❤️ ' + dungeon.hp + ' / ' + dungeon.max_hp + '</div></div><div class="dungeon-body"><div class="dungeon-enemies">' + enemies + '</div>' + (dungeon.can_rest ? '<div class="dungeon-actions"><button class="go sec" data-dungeon="rest">🛒 Лечение · ' + dungeon.heal_cost + '</button><button class="go" data-dungeon="descend">⬇️ Спуститься</button></div>' : '') + '<button class="go warn" style="margin-top:9px" data-dungeon="quit">Выйти из подземелья</button></div></div>';
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
    "</div>" + forgePanel();
}

function shortSkillName(name) { return String(name || "").replace(/^.*?: /, ""); }
const SCROLL_ELEMENTS = {
  fire: "🔥 Огненный", frost: "❄️ Морозный", water: "💧 Водный",
  earth: "🪨 Земля", plants: "🌿 Растения", air: "💨 Воздушный",
};
function scrollElement(spell) { return SCROLL_ELEMENTS[spell && spell.element] || ""; }

/* The damage and effect numbers, worded server-side by pets_scroll_catalog.effect_text so
   this page and the Telegram screen can never describe the same scroll differently. */
function scrollEffects(spell) {
  const lines = (spell && spell.effects_text) || [];
  if (!lines.length) return "";
  return '<div class="fx">' + lines.map((line) => '<span>' + esc(line) + '</span>').join("") + '</div>';
}

function liveSkillsPanel() {
  const rows = (S.skills && S.skills.slots) || [];
  const rewards = (S.skills && S.skills.rewards) || {};
  const hard = rewards.hard_quest_chances || {};
  return '<div class="panel"><div class="row spread"><h2 style="margin:0">📜 Свитки</h2>' +
    '<span class="tiny muted">в бою выбираются автоматически</span></div>' +
    '<div class="live-skills" style="margin-top:9px">' + rows.map((spell) =>
      spell.empty
        ? '<button class="go sec live-skill empty" data-liveskill="' + spell.slot +
          '"><b>' + spell.slot + ' · Пусто</b><small>' +
          (spell.ultimate ? 'слот под ультимейт' : 'слот свободен') + '</small></button>'
        : '<button class="go sec live-skill' + (spell.ultimate ? " ultimate" : "") +
          '" data-liveskill="' + spell.slot + '"><b>' + spell.slot + ' · ' + esc(spell.icon) + " " +
          esc(shortSkillName(spell.name)) + '</b>' + scrollEffects(spell) +
          '<small>' + esc(scrollElement(spell)) +
          (spell.dodgeable === false ? ' · нельзя увернуться' : '') + " · " +
          esc(spell.short) + '</small></button>'
    ).join("") + '</div><div class="tiny muted" style="margin-top:10px">Открыто ' +
      Number(S.skills.owned_count || 0) + ' из ' + Number(S.skills.catalogue_count || 0) +
      '. Новый #япокрасил: ' + Math.round(Number(rewards.paint_chance || 0) * 1000) / 10 +
      '% (не позже ' + Number(rewards.paint_pity || 0) + '-го). Сложные квесты 4/5: ' +
      Math.round(Number(hard[4] || 0) * 100) + '%/' + Math.round(Number(hard[5] || 0) * 100) +
      '% (не позже ' + Number(rewards.hard_quest_pity || 0) + '-го).</div></div>';
}

function openLiveSkillPicker(slot) {
  const number = Number(slot);
  const pool = number === 4 ? S.skills.ultimate : S.skills.regular;
  const current = S.skills.slots[number - 1];
  const filled = current && !current.empty;
  sheet('<h3>' + (number === 4 ? '✨ Ультимейт · один раз за бой' : '📜 Свиток · слот ' + number) +
    '</h3><p class="tiny muted">Слот можно оставить пустым. Каждый выбранный свиток используется один раз за бой; в автобою доступные свитки имеют одинаковый шанс применения.</p>' +
    (filled
      ? '<button class="go sec" data-liveskillset="' + number + ':">✖️ Освободить слот</button>'
      : '') +
    (pool.length
      ? ''
      : '<div class="empty">Открытых свитков для этого слота пока нет. Они выпадают за #япокрасил и за принятые сложные квесты.</div>') +
    pool.map((spell) => '<div class="panel"><b>' + esc(spell.icon) + " " + esc(spell.name) +
      '</b><div class="small">' + esc(spell.short) + '</div>' + scrollEffects(spell) +
      '<div class="tiny muted">' +
      esc(scrollElement(spell)) + (spell.dodgeable === false ? ' · нельзя увернуться' : '') +
      ' · один раз за бой</div>' +
      '<button class="go sec" style="margin-top:8px" data-liveskillset="' + number + ':' +
      esc(spell.code) + '"' + (current && current.code === spell.code ? ' disabled' : '') + '>' +
      (current && current.code === spell.code ? 'Выбрано' : 'Поставить в слот') + '</button></div>').join(''));
}

function forgePanel() {
  const names = { cursed: "проклятых", common: "обычных", rare: "редких", legendary: "легендарный" };
  const recipes = (S.forge && S.forge.recipes) || [];
  const runeState = S.runes || { runes: {}, enchantments: {}, cost: 15 };
  const weapons = (S.bag || []).filter((item) => item.slot === "weapon");
  const runeNames = { fire: "Огонь", frost: "Лёд", water: "Вода", earth: "Земля", air: "Воздух", plants: "Растения" };
  return '<div class="panel"><h2>⚒️ Кузница</h2>' +
    '<div class="small muted" style="margin-bottom:10px">6 проклятых превращаются в редкую проклятую реликвию, ' +
      '5 обычных — в редкий, а 7 редких — в легендарный. Надетые и защищённые вещи не расходуются.</div>' +
    recipes.map((recipe) => {
      const ingredients = recipe.ingredients.map((code) => S.bag.find((item) => item.code === code))
        .filter(Boolean);
      return '<div class="panel" style="margin:8px 0;padding:10px">' +
        '<div class="small"><b>' + recipe.required + ' ' + names[recipe.rarity] + ' → ' + names[recipe.result_rarity] +
        '</b> · доступно ' + recipe.available + '</div>' +
        (ingredients.length ? '<div class="tiny muted" style="margin:5px 0">Уйдут: ' +
          ingredients.map((item) => esc(item.name)).join(', ') + '</div>' : '') +
        '<button class="go sec" data-reforge="' + recipe.rarity + '"' +
          (recipe.can_forge ? '' : ' disabled') + '>Перековать</button></div>';
    }).join('') +
    '<div class="panel" style="margin:8px 0;padding:10px"><h2>🔮 Зачарования</h2>' +
      '<div class="tiny muted">Руна добавляет эффект оружию. Цена: ' + runeState.cost + ' 💎 и 1 руна.</div>' +
      (weapons.length ? weapons.map((weapon) => '<div class="small" style="margin-top:9px"><b>' + esc(weapon.name) +
        '</b>' + (runeState.enchantments[weapon.code] ? ' · ' + esc(runeNames[runeState.enchantments[weapon.code]]) : '') +
        '<div class="row" style="flex-wrap:wrap;margin-top:5px">' + Object.keys(runeNames).map((element) =>
          '<button class="go sec" data-enchant="' + esc(weapon.code) + ':' + element + '"' +
          (runeState.runes[element] ? '' : ' disabled') + '>' + esc(runeNames[element]) + ' · ' +
          Number(runeState.runes[element] || 0) + '</button>').join('') + '</div></div>').join('') :
        '<div class="empty">В сумке нет оружия для зачарования.</div>') + '</div>' +
    '<button class="go sec" disabled>🛠️ Ковка оружия — скоро</button></div>';
}

// `skipAll` for the shop, where every equipment slot has its own personal rotation.
function slotChips(active, key, skipAll) {
  const slots = [["all", "Всё"], ["weapon", "🗡 Оружие"], ["amulet", "📿 Амулеты"],
                 ["gloves", "🧤 Перчатки"], ["boots", "👢 Сапоги"], ["shield", "🛡 Щиты"]];
  return slots.filter(([value]) => !(skipAll && value === "all")).map(([value, label]) =>
    '<button class="chip' + (active === value ? " on" : "") + '" data-' + key + '="' + value + '">' +
    label + "</button>").join("");
}

function rarityChips(active, key) {
  const rarities = [["all", "Любая"], ["legendary", "🟣"], ["rare", "🔵"],
                    ["common", "⚪"], ["cursed", "☠️"]];
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
    '<span class="meta">' + bonusText(item.bonuses) + " · 💰" + money(item.price) + "</span>" +
    (item.effect && item.effect.text
      ? '<span class="tiny muted">✨ ' + esc(item.effect.text) + "</span>"
      : "") +
    "</button>";
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
      "Витрина · меняется каждые 12 часов" +
    "</h2>" +
    (items.length ? '<div class="items">' + items.map(shopCard).join("") + "</div>"
                  : '<div class="empty">Сегодня тут пусто.</div>') +
    "</div>";
}

// ----------------------------------------------------------------------- arena screen
async function renderArena() {
  const box = $("scr-arena");
  if (!S.pet) { box.innerHTML = '<div class="empty">Сначала нужно существо.</div>'; return; }
  if (TEST_SETUP || TEST_BATTLE) { renderTestBattle(box); return; }
  const arena = S.arena;
  if (!FOES) { box.innerHTML = '<div class="empty">Ищу соперников…</div>';
               FOES = await api("/api/opponents"); }

  // Two reasons a fight can be refused, and they stop different things. The farm stops
  // every kind: the creature is not here. An empty arena bank stops arena fights ONLY --
  // PVE keeps its own counter and spends nothing from this one (pets.record_mob_fight),
  // which is why mobPanel is handed the farm alone and never this combined flag.
  const farming = arena.farming ? "Существо на ферме — оттуда не дерутся." : null;
  const blocked = farming
    || (arena.available > 0 ? null : "Бои с игроками кончились. Восстановление: " +
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
    // Directly under the power rating it has already reduced. That is the whole of why
    // it is here and not on some notifications screen: this is the number it changed.
    debuffNote(FOES.my_debuff) +
    "</div>" +
    (blocked ? '<div class="panel"><div class="small">' + esc(blocked) + "</div></div>" : "") +
    '<div class="panel"><h2>🧪 Пошаговый бой · тест</h2>' +
      '<div class="small muted" style="margin-bottom:9px">Четыре свитка, щиты, защита и ручные ходы. ' +
      'Результаты, награды и счётчики не записываются.</div>' +
      '<button class="go" data-testbattle="open">Открыть боевую песочницу</button></div>' +
    // Above everything, including PVE: it is one day, and the point of it is that
    // nobody has to go looking.
    birthdayPanel() +
    // PVE above the roster: there is always a mob, and on a quiet day the player list is
    // the empty half of this screen.
    mobPanel(Boolean(farming)) +
    '<div class="panel"><h2>Соперники · ' + FOES.opponents.length + "</h2>" +
      (FOES.opponents.length
        ? FOES.opponents.map((foe) => foeRow(foe, !blocked)).join("")
        : '<div class="empty">Больше ни у кого нет существа.</div>') +
    "</div>";
  paintShots(box);
}

// --------------------------------------------------------- turn-based test battle
function testOptions(rows, selected) {
  return (rows || []).map((row) => '<option value="' + esc(row.code) + '"' +
    (row.code === selected ? " selected" : "") + '>' + esc(row.icon || "📜") + " " +
    esc(row.name) + " · " + esc(scrollElement(row)) + " · один раз за бой" +
    (row.dodgeable === false ? " · нельзя увернуться" : "") + "</option>").join("");
}

function testSelect(id, label, rows, selected) {
  return '<label class="small"><b>' + label + '</b><select class="test-select" id="' + id + '">' +
    testOptions(rows, selected) + "</select></label>";
}

// One corner of the stage overlay. Name, numbers and bar, mirrored for the enemy so the
// two read outward from the middle rather than both crowding the left edge.
function testHudSide(fighter, side) {
  const pct = Math.max(0, Math.min(100, 100 * fighter.hp / Math.max(1, fighter.max_hp)));
  return '<div data-side="' + side + '">' +
    '<div class="who">' + esc(fighter.name || "—") + "</div>" +
    '<div class="num">' + fighter.hp + " / " + fighter.max_hp +
      (fighter.barrier ? " · 🛡" + fighter.barrier : "") + "</div>" +
    '<div class="hpbar"><i style="width:' + pct + '%"></i></div></div>';
}

// --------------------------------------------------------------------- battle sprites
// Turns a fighter's flat portrait photo into an animated element via sprite.css: a
// .sprite wrapper carrying the archetype (data-kind, picked by sprite.css to choose an
// idle loop) and which way this fighter's attacks travel (--dir, see sprite.css for why).
// Everything here assumes the page's existing esc() and shot() helpers are already
// defined (esc: HTML-escaper: shot(url, crop, extra): renders a cropped image into a
// ".shot" box) and does not redefine either.

// The archetype contract. Kept as a Set (not read off the CSS) because the two have to
// agree on the same fixed list either way, and checking against a literal list here is
// what lets a bad or missing value be caught and swapped for "creature" BEFORE it ever
// reaches the DOM -- an unrecognized data-kind has no matching keyframes in sprite.css,
// which would leave that fighter's photo frozen for the whole fight instead of merely
// defaulting to the plain idle.
const SPRITE_KINDS = new Set([
  "humanoid", "quadruped", "bird", "insect", "aquatic", "reptile",
  "blob", "machine", "vehicle", "plant", "spirit", "creature",
]);

// side is "player" or "enemy"; anything else quietly becomes "player" rather than
// producing a sprite that no selector or querySelector call can ever find again.
function spriteMarkup(fighter, side) {
  try {
    const f = fighter || {};
    const kind = SPRITE_KINDS.has(f.kind) ? f.kind : "creature";
    const sideKey = side === "enemy" ? "enemy" : "player";
    const dir = sideKey === "enemy" ? -1 : 1;
    const body = f.portrait
      ? shot(f.portrait, f.crop || null)
      : '<span class="sprite-fallback">🗿</span>';   // a fighter with no photograph yet
    // data-owner is how the late-arriving classification finds this element again: the
    // archetype is worked out by /api/sprite seconds after the battle has already opened,
    // and applySpriteKind swaps data-kind on whatever is on screen by then.
    const owner = f.owner_id ? ' data-owner="' + esc(String(f.owner_id)) + '"' : "";
    return '<span class="sprite" data-kind="' + kind + '" data-side="' + sideKey +
      '"' + owner + ' style="--dir:' + dir + '">' + body + "</span>";
  } catch (e) {
    // A blank slot beats an exception that blanks the rest of the battle screen.
    return "";
  }
}

// Re-triggers a one-shot CSS animation class on a sprite. Reading offsetWidth between the
// remove and the re-add forces the browser to flush style before the class comes back --
// skip that and the browser coalesces the remove+add into a no-op, so calling this twice
// in a row (two quick hits) would only animate once.
function playSpriteAction(side, action) {
  try {
    if (action !== "lunge" && action !== "hurt" && action !== "ko") return;
    const sideKey = side === "enemy" ? "enemy" : side === "player" ? "player" : null;
    if (!sideKey) return;
    const el = document.querySelector('.sprite[data-side="' + sideKey + '"]');
    if (!el) return;
    el.classList.remove(action);
    void el.offsetWidth; // force reflow, see comment above
    el.classList.add(action);
    if (action === "ko") return; // ko is a settled pose, left on rather than cleaned up
    const clear = () => el.classList.remove(action);
    el.addEventListener("animationend", clear, { once: true });
    // animationend can fail to fire (backgrounded tab, reduced-motion swapping the
    // duration mid-flight) -- a leftover class would leave the sprite permanently offset
    // or tinted, so give it a timeout well past even the un-reduced lunge duration.
    setTimeout(clear, 900);
  } catch (e) {
    // Never let a display-only animation trip up the battle screen it decorates.
  }
}

// Kinds of log row that land as a hit worth flinching for. Mirrors the harmful-effect set
// in pets_test_combat.py loosely (not imported -- the client only needs a yes/no read of
// "did this look like a strike", not the real game rule), so a self-inflicted cost (a
// fighter paying their own HP for a spell) can occasionally misread as a hit landing on
// the opponent. That is an acceptable approximation: the log row only carries who ACTED,
// never who was targeted, so there is no field here that could disambiguate it further.
const SPRITE_HARMFUL_KINDS = {
  damage: 1, burn: 1, weaken: 1, blind: 1, vulnerable: 1, stun: 1, break: 1, reflect: 1,
};

// events is one turn's worth of battle log rows ({ kind, actor, ... }). Sequences the
// animation as: whoever acted this turn lunges immediately, and if something harmful
// actually landed (and nobody dodged it), the other side flinches about 180ms later --
// long enough that the lunge visibly arrives first, short enough that the two still read
// as one exchange rather than two unrelated events.
function playSpriteExchange(events) {
  try {
    const rows = Array.isArray(events) ? events : [];
    let attacker = null;
    for (const row of rows) {
      const actor = row && row.actor;
      if (actor === "player" || actor === "enemy") { attacker = actor; break; }
    }
    if (!attacker) return;
    const target = attacker === "player" ? "enemy" : "player";
    const dodged = rows.some((row) => row && row.kind === "dodge");
    const landed = !dodged && rows.some((row) =>
      row && row.actor === attacker && SPRITE_HARMFUL_KINDS[row.kind]);
    playSpriteAction(attacker, "lunge");
    if (landed) setTimeout(() => playSpriteAction(target, "hurt"), 180);
  } catch (e) {
    // Same rule as playSpriteAction: this is sequencing sugar, never a hard dependency.
  }
}

// The last turn number whose blows have already been played. The battle screen re-renders
// its whole markup after every action, so without this the sprites would replay the entire
// fight from the top on every tap.
let SPRITE_PLAYED_TURN = -1;

// One turn's worth of movement. The engine's log is the source of truth: each row carries
// `turn`, `actor` ("player"/"enemy") and `kind`, so the attacker is simply whoever owns the
// blow, and the target is the other one. Anything that is not a blow (a heal, a barrier,
// the start line) moves nobody -- the brief was that attacking is the only motion.
function animateTurn(log, finished, winner) {
  const rows = Array.isArray(log) ? log : [];
  const turn = rows.length ? Number(rows[rows.length - 1].turn || 0) : 0;
  if (turn === SPRITE_PLAYED_TURN) return;
  SPRITE_PLAYED_TURN = turn;
  // playSpriteExchange knows how to read one turn's rows -- which of them is a real strike
  // and whether it was dodged. All this has to do is hand it the right turn.
  playSpriteExchange(rows.filter((row) => Number(row.turn || 0) === turn));
  if (finished && winner) {
    setTimeout(() => playSpriteAction(winner === "player" ? "enemy" : "player", "ko"), 420);
  }
}

// Which archetypes the page has already asked about, so opening a battle does not fire the
// same classification request on every re-render. A battle re-renders after every action.
const SPRITE_KIND = {};
let SPRITE_ASKED = {};

function applySpriteKind(userId, kind) {
  SPRITE_KIND[userId] = kind;
  // Written into the battle state as well as onto the live element. The screen rebuilds
  // its markup from that state after every action, so touching only the DOM would show
  // the right idle until the next tap and then silently fall back to the neutral one.
  if (TEST_BATTLE && TEST_BATTLE.fighters) {
    for (const fighter of Object.values(TEST_BATTLE.fighters)) {
      if (fighter && String(fighter.owner_id) === String(userId)) fighter.kind = kind;
    }
  }
  document.querySelectorAll('.sprite[data-owner="' + CSS.escape(String(userId)) + '"]')
    .forEach((node) => { node.dataset.kind = kind; });
}

// The battle opens on the neutral idle and upgrades itself a moment later. Classification
// is a round trip to a vision model and takes seconds; making the screen wait for it would
// trade a real animation for a blank stage, and the fallback idle is perfectly watchable.
// Generated frames, once they exist: user_id -> [{name, url}]. A creature with frames
// flipbooks them; a creature without keeps breathing as a whole photograph under CSS.
// Both are real sprites as far as the rest of the screen is concerned.
const SPRITE_FRAMES = {};

function applySpriteFrames(userId, frames) {
  if (!frames || !frames.length) return;
  SPRITE_FRAMES[userId] = frames;
  if (TEST_BATTLE && TEST_BATTLE.fighters) {
    for (const fighter of Object.values(TEST_BATTLE.fighters)) {
      if (fighter && String(fighter.owner_id) === String(userId)) fighter.frames = frames;
    }
  }
  // Repainted in place rather than by re-rendering the screen: a render mid-fight would
  // restart the idle of BOTH creatures and replay the turn animation.
  document.querySelectorAll('.sprite[data-owner="' + CSS.escape(String(userId)) + '"]')
    .forEach(paintSpriteFrames);
}

// Swaps the single photograph for the generated frames, stacked and cross-faded by CSS.
// The archetype idle stays on the wrapper underneath, so a flipbooked creature still
// bobs like the animal it is -- the frames carry the breath, the transform carries the
// bounce, and neither has to know about the other.
function paintSpriteFrames(node) {
  const frames = SPRITE_FRAMES[node.dataset.owner];
  if (!frames || node.dataset.framed === "1") return;
  node.dataset.framed = "1";
  node.innerHTML = frames.map((frame, index) =>
    '<img class="frame" data-frame="' + esc(frame.name) + '" src="' + esc(frame.url) +
    '" alt="" style="animation-delay:' + (index * -1.1) + 's">').join("");
}

// Asks about both fighters once per page load, then keeps asking while a generation is
// still running. Four model calls take tens of seconds, so the battle opens on whatever
// exists and improves itself underneath the player -- polling is the only honest way to
// notice, since nothing pushes to this page.
async function requestSpriteKinds() {
  if (!TEST_BATTLE) return;
  const wanted = [TEST_BATTLE.fighters.player, TEST_BATTLE.fighters.enemy]
    .map((f) => f && f.owner_id)
    .filter((id) => id && !SPRITE_ASKED[id]);
  for (const id of wanted) {
    SPRITE_ASKED[id] = true;
    pollSprite(id, 0);
  }
}

async function pollSprite(userId, attempt) {
  let data;
  try {
    data = await api("/api/sprite?user_id=" + encodeURIComponent(userId));
  } catch (e) {
    return;                    // the photograph keeps breathing; not worth a visible error
  }
  if (data.kind) applySpriteKind(userId, data.kind);
  if (data.frames && data.frames.length) { applySpriteFrames(userId, data.frames); return; }
  // Give up after about two minutes. A generation that has not finished by then has
  // almost certainly failed, and a page left polling forever is a slow leak.
  if (data.status === "pending" && attempt < 12) {
    setTimeout(() => pollSprite(userId, attempt + 1), 10000);
  }
}

function testBattleLog(rows) {
  return (rows || []).slice(-18).map((row) =>
    '<div class="test-event ' + esc(row.kind || "") + '">' + esc(row.text || "") + "</div>"
  ).join("");
}

function renderTestBattle(box) {
  if (!TEST_BATTLE) {
    if (!TEST_SETUP) { box.innerHTML = '<div class="empty">Загружаю песочницу…</div>'; return; }
    const defaults = TEST_SETUP.defaults || {};
    const foes = TEST_SETUP.opponents || [];
    box.innerHTML =
      '<div class="test-banner"><b>🧪 Это отдельный тест.</b><br>Он читает характеристики питомцев, ' +
        'но не тратит бои, не выдаёт награды и ничего не пишет в историю.</div>' +
      '<div class="panel"><h2>Соперник</h2><select class="test-select" id="testOpponent">' +
        foes.map((foe) => '<option value="' + esc(foe.user_id) + '">' + esc(foe.name) +
          " · " + esc(foe.owner_name || "") + "</option>").join("") + '</select></div>' +
      '<div class="panel"><h2>Четыре слота свитков</h2><div class="test-loadout">' +
        testSelect("testSkill1", "1 · свиток", TEST_SETUP.regular_scrolls, defaults.skills[0]) +
        testSelect("testSkill2", "2 · свиток", TEST_SETUP.regular_scrolls, defaults.skills[1]) +
        testSelect("testSkill3", "3 · свиток", TEST_SETUP.regular_scrolls, defaults.skills[2]) +
        testSelect("testSkill4", "4 · УЛЬТИМЕЙТ · один раз", TEST_SETUP.ultimate_scrolls, defaults.skills[3]) +
        testSelect("testShield", "🛡 Щит · эффект срабатывает при защите", TEST_SETUP.shields,
                   defaults.shield) + '</div>' +
        '<button class="go sec" style="margin-top:10px" data-testcatalog>📚 Читать весь каталог</button></div>' +
      '<div class="panel"><h2>Режим</h2><div class="test-actions">' +
        '<button class="go" data-testmode="manual">🎮 Играть самому</button>' +
        '<button class="go sec" data-testmode="auto">🎲 Автоматический</button>' +
        '<button class="go sec" data-testmode="multiplayer" style="grid-column:1/-1">' +
          '👥 Два игрока · скоро</button></div></div>' +
      '<button class="go sec" data-testbattle="close">◀️ Вернуться на арену</button>';
    return;
  }

  const mine = TEST_BATTLE.fighters.player;
  const foe = TEST_BATTLE.fighters.enemy;
  const legal = new Set(TEST_BATTLE.legal_actions || []);
  const result = TEST_BATTLE.finished
    ? (TEST_BATTLE.draw ? "Ничья" : TEST_BATTLE.winner === "player" ? "Победа" : "Поражение")
    : "Ход " + TEST_BATTLE.turn;
  // One cell per action, four across, so everything fits two rows on a phone. The label
  // is the shortest thing that still identifies the scroll; its full rules text is one tap
  // away in the catalogue, which is where somebody reads rather than plays.
  const cell = (action, icon, label, note, extra) =>
    '<button class="go ' + (extra || "sec") + ' test-action" data-testaction="' + action + '"' +
    (legal.has(action) ? "" : " disabled") + '><span class="ic">' + esc(icon) + "</span>" +
    esc(label) + (note ? "<small>" + esc(note) + "</small>" : "") + "</button>";
  const slotButtons = (mine.slots || []).map((slot) => {
    const short = String(slot.name || "").replace(/^.*?: /, "");
    return '<button class="go sec test-action' + (slot.ultimate ? " ultimate" : "") +
      (slot.available ? "" : " spent") +
      '" data-testaction="skill_' + slot.slot + '"' +
      (legal.has("skill_" + slot.slot) ? "" : " disabled") + '><span class="ic">' +
      esc(slot.icon) + "</span>" + esc(short.slice(0, 16)) +
      "<small>" + esc(slot.available ? scrollElement(slot) : "использован") +
      "</small></button>";
  }).join("");

  box.innerHTML =
    '<div class="test-banner"><b>🧪 Тестовый бой</b> · ничего не тратится и никуда не пишется.</div>' +
    // The stage carries the health bars and the turn counter itself, so the fight stays
    // one object on screen and the controls sit directly under the thumb.
    '<div class="test-stage" id="testStage">' +
      '<div class="test-hud">' + testHudSide(mine, "player") + testHudSide(foe, "enemy") + "</div>" +
      '<div class="test-turn">' + esc(result) + "</div>" +
      '<div class="test-spot" data-side="player">' + spriteMarkup(mine, "player") + "</div>" +
      '<div class="test-spot" data-side="enemy">' + spriteMarkup(foe, "enemy") + "</div>" +
    "</div>" +
    (TEST_BATTLE.finished
      ? '<div class="test-actions" style="margin-top:9px">' +
        '<button class="go test-action" data-testbattle="restart" style="grid-column:span 2">' +
          '<span class="ic">⚙️</span>Новый бой</button>' +
        '<button class="go sec test-action" data-testbattle="close" style="grid-column:span 2">' +
          '<span class="ic">◀️</span>На арену</button></div>'
      : '<div class="test-actions" style="margin-top:9px">' +
        cell("attack", "⚔️", "Атака", "удар", "") +
        cell("defend", "🛡", "Защита", String(mine.shield.name || "").slice(0, 14)) +
        slotButtons +
        cell("auto", "🎲", "Авто", "до конца") +
        '<button class="go sec test-action" data-testbattle="close">' +
          '<span class="ic">◀️</span>Выход</button></div>') +
    '<div class="panel" style="margin-top:9px"><h2>Ход боя</h2><div class="test-log" id="testLog">' +
      testBattleLog(TEST_BATTLE.log) + "</div></div>";
  paintShots(box);
  const log = $("testLog"); if (log) log.scrollTop = log.scrollHeight;
  // Played after the markup lands, so the elements the animation looks for exist. The
  // turn that just resolved is whatever is new at the end of the log.
  document.querySelectorAll(".sprite").forEach(paintSpriteFrames);
  animateTurn(TEST_BATTLE.log, TEST_BATTLE.finished, TEST_BATTLE.winner);
  requestSpriteKinds();
}

async function openTestBattle() {
  if (TEST_BUSY) return;
  TEST_BUSY = true;
  TEST_BATTLE = null; TEST_SESSION = null; TEST_MODE = null;
  try { TEST_SETUP = await api("/api/test-battle"); }
  catch (e) { haptic("no"); toast(e.message); return; }
  finally { TEST_BUSY = false; }
  render();
}

async function startTestBattle(mode) {
  if (TEST_BUSY) return;
  const skills = [1, 2, 3, 4].map((index) => $("testSkill" + index).value);
  if (new Set(skills.slice(0, 3)).size !== 3) {
    haptic("no"); toast("В первые три слота нужны разные свитки."); return;
  }
  TEST_BUSY = true;
  try {
    const data = await api("/api/test-battle/start", {
      mode, opponent_id: $("testOpponent").value, shield: $("testShield").value, skills,
    });
    if (data.status === "coming_soon") { toast(data.message); return; }
    TEST_BATTLE = data.battle; TEST_SESSION = data.session; TEST_MODE = data.mode;
    SPRITE_PLAYED_TURN = -1;           // a new fight replays from its own first blow
    haptic(TEST_BATTLE.finished && TEST_BATTLE.winner === "player" ? "ok" : undefined);
    render();
  } catch (e) { haptic("no"); toast(e.message); }
  finally { TEST_BUSY = false; }
}

async function testBattleAction(action) {
  if (!TEST_SESSION || TEST_BUSY) return;
  TEST_BUSY = true;
  try {
    const data = await api("/api/test-battle/action", { session: TEST_SESSION, action });
    TEST_BATTLE = data.battle; TEST_MODE = data.mode;
    if (data.message) toast(data.message);
    haptic(TEST_BATTLE.finished ? (TEST_BATTLE.winner === "player" ? "ok" : "no") : undefined);
    render();
  } catch (e) { haptic("no"); toast(e.message); }
  finally { TEST_BUSY = false; }
}

function showTestCatalog() {
  const spells = [...(TEST_SETUP.regular_scrolls || []), ...(TEST_SETUP.ultimate_scrolls || [])];
  sheet('<h3>📚 Свитки и щиты</h3><p class="tiny muted">Каждый выбранный свиток применяется один раз за бой. ' +
    'Свойства берутся из редактируемой серверной таблицы.</p>' + spells.map((spell) =>
      '<div class="panel"><b>' + esc(spell.icon) + " " + esc(spell.name) + '</b><div class="small">' +
      esc(spell.short) + '</div>' + scrollEffects(spell) +
      '<div class="tiny muted">один раз за бой · ' + esc(scrollElement(spell)) +
      (spell.dodgeable === false ? " · нельзя увернуться" : "") +
      "</div></div>").join("") + '<h3>🛡 Щиты</h3>' + (TEST_SETUP.shields || []).map((shield) =>
      '<div class="panel"><b>' + esc(shield.icon) + " " + esc(shield.name) + '</b><div class="small">' +
      esc(shield.short) + "</div>" + scrollEffects(shield) + "</div>").join(""));
}

// ------------------------------------------------------------------------------- PVE
const TIER_TONE = { easy: "win", medium: "gold", hard: "loss" };
let MOB = null;

async function rollMob() {
  try {
    MOB = (await api("/api/mob")).mob;
  } catch (e) { haptic("no"); toast(e.message); return; }
  haptic();
  render();
}

async function fightMob() {
  if (!MOB) return;
  let data;
  try {
    data = await api("/api/mob", { code: MOB.code, tier: MOB.tier });
  } catch (e) { haptic("no"); toast(e.message); MOB = null; render(); return; }
  S = data.state;
  MOB = null;
  FOES = null;
  playDuel(data);
}

function birthdayAdmin(data) {
  const party = data.birthday;
  const rows = data.candidates || [];
  const current = party
    ? '<div class="panel"><h2>🎂 Сегодня празднует</h2>' +
      "<div class='row spread'><span><b>" + esc(party.owner_name) + "</b>" +
      (party.pet_name ? " <span class='tiny muted'>· " + esc(party.pet_name) + "</span>" : "") +
      "</span><span class='small'>поздравили: " + Number(party.greeted_count || 0) +
      "</span></div>" +
      '<button class="go sec" style="margin-top:10px" data-birthdayclear="1">Снять праздник</button>' +
      "</div>"
    : '<div class="panel"><h2>🎂 День рождения</h2>' +
      "<div class='small muted'>Сегодня никто не празднует. Выбери именинника — он встанет " +
      "первым на арене у всех, вместо кнопки атаки будет «Поздравить», и награду получат " +
      "оба: и поздравивший, и именинник.</div></div>";
  // Set for TODAY only, deliberately: the row carries its date and retires itself at
  // midnight, so a forgotten celebration cannot keep paying out all week.
  return current +
    '<div class="panel"><h2>Кого поздравляем</h2>' +
    '<input class="inp" data-birthdayfilter placeholder="Поиск по имени">' +
    "<div class='tiny muted' style='margin:8px 0'>Назначается на сегодня (" +
      esc(data.today || "") + "). Завтра снимется само.</div>" +
    (rows.length
      ? rows.map((row) =>
          // The searchable text rides on the row so filtering can hide and show rows in
          // place, the way the audit filter does -- re-rendering the list on every
          // keystroke would take the focus out of the box being typed into.
          '<div class="row spread" style="margin-bottom:8px" data-bdayrow="' +
          esc(((row.owner_name || "") + " " + (row.owner_username || "")).toLowerCase()) +
          '"><span class="small">' + esc(row.owner_name) +
          (row.owner_username ? " <span class='tiny muted'>@" + esc(row.owner_username) + "</span>" : "") +
          "</span>" +
          '<button class="chip" data-birthdayset="' + esc(row.user_id) + '"' +
          (party && party.user_id === row.user_id ? " disabled" : "") + ">" +
          (party && party.user_id === row.user_id ? "выбран" : "выбрать") + "</button></div>").join("")
      : '<div class="empty">Пока ни у кого нет существа.</div>') +
    "</div>";
}

async function setBirthday(userId) {
  try {
    const data = await api("/api/birthday", userId ? { user_id: userId } : { clear: true });
    toast(data.message || "Готово");
    haptic("ok");
    FOES = null;                       // the arena card has to be rebuilt for everyone
    render();
  } catch (e) { haptic("no"); toast(e.message); }
}

// Which mark the next tap on «выдать» hands out. Only ever one is defined today, so this
// starts on it and the picker only earns its keep once there are two.
let debuffPick = "";

function debuffAdmin(data) {
  const marks = data.debuffs || [];
  const holders = data.holders || [];
  const rows = data.candidates || [];
  if (!marks.some((m) => m.code === debuffPick)) debuffPick = (marks[0] || {}).code || "";
  const chosen = marks.find((m) => m.code === debuffPick) || {};

  // The admin reads exactly the copy the player will read, straight off the catalogue --
  // picking a punishment by its machine code is how the wrong one gets handed out.
  const picker = '<div class="panel"><h2>🎭 Эффекты игрокам</h2>' +
    marks.map((mark) =>
      '<button class="go sec" style="margin-bottom:8px;text-align:left"' +
      (mark.code === debuffPick ? " disabled" : "") +
      ' data-debuffpick="' + esc(mark.code) + '">' +
      esc(mark.emoji) + " " + esc(mark.title) + " · " + esc(mark.line) + "</button>").join("") +
    debuffNote(chosen) +
    "</div>";

  const current = '<div class="panel"><h2>Сейчас висит · ' + holders.length + "</h2>" +
    (holders.length
      ? holders.map((row) =>
          "<div class='row spread' style='margin-bottom:8px'><span class='small'>" +
          esc(row.owner_name) + " " + debuffTag(row) +
          (row.pet_name ? "<br><span class='tiny muted'>" + esc(row.pet_name) + "</span>" : "") +
          "</span><button class='chip' data-debuffclear='" + esc(row.user_id) +
          "'>снять</button></div>").join("") +
        "<div class='tiny muted' style='margin-top:6px'>Снимется и само, как только игрок " +
        "поменяет картинку существа.</div>"
      : "<div class='small muted'>Пока ни на ком.</div>") +
    "</div>";

  return picker + current +
    '<div class="panel"><h2>Кому выдать</h2>' +
    '<input class="inp" data-debufffilter placeholder="Поиск по имени">' +
    "<div class='tiny muted' style='margin:8px 0'>Выдаётся " + esc(chosen.title || "эффект") +
      ". Запоминается картинка, которая стоит сейчас — сменит её, и эффект спадёт.</div>" +
    (rows.length
      ? rows.map((row) =>
          // Same in-place filtering the birthday list uses: re-rendering on every
          // keystroke would take the focus out of the box being typed into.
          '<div class="row spread" style="margin-bottom:8px" data-dbfrow="' +
          esc(((row.owner_name || "") + " " + (row.owner_username || "")).toLowerCase()) +
          '"><span class="small">' + esc(row.owner_name) +
          (row.owner_username ? " <span class='tiny muted'>@" + esc(row.owner_username) + "</span>" : "") +
          (row.has_photo ? "" : " <span class='tiny muted'>· без картинки</span>") +
          "</span>" +
          '<button class="chip" data-debuffset="' + esc(row.user_id) + '">выдать</button></div>').join("")
      : '<div class="empty">Пока ни у кого нет существа.</div>') +
    "</div>";
}

async function setDebuff(userId, clear) {
  try {
    const data = await api("/api/debuff",
      clear ? { user_id: userId, clear: true } : { user_id: userId, code: debuffPick });
    toast(data.message || "Готово");
    haptic("ok");
    FOES = null;                       // power ratings on the roster have just moved
    render();
  } catch (e) { haptic("no"); toast(e.message); }
}

const PEEKED = {};                 // user_id -> loadout, so reopening never refetches

function statLine(stats) {
  return ["strength", "health", "agility", "luck", "armor"]
    .filter((key) => Number(stats[key] || 0))
    .map((key) => (STAT_ICON[key] || key) + " " + Number(stats[key] || 0))
    .join("  ");
}

function peekItem(slot) {
  const item = slot.item;
  if (!item) {
    return '<div class="peekrow empty-slot"><b>' + esc(slot.emoji) + " " + esc(slot.name) +
           "</b><span class='tiny muted'>пусто</span></div>";
  }
  const bonuses = Object.entries(item.bonuses || {})
    .map(([key, value]) => (STAT_ICON[key] || key) + " " + (value > 0 ? "+" : "") + value)
    .join("  ");
  const effect = (item.effect || {}).text;
  return '<div class="peekrow r-' + esc(item.rarity) + '">' +
    "<div class='row spread'><b>" + esc(slot.emoji) + " " + esc(item.name) + "</b>" +
    "<span class='tiny muted'>" + esc(item.rarity_name || "") + "</span></div>" +
    (bonuses ? "<div class='small stats'>" + esc(bonuses) + "</div>" : "") +
    (effect ? "<div class='small fxline'>" + esc(effect) + "</div>" : "") +
    (item.description ? "<div class='tiny muted desc'>" + esc(item.description) + "</div>" : "") +
    "</div>";
}

function peekPanel(data) {
  const scrolls = (data.skills || []).filter((row) => !row.empty);
  return "<div class='row spread' style='margin-bottom:8px'>" +
      "<span class='small'><b>" + esc(data.name || "—") + "</b> · ур. " + Number(data.level || 1) +
      "</span><span class='pw'>⚡ " + money(data.power) + "</span></div>" +
    "<div class='tiny muted' style='margin-bottom:9px'>" + esc(statLine(data.stats || {})) +
      " · боёв " + Number(data.fights || 0) + ", побед " + Number(data.wins || 0) + "</div>" +
    // Between the stats and the gear, because the stats printed above are the reduced
    // ones and anybody comparing them against the same build on somebody else deserves
    // to know why they do not add up.
    debuffNote(data.debuff) +
    (data.slots || []).map(peekItem).join("") +
    (scrolls.length
      ? "<div class='tiny muted' style='margin-top:9px'>📜 Свитки</div>" +
        scrolls.map((row) => "<div class='peekrow'><b>" + esc(row.icon) + " " +
          esc(row.name) + "</b>" + (row.effects_text || []).map((line) =>
            "<div class='small fxline'>" + esc(line) + "</div>").join("") + "</div>").join("")
      : "<div class='tiny muted' style='margin-top:9px'>Свитков пока нет.</div>");
}

async function togglePeek(userId) {
  const box = document.querySelector('[data-peekbody="' + CSS.escape(userId) + '"]');
  if (!box) return;
  if (!box.hidden) { box.hidden = true; return; }
  box.hidden = false;
  if (PEEKED[userId]) { box.innerHTML = peekPanel(PEEKED[userId]); return; }
  box.innerHTML = "<div class='tiny muted'>Загружаю…</div>";
  try {
    PEEKED[userId] = await api("/api/loadout?user_id=" + encodeURIComponent(userId));
    box.innerHTML = peekPanel(PEEKED[userId]);
  } catch (e) {
    box.innerHTML = "<div class='tiny muted'>" + esc(e.message) + "</div>";
  }
}

function birthdayPanel() {
  const party = FOES && FOES.birthday;
  if (!party) return "";
  const who = esc(party.owner_name || "именинник");
  const count = Number(party.greeted_count || 0);
  const tally = count
    ? "<div class='tiny muted' style='margin-top:8px;text-align:center'>Уже поздравили: " +
      count + "</div>"
    : "";
  // Three different people read this card: the one with the birthday, the one who has
  // already congratulated them, and everybody else. Only the last of those gets a button.
  let action;
  if (party.is_me) {
    action = "<div class='small' style='text-align:center'>Сегодня твой день. " +
      "Тебя поздравляют на арене — каждому поздравившему и тебе идёт награда.</div>";
  } else if (party.greeted) {
    action = "<div class='small' style='text-align:center'>✅ Ты уже поздравил" +
      "</div>";
  } else {
    action = '<button class="go" data-congratulate="1">🎉 Поздравить</button>' +
      "<div class='tiny muted' style='margin-top:7px;text-align:center'>" +
      "Бой это не тратит. Награду получите оба.</div>";
  }
  return '<div class="panel birthday"><h2>🎂 День рождения · ' + who + "</h2>" +
    (party.pet_name
      ? "<div class='tiny muted' style='margin-bottom:9px'>Существо: " +
        esc(party.pet_name) + "</div>"
      : "") +
    action + tally + "</div>";
}

async function congratulate() {
  try {
    const data = await api("/api/congratulate", {});
    S = data.state;
    FOES = null;                       // the card has to come back as "уже поздравил"
    haptic("ok");
    toast(data.receipt && data.receipt.already
      ? "Ты уже поздравил сегодня."
      : "Поздравление отправлено. +" + Number((data.receipt || {}).gold || 0) + " золота");
    render();
  } catch (e) { haptic("no"); toast(e.message); }
}

function mobPanel(farmBlocked) {
  const pve = (S && S.pve) || {};
  const left = pve.available != null ? pve.available : 0;
  // PVE has its own counter, so it is blocked by its own emptiness -- not by the arena's.
  const blocked = farmBlocked || left <= 0;
  const counter = '<div class="row spread tiny" style="margin-bottom:9px">' +
    "<span class='muted'>Атаки на мобов</span><span><b>" + left + "</b> / " +
    (pve.capacity || 0) + (pve.seconds_until_reset
      ? " <span class='muted'>· сброс через " + clock(pve.seconds_until_reset) + "</span>"
      : "") + "</span></div>";
  if (!MOB) {
    return '<div class="panel"><h2>👾 ПВЕ · мобы</h2>' + counter +
      "<div class='small muted' style='margin-bottom:9px'>Соперник из реального мира. " +
      "Платят вдвое меньше, чем за игрока, зато бои для них свои — арену они не " +
      "тратят, — и только с них падают <b>руби</b>.</div>" +
      (left <= 0
        ? "<div class='tiny muted' style='text-align:center'>Атаки кончились. " +
          "Новые придут сразу у всех на сервере.</div>"
        : '<button class="go" data-mob="roll"' + (blocked ? " disabled" : "") +
          ">🔍 Найти моба</button>") + "</div>";
  }
  return '<div class="panel mobcard"><h2>👾 ' + esc(MOB.name) + "</h2>" + counter +
    "<div class='row spread'><span class='tiny muted'>" + esc(MOB.flavour) + "</span>" +
    '<span class="tierchip ' + (TIER_TONE[MOB.tier] || "") + '">' + esc(MOB.tier_name) +
    "</span></div>" +
    "<div class='mobtaunt'>" + esc(MOB.taunt) + "</div>" +
    "<div class='row spread small' style='margin-top:9px'><span class='pw'>⚡ " +
      money(MOB.power) + "</span><span class='tiny muted'>" +
      ["strength", "health", "agility", "luck"].map((key) =>
        (STAT_ICON[key] || key) + (MOB.stats[key] || 0)).join(" ") +
      (MOB.armor ? " 🛡" + MOB.armor : "") + "</span></div>" +
    '<div class="row" style="margin-top:10px;gap:8px">' +
      '<button class="go" data-mob="fight"' + (blocked ? " disabled" : "") + ">⚔️ В бой</button>" +
      '<button class="go sec" data-mob="roll" style="flex:0 0 42%">🔍 Другой</button>' +
    "</div></div>";
}

// A granted mark, drawn the same way wherever it appears. Three shapes for three amounts
// of room, but the rule is the same in all of them: the mark never appears without the
// line that says what it costs and the line that says how to be rid of it. Muted, small
// and below the name -- the brief was "на видном месте, но не кричит в лицо", and a mark
// somebody else can see is embarrassing enough without a red banner helping.
function debuffTag(mark) {
  if (!mark) return "";
  return "<span class='dbf' title='" + esc(mark.description || "") + "'>" +
    esc(mark.emoji || "") + " " + esc(mark.title || "") + "</span>";
}

function debuffNote(mark, extra) {
  if (!mark) return "";
  return "<div class='dbfnote" + (extra ? " " + extra : "") + "'>" +
    "<div class='dbfhead'>" + esc(mark.emoji || "") + " <b>" + esc(mark.title || "") + "</b>" +
      (mark.line ? " <span class='dbfcost'>" + esc(mark.line) + "</span>" : "") + "</div>" +
    (mark.description ? "<div class='tiny muted'>" + esc(mark.description) + "</div>" : "") +
    (mark.hint ? "<div class='tiny muted' style='margin-top:4px'>" + esc(mark.hint) + "</div>" : "") +
    "</div>";
}

function foeRow(foe, canFight) {
  const usable = canFight && foe.attackable;
  return '<button class="foe' + (usable ? "" : " out") + '" data-foe="' + esc(foe.user_id) + '"' +
    (usable ? "" : " disabled") + '>' +
    '<span class="av">' + shot(foe.portrait, foe.crop) + "</span>" +
    "<span><b>" + esc(foe.name || "Существо") + "</b> <span class='muted small'>ур. " + foe.level +
      "</span> " + debuffTag(foe.debuff) +
      "<br><span class='tiny muted'>" + esc(foe.owner_name || "") +
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
      "<p class='small muted'>🎟 Билетов: " + (farm.tickets || 0) + "</p>" +
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
      // Above «Забрать сейчас» and in the accent colour, because the two buttons answer
      // the same impatience and only one of them costs you the payout.
      (farm.can_ticket
        ? '<button class="go" style="margin-top:10px" data-do="farmticket">🎟 Билет · закончить смену (' +
          (farm.tickets || 0) + ")</button>" +
          "<div class='tiny muted' style='margin-top:6px;text-align:center'>Заплатят как за все " +
          (farm.planned_hours || 0) + " ч</div>"
        : (farm.tickets
          ? "<div class='tiny muted' style='margin-top:8px;text-align:center'>🎟 Билетов: " +
            farm.tickets + "</div>"
          : "")) +
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
      '<div class="small muted" style="margin-top:4px">🎟 Билетов: ' + (farm.tickets || 0) + "</div>" +
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
let auditUser = "";
let auditHours = 24;
let auditFilter = "";
let auditData = null;

function signedMoney(value) {
  const number = Number(value || 0);
  return (number > 0 ? "+" : number < 0 ? "−" : "") + money(Math.abs(number));
}

function auditUserLabel(user) {
  return (user.username ? "@" + user.username + " · " : "") + user.name +
    (user.pet_name ? " · " + user.pet_name : "") + " · ID " + user.user_id;
}

function filteredAuditUsers() {
  const users = (auditData && auditData.users) || [];
  const needle = auditFilter.trim().toLowerCase();
  if (!needle) return users;
  return users.filter((user) => [
    user.name, user.username, user.username ? "@" + user.username : "",
    user.pet_name, user.user_id,
  ].filter(Boolean).join(" ").toLowerCase().includes(needle));
}

function auditUserOptions(users, selected) {
  const selectedVisible = users.some((user) => String(user.user_id) === String(selected));
  const placeholder = selectedVisible ? "" : '<option value="" selected disabled>' +
    (users.length ? "Выбери найденного игрока" : "Никого не найдено") + "</option>";
  return placeholder + users.map((user) =>
    '<option value="' + esc(user.user_id) + '"' +
      (String(user.user_id) === String(selected) ? ' selected' : '') + '>' +
      esc(auditUserLabel(user)) + '</option>'
  ).join("");
}

function refreshAuditUserFilter() {
  const select = document.querySelector("[data-audituser]");
  if (!select) return;
  const users = filteredAuditUsers();
  select.innerHTML = auditUserOptions(users, auditData && auditData.selected);
  select.disabled = !users.length;
  const count = document.querySelector("[data-auditcount]");
  if (count) count.textContent = auditFilter.trim()
    ? "Найдено: " + users.length
    : "Всего игроков: " + (((auditData && auditData.users) || []).length);
}

function economyAudit(data) {
  const report = data.report;
  if (!report) return '<div class="panel"><h2>🕵️ Денежный аудит</h2>' +
    '<div class="empty">В денежном журнале пока нет пользователей.</div></div>';
  auditUser = data.selected || auditUser;
  auditHours = report.hours || auditHours;
  auditData = data;
  const maximum = Math.max(1, ...(report.hourly || []).map((row) => Number(row.earned || 0)));
  const activeSources = (report.sources || []).filter((row) => row.earned || row.spent);
  const graph = (report.hourly || []).map((hour, index) => {
    const totalHeight = Math.round(112 * Number(hour.earned || 0) / maximum);
    const pieces = (hour.sources || []).filter((part) => part.earned > 0).map((part) => {
      const height = hour.earned ? Math.max(2, totalHeight * part.earned / hour.earned) : 0;
      return '<span class="audit-segment" style="height:' + height + 'px;background:' +
        esc(part.color) + '" title="' + esc(part.name) + ': +' + money(part.earned) + '"></span>';
    }).join("");
    const label = report.hours <= 24 || index % (report.hours <= 72 ? 3 : 12) === 0
      ? String(hour.label || "").slice(6) : "";
    return '<div class="audit-hour" title="' + esc(hour.label) + ': +' + money(hour.earned) +
      ' / −' + money(hour.spent) + '"><div class="audit-stack">' + pieces +
      '</div><span class="audit-label">' + esc(label) + '</span></div>';
  }).join("");
  const sourceRows = activeSources.map((row) =>
    '<span class="small"><span class="audit-dot" style="background:' + esc(row.color) +
    '"></span>' + esc(row.name) + '</span><b class="small audit-positive">+' + money(row.earned) +
    '</b><b class="small audit-negative">−' + money(row.spent) + '</b><b class="small">' +
    signedMoney(row.net) + '</b>'
  ).join("") || '<span class="small muted">Операций в этом окне нет.</span>';
  const hourDetails = (report.hourly || []).filter((hour) => hour.transactions).map((hour) =>
    '<div style="padding:9px 0;border-bottom:1px solid var(--line)">' +
      '<div class="row spread"><b>' + esc(hour.label) + '</b><span class="small">' +
        '<span class="audit-positive">+' + money(hour.earned) + '</span> · ' +
        '<span class="audit-negative">−' + money(hour.spent) + '</span> · <b>' +
        signedMoney(hour.net) + '</b></span></div>' +
      (hour.sources || []).map((part) =>
        '<div class="row spread tiny" style="margin-top:5px"><span><span class="audit-dot" ' +
        'style="background:' + esc(part.color) + '"></span>' + esc(part.name) + '</span><span>' +
        '<span class="audit-positive">+' + money(part.earned) + '</span> / ' +
        '<span class="audit-negative">−' + money(part.spent) + '</span> · ' +
        signedMoney(part.net) + '</span></div>').join("") + '</div>'
  ).join("") || '<div class="empty">В выбранном окне операций нет.</div>';
  const transactionRows = (report.transactions || []).slice(0, 100).map((row) =>
    '<div class="row spread small" style="gap:8px;margin-bottom:6px"><span><span class="muted">' +
    esc(row.time) + '</span><br>' + esc(row.source_name) +
    '<br><span class="tiny muted">' + esc(row.reason) + '</span></span><b class="' +
    (row.delta >= 0 ? 'audit-positive' : 'audit-negative') + '">' + signedMoney(row.delta) +
    '</b></div>'
  ).join("") || '<div class="empty">За период операций нет.</div>';
  const matchingUsers = filteredAuditUsers();
  const userOptions = auditUserOptions(matchingUsers, data.selected);
  const windows = (data.windows || [24, 72, 168]).map((hours) =>
    '<button class="chip' + (Number(hours) === Number(report.hours) ? ' on' : '') +
    '" data-audithours="' + hours + '">' + (hours === 24 ? '24 часа' : hours === 72 ? '3 дня' : '7 дней') +
    '</button>'
  ).join("");
  return '<div class="panel"><h2>🕵️ Денежный аудит</h2>' +
    '<input class="audit-select" style="margin-bottom:7px" type="search" autocomplete="off" ' +
      'placeholder="Имя, @username, существо или ID" value="' + esc(auditFilter) + '" data-auditfilter>' +
    '<select class="audit-select" data-audituser' + (matchingUsers.length ? '' : ' disabled') +
      '>' + userOptions + '</select>' +
    '<div class="tiny muted" style="margin-top:5px" data-auditcount>' +
      (auditFilter.trim() ? 'Найдено: ' + matchingUsers.length : 'Всего игроков: ' + (data.users || []).length) +
      '</div>' +
    '<div class="chiprow" style="margin-top:9px">' + windows + '</div>' +
    '<div class="audit-summary" style="margin-top:10px">' +
      tile('Начислено', '+' + money(report.earned)) + tile('Списано', '−' + money(report.spent)) +
      tile('Чистыми', signedMoney(report.net)) + '</div></div>' +
    '<div class="panel"><h2>Начисления по часам</h2><div class="audit-graph">' + graph + '</div>' +
      '<div class="audit-legend">' + activeSources.filter((row) => row.earned).map((row) =>
        '<span class="tiny"><span class="audit-dot" style="background:' + esc(row.color) + '"></span>' +
        esc(row.name) + '</span>').join("") + '</div></div>' +
    '<details class="panel" open><summary><b>Почасовая расшифровка</b></summary>' +
      '<div style="margin-top:7px">' + hourDetails + '</div></details>' +
    '<div class="panel"><h2>Откуда деньги</h2><div class="audit-table">' +
      '<span class="head">Источник</span><span class="head">Пришло</span>' +
      '<span class="head">Ушло</span><span class="head">Чистыми</span>' + sourceRows + '</div></div>' +
    '<div class="panel"><h2>XP-баланс вне графика</h2><div class="small">' +
      (report.xp_coins_archived == null ? 'Нет сохранённой оценки.' :
       'Около <b>' + money(report.xp_coins_archived) + '</b> монет заработано активностью за всё время.') +
      '</div><div class="tiny muted" style="margin-top:6px">' + esc(report.xp_coins_note || '') +
      '</div></div>' +
    '<details class="panel"><summary><b>Последние операции · ' +
      (report.transactions || []).length + '</b></summary><div style="margin-top:10px">' +
      transactionRows + '</div></details>' +
    (report.log_at_capacity ? '<div class="warn-note">Журнал достиг лимита; самые старые операции уже удалены.</div>' : '');
}

async function renderMore() {
  const box = $("scr-more");
  if (moreView === "menu") {
    const menu = ["quests:" + (S && S.quest_attention ? "❗ " : "") + "🎯 Квесты", "mail:📬 Почта", "ranking:🏆 Рейтинг существ",
                  "collection:📚 Коллекция оружия", "history:📜 История боёв",
                  "updates:" + (S && S.unread_updates ? "❗ " : "") + "📰 Обновления"];
    // The review queue is the one entry that is not for everybody. Whether it appears at
    // all is the server's answer, never a guess from the client -- and every route behind
    // it re-checks, so a hand-typed moreView cannot open anything.
    if (S && S.is_admin) menu.push("review:" + (S.pending_quests ? "🔴 " : "") + "🛡 Проверка квестов");
    if (S && S.is_economy_admin) menu.push("moneyaudit:🕵️ Денежный аудит");
    if (S && S.is_economy_admin) menu.push("birthday:🎂 День рождения");
    if (S && S.is_economy_admin) menu.push("debuff:🎭 Эффекты игрокам");
    box.innerHTML = '<div class="panel"><h2>Ещё</h2>' +
      menu.map((entry) => {
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
    body = '<div class="panel"><h2>Рейтинг</h2>' +
      "<div class='tiny muted' style='margin-bottom:9px'>Нажми на имя — покажет, " +
      "что существо носит прямо сейчас.</div>" + (data.rows.length
      ? data.rows.map((row) =>
          '<button class="foe" data-peek="' + esc(row.user_id) + '">' +
          '<span class="av">' + shot(row.portrait, null) + "</span>" +
          "<span class='small'><b>" + row.rank + ".</b> " + esc(row.name || "—") +
          (row.user_id === data.me ? " <span class='tiny muted'>(ты)</span>" : "") +
          " " + debuffTag(row.debuff) +
          "<br><span class='tiny muted'>" + esc(row.owner_name || "") + "</span></span>" +
          "<span class='pw'>⚡ " + money(row.power) + "</span></button>" +
          '<div class="peek" data-peekbody="' + esc(row.user_id) + '" hidden></div>').join("")
      : '<div class="empty">Пока пусто.</div>') + "</div>";
  } else if (moreView === "collection") {
    const data = await api("/api/collection");
    body = '<div class="panel"><h2>Найдено оружия · ' + data.rows.length + " из " + data.total +
      "</h2>" + (data.rows.length
        ? '<div class="items">' + data.rows.map((i) => itemCard(i)).join("") + "</div>"
        : '<div class="empty">Ещё ничего не найдено.</div>') + "</div>";
  } else if (moreView === "quests") {
    body = questBoard(await api("/api/quests"));
  } else if (moreView === "review") {
    body = reviewQueue(await api("/api/quests/review"));
  } else if (moreView === "moneyaudit") {
    const query = "?hours=" + encodeURIComponent(auditHours) +
      (auditUser ? "&user_id=" + encodeURIComponent(auditUser) : "");
    body = economyAudit(await api("/api/economy/audit" + query));
  } else if (moreView === "birthday") {
    body = birthdayAdmin(await api("/api/birthday"));
  } else if (moreView === "debuff") {
    body = debuffAdmin(await api("/api/debuff"));
  } else if (moreView === "mail") {
    const data = await api("/api/mail");
    body = '<div class="panel"><h2>📬 Почта</h2>' + mailFeed(data.rows || []) + "</div>";
  } else if (moreView === "history") {
    const data = await api("/api/history");
    body = '<div class="panel"><h2>Последние бои</h2>' + (data.rows.length
      ? data.rows.map(historyRow).join("")
      : '<div class="empty">Боёв ещё не было.</div>') + "</div>";
  } else if (moreView === "updates") {
    const data = await api("/api/updates");
    if (S) S.unread_updates = false;
    body = (data.rows || []).map((row) =>
      '<div class="panel"><h2>' + esc(row.title || "Обновление") + "</h2>" +
      '<div class="small" style="white-space:pre-wrap">' + esc(row.text || "") +
      "</div></div>").join("") || '<div class="empty">Пока тихо.</div>';
  }
  box.innerHTML = '<button class="go sec" data-more="menu">◀️ Назад</button>' + body;
  paintShots(box);
}

// ---------------------------------------------------------------------------- quests
const DIFF_NAMES = ["", "новичок", "просто", "средне", "сложно", "жёстко"];
const TOOL_NAMES = { brush: "кисть", airbrush: "аэрограф", any: "кисть или аэрограф" };

function pips(level) {
  let out = "";
  for (let i = 1; i <= 5; i += 1) out += i <= level ? "●" : "○";
  return '<span class="pips d' + level + '">' + out + "</span>";
}

function rewardLine(reward) {
  if (!reward) return "";
  const scroll = reward.scroll_chance
    ? " · <span class='gain'>📜 " + Math.round(reward.scroll_chance * 100) +
      "% (не позже " + Number(reward.scroll_pity || 0) + "-го)</span>"
    : "";
  return "<span class='gain'>💰 " + money(reward.gold) + "</span> · " +
    "<span class='gain'>✨ " + money(reward.xp) + "</span> · 🎟 " + (reward.tickets || 0) +
    " · 🎁 " + Math.round((reward.drop_chance || 0) * 100) + "%" + scroll;
}

// Quest calls do not return the game state the way /api/action does -- nothing here
// changes a pet -- so they redraw the screen they were pressed on instead of the world.
async function questCall(path, payload) {
  try {
    const data = await api(path, payload || {});
    haptic(data.ok ? "ok" : "no");
    if (data.message) toast(data.message);
    if (data.ok && data.receipt && data.receipt.item_name) {
      toast("Выпало: " + data.receipt.item_name);
    }
    if (data.ok && path === "/api/quests/review" && S) {
      S.pending_quests = Math.max(0, Number(S.pending_quests || 0) - 1);
    }
    render();
    return data.ok;
  } catch (e) {
    haptic("no");
    toast(e.message);
    return false;
  }
}

// Legacy single-card renderer retained for old embedded clients.
// only thing that differs here is the heading and what the quest asks you to photograph.
function legacyQuestCard(board, kind) {
  const paint = kind === "paint";
  const heading = paint ? "🎯 Челлендж дня · покрас" : "🌍 Квест в реале";
  const quest = board && board.quest;
  if (!quest) {
    const message = (board && board.status) === "exhausted"
      ? "Все квесты в реале пройдены — новые откроются, когда отдохнут старые."
      : "На сегодня всё — сдано. Новый придёт завтра.";
    return '<div class="panel"><h2>' + heading + "</h2><div class='empty'>" +
      message + "</div></div>";
  }
  const reviewing = board.status === "review";
  const asks = paint
    ? "<p class='small' style='margin:10px 0 4px'><b>Что красим:</b> " + esc(quest.subject) + "</p>"
    : "<p class='small' style='margin:10px 0 4px'><b>Что делаем:</b> " + esc(quest.subject) + "</p>";
  return '<div class="panel"><h2>' + heading + "</h2>" +
    '<div class="qtitle">' + esc(quest.title) + "</div>" +
    '<div class="row spread tiny muted" style="margin-top:2px"><span>' +
      pips(quest.difficulty) + " " + esc(DIFF_NAMES[quest.difficulty] || "") + "</span>" +
      (paint ? "<span>" + esc(TOOL_NAMES[quest.tool] || quest.tool) + "</span>"
             : (quest.badge ? "<span class='qbadge'>🧽 " + esc(quest.badge) + "</span>" : "")) +
    "</div>" +
    asks +
    "<p class='small muted' style='margin:0 0 8px'>" + esc(quest.technique) + "</p>" +
    "<p class='tiny muted' style='margin:0 0 10px'>💡 " + esc(quest.hint) + "</p>" +
    '<div class="qreward">' + rewardLine(quest.reward) + "</div>" +
    (board.has_pet === false
      ? "<div class='tiny muted' style='margin-top:6px;text-align:center'>" +
        "Опыт и предмет снаряжения начислить некуда — сначала приручи существо. " +
        "Монеты, билет и найденный свиток сохранятся в любом случае.</div>"
      : "") +
    (reviewing
      ? "<div class='qtag review'>Работа на проверке у модератора</div>"
      : '<div class="qtag">' +
        (paint ? "Выложи фото" : "Нужно: " + esc(quest.proof) + ". Выложи") +
        " в чат с хештегом <b>" + esc(quest.hashtag) + "</b></div>") +
    '<div class="row" style="margin-top:10px">' +
      '<button class="go sec" data-quest="' + kind + '"' +
        (board.rerolls_left && !reviewing ? "" : " disabled") + ">🎲 Реролл · " +
        (board.rerolls_left || 0) + " из " + (board.rerolls_total || 0) + "</button>" +
    "</div>" +
    (board.rerolls_left && !reviewing
      ? "<div class='warn-note'>Сначала покрась что-то новое: старые работы не подходят." +
        "<br>⚠️ Реролл даёт квест на ступень сложнее" +
        (quest.difficulty >= 5 ? " — но выше пятой ступени уже некуда, придёт другой такой же."
                               : " — и награда тоже вырастет.") + "</div>"
      : "") +
    "</div>";
}

function legacyQuestBoard(data) {
  const board = data || {};
  const done = (board.stats || {}).done || 0;
  const head = questCard(board, "paint") +
    questCard(board.real || {}, "real");

  const rows = board.history || [];
  return head +
    '<button class="go sec" data-questidea>💡 Предложить идею</button>' +
    '<div class="panel"><h2>Сдано квестов · ' + done + "</h2>" + (rows.length
      ? rows.map((row) =>
          '<div class="row spread small" style="margin-bottom:7px"><span>' +
          pips(row.difficulty) + " " + esc(row.title) + "</span>" +
          "<span class='tiny gain'>💰" + money(row.gold || 0) +
          (row.item_name ? " · 🎁" : "") + "</span></div>").join("")
      : "<div class='empty'>Пока ни одного.</div>") + "</div>";
}

let ACTIVE_QUEST_BOARD = null;

function questStatus(card) {
  if (card.status === "review") return ["⏳", "на проверке"];
  if (card.status === "done") return ["✅", "выполнен"];
  return ["❗", "доступен"];
}

function questCompactCard(card, kind, index) {
  const status = questStatus(card);
  return '<button class="panel" data-questopen="' + kind + ':' + esc(card.code) + '" ' +
    'style="width:100%;text-align:left;border:1px solid var(--line);margin-bottom:9px">' +
    '<div class="row spread"><b>' + index + '. ' + status[0] + ' ' + esc(card.title) + '</b>' +
    '<span class="tiny muted">' + status[1] + '</span></div>' +
    '<div class="tiny muted" style="margin-top:5px">' + pips(card.difficulty) + ' · ' +
    esc(card.subject) + '</div><div class="tiny gain" style="margin-top:6px">' +
    rewardLine(card.reward) + '</div></button>';
}

function questCard(board, kind) {
  const paint = kind !== "real";
  const heading = kind === "rune" ? "🔮 Рунические покрасы · элементы" :
    (paint ? "🎯 Квесты на покрас · 3 карточки" : "🌍 Квест в реале");
  const cards = (board && board.quests) || [];
  return '<div class="panel"><h2>' + heading + '</h2>' +
    '<div class="tiny muted" style="margin-bottom:9px">⏳ Новая подборка через ' +
    '<b class="quest-timer" data-seconds="' + Number(board.seconds_until_refresh || 0) + '">' +
    clock(board.seconds_until_refresh || 0) + '</b>. Успей отправить фото до обновления. ' +
    'Если выполнить всё раньше, новые задания придут через 8 часов.</div>' +
    (cards.length ? cards.map((card, index) => questCompactCard(card, kind, index + 1)).join("")
                  : '<div class="empty">Доступных заданий пока нет. Проверим снова через 8 часов.</div>') +
    '</div>';
}

function questBoard(data) {
  ACTIVE_QUEST_BOARD = data || {};
  if (S) {
    S.quest_attention = Boolean(
      ((ACTIVE_QUEST_BOARD.quests || []).some((row) => row.status === "open")) ||
      (((ACTIVE_QUEST_BOARD.real || {}).quests || []).some((row) => row.status === "open"))
      || (((ACTIVE_QUEST_BOARD.rune || {}).quests || []).some((row) => row.status === "open"))
    );
  }
  const done = (ACTIVE_QUEST_BOARD.stats || {}).done || 0;
  const rows = ACTIVE_QUEST_BOARD.history || [];
  return questCard(ACTIVE_QUEST_BOARD, "paint") +
    questCard(ACTIVE_QUEST_BOARD.real || {}, "real") +
    questCard(ACTIVE_QUEST_BOARD.rune || {}, "rune") +
    '<button class="go sec" data-questidea>💡 Предложить идею</button>' +
    '<div class="panel"><h2>Сдано квестов · ' + done + '</h2>' + (rows.length
      ? rows.map((row) => '<div class="row spread small" style="margin-bottom:7px"><span>' +
          pips(row.difficulty) + ' ' + esc(row.title) + '</span><span class="tiny gain">🪙' +
          money(row.gold || 0) + (row.item_name ? ' · 🎁' : '') + '</span></div>').join("")
      : '<div class="empty">Пока ни одного.</div>') + '</div>';
}

function openQuestDetail(kind, code) {
  const board = kind === "real" ? (ACTIVE_QUEST_BOARD.real || {}) : ACTIVE_QUEST_BOARD;
  const card = ((board && board.quests) || []).find((row) => row.code === code);
  if (!card) { toast("Подборка уже обновилась."); return; }
  const status = questStatus(card);
  const steps = [
    "Возьми новую, ещё не показанную работу и подготовь нужную деталь.",
    kind === "paint" ? "Нанеси технику небольшими контролируемыми этапами."
                     : "Выполни действие полностью, не только для фотографии.",
    "Сверь результат с подсказкой и поправь самые заметные места.",
    "Сделай чёткое фото: " + (card.proof || "готового результата") + ".",
    "Выложи фото в чат с хештегом " + card.hashtag + ".",
  ];
  sheet('<h3>' + status[0] + ' ' + esc(card.title) + '</h3>' +
    '<div class="tiny muted">' + pips(card.difficulty) + ' · ' +
      esc(DIFF_NAMES[card.difficulty] || '') + '</div>' +
    '<p class="small"><b>' + (kind === "paint" ? "Что красим: " : "Что делаем: ") +
      '</b>' + esc(card.subject) + '</p>' +
    '<p class="small"><b>Техника:</b> ' + esc(card.technique) + '</p>' +
    '<p class="small muted">💡 <b>Подсказка:</b> ' + esc(card.hint) + '</p>' +
    '<div class="panel"><h2>Как выполнить</h2>' + steps.map((step, index) =>
      '<div class="small" style="margin-bottom:7px"><b>' + (index + 1) + '.</b> ' +
      esc(step) + '</div>').join("") + '</div>' +
    '<div class="qreward">' + rewardLine(card.reward) + '</div>' +
    '<div class="qtag ' + (card.status === "review" ? "review" : "") + '">' +
      (card.status === "done" ? "Квест принят и завершён." :
       card.status === "review" ? "Работа на проверке у модератора." :
       "Старые работы не подходят — нужно покрасить что-то новое.") + '</div>' +
    (card.status === "open" && card.rerolls_left
      ? '<div class="warn-note">Реролл даст квест на ступень сложнее, и награда тоже вырастет.</div>' +
        '<div class="acts"><button class="go sec" data-questreroll="' + kind + ':' + esc(code) +
        '">🎲 Реролл · осталось ' + card.rerolls_left + '</button></div>' : ''));
}

// -------------------------------------------------------------------- quest review
let reviewIdeasOpen = false;
let QUEST_CATALOG = [];
function reviewQueue(data) {
  QUEST_CATALOG = data.catalog || [];
  const rows = data.rows || [];
  let out = '<div class="panel"><h2>🛡 На проверке · ' + rows.length + "</h2>" + (rows.length
    ? rows.map((row) =>
        '<div class="claim">' +
          '<span class="av">' + shot(row.portrait, null) + "</span>" +
          "<div class='cbody'><b>" + esc(row.author) + "</b>" +
            (row.username ? " <span class='tiny muted'>@" + esc(row.username) + "</span>" : "") +
            "<div class='small'>" + pips(row.difficulty) + " " + esc(row.title) + "</div>" +
            "<div class='tiny muted'>" + esc(row.subject) + "</div>" +
            "<div class='tiny' style='margin-top:4px'>" + esc(row.technique || "") + "</div>" +
            (row.hint ? "<div class='tiny muted'>💡 " + esc(row.hint) + "</div>" : "") +
            (row.proof ? "<div class='tiny muted'>Показать: " + esc(row.proof) + "</div>" : "") +
            "<div class='tiny' style='margin-top:4px'>" + rewardLine(row.reward) + "</div>" +
            (row.link ? '<a class="tiny" target="_blank" rel="noreferrer" href="' +
              esc(row.link) + '">Открыть пост в чате ↗</a>' : "") +
          "</div>" +
          '<div class="cacts">' +
            '<button class="go" data-accept="' + esc(row.id) + '">Принять</button>' +
            '<button class="go warn" data-reject="' + esc(row.id) + '">Отклонить</button>' +
          "</div>" +
        "</div>").join("")
    : "<div class='empty'>Пусто. Все работы разобраны.</div>") + "</div>";

  const ideas = data.ideas || [];
  out += '<button class="go sec" data-reviewideas>💡 Идеи' +
    (ideas.length ? " · " + ideas.length : "") + "</button>";
  if (reviewIdeasOpen) {
    out += '<div class="panel"><h2>💡 Идеи квестов · ' + ideas.length + "</h2>" + (ideas.length
      ? ideas.map((idea) => '<div class="small" style="margin-bottom:12px"><b>' +
          esc(idea.author_name || idea.author_username || idea.user_id || "Участник") + "</b>" +
          (idea.author_username ? ' <span class="tiny muted">@' + esc(idea.author_username) + "</span>" : "") +
          '<div style="white-space:pre-wrap;margin-top:3px">' + esc(idea.text) + "</div></div>").join("")
      : '<div class="empty">Пока никто ничего не предложил.</div>') + "</div>";
  }

  out += '<div class="panel"><h2>Награды по сложности</h2>' +
    "<div class='tiny muted' style='margin-bottom:8px'>Меняется сразу и только для этого чата.</div>" +
    (data.rewards || []).map((row) =>
      '<div class="rwrow"><span class="small">' + pips(row.difficulty) + "</span>" +
      ["gold", "xp", "tickets", "drop_chance"].map((field) =>
        '<label class="tiny muted">' + ({ gold: "💰", xp: "✨", tickets: "🎟", drop_chance: "🎁" })[field] +
        '<input class="rwin" type="number" step="' + (field === "drop_chance" ? "0.01" : "1") +
        '" value="' + row[field] + '" data-reward="' + field + '" data-level="' + row.difficulty +
        '"></label>').join("") + "</div>").join("") + "</div>";

  out += '<div class="panel"><h2>Квесты в ротации</h2>' +
    (data.catalog || []).map((quest) =>
      '<div class="row spread small" style="margin-bottom:6px"><button class="go sec" ' +
      'style="width:auto;text-align:left;padding:6px 8px" data-questedit="' + esc(quest.code) + '">' +
      pips(quest.difficulty) + " " + esc(quest.title) + "<br><span class='tiny muted'>" +
      esc(quest.hashtag) + "</span></button>" +
      '<button class="chip' + (quest.enabled ? " on" : "") + '" data-queston="' + esc(quest.code) +
      '" data-enabled="' + (quest.enabled ? "0" : "1") + '">' +
      (quest.enabled ? "в ротации" : "выключен") + "</button></div>").join("") + "</div>";
  return out;
}

// ------------------------------------------------------------------------------ mail
const MAIL_ICONS = { attack: "⚔️", defense: "🛡", farm: "🌾", gift_in: "🎁", gift_out: "🎁",
                     quest_ok: "🎯", quest_no: "🎯", scroll: "📜" };
const MAIL_VERDICTS = { win: "Победа", loss: "Поражение", draw: "Ничья" };
// The stripe colour, keyed on what the row means to the reader. A won defence is green
// for the same reason a won attack is: the question is whether the day went well, not
// who pressed the button.
const MAIL_TONES = { win: "win", loss: "loss", draw: "" };

function mailFeed(rows) {
  if (!rows.length) {
    return '<div class="empty">Пока пусто.<br>Здесь будут бои, смены на ферме и подарки.</div>';
  }
  let out = "", day = null;
  for (const row of rows) {
    if (row.day !== day) {
      day = row.day;
      out += '<div class="mday"><b>' + esc(row.day_label || day || "") + "</b><i></i></div>";
    }
    out += mailRow(row);
  }
  return out;
}

function mailTone(row) {
  if (row.kind === "quest_ok") return "win";
  if (row.kind === "quest_no") return "loss";
  if (row.kind === "scroll") return "win";
  if (row.kind === "farm") return "gold";
  if (row.kind === "gift_in" || row.kind === "gift_out") return "give";
  return MAIL_TONES[row.outcome] || "";
}

function mailWho(row) {
  const pet = esc(row.pet_name || "");
  const owner = esc(row.owner_name || "");
  if (pet && owner) return "<b>" + pet + "</b> <span class='muted'>(" + owner + ")</span>";
  return "<b>" + (pet || owner || "?") + "</b>";
}

function mailRow(row) {
  const coins = Number(row.coins || 0);
  const meta = [];
  if (row.kind !== "farm" && row.kind.indexOf("quest") !== 0 && MAIL_VERDICTS[row.outcome]) {
    meta.push("<span class='verdict'>" + MAIL_VERDICTS[row.outcome] + "</span>");
  }
  if (coins) {
    meta.push("<span class='" + (coins > 0 ? "gain" : "loss") + "'>" +
              (coins > 0 ? "+" : "−") + money(Math.abs(coins)) + " 💰</span>");
  }
  if (row.xp && (row.kind === "farm" || row.kind === "quest_ok")) {
    meta.push("<span class='gain'>+" + money(row.xp) + " ✨</span>");
  }
  if (row.kind === "quest_ok" && row.tickets) meta.push("<span class='gain'>+" + row.tickets + " 🎟</span>");
  if (row.scroll_name && row.kind !== "scroll") {
    meta.push("<span class='find'>📜 " + esc(row.scroll_name) + "</span>");
  }
  if (row.item_name) {
    // Tinted by rarity, the same colour the item's own card is bordered with -- a
    // legendary find should be legible as one from across the feed.
    meta.push('<span class="find" style="color:var(--r-' + esc(row.item_rarity || "common") +
              ')">' + esc(row.item_name) + (row.auto_equipped ? " · надето" : "") + "</span>");
  }
  let title;
  if (row.kind === "quest_ok") {
    title = "Квест «<b>" + esc(row.pet_name || "") + "</b>» принят";
  } else if (row.kind === "quest_no") {
    title = "Квест «<b>" + esc(row.pet_name || "") + "</b>» отклонён" +
      (row.note ? "<div class='tiny muted'>" + esc(row.note) + "</div>" : "");
  } else if (row.kind === "scroll") {
    title = "Открыт " + (row.scroll_ultimate ? "ультимейт" : "свиток") + ": «<b>" +
      esc(row.scroll_name || "свиток") + "</b>»";
  } else if (row.kind === "farm") {
    title = "Ферма — смена " + Number(row.hours || 0) + " ч";
  } else if (row.kind === "gift_out") {
    title = "Подарок для " + mailWho(row);
  } else if (row.kind === "gift_in") {
    title = "Подарок от " + mailWho(row);
  } else if (row.kind === "attack") {
    title = "Ты напал на " + mailWho(row);
  } else {
    title = "На тебя напал " + mailWho(row);
  }
  // A fight in the mailbox opens the same replay the fight log does -- it is the same
  // fight, and "what happened there?" is the question a one-line summary provokes.
  const tag = row.replayable ? "button" : "div";
  const open = '<' + tag + ' class="mail ' + mailTone(row) + (row.replayable ? " rerunable" : "") +
    '"' + (row.replayable ? ' data-replay="' + esc(row.fight_id) + '"' : "") + ">";
  if (row.replayable) meta.push("<span class='play'>▶ повтор</span>");
  return open +
    "<span class='mt'>" + esc(row.at || "") + "</span>" +
    "<span class='mi'>" + (MAIL_ICONS[row.kind] || "•") + "</span>" +
    "<div class='mb'>" + title +
    (meta.length ? "<div class='meta'>" + meta.join("") + "</div>" : "") +
    "</div></" + tag + ">";
}

function historyRow(row) {
  const coins = row.coins
    ? " · <span class='" + (row.coins > 0 ? "gain" : "loss") + "'>💰" +
      (row.coins > 0 ? "+" : "") + row.coins + "</span>"
    : "";
  // A replayable fight is a button; one from before snapshots were kept stays plain
  // text, because a control that can only apologise is worse than no control.
  const open = row.replayable
    ? '<button class="row spread small rerunable" data-replay="' + esc(row.id) + '">'
    : '<div class="row spread small" style="margin-bottom:7px">';
  return open + "<span>" +
    (row.attacked ? "Ты напал на " : "На тебя напал ") + "<b>" + esc(row.opponent) + "</b>" +
    "</span><span class='tiny'>" + esc(row.outcome) + coins +
    (row.replayable ? " <span class='play'>▶</span>" : "") + "</span>" +
    (row.replayable ? "</button>" : "</div>");
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
  for (const key of ["strength", "health", "agility", "luck", "endurance", "armor"]) {
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
    for (const key of ["strength", "health", "agility", "luck", "endurance", "armor"]) {
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
  playDuel(data);
}

// Watching a recorded fight is the same screen, because it is the same fight: the server
// puts the stored seed and the two stored fighters back through the same simulator and
// returns the same payload. Nothing here is a second rendering of it.
async function replay(id) {
  let data;
  try {
    data = await api("/api/replay?id=" + encodeURIComponent(id));
  } catch (e) { haptic("no"); toast(e.message); return; }
  playDuel(data);
}

// An item's effect amount is not always damage. These are the procs that give HP back, and
// the ones that stop damage rather than deal it; anything else with a number is a hit.
// Keyed on the legacy-compatible effect prefix pets_combat uses ("amulet_vampiric").
const HEAL_PROCS = ["vampiric", "bite", "blood_pact", "second_wind", "medkit", "dodge_heal", "regen", "adrenaline", "rewind"];
const SOAK_PROCS = ["opening_shield", "armor_burst", "safeguard", "crit_guard", "countercrit", "death_shield", "last_stand", "perfect_parry"];
const UTILITY_PROCS = ["gambler", "candle", "armor_shred"];

function amountTone(round) {
  const event = String(round.event || "");
  if (event.indexOf("amulet_") !== 0) return "harm";
  const code = event.slice(7);
  if (HEAL_PROCS.indexOf(code) >= 0) return "heal";
  if (SOAK_PROCS.indexOf(code) >= 0) return "soak";
  // Percentages and state changes are not HP, so they get no damage colour.
  return UTILITY_PROCS.indexOf(code) >= 0 ? "" : "harm";
}

const isDigit = (ch) => ch >= "0" && ch <= "9";

// The flavour text is a template with exactly three holes in it -- {attacker}, {defender}
// and {amount} (see pets_flavor) -- so highlighting those three needs no prose parsing:
// both names and the round's own damage figure are already known here.
//
// Scanned by hand rather than with a built regex, for two reasons. A pet name is player
// input and would have to be regex-escaped, and this page is a Python string, so every
// backslash in it has to survive being written twice -- one of those two mistakes is
// silent and the other takes the whole log down. And matching a whole digit RUN against
// the amount is exactly the "5 must not light up the 5 inside 15" rule, without needing
// a word boundary or a lookbehind for it.
//
// Matching happens on the raw text and every piece is escaped exactly once on the way
// out, so inserted markup is never rescanned and a pet called "span" changes nothing.
function paintBlow(round, mineName, theirName) {
  const rules = [];
  if (mineName) rules.push({ text: mineName, cls: "nm mine" });
  if (theirName && theirName !== mineName) rules.push({ text: theirName, cls: "nm them" });
  rules.sort((a, b) => b.text.length - a.text.length);   // longest name wins a tie
  const amount = Math.abs(Number(round.damage || 0));
  const tone = amountTone(round);
  const wanted = amount > 0 && tone ? String(amount) : null;

  const text = String(round.text || "");
  let out = "", plain = 0, at = 0;
  const flush = (upto) => { out += esc(text.slice(plain, upto)); };
  while (at < text.length) {
    let hit = null, length = 0;
    for (const rule of rules) {
      if (rule.text && text.startsWith(rule.text, at)) { hit = rule.cls; length = rule.text.length; break; }
    }
    if (hit === null && wanted && isDigit(text[at]) && !(at && isDigit(text[at - 1]))) {
      let end = at;
      while (end < text.length && isDigit(text[end])) end += 1;
      if (text.slice(at, end) === wanted) { hit = "amount " + tone; length = end - at; }
    }
    if (hit === null) { at += 1; continue; }
    flush(at);
    out += '<span class="' + hit + '">' + esc(text.substr(at, length)) + "</span>";
    at += length;
    plain = at;
  }
  flush(text.length);
  return out;
}

function playDuel(data) {
  const me = data.you;
  const maxHp = data.max_hp || {};
  // The names the TRANSCRIPT was written with, not the pets' current ones -- a rename
  // between the fight and the replay would otherwise leave every line naming a creature
  // that appears nowhere on the screen.
  const mineName = data.you_name || (S.pet && S.pet.name) || "Ты";
  const theirName = data.opponent.name || "Соперник";
  // Each bar against its OWN maximum. Falling back to the reader's own is what the whole
  // duel used to do, and it made a frailer opponent look untouched until they died.
  const mineMax = Math.max(1, maxHp[me] || (S.combat ? S.combat.max_hp : 100));
  const theirMax = Math.max(1, maxHp[data.opponent.user_id] || mineMax);

  const view = document.createElement("div");
  view.className = "duel";
  view.id = "duel";
  const fighterArt = data.dungeon
    ? '<div class="fighters"><span class="fighter-art">' + shot(S.pet && S.pet.portrait, S.pet && S.pet.crop) +
      '</span><span class="versus">VS</span><span class="fighter-art">' + dungeonArt(data.enemy_art || {}) + '</span></div>'
    : "";
  view.innerHTML =
    fighterArt + '<div class="side"><div class="row spread small"><b>' + esc(mineName) +
      '</b><span id="hpMine"></span></div><div class="hpbar"><i id="barMine" style="width:100%"></i></div></div>' +
    '<div class="side"><div class="row spread small"><b>' + esc(theirName) +
      '</b><span id="hpTheirs"></span></div><div class="hpbar"><i id="barTheirs" style="width:100%"></i></div></div>' +
    (data.replay ? '<div class="rerun">↺ Повтор боя' +
      (data.at ? " · " + esc(String(data.at).slice(11, 16).replace(":", ".")) : "") + "</div>" : "") +
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
    $("barMine").style.width = Math.min(100, (mineHp / mineMax) * 100) + "%";
    $("barTheirs").style.width = Math.min(100, (theirsHp / theirMax) * 100) + "%";
    $("hpMine").textContent = mineHp;
    $("hpTheirs").textContent = theirsHp;
    const eventName = String(round.event || "");
    const kind = eventName.indexOf("amulet_") === 0 ? "amulet"
      : (eventName === "dodge" || eventName === "skill_dodge" ? "dodge"
      : (eventName === "defend" ? "defend"
      : (eventName.indexOf("skill_") === 0 ? "skill"
      : (eventName.indexOf("crit") >= 0 ? "crit" : (mineTurn ? "mine" : "")))));
    $("duelLog").insertAdjacentHTML("beforeend",
      '<div class="blow ' + kind + '">' + paintBlow(round, mineName, theirName) + "</div>");
    $("duelLog").scrollTop = $("duelLog").scrollHeight;
    setTimeout(step, 520);
  };
  $("duelDone").onclick = () => {
    // Skipping fast-forwards the animation, not the formatting: the lines it dumps are
    // the same coloured lines step() would have written one at a time.
    while (index < data.rounds.length) { const r = data.rounds[index++];
      $("duelLog").insertAdjacentHTML("beforeend",
        '<div class="blow">' + paintBlow(r, mineName, theirName) + "</div>"); }
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
  const reviewTab = $("questReviewTab");
  reviewTab.hidden = !(S && S.is_admin);
  $("tabs").classList.toggle("has-review", !reviewTab.hidden);
  reviewTab.innerHTML = '<span class="ic">' + (S && S.pending_quests ? "🔴" : "🛡") +
                        "</span>Проверка";
  for (const button of $("tabs").children) {
    button.classList.toggle("on", button.dataset.tab === TAB ||
      (button.dataset.tab === "review" && TAB === "more" && moreView === "review"));
  }
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
  TAB = button.dataset.tab === "review" ? "more" : button.dataset.tab;
  if (button.dataset.tab === "review") moreView = "review";
  else if (TAB === "more") moreView = "menu";
  haptic();
  render();
});

// The mailbox lives in "Ещё" like every other read-only screen, but it is the one people
// come back to between fights -- so the HUD keeps a permanent way in, and «Назад» from it
// still lands on that menu rather than somewhere the tab bar disagrees with.
$("hudMail").addEventListener("click", () => {
  TAB = "more";
  moreView = "mail";
  haptic();
  render();
});

// One delegated handler for the whole game. Every control is a data- attribute rather than
// a bound listener, so a re-render (which replaces all of it) cannot leave a dead button
// or a duplicated one behind.
document.addEventListener("click", async (event) => {
  const target = event.target.closest("[data-item],[data-slot],[data-up],[data-do],[data-act]," +
    "[data-bagslot],[data-bagrarity],[data-bagsort],[data-shopslot],[data-foe],[data-more]," +
    "[data-farmstart],[data-feature],[data-gift],[data-equipnow],[data-shoptab],[data-replay]," +
    "[data-quest],[data-questopen],[data-questreroll],[data-questidea],[data-questedit],[data-reviewideas],[data-accept],[data-reject],[data-queston],[data-mob],[data-reforge],[data-enchant]," +
    "[data-testbattle],[data-testmode],[data-testaction],[data-testcatalog],[data-liveskill],[data-liveskillset],[data-audithours]," +
    "[data-congratulate],[data-birthdayset],[data-birthdayclear],[data-peek]," +
    "[data-debuffpick],[data-debuffset],[data-debuffclear],[data-dungeon]");
  if (!target) return;
  const d = target.dataset;
  if (d.dungeon) {
    const actions = { enter: "dungeon_enter", escalator: "dungeon_escalator", fight: "dungeon_fight", rest: "dungeon_rest", descend: "dungeon_descend", quit: "dungeon_quit" };
    await act(actions[d.dungeon], d.dungeon === "fight" ? { index: Number(d.index) } : {});
    return;
  }

  if (d.peek) { await togglePeek(d.peek); return; }
  if (d.congratulate) { await congratulate(); return; }
  if (d.birthdayset !== undefined) { await setBirthday(d.birthdayset); return; }
  if (d.birthdayclear !== undefined) { await setBirthday(null); return; }
  // Picking a mark is local state, so it re-renders without touching the server; the
  // other two are the mutation.
  if (d.debuffpick !== undefined) { debuffPick = d.debuffpick; render(); return; }
  if (d.debuffset !== undefined) { await setDebuff(d.debuffset, false); return; }
  if (d.debuffclear !== undefined) { await setDebuff(d.debuffclear, true); return; }

  if (d.testbattle === "open") { await openTestBattle(); return; }
  if (d.testbattle === "close") {
    TEST_SETUP = null; TEST_BATTLE = null; TEST_SESSION = null; TEST_MODE = null; render(); return;
  }
  if (d.testbattle === "restart") { TEST_BATTLE = null; TEST_SESSION = null; render(); return; }
  if (d.testmode) { await startTestBattle(d.testmode); return; }
  if (d.testaction) { await testBattleAction(d.testaction); return; }
  if (d.testcatalog !== undefined) { showTestCatalog(); return; }
  if (d.liveskill) { openLiveSkillPicker(d.liveskill); return; }
  if (d.liveskillset) {
    const split = d.liveskillset.indexOf(":");
    const slot = Number(d.liveskillset.slice(0, split));
    const code = d.liveskillset.slice(split + 1);
    closeSheet();
    await act("set_skill", { slot, code });
    return;
  }
  if (d.audithours) {
    auditHours = Number(d.audithours) || 24;
    render();
    return;
  }

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
  if (d.reforge) { await act("reforge", { rarity: d.reforge }); return; }
  if (d.enchant) {
    const [code, element] = d.enchant.split(":", 2);
    await act("enchant_weapon", { code, element });
    return;
  }
  if (d.more) { moreView = d.more; render(); return; }
  if (d.replay) { haptic(); replay(d.replay); return; }
  if (d.mob === "roll") { await rollMob(); return; }
  if (d.mob === "fight") { haptic(); await fightMob(); return; }
  if (d.questopen) {
    const [kind, code] = d.questopen.split(":", 2);
    openQuestDetail(kind, code);
    return;
  }
  if (d.questreroll) {
    const [kind, code] = d.questreroll.split(":", 2);
    closeSheet();
    await questCall("/api/quests/reroll", { kind, code });
    return;
  }
  if (d.quest) { await questCall("/api/quests/reroll", { kind: d.quest }); return; }
  if (d.questidea !== undefined) {
    sheet("<h3>💡 Предложить идею квеста</h3>" +
      "<textarea id='questIdeaText' class='go sec' style='min-height:110px;text-align:left' " +
      "maxlength='1000' placeholder='Опиши идею'></textarea><div class='acts'>" +
      "<button class='go' id='saveQuestIdea'>Отправить</button></div>");
    $("saveQuestIdea").onclick = async () => {
      const text = $("questIdeaText").value.trim();
      if (!text) { toast("Напиши текст идеи."); return; }
      closeSheet();
      await questCall("/api/quests/ideas", { text });
    };
    return;
  }
  if (d.questedit) {
    const quest = QUEST_CATALOG.find((row) => row.code === d.questedit);
    if (!quest) { toast("Квест не найден. Обнови страницу."); return; }
    const fields = [
      ["title", "Название", 160], ["subject", "Что покрасить / сделать", 500],
      ["technique", "Техника / описание", 1000], ["hint", "Подсказка", 1000],
      ["proof", "Что нужно показать", 500],
    ];
    sheet("<h3>🎯 Редактирование квеста</h3>" + fields.map(([key, label, max]) =>
      "<label class='small muted' style='display:block;margin:9px 0 3px'>" + label +
      "</label><textarea class='go sec' style='min-height:64px;text-align:left' maxlength='" + max +
      "' id='questEdit_" + key + "'>" + esc(quest[key] || "") + "</textarea>").join("") +
      "<div class='acts'><button class='go' id='saveQuestEdit'>Сохранить</button></div>");
    $("saveQuestEdit").onclick = async () => {
      const text = {};
      for (const [key] of fields) text[key] = $("questEdit_" + key).value.trim();
      if (Object.values(text).some((value) => !value)) { toast("Заполни все поля квеста."); return; }
      closeSheet();
      await questCall("/api/quests/config", { code: quest.code, text });
    };
    return;
  }
  if (d.reviewideas !== undefined) { reviewIdeasOpen = !reviewIdeasOpen; render(); return; }
  if (d.accept || d.reject) {
    // A verdict pays real coins, so it is the one moderator action that asks twice.
    const id = d.accept || d.reject;
    const accept = Boolean(d.accept);
    if (!accept) {
      const note = window.prompt("Напишите причину отклонения. Она придёт автору в личку бота.", "");
      if (note === null) return;
      if (!note.trim()) { toast("Причина отклонения обязательна."); return; }
      confirmThen("Отклонить работу и отправить причину автору?",
                  () => questCall("/api/quests/review", { id, accept, note: note.trim() }));
    } else {
      confirmThen("Принять работу и начислить награду?",
                  () => questCall("/api/quests/review", { id, accept }));
    }
    return;
  }
  if (d.queston) {
    await questCall("/api/quests/config", { code: d.queston, enabled: d.enabled === "1" });
    return;
  }
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
  else if (d.do === "respec") {
    confirmThen("Сбросить купленные статы за 15 💎? Золото не вернётся, но появятся свободные очки.",
                () => act("respec_stats"));
  }
  else if (d.do === "upcage") { await act("upgrade_cage"); }
  else if (d.do === "daily") { await act("daily_bonus"); }
  else if (d.do === "notify") { await act("notifications"); }
  else if (d.do === "farmup") { await act("farm_upgrade"); }
  else if (d.do === "farmticket") { await act("farm_ticket"); }
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

// User filtering is entirely local: typing must feel instant and must not turn every
// character into an admin API request.
document.addEventListener("input", (event) => {
  const birthday = event.target.closest("[data-birthdayfilter]");
  if (birthday) {
    const term = birthday.value.trim().toLowerCase();
    document.querySelectorAll("[data-bdayrow]").forEach((row) => {
      row.hidden = Boolean(term) && !row.dataset.bdayrow.includes(term);
    });
    return;
  }
  const marked = event.target.closest("[data-debufffilter]");
  if (marked) {
    const term = marked.value.trim().toLowerCase();
    document.querySelectorAll("[data-dbfrow]").forEach((row) => {
      row.hidden = Boolean(term) && !row.dataset.dbfrow.includes(term);
    });
    return;
  }
  const input = event.target.closest("[data-auditfilter]");
  if (!input) return;
  auditFilter = input.value;
  refreshAuditUserFilter();
});

// The reward editor commits on blur/enter rather than on every keystroke -- one number
// typed is several intermediate numbers, and each would be a saved edit to a live economy.
document.addEventListener("change", async (event) => {
  const audit = event.target.closest("[data-audituser]");
  if (audit) {
    auditUser = audit.value;
    render();
    return;
  }
  const input = event.target.closest("[data-reward]");
  if (!input) return;
  await questCall("/api/quests/config", {
    difficulty: Number(input.dataset.level),
    field: input.dataset.reward,
    value: Number(input.value),
  });
});

// Timers update in whole minutes. Repainting an arena every second recreates every
// opponent portrait, which is needless network/decode work and makes the faces flicker.
const TIMER_TICK_SECONDS = 60;
const TIMER_TICK_MS = TIMER_TICK_SECONDS * 1000;
function tick() {
  if (!S) return;
  let dirty = false;
  document.querySelectorAll(".quest-timer[data-seconds]").forEach((node) => {
    const left = Math.max(0, Number(node.dataset.seconds || 0) - TIMER_TICK_SECONDS);
    node.dataset.seconds = String(left);
    node.textContent = clock(left);
    if (!left && TAB === "more" && moreView === "quests") dirty = true;
  });
  if (S.arena && S.arena.seconds_until_next) {
    S.arena.seconds_until_next = Math.max(0, S.arena.seconds_until_next - TIMER_TICK_SECONDS);
    if (!S.arena.seconds_until_next) dirty = true;
    renderHud();
  }
  if (S.pve && S.pve.seconds_until_reset) {
    S.pve.seconds_until_reset = Math.max(0, S.pve.seconds_until_reset - TIMER_TICK_SECONDS);
    if (!S.pve.seconds_until_reset) dirty = true;
    else if (TAB === "arena") renderArena();
  }
  if (S.farm && S.farm.seconds_left) {
    S.farm.seconds_left = Math.max(0, S.farm.seconds_left - TIMER_TICK_SECONDS);
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

refresh().then(() => {
  if (START_VIEW === "review" && S && S.is_admin) {
    TAB = "more";
    moreView = "review";
    render();
  }
  if (START_VIEW === "economy" && S && S.is_economy_admin) {
    TAB = "more";
    moreView = "moneyaudit";
    render();
  }
  ticker = setInterval(tick, TIMER_TICK_MS);
});
</script>
</body>
</html>
"""
