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
id) but not secret. The rendered board picture (see below) is served the same way, being
a collage of exactly those photos.

Two pages live here, not one:

- PAGE_HTML at ROUTE_PREFIX is the ballot (and, with ?mode=admin, the moderation screen).
- BOARD_HTML at ROUTE_PREFIX + "/board" is the cropping page: the export's own three-column
  board, WYSIWYG, where an administrator frames each work by hand and then renders the
  picture (vote_image.py). Administrators only, enforced on every request it makes rather
  than by which URL was opened.
"""

import asyncio
import json
import re
from pathlib import Path
from typing import Awaitable, Callable

from aiohttp import web

import vote_image
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
# The subscription split is more sensitive than normal vote moderation. In production it
# is restricted to actual chat administrators and the owner (Sultan), excluding delegates.
_IS_STATS_ADMIN_KEY = web.AppKey("is_stats_admin", Callable[[dict], Awaitable[bool]])
# Same shape as _IS_ADMIN_KEY: whether this Telegram user currently subscribes to the
# artists' channel. It is a prompt/statistic attribute, never a ballot permission.
_IS_MEMBER_KEY = web.AppKey("is_member", Callable[[dict], Awaitable[bool]])
# Delivers the results of a closed vote. Takes the admin's user dict, the poll, and the
# FULL standings -- every admitted entry as an (Entry, votes) pair, ranked highest-first,
# element 0 being the recorded winner. Full, not the top 3: the results message
# bot_listener.py writes lists every entrant, and a caller that had already sliced the
# list to a podium could not be un-sliced from over there.
# What bot_listener.py then does with it is entirely its own business (today: a draft in
# the closing admin's DM, posted to the chat only once they confirm). Failure is caught by
# the caller and reported back, not raised through the API response.
_ANNOUNCE_KEY = web.AppKey(
    "announce", Callable[[dict, voting.Poll, list[tuple[voting.Entry, int]]], Awaitable[None]]
)
# Delivers a freshly rendered board picture (the cropping page's "Выгрузить картинку") to
# the administrator who asked for it. Takes their verified user dict, the poll, and the
# path of the file on disk -- the rendering happens here, the sending is bot_listener.py's,
# for the same split of concerns _ANNOUNCE_KEY draws. Failure is reported back to the page,
# not raised: the file is written either way, and the page offers a link to it.
_EXPORT_KEY = web.AppKey("export", Callable[[dict, voting.Poll, Path], Awaitable[None]])
# Fetches one author's Telegram profile photo. bot_listener owns the live Bot API client,
# so the web layer receives this as a callable just like announce/export rather than
# opening a second HTTP session of its own.
_AVATAR_KEY = web.AppKey("avatar", Callable[[int], Awaitable[bytes | None]])
_AVATAR_CACHE_KEY = web.AppKey("avatar_cache", dict)
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
    username = str(entry.author_username or "").lstrip("@")
    profile_url = (
        f"https://t.me/{username}" if re.fullmatch(r"[A-Za-z0-9_]+", username)
        else (f"tg://user?id={entry.author_id}" if entry.author_id is not None else None)
    )
    return {
        "id": entry.entry_id,
        "author_id": entry.author_id,
        "author": entry.author_name,
        "username": entry.author_username,
        "profile_url": profile_url,
        "avatar": (
            f"{base}/avatar/{poll.poll_id}/{entry.author_id}"
            if entry.author_id is not None else None
        ),
        "text": entry.text,
        "posted_at": entry.posted_at,
        "photos": [f"{base}/media/{poll.poll_id}/{name}" for name in entry.media],
    # The board editor stores the crop in the source photo's coordinates. Expose it
    # with the entry so both the moderation grid and the public ballot can render the
    # same first-photo framing as the exported board.
    "crop": poll.crops.get(entry.entry_id),
    }


def _results_payload(poll: voting.Poll, base: str) -> list[dict]:
    """One dict per APPROVED entry, ranked most-votes-first (poll.tally()'s own order) --
    what the page's standings table renders. A separate function from _entry_payload
    because results have nothing to do with the media/text a voter picks from; they are
    shown after the fact, next to a vote count.

    `photo` is the first picture only, and None for an entry that somehow has none: the
    standings row shows a thumbnail barely bigger than the text beside it, so the rest of
    an entry's media would be weight sent for nothing. It is the same url _entry_payload
    builds, so the browser serves it from cache rather than fetching the picture twice.
    """
    return [
        {
            "id": entry.entry_id,
            "author": entry.author_name,
            "username": entry.author_username,
            "votes": count,
            "photo": f"{base}/media/{poll.poll_id}/{entry.media[0]}" if entry.media else None,
        }
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
    can_view_vote_stats = admin_mode and await request.app[_IS_STATS_ADMIN_KEY](user)
    is_subscriber = await request.app[_IS_MEMBER_KEY](user)
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
        "can_view_vote_stats": can_view_vote_stats,
        "is_subscriber": is_subscriber,
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
        payload["results"] = _results_payload(poll, base)
    if admin_mode:
        payload["approved"] = list(poll.approved)
        payload["counts"] = {e.entry_id: count for e, count in poll.tally()}
        # Only in admin mode, and only because the cropping page (BOARD_HTML) is the one
        # thing that reads it -- a voter's ballot has no use for how the export is framed.
        payload["crops"] = poll.crops
    return web.json_response(payload)


async def handle_ballot(request: web.Request) -> web.Response:
    """Records this user's choices, replacing any earlier ballot of theirs -- unless the
    poll's own settings (set from the moderation screen) say otherwise: `max_choices`
    caps how many entries one ballot may name, and `allow_revote=False` locks a voter's
    first ballot in permanently. Both are rejected here, at the request boundary, rather
    than silently truncated in voting.record_vote -- a voter should know their ballot
    didn't count as sent, not have it quietly reshaped.

    The membership check is deliberately a snapshot rather than a gate: everybody's
    ballot counts, while non-subscribers receive an invitation after it is saved."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_error("malformed request body")

    user = await _authenticate(request, body)
    entry_name = request.app[_ENTRY_KEY]
    # A Telegram failure simply means we cannot confirm a subscription right now; the
    # ballot must still be accepted, and the client may invite the voter to join.
    is_subscriber = await request.app[_IS_MEMBER_KEY](user)
    # Everything from here to save_poll is under voting.poll_lock so two ballots cannot
    # interleave between the load and the save and erase one another.
    async with voting.poll_lock:
        poll = voting.latest_poll(entry_name)
        if poll is None:
            return _json_error("голосование ещё не создано", status=404)
        if not poll.open:
            return _json_error("голосование закрыто", status=409)
        choices = body.get("choices")
        if not isinstance(choices, list) or not all(isinstance(c, str) for c in choices):
            return _json_error("choices must be a list of entry ids")

        # Truthiness, not `in`: an EMPTY recorded ballot is not a cast one. A voter ends up
        # with the key present and no choices behind it when an admin un-admits everything
        # they picked (record_vote filters to the admitted set) -- and treating that as
        # "already voted" locks them out of the contest holding no vote at all.
        if not poll.allow_revote and poll.votes.get(str(user["id"])):
            return _json_error("менять голос нельзя -- голосование уже зафиксировано", status=409)
        distinct = len(set(choices))
        if poll.max_choices and distinct > poll.max_choices:
            return _json_error(f"можно выбрать не более {poll.max_choices}", status=400)

        voting.record_vote(poll, user["id"], choices, subscriber=is_subscriber)
        voting.save_poll(poll)
    request.app[_LOG_KEY](f"[vote_web] ballot from {voting.display_name(user)}: {len(choices)} choice(s)")
    return web.json_response({
        "ok": True,
        "my_vote": poll.votes[str(user["id"])],
        # Lets the page render the standings right after a vote without a second round
        # trip to /api/poll -- the voter has just earned the right to see them (see
        # handle_poll's docstring for the gating rule these mirror).
        "results": _results_payload(poll, request.app[_ROUTE_PREFIX_KEY]),
        "voter_count": len(poll.votes),
        "is_subscriber": is_subscriber,
    })


async def handle_vote_stats(request: web.Request) -> web.Response:
    """Weekly v1 ballot figures, including the private subscription split.

    The route does not trust the button being hidden: its owner/chat-admin check guards
    every request, so a copied URL cannot disclose voting behaviour.
    """
    user = await _authenticate(request)
    if not await request.app[_IS_STATS_ADMIN_KEY](user):
        return _json_error("статистика голосования доступна только администраторам", status=403)
    records = voting.weekly_vote_records(request.app[_ENTRY_KEY])
    # Only voters who were non-subscribers at the moment of voting need a fresh lookup:
    # it distinguishes "joined after" from "has not joined" without spending 59 Bot API
    # requests re-checking the historic ballots that the old membership gate guaranteed.
    to_check = set().union(*(record["non_subscriber_ids"] for record in records)) if records else set()
    semaphore = asyncio.Semaphore(8)

    async def is_current_subscriber(voter_id: str) -> tuple[str, bool] | None:
        try:
            async with semaphore:
                subscribed = await request.app[_IS_MEMBER_KEY]({"id": int(voter_id)})
        except Exception:
            # A failed Telegram lookup must not mislabel somebody as "did not join".
            return None
        return voter_id, bool(subscribed)

    checked = await asyncio.gather(*(is_current_subscriber(voter_id) for voter_id in to_check))
    checked_statuses = {result[0]: result[1] for result in checked if result is not None}
    current_subscriber_ids = {voter_id for voter_id, subscribed in checked_statuses.items() if subscribed}
    # Unavailable checks have no "after"/"not" classification yet; the next opening of
    # this private screen retries them rather than permanently recording an API failure.
    return web.json_response({
        "weeks": voting.weekly_vote_stats(
            request.app[_ENTRY_KEY], current_subscriber_ids=current_subscriber_ids,
            checked_voter_ids=set(checked_statuses), records=records,
        ),
        "checked_voters": len(checked_statuses),
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
    """Closes the vote, records the top-voted admitted entry as the winner, and hands the
    results to bot_listener.py. Administrators only.

    Closing and picking the winner happen even if that hand-off fails (a transient Bot API
    error, say) -- the poll's own state is the source of truth for who won, not whether a
    particular message went out, so a delivery failure is reported back rather than losing
    the result. Note that "notified" only promises the results REACHED bot_listener.py:
    what it does with them (today, a draft for the admin to approve) is not this module's
    concern, which is why the page's own wording no longer claims anything was posted.
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
    # which only ever tracks the single #1 winner -- the standings are purely a reporting
    # concern. The announcer gets all of them and the page gets the top 3: the results
    # message bot_listener.py writes names every admitted work, while this response only
    # ever feeds a three-line banner.
    standings = poll.tally()
    top = standings[:3]
    request.app[_LOG_KEY](
        f"[vote_web] {voting.display_name(user)} closed the vote -- winner {winner_entry.entry_id} ({votes} votes)"
    )

    notified = True
    try:
        await request.app[_ANNOUNCE_KEY](user, poll, standings)
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


def _export_subtitle(poll: voting.Poll, standings: list) -> str:
    return (
        f"Проголосовало: {len(poll.votes)} чел. · работ: {len(standings)} · "
        f"{'голосование открыто' if poll.open else 'голосование закрыто'}"
    )


async def handle_crops(request: web.Request) -> web.Response:
    """Stores how each work is framed in the exported picture -- the cropping page's
    "Сохранить". Administrators only, and the whole set at once (see voting.set_crops):
    the page always submits every card it is showing, so a save can never leave the poll
    holding half of one editing session and half of another.

    Under poll_lock like every other read-modify-write of a poll: two administrators
    cropping at the same moment would otherwise each load, edit and save, and the second
    would erase the first's admitting or votes along with their crops.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_error("malformed request body")

    user = await _authenticate(request, body)
    if not await request.app[_IS_ADMIN_KEY](user):
        return _json_error("только администраторы могут кадрировать работы", status=403)

    crops = body.get("crops")
    if not isinstance(crops, dict):
        return _json_error("crops must be an object of entry_id -> {x, y, size}")

    entry_name = request.app[_ENTRY_KEY]
    async with voting.poll_lock:
        poll = voting.latest_poll(entry_name)
        if poll is None:
            return _json_error("голосование ещё не создано", status=404)
        voting.set_crops(poll, crops)
        voting.save_poll(poll)
    request.app[_LOG_KEY](
        f"[vote_web] {voting.display_name(user)} saved framing for {len(poll.crops)} entr(ies)"
    )
    return web.json_response({"ok": True, "crops": poll.crops})


async def handle_export(request: web.Request) -> web.Response:
    """Renders the board as one picture and hands it to bot_listener.py to deliver.
    Administrators only.

    Takes the crops in the same request rather than trusting that "Сохранить" was pressed
    first: exporting a framing different from the one on screen is the one failure mode
    that would be invisible until the picture arrives.

    The render runs in a worker thread -- decoding and scaling every photo in the poll is
    seconds of CPU, and this is the same event loop that serves every ballot. Delivery
    failing (the admin never started a DM with the bot, say) does not fail the export: the
    file is on disk and the response carries a link to it either way.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_error("malformed request body")

    user = await _authenticate(request, body)
    if not await request.app[_IS_ADMIN_KEY](user):
        return _json_error("только администраторы могут выгружать картинку", status=403)

    entry_name = request.app[_ENTRY_KEY]
    async with voting.poll_lock:
        poll = voting.latest_poll(entry_name)
        if poll is None:
            return _json_error("голосование ещё не создано", status=404)
        if isinstance(body.get("crops"), dict):
            voting.set_crops(poll, body["crops"])
            voting.save_poll(poll)

    standings = poll.tally()
    if not standings:
        return _json_error("к голосованию не допущено ни одной работы -- нечего рисовать", status=409)

    # Clamped rather than validated: the page only ever sends 3 or 4, and a nonsense value
    # from anywhere else deserves the default board, not a failed export.
    columns = vote_image.clamp_columns(body.get("columns", vote_image.COLUMNS))
    try:
        path = await asyncio.to_thread(
            vote_image.render_poll_image,
            poll,
            voting.export_image_path(entry_name, poll.poll_id, columns),
            subtitle=_export_subtitle(poll, standings),
            columns=columns,
        )
    except Exception as e:
        request.app[_LOG_KEY](f"[vote_web] rendering the board failed: {e}")
        return _json_error("не получилось нарисовать картинку -- смотри логи сервера", status=500)

    delivered = True
    try:
        await request.app[_EXPORT_KEY](user, poll, path)
    except Exception as e:
        delivered = False
        request.app[_LOG_KEY](f"[vote_web] delivering the board picture failed: {e}")

    request.app[_LOG_KEY](
        f"[vote_web] {voting.display_name(user)} exported the board: {len(standings)} entr(ies), "
        f"{columns} columns, {path.stat().st_size} bytes, delivered={delivered}"
    )
    return web.json_response({
        "ok": True,
        "delivered": delivered,
        "entries": len(standings),
        "columns": columns,
        "bytes": path.stat().st_size,
        # Cache-busted by mtime: the same poll re-exported must not come back as whatever
        # the browser (or Telegram's own proxy) kept from the previous render.
        "url": (
            f"{request.app[_ROUTE_PREFIX_KEY]}/export/{path.name}"
            f"?v={int(path.stat().st_mtime)}"
        ),
    })


async def handle_export_image(request: web.Request) -> web.Response:
    """Serves a rendered board picture by file name. Unauthenticated for the same reason
    the photos are (see the module docstring): it is a collage of pictures already posted
    publicly in the chat, and an <img>/download link cannot carry a signed header.

    Same two-step guard as handle_media -- a strict name pattern AND a containment check
    on the resolved path -- so nothing outside the exports directory is reachable however
    the name is spelled."""
    name = request.match_info["name"]
    if not _SAFE_MEDIA_NAME.match(name or ""):
        raise web.HTTPNotFound()

    directory = voting.export_dir()
    path = (directory / name).resolve()
    if not str(path).startswith(str(directory.resolve())) or not path.is_file():
        raise web.HTTPNotFound()
    # no-store, unlike the photos' long cache: this file is rewritten every time anybody
    # presses "Выгрузить картинку", and a stale one looks exactly like a crop that didn't
    # save.
    return web.FileResponse(path, headers={"Cache-Control": "no-store"})


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


async def handle_avatar(request: web.Request) -> web.Response:
    """Serves an author's current Telegram avatar for a card in this poll.

    The route is public for the same reason as the nominated photos: an ``<img>`` cannot
    attach Mini App initData. It is not an arbitrary Telegram-user lookup, though -- the
    requested id must belong to an entry in the named poll. Successful downloads and
    missing-photo results are cached for the process lifetime, keeping a three-column
    board from asking Telegram for the same face for every viewer.
    """
    poll_id = request.match_info["poll_id"]
    raw_user_id = request.match_info["user_id"]
    if not _SAFE_MEDIA_NAME.match(poll_id or "") or not raw_user_id.isdigit():
        raise web.HTTPNotFound()
    user_id = int(raw_user_id)
    poll = voting.load_poll(request.app[_ENTRY_KEY], poll_id)
    if poll is None or not any(entry.author_id == user_id for entry in poll.entries):
        raise web.HTTPNotFound()

    cache = request.app[_AVATAR_CACHE_KEY]
    if user_id not in cache:
        try:
            avatar = await request.app[_AVATAR_KEY](user_id)
        except Exception as e:
            # A transient Telegram failure is not cached as "this user has no photo": a
            # later page load should be allowed to retry and recover on its own.
            request.app[_LOG_KEY](f"[vote_web] could not fetch avatar for {user_id}: {e}")
            raise web.HTTPServiceUnavailable()
        cache[user_id] = bytes(avatar) if avatar else None
    avatar = cache[user_id]
    if not avatar:
        raise web.HTTPNotFound()
    return web.Response(
        body=avatar, content_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def handle_page(request: web.Request) -> web.Response:
    return web.Response(
        text=PAGE_HTML.replace("__PREFIX__", request.app[_ROUTE_PREFIX_KEY]),
        content_type="text/html",
    )


async def handle_board_page(request: web.Request) -> web.Response:
    """The cropping page. Served to anyone who asks, like the ballot itself -- the page is
    only markup; every request it then makes is authenticated and admin-gated (handle_poll
    with mode=admin, handle_crops, handle_export), and a non-admin who opens the URL gets
    an error where the works would be."""
    return web.Response(
        text=BOARD_HTML.replace("__PREFIX__", request.app[_ROUTE_PREFIX_KEY]),
        content_type="text/html",
    )


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def create_app(
    cfg, entry: str, is_admin, announce=None, route_prefix: str = ROUTE_PREFIX, log=print,
    is_member=None, is_stats_admin=None, export=None, avatar=None, attach=None,
) -> web.Application:
    """`is_admin` is an async callable taking the verified Telegram user dict and
    returning a bool; `announce` is an async callable taking (user, poll, standings) --
    standings being the FULL list of ranked (Entry, votes) pairs, winner first -- that
    delivers the results of a closed vote. Both are supplied by bot_listener.py, which owns
    the Bot API client they need, so this module needs to know nothing about how
    administrators are determined or what finally happens to a set of results.

    `announce` defaults to a no-op so the app is still constructible (e.g. in tests that
    don't exercise closing a vote) without a bot_listener.py running alongside it.

    `is_member` is an async callable, same shape as `is_admin`, answering whether the
    voter currently subscribes. Its result is stored with a ballot and may prompt them to
    join; it never denies a vote. `is_stats_admin` is the narrower owner/chat-admin gate
    for the subscription breakdown and defaults to `is_admin` for standalone callers.

    `export` is an async callable taking (user, poll, path) that delivers a rendered board
    picture to the administrator who asked for it -- same arrangement as `announce`: this
    module draws the picture, bot_listener.py owns the Bot API client that can send it.
    Defaults to a no-op, which leaves the export working (the file is written, and the
    page links to it) minus the copy in the DM.

    `avatar` takes one author id and returns their current Telegram profile photo bytes,
    or None when they have no photo. The results are cached here and exposed only for
    authors who actually belong to the requested poll.

    `attach` is called with the finished application, for mounting something else on the
    same server -- today the arena (arena_web.attach), the second voting system. It runs
    last, so it can only add to what this module has already registered, and this module
    knows nothing about what it adds.
    """
    async def _default_announce(user, poll, standings):
        return None

    async def _default_is_member(user):
        return True

    async def _default_export(user, poll, path):
        return None

    async def _default_avatar(user_id):
        return None

    app = web.Application()
    app[_CFG_KEY] = cfg
    app[_ENTRY_KEY] = entry
    app[_IS_ADMIN_KEY] = is_admin
    app[_IS_STATS_ADMIN_KEY] = is_stats_admin or is_admin
    app[_IS_MEMBER_KEY] = is_member or _default_is_member
    app[_ANNOUNCE_KEY] = announce or _default_announce
    app[_EXPORT_KEY] = export or _default_export
    app[_AVATAR_KEY] = avatar or _default_avatar
    app[_AVATAR_CACHE_KEY] = {}
    app[_ROUTE_PREFIX_KEY] = route_prefix.rstrip("/")
    app[_LOG_KEY] = log

    prefix = app[_ROUTE_PREFIX_KEY]
    app.add_routes([
        web.get("/", handle_health),
        web.get("/health", handle_health),
        web.get(prefix, handle_page),
        web.get(f"{prefix}/", handle_page),
        # Before the api routes only for readability; aiohttp matches on the full path, so
        # /vote/board can never be mistaken for /vote/api/anything.
        web.get(f"{prefix}/board", handle_board_page),
        web.get(f"{prefix}/board/", handle_board_page),
        web.get(f"{prefix}/api/poll", handle_poll),
        web.post(f"{prefix}/api/ballot", handle_ballot),
        web.get(f"{prefix}/api/stats", handle_vote_stats),
        web.post(f"{prefix}/api/moderate", handle_moderate),
        web.post(f"{prefix}/api/crops", handle_crops),
        web.post(f"{prefix}/api/export", handle_export),
        web.post(f"{prefix}/api/announce", handle_announce),
        web.post(f"{prefix}/api/clear", handle_clear),
        web.get(prefix + "/media/{poll_id}/{name}", handle_media),
        web.get(prefix + "/avatar/{poll_id}/{user_id}", handle_avatar),
        web.get(prefix + "/export/{name}", handle_export_image),
    ])
    if attach:
        attach(app)
    return app


async def run_web_server(
    cfg, entry: str, is_admin, port: int, announce=None, log=print, is_member=None,
    is_stats_admin=None, export=None, avatar=None, attach=None,
) -> None:
    """Serves until cancelled, as a sibling task of the two listeners."""
    app = create_app(
        cfg, entry, is_admin, announce=announce, log=log, is_member=is_member,
        is_stats_admin=is_stats_admin, export=export,
        avatar=avatar, attach=attach,
    )
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
  /* Pinned dark rather than themed from the client: the photos on this page are shown
     against the same navy the finished picture is letterboxed with, and a client in
     light mode puts them on white cards under hint-grey text nobody can read. */
  :root {
    color-scheme: dark;
    --bg: #17212b;
    --fg: #f5f5f5;
    --muted: #8a9aa9;
    --card: #232e3c;
    --accent: #3390ec;
    --accent-fg: #fff;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  /* Same trap the cropping page fell into: [hidden] is a UA rule, and .winner's own
     display: flex outranks it -- which is why an empty winner card sat above the grid
     before anything had been announced. */
  [hidden] { display: none !important; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 15px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding-bottom: 96px;
  }
  header { padding: 14px 12px 6px; }
  h1 { font-size: 17px; margin: 0 0 2px; }
  .sub { color: var(--muted); font-size: 13px; }
  /* The browsing view: three columns, the whole poll at a glance. Every cell carries its
     own pick button, so voting never requires opening anything -- opening is for looking
     closely, which is what the reel below is. */
  .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; padding: 12px; }
  .gcard { background: var(--card); border-radius: 10px; overflow: hidden; position: relative; }
  .thumb { position: relative; width: 100%; aspect-ratio: 1; display: block; overflow: hidden;
           background: rgba(128,128,128,.2); }
  .thumb > img.workPhoto { width: 100%; height: 100%; object-fit: cover; display: block; }
  .thumb > img.workPhoto.framed { position: absolute; max-width: none; max-height: none; object-fit: fill; }
  .count { position: absolute; right: 4px; top: 4px; background: rgba(0,0,0,.6);
           color: #fff; font-size: 11px; padding: 1px 5px; border-radius: 8px; }
  .votes { position: absolute; left: 4px; top: 4px; background: var(--accent);
           color: var(--accent-fg); font-size: 11px; padding: 1px 6px; border-radius: 8px; }
  /* The face belongs on the work rather than in the caption: in the three-column collage
     it stays visible while somebody scans photos, and it has its own hit target so a tap
     opens the author instead of the full-size reel. */
  .authorAvatar { position: relative; display: block; width: 34px; height: 34px;
                  border-radius: 50%; overflow: hidden; flex: none; background: var(--card);
                  border: 2px solid rgba(255,255,255,.92); color: #fff;
                  text-decoration: none; box-shadow: 0 1px 5px rgba(0,0,0,.65); }
  .authorAvatar img { position: absolute; inset: 0; width: 100%; height: 100%;
                      object-fit: cover; display: block; }
  .avatarFallback { position: absolute; inset: 0; display: flex; align-items: center;
                    justify-content: center; background: var(--accent); color: #fff;
                    font-size: 14px; font-weight: 700; }
  .gcard .authorAvatar { position: absolute; left: 6px; bottom: 6px; z-index: 3; }
  .gcard .who { padding: 5px 6px 2px; font-size: 11px; overflow: hidden;
                text-overflow: ellipsis; white-space: nowrap; }
  .pick { display: block; width: 100%; border: 0; padding: 7px 4px; font-size: 12px;
          background: transparent; color: var(--muted); cursor: pointer;
          border-top: 1px solid rgba(128,128,128,.25); }
  .gcard.on { outline: 2px solid var(--accent); }
  .gcard.on .pick { background: var(--accent); color: var(--accent-fg); font-weight: 600; }
  .gcard.pending { opacity: .55; }
  .pick[disabled], .pickBtn[disabled] { opacity: .5; cursor: default; }
  .votebar { height: 4px; margin: 3px 6px 0; border-radius: 2px;
             background: rgba(128,128,128,.25); overflow: hidden; }
  .votebar-fill { height: 100%; background: var(--accent); border-radius: 2px; }

  /* Tapping a picture opens the reel: EVERY work in one continuous scroll, full width,
     starting at the one that was tapped. So looking properly at the whole poll is one
     gesture instead of open-look-close, once per entry -- but it stays a deliberate step,
     rather than the grid itself being replaced by a page you must scroll past.
     A fixed overlay rather than a <dialog> so the close button can float over its own
     scrolling content on every browser Telegram embeds. */
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
  .rcard .who { font-size: 13px; font-weight: 600; margin-bottom: 6px;
                display: flex; align-items: flex-start; gap: 6px; }
  .rcard .identityText { min-width: 0; flex: 1; }
  .rcard .authorName, .rcard .authorTag { overflow: hidden; text-overflow: ellipsis;
                                         white-space: nowrap; }
  .rcard .authorTag { color: var(--muted); font-size: 12px; font-weight: 400; }
  .rcard .reelAuthorAvatar { width: 46px; height: 46px; margin: 2px 0 10px; }
  .rcard .cap { white-space: pre-wrap; margin: 0 0 10px; font-size: 14px; }
  .rcard .photos { display: flex; flex-direction: column; gap: 6px; margin-bottom: 10px; }
  /* cursor, because tapping the picture is what closes the reel (see the #feed tap
     handler) -- without it nothing says the biggest thing on the screen is a control. */
  .rcard .photos img { width: 100%; border-radius: 10px; display: block; cursor: pointer; }
  .shot { position: relative; }
  /* An explicit button rather than a zoom GESTURE on the photo itself. Telegram's Android
     WebView does not give the page pinch-to-zoom the way iOS does, which is why only
     Android users reported the pictures as un-zoomable -- and the reflex gesture there,
     double-tap, is already spoken for: one tap on a photo closes the reel. A button
     conflicts with neither, and says out loud that a closer look is available.
     Spelled ⛶ to match the arena's own zoom button (arena_web.py), which solved the same
     problem for the duel view -- two voting systems in one bot should not disagree about
     what "look closer" looks like. */
  .zoomBtn { position: absolute; right: 8px; bottom: 8px; z-index: 2;
             border: 0; border-radius: 50%; width: 36px; height: 36px;
             background: rgba(0,0,0,.55); color: #fff; font-size: 16px; line-height: 1;
             display: flex; align-items: center; justify-content: center; cursor: pointer; }

  /* The lens: one photo, zoomable, over everything else. Its gestures are handled in JS
     rather than left to the browser (touch-action: none) precisely because the browser's
     own zoom is what is missing on Android -- so the same code runs on every client. */
  /* 30, above the vote bar's 20: a photo opened to be looked at closely should not have
     the submit button lying across it. Stated rather than left to source order, which is
     what decides it when two layers claim the same z-index. */
  .lens { position: fixed; inset: 0; z-index: 30; background: #000;
          touch-action: none; overscroll-behavior: contain; }
  .lensStage { position: absolute; inset: 0; overflow: hidden; touch-action: none; }
  .lensStage img { position: absolute; left: 0; top: 0; transform-origin: 0 0;
                   max-width: none; display: block; user-select: none;
                   -webkit-user-drag: none; -webkit-user-select: none; }
  .lensHint { position: fixed; left: 0; right: 0; pointer-events: none;
              bottom: calc(12px + env(safe-area-inset-bottom));
              text-align: center; color: #fff; opacity: .7; font-size: 12px;
              text-shadow: 0 1px 3px rgba(0,0,0,.9); transition: opacity .3s; }
  .lensHint.gone { opacity: 0; }
  .rcard .votebar { margin: 0 0 10px; }
  .votesBadge { margin-left: 6px; background: var(--accent); color: var(--accent-fg);
                font-size: 11px; padding: 1px 6px; border-radius: 8px; }
  .pickBtn { display: block; width: 100%; border: 1px solid rgba(128,128,128,.35);
             border-radius: 8px; padding: 10px; font-size: 14px; font-weight: 600;
             background: transparent; color: var(--fg); cursor: pointer; }
  .rcard.on .pickBtn { background: var(--accent); color: var(--accent-fg); border-color: var(--accent); }
  .rcard.pending { opacity: .6; }
  .bar {
    position: fixed; left: 0; right: 0; bottom: 0; padding: 10px 12px;
    padding-bottom: calc(10px + env(safe-area-inset-bottom));
    background: var(--bg); border-top: 1px solid rgba(128,128,128,.25);
    /* Above the reel: the ballot's submit button stays reachable while browsing it. */
    z-index: 20;
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
  /* Moderation has four independent actions. Keep them compact and two-across so they
     do not cover half the last work on a phone; voter controls retain the full-width
     primary action above. */
  .bar.adminActions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
  .bar.adminActions .go { min-height: 44px; margin: 0; padding: 9px 7px;
                          font-size: 13px; line-height: 1.2; }
  .bar.adminActions .go.danger { font-size: 12px; }
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
  /* Stands in when the vote controls are replaced with a sentence: a locked ballot or a
     closed poll. */
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
  /* One grid for the entire standings table, rather than a flex row per line: sized per
     row, a short name and a long one started their bars at different places, so the
     table read as ragged lines instead of columns. The rank and tally columns are `auto`,
     which sizes them to the widest entry in the whole table -- a three-digit count widens
     the column for everyone instead of clipping, and the digits stay right-aligned under
     each other. The name is capped at a share of the width so a long @username can never
     squeeze the bar away on a phone. The tally is class="num" and not the obvious
     class="count" because that name is already taken by the grid's photo-count badge,
     which is position:absolute -- inherited here, it lifted every number out of its row
     and stacked them all in the corner of the page. */
  .results .table { display: grid; align-items: center; column-gap: 8px; row-gap: 6px;
                    grid-template-columns: auto minmax(0, 38%) auto minmax(0, 1fr) auto; }
  .results .rank { color: var(--muted); font-size: 12px; text-align: right;
                    font-variant-numeric: tabular-nums; }
  .results .name { min-width: 0; overflow: hidden;
                    text-overflow: ellipsis; white-space: nowrap; }
  /* Just enough of the work to recognise it while reading the standings -- who won is the
     question here, not what they drew, and the reel is one tap away for that. Fixed square
     rather than the picture's own ratio, so the bars all still start at the same x.
     class="mini" and not the obvious class="thumb": that one belongs to the grid cell and
     is position:absolute-adjacent, the same trap the tally fell into (see above).
     An entry with no media still emits this cell, empty, because every row must contribute
     the same number of cells or the whole grid shifts by one from there down. */
  .results .mini { width: 22px; height: 22px; border-radius: 4px; display: block;
                    object-fit: cover; background: rgba(128,128,128,.2); }
  .results .track { height: 8px; border-radius: 4px;
                     background: rgba(128,128,128,.2); overflow: hidden; }
  /* display:block, because the fill is a span: width and height do nothing on an inline
     box, so without this the bar was a grey track with an invisible zero-sized fill in it.
     The track escapes the same fate only by being a grid item, which blockifies it. */
  .results .fill { display: block; height: 100%; background: var(--accent); border-radius: 4px; }
  .results .num { text-align: right; font-size: 12px; color: var(--muted);
                   font-variant-numeric: tabular-nums; }
  .modal { position: fixed; inset: 0; z-index: 50; display: flex; align-items: center;
           justify-content: center; padding: 20px; background: rgba(0,0,0,.68); }
  .modalCard { width: min(100%, 420px); max-height: calc(100vh - 40px); overflow-y: auto;
               padding: 20px; border-radius: 14px; background: var(--card); }
  .modalCard h2 { margin: 0 0 10px; font-size: 18px; }
  .modalCard p { margin: 0 0 16px; white-space: pre-line; }
  .modalActions { display: flex; gap: 8px; }
  .modalActions button, .modalActions a { flex: 1; border: 0; border-radius: 9px; padding: 11px;
      text-align: center; font: inherit; font-weight: 600; text-decoration: none; cursor: pointer;
      background: var(--accent); color: var(--accent-fg); }
  .modalActions .cancel { background: transparent; border: 1px solid rgba(128,128,128,.5); color: var(--fg); }
  .chart { margin: 16px 0; }
  .chart h3 { margin: 0 0 8px; font-size: 14px; }
  .chartRow { display: grid; grid-template-columns: 84px minmax(0, 1fr) auto; gap: 8px;
              align-items: center; margin: 6px 0; font-size: 12px; }
  .chartLabel { color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .chartTrack { height: 12px; overflow: hidden; border-radius: 6px; background: rgba(128,128,128,.25); }
  .chartFill { display: block; height: 100%; border-radius: 6px; background: var(--accent); }
  .chart.subscribers .chartFill { background: #46b96b; }
  .chart.nonSubscribers .chartFill { background: #e2a23a; }
  .statsWeek { display: block; width: 100%; margin: 0 0 8px; padding: 9px 10px;
               border: 1px solid rgba(128,128,128,.45); border-radius: 8px;
               background: var(--bg); color: var(--fg); font: inherit; }
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
<div class="grid" id="grid"></div>
<div class="msg" id="msg" hidden></div>
<div class="results" id="results" hidden></div>
<div class="bar" id="bar">
  <button class="go secondary" id="statsButton" hidden>📊 Статистика</button>
  <button class="go danger" id="clear" hidden>🗑 Очистить голосование</button>
  <button class="go secondary" id="announce" hidden>Подвести итоги</button>
  <button class="go" id="go" disabled>Загружаю…</button>
</div>

<div class="reel" id="reel" hidden>
  <button class="reelClose" id="reelClose" aria-label="Закрыть">✕</button>
  <div class="feed" id="feed"></div>
</div>

<div class="lens" id="lens" hidden>
  <div class="lensStage" id="lensStage"><img id="lensImg" alt=""></div>
  <button class="reelClose" id="lensClose" aria-label="Закрыть">✕</button>
  <div class="lensHint" id="lensHint">Щипок или двойное касание — приблизить</div>
</div>

<div class="modal" id="subscribePrompt" hidden>
  <div class="modalCard" role="dialog" aria-modal="true" aria-labelledby="subscribeTitle">
    <h2 id="subscribeTitle">Подпишитесь, чтобы подтвердить голос</h2>
    <p>С нами уже 500 художников по миниатюрам и не только</p>
    <div class="modalActions">
      <button class="cancel" id="subscribeClose">Позже</button>
      <a id="subscribeLink" href="https://t.me/papkahudojnicov">Подписаться</a>
    </div>
  </div>
</div>

<div class="modal" id="voteStats" hidden>
  <div class="modalCard" role="dialog" aria-modal="true" aria-labelledby="voteStatsTitle">
    <h2 id="voteStatsTitle">Статистика голосования</h2>
    <select class="statsWeek" id="voteStatsWeek" aria-label="Неделя голосования"></select>
    <div id="voteStatsCharts"></div>
    <div class="modalActions"><button class="cancel" id="voteStatsClose">Закрыть</button></div>
  </div>
</div>

<script>
const PREFIX = "__PREFIX__";
/* The header and the overscroll strip belong to the client, not to our CSS, so on a
   light-themed phone they stay white around our dark page unless asked. Each setter
   landed in a different Bot API version; a client too old to answer keeps its own. */
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

let poll = null;
let picked = new Set();     // entry ids the voter chose (server-confirmed when allow_revote)
let admitted = new Set();   // entry ids the admin admits (admin mode only)
let confirmTimers = [];     // pending timeouts for the fading "Голос принят" banner

const $ = (id) => document.getElementById(id);

// The bottom bar's height varies with how many buttons are actually showing (1 for a
// voter, up to 3 stacked for the admin's moderation view, or none once an allow_revote
// voter has nothing left to press) -- a fixed body padding sized for one button left the
// grid's last row hidden behind the bar whenever more (or fewer) buttons appeared.
// Tracked with a ResizeObserver instead of recomputed by hand after every place the bar's
// contents change, so it can't be missed. (id="bar" matters: observe(null) throws.)
// Published as --barH too, because the reel scrolls independently of the body and needs
// the same allowance at its own bottom.
if (window.ResizeObserver) {
  new ResizeObserver((entries) => {
    const height = entries[0].contentRect.height;
    document.documentElement.style.setProperty("--barH", height + "px");
    document.body.style.paddingBottom = (height + 16) + "px";
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

function avatarInitial(entry) {
  const value = String(entry.author || entry.username || "?").trim();
  return value ? Array.from(value)[0].toUpperCase() : "?";
}

function avatarHtml(entry, extraClass) {
  const classes = "authorAvatar" + (extraClass ? " " + extraClass : "");
  const label = "Открыть профиль " + (entry.author || who(entry));
  const picture =
    '<span class="avatarFallback">' + esc(avatarInitial(entry)) + "</span>" +
    (entry.avatar
      ? '<img loading="lazy" src="' + esc(entry.avatar) + '" alt="" ' +
        'onerror="this.hidden=true">'
      : "");
  if (!entry.profile_url) return '<span class="' + classes + '">' + picture + "</span>";
  return '<a class="' + classes + '" href="' + esc(entry.profile_url) +
    '" data-profile="' + esc(entry.profile_url) + '" aria-label="' + esc(label) + '">' +
    picture + "</a>";
}

function authorIdentityHtml(entry) {
  const tag = entry.username ? "@" + entry.username : "без @username";
  return '<div class="identityText"><div class="authorName">' + esc(entry.author) +
    '</div><div class="authorTag">' + esc(tag) + "</div></div>";
}

function openAuthorProfile(url) {
  if (!url) return;
  if (url.startsWith("https://t.me/") && tg && tg.openTelegramLink) {
    tg.openTelegramLink(url);
  } else {
    window.location.href = url;
  }
}

function showSubscribePrompt() {
  $("subscribePrompt").hidden = false;
}

function renderVoteChart(title, className, weeks, field) {
  const max = Math.max(1, ...weeks.map((week) => week[field] || 0));
  return '<section class="chart ' + className + '"><h3>' + esc(title) + '</h3>' +
    weeks.map((week) => {
      const value = week[field] || 0;
      return '<div class="chartRow"><span class="chartLabel">' + esc(week.week) +
        '</span><span class="chartTrack"><span class="chartFill" style="width:' +
        Math.round(100 * value / max) + '%"></span></span><span>' + value + '</span></div>';
    }).join('') + '</section>';
}

let voteStatsWeeks = [];

function renderSelectedVoteStats() {
  const selectedWeek = $("voteStatsWeek").value;
  const week = voteStatsWeeks.find((item) => item.week === selectedWeek);
  if (!week) return;
  $("voteStatsCharts").innerHTML =
    renderVoteChart('Все голоса', 'allVotes', [week], 'voters') +
    renderVoteChart('Голоса подписчиков на момент голосования', 'subscribers', [week], 'subscribers') +
    renderVoteChart('Подписались после голосования', 'subscribers', [week], 'subscribed_after') +
    renderVoteChart('Не подписались', 'nonSubscribers', [week], 'not_subscribed');
}

async function showVoteStats() {
  const response = await api('/api/stats');
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Не удалось загрузить статистику');
  voteStatsWeeks = data.weeks || [];
  const selector = $("voteStatsWeek");
  selector.hidden = voteStatsWeeks.length === 0;
  selector.innerHTML = voteStatsWeeks.map((week) =>
    '<option value="' + esc(week.week) + '">' + esc(week.week) + '</option>'
  ).join('');
  if (voteStatsWeeks.length) {
    selector.value = voteStatsWeeks[voteStatsWeeks.length - 1].week;
    renderSelectedVoteStats();
  } else {
    $("voteStatsCharts").innerHTML = '<p>Пока нет сохранённых голосов.</p>';
  }
  $("voteStats").hidden = false;
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
    // Five cells per entry, straight into one grid with no per-row wrapper -- a wrapper
    // would make every line its own formatting context again, which is what stopped the
    // columns lining up. An entry without a picture contributes an empty cell rather than
    // skipping it, since a row one cell short would slide every row after it sideways.
    '<div class="table">' +
    poll.results.map((r, i) =>
      '<span class="rank">' + (i + 1) + "</span>" +
      '<span class="name">' + esc(who(r)) + "</span>" +
      (r.photo
        ? '<img class="mini" loading="lazy" src="' + esc(r.photo) + '" alt="">'
        : '<span class="mini"></span>') +
      '<span class="track"><span class="fill" style="width:' +
        Math.round(100 * r.votes / max) + '%"></span></span>' +
      '<span class="num">' + r.votes + "</span>"
    ).join("") +
    "</div>";
}

function updateAdminButtons() {
  const announce = $("announce");
  const clear = $("clear");
  const statsButton = $("statsButton");
  $("bar").classList.toggle("adminActions", !!poll.is_admin);
  if (!poll.is_admin) {
    announce.hidden = true;
    clear.hidden = true;
    statsButton.hidden = true;
    return;
  }
  statsButton.hidden = !poll.can_view_vote_stats;
  statsButton.disabled = !poll.can_view_vote_stats;
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

// `short` is the grid's version: a cell one third of the screen wide cannot hold
// "Голос учтён ✓", and shouting it in three columns would be noise anyway.
function pickLabel(id, short) {
  if (poll.is_admin) {
    if (admitted.has(id)) return short ? "допущена" : "Допущена";
    return short ? "допустить" : "Допустить";
  }
  const accepted = poll.allow_revote
    ? picked.has(id)
    : ballotLocked() && (poll.my_vote || []).includes(id);
  if (accepted) return short ? "✓ учтён" : "Голос учтён ✓";
  if (!poll.allow_revote && !ballotLocked() && picked.has(id)) return short ? "выбрано" : "Выбрано";
  return short ? "выбрать" : "Выбрать";
}

function picksDisabled() {
  return false;
}

// The admin's vote bar is relative to the currently leading entry, not to the voter
// count, so it stays readable in a poll with only a handful of ballots in so far.
function voteBarHtml(entry, maxCount) {
  if (!poll.is_admin) return "";
  const count = poll.counts ? (poll.counts[entry.id] || 0) : 0;
  return '<div class="votebar"><div class="votebar-fill" style="width:' +
    Math.round(100 * count / maxCount) + '%"></div></div>';
}

function maxAdminCount() {
  return poll.is_admin && poll.counts ? Math.max(1, ...Object.values(poll.counts)) : 1;
}

// Crops are square coordinates in the source image, just like the editor and the export
// renderer use. A crop can extend outside the photo for a fitted, letterboxed frame, so
// this cannot use object-position: size and offset both come from the saved square.
function applyFrame(img) {
  const crop = img.crop;
  const frame = img.closest(".thumb");
  if (!crop || !frame || !img.naturalWidth || !img.naturalHeight || !Number(crop.size)) return;
  const scale = frame.clientWidth / Number(crop.size);
  if (!Number.isFinite(scale) || scale <= 0) return;
  img.classList.add("framed");
  img.style.width = (img.naturalWidth * scale) + "px";
  img.style.height = (img.naturalHeight * scale) + "px";
  img.style.left = (-Number(crop.x) * scale) + "px";
  img.style.top = (-Number(crop.y) * scale) + "px";
}

function applyEntryFrame(img, crop) {
  if (!crop) return;
  img.crop = crop;
  if (img.complete) applyFrame(img);
  else img.addEventListener("load", () => applyFrame(img), { once: true });
}

window.addEventListener("resize", () => {
  document.querySelectorAll(".thumb > img.workPhoto.framed").forEach(applyFrame);
});

function renderGrid() {
  const grid = $("grid");
  grid.innerHTML = "";
  const maxCount = maxAdminCount();
  const disabled = picksDisabled() ? " disabled" : "";

  for (const entry of poll.entries) {
    const card = document.createElement("div");
    card.className = "card gcard";
    // Also on the grid cell, not just the reel card: closing the reel scrolls back to the
    // cell for whatever entry was being read (see closeReelAt).
    card.dataset.entry = entry.id;
    if (isChosen(entry.id)) card.classList.add("on");
    if (poll.is_admin && !admitted.has(entry.id)) card.classList.add("pending");

    const count = poll.is_admin && poll.counts ? (poll.counts[entry.id] || 0) : 0;
    const votes = count ? '<span class="votes">' + count + "</span>" : "";
    const more = entry.photos.length > 1
      ? '<span class="count">+' + (entry.photos.length - 1) + "</span>" : "";

    card.innerHTML =
      '<div class="thumb" data-open="' + esc(entry.id) + '" role="button">' +
        '<img class="workPhoto" loading="lazy" src="' + esc(entry.photos[0]) + '" alt="">' +
        more + votes + avatarHtml(entry) +
      "</div>" +
      voteBarHtml(entry, maxCount) +
      '<div class="who">' + esc(who(entry)) + "</div>" +
      '<button class="pick" data-pick="' + esc(entry.id) + '"' + disabled + ">" +
        pickLabel(entry.id, true) +
      "</button>";
    grid.appendChild(card);
    applyEntryFrame(card.querySelector(".thumb > img.workPhoto"), entry.crop);
  }
}

function renderReel() {
  const feed = $("feed");
  feed.innerHTML = "";
  const maxCount = maxAdminCount();
  const disabled = picksDisabled() ? " disabled" : "";

  for (const entry of poll.entries) {
    const card = document.createElement("div");
    card.className = "card rcard";
    card.dataset.entry = entry.id;
    if (isChosen(entry.id)) card.classList.add("on");
    if (poll.is_admin && !admitted.has(entry.id)) card.classList.add("pending");

    const count = poll.is_admin && poll.counts ? (poll.counts[entry.id] || 0) : 0;
    const votes = count ? '<span class="votesBadge">' + count + "</span>" : "";

    card.innerHTML =
      '<div class="who">' + authorIdentityHtml(entry) + votes + "</div>" +
      avatarHtml(entry, "reelAuthorAvatar") +
      (entry.text ? '<div class="cap">' + esc(entry.text) + "</div>" : "") +
      '<div class="photos">' +
        entry.photos.map((p) =>
          '<div class="shot"><img loading="lazy" src="' + esc(p) + '" alt="">' +
          '<button type="button" class="zoomBtn" aria-label="Увеличить" ' +
          'data-zoom="' + esc(p) + '">⛶</button></div>'
        ).join("") +
      "</div>" +
      voteBarHtml(entry, maxCount) +
      '<button class="pickBtn" data-pick="' + esc(entry.id) + '"' + disabled + ">" +
        pickLabel(entry.id) +
      "</button>";
    feed.appendChild(card);
  }
}

// Repaints only what a pick changes -- the button labels and the chosen/pending classes,
// in the grid and the reel alike. Deliberately NOT a re-render: rebuilding the reel's
// innerHTML would send it back to the top, so voting on the fifth work would throw the
// reader out of their place every time.
function syncPicks() {
  const disabled = picksDisabled();
  for (const button of document.querySelectorAll("[data-pick]")) {
    const id = button.dataset.pick;
    button.textContent = pickLabel(id, button.classList.contains("pick"));
    button.disabled = disabled;
    const card = button.closest(".card");
    if (!card) continue;
    card.classList.toggle("on", isChosen(id));
    if (poll.is_admin) card.classList.toggle("pending", !admitted.has(id));
  }
  updateButton();
}

function render() {
  renderWinnerBanner();
  renderResults();
  updateAdminButtons();
  if (!poll.entries.length) {
    $("grid").innerHTML = "";
    $("feed").innerHTML = "";
    closeReel();
    $("msg").hidden = false;
    $("msg").textContent = poll.is_admin
      ? "За сегодня и вчера заявок с #итогинедели не нашлось."
      : "Работы ещё не допущены к голосованию. Загляни позже.";
    $("go").hidden = true;
    $("notice").hidden = true;
    return;
  }
  $("msg").hidden = true;
  // Kept across the rebuild: a full render can happen while the reel is open (saving
  // moderation settings does one), and losing the reader's place is exactly what
  // syncPicks exists to avoid on the far more common path.
  const reelScroll = $("reel").scrollTop;
  renderGrid();
  renderReel();
  $("reel").scrollTop = reelScroll;
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
  poll.is_subscriber = data.is_subscriber;
  if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
  showConfirmBanner();
  if (data.is_subscriber === false && data.my_vote.length) showSubscribePrompt();
}

// Opens the reel at the tapped work. Every entry is in it, so from here the rest of the
// poll is just more scrolling -- which is the point: one gesture in, then nothing to
// close and reopen per work.
function openReel(id) {
  const reel = $("reel");
  reel.hidden = false;
  document.body.classList.add("reelOpen");
  // Scanned rather than matched with an attribute selector: esc() escapes for HTML, not
  // for CSS, so an id needing either kind of quoting would silently find nothing.
  const target = [...$("feed").children].find((card) => card.dataset.entry === id);
  // scrollTop first: scrollIntoView on the first card is a no-op if the reel is already
  // scrolled from a previous open, and the reader would land wherever they left off.
  reel.scrollTop = 0;
  if (target) target.scrollIntoView({ block: "start" });
  if (tg && tg.BackButton) tg.BackButton.show();
}

function closeReel() {
  // The lens sits on top of the reel, so it goes first -- otherwise a render that empties
  // the poll (or any other closeReel from code) would leave a photo floating over a page
  // with nothing behind it. Called while the reel is still visible, so closeLens leaves
  // the body lock and the back arrow to the lines below.
  if (!$("lens").hidden) closeLens();
  $("reel").hidden = true;
  document.body.classList.remove("reelOpen");
  if (tg && tg.BackButton) tg.BackButton.hide();
}

// Closing from inside the feed puts the grid back under the entry that was being read,
// so a look through the reel doesn't cost the reader their place in the poll. Wraps
// closeReel rather than replacing it, both because the ✕ and Telegram's back arrow have
// no entry to return to and because the BackButton bookkeeping must stay in one place.
function closeReelAt(entryId) {
  closeReel();
  const card = [...$("grid").children].find((c) => c.dataset.entry === entryId);
  if (card) card.scrollIntoView({ block: "center" });
}

// ---------------------------------------------------------------------------- the lens
//
// One photo, as large as it will go, with zoom and pan of our own.
//
// Not the browser's zoom: Telegram's Android WebView does not offer the page pinch-to-zoom
// that iOS does, so on Android the pictures simply could not be examined -- reported from
// the chat, 2026-08-10. Handling the gestures here means every client behaves the same,
// and it costs nothing on the ones where pinch already worked.
//
// Geometry: the image is positioned at its natural size with transform-origin 0 0 and
// moved by `translate(x, y) scale(s)`. Zooming about a point keeps whatever is under the
// fingers under the fingers -- solve `px = x + u*s` for the image coordinate u and put it
// back at the new scale.
const LENS_MAX_ZOOM = 8;     // times the fit-to-screen scale
const LENS_TAP_ZOOM = 3;     // what a double tap jumps to
const LENS_TAP_MS = 260;     // wait this long for a second tap before treating one as one
let lens = { scale: 1, fit: 1, x: 0, y: 0 };
let lensPointers = new Map();
let lensPinch = null;
let lensTapTimer = null;

function lensViewport() {
  const stage = $("lensStage");
  return { w: stage.clientWidth, h: stage.clientHeight };
}

function lensApply() {
  const view = lensViewport();
  const img = $("lensImg");
  const width = img.naturalWidth * lens.scale;
  const height = img.naturalHeight * lens.scale;
  // Centred while it fits, held inside the edges once it doesn't -- so a pan can never
  // fling the picture off the screen and leave the viewer looking at black.
  lens.x = width <= view.w ? (view.w - width) / 2 : Math.min(0, Math.max(view.w - width, lens.x));
  lens.y = height <= view.h ? (view.h - height) / 2 : Math.min(0, Math.max(view.h - height, lens.y));
  img.style.transform = "translate(" + lens.x + "px," + lens.y + "px) scale(" + lens.scale + ")";
}

function lensFit() {
  const view = lensViewport();
  const img = $("lensImg");
  if (!img.naturalWidth || !img.naturalHeight) return;
  lens.fit = Math.min(view.w / img.naturalWidth, view.h / img.naturalHeight);
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
  $("lensHint").classList.remove("gone");
  document.body.classList.add("reelOpen");
  img.style.transform = "";
  img.src = src;
  if (img.complete && img.naturalWidth) lensFit();
  else img.addEventListener("load", lensFit, { once: true });
  // The hint has said its piece by the time anyone has looked at the picture.
  setTimeout(() => $("lensHint").classList.add("gone"), 2500);
  if (tg && tg.BackButton) tg.BackButton.show();
}

function closeLens() {
  $("lens").hidden = true;
  $("lensImg").removeAttribute("src");
  lensPointers.clear();
  lensPinch = null;
  // The reel is still open underneath, and it wants both the body lock and the back arrow.
  if (!$("reel").hidden) return;
  document.body.classList.remove("reelOpen");
  if (tg && tg.BackButton) tg.BackButton.hide();
}

const lensStageEl = $("lensStage");
lensStageEl.addEventListener("pointerdown", (event) => {
  lensStageEl.setPointerCapture(event.pointerId);
  lensPointers.set(event.pointerId, { x: event.clientX, y: event.clientY,
                                      startX: event.clientX, startY: event.clientY,
                                      at: Date.now() });
  if (lensPointers.size === 2) {
    const [a, b] = [...lensPointers.values()];
    lensPinch = {
      distance: Math.hypot(a.x - b.x, a.y - b.y) || 1,
      scale: lens.scale,
    };
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
    lensZoomTo(lensPinch.scale * (distance / lensPinch.distance),
               (a.x + b.x) / 2, (a.y + b.y) / 2);
    return;
  }
  // One finger pans, but only when there is something to pan: at fit scale the whole
  // picture is already on screen, and dragging it around would just be a way to lose it.
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
  if (moved > 10 || Date.now() - pointer.at > 600) return;   // a drag, not a tap

  if (lensTapTimer) {           // the second tap of a double
    clearTimeout(lensTapTimer);
    lensTapTimer = null;
    const zoomed = lens.scale > lens.fit * 1.05;
    if (zoomed) lensFit();
    else lensZoomTo(lens.fit * LENS_TAP_ZOOM, pointer.x, pointer.y);
    return;
  }
  lensTapTimer = setTimeout(() => {
    lensTapTimer = null;
    // A single tap closes -- but only when nothing is magnified. Mid-examination it is far
    // more likely to be a misplaced finger than a request to leave.
    if (lens.scale <= lens.fit * 1.05) closeLens();
  }, LENS_TAP_MS);
}
lensStageEl.addEventListener("pointerup", lensPointerDone);
lensStageEl.addEventListener("pointercancel", lensPointerDone);

// Desktop: the wheel zooms about the cursor, since there are no fingers to pinch with.
lensStageEl.addEventListener("wheel", (event) => {
  event.preventDefault();
  lensZoomTo(lens.scale * (event.deltaY < 0 ? 1.15 : 1 / 1.15), event.clientX, event.clientY);
}, { passive: false });

window.addEventListener("resize", () => { if (!$("lens").hidden) lensFit(); });
$("lensClose").addEventListener("click", closeLens);

// Telegram's own back arrow closes the reel too -- on a phone that is the gesture people
// reach for first, and without this it would close the whole Mini App instead. With the
// lens open it takes that first: one arrow, one step back, whichever layer is on top.
function goBack() {
  if (!$("lens").hidden) closeLens();
  else closeReel();
}
if (tg && tg.BackButton) tg.BackButton.onClick(goBack);
$("reelClose").addEventListener("click", closeReel);

// A tap on a picture closes the reel as well: while reading down the feed the picture is
// the whole screen, and the ✕ in the corner is the awkward way back to the grid. Only
// pictures -- the vote button and everything else in a card keep their own handling.
//
// A scroll that comes to rest on a picture must not dismiss the feed under the reader,
// and a `click` alone cannot tell the two apart on the desktop clients, where dragging a
// scrollbar or the mouse still ends in one. So the gesture is measured from pointerdown:
// the finger has to come back up near where it went down, soon enough to be a tap. When
// there is no pointerdown to measure (a keyboard activation, a client without pointer
// events) the click is taken at face value.
const TAP_SLOP = 10;    // px of drift still read as a tap rather than a drag
const TAP_MS = 600;     // longer is a press or a stalled scroll, not a tap
let reelTap = null;

$("feed").addEventListener("pointerdown", (event) => {
  reelTap = event.target.tagName === "IMG"
    ? { x: event.clientX, y: event.clientY, at: Date.now(), target: event.target }
    : null;
});
// A gesture the browser took over (the touch became a scroll) is remembered as cancelled
// rather than simply forgotten: forgotten, it would be indistinguishable from "no
// pointerdown to measure", and any click that still followed would read as a tap.
$("feed").addEventListener("pointercancel", () => { reelTap = { cancelled: true }; });

$("feed").addEventListener("click", (event) => {
  // An avatar is also an IMG, but its tap belongs to the profile link and must not close
  // the expanded work as if the work photo itself had been tapped.
  if (event.target.closest("[data-profile]")) { reelTap = null; return; }
  // The magnifier first: it sits ON the photo, and tapping a photo closes the reel.
  const zoom = event.target.closest("[data-zoom]");
  if (zoom) {
    event.preventDefault();
    reelTap = null;
    openLens(zoom.dataset.zoom);
    return;
  }
  if (event.target.tagName !== "IMG") return;
  const start = reelTap;
  reelTap = null;
  if (start) {
    if (start.cancelled || start.target !== event.target) return;
    if (Date.now() - start.at > TAP_MS) return;
    if (Math.abs(event.clientX - start.x) > TAP_SLOP) return;
    if (Math.abs(event.clientY - start.y) > TAP_SLOP) return;
  }
  const card = event.target.closest("[data-entry]");
  closeReelAt(card && card.dataset.entry);
});

function toggleAdmitted(id) {
  const adding = !admitted.has(id);
  if (adding) admitted.add(id); else admitted.delete(id);
  if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
  syncPicks();
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
  syncPicks();
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
  syncPicks();
  renderResults();
}

async function onPickTap(id) {
  if (poll.is_admin) { toggleAdmitted(id); return; }
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
  const profile = event.target.closest("[data-profile]");
  if (profile) {
    event.preventDefault();
    openAuthorProfile(profile.dataset.profile);
    return;
  }
  const open = event.target.closest("[data-open]");
  if (open) { event.preventDefault(); openReel(open.dataset.open); return; }
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

$("subscribeClose").addEventListener("click", () => { $("subscribePrompt").hidden = true; });
$("subscribeLink").addEventListener("click", (event) => {
  event.preventDefault();
  const url = event.currentTarget.href;
  if (tg && tg.openTelegramLink) tg.openTelegramLink(url);
  else window.location.href = url;
});
$("voteStatsClose").addEventListener("click", () => { $("voteStats").hidden = true; });
$("statsButton").addEventListener("click", async () => {
  try {
    await showVoteStats();
  } catch (e) {
    alert(String(e.message || e));
  }
});

$("announce").addEventListener("click", async () => {
  const button = $("announce");
  if (!confirm(
    "Закрыть голосование и подвести итоги? Дальше голосовать будет нельзя. " +
    "Итоги придут черновиком в личку -- в чат ничего не уйдёт без твоего подтверждения."
  )) return;
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
    // Nothing has been posted anywhere at this point: the bot only puts a DRAFT of the
    // results in the admin's DM, and the chat sees them once "Отправить" is pressed there.
    // The wording has to say so, or the admin walks away thinking the job is done.
    button.textContent = data.notified
      ? "Голосование закрыто. Черновик итогов ждёт в личке -- проверь и отправь"
      : "Победитель выбран, но черновик не отправился -- смотри логи";
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
    poll = { poll_id: null, entries: [], is_admin: true, can_moderate: true, open: false, is_subscriber: true };
    picked = new Set();
    admitted = new Set();
    $("winnerBanner").hidden = true;
    $("settings").hidden = true;
    $("notice").hidden = true;
    $("results").hidden = true;
    $("confirmBanner").hidden = true;
    $("announce").hidden = true;
    $("grid").innerHTML = "";
    $("feed").innerHTML = "";
    closeReel();
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


# The cropping page. Same board the export draws -- three columns, square thumbnail, the
# author underneath -- except every thumbnail here is live: tapping one opens a big editor
# where the photo can be dragged, pinched and zoomed inside its square, and the grid behind
# it is the preview. What is on screen IS what renders, which is the only honest way to
# offer cropping: a slider that promises a result you cannot see until the file arrives is
# worse than no cropping at all.
#
# The crop is stored as a square in the PHOTO's own pixel coordinates (see
# voting.Poll.crops), not as CSS: the browser and Pillow agree on those, whatever size
# either happens to be displaying the picture at. A square that hangs off the edge of the
# photo is how "fit the whole thing" is expressed, so fitted and cropped are one
# representation with one renderer, not two modes.
BOARD_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Кадрирование итогов</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  /* Hardcoded rather than themed, the whole palette for --thumb-bg's reason: the picture
     that comes out has this colour baked into it, so the preview must show it even to
     somebody running a light Telegram theme -- and a preview framed in white while the
     photo inside it is letterboxed navy is not a preview of anything. */
  :root {
    color-scheme: dark;
    --bg: #17212b;
    --fg: #f5f5f5;
    --muted: #8a9aa9;
    --card: #232e3c;
    --accent: #3390ec;
    --accent-fg: #fff;
    /* The letterbox colour vote_image.py paints behind a fitted photo. */
    --thumb-bg: #1a2532;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  /* [hidden] is only a UA rule (display: none), so ANY author rule that sets display --
     .editor and .tools both do, to lay themselves out -- silently wins over it and the
     element is never hidden at all. That is what pinned the crop editor permanently over
     the grid, showing one empty frame and no photos. !important, because the whole point
     is to beat every other display in this sheet. */
  [hidden] { display: none !important; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 15px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding-bottom: 130px;
  }
  header { padding: 14px 12px 6px; }
  h1 { font-size: 17px; margin: 0 0 2px; }
  .sub { color: var(--muted); font-size: 13px; }
  .tools { display: flex; gap: 8px; padding: 8px 12px 0; flex-wrap: wrap; align-items: center; }
  .tools .spacer { flex: 1; }
  .tools .label { color: var(--muted); font-size: 12px; }
  .chip { border: 1px solid rgba(128,128,128,.4); background: transparent; color: var(--fg);
          border-radius: 8px; padding: 6px 10px; font-size: 12px; cursor: pointer; }
  .chip.on { background: var(--accent); color: var(--accent-fg); border-color: var(--accent); }
  /* The export's own geometry, in CSS: the same number of columns it will render with
     (--cols, set from the chips), and a caption block tall enough for both the name and
     the @tag so a card without a tag is still the same height as one with -- exactly what
     vote_image.CAPTION_HEIGHT is a fixed constant for. */
  .grid { display: grid; grid-template-columns: repeat(var(--cols, 3), 1fr);
          gap: 8px; padding: 12px; }
  .gcard { background: var(--card); border-radius: 10px; overflow: hidden; cursor: pointer; }
  .frame { position: relative; width: 100%; aspect-ratio: 1; overflow: hidden;
           background: var(--thumb-bg); }
  .frame img { position: absolute; left: 0; top: 0; max-width: none; display: block;
               /* Nothing here is meaningful until its crop has been applied, and that
                  needs the natural size, which only arrives with the load event. */
               visibility: hidden; }
  .frame img.ready { visibility: visible; }
  .badge { position: absolute; left: 5px; top: 5px; background: var(--accent);
           color: var(--accent-fg); font-size: 11px; padding: 1px 6px; border-radius: 8px; }
  .edit { position: absolute; right: 5px; bottom: 5px; background: rgba(0,0,0,.6);
          color: #fff; font-size: 11px; padding: 2px 6px; border-radius: 8px; }
  /* A photo that never arrives must SAY so: an empty frame is indistinguishable from a
     page that hasn't finished loading, and there is nothing to crop either way. */
  .failed { position: absolute; inset: 0; display: flex; align-items: center;
            justify-content: center; text-align: center; padding: 6px;
            color: var(--muted); font-size: 11px; }
  .who { padding: 6px 7px 8px; font-size: 11px; line-height: 1.25; }
  .who .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .who .tag { color: var(--muted); overflow: hidden; text-overflow: ellipsis;
              white-space: nowrap; display: block; min-height: 14px; }
  .msg { padding: 24px 16px; color: var(--muted); text-align: center; }
  .bar { position: fixed; left: 0; right: 0; bottom: 0; padding: 10px 12px;
         padding-bottom: calc(10px + env(safe-area-inset-bottom));
         background: var(--bg); border-top: 1px solid rgba(128,128,128,.25); z-index: 20; }
  .status { font-size: 12px; color: var(--muted); text-align: center; margin-bottom: 8px;
            min-height: 16px; }
  .status a { color: var(--accent); }
  .go { width: 100%; border: 0; border-radius: 10px; padding: 14px; font-size: 16px;
        font-weight: 600; background: var(--accent); color: var(--accent-fg); cursor: pointer; }
  .go[disabled] { opacity: .5; cursor: default; }
  .go.secondary { background: transparent; color: var(--accent);
                  border: 1px solid var(--accent); margin-bottom: 8px; }

  /* The editor: one work, as big as the screen allows. A fixed overlay rather than a
     <dialog>, same as the ballot's reel -- it has to work in every browser Telegram
     embeds, including the ones without dialog support. */
  .editor { position: fixed; inset: 0; z-index: 30; background: var(--bg);
            display: flex; flex-direction: column; padding: 12px; overflow-y: auto;
            padding-bottom: calc(12px + env(safe-area-inset-bottom)); }
  body.editing { overflow: hidden; }
  .etop { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
  .etop .ewho { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis;
                white-space: nowrap; font-weight: 600; }
  .eclose { border: 0; border-radius: 50%; width: 34px; height: 34px; flex: none;
            background: rgba(128,128,128,.25); color: var(--fg); font-size: 16px;
            cursor: pointer; }
  .stagewrap { width: 100%; max-width: 460px; margin: 0 auto; }
  /* touch-action: none, or the first drag becomes a page scroll and the photo never moves
     -- on a phone that reads as the editor being broken. */
  .stage { position: relative; width: 100%; aspect-ratio: 1; overflow: hidden;
           background: var(--thumb-bg); border-radius: 12px; touch-action: none;
           cursor: grab; user-select: none; }
  .stage.dragging { cursor: grabbing; }
  .stage img { position: absolute; left: 0; top: 0; max-width: none; display: block;
               pointer-events: none; -webkit-user-drag: none; }
  /* The thirds, over the photo: without them "centred" is guesswork, since the square
     being framed has no edges of its own once it fills the screen. */
  .thirds { position: absolute; inset: 0; pointer-events: none; }
  .thirds i { position: absolute; background: rgba(255,255,255,.22); }
  .thirds i.v { top: 0; bottom: 0; width: 1px; }
  .thirds i.h { left: 0; right: 0; height: 1px; }
  .zoomrow { display: flex; align-items: center; gap: 10px; margin: 12px auto 0;
             max-width: 460px; width: 100%; }
  .zoomrow input[type="range"] { flex: 1; accent-color: var(--accent); }
  .zoomrow button { border: 1px solid rgba(128,128,128,.4); background: transparent;
                    color: var(--fg); border-radius: 8px; width: 36px; height: 32px;
                    font-size: 17px; cursor: pointer; }
  .erow { display: flex; gap: 8px; margin: 12px auto 0; max-width: 460px; width: 100%; }
  .erow button { flex: 1; border: 1px solid rgba(128,128,128,.4); background: transparent;
                 color: var(--fg); border-radius: 8px; padding: 10px; font-size: 13px;
                 cursor: pointer; }
  .erow button.primary { background: var(--accent); color: var(--accent-fg);
                         border-color: var(--accent); font-weight: 600; }
  .ehint { color: var(--muted); font-size: 12px; text-align: center; margin-top: 10px; }
</style>
</head>
<body>
<header>
  <h1>Кадрирование итогов</h1>
  <div class="sub" id="sub">Загружаю…</div>
</header>
<div class="tools" id="tools" hidden>
  <button class="chip" id="allFit">Все: вписать</button>
  <button class="chip" id="allFill">Все: заполнить</button>
  <span class="spacer"></span>
  <span class="label">В ряд:</span>
  <button class="chip on" id="cols3" data-cols="3">3</button>
  <button class="chip" id="cols4" data-cols="4">4</button>
</div>
<div class="grid" id="grid"></div>
<div class="msg" id="msg" hidden></div>
<div class="bar">
  <div class="status" id="status"></div>
  <button class="go secondary" id="save" disabled>Кадрирование сохранено</button>
  <button class="go" id="export">Выгрузить картинку</button>
</div>

<div class="editor" id="editor" hidden>
  <div class="etop">
    <button class="eclose" id="editorClose" aria-label="Закрыть">✕</button>
    <div class="ewho" id="editorWho"></div>
  </div>
  <div class="stagewrap">
    <div class="stage" id="stage">
      <img id="stageImg" alt="">
      <div class="thirds">
        <i class="v" style="left:33.33%"></i><i class="v" style="left:66.66%"></i>
        <i class="h" style="top:33.33%"></i><i class="h" style="top:66.66%"></i>
      </div>
    </div>
  </div>
  <div class="zoomrow">
    <button id="zoomOut" aria-label="Отдалить">-</button>
    <input type="range" id="zoom" min="0" max="1000" value="0">
    <button id="zoomIn" aria-label="Приблизить">+</button>
  </div>
  <div class="erow">
    <button id="doFit">Вписать</button>
    <button id="doFill">Заполнить</button>
    <button class="primary" id="doDone">Готово</button>
  </div>
  <div class="ehint">Тяни фото, чтобы сдвинуть. Двумя пальцами или ползунком - масштаб.</div>
</div>

<script>
const PREFIX = "__PREFIX__";
/* The header and the overscroll strip belong to the client, not to our CSS, so on a
   light-themed phone they stay white around our dark page unless asked. Each setter
   landed in a different Bot API version; a client too old to answer keeps its own. */
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

let works = [];       // the ranked board: {id, author, username, votes, photo}
let crops = {};       // id -> {x, y, size}: the square being framed, in photo pixels
let natural = {};     // id -> {w, h}: learned from each photo as it loads
let dirty = false;    // something changed since the last successful save
let editing = null;   // id of the work the editor is open on
let columns = 3;      // how many works per row, here AND in the exported picture

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

// ------------------------------------------------------------------- the crop itself

// The whole photo, centred: the smallest square that contains it, which leaves letterbox
// on the short side. Mirrors vote_image.default_crop -- an untouched card must render the
// same whether or not anybody ever opened this page.
function fitCrop(size) {
  const side = Math.max(size.w, size.h);
  return { x: (size.w - side) / 2, y: (size.h - side) / 2, size: side };
}

// The biggest square entirely inside the photo, centred: object-fit: cover, i.e. exactly
// what the ballot's own grid shows. The starting point for anyone who wants it filled.
function fillCrop(size) {
  const side = Math.min(size.w, size.h);
  return { x: (size.w - side) / 2, y: (size.h - side) / 2, size: side };
}

function minSize(size) { return Math.max(16, Math.min(size.w, size.h) / 8); }
function maxSize(size) { return Math.max(size.w, size.h) * 1.8; }

// Keeps a fifth of the frame on the picture, whichever way it is dragged. Without it the
// photo can be shoved off its own square entirely, and the card renders as a blank
// letterbox that looks like a bug rather than like a choice.
function clamp(id) {
  const size = natural[id], crop = crops[id];
  if (!size || !crop) return;
  crop.size = Math.min(maxSize(size), Math.max(minSize(size), crop.size));
  const keepX = Math.min(crop.size, size.w) * 0.2;
  const keepY = Math.min(crop.size, size.h) * 0.2;
  crop.x = Math.min(Math.max(crop.x, -crop.size + keepX), size.w - keepX);
  crop.y = Math.min(Math.max(crop.y, -crop.size + keepY), size.h - keepY);
}

// The one place a crop becomes CSS: the frame shows crop.size photo-pixels across, so
// everything scales by frame/crop.size and the photo is offset by where the square starts.
function applyTo(img, crop, size, framePx) {
  if (!img || !crop || !size || !framePx) return;
  const scale = framePx / crop.size;
  img.style.width = (size.w * scale) + "px";
  img.style.height = (size.h * scale) + "px";
  img.style.left = (-crop.x * scale) + "px";
  img.style.top = (-crop.y * scale) + "px";
  img.classList.add("ready");
}

function paint(id) {
  clamp(id);
  const cell = document.querySelector('[data-cell="' + CSS.escape(id) + '"]');
  if (cell) applyTo(cell.querySelector("img"), crops[id], natural[id], cell.clientWidth);
  if (editing === id) {
    applyTo($("stageImg"), crops[id], natural[id], $("stage").clientWidth);
    syncZoom();
  }
}

function markDirty() {
  dirty = true;
  const save = $("save");
  save.disabled = false;
  save.textContent = "Сохранить кадрирование";
}

// -------------------------------------------------------------------------- the grid

function renderGrid() {
  const grid = $("grid");
  grid.innerHTML = "";
  works.forEach((work, index) => {
    const cell = document.createElement("div");
    cell.className = "gcard";
    cell.dataset.cell = work.id;
    cell.innerHTML =
      '<div class="frame">' +
        (work.photo ? '<img alt="" src="' + esc(work.photo) + '">' : "") +
        '<span class="badge">' + (index + 1) + " - " + work.votes + "</span>" +
        '<span class="edit">кадр</span>' +
      "</div>" +
      '<div class="who"><div class="name">' + esc(work.author || "") + "</div>" +
        '<span class="tag">' + (work.username ? "@" + esc(work.username) : "") + "</span></div>";
    cell.addEventListener("click", () => openEditor(work.id));
    grid.appendChild(cell);

    const img = cell.querySelector("img");
    if (!img) {
      cell.querySelector(".frame").insertAdjacentHTML("beforeend", '<span class="failed">без фото</span>');
      return;
    }
    img.addEventListener("error", () => {
      cell.querySelector(".frame").insertAdjacentHTML(
        "beforeend", '<span class="failed">фото не загрузилось</span>'
      );
    });
    const onReady = () => {
      natural[work.id] = { w: img.naturalWidth, h: img.naturalHeight };
      // A saved crop wins; anything not framed yet starts fitted, which is what the
      // renderer would have done with it anyway.
      if (!crops[work.id]) crops[work.id] = fitCrop(natural[work.id]);
      paint(work.id);
    };
    if (img.complete && img.naturalWidth) onReady(); else img.addEventListener("load", onReady);
  });
}

// A phone rotating (or Telegram resizing the app) changes every frame's width, and a crop
// only becomes pixels against that width -- without this the photos keep the old scale and
// the whole grid goes visibly wrong.
window.addEventListener("resize", () => { works.forEach((w) => paint(w.id)); });

// ------------------------------------------------------------------------ the editor

function whoText(work) {
  if (!work) return "";
  return work.username ? work.author + " (@" + work.username + ")" : work.author;
}

function openEditor(id) {
  if (!natural[id]) {
    // No natural size means the photo hasn't arrived, so there is no square to frame yet
    // -- said out loud, because a tap that does nothing reads as a broken page.
    status("Фото ещё не загрузилось - попробуй через секунду.");
    return;
  }
  editing = id;
  const work = works.find((w) => w.id === id);
  $("editorWho").textContent = whoText(work);
  const img = $("stageImg");
  img.classList.remove("ready");
  img.src = work.photo;
  $("editor").hidden = false;
  document.body.classList.add("editing");
  if (tg && tg.BackButton) tg.BackButton.show();
  const show = () => { applyTo(img, crops[id], natural[id], $("stage").clientWidth); syncZoom(); };
  if (img.complete && img.naturalWidth) show(); else img.addEventListener("load", show, { once: true });
}

function closeEditor() {
  const id = editing;
  editing = null;
  $("editor").hidden = true;
  document.body.classList.remove("editing");
  if (tg && tg.BackButton) tg.BackButton.hide();
  if (id) paint(id);
}

function syncZoom() {
  const size = natural[editing], crop = crops[editing];
  if (!size || !crop) return;
  const low = minSize(size), high = maxSize(size);
  // Logarithmic, so one step of the slider is the same proportional zoom everywhere --
  // linear, the whole useful range of a big photo lives in the last tenth of the track.
  const value = 1000 * Math.log(high / crop.size) / Math.log(high / low);
  $("zoom").value = String(Math.max(0, Math.min(1000, Math.round(value))));
}

// Zooms to `size` photo-pixels across while keeping whatever is under (ax, ay) -- stage
// coordinates -- exactly where it is. That is what makes pinching feel like the photo is
// being pulled, rather than jumping to centre on every touch.
function zoomTo(size, ax, ay) {
  const crop = crops[editing], dims = natural[editing];
  if (!crop || !dims) return;
  const stagePx = $("stage").clientWidth || 1;
  const bounded = Math.min(maxSize(dims), Math.max(minSize(dims), size));
  const sx = crop.x + (ax / stagePx) * crop.size;
  const sy = crop.y + (ay / stagePx) * crop.size;
  crop.x = sx - (ax / stagePx) * bounded;
  crop.y = sy - (ay / stagePx) * bounded;
  crop.size = bounded;
  markDirty();
  paint(editing);
}

const stage = $("stage");
const pointers = new Map();     // touches (or mouse buttons) currently down on the stage
let pinchStart = null;          // {distance, size} from when the second finger went down

function stagePoint(event) {
  const box = stage.getBoundingClientRect();
  return { x: event.clientX - box.left, y: event.clientY - box.top };
}

stage.addEventListener("pointerdown", (event) => {
  if (!editing) return;
  stage.setPointerCapture(event.pointerId);
  pointers.set(event.pointerId, stagePoint(event));
  stage.classList.add("dragging");
  if (pointers.size === 2) {
    const two = [...pointers.values()];
    pinchStart = {
      distance: Math.hypot(two[0].x - two[1].x, two[0].y - two[1].y),
      size: crops[editing].size,
    };
  }
});

stage.addEventListener("pointermove", (event) => {
  if (!editing || !pointers.has(event.pointerId)) return;
  const previous = pointers.get(event.pointerId);
  const current = stagePoint(event);
  pointers.set(event.pointerId, current);

  if (pointers.size >= 2 && pinchStart) {
    const two = [...pointers.values()];
    const distance = Math.hypot(two[0].x - two[1].x, two[0].y - two[1].y);
    if (distance > 0 && pinchStart.distance > 0) {
      // Fingers apart => a smaller square of the photo fills the frame => zoomed in.
      zoomTo(
        pinchStart.size * (pinchStart.distance / distance),
        (two[0].x + two[1].x) / 2, (two[0].y + two[1].y) / 2
      );
    }
    return;
  }

  const crop = crops[editing];
  const perPixel = crop.size / (stage.clientWidth || 1);
  crop.x -= (current.x - previous.x) * perPixel;
  crop.y -= (current.y - previous.y) * perPixel;
  markDirty();
  paint(editing);
});

function endPointer(event) {
  pointers.delete(event.pointerId);
  // The pinch is over the moment either finger leaves: measuring the next one-finger drag
  // against a stale two-finger distance is how a crop jumps for no reason.
  if (pointers.size < 2) pinchStart = null;
  if (!pointers.size) stage.classList.remove("dragging");
}
stage.addEventListener("pointerup", endPointer);
stage.addEventListener("pointercancel", endPointer);

stage.addEventListener("wheel", (event) => {
  if (!editing) return;
  event.preventDefault();
  const point = stagePoint(event);
  zoomTo(crops[editing].size * (event.deltaY < 0 ? 1 / 1.12 : 1.12), point.x, point.y);
}, { passive: false });

$("zoom").addEventListener("input", () => {
  if (!editing) return;
  const size = natural[editing];
  const low = minSize(size), high = maxSize(size);
  const centre = ($("stage").clientWidth || 1) / 2;
  zoomTo(high * Math.pow(low / high, Number($("zoom").value) / 1000), centre, centre);
});

function nudgeZoom(factor) {
  if (!editing) return;
  const centre = ($("stage").clientWidth || 1) / 2;
  zoomTo(crops[editing].size * factor, centre, centre);
}
$("zoomIn").addEventListener("click", () => nudgeZoom(1 / 1.2));
$("zoomOut").addEventListener("click", () => nudgeZoom(1.2));

$("doFit").addEventListener("click", () => {
  if (!editing) return;
  crops[editing] = fitCrop(natural[editing]); markDirty(); paint(editing);
});
$("doFill").addEventListener("click", () => {
  if (!editing) return;
  crops[editing] = fillCrop(natural[editing]); markDirty(); paint(editing);
});
$("doDone").addEventListener("click", closeEditor);
$("editorClose").addEventListener("click", closeEditor);
// Telegram's own back arrow closes the editor -- on a phone that is the gesture people
// reach for first, and without this it would close the whole Mini App instead.
if (tg && tg.BackButton) tg.BackButton.onClick(() => { if (editing) closeEditor(); });

// Changing the column count changes the width of every frame, and a crop is only pixels
// once it meets a frame width -- so every card has to be repainted, not just re-flowed.
function setColumns(next) {
  columns = next;
  document.documentElement.style.setProperty("--cols", String(columns));
  document.querySelectorAll("[data-cols]").forEach((chip) => {
    chip.classList.toggle("on", Number(chip.dataset.cols) === columns);
  });
  works.forEach((w) => paint(w.id));
}

document.querySelectorAll("[data-cols]").forEach((chip) => {
  chip.addEventListener("click", () => setColumns(Number(chip.dataset.cols)));
});

$("allFit").addEventListener("click", () => {
  works.forEach((w) => { if (natural[w.id]) { crops[w.id] = fitCrop(natural[w.id]); paint(w.id); } });
  markDirty();
});
$("allFill").addEventListener("click", () => {
  works.forEach((w) => { if (natural[w.id]) { crops[w.id] = fillCrop(natural[w.id]); paint(w.id); } });
  markDirty();
});

// ------------------------------------------------------------------- saving, exporting

// Only the works actually on the board, and only the ones whose photo has loaded: sending
// a crop for a card whose natural size was never learned would save a square computed from
// nothing.
function payloadCrops() {
  const out = {};
  works.forEach((w) => { if (natural[w.id] && crops[w.id]) out[w.id] = crops[w.id]; });
  return out;
}

function status(html) { $("status").innerHTML = html; }

async function save() {
  const button = $("save");
  button.disabled = true;
  button.textContent = "Сохраняю…";
  try {
    const response = await api("/api/crops", {
      method: "POST",
      body: JSON.stringify({ init_data: initData, crops: payloadCrops() }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "не получилось");
    dirty = false;
    button.textContent = "Кадрирование сохранено";
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    return true;
  } catch (e) {
    button.disabled = false;
    button.textContent = "Сохранить кадрирование";
    status(esc(String(e.message || e)));
    return false;
  }
}

$("save").addEventListener("click", save);

function openImage(url) {
  const absolute = new URL(url, location.href).href;
  if (tg && tg.openLink) tg.openLink(absolute); else window.open(absolute, "_blank");
}

$("export").addEventListener("click", async () => {
  const button = $("export");
  button.disabled = true;
  const original = button.textContent;
  button.textContent = "Рисую картинку…";
  status("Это займёт несколько секунд.");
  try {
    // The crops ride along with the export, so what renders is what is on screen even if
    // "Сохранить" was never pressed -- the server stores them as part of the same request.
    const response = await api("/api/export", {
      method: "POST",
      body: JSON.stringify({ init_data: initData, crops: payloadCrops(), columns: columns }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "не получилось");
    dirty = false;
    $("save").disabled = true;
    $("save").textContent = "Кадрирование сохранено";
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    const kilobytes = Math.round((data.bytes || 0) / 1024);
    status(
      (data.delivered
        ? "Готово: картинка отправлена файлом в личку с ботом. "
        : "Картинка готова, но отправить её в личку не вышло. ") +
      '<a href="#" id="openImage">Открыть картинку</a> - ' +
      data.entries + " работ, " + data.columns + " в ряд, " + kilobytes + " КБ"
    );
    const link = $("openImage");
    if (link) {
      link.addEventListener("click", (event) => { event.preventDefault(); openImage(data.url); });
    }
  } catch (e) {
    status(esc(String(e.message || e)));
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
});

// A reload costs nothing but unsaved framing -- which is exactly the thing worth a
// warning, since it can be twenty photos' worth of work.
window.addEventListener("beforeunload", (event) => {
  if (!dirty) return;
  event.preventDefault();
  event.returnValue = "";
});

(async function load() {
  try {
    // mode=admin is what makes the server send `results` (the ranked board) and `crops` at
    // all -- and it only obeys it for a real administrator, whoever asks (see handle_poll).
    const response = await api("/api/poll?mode=admin");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "не получилось загрузить");
    if (!data.poll_id) {
      $("sub").textContent = "";
      $("msg").hidden = false;
      $("msg").textContent = "Голосование ещё не создано.";
      return;
    }
    if (!data.is_admin) {
      $("sub").textContent = "";
      $("msg").hidden = false;
      $("msg").textContent = "Кадрирование доступно только администраторам.";
      return;
    }
    crops = data.crops || {};
    works = data.results || [];
    if (!works.length) {
      $("sub").textContent = "";
      $("msg").hidden = false;
      $("msg").textContent = "К голосованию ещё не допущена ни одна работа -- /vote выбрать.";
      return;
    }
    $("sub").textContent =
      "Работ: " + works.length + " · проголосовало " + (data.voter_count || 0) +
      " · порядок как в картинке, по голосам";
    $("tools").hidden = false;
    renderGrid();
    setColumns(columns);  // publishes --cols and lights the matching chip
  } catch (e) {
    $("sub").textContent = "";
    $("msg").hidden = false;
    $("msg").textContent = String(e.message || e);
  }
})();
</script>
</body>
</html>
"""
