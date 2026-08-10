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
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pets
import pets_config as C
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
        ]
        for patcher in self._patchers:
            patcher.start()

        cfg = SimpleNamespace(telegram_bot_token=BOT_TOKEN)

        async def is_admin(user):
            return False

        async def is_member(user):
            return user.get("id") != NONMEMBER["id"]

        async def resolve_player(user):
            if user.get("id") == UNTRACKED["id"]:
                return None, None
            return None, RICH_XP

        # Built exactly as production builds it: v1's app, with the pet game attached the
        # way bot_listener's _attach_extra really attaches it.
        app = vote_web.create_app(
            cfg, CHAT, is_admin, log=lambda *_: None,
            attach=lambda a: pets_web.attach(
                a, cfg, CHAT, is_member=is_member, resolve_player=resolve_player,
                log=lambda *_: None,
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

    # ---- authentication -----------------------------------------------------------------

    async def test_every_route_refuses_an_unsigned_caller(self):
        """The whole game rides on economy.balance, which needs a player resolved against
        the chat's own statistics -- so nothing here, reads included, may run before
        initData has been verified."""
        for method, path in (
            ("get", "/api/state"), ("get", "/api/opponents"), ("get", "/api/shop"),
            ("get", "/api/leaderboard"), ("get", "/api/history"), ("get", "/api/collection"),
            ("get", "/api/updates"), ("post", "/api/action"), ("post", "/api/attack"),
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


if __name__ == "__main__":
    unittest.main()
