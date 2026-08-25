"""The game and the chat share one process. A bug in one must not silence the other.

Splitting them into two services is the obvious answer and the wrong one: one bot token
cannot be long-polled twice (Telegram answers the second poller with 409), one Telethon
user session cannot be held by two processes without risking the auth key, and the
in-process lock guarding the pet store means nothing across processes. So the two stay in
one process, and what is pinned here is that they fail apart rather than together.
"""

import ast
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot_listener
import listener
import pets_ui

GAME_MODULES = (
    "pets", "pets_combat", "pets_image", "pets_ui", "pets_updates", "pets_web",
    "casino", "arena", "arena_core", "arena_web",
)


class GuardedImportTests(unittest.TestCase):
    def test_the_game_modules_are_imported_behind_a_guard(self):
        self.assertTrue(bot_listener.game_available())
        self.assertTrue(bot_listener.arena_available())
        self.assertIsNone(bot_listener.GAME_IMPORT_ERROR)
        self.assertIsNone(bot_listener.ARENA_IMPORT_ERROR)

    def test_the_two_features_are_guarded_separately(self):
        """A dungeon bug must not close the contest the chat votes in.

        arena* and pets* are different features that happen to share a file. One guard
        over both would have made every pet crash take the weekly duel vote with it.
        """
        source = (ROOT / "bot_listener.py").read_text(encoding="utf-8")
        self.assertIn("GAME_IMPORT_ERROR", source)
        self.assertIn("ARENA_IMPORT_ERROR", source)
        self.assertNotEqual(
            bot_listener.GAME_UNAVAILABLE_NOTICE,
            bot_listener.ARENA_UNAVAILABLE_NOTICE,
        )

    def test_nothing_game_shaped_runs_while_the_module_is_being_read(self):
        """The guard only works because no game module is touched at import time.

        Every reference lives inside a function, so a failed import costs the game its
        commands and costs the chat nothing. A single module-level call -- a constant
        built from a catalogue, a decorator reading a config -- would quietly undo all of
        this and would not fail until production restarted.
        """
        for name in ("bot_listener.py", "listener.py", "quests.py"):
            tree = ast.parse((ROOT / name).read_text(encoding="utf-8"), filename=name)
            for node in tree.body:
                if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef,
                                     ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                for inner in ast.walk(node):
                    if (isinstance(inner, ast.Attribute)
                            and isinstance(inner.value, ast.Name)
                            and inner.value.id in GAME_MODULES):
                        self.fail(
                            f"{name}:{inner.lineno} reaches into {inner.value.id} at "
                            f"module scope, which runs while the file is being imported "
                            f"and defeats the guard around it"
                        )

    def test_the_router_knows_the_pet_prefix_without_the_module_that_defines_it(self):
        """The literal is a deliberate duplication, and this is what keeps it honest.

        pets_ui is exactly the module that may be missing when the dispatcher needs to
        recognise -- and refuse -- a pet button.
        """
        self.assertEqual(bot_listener.PETS_CALLBACK_PREFIX_LITERAL, pets_ui.CALLBACK_PREFIX)
        self.assertIn(
            'callback_data.startswith(f"{PETS_CALLBACK_PREFIX_LITERAL}:")',
            (ROOT / "bot_listener.py").read_text(encoding="utf-8"),
        )

    def test_every_game_entry_point_declines_instead_of_crashing(self):
        """A handler that just raises leaves a button spinning and a command silent."""
        source = (ROOT / "bot_listener.py").read_text(encoding="utf-8")
        tree = ast.parse(source, filename="bot_listener.py")
        wanted = {
            "handle_pets_command", "handle_pet_card_command", "handle_duel_command",
            "handle_arena_news_command", "handle_test_fight_command",
            "handle_pets_rename_command", "handle_arena_command",
            "handle_pets_callback", "handle_arena_action_callback",
            "maybe_handle_pets_flow_message",
        }
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in wanted:
                continue
            wanted.discard(node.name)
            body = ast.get_source_segment(source, node) or ""
            self.assertTrue(
                "_decline_game_command" in body or "_decline_game_callback" in body
                or "game_available()" in body,
                f"{node.name} runs game code without checking the game loaded",
            )
        self.assertFalse(wanted, f"these entry points have vanished: {sorted(wanted)}")


class BrokenGameModuleTests(unittest.TestCase):
    """The whole promise, tested the only way it can be: by actually breaking one.

    Run in a subprocess against a copy of the tree, because the guard runs once at import
    and cannot be re-run inside a process that has already imported everything cleanly.
    """

    def test_a_broken_game_module_leaves_the_chat_importable(self):
        with tempfile.TemporaryDirectory() as temporary:
            copy = Path(temporary) / "app"
            copy.mkdir()
            for path in ROOT.glob("*.py"):
                (copy / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            # A failure every Python version notices, unlike an annotation: 3.14 defers
            # those and would import the "broken" module without complaint.
            broken = copy / "pets_ui.py"
            broken.write_text(
                "raise RuntimeError('deliberately broken by the test')\n"
                + broken.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            probe = textwrap.dedent("""
                import bot_listener, listener
                assert listener is not None
                assert bot_listener.game_available() is False
                assert bot_listener.arena_available() is True
                assert "deliberately broken" in (bot_listener.GAME_IMPORT_ERROR or "")
                print("OK")
            """)
            result = subprocess.run(
                [sys.executable, "-c", probe], cwd=copy, capture_output=True, text=True,
                timeout=180,
            )
            self.assertEqual(
                result.returncode, 0,
                f"the chat could not start with a broken game module:\\n{result.stderr}",
            )
            self.assertIn("OK", result.stdout)


class SupervisorTests(unittest.TestCase):
    """One half falling over must not cancel the other."""

    def test_the_two_halves_are_supervised_rather_than_gathered_bare(self):
        source = (ROOT / "listener.py").read_text(encoding="utf-8")
        self.assertIn("_supervise(", source)
        self.assertIn('_supervise("listener"', source)
        self.assertIn('_supervise("bot_listener"', source)
        self.assertTrue(callable(listener._supervise))

    def test_a_half_that_keeps_dying_is_left_down_rather_than_retried_for_ever(self):
        import asyncio

        attempts = []

        async def always_fails():
            attempts.append(1)
            raise RuntimeError("nope")

        async def run():
            listener.HALF_RESTART_DELAY_SECONDS = 0
            await listener._supervise("test", always_fails, log=lambda *a, **k: None)

        asyncio.run(run())
        self.assertEqual(len(attempts), listener.HALF_RESTART_LIMIT)

    def test_a_half_that_finishes_is_finished(self):
        import asyncio

        calls = []

        async def finishes():
            calls.append(1)

        asyncio.run(listener._supervise("test", finishes, log=lambda *a, **k: None))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
