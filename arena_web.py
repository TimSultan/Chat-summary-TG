"""The arena's HTTP surface: a second Mini App, served beside v1's on the same port.

Mounted onto the application vote_web.py builds (see `attach`), which is the only thing
the two systems share at runtime -- a process and a port. Every route here reads and
writes arena.py's own storage; nothing touches a poll.

Authentication is the same mechanism as v1 and deliberately so: Telegram signs the
initData it hands a Mini App, voting.verify_init_data checks that signature against the
bot token, and the verified user id IS the voter's identity. In the reference
implementation (import/CLAUDE.md) that identity is an invite code handed out by the
organiser; here Telegram already knows who everybody is, so there are no codes to lose.

Photos are served unauthenticated for the same reason v1's are: an <img> cannot carry a
header, and these pictures were already posted publicly in the chat.
"""

import json
import re
from typing import Awaitable, Callable

from aiohttp import web

import arena
import voting
from arena_core import TIE

ROUTE_PREFIX = "/arena"

# How much of the table a voter who has finished is shown. The head of it is the interesting
# part and the tail is mostly noise at any realistic coverage -- see arena_core.is_separated.
TOP_LIMIT = 10

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")

_CFG_KEY = web.AppKey("arena_cfg")
_ENTRY_KEY = web.AppKey("arena_entry", str)
_IS_ADMIN_KEY = web.AppKey("arena_is_admin", Callable[[dict], Awaitable[bool]])
_IS_MEMBER_KEY = web.AppKey("arena_is_member", Callable[[dict], Awaitable[bool]])
_PREFIX_KEY = web.AppKey("arena_prefix", str)
_LOG_KEY = web.AppKey("arena_log", Callable[..., None])


def _json_error(message: str, status: int = 400, code: str = "ERROR") -> web.Response:
    return web.json_response({"error": code, "message": message}, status=status)


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


async def _body(request: web.Request) -> dict:
    try:
        return await request.json()
    except (json.JSONDecodeError, ValueError):
        return {}


def _entry_payload(entry, tournament, base: str) -> dict:
    return {
        "id": entry.entry_id,
        "author": entry.author_name,
        "username": entry.author_username,
        "text": entry.text,
        "photos": [
            f"{base}/media/{tournament.tournament_id}/{name}" for name in entry.media
        ],
    }


def _pair_payload(ballot, tournament, base: str) -> dict | None:
    """The pair this voter is on, with both works in full -- or None when they are done.
    Sent whole rather than as two ids because the page has nothing else to draw from: it
    never holds the field, only ever the two works in front of it."""
    if ballot.position >= len(ballot.pairs):
        return None
    left_id, right_id = ballot.pairs[ballot.position]
    left, right = tournament.entry_by_id(left_id), tournament.entry_by_id(right_id)
    if left is None or right is None:
        return None  # a work removed mid-session; the page treats it as the end
    return {
        "index": ballot.position,
        "left": _entry_payload(left, tournament, base),
        "right": _entry_payload(right, tournament, base),
    }


def _ballot_payload(ballot, tournament, base: str) -> dict:
    return {
        "position": ballot.position,
        "total": len(ballot.pairs),
        "status": ballot.status,
        "pair": _pair_payload(ballot, tournament, base),
    }


def _standings_payload(tournament, base: str, limit: int | None = None) -> list[dict]:
    """The fitted table for the admin view. `margin` travels with every row because a
    rating without its error bar invites reading a 12-point gap as a result."""
    rows = arena.standings_cached(tournament, force=True)["rows"]
    if limit:
        rows = rows[:limit]
    return [
        {
            "id": row["entry_id"],
            "author": row["entry"].author_name,
            "username": row["entry"].author_username,
            "rating": round(row["rating"]),
            "margin": (round(row["margin"]) if row["margin"] is not None else None),
            "played": row["played"],
            "score": row["score"],
            "win_rate": row["win_rate"],
            "photo": (
                f"{base}/media/{tournament.tournament_id}/{row['entry'].media[0]}"
                if row["entry"].media else None
            ),
        }
        for row in rows
    ]


async def handle_state(request: web.Request) -> web.Response:
    """What the page needs to draw itself, before anybody has voted.

    Read-only: opening the arena must not create a ballot, or a curious tap would use up
    somebody's one session. Starting is POST /api/session, always.

    Moderation data (every entry, the admitted flags, the table, the progress) is sent
    only in admin mode, and admin mode is asked for explicitly with ?mode=admin -- being
    an administrator is not by itself enough, exactly as in v1, or an admin could never
    open the plain arena to cast their own vote.
    """
    user = await _authenticate(request)
    entry_name = request.app[_ENTRY_KEY]
    base = request.app[_PREFIX_KEY]
    tournament = arena.latest_tournament(entry_name)
    if tournament is None:
        return web.json_response({"tournament_id": None, "is_admin": False, "can_moderate": False})

    can_moderate = await request.app[_IS_ADMIN_KEY](user)
    admin_mode = can_moderate and request.query.get("mode") == "admin"
    ballot = tournament.ballots.get(str(user["id"]))

    payload = {
        "tournament_id": tournament.tournament_id,
        "open": tournament.open,
        "is_admin": admin_mode,
        "can_moderate": can_moderate,
        "is_member": await request.app[_IS_MEMBER_KEY](user),
        "me": voting.display_name(user),
        "works": len(tournament.approved),
        "pairs_per_voter": tournament.pairs_per_voter,
        "pairing": tournament.pairing,
        "ballot": _ballot_payload(ballot, tournament, base) if ballot else None,
    }
    if admin_mode:
        payload["entries"] = [_entry_payload(e, tournament, base) for e in tournament.entries]
        payload["approved"] = list(tournament.approved)
        payload["standings"] = _standings_payload(tournament, base)
        payload["progress"] = tournament.progress()
    return web.json_response(payload)


async def handle_session(request: web.Request) -> web.Response:
    """Start or resume this voter's ballot -- the reference implementation's POST /session.

    Under the lock, like every other read-modify-write here: two taps arriving together
    would otherwise both find no ballot, deal two different sets of pairs, and one would
    overwrite the other -- which is precisely the "one voter, one ballot" rule.
    """
    body = await _body(request)
    user = await _authenticate(request, body)
    entry_name = request.app[_ENTRY_KEY]
    base = request.app[_PREFIX_KEY]

    async with arena.arena_lock:
        tournament = arena.latest_tournament(entry_name)
        if tournament is None:
            return _json_error("арена ещё не создана", status=404, code="NO_TOURNAMENT")
        if not await request.app[_IS_MEMBER_KEY](user):
            return _json_error(
                "голосовать могут только участники чата", status=403, code="NOT_A_MEMBER"
            )
        try:
            ballot = arena.start_session(tournament, user["id"], voting.display_name(user))
        except arena.ArenaError as e:
            status = 409 if e.code in ("ALREADY_VOTED", "VOTING_CLOSED") else 400
            return _json_error(e.message, status=status, code=e.code)
        arena.save_tournament(tournament)

    request.app[_LOG_KEY](
        f"[arena] {voting.display_name(user)} is on pair {ballot.position + 1}/{len(ballot.pairs)}"
    )
    return web.json_response({"ok": True, "ballot": _ballot_payload(ballot, tournament, base)})


async def handle_pick(request: web.Request) -> web.Response:
    """Record one head-to-head. Body: {position, pick} where pick is an entry id or "tie".

    Returns the whole ballot, not an acknowledgement: the page advances optimistically on
    the tap and reconciles against `position` when this lands, so a pick that did not count
    (a duplicate, a stale tab) puts the reader back where the server thinks they are.
    """
    body = await _body(request)
    user = await _authenticate(request, body)
    entry_name = request.app[_ENTRY_KEY]
    base = request.app[_PREFIX_KEY]

    position, pick = body.get("position"), body.get("pick")
    if not isinstance(position, int) or position < 0:
        return _json_error("position must be a whole number", code="BAD_REQUEST")
    if not isinstance(pick, str) or not pick:
        return _json_error("pick is required", code="BAD_REQUEST")

    async with arena.arena_lock:
        tournament = arena.latest_tournament(entry_name)
        if tournament is None:
            return _json_error("арена ещё не создана", status=404, code="NO_TOURNAMENT")
        try:
            ballot = arena.record_pick(tournament, user["id"], position, pick)
        except arena.ArenaError as e:
            status = 409 if e.code in ("BALLOT_COMPLETE", "VOTING_CLOSED") else 400
            return _json_error(e.message, status=status, code=e.code)
        arena.save_tournament(tournament)

    return web.json_response({"ok": True, "ballot": _ballot_payload(ballot, tournament, base)})


async def handle_undo(request: web.Request) -> web.Response:
    """Take back the last pick. No body beyond the credentials: there is only ever one
    judgement that can be undone, the most recent one, so a position would be a second
    opinion about where we are and a chance for the two to disagree.

    Returns the whole ballot like /api/pick does, and for the same reason -- the page
    redraws from it rather than trying to reconstruct the pair it just left.
    """
    body = await _body(request)
    user = await _authenticate(request, body)
    entry_name = request.app[_ENTRY_KEY]
    base = request.app[_PREFIX_KEY]

    async with arena.arena_lock:
        tournament = arena.latest_tournament(entry_name)
        if tournament is None:
            return _json_error("арена ещё не создана", status=404, code="NO_TOURNAMENT")
        try:
            ballot = arena.undo_pick(tournament, user["id"])
        except arena.ArenaError as e:
            status = 409 if e.code in ("BALLOT_COMPLETE", "VOTING_CLOSED") else 400
            return _json_error(e.message, status=status, code=e.code)
        arena.save_tournament(tournament)

    return web.json_response({"ok": True, "ballot": _ballot_payload(ballot, tournament, base)})


async def handle_top(request: web.Request) -> web.Response:
    """The head of the table, for somebody who has finished voting.

    Deliberately a separate route from handle_standings with a softer guard rather than a
    relaxed version of it: a voter gets the first few places only, and only once their OWN
    ballot is closed. A running ranking in front of an unfinished voter is precisely the
    bias the pairing is built to avoid; behind a closed ballot it can no longer reach any
    of their picks.
    """
    user = await _authenticate(request)
    tournament = arena.latest_tournament(request.app[_ENTRY_KEY])
    if tournament is None:
        return _json_error("арена ещё не создана", status=404, code="NO_TOURNAMENT")

    ballot = tournament.ballots.get(str(user["id"]))
    if not (ballot and ballot.status == "done") and not await request.app[_IS_ADMIN_KEY](user):
        return _json_error("сначала пройди все пары", status=403, code="NOT_FINISHED")
    return web.json_response({
        "top": _standings_payload(tournament, request.app[_PREFIX_KEY], limit=TOP_LIMIT)
    })


async def handle_standings(request: web.Request) -> web.Response:
    """The fitted table. Administrators only -- a running ranking shown to a voter who has
    not finished is exactly the bias the pairing is built to avoid."""
    user = await _authenticate(request)
    if not await request.app[_IS_ADMIN_KEY](user):
        return _json_error("только для администраторов", status=403, code="FORBIDDEN")
    tournament = arena.latest_tournament(request.app[_ENTRY_KEY])
    if tournament is None:
        return _json_error("арена ещё не создана", status=404, code="NO_TOURNAMENT")
    return web.json_response({
        "standings": _standings_payload(tournament, request.app[_PREFIX_KEY]),
        "progress": tournament.progress(),
    })


async def handle_progress(request: web.Request) -> web.Response:
    """How far along the vote is. Administrators only, same reason."""
    user = await _authenticate(request)
    if not await request.app[_IS_ADMIN_KEY](user):
        return _json_error("только для администраторов", status=403, code="FORBIDDEN")
    tournament = arena.latest_tournament(request.app[_ENTRY_KEY])
    if tournament is None:
        return _json_error("арена ещё не создана", status=404, code="NO_TOURNAMENT")
    return web.json_response({"progress": tournament.progress()})


async def handle_moderate(request: web.Request) -> web.Response:
    """Admit works and set how the arena runs: pairs per voter, pairing mode, open/closed.
    Administrators only. One "Сохранить" submits all of it, as in v1's moderation screen.

    Changing the admitted set invalidates the cached table outright rather than letting it
    go stale: un-admitting a work changes every rating, and adaptive pairing would go on
    seeding pairs from a table that still contains it.
    """
    body = await _body(request)
    user = await _authenticate(request, body)
    if not await request.app[_IS_ADMIN_KEY](user):
        return _json_error("только администраторы могут допускать работы", status=403, code="FORBIDDEN")

    entry_name = request.app[_ENTRY_KEY]
    async with arena.arena_lock:
        tournament = arena.latest_tournament(entry_name)
        if tournament is None:
            return _json_error("арена ещё не создана", status=404, code="NO_TOURNAMENT")

        approved = body.get("approved")
        if not isinstance(approved, list) or not all(isinstance(a, str) for a in approved):
            return _json_error("approved must be a list of entry ids", code="BAD_REQUEST")
        arena.set_approved(tournament, approved)

        if "pairs_per_voter" in body:
            value = body["pairs_per_voter"]
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 50:
                return _json_error("pairs_per_voter must be 1-50", code="BAD_REQUEST")
            tournament.pairs_per_voter = value
        if "pairing" in body:
            if body["pairing"] not in arena.PAIRING_MODES:
                return _json_error("unknown pairing mode", code="BAD_REQUEST")
            tournament.pairing = body["pairing"]
        if "open" in body:
            tournament.open = bool(body["open"])

        arena.invalidate_standings(tournament.tournament_id)
        arena.save_tournament(tournament)

    request.app[_LOG_KEY](
        f"[arena] {voting.display_name(user)} admitted {len(tournament.approved)}/"
        f"{len(tournament.entries)} works, {tournament.pairs_per_voter} pairs, {tournament.pairing}"
    )
    return web.json_response({
        "ok": True,
        "approved": tournament.approved,
        "pairs_per_voter": tournament.pairs_per_voter,
        "pairing": tournament.pairing,
        "open": tournament.open,
    })


async def handle_clear(request: web.Request) -> web.Response:
    """Deletes this arena outright -- works, ballots, photos. Administrators only.
    v1's poll is untouched by this, as by everything else here."""
    body = await _body(request)
    user = await _authenticate(request, body)
    if not await request.app[_IS_ADMIN_KEY](user):
        return _json_error("только администраторы могут очищать арену", status=403, code="FORBIDDEN")

    entry_name = request.app[_ENTRY_KEY]
    tournament = arena.latest_tournament(entry_name)
    if tournament is None:
        return _json_error("арена ещё не создана -- нечего очищать", status=404, code="NO_TOURNAMENT")

    arena.delete_tournament(entry_name, tournament.tournament_id)
    arena.invalidate_standings(tournament.tournament_id)
    request.app[_LOG_KEY](f"[arena] {voting.display_name(user)} cleared {tournament.tournament_id}")
    return web.json_response({"ok": True})


async def handle_media(request: web.Request) -> web.Response:
    """One photo out of the arena's OWN media directory. Same two-step guard as v1's: a
    strict name pattern and a containment check on the resolved path."""
    tournament_id = request.match_info["tournament_id"]
    name = request.match_info["name"]
    if not _SAFE_NAME.match(tournament_id or "") or not _SAFE_NAME.match(name or ""):
        raise web.HTTPNotFound()

    directory = arena.media_path(request.app[_ENTRY_KEY], tournament_id)
    path = (directory / name).resolve()
    if not str(path).startswith(str(directory.resolve())) or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


async def handle_page(request: web.Request) -> web.Response:
    return web.Response(
        text=PAGE_HTML.replace("__PREFIX__", request.app[_PREFIX_KEY]),
        content_type="text/html",
    )


def attach(app: web.Application, cfg, entry: str, is_admin, is_member=None, log=print,
           route_prefix: str = ROUTE_PREFIX) -> web.Application:
    """Adds the arena to an existing aiohttp application -- the one vote_web.create_app
    builds, so both systems answer on one port without either owning the other.

    Its own AppKeys throughout (arena_*), so nothing it stores can collide with v1's, and
    its own copies of `is_admin`/`is_member`: the two systems ask the same questions of
    Telegram but must be able to answer them differently later without touching each other.
    """
    async def _default_is_member(user):
        return True

    prefix = route_prefix.rstrip("/")
    app[_CFG_KEY] = cfg
    app[_ENTRY_KEY] = entry
    app[_IS_ADMIN_KEY] = is_admin
    app[_IS_MEMBER_KEY] = is_member or _default_is_member
    app[_PREFIX_KEY] = prefix
    app[_LOG_KEY] = log

    app.add_routes([
        web.get(prefix, handle_page),
        web.get(f"{prefix}/", handle_page),
        web.get(f"{prefix}/api/state", handle_state),
        web.post(f"{prefix}/api/session", handle_session),
        web.post(f"{prefix}/api/pick", handle_pick),
        web.post(f"{prefix}/api/undo", handle_undo),
        web.get(f"{prefix}/api/top", handle_top),
        web.get(f"{prefix}/api/standings", handle_standings),
        web.get(f"{prefix}/api/progress", handle_progress),
        web.post(f"{prefix}/api/moderate", handle_moderate),
        web.post(f"{prefix}/api/clear", handle_clear),
        web.get(prefix + "/media/{tournament_id}/{name}", handle_media),
    ])
    log(f"[arena] mounted at {prefix}")
    return app


PAGE_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Арена</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root {
    --bg: var(--tg-theme-bg-color, #17212b);
    --fg: var(--tg-theme-text-color, #f5f5f5);
    --muted: var(--tg-theme-hint-color, #8a9aa9);
    --card: var(--tg-theme-secondary-bg-color, #232e3c);
    --accent: var(--tg-theme-button-color, #3390ec);
    --accent-fg: var(--tg-theme-button-text-color, #fff);
    --thumb-bg: #1a2532;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  /* [hidden] is only a UA rule, and any author rule that sets display beats it -- which
     is how a fixed overlay ends up pinned over a page that looks empty. Settled here once
     for every element on this page. */
  [hidden] { display: none !important; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 15px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding-bottom: 110px;
  }
  header { padding: 12px 12px 4px; }
  h1 { font-size: 17px; margin: 0 0 2px; }
  .sub { color: var(--muted); font-size: 13px; }
  .msg { padding: 28px 16px; color: var(--muted); text-align: center; }

  /* The vote itself: two works, one tap. Stacked rather than side by side -- a phone is
     tall, and half a phone's width per picture is not enough to judge anything by. */
  .duel { padding: 8px 12px 0; }
  .card { background: var(--card); border-radius: 12px; overflow: hidden; margin-bottom: 10px;
          cursor: pointer; border: 2px solid transparent; }
  .card.chosen { border-color: var(--accent); }
  .card .shot { position: relative; width: 100%; aspect-ratio: 4 / 3; background: var(--thumb-bg); }
  .card .who { padding: 7px 10px 9px; font-size: 12px; color: var(--muted);
               overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  /* A work is often several photos, and judging it by the first one alone is judging half
     of it. The strip is a real scroller with snap points rather than a JS carousel: the
     browser's own inertia is the one that feels right, and it keeps working if the script
     that draws the dots ever throws. */
  .strip { position: absolute; inset: 0; display: flex; overflow-x: auto; overflow-y: hidden;
           scroll-snap-type: x mandatory; scrollbar-width: none; }
  .strip::-webkit-scrollbar { display: none; }
  .frame { flex: 0 0 100%; scroll-snap-align: center; height: 100%; }
  /* contain, not cover: this is a judgement of the work, so cropping it to fit would be
     judging something the artist did not make. */
  .frame img { width: 100%; height: 100%; object-fit: contain; display: block;
               pointer-events: none; }

  /* Three ways of saying the same thing, because "there is more to see" is the one thing a
     reader must not miss: a count, dots, and arrows that are also the desktop way through.
     All of them sit above the strip and swallow their own clicks, so none votes. */
  .shot .count, .shot .dots, .shot .arrow, .shot .zoom {
    position: absolute; z-index: 2; color: #fff; background: rgba(0,0,0,.45);
    border: 0; border-radius: 999px; backdrop-filter: blur(2px); cursor: pointer;
  }
  .shot .count { top: 8px; left: 8px; padding: 3px 8px; font-size: 11px;
                 font-variant-numeric: tabular-nums; cursor: default; }
  .shot .zoom { top: 6px; right: 6px; width: 30px; height: 30px; font-size: 15px; line-height: 1; }
  .shot .arrow { top: 50%; transform: translateY(-50%); width: 30px; height: 44px;
                 border-radius: 8px; font-size: 20px; line-height: 1; }
  .shot .arrow.prev { left: 6px; }
  .shot .arrow.next { right: 6px; }
  .shot .dots { left: 50%; transform: translateX(-50%); bottom: 8px; padding: 4px 7px;
                display: flex; gap: 5px; background: rgba(0,0,0,.4); cursor: default; }
  .shot .dots i { width: 6px; height: 6px; border-radius: 50%; background: rgba(255,255,255,.4); }
  .shot .dots i.on { background: #fff; }

  .versus { text-align: center; color: var(--muted); font-size: 12px; margin: 2px 0 8px;
            letter-spacing: .08em; }
  .progress { height: 4px; background: rgba(128,128,128,.25); border-radius: 2px;
              margin: 8px 12px 0; overflow: hidden; }
  .progress > span { display: block; height: 100%; background: var(--accent); border-radius: 2px;
                     transition: width .2s ease; }

  .bar { position: fixed; left: 0; right: 0; bottom: 0; padding: 10px 12px;
         padding-bottom: calc(10px + env(safe-area-inset-bottom));
         background: var(--bg); border-top: 1px solid rgba(128,128,128,.25); z-index: 20; }
  .go { width: 100%; border: 0; border-radius: 10px; padding: 13px; font-size: 15px;
        font-weight: 600; background: var(--accent); color: var(--accent-fg); cursor: pointer; }
  .go[disabled] { opacity: .5; cursor: default; }
  .go.secondary { background: transparent; color: var(--accent); border: 1px solid var(--accent);
                  margin-bottom: 8px; }
  .go.danger { background: transparent; color: #e5534b; border: 1px solid #e5534b;
               margin-bottom: 8px; font-size: 13px; padding: 10px; font-weight: 500; }
  .status { font-size: 12px; color: var(--muted); text-align: center; min-height: 16px;
            margin-bottom: 8px; }
  .notice { margin: 8px 12px 0; padding: 8px 10px; border-radius: 8px; font-size: 12px;
            color: var(--muted); background: var(--card); text-align: center; }

  /* Moderation: the same three-column board v1 uses, so the two systems feel like one
     tool with two modes rather than two tools. */
  .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; padding: 12px; }
  .gcard { background: var(--card); border-radius: 10px; overflow: hidden; position: relative; }
  .gcard.pending { opacity: .5; }
  .gcard .thumb { width: 100%; aspect-ratio: 1; background: var(--thumb-bg); }
  .gcard .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .gcard .who { padding: 5px 6px 2px; font-size: 11px; overflow: hidden;
                text-overflow: ellipsis; white-space: nowrap; }
  .pick { display: block; width: 100%; border: 0; padding: 7px 4px; font-size: 12px;
          background: transparent; color: var(--muted); cursor: pointer;
          border-top: 1px solid rgba(128,128,128,.25); }
  .gcard.on .pick { background: var(--accent); color: var(--accent-fg); font-weight: 600; }

  .panel { margin: 8px 12px; padding: 10px 12px; border-radius: 10px; background: var(--card);
           font-size: 13px; }
  .panel h2 { margin: 0 0 6px; font-size: 14px; }
  .row { display: flex; align-items: center; justify-content: space-between; gap: 8px;
         padding: 4px 0; }
  .row input[type="number"] { width: 60px; text-align: center; border-radius: 6px;
         border: 1px solid rgba(128,128,128,.4); background: var(--bg); color: var(--fg);
         padding: 4px; font-size: 13px; }
  .row select { border-radius: 6px; border: 1px solid rgba(128,128,128,.4);
         background: var(--bg); color: var(--fg); padding: 4px; font-size: 13px; }
  .row input[type="checkbox"] { width: 18px; height: 18px; }

  /* The table. Rating and its error bar together: a rating alone invites reading a
     12-point gap as a result when it is noise. */
  .table { display: grid; align-items: center; column-gap: 8px; row-gap: 6px;
           grid-template-columns: auto auto minmax(0, 1fr) auto; }
  .table .rank { color: var(--muted); font-size: 12px; text-align: right;
                 font-variant-numeric: tabular-nums; }
  .table .mini { width: 26px; height: 26px; border-radius: 4px; object-fit: cover;
                 background: rgba(128,128,128,.2); display: block; }
  .table .name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .table .num { text-align: right; font-variant-numeric: tabular-nums; }
  .table .num small { color: var(--muted); font-weight: 400; }

  /* The zoom. Its own layer over everything, with the gesture handled by hand rather than
     by the browser: native pinch and native scroll-snap want opposite touch-action values,
     and the one thing that must never happen here is a pinch that ends up dismissing the
     Mini App. */
  .lb { position: fixed; inset: 0; z-index: 60; background: #000; display: flex; }
  .lb .lbstrip { display: flex; width: 100%; height: 100%; touch-action: none;
                 will-change: transform; }
  .lb .lbframe { flex: 0 0 100%; height: 100%; display: flex; align-items: center;
                 justify-content: center; overflow: hidden; }
  .lb .lbframe img { max-width: 100%; max-height: 100%; object-fit: contain;
                     transform-origin: center center; pointer-events: none;
                     -webkit-user-select: none; user-select: none; }
  .lb .close { position: absolute; z-index: 2; top: calc(10px + env(safe-area-inset-top));
               right: 12px; width: 36px; height: 36px; border: 0; border-radius: 50%;
               background: rgba(255,255,255,.15); color: #fff; font-size: 18px; cursor: pointer; }
  .lb .lbcount { position: absolute; z-index: 2; top: calc(16px + env(safe-area-inset-top));
                 left: 14px; color: rgba(255,255,255,.85); font-size: 12px;
                 font-variant-numeric: tabular-nums; }
  .lb .hint { position: absolute; z-index: 2; left: 0; right: 0;
              bottom: calc(14px + env(safe-area-inset-bottom)); text-align: center;
              color: rgba(255,255,255,.6); font-size: 11px; }
</style>
</head>
<body>
<header>
  <h1>Арена: дуэли работ</h1>
  <div class="sub" id="sub">Загружаю…</div>
</header>
<div class="progress" id="progress" hidden><span id="progressFill" style="width:0%"></span></div>
<div class="notice" id="notice" hidden>Вы уже проголосовали.</div>

<div class="duel" id="duel" hidden>
  <div class="card" id="left" data-side="left">
    <div class="shot">
      <div class="strip" id="leftStrip"></div>
      <div class="count" id="leftCount" hidden></div>
      <button class="zoom" data-zoom="left" aria-label="Увеличить">⛶</button>
      <button class="arrow prev" data-nav="left:-1" aria-label="Предыдущее фото" hidden>‹</button>
      <button class="arrow next" data-nav="left:1" aria-label="Следующее фото" hidden>›</button>
      <div class="dots" id="leftDots" hidden></div>
    </div>
    <div class="who" id="leftWho"></div>
  </div>
  <div class="versus">ПРОТИВ</div>
  <div class="card" id="right" data-side="right">
    <div class="shot">
      <div class="strip" id="rightStrip"></div>
      <div class="count" id="rightCount" hidden></div>
      <button class="zoom" data-zoom="right" aria-label="Увеличить">⛶</button>
      <button class="arrow prev" data-nav="right:-1" aria-label="Предыдущее фото" hidden>‹</button>
      <button class="arrow next" data-nav="right:1" aria-label="Следующее фото" hidden>›</button>
      <div class="dots" id="rightDots" hidden></div>
    </div>
    <div class="who" id="rightWho"></div>
  </div>
</div>

<div class="msg" id="msg" hidden></div>
<div class="panel" id="top" hidden></div>

<div class="panel" id="settings" hidden>
  <h2>Настройки арены</h2>
  <div class="row"><span>Пар на одного голосующего</span>
    <input type="number" id="pairsPerVoter" min="1" max="50"></div>
  <div class="row"><span>Подбор пар</span>
    <select id="pairing">
      <option value="random">случайный</option>
      <option value="adaptive">адаптивный</option>
    </select></div>
  <div class="row"><span>Арена открыта</span><input type="checkbox" id="open"></div>
</div>
<div class="panel" id="progressPanel" hidden></div>
<div class="grid" id="grid"></div>
<div class="panel" id="standings" hidden></div>

<div class="bar" id="bar">
  <div class="status" id="status"></div>
  <button class="go danger" id="clear" hidden>🗑 Очистить арену</button>
  <button class="go secondary" id="back" hidden disabled>← Назад</button>
  <button class="go" id="go" disabled>Загружаю…</button>
</div>

<div class="lb" id="lb" hidden>
  <div class="lbstrip" id="lbStrip"></div>
  <div class="lbcount" id="lbCount"></div>
  <button class="close" id="lbClose" aria-label="Закрыть">✕</button>
  <div class="hint">Двумя пальцами — приблизить · двойное касание — сброс</div>
</div>

<script>
const PREFIX = "__PREFIX__";
const tg = window.Telegram && window.Telegram.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const initData = (tg && tg.initData) || "";

let state = null;
let ballot = null;
let admitted = new Set();
let sending = false;   // one pick in flight at a time

const $ = (id) => document.getElementById(id);

function api(path, options = {}) {
  const headers = Object.assign({ "X-Telegram-Init-Data": initData }, options.headers || {});
  if (options.body) headers["Content-Type"] = "application/json";
  return fetch(PREFIX + path, Object.assign({}, options, { headers }));
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function who(work) { return work.username ? "@" + work.username : work.author; }
function status(text) { $("status").textContent = text || ""; }

/* ------------------------------- photos ------------------------------- */
// A work is usually several pictures. The strip below is a plain scroller with snap points;
// everything here only reads where it came to rest and says so out loud.

const shown = { left: 0, right: 0 };   // which photo of that side is on screen
const shots = { left: [], right: [] }; // the photo urls, kept for the zoom view

function renderShot(side, work) {
  const photos = work.photos || [];
  shots[side] = photos;
  shown[side] = 0;

  const strip = $(side + "Strip");
  strip.scrollLeft = 0;
  strip.innerHTML = photos.length
    ? photos.map((url) => '<div class="frame"><img loading="lazy" src="' + esc(url) + '" alt=""></div>').join("")
    : '<div class="frame"></div>';

  const many = photos.length > 1;
  $(side + "Count").hidden = !many;
  $(side + "Dots").hidden = !many;
  for (const arrow of document.querySelectorAll('[data-nav^="' + side + ':"]')) arrow.hidden = !many;
  if (many) {
    $(side + "Dots").innerHTML = photos.map(() => "<i></i>").join("");
  }
  markShot(side);
}

function markShot(side) {
  const total = shots[side].length;
  if (total < 2) return;
  $(side + "Count").textContent = (shown[side] + 1) + " / " + total;
  const dots = $(side + "Dots").children;
  for (let i = 0; i < dots.length; i++) dots[i].classList.toggle("on", i === shown[side]);
}

for (const side of ["left", "right"]) {
  // Read from the scroller rather than tracked alongside it: the finger, the arrows and a
  // trackpad all move the same scrollLeft, and only one of them goes through our code.
  $(side + "Strip").addEventListener("scroll", () => {
    const strip = $(side + "Strip");
    const width = strip.clientWidth || 1;
    const at = Math.max(0, Math.min(shots[side].length - 1, Math.round(strip.scrollLeft / width)));
    if (at !== shown[side]) { shown[side] = at; markShot(side); }
  }, { passive: true });
}

function scrollShot(side, delta) {
  const strip = $(side + "Strip");
  const total = shots[side].length;
  const at = Math.max(0, Math.min(total - 1, shown[side] + delta));
  strip.scrollTo({ left: at * strip.clientWidth, behavior: "smooth" });
}

/* ------------------------------- voting ------------------------------- */

function showPair() {
  const pair = ballot && ballot.pair;
  if (!pair) return showFinished(false);
  $("duel").hidden = false;
  $("msg").hidden = true;
  $("top").hidden = true;
  $("notice").hidden = true;
  $("back").hidden = false;
  $("go").hidden = true;
  $("progress").hidden = false;
  $("progressFill").style.width = Math.round(100 * ballot.position / Math.max(1, ballot.total)) + "%";
  $("sub").textContent = "Пара " + (ballot.position + 1) + " из " + ballot.total + " · выбери, что нравится больше";
  // Nothing to go back to on the first pair, and the button says so by being there and
  // dead rather than by appearing and disappearing under the thumb.
  $("back").disabled = ballot.position <= 0;

  for (const side of ["left", "right"]) {
    const work = pair[side];
    renderShot(side, work);
    $(side + "Who").textContent = who(work);
    $(side).classList.remove("chosen");
    $(side).dataset.entry = work.id;
  }
}

// `already` separates the two ways of arriving here: having just finished, and opening the
// arena again afterwards. The second is the one that needs telling.
function showFinished(already) {
  $("duel").hidden = true;
  $("back").hidden = true;
  $("go").hidden = true;
  $("progress").hidden = false;
  $("progressFill").style.width = "100%";
  $("sub").textContent = "Готово";
  $("notice").hidden = !already;
  $("msg").hidden = false;
  $("msg").textContent = already
    ? "Твой бюллетень закрыт. Итоги объявит организатор."
    : "Спасибо, все пары пройдены. Результат считается по всем голосам сразу -- итоги объявит организатор.";
  loadTop();
}

// The table is only ever asked for once a ballot is closed -- see arena_web.handle_top. A
// failure here is silent: the top is a courtesy, and an error under "спасибо" would read
// as the vote having gone wrong.
async function loadTop() {
  try {
    const response = await api("/api/top");
    if (!response.ok) return;
    const rows = (await response.json()).top || [];
    if (!rows.length) return;
    $("top").hidden = false;
    $("top").innerHTML =
      "<h2>Сейчас в лидерах</h2>" +
      '<div class="table">' + rows.map((row, index) =>
        '<span class="rank">' + (index + 1) + "</span>" +
        (row.photo ? '<img class="mini" loading="lazy" src="' + esc(row.photo) + '" alt="">'
                   : '<span class="mini"></span>') +
        '<span class="name">' + esc(who(row)) + "</span>" +
        '<span class="num">' + row.rating + " <small>очк.</small></span>"
      ).join("") + "</div>" +
      '<div class="sub">Счёт ещё уточняется, пока голосуют остальные.</div>';
  } catch (e) { /* the vote is in; the table can wait */ }
}

// Optimistic: the next pair is drawn the moment you tap, and the server's answer only
// corrects it if the two disagree about which pair we were on. A duel that waited for a
// round trip per tap would feel broken on a phone.
async function sendPick(pick) {
  if (sending || !ballot || !ballot.pair) return;
  sending = true;
  const at = ballot.position;
  if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
  try {
    const response = await api("/api/pick", {
      method: "POST",
      body: JSON.stringify({ init_data: initData, position: at, pick: pick }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || "не получилось");
    ballot = data.ballot;
    showPair();
    status("");
  } catch (e) {
    status(String(e.message || e));
  } finally {
    sending = false;
  }
}

// Taking a pick back. The server refuses once the ballot is closed, so the button simply
// stops mattering at the end -- there is nothing here that has to know that rule twice.
async function sendUndo() {
  if (sending || !ballot || !ballot.pair) return;
  sending = true;
  if (tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred("light");
  try {
    const response = await api("/api/undo", {
      method: "POST", body: JSON.stringify({ init_data: initData }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || "не получилось");
    ballot = data.ballot;
    showPair();
    status("");
  } catch (e) {
    status(String(e.message || e));
  } finally {
    sending = false;
  }
}

// Choosing stays one tap on the work -- but a card is now also something you swipe through
// and pinch, and neither of those may cast a vote. So the tap is measured from pointerdown
// (the same rule vote_web.py's reel uses): the finger has to come back up near where it
// went down, soon enough to be a tap. A gesture the browser took over is remembered as
// cancelled rather than forgotten, or the click that still follows would read as a tap.
const TAP_SLOP = 10;    // px of drift still read as a tap rather than a drag
const TAP_MS = 600;     // longer is a press or a stalled scroll
let cardTap = null;

for (const side of ["left", "right"]) {
  const card = $(side);
  card.addEventListener("pointerdown", (event) => {
    cardTap = { x: event.clientX, y: event.clientY, at: Date.now() };
  });
  card.addEventListener("pointercancel", () => { cardTap = { cancelled: true }; });
  card.addEventListener("click", (event) => {
    const start = cardTap;
    cardTap = null;
    // The arrows, the zoom and the dots live inside the card and are not votes.
    if (event.target.closest("[data-nav], [data-zoom], .dots, .count")) return;
    if (start) {
      if (start.cancelled || Date.now() - start.at > TAP_MS) return;
      if (Math.abs(event.clientX - start.x) > TAP_SLOP) return;
      if (Math.abs(event.clientY - start.y) > TAP_SLOP) return;
    }
    if (!ballot || !ballot.pair) return;
    card.classList.add("chosen");
    sendPick(card.dataset.entry);
  });
}

$("back").addEventListener("click", sendUndo);

document.addEventListener("click", (event) => {
  const arrow = event.target.closest("[data-nav]");
  if (arrow) {
    const [side, delta] = arrow.dataset.nav.split(":");
    scrollShot(side, Number(delta));
    return;
  }
  const zoom = event.target.closest("[data-zoom]");
  if (zoom) openLightbox(zoom.dataset.zoom);
});

/* -------------------------------- zoom -------------------------------- */
// Handled by hand, not by the browser: native pinch-zoom and native scroll-snap want
// opposite touch-action values, and an unhandled pinch inside a Mini App can end up
// dragging the whole app down instead of magnifying anything.

const lb = { photos: [], index: 0, scale: 1, x: 0, y: 0, dragging: 0 };
const touching = new Map();   // live pointers, by id
let pinch = null;             // {gap, scale} at the moment the second finger landed
let drag = null;              // {x, y, ox, oy} since the finger went down
let lastTap = 0;

function lbImage() { return $("lbStrip").children[lb.index].firstElementChild; }

function openLightbox(side) {
  if (!shots[side].length) return;
  lb.photos = shots[side];
  lb.index = shown[side];
  lb.scale = 1; lb.x = 0; lb.y = 0; lb.dragging = 0;
  $("lbStrip").innerHTML = lb.photos
    .map((url) => '<div class="lbframe"><img src="' + esc(url) + '" alt=""></div>').join("");
  $("lb").hidden = false;
  // Telegram's own swipe-down would otherwise close the app halfway through a pan.
  if (tg && tg.disableVerticalSwipes) tg.disableVerticalSwipes();
  drawLightbox(false);
}

function closeLightbox() {
  $("lb").hidden = true;
  $("lbStrip").innerHTML = "";
  touching.clear(); pinch = null; drag = null;
  if (tg && tg.enableVerticalSwipes) tg.enableVerticalSwipes();
}

function drawLightbox(animate) {
  const strip = $("lbStrip");
  strip.style.transition = animate ? "transform .2s ease" : "none";
  strip.style.transform =
    "translateX(" + (-lb.index * strip.clientWidth + lb.dragging) + "px)";
  const image = lbImage();
  if (image) {
    image.style.transform =
      "translate(" + lb.x + "px," + lb.y + "px) scale(" + lb.scale + ")";
  }
  $("lbCount").textContent =
    lb.photos.length > 1 ? (lb.index + 1) + " / " + lb.photos.length : "";
}

// How far the picture may be pushed before its edge comes inside the screen. Measured off
// the drawn box (object-fit: contain leaves letterboxing that is not part of the picture),
// so a portrait photo cannot be dragged sideways into empty black.
function panLimits() {
  const image = lbImage();
  const frame = $("lbStrip");
  if (!image) return { x: 0, y: 0 };
  const width = image.clientWidth * lb.scale, height = image.clientHeight * lb.scale;
  return {
    x: Math.max(0, (width - frame.clientWidth) / 2),
    y: Math.max(0, (height - frame.clientHeight) / 2),
  };
}

function clampPan() {
  const limit = panLimits();
  lb.x = Math.max(-limit.x, Math.min(limit.x, lb.x));
  lb.y = Math.max(-limit.y, Math.min(limit.y, lb.y));
}

function setScale(next, animate) {
  lb.scale = Math.max(1, Math.min(6, next));
  if (lb.scale === 1) { lb.x = 0; lb.y = 0; } else clampPan();
  drawLightbox(animate);
}

function gap() {
  const [a, b] = [...touching.values()];
  return Math.hypot(a.x - b.x, a.y - b.y) || 1;
}

$("lbStrip").addEventListener("pointerdown", (event) => {
  $("lbStrip").setPointerCapture(event.pointerId);
  touching.set(event.pointerId, { x: event.clientX, y: event.clientY });
  if (touching.size === 2) {
    pinch = { gap: gap(), scale: lb.scale };
    drag = null;
  } else if (touching.size === 1) {
    drag = { x: event.clientX, y: event.clientY, ox: lb.x, oy: lb.y };
  }
});

$("lbStrip").addEventListener("pointermove", (event) => {
  if (!touching.has(event.pointerId)) return;
  touching.set(event.pointerId, { x: event.clientX, y: event.clientY });

  if (pinch && touching.size === 2) {
    setScale(pinch.scale * (gap() / pinch.gap), false);
    return;
  }
  if (!drag || touching.size !== 1) return;
  const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
  if (lb.scale > 1) {
    // Zoomed in, one finger moves the picture.
    lb.x = drag.ox + dx; lb.y = drag.oy + dy;
    clampPan();
  } else {
    // At rest, the same finger turns the page instead.
    lb.dragging = dx;
  }
  drawLightbox(false);
});

function endPointer(event) {
  touching.delete(event.pointerId);
  if (touching.size < 2) pinch = null;
  if (touching.size === 1) {
    // One finger left of a pinch keeps going as a pan, measured from where it is now --
    // otherwise letting go of one finger freezes the picture until both come off.
    const [remaining] = [...touching.values()];
    drag = { x: remaining.x, y: remaining.y, ox: lb.x, oy: lb.y };
  }
  if (touching.size > 0) return;

  if (drag && lb.scale === 1) {
    const moved = lb.dragging;
    lb.dragging = 0;
    // A quarter of the screen commits to the next picture; anything less springs back.
    if (Math.abs(moved) > $("lbStrip").clientWidth / 4) {
      const next = lb.index + (moved < 0 ? 1 : -1);
      if (next >= 0 && next < lb.photos.length) { lb.index = next; lb.x = 0; lb.y = 0; }
    }
    drawLightbox(true);
  }
  drag = null;
}

$("lbStrip").addEventListener("pointerup", (event) => {
  const start = drag;
  const still = start && Math.abs(event.clientX - start.x) < TAP_SLOP
                      && Math.abs(event.clientY - start.y) < TAP_SLOP;
  endPointer(event);
  if (!still) { lastTap = 0; return; }
  const now = Date.now();
  // Double tap: in to a fixed 2.5x on the first, all the way back out on the next.
  if (now - lastTap < 300) { setScale(lb.scale > 1 ? 1 : 2.5, true); lastTap = 0; }
  else lastTap = now;
});
$("lbStrip").addEventListener("pointercancel", endPointer);

// A trackpad and a mouse wheel are the desktop's pinch.
$("lbStrip").addEventListener("wheel", (event) => {
  event.preventDefault();
  setScale(lb.scale * (event.deltaY < 0 ? 1.12 : 1 / 1.12), false);
}, { passive: false });

$("lbClose").addEventListener("click", closeLightbox);
document.addEventListener("keydown", (event) => {
  if ($("lb").hidden) return;
  if (event.key === "Escape") closeLightbox();
  if (event.key === "ArrowRight" && lb.index < lb.photos.length - 1) {
    lb.index += 1; lb.x = 0; lb.y = 0; setScale(1, true);
  }
  if (event.key === "ArrowLeft" && lb.index > 0) {
    lb.index -= 1; lb.x = 0; lb.y = 0; setScale(1, true);
  }
});
window.addEventListener("resize", () => { if (!$("lb").hidden) drawLightbox(false); });

async function startSession() {
  const response = await api("/api/session", {
    method: "POST", body: JSON.stringify({ init_data: initData }),
  });
  const data = await response.json();
  if (!response.ok) {
    // A ballot that closed since this page loaded (a second tab, a slow tap) is not an
    // error to report -- it is the finished screen, arrived at the long way round.
    if (data.error === "ALREADY_VOTED") return showFinished(true);
    $("duel").hidden = true;
    $("back").hidden = true;
    $("go").hidden = true;
    $("msg").hidden = false;
    $("msg").textContent = data.message || "не получилось начать";
    $("sub").textContent = "";
    return;
  }
  ballot = data.ballot;
  showPair();
}

/* ----------------------------- moderation ----------------------------- */

function renderGrid() {
  const grid = $("grid");
  grid.innerHTML = "";
  for (const work of state.entries) {
    const card = document.createElement("div");
    card.className = "gcard" + (admitted.has(work.id) ? " on" : " pending");
    card.innerHTML =
      '<div class="thumb"><img loading="lazy" src="' + esc(work.photos[0] || "") + '" alt=""></div>' +
      '<div class="who">' + esc(who(work)) + "</div>" +
      '<button class="pick" data-admit="' + esc(work.id) + '">' +
        (admitted.has(work.id) ? "допущена" : "допустить") +
      "</button>";
    grid.appendChild(card);
  }
}

function renderStandings() {
  const box = $("standings");
  const rows = state.standings || [];
  box.hidden = false;
  box.innerHTML =
    "<h2>Рейтинг</h2>" +
    (rows.length
      ? '<div class="table">' + rows.map((row, index) =>
          '<span class="rank">' + (index + 1) + "</span>" +
          (row.photo ? '<img class="mini" loading="lazy" src="' + esc(row.photo) + '" alt="">'
                     : '<span class="mini"></span>') +
          '<span class="name">' + esc(who(row)) + "</span>" +
          '<span class="num">' + row.rating +
            (row.margin != null ? " <small>±" + row.margin + "</small>" : "") +
            " <small>· " + row.played + " дуэлей</small></span>"
        ).join("") + "</div>"
      : '<div class="sub">Пока не по чему считать.</div>');
}

function renderProgress() {
  const p = state.progress || {};
  $("progressPanel").hidden = false;
  $("progressPanel").innerHTML =
    "<h2>Ход арены</h2>" +
    '<div class="sub">Проголосовало: ' + (p.completed || 0) + " · в процессе: " + (p.in_progress || 0) +
    " · сравнений: " + (p.judgements || 0) + "</div>" +
    '<div class="sub">Покрытие: ' + (p.coverage || 0).toFixed(1) + " на пару" +
    ((p.coverage || 0) < 4 ? " — маловато, верх таблицы ещё не разделится" : " — достаточно") + "</div>";
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-admit]");
  if (!button) return;
  const id = button.dataset.admit;
  if (admitted.has(id)) admitted.delete(id); else admitted.add(id);
  if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
  renderGrid();
  $("go").textContent = "Сохранить: допущено " + admitted.size + " из " + state.entries.length;
});

async function saveModeration() {
  const go = $("go");
  go.disabled = true;
  const original = go.textContent;
  go.textContent = "Сохраняю…";
  try {
    const response = await api("/api/moderate", {
      method: "POST",
      body: JSON.stringify({
        init_data: initData,
        approved: [...admitted],
        pairs_per_voter: parseInt($("pairsPerVoter").value, 10) || 10,
        pairing: $("pairing").value,
        open: $("open").checked,
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || "не получилось");
    go.textContent = "Сохранено";
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    setTimeout(() => { load(); }, 1200);
  } catch (e) {
    go.textContent = original;
    go.disabled = false;
    status(String(e.message || e));
  }
}

$("go").addEventListener("click", () => { if (state && state.is_admin) saveModeration(); });

$("clear").addEventListener("click", async () => {
  if (!confirm("Точно очистить арену? Все работы и голоса удалятся безвозвратно. Голосование v1 это не тронет.")) return;
  const button = $("clear");
  button.disabled = true;
  try {
    const response = await api("/api/clear", {
      method: "POST", body: JSON.stringify({ init_data: initData }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || "не получилось");
    location.reload();
  } catch (e) {
    button.disabled = false;
    status(String(e.message || e));
  }
});

/* -------------------------------- load -------------------------------- */

async function load() {
  try {
    const mode = new URLSearchParams(location.search).get("mode");
    const response = await api("/api/state" + (mode ? "?mode=" + encodeURIComponent(mode) : ""));
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || "не получилось загрузить");
    state = data;

    if (!state.tournament_id) {
      $("sub").textContent = "";
      $("msg").hidden = false;
      $("msg").textContent = "Арена ещё не создана. Собери работы: /vote2 собрать";
      $("go").hidden = true;
      return;
    }

    if (state.is_admin) {
      admitted = new Set(state.approved || []);
      // Said outright rather than left to the markup's initial `hidden`: load() runs again
      // after every save, and a moderator who had a duel on screen would otherwise keep it
      // underneath the moderation board.
      $("duel").hidden = true;
      $("back").hidden = true;
      $("progress").hidden = true;
      $("notice").hidden = true;
      $("top").hidden = true;
      $("settings").hidden = false;
      $("pairsPerVoter").value = state.pairs_per_voter;
      $("pairing").value = state.pairing;
      $("open").checked = !!state.open;
      $("clear").hidden = false;
      $("go").hidden = false;
      $("go").disabled = false;
      $("go").textContent = "Сохранить: допущено " + admitted.size + " из " + state.entries.length;
      $("sub").textContent = "Режим модератора · работ " + state.entries.length;
      renderGrid();
      renderProgress();
      renderStandings();
      return;
    }

    if (state.is_member === false) {
      $("sub").textContent = "";
      $("msg").hidden = false;
      $("msg").textContent = "Голосовать могут только участники чата.";
      $("go").hidden = true;
      return;
    }
    $("go").hidden = true;
    if (state.can_moderate) $("sub").textContent = "модерация: /vote2 выбрать";
    // Somebody coming back to a ballot they already closed is told so, and shown the top of
    // the table -- asked for a session they would get a 409 and read it as a fault.
    if (state.ballot && state.ballot.status === "done") {
      ballot = state.ballot;
      showFinished(true);
      return;
    }
    await startSession();
  } catch (e) {
    $("sub").textContent = "";
    $("msg").hidden = false;
    $("msg").textContent = String(e.message || e);
    $("go").hidden = true;
  }
}

load();
</script>
</body>
</html>
"""
