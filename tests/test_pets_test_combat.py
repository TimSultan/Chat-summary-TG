import unittest

import pets_scroll_catalog as catalog
import pets_test_combat as combat


FIGHTER = {
    "name": "Герой",
    "stats": {"strength": 20, "health": 20, "agility": 20, "luck": 20, "armor": 10},
}


class ScrollCatalogTests(unittest.TestCase):
    def test_catalogue_is_large_editable_and_split_for_four_slots(self):
        self.assertEqual(len(catalog.REGULAR_SCROLLS), 30)
        self.assertEqual(len(catalog.ULTIMATE_SCROLLS), 10)
        self.assertEqual(len(catalog.SHIELDS), 20)
        self.assertTrue(all(row["auto_weight"] == 1 for row in catalog.SCROLLS))
        self.assertEqual({row["element"] for row in catalog.SCROLLS}, set(catalog.ELEMENTS))
        self.assertTrue(all(row["element"] in catalog.ELEMENTS for row in catalog.SCROLLS))
        self.assertTrue(any(row["dodgeable"] is False for row in catalog.SCROLLS))
        self.assertTrue(any("NMM" in row["name"] for row in catalog.SCROLLS))
        self.assertTrue(any("Звездопад" in row["name"] for row in catalog.SCROLLS))

    def test_loadout_reserves_fourth_slot_for_an_ultimate(self):
        self.assertEqual(catalog.validate_loadout(catalog.SAMPLE_LOADOUT), catalog.SAMPLE_LOADOUT)
        with self.assertRaisesRegex(ValueError, "четвёртый"):
            catalog.validate_loadout((
                catalog.SAMPLE_LOADOUT[3], *catalog.SAMPLE_LOADOUT[1:3],
                catalog.SAMPLE_LOADOUT[0],
            ))

    def test_slots_may_be_empty_individually_and_all_at_once(self):
        """A creature fields what it has found; the rest of the slots simply stay open."""
        self.assertEqual(catalog.validate_loadout(catalog.EMPTY_LOADOUT), catalog.EMPTY_LOADOUT)
        # Short, missing and blank entries all normalise to the same four-slot shape.
        self.assertEqual(catalog.validate_loadout(("", None, "", None)), catalog.EMPTY_LOADOUT)
        one = catalog.SAMPLE_LOADOUT[0]
        self.assertEqual(catalog.validate_loadout((one, None, None, None)),
                         (one, None, None, None))
        self.assertEqual(catalog.equipped_codes((one, None, None, None)), (one,))
        # What an empty slot must not become is an excuse to break the other rules.
        with self.assertRaisesRegex(ValueError, "четыре слота"):
            catalog.validate_loadout((one, None))
        with self.assertRaisesRegex(ValueError, "два слота"):
            catalog.validate_loadout((one, one, None, None))


class TestBattleEngineTests(unittest.TestCase):
    def _battle(self, **kwargs):
        return combat.start_battle(FIGHTER, dict(FIGHTER, name="Соперник"), seed=7, **kwargs)

    def test_strength_adds_prototype_hp_without_touching_live_combat(self):
        state = self._battle()
        expected = (
            combat.TEST_BASE_HP + 20 * combat.TEST_HP_PER_HEALTH
            + 20 * combat.TEST_HP_PER_STRENGTH
        )
        self.assertEqual(state["fighters"]["player"]["max_hp"], expected)
        stronger = combat.start_battle(
            dict(FIGHTER, stats={**FIGHTER["stats"], "strength": 21}),
            dict(FIGHTER, name="Соперник"), seed=7,
        )
        self.assertEqual(
            stronger["fighters"]["player"]["max_hp"] - expected,
            combat.TEST_HP_PER_STRENGTH,
        )

    def test_take_turn_is_pure_and_defend_triggers_shield_effect(self):
        state = self._battle(player_shield="shield_paper_buckler")
        result = combat.take_turn(state, "player", "defend")
        self.assertEqual(state["fighters"]["player"]["barrier"], 0)
        self.assertGreater(result["fighters"]["player"]["barrier"], 0)
        self.assertEqual(result["fighters"]["player"]["guard"], combat.BASE_GUARD)
        self.assertEqual(result["actor"], "enemy")

    def test_dodgeable_spell_can_miss_and_undodgeable_spell_cannot(self):
        dodgeable = self._battle()
        dodgeable["fighters"]["enemy"]["statuses"]["dodge_next"] = True
        missed = combat.take_turn(dodgeable, "player", "skill_1")
        self.assertEqual(
            missed["fighters"]["enemy"]["hp"], missed["fighters"]["enemy"]["max_hp"]
        )

        loadout = (
            "scroll_chain_lightning", "scroll_healing_rain", "scroll_nmm_glint",
            "ultimate_starfall",
        )
        certain = self._battle(player_loadout=loadout)
        certain["fighters"]["enemy"]["statuses"]["dodge_next"] = True
        hit = combat.take_turn(certain, "player", "skill_1")
        self.assertLess(hit["fighters"]["enemy"]["hp"], hit["fighters"]["enemy"]["max_hp"])
        self.assertTrue(hit["fighters"]["enemy"]["statuses"].get("dodge_next"))

    def test_ultimate_is_removed_from_legal_actions_after_one_use(self):
        state = combat.take_turn(self._battle(), "player", "skill_4")
        self.assertTrue(state["fighters"]["player"]["ultimate_used"])
        # Give the turn back without relying on what the enemy randomly chooses.
        state = combat.take_turn(state, "enemy", "attack")
        self.assertNotIn("skill_4", combat.legal_actions(state, "player"))
        with self.assertRaisesRegex(ValueError, "недоступно"):
            combat.take_turn(state, "player", "skill_4")

    def test_regular_scroll_is_removed_from_legal_actions_after_one_use(self):
        state = combat.take_turn(self._battle(), "player", "skill_1")
        self.assertTrue(state["fighters"]["player"]["used_scrolls"])
        self.assertNotIn("skill_1", combat.legal_actions(state, "player"))
        state = combat.take_turn(state, "enemy", "attack")
        with self.assertRaisesRegex(ValueError, "недоступно"):
            combat.take_turn(state, "player", "skill_1")

    def test_healing_rain_has_a_real_turn_value(self):
        state = self._battle()
        player = state["fighters"]["player"]
        player["hp"] -= round(player["max_hp"] * .50)
        result = combat.take_turn(state, "player", "skill_2")
        healed = result["fighters"]["player"]["hp"] - player["hp"]
        self.assertGreaterEqual(healed, round(player["max_hp"] * .24))
        self.assertGreaterEqual(result["fighters"]["player"]["barrier"], round(player["max_hp"] * .10))

    def test_auto_mode_uses_legal_uniform_candidates_and_always_finishes(self):
        state = self._battle()
        self.assertEqual(
            combat.legal_actions(state),
            ["attack", "defend", "skill_1", "skill_2", "skill_3", "skill_4"],
        )
        result = combat.run_auto(state)
        self.assertTrue(result["finished"])
        self.assertLessEqual(result["turn"], combat.TEST_TURN_LIMIT)
        self.assertTrue(result["winner"] or result["draw"])

    def test_public_payload_hides_random_stream_and_exposes_four_slots(self):
        payload = combat.public_state(self._battle())
        self.assertNotIn("seed", payload)
        self.assertNotIn("roll", payload)
        self.assertTrue(payload["test_only"])
        self.assertEqual(len(payload["fighters"]["player"]["slots"]), 4)
        self.assertTrue(payload["fighters"]["player"]["slots"][3]["ultimate"])

    def test_every_catalogue_scroll_and_shield_can_resolve_a_turn(self):
        """The table is meant to be edited often; one malformed effect must not strand a fight."""
        fillers = [row["code"] for row in catalog.REGULAR_SCROLLS[:4]]
        for row in catalog.REGULAR_SCROLLS:
            regular = [row["code"]] + [code for code in fillers if code != row["code"]][:2]
            if len(regular) < 3:
                regular.extend(
                    code for code in (item["code"] for item in catalog.REGULAR_SCROLLS)
                    if code not in regular
                )
            state = self._battle(player_loadout=(*regular[:3], catalog.SAMPLE_LOADOUT[3]))
            resolved = combat.take_turn(state, "player", "skill_1")
            self.assertGreaterEqual(resolved["turn"], 2, row["code"])

        for row in catalog.ULTIMATE_SCROLLS:
            state = self._battle(player_loadout=(*catalog.SAMPLE_LOADOUT[:3], row["code"]))
            resolved = combat.take_turn(state, "player", "skill_4")
            self.assertTrue(resolved["fighters"]["player"]["ultimate_used"], row["code"])

        for row in catalog.SHIELDS:
            state = self._battle(player_shield=row["code"])
            resolved = combat.take_turn(state, "player", "defend")
            self.assertEqual(resolved["last_action"], "defend", row["code"])


if __name__ == "__main__":
    unittest.main()
