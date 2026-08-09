"""The four pet modules composed, end to end.

Each of pets.py, pets_combat.py and pets_flavor.py is tested on its own elsewhere. This
file exists because those three were built independently against a written interface, and
a contract that every side honours in isolation can still leave a seam: a view that asks
for a key the store never writes, a fight result whose fields the recorder does not read,
a reward dict the report renders as "None". So this walks one member through the entire
game -- cage, taming, training, gear, a real fight, the history it leaves -- and renders
every screen at every step, asserting only what an integration test can usefully assert:
that nothing raises, that money actually moves, and that no screen shows a placeholder.

The one thing it does NOT do is re-test balance or storage semantics. Those belong to the
unit tests, and duplicating them here would mean two files to update for one decision.
"""

import json
import random
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import economy
import pets
import pets_combat
import pets_config as C
import pets_ui
import stats

ENTRY = "chat"
ALICE = 111
BOB = 222
# Enough XP that economy.balance opens well above the whole catalogue, so a refusal in
# this file is always a real bug and never "the wallet happened to be empty".
RICH_XP = 400_000


class PetsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._dir = TemporaryDirectory()
        path = Path(self._dir.name)
        self._real_stats_dir = stats._stats_dir
        stats._stats_dir = lambda: path
        self.addCleanup(self._restore)

    def _restore(self):
        stats._stats_dir = self._real_stats_dir
        self._dir.cleanup()

    def _balance(self, user_id):
        return economy.balance(ENTRY, user_id, RICH_XP)

    def _found(self, user_id, name, owner):
        ok, note = pets.buy_cage(ENTRY, user_id, RICH_XP)
        self.assertTrue(ok, note)
        ok, note = pets.tame(ENTRY, user_id, RICH_XP, name, f"photo_{user_id}", owner)
        self.assertTrue(ok, note)

    def _render_every_screen(self, user_id):
        """Every view a player can reach, rendered. The assertions are deliberately weak --
        this is a "does it compose" check, and pinning wording here would make every copy
        edit break a test."""
        screens = [
            pets_ui.main_view(ENTRY, user_id, RICH_XP),
            pets_ui.cage_view(ENTRY, user_id, RICH_XP),
            pets_ui.train_view(ENTRY, user_id, RICH_XP),
            pets_ui.bag_view(ENTRY, user_id, RICH_XP),
            pets_ui.fight_view(ENTRY, user_id, RICH_XP),
            pets_ui.history_view(ENTRY, user_id),
            pets_ui.pet_view(ENTRY, user_id),
        ] + [
            pets_ui.slot_view(ENTRY, user_id, RICH_XP, slot) for slot in C.SLOT_KEYS
        ]
        for text, keyboard in screens:
            self.assertTrue(text.strip())
            # A view that leaked a Python None into the caption is the exact failure this
            # file is here to catch.
            self.assertNotIn("None", text)
            self.assertIn("inline_keyboard", keyboard)
            for row in keyboard["inline_keyboard"]:
                for button in row:
                    self.assertTrue(button.get("text"))
                    data = button.get("callback_data")
                    if data is not None:
                        # Telegram silently drops a button whose callback_data is over 64
                        # bytes, which would look like a dead button in production.
                        self.assertLessEqual(len(data.encode("utf-8")), pets_ui.MAX_CALLBACK_BYTES)
                        self.assertIsNotNone(pets_ui.parse_callback(data))
        return screens

    # ------------------------------------------------------------------ the walkthrough

    def test_a_member_can_play_the_whole_game(self):
        # Nothing yet: the menu must still render for somebody with no cage at all.
        self._render_every_screen(ALICE)
        self.assertIsNone(pets.get_pet(ENTRY, ALICE))

        opening = self._balance(ALICE)
        self._found(ALICE, "Кабанчик", "Alice")
        self.assertEqual(
            self._balance(ALICE), opening - C.CAGE_PRICE - C.TAME_PRICE,
            "founding a pet must debit exactly the cage plus the taming",
        )
        self._render_every_screen(ALICE)

        # Training: the wallet moves by the published price, not by something else.
        before = self._balance(ALICE)
        ok, note, spent = pets.upgrade_stat(ENTRY, ALICE, RICH_XP, "strength", times=10)
        self.assertTrue(ok, note)
        self.assertEqual(spent, C.total_stat_cost(11, 1))
        self.assertEqual(self._balance(ALICE), before - spent)
        self.assertEqual(pets.stat_level(ENTRY, ALICE, "strength"), 11)

        # Gear: buying, equipping, and the stat actually landing on the creature.
        bare = pets.effective_stats(ENTRY, ALICE)
        weapon = next(
            item for item in C.daily_storefront_weapons(ENTRY, pets.today())
            if "strength" in item.bonuses
        )
        ok, note = pets.buy_item(ENTRY, ALICE, RICH_XP, weapon.code)
        self.assertTrue(ok, note)
        ok, note = pets.equip(ENTRY, ALICE, weapon.code)
        self.assertTrue(ok, note)
        armed = pets.effective_stats(ENTRY, ALICE)
        self.assertEqual(
            armed["strength"], bare["strength"] + weapon.bonuses["strength"]
        )
        self._render_every_screen(ALICE)

    def test_a_fight_pays_out_and_shows_up_in_both_histories(self):
        self._found(ALICE, "Кабанчик", "Alice")
        self._found(BOB, "Тумблер", "Bob")

        self.assertEqual(pets.find_opponent(ENTRY, ALICE), str(BOB))
        left_before = pets.fights_left(ENTRY, ALICE, pets.today())

        result = pets_combat.simulate(
            _fighter(ALICE, "Кабанчик"), _fighter(BOB, "Тумблер"),
            rng=random.Random(7),
        )
        purse_before = self._balance(ALICE)
        reward = pets.record_fight(
            ENTRY, ALICE, BOB, result, pets.today(), attacker_xp=RICH_XP,
        )

        # The daily allowance is spent by the ATTACKER only.
        self.assertEqual(pets.fights_left(ENTRY, ALICE, pets.today()), left_before - 1)
        self.assertEqual(
            pets.fights_left(ENTRY, BOB, pets.today()),
            pets.daily_allowance(ENTRY, BOB, pets.today()),
            "the defender did not choose this fight, so it must not come out of the "
            "budget they earned by chatting",
        )

        won = result.winner == str(ALICE)
        if won:
            self.assertGreaterEqual(reward["gold"], C.WIN_GOLD_MIN)
            self.assertLessEqual(reward["gold"], round(C.WIN_GOLD_MAX * 1.25))
            self.assertEqual(self._balance(ALICE), purse_before + reward["gold"])
        else:
            # Losing now costs half of what the winner took -- and the attacker here is
            # rich enough that the "pays what they have" clamp cannot be what is measured.
            self.assertEqual(reward["gold"], 0)
            self.assertEqual(reward["loss_gold"], C.loss_gold_for(_won_gold(ENTRY)))
            self.assertEqual(self._balance(ALICE), purse_before - reward["loss_gold"])

        # The report renders, and reads from the winner's side either way.
        report = pets_ui.fight_report(
            result, str(ALICE), {str(ALICE): "Кабанчик", str(BOB): "Тумблер"}, reward,
        )
        self.assertIn("Победа" if won else "Поражение", report)
        self.assertNotIn("None", report)
        self.assertNotIn("{", report, "an unformatted flavour template reached the player")

        # One fight, two points of view.
        mine = pets.history(ENTRY, ALICE)
        theirs = pets.history(ENTRY, BOB)
        self.assertEqual(len(mine), 1)
        self.assertEqual(len(theirs), 1)
        self.assertEqual(str(mine[0]["attacker_id"]), str(ALICE))
        self.assertEqual(str(theirs[0]["attacker_id"]), str(ALICE))
        for text, _ in (pets_ui.history_view(ENTRY, ALICE), pets_ui.history_view(ENTRY, BOB)):
            self.assertNotIn("None", text)
            self.assertNotIn("?", text.replace("Боёв пока не было.", ""))

    def test_losing_costs_half_of_what_the_winner_took(self):
        """The seeded walkthrough above only exercises whichever side happens to win, so
        the losing branch is pinned here on a fight whose outcome is not left to chance."""
        self._found(ALICE, "Кабанчик", "Alice")
        self._found(BOB, "Тумблер", "Bob")
        # Bob is overwhelming, so Alice loses; the result is asserted, not assumed.
        pets.upgrade_stat(ENTRY, BOB, RICH_XP, "strength", times=60)
        pets.upgrade_stat(ENTRY, BOB, RICH_XP, "health", times=60)
        result = pets_combat.simulate(
            _fighter(ALICE, "Кабанчик"), _fighter(BOB, "Тумблер"), rng=random.Random(1),
        )
        self.assertEqual(result.winner, str(BOB), "the fixture must produce a loss")

        alice_before, bob_before = self._balance(ALICE), self._balance(BOB)
        reward = pets.record_fight(
            ENTRY, ALICE, BOB, result, pets.today(), attacker_xp=RICH_XP,
        )
        won = _won_gold(ENTRY)

        self.assertGreater(won, 0)
        self.assertEqual(reward["loss_gold"], C.loss_gold_for(won))
        self.assertEqual(reward["loss_gold"], round(won * C.LOSS_GOLD_SHARE))
        self.assertEqual(self._balance(ALICE), alice_before - reward["loss_gold"])
        self.assertEqual(self._balance(BOB), bob_before + won)
        # And the loser's own history line shows the debit, not the winner's credit.
        row = pets.history(ENTRY, ALICE)[0]
        self.assertEqual(row["gold"], 0)
        self.assertEqual(row["loss_gold"], reward["loss_gold"])

    def test_an_empty_wallet_pays_what_it_has_and_never_goes_into_debt(self):
        self._found(ALICE, "Кабанчик", "Alice")
        self._found(BOB, "Тумблер", "Bob")
        # Alice is broke: no chat XP at all, so nothing was ever earned to spend.
        result = pets_combat.simulate(
            _fighter(ALICE, "Кабанчик"), _fighter(BOB, "Тумблер"), rng=random.Random(2),
        )
        loser = ALICE if result.winner == str(BOB) else BOB
        self.assertEqual(economy.balance(ENTRY, loser, 0), 0)
        reward = pets.record_fight(ENTRY, ALICE, BOB, result, pets.today())
        self.assertEqual(economy.balance(ENTRY, loser, 0), 0)
        self.assertGreaterEqual(reward["loss_gold"], 0)

    def _record_yesterday(self, user_id, messages=0, figurines=0):
        """Write a finalised day file for yesterday, the same shape stats.record_day
        writes, so the allowance has real activity to price off."""
        day = pets.today() - timedelta(days=1)
        payload = {
            "entry": ENTRY,
            "day": day.isoformat(),
            "recorded_at": day.isoformat(),
            "users": {
                str(user_id): {
                    "display_name": "Alice", "username": "alice",
                    "messages": messages, "chars": messages * 20, "media": 0,
                    "replies": 0, "hours": {}, "figurines": figurines,
                }
            },
        }
        stats._path(ENTRY, day).write_text(json.dumps(payload), encoding="utf-8")

    def test_chatting_yesterday_buys_fights_today(self):
        # The fixed budget deliberately ignores ordinary chat-message volume.
        self._found(ALICE, "Pet", "Alice")
        day = pets.today() - timedelta(days=1)
        payload = {
            "entry": ENTRY, "day": day.isoformat(), "recorded_at": day.isoformat(),
            "users": {str(ALICE): {"display_name": "Alice", "username": "alice",
                "messages": 412, "chars": 8240, "media": 0, "replies": 0,
                "hours": {}, "figurines": 0}},
        }
        stats._path(ENTRY, day).write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(pets.daily_allowance(ENTRY, ALICE, pets.today()), C.BASE_DAILY_FIGHTS)

    def test_duplicate_live_paint_is_counted_once_and_deletion_removes_buff(self):
        self._found(ALICE, "Pet", "Alice")
        today = pets.today()
        stats.record_figurine_live(ENTRY, today, ALICE, "alice", "Alice", message_id=77)
        stats.record_figurine_live(ENTRY, today, ALICE, "alice", "Alice", message_id=77)
        self.assertEqual(pets.daily_allowance(ENTRY, ALICE, today), C.BASE_DAILY_FIGHTS + 2)
        stats.delete_figurine_submission(ENTRY, ALICE, 77, "admin", "Admin")
        self.assertEqual(pets.daily_allowance(ENTRY, ALICE, today), C.BASE_DAILY_FIGHTS)

    def test_cage_farm_and_recent_paint_bonuses_compose(self):
        self._found(ALICE, "Pet", "Alice")
        today = pets.today()
        six_days_ago = today - timedelta(days=6)
        payload = {
            "entry": ENTRY, "day": six_days_ago.isoformat(), "recorded_at": six_days_ago.isoformat(),
            "users": {str(ALICE): {"display_name": "Alice", "username": "alice",
                "messages": 0, "chars": 0, "media": 2, "replies": 0, "hours": {},
                "figurines": 2, "figurine_posts": [["2026-08-01T10:00:00", 101], ["2026-08-01T11:00:00", 102]]}},
        }
        stats._path(ENTRY, six_days_ago).write_text(json.dumps(payload), encoding="utf-8")
        data = pets._load(ENTRY)
        data["pets"][str(ALICE)]["cage_level"] = 3
        data["pets"][str(ALICE)]["farm_level"] = 5
        pets._save(ENTRY, data)
        self.assertEqual(
            pets.daily_allowance(ENTRY, ALICE, today),
            C.BASE_DAILY_FIGHTS + C.CAGE_BONUS_FIGHTS[2] + 5 // C.FARM_LEVELS_PER_FIGHT + 4,
        )

    def test_paint_buff_expires_after_seven_calendar_days(self):
        self._found(ALICE, "Pet", "Alice")
        today = pets.today()
        expired_day = today - timedelta(days=C.RECENT_FIGURINE_FIGHT_BUFF_DAYS)
        payload = {
            "entry": ENTRY, "day": expired_day.isoformat(), "recorded_at": expired_day.isoformat(),
            "users": {str(ALICE): {"display_name": "Alice", "username": "alice",
                "messages": 0, "chars": 0, "media": 1, "replies": 0, "hours": {},
                "figurines": 1, "figurine_posts": [["2026-08-01T10:00:00", 103]]}},
        }
        stats._path(ENTRY, expired_day).write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(pets.daily_allowance(ENTRY, ALICE, today), C.BASE_DAILY_FIGHTS)

    def test_unfinalized_prior_day_live_painting_still_grants_its_buff(self):
        self._found(ALICE, "Pet", "Alice")
        today = pets.today()
        stats.record_figurine_live(
            ENTRY, today - timedelta(days=1), ALICE, "alice", "Alice", message_id=104,
        )
        self.assertEqual(pets.daily_allowance(ENTRY, ALICE, today), C.BASE_DAILY_FIGHTS + 2)

    def test_history_survives_a_rename(self):
        """The card shows the creature's name NOW; a fight that already happened keeps the
        name it was fought under. Snapshotting is the whole reason this is asserted."""
        self._found(ALICE, "Кабанчик", "Alice")
        self._found(BOB, "Тумблер", "Bob")
        result = pets_combat.simulate(
            _fighter(ALICE, "Кабанчик"), _fighter(BOB, "Тумблер"), rng=random.Random(3),
        )
        pets.record_fight(ENTRY, ALICE, BOB, result, pets.today())

        ok, note = pets.rename(ENTRY, ALICE, "Совершенно другое имя")
        self.assertTrue(ok, note)
        self.assertEqual(pets.history(ENTRY, ALICE)[0]["attacker_name"], "Кабанчик")
        self.assertIn("Совершенно другое имя", pets_ui.pet_card(ENTRY, ALICE, pets.get_pet(ENTRY, ALICE)))

    def test_the_daily_allowance_actually_runs_out(self):
        self._found(ALICE, "Кабанчик", "Alice")
        self._found(BOB, "Тумблер", "Bob")
        allowance = pets.fights_left(ENTRY, ALICE, pets.today())
        # No recorded day file in this fixture, so yesterday's activity is nothing at all
        # and everybody falls back to the base allowance.
        self.assertEqual(allowance, C.BASE_DAILY_FIGHTS + C.CAGE_BONUS_FIGHTS[0])

        for index in range(allowance):
            result = pets_combat.simulate(
                _fighter(ALICE, "Кабанчик"), _fighter(BOB, "Тумблер"),
                rng=random.Random(index),
            )
            pets.record_fight(ENTRY, ALICE, BOB, result, pets.today())
        self.assertEqual(pets.fights_left(ENTRY, ALICE, pets.today()), 0)

        # And the screen says so rather than offering a button that cannot work.
        with patch("pets.app_now", return_value=datetime(2026, 8, 9, 22, 41)):
            text, keyboard = pets_ui.fight_view(ENTRY, ALICE, RICH_XP)
        self.assertIn("Обновление боёв через: 1 ч 19 мин", text)
        actions = {
            pets_ui.parse_callback(b["callback_data"])[1]
            for row in keyboard["inline_keyboard"] for b in row if b.get("callback_data")
        }
        self.assertNotIn("search", actions)

        # History is capped at what the menu promises to show.
        self.assertLessEqual(len(pets.history(ENTRY, ALICE)), C.HISTORY_LIMIT)

    def test_a_pet_that_levels_up_gets_stronger_everywhere(self):
        """+1 to every stat per pet level is the strongest thing in the game, so it has to
        reach the fight, not just the card."""
        self._found(ALICE, "Кабанчик", "Alice")
        before = pets.effective_stats(ENTRY, ALICE)
        level, gained = pets.award_xp(ENTRY, ALICE, 50_000)
        self.assertGreater(gained, 0)
        after = pets.effective_stats(ENTRY, ALICE)
        for key in C.STAT_KEYS:
            self.assertEqual(after[key] - before[key], gained * C.PET_LEVEL_STAT_BONUS)

        fighter = _fighter(ALICE, "Кабанчик")
        derived = pets_combat.derive(fighter, fighter)
        self.assertGreater(derived["max_hp"], C.BASE_HP)


class FlavorGrammarTests(unittest.TestCase):
    """Russian numerals agree with the noun after them -- "92 очка", but "95 очков" -- and
    a template cannot know which it will get. Every damage figure in the bank therefore has
    to be followed by a word that does not decline ("урона", "HP") or by no noun at all.
    One batch of lines shipped with "{amount} очков здоровья" and rendered "92 очков"; this
    is here so the next batch cannot."""

    # Nouns that would have to agree with the number in front of them.
    COUNTABLE = ("очк", "единиц", "хитпоинт", "балл", "пункт")

    def test_a_menu_number_agrees_with_the_noun_after_it(self):
        """The menus know their numbers at render time, so unlike the flavour bank they
        decline properly. The 11-vs-21 exception is the one worth pinning: both end in 1,
        and only one of them takes the singular."""
        cases = {
            0: "0 монет", 1: "1 монета", 2: "2 монеты", 5: "5 монет",
            11: "11 монет", 21: "21 монета", 22: "22 монеты", 25: "25 монет",
            41: "41 монета", 100: "100 монет", 1234: "1.234 монеты",
        }
        for amount, expected in cases.items():
            self.assertEqual(pets_ui._coins(amount), expected)

    def test_no_damage_figure_is_followed_by_a_noun_that_must_agree(self):
        import re

        import pets_flavor

        for event, templates in pets_flavor.VARIANTS.items():
            for template in templates:
                for tail in re.findall(r"\{amount\}\s*(\w+)", template):
                    self.assertFalse(
                        tail.lower().startswith(self.COUNTABLE),
                        f"{event}: «{template}» -- «{tail}» must agree with the numeral",
                    )


def _won_gold(entry):
    """What the winner of the most recent fight actually took, cage bonus included.

    Read from BOTH sides and maxed, because history() zeroes the gold column on the
    loser's own line -- reading only the loser's view would return 0 and make a "the
    penalty is half the winnings" assertion pass against any penalty at all.
    """
    return max(
        pets.history(entry, ALICE)[0]["gold"],
        pets.history(entry, BOB)[0]["gold"],
    )


def _fighter(user_id, name):
    effective = pets.effective_stats(ENTRY, user_id)
    return pets_combat.Fighter(
        key=str(user_id), name=name,
        strength=effective["strength"], health=effective["health"],
        agility=effective["agility"], luck=effective["luck"],
        armor=effective["armor"],
    )


if __name__ == "__main__":
    unittest.main()
