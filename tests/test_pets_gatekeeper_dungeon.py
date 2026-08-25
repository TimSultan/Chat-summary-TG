"""Persistence, settlement and screens around the pure Gatekeeper state machine."""

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
import pets_gatekeeper
import pets_ui
import pets_web


CHAT = "gatekeeper-chat"
USER = 7710
RICH_XP = 10 ** 9
FLOOR = 10


class GatekeeperDungeonTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self._patch = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        self._patch.start()
        self.addCleanup(self._temporary.cleanup)
        self.addCleanup(self._patch.stop)
        self.assertTrue(pets.buy_cage(CHAT, USER, RICH_XP)[0])
        self.assertTrue(pets.tame(CHAT, USER, RICH_XP, "Клинок", "file", "Хозяин")[0])
        self._stand_on_gatekeeper()

    def _stand_on_gatekeeper(self):
        data = pets._load(CHAT)
        record = data["pets"][str(USER)]
        record["level"] = 20
        for key in pets.C.STAT_KEYS:
            record["stats"][key] = 300
        record["dungeon_run"] = {
            "run_id": "gatekeeper-run", "kills": 0, "floor": FLOOR,
            "hp": 99_999, "max_hp": 99_999, "cleared": [],
            "haul": pets._new_haul(), "floor_haul": pets._new_haul(),
        }
        pets._save(CHAT, data)

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

        _ok, _note, state = pets.gatekeeper_start(CHAT, USER)

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
            _ok, _note, state = pets.gatekeeper_action(CHAT, USER, moves[0])
            live = pets.get_pet(CHAT, USER).get("dungeon_run")
            if not live or not state:
                break
            self.assertLessEqual(live["hp"], live["max_hp"])
            self.assertEqual(live["hp"], state["hero_hp"])
            self.assertEqual(live["max_hp"], state["hero_max_hp"])

    def test_floor_ten_keeps_the_requested_card_and_starts_the_manual_fight(self):
        row = dungeon.encounter(FLOOR, 0)
        self.assertTrue(pets.is_gatekeeper(row))
        self.assertEqual(row["name"], "Стальной привратник")
        self.assertEqual(
            row["hint"],
            "В его забрале нет цели, но старый замок на груди всё ещё отсчитывает чужие шаги.",
        )
        text, markup = pets_ui.dungeon_view(CHAT, USER, RICH_XP)
        callbacks = [
            pets_ui.parse_callback(button["callback_data"])[1]
            for buttons in markup["inline_keyboard"] for button in buttons
        ]
        self.assertIn("Стальной привратник", text)
        self.assertIn("gatekeeperstart", callbacks)

    def test_old_auto_fight_endpoint_cannot_skip_the_interactive_boss(self):
        ok, message, receipt = pets.dungeon_fight(CHAT, USER, 0)
        self.assertFalse(ok)
        self.assertIsNone(receipt)
        self.assertIn("вручную", message)
        self.assertEqual(pets.get_pet(CHAT, USER)["dungeon_run"]["cleared"], [])

    def test_each_turn_is_persisted_and_reopening_does_not_restart_it(self):
        ok, _note, first = pets.gatekeeper_start(CHAT, USER)
        self.assertTrue(ok)
        self.assertEqual(
            pets.get_pet(CHAT, USER)["dungeon_run"]["gatekeeper"]["hero"]["guard"],
            .10,
            "a bare defensive stance must not inherit a free 40% shield",
        )
        move = first["actions"][0]["code"]
        ok, _note, after = pets.gatekeeper_action(CHAT, USER, move)
        self.assertTrue(ok)
        stored = pets.get_pet(CHAT, USER)["dungeon_run"]["gatekeeper"]
        self.assertEqual(json.loads(json.dumps(stored, ensure_ascii=False)), stored)
        self.assertEqual(pets.dungeon_status(CHAT, USER)["gatekeeper"]["turn"], after["turn"])

        ok, _note, reopened = pets.gatekeeper_start(CHAT, USER)
        self.assertTrue(ok)
        self.assertEqual(reopened["turn"], after["turn"])
        self.assertEqual(reopened["hero_hp"], after["hero_hp"])
        self.assertEqual(reopened["boss_hp"], after["boss_hp"])

    def test_telegram_screen_shows_locks_steps_prediction_and_server_actions(self):
        _ok, _note, state = pets.gatekeeper_start(CHAT, USER)
        text, markup = pets_ui.gatekeeper_view(CHAT, USER, RICH_XP, state)
        callbacks = [
            pets_ui.parse_callback(button["callback_data"])
            for buttons in markup["inline_keyboard"] for button in buttons
        ]
        self.assertIn("🔐 Замки:", text)
        self.assertIn("👣 Шаги:", text)
        self.assertIn("Привратник", text)
        self.assertTrue(callbacks)
        self.assertTrue(all(row[1] == "gatekeeperact" for row in callbacks))

    def test_victory_uses_the_ordinary_boss_reward_and_clears_the_floor(self):
        pets.gatekeeper_start(CHAT, USER)
        live = pets.get_pet(CHAT, USER)["dungeon_run"]["gatekeeper"]
        finished = dict(live)
        finished.update({"status": pets_gatekeeper.VICTORY, "boss_hp": 0})

        with patch.object(pets_gatekeeper, "take", return_value=finished):
            ok, note, state = pets.gatekeeper_action(CHAT, USER, live["current_boss_action"])

        self.assertTrue(ok, note)
        self.assertTrue(state["won"])
        run = pets.get_pet(CHAT, USER)["dungeon_run"]
        self.assertIn(0, run["cleared"])
        self.assertNotIn("gatekeeper", run)
        self.assertEqual(pets.get_pet(CHAT, USER)["gatekeeper_record"]["wins"], 1)
        self.assertIn("pet_dungeon_boss_win", [row["reason"] for row in economy._load(CHAT)["log"]])

    def test_defeat_ends_the_run_through_the_normal_dungeon_path(self):
        pets.gatekeeper_start(CHAT, USER)
        live = pets.get_pet(CHAT, USER)["dungeon_run"]["gatekeeper"]
        finished = dict(live)
        finished.update({"status": pets_gatekeeper.DEFEAT, "hero_hp": 0})

        with patch.object(pets_gatekeeper, "take", return_value=finished):
            ok, _note, state = pets.gatekeeper_action(CHAT, USER, live["current_boss_action"])

        self.assertFalse(ok)
        self.assertFalse(state["won"])
        record = pets.get_pet(CHAT, USER)
        self.assertIsNone(record["dungeon_run"])
        self.assertFalse(record["last_dungeon_haul"]["won"])

    def test_mini_app_contains_live_gatekeeper_state_and_both_controls(self):
        source = pets_web.PAGE_HTML
        self.assertIn("S.dungeon.gatekeeper = data.gatekeeper", source)
        self.assertIn('gatekeeper: "gatekeeper_start"', source)
        self.assertIn('gatekeepermove: "gatekeeper_action"', source)
        self.assertIn('data-dungeon="gatekeepermove"', source)
        self.assertIn("gatekeeperFight(dungeon, dungeon.gatekeeper)", source)

    def _committed_fight(self):
        """Feed the machine a pattern until it commits, and hand back the live state."""
        self.assertTrue(pets.gatekeeper_start(CHAT, USER)[0])
        for index in range(24):
            state = pets.gatekeeper_state(CHAT, USER)
            if state.get("committed"):
                return state
            legal = {row["code"] for row in state["actions"]}
            wanted = (pets_gatekeeper.WEAPON, pets_gatekeeper.DEFENCE)[index % 2]
            pets.gatekeeper_action(
                CHAT, USER, wanted if wanted in legal else sorted(legal)[0],
            )
        self.fail("the machine never committed to a prediction")

    def test_both_clients_say_what_it_expects_and_how_sure_it_is(self):
        """Hard is allowed; hidden is not.

        The prediction is deliberately back on screen -- see the pets_gatekeeper module
        docstring -- because the model underneath it changed. A player who loses has to be
        able to say "I missed the warning", never "the game decided to punish me", and the
        warning only exists if both clients actually print it.
        """
        state = self._committed_fight()
        self.assertIn(state["prediction"], pets_gatekeeper.CATEGORIES)
        self.assertGreaterEqual(state["confidence"], pets_gatekeeper.CONFIDENCE_COMMITTED)

        text, _keys = pets_ui.gatekeeper_view(CHAT, USER, 0, state=state)
        self.assertIn("Ожидает", text)
        self.assertIn(state["prediction_label"], text)
        self.assertIn(f"{round(state['confidence'] * 100)}%", text)

        source = pets_web.PAGE_HTML
        self.assertIn("gatekeeperForecast(fight)", source)
        self.assertIn("fight.prediction_label", source)
        self.assertIn("fight.covered_label", source)

    def test_the_second_counter_is_named_on_both_clients_when_it_is_up(self):
        """Two dangerous answers means two visible warnings, or it is a coin flip."""
        state = self._committed_fight()
        # Break the prediction once so the chest starts opening and the cover comes up.
        blocked = {state["prediction"], state["covered"]}
        escape = next(row["code"] for row in state["actions"]
                      if row["code"] not in blocked and row["code"] != pets_gatekeeper.FALSE_STEP)
        pets.gatekeeper_action(CHAT, USER, escape)
        state = self._committed_fight()
        if not state.get("covered"):
            self.skipTest("the machine has not put up a second counter on this turn")

        text, _keys = pets_ui.gatekeeper_view(CHAT, USER, 0, state=state)
        self.assertIn(state["covered_label"], text)
        self.assertIn("перекрыто", text)


if __name__ == "__main__":
    unittest.main()
