"""The Phoenix where it meets the dungeon: persistence, refusal, and the payout.

`pets_phoenix` is a pure state machine with its own tests. What is pinned here is the
half it deliberately does not have -- reading the pet, keeping the fight on the run
between two button presses, and settling it through the same reward path every other
kill uses. A boss that minted its own gold would be a second economy nobody could audit.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import economy
import pets
import pets_dungeon as dungeon
import pets_phoenix

CHAT = "phoenix-chat"
USER = 7700
RICH_XP = 10 ** 9
PHOENIX_FLOOR = 5


class PhoenixDungeonTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self._patch = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        self._patch.start()
        self.addCleanup(self._temporary.cleanup)
        self.addCleanup(self._patch.stop)
        self.assertTrue(pets.buy_cage(CHAT, USER, RICH_XP)[0])
        self.assertTrue(pets.tame(CHAT, USER, RICH_XP, "Пеструшка", "file", "Хозяин")[0])
        self._stand_on_the_phoenix()

    def _stand_on_the_phoenix(self, strength=300):
        data = pets._load(CHAT)
        record = data["pets"][str(USER)]
        record["level"] = 20
        for key in pets.C.STAT_KEYS:
            record["stats"][key] = strength
        record["dungeon_run"] = {
            "run_id": "phoenix-run", "kills": 0, "floor": PHOENIX_FLOOR,
            "hp": 99_999, "max_hp": 99_999, "cleared": [],
        }
        pets._save(CHAT, data)

    # ------------------------------------------------------------------ the refusal
    def test_the_auto_battler_refuses_the_phoenix_outright(self):
        """Refused rather than simulated, so no stale button can resolve it in one press.

        The whole rework is that this fight is played a turn at a time; a client that
        still sends the old action must be told no, not quietly handed a winner.
        """
        row = dungeon.encounter(PHOENIX_FLOOR, 0)
        self.assertTrue(pets.is_phoenix(row))

        ok, message, receipt = pets.dungeon_fight(CHAT, USER, 0)

        self.assertFalse(ok)
        self.assertIsNone(receipt)
        self.assertIn("вручную", message)
        # And nothing about the run moved: no kill, no cleared index, no loot.
        run = pets.get_pet(CHAT, USER)["dungeon_run"]
        self.assertEqual(run["cleared"], [])
        self.assertEqual(int(run.get("kills", 0) or 0), 0)

    def test_every_other_boss_is_still_simulated(self):
        """One boss changed how it is fought. The rest of the dungeon did not."""
        data = pets._load(CHAT)
        data["pets"][str(USER)]["dungeon_run"]["floor"] = 20
        pets._save(CHAT, data)
        self.assertFalse(pets.is_phoenix(dungeon.encounter(20, 0)))

        _ok, message, receipt = pets.dungeon_fight(CHAT, USER, 0)

        # Whether this hero WINS is beside the point and would make the test about the
        # balance of floor 20. What matters is that the fight was resolved at all rather
        # than refused: a receipt exists either way, and the refusal message does not.
        self.assertIsNotNone(receipt)
        self.assertNotIn("вручную", message)

    # ------------------------------------------------------------- the fight itself
    def test_a_fight_survives_being_closed_and_reopened(self):
        """A screen closed mid-boss must not hand back the mistakes already made.

        The state lives on the run, so reopening is a redraw. Restarting here would make
        every dangerous telegraph free: read it, quit, come back knowing the answer.
        """
        ok, _note, first = pets.phoenix_start(CHAT, USER)
        self.assertTrue(ok)
        self.assertIsNotNone(first)

        action = first["actions"][0]["code"]
        ok, _note, after = pets.phoenix_action(CHAT, USER, action)
        self.assertTrue(ok)
        live_status = pets.dungeon_status(CHAT, USER)["phoenix"]
        self.assertEqual(live_status["boss_hp"], after["boss_hp"])
        self.assertEqual(live_status["hero_hp"], after["hero_hp"])

        ok, _note, reopened = pets.phoenix_start(CHAT, USER)
        self.assertTrue(ok)
        self.assertEqual(reopened["hero_hp"], after["hero_hp"])
        self.assertEqual(reopened["boss_hp"], after["boss_hp"])
        self.assertEqual(reopened["phase"], after["phase"])

    def test_the_stored_fight_is_json_safe(self):
        """It is persisted into the save file, so anything json cannot carry is a crash
        on the next load rather than here."""
        pets.phoenix_start(CHAT, USER)
        run = pets.get_pet(CHAT, USER)["dungeon_run"]
        stored = run["phoenix"]

        self.assertEqual(json.loads(json.dumps(stored, ensure_ascii=False)), stored)

    def test_a_button_that_is_not_on_offer_is_refused(self):
        _ok, _note, state = pets.phoenix_start(CHAT, USER)
        offered = {row["code"] for row in state["actions"]}
        missing = next(code for code in pets_phoenix.ACTIONS if code not in offered)

        ok, message, unchanged = pets.phoenix_action(CHAT, USER, missing)

        self.assertFalse(ok)
        self.assertTrue(message)
        self.assertEqual(unchanged["hero_hp"], state["hero_hp"])

    def test_the_fight_needs_a_run_and_the_right_floor(self):
        data = pets._load(CHAT)
        data["pets"][str(USER)]["dungeon_run"]["floor"] = 10
        pets._save(CHAT, data)
        ok, message, state = pets.phoenix_start(CHAT, USER)
        self.assertFalse(ok)
        self.assertIsNone(state)
        self.assertIn("Феникс", message)

    # ------------------------------------------------------------------- the screen
    def test_the_last_answer_is_read_before_the_next_telegraph(self):
        """Order is the lesson. What a choice cost has to arrive BEFORE the next move.

        Under the telegraph it was being read after the decision it should have informed:
        the player answers, scrolls past the new telegraph to find out what the last one
        did, and then has to scroll back. Above it, the two blocks are read in the order
        they happened.
        """
        import pets_ui

        pets.phoenix_start(CHAT, USER)
        for _ in range(12):
            state = pets.phoenix_state(CHAT, USER)
            if state is None or state.get("over"):
                break
            if state.get("grade") == "bad" and state.get("telegraph"):
                text = pets_ui.phoenix_view(CHAT, USER, 0)[0]
                first_log = text.index(state["log"][0])
                self.assertLess(first_log, text.index(state["telegraph"]))
                # And a mistake is marked, because the numbers cannot say it: losing 2,000
                # health reads the same whether the block was mistimed or the move was the
                # one that punishes blocking.
                self.assertIn("💢", text)
                return
            offered = [row["code"] for row in state["actions"]]
            pets.phoenix_action(
                CHAT, USER,
                pets_phoenix.ATTACK if pets_phoenix.ATTACK in offered else offered[0],
            )
        self.fail("нужен хотя бы один плохой ответ, чтобы проверить порядок")

    # ------------------------------------------------------------------ the payout
    def test_a_win_pays_through_the_ordinary_boss_path_and_clears_the_floor(self):
        """Same ledger reason, same haul, same cleared index as any other boss.

        The Phoenix changed how it is FOUGHT, not what it is worth -- and the income
        audit reads the reason string, which is the only place the boss flag survives.
        """
        state = self._play_to_the_end(win=True)
        self.assertTrue(state["won"], state)

        reasons = [row["reason"] for row in economy._load(CHAT)["log"]]
        self.assertIn("pet_dungeon_boss_win", reasons)
        run = pets.get_pet(CHAT, USER)["dungeon_run"]
        self.assertIn(0, run["cleared"])
        self.assertNotIn("phoenix", run)

    def test_winning_the_fight_leaves_the_run_carrying_a_totem(self):
        """The prize for the bird that comes back, wired to the fight rather than the shop.

        Asserted through a whole played fight because the grant is asked for by the
        settle, not decided inside the shared boss payout -- the Gatekeeper settles
        through that same payout and must not come away with one.
        """
        state = self._play_to_the_end(win=True)
        self.assertTrue(state["won"], state)

        run = pets.get_pet(CHAT, USER)["dungeon_run"]
        self.assertTrue(run["phoenix_totem"])
        self.assertFalse(run.get("phoenix_totem_used"))

    def test_the_fight_spends_the_run_bar_and_never_a_pool_of_its_own(self):
        """One creature, one health bar, on both halves of the screen.

        `pets_combat.derive` sizes max_hp against the opponent, so the pool this boss
        would otherwise get is not the pool the run was opened with -- for a strong pet it
        is the LARGER of the two. Writing that back left the run bar pinned at 100% for
        the whole fight however hard the boss hit, which is what a player sees as the
        health bar being broken.
        """
        data = pets._load(CHAT)
        run = data["pets"][str(USER)]["dungeon_run"]
        # A pool the run actually owns, and a hero standing in it at a third of it.
        hero = pets._dungeon_fighter(data["pets"][str(USER)], str(USER))
        run["max_hp"] = round(pets.pets_combat.derive(hero, hero)["max_hp"])
        run["hp"] = round(run["max_hp"] * 0.30)
        pets._save(CHAT, data)
        wounded = run["hp"]

        _ok, _note, state = pets.phoenix_start(CHAT, USER)

        # Same ceiling as the run, and standing where the run left them -- not healed up.
        self.assertEqual(state["hero_max_hp"], run["max_hp"])
        self.assertEqual(state["hero_hp"], wounded)

        # And every turn writes back on that same scale, so the bar can be read.
        for _ in range(4):
            if state.get("over"):
                break
            moves = [row["code"] for row in state.get("actions") or [] if row.get("code")]
            if not moves:
                break
            _ok, _note, state = pets.phoenix_action(CHAT, USER, moves[0])
            live = pets.get_pet(CHAT, USER).get("dungeon_run")
            if not live or not state:
                break
            self.assertLessEqual(live["hp"], live["max_hp"])
            self.assertEqual(live["hp"], state["hero_hp"])
            self.assertEqual(live["max_hp"], state["hero_max_hp"])

    def test_a_loss_ends_the_run_the_way_every_other_defeat_does(self):
        state = self._play_to_the_end(win=False)
        self.assertFalse(state["won"], state)

        record = pets.get_pet(CHAT, USER)
        self.assertIsNone(record.get("dungeon_run"))
        self.assertFalse(record["last_dungeon_haul"]["won"])

    def _play_to_the_end(self, win: bool):
        """Walk a fight to its end, either playing well or pressing ⚔️ forever."""
        _ok, _note, state = pets.phoenix_start(CHAT, USER)
        for _ in range(400):
            if state.get("over"):
                return state
            offered = [row["code"] for row in state["actions"]]
            if not offered:
                break
            if win:
                code = self._best(state, offered)
            else:
                code = pets_phoenix.ATTACK if pets_phoenix.ATTACK in offered else offered[0]
            _ok, _note, state = pets.phoenix_action(CHAT, USER, code)
            if state is None:
                break
        return state or {"won": False, "over": True}

    @staticmethod
    def _best(state, offered):
        """The answer a player who has learned the boss would give.

        Chosen by trying each button against the pure engine, which is a harness
        privilege: the player never sees a hint, and the telegraph never carries one.
        """
        stored = pets.get_pet(CHAT, USER)["dungeon_run"]["phoenix"]
        best, best_score = offered[0], None
        for code in offered:
            try:
                after = pets_phoenix.take(dict(stored), code, seed=99)
            except ValueError:
                continue
            score = (after.get("hero_hp", 0) - stored.get("hero_hp", 0)) * 3 \
                + (stored.get("boss_hp", 0) - after.get("boss_hp", 0)) \
                - after.get("burn", 0) * 40
            if best_score is None or score > best_score:
                best, best_score = code, score
        return best


if __name__ == "__main__":
    unittest.main()
