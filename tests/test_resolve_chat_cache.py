"""resolve_chat remembers what it has already resolved.

The chat this bot runs in is named in LISTENER_ALLOWED_CHATS by its TITLE, and a title is
the one form get_entity() cannot resolve -- so every lookup fell through to a full
iter_dialogs() walk over the network. Every /stat, /top, /tree and transcript fetch passes
that title as a string, so every one of them paid the walk before doing any work of its
own, which is what made the bot slow to answer.

What's pinned here: the walk happens once, an error is never cached, and an
already-resolved entity is never re-looked-up.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import telegram_fetch
from errors import ChatSummaryError


class FakeClient:
    """Stands in for a Telethon client that cannot resolve a title -- get_entity raises,
    the same as it does for a plain group name, so resolution falls through to dialogs."""

    def __init__(self, dialogs):
        self._dialogs = dialogs
        self.get_entity_calls = 0
        self.dialog_walks = 0

    async def get_entity(self, candidate):
        self.get_entity_calls += 1
        raise ValueError(f"Cannot find any entity corresponding to {candidate!r}")

    def iter_dialogs(self):
        self.dialog_walks += 1
        dialogs = self._dialogs

        async def _walk():
            for dialog in dialogs:
                yield dialog

        return _walk()


def _dialog(name):
    return SimpleNamespace(name=name, entity=SimpleNamespace(id=hash(name), title=name))


class ResolveChatCacheTests(unittest.TestCase):
    def setUp(self):
        # Module-level cache: other tests in the same process must not see this one's
        # entries, and vice versa.
        telegram_fetch._entity_cache.clear()

    tearDown = setUp

    def test_the_dialog_walk_happens_once_per_chat(self):
        client = FakeClient([_dialog("Единый Чат Художников"), _dialog("Другой чат")])

        async def run():
            return [
                await telegram_fetch.resolve_chat(client, "Единый Чат Художников")
                for _ in range(5)
            ]

        entities = asyncio.run(run())
        self.assertEqual(client.dialog_walks, 1, "the account's dialogs were walked more than once")
        self.assertEqual(client.get_entity_calls, 1)
        self.assertEqual(len({id(e) for e in entities}), 1, "callers got different entity objects")

    def test_different_chats_are_cached_separately(self):
        client = FakeClient([_dialog("Единый Чат Художников"), _dialog("Другой чат")])

        async def run():
            first = await telegram_fetch.resolve_chat(client, "Единый Чат Художников")
            second = await telegram_fetch.resolve_chat(client, "Другой чат")
            return first, second

        first, second = asyncio.run(run())
        self.assertEqual(client.dialog_walks, 2)
        self.assertNotEqual(first.title, second.title)

    def test_a_chat_that_cannot_be_found_stays_an_error(self):
        # Caching a failure would turn a temporary problem (not joined yet, a typo since
        # fixed in .env) into one that survives until the process is restarted.
        client = FakeClient([_dialog("Единый Чат Художников")])

        async def run():
            for _ in range(2):
                with self.assertRaises(ChatSummaryError):
                    await telegram_fetch.resolve_chat(client, "Нет такого чата")

        asyncio.run(run())
        self.assertEqual(client.dialog_walks, 2)
        self.assertEqual(telegram_fetch._entity_cache, {})

    def test_an_ambiguous_title_stays_an_error(self):
        client = FakeClient([_dialog("Чат Художников"), _dialog("Чат Художников 2")])

        async def run():
            with self.assertRaises(ChatSummaryError):
                await telegram_fetch.resolve_chat(client, "Чат Художников")

        asyncio.run(run())
        self.assertEqual(telegram_fetch._entity_cache, {})

    def test_a_resolvable_username_is_cached_too(self):
        # The @username path never walked the dialogs, but it is still a network round
        # trip per call, and there is no reason to repeat it either.
        entity = SimpleNamespace(id=7, title="Чат", username="chat")

        class ResolvingClient(FakeClient):
            async def get_entity(self, candidate):
                self.get_entity_calls += 1
                return entity

        client = ResolvingClient([])

        async def run():
            for _ in range(3):
                await telegram_fetch.resolve_chat(client, "@chat")

        asyncio.run(run())
        self.assertEqual(client.get_entity_calls, 1)
        self.assertEqual(client.dialog_walks, 0)

    def test_an_empty_chat_ref_is_still_rejected(self):
        async def run():
            with self.assertRaises(ChatSummaryError):
                await telegram_fetch.resolve_chat(FakeClient([]), "   ")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
