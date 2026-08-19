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
    "signature_strength": 2,
    "signature_health": 2,
    "signature_agility": 2,
    "signature_agility_counter": 2,
    "signature_luck": 2,
    "signature_armor": 2,
    "signature_armor_recoil": 2,
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
    def test_accident_pool_has_one_hundred_unique_variants(self):
        self.assertEqual(len(flavor.ACCIDENT_VARIANTS), 100)
        self.assertEqual(len(flavor.ACCIDENT_VARIANTS), len(set(flavor.ACCIDENT_VARIANTS)))

    def test_result_line_has_three_hundred_unique_variants(self):
        self.assertEqual(len(flavor.RESULT_VARIANTS), 300)
        self.assertEqual(len(flavor.RESULT_VARIANTS), len(set(flavor.RESULT_VARIANTS)))

    def test_public_result_pool_has_distinct_variants(self):
        self.assertGreaterEqual(len(flavor.PUBLIC_RESULT_VARIANTS), 30)
        self.assertEqual(len(flavor.PUBLIC_RESULT_VARIANTS), len(set(flavor.PUBLIC_RESULT_VARIANTS)))

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
    def test_accident_line_is_filled(self):
        text = flavor.accident_line("Мурзик", "Плюшка", rng=random.Random(1))
        self.assertIn("Мурзик", text)
        self.assertIn("Плюшка", text)
        self.assertNotIn("{", text)

    def test_result_and_draw_lines_are_filled(self):
        self.assertNotIn("{", flavor.result_line("Мурзик", "Плюшка", rng=random.Random(1)))
        self.assertNotIn("{", flavor.draw_line("Мурзик", "Плюшка", rng=random.Random(1)))

    def test_public_result_line_is_filled(self):
        text = flavor.public_result_line("Мурзик", rng=random.Random(1))
        self.assertIn("Мурзик", text)
        self.assertNotIn("{", text)
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


class TranscriptMarkTests(unittest.TestCase):
    """Whose turn it is and what kind of turn it was: the two things a fight log never
    said outright, so a reader had to infer the actor from a name buried in the prose and
    the kind from the wording."""

    def _every_event_the_engine_emits(self):
        """Fights built to fire as much of the machinery as possible, so the check is
        against what combat ACTUALLY produces rather than against a list somebody kept
        up to date by hand."""
        import random
        import pets_combat
        import pets_scroll_catalog as scrolls

        rng = random.Random(4)
        codes = [row["code"] for row in scrolls.REGULAR_SCROLLS]
        shields = [row["code"] for row in scrolls.SHIELDS]
        passives = ["thorns", "lifesteal", "regen", "ward", "crit_up", "dodge_up",
                    "burn", "venom", "stun", "armor_shred", "double_hit", "combo"]
        seen = set()
        for seed in range(120):
            def fighter(key):
                return pets_combat.Fighter(
                    key=key, name=key, strength=rng.randint(40, 200),
                    health=rng.randint(40, 200), agility=rng.randint(10, 90),
                    luck=rng.randint(10, 90), armor=rng.randint(0, 40),
                    level=rng.randint(5, 40),
                    effects=tuple({"code": code, "value": rng.randint(5, 40)}
                                  for code in rng.sample(passives, 3)),
                    skills=tuple(rng.sample(codes, 4)),
                    shield=scrolls.SHIELD_BY_CODE[rng.choice(shields)],
                )
            for row in pets_combat.simulate(fighter("a"), fighter("b"), seed=seed).rounds:
                seen.add(row.event)
        return seen

    def test_every_event_combat_can_emit_has_a_mark(self):
        unmarked = sorted(
            event for event in self._every_event_the_engine_emits()
            if flavor.event_mark(event) == flavor.EVENT_MARK_DEFAULT
        )
        self.assertFalse(unmarked, f"без метки остались: {unmarked}")

    def test_a_family_of_events_is_covered_by_its_prefix(self):
        """Forty events, not forty table rows: a shield gains a new defend effect without
        anybody having to remember to mark it."""
        for event, expected in (
            ("shield_defend_something_new", "🛡"),
            ("amulet_something_new", "🧿"),
            ("signature_whatever", "🌟"),
            ("deficit_whatever", "📉"),
        ):
            with self.subTest(event=event):
                self.assertEqual(flavor.event_mark(event)[0], expected)

    def test_an_unknown_event_reads_as_a_neutral_dot_rather_than_breaking(self):
        icon, label = flavor.event_mark("something_nobody_has_written_yet")
        self.assertEqual((icon, label), flavor.EVENT_MARK_DEFAULT)
        self.assertTrue(icon and label)
        # None and "" must not blow up a log line either.
        self.assertEqual(flavor.event_mark(None), flavor.EVENT_MARK_DEFAULT)
        self.assertEqual(flavor.event_mark(""), flavor.EVENT_MARK_DEFAULT)

    def test_the_shared_family_is_named_for_what_happened_not_where_it_came_from(self):
        """`skill_reflect` is emitted by a shield's thorns as well as by a spell, so
        calling it «Магия» would be a label that is wrong half the time."""
        self.assertEqual(flavor.event_mark("skill_reflect")[1], "Отражение")
        self.assertEqual(flavor.event_mark("skill_lifesteal")[1], "Вампиризм")
        # Anything else spell-shaped still reads as magic.
        self.assertEqual(flavor.event_mark("skill_meteor")[1], "Магия")

    def test_the_table_ships_to_the_browser_whole(self):
        table = flavor.event_mark_table()
        self.assertEqual(set(table), {"exact", "prefixes", "default"})
        self.assertEqual(table["exact"]["crit"], ["💥", "Крит"])
        self.assertEqual(table["default"], list(flavor.EVENT_MARK_DEFAULT))
        # It has to survive JSON, since that is how it reaches the page.
        import json
        self.assertEqual(json.loads(json.dumps(table)), table)
