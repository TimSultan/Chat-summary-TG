"""Fixture style (mocking the OpenAI client, never touching the network) copied from how
critique.py's caller is exercised elsewhere in this suite; test naming follows
test_pets_debuff.py's convention of full-sentence names.
"""

import unittest
from unittest.mock import MagicMock, patch

import pets_sprite as sprite

FAKE_IMAGE = b"\xff\xd8\xff not a real jpeg but nonzero bytes"


def _client_returning(content) -> MagicMock:
    """A mock OpenAI() client whose chat.completions.create(...) answers with `content`
    as the model's message text, matching the shape pets_sprite.classify reads."""
    client = MagicMock()
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content=content))]
    client.chat.completions.create.return_value = response
    return client


class ClassifyRoundTripTests(unittest.TestCase):
    def test_every_archetype_code_round_trips_when_the_model_returns_it(self):
        # The twelve codes are a contract with the front-end -- if the model names one
        # exactly, classify must hand it back unchanged, for every single one of them.
        for code in sprite.ARCHETYPES:
            with self.subTest(code=code):
                with patch("pets_sprite.OpenAI", return_value=_client_returning(code)):
                    result = sprite.classify(FAKE_IMAGE, api_key="k", model="m")
                self.assertEqual(result, code)

    def test_whitespace_punctuation_and_case_around_the_reply_are_ignored(self):
        # A vision model padding its one-word answer with a period, shouting-case, or a
        # stray newline is still a right answer -- only an actually different word should
        # ever be treated as a miss.
        for messy in ("  Quadruped.\n", "MACHINE!", "\"spirit\"", "Bird\n"):
            with self.subTest(reply=messy):
                with patch("pets_sprite.OpenAI", return_value=_client_returning(messy)):
                    result = sprite.classify(FAKE_IMAGE, api_key="k", model="m")
                self.assertEqual(result, messy.strip(" \n\"!.").lower())

    def test_an_unknown_word_falls_back_to_creature(self):
        with patch("pets_sprite.OpenAI", return_value=_client_returning("dragonkin")):
            result = sprite.classify(FAKE_IMAGE, api_key="k", model="m")
        self.assertEqual(result, sprite.DEFAULT_ARCHETYPE)

    def test_an_empty_reply_falls_back_to_creature(self):
        with patch("pets_sprite.OpenAI", return_value=_client_returning("")):
            result = sprite.classify(FAKE_IMAGE, api_key="k", model="m")
        self.assertEqual(result, sprite.DEFAULT_ARCHETYPE)

    def test_an_openai_error_falls_back_to_creature_instead_of_propagating(self):
        """A broken vision call must not be able to break a fight: the battle screen has
        no meaningful way to react to an exception, only to a sprite choice."""
        client = MagicMock()
        client.chat.completions.create.side_effect = sprite.OpenAIError("boom")
        with patch("pets_sprite.OpenAI", return_value=client):
            result = sprite.classify(FAKE_IMAGE, api_key="k", model="m")
        self.assertEqual(result, sprite.DEFAULT_ARCHETYPE)

    def test_a_generic_exception_also_falls_back_to_creature_instead_of_propagating(self):
        """Not every transport failure arrives wrapped in OpenAIError -- a raw connection
        error from the underlying HTTP client must be swallowed exactly the same way."""
        client = MagicMock()
        client.chat.completions.create.side_effect = ConnectionError("connection reset")
        with patch("pets_sprite.OpenAI", return_value=client):
            result = sprite.classify(FAKE_IMAGE, api_key="k", model="m")
        self.assertEqual(result, sprite.DEFAULT_ARCHETYPE)

    def test_an_oversized_image_is_refused_without_ever_constructing_a_client(self):
        """The size limit exists to avoid spending a request on something that was never
        going to fit -- so it has to be checked before the client is even built, not
        rediscovered as an API error after the fact."""
        oversized = b"x" * (sprite.MAX_IMAGE_BYTES + 1)
        with patch("pets_sprite.OpenAI") as constructor:
            result = sprite.classify(oversized, api_key="k", model="m")
        constructor.assert_not_called()
        self.assertEqual(result, sprite.DEFAULT_ARCHETYPE)

    def test_empty_image_bytes_are_refused_without_ever_constructing_a_client(self):
        with patch("pets_sprite.OpenAI") as constructor:
            result = sprite.classify(b"", api_key="k", model="m")
        constructor.assert_not_called()
        self.assertEqual(result, sprite.DEFAULT_ARCHETYPE)


class ArchetypeLookupTests(unittest.TestCase):
    def test_every_catalogue_entry_has_a_russian_title_and_hint(self):
        for code, row in sprite.ARCHETYPES.items():
            with self.subTest(code=code):
                self.assertTrue(row["title"])
                self.assertTrue(row["hint"])

    def test_unknown_code_returns_the_creature_entry(self):
        result = sprite.archetype("something-made-up")
        self.assertEqual(result["code"], "creature")
        self.assertEqual(result["title"], sprite.ARCHETYPES["creature"]["title"])
        self.assertEqual(result["hint"], sprite.ARCHETYPES["creature"]["hint"])

    def test_none_also_returns_the_creature_entry(self):
        self.assertEqual(sprite.archetype(None)["code"], "creature")

    def test_a_known_code_returns_its_own_entry_with_the_code_attached(self):
        result = sprite.archetype("machine")
        self.assertEqual(result["code"], "machine")
        self.assertEqual(result["title"], sprite.ARCHETYPES["machine"]["title"])

    def test_lookup_is_case_and_whitespace_insensitive(self):
        # set_photo-style admin tooling or a hand-edited save could plausibly pass this
        # in with different casing; the lookup should not punish that with a fallback.
        self.assertEqual(sprite.archetype(" Machine \n")["code"], "machine")


class CachedArchetypeTests(unittest.TestCase):
    def test_a_matching_photo_file_id_returns_the_stored_code(self):
        record = {
            "photo_file_id": "file-123",
            sprite.SPRITE_KEY: sprite.sprite_row("quadruped", "file-123", "2026-08-11T00:00:00"),
        }
        self.assertEqual(sprite.cached_archetype(record), "quadruped")

    def test_a_changed_photo_file_id_reads_as_a_miss_not_a_stale_answer(self):
        """The whole point of re-deriving the sprite from a new photo is that a new
        picture is a new subject -- serving the old classification here would silently
        show a dog idling like a robot because someone repainted their miniature."""
        record = {
            "photo_file_id": "file-456-new-photo",
            sprite.SPRITE_KEY: sprite.sprite_row("quadruped", "file-123-old-photo", "2026-08-11T00:00:00"),
        }
        self.assertIsNone(sprite.cached_archetype(record))

    def test_no_sprite_row_at_all_is_a_miss(self):
        self.assertIsNone(sprite.cached_archetype({"photo_file_id": "file-123"}))

    def test_none_record_is_a_miss(self):
        self.assertIsNone(sprite.cached_archetype(None))

    def test_a_retired_archetype_code_reads_as_a_miss_rather_than_crashing(self):
        """A row written by an older or newer build must not be able to break the screen
        reading it -- same rule this repo already applies to pets.py's debuffs."""
        record = {
            "photo_file_id": "file-123",
            sprite.SPRITE_KEY: {
                "archetype": "no-longer-a-real-code",
                "photo_file_id": "file-123",
                "at": "2026-08-11T00:00:00",
            },
        }
        self.assertIsNone(sprite.cached_archetype(record))

    def test_sprite_row_shape_carries_exactly_what_the_caller_stores(self):
        row = sprite.sprite_row("bird", "file-789", "2026-08-11T12:00:00")
        self.assertEqual(row, {
            "archetype": "bird",
            "photo_file_id": "file-789",
            "at": "2026-08-11T12:00:00",
        })


if __name__ == "__main__":
    unittest.main()
