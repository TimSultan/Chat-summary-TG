import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

import pets_image


def _png(color) -> bytes:
    image = Image.new("RGB", (80, 60), color)
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
