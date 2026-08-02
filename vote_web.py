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
# Takes the FULL verified Telegram user dict, not just an id -- bot_listener.py's
# _can_manage_chat also honors a hardcoded username allowlist (PRIVILEGED_MANAGEMENT_
# USERNAMES), which needs the username, not only the id, to check.
_IS_ADMIN_KEY = web.AppKey("is_admin", Callable[[dict], Awaitable[bool]])
# Sends the winner announcement wherever bot_listener.py decides "the chat" currently
# means (its own DM with the admin who closed the vote, for now). Takes the admin's user
# dict, the poll, the winning entry, and its vote count; failure is caught by the caller
# and reported back, not raised through the API response.
_ANNOUNCE_KEY = web.AppKey("announce", Callable[[dict, voting.Poll, voting.Entry, int], Awaitable[None]])
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

    Moderation mode is requested explicitly, via `?mode=admin` on the page URL (see
    bot_listener.handle_vote_command's "/vote выбрать") -- being an administrator is NOT
    by itself enough to switch the view. Otherwise an administrator could never open the
    plain ballot to cast their own vote; bare "/vote" would always land them back in
    moderation. `can_moderate` still reports true admin status regardless of which view
    is showing, so the page can point them at "/vote выбрать" instead of hiding it.

    Moderation mode gets every nomination plus the admitted flags and live counts.
    Everyone else (including an admin viewing the plain ballot) gets only what has been
    admitted -- an unmoderated poll shows them nothing rather than showing them posts
    nobody has approved yet.
    """
    user = await _authenticate(request)
    entry_name = request.app[_ENTRY_KEY]
    poll = voting.latest_poll(entry_name)
    if poll is None:
        return web.json_response({
            "poll_id": None, "entries": [], "is_admin": False, "can_moderate": False, "open": False,
        })

    can_moderate = await request.app[_IS_ADMIN_KEY](user)
    admin_mode = can_moderate and request.query.get("mode") == "admin"
    base = request.app[_ROUTE_PREFIX_KEY]
    visible = poll.entries if admin_mode else poll.approved_entries()
    winner = poll.winner()

    payload = {
        "poll_id": poll.poll_id,
        "chat": poll.entry,
        "open": poll.open,
        "is_admin": admin_mode,
        "can_moderate": can_moderate,
        "me": voting.display_name(user),
        "my_vote": poll.votes.get(str(user["id"]), []),
        "entries": [_entry_payload(e, poll, base) for e in visible],
        "winner": _entry_payload(winner, poll, base) if winner else None,
    }
    if admin_mode:
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
    if not await request.app[_IS_ADMIN_KEY](user):
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


async def handle_announce(request: web.Request) -> web.Response:
    """Closes the vote, records the top-voted admitted entry as the winner, and asks
    bot_listener.py to send the announcement. Administrators only.

    Closing and picking the winner happen even if the send itself fails (a transient
    Bot API error, say) -- the poll's own state is the source of truth for who won, not
    whether a particular message went out, so a delivery failure is reported back rather
    than losing the result.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_error("malformed request body")

    user = await _authenticate(request, body)
    if not await request.app[_IS_ADMIN_KEY](user):
        return _json_error("только администраторы могут подводить итоги", status=403)

    entry_name = request.app[_ENTRY_KEY]
    poll = voting.latest_poll(entry_name)
    if poll is None:
        return _json_error("голосование ещё не создано", status=404)

    result = voting.close_and_announce(poll)
    if result is None:
        return _json_error("пока не за что подводить итоги -- нет голосов за допущенные работы", status=409)
    winner_entry, votes = result
    voting.save_poll(poll)
    request.app[_LOG_KEY](
        f"[vote_web] {voting.display_name(user)} closed the vote -- winner {winner_entry.entry_id} ({votes} votes)"
    )

    notified = True
    try:
        await request.app[_ANNOUNCE_KEY](user, poll, winner_entry, votes)
    except Exception as e:
        notified = False
        request.app[_LOG_KEY](f"[vote_web] announcing the winner failed: {e}")

    base = request.app[_ROUTE_PREFIX_KEY]
    return web.json_response({
        "ok": True,
        "notified": notified,
        "winner": _entry_payload(winner_entry, poll, base),
        "votes": votes,
    })


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


def create_app(
    cfg, entry: str, is_admin, announce=None, route_prefix: str = ROUTE_PREFIX, log=print
) -> web.Application:
    """`is_admin` is an async callable taking the verified Telegram user dict and
    returning a bool; `announce` is an async callable taking (user, poll, winner_entry,
    votes) that delivers the winner message. Both are supplied by bot_listener.py, which
    owns the Bot API client they need, so this module needs to know nothing about how
    administrators are determined or where an announcement actually goes.

    `announce` defaults to a no-op so the app is still constructible (e.g. in tests that
    don't exercise closing a vote) without a bot_listener.py running alongside it.
    """
    async def _default_announce(user, poll, winner_entry, votes):
        return None

    app = web.Application()
    app[_CFG_KEY] = cfg
    app[_ENTRY_KEY] = entry
    app[_IS_ADMIN_KEY] = is_admin
    app[_ANNOUNCE_KEY] = announce or _default_announce
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
        web.post(f"{prefix}/api/announce", handle_announce),
        web.get(prefix + "/media/{poll_id}/{name}", handle_media),
    ])
    return app


async def run_web_server(cfg, entry: str, is_admin, port: int, announce=None, log=print) -> None:
    """Serves until cancelled, as a sibling task of the two listeners."""
    app = create_app(cfg, entry, is_admin, announce=announce, log=log)
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
  .go.secondary { background: transparent; color: var(--accent);
                  border: 1px solid var(--accent); margin-bottom: 8px; }
  .msg { padding: 24px 16px; color: var(--muted); text-align: center; }
  .winner { margin: 0 12px 4px; padding: 10px 12px; border-radius: 10px;
            background: var(--card); display: flex; gap: 10px; align-items: center; }
  .winner img { width: 48px; height: 48px; border-radius: 8px; object-fit: cover; flex: none; }
  .winner .label { font-size: 12px; color: var(--muted); }
  .winner .name { font-weight: 600; }
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
<div class="winner" id="winnerBanner" hidden></div>
<div class="grid" id="grid"></div>
<div class="msg" id="msg" hidden></div>
<div class="bar">
  <button class="go secondary" id="announce" hidden>Подвести итоги</button>
  <button class="go" id="go" disabled>Загружаю…</button>
</div>

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

function renderWinnerBanner() {
  const banner = $("winnerBanner");
  if (!poll.winner) { banner.hidden = true; return; }
  const w = poll.winner;
  banner.hidden = false;
  banner.innerHTML =
    (w.photos[0] ? '<img src="' + esc(w.photos[0]) + '" alt="">' : "") +
    '<div><div class="label">🏆 Победитель голосования</div>' +
    '<div class="name">' + esc(who(w)) + "</div></div>";
}

function updateAnnounceButton() {
  const button = $("announce");
  if (!poll.is_admin) { button.hidden = true; return; }
  button.hidden = false;
  button.disabled = false;
  button.textContent = poll.open ? "Закрыть голосование и объявить победителя" : "Пересчитать победителя";
}

function render() {
  renderWinnerBanner();
  updateAnnounceButton();
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

$("announce").addEventListener("click", async () => {
  const button = $("announce");
  if (!confirm("Закрыть голосование и объявить победителя? Дальше голосовать будет нельзя.")) return;
  button.disabled = true;
  const original = button.textContent;
  button.textContent = "Подвожу итоги…";
  try {
    const response = await api("/api/announce", {
      method: "POST", body: JSON.stringify({ init_data: initData }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "не получилось");
    poll.open = false;
    poll.winner = data.winner;
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    button.textContent = data.notified
      ? "Победитель объявлен, сообщение отправлено тебе в личку"
      : "Победитель выбран, но сообщение не отправилось -- смотри логи";
    render();
    setTimeout(() => { button.textContent = original; button.disabled = false; }, 4000);
  } catch (e) {
    button.textContent = String(e.message || e);
    setTimeout(() => { button.textContent = original; button.disabled = false; }, 2500);
  }
});

(async function load() {
  try {
    // Forwards ?mode=admin straight through -- the server decides moderation vs. plain
    // ballot from this, being an admin alone is not enough (see handle_poll).
    const mode = new URLSearchParams(location.search).get("mode");
    const response = await api("/api/poll" + (mode ? "?mode=" + encodeURIComponent(mode) : ""));
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
    if (poll.is_admin) {
      $("sub").textContent =
        "Режим модератора · заявок " + poll.entries.length + " · проголосовало " + (poll.voter_count || 0);
    } else {
      $("sub").textContent = poll.my_vote && poll.my_vote.length
        ? "Твой голос учтён — можно переголосовать"
        : "Выбери понравившиеся работы";
      if (poll.can_moderate) {
        $("sub").textContent += " · модерация: /vote выбрать";
      }
    }
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
