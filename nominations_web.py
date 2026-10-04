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


def _state_payload(contest, user_id, admin_mode: bool, can_moderate: bool, base: str) -> dict:
    """Everything the page draws, and only that.

    A voter gets the nominations that have works in them and the works those name -- not
    the pool, which is the administrator's raw material and would be sent to every voter
    for nothing. The administrator gets the whole pool, since adding works to a nomination
    is choosing from it."""
    if contest is None:
        return {
            "exists": False, "open": False, "max_choices": None,
            "is_admin": admin_mode, "can_moderate": can_moderate,
            "nominations": [], "entries": [],
        }
    if admin_mode:
        shown = list(contest.nominations)
        entries = contest.entries
    else:
        shown = [n for n in contest.nominations if contest.members(n)]
        used = {entry_id for n in shown for entry_id in n.entry_ids}
        entries = [e for e in contest.entries if e.entry_id in used]
    return {
        "exists": True,
        "open": contest.open,
        "max_choices": contest.max_choices,
        "is_admin": admin_mode,
        "can_moderate": can_moderate,
        "nominations": [_nomination_payload(contest, n, user_id, admin_mode) for n in shown],
        "entries": [_entry_payload(e, base) for e in entries],
    }


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

    def build() -> dict:
        contest = nominations.load_contest(entry)
        return _state_payload(contest, user["id"], admin_mode, can_moderate, base)

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


async def handle_page(request: web.Request) -> web.Response:
    return web.Response(
        text=PAGE_HTML.replace("__PREFIX__", request.app[_PREFIX_KEY]),
        content_type="text/html",
    )


def attach(app: web.Application, cfg, entry: str, is_admin, log=print,
           route_prefix: str = ROUTE_PREFIX) -> web.Application:
    """Adds v3 to the application vote_web.create_app builds. Its own AppKeys throughout
    (nominations_*), so nothing it stores can collide with v1's or the arena's."""
    prefix = route_prefix.rstrip("/")
    app[_CFG_KEY] = cfg
    app[_ENTRY_KEY] = entry
    app[_IS_ADMIN_KEY] = is_admin
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
    --fg: #f5f5f5;
    --muted: #8a9aa9;
    --card: #232e3c;
    --accent: #3390ec;
    --accent-fg: #fff;
    --danger: #e5534b;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  [hidden] { display: none !important; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 15px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding-bottom: 96px;
  }
  header { padding: 14px 12px 4px; }
  h1 { font-size: 17px; margin: 0 0 2px; }
  .sub { color: var(--muted); font-size: 13px; }

  /* The tabs stay on screen while the grid scrolls under them: switching nomination is
     the thing this page exists for, and it should never need a scroll back up. */
  .tabs { position: sticky; top: 0; z-index: 5; background: var(--bg);
          display: flex; gap: 6px; overflow-x: auto; padding: 8px 12px;
          scrollbar-width: none; border-bottom: 1px solid rgba(128,128,128,.2); }
  .tabs::-webkit-scrollbar { display: none; }
  .tab { flex: none; border: 1px solid rgba(128,128,128,.4); border-radius: 16px;
         background: transparent; color: var(--fg); padding: 6px 12px; font: inherit;
         font-size: 14px; cursor: pointer; white-space: nowrap; }
  .tab.active { background: var(--accent); border-color: var(--accent); color: var(--accent-fg); }
  .tab .tick { margin-left: 4px; font-size: 12px; }
  .tab.add { border-style: dashed; color: var(--accent); }

  .nomHead { margin: 8px 12px 0; display: flex; align-items: center; gap: 8px; }
  .nomHead .info { flex: 1; min-width: 0; color: var(--muted); font-size: 13px; }
  .nomHead .info b { color: var(--fg); font-size: 15px; display: block;
                     overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .iconBtn { flex: none; border: 1px solid rgba(128,128,128,.4); border-radius: 8px;
             background: transparent; color: var(--fg); padding: 6px 10px; font: inherit;
             font-size: 13px; cursor: pointer; }
  .iconBtn.danger { color: var(--danger); border-color: var(--danger); }

  .panel { margin: 8px 12px 0; padding: 10px 12px; border-radius: 10px;
           background: var(--card); font-size: 13px; }
  .panel .row { display: flex; align-items: center; justify-content: space-between;
                gap: 8px; padding: 4px 0; }
  .panel input[type="number"] { width: 56px; text-align: center; border-radius: 6px;
              border: 1px solid rgba(128,128,128,.4); background: var(--bg);
              color: var(--fg); padding: 4px; font-size: 13px; }
  .nomForm { display: flex; gap: 6px; }
  .nomForm input { flex: 1; min-width: 0; border-radius: 8px; padding: 9px 10px;
                   border: 1px solid rgba(128,128,128,.4); background: var(--bg);
                   color: var(--fg); font: inherit; }
  .nomForm button { border: 0; border-radius: 8px; padding: 9px 12px; font: inherit;
                    font-weight: 600; background: var(--accent); color: var(--accent-fg);
                    cursor: pointer; }
  .nomForm button.cancel { background: transparent; color: var(--muted);
                           border: 1px solid rgba(128,128,128,.4); font-weight: 400; }
  .formTitle { margin-bottom: 6px; color: var(--muted); }

  .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; padding: 12px; }
  .gcard { background: var(--card); border-radius: 10px; overflow: hidden; position: relative; }
  .thumb { position: relative; width: 100%; aspect-ratio: 1; display: block; overflow: hidden;
           background: rgba(128,128,128,.2); cursor: pointer; }
  .thumb > img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .count { position: absolute; right: 4px; top: 4px; background: rgba(0,0,0,.6);
           color: #fff; font-size: 11px; padding: 1px 5px; border-radius: 8px; }
  .votes { position: absolute; left: 4px; top: 4px; background: var(--accent);
           color: var(--accent-fg); font-size: 11px; padding: 1px 6px; border-radius: 8px; }
  .gcard .who { padding: 5px 6px 2px; font-size: 11px; overflow: hidden;
                text-overflow: ellipsis; white-space: nowrap; }
  .pick { display: block; width: 100%; border: 0; padding: 7px 4px; font-size: 12px;
          background: transparent; color: var(--muted); cursor: pointer;
          border-top: 1px solid rgba(128,128,128,.25); }
  .gcard.on { outline: 2px solid var(--accent); }
  .gcard.on .pick { background: var(--accent); color: var(--accent-fg); font-weight: 600; }
  .gcard.out { opacity: .5; }
  .pick[disabled], .pickBtn[disabled] { opacity: .5; cursor: default; }

  .reel { position: fixed; inset: 0; z-index: 10; background: var(--bg);
          overflow-y: auto; -webkit-overflow-scrolling: touch; }
  body.reelOpen { overflow: hidden; }
  .reelClose { position: fixed; top: 10px; right: 10px; z-index: 12;
               border: 0; border-radius: 50%; width: 36px; height: 36px;
               background: rgba(0,0,0,.55); color: #fff; font-size: 17px;
               line-height: 1; cursor: pointer; }
  .feed { padding: 12px 12px calc(var(--barH, 96px) + 16px); }
  .rcard { padding: 14px 0; border-bottom: 1px solid rgba(128,128,128,.2); }
  .rcard:last-child { border-bottom: 0; }
  .rcard .who { font-size: 13px; font-weight: 600; margin-bottom: 6px; }
  .rcard .who .tag { color: var(--muted); font-weight: 400; margin-left: 4px; }
  .rcard .cap { white-space: pre-wrap; margin: 0 0 10px; font-size: 14px; }
  .rcard .photos { display: flex; flex-direction: column; gap: 6px; margin-bottom: 10px; }
  .rcard .photos img { width: 100%; border-radius: 10px; display: block; cursor: pointer; }
  .rcard.out { opacity: .6; }
  .shot { position: relative; }
  .zoomBtn { position: absolute; right: 8px; bottom: 8px; z-index: 2;
             border: 0; border-radius: 50%; width: 36px; height: 36px;
             background: rgba(0,0,0,.55); color: #fff; font-size: 16px; line-height: 1;
             display: flex; align-items: center; justify-content: center; cursor: pointer; }
  .votesBadge { margin-left: 6px; background: var(--accent); color: var(--accent-fg);
                font-size: 11px; padding: 1px 6px; border-radius: 8px; }
  .pickBtn { display: block; width: 100%; border: 1px solid rgba(128,128,128,.35);
             border-radius: 8px; padding: 10px; font-size: 14px; font-weight: 600;
             background: transparent; color: var(--fg); cursor: pointer; }
  .rcard.on .pickBtn { background: var(--accent); color: var(--accent-fg); border-color: var(--accent); }

  .lens { position: fixed; inset: 0; z-index: 30; background: #000;
          touch-action: none; overscroll-behavior: contain; }
  .lensStage { position: absolute; inset: 0; overflow: hidden; touch-action: none; }
  .lensStage img { position: absolute; left: 0; top: 0; transform-origin: 0 0;
                   max-width: none; display: block; user-select: none;
                   -webkit-user-drag: none; -webkit-user-select: none; }

  .bar { position: fixed; left: 0; right: 0; bottom: 0; padding: 10px 12px;
         padding-bottom: calc(10px + env(safe-area-inset-bottom));
         background: var(--bg); border-top: 1px solid rgba(128,128,128,.25); z-index: 20; }
  .go { width: 100%; border: 0; border-radius: 10px; padding: 14px;
        font-size: 16px; font-weight: 600; background: var(--accent);
        color: var(--accent-fg); cursor: pointer; }
  .go[disabled] { opacity: .5; }
  .go.secondary { background: transparent; color: var(--accent); border: 1px solid var(--accent); }
  .go.danger { background: transparent; color: var(--danger); border: 1px solid var(--danger); }
  .msg { padding: 24px 16px; color: var(--muted); text-align: center; }
  .notice { padding: 10px 12px; margin: 8px 12px 0; border-radius: 10px;
            background: var(--card); color: var(--muted); font-size: 13px; text-align: center; }
  .confirmBanner { margin: 8px 12px 0; padding: 8px 12px; border-radius: 8px;
                   background: var(--accent); color: var(--accent-fg); font-size: 13px;
                   text-align: center; opacity: 1; transition: opacity .6s ease; }
  .confirmBanner.fade { opacity: 0; }

  .results { margin: 4px 12px 12px; padding: 10px 12px; border-radius: 10px;
             background: var(--card); font-size: 13px; }
  .results h2 { margin: 0 0 4px; font-size: 14px; }
  .results .voterCount { color: var(--muted); margin-bottom: 8px; }
  /* One grid for the whole table so every bar starts at the same x (v1's lesson). */
  .results .table { display: grid; align-items: center; column-gap: 8px; row-gap: 6px;
                    grid-template-columns: auto minmax(0, 38%) auto minmax(0, 1fr) auto; }
  .results .rank { color: var(--muted); font-size: 12px; text-align: right;
                   font-variant-numeric: tabular-nums; }
  .results .name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .results .mini { width: 22px; height: 22px; border-radius: 4px; display: block;
                   object-fit: cover; background: rgba(128,128,128,.2); }
  .results .track { height: 8px; border-radius: 4px; background: rgba(128,128,128,.2); overflow: hidden; }
  .results .fill { display: block; height: 100%; background: var(--accent); border-radius: 4px; }
  .results .num { text-align: right; font-size: 12px; color: var(--muted);
                  font-variant-numeric: tabular-nums; }
</style>
</head>
<body>
<header>
  <h1 id="title">Номинации</h1>
  <div class="sub" id="sub">Загружаю…</div>
</header>
<!-- Above the tabs, because it is one setting for every nomination, not a property of
     the one whose tab is open. -->
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
    <button type="button" class="cancel" id="nomCancel">✕</button>
  </form>
</div>
<div class="nomHead" id="nomHead" hidden>
  <div class="info" id="nomInfo"></div>
  <button class="iconBtn" id="renameBtn" hidden>✏️</button>
  <button class="iconBtn danger" id="deleteBtn" hidden>🗑</button>
</div>
<div class="confirmBanner" id="confirmBanner" hidden>Голос учтён</div>
<div class="notice" id="notice" hidden></div>
<div class="grid" id="grid"></div>
<div class="msg" id="msg" hidden></div>
<div class="results" id="results" hidden></div>
<div class="bar" id="bar" hidden>
  <button class="go" id="go"></button>
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
  ask("setBottomBarColor", "#232e3c", "7.10");
}
const initData = (tg && tg.initData) || "";
const MODE = new URLSearchParams(location.search).get("mode");

let state = null;          // the server's state, see nominations_web._state_payload
let active = null;         // id of the nomination whose tab is open
let works = new Map();     // entry id -> entry payload
let formMode = null;       // "create" | "rename" while the name form is open
let ballotInFlight = false;
let confirmTimers = [];
// Administrator only: nomination id -> {inFlight, dirty}. Membership taps apply at once
// on screen and are sent in the background; a tap made while a save is in flight marks
// it dirty, and the save loop sends the latest set again, so the last tap always wins.
const saving = {};

const $ = (id) => document.getElementById(id);

if (window.ResizeObserver) {
  new ResizeObserver((entries) => {
    const height = entries[0].contentRect.height;
    document.documentElement.style.setProperty("--barH", height + "px");
    document.body.style.paddingBottom = (height + 16) + "px";
  }).observe($("bar"));
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function who(entry) { return entry.username ? "@" + entry.username : entry.author; }

function haptic(kind) {
  if (!tg || !tg.HapticFeedback) return;
  if (kind === "select") tg.HapticFeedback.selectionChanged();
  else tg.HapticFeedback.notificationOccurred(kind);
}

async function call(path, body) {
  const options = { headers: { "X-Telegram-Init-Data": initData } };
  if (body !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(Object.assign({ init_data: initData }, body));
  }
  const response = await fetch(PREFIX + path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "не получилось");
  return data;
}

function nom(id) { return state.nominations.find((n) => n.id === id) || null; }
function current() { return nom(active); }

// The works the grid and the reel show: the whole pool for the administrator (choosing
// from it is the job), the open nomination's works for everybody else.
function shownWorks() {
  if (state.is_admin) return state.entries;
  const n = current();
  return n ? n.entry_ids.map((id) => works.get(id)).filter(Boolean) : [];
}

function voted(n) { return n.my_vote && n.my_vote.length > 0; }

function applyState(data) {
  state = data;
  works = new Map(state.entries.map((e) => [e.id, e]));
  if (!current()) {
    // A voter lands on the first nomination they have not voted in yet: that is where
    // the next thing for them to do is.
    const fresh = state.is_admin ? null : state.nominations.find((n) => !voted(n));
    active = (fresh || state.nominations[0] || {}).id || null;
  }
}

// ------------------------------------------------------------------------- rendering

function renderTabs() {
  const tabs = $("tabs");
  const items = state.nominations.map((n) =>
    '<button class="tab' + (n.id === active ? " active" : "") + '" data-tab="' + esc(n.id) + '">' +
      esc(n.name) +
      (state.is_admin
        ? ' <span class="tick">' + n.entry_ids.length + "</span>"
        : (voted(n) ? ' <span class="tick">✓</span>' : "")) +
    "</button>"
  );
  // First, not last: at the end of a long strip it scrolls out of sight, and adding a
  // nomination is the one thing an administrator has to be able to find.
  if (state.is_admin) items.unshift('<button class="tab add" data-add="1">＋ Номинация</button>');
  // The strip is redrawn on every membership tap, so it keeps its own scroll and only
  // slides sideways to the open tab -- scrollIntoView could move the PAGE as well, and
  // throw an administrator out of their place halfway down the pool.
  const keep = tabs.scrollLeft;
  tabs.innerHTML = items.join("");
  tabs.hidden = items.length === 0;
  tabs.scrollLeft = keep;
  const on = tabs.querySelector(".tab.active");
  if (on) {
    const left = on.offsetLeft, right = left + on.offsetWidth;
    if (left < tabs.scrollLeft) tabs.scrollLeft = left - 12;
    else if (right > tabs.scrollLeft + tabs.clientWidth) tabs.scrollLeft = right - tabs.clientWidth + 12;
  }
}

function renderSub() {
  const sub = $("sub");
  if (!state.exists && !state.is_admin) { sub.textContent = ""; return; }
  if (state.is_admin) {
    sub.textContent = "Настройка номинаций · работ собрано " + state.entries.length +
      " · " + (state.open ? "голосование открыто" : "голосование закрыто");
    return;
  }
  const total = state.nominations.length;
  const done = state.nominations.filter(voted).length;
  sub.textContent = !state.open ? "Голосование закрыто"
    : (total ? "Проголосовано в " + done + " из " + total + " номинаций" : "");
  if (state.can_moderate) sub.textContent += " · настройка: /vote3 выбрать";
}

function renderHead() {
  const n = current();
  const head = $("nomHead");
  if (!n) { head.hidden = true; return; }
  head.hidden = false;
  const cap = state.max_choices;
  $("nomInfo").innerHTML = "<b>" + esc(n.name) + "</b>" + (state.is_admin
    ? "работ в номинации " + n.entry_ids.length + " из " + state.entries.length +
      " · проголосовало " + n.voter_count
    : (cap === 1 ? "Выбери одну работу" : cap ? "Выбери до " + cap + " работ" : "Выбери понравившиеся работы"));
  $("renameBtn").hidden = !state.is_admin;
  $("deleteBtn").hidden = !state.is_admin;
}

function isChosen(id) {
  const n = current();
  if (!n) return false;
  return state.is_admin ? n.entry_ids.includes(id) : n.my_vote.includes(id);
}

function pickLabel(id, short) {
  if (state.is_admin) {
    if (isChosen(id)) return short ? "✓ в номинации" : "В номинации ✓ (убрать)";
    return short ? "добавить" : "Добавить в номинацию";
  }
  if (isChosen(id)) return short ? "✓ учтён" : "Голос учтён ✓";
  return short ? "выбрать" : "Выбрать";
}

function picksDisabled() { return !state.is_admin && !state.open; }

function countFor(id) {
  const n = current();
  if (!n || !n.results) return 0;
  const row = n.results.find((r) => r.id === id);
  return row ? row.votes : 0;
}

function renderGrid() {
  const grid = $("grid");
  grid.innerHTML = "";
  const disabled = picksDisabled() ? " disabled" : "";
  const showCounts = state.is_admin;
  for (const entry of shownWorks()) {
    const card = document.createElement("div");
    card.className = "card gcard";
    card.dataset.entry = entry.id;
    const count = showCounts && isChosen(entry.id) ? countFor(entry.id) : 0;
    const more = entry.photos.length > 1 ? '<span class="count">+' + (entry.photos.length - 1) + "</span>" : "";
    card.innerHTML =
      '<div class="thumb" data-open="' + esc(entry.id) + '" role="button">' +
        (entry.photos[0] ? '<img loading="lazy" src="' + esc(entry.photos[0]) + '" alt="">' : "") +
        more + (count ? '<span class="votes">' + count + "</span>" : "") +
      "</div>" +
      '<div class="who">' + esc(who(entry)) + "</div>" +
      '<button class="pick" data-pick="' + esc(entry.id) + '"' + disabled + ">" +
        pickLabel(entry.id, true) + "</button>";
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
    const count = state.is_admin && isChosen(entry.id) ? countFor(entry.id) : 0;
    card.innerHTML =
      '<div class="who">' + esc(entry.author) +
        (entry.username ? '<span class="tag">@' + esc(entry.username) + "</span>" : "") +
        (count ? '<span class="votesBadge">' + count + "</span>" : "") + "</div>" +
      (entry.text ? '<div class="cap">' + esc(entry.text) + "</div>" : "") +
      '<div class="photos">' + entry.photos.map((p) =>
        '<div class="shot"><img loading="lazy" src="' + esc(p) + '" alt="">' +
        '<button type="button" class="zoomBtn" aria-label="Увеличить" data-zoom="' + esc(p) + '">⛶</button></div>'
      ).join("") + "</div>" +
      '<button class="pickBtn" data-pick="' + esc(entry.id) + '"' + disabled + ">" +
        pickLabel(entry.id) + "</button>";
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
  box.innerHTML =
    "<h2>Голоса · " + esc(n.name) + "</h2>" +
    '<div class="voterCount">Проголосовало: ' + (n.voter_count || 0) + "</div>" +
    '<div class="table">' + n.results.map((r, i) => {
      const entry = works.get(r.id) || { author: "?", photos: [] };
      return '<span class="rank">' + (i + 1) + "</span>" +
        '<span class="name">' + esc(who(entry)) + "</span>" +
        (entry.photos[0] ? '<img class="mini" loading="lazy" src="' + esc(entry.photos[0]) + '" alt="">'
                         : '<span class="mini"></span>') +
        '<span class="track"><span class="fill" style="width:' + Math.round(100 * r.votes / max) + '%"></span></span>' +
        '<span class="num">' + r.votes + "</span>";
    }).join("") + "</div>";
}

// The voter's way through the tabs: the next nomination they have not voted in, or
// simply the next one once they have voted everywhere.
function nextNomination() {
  const list = state.nominations;
  const at = list.findIndex((n) => n.id === active);
  const order = list.slice(at + 1).concat(list.slice(0, Math.max(at, 0)));
  return order.find((n) => !voted(n)) || order[0] || null;
}

function updateBar() {
  const bar = $("bar");
  const go = $("go");
  go.className = "go";
  go.disabled = false;
  if (state.is_admin) {
    bar.hidden = !state.exists;
    go.textContent = state.open ? "Закрыть голосование" : "Открыть голосование";
    if (state.open) go.classList.add("danger");
    return;
  }
  const next = state.nominations.length > 1 ? nextNomination() : null;
  bar.hidden = !next;
  if (next) {
    go.classList.add("secondary");
    go.textContent = "Дальше: " + next.name + " →";
  }
}

function renderNotice() {
  const notice = $("notice");
  notice.hidden = state.is_admin || state.open || !state.nominations.length;
  notice.textContent = "Голосование закрыто — смотри итоги в каждой номинации.";
}

function render() {
  renderSub();
  renderTabs();
  renderHead();
  renderNotice();
  $("settings").hidden = !state.is_admin || !state.exists;
  $("maxChoices").value = state.max_choices || "";
  const msg = $("msg");
  const list = shownWorks();
  let empty = "";
  if (state.is_admin && !state.entries.length) {
    empty = "Работ пока нет. Собери их: /vote3 собрать — или возьми из основного голосования: /vote3 импорт.";
  } else if (state.is_admin && !state.nominations.length) {
    empty = "Создай первую номинацию кнопкой «＋ Номинация» и отметь, какие работы в ней участвуют.";
  } else if (!state.is_admin && !state.nominations.length) {
    empty = "Номинации ещё не готовы. Загляни позже.";
  } else if (!list.length) {
    empty = "В этой номинации пока нет работ.";
  }
  msg.hidden = !empty;
  msg.textContent = empty;
  // A full render can happen with the reel open (an admin save); keep the reader's place.
  const reelScroll = $("reel").scrollTop;
  renderGrid();
  renderReel();
  $("reel").scrollTop = reelScroll;
  syncPicks();
  renderResults();
  updateBar();
}

function showConfirmBanner(text) {
  const banner = $("confirmBanner");
  confirmTimers.forEach(clearTimeout);
  confirmTimers = [];
  banner.textContent = text;
  banner.hidden = false;
  banner.classList.remove("fade");
  confirmTimers.push(setTimeout(() => banner.classList.add("fade"), 1600));
  confirmTimers.push(setTimeout(() => { banner.hidden = true; }, 2300));
}

function switchTab(id) {
  if (id === active) return;
  active = id;
  // The banner names the nomination it confirmed, which is no longer the one on screen.
  confirmTimers.forEach(clearTimeout);
  confirmTimers = [];
  $("confirmBanner").hidden = true;
  closeForm();
  closeReel();
  render();
  window.scrollTo({ top: 0 });
  haptic("select");
}

// --------------------------------------------------------------------------- voting

// The tap IS the vote, as in v1's default mode; tapping a chosen work takes it back.
// One ballot at a time: a second tap computed from a `my_vote` the first request has not
// updated yet could otherwise quietly drop a choice.
async function vote(id) {
  if (ballotInFlight) return;
  const n = current();
  if (!n) return;
  const has = n.my_vote.includes(id);
  const cap = state.max_choices;
  let next;
  if (cap === 1) {
    next = has ? [] : [id];   // a single-choice nomination behaves like a radio button
  } else {
    if (!has && cap && n.my_vote.length >= cap) {
      alert("В номинации можно выбрать не более " + cap + ".");
      return;
    }
    next = has ? n.my_vote.filter((x) => x !== id) : n.my_vote.concat([id]);
  }
  haptic("select");
  ballotInFlight = true;
  try {
    const data = await call("/api/ballot", { nomination_id: n.id, choices: next });
    Object.assign(n, data.nomination);
    haptic("success");
    showConfirmBanner(next.length ? "Голос учтён · " + n.name : "Голос снят · " + n.name);
  } catch (e) {
    alert(String(e.message || e));
    // Most refusals mean the page is out of date (the vote closed, the nomination was
    // edited or deleted), so it is brought up to date rather than left showing a lie.
    ballotInFlight = false;
    await reload().catch(() => {});
    return;
  } finally {
    ballotInFlight = false;
  }
  syncPicks();
  renderSub();
  renderTabs();
  renderResults();
  updateBar();
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
  syncPicks();
  renderTabs();
  renderHead();
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
      if (nominationId === active) { renderResults(); renderHead(); }
    }
  } catch (e) {
    alert("Не сохранилось: " + String(e.message || e));
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
    if (data.nomination_id && path.endsWith("/create")) active = data.nomination_id;
    render();
    haptic("success");
    if (success) showConfirmBanner(success);
    return true;
  } catch (e) {
    alert(String(e.message || e));
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
    : await adminChange("/api/nominations/create", { name }, "Номинация создана — отметь её работы");
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

$("go").addEventListener("click", async () => {
  if (state.is_admin) {
    const opening = !state.open;
    if (!opening && !confirm("Закрыть голосование во всех номинациях? Голосовать будет нельзя, пока не откроешь снова.")) return;
    await adminChange("/api/settings", { open: opening },
                      opening ? "Голосование открыто" : "Голосование закрыто");
    return;
  }
  const next = nextNomination();
  if (next) switchTab(next.id);
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
  if (tg && tg.BackButton) tg.BackButton.hide();
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
  if (tg && tg.BackButton) tg.BackButton.hide();
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

if (tg && tg.BackButton) {
  tg.BackButton.onClick(() => { if (!$("lens").hidden) closeLens(); else closeReel(); });
}
$("reelClose").addEventListener("click", closeReel);

// A tap on a picture in the reel closes it back to the grid; a scroll that comes to rest
// on one must not (measured from pointerdown, as v1 does).
let reelTap = null;
$("feed").addEventListener("pointerdown", (event) => {
  reelTap = event.target.tagName === "IMG"
    ? { x: event.clientX, y: event.clientY, at: Date.now(), target: event.target } : null;
});
$("feed").addEventListener("pointercancel", () => { reelTap = { cancelled: true }; });
$("feed").addEventListener("click", (event) => {
  const zoom = event.target.closest("[data-zoom]");
  if (zoom) { event.preventDefault(); reelTap = null; openLens(zoom.dataset.zoom); return; }
  if (event.target.tagName !== "IMG") return;
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
  $("sub").textContent = "";
  $("msg").hidden = false;
  $("msg").textContent = String(e.message || e);
});
</script>
</body>
</html>
"""
