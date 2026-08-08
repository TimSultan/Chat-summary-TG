"""post_stats_web's HTTP surface: a token gate on /api/data (and only there -- the page
shell and thumbnails are unauthenticated on purpose, matching arena_web/vote_web), and
the two-step path guard on /thumb/{chat_id}/{name}.

resolve_chat and fetch_post_stats are patched with async stubs rather than driven
through a fake Telethon client -- post_stats_web only ever calls those two functions
(plus whatever the fakes return), so that's the real seam.
"""

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import post_stats_web
from errors import ChatSummaryError

TOKEN = "secret123"


def _fake_cfg(token=TOKEN):
    return SimpleNamespace(post_stats_access_token=token)


def _fake_entity(id=42, title="Test Chat", username=None):
    return SimpleNamespace(id=id, title=title, username=username)


def _fake_post(message_id, thumbnail_path=None):
    return SimpleNamespace(
        message_id=message_id,
        date=datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc),
        text_preview=f"post {message_id}",
        thumbnail_path=thumbnail_path,
        views=100 + message_id,
        forwards=1,
        reactions_total=2,
        reactions_breakdown={"👍": 2},
        comments=3,
        is_edited=False,
        media_type="photo" if thumbnail_path else "none",
        link=f"https://t.me/c/42/{message_id}",
    )


class PostStatsWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self._thumb_base = Path(self._temporary.name) / "thumbs"
        self._patchers = [patch.object(post_stats_web, "_THUMB_BASE", self._thumb_base)]
        for patcher in self._patchers:
            patcher.start()

        app = web.Application()
        post_stats_web.attach(app, client=object(), cfg=_fake_cfg(), log=lambda *_: None)
        self.server = TestServer(app)
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        for patcher in self._patchers:
            patcher.stop()
        self._temporary.cleanup()

    # ---- token gate -------------------------------------------------------------

    async def test_no_token_is_unauthorized(self):
        response = await self.client.get(
            post_stats_web.ROUTE_PREFIX + "/api/data", params={"chat": "foo", "range": "today"}
        )
        self.assertEqual(response.status, 401)
        self.assertIn("error", await response.json())

    async def test_wrong_token_is_unauthorized(self):
        response = await self.client.get(post_stats_web.ROUTE_PREFIX + "/api/data", params={
            "token": "nope", "chat": "foo", "range": "today",
        })
        self.assertEqual(response.status, 401)

    async def test_unset_token_refuses_everyone(self):
        app = web.Application()
        post_stats_web.attach(app, client=object(), cfg=_fake_cfg(token=None), log=lambda *_: None)
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            response = await client.get(post_stats_web.ROUTE_PREFIX + "/api/data", params={
                "token": "", "chat": "foo", "range": "today",
            })
            self.assertEqual(response.status, 401)
        finally:
            await client.close()

    # ---- the page shell is not gated ---------------------------------------------

    async def test_page_needs_no_token(self):
        response = await self.client.get(post_stats_web.ROUTE_PREFIX)
        self.assertEqual(response.status, 200)
        self.assertNotIn("__PREFIX__", await response.text())

    # ---- /api/data happy path ------------------------------------------------------

    async def test_data_shape_matches_contract(self):
        entity = _fake_entity()
        posts = [_fake_post(1, thumbnail_path=Path("x.jpg")), _fake_post(2, thumbnail_path=None)]

        async def fake_resolve(client, chat):
            return entity

        async def fake_fetch(client, ent, start, end, thumb_dir, log=print):
            return posts

        with patch("post_stats_web.resolve_chat", side_effect=fake_resolve), \
             patch("post_stats_web.fetch_post_stats", side_effect=fake_fetch):
            response = await self.client.get(post_stats_web.ROUTE_PREFIX + "/api/data", params={
                "token": TOKEN, "chat": "foo", "range": "today",
            })
        self.assertEqual(response.status, 200)
        data = await response.json()

        self.assertEqual(data["chat_title"], "Test Chat")
        self.assertIn("period_label", data)
        self.assertIn("generated_at", data)
        self.assertEqual(len(data["posts"]), 2)

        first, second = data["posts"]
        for key in (
            "message_id", "date", "text_preview", "thumbnail_url", "views", "forwards",
            "reactions_total", "reactions_breakdown", "comments", "is_edited", "media_type",
            "link",
        ):
            self.assertIn(key, first)

        self.assertEqual(
            first["thumbnail_url"],
            f"{post_stats_web.ROUTE_PREFIX}/thumb/{entity.id}/{first['message_id']}.jpg",
        )
        self.assertIsNone(second["thumbnail_url"])
        self.assertEqual(first["views"], 101)
        self.assertEqual(first["reactions_breakdown"], {"👍": 2})

    # ---- validation -----------------------------------------------------------------

    async def test_missing_chat_is_bad_request(self):
        response = await self.client.get(post_stats_web.ROUTE_PREFIX + "/api/data", params={
            "token": TOKEN, "range": "today",
        })
        self.assertEqual(response.status, 400)

    async def test_missing_range_and_dates_is_bad_request(self):
        response = await self.client.get(post_stats_web.ROUTE_PREFIX + "/api/data", params={
            "token": TOKEN, "chat": "foo",
        })
        self.assertEqual(response.status, 400)

    async def test_bad_custom_date_is_bad_request(self):
        response = await self.client.get(post_stats_web.ROUTE_PREFIX + "/api/data", params={
            "token": TOKEN, "chat": "foo", "start": "not-a-date", "end": "also-not-a-date",
        })
        self.assertEqual(response.status, 400)

    async def test_custom_start_end_range_works(self):
        entity = _fake_entity()

        async def fake_resolve(client, chat):
            return entity

        async def fake_fetch(client, ent, start, end, thumb_dir, log=print):
            return []

        with patch("post_stats_web.resolve_chat", side_effect=fake_resolve), \
             patch("post_stats_web.fetch_post_stats", side_effect=fake_fetch):
            response = await self.client.get(post_stats_web.ROUTE_PREFIX + "/api/data", params={
                "token": TOKEN, "chat": "foo", "start": "2026-08-01", "end": "2026-08-02",
            })
        self.assertEqual(response.status, 200)

    async def test_chat_not_found_is_404(self):
        async def fake_resolve(client, chat):
            raise ChatSummaryError("no such chat")

        with patch("post_stats_web.resolve_chat", side_effect=fake_resolve):
            response = await self.client.get(post_stats_web.ROUTE_PREFIX + "/api/data", params={
                "token": TOKEN, "chat": "foo", "range": "today",
            })
        self.assertEqual(response.status, 404)
        self.assertIn("error", await response.json())

    async def test_fetch_failure_is_502_with_no_leaked_message(self):
        entity = _fake_entity()

        async def fake_resolve(client, chat):
            return entity

        async def fake_fetch(client, ent, start, end, thumb_dir, log=print):
            raise RuntimeError("boom: secret internal detail")

        with patch("post_stats_web.resolve_chat", side_effect=fake_resolve), \
             patch("post_stats_web.fetch_post_stats", side_effect=fake_fetch):
            response = await self.client.get(post_stats_web.ROUTE_PREFIX + "/api/data", params={
                "token": TOKEN, "chat": "foo", "range": "today",
            })
        self.assertEqual(response.status, 502)
        body = await response.json()
        self.assertNotIn("boom", body["error"])

    async def test_chat_summary_error_from_fetch_is_502(self):
        entity = _fake_entity()

        async def fake_resolve(client, chat):
            return entity

        async def fake_fetch(client, ent, start, end, thumb_dir, log=print):
            raise ChatSummaryError("telegram said no")

        with patch("post_stats_web.resolve_chat", side_effect=fake_resolve), \
             patch("post_stats_web.fetch_post_stats", side_effect=fake_fetch):
            response = await self.client.get(post_stats_web.ROUTE_PREFIX + "/api/data", params={
                "token": TOKEN, "chat": "foo", "range": "today",
            })
        self.assertEqual(response.status, 502)

    # ---- /thumb -----------------------------------------------------------------------

    async def test_thumb_path_traversal_is_404(self):
        response = await self.client.get(
            post_stats_web.ROUTE_PREFIX + "/thumb/42/..%2F..%2Fsecrets.txt"
        )
        self.assertEqual(response.status, 404)

    async def test_thumb_bad_name_pattern_is_404(self):
        response = await self.client.get(post_stats_web.ROUTE_PREFIX + "/thumb/42/notanumber.jpg")
        self.assertEqual(response.status, 404)

    async def test_thumb_bad_chat_id_pattern_is_404(self):
        response = await self.client.get(post_stats_web.ROUTE_PREFIX + "/thumb/notanumber/1.jpg")
        self.assertEqual(response.status, 404)

    async def test_thumb_missing_file_is_404(self):
        response = await self.client.get(post_stats_web.ROUTE_PREFIX + "/thumb/42/999.jpg")
        self.assertEqual(response.status, 404)

    async def test_thumb_real_file_is_served(self):
        chat_dir = self._thumb_base / "42"
        chat_dir.mkdir(parents=True, exist_ok=True)
        target = chat_dir / "7.jpg"
        target.write_bytes(b"\xff\xd8\xff\xd9fake-jpeg-bytes")

        response = await self.client.get(post_stats_web.ROUTE_PREFIX + "/thumb/42/7.jpg")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), b"\xff\xd8\xff\xd9fake-jpeg-bytes")

    # ---- attach ---------------------------------------------------------------------

    async def test_attach_mounts_all_four_routes_and_returns_app(self):
        app = web.Application()
        result = post_stats_web.attach(app, client=object(), cfg=_fake_cfg(), log=lambda *_: None)
        self.assertIs(result, app)
        paths = {route.resource.canonical for route in app.router.routes()}
        prefix = post_stats_web.ROUTE_PREFIX
        self.assertIn(prefix, paths)
        self.assertIn(f"{prefix}/", paths)
        self.assertIn(f"{prefix}/api/data", paths)
        self.assertIn(prefix + "/thumb/{chat_id}/{name}", paths)


if __name__ == "__main__":
    unittest.main()
