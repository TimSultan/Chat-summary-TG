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

    def test_guardian_renderer_contains_both_pet_panels(self):
        attacker = {
            "id": "a", "pet_name": "Взрослый", "owner_name": "Alice",
            "level": 8,
            "stats": {"strength": 20, "health": 18, "agility": 14, "luck": 11, "armor": 5},
            "power": 321, "pet_photo": _png("green"), "owner_avatar": _png("blue"),
            "remaining_hp": 120, "max_hp": 120,
        }
        defender = {
            "id": "b", "pet_name": "Малыш", "owner_name": "Bob",
            "level": 1,
            "stats": {"strength": 7, "health": 8, "agility": 6, "luck": 5, "armor": 1},
            "power": 90, "pet_photo": _png("red"), "owner_avatar": _png("yellow"),
            "remaining_hp": 80, "max_hp": 80,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = pets_image.render_guardian_result(
                Path(directory) / "guardian.jpg", attacker, defender, xp=5,
            )
            with Image.open(path) as image:
                self.assertEqual(image.size, (pets_image.WIDTH, pets_image.HEIGHT))
                self.assertNotEqual(image.getpixel((100, 200)), image.getpixel((700, 200)))


if __name__ == "__main__":
    unittest.main()
