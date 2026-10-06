"""The Hall of Fame website -- «Доска почёта» -- mounted on the voting server at /hall.

A public site, not a Mini App: it has nothing to vote with and nothing to hide from a
voter, so there is no initData and no admin mode. What it shows is what the chat already
saw announced -- the closed contests (hall_of_fame.py), who took part with which work,
the places and the votes -- arranged four ways: the hall itself (the latest winners, the
best artists, the thematic contests), the chronology of every contest, the list of
artists, and one page per artist with every work they entered and every badge they won.

One page, five screens, chosen by the URL's #fragment; each screen asks for exactly the
data it draws (/api/overview, /api/contests, /api/contests/<id>, /api/artists,
/api/artists/<key>), so the front page never carries the whole archive. Every request
reads the hall through hall_of_fame.snapshot in a worker thread, together with building
its response: this event loop also serves the ballots.

Pictures are served from the hall's own copies. Avatars are fetched through the bot (the
same callable the ballot uses) and only for somebody who is actually in the hall -- the
route is not a way to look up any Telegram user's face.
"""

import asyncio
import os
import re
from typing import Awaitable, Callable

from aiohttp import web

import hall_of_fame
import voting

ROUTE_PREFIX = "/hall"
# The community's name, in the page title and its header.
HALL_BRAND = os.getenv("HALL_BRAND", "ЕЧХ")

_ENTRY_KEY = web.AppKey("hall_entry", str)
_PREFIX_KEY = web.AppKey("hall_prefix", str)
_LOG_KEY = web.AppKey("hall_log", Callable[..., None])
_AVATAR_KEY = web.AppKey("hall_avatar", Callable[[int], Awaitable[bytes | None]])
_AVATAR_CACHE_KEY = web.AppKey("hall_avatar_cache", dict)
# Async, returns (chat @username or None, marked chat id or None): where the works were
# posted, for a "открыть пост" link. Resolved once and kept -- see _link_base.
_CHAT_KEY = web.AppKey("hall_chat", Callable[[], Awaitable[tuple]])
_CHAT_CACHE_KEY = web.AppKey("hall_chat_cache", dict)
_BOT_KEY = web.AppKey("hall_bot", str)

# How many of each the front page shows; the rest is one tap away on its own screen.
OVERVIEW_WINNERS = 12
OVERVIEW_ARTISTS = 10

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_ARTIST_KEY = re.compile(r"^(?:\d{1,20}|n[0-9a-f]{10})$")


# ----------------------------------------------------------------------------- payloads


def _photo_url(base: str, contest: hall_of_fame.Contest, name: str | None) -> str | None:
    return f"{base}/media/{contest.contest_id}/{name}" if name else None


def _author(base: str, author_id, name: str, username: str | None) -> dict:
    handle = str(username or "").lstrip("@")
    return {
        "key": hall_of_fame.author_key(author_id, name),
        "name": name,
        "username": handle or None,
        "avatar": f"{base}/avatar/{author_id}" if author_id is not None else None,
        "telegram": f"https://t.me/{handle}" if re.fullmatch(r"[A-Za-z0-9_]{3,}", handle) else None,
    }


def _post_url(link_base: str | None, message_id: int) -> str | None:
    return f"{link_base}/{message_id}" if link_base and message_id else None


def _work_brief(base: str, contest: hall_of_fame.Contest, work: hall_of_fame.Work) -> dict:
    first = work.photos[0] if work.photos else None
    return {
        "id": work.entry_id,
        "contest": contest.contest_id,
        "place": work.place,
        "votes": work.votes,
        "winner": contest.winner() is work,
        "podium": contest.on_podium(work),
        "author": _author(base, work.author_id, work.author_name, work.author_username),
        "thumb": _photo_url(base, contest, work.thumb or first),
        "photo": _photo_url(base, contest, first),
    }


def _work_full(base: str, contest: hall_of_fame.Contest, work: hall_of_fame.Work,
               link_base: str | None) -> dict:
    payload = _work_brief(base, contest, work)
    payload.update({
        "photos": [_photo_url(base, contest, name) for name in work.photos],
        "text": work.text,
        "posted_at": work.posted_at,
        "post_url": _post_url(link_base, work.message_id),
    })
    return payload


def _contest_summary(base: str, contest: hall_of_fame.Contest) -> dict:
    winner = contest.winner()
    return {
        "id": contest.contest_id,
        "title": contest.label(),
        "hashtag": contest.hashtag,
        "weekly": contest.is_weekly,
        "badge": contest.winner_badge(),
        "week": contest.week(),
        "closed_at": contest.when(),
        "voters": contest.voters,
        "works": len(contest.works),
        "podium": [_work_brief(base, contest, work) for work in contest.podium()],
        "winner": _work_brief(base, contest, winner) if winner else None,
    }


def _badges(artist: hall_of_fame.Artist) -> list[dict]:
    """One pill per kind of win: every weekly win is the same 🏆 with a count, each
    thematic contest is its own badge -- that is the achievement the contest promised."""
    grouped: dict[tuple, dict] = {}
    for contest in artist.wins:
        title = voting.WEEKLY_CONTEST_TITLE if contest.is_weekly else contest.label()
        key = (contest.winner_badge(), title)
        badge = grouped.setdefault(key, {
            "badge": key[0], "title": title, "weekly": contest.is_weekly, "count": 0, "contests": [],
        })
        badge["count"] += 1
        badge["contests"].append(contest.contest_id)
    # Thematic badges first: each is a one-off, the weekly cup is a tally.
    return sorted(grouped.values(), key=lambda b: (b["weekly"], -b["count"], b["title"]))


def _artist_summary(base: str, artist: hall_of_fame.Artist, rank: int) -> dict:
    newest_contest, newest_work = artist.entries[0]
    best = next(((c, w) for c, w in artist.entries if c.winner() is w), None) or (newest_contest, newest_work)
    payload = _author(base, artist.author_id, artist.name, artist.username)
    payload.update({
        "rank": rank,
        "works": len(artist.entries),
        "wins": len(artist.wins),
        "podiums": artist.podiums,
        "votes": artist.total_votes,
        "best_place": artist.best_place(),
        "badges": _badges(artist),
        "cover": _photo_url(base, best[0], best[1].thumb or (best[1].photos[0] if best[1].photos else None)),
    })
    return payload


def overview_payload(hall: hall_of_fame.Hall, base: str, bot: str = "") -> dict:
    winners = [c for c in hall.contests if c.winner() is not None]
    return {
        "brand": HALL_BRAND,
        "bot": bot or None,
        "stats": {
            "contests": len(hall.contests),
            "works": sum(len(c.works) for c in hall.contests),
            "artists": len(hall.artists),
            "themes": sum(1 for c in hall.contests if not c.is_weekly),
        },
        "latest": [_contest_summary(base, c) for c in winners[:OVERVIEW_WINNERS]],
        "artists": [_artist_summary(base, a, rank) for rank, a in enumerate(hall.artists[:OVERVIEW_ARTISTS], start=1)],
        "themes": [_contest_summary(base, c) for c in hall.contests if not c.is_weekly],
    }


def contests_payload(hall: hall_of_fame.Hall, base: str) -> dict:
    return {"contests": [_contest_summary(base, c) for c in hall.contests]}


def contest_payload(hall: hall_of_fame.Hall, base: str, contest_id: str, link_base: str | None) -> dict | None:
    contest = hall.by_id.get(contest_id)
    if contest is None:
        return None
    payload = _contest_summary(base, contest)
    payload["entries"] = [_work_full(base, contest, work, link_base) for work in contest.works]
    return {"contest": payload}


def artists_payload(hall: hall_of_fame.Hall, base: str) -> dict:
    return {"artists": [_artist_summary(base, a, rank) for rank, a in enumerate(hall.artists, start=1)]}


def artist_payload(hall: hall_of_fame.Hall, base: str, key: str, link_base: str | None) -> dict | None:
    artist = hall.by_key.get(key)
    if artist is None:
        return None
    rank = hall.artists.index(artist) + 1
    payload = _artist_summary(base, artist, rank)
    payload["entries"] = [
        dict(_work_full(base, contest, work, link_base),
             contest_title=contest.label(), contest_week=contest.week(),
             contest_weekly=contest.is_weekly, contest_badge=contest.winner_badge(),
             contest_works=len(contest.works))
        for contest, work in artist.entries
    ]
    return {"artist": payload}


# ----------------------------------------------------------------------------- handlers


async def _link_base(request: web.Request) -> str | None:
    """'https://t.me/<chat>' or 'https://t.me/c/<id>' for links to the original posts.

    Asked of the bot once and kept: the chat does not move, and resolving it is a Telegram
    round trip. A failure is not kept, so a later request can still find it. A basic group
    (no -100 id) and a private chat have no link form, and the page simply shows none."""
    cache = request.app[_CHAT_CACHE_KEY]
    if "base" not in cache:
        try:
            username, chat_id = await request.app[_CHAT_KEY]()
        except Exception as e:
            request.app[_LOG_KEY](f"[hall] could not resolve the chat for post links: {e}")
            return None
        if username:
            cache["base"] = f"https://t.me/{str(username).lstrip('@')}"
        elif chat_id is not None and str(chat_id).startswith("-100"):
            cache["base"] = f"https://t.me/c/{str(chat_id)[4:]}"
        else:
            cache["base"] = None
    return cache["base"]


async def _serve(request: web.Request, build, needs_link: bool = False) -> web.Response:
    """Runs `build(hall, base, link_base)` -- the snapshot read and the payload together --
    in a worker thread, and answers 404 when it returns None."""
    entry = request.app[_ENTRY_KEY]
    base = request.app[_PREFIX_KEY]
    link_base = await _link_base(request) if needs_link else None

    def work():
        return build(hall_of_fame.snapshot(entry), base, link_base)

    payload = await asyncio.to_thread(work)
    if payload is None:
        return web.json_response({"error": "не найдено"}, status=404)
    # A minute is invisible to a reader -- contests close once a week -- and lets a
    # browser (or Telegram's proxy) answer a back-and-forth without asking again.
    return web.json_response(payload, headers={"Cache-Control": "public, max-age=60"})


async def handle_overview(request: web.Request) -> web.Response:
    bot = request.app[_BOT_KEY]
    return await _serve(request, lambda hall, base, _: overview_payload(hall, base, bot))


async def handle_contests(request: web.Request) -> web.Response:
    return await _serve(request, lambda hall, base, _: contests_payload(hall, base))


async def handle_contest(request: web.Request) -> web.Response:
    contest_id = request.match_info["contest_id"]
    if not _SAFE_NAME.match(contest_id or ""):
        raise web.HTTPNotFound()
    return await _serve(
        request, lambda hall, base, link: contest_payload(hall, base, contest_id, link), needs_link=True,
    )


async def handle_artists(request: web.Request) -> web.Response:
    return await _serve(request, lambda hall, base, _: artists_payload(hall, base))


async def handle_artist(request: web.Request) -> web.Response:
    key = request.match_info["key"]
    if not _ARTIST_KEY.match(key or ""):
        raise web.HTTPNotFound()
    return await _serve(
        request, lambda hall, base, link: artist_payload(hall, base, key, link), needs_link=True,
    )


async def handle_media(request: web.Request) -> web.Response:
    path = hall_of_fame.media_file(
        request.app[_ENTRY_KEY], request.match_info["contest_id"], request.match_info["name"],
    )
    if path is None:
        raise web.HTTPNotFound()
    # A week: a recorded photo never changes under its name (a re-record copies only what
    # is missing), and the thumbnails are rewritten under the same name only on a re-close.
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=604800"})


async def handle_avatar(request: web.Request) -> web.Response:
    """An artist's current Telegram avatar -- only for somebody who is in the hall. Found
    photos and "has no photo" are cached for the process; a failed fetch is not, so the
    next page view can retry it."""
    raw = request.match_info["user_id"]
    if not raw.isdigit():
        raise web.HTTPNotFound()
    user_id = int(raw)
    cache = request.app[_AVATAR_CACHE_KEY]
    if user_id not in cache:
        entry = request.app[_ENTRY_KEY]
        known = await asyncio.to_thread(lambda: str(user_id) in hall_of_fame.snapshot(entry).by_key)
        if not known:
            raise web.HTTPNotFound()
        try:
            avatar = await request.app[_AVATAR_KEY](user_id)
        except Exception as e:
            request.app[_LOG_KEY](f"[hall] could not fetch avatar for {user_id}: {e}")
            raise web.HTTPServiceUnavailable()
        cache[user_id] = bytes(avatar) if avatar else None
    if not cache[user_id]:
        raise web.HTTPNotFound()
    return web.Response(body=cache[user_id], content_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


async def handle_page(request: web.Request) -> web.Response:
    return web.Response(
        text=PAGE_HTML.replace("__PREFIX__", request.app[_PREFIX_KEY]).replace("__BRAND__", HALL_BRAND),
        content_type="text/html",
    )


def attach(app: web.Application, entry: str, log=print, route_prefix: str = ROUTE_PREFIX,
           avatar=None, chat=None, bot_username: str | None = None) -> web.Application:
    """Adds the site to the application vote_web.create_app builds, under its own AppKeys
    (hall_*). `avatar` takes an author id and returns photo bytes or None; `chat` is an
    async callable returning (chat @username, marked chat id) for links to the original
    posts. Either may be left out: the page then shows initials and no post links."""
    async def _no_avatar(user_id):
        return None

    async def _no_chat():
        return None, None

    prefix = route_prefix.rstrip("/")
    app[_ENTRY_KEY] = entry
    app[_PREFIX_KEY] = prefix
    app[_LOG_KEY] = log
    app[_AVATAR_KEY] = avatar or _no_avatar
    app[_AVATAR_CACHE_KEY] = {}
    app[_CHAT_KEY] = chat or _no_chat
    app[_CHAT_CACHE_KEY] = {}
    app[_BOT_KEY] = (bot_username or "").lstrip("@")
    app.add_routes([
        web.get(prefix, handle_page),
        web.get(f"{prefix}/", handle_page),
        web.get(f"{prefix}/api/overview", handle_overview),
        web.get(f"{prefix}/api/contests", handle_contests),
        web.get(prefix + "/api/contests/{contest_id}", handle_contest),
        web.get(f"{prefix}/api/artists", handle_artists),
        web.get(prefix + "/api/artists/{key}", handle_artist),
        web.get(prefix + "/media/{contest_id}/{name}", handle_media),
        web.get(prefix + "/avatar/{user_id}", handle_avatar),
    ])
    log(f"[hall] mounted at {prefix}")
    return app


PAGE_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
<title>Доска почёта · __BRAND__</title>
<script src="https://telegram.org/js/telegram-web-app.js" async></script>
<style>
  /* v1's navy, pinned dark for v1's reason -- miniatures read against it -- plus the
     three medal colours, which are what a hall of fame is made of. */
  :root {
    color-scheme: dark;
    --bg: #121a23;
    --surface: #17212b;
    --card: #1e2a37;
    --card-hi: #243242;
    --line: rgba(255,255,255,.08);
    --fg: #f3f5f7;
    --muted: #8d9cab;
    --accent: #3390ec;
    --gold: #f2c14e;
    --gold-soft: rgba(242,193,78,.14);
    --silver: #c3ccd6;
    --bronze: #d6905f;
    --radius: 16px;
    --shadow: 0 10px 30px rgba(0,0,0,.35);
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  [hidden] { display: none !important; }
  html, body { overflow-x: hidden; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
  }
  a { color: inherit; text-decoration: none; }
  button { font: inherit; color: inherit; cursor: pointer; }
  img { display: block; }
  .wrap { max-width: 1120px; margin: 0 auto; padding: 0 16px; }

  /* ------------------------------------------------------------------ top bar */
  .top { position: sticky; top: 0; z-index: 20; background: rgba(18,26,35,.94);
         border-bottom: 1px solid var(--line); backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px); }
  .topInner { display: flex; align-items: center; gap: 14px; height: 56px; }
  .brand { display: flex; align-items: center; gap: 9px; font-weight: 800; letter-spacing: .01em; white-space: nowrap; }
  .brand .cup { font-size: 22px; }
  .brand small { color: var(--muted); font-weight: 600; }
  .nav { display: flex; gap: 4px; overflow-x: auto; scrollbar-width: none; margin-left: auto; }
  .nav::-webkit-scrollbar { display: none; }
  .nav a { padding: 7px 12px; border-radius: 999px; color: var(--muted); font-weight: 600; font-size: 14px; white-space: nowrap; }
  .nav a.on { background: var(--card-hi); color: var(--fg); }
  @media (max-width: 640px) {
    .topInner { height: auto; flex-wrap: wrap; padding: 10px 0 8px; gap: 6px; }
    .nav { margin-left: -6px; width: calc(100% + 12px); }
  }

  /* main.wrap, not main: .wrap's own padding would otherwise win and drop these. */
  main.wrap { padding-top: 26px; padding-bottom: 64px; min-height: 70vh; }
  h1 { font-size: clamp(26px, 5vw, 40px); line-height: 1.12; margin: 0 0 8px; letter-spacing: -.01em; }
  h2 { font-size: 21px; margin: 0; letter-spacing: -.005em; }
  .lead { color: var(--muted); max-width: 640px; margin: 0; }
  .section { margin-top: 38px; }
  .sectionHead { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 14px; }
  .more { color: var(--accent); font-weight: 600; font-size: 14px; white-space: nowrap; }
  .muted { color: var(--muted); }
  .chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }
  .chip { display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px; border-radius: 999px;
          background: var(--card); border: 1px solid var(--line); font-size: 13px; font-weight: 600; }
  .chip b { font-size: 15px; }
  button.chip { border-color: var(--line); }
  button.chip.on { background: var(--fg); color: var(--bg); }
  .tag { display: inline-block; padding: 1px 8px; border-radius: 999px; background: var(--card-hi); color: var(--muted);
         font-size: 12px; font-weight: 600; }

  /* ------------------------------------------------------------------ avatars */
  .ava { position: relative; flex: none; border-radius: 50%; overflow: hidden; display: inline-flex;
         align-items: center; justify-content: center; font-weight: 700; color: #fff; }
  .ava img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
  .ava.s { width: 28px; height: 28px; font-size: 12px; }
  .ava.m { width: 44px; height: 44px; font-size: 17px; }
  .ava.l { width: 96px; height: 96px; font-size: 36px; box-shadow: 0 0 0 4px var(--bg), 0 0 0 6px var(--gold); }

  /* ------------------------------------------------------------------ hero winner */
  .hero { display: grid; grid-template-columns: 1.25fr 1fr; gap: 18px; margin-top: 22px; }
  @media (max-width: 760px) { .hero { grid-template-columns: 1fr; } }
  .heroPic { position: relative; border-radius: var(--radius); overflow: hidden; background: var(--card);
             aspect-ratio: 4 / 3; box-shadow: var(--shadow); cursor: zoom-in; }
  .heroPic img { width: 100%; height: 100%; object-fit: cover; }
  .heroPic .ribbon { position: absolute; left: 14px; top: 14px; background: var(--gold); color: #2a1e00;
                     padding: 5px 12px; border-radius: 999px; font-weight: 800; font-size: 13px; }
  .heroInfo { background: linear-gradient(160deg, rgba(242,193,78,.16), rgba(242,193,78,0) 55%), var(--card);
              border-radius: var(--radius); padding: 22px; display: flex; flex-direction: column; gap: 14px;
              border: 1px solid rgba(242,193,78,.22); }
  .heroWho { display: flex; align-items: center; gap: 14px; }
  .heroWho .name { font-size: 22px; font-weight: 800; line-height: 1.2; }
  .heroMeta { color: var(--muted); font-size: 14px; }
  .podiumMini { display: grid; gap: 8px; margin-top: auto; }
  .pmRow { display: flex; align-items: center; gap: 10px; padding: 8px; border-radius: 12px; background: rgba(255,255,255,.04); }
  .pmRow img.th { width: 44px; height: 44px; border-radius: 9px; object-fit: cover; background: var(--card-hi); }
  .pmRow .who { flex: 1; min-width: 0; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pmRow .v { color: var(--muted); font-size: 13px; white-space: nowrap; }

  /* ------------------------------------------------------------------ cards */
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(168px, 1fr)); gap: 14px; }
  @media (max-width: 420px) { .grid { grid-template-columns: repeat(2, 1fr); gap: 10px; } }
  .card { position: relative; background: var(--card); border-radius: var(--radius); overflow: hidden;
          border: 1px solid var(--line); transition: transform .15s, border-color .15s; display: flex; flex-direction: column; }
  .card:hover { transform: translateY(-2px); border-color: rgba(255,255,255,.18); }
  .card .pic { position: relative; aspect-ratio: 1; background: var(--card-hi); cursor: zoom-in; }
  .card .pic img { width: 100%; height: 100%; object-fit: cover; }
  .card .noPic { width: 100%; height: 100%; display: flex; align-items: center; justify-content: center;
                 color: var(--muted); font-size: 12px; text-align: center; padding: 10px; }
  .place { position: absolute; left: 8px; top: 8px; min-width: 30px; height: 30px; padding: 0 8px; border-radius: 999px;
           display: inline-flex; align-items: center; justify-content: center; font-weight: 800; font-size: 14px;
           background: rgba(0,0,0,.62); color: #fff; }
  .place.p1 { background: var(--gold); color: #2a1e00; }
  .place.p2 { background: var(--silver); color: #1d242c; }
  .place.p3 { background: var(--bronze); color: #2b1608; }
  .votes { position: absolute; right: 8px; top: 8px; padding: 3px 9px; border-radius: 999px; background: rgba(0,0,0,.62);
           font-size: 12px; font-weight: 700; }
  .card .foot { display: flex; align-items: center; gap: 8px; padding: 10px; min-width: 0; }
  .card .foot .txt { min-width: 0; flex: 1; }
  .card .foot .n { font-weight: 700; font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .card .foot .sub { color: var(--muted); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .card.win { border-color: rgba(242,193,78,.45); }

  /* ------------------------------------------------------------------ leaderboard */
  .board { display: grid; gap: 8px; }
  .row { display: flex; align-items: center; gap: 12px; padding: 10px 12px; background: var(--card);
         border-radius: 14px; border: 1px solid var(--line); }
  .row:hover { border-color: rgba(255,255,255,.18); }
  .row .rk { width: 26px; text-align: center; font-weight: 800; color: var(--muted); }
  .row .rk.r1 { color: var(--gold); } .row .rk.r2 { color: var(--silver); } .row .rk.r3 { color: var(--bronze); }
  .row .who { flex: 1; min-width: 0; }
  .row .who .n { font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .row .who .b { font-size: 13px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .row .nums { display: flex; gap: 14px; text-align: center; font-size: 12px; color: var(--muted); }
  .row .nums b { display: block; color: var(--fg); font-size: 16px; }
  @media (max-width: 520px) { .row .nums .opt { display: none; } }
  .row img.cov { width: 48px; height: 48px; border-radius: 10px; object-fit: cover; background: var(--card-hi); }
  @media (max-width: 420px) { .row img.cov { display: none; } }

  /* ------------------------------------------------------------------ chronology */
  .timeline { display: grid; gap: 14px; }
  .contest { background: var(--card); border-radius: var(--radius); border: 1px solid var(--line); padding: 16px; }
  .contest:hover { border-color: rgba(255,255,255,.18); }
  .cHead { display: flex; align-items: flex-start; gap: 12px; }
  .cHead .em { font-size: 28px; line-height: 1; }
  .cHead .t { font-weight: 800; font-size: 17px; }
  .cHead .m { color: var(--muted); font-size: 13px; }
  /* The whole header is the link; on a phone the words only crowd the title. */
  @media (max-width: 520px) { .cHead .more { display: none; } }
  .pod { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-top: 14px; }
  .pod .slot { position: relative; border-radius: 12px; overflow: hidden; background: var(--card-hi); aspect-ratio: 1; }
  .pod .slot img { width: 100%; height: 100%; object-fit: cover; }
  .pod .slot .cap { position: absolute; left: 0; right: 0; bottom: 0; padding: 18px 8px 7px; font-size: 12px; font-weight: 700;
                    background: linear-gradient(transparent, rgba(0,0,0,.78)); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .monthHead { color: var(--muted); font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: .06em; margin: 22px 0 -2px; }

  /* ------------------------------------------------------------------ profile */
  .profile { display: flex; align-items: center; gap: 20px; flex-wrap: wrap; margin-top: 8px; }
  .profile .n { font-size: clamp(24px, 4.5vw, 34px); font-weight: 800; line-height: 1.15; }
  .profile .u { color: var(--accent); font-weight: 600; }
  .tiles { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-top: 22px; }
  @media (max-width: 520px) { .tiles { grid-template-columns: repeat(2, 1fr); } }
  .tile { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 12px 14px; }
  .tile b { display: block; font-size: 24px; line-height: 1.2; }
  .tile span { color: var(--muted); font-size: 13px; }
  .badges { display: flex; flex-wrap: wrap; gap: 8px; }
  .badge { display: inline-flex; align-items: center; gap: 8px; padding: 8px 14px 8px 10px; border-radius: 999px;
           background: var(--gold-soft); border: 1px solid rgba(242,193,78,.35); font-weight: 700; font-size: 14px; }
  .badge .e { font-size: 20px; line-height: 1; }
  .badge .x { color: var(--gold); }

  /* ------------------------------------------------------------------ themes */
  .explain { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; margin-top: 16px; }
  .step { background: var(--card); border: 1px solid var(--line); border-radius: var(--radius); padding: 16px; }
  .step .e { font-size: 26px; }
  .step h3 { margin: 6px 0 4px; font-size: 16px; }
  .step p { margin: 0; color: var(--muted); font-size: 14px; }
  .quote { margin-top: 16px; padding: 16px 18px; border-left: 3px solid var(--gold); background: var(--card); border-radius: 0 14px 14px 0; }
  .slogan { margin-top: 18px; font-weight: 800; font-size: 18px; line-height: 1.4; }

  .empty { text-align: center; padding: 48px 16px; color: var(--muted); background: var(--card); border-radius: var(--radius);
           border: 1px dashed rgba(255,255,255,.14); }
  .empty .e { font-size: 40px; }
  .search { width: 100%; max-width: 360px; padding: 10px 14px; border-radius: 12px; border: 1px solid var(--line);
            background: var(--card); color: var(--fg); font: inherit; }
  .search:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
  .error { color: #ff8a80; }
  .skeleton { height: 220px; border-radius: var(--radius); background: linear-gradient(90deg, var(--card), var(--card-hi), var(--card));
              background-size: 200% 100%; animation: sk 1.2s infinite; }
  @keyframes sk { to { background-position: -200% 0; } }
  footer { border-top: 1px solid var(--line); padding: 22px 0 40px; color: var(--muted); font-size: 13px; }
  footer a { color: var(--accent); }

  /* ------------------------------------------------------------------ lightbox */
  .lb { position: fixed; inset: 0; z-index: 50; background: #05080c; display: flex; flex-direction: column; }
  .lbTop { display: flex; align-items: center; gap: 10px; padding: 10px 12px; padding-top: max(10px, env(safe-area-inset-top)); }
  .lbTop .count { color: var(--muted); font-size: 13px; margin-left: auto; }
  .lbBtn { width: 42px; height: 42px; border-radius: 50%; border: none; background: rgba(255,255,255,.1); font-size: 20px;
           display: inline-flex; align-items: center; justify-content: center; }
  .lbStrip { flex: 1; min-height: 0; display: flex; overflow-x: auto; scroll-snap-type: x mandatory; scrollbar-width: none; }
  .lbStrip::-webkit-scrollbar { display: none; }
  .lbStrip .ph { flex: none; width: 100%; height: 100%; scroll-snap-align: center; display: flex; align-items: center; justify-content: center; padding: 0 8px; }
  .lbStrip .ph img { max-width: 100%; max-height: 100%; object-fit: contain; border-radius: 6px; }
  .lbNav { position: absolute; top: 50%; transform: translateY(-50%); }
  .lbNav.prev { left: 10px; } .lbNav.next { right: 10px; }
  @media (hover: none) { .lbNav { display: none; } }
  .lbInfo { padding: 12px 16px; padding-bottom: max(14px, env(safe-area-inset-bottom)); display: flex; gap: 12px; align-items: center;
            border-top: 1px solid var(--line); background: rgba(18,26,35,.9); }
  .lbInfo .txt { flex: 1; min-width: 0; }
  .lbInfo .n { font-weight: 700; }
  .lbInfo .sub { color: var(--muted); font-size: 13px; }
  .lbInfo .cap { font-size: 13px; margin-top: 4px; max-height: 3.2em; overflow: auto; }
  .lbInfo .go { flex: none; padding: 8px 12px; border-radius: 10px; background: var(--card-hi); font-size: 13px; font-weight: 700; }
</style>
</head>
<body>
<header class="top">
  <div class="wrap topInner">
    <a class="brand" href="#/"><span class="cup">🏆</span> Доска почёта <small>__BRAND__</small></a>
    <nav class="nav" id="nav">
      <a href="#/" data-r="">Зал славы</a>
      <a href="#/contests" data-r="contests">Хронология</a>
      <a href="#/artists" data-r="artists">Художники</a>
      <a href="#/themes" data-r="themes">Тематические конкурсы</a>
    </nav>
  </div>
</header>
<main class="wrap" id="app"><div class="skeleton"></div></main>
<footer class="wrap" id="footer">Доска почёта __BRAND__ — все итоги, работы и победители конкурсов чата.</footer>

<div class="lb" id="lb" hidden>
  <div class="lbTop">
    <button class="lbBtn" id="lbClose" aria-label="Закрыть">✕</button>
    <span class="count" id="lbCount"></span>
  </div>
  <div class="lbStrip" id="lbStrip"></div>
  <button class="lbBtn lbNav prev" id="lbPrev" aria-label="Предыдущее фото">‹</button>
  <button class="lbBtn lbNav next" id="lbNext" aria-label="Следующее фото">›</button>
  <div class="lbInfo" id="lbInfo"></div>
</div>

<script>
"use strict";
const PREFIX = "__PREFIX__";
const BRAND = "__BRAND__";
const app = document.getElementById("app");
const responses = new Map();      // url -> parsed JSON: back and forth costs nothing
const scrolls = new Map();        // route -> scrollY, restored when the route is shown again
// Telegram's script defines WebApp in any browser; only inside Telegram is the platform known.
const tg = () => {
  const webApp = window.Telegram && window.Telegram.WebApp;
  return webApp && webApp.platform && webApp.platform !== "unknown" ? webApp : null;
};

// ------------------------------------------------------------------- helpers
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function plural(n, one, few, many) {
  const t2 = Math.abs(n) % 100, t1 = Math.abs(n) % 10;
  if (t2 >= 11 && t2 <= 14) return `${n} ${many}`;
  if (t1 === 1) return `${n} ${one}`;
  if (t1 >= 2 && t1 <= 4) return `${n} ${few}`;
  return `${n} ${many}`;
}
const votesLabel = (n) => plural(n, "голос", "голоса", "голосов");
const worksLabel = (n) => plural(n, "работа", "работы", "работ");

const MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
                "сентябрь", "октябрь", "ноябрь", "декабрь"];
const MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
                    "сентября", "октября", "ноября", "декабря"];
function parseDate(iso) { const d = iso ? new Date(iso) : null; return d && !isNaN(d) ? d : null; }
function dayLabel(iso) {
  const d = parseDate(iso);
  return d ? `${d.getDate()} ${MONTHS_GEN[d.getMonth()]} ${d.getFullYear()}` : "";
}
function weekLabel(week) {
  const m = /^(\d{4})-W(\d{2})$/.exec(week || "");
  return m ? `${+m[2]}-я неделя ${m[1]}` : "";
}

const HUES = [210, 340, 28, 160, 265, 190, 8, 120];
function avatar(author, size) {
  const name = (author && author.name) || "?";
  const letter = (Array.from(name.trim())[0] || "?").toUpperCase();
  let hash = 0;
  for (const ch of name) hash = (hash * 31 + ch.codePointAt(0)) >>> 0;
  const el = h("span", {class: `ava ${size}`, style: `background: hsl(${HUES[hash % HUES.length]} 45% 42%)`}, letter);
  if (author && author.avatar) {
    const img = h("img", {src: author.avatar, alt: "", loading: "lazy"});
    img.addEventListener("error", () => img.remove());
    el.append(img);
  }
  return el;
}

function picture(url, alt) {
  return url ? h("img", {src: url, alt: alt || "", loading: "lazy"}) : h("div", {class: "noPic"}, "фото не сохранилось");
}
const medal = (place) => place === 1 ? "🥇" : place === 2 ? "🥈" : place === 3 ? "🥉" : "";

async function load(path) {
  const url = PREFIX + "/api" + path;
  if (responses.has(url)) return responses.get(url);
  const response = await fetch(url);
  if (!response.ok) throw new Error(response.status === 404 ? "Такой страницы нет." : "Не получилось загрузить.");
  const data = await response.json();
  responses.set(url, data);
  return data;
}

function section(title, moreHref, moreText, ...body) {
  return h("section", {class: "section"},
    h("div", {class: "sectionHead"}, h("h2", {}, title), moreHref ? h("a", {class: "more", href: moreHref}, moreText) : null),
    ...body);
}

function emptyState(text) {
  return h("div", {class: "empty"}, h("div", {class: "e"}, "🖼"), h("p", {}, text));
}

// ------------------------------------------------------------------- pieces
function workCard(work, sub, list, index) {
  const placeClass = work.podium ? `place p${work.place}` : "place";
  return h("div", {class: "card" + (work.winner ? " win" : "")},
    h("div", {class: "pic", onclick: () => openLightbox(list, index)},
      picture(work.thumb, work.author.name),
      h("span", {class: placeClass}, work.podium ? `${medal(work.place)} ${work.place}` : `#${work.place}`),
      h("span", {class: "votes"}, votesLabel(work.votes))),
    h("a", {class: "foot", href: `#/artist/${work.author.key}`},
      avatar(work.author, "s"),
      h("div", {class: "txt"}, h("div", {class: "n"}, work.author.name), sub ? h("div", {class: "sub"}, sub) : null)));
}

function badgePills(badges) {
  if (!badges || !badges.length) return null;
  return h("div", {class: "badges"}, badges.map((b) =>
    h("span", {class: "badge", title: b.weekly ? "Победы в итогах недели" : "Победа в тематическом конкурсе"},
      h("span", {class: "e"}, b.badge), b.title, b.count > 1 ? h("span", {class: "x"}, `×${b.count}`) : null)));
}

function badgeLine(badges) {
  return (badges || []).map((b) => b.badge + (b.count > 1 ? `×${b.count}` : "")).join(" ");
}

function artistRow(artist) {
  const rk = artist.rank <= 3 ? `rk r${artist.rank}` : "rk";
  return h("a", {class: "row", href: `#/artist/${artist.key}`},
    h("span", {class: rk}, artist.rank),
    avatar(artist, "m"),
    h("div", {class: "who"},
      h("div", {class: "n"}, artist.name),
      h("div", {class: "b"}, badgeLine(artist.badges) || (artist.username ? "@" + artist.username : worksLabel(artist.works)))),
    h("div", {class: "nums"},
      h("div", {}, h("b", {}, artist.wins), "побед"),
      h("div", {class: "opt"}, h("b", {}, artist.podiums), "призов"),
      h("div", {}, h("b", {}, artist.works), "работ")),
    artist.cover ? h("img", {class: "cov", src: artist.cover, alt: "", loading: "lazy"}) : null);
}

function contestMeta(contest) {
  return [weekLabel(contest.week) || dayLabel(contest.closed_at), worksLabel(contest.works),
          contest.voters ? plural(contest.voters, "голосующий", "голосующих", "голосующих") : null]
    .filter(Boolean).join(" · ");
}

function contestBlock(contest) {
  const slots = [1, 2, 3].map((place) => {
    const work = contest.podium.find((w) => w.place === place);
    return h("a", {class: "slot", href: work ? `#/artist/${work.author.key}` : `#/contest/${contest.id}`},
      work ? picture(work.thumb, work.author.name) : null,
      work ? h("span", {class: `place p${place}`}, medal(place)) : null,
      work ? h("span", {class: "cap"}, work.author.name) : null);
  });
  return h("article", {class: "contest"},
    h("a", {class: "cHead", href: `#/contest/${contest.id}`},
      h("span", {class: "em"}, contest.badge),
      h("div", {style: "flex:1;min-width:0"},
        h("div", {class: "t"}, contest.title, " ", contest.weekly ? null : h("span", {class: "tag"}, contest.hashtag)),
        h("div", {class: "m"}, contestMeta(contest))),
      h("span", {class: "more"}, "Все работы →")),
    contest.podium.length ? h("div", {class: "pod"}, slots) : h("p", {class: "muted"}, "В этом конкурсе никто не набрал голосов."));
}

// ------------------------------------------------------------------- screens
async function screenHome() {
  const data = await load("/overview");
  document.title = `Доска почёта · ${BRAND}`;
  const nodes = [
    h("h1", {}, "Доска почёта"),
    h("p", {class: "lead"}, `Все итоги конкурсов ${BRAND}: победители, их работы и вся хронология — от первой недели до сегодняшней.`),
    h("div", {class: "chips"},
      h("span", {class: "chip"}, h("b", {}, data.stats.contests), plural(data.stats.contests, "конкурс", "конкурса", "конкурсов").replace(/^\d+ /, "")),
      h("span", {class: "chip"}, h("b", {}, data.stats.works), plural(data.stats.works, "работа", "работы", "работ").replace(/^\d+ /, "")),
      h("span", {class: "chip"}, h("b", {}, data.stats.artists), plural(data.stats.artists, "художник", "художника", "художников").replace(/^\d+ /, "")),
      data.stats.themes ? h("span", {class: "chip"}, h("b", {}, data.stats.themes), "тематических") : null),
  ];
  if (!data.latest.length) {
    nodes.push(h("div", {class: "section"}, emptyState(
      "Доска почёта пока пуста: первый победитель появится здесь, как только закроется голосование.")));
    nodes.push(themesTeaser());
    return nodes;
  }
  const top = data.latest[0];
  const win = top.winner;
  nodes.push(h("div", {class: "hero"},
    h("div", {class: "heroPic", onclick: () => openContestWinner(top)},
      picture(win.photo || win.thumb, win.author.name),
      h("span", {class: "ribbon"}, `${top.badge} Последний победитель`)),
    h("div", {class: "heroInfo"},
      h("a", {class: "heroWho", href: `#/artist/${win.author.key}`},
        avatar(win.author, "m"),
        h("div", {}, h("div", {class: "name"}, win.author.name),
          win.author.username ? h("div", {class: "heroMeta"}, "@" + win.author.username) : null)),
      h("a", {href: `#/contest/${top.id}`},
        h("div", {style: "font-weight:700"}, `${top.title} `, top.weekly ? null : h("span", {class: "tag"}, top.hashtag)),
        h("div", {class: "heroMeta"}, `${contestMeta(top)} · ${votesLabel(win.votes)} у победителя`)),
      h("div", {class: "podiumMini"}, top.podium.map((w) =>
        h("a", {class: "pmRow", href: `#/artist/${w.author.key}`},
          h("span", {style: "font-size:20px"}, medal(w.place)),
          w.thumb ? h("img", {class: "th", src: w.thumb, alt: "", loading: "lazy"}) : null,
          h("span", {class: "who"}, w.author.name),
          h("span", {class: "v"}, votesLabel(w.votes))))))));

  const winners = data.latest.map((c) => c.winner);
  nodes.push(section("Зал славы", "#/contests", "Вся хронология →",
    h("div", {class: "grid"}, data.latest.map((c, i) =>
      workCard(c.winner, `${c.badge} ${c.title}${c.week ? " · " + weekLabel(c.week) : ""}`, winners, i)))));
  if (data.artists.length) {
    nodes.push(section("Лучшие художники", "#/artists", "Все художники →",
      h("div", {class: "board"}, data.artists.map(artistRow))));
  }
  nodes.push(themesTeaser(data.themes));
  return nodes;
}

function themesTeaser(themes) {
  const list = (themes || []).filter((c) => c.winner);
  return section("Тематические конкурсы", "#/themes", "Как это работает →",
    h("p", {class: "lead"}, "Помимо любимых всеми #итогинедели — конкурсы на тему: аниме-покрас, миниатюра 32 мм, скульпт, новички. "
      + "Победитель получает тематический значок-ачивку — он появляется в его профиле здесь."),
    list.length ? h("div", {class: "grid", style: "margin-top:14px"}, list.map((c, i) =>
      workCard(c.winner, `${c.badge} ${c.title}`, list.map((x) => x.winner), i))) : null);
}

async function screenContests() {
  const data = await load("/contests");
  document.title = `Хронология · ${BRAND}`;
  let filter = "all";
  const holder = h("div", {class: "timeline"});
  const chips = h("div", {class: "chips"});
  const draw = () => {
    chips.replaceChildren(...[["all", "Все"], ["weekly", "Итоги недели"], ["themes", "Тематические"]].map(([key, label]) =>
      h("button", {class: "chip" + (filter === key ? " on" : ""), onclick: () => { filter = key; draw(); }}, label)));
    const shown = data.contests.filter((c) => filter === "all" || (filter === "weekly") === c.weekly);
    const nodes = [];
    let month = "";
    for (const contest of shown) {
      const d = parseDate(contest.closed_at);
      const label = d ? `${MONTHS[d.getMonth()]} ${d.getFullYear()}` : "";
      if (label && label !== month) { month = label; nodes.push(h("div", {class: "monthHead"}, label)); }
      nodes.push(contestBlock(contest));
    }
    holder.replaceChildren(...(nodes.length ? nodes : [emptyState("Здесь пока ничего нет.")]));
  };
  draw();
  return [h("h1", {}, "Хронология итогов"),
          h("p", {class: "lead"}, "Каждый закрытый конкурс — с призёрами и всеми работами, от новых к старым."),
          chips, h("div", {class: "section", style: "margin-top:18px"}, holder)];
}

async function screenContest(id) {
  const {contest} = await load(`/contests/${encodeURIComponent(id)}`);
  document.title = `${contest.title} · ${BRAND}`;
  return [
    h("a", {class: "more", href: "#/contests"}, "← Хронология"),
    h("h1", {style: "margin-top:10px"}, `${contest.badge} ${contest.title}`),
    h("p", {class: "lead"}, contest.weekly ? null : h("span", {class: "tag"}, contest.hashtag), " ", contestMeta(contest),
      contest.closed_at ? ` · итоги ${dayLabel(contest.closed_at)}` : ""),
    section(contest.podium.length ? "Призёры и все работы" : "Все работы", null, null,
      h("div", {class: "grid"}, contest.entries.map((w, i) =>
        workCard(w, w.author.username ? "@" + w.author.username : votesLabel(w.votes), contest.entries, i)))),
  ];
}

async function screenArtists() {
  const data = await load("/artists");
  document.title = `Художники · ${BRAND}`;
  const board = h("div", {class: "board"});
  const draw = (needle) => {
    const q = (needle || "").trim().toLowerCase().replace(/^@/, "");
    const shown = data.artists.filter((a) => !q || a.name.toLowerCase().includes(q) || (a.username || "").toLowerCase().includes(q));
    board.replaceChildren(...(shown.length ? shown.map(artistRow) : [emptyState("Никого не нашлось.")]));
  };
  draw("");
  return [h("h1", {}, "Художники"),
          h("p", {class: "lead"}, "Все, кто участвовал в конкурсах, — по победам, призовым местам и голосам."),
          h("div", {class: "section", style: "margin-top:18px"},
            h("input", {class: "search", type: "search", placeholder: "Найти по имени или @нику", oninput: (e) => draw(e.target.value)}),
            h("div", {style: "height:14px"}), board)];
}

async function screenArtist(key) {
  const {artist} = await load(`/artists/${encodeURIComponent(key)}`);
  document.title = `${artist.name} · ${BRAND}`;
  const entries = artist.entries;
  return [
    h("a", {class: "more", href: "#/artists"}, "← Художники"),
    h("div", {class: "profile", style: "margin-top:14px"},
      avatar(artist, "l"),
      h("div", {},
        h("div", {class: "n"}, artist.name),
        artist.telegram ? h("a", {class: "u", href: artist.telegram, target: "_blank", rel: "noopener"}, "@" + artist.username)
          : artist.username ? h("div", {class: "u"}, "@" + artist.username) : null,
        h("div", {class: "muted", style: "margin-top:4px"}, `${artist.rank}-е место среди художников`))),
    h("div", {class: "tiles"},
      h("div", {class: "tile"}, h("b", {}, artist.wins), h("span", {}, "побед")),
      h("div", {class: "tile"}, h("b", {}, artist.podiums), h("span", {}, "призовых мест")),
      h("div", {class: "tile"}, h("b", {}, artist.works), h("span", {}, "работ в конкурсах")),
      h("div", {class: "tile"}, h("b", {}, artist.votes), h("span", {}, "голосов всего"))),
    artist.badges.length ? section("Значки и ачивки", null, null, badgePills(artist.badges)) : null,
    section("Работы", null, null,
      h("div", {class: "grid"}, entries.map((w, i) =>
        workCard(w, `${w.contest_badge} ${w.contest_title}${w.contest_week ? " · " + weekLabel(w.contest_week) : ""}`, entries, i)))),
  ];
}

async function screenThemes() {
  document.title = `Тематические конкурсы · ${BRAND}`;
  const data = await load("/overview");
  const steps = [
    ["🗳", "Тему выбирает сообщество", "Вы предлагаете варианты — голосуем — выбираем следующую тему."],
    ["🎨", "Кидаете работу под тему", "С хэштегом конкурса. Красить новую не обязательно: подойдёт та самая минька из шкафа, которой вы гордитесь, — хоть пятилетней давности."],
    ["✅", "Голосуем за победителя", "Работы собираются в голосование в боте, как #итогинедели."],
    ["🏅", "Победителю — значок-ачивка", "Тематический значок появляется в профиле художника здесь, на Доске почёта."],
  ];
  const levels = [
    ["🌱", "Новичок?", "Есть конкурс для новичков."],
    ["🖌", "Покрасчик?", "Есть темы для покраса: аниме-покрас, миниатюра 32 мм и не только."],
    ["🗿", "Скульптор?", "Для вас тоже будет отдельный конкурс."],
  ];
  const themes = data.themes || [];
  return [
    h("h1", {}, "Тематические конкурсы"),
    h("p", {class: "lead"}, "Любимые всеми #итогинедели остаются как есть, а сверху — ещё одна движуха: конкурсы на тему, "
      + "где каждый может показать себя в своей специализации."),
    h("div", {class: "explain"}, steps.map(([e, t, p]) => h("div", {class: "step"}, h("div", {class: "e"}, e), h("h3", {}, t), h("p", {}, p)))),
    section("Участвовать можно на любом уровне", null, null,
      h("div", {class: "explain", style: "margin-top:0"}, levels.map(([e, t, p]) => h("div", {class: "step"}, h("div", {class: "e"}, e), h("h3", {}, t), h("p", {}, p))))),
    h("div", {class: "quote"}, "Примеры тем: лучший аниме-покрас · лучшая миниатюра 32 мм · лучший скульпт · лучший новичок · и ещё куча всего. "
      + "На старте — раз в неделю; дальше подстроим формат по активности."),
    themes.length
      ? section("Прошедшие конкурсы", "#/contests", "Вся хронология →", h("div", {class: "timeline"}, themes.map(contestBlock)))
      : section("Прошедшие конкурсы", null, null, emptyState("Первый тематический конкурс ещё впереди — тему выберем вместе 👀")),
    h("div", {class: "slogan"}, `${BRAND}, дальше — больше. Дальше — круче. Дальше — сильнее. 🔥`),
  ];
}

// ------------------------------------------------------------------- router
const ROUTES = [
  [/^$/, screenHome, ""],
  [/^contests$/, screenContests, "contests"],
  [/^contest\/([A-Za-z0-9_.-]+)$/, screenContest, "contests"],
  [/^artists$/, screenArtists, "artists"],
  [/^artist\/([0-9a-z]+)$/, screenArtist, "artists"],
  [/^themes$/, screenThemes, "themes"],
];
let current = null;
const visited = [];   // the screens this visit has been through, for Telegram's back button

async function route() {
  const path = decodeURIComponent(location.hash.replace(/^#\/?/, ""));
  if (visited.length > 1 && visited[visited.length - 2] === path) visited.pop();
  else if (visited[visited.length - 1] !== path) visited.push(path);
  if (current !== null) scrolls.set(current, window.scrollY);
  current = path;
  const match = ROUTES.map(([re, fn, nav]) => [re.exec(path), fn, nav]).find(([m]) => m) || [[""], screenHome, ""];
  const [m, fn, nav] = match;
  for (const link of document.querySelectorAll("#nav a")) link.classList.toggle("on", link.dataset.r === nav);
  const telegram = tg();
  if (telegram && telegram.BackButton) path ? telegram.BackButton.show() : telegram.BackButton.hide();
  try {
    const nodes = await fn(...m.slice(1));
    if (current !== path) return;  // the reader moved on while this was loading
    app.replaceChildren(...[].concat(nodes).filter(Boolean));
  } catch (e) {
    if (current !== path) return;
    app.replaceChildren(h("div", {class: "empty"}, h("div", {class: "e"}, "😕"), h("p", {class: "error"}, e.message || "Ошибка."),
      h("a", {class: "more", href: "#/"}, "На главную")));
  }
  window.scrollTo(0, scrolls.get(path) || 0);
}

// ------------------------------------------------------------------- lightbox
const lb = document.getElementById("lb");
const strip = document.getElementById("lbStrip");
let lbList = [], lbIndex = 0, lbOpen = false, afterClose = null;

function openContestWinner(contest) { openLightbox([contest.winner], 0); }

function openLightbox(list, index) {
  lbList = list; lbIndex = index;
  showWork(index);
  lb.hidden = false;
  document.body.style.overflow = "hidden";
  if (!lbOpen) { lbOpen = true; history.pushState({lightbox: true}, "", location.href); }
  const telegram = tg();
  if (telegram && telegram.BackButton) telegram.BackButton.show();
}

function closeLightbox(fromHistory) {
  if (!lbOpen) return;
  lbOpen = false;
  lb.hidden = true;
  document.body.style.overflow = "";
  strip.replaceChildren();
  if (!fromHistory) history.back();
  const telegram = tg();
  if (telegram && telegram.BackButton && !location.hash.replace(/^#\/?/, "")) telegram.BackButton.hide();
}

function showWork(index) {
  lbIndex = (index + lbList.length) % lbList.length;
  const work = lbList[lbIndex];
  const photos = (work.photos && work.photos.length) ? work.photos : [work.photo || work.thumb].filter(Boolean);
  strip.replaceChildren(...photos.map((url) => h("div", {class: "ph"}, h("img", {src: url, alt: work.author.name}))));
  strip.scrollLeft = 0;
  document.getElementById("lbCount").textContent =
    (lbList.length > 1 ? `Работа ${lbIndex + 1} из ${lbList.length}` : "") + (photos.length > 1 ? ` · ${photos.length} фото` : "");
  const where = [work.contest_title, work.podium ? `${medal(work.place)} ${work.place} место` : `${work.place} место`, votesLabel(work.votes)]
    .filter(Boolean).join(" · ");
  document.getElementById("lbInfo").replaceChildren(
    avatar(work.author, "m"),
    h("div", {class: "txt"},
      h("a", {class: "n", href: `#/artist/${work.author.key}`, onclick: (e) => {
        // Closed through history first, so the photo's own history entry is gone before the
        // profile's is added -- otherwise "back" from the profile lands on nothing.
        e.preventDefault(); afterClose = `#/artist/${work.author.key}`; closeLightbox(false);
      }}, work.author.name),
      h("div", {class: "sub"}, where),
      work.text ? h("div", {class: "cap"}, work.text) : null),
    work.post_url ? h("a", {class: "go", href: work.post_url, target: "_blank", rel: "noopener"}, "Пост в чате ↗") : null);
  const many = lbList.length > 1 || photos.length > 1;
  document.getElementById("lbPrev").hidden = !many;
  document.getElementById("lbNext").hidden = !many;
}

function step(direction) {
  // Through the photos of this work first, then on to the next work.
  const width = strip.clientWidth || 1;
  const page = Math.round(strip.scrollLeft / width);
  const pages = strip.children.length;
  if (page + direction >= 0 && page + direction < pages) strip.scrollTo({left: (page + direction) * width, behavior: "smooth"});
  else if (lbList.length > 1) showWork(lbIndex + direction);
}

document.getElementById("lbClose").addEventListener("click", () => closeLightbox(false));
document.getElementById("lbPrev").addEventListener("click", () => step(-1));
document.getElementById("lbNext").addEventListener("click", () => step(1));
strip.addEventListener("click", (e) => { if (e.target === strip || e.target.classList.contains("ph")) closeLightbox(false); });
document.addEventListener("keydown", (e) => {
  if (!lbOpen) return;
  if (e.key === "Escape") closeLightbox(false);
  else if (e.key === "ArrowLeft") step(-1);
  else if (e.key === "ArrowRight") step(1);
});
window.addEventListener("popstate", () => {
  if (lbOpen) closeLightbox(true);
  if (afterClose) { const target = afterClose; afterClose = null; location.hash = target; }
});
window.addEventListener("hashchange", () => { if (lbOpen) closeLightbox(true); route(); });

// ------------------------------------------------------------------- boot
window.addEventListener("load", () => {
  const telegram = tg();
  if (!telegram) return;
  try {
    telegram.ready();
    telegram.expand();
    // Opened straight onto an inner page there is no screen of ours to go back to, and
    // history.back() would do nothing -- so that case goes to the front page instead.
    if (telegram.BackButton) telegram.BackButton.onClick(() => {
      if (lbOpen || visited.length > 1) history.back(); else location.hash = "#/";
    });
    if (location.hash.replace(/^#\/?/, "") && telegram.BackButton) telegram.BackButton.show();
  } catch (e) { /* an old client without these is still a browser */ }
});
load("/overview").then((data) => {
  if (data.bot) {
    document.getElementById("footer").replaceChildren(
      `Доска почёта ${BRAND}. Голосование и конкурсы — в боте `,
      h("a", {href: `https://t.me/${data.bot}?start=vote`, target: "_blank", rel: "noopener"}, "@" + data.bot), ".");
  }
}).catch(() => {});
route();
</script>
</body>
</html>
"""
