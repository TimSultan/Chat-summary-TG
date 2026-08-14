import tempfile
import unittest
from pathlib import Path

from PIL import Image

import pets_dungeon_image


class DungeonFloorImageTests(unittest.TestCase):
    def test_regular_and_boss_floor_images_render_at_telegram_size(self):
        with tempfile.TemporaryDirectory() as directory:
            regular = pets_dungeon_image.render_floor(Path(directory) / "regular.jpg", 1)
            boss = pets_dungeon_image.render_floor(Path(directory) / "boss.jpg", 5)
            with Image.open(regular) as regular_image, Image.open(boss) as boss_image:
                self.assertEqual(regular_image.size, (1200, 720))
                self.assertEqual(boss_image.size, (1200, 720))
                self.assertNotEqual(regular_image.tobytes(), boss_image.tobytes())


if __name__ == "__main__":
    unittest.main()