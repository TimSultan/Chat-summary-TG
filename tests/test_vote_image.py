"""vote_image.render_standings_image -- the one-picture export of a vote's board.

Pure Pillow work over a directory of photos, so it is exercised for real (actual JPEGs
written to a temp dir, actual image read back) rather than mocked: the things worth
protecting here are the geometry (three columns, one row per three works, no cropping) and
the fact that a missing photo doesn't lose the board.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_listener
import vote_image
import voting


def _entry(entry_id, author_name="Author", username=None, media=()):
    return voting.Entry(
        entry_id=entry_id, message_id=int(entry_id), author_id=1,
        author_name=author_name, author_username=username, text="",
        media=list(media),
    )


class RenderStandingsImageTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.media = self.root / "media"
        self.media.mkdir()

    def _photo(self, name, size=(800, 600), color=(200, 30, 30)):
        path = self.media / name
        Image.new("RGB", size, color).save(path, "JPEG")
        return name

    def _standings(self, count, size=(800, 600)):
        return [
            (_entry(str(i), f"Автор {i}", f"user{i}", [self._photo(f"{i}.jpg", size)]), count - i)
            for i in range(count)
        ]

    def test_writes_a_readable_picture_of_the_expected_size(self):
        out = self.root / "board.jpg"
        path = vote_image.render_standings_image(
            self._standings(5), self.media, out, subtitle="Проголосовало: 3"
        )
        self.assertEqual(path, out)
        with Image.open(path) as image:
            self.assertEqual(image.width, vote_image.WIDTH)
            # 5 works over 3 columns is 2 rows -- the height is entirely a function of
            # that, which is what "one long image" means here.
            self.assertGreater(image.height, 2 * vote_image.CARD_HEIGHT)

    def test_height_grows_one_row_at_a_time_not_one_entry_at_a_time(self):
        heights = []
        for count in (1, 3, 4, 6, 7):
            out = self.root / f"board{count}.jpg"
            vote_image.render_standings_image(self._standings(count), self.media, out)
            with Image.open(out) as image:
                heights.append(image.height)
        # 1 and 3 works are both one row; 4 and 6 both two; 7 starts a third.
        self.assertEqual(heights[0], heights[1])
        self.assertEqual(heights[2], heights[3])
        self.assertEqual(
            heights[4] - heights[3], vote_image.CARD_HEIGHT + vote_image.GAP
        )

    def test_a_missing_photo_leaves_an_empty_card_rather_than_failing(self):
        standings = [
            (_entry("1", "Есть фото", "a", [self._photo("1.jpg")]), 2),
            (_entry("2", "Нет файла", "b", ["nope.jpg"]), 1),
            (_entry("3", "Совсем без медиа", None, []), 0),
        ]
        out = self.root / "board.jpg"
        vote_image.render_standings_image(standings, self.media, out)
        self.assertTrue(out.exists())

    def test_a_tall_photo_is_fitted_not_cropped(self):
        """A portrait shot keeps its whole self: fitted into the square thumbnail, it must
        be narrower than the card and letterboxed on both sides -- the opposite of the
        page's object-fit: cover, and the point of the export."""
        standings = [(_entry("1", "Портрет", "p", [self._photo("1.jpg", (400, 1200), (255, 0, 0))]), 1)]
        out = self.root / "board.jpg"
        vote_image.render_standings_image(standings, self.media, out, title="", subtitle="")
        with Image.open(out) as image:
            pixels = image.convert("RGB").load()
            # Middle of the first card's thumbnail row: red in the centre (the photo),
            # letterbox at the left edge (nothing of the photo cropped away to fill it).
            y = vote_image.MARGIN + vote_image.THUMB_HEIGHT // 2
            centre = pixels[vote_image.MARGIN + vote_image.CARD_WIDTH // 2, y]
            edge = pixels[vote_image.MARGIN + 4, y]
        self.assertGreater(centre[0], 200)
        self.assertLess(centre[1], 60)
        self.assertLess(edge[0], 100)

    def test_empty_standings_is_refused_rather_than_written_blank(self):
        with self.assertRaises(ValueError):
            vote_image.render_standings_image([], self.media, self.root / "board.jpg")

    def test_order_is_taken_as_given_never_re_sorted(self):
        """The caller hands over a tally() result; drawing it in any other order would put
        the export out of step with both the page and the announcement text."""
        drawn = []
        original = vote_image._draw_card

        def spy(entry, votes, media_dir, show_votes):
            drawn.append(entry.entry_id)
            return original(entry, votes, media_dir, show_votes)

        vote_image._draw_card = spy
        self.addCleanup(setattr, vote_image, "_draw_card", original)
        standings = [
            (_entry("7", "Third", None, [self._photo("7.jpg")]), 0),
            (_entry("1", "First", None, [self._photo("1.jpg")]), 9),
            (_entry("4", "Second", None, [self._photo("4.jpg")]), 4),
        ]
        vote_image.render_standings_image(standings, self.media, self.root / "board.jpg")
        self.assertEqual(drawn, ["7", "1", "4"])


class RenderPollImageTests(unittest.TestCase):
    """render_poll_image resolves the poll's own media directory and ranks by tally(), so
    only ADMITTED entries reach the picture."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        patcher = patch("voting._voting_dir", return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_only_admitted_entries_are_drawn(self):
        poll = voting.Poll(
            poll_id="p", entry="Chat", created_at="t0",
            entries=[_entry("1", "A", "a", ["1.jpg"]), _entry("2", "B", "b", ["2.jpg"])],
        )
        media = voting.media_path("Chat", "p")
        media.mkdir(parents=True)
        for name in ("1.jpg", "2.jpg"):
            Image.new("RGB", (300, 300), (10, 120, 200)).save(media / name, "JPEG")
        voting.set_approved(poll, ["1"])

        drawn = []
        original = vote_image._draw_card

        def spy(entry, votes, media_dir, show_votes):
            drawn.append(entry.entry_id)
            return original(entry, votes, media_dir, show_votes)

        vote_image._draw_card = spy
        self.addCleanup(setattr, vote_image, "_draw_card", original)
        out = voting.export_image_path("Chat", "p")
        path = vote_image.render_poll_image(poll, out)
        self.assertEqual(drawn, ["1"])
        self.assertTrue(path.exists())


class VoteImageCommandTests(unittest.TestCase):
    """"/vote картинка" end to end: the admin gate, the render, the file on disk and the
    upload. This is the path the moderator menu's "🖼 Картинка итогов" button takes -- it
    synthesizes exactly this command text (see VOTE_ACTIONS)."""

    class FakeApi:
        def __init__(self):
            self.sent = []
            self.documents = []

        async def send_message(self, chat_id, text, reply_to_message_id=None,
                                reply_markup=None, parse_mode=None):
            self.sent.append(text)
            return {"message_id": 999}

        async def send_document_file(self, chat_id, path, caption=None,
                                      reply_to_message_id=None, reply_markup=None,
                                      parse_mode=None):
            self.documents.append(Path(path))
            return {"message_id": 1000}

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        patcher = patch("voting._voting_dir", return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api = self.FakeApi()

    def _run_command(self, is_admin=True):
        import asyncio
        from types import SimpleNamespace

        message = {
            "message_id": 1,
            "chat": {"id": 5, "type": "private"},
            "from": {"id": 7, "username": "admin"},
            "text": "/vote картинка",
        }
        cfg = SimpleNamespace(
            webapp_public_url="https://example.com",
            vote_miniapp_short_name=None,
            vote_announce_extra_chat=None,
        )

        async def resolve(*args, **kwargs):
            return -100

        async def can_manage(*args, **kwargs):
            return is_admin

        with patch.object(bot_listener, "_resolve_chat_id", resolve), \
             patch.object(bot_listener, "_can_manage_chat", can_manage):
            asyncio.run(bot_listener.handle_vote_command(
                self.api, None, cfg, None, message, "Chat", "testbot", set(),
                log=lambda *_: None,
            ))

    def _poll_with_one_admitted_work(self):
        poll = voting.Poll(
            poll_id="p", entry="Chat", created_at="t0",
            entries=[_entry("1", "Автор", "author", ["1.jpg"])],
        )
        media = voting.media_path("Chat", "p")
        media.mkdir(parents=True)
        Image.new("RGB", (500, 700), (30, 140, 210)).save(media / "1.jpg", "JPEG")
        voting.set_approved(poll, ["1"])
        voting.record_vote(poll, 42, ["1"])
        voting.save_poll(poll)
        return poll

    def test_renders_saves_and_uploads_the_board(self):
        self._poll_with_one_admitted_work()
        self._run_command()

        expected = voting.export_image_path("Chat", "p")
        self.assertTrue(expected.exists(), "the picture must stay on disk, not only be sent")
        self.assertEqual(self.api.documents, [expected])
        with Image.open(expected) as image:
            self.assertEqual(image.width, vote_image.WIDTH)

    def test_a_non_admin_gets_nothing_rendered(self):
        self._poll_with_one_admitted_work()
        self._run_command(is_admin=False)
        self.assertEqual(self.api.documents, [])
        self.assertFalse(voting.export_image_path("Chat", "p").exists())

    def test_nothing_admitted_yet_is_said_rather_than_drawn(self):
        poll = voting.Poll(
            poll_id="p", entry="Chat", created_at="t0",
            entries=[_entry("1", "Автор", "author", ["1.jpg"])],
        )
        voting.save_poll(poll)
        self._run_command()
        self.assertEqual(self.api.documents, [])
        self.assertIn("не допущена", " ".join(self.api.sent))

    def test_no_poll_at_all_is_said_rather_than_drawn(self):
        self._run_command()
        self.assertEqual(self.api.documents, [])
        self.assertIn("ещё не создано", " ".join(self.api.sent))


class VoteImageButtonTests(unittest.TestCase):
    def test_the_menu_button_synthesizes_a_command_the_parser_recognizes(self):
        """The moderator menu's buttons work by handing handle_vote_command a synthetic
        message carrying VOTE_ACTIONS' text. If that text and the word set ever drifted
        apart the button would silently open the plain ballot instead."""
        words = {
            "collect": bot_listener.VOTE_COLLECT_WORDS,
            "chat": bot_listener.VOTE_CHAT_WORDS,
            "image": bot_listener.VOTE_IMAGE_WORDS,
            "clear": bot_listener.VOTE_CLEAR_WORDS,
        }
        self.assertEqual(set(words), set(bot_listener.VOTE_ACTIONS))
        for action, command in bot_listener.VOTE_ACTIONS.items():
            argument = command[len("/vote"):].strip().lower()
            self.assertIn(argument, words[action], f"{action}: {command} is unparseable")


if __name__ == "__main__":
    unittest.main()
