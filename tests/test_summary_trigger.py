"""What counts as asking for a summary.

Every match here costs an OpenAI call in a live chat, so these tests are deliberately
adversarial about the near-misses: the command quoted mid-sentence, a longer word that
starts with it, and the bare word without its slash. Those all used to fire a real
request, and in a busy chat that is a bill nobody chose to run up.

There is nothing to configure -- "/summary" is the command, full stop. It used to come
from LISTENER_TRIGGER_KEYWORDS, and a deployment that had set that variable to "sum" both
billed a summary for any message containing those three letters and, once matching became
position-anchored, lost "/summary" altogether. The last class of test here pins that the
variable is gone for good.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from listener import is_summary_command, strip_summary_command


class TriggerMatchingTests(unittest.TestCase):
    def test_the_command_on_its_own_is_the_invocation(self):
        for text in ("/summary", "/summary ", "  /summary", "/Summary", "/SUMMARY"):
            self.assertTrue(is_summary_command(text), text)

    def test_a_question_after_the_command_still_invokes_it(self):
        self.assertTrue(is_summary_command("/summary за вчера"))
        self.assertTrue(is_summary_command("/summary\nкто такой Степан"))

    def test_telegram_group_spelling_with_the_bot_mention(self):
        """In a group Telegram writes the command as "/summary@my_bot" -- the same ask."""
        self.assertTrue(is_summary_command("/summary@my_bot"))
        self.assertTrue(is_summary_command("/summary@my_bot за неделю"))

    def test_not_in_the_first_place_is_not_an_invocation(self):
        # Talking ABOUT the command -- the exact case that used to bill a summary.
        for text in (
            "я написал /summary а он молчит",
            "напиши /summary чтобы получить сводку",
            "а /summary работает?",
            "?/summary",
        ):
            self.assertFalse(is_summary_command(text), text)

    def test_the_bare_word_without_a_slash_never_triggers(self):
        for text in ("summary", "summary за вчера", "sultan summary", "SUMMARY!"):
            self.assertFalse(is_summary_command(text), text)

    def test_a_longer_word_starting_with_the_command_is_a_different_command(self):
        for text in ("/summarystats", "/summarize за вчера", "/summary_old"):
            self.assertFalse(is_summary_command(text), text)

    def test_the_old_short_trigger_is_dead(self):
        """Production ran with "sum" as the trigger, so every message opening with those
        three letters bought an OpenAI call. Nothing can bring that back."""
        for text in ("sum", "sum за вчера", "sumo", "суммируй"):
            self.assertFalse(is_summary_command(text), text)

    def test_ordinary_chat_does_not_trigger(self):
        for text in ("", "   ", None, "привет", "как дела"):
            self.assertFalse(is_summary_command(text), text)


class NothingIsConfigurableTests(unittest.TestCase):
    """The trigger is not a setting any more, and no environment can change it."""

    def test_the_command_is_a_constant(self):
        self.assertEqual(config.SUMMARY_COMMAND, "/summary")

    def test_the_config_carries_no_summary_trigger_field(self):
        # Other features keep their own keywords (save_trigger_keyword) -- it is
        # specifically the summary trigger that stopped being configurable.
        self.assertNotIn("listener_trigger_keywords", config.Config.__annotations__)

    def test_setting_the_old_variable_changes_nothing(self):
        with patch.dict(os.environ, {"LISTENER_TRIGGER_KEYWORDS": "sum,сводка"}, clear=False):
            self.assertTrue(is_summary_command("/summary за вчера"))
            self.assertFalse(is_summary_command("sum за вчера"))
            self.assertFalse(is_summary_command("сводка за вчера"))


class StripSummaryCommandTests(unittest.TestCase):
    def test_the_opening_invocation_is_removed(self):
        self.assertEqual(strip_summary_command("/summary кто такой Степан"), "кто такой Степан")
        self.assertEqual(strip_summary_command("  /summary   за вчера "), "за вчера")

    def test_the_bot_mention_goes_with_it(self):
        self.assertEqual(strip_summary_command("/summary@my_bot за вчера"), "за вчера")

    def test_a_bare_invocation_leaves_no_question(self):
        self.assertEqual(strip_summary_command("/summary"), "")

    def test_the_same_word_later_in_the_question_survives(self):
        """It is part of what the person asked, not the invocation -- stripping every
        occurrence used to mangle the prompt the model was given."""
        self.assertEqual(
            strip_summary_command("/summary что такое /summary"),
            "что такое /summary",
        )

    def test_text_that_never_invoked_anything_is_returned_as_is(self):
        self.assertEqual(strip_summary_command("кто написал /summary"), "кто написал /summary")


if __name__ == "__main__":
    unittest.main()
