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
# Same shape as _IS_ADMIN_KEY, for a different question: is this Telegram user a member of
# the chat the poll belongs to at all. bot_listener.py's real implementation checks
# Telegram chat membership; see create_app's docstring for why the default is permissive.
_IS_MEMBER_KEY = web.AppKey("is_member", Callable[[dict], Awaitable[bool]])
# Sends the winner announcement wherever bot_listener.py decides "the chat" currently
# means (its own DM with the admin who closed the vote, for now). Takes the admin's user
# dict, the poll, and the top 3 (Entry, votes) pairs ranked highest-first (fewer if there
# aren't 3 admitted entries) -- element 0 is always the recorded winner. Failure is caught
# by the caller and reported back, not raised through the API response.
_ANNOUNCE_KEY = web.AppKey(
    "announce", Callable[[dict, voting.Poll, list[tuple[voting.Entry, int]]], Awaitable[None]]
)
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


def _results_payload(poll: voting.Poll) -> list[dict]:
    """One dict per APPROVED entry, ranked most-votes-first (poll.tally()'s own order) --
    what the page's standings table renders. A separate function from _entry_payload
    because results have nothing to do with the media/text a voter picks from; they are
    shown after the fact, next to a vote count."""
    return [
        {"id": entry.entry_id, "author": entry.author_name, "username": entry.author_username, "votes": count}
        for entry, count in poll.tally()
    ]


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

    `results` (the standings, from _results_payload) is withheld from a voter who has not
    voted yet on a poll that's still open -- showing somebody a running vote count before
    they've cast their own ballot biases what they pick, so they only earn it by voting,
    or once there's nothing left to bias (poll closed), or in admin mode where it's simply
    moderation information. `voter_count` alone (just how many people have voted, not for
    whom) carries none of that bias, so it goes out unconditionally.
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
    is_member = await request.app[_IS_MEMBER_KEY](user)
    base = request.app[_ROUTE_PREFIX_KEY]
    visible = poll.entries if admin_mode else poll.approved_entries()
    winner = poll.winner()
    voted_already = str(user["id"]) in poll.votes

    payload = {
        "poll_id": poll.poll_id,
        "chat": poll.entry,
        "open": poll.open,
        "is_admin": admin_mode,
        "can_moderate": can_moderate,
        "is_member": is_member,
        "me": voting.display_name(user),
        "my_vote": poll.votes.get(str(user["id"]), []),
        "voter_count": len(poll.votes),
        "entries": [_entry_payload(e, poll, base) for e in visible],
        "winner": _entry_payload(winner, poll, base) if winner else None,
        # Both are voter-facing (they decide what the ballot UI lets you do), not just
        # admin-mode information -- unlike approved/counts below.
        "max_choices": poll.max_choices,
        "allow_revote": poll.allow_revote,
    }
    if voted_already or not poll.open or admin_mode:
        payload["results"] = _results_payload(poll)
    if admin_mode:
        payload["approved"] = list(poll.approved)
        payload["counts"] = {e.entry_id: count for e, count in poll.tally()}
    return web.json_response(payload)


async def handle_ballot(request: web.Request) -> web.Response:
    """Records this user's choices, replacing any earlier ballot of theirs -- unless the
    poll's own settings (set from the moderation screen) say otherwise: `max_choices`
    caps how many entries one ballot may name, and `allow_revote=False` locks a voter's
    first ballot in permanently. Both are rejected here, at the request boundary, rather
    than silently truncated in voting.record_vote -- a voter should know their ballot
    didn't count as sent, not have it quietly reshaped.

    Also refuses anyone the membership gate (_IS_MEMBER_KEY) doesn't recognize as still
    in the chat -- checked here rather than in voting.record_vote so the rule lives with
    the rest of the request-boundary validation, and so a rejected ballot never touches
    the poll at all."""
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
    if not await request.app[_IS_MEMBER_KEY](user):
        return _json_error("голосовать могут только участники чата", status=403)

    choices = body.get("choices")
    if not isinstance(choices, list) or not all(isinstance(c, str) for c in choices):
        return _json_error("choices must be a list of entry ids")

    if not poll.allow_revote and str(user["id"]) in poll.votes:
        return _json_error("менять голос нельзя -- голосование уже зафиксировано", status=409)
    distinct = len(set(choices))
    if poll.max_choices and distinct > poll.max_choices:
        return _json_error(f"можно выбрать не более {poll.max_choices}", status=400)

    voting.record_vote(poll, user["id"], choices)
    voting.save_poll(poll)
    request.app[_LOG_KEY](f"[vote_web] ballot from {voting.display_name(user)}: {len(choices)} choice(s)")
    return web.json_response({
        "ok": True,
        "my_vote": poll.votes[str(user["id"])],
        # Lets the page render the standings right after a vote without a second round
        # trip to /api/poll -- the voter has just earned the right to see them (see
        # handle_poll's docstring for the gating rule these mirror).
        "results": _results_payload(poll),
        "voter_count": len(poll.votes),
    })


async def handle_moderate(request: web.Request) -> web.Response:
    """Sets which nominations are admitted to the vote, and (optionally, whenever present
    in the body) its `max_choices`/`allow_revote` settings -- the moderation screen's one
    "Сохранить" button submits all of it together. Administrators only."""
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

    if "max_choices" in body:
        max_choices = body["max_choices"]
        if max_choices is not None and (
            isinstance(max_choices, bool) or not isinstance(max_choices, int) or max_choices < 1
        ):
            return _json_error("max_choices must be a positive integer or null")
        poll.max_choices = max_choices
    if "allow_revote" in body:
        poll.allow_revote = bool(body["allow_revote"])

    voting.set_approved(poll, approved)
    if "open" in body:
        poll.open = bool(body["open"])
    voting.save_poll(poll)
    request.app[_LOG_KEY](
        f"[vote_web] {voting.display_name(user)} admitted {len(poll.approved)}/{len(poll.entries)} entries"
    )
    return web.json_response({
        "ok": True, "approved": poll.approved, "open": poll.open,
        "max_choices": poll.max_choices, "allow_revote": poll.allow_revote,
    })


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
    # Recomputed fresh from the poll (now closed) rather than reused from close_and_announce,
    # which only ever tracks the single #1 winner -- the top 3 is purely a reporting concern.
    top = poll.tally()[:3]
    request.app[_LOG_KEY](
        f"[vote_web] {voting.display_name(user)} closed the vote -- winner {winner_entry.entry_id} ({votes} votes)"
    )

    notified = True
    try:
        await request.app[_ANNOUNCE_KEY](user, poll, top)
    except Exception as e:
        notified = False
        request.app[_LOG_KEY](f"[vote_web] announcing the winner failed: {e}")

    base = request.app[_ROUTE_PREFIX_KEY]
    return web.json_response({
        "ok": True,
        "notified": notified,
        "winner": _entry_payload(winner_entry, poll, base),
        "votes": votes,
        "top": [{"entry": _entry_payload(e, poll, base), "votes": v} for e, v in top],
    })


async def handle_clear(request: web.Request) -> web.Response:
    """Deletes the current poll outright -- entries, votes, admitted flags, downloaded
    photos, all of it -- so the next "/vote собрать" starts a genuinely fresh poll.
    Administrators only. Unlike announcing, there is nothing to keep on a failure here:
    delete_poll is a local filesystem operation, not a Telegram send that can fail
    independently of the state change."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_error("malformed request body")

    user = await _authenticate(request, body)
    if not await request.app[_IS_ADMIN_KEY](user):
        return _json_error("только администраторы могут очищать голосование", status=403)

    entry_name = request.app[_ENTRY_KEY]
    poll = voting.latest_poll(entry_name)
    if poll is None:
        return _json_error("голосование ещё не создано -- нечего очищать", status=404)

    voting.delete_poll(entry_name, poll.poll_id)
    request.app[_LOG_KEY](f"[vote_web] {voting.display_name(user)} cleared poll {poll.poll_id}")
    return web.json_response({"ok": True})


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
    cfg, entry: str, is_admin, announce=None, route_prefix: str = ROUTE_PREFIX, log=print,
    is_member=None,
) -> web.Application:
    """`is_admin` is an async callable taking the verified Telegram user dict and
    returning a bool; `announce` is an async callable taking (user, poll, top3) -- top3
    being the ranked (Entry, votes) pairs, winner first -- that delivers the announcement.
    Both are supplied by bot_listener.py, which owns the Bot API client they need, so this
    module needs to know nothing about how administrators are determined or where an
    announcement actually goes.

    `announce` defaults to a no-op so the app is still constructible (e.g. in tests that
    don't exercise closing a vote) without a bot_listener.py running alongside it.

    `is_member` is an async callable, same shape as `is_admin`, answering "is this
    Telegram user still in the chat". bot_listener.py supplies the real one, backed by a
    Telegram chat-membership check. It defaults to a permissive stand-in that always
    returns True -- not a security stance, just what keeps this module constructible
    standalone (tests, or running the page without the bot alongside it) instead of
    refusing every voter for want of a Bot API client to ask.
    """
    async def _default_announce(user, poll, top):
        return None

    async def _default_is_member(user):
        return True

    app = web.Application()
    app[_CFG_KEY] = cfg
    app[_ENTRY_KEY] = entry
    app[_IS_ADMIN_KEY] = is_admin
    app[_IS_MEMBER_KEY] = is_member or _default_is_member
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
        web.post(f"{prefix}/api/clear", handle_clear),
        web.get(prefix + "/media/{poll_id}/{name}", handle_media),
    ])
    return app


async def run_web_server(
    cfg, entry: str, is_admin, port: int, announce=None, log=print, is_member=None
) -> None:
    """Serves until cancelled, as a sibling task of the two listeners."""
    app = create_app(cfg, entry, is_admin, announce=announce, log=log, is_member=is_member)
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
  /* One continuous vertical feed, full-width cards -- no grid, nothing to open. A thin
     rule between cards (border-bottom) stands in for the grid's gaps as the separator. */
  .feed { padding: 4px 12px 0; }
  .card { padding: 14px 0; border-bottom: 1px solid rgba(128,128,128,.2); }
  .card:last-child { border-bottom: 0; }
  .card .who { font-size: 13px; font-weight: 600; margin-bottom: 6px;
               display: flex; align-items: center; }
  .card .cap { white-space: pre-wrap; margin: 0 0 10px; font-size: 14px; }
  .card .photos { display: flex; flex-direction: column; gap: 6px; margin-bottom: 10px; }
  .card .photos img { width: 100%; border-radius: 10px; display: block; }
  .votesBadge { margin-left: 6px; background: var(--accent); color: var(--accent-fg);
                font-size: 11px; padding: 1px 6px; border-radius: 8px; }
  .votebar { height: 4px; margin: 0 0 10px; border-radius: 2px;
             background: rgba(128,128,128,.25); overflow: hidden; }
  .votebar-fill { height: 100%; background: var(--accent); border-radius: 2px; }
  .pickBtn { display: block; width: 100%; border: 1px solid rgba(128,128,128,.35);
             border-radius: 8px; padding: 10px; font-size: 14px; font-weight: 600;
             background: transparent; color: var(--fg); cursor: pointer; }
  .pickBtn[disabled] { opacity: .5; cursor: default; }
  .card.on .pickBtn { background: var(--accent); color: var(--accent-fg); border-color: var(--accent); }
  .card.pending { opacity: .6; }
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
  .go.danger { background: transparent; color: #e5534b;
               border: 1px solid #e5534b; margin-bottom: 8px;
               font-size: 13px; padding: 10px; font-weight: 500; }
  .msg { padding: 24px 16px; color: var(--muted); text-align: center; }
  .winner { margin: 0 12px 4px; padding: 10px 12px; border-radius: 10px;
            background: var(--card); display: flex; gap: 10px; align-items: center; }
  .winner img { width: 48px; height: 48px; border-radius: 8px; object-fit: cover; flex: none; }
  .winner .label { font-size: 12px; color: var(--muted); }
  .winner .name { font-weight: 600; }
  .settings { margin: 0 12px 4px; padding: 10px 12px; border-radius: 10px;
              background: var(--card); font-size: 13px; }
  .settings .row { display: flex; align-items: center; justify-content: space-between;
                    gap: 8px; padding: 4px 0; }
  .settings input[type="number"] { width: 56px; text-align: center; border-radius: 6px;
              border: 1px solid rgba(128,128,128,.4); background: var(--bg);
              color: var(--fg); padding: 4px; font-size: 13px; }
  .settings input[type="checkbox"] { width: 18px; height: 18px; }
  /* Stands in for whatever reason the vote controls are replaced with a sentence instead
     of a button: locked-in ballot, closed poll, or (see is_member) not a chat member. */
  .notice { padding: 10px 12px; margin: 0 12px 4px; border-radius: 10px;
            background: var(--card); color: var(--muted); font-size: 13px; text-align: center; }
  /* The acceptance confirmation for an immediate (allow_revote) vote -- fades on its own
     rather than waiting to be dismissed, since nothing about it needs a decision. */
  .confirmBanner { margin: 8px 12px 0; padding: 8px 12px; border-radius: 8px;
                   background: var(--accent); color: var(--accent-fg); font-size: 13px;
                   text-align: center; opacity: 1; transition: opacity .6s ease; }
  .confirmBanner.fade { opacity: 0; }
  .results { margin: 4px 12px 12px; padding: 10px 12px; border-radius: 10px;
             background: var(--card); font-size: 13px; }
  .results h2 { margin: 0 0 4px; font-size: 14px; }
  .results .voterCount { color: var(--muted); margin-bottom: 8px; }
  .results .row { display: flex; align-items: center; gap: 8px; padding: 4px 0; }
  .results .rank { width: 16px; flex: none; color: var(--muted); font-size: 12px; }
  .results .name { flex: none; max-width: 38%; overflow: hidden;
                    text-overflow: ellipsis; white-space: nowrap; }
  .results .track { flex: 1; height: 8px; border-radius: 4px;
                     background: rgba(128,128,128,.2); overflow: hidden; }
  .results .fill { height: 100%; background: var(--accent); border-radius: 4px; }
  .results .count { flex: none; width: 26px; text-align: right; font-size: 12px; color: var(--muted); }
</style>
</head>
<body>
<header>
  <h1 id="title">Итоги недели</h1>
  <div class="sub" id="sub">Загружаю…</div>
</header>
<div class="confirmBanner" id="confirmBanner" hidden>Голос принят</div>
<div class="winner" id="winnerBanner" hidden></div>
<div class="settings" id="settings" hidden>
  <div class="row">
    <span>Сколько работ можно выбрать</span>
    <input type="number" id="maxChoices" min="1" placeholder="∞">
  </div>
  <div class="row">
    <span>Разрешить менять голос</span>
    <input type="checkbox" id="allowRevote">
  </div>
</div>
<div class="notice" id="notice" hidden></div>
<div class="feed" id="feed"></div>
<div class="msg" id="msg" hidden></div>
<div class="results" id="results" hidden></div>
<div class="bar" id="bar">
  <button class="go danger" id="clear" hidden>🗑 Очистить голосование</button>
  <button class="go secondary" id="announce" hidden>Подвести итоги</button>
  <button class="go" id="go" disabled>Загружаю…</button>
</div>

<script>
const PREFIX = "__PREFIX__";
const tg = window.Telegram && window.Telegram.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const initData = (tg && tg.initData) || "";

let poll = null;
let picked = new Set();     // entry ids the voter chose (server-confirmed when allow_revote)
let admitted = new Set();   // entry ids the admin admits (admin mode only)
let confirmTimers = [];     // pending timeouts for the fading "Голос принят" banner

const $ = (id) => document.getElementById(id);

// The bottom bar's height varies with how many buttons are actually showing (1 for a
// voter, up to 3 stacked for the admin's moderation view, or none once an allow_revote
// voter has nothing left to press) -- a fixed body padding sized for one button left the
// feed's last card hidden behind the bar whenever more (or fewer) buttons appeared.
// Tracked with a ResizeObserver instead of recomputed by hand after every place the bar's
// contents change, so it can't be missed. (id="bar" matters: observe(null) throws.)
if (window.ResizeObserver) {
  new ResizeObserver((entries) => {
    document.body.style.paddingBottom = (entries[0].contentRect.height + 16) + "px";
  }).observe($("bar"));
}

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

// Shows "Голос принят" for a couple of seconds, then fades it out -- the acceptance
// confirmation both voting modes give on a successful ballot (see submitBallot).
function showConfirmBanner() {
  const banner = $("confirmBanner");
  confirmTimers.forEach(clearTimeout);
  confirmTimers = [];
  banner.hidden = false;
  banner.classList.remove("fade");
  confirmTimers.push(setTimeout(() => banner.classList.add("fade"), 1600));
  confirmTimers.push(setTimeout(() => { banner.hidden = true; }, 2300));
}

function renderResults() {
  const box = $("results");
  if (!poll.results) { box.hidden = true; return; }  // not yet earned, see handle_poll
  box.hidden = false;
  const max = Math.max(1, ...poll.results.map((r) => r.votes));
  box.innerHTML =
    "<h2>Голоса</h2>" +
    '<div class="voterCount">Проголосовало: ' + (poll.voter_count || 0) + "</div>" +
    poll.results.map((r, i) =>
      '<div class="row">' +
        '<span class="rank">' + (i + 1) + "</span>" +
        '<span class="name">' + esc(who(r)) + "</span>" +
        '<span class="track"><span class="fill" style="width:' +
          Math.round(100 * r.votes / max) + '%"></span></span>' +
        '<span class="count">' + r.votes + "</span>" +
      "</div>"
    ).join("");
}

function updateAdminButtons() {
  const announce = $("announce");
  const clear = $("clear");
  if (!poll.is_admin) { announce.hidden = true; clear.hidden = true; return; }
  announce.hidden = false;
  announce.disabled = false;
  announce.textContent = poll.open ? "Закрыть голосование и объявить победителя" : "Пересчитать победителя";
  clear.hidden = false;
  clear.disabled = false;
}

// True once a voter can no longer submit anything, either because voting closed or
// because their one ballot is locked in (allow_revote=false and they already voted).
// Never true for the admin's own moderation view.
function ballotLocked() {
  if (poll.is_admin) return false;
  if (!poll.open) return true;
  return !poll.allow_revote && poll.my_vote && poll.my_vote.length > 0;
}

// Whether `id`'s card currently reads as chosen, and what its button should say. In
// allow_revote mode `picked` IS the server-confirmed vote (submitBallot keeps it in sync
// on every tap), so "chosen" there already means "accepted" -- hence "Голос учтён ✓"
// unconditionally. Without allow_revote, `picked` is a draft the voter is still lining up
// until they press the bottom bar's button, so it only means "accepted" once the ballot
// is actually locked in.
function isChosen(id) {
  if (poll.is_admin) return admitted.has(id);
  if (poll.allow_revote) return picked.has(id);
  if (ballotLocked()) return (poll.my_vote || []).includes(id);
  return picked.has(id);
}

function pickLabel(id) {
  if (poll.is_admin) return admitted.has(id) ? "Допущена" : "Допустить";
  if (poll.allow_revote) return picked.has(id) ? "Голос учтён ✓" : "Выбрать";
  if (ballotLocked()) return (poll.my_vote || []).includes(id) ? "Голос учтён ✓" : "Выбрать";
  return picked.has(id) ? "Выбрано" : "Выбрать";
}

function render() {
  renderWinnerBanner();
  renderResults();
  updateAdminButtons();
  const feed = $("feed");
  feed.innerHTML = "";
  if (!poll.entries.length) {
    $("msg").hidden = false;
    $("msg").textContent = poll.is_admin
      ? "За сегодня и вчера заявок с #итогинедели не нашлось."
      : "Работы ещё не допущены к голосованию. Загляни позже.";
    $("go").hidden = true;
    $("notice").hidden = true;
    return;
  }
  $("msg").hidden = true;
  // The bar is relative to the currently leading entry, not to the voter count, so it
  // stays readable in a poll with only a handful of ballots in so far.
  const maxCount = poll.is_admin && poll.counts
    ? Math.max(1, ...Object.values(poll.counts)) : 1;
  // A voter who isn't a chat member gets a disabled button on every card, plus the
  // notice updateButton() shows in place of the bar's own button. Admins are unaffected.
  const disablePicks = !poll.is_admin && poll.is_member === false;

  for (const entry of poll.entries) {
    const card = document.createElement("div");
    card.className = "card";
    const chosen = isChosen(entry.id);
    if (chosen) card.classList.add("on");
    if (poll.is_admin && !admitted.has(entry.id)) card.classList.add("pending");

    const count = poll.is_admin && poll.counts ? (poll.counts[entry.id] || 0) : 0;
    const votes = poll.is_admin && poll.counts && count
      ? '<span class="votesBadge">' + count + "</span>" : "";
    const votebar = poll.is_admin
      ? '<div class="votebar"><div class="votebar-fill" style="width:' +
        Math.round(100 * count / maxCount) + '%"></div></div>'
      : "";

    card.innerHTML =
      '<div class="who">' + esc(who(entry)) + votes + "</div>" +
      (entry.text ? '<div class="cap">' + esc(entry.text) + "</div>" : "") +
      '<div class="photos">' +
        entry.photos.map((p) => '<img loading="lazy" src="' + esc(p) + '" alt="">').join("") +
      "</div>" +
      votebar +
      '<button class="pickBtn" data-pick="' + esc(entry.id) + '"' +
        (disablePicks ? " disabled" : "") + ">" + pickLabel(entry.id) +
      "</button>";
    feed.appendChild(card);
  }
  updateButton();
}

function updateButton() {
  const go = $("go");
  const notice = $("notice");
  if (poll.is_admin) {
    notice.hidden = true;
    go.hidden = false;
    go.disabled = false;
    go.textContent = "Сохранить: допущено " + admitted.size + " из " + poll.entries.length;
    return;
  }
  if (poll.is_member === false) {
    go.hidden = true;
    notice.hidden = false;
    notice.textContent = "Голосовать могут только участники чата";
    return;
  }
  if (ballotLocked()) {
    go.hidden = true;
    notice.hidden = false;
    notice.textContent = poll.open ? "Голос зафиксирован — менять нельзя." : "Голосование закрыто.";
    return;
  }
  notice.hidden = true;
  if (poll.allow_revote) {
    // Voting IS the tap on a card in this mode -- there is nothing left for a bottom-bar
    // button to do (see onPickTap/toggleAndSubmit below).
    go.hidden = true;
    return;
  }
  go.hidden = false;
  const cap = poll.max_choices;
  go.disabled = picked.size === 0;
  go.textContent = picked.size
    ? "Проголосовать (" + picked.size + (cap ? "/" + cap : "") + ")"
    : (cap ? "Выбери до " + cap + " работ" : "Выбери работы");
}

// max_choices === 1 is special-cased to REPLACE the current pick rather than being
// refused for exceeding the cap -- a single-choice poll behaves like a radio button, not
// a checkbox that happens to top out at one.
function nextSelection(id, adding) {
  if (poll.max_choices === 1) return adding ? new Set([id]) : new Set();
  const next = new Set(picked);
  if (adding) next.add(id); else next.delete(id);
  return next;
}

function overCap(adding) {
  return adding && poll.max_choices && poll.max_choices !== 1 && picked.size >= poll.max_choices;
}

// Sends the current selection as a ballot and folds the server's answer back in --
// my_vote is the authoritative record, results/voter_count let the standings appear
// immediately without a second /api/poll round trip.
async function submitBallot(choices) {
  const response = await api("/api/ballot", {
    method: "POST", body: JSON.stringify({ init_data: initData, choices }),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "не получилось");
  picked = new Set(data.my_vote);
  poll.my_vote = data.my_vote;
  poll.results = data.results;
  poll.voter_count = data.voter_count;
  if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
  showConfirmBanner();
}

function toggleAdmitted(id) {
  const adding = !admitted.has(id);
  if (adding) admitted.add(id); else admitted.delete(id);
  if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
  render();
}

// allow_revote=false: tapping only stages the choice locally -- the ballot is final once
// sent, so the voter needs to be able to line up several picks before committing.
function toggleDraftPick(id) {
  const adding = !picked.has(id);
  if (overCap(adding)) {
    alert("Можно выбрать не более " + poll.max_choices + ".");
    return;
  }
  picked = nextSelection(id, adding);
  if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
  render();
}

// allow_revote=true (the default): the tap itself IS the vote -- no separate submit step.
// Tapping an already-chosen card removes that choice and re-submits.
//
// One ballot at a time: without the guard, a second tap while the first is still in
// flight would compute its selection from a `picked` the first request hasn't updated
// yet, and whichever response landed last would win -- so a fast double-tap could quietly
// drop a choice. Dropping the extra tap is better than recording the wrong ballot.
let ballotInFlight = false;

async function toggleAndSubmit(id) {
  if (ballotInFlight) return;
  const adding = !picked.has(id);
  if (overCap(adding)) {
    alert("Можно выбрать не более " + poll.max_choices + ".");
    return;
  }
  const choices = [...nextSelection(id, adding)];
  if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
  ballotInFlight = true;
  try {
    await submitBallot(choices);
  } catch (e) {
    alert(String(e.message || e));
  } finally {
    ballotInFlight = false;
  }
  render();
}

async function onPickTap(id) {
  if (poll.is_admin) { toggleAdmitted(id); return; }
  if (poll.is_member === false) {
    alert("Голосовать могут только участники чата");
    return;
  }
  if (ballotLocked()) {
    alert(poll.open ? "Голос уже зафиксирован, менять нельзя." : "Голосование закрыто.");
    return;
  }
  if (poll.allow_revote) {
    await toggleAndSubmit(id);
  } else {
    toggleDraftPick(id);
  }
}

document.addEventListener("click", (event) => {
  const pick = event.target.closest("[data-pick]");
  if (!pick || pick.disabled) return;
  event.preventDefault();
  onPickTap(pick.dataset.pick);
});

async function saveModeration() {
  const go = $("go");
  go.disabled = true;
  const original = go.textContent;
  go.textContent = "Отправляю…";
  try {
    const body = {
      init_data: initData,
      approved: [...admitted],
      max_choices: $("maxChoices").value ? parseInt($("maxChoices").value, 10) : null,
      allow_revote: $("allowRevote").checked,
    };
    const response = await api("/api/moderate", { method: "POST", body: JSON.stringify(body) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "не получилось");
    poll.max_choices = data.max_choices;
    poll.allow_revote = data.allow_revote;
    go.textContent = "Сохранено";
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    // Deferred, not immediate: render() would otherwise overwrite this confirmation text
    // right away with whatever updateButton() computes next.
    setTimeout(() => { render(); }, 1500);
  } catch (e) {
    go.textContent = String(e.message || e);
    setTimeout(() => { go.textContent = original; go.disabled = false; }, 2500);
  }
}

// allow_revote=false only: the bottom bar's own button, guarded by a confirm() since
// this submit is the one that locks the ballot in for good.
async function finalizeBallot() {
  if (!confirm(
    "Голос будет отправлен, изменить его потом будет нельзя. Проголосовать?"
  )) return;
  const go = $("go");
  go.disabled = true;
  const original = go.textContent;
  go.textContent = "Отправляю…";
  try {
    await submitBallot([...picked]);
    render();
  } catch (e) {
    go.textContent = String(e.message || e);
    setTimeout(() => { go.textContent = original; go.disabled = false; }, 2500);
  }
}

$("go").addEventListener("click", async () => {
  if (poll.is_admin) { await saveModeration(); return; }
  await finalizeBallot();
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
    renderWinnerBanner();  // shows the banner right away, without touching this button's text
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    button.textContent = data.notified
      ? "Победитель объявлен, сообщение отправлено тебе в личку"
      : "Победитель выбран, но сообщение не отправилось -- смотри логи";
    // Deferred: a full render() right now would immediately overwrite this confirmation
    // text via updateAdminButtons(), which recomputes the button label from poll.open.
    setTimeout(() => { render(); }, 4000);
  } catch (e) {
    button.textContent = String(e.message || e);
    setTimeout(() => { button.textContent = original; button.disabled = false; }, 2500);
  }
});

$("clear").addEventListener("click", async () => {
  if (!confirm(
    "Точно очистить голосование? Все заявки, голоса и настройки удалятся безвозвратно " +
    "-- дальше нужно будет /vote собрать заново."
  )) return;
  const button = $("clear");
  button.disabled = true;
  button.textContent = "Очищаю…";
  try {
    const response = await api("/api/clear", {
      method: "POST", body: JSON.stringify({ init_data: initData }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "не получилось");
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    poll = { poll_id: null, entries: [], is_admin: true, can_moderate: true, open: false, is_member: true };
    picked = new Set();
    admitted = new Set();
    $("winnerBanner").hidden = true;
    $("settings").hidden = true;
    $("notice").hidden = true;
    $("results").hidden = true;
    $("confirmBanner").hidden = true;
    $("announce").hidden = true;
    $("feed").innerHTML = "";
    $("go").hidden = true;
    $("sub").textContent = "";
    $("msg").hidden = false;
    $("msg").textContent = "Голосование очищено. Собери заново: /vote собрать";
    button.textContent = "Очищено";
  } catch (e) {
    button.textContent = "🗑 Очистить голосование";
    button.disabled = false;
    alert(String(e.message || e));
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
    $("settings").hidden = !poll.is_admin;
    if (poll.is_admin) {
      $("sub").textContent =
        "Режим модератора · заявок " + poll.entries.length + " · проголосовало " + (poll.voter_count || 0);
      $("maxChoices").value = poll.max_choices || "";
      $("allowRevote").checked = poll.allow_revote !== false;
    } else if (poll.is_member === false) {
      $("sub").textContent = "Голосовать могут только участники чата";
    } else {
      const voted = poll.my_vote && poll.my_vote.length;
      if (voted && !poll.allow_revote) {
        $("sub").textContent = "Твой голос зафиксирован — менять нельзя";
      } else {
        $("sub").textContent = voted ? "Твой голос учтён — можно переголосовать" : "Выбери понравившиеся работы";
      }
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
