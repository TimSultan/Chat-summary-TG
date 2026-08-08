"""Pure-mapping tests for the post-stats desktop app's data layer.

post_stats.py is imported by a separate report renderer + Tkinter GUI (built in
parallel, against the same contract), so what's pinned here is the shape of PostStat
and the two pure functions that build it (post_link, _message_to_post_stat) -- no
real Telegram connection is used or needed.
"""

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from post_stats import PostStat, _message_to_post_stat, post_link


def _msg(**kwargs):
    """A minimal stub message -- only sets the attributes a given test cares about.
    _message_to_post_stat must use getattr(..., None) defensively for everything else,
    since real Telethon messages always have every attribute but these stubs don't."""
    return SimpleNamespace(**kwargs)


class PostLinkTests(unittest.TestCase):
    def test_public_entity_uses_username_form(self):
        entity = SimpleNamespace(username="somechannel", id=12345)
        self.assertEqual(post_link(entity, 42), "https://t.me/somechannel/42")

    def test_private_entity_uses_c_id_form(self):
        entity = SimpleNamespace(username=None, id=12345)
        self.assertEqual(post_link(entity, 42), "https://t.me/c/12345/42")

    def test_empty_username_treated_as_private(self):
        entity = SimpleNamespace(username="", id=999)
        self.assertEqual(post_link(entity, 7), "https://t.me/c/999/7")


class MessageToPostStatTests(unittest.TestCase):
    def setUp(self):
        self.entity = SimpleNamespace(username="chan", id=555)
        self.date = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

    def test_full_fields_map_correctly(self):
        reactions = SimpleNamespace(
            results=[
                SimpleNamespace(reaction=SimpleNamespace(emoticon="🔥"), count=3),
                SimpleNamespace(reaction=SimpleNamespace(emoticon="❤"), count=2),
            ]
        )
        replies = SimpleNamespace(replies=7)
        msg = _msg(
            id=10,
            date=self.date,
            text="hello world",
            views=100,
            forwards=5,
            reactions=reactions,
            replies=replies,
            edit_date=datetime(2026, 8, 1, 13, 0, tzinfo=timezone.utc),
            grouped_id=None,
            photo=None,
            video=None,
            document=None,
            sticker=None,
            gif=None,
            voice=None,
            video_note=None,
            contact=None,
            geo=None,
            poll=None,
            action=None,
        )
        stat = _message_to_post_stat(msg, self.entity, thumbnail_path=None)
        self.assertIsInstance(stat, PostStat)
        self.assertEqual(stat.message_id, 10)
        self.assertEqual(stat.date, self.date)
        self.assertEqual(stat.text_preview, "hello world")
        self.assertIsNone(stat.thumbnail_path)
        self.assertEqual(stat.views, 100)
        self.assertEqual(stat.forwards, 5)
        self.assertEqual(stat.reactions_total, 5)
        self.assertEqual(stat.reactions_breakdown, {"🔥": 3, "❤": 2})
        self.assertEqual(stat.comments, 7)
        self.assertTrue(stat.is_edited)
        self.assertEqual(stat.media_type, "none")
        self.assertEqual(stat.link, "https://t.me/chan/10")

    def test_none_reactions_and_replies_do_not_crash(self):
        msg = _msg(id=11, date=self.date, reactions=None, replies=None)
        stat = _message_to_post_stat(msg, self.entity, thumbnail_path=None)
        self.assertEqual(stat.reactions_total, 0)
        self.assertEqual(stat.reactions_breakdown, {})
        self.assertEqual(stat.comments, 0)
        self.assertFalse(stat.is_edited)
        self.assertEqual(stat.text_preview, "")

    def test_custom_emoji_reaction_keys_on_document_id(self):
        reactions = SimpleNamespace(
            results=[
                SimpleNamespace(
                    reaction=SimpleNamespace(document_id=987654321), count=1
                ),
            ]
        )
        msg = _msg(id=12, date=self.date, reactions=reactions, replies=None)
        stat = _message_to_post_stat(msg, self.entity, thumbnail_path=None)
        self.assertEqual(stat.reactions_breakdown, {"987654321": 1})

    def test_custom_emoji_reaction_falls_back_to_question_mark(self):
        reactions = SimpleNamespace(
            results=[SimpleNamespace(reaction=SimpleNamespace(), count=1)]
        )
        msg = _msg(id=13, date=self.date, reactions=reactions, replies=None)
        stat = _message_to_post_stat(msg, self.entity, thumbnail_path=None)
        self.assertEqual(stat.reactions_breakdown, {"?": 1})

    def test_text_preview_truncated_at_160_chars(self):
        long_text = "a" * 200
        msg = _msg(id=14, date=self.date, text=long_text)
        stat = _message_to_post_stat(msg, self.entity, thumbnail_path=None)
        self.assertEqual(len(stat.text_preview), 161)  # 160 chars + "…"
        self.assertTrue(stat.text_preview.endswith("…"))
        self.assertEqual(stat.text_preview[:160], "a" * 160)

    def test_text_preview_uses_first_non_blank_line_and_collapses_whitespace(self):
        text = "\n\n   \nfirst   real   line\nsecond line"
        msg = _msg(id=15, date=self.date, text=text)
        stat = _message_to_post_stat(msg, self.entity, thumbnail_path=None)
        self.assertEqual(stat.text_preview, "first real line")

    def test_text_preview_empty_when_text_is_none(self):
        msg = _msg(id=16, date=self.date, text=None)
        stat = _message_to_post_stat(msg, self.entity, thumbnail_path=None)
        self.assertEqual(stat.text_preview, "")

    def test_media_type_photo(self):
        msg = _msg(id=17, date=self.date, photo=object(), grouped_id=None)
        stat = _message_to_post_stat(msg, self.entity, thumbnail_path=None)
        self.assertEqual(stat.media_type, "photo")

    def test_media_type_video(self):
        msg = _msg(id=18, date=self.date, video=object(), grouped_id=None)
        stat = _message_to_post_stat(msg, self.entity, thumbnail_path=None)
        self.assertEqual(stat.media_type, "video")

    def test_media_type_album_takes_priority_over_photo(self):
        msg = _msg(id=19, date=self.date, photo=object(), grouped_id=98765)
        stat = _message_to_post_stat(msg, self.entity, thumbnail_path=None)
        self.assertEqual(stat.media_type, "album")

    def test_media_type_album_takes_priority_over_video(self):
        msg = _msg(id=20, date=self.date, video=object(), grouped_id=98765)
        stat = _message_to_post_stat(msg, self.entity, thumbnail_path=None)
        self.assertEqual(stat.media_type, "album")

    def test_media_type_none_for_plain_text(self):
        msg = _msg(id=21, date=self.date, text="just text", grouped_id=None)
        stat = _message_to_post_stat(msg, self.entity, thumbnail_path=None)
        self.assertEqual(stat.media_type, "none")

    def test_media_type_other_for_document(self):
        msg = _msg(
            id=22,
            date=self.date,
            grouped_id=None,
            document=SimpleNamespace(mime_type="application/pdf"),
            file=SimpleNamespace(name="report.pdf"),
        )
        stat = _message_to_post_stat(msg, self.entity, thumbnail_path=None)
        self.assertEqual(stat.media_type, "other")

    def test_thumbnail_path_is_passed_through(self):
        thumb = Path("some/thumb.jpg")
        msg = _msg(id=23, date=self.date)
        stat = _message_to_post_stat(msg, self.entity, thumbnail_path=thumb)
        self.assertEqual(stat.thumbnail_path, thumb)


class FetchPostStatsTests(unittest.IsolatedAsyncioTestCase):
    """Optional light coverage of fetch_post_stats using a minimal fake client --
    _message_to_post_stat above is the important pure-mapper coverage."""

    async def test_stops_before_start_and_skips_bots_and_service_messages(self):
        import post_stats

        entity = SimpleNamespace(username="chan", id=1)
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        end = datetime(2026, 8, 3, tzinfo=timezone.utc)

        real_msg = _msg(
            id=1,
            date=datetime(2026, 8, 2, tzinfo=timezone.utc),
            text="a real post",
            action=None,
            # fetch_post_stats calls telegram_fetch.is_image_message/is_video_message
            # directly on messages that survive the service/bot skips (see
            # telegram_fetch.py -- those check msg.photo/video/document as plain
            # attributes, unlike this module's own defensive getattr mapping), so this
            # is the one stub in the suite that needs them present.
            photo=None,
            video=None,
            document=None,
        )
        bot_msg = _msg(
            id=2,
            date=datetime(2026, 8, 2, 1, tzinfo=timezone.utc),
            text="bot reply",
            action=None,
        )
        service_msg = _msg(
            id=3,
            date=datetime(2026, 8, 2, 2, tzinfo=timezone.utc),
            text=None,
            action=object(),
        )
        too_old_msg = _msg(
            id=4,
            date=datetime(2026, 7, 31, tzinfo=timezone.utc),
            text="too old",
            action=None,
        )

        class FakeClient:
            async def iter_messages(self, ent, offset_date, reverse):
                for m in (bot_msg, service_msg, real_msg, too_old_msg):
                    yield m

        # Telethon's real Message.get_sender() is a bound method; the stub attaches
        # an equivalent per-message closure instead of a shared client-level one.
        async def real_sender():
            return SimpleNamespace(bot=False)

        async def bot_sender():
            return SimpleNamespace(bot=True)

        real_msg.get_sender = real_sender
        bot_msg.get_sender = bot_sender
        service_msg.get_sender = real_sender
        too_old_msg.get_sender = real_sender

        with tempfile.TemporaryDirectory() as tmp:
            results = await post_stats.fetch_post_stats(
                FakeClient(), entity, start, end, thumb_dir=Path(tmp) / "thumbs", log=lambda *a: None,
            )
        self.assertEqual([r.message_id for r in results], [1])


if __name__ == "__main__":
    unittest.main()
