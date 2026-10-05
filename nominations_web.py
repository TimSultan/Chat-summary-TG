"""Vote v3's HTTP surface: the nominations Mini App, mounted beside v1's page and the
arena's on the same server (see `attach`), under its own prefix and its own AppKeys.

Every route reads and writes nominations.py's storage and nothing else -- not a poll, not
a tournament. The only thing shared with v1 at runtime is the process and the port, and
bot_listener mounts this inside a try/except, so even a broken attach leaves /vote served.

Authentication is v1's, for v1's reasons: the caller sends back the initData Telegram
handed the Mini App and voting.verify_init_data checks its signature, so the verified user
id IS the voter. Photos are the one thing served without it, because an <img> cannot send
a header and these pictures were already posted publicly in the chat.

Every disk read and write runs in a worker thread (asyncio.to_thread), together with the
response it feeds: this event loop also serves v1's ballots, and v3 must not be able to
make them wait. Each admin change answers with the whole fresh admin state, so the page
never needs a second request to show what it just did.
"""

import asyncio
import json
import re
from typing import Awaitable, Callable

from aiohttp import web

import nominations
import voting

ROUTE_PREFIX = "/nominations"

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")

_CFG_KEY = web.AppKey("nominations_cfg")
_ENTRY_KEY = web.AppKey("nominations_entry", str)
_IS_ADMIN_KEY = web.AppKey("nominations_is_admin", Callable[[dict], Awaitable[bool]])
# Fetches one author's Telegram profile photo; bot_listener owns the Bot API client, so it
# arrives as a callable, as v1's does. Its own cache: v3 must not lean on v1's app state.
_AVATAR_KEY = web.AppKey("nominations_avatar", Callable[[int], Awaitable[bytes | None]])
_AVATAR_CACHE_KEY = web.AppKey("nominations_avatar_cache", dict)
_PREFIX_KEY = web.AppKey("nominations_prefix", str)
_LOG_KEY = web.AppKey("nominations_log", Callable[..., None])


def _json_error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


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
            text=json.dumps({"error": str(e)}, ensure_ascii=False),
            content_type="application/json",
        )


async def _body(request: web.Request) -> dict | None:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return None
    return body if isinstance(body, dict) else None


# ----------------------------------------------------------------------------- payloads


def _entry_payload(entry: voting.Entry, base: str) -> dict:
    return {
        "id": entry.entry_id,
        "author": entry.author_name,
        "username": entry.author_username,
        "text": entry.text,
        "photos": [f"{base}/media/{name}" for name in entry.media],
        "avatar": f"{base}/avatar/{entry.author_id}" if entry.author_id is not None else None,
    }


def _nomination_payload(contest: nominations.Contest, nomination, user_id, admin_mode: bool) -> dict:
    """One tab. `results` follows v1's rule: withheld from a voter who has not voted in
    THIS nomination while it is still open, since a running count shown first biases the
    pick. Voting in one tab earns that tab's standings, not every tab's."""
    my_vote = contest.ballot(nomination, user_id)
    payload = {
        "id": nomination.nomination_id,
        "name": nomination.name,
        "entry_ids": contest.members(nomination),
        "my_vote": my_vote,
        "voter_count": contest.voter_count(nomination),
        "results": None,
    }
    if admin_mode or my_vote or not contest.open:
        payload["results"] = [
            {"id": work.entry_id, "votes": votes} for work, votes in contest.tally(nomination)
        ]
    return payload


def _state_payload(contest, user_id, admin_mode: bool, can_moderate: bool, base: str,
                   vote_admitted: set[str] | None = None) -> dict:
    """Everything the page draws, and only that.

    A voter gets the nominations that have works in them and the works those name -- not
    the pool, which is the administrator's raw material and would be sent to every voter
    for nothing. The administrator gets the whole pool, since adding works to a nomination
    is choosing from it.

    `vote_admitted` -- which pool works /vote has admitted to its own ballot -- is sent
    only when it was just read (the administrator's state load, which syncs from /vote);
    the page keeps the last one it got. Re-reading v1 on every edit would be a read of
    another system's files per tap, for a filter that changes once a week."""
    if contest is None:
        payload = {
            "exists": False, "open": False, "max_choices": None,
            "is_admin": admin_mode, "can_moderate": can_moderate,
            "nominations": [], "entries": [],
        }
        if vote_admitted is not None:
            payload["vote_admitted"] = []
        return payload
    if admin_mode:
        shown = list(contest.nominations)
        entries = contest.entries
    else:
        shown = [n for n in contest.nominations if contest.members(n)]
        used = {entry_id for n in shown for entry_id in n.entry_ids}
        entries = [e for e in contest.entries if e.entry_id in used]
    payload = {
        "exists": True,
        "open": contest.open,
        "max_choices": contest.max_choices,
        "is_admin": admin_mode,
        "can_moderate": can_moderate,
        "nominations": [_nomination_payload(contest, n, user_id, admin_mode) for n in shown],
        "entries": [_entry_payload(e, base) for e in entries],
    }
    if vote_admitted is not None:
        payload["vote_admitted"] = [e.entry_id for e in entries if e.entry_id in vote_admitted]
    return payload


# ----------------------------------------------------------------------------- handlers


async def handle_state(request: web.Request) -> web.Response:
    """The page's one read. `?mode=admin` asks for the editing view, and being an
    administrator is not by itself enough to get it -- otherwise an administrator could
    never open the plain ballot to vote in it themselves (v1's rule, same reason)."""
    user = await _authenticate(request)
    can_moderate = await request.app[_IS_ADMIN_KEY](user)
    admin_mode = can_moderate and request.query.get("mode") == "admin"
    entry = request.app[_ENTRY_KEY]
    base = request.app[_PREFIX_KEY]
    log = request.app[_LOG_KEY]

    def build() -> dict:
        if not admin_mode:
            contest = nominations.load_contest(entry)
            return _state_payload(contest, user["id"], False, can_moderate, base)
        # The editing view is where the pool is chosen from, so it is brought up to date
        # with /vote first -- there is no import step for anybody to forget.
        try:
            contest, added, admitted = nominations.sync_from_v1(entry)
            if added:
                log(f"[nominations] {added} new work(s) from /vote")
        except nominations.ContestError as e:
            log(f"[nominations] could not sync from /vote: {e.message}")
            contest, admitted = nominations.load_contest(entry), None
        return _state_payload(contest, user["id"], True, can_moderate, base, admitted)

    return web.json_response(await asyncio.to_thread(build))


async def handle_ballot(request: web.Request) -> web.Response:
    """Records this voter's choices in ONE nomination, replacing their earlier ballot
    there and leaving every other nomination's alone."""
    body = await _body(request)
    if body is None:
        return _json_error("malformed request body")
    user = await _authenticate(request, body)
    entry = request.app[_ENTRY_KEY]
    nomination_id = body.get("nomination_id")
    choices = body.get("choices")

    def vote() -> dict:
        contest, _ = nominations.update_contest(
            entry, lambda c: nominations.record_vote(c, nomination_id, user["id"], choices),
        )
        return _nomination_payload(contest, contest.nomination(str(nomination_id)), user["id"], False)

    try:
        nomination = await asyncio.to_thread(vote)
    except nominations.ContestError as e:
        return _json_error(e.message, e.status)
    request.app[_LOG_KEY](
        f"[nominations] ballot from {voting.display_name(user)} in {nomination['name']!r}: "
        f"{len(nomination['my_vote'])} choice(s)"
    )
    return web.json_response({"ok": True, "nomination": nomination})


def _admin_route(action: str, mutate, create: bool = False):
    """An administrator-only change: authenticate, check, apply `mutate(contest, body)`
    under the write lock, answer with the fresh admin state. The four editing routes differ
    only in the mutation, so they are built here rather than written out four times."""
    async def handler(request: web.Request) -> web.Response:
        body = await _body(request)
        if body is None:
            return _json_error("malformed request body")
        user = await _authenticate(request, body)
        if not await request.app[_IS_ADMIN_KEY](user):
            return _json_error("только администраторы могут настраивать номинации", status=403)
        entry = request.app[_ENTRY_KEY]
        base = request.app[_PREFIX_KEY]

        def apply() -> tuple[dict, object]:
            contest, result = nominations.update_contest(
                entry, lambda c: mutate(c, body), create=create,
            )
            return _state_payload(contest, user["id"], True, True, base), result

        try:
            state, result = await asyncio.to_thread(apply)
        except nominations.ContestError as e:
            return _json_error(e.message, e.status)
        request.app[_LOG_KEY](f"[nominations] {voting.display_name(user)}: {action}")
        payload = {"ok": True, "state": state}
        if isinstance(result, nominations.Nomination):
            payload["nomination_id"] = result.nomination_id
        return web.json_response(payload)

    return handler


def _update(contest: nominations.Contest, body: dict):
    nomination_id = body.get("nomination_id")
    nomination = None
    if "name" in body:
        nomination = nominations.rename_nomination(contest, nomination_id, body["name"])
    if "entry_ids" in body:
        nomination = nominations.set_nomination_entries(contest, nomination_id, body["entry_ids"])
    if nomination is None:
        raise nominations.ContestError("нечего менять: нет ни name, ни entry_ids")
    return nomination


handle_create = _admin_route(
    "created a nomination", lambda c, body: nominations.add_nomination(c, body.get("name")),
    # A nomination can be named before any work is collected; the pool fills in later.
    create=True,
)
handle_update = _admin_route("edited a nomination", _update)
handle_delete = _admin_route(
    "deleted a nomination", lambda c, body: nominations.delete_nomination(c, body.get("nomination_id")),
)
handle_settings = _admin_route("changed the settings", nominations.set_settings)


async def handle_media(request: web.Request) -> web.Response:
    """One photo out of v3's own media directory. Same two-step guard as v1's: a strict
    name pattern AND a containment check on the resolved path."""
    name = request.match_info["name"]
    if not _SAFE_NAME.match(name or ""):
        raise web.HTTPNotFound()
    directory = nominations.media_path(request.app[_ENTRY_KEY])
    path = (directory / name).resolve()
    if not str(path).startswith(str(directory.resolve())) or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


async def handle_avatar(request: web.Request) -> web.Response:
    """An author's current Telegram avatar, for a card in this contest.

    Public for the photos' reason (an <img> cannot sign itself), but not an arbitrary
    Telegram-user lookup: the id must be the author of a work in the pool. Found photos and
    "has no photo" are both cached for the process; a failed fetch is not, so it can retry.
    """
    raw_user_id = request.match_info["user_id"]
    if not raw_user_id.isdigit():
        raise web.HTTPNotFound()
    user_id = int(raw_user_id)
    cache = request.app[_AVATAR_CACHE_KEY]
    if user_id not in cache:
        contest = await asyncio.to_thread(nominations.load_contest, request.app[_ENTRY_KEY])
        if contest is None or not any(work.author_id == user_id for work in contest.entries):
            raise web.HTTPNotFound()
        try:
            avatar = await request.app[_AVATAR_KEY](user_id)
        except Exception as e:
            request.app[_LOG_KEY](f"[nominations] could not fetch avatar for {user_id}: {e}")
            raise web.HTTPServiceUnavailable()
        cache[user_id] = bytes(avatar) if avatar else None
    if not cache[user_id]:
        raise web.HTTPNotFound()
    return web.Response(body=cache[user_id], content_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


async def handle_page(request: web.Request) -> web.Response:
    return web.Response(
        text=PAGE_HTML.replace("__PREFIX__", request.app[_PREFIX_KEY]),
        content_type="text/html",
    )


def attach(app: web.Application, cfg, entry: str, is_admin, log=print,
           route_prefix: str = ROUTE_PREFIX, avatar=None) -> web.Application:
    """Adds v3 to the application vote_web.create_app builds. Its own AppKeys throughout
    (nominations_*), so nothing it stores can collide with v1's or the arena's. `avatar`
    takes an author id and returns their profile photo bytes or None; without it every
    card shows the author's initial instead."""
    async def _no_avatar(user_id):
        return None

    prefix = route_prefix.rstrip("/")
    app[_CFG_KEY] = cfg
    app[_ENTRY_KEY] = entry
    app[_IS_ADMIN_KEY] = is_admin
    app[_AVATAR_KEY] = avatar or _no_avatar
    app[_AVATAR_CACHE_KEY] = {}
    app[_PREFIX_KEY] = prefix
    app[_LOG_KEY] = log
    app.add_routes([
        web.get(prefix, handle_page),
        web.get(f"{prefix}/", handle_page),
        web.get(f"{prefix}/api/state", handle_state),
        web.post(f"{prefix}/api/ballot", handle_ballot),
        web.post(f"{prefix}/api/nominations/create", handle_create),
        web.post(f"{prefix}/api/nominations/update", handle_update),
        web.post(f"{prefix}/api/nominations/delete", handle_delete),
        web.post(f"{prefix}/api/settings", handle_settings),
        web.get(prefix + "/media/{name}", handle_media),
        web.get(prefix + "/avatar/{user_id}", handle_avatar),
    ])
    log(f"[nominations] mounted at {prefix}")
    return app


PAGE_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Номинации</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  /* v1's palette, pinned dark for v1's reason: the photos read against navy, and a light
     client would put them on white under grey text. */
  :root {
    color-scheme: dark;
    --bg: #17212b;
    --surface: #1d2834;
    --card: #232e3c;
    --line: rgba(255,255,255,.08);
    --fg: #f5f5f5;
    --muted: #8a9aa9;
    --accent: #3390ec;
    --accent-soft: rgba(51,144,236,.16);
    --accent-fg: #fff;
    --good: #4fbf77;
    --good-soft: rgba(79,191,119,.16);
    --danger: #e5534b;
    --radius: 14px;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  [hidden] { display: none !important; }
  html, body { overflow-x: hidden; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding-bottom: 110px;
  }
  button { font: inherit; color: inherit; }

  /* ------------------------------------------------------------------ header */
  .hero { padding: 18px 16px 10px; }
  .eyebrow { color: var(--muted); font-size: 12px; letter-spacing: .06em; text-transform: uppercase; }
  .hero h1 { font-size: 22px; line-height: 1.2; margin: 4px 0 12px; }
  .progress { display: flex; gap: 4px; }
  .progress span { flex: 1; height: 5px; border-radius: 3px; background: rgba(255,255,255,.1);
                   transition: background .3s; }
  .progress span.done { background: var(--good); }
  .progress span.here { background: var(--accent); }
  .progressText { margin-top: 8px; font-size: 13px; color: var(--muted); }
  .progressText.complete { color: var(--good); font-weight: 600; }
  .adminStats { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; color: var(--muted); font-size: 13px; }
  .pill { display: inline-flex; align-items: center; gap: 5px; padding: 3px 10px; border-radius: 999px;
          font-size: 12px; font-weight: 600; }
  .pill.open { background: var(--good-soft); color: var(--good); }
  .pill.closed { background: rgba(229,83,75,.15); color: var(--danger); }

  /* -------------------------------------------------------------------- tabs */
  /* Sticky: switching nomination is what this page is for, and it should never need a
     scroll back up. */
  /* Opaque, not frosted: a photo sliding under a translucent strip shows its captions
     through it, and that reads as text bleeding into the controls. */
  .tabs { position: sticky; top: 0; z-index: 5; background: var(--bg);
          display: flex; gap: 8px; overflow-x: auto; padding: 10px 16px;
          scrollbar-width: none; border-bottom: 1px solid var(--line); }
  .tabs::-webkit-scrollbar { display: none; }
  .tab { flex: none; display: inline-flex; align-items: center; gap: 7px; cursor: pointer;
         border: 1px solid rgba(255,255,255,.14); border-radius: 999px; background: transparent;
         padding: 6px 14px 6px 6px; font-size: 14px; white-space: nowrap;
         transition: background .2s, border-color .2s; }
  .tab .tabNum { width: 24px; height: 24px; border-radius: 50%; display: inline-flex;
                 align-items: center; justify-content: center; font-size: 12px; font-weight: 700;
                 background: rgba(255,255,255,.1); }
  .tab.voted .tabNum { background: var(--good); color: #fff; }
  .tab.active { background: var(--accent); border-color: var(--accent); color: var(--accent-fg); font-weight: 600; }
  .tab.active .tabNum { background: rgba(255,255,255,.25); }
  .tab.active.voted .tabNum { background: #fff; color: var(--accent); }
  .tab .tabCount { font-size: 12px; opacity: .75; }
  .tab.add { padding: 6px 14px; border-style: dashed; color: var(--accent); border-color: var(--accent); }

  /* ---------------------------------------------------------- nomination head */
  .nomHero { padding: 16px 16px 4px; display: flex; gap: 10px; align-items: flex-start; }
  .nomHero .text { flex: 1; min-width: 0; }
  .nomIndex { color: var(--muted); font-size: 12px; letter-spacing: .04em; text-transform: uppercase; }
  .nomName { font-size: 24px; font-weight: 800; line-height: 1.15; margin: 2px 0 4px;
             overflow-wrap: anywhere; }
  .nomHint { color: var(--muted); font-size: 13px; }
  .iconBtn { flex: none; width: 40px; height: 40px; border-radius: 12px; cursor: pointer;
             border: 1px solid rgba(255,255,255,.14); background: transparent; font-size: 16px; }
  .iconBtn.danger { border-color: rgba(229,83,75,.6); }

  .panel { margin: 10px 16px 0; padding: 12px 14px; border-radius: var(--radius); background: var(--card); }
  .panel .row { display: flex; align-items: center; justify-content: space-between; gap: 10px; font-size: 13px; }
  .panel input[type="number"] { width: 64px; text-align: center; border-radius: 10px;
              border: 1px solid rgba(255,255,255,.18); background: var(--bg);
              color: var(--fg); padding: 7px; font-size: 14px; }
  .formTitle { margin-bottom: 8px; color: var(--muted); font-size: 13px; }
  .nomForm { display: flex; gap: 8px; }
  .nomForm input { flex: 1; min-width: 0; border-radius: 10px; padding: 10px 12px;
                   border: 1px solid rgba(255,255,255,.18); background: var(--bg); color: var(--fg); font: inherit; }
  .nomForm input:focus { outline: none; border-color: var(--accent); }
  .nomForm button { border: 0; border-radius: 10px; padding: 10px 14px; font-weight: 600;
                    background: var(--accent); color: var(--accent-fg); cursor: pointer; }
  .nomForm button.cancel { background: transparent; color: var(--muted);
                           border: 1px solid rgba(255,255,255,.18); font-weight: 400; }

  /* Administrator's filter over the pool. */
  .segments { display: flex; margin: 12px 16px 0; padding: 3px; border-radius: 12px; background: var(--card); gap: 3px; }
  .segments button { flex: 1; border: 0; border-radius: 9px; padding: 7px 4px; background: transparent;
                     color: var(--muted); font-size: 12px; cursor: pointer; }
  .segments button b { color: var(--fg); font-weight: 600; margin-left: 3px; }
  .segments button.on { background: var(--accent); color: var(--accent-fg); }
  .segments button.on b { color: var(--accent-fg); }
  .adminHint { margin: 8px 16px 0; color: var(--muted); font-size: 12px; }

  /* -------------------------------------------------------------------- works */
  /* Two across for voters: few works per nomination, and judging a painting wants a
     picture bigger than a thumbnail. The administrator's pool is denser, three across. */
  .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; padding: 14px 16px; }
  .grid.dense { grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
  @media (min-width: 640px) { .grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
                              .grid.dense { grid-template-columns: repeat(5, minmax(0, 1fr)); } }
  .gcard { position: relative; background: var(--card); border-radius: var(--radius); overflow: hidden;
           box-shadow: 0 0 0 1px var(--line); transition: box-shadow .2s, transform .15s, opacity .2s; }
  .gcard.on { box-shadow: 0 0 0 2px var(--accent), 0 6px 18px rgba(51,144,236,.25); }
  .gcard.out { opacity: .45; }
  .gcard:active { transform: scale(.985); }
  .thumb { position: relative; display: block; width: 100%; aspect-ratio: 1; overflow: hidden;
           background: rgba(255,255,255,.05); cursor: zoom-in; }
  .thumb > img.photo { width: 100%; height: 100%; object-fit: cover; display: block; }
  .badge { position: absolute; top: 8px; padding: 2px 8px; border-radius: 999px; font-size: 11px;
           font-weight: 600; background: rgba(0,0,0,.55); color: #fff; }
  .badge.more { right: 8px; }
  .badge.votes { left: 8px; background: var(--accent); }
  .mark { position: absolute; right: 8px; bottom: 8px; width: 30px; height: 30px; border-radius: 50%;
          background: var(--accent); color: #fff; display: flex; align-items: center; justify-content: center;
          font-weight: 800; font-size: 15px; box-shadow: 0 2px 8px rgba(0,0,0,.4);
          transform: scale(0); transition: transform .25s cubic-bezier(.3,1.6,.6,1); }
  .gcard.on .mark { transform: scale(1); }
  .meta { display: flex; align-items: center; gap: 7px; padding: 8px 10px 0; min-width: 0; }
  .grid.dense .meta { padding: 6px 7px 0; }
  .name { font-size: 13px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .grid.dense .name { font-size: 11px; font-weight: 500; }
  .ava { position: relative; flex: none; width: 24px; height: 24px; border-radius: 50%; overflow: hidden;
         background: var(--accent-soft); color: var(--accent); font-size: 11px; font-weight: 700;
         display: inline-flex; align-items: center; justify-content: center; }
  .ava img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
  .grid.dense .ava { width: 18px; height: 18px; font-size: 9px; }
  .ava.big { width: 40px; height: 40px; font-size: 16px; }
  .pick { display: block; width: calc(100% - 16px); margin: 8px; border-radius: 10px; cursor: pointer;
          border: 1px solid var(--accent); background: transparent; color: var(--accent);
          padding: 9px 4px; font-size: 14px; font-weight: 600; transition: background .2s, color .2s; }
  .grid.dense .pick { width: calc(100% - 10px); margin: 6px 5px; padding: 6px 2px; font-size: 11px;
                      border-color: rgba(255,255,255,.18); color: var(--muted); font-weight: 500; }
  .gcard.on .pick, .grid.dense .gcard.on .pick { background: var(--accent); border-color: var(--accent); color: var(--accent-fg); }
  .pick[disabled], .pickBtn[disabled] { opacity: .45; cursor: default; }

  .empty { margin: 28px 24px; text-align: center; color: var(--muted); }
  .empty .icon { font-size: 40px; margin-bottom: 8px; }
  .empty b { display: block; color: var(--fg); font-size: 16px; margin-bottom: 4px; }
  .notice { margin: 12px 16px 0; padding: 12px 14px; border-radius: var(--radius);
            background: var(--card); color: var(--muted); font-size: 13px; text-align: center; }

  /* ------------------------------------------------------------------ results */
  .results { margin: 4px 16px 16px; padding: 14px; border-radius: var(--radius); background: var(--card); }
  .results h3 { margin: 0; font-size: 15px; }
  .results .sub { color: var(--muted); font-size: 12px; margin: 2px 0 12px; }
  /* One grid for the whole table so every bar starts at the same x (v1's lesson). */
  .results .table { display: grid; align-items: center; column-gap: 8px; row-gap: 8px;
                    grid-template-columns: auto 26px minmax(0, 40%) minmax(0, 1fr) auto; }
  .results .rank { color: var(--muted); font-size: 12px; text-align: right; font-variant-numeric: tabular-nums; }
  .results .mini { width: 26px; height: 26px; border-radius: 6px; object-fit: cover; display: block;
                   background: rgba(255,255,255,.06); }
  .results .who { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
  .results .who.mine { color: var(--accent); font-weight: 600; }
  .results .track { height: 8px; border-radius: 4px; background: rgba(255,255,255,.08); overflow: hidden; }
  .results .fill { display: block; height: 100%; border-radius: 4px; background: var(--accent);
                   transition: width .5s ease; }
  .results .num { text-align: right; font-size: 13px; font-weight: 600; font-variant-numeric: tabular-nums; }

  /* ---------------------------------------------------------------------- bar */
  .bar { position: fixed; left: 0; right: 0; bottom: 0; z-index: 20; display: flex; gap: 8px;
         padding: 10px 16px calc(10px + env(safe-area-inset-bottom));
         background: var(--bg); border-top: 1px solid var(--line); }
  .go { flex: 1; border: 0; border-radius: 12px; padding: 14px; cursor: pointer;
        font-size: 15px; font-weight: 700; background: var(--accent); color: var(--accent-fg);
        overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .go.ghost { background: transparent; color: var(--fg); border: 1px solid rgba(255,255,255,.18); font-weight: 600; }
  .go.danger { background: transparent; color: var(--danger); border: 1px solid var(--danger); }
  .go.square { flex: none; width: 52px; padding: 0; font-size: 20px; }
  .go[disabled] { opacity: .4; cursor: default; }
  .doneNote { flex: 1; text-align: center; padding: 13px; color: var(--good); font-weight: 600; }

  /* -------------------------------------------------------------------- toast */
  .toast { position: fixed; left: 50%; bottom: calc(86px + env(safe-area-inset-bottom)); z-index: 40;
           transform: translate(-50%, 20px); opacity: 0; pointer-events: none;
           padding: 9px 16px; border-radius: 999px; background: #0e161e; color: var(--fg);
           font-size: 13px; box-shadow: 0 6px 20px rgba(0,0,0,.4); white-space: nowrap;
           transition: opacity .25s, transform .25s; }
  .toast.show { opacity: 1; transform: translate(-50%, 0); }
  .toast.bad { background: var(--danger); color: #fff; }

  /* ------------------------------------------------------------ thank-you popup */
  .modal { position: fixed; inset: 0; z-index: 50; display: flex; align-items: center; justify-content: center;
           padding: 24px; background: rgba(5,10,15,.72); animation: fade .2s ease; }
  .sheet { width: min(100%, 360px); padding: 26px 22px 18px; border-radius: 22px; background: var(--card);
           text-align: center; box-shadow: 0 20px 60px rgba(0,0,0,.5); animation: pop .32s cubic-bezier(.2,1.3,.5,1); }
  .okIcon { width: 56px; height: 56px; margin: 0 auto 12px; border-radius: 50%; background: var(--good);
            color: #fff; font-size: 30px; font-weight: 800; display: flex; align-items: center; justify-content: center;
            box-shadow: 0 0 0 8px var(--good-soft); }
  .sheet h2 { margin: 0 0 4px; font-size: 21px; }
  .sheet .where { margin: 0; color: var(--muted); font-size: 14px; }
  .nextBlock { margin: 20px 0 18px; }
  .nextLabel { color: var(--muted); font-size: 12px; letter-spacing: .06em; text-transform: uppercase; }
  /* The big number: which nomination is next, in the same numbering the tabs carry. */
  .bigNum { width: 112px; height: 112px; margin: 10px auto 6px; border-radius: 50%;
            display: flex; align-items: center; justify-content: center;
            font-size: 60px; font-weight: 800; line-height: 1; color: var(--accent);
            background: var(--accent-soft); box-shadow: inset 0 0 0 3px var(--accent);
            font-variant-numeric: tabular-nums; }
  .bigNum.complete { color: var(--good); background: var(--good-soft); box-shadow: inset 0 0 0 3px var(--good); }
  .bigOf { color: var(--muted); font-size: 13px; }
  .nextName { margin-top: 6px; font-size: 18px; font-weight: 700; overflow-wrap: anywhere; }
  .sheet .go { display: block; width: 100%; white-space: normal; line-height: 1.3; }
  .sheet .go + .go { margin-top: 8px; }
  @keyframes pop { from { transform: scale(.86); opacity: 0; } to { transform: scale(1); opacity: 1; } }
  @keyframes fade { from { opacity: 0; } to { opacity: 1; } }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation: none !important; transition: none !important; }
  }

  /* ----------------------------------------------------------- reel and lens */
  .reel { position: fixed; inset: 0; z-index: 10; background: var(--bg); overflow-y: auto;
          -webkit-overflow-scrolling: touch; }
  body.reelOpen { overflow: hidden; }
  .reelClose { position: fixed; top: 10px; right: 10px; z-index: 12; border: 0; border-radius: 50%;
               width: 38px; height: 38px; background: rgba(0,0,0,.55); color: #fff; font-size: 17px;
               line-height: 1; cursor: pointer; }
  .feed { padding: 12px 16px calc(var(--barH, 96px) + 16px); }
  .rcard { padding: 16px 0; border-bottom: 1px solid var(--line); }
  .rcard:last-child { border-bottom: 0; }
  .rhead { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; padding-right: 44px; }
  .rhead .text { min-width: 0; }
  .rhead .name { font-size: 15px; }
  .rhead .tag { color: var(--muted); font-size: 12px; }
  .rcard .cap { white-space: pre-wrap; margin: 0 0 10px; font-size: 14px; }
  .rcard .photos { display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px; }
  .rcard .photos img { width: 100%; border-radius: 12px; display: block; cursor: pointer; }
  .rcard.out { opacity: .6; }
  .shot { position: relative; }
  .zoomBtn { position: absolute; right: 10px; bottom: 10px; z-index: 2; border: 0; border-radius: 50%;
             width: 38px; height: 38px; background: rgba(0,0,0,.55); color: #fff; font-size: 16px;
             line-height: 1; display: flex; align-items: center; justify-content: center; cursor: pointer; }
  .pickBtn { display: block; width: 100%; border-radius: 12px; padding: 12px; cursor: pointer;
             font-size: 15px; font-weight: 700; border: 1px solid var(--accent); background: transparent;
             color: var(--accent); }
  .rcard.on .pickBtn { background: var(--accent); color: var(--accent-fg); }
  .lens { position: fixed; inset: 0; z-index: 30; background: #000; touch-action: none; overscroll-behavior: contain; }
  .lensStage { position: absolute; inset: 0; overflow: hidden; touch-action: none; }
  .lensStage img { position: absolute; left: 0; top: 0; transform-origin: 0 0; max-width: none; display: block;
                   user-select: none; -webkit-user-drag: none; -webkit-user-select: none; }
</style>
</head>
<body>
<header class="hero">
  <div class="eyebrow" id="eyebrow">Голосование</div>
  <h1 id="title">Номинации</h1>
  <div id="heroBody"><div class="progressText">Загружаю…</div></div>
</header>
<div class="panel" id="settings" hidden>
  <div class="row">
    <span>Сколько работ можно выбрать в каждой номинации</span>
    <input type="number" id="maxChoices" min="1" placeholder="∞">
  </div>
</div>
<nav class="tabs" id="tabs" hidden></nav>
<div class="panel" id="formPanel" hidden>
  <div class="formTitle" id="formTitle">Новая номинация</div>
  <form class="nomForm" id="nomForm">
    <input id="nomName" maxlength="40" autocomplete="off" placeholder="Например: Аниме">
    <button type="submit" id="nomSubmit">Создать</button>
    <button type="button" class="cancel" id="nomCancel" aria-label="Отмена">✕</button>
  </form>
</div>
<section class="nomHero" id="nomHero" hidden>
  <div class="text">
    <div class="nomIndex" id="nomIndex"></div>
    <div class="nomName" id="nomTitle"></div>
    <div class="nomHint" id="nomHint"></div>
  </div>
  <button class="iconBtn" id="renameBtn" aria-label="Переименовать" hidden>✏️</button>
  <button class="iconBtn danger" id="deleteBtn" aria-label="Удалить" hidden>🗑</button>
</section>
<div class="segments" id="segments" hidden></div>
<div class="adminHint" id="adminHint" hidden>Нажмите «Добавить» под работой, чтобы включить её в номинацию, или ещё раз — чтобы убрать. «Допущены» — работы, допущенные в основном голосовании /vote.</div>
<div class="notice" id="notice" hidden></div>
<div class="grid" id="grid"></div>
<div class="empty" id="empty" hidden></div>
<div class="results" id="results" hidden></div>
<div class="bar" id="bar" hidden></div>
<div class="toast" id="toast"></div>

<div class="modal" id="thanks" hidden>
  <div class="sheet" role="dialog" aria-modal="true" aria-labelledby="thanksTitle">
    <div class="okIcon">✓</div>
    <h2 id="thanksTitle">Спасибо за ваш голос!</h2>
    <p class="where" id="thanksWhere"></p>
    <div class="nextBlock">
      <div class="nextLabel" id="thanksLabel"></div>
      <div class="bigNum" id="thanksNum"></div>
      <div class="bigOf" id="thanksOf"></div>
      <div class="nextName" id="thanksNext"></div>
    </div>
    <button class="go" id="thanksGo"></button>
    <button class="go ghost" id="thanksStay"></button>
  </div>
</div>

<div class="reel" id="reel" hidden>
  <button class="reelClose" id="reelClose" aria-label="Закрыть">✕</button>
  <div class="feed" id="feed"></div>
</div>

<div class="lens" id="lens" hidden>
  <div class="lensStage" id="lensStage"><img id="lensImg" alt=""></div>
  <button class="reelClose" id="lensClose" aria-label="Закрыть">✕</button>
</div>

<script>
const PREFIX = "__PREFIX__";
const tg = window.Telegram && window.Telegram.WebApp;
if (tg) {
  tg.ready(); tg.expand();
  const ask = (method, colour, since) => {
    try { if (tg.isVersionAtLeast(since)) tg[method](colour); } catch (e) {}
  };
  ask("setBackgroundColor", "#17212b", "6.1");
  ask("setHeaderColor", "#17212b", "6.9");
  ask("setBottomBarColor", "#17212b", "7.10");
}
const initData = (tg && tg.initData) || "";
const MODE = new URLSearchParams(location.search).get("mode");

let state = null;          // the server's state, see nominations_web._state_payload
let active = null;         // id of the nomination whose tab is open
let works = new Map();     // entry id -> entry payload
let inVote = new Set();    // administrator: pool works /vote has admitted (kept between edits)
let filter = "all";        // administrator: "all" | "vote" | "members"
let formMode = null;       // "create" | "rename" while the name form is open
let ballotInFlight = false;
let thanksTarget = null;   // nomination the thank-you popup's main button leads to
let toastTimer = null;
// Administrator only: nomination id -> {inFlight, dirty}. Membership taps apply at once
// on screen and are sent in the background; a tap made while a save is in flight marks
// it dirty, and the save loop sends the latest set again, so the last tap always wins.
const saving = {};

const $ = (id) => document.getElementById(id);

if (window.ResizeObserver) {
  new ResizeObserver((entries) => {
    const height = $("bar").hidden ? 0 : entries[0].contentRect.height;
    document.documentElement.style.setProperty("--barH", height + "px");
    document.body.style.paddingBottom = (height + 24) + "px";
  }).observe($("bar"));
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function displayName(entry) { return entry.author || (entry.username ? "@" + entry.username : "Автор"); }

function avatarHtml(entry, extra) {
  const initial = Array.from(String(entry.author || entry.username || "?").trim())[0] || "?";
  return '<span class="ava' + (extra ? " " + extra : "") + '">' + esc(initial.toUpperCase()) +
    (entry.avatar ? '<img loading="lazy" src="' + esc(entry.avatar) + '" alt="" onerror="this.remove()">' : "") +
    "</span>";
}

function haptic(kind) {
  if (!tg || !tg.HapticFeedback) return;
  try {
    if (kind === "select") tg.HapticFeedback.selectionChanged();
    else tg.HapticFeedback.notificationOccurred(kind);
  } catch (e) {}
}

function toast(text, bad) {
  const box = $("toast");
  box.textContent = text;
  box.classList.toggle("bad", !!bad);
  box.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => box.classList.remove("show"), bad ? 3200 : 1800);
}

async function call(path, body) {
  const options = { headers: { "X-Telegram-Init-Data": initData } };
  if (body !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(Object.assign({ init_data: initData }, body));
  }
  const response = await fetch(PREFIX + path, options);
  let data = {};
  try { data = await response.json(); } catch (e) {}
  if (!response.ok) throw new Error(data.error || "Не получилось — попробуйте ещё раз");
  return data;
}

function nom(id) { return state.nominations.find((n) => n.id === id) || null; }
function current() { return nom(active); }
function position(n) { return state.nominations.indexOf(n) + 1; }
function voted(n) { return n.my_vote && n.my_vote.length > 0; }
function allVoted() { return state.nominations.length > 0 && state.nominations.every(voted); }

// The works the grid and the reel show: the open nomination's for a voter; for the
// administrator, the pool through the chosen filter.
function shownWorks() {
  const n = current();
  if (!state.is_admin) return n ? n.entry_ids.map((id) => works.get(id)).filter(Boolean) : [];
  if (filter === "vote") return state.entries.filter((e) => inVote.has(e.id));
  if (filter === "members") return n ? state.entries.filter((e) => n.entry_ids.includes(e.id)) : [];
  return state.entries;
}

function applyState(data) {
  state = data;
  works = new Map(state.entries.map((e) => [e.id, e]));
  if (Array.isArray(data.vote_admitted)) inVote = new Set(data.vote_admitted);
  if (!current()) {
    // A voter lands on the first nomination they have not voted in: that is where the
    // next thing for them to do is.
    const fresh = state.is_admin ? null : state.nominations.find((n) => !voted(n));
    active = (fresh || state.nominations[0] || {}).id || null;
  }
}

// The next nomination still waiting for this voter's vote, looking forward from the open
// one and wrapping round; null once they have voted everywhere.
function nextUnvoted() {
  const list = state.nominations;
  const at = list.findIndex((n) => n.id === active);
  const order = list.slice(at + 1).concat(list.slice(0, Math.max(at, 0)));
  return order.find((n) => !voted(n)) || null;
}

// ------------------------------------------------------------------------- rendering

function renderHero() {
  const list = state.nominations;
  if (state.is_admin) {
    $("eyebrow").textContent = "Настройка · тестовая версия";
    $("title").textContent = "Номинации";
    $("heroBody").innerHTML = '<div class="adminStats">' +
      '<span class="pill ' + (state.open ? "open" : "closed") + '">' +
        (state.open ? "● Голосование открыто" : "● Голосование закрыто") + "</span>" +
      "<span>Работ из /vote: " + state.entries.length + " · номинаций: " + list.length + "</span></div>";
    return;
  }
  $("eyebrow").textContent = "Голосование";
  $("title").textContent = "Номинации";
  if (!list.length) { $("heroBody").innerHTML = ""; return; }
  const done = list.filter(voted).length;
  const segments = list.map((n) =>
    '<span class="' + (voted(n) ? "done" : (n.id === active ? "here" : "")) + '"></span>').join("");
  const text = !state.open ? "Голосование закрыто — смотрите итоги в каждой номинации"
    : (allVoted() ? "Спасибо! Вы проголосовали во всех номинациях"
                  : "Вы проголосовали в " + done + " из " + list.length + " номинаций");
  $("heroBody").innerHTML = '<div class="progress">' + segments + "</div>" +
    '<div class="progressText' + (allVoted() && state.open ? " complete" : "") + '">' + esc(text) + "</div>" +
    (state.can_moderate ? '<div class="progressText">Вы администратор — настройка: /vote3 выбрать</div>' : "");
}

function renderTabs() {
  const tabs = $("tabs");
  const items = state.nominations.map((n, i) => {
    const classes = "tab" + (n.id === active ? " active" : "") + (!state.is_admin && voted(n) ? " voted" : "");
    const badge = !state.is_admin && voted(n) ? "✓" : String(i + 1);
    return '<button class="' + classes + '" data-tab="' + esc(n.id) + '">' +
      '<span class="tabNum">' + badge + "</span>" + esc(n.name) +
      (state.is_admin ? ' <span class="tabCount">' + n.entry_ids.length + "</span>" : "") + "</button>";
  });
  // First, not last: at the end of a long strip it scrolls out of sight, and adding a
  // nomination is the one thing an administrator has to be able to find.
  if (state.is_admin) items.unshift('<button class="tab add" data-add="1">＋ Номинация</button>');
  // The strip is redrawn on every tap, so it keeps its own scroll and only slides
  // sideways to the open tab -- scrollIntoView could move the PAGE as well.
  const keep = tabs.scrollLeft;
  tabs.innerHTML = items.join("");
  tabs.hidden = items.length === 0;
  tabs.scrollLeft = keep;
  const on = tabs.querySelector(".tab.active");
  if (on) {
    const left = on.offsetLeft, right = left + on.offsetWidth;
    if (left < tabs.scrollLeft + 16) tabs.scrollLeft = left - 16;
    else if (right > tabs.scrollLeft + tabs.clientWidth - 16) tabs.scrollLeft = right - tabs.clientWidth + 16;
  }
}

function hintFor() {
  const cap = state.max_choices;
  if (!state.open) return "Голосование закрыто";
  const pick = cap === 1 ? "Выберите одну работу" : cap ? "Выберите до " + cap + " работ" : "Выберите понравившиеся работы";
  return pick + " · нажмите на фото, чтобы рассмотреть";
}

function renderNomHero() {
  const n = current();
  $("nomHero").hidden = !n;
  if (!n) return;
  $("nomIndex").textContent = "Номинация " + position(n) + " из " + state.nominations.length;
  $("nomTitle").textContent = n.name;
  $("nomHint").textContent = state.is_admin
    ? "В номинации работ: " + n.entry_ids.length + " · проголосовало: " + n.voter_count
    : hintFor();
  $("renameBtn").hidden = !state.is_admin;
  $("deleteBtn").hidden = !state.is_admin;
}

function renderSegments() {
  const box = $("segments");
  const n = current();
  box.hidden = !state.is_admin || !n || !state.entries.length;
  $("adminHint").hidden = box.hidden;
  if (box.hidden) return;
  const options = [
    ["all", "Все", state.entries.length],
    ["vote", "Допущены", state.entries.filter((e) => inVote.has(e.id)).length],
    ["members", "В номинации", n.entry_ids.length],
  ];
  box.innerHTML = options.map(([key, label, count]) =>
    '<button data-filter="' + key + '" class="' + (filter === key ? "on" : "") + '">' +
    esc(label) + "<b>" + count + "</b></button>").join("");
}

function isChosen(id) {
  const n = current();
  if (!n) return false;
  return state.is_admin ? n.entry_ids.includes(id) : n.my_vote.includes(id);
}

function pickLabel(id, short) {
  if (state.is_admin) return isChosen(id) ? "✓ В номинации" : "＋ Добавить";
  if (isChosen(id)) return short ? "✓ Ваш голос" : "✓ Ваш голос — отменить";
  return short ? "Голосовать" : "Голосовать за эту работу";
}

function picksDisabled() { return !state.is_admin && !state.open; }

function countFor(id) {
  const n = current();
  const row = n && n.results ? n.results.find((r) => r.id === id) : null;
  return row ? row.votes : 0;
}

function renderGrid() {
  const grid = $("grid");
  grid.className = "grid" + (state.is_admin ? " dense" : "");
  grid.innerHTML = "";
  const disabled = picksDisabled() ? " disabled" : "";
  for (const entry of shownWorks()) {
    const card = document.createElement("div");
    card.className = "card gcard";
    card.dataset.entry = entry.id;
    const count = state.is_admin && isChosen(entry.id) ? countFor(entry.id) : 0;
    card.innerHTML =
      '<div class="thumb" data-open="' + esc(entry.id) + '" role="button" aria-label="Рассмотреть работу">' +
        (entry.photos[0] ? '<img class="photo" loading="lazy" src="' + esc(entry.photos[0]) + '" alt="">' : "") +
        (entry.photos.length > 1 ? '<span class="badge more">+' + (entry.photos.length - 1) + "</span>" : "") +
        (count ? '<span class="badge votes">' + count + "</span>" : "") +
        '<span class="mark">✓</span>' +
      "</div>" +
      '<div class="meta">' + avatarHtml(entry) + '<span class="name">' + esc(displayName(entry)) + "</span></div>" +
      '<button class="pick" data-pick="' + esc(entry.id) + '"' + disabled + ">" + pickLabel(entry.id, true) + "</button>";
    grid.appendChild(card);
  }
}

function renderReel() {
  const feed = $("feed");
  feed.innerHTML = "";
  const disabled = picksDisabled() ? " disabled" : "";
  for (const entry of shownWorks()) {
    const card = document.createElement("div");
    card.className = "card rcard";
    card.dataset.entry = entry.id;
    card.innerHTML =
      '<div class="rhead">' + avatarHtml(entry, "big") + '<div class="text"><div class="name">' +
        esc(displayName(entry)) + "</div>" +
        (entry.username ? '<div class="tag">@' + esc(entry.username) + "</div>" : "") + "</div></div>" +
      (entry.text ? '<div class="cap">' + esc(entry.text) + "</div>" : "") +
      '<div class="photos">' + entry.photos.map((p) =>
        '<div class="shot"><img loading="lazy" src="' + esc(p) + '" alt="">' +
        '<button type="button" class="zoomBtn" aria-label="Увеличить" data-zoom="' + esc(p) + '">⛶</button></div>'
      ).join("") + "</div>" +
      '<button class="pickBtn" data-pick="' + esc(entry.id) + '"' + disabled + ">" + pickLabel(entry.id) + "</button>";
    feed.appendChild(card);
  }
}

// Repaints what a pick changes without rebuilding anything, so a tap deep in the reel
// does not throw the reader back to its top.
function syncPicks() {
  const disabled = picksDisabled();
  for (const button of document.querySelectorAll("[data-pick]")) {
    const id = button.dataset.pick;
    button.textContent = pickLabel(id, button.classList.contains("pick"));
    button.disabled = disabled;
    const card = button.closest(".card");
    if (!card) continue;
    card.classList.toggle("on", isChosen(id));
    if (state.is_admin) card.classList.toggle("out", !isChosen(id));
  }
}

function renderResults() {
  const box = $("results");
  const n = current();
  if (!n || !n.results || !n.results.length) { box.hidden = true; return; }
  box.hidden = false;
  const max = Math.max(1, ...n.results.map((r) => r.votes));
  const mine = new Set(n.my_vote || []);
  box.innerHTML =
    "<h3>Голоса в номинации «" + esc(n.name) + "»</h3>" +
    '<div class="sub">Проголосовало: ' + (n.voter_count || 0) + "</div>" +
    '<div class="table">' + n.results.map((r, i) => {
      const entry = works.get(r.id) || { author: "?", photos: [] };
      return '<span class="rank">' + (i + 1) + "</span>" +
        (entry.photos[0] ? '<img class="mini" loading="lazy" src="' + esc(entry.photos[0]) + '" alt="">'
                         : '<span class="mini"></span>') +
        '<span class="who' + (mine.has(r.id) ? " mine" : "") + '">' + esc(displayName(entry)) +
          (mine.has(r.id) ? " · ваш голос" : "") + "</span>" +
        '<span class="track"><span class="fill" style="width:' + Math.round(100 * r.votes / max) + '%"></span></span>' +
        '<span class="num">' + r.votes + "</span>";
    }).join("") + "</div>";
}

function renderBar() {
  const bar = $("bar");
  if (state.is_admin) {
    bar.hidden = !state.exists;
    bar.innerHTML = '<button class="go ' + (state.open ? "danger" : "") + '" data-bar="toggle">' +
      (state.open ? "Закрыть голосование" : "Открыть голосование") + "</button>";
    return;
  }
  const list = state.nominations;
  const n = current();
  if (list.length < 2 || !n) { bar.hidden = true; return; }
  bar.hidden = false;
  const at = list.indexOf(n);
  const target = nextUnvoted() || list[at + 1] || null;
  const prev = '<button class="go ghost square" data-bar="prev" aria-label="Предыдущая номинация"' +
    (at > 0 ? "" : " disabled") + ">‹</button>";
  bar.innerHTML = prev + (target
    ? '<button class="go" data-bar="next" data-target="' + esc(target.id) + '">Дальше: ' +
        position(target) + ". " + esc(target.name) + " →</button>"
    : '<div class="doneNote">✓ Все номинации пройдены</div>');
}

function renderEmpty() {
  const box = $("empty");
  let icon = "", title = "", text = "";
  if (state.is_admin && !state.entries.length) {
    icon = "🖼"; title = "Работ пока нет";
    text = "Работы берутся из основного голосования. Соберите их там: /vote собрать — и они появятся здесь сами.";
  } else if (state.is_admin && !state.nominations.length) {
    icon = "🏷"; title = "Создайте первую номинацию";
    text = "Нажмите «＋ Номинация», дайте ей название и отметьте работы, которые в ней участвуют.";
  } else if (!state.is_admin && !state.nominations.length) {
    icon = "⏳"; title = "Номинации ещё готовятся";
    text = "Загляните чуть позже — здесь появятся вкладки с номинациями.";
  } else if (!shownWorks().length) {
    icon = "∅";
    title = state.is_admin && filter !== "all" ? "Под этот фильтр ничего не подходит" : "В этой номинации пока нет работ";
    text = state.is_admin ? "Переключите фильтр на «Все», чтобы добавить работы." : "";
  }
  box.hidden = !title;
  box.innerHTML = title ? '<div class="icon">' + icon + "</div><b>" + esc(title) + "</b>" + esc(text) : "";
}

function renderNotice() {
  const notice = $("notice");
  notice.hidden = state.is_admin || state.open || !state.nominations.length;
  notice.textContent = "Голосование закрыто. Итоги — под работами в каждой номинации.";
}

function renderWorks() {
  // A full render can happen with the reel open (an admin save); keep the reader's place.
  const reelScroll = $("reel").scrollTop;
  renderGrid();
  renderReel();
  $("reel").scrollTop = reelScroll;
  syncPicks();
}

function render() {
  renderHero();
  renderTabs();
  renderNomHero();
  renderSegments();
  renderNotice();
  $("settings").hidden = !state.is_admin || !state.exists;
  $("maxChoices").value = state.max_choices || "";
  renderWorks();
  renderEmpty();
  renderResults();
  renderBar();
}

function switchTab(id) {
  if (!id || id === active) return;
  active = id;
  closeForm();
  closeReel();
  render();
  window.scrollTo({ top: 0, behavior: "smooth" });
  haptic("select");
}

// ---------------------------------------------------------------- the thank-you popup

// After the vote that counts: the first choice in a nomination, or the one that fills
// its cap. Every further tap in the same nomination only gets the small toast -- a popup
// on each of several picks would be a popup to dismiss on each of them.
function showThanks(n) {
  const list = state.nominations;
  const next = nextUnvoted();
  const cap = state.max_choices;
  const canPickMore = state.open && cap !== 1 && (!cap || n.my_vote.length < cap);
  $("thanksWhere").textContent = "Голос в номинации «" + n.name + "» учтён";
  const big = $("thanksNum");
  if (next) {
    thanksTarget = next.id;
    $("thanksLabel").textContent = "Следующая номинация";
    big.textContent = String(position(next));
    big.classList.remove("complete");
    $("thanksOf").textContent = "из " + list.length;
    $("thanksNext").textContent = "«" + next.name + "»";
    $("thanksGo").textContent = "Перейти к голосованию номинации «" + next.name + "»";
    $("thanksStay").textContent = canPickMore ? "Выбрать ещё работы здесь" : "Остаться здесь";
  } else {
    thanksTarget = null;
    $("thanksLabel").textContent = list.length > 1 ? "Вы проголосовали во всех номинациях" : "Номинация пройдена";
    big.textContent = String(list.filter(voted).length);
    big.classList.add("complete");
    $("thanksOf").textContent = "из " + list.length;
    $("thanksNext").textContent = "";
    $("thanksGo").textContent = "Посмотреть результаты";
    $("thanksStay").textContent = canPickMore ? "Выбрать ещё работы" : "Закрыть";
  }
  $("thanks").hidden = false;
  if (tg && tg.BackButton) tg.BackButton.show();
  $("thanksGo").focus({ preventScroll: true });
}

function closeThanks() {
  $("thanks").hidden = true;
  if ($("reel").hidden && $("lens").hidden && tg && tg.BackButton) tg.BackButton.hide();
}

$("thanksGo").addEventListener("click", () => {
  closeThanks();
  if (thanksTarget) { switchTab(thanksTarget); return; }
  closeReel();
  const results = $("results");
  if (!results.hidden) results.scrollIntoView({ behavior: "smooth", block: "start" });
});
$("thanksStay").addEventListener("click", closeThanks);
$("thanks").addEventListener("click", (event) => { if (event.target === $("thanks")) closeThanks(); });

// --------------------------------------------------------------------------- voting

// The tap IS the vote; tapping a chosen work takes it back. One ballot at a time: a
// second tap computed from a `my_vote` the first request has not updated yet could
// otherwise quietly drop a choice.
async function vote(id) {
  if (ballotInFlight) return;
  const n = current();
  if (!n) return;
  const before = n.my_vote.slice();
  const has = before.includes(id);
  const cap = state.max_choices;
  let next;
  if (cap === 1) {
    next = has ? [] : [id];   // a single-choice nomination behaves like a radio button
  } else {
    if (!has && cap && before.length >= cap) {
      toast("В номинации можно выбрать не более " + cap, true);
      haptic("error");
      return;
    }
    next = has ? before.filter((x) => x !== id) : before.concat([id]);
  }
  haptic("select");
  ballotInFlight = true;
  try {
    const data = await call("/api/ballot", { nomination_id: n.id, choices: next });
    Object.assign(n, data.nomination);
  } catch (e) {
    ballotInFlight = false;
    toast(String(e.message || e), true);
    // Most refusals mean the page is out of date (the vote closed, the nomination was
    // edited or deleted), so it is brought up to date rather than left showing a lie.
    await reload().catch(() => {});
    return;
  } finally {
    ballotInFlight = false;
  }
  syncPicks();
  renderHero();
  renderTabs();
  renderResults();
  renderBar();
  const after = n.my_vote.length;
  const counts = after > 0 && (before.length === 0 || (cap && cap > 1 && after >= cap && before.length < cap));
  if (counts) {
    haptic("success");
    showThanks(n);
  } else {
    toast(after ? (before.length ? "Голос обновлён" : "Голос учтён") : "Голос снят");
  }
}

// ------------------------------------------------------------------- administration

function toggleMember(id) {
  const n = current();
  if (!n) return;
  const members = new Set(n.entry_ids);
  if (members.has(id)) members.delete(id); else members.add(id);
  // Pool order, the order the server stores and the voter will see.
  n.entry_ids = state.entries.map((e) => e.id).filter((x) => members.has(x));
  haptic("select");
  // Under "В номинации" a removed work leaves the view, so that one redraws.
  if (filter === "members") { renderWorks(); renderEmpty(); } else syncPicks();
  renderTabs();
  renderNomHero();
  renderSegments();
  saveMembers(n.id);
}

async function saveMembers(nominationId) {
  const slot = saving[nominationId] || (saving[nominationId] = { inFlight: false, dirty: false });
  if (slot.inFlight) { slot.dirty = true; return; }
  slot.inFlight = true;
  try {
    let data;
    do {
      slot.dirty = false;
      const n = nom(nominationId);
      if (!n) return;
      data = await call("/api/nominations/update", { nomination_id: nominationId, entry_ids: n.entry_ids });
    } while (slot.dirty);
    // Only the counts are taken from the answer: the membership on screen is newer than
    // or equal to what was sent, and replacing it could undo a tap made meanwhile.
    const fresh = data.state.nominations.find((x) => x.id === nominationId);
    const n = nom(nominationId);
    if (fresh && n) {
      n.results = fresh.results;
      n.voter_count = fresh.voter_count;
      if (nominationId === active) { renderResults(); renderNomHero(); }
    }
  } catch (e) {
    toast("Не сохранилось: " + String(e.message || e), true);
    await reload();
  } finally {
    slot.inFlight = false;
  }
}

function openForm(mode) {
  formMode = mode;
  $("formPanel").hidden = false;
  $("formTitle").textContent = mode === "rename" ? "Новое название номинации" : "Новая номинация";
  $("nomSubmit").textContent = mode === "rename" ? "Сохранить" : "Создать";
  $("nomName").value = mode === "rename" && current() ? current().name : "";
  $("nomName").focus();
}

function closeForm() {
  formMode = null;
  $("formPanel").hidden = true;
}

async function adminChange(path, body, success) {
  try {
    const data = await call(path, body);
    applyState(data.state);
    if (data.nomination_id && path.endsWith("/create")) { active = data.nomination_id; filter = "all"; }
    render();
    haptic("success");
    if (success) toast(success);
    return true;
  } catch (e) {
    toast(String(e.message || e), true);
    return false;
  }
}

$("nomForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = $("nomName").value.trim();
  if (!name) return;
  const button = $("nomSubmit");
  button.disabled = true;
  const ok = formMode === "rename"
    ? await adminChange("/api/nominations/update", { nomination_id: active, name }, "Переименовано")
    : await adminChange("/api/nominations/create", { name }, "Номинация создана — отметьте её работы");
  button.disabled = false;
  if (ok) closeForm();
});
$("nomCancel").addEventListener("click", closeForm);
$("renameBtn").addEventListener("click", () => openForm("rename"));
$("deleteBtn").addEventListener("click", async () => {
  const n = current();
  if (!n) return;
  if (!confirm("Удалить номинацию «" + n.name + "»? Голоса в ней пропадут.")) return;
  active = null;
  await adminChange("/api/nominations/delete", { nomination_id: n.id }, "Номинация удалена");
});
$("maxChoices").addEventListener("change", async () => {
  const raw = $("maxChoices").value;
  const value = raw ? parseInt(raw, 10) : null;
  if (value !== null && !(value >= 1)) { $("maxChoices").value = state.max_choices || ""; return; }
  await adminChange("/api/settings", { max_choices: value }, "Сохранено");
});

$("bar").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-bar]");
  if (!button || button.disabled) return;
  const action = button.dataset.bar;
  if (action === "toggle") {
    const opening = !state.open;
    if (!opening && !confirm("Закрыть голосование во всех номинациях? Голосовать будет нельзя, пока не откроете снова.")) return;
    await adminChange("/api/settings", { open: opening }, opening ? "Голосование открыто" : "Голосование закрыто");
  } else if (action === "next") {
    switchTab(button.dataset.target);
  } else if (action === "prev") {
    const list = state.nominations;
    const at = list.findIndex((n) => n.id === active);
    if (at > 0) switchTab(list[at - 1].id);
  }
});

// ------------------------------------------------------------------- reel and lens

function openReel(id) {
  const reel = $("reel");
  reel.hidden = false;
  document.body.classList.add("reelOpen");
  const target = [...$("feed").children].find((card) => card.dataset.entry === id);
  reel.scrollTop = 0;
  if (target) target.scrollIntoView({ block: "start" });
  if (tg && tg.BackButton) tg.BackButton.show();
}

function closeReel() {
  if (!$("lens").hidden) closeLens();
  $("reel").hidden = true;
  document.body.classList.remove("reelOpen");
  if ($("thanks").hidden && tg && tg.BackButton) tg.BackButton.hide();
}

function closeReelAt(entryId) {
  closeReel();
  const card = [...$("grid").children].find((c) => c.dataset.entry === entryId);
  if (card) card.scrollIntoView({ block: "center" });
}

// The lens: one photo with zoom and pan of our own, because Telegram's Android WebView
// gives the page no pinch-to-zoom (see vote_web.py, where this was first needed).
const LENS_MAX_ZOOM = 8;
const LENS_TAP_ZOOM = 3;
const LENS_TAP_MS = 260;
let lens = { scale: 1, fit: 1, x: 0, y: 0 };
let lensPointers = new Map();
let lensPinch = null;
let lensTapTimer = null;

function lensApply() {
  const stage = $("lensStage");
  const img = $("lensImg");
  const width = img.naturalWidth * lens.scale;
  const height = img.naturalHeight * lens.scale;
  const vw = stage.clientWidth, vh = stage.clientHeight;
  lens.x = width <= vw ? (vw - width) / 2 : Math.min(0, Math.max(vw - width, lens.x));
  lens.y = height <= vh ? (vh - height) / 2 : Math.min(0, Math.max(vh - height, lens.y));
  img.style.transform = "translate(" + lens.x + "px," + lens.y + "px) scale(" + lens.scale + ")";
}

function lensFit() {
  const stage = $("lensStage");
  const img = $("lensImg");
  if (!img.naturalWidth || !img.naturalHeight) return;
  lens.fit = Math.min(stage.clientWidth / img.naturalWidth, stage.clientHeight / img.naturalHeight);
  lens.scale = lens.fit;
  lensApply();
}

function lensZoomTo(scale, px, py) {
  const next = Math.max(lens.fit, Math.min(lens.fit * LENS_MAX_ZOOM, scale));
  lens.x = px - (px - lens.x) * (next / lens.scale);
  lens.y = py - (py - lens.y) * (next / lens.scale);
  lens.scale = next;
  lensApply();
}

function openLens(src) {
  const img = $("lensImg");
  $("lens").hidden = false;
  document.body.classList.add("reelOpen");
  img.style.transform = "";
  img.src = src;
  if (img.complete && img.naturalWidth) lensFit();
  else img.addEventListener("load", lensFit, { once: true });
  if (tg && tg.BackButton) tg.BackButton.show();
}

function closeLens() {
  $("lens").hidden = true;
  $("lensImg").removeAttribute("src");
  lensPointers.clear();
  lensPinch = null;
  if (!$("reel").hidden) return;
  document.body.classList.remove("reelOpen");
  if ($("thanks").hidden && tg && tg.BackButton) tg.BackButton.hide();
}

const lensStageEl = $("lensStage");
lensStageEl.addEventListener("pointerdown", (event) => {
  lensStageEl.setPointerCapture(event.pointerId);
  lensPointers.set(event.pointerId, { x: event.clientX, y: event.clientY,
                                      startX: event.clientX, startY: event.clientY, at: Date.now() });
  if (lensPointers.size === 2) {
    const [a, b] = [...lensPointers.values()];
    lensPinch = { distance: Math.hypot(a.x - b.x, a.y - b.y) || 1, scale: lens.scale };
  }
});
lensStageEl.addEventListener("pointermove", (event) => {
  const pointer = lensPointers.get(event.pointerId);
  if (!pointer) return;
  const previous = { x: pointer.x, y: pointer.y };
  pointer.x = event.clientX;
  pointer.y = event.clientY;
  if (lensPointers.size >= 2 && lensPinch) {
    const [a, b] = [...lensPointers.values()];
    const distance = Math.hypot(a.x - b.x, a.y - b.y) || 1;
    lensZoomTo(lensPinch.scale * (distance / lensPinch.distance), (a.x + b.x) / 2, (a.y + b.y) / 2);
    return;
  }
  if (lens.scale > lens.fit * 1.001) {
    lens.x += pointer.x - previous.x;
    lens.y += pointer.y - previous.y;
    lensApply();
  }
});
function lensPointerDone(event) {
  const pointer = lensPointers.get(event.pointerId);
  lensPointers.delete(event.pointerId);
  if (lensPointers.size < 2) lensPinch = null;
  if (!pointer || event.type !== "pointerup") return;
  const moved = Math.hypot(pointer.x - pointer.startX, pointer.y - pointer.startY);
  if (moved > 10 || Date.now() - pointer.at > 600) return;
  // The black around the picture closes it at any zoom -- there is nothing there to tap.
  const box = $("lensImg").getBoundingClientRect();
  if (pointer.x < box.left || pointer.x > box.right || pointer.y < box.top || pointer.y > box.bottom) {
    clearTimeout(lensTapTimer);
    lensTapTimer = null;
    closeLens();
    return;
  }
  if (lensTapTimer) {
    clearTimeout(lensTapTimer);
    lensTapTimer = null;
    if (lens.scale > lens.fit * 1.05) lensFit();
    else lensZoomTo(lens.fit * LENS_TAP_ZOOM, pointer.x, pointer.y);
    return;
  }
  lensTapTimer = setTimeout(() => {
    lensTapTimer = null;
    if (lens.scale <= lens.fit * 1.05) closeLens();
  }, LENS_TAP_MS);
}
lensStageEl.addEventListener("pointerup", lensPointerDone);
lensStageEl.addEventListener("pointercancel", lensPointerDone);
lensStageEl.addEventListener("wheel", (event) => {
  event.preventDefault();
  lensZoomTo(lens.scale * (event.deltaY < 0 ? 1.15 : 1 / 1.15), event.clientX, event.clientY);
}, { passive: false });
window.addEventListener("resize", () => { if (!$("lens").hidden) lensFit(); });
$("lensClose").addEventListener("click", closeLens);

// Telegram's back arrow steps back one layer: the popup, then the lens, then the reel.
// Esc does the same on a computer.
function stepBack() {
  if (!$("thanks").hidden) closeThanks();
  else if (!$("lens").hidden) closeLens();
  else closeReel();
}
if (tg && tg.BackButton) tg.BackButton.onClick(stepBack);
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!$("thanks").hidden || !$("lens").hidden || !$("reel").hidden) { event.preventDefault(); stepBack(); }
});
$("reelClose").addEventListener("click", closeReel);
// A click on the reel's empty space -- around the works, not on one -- closes it too.
$("reel").addEventListener("click", (event) => {
  const target = event.target;
  if (target === $("reel") || target === $("feed") || target.classList.contains("rcard")) closeReel();
});

// A tap on a picture in the reel closes it back to the grid; a scroll that comes to rest
// on one must not (measured from pointerdown, as v1 does).
let reelTap = null;
$("feed").addEventListener("pointerdown", (event) => {
  reelTap = event.target.tagName === "IMG" && !event.target.closest(".ava")
    ? { x: event.clientX, y: event.clientY, at: Date.now(), target: event.target } : null;
});
$("feed").addEventListener("pointercancel", () => { reelTap = { cancelled: true }; });
$("feed").addEventListener("click", (event) => {
  const zoom = event.target.closest("[data-zoom]");
  if (zoom) { event.preventDefault(); reelTap = null; openLens(zoom.dataset.zoom); return; }
  if (event.target.tagName !== "IMG" || event.target.closest(".ava")) return;
  const start = reelTap;
  reelTap = null;
  if (start) {
    if (start.cancelled || start.target !== event.target) return;
    if (Date.now() - start.at > 600) return;
    if (Math.abs(event.clientX - start.x) > 10 || Math.abs(event.clientY - start.y) > 10) return;
  }
  const card = event.target.closest("[data-entry]");
  closeReelAt(card && card.dataset.entry);
});

document.addEventListener("click", (event) => {
  const tab = event.target.closest("[data-tab]");
  if (tab) { switchTab(tab.dataset.tab); return; }
  if (event.target.closest("[data-add]")) { openForm("create"); return; }
  const chip = event.target.closest("[data-filter]");
  if (chip) { filter = chip.dataset.filter; renderSegments(); renderWorks(); renderEmpty(); return; }
  const open = event.target.closest("[data-open]");
  if (open) { event.preventDefault(); openReel(open.dataset.open); return; }
  const pick = event.target.closest("[data-pick]");
  if (!pick || pick.disabled) return;
  event.preventDefault();
  if (state.is_admin) toggleMember(pick.dataset.pick);
  else vote(pick.dataset.pick);
});

async function reload() {
  const data = await call("/api/state" + (MODE ? "?mode=" + encodeURIComponent(MODE) : ""));
  applyState(data);
  render();
}

reload().catch((e) => {
  $("heroBody").innerHTML = "";
  const box = $("empty");
  box.hidden = false;
  box.innerHTML = '<div class="icon">⚠️</div><b>Не получилось открыть</b>' + esc(String(e.message || e));
});
</script>
</body>
</html>
"""
