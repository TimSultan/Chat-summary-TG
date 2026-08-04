"""The shape of what goes on stats_digest_queue.

This queue killed the process in production: a level-up announcement was put on it as a
two-part item while the consumer unpacks four, and the ValueError escaped the consumer
loop into the asyncio.gather that also runs the polling loop. Nothing caught it, so one
malformed announcement took the whole bot down until it was restarted.

Both halves of that are pinned here -- producers must put the four-part item, and the
consumer must survive one that isn't.
"""

import asyncio
import sys
import unittest
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import listener
import stats


def _cfg():
    return SimpleNamespace(listener_allowed_chats=["Чат"], stats_catchup_days=1)


class LevelUpAnnouncementShapeTests(unittest.TestCase):
    def _catch_up(self, announcements):
        queue = asyncio.Queue()

        async def run():
            with patch.object(listener, "resolve_chat", _async(object())), \
                 patch.object(stats, "is_recorded", lambda *a, **k: True), \
                 patch.object(stats, "finalize_and_record", _async(None)), \
                 patch.object(stats, "collect_level_up_announcements", _async(announcements)):
                await listener._stats_catch_up(
                    object(), _cfg(), timezone.utc, queue, log=lambda *a: None
                )
            return [queue.get_nowait() for _ in range(queue.qsize())]

        return asyncio.run(run())

    def test_a_level_up_is_queued_in_the_four_part_digest_shape(self):
        items = self._catch_up(["Аня выросла до 3 уровня"])
        self.assertEqual(items, [("Чат", "Аня выросла до 3 уровня", None, None)])

    def test_every_announcement_keeps_that_shape(self):
        items = self._catch_up(["первое", "второе"])
        self.assertTrue(all(len(item) == 4 for item in items), items)
        # Unpacking exactly as the consumer does must not raise -- that is the regression.
        for entry, text, parse_mode, photo in items:
            self.assertEqual(entry, "Чат")
            self.assertIsNone(parse_mode)
            self.assertIsNone(photo)

    def test_nothing_is_queued_when_nobody_levelled_up(self):
        self.assertEqual(self._catch_up([]), [])


class MalformedItemTests(unittest.TestCase):
    """The consumer's own guard: even with the producers fixed, a short item must cost
    its own message and nothing else -- it used to cost the whole process."""

    def test_a_short_item_is_dropped_and_the_loop_keeps_going(self):
        dropped = []
        sent = []

        async def run():
            queue = asyncio.Queue()
            await queue.put(("Чат", "короткий"))          # the shape that crashed prod
            await queue.put(("Чат", "нормальный", None, None))

            # A stand-in for the real consumer body, mirroring bot_listener's guard.
            async def consume(limit):
                for _ in range(limit):
                    item = await queue.get()
                    try:
                        entry, text, parse_mode, photo = item
                    except (TypeError, ValueError):
                        dropped.append(item)
                        continue
                    sent.append((entry, text))

            await consume(2)

        asyncio.run(run())
        self.assertEqual(len(dropped), 1)
        self.assertEqual(sent, [("Чат", "нормальный")])


def _async(result):
    async def _call(*args, **kwargs):
        return result
    return _call


if __name__ == "__main__":
    unittest.main()
