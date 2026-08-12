import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw

import pets_image


def _png(color, size=(80, 60)) -> bytes:
    image = Image.new("RGB", size, color)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class FightImageTests(unittest.TestCase):
    def test_names_are_normalized_and_unrenderable_glyphs_are_removed(self):
        self.assertEqual(pets_image.legible("𝓐𝓷𝓷𝓪", 25), "Anna")
        self.assertEqual(pets_image._short("🌸", 24), "Без имени")

    def test_winner_name_uses_green(self):
        self.assertEqual(pets_image.WINNER_NAME_COLOR, "#147a59")

    def test_loser_name_uses_red(self):
        self.assertEqual(pets_image.LOSER_NAME_COLOR, "#b83e58")

    def test_pet_photo_is_contained_so_portraits_are_not_cropped(self):
        """A tall image retains both ends and receives side letterboxing."""
        portrait = Image.new("RGB", (100, 300), "red")
        portrait.paste((0, 0, 255), (0, 150, 100, 300))
        output = BytesIO()
        portrait.save(output, format="PNG")

        fitted = pets_image._photo(
            output.getvalue(), pets_image.PET_IMAGE_SIZE, (20, 30, 40), crop=False,
        )

        centre = fitted.width // 2
        self.assertEqual(fitted.getpixel((centre, 2)), (255, 0, 0))
        self.assertEqual(fitted.getpixel((centre, fitted.height - 3)), (0, 0, 255))
        self.assertEqual(fitted.getpixel((2, fitted.height // 2)), (20, 30, 40))

    def test_layout_prioritizes_tall_images_and_bottom_aligned_stats(self):
        self.assertGreaterEqual(pets_image.PET_IMAGE_SIZE[1], 360)
        stats_top = pets_image._stats_top(5)
        self.assertEqual(
            pets_image.PANEL_BOTTOM - stats_top,
            pets_image.STATS_BOTTOM_PADDING + 5 * pets_image.STAT_ROW_HEIGHT,
        )
        self.assertGreater(stats_top, pets_image.STATS_DIVIDER_TOP)
        self.assertLess(pets_image.WEAPON_NAME_TOP, pets_image.AMULET_NAME_TOP)
        self.assertLess(pets_image.AMULET_EFFECT_TOP, pets_image.EQUIPMENT_DIVIDER_TOP)
        self.assertLess(pets_image.EQUIPMENT_DIVIDER_TOP, pets_image.PET_NAME_TOP)
        self.assertLessEqual(pets_image.PANEL_BOTTOM, 800)

    def test_equipment_helpers_show_rarity_and_icon_stats(self):
        weapon = {
            "name": "Сковородка",
            "rarity": "rare",
            "bonuses": {"strength": 6, "luck": 2, "armor": -1},
        }
        amulet = {
            "name": "Чайный пакетик",
            "rarity": "uncommon",
            "bonuses": {"health": 3},
            "effect": "В бою +14 здоровья.",
        }
        self.assertEqual(pets_image.RARITY_SYMBOLS[weapon["rarity"]][0], "♦")
        self.assertEqual(pets_image.RARITY_SYMBOLS[amulet["rarity"]][0], "●")
        bonuses = pets_image._equipment_bonus_text(weapon)
        self.assertIn("† +6", bonuses)
        self.assertIn("♣ +2", bonuses)
        self.assertIn("■ -1", bonuses)

    def test_renderer_creates_a_shareable_result_board(self):
        result = SimpleNamespace(
            winner="a", is_draw=False, stopped_early=False,
            total_damage={"a": 420, "b": 315},
        )
        attacker = {
            "id": "a", "pet_name": "Альфа", "owner_name": "Alice",
            "stats": {"strength": 12, "health": 11, "agility": 10, "luck": 9, "armor": 5},
            "power": 250, "pet_photo": _png("green"), "owner_avatar": _png("blue"),
            "remaining_hp": 66, "max_hp": 100,
            "weapon": {"name": "Сковородка", "rarity": "rare", "bonuses": {"strength": 6}},
            "amulet": {"name": "Чайный пакетик", "rarity": "uncommon", "bonuses": {"health": 3},
                       "effect": "В бою +14 здоровья."},
            "shield": {"name": "Картонный баклер", "rarity": "common", "bonuses": {"armor": 10},
                       "effect": "При защите создаёт барьер."},
        }
        defender = {
            "id": "b", "pet_name": "Бета", "owner_name": "Bob",
            "stats": {"strength": 10, "health": 14, "agility": 8, "luck": 11, "armor": 7},
            "power": 248, "pet_photo": _png("red"), "owner_avatar": _png("yellow"),
            "remaining_hp": 0, "max_hp": 120,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = pets_image.render_fight_result(Path(directory) / "fight.jpg", result, attacker, defender)
            self.assertTrue(path.exists())
            with Image.open(path) as image:
                self.assertEqual(image.size, (pets_image.WIDTH, pets_image.HEIGHT))
                self.assertEqual(image.format, "JPEG")
                self.assertNotEqual(image.getpixel((100, 200)), image.getpixel((700, 200)))

    def test_hp_bar_is_red_for_remaining_health_and_grey_when_empty(self):
        image = Image.new("RGB", (pets_image.WIDTH, pets_image.HEIGHT), "white")
        draw = ImageDraw.Draw(image)
        living = {"remaining_hp": 50, "max_hp": 100}
        empty = {"remaining_hp": 0, "max_hp": 100}
        pets_image._draw_hp_bar(draw, 45, living)
        pets_image._draw_hp_bar(draw, 685, empty)
        y = pets_image.HP_BAR_TOP + pets_image.HP_BAR_HEIGHT // 2
        self.assertEqual(image.getpixel((45 + pets_image.PANEL_PADDING_X + 50, y)), (200, 68, 75))
        self.assertEqual(image.getpixel((685 + pets_image.PANEL_PADDING_X + 50, y)), (174, 182, 181))

class BattleLogImageTests(unittest.TestCase):
    ATTACKER = {"id": "a", "pet_name": "Альфа"}
    DEFENDER = {"id": "b", "pet_name": "Бета"}

    def _round(self, attacker, number=1, damage=100, event="hit", text="Удар."):
        return SimpleNamespace(
            number=number, attacker=attacker, event=event, damage=damage,
            attacker_hp=400, defender_hp=300, text=text,
        )

    def _result(self, rounds):
        return SimpleNamespace(
            rounds=tuple(rounds), opening="Бой начинается.", closing="Бой окончен.",
            winner="a", loser="b", total_damage={"a": 1, "b": 0},
            stopped_early=False, is_draw=False, seed=1, accident=None,
        )

    def _render(self, result):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return pets_image.render_fight_log(
            Path(directory.name) / "log.jpg", result, self.ATTACKER, self.DEFENDER,
        )

    def test_attacker_rows_are_red_and_defender_rows_are_blue(self):
        entries = pets_image._log_entries(
            self._result([self._round("a"), self._round("b")]), self.ATTACKER, self.DEFENDER,
        )
        self.assertEqual(entries[0]["color"], pets_image.LOG_ATTACKER_COLOR)
        self.assertEqual(entries[1]["color"], pets_image.LOG_DEFENDER_COLOR)

    def test_board_grows_with_the_transcript_instead_of_clipping_it(self):
        short = self._render(self._result([self._round("a")]))
        longer = self._render(self._result([self._round("a")] * 6))
        with Image.open(short) as small, Image.open(longer) as big:
            self.assertEqual(small.size[0], pets_image.WIDTH)
            self.assertGreater(big.size[1], small.size[1])

    def test_consecutive_attacks_are_separated_by_a_margin(self):
        """The gap is the whole point of the row layout: two blows must not read as one.

        Asserted through the colour stripe rather than a single pixel: scanning down the
        stripe column must find two separate dark bands, not one continuous block.
        """
        path = self._render(self._result([self._round("a"), self._round("a")]))
        with Image.open(path) as image:
            column = pets_image.LOG_MARGIN_X + pets_image.LOG_STRIPE_WIDTH // 2
            dark = [
                sum(image.getpixel((column, y))) < 550
                for y in range(pets_image.LOG_ROWS_TOP, image.size[1])
            ]
        bands = sum(1 for index, value in enumerate(dark) if value and not dark[index - 1])
        self.assertEqual(bands, 2)
        self.assertGreaterEqual(sum(1 for value in dark if not value), pets_image.LOG_ROW_GAP)

    def test_a_long_fight_stays_inside_telegram_photo_limits(self):
        rounds = [self._round("a" if index % 2 else "b", number=index) for index in range(120)]
        path = self._render(self._result(rounds))
        with Image.open(path) as image:
            self.assertLess(sum(image.size), 10_000)
            self.assertLess(max(image.size) / min(image.size), 20)

    def test_an_elided_middle_reports_how_much_it_hid(self):
        rounds = [self._round("a", number=index) for index in range(pets_image.LOG_MAX_ROWS + 9)]
        entries = pets_image._log_entries(self._result(rounds), self.ATTACKER, self.DEFENDER)
        elisions = [entry for entry in entries if entry.get("elision")]
        self.assertEqual(len(elisions), 1)
        self.assertIn("9", elisions[0]["elision"])
        self.assertEqual(len(entries), pets_image.LOG_MAX_ROWS + 1)

    def test_a_passive_proc_never_renders_its_amount_as_damage(self):
        """A +164 shield displayed as "-164" would state the opposite of what happened."""
        entries = pets_image._log_entries(
            self._result([self._round("a", event="amulet_opening_shield", damage=164)]),
            self.ATTACKER, self.DEFENDER,
        )
        self.assertTrue(entries[0]["passive"])
        # A proc reports the owner's own health, not the target's.
        self.assertEqual(entries[0]["health"], 400)

    def test_a_blow_reports_the_health_of_whoever_took_it(self):
        entries = pets_image._log_entries(
            self._result([self._round("a")]), self.ATTACKER, self.DEFENDER,
        )
        self.assertFalse(entries[0]["passive"])
        self.assertEqual(entries[0]["health"], 300)

    def test_defend_and_non_damaging_scrolls_show_the_casters_health(self):
        entries = pets_image._log_entries(
            self._result([
                self._round("a", event="defend", damage=0),
                self._round("a", event="skill_scroll_healing_rain", damage=-50),
                self._round("a", event="skill_scroll_arcane_spark", damage=90),
            ]),
            self.ATTACKER, self.DEFENDER,
        )
        self.assertEqual([row["passive"] for row in entries], [True, True, False])
        self.assertEqual([row["health"] for row in entries], [400, 400, 300])


if __name__ == "__main__":
    unittest.main()
