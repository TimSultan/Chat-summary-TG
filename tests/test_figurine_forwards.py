"""A #япокрасил post earns a figurine only when the member sent it themselves.

A forward carries the original caption, hashtag included, so a member who reposted
somebody else's painted model used to be credited with it. Both places that count
figurines are pinned here: the live counter in listener.py and the day's recount from
the transcript in stats.compute_day_stats, plus the transcript itself, which has to carry
the forward flag from Telethon through the on-disk cache for the recount to see it.
"""

import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

import listener
import stats
import telegram_fetch

FORWARD_HEADER = SimpleNamespace(from_id=None, from_name="Somebody else")


def _telethon_message(text="#япокрасил дредноут", forwarded=False, message_id=101):
    """Just the attributes the fetchers and the live check read from a Telethon message."""
    return SimpleNamespace(
        id=message_id,
        date=datetime(2026, 10, 4, 12, tzinfo=timezone.utc),
        action=None,
        text=text,
        raw_text=text,
        is_reply=False,
        fwd_from=FORWARD_HEADER if forwarded else None,
        photo=object(),
        video=None,
        document=None,
        video_note=None,
        voice=None,
        gif=None,
        sticker=None,
        contact=None,
        geo=None,
        poll=None,
        file=None,
    )


def _transcript_message(message_id, forwarded=None):
    """A transcript entry as compute_day_stats receives it. forwarded=None leaves the
    attribute off entirely, the shape every test written before forwards had."""
    fields = dict(
        sender_id=20,
        sender_name="Painter",
        sender_username="painter",
        text="[Photo] #япокрасил дредноут",
        dt_local=datetime(2026, 10, 4, 12, message_id % 60, tzinfo=timezone.utc),
        message_id=message_id,
        is_reply=False,
    )
    if forwarded is not None:
        fields["is_forward"] = forwarded
    return SimpleNamespace(**fields)


class LiveCounterTests(unittest.TestCase):
    def test_an_original_post_counts(self):
        message = _telethon_message()
        self.assertTrue(listener.is_figurine_post(message, message.raw_text))

    def test_a_forwarded_post_does_not_count(self):
        message = _telethon_message(forwarded=True)
        self.assertFalse(listener.is_figurine_post(message, message.raw_text))

    def test_the_hashtag_and_the_picture_are_still_both_required(self):
        without_tag = _telethon_message(text="просто дредноут")
        self.assertFalse(listener.is_figurine_post(without_tag, without_tag.raw_text))
        without_picture = _telethon_message()
        without_picture.photo = None
        self.assertFalse(listener.is_figurine_post(without_picture, without_picture.raw_text))


class DayRecountTests(unittest.TestCase):
    def test_a_forward_earns_no_figurine_but_still_counts_as_a_message(self):
        users = stats.compute_day_stats(
            [_transcript_message(101), _transcript_message(102, forwarded=True)]
        )
        painter = users["20"]

        self.assertEqual(painter["figurines"], 1)
        self.assertEqual([post[1] for post in painter["figurine_posts"]], [101])
        # Reposting is still taking part in the chat; only the figurine is refused.
        self.assertEqual(painter["messages"], 2)

    def test_a_message_without_the_flag_is_read_as_an_original(self):
        users = stats.compute_day_stats([_transcript_message(101)])
        self.assertEqual(users["20"]["figurines"], 1)


class TranscriptTests(unittest.TestCase):
    def test_the_fetcher_records_which_messages_were_forwarded(self):
        class FakeClient:
            async def iter_messages(self, entity, **kwargs):
                for message in (
                    _telethon_message(message_id=101),
                    _telethon_message(message_id=102, forwarded=True),
                ):
                    async def get_sender():
                        return SimpleNamespace(id=20, username="painter", bot=False)
                    message.get_sender = get_sender
                    yield message

        _, messages = asyncio.run(
            telegram_fetch.fetch_new_messages(
                FakeClient(), SimpleNamespace(title="chat"), timezone.utc, min_id=100
            )
        )

        self.assertEqual([(m.message_id, m.is_forward) for m in messages], [(101, False), (102, True)])

    def test_the_flag_survives_the_transcript_cache(self):
        message = telegram_fetch.ChatMessage(
            message_id=102,
            dt_local=datetime(2026, 10, 4, 12, tzinfo=timezone.utc),
            sender_name="Painter",
            sender_username="painter",
            sender_id=20,
            text="[Photo] #япокрасил",
            is_reply=False,
            is_forward=True,
        )
        stored = telegram_fetch._message_to_dict(message)

        self.assertTrue(telegram_fetch._message_from_dict(stored).is_forward)

    def test_a_cache_written_before_forwards_were_tracked_still_loads(self):
        stored = telegram_fetch._message_to_dict(
            telegram_fetch.ChatMessage(
                message_id=101,
                dt_local=datetime(2026, 10, 4, 12, tzinfo=timezone.utc),
                sender_name="Painter",
                sender_username="painter",
                sender_id=20,
                text="[Photo] #япокрасил",
                is_reply=False,
            )
        )
        del stored["is_forward"]

        self.assertFalse(telegram_fetch._message_from_dict(stored).is_forward)


if __name__ == "__main__":
    unittest.main()
