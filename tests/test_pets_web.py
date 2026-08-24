"""The pet game's Mini App HTTP surface: authentication on every route, a resolved
player's live chat XP gates spending, membership gates acting but never reading, and every
mutation answers with the SAME state shape GET /api/state would -- so a client is never
left one request behind what the server actually did.

Mounted the way it really is -- onto the application vote_web.py builds, exactly the way
bot_listener's _attach_extra mounts it in production -- so these also prove pets_web
coexists with the rest of the server without colliding.
"""

import hashlib
import hmac
import asyncio
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_listener
import donations
import economy
import maintenance
import pets
import pets_config as C
import pets_mobs
import pets_scroll_catalog as SCROLLS
import pets_sprite
import pets_sprite_store
import pets_updates
import pets_weapon_catalog
import quests
import pets_dungeon as dungeon
import pets_web
import stats
import vote_web
from pets_ui import valuable_item  # the same "needs confirming" rarity rule pets_web uses

BOT_TOKEN = "123456:FAKE-TOKEN-FOR-TESTS"
CHAT = "Chat"
# coins_for_xp(100_000) == 20_000 -- comfortably enough to buy a cage, a tame and any
# single shop item in every test below, without the number itself meaning anything.
RICH_XP = 100_000

PLAYER = {"id": 42, "username": "player", "first_name": "Player"}
OPPONENT = {"id": 43, "username": "opponent", "first_name": "Opponent"}
THIRD = {"id": 44, "username": "third", "first_name": "Third"}
NONMEMBER = {"id": 99, "username": "outsider", "first_name": "Outsider"}
# A chat admin or /badgeadmin delegate -- the only person who may review a quest.
MODERATOR = {"id": 55, "username": "mod", "first_name": "Mod"}
UNTRACKED = {"id": 77, "username": "ghost", "first_name": "Ghost"}


def _sign(fields: dict) -> str:
    payload = dict(fields)
    check = "\n".join(f"{k}={payload[k]}" for k in sorted(payload))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    payload["hash"] = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    return urlencode(payload)


def _init_data(user_id: int) -> str:
    return _sign({
        "auth_date": str(int(time.time())),
        "user": json.dumps({"id": user_id, "username": f"u{user_id}", "first_name": "V"}),
    })


def _jpeg_bytes(size=(64, 64), colour=(200, 30, 30)) -> bytes:
    """A tiny, real JPEG -- for tests that just need something _normalise_photo accepts."""
    buffer = BytesIO()
    Image.new("RGB", size, colour).save(buffer, "JPEG")
    return buffer.getvalue()


def _large_jpeg_bytes(edge: int = 2000, quality: int = 95) -> bytes:
    """A JPEG well above PORTRAIT_MAX_EDGE (1280) on a side, textured enough that shrinking
    it to 1280px actually costs bytes -- unlike a flat colour, which a JPEG encoder already
    squashes to almost nothing regardless of pixel count. Upscaled from real randomness
    (rather than randomised pixel-by-pixel, which is both slow to generate at this size and
    -- as pure noise -- sometimes RE-encodes larger after a small resize, the opposite of
    what this helper needs to demonstrate) so it stays well under aiohttp's request-body
    ceiling too.
    """
    small = Image.frombytes("RGB", (60, 60), os.urandom(60 * 60 * 3))
    buffer = BytesIO()
    small.resize((edge, edge), Image.Resampling.BICUBIC).save(buffer, "JPEG", quality=quality)
    return buffer.getvalue()


class PetsWebApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Both are module-level and keyed on (entry, user id), which every test here
        # shares -- a resolution held from the previous test would otherwise stand in for
        # one this test expects to be made.
        pets_web._xp_cache.clear()
        pets_web._member_cache.clear()
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        self._patchers = [
            patch("stats._stats_dir", return_value=root),
            # No real art directory in a test run -- pin it to an empty one explicitly
            # rather than relying on DATA_DIR/pets/items not existing, so the placeholder
            # path is what every test exercises regardless of where it runs.
            patch("pets_web.art_dir", return_value=root / "art"),
            # portrait_dir() reads DATA_DIR itself at call time rather than being patched
            # directly -- steer it into the tempdir the same way production steers it, or
            # every portrait test would litter DATA_DIR/pets/portraits in the real repo.
            patch.dict(os.environ, {"DATA_DIR": str(root)}),
        ]
        for patcher in self._patchers:
            patcher.start()

        # The sprite route needs a key to be present at all before it will spend a
        # vision call; `pets_sprite.classify` itself is patched in the tests that
        # exercise it, so nothing here ever reaches the network.
        cfg = SimpleNamespace(
            telegram_bot_token=BOT_TOKEN,
            openai_api_key="test-key", openai_model="test-model",
        )

        # vote_web's admin gate. pets_web gets its own below -- the same callable in
        # production (_is_vote_admin), but named separately here so a test can prove the
        # quest routes ask for themselves rather than trusting a menu flag.
        async def is_admin(user):
            return user.get("id") == MODERATOR["id"]

        async def is_economy_admin(user):
            # Financial records use a separate, narrower gate from quest review.
            return user.get("id") == THIRD["id"]

        # Both gates cost a Telegram round trip in production and pets_web caches a
        # confirmed answer, so how OFTEN they are consulted is behaviour under test.
        self.member_calls: list[int] = []
        self.resolve_calls: list[int] = []

        async def is_member(user):
            self.member_calls.append(user.get("id"))
            return user.get("id") != NONMEMBER["id"]

        async def resolve_player(user):
            self.resolve_calls.append(user.get("id"))
            if user.get("id") == UNTRACKED["id"]:
                return None, None
            return None, RICH_XP

        # fetch_photo/save_photo stand in for the Bot API client production wires in --
        # keyed and recorded so a test can both script a download and assert on what the
        # route actually asked for.
        self.fetch_calls: list[str] = []
        self._photos: dict[str, object] = {}

        async def fetch_photo(file_id):
            self.fetch_calls.append(file_id)
            result = self._photos.get(file_id)
            if isinstance(result, Exception):
                raise result
            return result

        self.save_calls: list[tuple] = []
        self.next_saved_file_id = "uploaded_file_id"

        async def save_photo(user_id, data):
            self.save_calls.append((user_id, data))
            return self.next_saved_file_id

        # Captured rather than discarded: the log is the only thing that distinguishes the
        # three reasons a portrait can come back as a placeholder, so it is behaviour under
        # test and not just noise (see the placeholder-reason tests below).
        self.logs: list[str] = []
        self.quest_feedback: list[tuple] = []
        self.quest_completions: list[dict] = []

        async def quest_feedback(user_id, title, note):
            self.quest_feedback.append((str(user_id), title, note))

        async def quest_completion(row):
            self.quest_completions.append(dict(row))

        # Recorded rather than sent: the birthday DM is the one part of a greeting that
        # can fail in production (a bot cannot write to somebody who never opened it), so
        # a test needs to see both that it was attempted and that failing it is survivable.
        self.birthday_greetings: list[tuple] = []
        self.birthday_notify_raises: Exception | None = None

        async def birthday_notify(celebrant, greeter_name, gold, xp):
            if self.birthday_notify_raises:
                raise self.birthday_notify_raises
            self.birthday_greetings.append((str(celebrant), greeter_name, gold, xp))

        # Recorded rather than sent, and able to fail on demand: a pledge the owner cannot
        # be told about must still be kept, which is a behaviour worth testing.
        self.support_pledges: list[dict] = []
        self.support_notify_raises: Exception | None = None

        async def support_notify(pledge):
            if self.support_notify_raises:
                raise self.support_notify_raises
            self.support_pledges.append(dict(pledge))

        # Built exactly as production builds it: v1's app, with the pet game attached the
        # way bot_listener's _attach_extra really attaches it.
        app = vote_web.create_app(
            cfg, CHAT, is_admin, log=lambda *_: None,
            attach=lambda a: pets_web.attach(
                a, cfg, CHAT, is_member=is_member, is_admin=is_admin,
                is_economy_admin=is_economy_admin,
                resolve_player=resolve_player,
                fetch_photo=fetch_photo, save_photo=save_photo,
                quest_feedback=quest_feedback,
                quest_completion=quest_completion,
                birthday_notify=birthday_notify,
                support_notify=support_notify,
                log=self.logs.append,
            ),
        )
        # Kept on the case so a test can flip a setting (the Gemini key) or await the
        # background sprite jobs the app holds references to.
        self.app = app
        self.app_cfg = cfg
        self.server = TestServer(app)
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        for patcher in self._patchers:
            patcher.stop()
        self._temporary.cleanup()

    # ---- helpers ----------------------------------------------------------------------

    def _auth(self, user):
        return {"X-Telegram-Init-Data": _init_data(user["id"])}

    def _tame(self, user, name="Кабанчик"):
        """Give `user` a cage and a named creature, the same two calls test_pets_command.py
        uses -- the game logic itself is proven correct there, this module only has to
        prove the HTTP layer reports it faithfully."""
        self.assertTrue(pets.buy_cage(CHAT, user["id"], RICH_XP)[0])
        self.assertTrue(
            pets.tame(CHAT, user["id"], RICH_XP, name, "file_id", user["first_name"])[0]
        )

    async def _get(self, path, user):
        return await self.client.get(pets_web.ROUTE_PREFIX + path, headers=self._auth(user))

    async def _post(self, path, user, payload=None):
        """POST a signed mutation, the way every non-/api/action route takes it: initData
        travels in the JSON body rather than the header."""
        return await self.client.post(pets_web.ROUTE_PREFIX + path, json={
            "init_data": _init_data(user["id"]), **(payload or {}),
        })

    async def _action_raw(self, user, action, **payload):
        """POST /api/action without asserting the transport succeeded -- for the tests
        that are about the status code itself."""
        return await self.client.post(pets_web.ROUTE_PREFIX + "/api/action", json={
            "init_data": _init_data(user["id"]), "action": action, **payload,
        })

    async def _action(self, user, action, **payload):
        """POST /api/action, asserting only the transport succeeded (HTTP 200) -- a
        refused action (ok: false) is still a 200, so callers check `ok` themselves."""
        response = await self.client.post(pets_web.ROUTE_PREFIX + "/api/action", json={
            "init_data": _init_data(user["id"]), "action": action, **payload,
        })
        self.assertEqual(response.status, 200, await response.text())
        return await response.json()

    async def _upload_portrait(self, user, data: bytes, pet_name: str | None = None):
        """POST raw bytes to /api/portrait the way the page's canvas upload does -- the
        body IS the image, so initData travels in the header instead of the JSON payload
        every other mutation uses."""
        headers = {**self._auth(user), "Content-Type": "image/jpeg"}
        suffix = "?pet_name=" + pet_name if pet_name else ""
        return await self.client.post(
            pets_web.ROUTE_PREFIX + "/api/portrait" + suffix, data=data,
            headers=headers,
        )

    # ---- authentication -----------------------------------------------------------------

    async def test_every_route_refuses_an_unsigned_caller(self):
        """The whole game rides on economy.balance, which needs a player resolved against
        the chat's own statistics -- so nothing here, reads included, may run before
        initData has been verified."""
        for method, path in (
            ("get", "/api/state"), ("get", "/api/opponents"), ("get", "/api/shop"),
            ("get", "/api/leaderboard"), ("get", "/api/history"), ("get", "/api/collection"),
            ("get", "/api/updates"), ("get", "/api/mail"),
            ("get", "/api/economy/audit"),
            ("get", "/api/test-battle"),
            ("post", "/api/action"), ("post", "/api/attack"),
            ("post", "/api/updates/claim"),
            ("post", "/api/test-battle/start"), ("post", "/api/test-battle/action"),
        ):
            response = await getattr(self.client, method)(pets_web.ROUTE_PREFIX + path, json={})
            self.assertEqual(response.status, 401, f"{method} {path} let an unsigned caller in")

    async def test_an_untracked_caller_is_turned_away_before_anything_is_priced(self):
        """resolve_player returning xp=None means the chat has no statistics for this
        person at all -- _player() treats that as a hard stop rather than pricing
        anything off a blank history, which is also what stops somebody who has never
        written in the chat from farming the arena."""
        response = await self._get("/api/state", UNTRACKED)
        self.assertEqual(response.status, 403)
        self.assertEqual((await response.json())["error"], "NOT_TRACKED")

    # ---- state ----------------------------------------------------------------------

    async def test_state_shows_the_free_cage_before_pet_creation_and_the_full_paperdoll_after_taming(self):
        """GET /api/state is the one shape every screen renders from. A player with
        nothing yet still needs a price to look at; a tamed player needs every panel --
        stats, gear (all five slots, empty ones included), bag, arena, farm -- in that
        same single call, because the whole point of the design is that no screen has to
        ask twice."""
        bare = await (await self._get("/api/state", PLAYER)).json()
        self.assertIsNone(bare["pet"])
        self.assertTrue(bare["has_cage"])
        self.assertEqual(bare["cage"]["level"], 1)

        self._tame(PLAYER)
        full = await (await self._get("/api/state", PLAYER)).json()
        self.assertIsNotNone(full["pet"])
        self.assertTrue(full["has_cage"])
        self.assertIn("stats", full)
        self.assertEqual(len(full["equipment"]), 5)
        self.assertEqual({slot["slot"] for slot in full["equipment"]}, set(C.SLOT_KEYS))
        self.assertTrue(all(slot["item"] is None for slot in full["equipment"]))
        # Four slots from the moment it is tamed, all of them empty and none of them
        # offering anything to equip yet -- a creature earns every scroll it fields.
        self.assertEqual(len(full["skills"]["slots"]), 4)
        self.assertTrue(all(slot["empty"] for slot in full["skills"]["slots"]))
        self.assertEqual(full["skills"]["owned_count"], 0)
        self.assertEqual(full["skills"]["regular"], [])
        self.assertEqual(full["skills"]["ultimate"], [])
        self.assertTrue(full["skills"]["slots"][3]["ultimate"])
        # A newly tamed creature watches PVE by default; the preference is stored with
        # the pet instead of being lost when the Mini App is reopened.
        self.assertFalse(full["pet"]["skip_pve_replays"])
        self.assertFalse(full["is_economy_admin"])
        self.assertIn("bag", full)
        self.assertIn("arena", full)
        self.assertIn("farm", full)

    async def test_mob_replay_skip_preference_is_persistent_and_defaults_to_watching(self):
        self._tame(PLAYER)

        initial = await (await self._get("/api/state", PLAYER)).json()
        self.assertFalse(initial["pet"]["skip_pve_replays"])

        skipped = await self._action(PLAYER, "pve_replays")
        self.assertTrue(skipped["ok"])
        self.assertTrue(skipped["state"]["pet"]["skip_pve_replays"])
        self.assertIn("пропускаться", skipped["message"])

        restored = await self._action(PLAYER, "pve_replays")
        self.assertTrue(restored["ok"])
        self.assertFalse(restored["state"]["pet"]["skip_pve_replays"])

    async def test_mob_search_prefetches_five_distinct_opponents_across_all_tiers(self):
        self._tame(PLAYER)

        response = await self._get("/api/mob", PLAYER)
        self.assertEqual(response.status, 200)
        body = await response.json()

        self.assertEqual(len(body["mobs"]), 5)
        self.assertEqual(len({row["code"] for row in body["mobs"]}), 5)
        self.assertEqual({row["tier"] for row in body["mobs"]}, set(pets_mobs.TIERS))
        self.assertEqual(body["mob"], body["mobs"][0])

    async def test_page_defends_hero_rendering_when_equipment_is_empty(self):
        page = await (await self.client.get(pets_web.ROUTE_PREFIX + "/")).text()
        self.assertIn('for (const s of (S.equipment || []))', page)
        self.assertIn('emptySlot("weapon")', page)
        self.assertRegex(page, r"\.slot \{\s*width: 100%")
        self.assertIn("Снаряжения пока нет.", page)

    async def test_shop_purchase_removes_an_offer_without_replenishing_it(self):
        self._tame(PLAYER)
        before = await (await self._get("/api/shop", PLAYER)).json()
        self.assertEqual(len(before["weapons"]), C.DAILY_STOREFRONT_SIZE)

        bought = before["weapons"][0]
        result = await self._action(PLAYER, "buy", code=bought["code"])
        self.assertTrue(result["ok"], result)

        after = await (await self._get("/api/shop", PLAYER)).json()
        self.assertEqual(len(after["weapons"]), C.DAILY_STOREFRONT_SIZE - 1)
        self.assertNotIn(bought["code"], {item["code"] for item in after["weapons"]})

    async def test_a_mutating_action_returns_state_that_already_reflects_it(self):
        """The point of one action endpoint is that its own response IS the new truth --
        a client that trusted a stale GET instead of this response would still draw a cage
        that needs buying. Assert on the response body, never on a follow-up read."""
        body = await self._action(PLAYER, "buy_cage")
        self.assertTrue(body["ok"], body["message"])
        self.assertTrue(body["state"]["has_cage"])

    async def test_the_state_survives_a_running_farm_and_its_live_timestamps(self):
        """Everything in the state is forwarded straight from pets.py, and some of what it
        returns is a live datetime rather than a string -- passive_income_status's
        "next_hour" is one, on every branch including the zero-rate one a freshly tamed pet
        takes. json.dumps refuses those outright, and since every action re-embeds the
        state, one such field turns the whole Mini App into a 500 for anyone who owns a
        pet. _jsonable converts at the boundary; this is the test that says so."""
        self._tame(PLAYER)
        pets.upgrade_farm(CHAT, PLAYER["id"], RICH_XP)
        pets.start_farm(CHAT, PLAYER["id"], hours=2)

        response = await self._get("/api/state", PLAYER)
        self.assertEqual(response.status, 200)
        state = await response.json()
        self.assertIsInstance(state["farm"]["passive"]["next_hour"], (str, type(None)))
        self.assertTrue(state["farm"]["running"])

    async def test_quarry_exposes_four_reward_cards_and_starts_the_selected_duration(self):
        self._tame(PLAYER)
        data = pets._load(CHAT)
        data["pets"][str(PLAYER["id"])]["pickaxe_runs"] = 1
        pets._save(CHAT, data)

        before = await (await self._get("/api/state", PLAYER)).json()
        previews = before["quarry"]["hour_previews"]
        self.assertEqual([row["hours"] for row in previews], [1, 2, 4, 8])
        self.assertTrue(all(
            row["ruby_min"] > 0 and row["ruby_max"] >= row["ruby_min"]
            for row in previews
        ))
        self.assertTrue(all(
            row["gold"] > 0 and row["xp"] > 0 and row["drop_chance"] > 0
            for row in previews
        ))

        started = await self._action(PLAYER, "quarry_start", hours=2)
        self.assertTrue(started["ok"], started)
        self.assertTrue(started["state"]["quarry"]["running"])
        run = pets._load(CHAT)["pets"][str(PLAYER["id"])]["quarry_run"]
        self.assertEqual(run["hours"], 2)

    async def test_meadow_start_spends_tickets_and_opens_a_round(self):
        """Meadow tickets are their own currency -- the same 🎟 glyph the farm's early-
        recall ticket wears, but a different balance underneath. This is the action wired
        to pets.start_meadow, and it must actually spend from the meadow wallet and hand
        back a fresh round rather than reuse the farm's."""
        self._tame(PLAYER)
        pets.grant_meadow_ticket(CHAT, PLAYER["id"], 1)

        result = await self._action(PLAYER, "meadow_start", size="small")
        self.assertTrue(result["ok"], result)
        meadow = result["state"]["meadow"]
        self.assertEqual(meadow["tickets"], 0)
        self.assertEqual(meadow["round"]["size"], "small")
        self.assertEqual(meadow["round"]["picks"], 3)
        self.assertEqual(meadow["round"]["picks_left"], 3)
        self.assertEqual(meadow["round"]["revealed"], {})

    async def test_meadow_pick_opens_a_cell_and_credits_the_diamond_it_finds(self):
        """meadow_pick is billed on the state it hands back, not on a probability: force a
        known layout (the same trick the quarry test above uses on pickaxe_runs) so this
        asserts an exact ruby count rather than "some number greater than zero"."""
        self._tame(PLAYER)
        pets.grant_meadow_ticket(CHAT, PLAYER["id"], 1)
        started = await self._action(PLAYER, "meadow_start", size="small")
        self.assertTrue(started["ok"], started)

        data = pets._load(CHAT)
        data["meadow"][str(PLAYER["id"])]["round"]["cells"][0] = "diamond"
        pets._save(CHAT, data)
        before_rubies = pets.ruby_balance(CHAT, PLAYER["id"])

        picked = await self._action(PLAYER, "meadow_pick", index=0)
        self.assertTrue(picked["ok"], picked)
        round_state = picked["state"]["meadow"]["round"]
        self.assertEqual(round_state["revealed"], {"0": "diamond"})
        self.assertEqual(round_state["rubies_won"], 1)
        self.assertEqual(round_state["picks_left"], 2)
        self.assertEqual(pets.ruby_balance(CHAT, PLAYER["id"]), before_rubies + 1)

    async def test_meadow_never_exposes_unopened_cells_before_the_round_finishes(self):
        """The meadow's whole anti-cheat design is that the server never sends an unpicked
        cell's contents (pets_meadow.public_state strips it). This checks that structurally
        -- on both responses a browser actually sees, GET /api/state and the meadow_pick
        action's own returned state -- so a future change that starts forwarding the raw
        board (say, "for a debug panel") fails a test instead of shipping."""
        self._tame(PLAYER)
        pets.grant_meadow_ticket(CHAT, PLAYER["id"], 1)
        started = await self._action(PLAYER, "meadow_start", size="small")
        self.assertTrue(started["ok"], started)

        def assert_no_full_board(round_row: dict):
            # "cells" here is the CELL COUNT (an int, e.g. 9), not the board -- pets_meadow
            # reuses the name for the tally on purpose, which is exactly the kind of mix-up
            # that would let a raw board slip through unnoticed. So the real check is
            # structural: no field in the round is a list as long as the board itself,
            # because that is the shape a leaked layout would take, whatever it was named.
            self.assertNotIn("board", round_row)   # only appears once finished
            self.assertIsInstance(round_row["cells"], int)
            self.assertFalse(any(
                isinstance(value, list) and len(value) == round_row["cells"]
                for value in round_row.values()
            ), round_row)

        state_after_start = await (await self._get("/api/state", PLAYER)).json()
        round_after_start = state_after_start["meadow"]["round"]
        assert_no_full_board(round_after_start)
        self.assertEqual(round_after_start["revealed"], {})

        picked = await self._action(PLAYER, "meadow_pick", index=4)
        self.assertTrue(picked["ok"], picked)
        round_after_pick = picked["state"]["meadow"]["round"]
        assert_no_full_board(round_after_pick)
        # Exactly the cell that was picked, nothing more -- a leak of a second index would
        # still pass the full-board check above but fail this one.
        self.assertEqual(set(round_after_pick["revealed"]), {"4"})

        state_after_pick = await (await self._get("/api/state", PLAYER)).json()
        round_after_pick_state = state_after_pick["meadow"]["round"]
        assert_no_full_board(round_after_pick_state)
        self.assertEqual(set(round_after_pick_state["revealed"]), {"4"})

    async def test_an_unrecognised_action_name_is_a_400(self):
        """A typo'd or outdated action name must fail loudly and specifically, not fall
        through to some default -- there is no default mutation to fall through to."""
        response = await self.client.post(pets_web.ROUTE_PREFIX + "/api/action", json={
            "init_data": _init_data(PLAYER["id"]), "action": "levitate",
        })
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"], "UNKNOWN_ACTION")

    # ---- membership -------------------------------------------------------------------

    async def test_a_non_member_is_blocked_from_acting_but_can_still_read(self):
        """Membership gates spending and fighting -- someone who has left the chat cannot
        touch the economy through the page, but the page itself (and reading it) is not
        the thing being protected."""
        action = await self.client.post(pets_web.ROUTE_PREFIX + "/api/action", json={
            "init_data": _init_data(NONMEMBER["id"]), "action": "buy_cage",
        })
        self.assertEqual(action.status, 403)
        attack = await self.client.post(pets_web.ROUTE_PREFIX + "/api/attack", json={
            "init_data": _init_data(NONMEMBER["id"]), "opponent_id": str(PLAYER["id"]),
        })
        self.assertEqual(attack.status, 403)

        state = await self._get("/api/state", NONMEMBER)
        self.assertEqual(state.status, 200)

    async def test_a_confirmed_member_is_asked_about_once_not_once_per_button(self):
        """Both the XP resolve and the membership check are Telegram round trips, and the
        page makes one of each per tap. A confirmed answer is reused briefly so a burst of
        clicking costs one lookup rather than one per click."""
        self._tame(PLAYER)
        self.member_calls.clear()
        self.resolve_calls.clear()

        for _ in range(5):
            answer = await self._action(PLAYER, "notifications")
            self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.member_calls, [PLAYER["id"]])
        self.assertEqual(self.resolve_calls, [PLAYER["id"]])

    async def test_a_refusal_is_never_cached(self):
        """The dangerous half of caching a gate. A denial must always be re-derived: a
        stranger who joins, or somebody who writes their first chat message, has to be let
        in on their very next tap rather than after a cache expires -- and a 'no' that
        could be served from memory is a 'no' that outlives the fact behind it."""
        self.member_calls.clear()
        self.resolve_calls.clear()

        # A non-member is refused, and asked about again every single time.
        for _ in range(3):
            blocked = await self.client.post(pets_web.ROUTE_PREFIX + "/api/action", json={
                "init_data": _init_data(NONMEMBER["id"]), "action": "notifications",
            })
            self.assertEqual(blocked.status, 403)
        self.assertEqual(self.member_calls.count(NONMEMBER["id"]), 3)

        # Same for somebody the chat has never seen: never cached, so writing their first
        # message lets them straight in.
        for _ in range(3):
            ghost = await self.client.post(pets_web.ROUTE_PREFIX + "/api/action", json={
                "init_data": _init_data(UNTRACKED["id"]), "action": "notifications",
            })
            self.assertEqual(ghost.status, 403)
        self.assertEqual(self.resolve_calls.count(UNTRACKED["id"]), 3)

    # ---- money audit -----------------------------------------------------------------

    async def test_money_audit_is_hidden_from_players_and_rechecked_by_its_own_gate(self):
        player_state = await (await self._get("/api/state", PLAYER)).json()
        quest_mod_state = await (await self._get("/api/state", MODERATOR)).json()
        admin_state = await (await self._get("/api/state", THIRD)).json()
        self.assertFalse(player_state["is_economy_admin"])
        self.assertFalse(quest_mod_state["is_economy_admin"])
        self.assertTrue(admin_state["is_economy_admin"])

        denied = await self._get("/api/economy/audit", PLAYER)
        self.assertEqual(denied.status, 403)
        self.assertEqual((await denied.json())["error"], "NOT_AN_ECONOMY_ADMIN")


    # ---- the pause switch ----------------------------------------------------------
    # Methods on the existing suite rather than a subclass of it: a subclass inherits
    # and re-runs every parent test, which is a hundred-odd duplicates for four new
    # assertions. Each one reopens the game via addCleanup -- a leaked pause would fail
    # every later test in the file with a 503 and no obvious cause.

    async def test_actions_are_refused_while_reads_keep_working(self):
        self._tame(PLAYER)
        self.addCleanup(maintenance.resume)
        maintenance.pause("Обновление, минут на пять")

        refused = await self.client.post(pets_web.ROUTE_PREFIX + "/api/action", json={
            "init_data": _init_data(PLAYER["id"]), "action": "notifications",
        })
        self.assertEqual(refused.status, 503, await refused.text())
        body = await refused.json()
        self.assertEqual(body["error"], "PAUSED")
        self.assertIn("минут на пять", body["message"])

        # Fighting is the thing most worth stopping mid-deploy.
        attack = await self.client.post(pets_web.ROUTE_PREFIX + "/api/attack", json={
            "init_data": _init_data(PLAYER["id"]), "opponent_id": str(OPPONENT["id"]),
        })
        self.assertEqual(attack.status, 503, await attack.text())

        # And the screen still draws, carrying the reason with it.
        state = await self._get("/api/state", PLAYER)
        self.assertEqual(state.status, 200)
        payload = await state.json()
        self.assertTrue(payload["maintenance"]["paused"])
        self.assertIn("минут на пять", payload["maintenance"]["notice"])
        self.assertEqual(payload["pet"]["name"], "Кабанчик")

    async def test_everything_works_again_the_moment_it_is_reopened(self):
        self._tame(PLAYER)
        self.addCleanup(maintenance.resume)
        maintenance.pause("Обновление")
        self.assertEqual(
            (await self._action_raw(PLAYER, "notifications")).status, 503)
        maintenance.resume()
        answer = await self._action(PLAYER, "notifications")
        self.assertTrue(answer["ok"], answer)
        self.assertFalse(answer["state"]["maintenance"]["paused"])

    async def test_only_an_admin_can_flip_it_and_that_route_ignores_the_pause(self):
        """The switch that reopens the game has to work while the game is closed."""
        self.addCleanup(maintenance.resume)
        maintenance.pause("Обновление")
        denied = await self.client.post(pets_web.ROUTE_PREFIX + "/api/maintenance", json={
            "init_data": _init_data(PLAYER["id"]), "paused": False,
        })
        self.assertEqual(denied.status, 403, await denied.text())
        self.assertTrue(maintenance.is_paused())

        reopened = await self.client.post(pets_web.ROUTE_PREFIX + "/api/maintenance", json={
            "init_data": _init_data(THIRD["id"]), "paused": False,
        })
        self.assertEqual(reopened.status, 200, await reopened.text())
        self.assertFalse((await reopened.json())["paused"])
        self.assertFalse(maintenance.is_paused())

    async def test_the_page_shows_one_banner_for_the_whole_app(self):
        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        self.assertIn(".maint-bar {", page)
        self.assertIn('bar.id = "maintBar"', page)
        self.assertIn("maintenance.paused", page)
        self.assertIn("function maintenancePanel(state)", page)
        self.assertIn('data-maint="', page)

    # ---- economy + progression overview ------------------------------------------------

    async def test_the_overview_is_behind_the_same_gate_as_the_audit(self):
        for user in (PLAYER, MODERATOR):
            with self.subTest(user=user["username"]):
                denied = await self._get("/api/economy/overview", user)
                self.assertEqual(denied.status, 403, await denied.text())
                self.assertEqual((await denied.json())["error"], "NOT_AN_ECONOMY_ADMIN")
        allowed = await self._get("/api/economy/overview", THIRD)
        self.assertEqual(allowed.status, 200, await allowed.text())

    async def test_the_overview_totals_reconcile_with_its_own_daily_and_source_columns(self):
        """Three views of one ledger read. If the day column, the source column and the
        headline total ever disagree, at least two of them are lying."""
        self._tame(PLAYER)
        self._tame(THIRD, name="Второй")
        economy.grant(CHAT, PLAYER["id"], 300, "pet_mob_win")
        economy.grant(CHAT, PLAYER["id"], 120, "grant:pet:farm:run-1")
        economy.grant(CHAT, PLAYER["id"], -80, "buy:pet_item:w001")
        economy.grant(CHAT, THIRD["id"], 200, "daily_bonus")

        body = await (await self._get(
            "/api/economy/overview?days=7&user_id=" + str(PLAYER["id"]), THIRD)).json()
        flow = body["flow"]
        self.assertEqual(body["selected"], str(PLAYER["id"]))
        self.assertEqual(flow["days"], 7)
        self.assertEqual(len(flow["daily"]), 7)
        self.assertEqual(flow["players"], 2)

        self.assertEqual(sum(d["total_earned"] for d in flow["daily"]), flow["totals"]["earned"])
        self.assertEqual(sum(s["earned"] for s in flow["sources"]), flow["totals"]["earned"])
        self.assertEqual(sum(d["mine_earned"] for d in flow["daily"]), flow["mine"]["earned"])
        self.assertEqual(flow["totals"]["earned"], 620)
        self.assertEqual(flow["totals"]["spent"], 80)
        # The selected player's own slice, drawn inside the chat's bar on screen.
        self.assertEqual(flow["mine"]["earned"], 420)
        self.assertEqual(flow["mine"]["spent"], 80)

        by_code = {row["code"]: row for row in flow["sources"]}
        self.assertEqual(by_code["mobs"]["earned"], 300)
        self.assertEqual(by_code["farm"]["earned"], 120)
        self.assertEqual(by_code["daily"]["earned"], 200)
        self.assertEqual(by_code["purchases"]["spent"], 80)
        # The comparison line is per ACTIVE player, and that denominator is the window's,
        # not the day's -- 620 minted between two players reads as 310 each.
        self.assertEqual(by_code["mobs"]["average_earned"], 150)
        self.assertEqual(by_code["daily"]["players"], 1)

    async def test_the_overview_places_the_selected_player_in_the_field(self):
        """The progression half: a distribution for everybody plus where one player sits,
        which is the thing a bare leaderboard cannot show."""
        self._tame(PLAYER)
        self._tame(THIRD, name="Второй")
        data = pets._load(CHAT)
        data["pets"][str(PLAYER["id"])]["level"] = 12
        data["pets"][str(THIRD["id"])]["level"] = 4
        data["rubies"] = {str(PLAYER["id"]): 50, str(THIRD["id"]): 10}
        pets._save(CHAT, data)

        body = await (await self._get(
            "/api/economy/overview?user_id=" + str(PLAYER["id"]), THIRD)).json()
        progression = body["progression"]
        self.assertEqual(progression["players"], 2)
        self.assertTrue(progression["has_selected"])

        level = progression["measures"]["level"]
        self.assertEqual(level["mine"], 12)
        self.assertEqual(level["max"], 12)
        self.assertEqual(level["average"], 8)
        self.assertEqual(level["percentile"], 100)          # nobody is above them
        self.assertTrue(level["histogram"])
        self.assertEqual(sum(b["count"] for b in level["histogram"]), 2)

        self.assertEqual(progression["measures"]["rubies"]["mine"], 50)
        self.assertEqual(progression["measures"]["rubies"]["median"], 30)
        # All-time counters ride along so the faucets can be read without a second request.
        self.assertIn("farm_gold_minted", progression["metrics"])

    async def test_the_overview_survives_an_empty_chat_and_an_unknown_player(self):
        body = await (await self._get("/api/economy/overview?user_id=999999", THIRD)).json()
        self.assertEqual(body["flow"]["totals"]["earned"], 0)
        self.assertEqual(body["flow"]["players"], 0)
        self.assertEqual(body["progression"]["players"], 0)
        self.assertFalse(body["progression"]["has_selected"])
        # Every measure still reports, with no "mine" to place -- the screen draws the
        # field and says the player has no creature rather than breaking.
        self.assertIsNone(body["progression"]["measures"]["level"]["mine"])
        self.assertIsNone(body["progression"]["measures"]["level"]["percentile"])

    async def test_a_tap_shows_itself_immediately_and_for_as_long_as_it_takes(self):
        """A slow answer must not be indistinguishable from a button that never noticed
        the press. Two halves: :active paints in the same frame as the finger without any
        JavaScript, and .pressed is held for as long as the handler's work is in flight."""
        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        self.assertIn(".go:active:not(:disabled)", page)
        self.assertIn(".dungeon-enemy:active:not(:disabled)", page)
        self.assertIn(".pressed {", page)
        self.assertIn("@keyframes pressspin", page)
        # Without this the browser sits on the tap for 300ms before dispatching at all.
        self.assertIn("touch-action: manipulation", page)
        self.assertIn("prefers-reduced-motion: reduce", page)

        # The busy class is applied by the delegated handler and released when the work
        # settles, whatever it was.
        self.assertIn('target.classList.add("pressed")', page)
        self.assertIn('target.classList.remove("pressed")', page)
        self.assertIn(".finally(release)", page)
        self.assertIn("async function handleClick(event, target)", page)

    async def test_the_inventory_only_travels_to_screens_that_draw_items(self):
        """The bag was three quarters of every response, re-sent on every button press --
        including dungeon fights, which draw no items at all. It now goes only where it is
        rendered, and `null` means "not sent" so the client fetches instead of drawing a
        stale or empty one."""
        self._tame(PLAYER)
        for code in [item.code for item in C.items_for_slot("weapon")[:12]]:
            pets._load(CHAT)
        data = pets._load(CHAT)
        data["pets"][str(PLAYER["id"])]["inventory"] = [
            item.code for item in C.items_for_slot("weapon")[:12]
        ]
        pets._save(CHAT, data)

        for view in ("hero", "bag", "shop"):
            with self.subTest(view=view):
                body = await (await self._get("/api/state?view=" + view, PLAYER)).json()
                self.assertEqual(len(body["bag"]), 12)
        for view in ("dungeon", "arena", "farm", "quests", "more"):
            with self.subTest(view=view):
                body = await (await self._get("/api/state?view=" + view, PLAYER)).json()
                self.assertIsNone(body["bag"])

        # An unknown or absent view still gets it: withholding it wrongly shows an empty
        # bag, while sending it needlessly only costs bytes.
        self.assertIsNotNone((await (await self._get("/api/state", PLAYER)).json())["bag"])
        self.assertIsNotNone(
            (await (await self._get("/api/state?view=whatever", PLAYER)).json())["bag"])

        # And an action carries the same view, so a dungeon fight answers small.
        fight_view = await self._action(PLAYER, "notifications", view="dungeon")
        self.assertIsNone(fight_view["state"]["bag"])
        bag_view = await self._action(PLAYER, "notifications", view="bag")
        self.assertEqual(len(bag_view["state"]["bag"]), 12)

    async def test_a_response_reports_how_long_the_server_itself_took(self):
        """Without this, "the button waits two seconds" cannot be attributed: server time
        and connection time are indistinguishable from the outside."""
        self._tame(PLAYER)
        state = await self._get("/api/state", PLAYER)
        self.assertIn("Server-Timing", state.headers)
        self.assertRegex(state.headers["Server-Timing"], r"^app;dur=[0-9.]+$")

        action = await self.client.post(pets_web.ROUTE_PREFIX + "/api/action", json={
            "init_data": _init_data(PLAYER["id"]), "action": "notifications",
        })
        self.assertEqual(action.status, 200)
        self.assertIn("Server-Timing", action.headers)
        # A local action is fast; the assertion is that the number is real, not that it
        # is small -- a slow machine must not fail the suite.
        self.assertGreaterEqual(float(action.headers["Server-Timing"].split("=")[1]), 0.0)

        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        self.assertIn("LAST_TIMING", page)
        self.assertIn('response.headers.get("Server-Timing")', page)

    async def test_the_page_fetches_the_bag_before_drawing_it(self):
        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        self.assertIn('const BAG_VIEWS = new Set(["hero", "bag", "shop"]);', page)
        self.assertIn("async function ensureBag()", page)
        self.assertIn("if (BAG_VIEWS.has(TAB)) await ensureBag();", page)
        # Every action tells the server which screen is open.
        self.assertIn('Object.assign({ action, view: TAB }, payload || {})', page)
        self.assertIn('api("/api/state?view=" + encodeURIComponent(TAB))', page)
        # A not-yet-fetched bag must never render as an empty one.
        self.assertIn('if (!S.bag) { box.innerHTML = \'<div class="empty">Загружаю сумку…</div>\'', page)

    async def test_the_forge_button_forges_instead_of_opening_a_slot(self):
        """The whole app is one delegated if-chain over data- attributes, so a button that
        carries an attribute tested EARLIER than its own is silently answered by the wrong
        branch. It shipped exactly that way: the forge button carried `data-slot` to say
        which kind of item to melt, `d.slot` is tested further up and opens the equipment
        sheet, and pressing «Перековать» opened the weapon window instead of forging.
        """
        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        order = re.findall(r"^  if \(d\.([a-z]+)", page, re.M)
        self.assertIn("reforge", order, "the forge branch must exist to be reached")

        button = re.search(r"<button class=\"go sec\" data-reforge=[^\n]*", page).group(0)
        carried = set(re.findall(r"data-([a-z]+)=", button))
        self.assertIn("reforge", carried)
        # Every OTHER attribute it carries must be pure payload -- something the chain
        # never dispatches on -- or the branch that owns it answers first.
        for name in carried - {"reforge"}:
            with self.subTest(attribute=name):
                self.assertNotIn(
                    name, order,
                    f"кнопка ковки несёт data-{name}, а d.{name} перехватывает её раньше",
                )
        # And the kind of item travels under its own name.
        self.assertIn("forgeslot", carried)
        # The cursed ladder shares "rare" and "legendary" with the ordinary ladder, so the
        # button has to carry which one it is -- lost between button and action, a cursed
        # recipe would silently forge as the ordinary one.
        self.assertIn("forgecursed", carried)
        self.assertIn(
            'if (d.reforge) { await act("reforge", '
            '{ rarity: d.reforge, slot: d.forgeslot || "", cursed: !!d.forgecursed }); return; }',
            page,
        )

    async def test_a_dungeon_replay_is_faster_and_obeys_the_skip_preference(self):
        """The dungeon's slowness was never the server -- it was ten seconds of replay
        animation after it had already answered."""
        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        self.assertIn("const DUEL_ROUND_MS = 300;", page)
        self.assertNotIn(": 520);", page)
        # One preference about watching replays, honoured for the dungeon boss too.
        self.assertIn(
            "if (data.battle && !(S.pet && S.pet.skip_pve_replays)) playDuel(data.battle);",
            page,
        )
        self.assertIn("data.pve || data.dungeon ?", page)

    async def test_the_overview_screen_is_wired_into_the_page(self):
        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        self.assertIn('econstats:📊 Экономика и прогресс', page)
        self.assertIn('moreView === "econstats"', page)
        self.assertIn("function economyOverview(data)", page)
        self.assertIn('api("/api/economy/overview" + query)', page)
        self.assertIn("data-statsdays=", page)
        self.assertIn("data-statsmetric=", page)
        self.assertIn("data-statsuser", page)

    async def test_fight_audit_has_public_page_pet_filter_and_lookup(self):
        page = await self.client.get("/audit")
        self.assertEqual(page.status, 200)
        html = await page.text()
        self.assertIn("Fight audit", html)
        self.assertIn('id="petSearch"', html)
        self.assertIn('id="petSuggestions"', html)
        self.assertIn("showPetSuggestions()", html)
        self.assertIn('data-pet="', html)
        self.assertIn("function auditItem(i)", html)
        self.assertIn("Items and exact effects", html)
        self.assertIn("Combat effect snapshot", html)
        self.assertNotIn('id="auditKey"', html)

        data = pets._load(CHAT)
        data["fight_audits"].append({
            "fight_id": "F-20260815-ABCDEF123456", "kind": "pve",
            "at": "2026-08-15T12:00:00+00:00", "winner": "42", "draw": False,
            "fighters": {"42": {"fighter": {"name": "Hero"}}}, "moves": [],
        })
        data["fight_audits"].append({
            "fight_id": "F-20260815-222222222222", "kind": "arena",
            "at": "2026-08-15T12:05:00+00:00", "winner": "43", "draw": False,
            "fighters": [{"key": "42", "name": "Hero"}, {"key": "43", "name": "Rival"}],
            "moves": 8,
        })
        data["fight_audits"].append({
            "fight_id": "F-20260815-333333333333", "kind": "pve",
            "at": "2026-08-15T12:10:00+00:00", "winner": "43", "draw": False,
            "fighters": [{"key": "43", "name": "Rival"}, {"key": "mob:rat", "name": "Rat"}],
            "moves": 4,
        })
        data.setdefault("fights", []).append({
            "ts": "2026-08-15T12:07:00+00:00", "attacker_id": "44", "defender_id": "45",
            "attacker_name": "Older Hero", "defender_name": "Older Rival",
            "winner_id": "44", "draw": False,
        })
        pets._save(CHAT, data)

        listed = await self.client.get("/audit/api/fights")
        self.assertEqual(listed.status, 200)
        listed_body = await listed.json()
        self.assertEqual(listed_body["fights"][0]["fight_id"], "F-20260815-333333333333")
        self.assertEqual(
            {row["user_id"] for row in listed_body["pets"]}, {"42", "43", "44", "45"},
        )
        self.assertTrue(any(row.get("historic") for row in listed_body["fights"]))
        filtered = await self.client.get(
            "/audit/api/fights?pet_id=42&limit=500", headers=self._auth(THIRD),
        )
        filtered_body = await filtered.json()
        self.assertEqual(filtered_body["selected_pet"], "42")
        self.assertEqual(
            [row["fight_id"] for row in filtered_body["fights"]],
            ["F-20260815-222222222222", "F-20260815-ABCDEF123456"],
        )
        found = await self.client.get(
            "/audit/api/fights?id=F-20260815-ABCDEF123456", headers=self._auth(THIRD),
        )
        self.assertEqual((await found.json())["fight"]["kind"], "pve")

    async def test_money_audit_returns_user_picker_hour_graph_and_source_net(self):
        moment = datetime(2026, 8, 12, 18, 20, tzinfo=timezone.utc)
        with patch("economy.app_now", return_value=moment):
            economy.grant(CHAT, PLAYER["id"], 40, "grant:quest:submission-1")
            economy.grant(CHAT, PLAYER["id"], -10, "wager:casino:poker")
            economy.grant(CHAT, PLAYER["id"], 20, "wager_payout:casino:poker")
            response = await self._get(
                f"/api/economy/audit?user_id={PLAYER['id']}&hours=24", THIRD,
            )
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["selected"], str(PLAYER["id"]))
        self.assertEqual(body["windows"], [24, 72, 168])
        self.assertTrue(any(row["user_id"] == str(PLAYER["id"]) for row in body["users"]))
        self.assertEqual(len(body["report"]["hourly"]), 24)
        poker = next(row for row in body["report"]["sources"] if row["code"] == "casino_poker")
        self.assertEqual((poker["earned"], poker["spent"], poker["net"]), (20, 10, 10))
        quest = next(row for row in body["report"]["sources"] if row["code"] == "quests")
        self.assertEqual(quest["earned"], 40)
        self.assertTrue(body["report"]["xp_not_hourly"])

    async def _income(self, query=""):
        response = await self.client.get("/audit/api/income" + query)
        self.assertEqual(response.status, 200)
        return await response.json()

    def _record_chat_day(self, day, who, messages=30):
        """One recorded day of real chatter, so the XP-derived coin faucet has something
        to derive from -- it reads the day files, not the ledger."""
        start = datetime(day.year, day.month, day.day, 9, tzinfo=timezone.utc)
        stats.record_day(CHAT, day, [
            SimpleNamespace(
                sender_id=who["id"], sender_name=who["first_name"],
                sender_username=who["username"],
                text="достаточно длинное сообщение для подсчёта очков",
                dt_local=start + timedelta(minutes=index), message_id=index,
                is_reply=False,
            )
            for index in range(1, messages + 1)
        ])

    def _seed_income(self, moment):
        """Two players earning both currencies from different faucets, so every filter
        below has something it can actually change."""
        with patch("economy.app_now", return_value=moment), \
                patch("pets.app_now", return_value=moment):
            economy.grant(CHAT, PLAYER["id"], 300, "grant:quest:submission-1")
            economy.grant(CHAT, PLAYER["id"], 100, "pet_mob_win")
            economy.grant(CHAT, OPPONENT["id"], 100, "grant:pet:farm:shift-1")
            pets.grant_rubies(CHAT, PLAYER["id"], 9, "pet_mob_win")
            pets.grant_rubies_once(CHAT, PLAYER["id"], 3, "quarry:run-1")
            pets.grant_rubies_once(CHAT, OPPONENT["id"], 6, "dungeon-ruby:mob:token-1")

    async def test_income_audit_splits_both_currencies_by_source_with_shares(self):
        moment = datetime(2026, 8, 12, 18, 20, tzinfo=timezone.utc)
        self._seed_income(moment)
        with patch("economy.app_now", return_value=moment), \
                patch("pets.app_now", return_value=moment):
            body = await self._income("?days=30")

        coins = {row["code"]: row for row in body["coins"]["sources"]}
        self.assertEqual(coins["quests"]["earned"], 300)
        self.assertEqual(coins["mobs"]["earned"], 100)
        self.assertEqual(coins["farm"]["earned"], 100)
        # Shares are of everything minted in the window and must account for all of it.
        self.assertAlmostEqual(
            sum(row["share"] for row in body["coins"]["sources"]), 1.0, places=6,
        )
        self.assertAlmostEqual(coins["quests"]["share"], 0.6, places=6)

        rubies = {row["code"]: row for row in body["rubies"]["sources"]}
        self.assertEqual(rubies["mobs"]["earned"], 9)
        self.assertEqual(rubies["quarry"]["earned"], 3)
        self.assertEqual(rubies["dungeon_mobs"]["earned"], 6)
        self.assertEqual(body["rubies"]["totals"]["earned"], 18)
        self.assertAlmostEqual(rubies["mobs"]["share"], 0.5, places=6)

        # The two currencies stay separate -- a coin source never leaks into the diamonds.
        self.assertNotIn("quests", rubies)
        self.assertEqual(body["windows"], [1, 7, 30, 90, 365])

    async def test_income_audit_reports_per_player_shares_and_filters_to_chosen_players(self):
        moment = datetime(2026, 8, 12, 18, 20, tzinfo=timezone.utc)
        self._seed_income(moment)
        with patch("economy.app_now", return_value=moment), \
                patch("pets.app_now", return_value=moment):
            everybody = await self._income("?days=30")
            filtered = await self._income(f"?days=30&user_ids={PLAYER['id']}")

        rows = {row["user_id"]: row for row in everybody["coins"]["players"]}
        self.assertEqual(rows[str(PLAYER["id"])]["earned"], 400)
        self.assertEqual(rows[str(PLAYER["id"])]["by_source"]["quests"], 300)
        self.assertEqual(rows[str(OPPONENT["id"])]["earned"], 100)
        self.assertAlmostEqual(rows[str(OPPONENT["id"])]["share"], 0.2, places=6)

        # Filtering restricts the population AND restates the percentages against it, so
        # the one remaining player is 100% of the income being looked at.
        self.assertEqual(
            [row["user_id"] for row in filtered["coins"]["players"]], [str(PLAYER["id"])],
        )
        self.assertAlmostEqual(filtered["coins"]["players"][0]["share"], 1.0, places=6)
        self.assertEqual(filtered["rubies"]["totals"]["earned"], 12)
        self.assertEqual(filtered["filters"]["matched"], 1)
        # The picker still offers everybody, or a filter could never be widened again.
        self.assertIn(str(OPPONENT["id"]), {row["user_id"] for row in filtered["roster"]})

    async def test_income_audit_filters_by_pet_level_and_excludes_the_petless(self):
        moment = datetime(2026, 8, 12, 18, 20, tzinfo=timezone.utc)
        self._seed_income(moment)
        data = pets._load(CHAT)
        data["pets"] = {
            str(PLAYER["id"]): {"name": "Hero", "level": 12},
            str(OPPONENT["id"]): {"name": "Rival", "level": 3},
        }
        pets._save(CHAT, data)
        with patch("economy.app_now", return_value=moment), \
                patch("pets.app_now", return_value=moment):
            high = await self._income("?days=30&min_level=10")
            low = await self._income("?days=30&min_level=1&max_level=5")

        self.assertEqual(
            [row["user_id"] for row in high["coins"]["players"]], [str(PLAYER["id"])],
        )
        self.assertEqual(
            [row["user_id"] for row in low["coins"]["players"]], [str(OPPONENT["id"])],
        )
        self.assertEqual(low["rubies"]["totals"]["earned"], 6)
        self.assertEqual(high["level_range"], {"min": 3, "max": 12})
        self.assertEqual(
            {row["user_id"]: row["level"] for row in high["roster"]}[str(PLAYER["id"])], 12,
        )

    async def test_income_audit_window_excludes_older_rows(self):
        old = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
        recent = datetime(2026, 8, 12, 18, 20, tzinfo=timezone.utc)
        with patch("economy.app_now", return_value=old), patch("pets.app_now", return_value=old):
            economy.grant(CHAT, PLAYER["id"], 500, "grant:quest:old-one")
            pets.grant_rubies(CHAT, PLAYER["id"], 40, "pet_mob_win")
        with patch("economy.app_now", return_value=recent), \
                patch("pets.app_now", return_value=recent):
            economy.grant(CHAT, PLAYER["id"], 20, "grant:quest:new-one")
            pets.grant_rubies(CHAT, PLAYER["id"], 2, "pet_mob_win")
            week = await self._income("?days=7")
            forever = await self._income("?days=all")

        self.assertEqual(week["coins"]["totals"]["ledger_earned"], 20)
        self.assertEqual(week["rubies"]["totals"]["earned"], 2)
        self.assertEqual(forever["coins"]["totals"]["ledger_earned"], 520)
        self.assertEqual(forever["rubies"]["totals"]["earned"], 42)
        self.assertIsNone(forever["filters"]["days"])

    async def test_income_audit_counts_chat_xp_coins_as_their_own_estimated_source(self):
        moment = datetime(2026, 8, 12, 18, 20, tzinfo=timezone.utc)
        self._record_chat_day(date(2026, 8, 11), PLAYER)
        with patch("economy.app_now", return_value=moment), \
                patch("pets.app_now", return_value=moment):
            economy.grant(CHAT, PLAYER["id"], 100, "grant:quest:submission-1")
            body = await self._income("?days=30")

        chat = next(
            row for row in body["coins"]["sources"] if row["code"] == "chat_activity"
        )
        # Derived from XP rather than from a ledger row, and it has to say so on screen.
        self.assertTrue(chat["estimate"])
        self.assertEqual(chat["transactions"], 0)
        self.assertGreater(chat["earned"], 0)
        self.assertEqual(
            body["coins"]["totals"]["earned"],
            body["coins"]["totals"]["ledger_earned"] + chat["earned"],
        )
        # The ledger-only sources are diluted by it, which is the entire point.
        quests_row = next(r for r in body["coins"]["sources"] if r["code"] == "quests")
        self.assertLess(quests_row["share"], 1.0)
        self.assertAlmostEqual(
            sum(row["share"] for row in body["coins"]["sources"]), 1.0, places=6,
        )

    async def test_income_audit_reconstructs_all_time_diamonds_and_admits_its_gaps(self):
        moment = datetime(2026, 8, 12, 18, 20, tzinfo=timezone.utc)
        with patch("pets.app_now", return_value=moment):
            # Only grant_rubies_once writes ruby_sources; the mob drop never did, so the
            # reconstruction must under-report and the coverage figure must show it.
            pets.grant_rubies_once(CHAT, PLAYER["id"], 30, "quarry:run-1")
            pets.grant_rubies(CHAT, PLAYER["id"], 70, "pet_mob_win")
            body = await self._income("?days=30")

        history = body["rubies_all_time"]
        self.assertEqual(history["minted_all_time"], 100)
        self.assertEqual(history["explained"], 30)
        self.assertAlmostEqual(history["coverage"], 0.3, places=6)
        self.assertEqual(
            [(row["code"], row["earned"]) for row in history["sources"]], [("quarry", 30)],
        )

    async def test_ruby_ledger_records_every_faucet_and_sink(self):
        moment = datetime(2026, 8, 12, 18, 20, tzinfo=timezone.utc)
        with patch("pets.app_now", return_value=moment):
            pets.grant_rubies(CHAT, PLAYER["id"], 50, "pet_mob_win")
            pets.grant_rubies_once(CHAT, PLAYER["id"], 5, "arena-ruby:a:1:2")
            # A replayed settlement must not be logged twice.
            pets.grant_rubies_once(CHAT, PLAYER["id"], 5, "arena-ruby:a:1:2")
            data = pets._load(CHAT)
            data["pets"] = {str(PLAYER["id"]): {
                "name": "Hero", "level": 1, "xp": 10_000,
                "stats": {key: C.STAT_MIN_LEVEL + 2 for key in C.STAT_KEYS},
            }}
            pets._save(CHAT, data)
            self.assertTrue(pets.respec_stats(CHAT, PLAYER["id"])[0])

        rows = [(row["reason"], row["delta"]) for row in pets.ruby_log_rows(CHAT)]
        self.assertEqual(rows, [
            ("pet_mob_win", 50),
            ("arena-ruby:a:1:2", 5),
            ("spend:respec", -C.STAT_RESPEC_RUBY_COST),
        ])
        self.assertEqual(pets.ruby_source_of("spend:respec"), "respec")

        with patch("pets.app_now", return_value=moment):
            report = pets.ruby_income_report(CHAT, days=30)
        respec = next(row for row in report["sources"] if row["code"] == "respec")
        self.assertEqual((respec["earned"], respec["spent"]), (0, C.STAT_RESPEC_RUBY_COST))
        # A pure sink is 0% of income and is read off the spent column instead.
        self.assertEqual(respec["share"], 0.0)

    async def test_income_audit_splits_the_dungeon_into_mobs_bosses_and_its_own_sink(self):
        moment = datetime(2026, 8, 12, 18, 20, tzinfo=timezone.utc)
        with patch("economy.app_now", return_value=moment), \
                patch("pets.app_now", return_value=moment):
            economy.grant(CHAT, PLAYER["id"], 300, "pet_dungeon_boss_win")
            economy.grant(CHAT, PLAYER["id"], 120, "pet_dungeon_mob_win")
            # Written before the split existed: it has no boss flag and must not be
            # silently counted as either kind.
            economy.grant(CHAT, PLAYER["id"], 90, "pet_dungeon_win")
            economy.grant(CHAT, PLAYER["id"], -40, "pet_dungeon_heal")
            pets.grant_rubies_once(CHAT, PLAYER["id"], 4, "dungeon-ruby:boss:run-1:5:0:1")
            pets.grant_rubies_once(CHAT, PLAYER["id"], 1, "dungeon-ruby:mob:run-1:5:1:2")
            pets.grant_rubies_once(CHAT, PLAYER["id"], 7, "dungeon-ruby:legacy-token")
            body = await self._income("?days=30")

        coins = {row["code"]: row for row in body["coins"]["sources"]}
        self.assertEqual(coins["dungeon_boss"]["earned"], 300)
        self.assertEqual(coins["dungeon_mobs"]["earned"], 120)
        self.assertEqual(coins["dungeon_legacy"]["earned"], 90)
        self.assertEqual(coins["dungeon_heal"]["spent"], 40)
        # The whole point of the change: none of it lands in the catch-all any more.
        self.assertNotIn("other", coins)

        rubies = {row["code"]: row for row in body["rubies"]["sources"]}
        self.assertEqual(rubies["dungeon_boss"]["earned"], 4)
        self.assertEqual(rubies["dungeon_mobs"]["earned"], 1)
        self.assertEqual(rubies["dungeon_legacy"]["earned"], 7)
        # Both currencies name the dungeon the same way, so the two can be read together.
        self.assertEqual(coins["dungeon_boss"]["name"], rubies["dungeon_boss"]["name"])

    def test_a_dungeon_kill_records_whether_it_was_a_boss_in_both_currencies(self):
        for boss, coin_reason, ruby_kind in (
            (True, "pet_dungeon_boss_win", "boss"), (False, "pet_dungeon_mob_win", "mob"),
        ):
            with self.subTest(boss=boss):
                self.assertEqual(economy._audit_source(coin_reason),
                                 "dungeon_boss" if boss else "dungeon_mobs")
                self.assertEqual(pets.ruby_source_of(f"dungeon-ruby:{ruby_kind}:tok"),
                                 "dungeon_boss" if boss else "dungeon_mobs")
        self.assertEqual(economy._audit_source("pet_dungeon_win"), "dungeon_legacy")
        self.assertEqual(pets.ruby_source_of("dungeon-ruby:tok"), "dungeon_legacy")

    # ---- news rewards -------------------------------------------------------------------

    def _rewarded_update(self, amount=1, code="test-reward"):
        """Pin a rewarding note in place of the shipped log, so these tests keep passing
        when the real changelog gains entries."""
        note = pets_updates.Update(code, "Награда", "Текст", reward_rubies=amount)
        return patch.object(pets_updates, "UPDATES", (note,))

    async def test_a_news_reward_is_paid_once_and_shows_as_claimed_afterwards(self):
        with self._rewarded_update(amount=3):
            listing = await self._get("/api/updates", PLAYER)
            row = (await listing.json())["rows"][0]
            self.assertEqual((row["id"], row["reward"], row["claimed"]),
                             ("test-reward", 3, False))

            claimed = await self._post("/api/updates/claim", PLAYER, {"id": "test-reward"})
            self.assertEqual(claimed.status, 200)
            self.assertEqual((await claimed.json())["rubies"], 3)
            self.assertEqual(pets.ruby_balance(CHAT, PLAYER["id"]), 3)

            # Pressing it twice must not pay twice, and the feed must say so.
            again = await self._post("/api/updates/claim", PLAYER, {"id": "test-reward"})
            self.assertEqual(again.status, 409)
            self.assertEqual((await again.json())["error"], "ALREADY_CLAIMED")
            self.assertEqual(pets.ruby_balance(CHAT, PLAYER["id"]), 3)
            reread = await self._get("/api/updates", PLAYER)
            self.assertTrue((await reread.json())["rows"][0]["claimed"])

        # The payout is a real ledger row, so the income audit sees it as a grant.
        rows = [r for r in pets.ruby_log_rows(CHAT) if r["reason"].startswith("update-reward:")]
        self.assertEqual([(r["user_id"], r["delta"]) for r in rows], [(str(PLAYER["id"]), 3)])
        self.assertEqual(pets.ruby_source_of(rows[0]["reason"]), "grants")

    async def test_each_player_claims_their_own_copy_of_the_same_reward(self):
        """The grant key carries the user id; without it the first claimer would take the
        note's reward and everybody else would be silently refused."""
        with self._rewarded_update(amount=2):
            for who in (PLAYER, OPPONENT):
                response = await self._post("/api/updates/claim", who, {"id": "test-reward"})
                self.assertEqual(response.status, 200, who["username"])
            self.assertEqual(pets.ruby_balance(CHAT, PLAYER["id"]), 2)
            self.assertEqual(pets.ruby_balance(CHAT, OPPONENT["id"]), 2)

    async def test_state_advertises_an_owed_reward_until_it_is_taken(self):
        with self._rewarded_update(amount=5):
            before = await (await self._get("/api/state", PLAYER)).json()
            self.assertEqual(before["updates_reward"], 5)
            self.assertTrue(before["unread_updates"])

            # Reading the log clears "unread" but must NOT clear what is still owed.
            await self._get("/api/updates", PLAYER)
            mid = await (await self._get("/api/state", PLAYER)).json()
            self.assertFalse(mid["unread_updates"])
            self.assertEqual(mid["updates_reward"], 5)

            await self._post("/api/updates/claim", PLAYER, {"id": "test-reward"})
            after = await (await self._get("/api/state", PLAYER)).json()
            self.assertEqual(after["updates_reward"], 0)

    async def test_a_note_without_a_reward_cannot_be_claimed(self):
        plain = pets_updates.Update("plain", "Без награды", "Текст")
        with patch.object(pets_updates, "UPDATES", (plain,)):
            response = await self._post("/api/updates/claim", PLAYER, {"id": "plain"})
            self.assertEqual(response.status, 409)
            self.assertEqual((await response.json())["error"], "NO_REWARD")
            missing = await self._post("/api/updates/claim", PLAYER, {"id": "nope"})
            self.assertEqual(missing.status, 404)
        self.assertEqual(pets.ruby_balance(CHAT, PLAYER["id"]), 0)

    async def test_a_chat_written_note_can_never_carry_a_reward(self):
        """/arenanews is a writing tool, not a mint: whatever is stored, it reads as zero."""
        pets_updates.add(CHAT, "Своя новость", "Текст", author_id=MODERATOR["id"])
        written = pets_updates.custom(CHAT)[0]
        self.assertEqual(written.reward_rubies, 0)
        response = await self._post("/api/updates/claim", PLAYER, {"id": written.id})
        self.assertEqual(response.status, 409)

    async def test_the_hud_carries_a_news_button_that_turns_into_an_animated_gift(self):
        page = await self.client.get(pets_web.ROUTE_PREFIX + "/", headers=self._auth(PLAYER))
        html = await page.text()
        self.assertIn('id="hudNews"', html)
        # Beside the mailbox, not buried in «Ещё».
        self.assertLess(html.index('id="hudMail"'), html.index('id="hudNews"'))
        self.assertIn("@keyframes nudge", html)
        self.assertIn('news.classList.toggle("gift", fresh)', html)
        # Motion is dropped for anyone who asked the OS for less of it.
        self.assertIn("prefers-reduced-motion", html)
        self.assertIn("🎁 Забрать награду", html)

    async def test_audit_page_offers_an_income_tab_with_all_three_filters(self):
        page = await self.client.get("/audit")
        html = await page.text()
        self.assertIn('<button id="tabIncome">Income</button>', html)
        for control in ("incDays", "incMinLevel", "incMaxLevel", "incSearch"):
            self.assertIn(f'id="{control}"', html)
        self.assertIn("/audit/api/income?", html)
        # Both currencies get their own single-hue chart; neither shares an axis with the
        # other, and no bar is shaded by its own value.
        self.assertIn("--coin:#c98500", html)
        self.assertIn("--gem:#3987e5", html)
        self.assertIn('currencySection("Coins"', html)
        self.assertIn('currencySection("Diamonds"', html)

    async def test_page_contains_admin_money_button_graph_and_user_selector(self):
        page = await self.client.get(pets_web.ROUTE_PREFIX + "/", headers=self._auth(THIRD))
        html = await page.text()
        self.assertIn("moneyaudit:🕵️ Денежный аудит", html)
        self.assertIn("data-auditfilter", html)
        self.assertIn("Имя, @username, существо или ID", html)
        self.assertIn("refreshAuditUserFilter()", html)
        self.assertIn("data-audituser", html)
        self.assertIn("audit-graph", html)

    # ---- equipment --------------------------------------------------------------------

    async def test_live_scroll_loadout_can_be_changed_without_inventory_items(self):
        self._tame(PLAYER)
        regular = SCROLLS.REGULAR_SCROLLS[-1]["code"]
        ultimate = SCROLLS.ULTIMATE_SCROLLS[-1]["code"]
        data = pets._load(CHAT)
        data["pets"][str(PLAYER["id"])]["owned_scrolls"].extend([regular, ultimate])
        pets._save(CHAT, data)

        wrong = await self._action(PLAYER, "set_skill", slot=1, code=ultimate)
        self.assertFalse(wrong["ok"])
        changed = await self._action(PLAYER, "set_skill", slot=2, code=regular)
        self.assertTrue(changed["ok"], changed["message"])
        changed = await self._action(PLAYER, "set_skill", slot=4, code=ultimate)
        self.assertTrue(changed["ok"], changed["message"])
        self.assertEqual(changed["state"]["skills"]["slots"][1]["code"], regular)
        self.assertEqual(changed["state"]["skills"]["slots"][3]["code"], ultimate)

    async def test_page_contains_live_scroll_picker_and_the_shield_paperdoll_slot(self):
        page = await self.client.get(pets_web.ROUTE_PREFIX + "/", headers=self._auth(PLAYER))
        html = await page.text()
        self.assertIn("data-liveskill", html)
        self.assertIn("data-liveskillset", html)
        self.assertIn("SCROLL_ELEMENTS", html)
        self.assertNotIn("можно увернуться", html)
        self.assertIn('["shield", "🛡 Щиты"]', html)

    async def test_equipping_and_unequipping_move_an_item_between_bag_and_slot(self):
        """The paperdoll and the bag are two views of the same inventory, not two separate
        stores -- equip must fill the slot and flip the bag card's `equipped` flag in the
        one response, and unequip must undo exactly that."""
        self._tame(PLAYER)
        # From this player's own shelf: every slot rotates now, and buy_item refuses
        # anything that is not currently on offer to THEM.
        item = pets.daily_storefront_items(CHAT, "amulet", user_id=PLAYER["id"])[0]
        self.assertTrue(pets.buy_item(CHAT, PLAYER["id"], RICH_XP, item.code)[0])

        equipped = await self._action(PLAYER, "equip", code=item.code)
        slot = next(s for s in equipped["state"]["equipment"] if s["slot"] == "amulet")
        self.assertEqual(slot["item"]["code"], item.code)
        in_bag = next(b for b in equipped["state"]["bag"] if b["code"] == item.code)
        self.assertTrue(in_bag["equipped"])

        unequipped = await self._action(PLAYER, "unequip", slot="amulet")
        slot = next(s for s in unequipped["state"]["equipment"] if s["slot"] == "amulet")
        self.assertIsNone(slot["item"])
        in_bag = next(b for b in unequipped["state"]["bag"] if b["code"] == item.code)
        self.assertFalse(in_bag["equipped"])

    # ---- forging ------------------------------------------------------------------------

    async def test_reforge_action_with_the_cursed_flag_grants_a_cursed_legendary(self):
        """The cursed ladder shares "rare" and "legendary" with the ordinary ladder, so
        the client's `cursed: true` payload is the only thing that tells the server which
        recipe was pressed. If it got dropped somewhere between the forge button and
        pets.reforge_items, a player who melted five rare cursed weapons through the Mini
        App would quietly get a plain legendary back instead of the cursed one they asked
        for -- which is the exact failure this flag exists to rule out.
        """
        self._tame(PLAYER)
        rare_cursed = [
            C.find_item(code) for code in sorted(pets_weapon_catalog.RARE_CURSED_CODES)
        ][:pets.FORGE_REQUIREMENTS["rare"]]
        data = pets._load(CHAT)
        data["pets"][str(PLAYER["id"])]["inventory"] = [item.code for item in rare_cursed]
        pets._save(CHAT, data)

        result = await self._action(PLAYER, "reforge", rarity="rare", slot="weapon", cursed=True)

        self.assertTrue(result["ok"], result.get("message"))
        old_codes = {item.code for item in rare_cursed}
        inventory = pets.get_pet(CHAT, PLAYER["id"])["inventory"]
        new_codes = [code for code in inventory if code not in old_codes]
        self.assertEqual(len(new_codes), 1)
        forged = C.find_item(new_codes[0])
        self.assertEqual(forged.rarity, "legendary")
        self.assertTrue(forged.cursed)
        # The response's own bag, not a follow-up read: this is what marks the item
        # cursed on the inventory card the player sees right after forging it.
        bag_entry = next(row for row in result["state"]["bag"] if row["code"] == forged.code)
        self.assertTrue(bag_entry["cursed"])

    async def test_reforge_action_without_the_cursed_flag_still_forges_the_ordinary_recipe(self):
        """Every recipe on the ordinary ladder, and every button drawn before this flag
        existed, sends no `cursed` field at all. The server default has to keep reading
        that as "not cursed" -- the only thing an absent field ever meant -- or an old
        client and the ordinary ladder both break at once.
        """
        self._tame(PLAYER)
        ordinary_rare = [
            item for item in C.ITEMS
            if item.slot == "weapon" and item.rarity == "rare" and item.source == "drop"
            and not getattr(item, "cursed", False)
        ][:pets.FORGE_REQUIREMENTS["rare"]]
        data = pets._load(CHAT)
        data["pets"][str(PLAYER["id"])]["inventory"] = [item.code for item in ordinary_rare]
        pets._save(CHAT, data)

        result = await self._action(PLAYER, "reforge", rarity="rare", slot="weapon")

        self.assertTrue(result["ok"], result.get("message"))
        old_codes = {item.code for item in ordinary_rare}
        inventory = pets.get_pet(CHAT, PLAYER["id"])["inventory"]
        new_codes = [code for code in inventory if code not in old_codes]
        self.assertEqual(len(new_codes), 1)
        forged = C.find_item(new_codes[0])
        self.assertEqual(forged.rarity, "legendary")
        self.assertFalse(getattr(forged, "cursed", False))

    # ---- selling ------------------------------------------------------------------------

    async def test_gear_can_still_be_changed_from_inside_a_dungeon_run(self):
        """The run gate used to refuse every action but the dungeon's own, which included
        the one thing a boss's stated weakness asks a player to do."""
        self._tame(PLAYER)
        data = pets._load(CHAT)
        record = data["pets"][str(PLAYER["id"])]
        record["inventory"].append("w001")
        record["dungeon_run"] = {
            "floor": 1, "hp": 10, "max_hp": 10, "cleared": [],
        }
        pets._save(CHAT, data)

        body = await self._action(PLAYER, "equip", code="w001")

        self.assertTrue(body["ok"], body["message"])
        self.assertEqual(
            pets.get_pet(CHAT, PLAYER["id"])["equipped"].get("weapon"), "w001")
        # The run is untouched by it: no healing, no reset, no free floor.
        run = pets.get_pet(CHAT, PLAYER["id"])["dungeon_run"]
        self.assertEqual((run["hp"], run["floor"], run["cleared"]), (10, 1, []))

        # And leaving for another mode is still refused, with the reason spelled out.
        refused = await self._action_raw(PLAYER, "farm_start", hours=1)
        self.assertEqual(refused.status, 409)
        body = await refused.json()
        self.assertEqual(body["error"], "DUNGEON_ACTIVE")
        self.assertIn("снаряжение", body["message"])

    async def test_selling_a_rare_item_needs_confirm_true(self):
        """valuable_item's rarity gate exists so a stray tap cannot vanish something hard
        to replace. pets.sell_item enforces the real one-time token, but the action
        wrapper is what decides whether the client is even allowed to ask for one --
        without `confirm: true` the sale must be refused outright, not merely queued."""
        self._tame(PLAYER)
        # Every slot is gated by the daily rotation, so the item under test has to be the
        # rare one actually on this player's shelf rather than any rare in the catalogue.
        item = next(
            offer for offer in pets.daily_storefront_items(
                CHAT, "amulet", user_id=PLAYER["id"],
            )
            if valuable_item(offer)
        )
        self.assertTrue(pets.buy_item(CHAT, PLAYER["id"], RICH_XP, item.code)[0])

        refused = await self._action(PLAYER, "sell", code=item.code)
        self.assertFalse(refused["ok"])
        self.assertIn(item.code, pets.get_pet(CHAT, PLAYER["id"])["inventory"])

        confirmed = await self._action(PLAYER, "sell", code=item.code, confirm=True)
        self.assertTrue(confirmed["ok"], confirmed["message"])
        self.assertNotIn(item.code, pets.get_pet(CHAT, PLAYER["id"])["inventory"])

    # ---- item art -------------------------------------------------------------------

    async def test_item_art_needs_no_auth_and_is_deterministic_per_code(self):
        """An <img> tag cannot carry the initData header, so this route has to work
        without one; and a placeholder must render identically on every load, or two
        views of the same item in a crowded bag would visibly disagree with each other."""
        first = await self.client.get(pets_web.ROUTE_PREFIX + "/img/bead.svg")
        self.assertEqual(first.status, 200)
        self.assertEqual(first.content_type, "image/svg+xml")
        first_bytes = await first.read()

        again = await self.client.get(pets_web.ROUTE_PREFIX + "/img/bead.svg")
        self.assertEqual(await again.read(), first_bytes)

        other = await self.client.get(pets_web.ROUTE_PREFIX + "/img/mittens.svg")
        self.assertNotEqual(await other.read(), first_bytes)

    async def test_item_art_refuses_traversal_and_illegal_characters(self):
        """The code reaches the filesystem via _art_file -- the same two-step guard
        vote_web.handle_media uses -- so a path that is not a bare item code must never
        get as far as an os.path join."""
        traversal = await self.client.get(pets_web.ROUTE_PREFIX + "/img/../../secrets.svg")
        self.assertIn(traversal.status, (400, 403, 404))
        illegal = await self.client.get(pets_web.ROUTE_PREFIX + "/img/bad@code.svg")
        self.assertEqual(illegal.status, 404)

    # ---- attacking ------------------------------------------------------------------

    async def test_attacking_a_real_opponent_returns_rounds_a_reward_and_fresh_state(self):
        """The chat interface can only post a verdict; the page's whole reason to exist
        here is to replay the fight blow by blow, so the rounds themselves -- not just a
        winner -- have to come back, alongside the reward and the state the fight left
        behind."""
        self._tame(PLAYER)
        self._tame(OPPONENT, name="Соперник")

        response = await self.client.post(pets_web.ROUTE_PREFIX + "/api/attack", json={
            "init_data": _init_data(PLAYER["id"]), "opponent_id": str(OPPONENT["id"]),
        })
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertTrue(body["rounds"])
        self.assertIn("gold", body["reward"])
        self.assertIsNotNone(body["state"]["pet"])

    async def test_attacking_an_opponent_that_does_not_exist_is_409(self):
        """A card dealt by /api/opponents can go stale by the time it is tapped -- the
        target may have no pet at all by then, and the server must say so rather than
        crash or silently pick someone else."""
        self._tame(PLAYER)
        response = await self.client.post(pets_web.ROUTE_PREFIX + "/api/attack", json={
            "init_data": _init_data(PLAYER["id"]), "opponent_id": "999999",
        })
        self.assertEqual(response.status, 409)
        self.assertEqual((await response.json())["error"], "NO_OPPONENT")

    # ---- opponents ------------------------------------------------------------------

    async def test_opponents_list_excludes_self_and_includes_every_other_tamed_pet(self):
        """The field, not one candidate: everyone else with a creature belongs on the
        list so an even fight is visible at a glance, and the viewer's own creature is
        never a valid target for itself."""
        self._tame(PLAYER)
        self._tame(OPPONENT, name="Соперник")
        self._tame(THIRD, name="Третий")

        response = await self._get("/api/opponents", PLAYER)
        self.assertEqual(response.status, 200)
        body = await response.json()
        ids = {row["user_id"] for row in body["opponents"]}
        self.assertNotIn(str(PLAYER["id"]), ids)
        self.assertEqual(ids, {str(OPPONENT["id"]), str(THIRD["id"])})

    async def test_a_repeatedly_fought_opponent_stays_attackable_and_is_counted(self):
        """Neither a cap nor a penalty any more -- the card just counts the rematches."""
        self._tame(PLAYER)
        self._tame(OPPONENT, name="Соперник")
        self._tame(THIRD, name="Третий")
        result = SimpleNamespace(winner=str(PLAYER["id"]), loser=str(OPPONENT["id"]))
        for _ in range(4):
            data = pets._load(CHAT)
            data["pets"][str(PLAYER["id"])]["fight_bank"] = 99
            pets._save(CHAT, data)
            pets.record_fight(CHAT, PLAYER["id"], OPPONENT["id"], result, pets.today())

        body = await (await self._get("/api/opponents", PLAYER)).json()
        rows = {row["user_id"]: row for row in body["opponents"]}
        fought, fresh = rows[str(OPPONENT["id"])], rows[str(THIRD["id"])]

        self.assertTrue(fought["attackable"])
        self.assertEqual(fought["repeat_fights"]["count"], 4)
        self.assertIn("×4", fought["repeat_fights"]["tag"])
        # Nothing about a cost: fighting the same face again is simply free now.
        self.assertNotIn("percent", fought["repeat_fights"])
        # A face you have not seen today carries nothing, and sorts above the tired one.
        self.assertIsNone(fresh["repeat_fights"])
        self.assertLess(
            [row["user_id"] for row in body["opponents"]].index(str(THIRD["id"])),
            [row["user_id"] for row in body["opponents"]].index(str(OPPONENT["id"])),
        )

    async def test_the_arena_card_shows_swords_and_a_count_and_nothing_else(self):
        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        self.assertIn("function repeatTag(mark)", page)
        self.assertIn("repeatTag(repeats)", page)
        self.assertIn('"<span class=\'dbf fam\'', page)
        self.assertIn('" ×" + Number(mark.count)', page)
        # No trace of the penalty it replaced: no percentage and no explanation of a cost
        # that no longer exists. Scoped to these two functions -- debuffNote still spells
        # out the granted mark, which really does cut stats.
        tag = page.split("function repeatTag(mark)", 1)[1].split("function foeRow(", 1)[0]
        row = page.split("function foeRow(foe, canFight)", 1)[1].split("\n}", 1)[0]
        for dead in (".line", ".percent", ".stacks"):
            self.assertNotIn(dead, tag)
            self.assertNotIn(dead, row)
        self.assertNotIn("familiarTag", page)
        self.assertNotIn("Знакомое лицо", page)

    # ---- isolated turn-based prototype ----------------------------------------------

    async def test_turn_based_setup_exposes_four_slots_and_the_editable_catalogues(self):
        self._tame(PLAYER)
        self._tame(OPPONENT, name="Соперник")

        response = await self._get("/api/test-battle", PLAYER)
        self.assertEqual(response.status, 200, await response.text())
        body = await response.json()

        self.assertTrue(body["test_only"])
        self.assertEqual(len(body["defaults"]["skills"]), 4)
        self.assertEqual(len(body["regular_scrolls"]), 30)
        self.assertEqual(len(body["ultimate_scrolls"]), 10)
        self.assertEqual(len(body["shields"]), len(SCROLLS.SHIELDS))
        self.assertTrue(all(row["auto_weight"] == 1 for row in body["regular_scrolls"]))
        self.assertTrue(all(row["effects"] for row in body["regular_scrolls"]))
        self.assertEqual(
            {row["element"] for row in body["regular_scrolls"] + body["ultimate_scrolls"]},
            set(SCROLLS.ELEMENTS),
        )
        self.assertEqual(
            {row["user_id"] for row in body["opponents"]},
            {"dummy", str(OPPONENT["id"])},
        )

    async def test_manual_turns_and_auto_finish_do_not_touch_the_live_game(self):
        self._tame(PLAYER)
        before = json.dumps(pets._load(CHAT), ensure_ascii=False, sort_keys=True)

        start = await self.client.post(pets_web.ROUTE_PREFIX + "/api/test-battle/start", json={
            "init_data": _init_data(PLAYER["id"]), "mode": "manual", "opponent_id": "dummy",
        })
        self.assertEqual(start.status, 200, await start.text())
        opened = await start.json()
        self.assertTrue(opened["ok"])
        self.assertTrue(opened["session"])
        self.assertTrue(opened["battle"]["test_only"])
        self.assertEqual(len(opened["battle"]["fighters"]["player"]["slots"]), 4)

        defended = await self.client.post(pets_web.ROUTE_PREFIX + "/api/test-battle/action", json={
            "init_data": _init_data(PLAYER["id"]), "session": opened["session"],
            "action": "defend",
        })
        self.assertEqual(defended.status, 200, await defended.text())
        defended_body = await defended.json()
        self.assertEqual(defended_body["battle"]["actor"], "player")
        self.assertTrue(any(row["kind"] == "defend" for row in defended_body["battle"]["log"]))

        finished = await self.client.post(pets_web.ROUTE_PREFIX + "/api/test-battle/action", json={
            "init_data": _init_data(PLAYER["id"]), "session": opened["session"],
            "action": "auto",
        })
        self.assertEqual(finished.status, 200, await finished.text())
        self.assertTrue((await finished.json())["battle"]["finished"])
        self.assertEqual(json.dumps(pets._load(CHAT), ensure_ascii=False, sort_keys=True), before)

    async def test_a_dungeon_boss_fight_never_dms_a_transcript(self):
        """A boss fight used to also post the complete text transcript to the player's
        private chat with the bot. That DM is gone -- the live replay in this response is
        the only place the fight is shown."""
        self._tame(PLAYER)
        data = pets._load(CHAT)
        data["pets"][str(PLAYER["id"])]["dungeon_run"] = {
            "floor": 5, "hp": 500, "max_hp": 500, "cleared": [],
        }
        pets._save(CHAT, data)
        result = SimpleNamespace(
            opening="Герой против босса.", accident=None,
            rounds=(SimpleNamespace(text="Удар."),), closing="Бой окончен.",
            fight_id="F-20260815-ABCDEF123456",
        )
        receipt = {
            "encounter": {"boss": True, "name": "Стальной привратник"},
            "result": result, "hero": object(),
            "enemy": SimpleNamespace(key="dungeon:boss"), "reward": {},
        }
        with patch.object(pets, "dungeon_fight", return_value=(True, "Победа.", receipt)), \
                patch.object(pets_web, "_playback_payload", return_value={"rounds": []}):
            body = await self._action(PLAYER, "dungeon_fight", index=0)

        self.assertTrue(body["ok"])
        self.assertEqual(body["battle"]["rounds"], [])

    async def test_a_dungeon_kill_reports_its_loot_and_never_the_fight_id(self):
        """«Побеждён: X» used to be followed by the fight's internal id, which means
        nothing to a player. It carries what the kill actually paid instead."""
        self._tame(PLAYER)
        data = pets._load(CHAT)
        data["pets"][str(PLAYER["id"])]["dungeon_run"] = {
            "floor": 3, "hp": 500, "max_hp": 500, "cleared": [],
        }
        pets._save(CHAT, data)
        receipt = {
            "encounter": {"boss": False, "name": "Стайный вампир"},
            "reward": {"gold": 140, "xp": 55},
            "rubies": 1,
            "dropped": {"code": "w001", "name": "Ржавый меч", "auto_equipped": True},
            "scroll": {"granted": True, "name": "Искра"},
            "rune": {"granted": 1, "element": "fire"},
        }
        with patch.object(pets, "dungeon_fight",
                          return_value=(True, "Побеждён: Стайный вампир.", receipt)):
            body = await self._action(PLAYER, "dungeon_fight", index=0)

        self.assertTrue(body["ok"])
        message = body["message"]
        self.assertIn("Побеждён: Стайный вампир.", message)
        self.assertNotIn("Fight ID", message)
        for expected in ("🪙 +140", "✨ +55", "💎 +1", "🎁 Ржавый меч", "(надето)",
                         # By its Russian name: "fire" is the loot table's word, not a
                         # word the receipt should be showing a player.
                         "📜 Искра", "🔮 Огонь +1"):
            with self.subTest(expected=expected):
                self.assertIn(expected, message)

    async def test_a_dungeon_kill_that_paid_nothing_adds_no_loot_line(self):
        """A gimmick floor can pay nothing at all -- that must read as the plain result,
        not as an empty «Забрал:» with nothing after it."""
        self._tame(PLAYER)
        data = pets._load(CHAT)
        data["pets"][str(PLAYER["id"])]["dungeon_run"] = {
            "floor": 3, "hp": 500, "max_hp": 500, "cleared": [],
        }
        pets._save(CHAT, data)
        with patch.object(pets, "dungeon_fight",
                          return_value=(True, "Голова падает.", {"encounter": {}, "reward": {}})):
            body = await self._action(PLAYER, "dungeon_fight", index=0)

        self.assertEqual(body["message"], "Голова падает.")
        self.assertNotIn("Забрал", body["message"])

    async def test_automatic_and_multiplayer_placeholder_create_no_live_results(self):
        self._tame(PLAYER)
        before = json.dumps(pets._load(CHAT), ensure_ascii=False, sort_keys=True)

        automatic = await self.client.post(
            pets_web.ROUTE_PREFIX + "/api/test-battle/start", json={
                "init_data": _init_data(PLAYER["id"]), "mode": "auto",
                "opponent_id": "dummy",
            },
        )
        self.assertEqual(automatic.status, 200, await automatic.text())
        auto_body = await automatic.json()
        self.assertIsNone(auto_body["session"])
        self.assertTrue(auto_body["battle"]["finished"])

        multiplayer = await self.client.post(
            pets_web.ROUTE_PREFIX + "/api/test-battle/start", json={
                "init_data": _init_data(PLAYER["id"]), "mode": "multiplayer",
            },
        )
        self.assertEqual(multiplayer.status, 200, await multiplayer.text())
        multi_body = await multiplayer.json()
        self.assertEqual(multi_body["status"], "coming_soon")
        self.assertNotIn("session", multi_body)
        self.assertEqual(json.dumps(pets._load(CHAT), ensure_ascii=False, sort_keys=True), before)

    async def test_a_test_session_is_private_and_rejects_unknown_actions(self):
        self._tame(PLAYER)
        self._tame(OPPONENT, name="Соперник")
        start = await self.client.post(pets_web.ROUTE_PREFIX + "/api/test-battle/start", json={
            "init_data": _init_data(PLAYER["id"]), "mode": "manual", "opponent_id": "dummy",
        })
        token = (await start.json())["session"]

        stolen = await self.client.post(pets_web.ROUTE_PREFIX + "/api/test-battle/action", json={
            "init_data": _init_data(OPPONENT["id"]), "session": token, "action": "attack",
        })
        self.assertEqual(stolen.status, 403)
        self.assertEqual((await stolen.json())["error"], "WRONG_TEST_OWNER")

        invalid = await self.client.post(pets_web.ROUTE_PREFIX + "/api/test-battle/action", json={
            "init_data": _init_data(PLAYER["id"]), "session": token, "action": "delete_everything",
        })
        self.assertEqual(invalid.status, 409)
        self.assertEqual((await invalid.json())["error"], "BAD_TEST_ACTION")

    async def test_turn_based_page_warns_that_it_is_a_test_and_wires_all_moves(self):
        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        self.assertIn('data-testbattle="open"', page)
        self.assertIn('data-testmode="multiplayer"', page)
        self.assertIn("testSkill4", page)
        self.assertIn("Результаты, награды и счётчики не записываются", page)
        # The buttons are built through one `cell()` helper now that they have to fit two
        # rows on a phone, so the rendered `data-testaction="attack"` no longer appears
        # literally in the source. What still has to be true is that every move the engine
        # accepts has a call site, and that the click dispatcher listens for them.
        self.assertIn("[data-testaction]", page)
        for action in ('cell("attack"', 'cell("defend"', 'cell("auto"',
                       'data-testaction="skill_'):
            with self.subTest(action=action):
                self.assertIn(action, page)

    # ---- portrait: the image route ---------------------------------------------------

    async def test_portrait_route_serves_the_photo_and_downloads_it_only_once(self):
        """An <img> tag cannot carry the initData header, so this route has to work
        unauthenticated -- and the Bot API call behind it is not free, so the same
        file_id must cost exactly one download no matter how many times the picture is
        requested; every later request is served off the disk cache."""
        self._tame(PLAYER)  # _tame always uses photo_file_id "file_id"
        self._photos["file_id"] = _jpeg_bytes()

        first = await self.client.get(pets_web.ROUTE_PREFIX + f"/img/pet/{PLAYER['id']}.jpg")
        self.assertEqual(first.status, 200)
        self.assertEqual(first.content_type, "image/jpeg")

        second = await self.client.get(pets_web.ROUTE_PREFIX + f"/img/pet/{PLAYER['id']}.jpg")
        self.assertEqual(second.status, 200)
        self.assertEqual(second.content_type, "image/jpeg")

        self.assertEqual(self.fetch_calls, ["file_id"])

    async def test_no_photo_and_an_unowned_id_both_render_the_svg_placeholder(self):
        """A roster of opponents has to render even when one player's picture is missing
        -- a pet with no photo, and a user id nobody owns, are both a 200 placeholder,
        never a 404 or a broken image."""
        self._tame(PLAYER)
        ok, _ = pets.set_photo(CHAT, PLAYER["id"], None)
        self.assertTrue(ok)

        no_photo = await self.client.get(pets_web.ROUTE_PREFIX + f"/img/pet/{PLAYER['id']}.jpg")
        self.assertEqual(no_photo.status, 200)
        self.assertEqual(no_photo.content_type, "image/svg+xml")

        nobody = await self.client.get(pets_web.ROUTE_PREFIX + f"/img/pet/{NONMEMBER['id']}.jpg")
        self.assertEqual(nobody.status, 200)
        self.assertEqual(nobody.content_type, "image/svg+xml")

        # Neither case has a file_id to fetch in the first place.
        self.assertEqual(self.fetch_calls, [])

    async def test_a_failed_download_falls_back_to_the_placeholder_not_a_500(self):
        """A Bot API call is a network call, and a network call fails in two shapes --
        reporting nothing back, or raising outright. Both must land on the same
        placeholder a missing photo does, not on a crash that takes the whole roster
        down with it."""
        self._tame(PLAYER)
        pets.set_photo(CHAT, PLAYER["id"], "fails_quietly")
        self._photos["fails_quietly"] = None

        quiet = await self.client.get(pets_web.ROUTE_PREFIX + f"/img/pet/{PLAYER['id']}.jpg")
        self.assertEqual(quiet.status, 200)
        self.assertEqual(quiet.content_type, "image/svg+xml")

        self._tame(OPPONENT, name="Соперник")
        pets.set_photo(CHAT, OPPONENT["id"], "fails_loudly")
        self._photos["fails_loudly"] = RuntimeError("Bot API is down")

        loud = await self.client.get(pets_web.ROUTE_PREFIX + f"/img/pet/{OPPONENT['id']}.jpg")
        self.assertEqual(loud.status, 200)
        self.assertEqual(loud.content_type, "image/svg+xml")

        # Both were actually attempted, not skipped -- the placeholder is a fallback,
        # not a shortcut around ever calling fetch_photo.
        self.assertEqual(self.fetch_calls, ["fails_quietly", "fails_loudly"])

    async def test_the_cache_is_keyed_on_file_id_not_on_the_player(self):
        """The cache path is a hash of the file_id, never of the owner -- so a changed
        photo must not keep serving yesterday's picture from disk, and must cost a fresh
        download that returns the new pixels."""
        self._tame(PLAYER)
        self._photos["file_id"] = _jpeg_bytes(colour=(200, 30, 30))
        first = await self.client.get(pets_web.ROUTE_PREFIX + f"/img/pet/{PLAYER['id']}.jpg")
        first_bytes = await first.read()

        pets.set_photo(CHAT, PLAYER["id"], "file_id_v2")
        self._photos["file_id_v2"] = _jpeg_bytes(colour=(30, 200, 30))
        second = await self.client.get(pets_web.ROUTE_PREFIX + f"/img/pet/{PLAYER['id']}.jpg")
        second_bytes = await second.read()

        self.assertEqual(self.fetch_calls, ["file_id", "file_id_v2"])
        self.assertNotEqual(first_bytes, second_bytes)

    # ---- portrait: uploading -----------------------------------------------------------

    async def test_uploading_a_photo_re_encodes_it_smaller_and_bounded(self):
        """The upload path exists so a phone's multi-megabyte original never leaves the
        server unbounded -- everything is re-encoded and capped at 1280px on the long
        edge before it is handed to Telegram, and the state in the response has to show
        the new photo immediately."""
        self._tame(PLAYER)
        self.next_saved_file_id = "fresh_file_id"
        original = _large_jpeg_bytes()

        response = await self._upload_portrait(PLAYER, original)
        self.assertEqual(response.status, 200, await response.text())
        body = await response.json()
        self.assertTrue(body["ok"], body["message"])
        self.assertTrue(body["state"]["pet"]["has_photo"])

        self.assertEqual(len(self.save_calls), 1)
        saved_user_id, saved_bytes = self.save_calls[0]
        self.assertEqual(saved_user_id, PLAYER["id"])
        self.assertLess(len(saved_bytes), len(original))
        self.assertLessEqual(max(Image.open(BytesIO(saved_bytes)).size), 1280)

        # The id save_photo returned -- not the original "file_id" from taming -- is what
        # actually got stored.
        self.assertEqual(pets.get_pet(CHAT, PLAYER["id"])["photo_file_id"], "fresh_file_id")

    async def test_uploading_non_image_bytes_is_refused_before_telegram_sees_them(self):
        """_normalise_photo runs before save_photo is ever called -- nothing unvalidated
        should be forwarded to Telegram through the bot, so bytes that are not a picture
        must be refused right here, with save_photo never even reached."""
        self._tame(PLAYER)
        response = await self._upload_portrait(PLAYER, b"not actually a jpeg")
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"], "NOT_AN_IMAGE")
        self.assertEqual(self.save_calls, [])

    async def test_a_real_phone_photo_is_bigger_than_the_servers_default_body_limit(self):
        """aiohttp caps a request body at 1 MB for the whole application, and an ordinary
        phone picture is two or three times that. Read through request.read() the upload
        would die on aiohttp's own generic 413 before this route ran, telling the player
        nothing -- so the route counts the bytes itself against its own ceiling."""
        self._tame(PLAYER)
        photo = _large_jpeg_bytes(edge=3000, quality=97)
        self.assertGreater(len(photo), 1024 * 1024, "the point of this test is a >1MB body")

        response = await self._upload_portrait(PLAYER, photo)

        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(len(self.save_calls), 1)

    async def test_an_upload_past_the_routes_own_ceiling_is_refused_as_such(self):
        """...and the ceiling is still a ceiling: past it the answer is this module's own
        TOO_BIG, not a generic transport error, and nothing is buffered whole to find out."""
        self._tame(PLAYER)
        response = await self._upload_portrait(PLAYER, b"x" * (pets_web.PORTRAIT_MAX_BYTES + 1))
        self.assertEqual(response.status, 413)
        self.assertEqual((await response.json())["error"], "TOO_BIG")
        self.assertEqual(self.save_calls, [])

    async def test_upload_creates_a_first_pet_and_still_enforces_membership_and_auth(self):
        """The same web upload used for a new portrait creates the first pet when a name
        is supplied; it remains a members-only authenticated mutation."""
        missing_name = await self._upload_portrait(PLAYER, _jpeg_bytes())
        self.assertEqual(missing_name.status, 400)
        self.assertEqual((await missing_name.json())["error"], "PET_NAME_REQUIRED")

        created = await self._upload_portrait(PLAYER, _jpeg_bytes(), pet_name="Брауни")
        self.assertEqual(created.status, 200, await created.text())
        created_body = await created.json()
        self.assertTrue(created_body["ok"], created_body["message"])
        self.assertEqual(created_body["state"]["pet"]["name"], "Брауни")
        self.assertEqual(pets.get_pet(CHAT, PLAYER["id"])["photo_file_id"], "uploaded_file_id")

        non_member = await self._upload_portrait(NONMEMBER, _jpeg_bytes())
        self.assertEqual(non_member.status, 403)

        unsigned = await self.client.post(
            pets_web.ROUTE_PREFIX + "/api/portrait", data=_jpeg_bytes(),
            headers={"Content-Type": "image/jpeg"},
        )
        self.assertEqual(unsigned.status, 401)

    async def test_page_offers_pet_creation_at_the_top_and_in_onboarding(self):
        page = await (await self.client.get(pets_web.ROUTE_PREFIX + "/")).text()
        self.assertIn('id="hudCreate"', page)
        self.assertIn('$("hudCreate").hidden = Boolean(pet);', page)
        self.assertIn('data-do="tame">Создать существо', page)
        self.assertIn("function openPetCreation()", page)
        self.assertIn('openPetCreation();', page)
        self.assertIn('"?pet_name=" + encodeURIComponent(petName)', page)

    # ---- portrait: cropping -------------------------------------------------------------

    async def test_portrait_crop_action_stores_reads_back_and_rejects_nonsense(self):
        """The crop is a rectangle the player chose, not a free-form blob -- a valid one
        round-trips through state exactly, `crop: null` is the documented way back to
        "fit the whole photo", and anything that is not a real rectangle (a missing side,
        a size that cannot enclose anything, a square bigger than any photo could be) has
        to be refused rather than stored."""
        self._tame(PLAYER)
        saved = await self._action(PLAYER, "portrait_crop", crop={"x": 10, "y": 20, "size": 50})
        self.assertTrue(saved["ok"], saved["message"])
        self.assertEqual(saved["state"]["pet"]["crop"], {"x": 10.0, "y": 20.0, "size": 50.0})

        cleared = await self._action(PLAYER, "portrait_crop", crop=None)
        self.assertTrue(cleared["ok"], cleared["message"])
        self.assertIsNone(cleared["state"]["pet"]["crop"])

        for bad in (
            {"x": 1, "y": 2},                     # no "size" at all
            {"x": 1, "y": 2, "size": 0},           # cannot enclose anything
            {"x": 1, "y": 2, "size": -5},          # negative
            {"x": 1, "y": 2, "size": 1_000_000},   # absurdly large
        ):
            refused = await self._action(PLAYER, "portrait_crop", crop=bad)
            self.assertFalse(refused["ok"], bad)
            self.assertIsNone(refused["state"]["pet"]["crop"])

    async def test_changing_the_photo_clears_the_stored_crop(self):
        """A crop is a rectangle chosen for ONE composition -- pets.set_photo pops it the
        moment the picture underneath changes, so a frame that used to centre the old
        figurine can never be silently reapplied to a different one."""
        self._tame(PLAYER)
        cropped = await self._action(PLAYER, "portrait_crop", crop={"x": 1, "y": 2, "size": 10})
        self.assertIsNotNone(cropped["state"]["pet"]["crop"])

        response = await self._upload_portrait(PLAYER, _jpeg_bytes())
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertTrue(body["ok"], body["message"])
        self.assertIsNone(body["state"]["pet"]["crop"])

    # ---- portrait: everywhere a pet is listed --------------------------------------------

    async def test_opponents_and_leaderboard_rows_each_carry_a_portrait_url(self):
        """Every list of pets is a list of faces, not just names -- opponents and the
        leaderboard are two separate payloads built by two separate handlers, and both
        have to carry the same portrait URL shape the page's <img> tags rely on."""
        self._tame(PLAYER)
        self._tame(OPPONENT, name="Соперник")

        opponents = await (await self._get("/api/opponents", PLAYER)).json()
        self.assertTrue(opponents["opponents"])
        for row in opponents["opponents"]:
            self.assertEqual(
                row["portrait"], f"{pets_web.ROUTE_PREFIX}/img/pet/{row['user_id']}.jpg"
            )

        leaderboard = await (await self._get("/api/leaderboard", PLAYER)).json()
        self.assertTrue(leaderboard["rows"])
        for row in leaderboard["rows"]:
            self.assertEqual(
                row["portrait"], f"{pets_web.ROUTE_PREFIX}/img/pet/{row['user_id']}.jpg"
            )

    async def test_a_portrait_frame_is_blockified_so_it_can_have_a_size(self):
        """The reason opponents' photos were invisible while the player's own was fine.

        `shot()` emits a <span>, and width/height do not apply to a non-replaced INLINE
        box -- so an un-blockified .shot is a zero-sized containing block and the
        absolutely positioned <img> inside resolves width:100% to nothing. The two
        callers that worked (.hud .face, .doll .portrait) are flex containers, which
        blockify their items for free; the opponent roster and the ranking are plain
        blocks, and they are exactly the two screens that showed no faces. Asserted
        rather than left to a CSS reading, because nothing else in this file would fail
        if the declaration were dropped again.
        """
        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        rule = next(line for line in page.splitlines() if line.strip().startswith(".shot {"))
        self.assertIn("display: block", rule)
        # The frames that hold one. If any of these stops being sized, the photo inside it
        # has nothing to fill.
        for frame in (".hud .face", ".foe .av", ".doll .portrait"):
            self.assertIn(frame, page)

    # ---- the log ----------------------------------------------------------------------

    def _logged(self, needle):
        return [line for line in self.logs if needle in line]

    async def test_the_three_reasons_for_a_placeholder_are_told_apart_in_the_log(self):
        """A pet nobody photographed, a lookup that found no pet at all, and a download
        that failed all render the SAME grey tile. On screen they are indistinguishable, so
        the log is the only thing that can tell a working game from a broken one -- which
        is the whole reason the reason is written down."""
        self._tame(PLAYER)                       # has a photo (file_id "file_id")
        self._photos["file_id"] = None           # ...that Telegram declines to hand over
        pets.buy_cage(CHAT, OPPONENT["id"], RICH_XP)
        pets.tame(CHAT, OPPONENT["id"], RICH_XP, "Голый", None, "Opponent")

        await self.client.get(f"{pets_web.ROUTE_PREFIX}/img/pet/{OPPONENT['id']}.jpg")
        await self.client.get(f"{pets_web.ROUTE_PREFIX}/img/pet/123456.jpg")
        await self.client.get(f"{pets_web.ROUTE_PREFIX}/img/pet/{PLAYER['id']}.jpg")

        self.assertTrue(self._logged("no photo_file_id"))
        self.assertTrue(self._logged("no pet under entry"))
        self.assertTrue(self._logged("download returned nothing"))

    async def test_a_cached_portrait_is_not_logged_again(self):
        """One line per image VIEW would bury everything else in the log -- a single arena
        screen is a dozen portraits. Only the fetch is worth a line; a cache hit is not."""
        self._tame(PLAYER)
        self._photos["file_id"] = _jpeg_bytes()

        for _ in range(3):
            await self.client.get(f"{pets_web.ROUTE_PREFIX}/img/pet/{PLAYER['id']}.jpg")

        self.assertEqual(len(self._logged("fetched")), 1, self.logs)

    async def test_a_download_that_raises_is_logged_and_still_answers(self):
        """A broken picture must not become a broken page: the roster still has to render,
        so the exception is recorded and the placeholder is served."""
        self._tame(PLAYER)
        self._photos["file_id"] = RuntimeError("Telegram said no")

        response = await self.client.get(f"{pets_web.ROUTE_PREFIX}/img/pet/{PLAYER['id']}.jpg")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "image/svg+xml")
        self.assertTrue(self._logged("fetch raised"))
        self.assertTrue(any("Telegram said no" in line for line in self.logs))

    async def test_every_action_is_logged_with_who_did_it_and_whether_it_took(self):
        """When a player reports that something went wrong, this is the record of what they
        actually did -- the alternative is asking them to remember. A REFUSED action is
        logged too: "nothing happened" is exactly the case worth being able to look up."""
        self._tame(PLAYER)

        await self._action(PLAYER, "upgrade_stat", stat="strength", times=1)
        await self._action(PLAYER, "equip", code="w001")   # not owned -- refused

        self.assertTrue(self._logged(f"{PLAYER['id']} upgrade_stat strength -> ok"))
        self.assertTrue(self._logged(f"{PLAYER['id']} equip w001 -> refused"))

    async def test_a_fight_is_logged_with_its_outcome(self):
        """A fight moves coins, XP and sometimes an item. It is the most consequential
        thing the page does, so it leaves a line saying what it decided."""
        self._tame(PLAYER)
        self._tame(OPPONENT, name="Соперник")

        await self.client.post(pets_web.ROUTE_PREFIX + "/api/attack", json={
            "init_data": _init_data(PLAYER["id"]), "opponent_id": str(OPPONENT["id"]),
        })

        line = self._logged(f"fight {PLAYER['id']} vs {OPPONENT['id']}")
        self.assertEqual(len(line), 1, self.logs)
        self.assertRegex(line[0], r"(win|loss|draw)")
        self.assertIn("rounds", line[0])

    async def test_the_upload_logs_both_byte_counts(self):
        """The re-encode is the part most likely to be wrong later -- a change that stopped
        shrinking photos would be invisible without the two numbers side by side."""
        self._tame(PLAYER)

        await self._upload_portrait(PLAYER, _large_jpeg_bytes())

        self.assertTrue(self._logged("portrait upload from"))
        self.assertRegex(self._logged("portrait upload from")[0], r"\d+ -> \d+ bytes")

    # ---- the slot picker (page wiring) --------------------------------------------------

    async def test_tapping_a_slot_offers_what_else_fits_it(self):
        """A hand on an equipment slot is asking "what can go here?", not "what is here?".
        The paperdoll therefore opens the slot, not the single item in it -- and every
        alternative is one tap from being worn, with its stat delta against what is."""
        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        self.assertIn("function openSlot(", page)
        self.assertIn("if (d.slot !== undefined && d.slot && !d.act) { openSlot(d.slot); return; }", page)
        self.assertIn("data-equipnow=", page)
        self.assertIn('if (d.equipnow) { closeSheet(); await act("equip", { code: d.equipnow }); return; }', page)

    async def test_a_slot_with_no_alternatives_points_at_the_shop(self):
        """An empty answer is still an answer, but a dead end is not: the one thing to do
        about a slot you own nothing for is to go and buy something for it."""
        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        self.assertIn("data-shoptab=", page)
        self.assertIn('if (d.shoptab) { closeSheet(); TAB = "shop";', page)

    async def test_a_replaced_photo_is_not_hidden_behind_a_weeklong_cache(self):
        """/img/pet/<id>.jpg is keyed on the OWNER, not on the file id -- so the same URL
        really does point at different pixels once somebody changes their photo. It used
        to be served as immutable for a week on the strength of the disk cache's name,
        which is the one way a working upload still looks like nothing happened."""
        self._tame(PLAYER)
        self._photos["file_id"] = _jpeg_bytes()

        first = await self.client.get(f"{pets_web.ROUTE_PREFIX}/img/pet/{PLAYER['id']}.jpg")
        self.assertEqual(first.status, 200)
        self.assertIn("max-age=300", first.headers["Cache-Control"])
        # Cheap when nothing changed: a conditional request costs a 304, not the image.
        again = await self.client.get(
            f"{pets_web.ROUTE_PREFIX}/img/pet/{PLAYER['id']}.jpg",
            headers={"If-Modified-Since": first.headers["Last-Modified"]},
        )
        self.assertEqual(again.status, 304)

        # A new photo is a new file_id, so it lands in a different cache file and is
        # served immediately rather than after the old URL's lifetime expires.
        self._photos["file_id_2"] = _jpeg_bytes(size=(80, 80), colour=(20, 200, 40))
        self.assertTrue(pets.set_photo(CHAT, PLAYER["id"], "file_id_2")[0])
        fresh = await self.client.get(f"{pets_web.ROUTE_PREFIX}/img/pet/{PLAYER['id']}.jpg")
        self.assertEqual(fresh.status, 200)
        self.assertNotEqual(await fresh.read(), await first.read())

    # ---- quests -------------------------------------------------------------------------

    async def test_the_quest_board_carries_everything_needed_to_go_and_paint(self):
        board = await (await self._get("/api/quests", PLAYER)).json()
        self.assertEqual(len(board["quests"]), 3)
        self.assertEqual(len({card["code"] for card in board["quests"]}), 3)
        self.assertEqual(len(board["real"]["quests"]), 1)
        self.assertFalse(board["auto_refresh"])
        self.assertIsNone(board["seconds_until_refresh"])
        self.assertTrue(board["real"]["auto_refresh"])
        self.assertGreater(board["real"]["seconds_until_refresh"], 0)
        self.assertLessEqual(board["real"]["seconds_until_refresh"], 24 * 60 * 60)
        self.assertFalse(board["rune"]["auto_refresh"])
        self.assertIsNone(board["rune"]["seconds_until_refresh"])
        self.assertEqual(len(board["rune"]["quests"]), 5)
        quest = board["quest"]
        for field in ("code", "hashtag", "title", "subject", "technique", "hint",
                      "tool", "difficulty", "reward"):
            self.assertIn(field, quest)
        # Underscores, not hyphens: Telegram ends a hashtag at the first character
        # outside letters/digits/underscore, so "#quest-nmm" was never one tag.
        self.assertEqual(quest["hashtag"], "#quest_" + quest["code"])
        self.assertNotIn("-", quest["code"])
        self.assertTrue(board["reroll_available"])
        # A quest board is not an admin surface, and the menu flag must never be the
        # thing that decides -- but it still has to be honest about which menu to draw.
        self.assertFalse(board["is_admin"])
        self.assertTrue((await (await self._get("/api/quests", MODERATOR)).json())["is_admin"])

    async def test_the_web_reroll_replaces_a_whole_group_and_starts_twelve_hour_cooldown(self):
        before = await (await self._get("/api/quests", PLAYER)).json()
        old_codes = {card["code"] for card in before["quests"]}
        response = await self.client.post(
            pets_web.ROUTE_PREFIX + "/api/quests/reroll",
            json={"init_data": _init_data(PLAYER["id"]), "kind": "paint"},
            headers=self._auth(PLAYER),
        )
        payload = await response.json()
        self.assertTrue(payload["ok"], payload)
        self.assertIn("Следующий реролл в", payload["message"])
        board = payload["board"]
        self.assertTrue(old_codes.isdisjoint({card["code"] for card in board["quests"]}))
        self.assertFalse(board["reroll_available"])
        self.assertIsNotNone(board["reroll_at_label"])
        self.assertGreater(board["seconds_until_reroll"], 0)
        self.assertLessEqual(board["seconds_until_reroll"], 12 * 60 * 60)

        refused = await (await self.client.post(
            pets_web.ROUTE_PREFIX + "/api/quests/reroll",
            json={"init_data": _init_data(PLAYER["id"]), "kind": "paint"},
            headers=self._auth(PLAYER),
        )).json()
        self.assertFalse(refused["ok"])
        self.assertIn("Следующий реролл в", refused["message"])

    async def test_only_a_moderator_can_see_or_decide_a_quest_submission(self):
        """The review queue holds other people's work and the accept button spends real
        coins. Every one of these routes asks the gate for itself -- a client that simply
        navigates to the tab, or forges the menu flag, still gets nothing."""
        for method, path in (("get", "/api/quests/review"), ("post", "/api/quests/review"),
                             ("post", "/api/quests/config")):
            unsigned = await getattr(self.client, method)(pets_web.ROUTE_PREFIX + path, json={})
            self.assertEqual(unsigned.status, 401, f"{method} {path} let an unsigned caller in")
            refused = await getattr(self.client, method)(
                pets_web.ROUTE_PREFIX + path,
                json={"init_data": _init_data(PLAYER["id"])}, headers=self._auth(PLAYER),
            )
            self.assertEqual(refused.status, 403, f"{method} {path} let a player moderate")
            self.assertEqual((await refused.json())["error"], "NOT_AN_ADMIN")

    async def test_a_moderator_accepts_a_quest_and_the_reward_lands_exactly_once(self):
        """Accept is a web button on a slow connection, so it will be double-tapped. The
        second press must find the row already decided and move no money at all."""
        self._tame(PLAYER)
        board = await (await self._get("/api/quests", PLAYER)).json()
        code = board["quest"]["code"]
        self.assertTrue(quests.submit(
            CHAT, PLAYER["id"], code, chat_id=-1001234567890, message_id=777,
            photo_file_id="quest-photo", author_name="Player")[0])

        queue = await (await self._get("/api/quests/review", MODERATOR)).json()
        self.assertEqual(len(queue["rows"]), 1)
        row = queue["rows"][0]
        self.assertEqual(row["code"], code)
        # The photo is not re-hosted: the moderator is sent to the post itself.
        self.assertEqual(row["link"], "https://t.me/c/1234567890/777")

        before = economy.balance(CHAT, PLAYER["id"], RICH_XP)
        answer = await (await self.client.post(
            pets_web.ROUTE_PREFIX + "/api/quests/review",
            json={"init_data": _init_data(MODERATOR["id"]), "id": row["id"], "accept": True},
        )).json()
        self.assertTrue(answer["ok"], answer)
        paid = answer["receipt"]
        self.assertEqual(economy.balance(CHAT, PLAYER["id"], RICH_XP), before + paid["gold"])
        self.assertEqual(pets.farm_tickets(CHAT, PLAYER["id"]), paid["tickets"])
        self.assertEqual(len(self.quest_completions), 1)
        self.assertEqual(self.quest_completions[0]["user_id"], str(PLAYER["id"]))
        self.assertEqual(self.quest_completions[0]["message_id"], 777)
        self.assertEqual(self.quest_completions[0]["photo_file_id"], "quest-photo")
        self.assertEqual(self.quest_completions[0]["hashtag"], "#quest_" + code)
        self.assertEqual(self.quest_completions[0]["paid"], paid)

        again = await (await self.client.post(
            pets_web.ROUTE_PREFIX + "/api/quests/review",
            json={"init_data": _init_data(MODERATOR["id"]), "id": row["id"], "accept": True},
        )).json()
        self.assertFalse(again["ok"])
        self.assertEqual(economy.balance(CHAT, PLAYER["id"], RICH_XP), before + paid["gold"])
        self.assertEqual(pets.farm_tickets(CHAT, PLAYER["id"]), paid["tickets"])
        self.assertEqual(len(self.quest_completions), 1)
        self.assertEqual((await (await self._get("/api/quests/review", MODERATOR)).json())["rows"], [])

    async def test_rejection_needs_a_reason_sends_it_to_the_player_and_ideas_reach_review(self):
        self._tame(PLAYER)
        code = (await (await self._get("/api/quests", PLAYER)).json())["quest"]["code"]
        self.assertTrue(quests.submit(CHAT, PLAYER["id"], code, author_name="Player")[0])
        row = (await (await self._get("/api/quests/review", MODERATOR)).json())["rows"][0]

        missing = await (await self.client.post(pets_web.ROUTE_PREFIX + "/api/quests/review", json={
            "init_data": _init_data(MODERATOR["id"]), "id": row["id"], "accept": False,
        })).json()
        self.assertFalse(missing["ok"])
        self.assertEqual(len((await (await self._get("/api/quests/review", MODERATOR)).json())["rows"]), 1)

        rejected = await (await self.client.post(pets_web.ROUTE_PREFIX + "/api/quests/review", json={
            "init_data": _init_data(MODERATOR["id"]), "id": row["id"], "accept": False,
            "note": "Нужна более чёткая фотография работы.",
        })).json()
        self.assertTrue(rejected["ok"], rejected)
        self.assertEqual(self.quest_feedback, [
            (str(PLAYER["id"]), row["title"], "Нужна более чёткая фотография работы.")
        ])

        idea = await (await self.client.post(pets_web.ROUTE_PREFIX + "/api/quests/ideas", json={
            "init_data": _init_data(PLAYER["id"]), "text": "Добавить зимний квест.",
        })).json()
        self.assertTrue(idea["ok"], idea)
        queue = await (await self._get("/api/quests/review", MODERATOR)).json()
        self.assertEqual(queue["ideas"][0]["text"], "Добавить зимний квест.")

    async def test_a_moderator_can_retune_the_reward_table_within_limits(self):
        answer = await (await self.client.post(
            pets_web.ROUTE_PREFIX + "/api/quests/config",
            json={"init_data": _init_data(MODERATOR["id"]),
                  "difficulty": 3, "field": "gold", "value": 999_999},
        )).json()
        self.assertTrue(answer["ok"])
        # Clamped, not accepted: a mistyped figure in a text box is a chat's economy gone,
        # and coins already spent cannot be taken back.
        capped = quests.REWARD_LIMITS["gold"][1]
        self.assertEqual(quests.rewards_for(CHAT, 3)["gold"], capped)
        row = next(r for r in answer["rewards"] if r["difficulty"] == 3)
        self.assertEqual(row["gold"], capped)

        refused = await (await self.client.post(
            pets_web.ROUTE_PREFIX + "/api/quests/config",
            json={"init_data": _init_data(MODERATOR["id"]),
                  "difficulty": 3, "field": "everything", "value": 1},
        )).json()
        self.assertFalse(refused["ok"])

    async def test_a_moderator_can_toggle_rotation_and_edit_the_full_quest_brief(self):
        before = await (await self._get("/api/quests/review", MODERATOR)).json()
        quest = before["catalog"][0]
        switched = await (await self.client.post(pets_web.ROUTE_PREFIX + "/api/quests/config", json={
            "init_data": _init_data(MODERATOR["id"]), "code": quest["code"], "enabled": False,
        })).json()
        self.assertTrue(switched["ok"], switched)
        after_switch = await (await self._get("/api/quests/review", MODERATOR)).json()
        self.assertFalse(next(row for row in after_switch["catalog"] if row["code"] == quest["code"])["enabled"])

        edited = await (await self.client.post(pets_web.ROUTE_PREFIX + "/api/quests/config", json={
            "init_data": _init_data(MODERATOR["id"]), "code": quest["code"],
            "text": {"title": "Новый бриф", "subject": "Новая деталь", "technique": "Тонкие слои",
                     "hint": "Не используй старую работу", "proof": "Фото новой детали"},
        })).json()
        self.assertTrue(edited["ok"], edited)
        saved = next(row for row in (await (await self._get("/api/quests/review", MODERATOR)).json())["catalog"]
                     if row["code"] == quest["code"])
        self.assertEqual(saved["title"], "Новый бриф")
        self.assertEqual(saved["proof"], "Фото новой детали")

    async def test_the_page_only_draws_the_review_tab_for_a_moderator(self):
        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        self.assertIn("function questBoard(", page)
        self.assertIn("function reviewQueue(", page)
        self.assertIn('S.quest_attention ? "❗ "', page)
        self.assertIn('"🎯 Квесты"', page)
        self.assertIn("data-questopen", page)
        self.assertIn("class=\"quest-timer\"", page)
        self.assertIn('id="questReviewTab"', page)
        self.assertIn('id="scr-quests"', page)
        self.assertIn('data-tab="quests"', page)
        self.assertIn('data-questgroup', page)
        self.assertIn('[data-questgroup]', page)
        self.assertIn('Без дедлайна:', page)
        self.assertIn('data-questidea', page)
        self.assertIn('data-reviewideas', page)
        self.assertIn('data-questedit', page)
        self.assertIn('Причина отклонения обязательна.', page)
        # A verdict spends coins, so it is confirmed rather than fired on one tap.
        self.assertIn("confirmThen(\"Принять работу и начислить награду?\"", page)

    # ---- the farm ticket ----------------------------------------------------------------

    async def test_a_farm_ticket_ends_the_shift_immediately_without_touching_the_payout(self):
        """The ticket buys the waiting, not the work: the eight-hour reward has to survive
        being collected eight hours early, or the button is just a worse «Забрать сейчас»."""
        self._tame(PLAYER)
        self.assertTrue(pets.upgrade_farm(CHAT, PLAYER["id"], RICH_XP)[0])
        self.assertTrue(pets.start_farm(CHAT, PLAYER["id"], 8)[0])
        pets.grant_farm_ticket(CHAT, PLAYER["id"], "figurine:1")

        before = (await (await self._get("/api/state", PLAYER)).json())["farm"]
        self.assertEqual(before["tickets"], 1)
        self.assertTrue(before["can_ticket"])
        expected = before["reward"]

        answer = await self._action(PLAYER, "farm_ticket")
        self.assertTrue(answer["ok"], answer)
        # The settlement happens inside the ticket action itself, so the payout is in the
        # toast message -- by the time the response's own state is assembled a moment
        # later, the run is already gone and there is nothing left for farm_receipts to
        # report a second time.
        self.assertIn(f"💰{expected['gold']}", answer["message"])
        self.assertIn(f"✨{expected['xp']}", answer["message"])
        farm = answer["state"]["farm"]
        self.assertFalse(farm["running"])
        self.assertEqual(farm["tickets"], 0)
        self.assertFalse(farm["can_ticket"])

        # Spending the only ticket means the button is gone, not broken.
        again = await self._action(PLAYER, "farm_ticket")
        self.assertFalse(again["ok"])

    async def test_the_farm_screen_only_offers_a_ticket_that_would_do_something(self):
        self._tame(PLAYER)
        self.assertTrue(pets.upgrade_farm(CHAT, PLAYER["id"], RICH_XP)[0])

        # A ticket in hand but no shift running: nothing to shorten.
        pets.grant_farm_ticket(CHAT, PLAYER["id"], "figurine:1")
        idle = (await (await self._get("/api/state", PLAYER)).json())["farm"]
        self.assertEqual(idle["tickets"], 1)
        self.assertFalse(idle["can_ticket"])
        self.assertFalse((await self._action(PLAYER, "farm_ticket"))["ok"])

        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        self.assertIn('data-do="farmticket"', page)
        self.assertIn('else if (d.do === "farmticket") { await act("farm_ticket"); }', page)
        self.assertIn("farm.can_ticket", page)

    # ---- replaying a recorded fight -----------------------------------------------------

    # ------------------------------------------------------------- the boss workshop
    async def test_the_boss_workshop_is_admin_only_on_both_of_its_routes(self):
        """A menu flag is a hint. Every route re-asks, so a hand-typed one opens nothing."""
        self._tame(PLAYER)
        for user in (PLAYER, MODERATOR):
            with self.subTest(user=user["username"]):
                self.assertEqual((await self._get("/api/boss-test", user)).status, 403)
                response = await self._post("/api/boss-test/run", user, {
                    "floor": 5, "user_id": str(PLAYER["id"]), "fights": 1,
                })
                self.assertEqual(response.status, 403)
        self.assertEqual((await self._get("/api/boss-test", THIRD)).status, 200)

    async def test_the_workshop_lists_every_boss_and_every_pet_strongest_first(self):
        self._tame(PLAYER)
        self._tame(OPPONENT, name="Соперник")
        # Make the ordering a real question rather than a tie.
        stored = pets._load(CHAT)
        stored["pets"][str(OPPONENT["id"])]["stats"]["strength"] = 90
        pets._save(CHAT, stored)

        data = await (await self._get("/api/boss-test", THIRD)).json()
        self.assertTrue(data["test_only"])
        floors = [row["floor"] for row in data["bosses"]]
        self.assertEqual(floors, list(range(dungeon.BOSS_EVERY,
                                            dungeon.LAST_FLOOR + 1, dungeon.BOSS_EVERY)))
        # The gimmick and its one-line rule travel with the boss: tuning one without
        # being told it is the ghost that eats steel is tuning it blind.
        ghost = next(row for row in data["bosses"] if row["gimmick"] == "spells_only")
        self.assertTrue(ghost["weakness"])
        self.assertTrue(ghost["stat_line"])

        powers = [row["power"] for row in data["fighters"]]
        self.assertEqual(powers, sorted(powers, reverse=True))
        self.assertEqual(data["fighters"][0]["user_id"], str(OPPONENT["id"]))
        # Whoever is fighting is described by what they actually carry, because that is
        # the half of "as this character" a stat line does not cover.
        self.assertIn("weapon", data["fighters"][0])
        self.assertEqual(len(data["fighters"][0]["scrolls"]), 4)

    async def test_a_workshop_fight_is_the_real_boss_and_changes_nothing(self):
        self._tame(PLAYER)
        before = pets._load(CHAT)["pets"][str(PLAYER["id"])]
        snapshot = json.dumps(before, sort_keys=True, ensure_ascii=False)

        data = await (await self._post("/api/boss-test/run", THIRD, {
            "floor": 25, "user_id": str(PLAYER["id"]), "fights": 1,
        })).json()
        self.assertTrue(data["test_only"])
        self.assertEqual(data["fights"], 1)
        self.assertEqual(data["boss"]["gimmick"], "spells_only")
        # The transcript is the same payload the dungeon animates, so what the workshop
        # shows is what a player would see rather than a second rendering of it.
        self.assertTrue(data["battle"]["dungeon"])
        self.assertEqual(data["battle"]["you"], str(PLAYER["id"]))
        self.assertTrue(data["battle"]["rounds"])

        after = pets._load(CHAT)["pets"][str(PLAYER["id"])]
        self.assertEqual(json.dumps(after, sort_keys=True, ensure_ascii=False), snapshot)
        self.assertIsNone(after.get("dungeon_run"))
        self.assertEqual((await (await self._get("/api/history", PLAYER)).json())["rows"], [])

    async def test_a_batch_reports_a_win_rate_and_is_capped(self):
        """One fight is a story; a boss is tuned against a number."""
        self._tame(PLAYER)
        data = await (await self._post("/api/boss-test/run", THIRD, {
            "floor": 5, "user_id": str(PLAYER["id"]), "fights": 10_000,
        })).json()
        self.assertEqual(data["fights"], pets_web.BOSS_TEST_MAX_FIGHTS)
        self.assertLessEqual(data["wins"], data["fights"])
        self.assertGreaterEqual(data["win_rate"], 0)
        self.assertLessEqual(data["win_rate"], 100)
        self.assertGreater(data["fighter"]["max_hp"], 0)

    async def test_the_workshop_refuses_a_floor_without_a_boss_and_a_pet_without_an_owner(self):
        self._tame(PLAYER)
        for payload, code in (
            ({"floor": 4, "user_id": str(PLAYER["id"]), "fights": 1}, "BAD_BOSS_TEST"),
            ({"floor": 5, "user_id": "999999", "fights": 1}, "BAD_BOSS_TEST"),
            ({"floor": 5, "fights": 1}, "BAD_BOSS_TEST"),
        ):
            with self.subTest(payload=payload):
                response = await self._post("/api/boss-test/run", THIRD, payload)
                self.assertEqual((await response.json())["error"], code)

    async def test_opening_the_achievement_list_answers_instead_of_breaking(self):
        """The list is fetched by its own action, so it has its own way to fail.

        The badge on the hero screen is a cheap summary; everything the screen actually
        lists is worked out here, which makes this the one request in the feature that
        touches the chat history at all.
        """
        self._tame(PLAYER)

        response = await self.client.post(pets_web.ROUTE_PREFIX + "/api/action", json={
            "init_data": _init_data(PLAYER["id"]), "action": "achievements_open",
        })

        self.assertEqual(response.status, 200, await response.text())
        data = await response.json()
        self.assertTrue(data["ok"], data)
        self.assertIn("achievements", data)
        self.assertTrue(data["achievements"]["rows"])
        self.assertIn("claimable", data["achievements"])

    async def test_a_replay_is_the_same_fight_blow_for_blow(self):
        """Not "a fight like that one" -- that one. The stored seed and the two stored
        fighters go back through the same pure simulate(), so every round, every flavour
        line and the verdict have to come back identical. If this ever drifts, the page is
        showing players a fight that never happened."""
        self._tame(PLAYER)
        self._tame(OPPONENT, name="Соперник")
        # Freeze one effect-bearing item into the replay. Its explanatory copy is part of
        # the audit contract just as much as the stats and rounds are.
        echo_item = next(
            i for i in C.ITEMS
            if dict(i.effect or {}).get("code") == "echo_strike"
            and dict(i.effect or {}).get("value") == 100
        )
        stored = pets._load(CHAT)
        hero = stored["pets"][str(PLAYER["id"])]
        hero["inventory"].append(echo_item.code)
        hero["equipped"][echo_item.slot] = echo_item.code
        pets._save(CHAT, stored)

        live = await (await self.client.post(pets_web.ROUTE_PREFIX + "/api/attack", json={
            "init_data": _init_data(PLAYER["id"]), "opponent_id": str(OPPONENT["id"]),
        })).json()

        fight_id = (await (await self._get("/api/history", PLAYER)).json())["rows"][0]["id"]
        again = await (await self._get(f"/api/replay?id={fight_id}", PLAYER)).json()

        self.assertTrue(again["replay"])
        self.assertEqual(again["winner"], live["winner"])
        self.assertEqual(again["draw"], live["draw"])
        self.assertEqual(again["opening"], live["opening"])
        self.assertEqual(again["closing"], live["closing"])
        self.assertEqual(again["rounds"], live["rounds"])
        self.assertEqual(again["max_hp"], live["max_hp"])
        self.assertEqual(again["fighters"], live["fighters"])
        hero_header = live["fighters"][str(PLAYER["id"])]
        self.assertEqual(
            set(hero_header["stats"]),
            {"strength", "health", "agility", "luck", "magic", "armor"},
        )
        self.assertTrue(hero_header["portrait"].endswith(f"/{PLAYER['id']}.jpg"))
        displayed = next(i for i in hero_header["items"] if i["code"] == echo_item.code)
        self.assertEqual(displayed["description"], echo_item.description)
        self.assertEqual(displayed["effect"]["value"], 100)

        audited = await self.client.get(f"/audit/api/fights?id={fight_id}")
        audit_hero = (await audited.json())["fight"]["fighters"][str(PLAYER["id"])]
        audit_item = next(i for i in audit_hero["equipped"] if i["code"] == echo_item.code)
        self.assertEqual(audit_item["description"], echo_item.description)
        self.assertEqual(audit_item["effect"]["value"], 100)

    async def test_a_replay_pays_nothing_and_reports_what_was_paid(self):
        """Watching a fight again must not re-run it: no coins, no XP, no fight spent, and
        the money on screen is what the ledger recorded at the time rather than today's
        prices applied to an old result."""
        self._tame(PLAYER)
        self._tame(OPPONENT, name="Соперник")

        live = await (await self.client.post(pets_web.ROUTE_PREFIX + "/api/attack", json={
            "init_data": _init_data(PLAYER["id"]), "opponent_id": str(OPPONENT["id"]),
        })).json()
        after_fight = await (await self._get("/api/state", PLAYER)).json()

        fight_id = (await (await self._get("/api/history", PLAYER)).json())["rows"][0]["id"]
        replayed = await (await self._get(f"/api/replay?id={fight_id}", PLAYER)).json()

        settled = await (await self._get("/api/state", PLAYER)).json()
        self.assertEqual(settled["coins"], after_fight["coins"])
        self.assertEqual(settled["arena"]["available"], after_fight["arena"]["available"])
        self.assertEqual(len((await (await self._get("/api/history", PLAYER)).json())["rows"]), 1)
        # A replay carries no fresh state to apply, either -- the client must not be able
        # to mistake it for something that changed the game.
        self.assertNotIn("state", replayed)

        won = live["winner"] == str(PLAYER["id"])
        self.assertEqual(replayed["reward"]["gold"], live["reward"]["gold"] if won else 0)

    async def test_the_defender_can_replay_the_fight_from_their_own_side(self):
        """A fight has two participants and one transcript. The defender never pressed
        anything, so their mailbox is the only place they will ever see it -- and it has to
        play from their side, with them on the left."""
        self._tame(PLAYER)
        self._tame(OPPONENT, name="Соперник")

        await self.client.post(pets_web.ROUTE_PREFIX + "/api/attack", json={
            "init_data": _init_data(PLAYER["id"]), "opponent_id": str(OPPONENT["id"]),
        })

        rows = (await (await self._get("/api/mail", OPPONENT)).json())["rows"]
        self.assertEqual(rows[0]["kind"], "defense")
        self.assertTrue(rows[0]["replayable"])

        replayed = await (await self._get(
            f"/api/replay?id={rows[0]['fight_id']}", OPPONENT)).json()
        self.assertEqual(replayed["you"], str(OPPONENT["id"]))
        self.assertEqual(replayed["opponent"]["user_id"], str(PLAYER["id"]))
        # Same transcript as the attacker sees, only read from the other chair.
        attacker_rows = (await (await self._get("/api/history", PLAYER)).json())["rows"]
        mine = await (await self._get(
            f"/api/replay?id={attacker_rows[0]['id']}", PLAYER)).json()
        self.assertEqual(replayed["rounds"], mine["rounds"])

    async def test_a_stranger_cannot_replay_a_fight_they_were_not_in(self):
        """The fight log is chat-wide on disk. Participation is the whole access rule, and
        a timestamp is guessable, so it is enforced at the lookup rather than by whichever
        list happened to hand out the id."""
        self._tame(PLAYER)
        self._tame(OPPONENT, name="Соперник")
        self._tame(THIRD, name="Третий")

        await self.client.post(pets_web.ROUTE_PREFIX + "/api/attack", json={
            "init_data": _init_data(PLAYER["id"]), "opponent_id": str(OPPONENT["id"]),
        })
        fight_id = (await (await self._get("/api/history", PLAYER)).json())["rows"][0]["id"]

        outsider = await self._get(f"/api/replay?id={fight_id}", THIRD)
        self.assertEqual(outsider.status, 404)
        missing = await self._get("/api/replay?id=2020-01-01T00:00:00", PLAYER)
        self.assertEqual(missing.status, 404)

    async def test_a_fight_from_before_snapshots_says_so_instead_of_faking_one(self):
        """Fights recorded before the snapshot was kept have nothing to replay from. The
        list marks them unplayable so they never become a button, and the route refuses
        rather than re-rolling a plausible-looking fight that is not the one that
        happened."""
        self._tame(PLAYER)
        self._tame(OPPONENT, name="Соперник")
        await self.client.post(pets_web.ROUTE_PREFIX + "/api/attack", json={
            "init_data": _init_data(PLAYER["id"]), "opponent_id": str(OPPONENT["id"]),
        })

        data = pets._load(CHAT)
        rows = pets.fight_log_rows(CHAT)
        rows[0]["combat_snapshot"] = None
        pets.stats._write_json_atomic(pets._fight_log_path(CHAT), rows)
        pets._save(CHAT, data)

        row = (await (await self._get("/api/history", PLAYER)).json())["rows"][0]
        self.assertFalse(row["replayable"])
        refused = await self._get(f"/api/replay?id={row['id']}", PLAYER)
        self.assertEqual(refused.status, 409)
        self.assertEqual((await refused.json())["error"], "NO_REPLAY")

    async def test_a_fight_whose_rules_have_changed_is_not_replayed_as_if_they_had_not(self):
        """A replay is trustworthy only while the simulator still agrees with the recorded
        verdict. If a rebalance changes who would win, the stored transcript is no longer
        this fight -- and a confident animation ending on the wrong winner is a worse
        answer than admitting it."""
        self._tame(PLAYER)
        self._tame(OPPONENT, name="Соперник")
        await self.client.post(pets_web.ROUTE_PREFIX + "/api/attack", json={
            "init_data": _init_data(PLAYER["id"]), "opponent_id": str(OPPONENT["id"]),
        })

        data = pets._load(CHAT)
        recorded = pets.fight_log_rows(CHAT)[0]
        # Stand in for "the rules moved" by flipping the recorded winner: the check is
        # simulate-vs-record, so either side of it drifting trips the same wire.
        recorded["winner_id"], recorded["loser_id"] = recorded["loser_id"], recorded["winner_id"]
        pets._save(CHAT, data)

        refused = await self._get(f"/api/replay?id={pets.fight_id(recorded)}", PLAYER)
        self.assertEqual(refused.status, 409)
        self.assertEqual((await refused.json())["error"], "RULES_CHANGED")
        self.assertTrue(self._logged("drifted"))

    async def test_a_replay_is_narrated_with_the_names_the_fight_was_fought_under(self):
        """The transcript is prose with the pets' names baked into every line. The client
        highlights those names, and the header prints them -- so it needs the names AS OF
        the fight, not today's. After a rename, reading them off the live pet would name a
        creature that appears nowhere in the text being displayed."""
        self._tame(PLAYER, name="Кабанчик")
        self._tame(OPPONENT, name="Соперник")
        await self.client.post(pets_web.ROUTE_PREFIX + "/api/attack", json={
            "init_data": _init_data(PLAYER["id"]), "opponent_id": str(OPPONENT["id"]),
        })

        self.assertTrue(pets.rename(CHAT, PLAYER["id"], "Совсем другой")[0])
        row = (await (await self._get("/api/history", PLAYER)).json())["rows"][0]
        replayed = await (await self._get(f"/api/replay?id={row['id']}", PLAYER)).json()

        self.assertEqual(replayed["you_name"], "Кабанчик")
        self.assertEqual(replayed["opponent"]["name"], "Соперник")
        # And it really is the name the rounds talk about.
        self.assertTrue(any("Кабанчик" in r["text"] for r in replayed["rounds"]))

    async def test_both_pages_mark_a_transcript_from_the_one_table(self):
        """The game and the audit must not disagree about what a crit looks like, so the
        vocabulary is generated from pets_flavor rather than typed into two pages."""
        import pets_flavor

        app_page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        audit_page = await (await self.client.get("/audit")).text()

        for name, page in (("app", app_page), ("audit", audit_page)):
            with self.subTest(page=name):
                # Substituted, not shipped as a placeholder.
                self.assertNotIn("__EVENT_MARKS__", page)
                self.assertIn("const EVENT_MARKS=", page.replace("EVENT_MARKS = ", "EVENT_MARKS="))
                self.assertIn("function eventMark(", page)
                # A couple of marks the table actually carries, so a page that shipped an
                # empty object fails here rather than silently drawing no icons.
                self.assertIn(pets_flavor.EVENT_MARKS["crit"][1], page)
                self.assertIn(pets_flavor.EVENT_MARKS["dodge"][1], page)

    async def test_the_audit_page_says_whose_turn_it_is_and_can_play_a_fight(self):
        """Reading somebody else's fight meant reading raw keys like `dungeon:boss_15`
        down a column of collapsed rows. It names the fighter now, tags what kind of turn
        it was, and will play the whole thing back."""
        page = await (await self.client.get("/audit")).text()

        # Whose turn, by name, with the kind beside it.
        self.assertIn("function moveHead(", page)
        self.assertIn("function actorOf(", page)
        self.assertIn("function castOf(", page)
        self.assertIn("moveHead(m)", page)
        # A replay, with its own controls and two health bars.
        self.assertIn("function playFight(", page)
        self.assertIn('id="pplay"', page)
        self.assertIn('id="pall"', page)
        self.assertIn('id="plog"', page)
        self.assertIn("stopPlayback()", page)
        # And the pet filter it already had is still how you get to another player.
        self.assertIn('id="petSearch"', page)
        self.assertIn('id="allPets"', page)

    async def test_the_fight_log_colours_the_three_things_a_line_is_made_of(self):
        """pets_flavor fills exactly {attacker}, {defender} and {amount} into every
        template, so those three are what the log highlights -- and an amulet's amount is
        not always damage, which is the distinction the tone map exists for."""
        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        self.assertIn("function paintBlow(", page)
        self.assertIn("function amountTone(", page)
        for tone in (".duel .nm.mine", ".duel .nm.them",
                     ".duel .amount.harm", ".duel .amount.heal", ".duel .amount.soak"):
            self.assertIn(tone, page)
        # Healing procs must be listed by the code pets_combat actually emits, or a
        # vampiric drain gets painted as damage taken.
        for code in ("vampiric", "second_wind", "dodge_heal", "regen"):
            self.assertIn(code, page)
        # Both the animated path and the skip button go through the same painter AND the
        # same head, so a skipped fight is not a differently formatted one. Matched
        # without pinning whitespace: what matters is that neither call site skips either.
        self.assertEqual(page.count("function paintBlow("), 1)
        self.assertEqual(page.count("function blowHead("), 1)
        animated = re.search(
            r"blowHead\(round, mineName, theirName, me\)\s*\+\s*"
            r"paintBlow\(round, mineName, theirName\)", page)
        skipped = re.search(
            r"blowHead\(r, mineName, theirName, me\)\s*\+\s*"
            r"paintBlow\(r, mineName, theirName\)", page)
        self.assertTrue(animated, "the animated line lost its head or its painter")
        self.assertTrue(skipped, "the skipped line lost its head or its painter")
        # The two side colours are their own tokens: --xp/--hp already mean "your money
        # went up/down" everywhere else, and the numbers keep saying that.
        self.assertIn("--mine:", page)
        self.assertIn("--foe:", page)

    async def test_arena_timers_update_once_a_minute_without_rebuilding_hud_portrait(self):
        """Minute-resolution timers avoid recreating the opponent roster and avatars every
        second, while the HUD still keeps its face node behind its change guard."""
        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        self.assertIn("const TIMER_TICK_SECONDS = 60;", page)
        self.assertIn("setInterval(tick, TIMER_TICK_MS);", page)
        self.assertNotIn("setInterval(tick, 1000)", page)
        self.assertIn("let hudFaceKey = null;", page)
        self.assertIn("if (faceKey !== hudFaceKey) {", page)
        # The repaint has to be INSIDE the guard, so grab the guarded block and look.
        guarded = page.split("if (faceKey !== hudFaceKey) {", 1)[1].split("}", 1)[0]
        self.assertIn('$("hudFace").innerHTML', guarded)
        self.assertEqual(page.count('$("hudFace").innerHTML'), 1)

    async def test_shop_cards_show_an_items_effect_without_opening_the_detail_sheet(self):
        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        self.assertIn("function shopCard(item)", page)
        self.assertIn("item.effect && item.effect.text", page)
        self.assertIn("✨ ' + esc(item.effect.text)", page)

    async def test_the_page_turns_a_logged_fight_into_a_button(self):
        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        self.assertIn("async function replay(id)", page)
        self.assertIn("function playDuel(data)", page)
        self.assertIn("data-replay=", page)
        self.assertIn("if (d.replay) { haptic(); replay(d.replay); return; }", page)
        # The live fight and the replay must go through one playback, or they drift.
        self.assertEqual(page.count("function playDuel("), 1)
        self.assertIn(".duel .rerun", page)
        self.assertIn('class="matchup"', page)
        self.assertIn("function duelFighter(fighter, fallbackArt)", page)
        self.assertIn("data-fight-detail=", page)
        self.assertIn("function openFightDetail(key)", page)
        self.assertIn("Точные параметры:", page)
        self.assertNotIn("duel-effects", page)
        self.assertIn("function openDuelPortrait(key)", page)
        self.assertIn("data-duel-portrait=", page)
        self.assertIn("overlay.onclick = close;", page)

    async def test_arena_prefetches_five_mobs_and_fights_a_selected_local_offer(self):
        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        arena = page.split("async function renderArena()", 1)[1].split(
            "// --------------------------------------------------------- turn-based", 1,
        )[0]
        self.assertIn("Promise.all([", arena)
        self.assertIn('api("/api/mob")', arena)
        self.assertIn("MOBS = results[1].mobs", arena)
        self.assertIn('data-mobfight="', page)
        self.assertIn("MOBS.splice(Number(index), 1);", page)

    async def test_a_fight_refused_at_the_last_moment_still_takes_the_mirror_back_off(self):
        """The automatic Зеркало души goes on BEFORE record_fight and comes off after.

        record_fight raises when the bank emptied or the pet walked off to the farm
        between drawing the page and pressing the button, and returning from that used to
        skip the restore -- leaving the mirror worn and the player's own amulet stranded.
        """
        self._tame(PLAYER)
        self._tame(OPPONENT, name="Соперник")
        data = pets._load(CHAT)
        me = data["pets"][str(PLAYER["id"])]
        me["level"] = 20
        data["pets"][str(OPPONENT["id"])]["level"] = 1
        me["inventory"] = ["amulet_red_button", pets.MIRROR_AMULET_CODE]
        me["equipped"]["amulet"] = "amulet_red_button"
        pets._save(CHAT, data)

        with patch.object(pets, "record_fight", side_effect=ValueError("Бои кончились.")):
            response = await self.client.post(pets_web.ROUTE_PREFIX + "/api/attack", json={
                "init_data": _init_data(PLAYER["id"]), "opponent_id": str(OPPONENT["id"]),
            })

        self.assertEqual(response.status, 409)
        after = pets.get_pet(CHAT, PLAYER["id"])
        self.assertEqual(after["equipped"]["amulet"], "amulet_red_button")
        self.assertNotIn("mirror_restore", after)

    async def test_the_dungeon_is_its_own_tab_beside_the_arena(self):
        """It is a whole game mode; it used to be wedged into the middle of the hero page."""
        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        self.assertIn('<button data-tab="dungeon"><span class="ic">🏰</span>Данж</button>', page)
        self.assertIn('id="scr-dungeon"', page)
        self.assertIn('else if (TAB === "dungeon") renderDungeon();', page)
        self.assertIn("function renderDungeon()", page)
        self.assertIn('"hero", "bag", "shop", "arena", "dungeon", "farm", "quests", "more"', page)

        # Right OF the arena, and the hero page no longer renders it.
        tabs = page.split('<nav class="tabs"', 1)[-1] if '<nav class="tabs"' in page else page
        self.assertLess(tabs.index('data-tab="arena"'), tabs.index('data-tab="dungeon"'))
        self.assertLess(tabs.index('data-tab="dungeon"'), tabs.index('data-tab="farm"'))
        hero = page.split("function renderHero()", 1)[1].split("function tile(", 1)[0]
        self.assertNotIn("dungeonPanel()", hero)
        # The grid has to grow with the row, or the new tab overflows the bar.
        self.assertIn("grid-template-columns: repeat(8, 1fr);", page)
        self.assertIn(".tabs.has-review { grid-template-columns: repeat(9, 1fr); }", page)

    async def test_the_quarry_offers_the_same_early_recall_the_farm_does(self):
        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        self.assertIn('data-do="quarrycancel"', page)
        self.assertIn('d.do === "quarrycancel"', page)
        self.assertIn("Заплатят по ближайшей меньшей смене", page)
        self.assertIn("quarry_cancel", pets_web._ACTIONS)

        self._tame(PLAYER)
        data = pets._load(CHAT)
        data["pets"][str(PLAYER["id"])]["pickaxe_runs"] = 1
        pets._save(CHAT, data)
        self.assertTrue(pets.start_quarry(CHAT, PLAYER["id"], 8)[0])

        response = await self.client.post(pets_web.ROUTE_PREFIX + "/api/action", json={
            "init_data": _init_data(PLAYER["id"]), "action": "quarry_cancel",
        })
        self.assertEqual(response.status, 200, await response.text())
        self.assertFalse((await response.json())["state"]["quarry"]["running"])

    async def test_farm_screen_hides_start_buttons_while_the_creature_is_elsewhere(self):
        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        farm = page.split("function renderFarm()", 1)[1].split(
            "const FEATURE_NAMES", 1,
        )[0]
        # One creature, one place: the hour chips are replaced by the reason, not merely
        # disabled, on both sides.
        self.assertIn("farm.blocked_by_quarry", farm)
        self.assertIn("quarry.blocked_by_farm", farm)
        self.assertIn('busyElsewhere("в карьере")', farm)
        self.assertIn('busyElsewhere("на ферме")', farm)
        self.assertIn("обе фигурки: фермера и шахтёра", page)
        # Buying a pickaxe is not going anywhere, so it survives the block.
        self.assertIn('const hasPickaxe = quarry.pickaxe_unlimited', farm)
        self.assertIn("figurinePanel(farm)", farm)

    async def test_pve_shows_one_mob_and_swaps_it_without_a_request(self):
        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        panel = page.split("function mobPanel(farmBlocked)", 1)[1].split(
            "function debuffTag(", 1,
        )[0]
        # One card on screen, chosen by the pointer -- not the whole prefetched batch.
        self.assertNotIn("MOBS.map(", panel)
        self.assertIn("const mob = MOBS[index];", panel)
        self.assertIn('data-mob="next"', panel)
        # The swap itself must stay local: an index step and a repaint, no api() call.
        swap = page.split("function nextMob()", 1)[1].split("async function refillMobs()", 1)[0]
        self.assertIn("MOB_INDEX = (MOB_INDEX + 1) % MOBS.length;", swap)
        self.assertNotIn("api(", swap)
        self.assertIn('if (d.mob === "next") { nextMob(); return; }', page)
        # Only a batch the player has seen through costs a request, and it runs detached.
        self.assertIn("if (MOB_INDEX === 0) refillMobs();", swap)

    async def test_back_to_back_mob_fights_both_land_with_no_artificial_wait(self):
        """The web app used to force a full second between mob-fight requests -- a delay
        the bot side never had. record_mob_fight already serialises the attack counter
        under its own lock, so two fights fired immediately one after another must both
        be counted, neither dropped nor double-counted, and neither refused as a replay."""
        self._tame(PLAYER)
        offer = await (await self._get("/api/mob", PLAYER)).json()
        first_mob = offer["mob"]

        first = await self.client.post(pets_web.ROUTE_PREFIX + "/api/mob", json={
            "init_data": _init_data(PLAYER["id"]),
            "code": first_mob["code"], "tier": first_mob["tier"],
        })
        self.assertEqual(first.status, 200, await first.text())

        second_offer = await (await self._get("/api/mob", PLAYER)).json()
        second_mob = second_offer["mob"]
        second = await self.client.post(pets_web.ROUTE_PREFIX + "/api/mob", json={
            "init_data": _init_data(PLAYER["id"]),
            "code": second_mob["code"], "tier": second_mob["tier"],
        })
        self.assertEqual(second.status, 200, await second.text())

        record = pets.get_pet(CHAT, PLAYER["id"])
        self.assertEqual(record["fights"], 2)

        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        self.assertNotIn("PVE_ACTION_COOLDOWN_SECONDS", page)
        self.assertNotIn("_pve_action_cooldowns", page)
        # Fast clicks queue instead of vanishing while a fight is already in flight.
        self.assertIn("MOB_FIGHT_QUEUED", page)
        self.assertIn("MOB_FIGHT_QUEUED++", page)

    async def test_hero_order_and_pve_replay_controls_are_exposed_by_the_page(self):
        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        hero = page.split("function renderHero()", 1)[1].split("function tile(", 1)[0]
        self.assertLess(hero.index('<h2>Характеристики</h2>'), hero.index('<h2>В бою</h2>'))
        self.assertLess(hero.index('<h2>В бою</h2>'), hero.index("liveSkillsPanel()"))
        self.assertIn("Дом существа: увеличивает запас боёв и золото за победы.", page)
        self.assertIn('if (!(S.pet && S.pet.skip_pve_replays)) { playDuel(data); return; }', page)
        self.assertIn('"Пропускать бои"', page)
        self.assertIn('"Не пропускать бои"', page)
        self.assertIn('data-quarrystart="', page)
        self.assertIn('grid-template-columns:repeat(4,minmax(0,1fr))', page)

    async def test_farm_quarry_tool_prompts_and_scrollable_quest_briefs_are_exposed(self):
        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        farm = page.split("function renderFarm()", 1)[1].split(
            "// ------------------------------------------------------------------------ more screen", 1,
        )[0]

        self.assertIn("const FARM_QUICK_HOURS = [1, 2, 4, 8];", page)
        self.assertIn("FARM_QUICK_HOURS.includes(Number(preview.hours))", farm)
        self.assertIn("box.innerHTML = shift + quarryPanel +", farm)
        self.assertIn("Покрась лопату в технике <b>NMM</b>", farm)
        self.assertIn("+50% золота", farm)
        self.assertIn("Покрась кирку в технике <b>NMM</b>", farm)
        self.assertIn("+50% ко всей добыче", farm)

        self.assertIn("function questBenefit(card)", page)
        self.assertIn('class="quest-technique"', page)
        self.assertIn('class="quest-benefit"', page)
        self.assertIn('"quest-sheet"', page)
        self.assertIn("touch-action:pan-y", page)
        self.assertIn("tg.disableVerticalSwipes", page)
        self.assertIn("tg.enableVerticalSwipes", page)

    # ---- the mailbox --------------------------------------------------------------------

    async def test_mail_returns_the_readers_own_feed_with_server_side_times(self):
        """Fights, farm shifts and gifts in one list -- and the HH.MM and day heading come
        from the server, because the page's only clock is the phone's and the chat's
        timezone is what every other timestamp in the game is in."""
        self._tame(PLAYER)
        self._tame(OPPONENT, name="Соперник")

        await self.client.post(pets_web.ROUTE_PREFIX + "/api/attack", json={
            "init_data": _init_data(PLAYER["id"]), "opponent_id": str(OPPONENT["id"]),
        })

        rows = (await (await self._get("/api/mail", PLAYER)).json())["rows"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["kind"], "attack")
        self.assertEqual(row["pet_name"], "Соперник")
        self.assertIn(row["outcome"], ("win", "loss", "draw"))
        self.assertRegex(row["at"], r"^\d{2}\.\d{2}$")
        self.assertEqual(row["day_label"], "Сегодня")
        self.assertIsInstance(row["ts"], str)

        # The same fight, from the other side, is a defence -- one event, two mailboxes.
        theirs = (await (await self._get("/api/mail", OPPONENT)).json())["rows"]
        self.assertEqual(theirs[0]["kind"], "defense")

    async def test_mail_is_gated_and_empty_for_a_player_with_nothing_yet(self):
        unsigned = await self.client.get(pets_web.ROUTE_PREFIX + "/api/mail")
        self.assertEqual(unsigned.status, 401)

        # No cage, no pet, nothing ever happened: an empty feed, not an error.
        rows = (await (await self._get("/api/mail", PLAYER)).json())["rows"]
        self.assertEqual(rows, [])

    async def test_the_page_colour_codes_the_feed_and_keeps_a_way_into_it(self):
        """The mailbox is checked between fights rather than played, so it stays in «Ещё»
        with the other read-only screens -- but the HUD keeps a permanent 📬 into it, and
        the rows are striped by what happened rather than by which subsystem wrote them."""
        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        self.assertIn('id="hudMail"', page)
        self.assertIn('moreView = "mail"', page)
        self.assertIn("mail:📬 Почта", page)
        self.assertIn("function mailFeed(", page)
        # A 20px emoji in a 42px square is not centred by a button's own text layout --
        # it sits on a baseline inside default padding. Flex centring, padding zeroed.
        rule = page.split(".hud .post {", 1)[1].split("}", 1)[0]
        for declaration in ("display: flex", "align-items: center",
                            "justify-content: center", "padding: 0"):
            self.assertIn(declaration, rule)
        for tone in (".mail.win", ".mail.loss", ".mail.gold", ".mail.give"):
            self.assertIn(tone, page)
        # A find is tinted with the same rarity colour its item card is bordered with.
        self.assertIn('style="color:var(--r-', page)

    async def test_the_slot_placeholders_use_the_games_own_emoji(self):
        """⚔ ◈ ▲ are typographic lookalikes and render as flat text next to 🧤, which reads
        as three broken icons beside one working one. The game already has a slot emoji per
        slot; the art placeholder uses those."""
        self.assertEqual(pets_web.SLOT_GLYPHS, dict(C.SLOT_EMOJI))
        for slot, emoji in C.SLOT_EMOJI.items():
            with self.subTest(slot=slot):
                self.assertIn(emoji, pets_web.placeholder_svg("w001", "common", slot))

    # ---- leaderboard peek ---------------------------------------------------------------

    async def test_the_leaderboard_peek_shows_what_another_pet_is_wearing(self):
        """Tapping a name on the ranking opens this. It is about somebody else, so the
        equipped flag has to describe the OWNER -- a panel that answered "do you have
        this equipped" while showing their gear would be answering the wrong question."""
        self._tame(PLAYER)
        self._tame(OPPONENT, name="Соперник")
        item = next(i for i in C.items_for_slot("weapon") if i.source == "shop")
        data = pets._load(CHAT)
        data["pets"][str(OPPONENT["id"])]["inventory"].append(item.code)
        pets._save(CHAT, data)
        self.assertTrue(pets.equip(CHAT, str(OPPONENT["id"]), item.code)[0])

        response = await self._get(
            f"/api/loadout?user_id={OPPONENT['id']}", PLAYER,
        )
        self.assertEqual(response.status, 200, await response.text())
        body = await response.json()

        self.assertEqual(body["user_id"], str(OPPONENT["id"]))
        self.assertEqual(body["name"], "Соперник")
        self.assertEqual({row["slot"] for row in body["slots"]}, set(C.SLOT_KEYS))
        weapon = next(row for row in body["slots"] if row["slot"] == "weapon")
        self.assertEqual(weapon["item"]["code"], item.code)
        # Flagged from the owner's record, not the viewer's -- PLAYER owns none of this.
        self.assertTrue(weapon["item"]["equipped"])
        self.assertTrue(weapon["item"]["description"])
        self.assertIn("bonuses", weapon["item"])
        # Empty slots are still drawn, so the panel shows what is missing too.
        self.assertTrue(any(row["item"] is None for row in body["slots"]))
        self.assertEqual(len(body["skills"]), 4)
        self.assertTrue(all(row["empty"] for row in body["skills"]))

    async def test_the_peek_refuses_an_unknown_or_petless_player(self):
        self._tame(PLAYER)
        missing = await self._get("/api/loadout?user_id=123456", PLAYER)
        self.assertEqual(missing.status, 404, await missing.text())
        blank = await self._get("/api/loadout", PLAYER)
        self.assertEqual(blank.status, 400, await blank.text())

    # ---- sprite archetypes ----------------------------------------------------------------
    async def test_a_configured_gemini_generates_frames_in_the_background_and_serves_them(self):
        """The route must never wait for the model. Four generations take tens of seconds,
        so the first look starts the job and answers "pending" with whatever it already
        has; the frames appear on a later look and are then served as ordinary images."""
        self._tame(PLAYER)
        self._photos["file_id"] = _jpeg_bytes()
        png = bytes.fromhex("89504e470d0a1a0a") + b"drawn"
        drawn = {"idle_a": png, "idle_b": png, "attack": png}
        reading = {"archetype": "reptile", "subject": "a lizard"}
        self.app_cfg.gemini_api_key = "gemini-key"
        self.addCleanup(setattr, self.app_cfg, "gemini_api_key", "")
        self.addCleanup(pets_sprite_store.forget, "file_id")

        with (
            patch("pets_gemini.available", return_value=True),
            patch("pets_gemini.analyse", return_value=reading),
            patch("pets_gemini.generate_frames", return_value=drawn),
        ):
            first = await self._get(f"/api/sprite?user_id={PLAYER['id']}", PLAYER)
            self.assertEqual(first.status, 200, await first.text())
            self.assertEqual((await first.json())["status"], "pending")
            # The job is a background task the app holds a reference to; let it finish.
            await asyncio.gather(*list(self.app[pets_web._SPRITE_JOBS_KEY]))

        ready = await (await self._get(f"/api/sprite?user_id={PLAYER['id']}", PLAYER)).json()
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["kind"], "reptile")
        self.assertEqual([f["name"] for f in ready["frames"]], ["idle_a", "idle_b", "attack"])

        # And the frame URLs it handed out actually serve the bytes.
        image = await self.client.get(ready["frames"][0]["url"])
        self.assertEqual(image.status, 200)
        self.assertEqual(await image.read(), png)


    async def test_an_unknown_frame_or_a_creature_without_one_is_a_plain_404(self):
        """The route takes a name straight out of the URL, so it has to be checked against
        the known frames rather than joined onto a path -- otherwise it is a way to ask the
        server for arbitrary files."""
        self._tame(PLAYER)
        for path in (f"/img/sprite/{PLAYER['id']}/idle_a.png",
                     f"/img/sprite/{PLAYER['id']}/..%2F..%2Fsecrets.png",
                     f"/img/sprite/{PLAYER['id']}/nonsense.png"):
            with self.subTest(path=path):
                response = await self.client.get(pets_web.ROUTE_PREFIX + path)
                self.assertEqual(response.status, 404)

    async def test_every_archetype_has_an_idle_animation_on_the_page(self):
        """The archetype list is a contract across three languages, and nothing else joins
        them up: `pets_sprite` decides the code, the page's JS accepts it, and the CSS is
        what actually animates it. Add a thirteenth kind in Python alone and that creature
        stands frozen for the whole fight -- no error anywhere, just a photo that never
        moves. This is the only thing that would catch it."""
        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        for code in pets_sprite.ARCHETYPES:
            with self.subTest(archetype=code):
                self.assertIn(f'"{code}"', page)                    # in the JS whitelist
                self.assertIn(f'@keyframes sprite-idle-{code}', page)
                self.assertIn(f'.sprite[data-kind="{code}"]', page)
        # And the other direction: a keyframe set the vocabulary no longer contains is
        # dead weight that will confuse the next person to read it.
        drawn = set(re.findall(r"@keyframes sprite-idle-([a-z]+)", page))
        self.assertEqual(drawn, set(pets_sprite.ARCHETYPES))

    async def test_the_battle_stage_keeps_its_controls_to_two_rows(self):
        """Eight cells in a four-column grid is what makes the fight fit a phone: the
        stage, then attack, defend, four scrolls, auto and exit. A ninth action would
        silently wrap onto a third row and push the log off the screen."""
        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        self.assertIn("grid-template-columns: repeat(4, 1fr)", page)
        self.assertIn('class="test-stage"', page)
        # The sprite element carries everything the animation needs: which idle to run,
        # which way its attacks travel, and who to re-ask about once classified.
        for attribute in ('data-kind="', 'data-side="', 'data-owner="', "--dir:"):
            with self.subTest(attribute=attribute):
                self.assertIn(attribute, page)


    async def test_the_sprite_route_classifies_a_photo_once_and_remembers_it(self):
        """One vision call per PHOTOGRAPH, not per battle, and not per re-render.

        The battle screen asks on open and re-renders after every action, so a route that
        re-classified would spend a paid model call several times a turn. What makes the
        cache safe is that the answer cannot change while the picture does not."""
        self._tame(PLAYER)
        self._photos["file_id"] = _jpeg_bytes()

        with patch("pets_sprite.classify", return_value="quadruped") as classify:
            first = await self._get(
                f"/api/sprite?user_id={PLAYER['id']}", PLAYER)
            self.assertEqual(first.status, 200, await first.text())
            self.assertEqual(await first.json(), {
                "user_id": str(PLAYER["id"]), "kind": "quadruped",
                "frames": [], "status": "none",
            })
            second = await self._get(f"/api/sprite?user_id={PLAYER['id']}", PLAYER)
            self.assertEqual((await second.json())["kind"], "quadruped")
        self.assertEqual(classify.call_count, 1)

        # And a new photograph is a new subject, so it asks again rather than answering
        # about the picture that is no longer there.
        pets.set_photo(CHAT, str(PLAYER["id"]), "a-different-file")
        self._photos["a-different-file"] = _jpeg_bytes()
        with patch("pets_sprite.classify", return_value="machine") as classify:
            again = await self._get(f"/api/sprite?user_id={PLAYER['id']}", PLAYER)
            self.assertEqual((await again.json())["kind"], "machine")
        self.assertEqual(classify.call_count, 1)

    async def test_a_failed_classification_is_answered_but_never_cached(self):
        """Caching the fallback would record "we could not reach the model" as if it were
        "this is an unrecognisable creature", and nothing would ever ask again."""
        self._tame(PLAYER)
        self._photos["file_id"] = _jpeg_bytes()
        with patch("pets_sprite.classify", return_value="creature") as classify:
            for _ in range(2):
                response = await self._get(f"/api/sprite?user_id={PLAYER['id']}", PLAYER)
                body = await response.json()
                self.assertEqual(response.status, 200)
                self.assertEqual(body["kind"], "creature")
                self.assertEqual(body["frames"], [])
        self.assertEqual(classify.call_count, 2)

    async def test_the_sprite_route_answers_usably_for_a_player_with_no_pet_or_no_photo(self):
        """The battle screen has no way to draw "error". Every path returns a code it can
        animate, so a missing picture degrades to the neutral idle instead of a blank."""
        missing = await self._get("/api/sprite?user_id=999999", PLAYER)
        self.assertEqual(missing.status, 200)
        self.assertEqual((await missing.json())["kind"], "creature")

        self._tame(PLAYER)                      # tamed, but no bytes behind the file_id
        with patch("pets_sprite.classify") as classify:
            no_photo = await self._get(f"/api/sprite?user_id={PLAYER['id']}", PLAYER)
        self.assertEqual((await no_photo.json())["kind"], "creature")
        classify.assert_not_called()

    async def test_a_battle_starts_with_the_archetype_already_known(self):
        """The fighter payload carries whatever has been worked out, so a creature that
        has been classified before animates correctly from the first frame."""
        self._tame(PLAYER)
        pets.remember_sprite(CHAT, str(PLAYER["id"]), "spirit")
        started = await self.client.post(
            pets_web.ROUTE_PREFIX + "/api/test-battle/start",
            json={"init_data": _init_data(PLAYER["id"]), "mode": "manual",
                  "opponent_id": "dummy"},
        )
        self.assertEqual(started.status, 200, await started.text())
        fighters = (await started.json())["battle"]["fighters"]
        self.assertEqual(fighters["player"]["kind"], "spirit")
        self.assertEqual(fighters["player"]["owner_id"], str(PLAYER["id"]))
        # The training golem has no photograph and reads as the lump it is.
        self.assertEqual(fighters["enemy"]["kind"], "blob")
        self.assertIsNone(fighters["enemy"]["owner_id"])

    # ---- granted debuffs ------------------------------------------------------------------

    async def _debuff(self, user, **payload):
        return await self.client.post(
            pets_web.ROUTE_PREFIX + "/api/debuff",
            json={"init_data": _init_data(user["id"]), **payload},
        )

    async def test_only_a_chat_admin_can_open_or_hand_out_a_debuff(self):
        """Same narrow gate as the birthday and the money audit. Both routes ask for
        themselves -- the menu entry the client draws is a convenience, not a permission."""
        self._tame(PLAYER)
        for user in (PLAYER, MODERATOR):
            with self.subTest(user=user["username"]):
                read = await self._get("/api/debuff", user)
                self.assertEqual(read.status, 403, await read.text())
                write = await self._debuff(user, user_id=PLAYER["id"], code="impostor")
                self.assertEqual(write.status, 403, await write.text())

        read = await self._get("/api/debuff", THIRD)   # the economy admin in this fixture
        self.assertEqual(read.status, 200, await read.text())
        body = await read.json()
        self.assertEqual(body["holders"], [])
        self.assertIn("impostor", [row["code"] for row in body["debuffs"]])
        self.assertIn(str(PLAYER["id"]), [row["user_id"] for row in body["candidates"]])

    async def test_the_catalogue_the_admin_picks_from_carries_the_players_own_copy(self):
        """The admin chooses by reading the same title, joke and get-out line the player
        will read. Picking a punishment by machine code is how the wrong one goes out."""
        read = await self._get("/api/debuff", THIRD)
        mark = next(row for row in (await read.json())["debuffs"] if row["code"] == "impostor")
        for field in ("emoji", "title", "line", "description", "hint"):
            with self.subTest(field=field):
                self.assertEqual(mark[field], C.DEBUFFS["impostor"][field])

    async def test_a_granted_debuff_reaches_the_arena_the_state_and_the_peek_panel(self):
        """The three places a player actually meets it, and each has to carry the
        explanation with it -- a stat penalty with no words attached reads as a bug."""
        self._tame(PLAYER)
        self._tame(THIRD, name="Наблюдатель")
        granted = await self._debuff(THIRD, user_id=PLAYER["id"], code="impostor")
        self.assertEqual(granted.status, 200, await granted.text())

        state = await (await self._get("/api/state", PLAYER)).json()
        self.assertEqual(state["debuff"]["title"], C.DEBUFFS["impostor"]["title"])
        self.assertEqual(state["debuff"]["hint"], C.DEBUFFS["impostor"]["hint"])

        # The marked player's own arena, and the same mark on somebody else's roster row.
        mine = await (await self._get("/api/opponents", PLAYER)).json()
        self.assertEqual(mine["my_debuff"]["code"], "impostor")
        theirs = await (await self._get("/api/opponents", THIRD)).json()
        row = next(r for r in theirs["opponents"] if r["user_id"] == str(PLAYER["id"]))
        self.assertEqual(row["debuff"]["description"], C.DEBUFFS["impostor"]["description"])

        peek = await (await self._get(
            "/api/loadout?user_id=" + str(PLAYER["id"]), THIRD)).json()
        self.assertEqual(peek["debuff"]["code"], "impostor")

        # Nobody else picked one up.
        self.assertIsNone(theirs["my_debuff"])
        self.assertIsNone((await (await self._get("/api/state", THIRD)).json())["debuff"])

    async def test_a_debuff_can_be_taken_off_by_hand_and_by_changing_the_picture(self):
        self._tame(PLAYER)
        self.assertEqual((await self._debuff(THIRD, user_id=PLAYER["id"], code="impostor")).status, 200)
        cleared = await self._debuff(THIRD, user_id=PLAYER["id"], clear=True)
        self.assertEqual(cleared.status, 200, await cleared.text())
        self.assertEqual((await cleared.json())["holders"], [])

        # And again, this time letting the player lift it themselves by uploading a new
        # portrait through the page -- the route the feature is actually about.
        self.assertEqual((await self._debuff(THIRD, user_id=PLAYER["id"], code="impostor")).status, 200)
        self.assertIsNotNone(pets.debuff(CHAT, str(PLAYER["id"])))
        uploaded = await self._upload_portrait(PLAYER, _jpeg_bytes())
        self.assertEqual(uploaded.status, 200, await uploaded.text())
        self.assertIsNone(pets.debuff(CHAT, str(PLAYER["id"])))

    async def test_an_unknown_code_or_a_petless_player_is_refused(self):
        self._tame(PLAYER)
        bad_code = await self._debuff(THIRD, user_id=PLAYER["id"], code="no_such_mark")
        self.assertEqual(bad_code.status, 400, await bad_code.text())
        self.assertEqual((await bad_code.json())["error"], "BAD_DEBUFF")

        no_pet = await self._debuff(THIRD, user_id=MODERATOR["id"], code="impostor")
        self.assertEqual(no_pet.status, 400, await no_pet.text())

    # ---- admin resource grants -----------------------------------------------------------

    async def _grant(self, user, **payload):
        return await self.client.post(
            pets_web.ROUTE_PREFIX + "/api/grant",
            json={"init_data": _init_data(user["id"]), **payload},
        )

    # ---- supporting the project --------------------------------------------------------

    async def test_the_support_page_shows_the_pitch_and_the_roll_of_honour(self):
        donations.add_donor(CHAT, "Кломбик", 25, "своё оружие")
        donations.add_donor(CHAT, "БИ-2", 10)
        body = await (await self._get("/api/support", PLAYER)).json()
        self.assertTrue(body["paragraphs"])
        self.assertTrue(body["perks"])
        self.assertEqual([row["name"] for row in body["donors"]], ["Кломбик", "БИ-2"])
        self.assertEqual(body["donors"][0]["amount"], 25)
        self.assertEqual(body["donors"][0]["note"], "своё оружие")

        blocked = await self._get("/api/support", NONMEMBER)
        self.assertEqual(blocked.status, 403)

    async def test_a_pledge_is_recorded_and_the_owner_is_told(self):
        """The pledge is the record; the message to the owner is only the tap on the
        shoulder. Nothing here takes payment details -- just a number and who typed it."""
        answer = await self.client.post(pets_web.ROUTE_PREFIX + "/api/support", json={
            "init_data": _init_data(PLAYER["id"]), "amount": 20,
        })
        self.assertEqual(answer.status, 200, await answer.text())
        body = await answer.json()
        self.assertIn("свяжусь", body["thanks"])

        stored = donations.pledges(CHAT)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["amount"], 20)
        self.assertEqual(stored[0]["user_id"], str(PLAYER["id"]))
        self.assertEqual(stored[0]["id"], body["pledge_id"])

        self.assertEqual(len(self.support_pledges), 1)
        summary = donations.pledge_summary(self.support_pledges[0])
        self.assertIn("$20", summary)
        self.assertIn(str(PLAYER["id"]), summary)

    async def test_a_pledge_survives_the_owner_being_unreachable(self):
        """A bot cannot open a chat with somebody who never started it, so the DM is
        allowed to fail -- but the pledge must not be lost with it."""
        self.support_notify_raises = RuntimeError("cannot message the owner")
        answer = await self.client.post(pets_web.ROUTE_PREFIX + "/api/support", json={
            "init_data": _init_data(PLAYER["id"]), "amount": 15,
        })
        self.assertEqual(answer.status, 200, await answer.text())
        self.assertEqual(len(donations.pledges(CHAT)), 1)
        self.assertEqual(donations.pledges(CHAT)[0]["amount"], 15)

    async def test_a_pledge_refuses_anything_that_is_not_a_sum(self):
        for bad in ("", "как-нибудь потом", 0, -5, 999999999):
            with self.subTest(amount=bad):
                answer = await self.client.post(pets_web.ROUTE_PREFIX + "/api/support", json={
                    "init_data": _init_data(PLAYER["id"]), "amount": bad,
                })
                self.assertEqual(answer.status, 400, await answer.text())
                self.assertEqual((await answer.json())["error"], "BAD_AMOUNT")
        self.assertEqual(donations.pledges(CHAT), [])
        # And the shapes a person really types all work.
        for good, expected in (("5", 5), ("$20", 20), ("12.7", 12), ("30 долларов", 30)):
            with self.subTest(amount=good):
                self.assertEqual(pets_web._support_amount(good), expected)

    async def test_the_support_entry_is_the_quietest_thing_on_the_main_screen(self):
        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        # A muted underlined line at the very bottom of the hero screen, not a button.
        hero = page.split("function renderHero()", 1)[1].split("function tile(", 1)[0]
        self.assertIn('<div class="support-line"><a data-support="open">', hero)
        self.assertLess(hero.index('data-do="rename"'), hero.index('data-support="open"'))
        self.assertIn(".support-line a { color:var(--muted); font-size:12px;", page)
        # Read -> confirm -> amount, and the way out is offered on the confirm step.
        self.assertIn("function openSupportConfirm()", page)
        self.assertIn('data-support="amount"', page)
        self.assertIn("Нет, просто смотрел", page)
        self.assertIn("async function sendSupport()", page)

    async def test_only_a_chat_admin_can_open_or_use_the_grant_panel(self):
        """Same narrow gate as debuffs and the birthday -- both routes ask for themselves."""
        self._tame(PLAYER)
        for user in (PLAYER, MODERATOR):
            with self.subTest(user=user["username"]):
                read = await self._get("/api/grant", user)
                self.assertEqual(read.status, 403, await read.text())
                write = await self._grant(user, user_id=PLAYER["id"], resource="gold", amount=100)
                self.assertEqual(write.status, 403, await write.text())

        read = await self._get("/api/grant", THIRD)
        self.assertEqual(read.status, 200, await read.text())
        body = await read.json()
        self.assertEqual(
            {row["code"] for row in body["resources"]},
            {"gold", "rubies", "farm_tickets", "dungeon_tickets", "server_xp", "arena_xp"},
        )
        self.assertIn(str(PLAYER["id"]), [row["user_id"] for row in body["candidates"]])

    async def test_granting_each_resource_moves_the_real_balance(self):
        self._tame(PLAYER)
        before = economy.balance(CHAT, PLAYER["id"], RICH_XP)
        gold = await self._grant(THIRD, user_id=PLAYER["id"], resource="gold", amount=500)
        self.assertEqual(gold.status, 200, await gold.text())
        self.assertIn("500", (await gold.json())["message"])
        self.assertEqual(economy.balance(CHAT, PLAYER["id"], RICH_XP), before + 500)

        rubies = await self._grant(THIRD, user_id=PLAYER["id"], resource="rubies", amount=250)
        self.assertEqual(rubies.status, 200, await rubies.text())
        self.assertEqual(pets.ruby_balance(CHAT, PLAYER["id"]), 250)

        farm = await self._grant(THIRD, user_id=PLAYER["id"], resource="farm_tickets", amount=3)
        self.assertEqual(farm.status, 200, await farm.text())
        self.assertEqual(pets.farm_tickets(CHAT, PLAYER["id"]), 3)

        dungeon = await self._grant(THIRD, user_id=PLAYER["id"], resource="dungeon_tickets", amount=2)
        self.assertEqual(dungeon.status, 200, await dungeon.text())
        self.assertEqual(pets.dungeon_tickets(CHAT, PLAYER["id"]), 2)

    async def test_xp_can_be_given_and_taken_back_from_the_same_panel(self):
        """The two XP kinds are the only signed resources: everything else is a wallet
        that is topped up by hand, but XP is what people are RANKED by, so a mistake in it
        has to be reversible without a shell."""
        self._tame(PLAYER)

        # Arena XP moves the creature's level with it.
        up = await self._grant(THIRD, user_id=PLAYER["id"], resource="arena_xp", amount=50_000)
        self.assertEqual(up.status, 200, await up.text())
        # Granted XP BANKS levels rather than granting them -- an admin top-up gives a
        # stack of levels the player can afford to buy, not fifty free ones.
        self.assertEqual(pets.get_pet(CHAT, PLAYER["id"])["level"], 1)
        self.assertGreater(pets.pending_level_ups(pets.get_pet(CHAT, PLAYER["id"])), 1)

        down = await self._grant(THIRD, user_id=PLAYER["id"], resource="arena_xp", amount=-50_000)
        self.assertEqual(down.status, 200, await down.text())
        after = pets.get_pet(CHAT, PLAYER["id"])
        self.assertEqual((after["level"], after["xp"]), (1, 0))

        # Server XP is the /top figure.
        given = await self._grant(THIRD, user_id=PLAYER["id"], resource="server_xp", amount=10_000)
        self.assertEqual(given.status, 200, await given.text())
        self.assertEqual(stats.xp_breakdown(CHAT, str(PLAYER["id"]))["granted"], 10_000)
        taken = await self._grant(THIRD, user_id=PLAYER["id"], resource="server_xp", amount=-10_000)
        self.assertEqual(taken.status, 200, await taken.text())
        self.assertEqual(stats.xp_breakdown(CHAT, str(PLAYER["id"]))["granted"], 0)

    async def test_neither_xp_can_be_driven_below_zero(self):
        """Asking to remove more than exists removes what exists, and says so."""
        self._tame(PLAYER)
        await self._grant(THIRD, user_id=PLAYER["id"], resource="arena_xp", amount=1_000)
        await self._grant(THIRD, user_id=PLAYER["id"], resource="arena_xp", amount=-999_999_999)
        floored = pets.get_pet(CHAT, PLAYER["id"])
        self.assertEqual((floored["level"], floored["xp"]), (1, 0))

        await self._grant(THIRD, user_id=PLAYER["id"], resource="server_xp", amount=500)
        answer = await self._grant(
            THIRD, user_id=PLAYER["id"], resource="server_xp", amount=-999_999_999)
        self.assertEqual(answer.status, 200, await answer.text())
        breakdown = stats.xp_breakdown(CHAT, str(PLAYER["id"]))
        self.assertGreaterEqual(breakdown["total"], 0)
        self.assertEqual(breakdown["granted"], -breakdown["earned"])

    async def test_only_xp_may_be_negative(self):
        """A wallet has no un-grant path, so a minus there would look like it worked and
        do nothing. Refused outright instead."""
        self._tame(PLAYER)
        for resource in ("gold", "rubies", "farm_tickets", "dungeon_tickets"):
            with self.subTest(resource=resource):
                answer = await self._grant(
                    THIRD, user_id=PLAYER["id"], resource=resource, amount=-100)
                self.assertEqual(answer.status, 400, await answer.text())
                self.assertEqual((await answer.json())["error"], "BAD_AMOUNT")

    async def test_the_panel_offers_a_direction_only_for_xp(self):
        page = await (await self.client.get(pets_web.ROUTE_PREFIX)).text()
        self.assertIn('const SIGNED_RESOURCES = new Set(["server_xp", "arena_xp"]);', page)
        self.assertIn('data-grantsign="-1"', page)
        # Leaving an XP row must disarm the minus, or it follows you to a wallet.
        self.assertIn("if (!signed) grantSign = 1;", page)
        self.assertIn("amount: amount * grantSign", page)

    async def test_granting_the_same_resource_twice_adds_up_rather_than_replaying(self):
        """Unlike a listener event, an admin pressing the button twice means twice -- the
        reason key is fresh every call specifically so this is never swallowed as a replay."""
        self._tame(PLAYER)
        first = await self._grant(THIRD, user_id=PLAYER["id"], resource="rubies", amount=100)
        second = await self._grant(THIRD, user_id=PLAYER["id"], resource="rubies", amount=100)
        self.assertEqual(first.status, 200, await first.text())
        self.assertEqual(second.status, 200, await second.text())
        self.assertEqual(pets.ruby_balance(CHAT, PLAYER["id"]), 200)

    async def test_grant_rejects_bad_resource_amount_and_unknown_player(self):
        self._tame(PLAYER)
        bad_resource = await self._grant(THIRD, user_id=PLAYER["id"], resource="diamonds", amount=10)
        self.assertEqual(bad_resource.status, 400, await bad_resource.text())
        self.assertEqual((await bad_resource.json())["error"], "BAD_RESOURCE")

        zero_amount = await self._grant(THIRD, user_id=PLAYER["id"], resource="gold", amount=0)
        self.assertEqual(zero_amount.status, 400, await zero_amount.text())
        self.assertEqual((await zero_amount.json())["error"], "BAD_AMOUNT")

        too_many_tickets = await self._grant(
            THIRD, user_id=PLAYER["id"], resource="farm_tickets", amount=51,
        )
        self.assertEqual(too_many_tickets.status, 400, await too_many_tickets.text())

        no_pet = await self._grant(THIRD, user_id=MODERATOR["id"], resource="gold", amount=10)
        self.assertEqual(no_pet.status, 404, await no_pet.text())
        self.assertEqual((await no_pet.json())["error"], "NOT_FOUND")

    # ---- birthdays ----------------------------------------------------------------------

    async def _birthday(self, user, **payload):
        response = await self.client.post(
            pets_web.ROUTE_PREFIX + "/api/birthday",
            json={"init_data": _init_data(user["id"]), **payload},
        )
        return response

    async def test_only_a_chat_admin_can_open_or_set_the_birthday(self):
        """The same narrow gate the money audit uses. Both routes ask for themselves --
        the menu entry the client draws is a convenience, never the permission."""
        self._tame(PLAYER)
        for user in (PLAYER, MODERATOR):
            with self.subTest(user=user["username"]):
                read = await self._get("/api/birthday", user)
                self.assertEqual(read.status, 403, await read.text())
                write = await self._birthday(user, user_id=PLAYER["id"])
                self.assertEqual(write.status, 403, await write.text())

        # THIRD is the economy admin in this fixture.
        read = await self._get("/api/birthday", THIRD)
        self.assertEqual(read.status, 200, await read.text())
        body = await read.json()
        self.assertIsNone(body["birthday"])
        self.assertIn(str(PLAYER["id"]), [row["user_id"] for row in body["candidates"]])

    async def test_a_greeting_pays_both_sides_dms_the_celebrant_and_only_lands_once(self):
        self._tame(PLAYER)
        self._tame(THIRD, name="Именинник")
        self.assertEqual((await self._birthday(THIRD, user_id=THIRD["id"])).status, 200)

        before_greeter = economy.balance(CHAT, str(PLAYER["id"]), RICH_XP)
        before_celebrant = economy.balance(CHAT, str(THIRD["id"]), RICH_XP)
        response = await self.client.post(
            pets_web.ROUTE_PREFIX + "/api/congratulate",
            json={"init_data": _init_data(PLAYER["id"])},
        )
        self.assertEqual(response.status, 200, await response.text())
        receipt = (await response.json())["receipt"]

        self.assertFalse(receipt["already"])
        self.assertEqual(
            economy.balance(CHAT, str(PLAYER["id"]), RICH_XP) - before_greeter, receipt["gold"],
        )
        self.assertEqual(
            economy.balance(CHAT, str(THIRD["id"]), RICH_XP) - before_celebrant,
            receipt["celebrant_gold"],
        )
        self.assertEqual(len(self.birthday_greetings), 1)
        celebrant, greeter_name, gold, _xp = self.birthday_greetings[0]
        self.assertEqual(celebrant, str(THIRD["id"]))
        self.assertEqual(greeter_name, PLAYER["first_name"])
        self.assertEqual(gold, receipt["celebrant_gold"])

        # A second press pays nothing more and sends no second message.
        repeat = await self.client.post(
            pets_web.ROUTE_PREFIX + "/api/congratulate",
            json={"init_data": _init_data(PLAYER["id"])},
        )
        self.assertTrue((await repeat.json())["receipt"]["already"])
        self.assertEqual(len(self.birthday_greetings), 1)

    async def test_a_closed_dm_does_not_fail_a_greeting_that_is_already_paid(self):
        """A bot cannot write to somebody who never opened its chat. The money is banked
        before the message is attempted, so losing it must not hand the greeter an error
        and an unspendable retry."""
        self._tame(PLAYER)
        self._tame(THIRD, name="Именинник")
        self.assertEqual((await self._birthday(THIRD, user_id=THIRD["id"])).status, 200)
        self.birthday_notify_raises = RuntimeError("Forbidden: bot can't initiate conversation")

        before = economy.balance(CHAT, str(THIRD["id"]), RICH_XP)
        response = await self.client.post(
            pets_web.ROUTE_PREFIX + "/api/congratulate",
            json={"init_data": _init_data(PLAYER["id"])},
        )
        self.assertEqual(response.status, 200, await response.text())
        receipt = (await response.json())["receipt"]
        self.assertFalse(receipt["already"])
        self.assertEqual(
            economy.balance(CHAT, str(THIRD["id"]), RICH_XP) - before, receipt["celebrant_gold"],
        )
        self.assertTrue(any("birthday DM" in line for line in self.logs), self.logs)

    async def test_the_celebrant_is_pinned_first_in_the_roster_and_is_not_attackable(self):
        self._tame(PLAYER)
        self._tame(MODERATOR, name="Обычный")
        self._tame(THIRD, name="Именинник")
        self.assertEqual((await self._birthday(THIRD, user_id=THIRD["id"])).status, 200)

        body = await (await self._get("/api/opponents", PLAYER)).json()
        self.assertEqual(body["opponents"][0]["user_id"], str(THIRD["id"]))
        self.assertTrue(body["opponents"][0]["birthday"])
        # Their card offers a greeting, so it must not also offer a fight.
        self.assertFalse(body["opponents"][0]["attackable"])
        self.assertEqual(body["birthday"]["user_id"], str(THIRD["id"]))
        self.assertFalse(body["birthday"]["is_me"])
        self.assertFalse(body["birthday"]["greeted"])

        celebrant_view = await (await self._get("/api/opponents", THIRD)).json()
        self.assertTrue(celebrant_view["birthday"]["is_me"])

    async def test_clearing_takes_the_celebration_out_of_the_roster(self):
        self._tame(PLAYER)
        self._tame(THIRD, name="Именинник")
        self.assertEqual((await self._birthday(THIRD, user_id=THIRD["id"])).status, 200)
        self.assertEqual((await self._birthday(THIRD, clear=True)).status, 200)

        body = await (await self._get("/api/opponents", PLAYER)).json()
        self.assertIsNone(body["birthday"])
        self.assertFalse(any(row.get("birthday") for row in body["opponents"]))
        refused = await self.client.post(
            pets_web.ROUTE_PREFIX + "/api/congratulate",
            json={"init_data": _init_data(PLAYER["id"])},
        )
        self.assertEqual(refused.status, 409, await refused.text())


if __name__ == "__main__":
    unittest.main()


class AttachContractTests(unittest.TestCase):
    """bot_listener calls pets_web.attach with a fixed set of keywords, and nothing before
    this checked that attach still accepts them.

    It caught nothing when a keyword was added to the CALL and lost from the DEFINITION:
    every unit test builds the app itself with its own arguments, the modules import fine
    because the mismatch only exists at call time, and the failure surfaced as the whole
    process dying on boot in production.
    """

    def _attach_kwargs(self) -> list[str]:
        source = inspect.getsource(bot_listener.run_bot_listener)
        block = source.split("pets_web.attach(")[1].split(")\n")[0]
        names = [
            line.split("=")[0].strip() for line in block.splitlines()
            if "=" in line and not line.strip().startswith("#")
        ]
        return [name for name in names if name.isidentifier()]

    def test_every_keyword_production_passes_is_one_attach_accepts(self):
        accepted = inspect.signature(pets_web.attach).parameters
        passed = self._attach_kwargs()
        self.assertTrue(passed, "could not read the real attach() call")
        for name in passed:
            with self.subTest(keyword=name):
                self.assertIn(name, accepted)

    def test_attach_builds_an_app_with_exactly_that_call(self):
        """The signature agreeing is not quite enough -- run it."""
        async def noop(*args, **kwargs):
            return None

        app = web.Application()
        pets_web.attach(
            app, SimpleNamespace(telegram_bot_token=BOT_TOKEN), CHAT,
            **{name: (lambda *a, **k: None) if name == "log" else noop
               for name in self._attach_kwargs()},
        )
        self.assertIn(pets_web.ROUTE_PREFIX + "/api/state",
                      [getattr(route.resource, "canonical", "") for route in app.router.routes()])


class PageScriptSyntaxTests(unittest.TestCase):
    """The page is one <script>. A single broken string literal in it does not degrade
    anything -- it stops the whole file parsing, and the Mini App opens as a blank screen.

    That shipped: removing the escalator button left `'</button>'</div></div>';` behind,
    and every route still answered 200 while the app was unusable. Nothing else in this
    suite reads the page as JavaScript, so nothing else could have caught it.
    """

    def _check(self, html: str, label: str):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not available to parse the page")
        body = re.findall(r"<script>(.*?)</script>", html, re.S)
        self.assertTrue(body, f"{label} has no inline script to check")
        for index, script in enumerate(body):
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                             encoding="utf-8") as handle:
                handle.write(script)
                path = handle.name
            try:
                result = subprocess.run([node, "--check", path],
                                        capture_output=True, text=True)
                self.assertEqual(
                    result.returncode, 0,
                    f"{label} script #{index} does not parse:\n{result.stderr}",
                )
            finally:
                os.unlink(path)

    def test_the_mini_app_page_is_valid_javascript(self):
        self._check(pets_web.PAGE_HTML.replace("__PREFIX__", "/pets"), "PAGE_HTML")

    def test_the_audit_page_is_valid_javascript(self):
        self._check(pets_web.AUDIT_HTML, "AUDIT_HTML")
