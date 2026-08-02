"""The voting page and its API -- a Telegram Mini App served by the same process as the
bot, over aiohttp (already a dependency, for the Bot API client).

Everything that changes state authenticates the same way: the caller sends back the
initData Telegram handed the Mini App, and voting.verify_init_data checks its signature
against the bot token. There is no session, no cookie and no fallback path -- an
unsigned request is refused, so opening the URL in an ordinary browser can look but
cannot vote.

Photos are the one thing served without that check: an <img> cannot send a header, and
signing every image URL to protect pictures that were already posted publicly in the chat
would be ceremony without a threat. Their URLs are unguessable-ish (poll id plus message
id) but not secret.
"""

import json
import re
from pathlib import Path
from typing import Awaitable, Callable

from aiohttp import web

import voting

# Where the page and its API live, so the domain root stays free for a health check.
ROUTE_PREFIX = "/vote"

_SAFE_MEDIA_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")

# Typed keys for state stashed on the aiohttp Application -- plain string keys still work
# but warn on every access as of aiohttp 3.9+.
_CFG_KEY = web.AppKey("cfg")
_ENTRY_KEY = web.AppKey("entry", str)
_IS_ADMIN_KEY = web.AppKey("is_admin", Callable[[int], Awaitable[bool]])
_ROUTE_PREFIX_KEY = web.AppKey("route_prefix", str)
_LOG_KEY = web.AppKey("log", Callable[..., None])


def _json_error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _init_data_from(request: web.Request, body: dict | None = None) -> str:
    """Mini Apps send initData in a header on reads and in the body on writes; accept
    either so the client can use whichever fits the request."""
    if body and isinstance(body.get("init_data"), str):
        return body["init_data"]
    return request.headers.get("X-Telegram-Init-Data", "")


async def _authenticate(request: web.Request, body: dict | None = None) -> dict:
    """Returns the verified Telegram user, or raises web.HTTPUnauthorized. Every handler
    that reads or writes a poll goes through here -- there is deliberately no anonymous
    path to fall back to."""
    cfg = request.app[_CFG_KEY]
    try:
        return voting.verify_init_data(_init_data_from(request, body), cfg.telegram_bot_token)
    except voting.InitDataError as e:
        raise web.HTTPUnauthorized(
            text=json.dumps({"error": str(e)}, ensure_ascii=False),
            content_type="application/json",
        )


def _entry_payload(entry: voting.Entry, poll: voting.Poll, base: str) -> dict:
    return {
        "id": entry.entry_id,
        "author": entry.author_name,
        "username": entry.author_username,
        "text": entry.text,
        "posted_at": entry.posted_at,
        "photos": [f"{base}/media/{poll.poll_id}/{name}" for name in entry.media],
    }


async def handle_poll(request: web.Request) -> web.Response:
    """What the page renders: the entries this caller may see, and their own current vote.

    An administrator gets every nomination plus the admitted flags and live counts, which
    is what turns the same page into the moderation screen. Everyone else gets only what
    has been admitted -- an unmoderated poll shows them nothing rather than showing them
    posts nobody has approved yet.
    """
    user = await _authenticate(request)
    entry_name = request.app[_ENTRY_KEY]
    poll = voting.latest_poll(entry_name)
    if poll is None:
        return web.json_response({"poll_id": None, "entries": [], "is_admin": False, "open": False})

    is_admin = await request.app[_IS_ADMIN_KEY](user["id"])
    base = request.app[_ROUTE_PREFIX_KEY]
    visible = poll.entries if is_admin else poll.approved_entries()

    payload = {
        "poll_id": poll.poll_id,
        "chat": poll.entry,
        "open": poll.open,
        "is_admin": is_admin,
        "me": voting.display_name(user),
        "my_vote": poll.votes.get(str(user["id"]), []),
        "entries": [_entry_payload(e, poll, base) for e in visible],
    }
    if is_admin:
        payload["approved"] = list(poll.approved)
        payload["voter_count"] = len(poll.votes)
        payload["counts"] = {e.entry_id: count for e, count in poll.tally()}
    return web.json_response(payload)


async def handle_ballot(request: web.Request) -> web.Response:
    """Records this user's choices, replacing any earlier ballot of theirs."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_error("malformed request body")

    user = await _authenticate(request, body)
    entry_name = request.app[_ENTRY_KEY]
    poll = voting.latest_poll(entry_name)
    if poll is None:
        return _json_error("голосование ещё не создано", status=404)
    if not poll.open:
        return _json_error("голосование закрыто", status=409)

    choices = body.get("choices")
    if not isinstance(choices, list) or not all(isinstance(c, str) for c in choices):
        return _json_error("choices must be a list of entry ids")

    voting.record_vote(poll, user["id"], choices)
    voting.save_poll(poll)
    request.app[_LOG_KEY](f"[vote_web] ballot from {voting.display_name(user)}: {len(choices)} choice(s)")
    return web.json_response({"ok": True, "my_vote": poll.votes[str(user["id"])]})


async def handle_moderate(request: web.Request) -> web.Response:
    """Sets which nominations are admitted to the vote. Administrators only."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_error("malformed request body")

    user = await _authenticate(request, body)
    if not await request.app[_IS_ADMIN_KEY](user["id"]):
        return _json_error("только администраторы могут допускать работы", status=403)

    entry_name = request.app[_ENTRY_KEY]
    poll = voting.latest_poll(entry_name)
    if poll is None:
        return _json_error("голосование ещё не создано", status=404)

    approved = body.get("approved")
    if not isinstance(approved, list) or not all(isinstance(a, str) for a in approved):
        return _json_error("approved must be a list of entry ids")

    voting.set_approved(poll, approved)
    if "open" in body:
        poll.open = bool(body["open"])
    voting.save_poll(poll)
    request.app[_LOG_KEY](
        f"[vote_web] {voting.display_name(user)} admitted {len(poll.approved)}/{len(poll.entries)} entries"
    )
    return web.json_response({"ok": True, "approved": poll.approved, "open": poll.open})


async def handle_media(request: web.Request) -> web.Response:
    """Serves one downloaded photo. The name is matched against a strict pattern rather
    than merely resolved, so nothing outside the poll's own media directory is reachable
    however the path is spelled."""
    poll_id = request.match_info["poll_id"]
    name = request.match_info["name"]
    if not _SAFE_MEDIA_NAME.match(poll_id or "") or not _SAFE_MEDIA_NAME.match(name or ""):
        raise web.HTTPNotFound()

    directory = voting.media_path(request.app[_ENTRY_KEY], poll_id)
    path = (directory / name).resolve()
    if not str(path).startswith(str(directory.resolve())) or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


async def handle_page(request: web.Request) -> web.Response:
    return web.Response(
        text=PAGE_HTML.replace("__PREFIX__", request.app[_ROUTE_PREFIX_KEY]),
        content_type="text/html",
    )


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def create_app(cfg, entry: str, is_admin, route_prefix: str = ROUTE_PREFIX, log=print) -> web.Application:
    """`is_admin` is an async callable taking a Telegram user id and returning a bool --
    supplied by bot_listener.py, which owns the Bot API client that can answer it, so this
    module needs to know nothing about how administrators are determined."""
    app = web.Application()
    app[_CFG_KEY] = cfg
    app[_ENTRY_KEY] = entry
    app[_IS_ADMIN_KEY] = is_admin
    app[_ROUTE_PREFIX_KEY] = route_prefix.rstrip("/")
    app[_LOG_KEY] = log

    prefix = app[_ROUTE_PREFIX_KEY]
    app.add_routes([
        web.get("/", handle_health),
        web.get("/health", handle_health),
        web.get(prefix, handle_page),
        web.get(f"{prefix}/", handle_page),
        web.get(f"{prefix}/api/poll", handle_poll),
        web.post(f"{prefix}/api/ballot", handle_ballot),
        web.post(f"{prefix}/api/moderate", handle_moderate),
        web.get(prefix + "/media/{poll_id}/{name}", handle_media),
    ])
    return app


async def run_web_server(cfg, entry: str, is_admin, port: int, log=print) -> None:
    """Serves until cancelled, as a sibling task of the two listeners."""
    app = create_app(cfg, entry, is_admin, log=log)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    log(f"[vote_web] voting page on port {port} at {ROUTE_PREFIX}")
    try:
        # Nothing else to do: aiohttp serves from the runner's own tasks. Sleeping
        # forever keeps the runner alive and gives cancellation somewhere to land.
        import asyncio

        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


PAGE_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Голосование</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root {
    --bg: var(--tg-theme-bg-color, #17212b);
    --fg: var(--tg-theme-text-color, #f5f5f5);
    --muted: var(--tg-theme-hint-color, #8a9aa9);
    --card: var(--tg-theme-secondary-bg-color, #232e3c);
    --accent: var(--tg-theme-button-color, #3390ec);
    --accent-fg: var(--tg-theme-button-text-color, #fff);
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 15px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding-bottom: 96px;
  }
  header { padding: 14px 12px 6px; }
  h1 { font-size: 17px; margin: 0 0 2px; }
  .sub { color: var(--muted); font-size: 13px; }
  .grid {
    display: grid; grid-template-columns: repeat(3, 1fr);
    gap: 8px; padding: 12px;
  }
  .card { background: var(--card); border-radius: 10px; overflow: hidden; position: relative; }
  .thumb { position: relative; width: 100%; aspect-ratio: 1; display: block; }
  .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .count { position: absolute; right: 4px; top: 4px; background: rgba(0,0,0,.6);
           color: #fff; font-size: 11px; padding: 1px 5px; border-radius: 8px; }
  .who { padding: 5px 6px 2px; font-size: 11px; overflow: hidden;
         text-overflow: ellipsis; white-space: nowrap; }
  .pick { display: block; width: 100%; border: 0; padding: 7px 4px; font-size: 12px;
          background: transparent; color: var(--muted); cursor: pointer;
          border-top: 1px solid rgba(128,128,128,.25); }
  .card.on { outline: 2px solid var(--accent); }
  .card.on .pick { background: var(--accent); color: var(--accent-fg); font-weight: 600; }
  .card.pending { opacity: .55; }
  .votes { position: absolute; left: 4px; top: 4px; background: var(--accent);
           color: var(--accent-fg); font-size: 11px; padding: 1px 6px; border-radius: 8px; }
  .bar {
    position: fixed; left: 0; right: 0; bottom: 0; padding: 10px 12px;
    padding-bottom: calc(10px + env(safe-area-inset-bottom));
    background: var(--bg); border-top: 1px solid rgba(128,128,128,.25);
  }
  .go { width: 100%; border: 0; border-radius: 10px; padding: 14px;
        font-size: 16px; font-weight: 600; background: var(--accent);
        color: var(--accent-fg); cursor: pointer; }
  .go[disabled] { opacity: .5; }
  .msg { padding: 24px 16px; color: var(--muted); text-align: center; }
  dialog {
    border: 0; padding: 0; width: 100%; max-width: 100%; height: 100%; max-height: 100%;
    margin: 0; background: var(--bg); color: var(--fg);
  }
  dialog::backdrop { background: rgba(0,0,0,.85); }
  .full { padding: 12px 12px 100px; overflow-y: auto; height: 100%; }
  .full img { width: 100%; border-radius: 10px; margin-bottom: 8px; }
  .full .cap { white-space: pre-wrap; margin: 8px 0 4px; }
  .close { position: sticky; top: 0; float: right; border: 0; border-radius: 8px;
           background: var(--card); color: var(--fg); font-size: 15px;
           padding: 8px 14px; cursor: pointer; z-index: 2; }
  .toggle { margin-left: 6px; font-size: 12px; color: var(--muted); }
</style>
</head>
<body>
<header>
  <h1 id="title">Итоги недели</h1>
  <div class="sub" id="sub">Загружаю…</div>
</header>
<div class="grid" id="grid"></div>
<div class="msg" id="msg" hidden></div>
<div class="bar"><button class="go" id="go" disabled>Загружаю…</button></div>

<dialog id="lightbox"><div class="full" id="full"></div></dialog>

<script>
const PREFIX = "__PREFIX__";
const tg = window.Telegram && window.Telegram.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const initData = (tg && tg.initData) || "";

let poll = null;
let picked = new Set();     // entry ids the voter chose
let admitted = new Set();   // entry ids the admin admits (admin mode only)

const $ = (id) => document.getElementById(id);

function api(path, options = {}) {
  const headers = Object.assign(
    { "X-Telegram-Init-Data": initData }, options.headers || {}
  );
  if (options.body) headers["Content-Type"] = "application/json";
  return fetch(PREFIX + path, Object.assign({}, options, { headers }));
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function who(entry) {
  return entry.username ? "@" + entry.username : entry.author;
}

function render() {
  const grid = $("grid");
  grid.innerHTML = "";
  if (!poll.entries.length) {
    $("msg").hidden = false;
    $("msg").textContent = poll.is_admin
      ? "За сегодня и вчера заявок с #итогинедели не нашлось."
      : "Работы ещё не допущены к голосованию. Загляни позже.";
    $("go").hidden = true;
    return;
  }
  for (const entry of poll.entries) {
    const card = document.createElement("div");
    card.className = "card";
    const chosen = poll.is_admin ? admitted.has(entry.id) : picked.has(entry.id);
    if (chosen) card.classList.add("on");
    if (poll.is_admin && !admitted.has(entry.id)) card.classList.add("pending");

    const votes = poll.is_admin && poll.counts && poll.counts[entry.id]
      ? '<span class="votes">' + poll.counts[entry.id] + "</span>" : "";
    const more = entry.photos.length > 1
      ? '<span class="count">+' + (entry.photos.length - 1) + "</span>" : "";

    card.innerHTML =
      '<a class="thumb" href="#" data-open="' + esc(entry.id) + '">' +
        '<img loading="lazy" src="' + esc(entry.photos[0]) + '" alt="">' +
        more + votes +
      "</a>" +
      '<div class="who">' + esc(who(entry)) + "</div>" +
      '<button class="pick" data-pick="' + esc(entry.id) + '">' +
        (poll.is_admin ? (chosen ? "допущена" : "допустить")
                       : (chosen ? "выбрано" : "выбрать")) +
      "</button>";
    grid.appendChild(card);
  }
  updateButton();
}

function updateButton() {
  const go = $("go");
  go.hidden = false;
  if (poll.is_admin) {
    go.disabled = false;
    go.textContent = "Сохранить: допущено " + admitted.size + " из " + poll.entries.length;
  } else if (!poll.open) {
    go.disabled = true;
    go.textContent = "Голосование закрыто";
  } else {
    go.disabled = picked.size === 0;
    go.textContent = picked.size ? "Проголосовать (" + picked.size + ")" : "Выбери работы";
  }
}

function openEntry(id) {
  const entry = poll.entries.find((e) => e.id === id);
  if (!entry) return;
  const chosen = poll.is_admin ? admitted.has(id) : picked.has(id);
  $("full").innerHTML =
    '<button class="close" id="closeBtn">Закрыть</button>' +
    "<h2>" + esc(who(entry)) + "</h2>" +
    (entry.text ? '<div class="cap">' + esc(entry.text) + "</div>" : "") +
    entry.photos.map((p) => '<img src="' + esc(p) + '" alt="">').join("") +
    '<button class="go" data-pick="' + esc(entry.id) + '">' +
      (poll.is_admin ? (chosen ? "Не допускать" : "Допустить")
                     : (chosen ? "Убрать выбор" : "Выбрать")) +
    "</button>";
  $("lightbox").showModal();
}

function togglePick(id) {
  const set = poll.is_admin ? admitted : picked;
  if (set.has(id)) set.delete(id); else set.add(id);
  if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
  render();
}

document.addEventListener("click", (event) => {
  const open = event.target.closest("[data-open]");
  if (open) { event.preventDefault(); openEntry(open.dataset.open); return; }
  const pick = event.target.closest("[data-pick]");
  if (pick) {
    event.preventDefault();
    const inDialog = !!pick.closest("dialog");
    togglePick(pick.dataset.pick);
    if (inDialog) $("lightbox").close();
    return;
  }
  if (event.target.id === "closeBtn") $("lightbox").close();
});

$("go").addEventListener("click", async () => {
  const go = $("go");
  go.disabled = true;
  const original = go.textContent;
  go.textContent = "Отправляю…";
  try {
    const path = poll.is_admin ? "/api/moderate" : "/api/ballot";
    const body = poll.is_admin
      ? { init_data: initData, approved: [...admitted] }
      : { init_data: initData, choices: [...picked] };
    const response = await api(path, { method: "POST", body: JSON.stringify(body) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "не получилось");
    go.textContent = poll.is_admin ? "Сохранено" : "Голос принят";
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    setTimeout(() => { go.textContent = original; go.disabled = false; }, 1500);
  } catch (e) {
    go.textContent = String(e.message || e);
    setTimeout(() => { go.textContent = original; go.disabled = false; }, 2500);
  }
});

(async function load() {
  try {
    const response = await api("/api/poll");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "не получилось загрузить");
    poll = data;
    if (!poll.poll_id) {
      $("sub").textContent = "";
      $("msg").hidden = false;
      $("msg").textContent = "Голосование ещё не создано.";
      $("go").hidden = true;
      return;
    }
    picked = new Set(poll.my_vote || []);
    admitted = new Set(poll.approved || []);
    $("sub").textContent = poll.is_admin
      ? "Режим модератора · заявок " + poll.entries.length + " · проголосовало " + (poll.voter_count || 0)
      : (poll.my_vote && poll.my_vote.length ? "Твой голос учтён — можно переголосовать" : "Выбери понравившиеся работы");
    render();
  } catch (e) {
    $("sub").textContent = "";
    $("msg").hidden = false;
    $("msg").textContent = String(e.message || e);
    $("go").hidden = true;
  }
})();
</script>
</body>
</html>
"""
