"""What counts as asking for a summary.

Every match here costs an OpenAI call in a live chat, so these tests are deliberately
adversarial about the near-misses: the command quoted mid-sentence, a longer word that
starts with it, and the bare word without its slash. Those all used to fire a real
request, and in a busy chat that is a bill nobody chose to run up.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from listener import has_trigger_keyword, matched_trigger_keyword, strip_trigger_keywords

KEYWORDS = ["/summary"]


class TriggerMatchingTests(unittest.TestCase):
    def test_the_command_on_its_own_is_the_invocation(self):
        for text in ("/summary", "/summary ", "  /summary", "/Summary", "/SUMMARY"):
            self.assertTrue(has_trigger_keyword(text, KEYWORDS), text)

    def test_a_question_after_the_command_still_invokes_it(self):
        self.assertTrue(has_trigger_keyword("/summary за вчера", KEYWORDS))
        self.assertTrue(has_trigger_keyword("/summary\nкто такой Степан", KEYWORDS))

    def test_telegram_group_spelling_with_the_bot_mention(self):
        """In a group Telegram writes the command as "/summary@my_bot" -- the same ask."""
        self.assertTrue(has_trigger_keyword("/summary@my_bot", KEYWORDS))
        self.assertTrue(has_trigger_keyword("/summary@my_bot за неделю", KEYWORDS))

    def test_not_in_the_first_place_is_not_an_invocation(self):
        # Talking ABOUT the command -- the exact case that used to bill a summary.
        for text in (
            "я написал /summary а он молчит",
            "напиши /summary чтобы получить сводку",
            "а /summary работает?",
            "?/summary",
        ):
            self.assertFalse(has_trigger_keyword(text, KEYWORDS), text)

    def test_the_bare_word_without_a_slash_never_triggers(self):
        for text in ("summary", "summary за вчера", "sultan summary", "SUMMARY!"):
            self.assertFalse(has_trigger_keyword(text, KEYWORDS), text)

    def test_a_longer_word_starting_with_the_command_is_a_different_word(self):
        for text in ("/summarystats", "/summarize за вчера", "/summary_old"):
            self.assertFalse(has_trigger_keyword(text, KEYWORDS), text)

    def test_ordinary_chat_does_not_trigger(self):
        for text in ("", "   ", "привет", "как дела"):
            self.assertFalse(has_trigger_keyword(text, KEYWORDS), text)

    def test_the_matched_keyword_is_reported_back(self):
        self.assertEqual(matched_trigger_keyword("/summary за вчера", KEYWORDS), "/summary")
        self.assertIsNone(matched_trigger_keyword("нет тут ничего", KEYWORDS))

    def test_the_longest_of_several_configured_keywords_wins(self):
        """LISTENER_TRIGGER_KEYWORDS is a list, and two of them can overlap -- the more
        specific one has to be the one stripped, or its tail leaks into the question."""
        keywords = ["/summary", "/summary_full"]
        self.assertEqual(matched_trigger_keyword("/summary_full за вчера", keywords), "/summary_full")
        self.assertEqual(strip_trigger_keywords("/summary_full за вчера", keywords), "за вчера")

    def test_a_non_slash_keyword_still_has_to_open_the_message(self):
        """The keyword list is configurable, so someone may drop the slash -- the
        position rule is what stops that from matching mid-sentence anyway."""
        keywords = ["сводка"]
        self.assertTrue(has_trigger_keyword("сводка за вчера", keywords))
        self.assertFalse(has_trigger_keyword("нужна сводка за вчера", keywords))


class StripTriggerKeywordsTests(unittest.TestCase):
    def test_the_opening_invocation_is_removed(self):
        self.assertEqual(strip_trigger_keywords("/summary кто такой Степан", KEYWORDS),
                         "кто такой Степан")
        self.assertEqual(strip_trigger_keywords("  /summary   за вчера ", KEYWORDS), "за вчера")

    def test_the_bot_mention_goes_with_it(self):
        self.assertEqual(strip_trigger_keywords("/summary@my_bot за вчера", KEYWORDS), "за вчера")

    def test_a_bare_invocation_leaves_no_question(self):
        self.assertEqual(strip_trigger_keywords("/summary", KEYWORDS), "")

    def test_the_same_word_later_in_the_question_survives(self):
        """It is part of what the person asked, not the invocation -- stripping every
        occurrence used to mangle the prompt the model was given."""
        self.assertEqual(
            strip_trigger_keywords("/summary что такое /summary", KEYWORDS),
            "что такое /summary",
        )

    def test_text_that_never_invoked_anything_is_returned_as_is(self):
        self.assertEqual(strip_trigger_keywords("кто написал /summary", KEYWORDS),
                         "кто написал /summary")


if __name__ == "__main__":
    unittest.main()
