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
import json
import os
import sys
import tempfile
import time
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import economy
import pets
import pets_config as C
import quests
import pets_web
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

        cfg = SimpleNamespace(telegram_bot_token=BOT_TOKEN)

        # vote_web's admin gate. pets_web gets its own below -- the same callable in
        # production (_is_vote_admin), but named separately here so a test can prove the
        # quest routes ask for themselves rather than trusting a menu flag.
        async def is_admin(user):
            return user.get("id") == MODERATOR["id"]

        async def is_member(user):
            return user.get("id") != NONMEMBER["id"]

        async def resolve_player(user):
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

        # Built exactly as production builds it: v1's app, with the pet game attached the
        # way bot_listener's _attach_extra really attaches it.
        app = vote_web.create_app(
            cfg, CHAT, is_admin, log=lambda *_: None,
            attach=lambda a: pets_web.attach(
                a, cfg, CHAT, is_member=is_member, is_admin=is_admin,
                resolve_player=resolve_player,
                fetch_photo=fetch_photo, save_photo=save_photo,
                quest_feedback=quest_feedback,
                quest_completion=quest_completion,
                log=self.logs.append,
            ),
        )
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

    async def _action(self, user, action, **payload):
        """POST /api/action, asserting only the transport succeeded (HTTP 200) -- a
        refused action (ok: false) is still a 200, so callers check `ok` themselves."""
        response = await self.client.post(pets_web.ROUTE_PREFIX + "/api/action", json={
            "init_data": _init_data(user["id"]), "action": action, **payload,
        })
        self.assertEqual(response.status, 200, await response.text())
        return await response.json()

    async def _upload_portrait(self, user, data: bytes):
        """POST raw bytes to /api/portrait the way the page's canvas upload does -- the
        body IS the image, so initData travels in the header instead of the JSON payload
        every other mutation uses."""
        return await self.client.post(
            pets_web.ROUTE_PREFIX + "/api/portrait", data=data,
            headers={**self._auth(user), "Content-Type": "image/jpeg"},
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
            ("get", "/api/test-battle"),
            ("post", "/api/action"), ("post", "/api/attack"),
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

    async def test_state_shows_no_pet_before_a_cage_and_the_full_paperdoll_after_taming(self):
        """GET /api/state is the one shape every screen renders from. A player with
        nothing yet still needs a price to look at; a tamed player needs every panel --
        stats, gear (all four slots, empty ones included), bag, arena, farm -- in that
        same single call, because the whole point of the design is that no screen has to
        ask twice."""
        bare = await (await self._get("/api/state", PLAYER)).json()
        self.assertIsNone(bare["pet"])
        self.assertFalse(bare["has_cage"])
        self.assertEqual(bare["cage"]["price"], C.CAGE_PRICE)

        self._tame(PLAYER)
        full = await (await self._get("/api/state", PLAYER)).json()
        self.assertIsNotNone(full["pet"])
        self.assertTrue(full["has_cage"])
        self.assertIn("stats", full)
        self.assertEqual(len(full["equipment"]), 4)
        self.assertEqual({slot["slot"] for slot in full["equipment"]}, set(C.SLOT_KEYS))
        self.assertTrue(all(slot["item"] is None for slot in full["equipment"]))
        self.assertIn("bag", full)
        self.assertIn("arena", full)
        self.assertIn("farm", full)

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

    # ---- equipment --------------------------------------------------------------------

    async def test_equipping_and_unequipping_move_an_item_between_bag_and_slot(self):
        """The paperdoll and the bag are two views of the same inventory, not two separate
        stores -- equip must fill the slot and flip the bag card's `equipped` flag in the
        one response, and unequip must undo exactly that."""
        self._tame(PLAYER)
        item = C.items_for_slot("amulet", source="shop")[0]
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

    # ---- selling ------------------------------------------------------------------------

    async def test_selling_a_rare_item_needs_confirm_true(self):
        """valuable_item's rarity gate exists so a stray tap cannot vanish something hard
        to replace. pets.sell_item enforces the real one-time token, but the action
        wrapper is what decides whether the client is even allowed to ask for one --
        without `confirm: true` the sale must be refused outright, not merely queued."""
        self._tame(PLAYER)
        # Weapon-slot shop items are also gated by the daily rotation; an accessory sidesteps
        # that and is guaranteed to exist regardless of which catalogue modules are loaded.
        item = next(
            i for i in C.ITEMS if valuable_item(i) and i.source == "shop" and i.slot != "weapon"
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
        self.assertEqual(len(body["shields"]), 10)
        self.assertTrue(all(row["auto_weight"] == 1 for row in body["regular_scrolls"]))
        self.assertTrue(all(row["effects"] for row in body["regular_scrolls"]))
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
        self.assertIn('data-testaction="attack"', page)
        self.assertIn('data-testaction="defend"', page)
        self.assertIn('data-testmode="multiplayer"', page)
        self.assertIn("testSkill4", page)
        self.assertIn("Результаты, награды и счётчики не записываются", page)

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

    async def test_upload_is_gated_like_every_other_mutation(self):
        """A picture upload is still a mutation of the pet, so it inherits the same three
        gates the rest of the game enforces: something to attach the photo to, membership
        to be allowed to act at all, and a signed caller in the first place."""
        no_pet = await self._upload_portrait(PLAYER, _jpeg_bytes())
        self.assertEqual(no_pet.status, 409)
        self.assertEqual((await no_pet.json())["error"], "NO_PET")

        self._tame(PLAYER)
        non_member = await self._upload_portrait(NONMEMBER, _jpeg_bytes())
        self.assertEqual(non_member.status, 403)

        unsigned = await self.client.post(
            pets_web.ROUTE_PREFIX + "/api/portrait", data=_jpeg_bytes(),
            headers={"Content-Type": "image/jpeg"},
        )
        self.assertEqual(unsigned.status, 401)

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
        self.assertGreater(board["seconds_until_refresh"], 0)
        self.assertLessEqual(board["seconds_until_refresh"], 24 * 60 * 60)
        quest = board["quest"]
        for field in ("code", "hashtag", "title", "subject", "technique", "hint",
                      "tool", "difficulty", "reward"):
            self.assertIn(field, quest)
        # Underscores, not hyphens: Telegram ends a hashtag at the first character
        # outside letters/digits/underscore, so "#quest-nmm" was never one tag.
        self.assertEqual(quest["hashtag"], "#quest_" + quest["code"])
        self.assertNotIn("-", quest["code"])
        self.assertEqual(board["rerolls_left"], quests.REROLLS_PER_QUEST)
        # A quest board is not an admin surface, and the menu flag must never be the
        # thing that decides -- but it still has to be honest about which menu to draw.
        self.assertFalse(board["is_admin"])
        self.assertTrue((await (await self._get("/api/quests", MODERATOR)).json())["is_admin"])

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
        self.assertIn('data-questidea', page)
        self.assertIn('data-reviewideas', page)
        self.assertIn('data-questedit', page)
        self.assertIn('Причина отклонения обязательна.', page)
        # A verdict spends coins, so it is confirmed rather than fired on one tap.
        self.assertIn("confirmThen(\"Принять работу и начислить награду?\"", page)

    # ---- the farm ticket ----------------------------------------------------------------

    async def test_a_farm_ticket_ends_the_shift_in_a_minute_without_touching_the_payout(self):
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
        farm = answer["state"]["farm"]
        self.assertTrue(farm["running"])
        self.assertLessEqual(farm["seconds_left"], C.FARM_TICKET_SECONDS)
        self.assertEqual(farm["planned_hours"], 8)
        self.assertEqual(farm["tickets"], 0)
        self.assertFalse(farm["can_ticket"])
        self.assertEqual(farm["reward"], expected)

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

    async def test_a_replay_is_the_same_fight_blow_for_blow(self):
        """Not "a fight like that one" -- that one. The stored seed and the two stored
        fighters go back through the same pure simulate(), so every round, every flavour
        line and the verdict have to come back identical. If this ever drifts, the page is
        showing players a fight that never happened."""
        self._tame(PLAYER)
        self._tame(OPPONENT, name="Соперник")

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
        data["fights"][0]["combat_snapshot"] = None
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
        recorded = data["fights"][0]
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
        # Both the animated path and the skip button go through the same painter, so a
        # skipped fight is not a differently formatted one.
        self.assertEqual(page.count("function paintBlow("), 1)
        self.assertIn('+ paintBlow(round, mineName, theirName) + "</div>"', page)
        self.assertIn('+ paintBlow(r, mineName, theirName) + "</div>"', page)
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


if __name__ == "__main__":
    unittest.main()
