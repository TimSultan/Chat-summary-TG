"""Self-deleting replies (schedule_bot_delete) -- see bot_listener.

A reply that self-deletes takes the command that asked for it along, so an exchange
either stays in the chat whole or leaves nothing behind. The alternative -- which is
what these tests exist to prevent regressing to -- is a chat littered with orphaned
"/stat" lines quoting answers that are already gone.
"""

import asyncio
import inspect
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_listener

CHAT = -1001234567890


class FakeApi:
    def __init__(self):
        self.deleted = []

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


def _run(coro):
    return asyncio.run(coro)


async def _drain(background_tasks: set):
    """schedule_bot_delete is fire-and-forget -- wait for the tasks it spawned."""
    while background_tasks:
        await asyncio.gather(*list(background_tasks))


class ScheduleBotDeleteTests(unittest.TestCase):
    def _sweep(self, message_ids, trigger_message_id=None, delay=0):
        async def go():
            api = FakeApi()
            background_tasks = set()
            bot_listener.schedule_bot_delete(
                api, CHAT, message_ids, delay, print, background_tasks,
                trigger_message_id=trigger_message_id,
            )
            await _drain(background_tasks)
            return api.deleted

        return _run(go())

    def test_the_prompting_command_goes_with_the_answer(self):
        self.assertEqual(
            self._sweep([200], trigger_message_id=100),
            [(CHAT, 200), (CHAT, 100)],
        )

    def test_a_multi_part_answer_still_sweeps_the_one_command(self):
        """Long summaries are split across several messages by send_long_bot_message."""
        self.assertEqual(
            self._sweep([200, 201, 202], trigger_message_id=100),
            [(CHAT, 200), (CHAT, 201), (CHAT, 202), (CHAT, 100)],
        )

    def test_without_a_trigger_only_our_own_message_goes(self):
        """The dismissal path: a 👍 reaction on something we sent, prompted by no command."""
        self.assertEqual(self._sweep([200]), [(CHAT, 200)])

    def test_the_trigger_is_not_deleted_twice(self):
        self.assertEqual(self._sweep([100], trigger_message_id=100), [(CHAT, 100)])

    def test_the_delay_is_waited_out_before_anything_is_deleted(self):
        async def go():
            api = FakeApi()
            background_tasks = set()
            bot_listener.schedule_bot_delete(
                api, CHAT, [200], 0.05, print, background_tasks, trigger_message_id=100
            )
            self.assertEqual(api.deleted, [])  # nothing goes early
            await _drain(background_tasks)
            return api.deleted

        self.assertEqual(_run(go()), [(CHAT, 200), (CHAT, 100)])

    def test_a_bot_without_delete_rights_does_not_break_the_reply(self):
        """api.delete_message swallows Telegram's error, so losing the sweep is not fatal."""

        class RefusingApi:
            async def delete_message(self, chat_id, message_id):
                return None  # what bot_api.delete_message does on a ChatSummaryError

        async def go():
            background_tasks = set()
            bot_listener.schedule_bot_delete(
                RefusingApi(), CHAT, [200], 0, print, background_tasks, trigger_message_id=100
            )
            await _drain(background_tasks)

        _run(go())  # no exception


class CallSiteTests(unittest.TestCase):
    """Every user-prompted self-deleting reply has to pass its trigger, or that command
    is the one thing left behind."""

    def _call_sites(self, source: str) -> list[str]:
        return [
            m.group(0)
            for m in re.finditer(r"schedule_bot_delete\(.*?\n\s*\)|schedule_bot_delete\([^\n]*\)", source, re.S)
            if not m.group(0).startswith("def ")
        ]

    def test_every_command_handler_sweeps_its_trigger(self):
        for func in (
            bot_listener.handle_tree_command,
            bot_listener.handle_shop_command,
            bot_listener.handle_bot_summary_request,
        ):
            source = inspect.getsource(func)
            with self.subTest(func=func.__name__):
                sites = self._call_sites(source)
                self.assertTrue(sites, f"{func.__name__} no longer self-deletes")
                for site in sites:
                    self.assertIn("trigger_message_id", site)

    def test_the_dismissal_path_passes_no_trigger(self):
        """A 👍 on a bot message is a reaction, not a command -- there is nothing of the
        user's to take with it, and guessing would delete an unrelated message."""
        consumer = inspect.getsource(bot_listener.run_bot_listener)
        consumer = consumer.split("_consume_dismissals")[1].split("tasks = [")[0]
        for site in self._call_sites(consumer):
            self.assertNotIn("trigger_message_id", site)

    def test_the_blocked_file_notice_sweeps_itself_but_takes_nothing_with_it(self):
        """The "files only in DMs" notice goes on its own 30s clock. It must NOT pass a
        trigger: what prompted it is the attachment, which this same consumer deleted a
        few lines earlier -- passing its id would be a second delete of a message that is
        already gone."""
        consumer = inspect.getsource(bot_listener.run_bot_listener)
        consumer = consumer.split("async def _consume_file_blocks")[1].split("async def _consume_stats_digests")[0]
        sites = self._call_sites(consumer)
        self.assertTrue(sites, "the blocked-file notice no longer self-deletes")
        for site in sites:
            self.assertNotIn("trigger_message_id", site)
            self.assertIn("BLOCKED_FILE_NOTICE_DELETE_AFTER", site)


if __name__ == "__main__":
    unittest.main()
