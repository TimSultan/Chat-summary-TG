import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

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
        self.assertGreater(stats_top, pets_image.RATING_TOP)
        self.assertLessEqual(pets_image.PANEL_BOTTOM, 800)

    def test_renderer_creates_a_shareable_result_board(self):
        result = SimpleNamespace(
            winner="a", is_draw=False, stopped_early=False,
            total_damage={"a": 420, "b": 315},
        )
        attacker = {
            "id": "a", "pet_name": "Альфа", "owner_name": "Alice",
            "stats": {"strength": 12, "health": 11, "agility": 10, "luck": 9, "armor": 5},
            "power": 250, "pet_photo": _png("green"), "owner_avatar": _png("blue"),
        }
        defender = {
            "id": "b", "pet_name": "Бета", "owner_name": "Bob",
            "stats": {"strength": 10, "health": 14, "agility": 8, "luck": 11, "armor": 7},
            "power": 248, "pet_photo": _png("red"), "owner_avatar": _png("yellow"),
        }
        with tempfile.TemporaryDirectory() as directory:
            path = pets_image.render_fight_result(Path(directory) / "fight.jpg", result, attacker, defender)
            self.assertTrue(path.exists())
            with Image.open(path) as image:
                self.assertEqual(image.size, (pets_image.WIDTH, pets_image.HEIGHT))
                self.assertEqual(image.format, "JPEG")
                self.assertNotEqual(image.getpixel((100, 200)), image.getpixel((700, 200)))


if __name__ == "__main__":
    unittest.main()
