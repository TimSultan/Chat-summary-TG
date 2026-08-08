"""A third page on the same aiohttp server as vote_web's and arena_web's: per-post
Telegram stats (views/forwards/reactions/comments) for a chosen chat/channel over a
chosen time range, as an interactive chart + table.

Unlike vote_web's and arena_web's Mini Apps, this page is a plain bookmarkable URL
opened in an ordinary browser -- there is no Telegram initData to verify here, so the
gate is a bearer token instead (see _authorize). Two kinds: the unscoped "owner" token
may ask for any chat the account can see; a scoped token is locked server-side to
exactly the one chat it was issued for, for handing someone read access to their own
group without exposing every other chat the owner token can reach. The page shell itself
is not gated (same reasoning as the other two: it's empty markup without data, nothing
sensitive in serving it) -- every API call under it is what's actually gated.

Mounted onto the SAME web.Application vote_web.py builds via `attach`, exactly the
pattern arena_web.py uses: its own AppKeys (post_stats_*) so nothing here can collide
with vote_web's or arena_web's, and its own two-step guard (a strict filename regex AND
a resolved-path containment check) for serving thumbnails safely.
"""

import hmac
import json
import os
import re
import traceback
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Callable

from aiohttp import web

from errors import ChatSummaryError
from main import parse_date_range, period_label, resolve_tz
from post_stats import fetch_post_stats
from post_stats_page import PAGE_HTML
from telegram_fetch import resolve_chat, sender_display_name

ROUTE_PREFIX = "/poststats"

# Mirrors stats.py's own DATA_DIR convention exactly: on a host with a mounted volume
# (DATA_DIR set) thumbnails land on persistent storage same as everything else in this
# repo; locally it's just the working directory.
DATA_DIR = Path(os.getenv("DATA_DIR", "."))
_THUMB_BASE = DATA_DIR / "cache" / "post_stats_thumbs"

_RANGE_KEYWORDS = {"today", "last7days", "last30days"}
_SAFE_CHAT_ID = re.compile(r"^-?[0-9]+$")
_SAFE_THUMB_NAME = re.compile(r"^[0-9]+\.jpg$")

_CLIENT_KEY = web.AppKey("post_stats_client")
_CFG_KEY = web.AppKey("post_stats_cfg")
_PREFIX_KEY = web.AppKey("post_stats_prefix", str)
_LOG_KEY = web.AppKey("post_stats_log", Callable[..., None])


def _json_error(message: str, status: int) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _unauthorized() -> web.HTTPUnauthorized:
    return web.HTTPUnauthorized(
        text=json.dumps({"error": "unauthorized"}), content_type="application/json"
    )


def _authorize(request: web.Request) -> str | None:
    """Checks the bearer token and says which chat it's allowed to see.

    Two kinds of token, both constant-time compared: the single "owner" token
    (post_stats_access_token) may ask for ANY chat -- this returns None, meaning "let the
    caller's own chat= param through unchanged". A scoped token (one of
    post_stats_scoped_tokens) is locked to exactly the one chat_ref it was issued for --
    this returns that chat_ref, and the caller must use it INSTEAD OF whatever chat= the
    request itself carries, so a scoped token can never be used to reach a different chat
    no matter what the request asks for.

    Fails closed (raises HTTPUnauthorized) when neither is configured at all, when the
    token is missing, or when it matches nothing -- a unit test or future caller
    shouldn't get a route that silently accepts anything just because nothing is set.
    """
    cfg = request.app[_CFG_KEY]
    provided = request.query.get("token", "")
    if not provided:
        raise _unauthorized()

    owner_token = getattr(cfg, "post_stats_access_token", None)
    if owner_token and hmac.compare_digest(provided, owner_token):
        return None

    # Checked against every scoped token, not returned on first match -- so which
    # (if any) scoped token exists is never distinguishable from response timing alone.
    matched_chat_ref = None
    for candidate_token, chat_ref in (getattr(cfg, "post_stats_scoped_tokens", None) or {}).items():
        if hmac.compare_digest(provided, candidate_token):
            matched_chat_ref = chat_ref
    if matched_chat_ref is not None:
        return matched_chat_ref

    raise _unauthorized()


async def handle_page(request: web.Request) -> web.Response:
    return web.Response(
        text=PAGE_HTML.replace("__PREFIX__", request.app[_PREFIX_KEY]),
        content_type="text/html",
    )


async def handle_data(request: web.Request) -> web.Response:
    forced_chat_ref = _authorize(request)
    chat_locked = forced_chat_ref is not None
    chat = forced_chat_ref if chat_locked else request.query.get("chat", "").strip()
    if not chat:
        return _json_error("chat is required", 400)

    range_kw = request.query.get("range")
    start_q = request.query.get("start")
    end_q = request.query.get("end")
    if range_kw in _RANGE_KEYWORDS:
        date_value = range_kw
    elif start_q and end_q:
        date_value = f"{start_q}:{end_q}"
    else:
        return _json_error(
            "provide range=today|last7days|last30days, or start and end (YYYY-MM-DD)", 400
        )

    try:
        start_day, end_day = parse_date_range(date_value)
    except ChatSummaryError as e:
        return _json_error(str(e), 400)

    tz = resolve_tz(None)
    start_utc = datetime.combine(start_day, time.min, tzinfo=tz).astimezone(timezone.utc)
    end_utc = datetime.combine(end_day, time.max, tzinfo=tz).astimezone(timezone.utc)

    client = request.app[_CLIENT_KEY]
    log = request.app[_LOG_KEY]

    try:
        entity = await resolve_chat(client, chat)
    except ChatSummaryError as e:
        return _json_error(str(e), 404)

    chat_title = getattr(entity, "title", None) or sender_display_name(entity)

    # Per-chat subdirectory: fetch_post_stats names thumbnails f"{message_id}.jpg", and
    # message ids are only unique WITHIN one chat, so a flat directory shared across chats
    # could serve the wrong picture under a colliding id.
    thumb_dir = _THUMB_BASE / str(entity.id)
    thumb_dir.mkdir(parents=True, exist_ok=True)

    try:
        posts = await fetch_post_stats(client, entity, start_utc, end_utc, thumb_dir, log=log)
    except ChatSummaryError as e:
        return _json_error(str(e), 502)
    except Exception:
        log(f"[post_stats_web] fetch failed: {traceback.format_exc()}")
        return _json_error("failed to fetch stats -- see server logs", 502)

    prefix = request.app[_PREFIX_KEY]
    return web.json_response({
        "chat_title": chat_title,
        "chat_locked": chat_locked,
        "period_label": period_label(start_day, end_day),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "posts": [
            {
                "message_id": p.message_id,
                "date": p.date.isoformat(),
                "text_preview": p.text_preview,
                "thumbnail_url": (
                    f"{prefix}/thumb/{entity.id}/{p.message_id}.jpg"
                    if p.thumbnail_path is not None else None
                ),
                "views": p.views,
                "forwards": p.forwards,
                "reactions_total": p.reactions_total,
                "reactions_breakdown": p.reactions_breakdown,
                "comments": p.comments,
                "is_edited": p.is_edited,
                "media_type": p.media_type,
                "link": p.link,
            }
            for p in posts
        ],
    })


async def handle_thumb(request: web.Request) -> web.Response:
    """One thumbnail out of _THUMB_BASE/<chat_id>/. NOT token-gated -- same reasoning as
    arena_web.handle_media/vote_web.handle_media: this is already-public Telegram post
    content, and an <img src> cannot carry a header or a checked query token cleanly
    alongside caching. Tighter name pattern than arena_web's general _SAFE_NAME since we
    control the exact filenames generated here (f"{message_id}.jpg")."""
    chat_id = request.match_info["chat_id"]
    name = request.match_info["name"]
    if not _SAFE_CHAT_ID.match(chat_id or "") or not _SAFE_THUMB_NAME.match(name or ""):
        raise web.HTTPNotFound()

    directory = _THUMB_BASE / chat_id
    path = (directory / name).resolve()
    if not str(path).startswith(str(directory.resolve())) or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


def attach(app: web.Application, client, cfg, route_prefix: str = ROUTE_PREFIX,
           log=print, page_handler=None) -> web.Application:
    """Adds the post-stats page to an existing aiohttp application -- the one vote_web
    builds, so all three systems answer on one port without any of them owning the
    others. Its own AppKeys throughout (post_stats_*), so nothing it stores can collide
    with vote_web's or arena_web's."""
    prefix = route_prefix.rstrip("/")
    app[_CLIENT_KEY] = client
    app[_CFG_KEY] = cfg
    app[_PREFIX_KEY] = prefix
    app[_LOG_KEY] = log

    app.add_routes([
        web.get(prefix, page_handler or handle_page),
        web.get(f"{prefix}/", page_handler or handle_page),
        web.get(f"{prefix}/api/data", handle_data),
        web.get(prefix + "/thumb/{chat_id}/{name}", handle_thumb),
    ])
    log(f"[post_stats_web] mounted at {prefix}")
    return app
