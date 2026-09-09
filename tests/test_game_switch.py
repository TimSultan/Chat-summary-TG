"""GAME_ENABLED=0 -- the pet game closed, quests still running.

The switch exists for one measured reason: `pets_web` builds the whole Mini App as
module-level strings and costs about 17 MB of the process's ~91 MB. Not importing it is
the entire saving, so the first thing pinned here is that the flag really does skip that
import -- in a fresh interpreter, because a module cannot be un-imported and an in-process
assertion would prove nothing.

The rest is the promise that came with it: closing the game must not close the quest
queue. Somebody painted a model and is waiting to hear whether it counted, and that is a
different thing from wanting to fight in an arena.
"""

import inspect
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot_listener
import pets_ui


def _in_fresh_process(flag: str | None, body: str) -> str:
    """Import the bot with GAME_ENABLED set to `flag` (or removed, for None), then print
    whatever `body` asks. A fresh interpreter because a module cannot be un-imported."""
    lines = ["import os, sys"]
    if flag is None:
        lines.append("os.environ.pop('GAME_ENABLED', None)")
    else:
        lines.append(f"os.environ['GAME_ENABLED'] = {flag!r}")
    lines += [f"sys.path.insert(0, {str(ROOT)!r})", "import bot_listener", body]
    result = subprocess.run(
        [sys.executable, "-c", chr(10).join(lines)],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.strip()[-800:])
    return result.stdout.strip()


class ImportTests(unittest.TestCase):
    """The saving itself. Everything else here is about not breaking quests while taking it."""

    def test_the_mini_app_is_not_imported_when_the_game_is_off(self):
        self.assertEqual(
            _in_fresh_process("0", "print('pets_web' in sys.modules)"), "False",
        )

    def test_it_is_imported_when_the_game_is_on(self):
        self.assertEqual(
            _in_fresh_process("1", "print('pets_web' in sys.modules)"), "True",
        )

    def test_the_default_is_on(self):
        """An unset variable leaves the game exactly as it was.

        The default belongs to the deployment, not to the repository -- the same call
        STATS_ENABLED makes. Checked in a fresh process with GAME_ENABLED removed from the
        environment rather than by reading the module's flag, so this passes whatever the
        developer running it has in their own shell.
        """
        self.assertEqual(
            _in_fresh_process(None, "print(bot_listener.GAME_ENABLED, bot_listener.game_open())"),
            "True True",
        )

    def test_one_variable_closes_it(self):
        self.assertEqual(
            _in_fresh_process("0", "print(bot_listener.GAME_ENABLED, bot_listener.game_open())"),
            "False False",
        )

    def test_quests_still_have_the_modules_they_need(self):
        """`quests` imports `pets`, and review draws `pets_ui` screens. Closing the game
        may not take those away -- only the Mini App goes."""
        self.assertEqual(
            _in_fresh_process(
                "0",
                "print(bot_listener.game_available(), bot_listener.game_open(),"
                " bot_listener.pets is not None, bot_listener.pets_ui is not None)",
            ),
            "True False True True",
        )

    def test_the_flag_reads_the_spellings_people_actually_use(self):
        for raw, expected in (
            ("0", False), ("false", False), ("off", False), ("no", False), ("", True),
            ("1", True), ("true", True), ("on", True), ("yes", True), ("YES", True),
        ):
            with self.subTest(raw=raw), patch.dict("os.environ", {"GAME_ENABLED": raw}):
                self.assertIs(bot_listener._env_flag("GAME_ENABLED", True), expected)

    def test_an_unset_flag_keeps_the_default(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertTrue(bot_listener._env_flag("GAME_ENABLED", True))
            self.assertFalse(bot_listener._env_flag("GAME_ENABLED", False))


class ClosedGameTests(unittest.TestCase):
    def setUp(self):
        # The flag is read once at import; these pin the behaviour it selects, not the read.
        patcher = patch.object(bot_listener, "GAME_ENABLED", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_closed_game_is_not_a_broken_one(self):
        self.assertTrue(bot_listener.game_available())
        self.assertFalse(bot_listener.game_open())

    def test_players_are_told_it_is_closed_rather_than_being_repaired(self):
        """"чиню" is a promise it comes back today. A closed game makes no such promise."""
        notice = bot_listener._game_down_notice(False)
        self.assertEqual(notice, bot_listener.GAME_CLOSED_NOTICE)
        self.assertNotEqual(notice, bot_listener.GAME_UNAVAILABLE_NOTICE)
        self.assertIn("квест", notice.lower(), "the one thing still working goes unmentioned")
        self.assertIn("GAME_ENABLED", bot_listener._game_down_reason(False))

    def test_a_genuinely_broken_game_still_says_it_is_being_fixed(self):
        with patch.object(bot_listener, "GAME_IMPORT_ERROR", "Traceback: boom"):
            self.assertEqual(
                bot_listener._game_down_notice(False), bot_listener.GAME_UNAVAILABLE_NOTICE,
            )
            self.assertIn("boom", bot_listener._game_down_reason(False))

    def test_the_weekly_vote_is_a_separate_switch(self):
        # arena* is the #итогинедели vote, not the game. Closing one may not close it.
        self.assertTrue(bot_listener.arena_available())
        self.assertEqual(
            bot_listener._game_down_notice(True), bot_listener.ARENA_UNAVAILABLE_NOTICE,
        )

    def test_no_mini_app_link_is_offered(self):
        cfg = type("Cfg", (), {"webapp_public_url": "https://example.com"})()
        with patch.object(bot_listener, "pets_web", None):
            self.assertIsNone(bot_listener._pets_page_url(cfg))

    def test_the_quest_alert_keeps_its_telegram_button_and_drops_the_web_one(self):
        submission = {"id": "s1", "quest_code": "nmm", "author_name": "Аня"}
        _, keyboard = pets_ui.quest_submission_notification_view("42", submission, None)
        buttons = [b for row in keyboard["inline_keyboard"] for b in row]
        self.assertEqual(len(buttons), 1)
        self.assertIn("callback_data", buttons[0])
        self.assertNotIn("web_app", buttons[0])
        # And with a URL -- the game open -- both surfaces are offered, as before.
        _, open_keyboard = pets_ui.quest_submission_notification_view(
            "42", submission, "https://example.com/pets",
        )
        self.assertEqual(len([b for row in open_keyboard["inline_keyboard"] for b in row]), 2)


class DeclineTests(unittest.TestCase):
    """What a player actually gets back, rather than what a flag says."""

    def setUp(self):
        patcher = patch.object(bot_listener, "GAME_ENABLED", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_play_command_is_refused_with_the_closed_notice(self):
        import asyncio

        sent = []

        class Api:
            async def send_message(self, chat_id, text, reply_to_message_id=None,
                                   parse_mode=None):
                sent.append(text)
                return {"message_id": 1}

        message = {"chat": {"id": 555, "type": "private"}, "message_id": 5}
        declined = asyncio.run(
            bot_listener._decline_game_command(Api(), message, log=lambda *a: None)
        )
        self.assertTrue(declined)
        self.assertEqual(sent, [bot_listener.GAME_CLOSED_NOTICE])

    def test_a_play_button_is_refused_as_a_toast(self):
        import asyncio

        answered = []

        class Api:
            async def answer_callback_query(self, callback_id, text=None):
                answered.append(text)

        declined = asyncio.run(
            bot_listener._decline_game_callback(
                Api(), {"id": "cb"}, log=lambda *a: None,
            )
        )
        self.assertTrue(declined)
        self.assertEqual(answered, [bot_listener.GAME_CLOSED_NOTICE])

    def test_nothing_is_refused_while_the_game_is_open(self):
        import asyncio

        with patch.object(bot_listener, "GAME_ENABLED", True):
            self.assertFalse(asyncio.run(bot_listener._decline_game_callback(
                object(), {"id": "cb"}, log=lambda *a: None,
            )))


class AllowListTests(unittest.TestCase):
    """Which buttons survive the close, and the drift that would quietly break review."""

    def test_every_quest_action_the_menu_handles_is_allowed_through(self):
        source = inspect.getsource(bot_listener.handle_pets_callback)
        handled = {
            "questreview", "questaccept", "questreject",
            "questmods", "questmodadd", "questmoddel",
        }
        for action in handled:
            with self.subTest(action=action):
                self.assertIn(action, source, "action no longer handled -- update the set")
                self.assertIn(action, bot_listener.QUEST_MODERATION_ACTIONS)

    def test_nothing_that_reads_as_play_is_allowed_through(self):
        for action in ("fight", "farm", "dungeon", "casino", "cage", "train", "shop",
                       "main", "quests", "questdetail"):
            with self.subTest(action=action):
                self.assertNotIn(action, bot_listener.QUEST_MODERATION_ACTIONS)

    def test_the_close_is_narrower_than_a_pause(self):
        """A pause is a few minutes mid-deploy and keeps navigation open; a close is
        indefinite and must not advertise a game nobody can play."""
        self.assertLess(
            len(bot_listener.QUEST_MODERATION_ACTIONS),
            len(bot_listener.PAUSE_SAFE_PET_ACTIONS),
        )
        self.assertNotIn("main", bot_listener.QUEST_MODERATION_ACTIONS)
        self.assertIn("main", bot_listener.PAUSE_SAFE_PET_ACTIONS)


class WiringTests(unittest.TestCase):
    """Source-level, like tests/test_feature_isolation.py: what these pin is a shape that
    no in-process call can reach, and getting it wrong costs RAM or pays out prizes into a
    game nobody can open."""

    def test_the_game_s_clocks_only_run_while_it_is_open(self):
        source = inspect.getsource(bot_listener.run_bot_listener)
        opened = source.split("if game_open():")[1].split("else:")[0]
        self.assertIn("_farm_returns_loop()", opened)
        self.assertIn("_daily_chatter_prize_loop()", opened)
        # And nowhere else -- an unconditional copy would undo the whole branch.
        self.assertEqual(source.count("tasks.append(_farm_returns_loop())"), 1)
        self.assertEqual(source.count("_daily_chatter_prize_loop()"), 2)  # def + the one call

    def test_the_mini_app_is_attached_only_when_it_was_imported(self):
        source = inspect.getsource(bot_listener.run_bot_listener)
        self.assertIn("if pets_web is None:", source)

    def test_the_callback_door_checks_for_modules_not_for_an_open_game(self):
        # Reversing these two is exactly how closing the game would close quest review.
        source = inspect.getsource(bot_listener.handle_pets_callback)
        self.assertIn("if not game_available() and await _decline_game_callback", source)
        self.assertIn("action not in QUEST_MODERATION_ACTIONS", source)


if __name__ == "__main__":
    unittest.main()
