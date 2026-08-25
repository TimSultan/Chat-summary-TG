"""Contracts for owner-bound rewards from specialist rune-paint quests."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import economy
import pets
import pets_combat
import pets_config as C
import pets_ui
import pets_web
import pets_scroll_catalog as scrolls
import quests
import stats


class PersonalPaintRuneTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)
        self.entry = "chat"
        self.user = "1"
        economy.grant(self.entry, self.user, C.TAME_PRICE, "test")
        ok, message = pets.buy_cage(self.entry, self.user, 0)
        self.assertTrue(ok, message)
        ok, message = pets.tame(self.entry, self.user, 0, "Painter", "pet-photo", "Owner")
        self.assertTrue(ok, message)

    def _own_item(self, slot):
        item = next(row for row in C.ITEMS if row.slot == slot and any(value > 0 for value in row.bonuses.values()))
        data = pets._load(self.entry)
        record = data["pets"][self.user]
        record["inventory"].append(item.code)
        record["equipped"][slot] = item.code
        pets._save(self.entry, data)
        return item

    def _grant(self, quest_code, source="submission-1"):
        receipt = pets.grant_personal_paint_rune(
            self.entry, self.user, quest_code, source, "submitted-photo-id",
        )
        self.assertTrue(receipt["granted"], receipt)
        return receipt["rune"]

    def test_matching_item_rune_is_one_use_and_boosts_only_positive_item_stats(self):
        item = self._own_item("weapon")
        before = pets.effective_stats(self.entry, self.user)
        rune = self._grant("rune_paint_weapon")

        ok, message, applied = pets.apply_personal_paint_rune(self.entry, self.user, rune["id"], item.code)
        self.assertTrue(ok, message)
        self.assertEqual(applied["target"], "weapon")
        self.assertEqual(applied["photo_file_id"], "submitted-photo-id")
        self.assertEqual(pets.personal_paint_status(self.entry, self.user)["runes"], [])
        replay_after_consumption = pets.grant_personal_paint_rune(
            self.entry, self.user, "rune_paint_weapon", "submission-1", "submitted-photo-id",
        )
        self.assertFalse(replay_after_consumption["granted"])
        self.assertEqual(replay_after_consumption["reason"], "already_granted")

        after = pets.effective_stats(self.entry, self.user)
        for stat, amount in item.bonuses.items():
            if stat == "armor" or amount <= 0:
                continue
            self.assertEqual(after[stat] - before[stat], round(amount * 1.3) - amount)

        second = self._grant("rune_paint_weapon", "submission-2")
        ok, message, _ = pets.apply_personal_paint_rune(self.entry, self.user, second["id"], item.code)
        self.assertFalse(ok)
        self.assertIn("уже", message)
        self.assertEqual(len(pets.personal_paint_status(self.entry, self.user)["runes"]), 1)

    def test_elemental_then_personal_gives_two_slots_and_third_enchantment_is_refused(self):
        item = self._own_item("weapon")
        data = pets._load(self.entry)
        record = data["pets"][self.user]
        record["runes"] = {"fire": 1, "frost": 1}
        data["rubies"] = {self.user: pets.RUNE_ENCHANT_RUBY_COST * 2}
        pets._save(self.entry, data)

        ok, message = pets.enchant_weapon(self.entry, self.user, item.code, "fire")
        self.assertTrue(ok, message)
        self.assertIn("1/2", message)

        paint = self._grant("rune_paint_weapon")
        ok, message, _receipt = pets.apply_personal_paint_rune(
            self.entry, self.user, paint["id"], item.code,
        )
        self.assertTrue(ok, message)
        self.assertIn("2/2", message)

        pet = pets.get_pet(self.entry, self.user)
        self.assertEqual(pet["weapon_enchantments"][item.code], "fire")
        self.assertIn(item.code, pet["personal_enchantments"])
        payload = pets_web._item_payload(item, "/pets", pet)
        self.assertEqual(payload["enchantment"], "fire")
        self.assertIsNotNone(payload["personal_paint"])

        rubies_before = pets.ruby_balance(self.entry, self.user)
        frost_before = pets.rune_status(self.entry, self.user)["runes"]["frost"]
        ok, message = pets.enchant_weapon(self.entry, self.user, item.code, "frost")
        self.assertFalse(ok)
        self.assertIn("2/2", message)
        self.assertEqual(pets.ruby_balance(self.entry, self.user), rubies_before)
        self.assertEqual(pets.rune_status(self.entry, self.user)["runes"]["frost"], frost_before)
        self.assertEqual(pets.get_pet(self.entry, self.user)["weapon_enchantments"][item.code], "fire")

    def test_personal_then_elemental_is_allowed_and_ui_disables_a_second_element(self):
        item = self._own_item("weapon")
        paint = self._grant("rune_paint_weapon")
        self.assertTrue(pets.apply_personal_paint_rune(
            self.entry, self.user, paint["id"], item.code,
        )[0])
        data = pets._load(self.entry)
        data["pets"][self.user]["runes"] = {"water": 1}
        data["rubies"] = {self.user: pets.RUNE_ENCHANT_RUBY_COST}
        pets._save(self.entry, data)

        ok, message = pets.enchant_weapon(self.entry, self.user, item.code, "water")
        self.assertTrue(ok, message)
        self.assertIn("2/2", message)

        text, keyboard = pets_ui.enchant_weapon_view(self.entry, self.user, item.code)
        self.assertIn("2/2", text)
        rune_actions = [
            pets_ui.parse_callback(button["callback_data"])[1]
            for row in keyboard["inline_keyboard"][:-1] for button in row
        ]
        self.assertTrue(rune_actions)
        self.assertEqual(set(rune_actions), {"noop"})

    def test_type_owner_and_source_guards_cannot_be_bypassed(self):
        item = self._own_item("weapon")
        shield_rune = self._grant("rune_paint_shield")
        ok, _message, _ = pets.apply_personal_paint_rune(self.entry, self.user, shield_rune["id"], item.code)
        self.assertFalse(ok)
        self.assertEqual(len(pets.personal_paint_status(self.entry, self.user)["runes"]), 1)

        replay = pets.grant_personal_paint_rune(
            self.entry, self.user, "rune_paint_shield", "submission-1", "another-photo",
        )
        self.assertFalse(replay["granted"])
        self.assertEqual(replay["reason"], "already_granted")
        missing = pets.grant_personal_paint_rune(
            self.entry, self.user, "rune_paint_weapon", "submission-3", None,
        )
        self.assertFalse(missing["granted"])
        self.assertEqual(missing["reason"], "missing_submission_image")

    def test_telegram_rune_picker_lists_safe_targets_with_callback_sized_buttons(self):
        item = self._own_item("weapon")
        rune = self._grant("rune_paint_weapon")
        text, keyboard = pets_ui.personal_paint_runes_view(self.entry, self.user)
        self.assertIn("+30%", text)
        rune_button = next(
            button for row in keyboard["inline_keyboard"] for button in row
            if pets_ui.parse_callback(button["callback_data"])[1] == "paintrune"
        )
        self.assertLessEqual(len(rune_button["callback_data"].encode()), pets_ui.MAX_CALLBACK_BYTES)

        text, keyboard = pets_ui.personal_paint_targets_view(self.entry, self.user, rune["id"])
        self.assertIn(item.name, text + " ".join(
            button["text"] for row in keyboard["inline_keyboard"] for button in row
        ))
        apply_button = next(
            button for row in keyboard["inline_keyboard"] for button in row
            if pets_ui.parse_callback(button["callback_data"])[1] == "paintapply"
        )
        self.assertLessEqual(len(apply_button["callback_data"].encode()), pets_ui.MAX_CALLBACK_BYTES)

    def test_painted_item_art_survives_the_immutable_fight_replay_snapshot(self):
        item = self._own_item("weapon")
        rune = self._grant("rune_paint_weapon")
        self.assertTrue(pets.apply_personal_paint_rune(
            self.entry, self.user, rune["id"], item.code,
        )[0])
        record = pets.get_pet(self.entry, self.user)
        stored = pets_web._fight_record_snapshot(record, "/pets")
        self.assertEqual(stored["items"][0]["art"], f"/pets/img/personal-paint/{rune['id']}.jpg")

        mine = pets_combat.Fighter("1", "Painter", 10, 10, 10, 10, 0)
        foe = pets_combat.Fighter("2", "Foe", 10, 10, 10, 10, 0)
        playback = pets_web._playback_side_payload(mine, foe, "1", "/pets", stored)
        painted = next(row for row in playback["items"] if row["code"] == item.code)
        self.assertEqual(painted["art"], f"/pets/img/personal-paint/{rune['id']}.jpg")

    def test_scroll_rune_scales_power_but_not_turns_or_blind_probability(self):
        code = "scroll_headlong_rush"
        data = pets._load(self.entry)
        record = data["pets"][self.user]
        record["owned_scrolls"].append(code)
        pets._save(self.entry, data)
        rune = self._grant("rune_paint_scroll")
        ok, message, _ = pets.apply_personal_paint_rune(self.entry, self.user, rune["id"], code)
        self.assertTrue(ok, message)
        self.assertIn(code, pets.personal_enchanted_scrolls(self.entry, self.user))
        web_scroll = next(
            row for row in pets_web._skills_payload(pets.get_pet(self.entry, self.user), "/pets")["regular"]
            if row["code"] == code
        )
        base_damage = next(row["amount"] for row in scrolls.scroll(code)["effects"] if row["op"] == "damage")
        painted_damage = next(row["amount"] for row in web_scroll["effects"] if row["op"] == "damage")
        self.assertEqual(painted_damage, base_damage * 1.3)
        self.assertIn(f"{round(base_damage * 1.3 * 100)}%", " ".join(web_scroll["effects_text"]))

        # A combat snapshot retains the fact that this scroll is painted, so a replay
        # cannot quietly lose the 30% bonus after an inventory change.
        fighter = pets_combat.Fighter(
            "a", "A", 30, 30, 1, 1, 0, skills=(code, None, None, None),
            personal_enchanted_scrolls=(code,),
        )
        restored = pets_combat.restore(pets_combat.snapshot(fighter))
        self.assertEqual(restored.personal_enchanted_scrolls, (code,))

        # The specialist scroll has a damage multiplier: observe a deterministic skill
        # use and ensure the painted version lands more, without granting extra turns.
        target = pets_combat.Fighter("b", "B", 1, 300, 1, 1, 0)
        plain = pets_combat.Fighter("a", "A", 30, 30, 1, 1, 0, skills=(code, None, None, None))
        for seed in range(200):
            normal = pets_combat.simulate(plain, target, seed=seed, max_actions=1)
            painted = pets_combat.simulate(fighter, target, seed=seed, max_actions=1)
            normal_skill = next((row for row in normal.rounds if row.event == f"skill_{code}"), None)
            painted_skill = next((row for row in painted.rounds if row.event == f"skill_{code}"), None)
            if normal_skill and painted_skill:
                self.assertGreater(painted_skill.damage, normal_skill.damage)
                break
        else:
            self.fail("could not sample the deterministic scroll action")

    def test_vial_rune_boosts_healing_power_without_double_boosting_item_stats(self):
        item = C.find_item("amulet_first_aid_heart")
        self.assertIsNotNone(item)
        data = pets._load(self.entry)
        record = data["pets"][self.user]
        record["inventory"].append(item.code)
        record["equipped"][item.slot] = item.code
        pets._save(self.entry, data)
        before = pets.effective_stats(self.entry, self.user)

        rune = self._grant("rune_paint_vial")
        ok, message, _ = pets.apply_personal_paint_rune(self.entry, self.user, rune["id"], item.code)
        self.assertTrue(ok, message)

        effect = next(row for row in pets.equipped_combat_effects(self.entry, self.user)
                      if row["code"] == "medkit")
        self.assertEqual(effect["value"], item.effect["value"] * 1.3)
        self.assertEqual(pets.effective_stats(self.entry, self.user), before)

    def test_specialist_quest_payout_is_personal_rune_not_generic_rune_or_scroll(self):
        paid = quests._pay(self.entry, {
            "user_id": self.user, "code": "rune_paint_weapon", "kind": "rune",
            "difficulty": 4, "gold": 0, "xp": 0, "tickets": 0, "drop_chance": 0,
            "photo_file_id": "submitted-photo-id",
        }, "specialist-submission")
        self.assertTrue(paid["personal_paint_rune"]["granted"])
        self.assertEqual(paid["rune"]["granted"], 0)
        self.assertIsNone(paid["scroll"])
