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


class PersonalPaintRuneReturnTests(unittest.TestCase):
    """A paint is somebody's painted miniature: no path may destroy the rune behind it."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)
        self.entry = "chat"
        self.user = "1"
        self._tame(self.user, "Painter")

    def _tame(self, user, name):
        economy.grant(self.entry, user, C.TAME_PRICE, "test")
        ok, message = pets.buy_cage(self.entry, user, 0)
        self.assertTrue(ok, message)
        ok, message = pets.tame(self.entry, user, 0, name, f"pet-photo-{user}", "Owner")
        self.assertTrue(ok, message)

    def _commons(self, slot, count):
        items = [
            item for item in C.ITEMS
            if item.rarity == "common" and item.slot == slot and not getattr(item, "cursed", False)
        ]
        self.assertGreaterEqual(len(items), count)
        return items[:count]

    def _set_bag(self, codes, equipped=None):
        data = pets._load(self.entry)
        record = data["pets"][self.user]
        record["inventory"] = list(codes)
        for slot in C.SLOT_KEYS:
            record["equipped"][slot] = None
        record["equipped"].update(equipped or {})
        pets._save(self.entry, data)

    def _painted(self, code, source="submission-1"):
        rune = pets.grant_personal_paint_rune(
            self.entry, self.user, "rune_paint_weapon", source, "submitted-photo-id",
        )["rune"]
        ok, message, _ = pets.apply_personal_paint_rune(self.entry, self.user, rune["id"], code)
        self.assertTrue(ok, message)
        return rune

    def _wallet_ids(self):
        return [row["id"] for row in pets.personal_paint_status(self.entry, self.user)["runes"]]

    def _paints(self):
        return pets.get_pet(self.entry, self.user).get("personal_enchantments", {})

    def _forge_recipe(self):
        return next(row for row in pets.forge_status(self.entry, self.user)["recipes"]
                    if row["slot"] == "weapon" and row["rarity"] == "common" and not row["cursed"])

    def test_forging_a_painted_items_last_copy_returns_the_same_rune(self):
        commons = self._commons("weapon", pets.FORGE_REQUIREMENTS["common"])
        self._set_bag([item.code for item in commons])
        painted = commons[0]
        rune = self._painted(painted.code)
        self.assertEqual(self._forge_recipe()["returned_paints"], [painted.code])

        ok, message, result_code = pets.reforge_items(self.entry, self.user, "common", "weapon")
        self.assertTrue(ok, message)
        self.assertIn("вернулся руной", message)
        self.assertEqual(self._wallet_ids(), [rune["id"]])
        wallet_row = pets.personal_paint_status(self.entry, self.user)["runes"][0]
        self.assertEqual(wallet_row["photo_file_id"], "submitted-photo-id")
        self.assertEqual(wallet_row["target"], "weapon")
        self.assertNotIn(painted.code, self._paints())

        # Usable again, on the very weapon the forge just made.
        ok, message, _ = pets.apply_personal_paint_rune(self.entry, self.user, rune["id"], result_code)
        self.assertTrue(ok, message)
        self.assertEqual(self._wallet_ids(), [])

    def test_melting_a_spare_leaves_the_paint_on_the_worn_twin(self):
        commons = self._commons("weapon", pets.FORGE_REQUIREMENTS["common"])
        painted = commons[0]
        # One worn copy (reserved from the forge) and one spare that WILL be melted.
        self._set_bag([painted.code, painted.code, *[item.code for item in commons[1:]]],
                      {"weapon": painted.code})
        rune = self._painted(painted.code)
        recipe = self._forge_recipe()
        self.assertIn(painted.code, recipe["ingredients"])
        self.assertEqual(recipe["returned_paints"], [])

        ok, message, _code = pets.reforge_items(self.entry, self.user, "common", "weapon")
        self.assertTrue(ok, message)
        self.assertNotIn("вернулся руной", message)
        self.assertEqual(self._paints()[painted.code]["rune_id"], rune["id"])
        self.assertEqual(self._wallet_ids(), [])

    def test_paint_can_be_taken_off_and_put_back_without_ever_duplicating(self):
        item = self._commons("weapon", 1)[0]
        self._set_bag([item.code], {"weapon": item.code})
        base = pets.effective_stats(self.entry, self.user)
        rune = self._painted(item.code)
        self.assertNotEqual(pets.effective_stats(self.entry, self.user), base)

        ok, message = pets.remove_personal_paint_rune(self.entry, self.user, item.code)
        self.assertTrue(ok, message)
        self.assertEqual(self._wallet_ids(), [rune["id"]])
        self.assertNotIn(item.code, self._paints())
        self.assertEqual(pets.effective_stats(self.entry, self.user), base)

        ok, _message = pets.remove_personal_paint_rune(self.entry, self.user, item.code)
        self.assertFalse(ok)
        self.assertEqual(self._wallet_ids(), [rune["id"]])

        ok, message, _ = pets.apply_personal_paint_rune(self.entry, self.user, rune["id"], item.code)
        self.assertTrue(ok, message)
        self.assertEqual(self._wallet_ids(), [])
        self.assertEqual(self._paints()[item.code]["source"], "quest-personal-paint:submission-1")

    def _gift(self, code):
        if pets.get_pet(self.entry, "2") is None:
            self._tame("2", "Friend")
        data = pets._load(self.entry)
        data["pets"][self.user]["level"] = C.GIFT_MIN_PET_LEVEL
        data["pets"][self.user].pop("gift_last_at", None)
        pets._save(self.entry, data)
        ok, message = pets.gift_item(self.entry, self.user, "2", code)
        self.assertTrue(ok, message)
        return message

    def test_selling_the_last_painted_copy_returns_the_rune_but_gifting_spends_it(self):
        sold, gifted = self._commons("weapon", 2)
        self._set_bag([sold.code, gifted.code])
        first = self._painted(sold.code, "submission-1")
        second = self._painted(gifted.code, "submission-2")

        ok, message, _gold = pets.sell_item(self.entry, self.user, sold.code)
        self.assertTrue(ok, message)
        self.assertIn("вернулся руной", message)
        self.assertEqual(self._wallet_ids(), [first["id"]])

        self.assertTrue(pets.gift_spends_paint(self.entry, self.user, gifted.code))
        message = self._gift(gifted.code)
        self.assertNotIn("руной", message)
        self.assertEqual(self._wallet_ids(), [first["id"]])
        self.assertNotIn(gifted.code, self._paints())
        # Neither painter nor recipient gets it, and the ledger knows it was a gift.
        self.assertEqual(pets.get_pet(self.entry, "2").get("personal_enchantments"), {})
        self.assertEqual(pets.personal_paint_status(self.entry, "2")["runes"], [])
        data = pets._load(self.entry)
        ledger = data["personal_paint_rune_sources"]["quest-personal-paint:submission-2"]
        self.assertEqual((ledger["rune_id"], ledger["spent"]), (second["id"], "gift"))
        self.assertEqual(data["gift_history"][-1]["personal_paint_rune_id"], second["id"])
        # So the startup repair leaves it spent.
        self.assertEqual(pets.restore_lost_personal_paint_runes([self.entry]),
                         {"restored": [], "gifted": [], "unrecoverable": 0})
        self.assertEqual(self._wallet_ids(), [first["id"]])

    def test_gifting_a_spare_keeps_the_paint_on_the_remaining_copy(self):
        item = self._commons("weapon", 1)[0]
        self._set_bag([item.code, item.code])
        rune = self._painted(item.code)
        self.assertFalse(pets.gift_spends_paint(self.entry, self.user, item.code))
        self._gift(item.code)
        self.assertEqual(self._paints()[item.code]["rune_id"], rune["id"])

    def test_the_loader_returns_a_paint_whose_item_left_by_any_other_path(self):
        item = self._commons("weapon", 1)[0]
        self._set_bag([item.code])
        rune = self._painted(item.code)
        # What the forge used to leave on disk: paint still keyed, item gone.
        data = pets._load(self.entry)
        data["pets"][self.user]["inventory"] = []
        pets._save(self.entry, data)

        self.assertEqual(self._wallet_ids(), [rune["id"]])
        self.assertNotIn(item.code, self._paints())
        # And a rune can never be both spendable and on an item.
        data = pets._load(self.entry)
        data["pets"][self.user]["inventory"] = [item.code]
        data["pets"][self.user]["personal_enchantments"] = {item.code: {
            "target": "weapon", "rune_id": rune["id"], "photo_file_id": "submitted-photo-id",
        }}
        pets._save(self.entry, data)
        self.assertEqual(self._wallet_ids(), [])
        self.assertIn(item.code, self._paints())

    def _lose_historically(self, item, source):
        """Reproduce the old loss: the loader dropped the paint and the drop was saved."""
        rune = self._painted(item.code, source)
        data = pets._load(self.entry)
        data["pets"][self.user]["personal_enchantments"].pop(item.code)
        data["pets"][self.user]["inventory"].remove(item.code)
        pets._save(self.entry, data)
        return rune

    def _count_store_io(self):
        counts = {"pets_load": 0, "pets_save": 0, "quests_load": 0}

        def counted(key, function):
            def wrapper(*args, **kwargs):
                counts[key] += 1
                return function(*args, **kwargs)
            return wrapper

        for target, key, function in (("pets._load", "pets_load", pets._load),
                                      ("pets._save", "pets_save", pets._save),
                                      ("quests._load", "quests_load", quests._load)):
            patcher = patch(target, counted(key, function))
            patcher.start()
            self.addCleanup(patcher.stop)
        return counts

    def test_startup_repair_rebuilds_lost_runes_from_the_ledger_and_quest_photo(self):
        items = self._commons("weapon", 3)
        self._set_bag([item.code for item in items])
        # Three losses in one store, to prove the I/O is per store, not per rune.
        lost = [self._lose_historically(item, f"submission-{index}")
                for index, item in enumerate(items, 1)]
        self.assertEqual(self._wallet_ids(), [])
        board = quests._load(self.entry)
        board["submissions"] = [
            {"id": f"submission-{index}", "user_id": self.user, "code": "rune_paint_weapon",
             "status": "accepted", "photo_file_id": f"photo-{index}"}
            for index in (1, 2, 3)
        ]
        quests._save(self.entry, board)

        counts = self._count_store_io()
        result = pets.restore_lost_personal_paint_runes([self.entry])
        self.assertEqual(sorted(row["rune_id"] for row in result["restored"]),
                         sorted(rune["id"] for rune in lost))
        self.assertEqual(result["unrecoverable"], 0)
        self.assertEqual(counts, {"pets_load": 2, "pets_save": 1, "quests_load": 1})

        wallet = {row["id"]: row for row in pets.personal_paint_status(self.entry, self.user)["runes"]}
        self.assertEqual(wallet[lost[0]["id"]]["photo_file_id"], "photo-1")
        self.assertEqual(wallet[lost[2]["id"]]["target"], "weapon")

        counts.update({"pets_load": 0, "pets_save": 0, "quests_load": 0})
        again = pets.restore_lost_personal_paint_runes([self.entry])
        self.assertEqual(again, {"restored": [], "gifted": [], "unrecoverable": 0})
        self.assertEqual(counts, {"pets_load": 1, "pets_save": 0, "quests_load": 0})
        self.assertEqual(len(pets.personal_paint_status(self.entry, self.user)["runes"]), 3)

    def test_startup_repair_falls_back_to_replay_snapshots_and_never_invents_a_photo(self):
        first, second = self._commons("weapon", 2)
        self._set_bag([first.code, second.code])
        seen = self._lose_historically(first, "submission-1")
        self._lose_historically(second, "submission-2")
        pets._fight_log_path(self.entry).write_text(pets.json.dumps([{
            "combat_snapshot": {"records": {self.user: {"personal_enchantments": {first.code: {
                "target": "weapon", "rune_id": seen["id"], "quest_code": "rune_paint_weapon",
                "photo_file_id": "replay-photo",
            }}}}},
        }]), encoding="utf-8")

        result = pets.restore_lost_personal_paint_runes([self.entry])
        self.assertEqual([row["rune_id"] for row in result["restored"]], [seen["id"]])
        self.assertEqual(result["unrecoverable"], 1)
        wallet = pets.personal_paint_status(self.entry, self.user)["runes"]
        self.assertEqual([(row["id"], row["photo_file_id"]) for row in wallet],
                         [(seen["id"], "replay-photo")])

    def _replay_sightings(self, rows):
        """An arena log whose snapshots saw these (code, rune, applied_at) paints."""
        pets._fight_log_path(self.entry).write_text(pets.json.dumps([{
            "combat_snapshot": {"records": {self.user: {"personal_enchantments": {code: {
                "target": "weapon", "rune_id": rune_id, "quest_code": "rune_paint_weapon",
                "photo_file_id": "replay-photo", "applied_at": applied_at,
            }}}}},
        } for code, rune_id, applied_at in rows]), encoding="utf-8")

    def _historic_gift(self, code, ts):
        data = pets._load(self.entry)
        data["gift_history"].append({"ts": ts, "giver_id": self.user, "receiver_id": "2",
                                     "item_code": code})
        pets._save(self.entry, data)

    def test_startup_repair_leaves_a_rune_that_left_with_an_old_gift_spent(self):
        melted, gifted, earlier = self._commons("weapon", 3)
        self._set_bag([melted.code, gifted.code, earlier.code])
        forge_loss = self._lose_historically(melted, "submission-1")
        gift_loss = self._lose_historically(gifted, "submission-2")
        self._replay_sightings([
            (melted.code, forge_loss["id"], "2026-09-01T10:00:00+03:00"),
            (gifted.code, gift_loss["id"], "2026-09-01T10:00:00+03:00"),
        ])
        # Before the ledger learned about gifts: only the gift log remembers. A gift of
        # the other design, and one made before the paint went on, must not count.
        self._historic_gift(gifted.code, "2026-09-05T12:00:00+03:00")
        self._historic_gift(melted.code, "2026-08-20T12:00:00+03:00")
        self._historic_gift(earlier.code, "2026-09-06T12:00:00+03:00")

        result = pets.restore_lost_personal_paint_runes([self.entry])
        self.assertEqual([row["rune_id"] for row in result["restored"]], [forge_loss["id"]])
        self.assertEqual([row["rune_id"] for row in result["gifted"]], [gift_loss["id"]])
        self.assertEqual(result["restored"][0]["username"], None)
        self.assertEqual(self._wallet_ids(), [forge_loss["id"]])

    def test_without_a_replay_any_later_gift_the_rune_fits_keeps_it_spent(self):
        weapon = self._commons("weapon", 1)[0]
        boots = self._commons("boots", 1)[0]
        self._set_bag([weapon.code])
        lost = self._lose_historically(weapon, "submission-1")
        board = quests._load(self.entry)
        board["submissions"] = [{
            "id": "submission-1", "user_id": self.user, "code": "rune_paint_weapon",
            "status": "accepted", "photo_file_id": "photo-1",
            "reviewed_at": "2026-09-01T10:00:00+03:00",
        }]
        quests._save(self.entry, board)

        self._historic_gift(boots.code, "2026-09-05T12:00:00+03:00")
        self.assertEqual([row["rune_id"] for row in
                          pets.restore_lost_personal_paint_runes([self.entry])["restored"]],
                         [lost["id"]])

        # A gift of boots cannot have carried a weapon rune. Lose it again, and this time
        # the owner also gave away a weapon after the quest: now it could have.
        data = pets._load(self.entry)
        data["personal_paint_runes"][self.user] = []
        data["gift_history"].append({"ts": "2026-09-05T12:00:00+03:00", "giver_id": self.user,
                                     "receiver_id": "2", "item_code": self._commons("weapon", 2)[1].code})
        pets._save(self.entry, data)
        result = pets.restore_lost_personal_paint_runes([self.entry])
        self.assertEqual(result["restored"], [])
        self.assertEqual([row["rune_id"] for row in result["gifted"]], [lost["id"]])

    def test_web_and_telegram_offer_the_removal(self):
        item = self._commons("weapon", 1)[0]
        self._set_bag([item.code], {"weapon": item.code})
        rune = self._painted(item.code)
        self.assertIn("remove_personal_paint", pets_web._ACTIONS)
        # Parity with applying one: neither is a mid-dungeon action.
        self.assertNotIn("remove_personal_paint", pets_web._ALLOWED_IN_DUNGEON)
        self.assertNotIn("apply_personal_paint", pets_web._ALLOWED_IN_DUNGEON)
        self.assertEqual(pets.personal_paint_status(self.entry, self.user)["applied"][0]["name"],
                         item.name)

        _text, keyboard = pets_ui.personal_paint_runes_view(self.entry, self.user)
        remove = next(
            button for row in keyboard["inline_keyboard"] for button in row
            if pets_ui.parse_callback(button["callback_data"])[1] == "paintremove"
        )
        self.assertEqual(pets_ui.parse_callback(remove["callback_data"])[2], rune["id"])
        long_owner = pets_ui.callback_data("1234567890123", "paintremove", rune["id"])
        self.assertLessEqual(len(long_owner.encode()), pets_ui.MAX_CALLBACK_BYTES)

        ok, message = pets_web._ACTIONS["remove_personal_paint"](
            self.entry, self.user, 0, {"code": item.code},
        )
        self.assertTrue(ok, message)
        self.assertEqual(self._wallet_ids(), [rune["id"]])
