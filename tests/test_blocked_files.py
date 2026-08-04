"""Archives and 3D models don't belong in the chat: they are deleted and their sender is
told to pass them around in a DM instead (see BLOCKED_FILE_EXTENSIONS in listener.py).

What's pinned here is the detection itself -- which attachments match and, just as
importantly, which must not -- plus the wording/addressing of the notice that replaces the
removed message.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telethon.tl.types import User

import listener


def _sender(user_id, first_name, last_name=None, username=None):
    """A real telethon User, not a stand-in: sender_display_name -- which the notice goes
    through -- dispatches on isinstance(sender, User), so a duck-typed object would test
    the "Unknown sender" path instead of the one that actually runs."""
    return User(id=user_id, first_name=first_name, last_name=last_name, username=username)


def _document_message(*file_names, mime_type="application/octet-stream"):
    """A message carrying one document, named the way Telethon exposes it: the filename
    lives in a DocumentAttributeFilename among the document's attributes, not on the
    message. Several names may be given to cover a document with other attributes too."""
    attributes = [SimpleNamespace(file_name=name) for name in file_names]
    return SimpleNamespace(
        document=SimpleNamespace(attributes=attributes, mime_type=mime_type)
    )


class DetectionTests(unittest.TestCase):
    def test_every_blocked_extension_is_caught(self):
        for ext in listener.BLOCKED_FILE_EXTENSIONS:
            with self.subTest(ext=ext):
                self.assertEqual(
                    listener.blocked_file_name(_document_message(f"model{ext}")),
                    f"model{ext}",
                )

    def test_the_extension_check_is_case_insensitive(self):
        # Windows and plenty of slicers hand over MODEL.STL, which is the same file.
        self.assertEqual(listener.blocked_file_name(_document_message("MODEL.STL")), "MODEL.STL")

    def test_an_ordinary_document_is_left_alone(self):
        for name in ("readme.txt", "схема.pdf", "покрас.png", "archive.zip.txt"):
            with self.subTest(name=name):
                self.assertIsNone(listener.blocked_file_name(_document_message(name)))

    def test_a_message_without_a_document_never_matches(self):
        # A compressed photo/video -- what a #япокрасил post is -- has no document at all,
        # so the figurine flow below this check must stay untouched by it.
        self.assertIsNone(listener.blocked_file_name(SimpleNamespace(document=None)))
        self.assertIsNone(listener.blocked_file_name(SimpleNamespace()))

    def test_a_document_with_no_filename_attribute_never_matches(self):
        # Voice notes, round videos and stickers all arrive as documents with attributes
        # that carry no file_name -- reading one off them must not raise.
        self.assertIsNone(
            listener.blocked_file_name(
                SimpleNamespace(document=SimpleNamespace(attributes=[SimpleNamespace(duration=3)]))
            )
        )
        self.assertIsNone(
            listener.blocked_file_name(SimpleNamespace(document=SimpleNamespace(attributes=None)))
        )

    def test_the_filename_is_found_among_other_attributes(self):
        msg = SimpleNamespace(
            document=SimpleNamespace(
                attributes=[SimpleNamespace(duration=3), SimpleNamespace(file_name="печать.stl")]
            )
        )
        self.assertEqual(listener.blocked_file_name(msg), "печать.stl")


class NoticeTests(unittest.TestCase):
    def test_a_sender_with_a_username_is_addressed_by_it(self):
        notice = listener.format_blocked_file_notice(_sender(7, "Аня", username="anna"))
        self.assertEqual(
            notice, "@anna, пересылка файлов разрешена только в личке. Спасибо за понимание."
        )

    def test_a_sender_without_a_username_gets_a_mention_link(self):
        notice = listener.format_blocked_file_notice(_sender(42, "Аня", last_name="К"))
        self.assertTrue(notice.startswith('<a href="tg://user?id=42">Аня К</a>, '), notice)

    def test_a_display_name_cannot_break_the_html(self):
        # Somebody else's uncontrolled text goes into an HTML message -- an unescaped "<"
        # in a display name would make Telegram reject the whole send.
        notice = listener.format_blocked_file_notice(_sender(42, "<b>Аня"))
        self.assertIn("&lt;b&gt;Аня", notice)
        self.assertNotIn("<b>", notice)


if __name__ == "__main__":
    unittest.main()
