import random
import re
import unittest

import pets_flavor as flavor

# The contract's floor per event -- see PETS_CONTRACT.md. Kept here (not imported from
# the module) so a future accidental trim of VARIANTS fails this test instead of silently
# shipping below spec.
_MIN_VARIANTS = {
    "hit": 40,
    "crit": 30,
    "dodge": 40,
    "blocked": 25,
    "low_damage": 20,
    "opening": 20,
    "victory": 25,
    "round_flavor": 25,
}

# A very small set of the reactions Telegram/most fonts render as emoji-ish glyphs.
# Ranges cover the blocks flavour text could plausibly leak an emoji from.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF"  # arrows, sometimes used as decoration
    "\U00002B00-\U00002BFF"
    "️"
    "]"
)


class EventCoverageTests(unittest.TestCase):
    def test_events_tuple_matches_variants_keys(self):
        self.assertEqual(flavor.EVENTS, tuple(flavor.VARIANTS))

    def test_all_required_events_present(self):
        for event in _MIN_VARIANTS:
            self.assertIn(event, flavor.VARIANTS)

    def test_no_unexpected_events(self):
        # Guards against a typo'd event key nobody notices because line() never asks for it.
        self.assertEqual(set(flavor.VARIANTS), set(_MIN_VARIANTS))

    def test_variant_counts_meet_the_contract_floor(self):
        for event, minimum in _MIN_VARIANTS.items():
            with self.subTest(event=event):
                self.assertGreaterEqual(len(flavor.VARIANTS[event]), minimum)

    def test_variants_are_all_distinct_within_an_event(self):
        # A reskin (same sentence, one word swapped) is a real failure mode here, but the
        # obvious pass -- exact duplicates -- is the mechanical floor a test can check.
        for event, variants in flavor.VARIANTS.items():
            with self.subTest(event=event):
                self.assertEqual(len(variants), len(set(variants)))


class FormattingTests(unittest.TestCase):
    def test_every_template_formats_without_keyerror(self):
        for event, variants in flavor.VARIANTS.items():
            for template in variants:
                with self.subTest(event=event, template=template):
                    template.format(attacker="Мурзик", defender="Плюшка", amount=42)

    def test_line_returns_a_filled_sentence_for_every_event(self):
        rng = random.Random(1)
        for event in flavor.EVENTS:
            with self.subTest(event=event):
                text = flavor.line(event, "Мурзик", "Плюшка", 17, rng=rng)
                self.assertIsInstance(text, str)
                self.assertTrue(text.strip())
                # Nothing was left unformatted.
                self.assertNotIn("{attacker}", text)
                self.assertNotIn("{defender}", text)
                self.assertNotIn("{amount}", text)

    def test_unknown_event_raises(self):
        with self.assertRaises(ValueError):
            flavor.line("nonsense", "Мурзик")

    def test_line_defaults_work_without_defender_or_amount(self):
        # opening/victory/round_flavor callers always pass names, but the signature
        # promises attacker-only callers do not crash.
        text = flavor.line("round_flavor", "Мурзик")
        self.assertIsInstance(text, str)


class NoEmojiTests(unittest.TestCase):
    def test_no_emoji_anywhere_in_any_template(self):
        for event, variants in flavor.VARIANTS.items():
            for template in variants:
                with self.subTest(event=event, template=template):
                    self.assertIsNone(_EMOJI_RE.search(template))


class DeterminismTests(unittest.TestCase):
    def test_seeded_rng_is_reproducible(self):
        seq_a = [
            flavor.line(event, "Мурзик", "Плюшка", 30, rng=random.Random(7))
            for event in flavor.EVENTS
        ]
        seq_b = [
            flavor.line(event, "Мурзик", "Плюшка", 30, rng=random.Random(7))
            for event in flavor.EVENTS
        ]
        self.assertEqual(seq_a, seq_b)

    def test_different_seeds_can_diverge(self):
        # Not a hard guarantee for every single event (a 1-variant event would defeat
        # this), but every event here has well over one variant, so across all of them at
        # least one seed pair should land on a different line.
        seq_a = [
            flavor.line(event, "Мурзик", "Плюшка", 30, rng=random.Random(1))
            for event in flavor.EVENTS
        ]
        seq_b = [
            flavor.line(event, "Мурзик", "Плюшка", 30, rng=random.Random(2))
            for event in flavor.EVENTS
        ]
        self.assertNotEqual(seq_a, seq_b)

    def test_module_random_used_when_rng_omitted(self):
        # Just needs to not blow up and to return a valid line -- module-level random is
        # not itself seeded here.
        text = flavor.line("hit", "Мурзик", "Плюшка", 30)
        self.assertIsInstance(text, str)


if __name__ == "__main__":
    unittest.main()
