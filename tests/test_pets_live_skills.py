"""Contracts for the four live scroll slots and the fifth, shield equipment slot."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import economy
import pets
import pets_combat as combat
import pets_config as C
import pets_scroll_catalog as scrolls


def _fighter(key: str, *, loadout=scrolls.DEFAULT_LOADOUT, shield=None):
    return combat.Fighter(
        key=key, name=key.upper(), strength=20, health=20, agility=20, luck=20,
        armor=5, skills=loadout, shield=shield,
    )


def _loadout_containing(code: str) -> tuple[str, str, str, str]:
    spell = scrolls.scroll(code)
    if spell["ultimate"]:
        return (*scrolls.DEFAULT_LOADOUT[:3], code)
    ordinary = [code]
    ordinary.extend(row for row in scrolls.DEFAULT_LOADOUT[:3] if row != code)
    return (*ordinary[:3], scrolls.DEFAULT_LOADOUT[3])


class LiveCombatTableTests(unittest.TestCase):
    def test_catalogue_has_three_regular_slots_one_ultimate_and_ten_live_shields(self):
        self.assertEqual(len(scrolls.REGULAR_SCROLLS), 30)
        self.assertEqual(len(scrolls.ULTIMATE_SCROLLS), 10)
        self.assertEqual(len(scrolls.SHIELDS), 10)
        self.assertEqual(len(C.items_for_slot("shield")), 10)

        shop = C.items_for_slot("shield", "shop")
        drops = [item for item in C.items_for_slot("shield") if item.source == "drop"]
        self.assertEqual(len(shop), 3)
        self.assertEqual(len(drops), 7)
        self.assertEqual(sum(item.rarity == "legendary" for item in drops), 3)
        self.assertTrue(all(item.price > 0 and item.drop_weight == 0 for item in shop))
        self.assertTrue(all(item.resale_price > 0 and item.drop_weight > 0 for item in drops))
        self.assertTrue(all(item.effect and item.effect.get("defend_effects") is not None
                            for item in C.items_for_slot("shield")))

    def test_live_strength_hp_bonus_does_not_rewrite_classic_or_historic_fights(self):
        classic = _fighter("classic", loadout=())
        skilled = _fighter("skilled")
        opponent = _fighter("opponent", loadout=())
        self.assertEqual(
            combat.derive(classic, opponent)["max_hp"],
            C.BASE_HP + classic.health * C.HP_PER_POINT,
        )
        self.assertEqual(
            combat.derive(skilled, opponent)["max_hp"],
            C.BASE_HP + skilled.health * C.HP_PER_POINT
            + skilled.strength * C.HP_PER_STRENGTH_WITH_SKILLS,
        )
        old_snapshot = combat.snapshot(classic)
        old_snapshot.pop("skills")
        old_snapshot.pop("shield")
        self.assertEqual(combat.restore(old_snapshot), classic)

    def test_every_scroll_can_resolve_in_live_combat_and_replays_are_deterministic(self):
        opponent = _fighter("b")
        for spell in scrolls.SCROLLS:
            fighter = _fighter("a", loadout=_loadout_containing(spell["code"]))
            observed = None
            for seed in range(4):
                result = combat.simulate(fighter, opponent, seed=seed)
                self.assertEqual(result, combat.simulate(fighter, opponent, seed=seed))
                self.assertTrue(all(row.attacker_hp >= 0 and row.defender_hp >= 0
                                    for row in result.rounds))
                if any(row.event == f"skill_{spell['code']}" for row in result.rounds):
                    observed = result
                    break
            self.assertIsNotNone(observed, spell["code"])
            self.assertLessEqual(
                sum(
                    row.event == f"skill_{spell['code']}" and row.attacker == fighter.key
                    for row in observed.rounds
                ),
                1,
                spell["code"],
            )

    def test_defend_uses_the_equipped_shields_data_driven_hook(self):
        mirror = scrolls.shield("shield_mirror")
        fighter = _fighter("a", shield=mirror)
        result = combat.simulate(fighter, _fighter("b"), seed=1)
        self.assertTrue(any(row.event == "defend" and row.attacker == "a"
                            for row in result.rounds))
        self.assertTrue(any(row.event == "skill_reflect" and row.attacker == "a"
                            for row in result.rounds))
        self.assertLessEqual(
            sum(row.event == "skill_ultimate_starfall" and row.attacker == "a"
                for row in result.rounds),
            1,
        )


class LiveLoadoutStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self.temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.temporary.cleanup)
        economy.grant("chat", "1", C.CAGE_PRICE + C.TAME_PRICE, "test")
        self.assertTrue(pets.buy_cage("chat", "1", 0)[0])
        self.assertTrue(pets.tame("chat", "1", 0, "Hero", "photo", "Owner")[0])

    def test_old_pet_migrates_to_four_skills_and_an_empty_shield_slot(self):
        data = pets._load("chat")
        record = data["pets"]["1"]
        record.pop("skill_slots")
        record["equipped"].pop("shield")
        pets._save("chat", data)

        migrated = pets.get_pet("chat", "1")
        self.assertEqual(tuple(migrated["skill_slots"]), scrolls.DEFAULT_LOADOUT)
        self.assertIn("shield", migrated["equipped"])
        self.assertIsNone(migrated["equipped"]["shield"])

    def test_slots_reject_wrong_types_and_persist_a_valid_selection(self):
        regular = scrolls.REGULAR_SCROLLS[-1]["code"]
        ultimate = scrolls.ULTIMATE_SCROLLS[-1]["code"]
        data = pets._load("chat")
        data["pets"]["1"]["owned_scrolls"].extend([regular, ultimate])
        pets._save("chat", data)
        self.assertFalse(pets.set_skill_slot("chat", "1", 1, ultimate)[0])
        self.assertFalse(pets.set_skill_slot("chat", "1", 4, regular)[0])
        self.assertTrue(pets.set_skill_slot("chat", "1", 2, regular)[0])
        self.assertTrue(pets.set_skill_slot("chat", "1", 4, ultimate)[0])
        self.assertEqual(pets.skill_loadout("chat", "1")[1], regular)
        self.assertEqual(pets.skill_loadout("chat", "1")[3], ultimate)

    def test_equipped_shield_reaches_the_live_fighter_snapshot(self):
        shield = C.items_for_slot("shield", "shop")[0]
        data = pets._load("chat")
        data["pets"]["1"]["inventory"].append(shield.code)
        pets._save("chat", data)
        self.assertTrue(pets.equip("chat", "1", shield.code)[0])

        snapshot = pets.combat_shield("chat", "1")
        self.assertEqual(snapshot["code"], shield.code)
        self.assertEqual(snapshot["name"], shield.name)
        self.assertEqual(snapshot["defend_effects"], shield.effect["defend_effects"])

    def test_scroll_rewards_are_idempotent_and_survive_until_a_painter_tames(self):
        # Painting belongs to a person, not their current pet ownership: a rare unlock
        # cannot disappear merely because it arrived before the cage purchase.
        first = pets.grant_scroll_reward(
            "chat", "2", source="paint:501", kind="paint", chance=1.0, pity_after=20,
        )
        self.assertTrue(first["granted"])
        replay = pets.grant_scroll_reward(
            "chat", "2", source="paint:501", kind="paint", chance=0.0, pity_after=20,
        )
        self.assertEqual(replay, first)

        economy.grant("chat", "2", C.CAGE_PRICE + C.TAME_PRICE, "test")
        self.assertTrue(pets.buy_cage("chat", "2", 0)[0])
        self.assertTrue(pets.tame("chat", "2", 0, "Painter", "photo", "Owner")[0])
        self.assertIn(first["code"], pets.owned_scrolls("chat", "2"))

    def test_unowned_scrolls_cannot_be_equipped(self):
        unknown = next(code for code in (row["code"] for row in scrolls.REGULAR_SCROLLS)
                       if code not in pets.owned_scrolls("chat", "1"))
        ok, message = pets.set_skill_slot("chat", "1", 1, unknown)
        self.assertFalse(ok)
        self.assertIn("ещё не открыт", message)

    def test_scroll_pity_forces_a_unique_unlock_and_resets(self):
        first = pets.grant_scroll_reward(
            "chat", "2", source="paint:1", kind="paint", chance=0.0, pity_after=3,
        )
        second = pets.grant_scroll_reward(
            "chat", "2", source="paint:2", kind="paint", chance=0.0, pity_after=3,
        )
        third = pets.grant_scroll_reward(
            "chat", "2", source="paint:3", kind="paint", chance=0.0, pity_after=3,
        )
        self.assertFalse(first["granted"])
        self.assertFalse(second["granted"])
        self.assertTrue(third["granted"])
        self.assertTrue(third["forced"])

        fourth = pets.grant_scroll_reward(
            "chat", "2", source="paint:4", kind="paint", chance=0.0, pity_after=3,
        )
        self.assertFalse(fourth["granted"])
        self.assertEqual(fourth["pity"], 1)
        fifth = pets.grant_scroll_reward(
            "chat", "2", source="paint:5", kind="paint", chance=1.0, pity_after=3,
        )
        self.assertTrue(fifth["granted"])
        self.assertNotEqual(fifth["code"], third["code"])


if __name__ == "__main__":
    unittest.main()
