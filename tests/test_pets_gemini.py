"""The Gemini boundary, and the disk store that caches what it returns.

The rule the whole feature rests on: nothing here may raise. A sprite is decoration on a
battle screen, so every failure -- no package, no key, a dropped connection, a reply that
is not JSON, a blob that is not an image -- has to fold into "animate the photograph
plainly", which is what the arena did before any of this existed. A test suite that only
proved the happy path would be proving the least important half.

No network, ever: the SDK is patched out entirely.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pets_gemini
import pets_sprite
import pets_sprite_store

PNG = b"\x89PNG\r\n\x1a\n" + b"generated pixels"
JPEG = b"\xff\xd8\xff" + b"generated pixels"
PHOTO = b"\xff\xd8\xff" + b"the player's own photograph"


def _reply(text=None, image=None):
    """A stand-in for the SDK's response object, shaped the way the real one is."""
    inline = SimpleNamespace(data=image, mime_type="image/png") if image else None
    part = SimpleNamespace(inline_data=inline)
    candidate = SimpleNamespace(content=SimpleNamespace(parts=[part]))
    return SimpleNamespace(text=text, candidates=[candidate])


class FakeModels:
    """Records every call so a test can assert on what was actually sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0) if self.replies else _reply()
        if isinstance(reply, Exception):
            raise reply
        return reply


class GeminiTestCase(unittest.TestCase):
    def _run(self, replies):
        models = FakeModels(replies)
        client = SimpleNamespace(models=models)
        patcher = patch.object(pets_gemini, "genai", SimpleNamespace(Client=lambda **k: client))
        patcher.start()
        self.addCleanup(patcher.stop)
        return models


class AnalyseTests(GeminiTestCase):
    def test_a_good_reply_gives_back_the_archetype_and_the_subject(self):
        self._run([_reply(text='{"archetype": "quadruped", "subject": "a snarling grey wolf"}')])
        self.assertEqual(
            pets_gemini.analyse(PHOTO, api_key="k", model="m"),
            {"archetype": "quadruped", "subject": "a snarling grey wolf"},
        )

    def test_a_reply_wrapped_in_a_markdown_fence_still_parses(self):
        """Models add a fence even when told not to. A fenced reply is a correct answer
        wrapped in decoration, and throwing it away would be the wrong read of it."""
        fenced = '```json\n{"archetype": "machine", "subject": "a red battle tank"}\n```'
        self._run([_reply(text=fenced)])
        self.assertEqual(pets_gemini.analyse(PHOTO, api_key="k", model="m")["archetype"], "machine")

    def test_an_archetype_outside_the_vocabulary_falls_back(self):
        """The twelve codes are a contract with the CSS. A thirteenth would have no
        keyframes behind it and would leave that creature frozen for the whole fight."""
        self._run([_reply(text='{"archetype": "kaiju", "subject": "a big lizard"}')])
        result = pets_gemini.analyse(PHOTO, api_key="k", model="m")
        self.assertEqual(result["archetype"], pets_sprite.DEFAULT_ARCHETYPE)
        self.assertEqual(result["subject"], "a big lizard")   # still worth keeping

    def test_every_failure_shape_falls_back_instead_of_raising(self):
        for label, reply in (
            ("not json", _reply(text="I think it is a wolf?")),
            ("json but not an object", _reply(text="[1, 2, 3]")),
            ("empty", _reply(text="")),
            ("sdk error", RuntimeError("upstream is down")),
            ("connection dropped", ConnectionError("reset")),
        ):
            with self.subTest(failure=label):
                self._run([reply])
                self.assertEqual(
                    pets_gemini.analyse(PHOTO, api_key="k", model="m"),
                    {"archetype": pets_sprite.DEFAULT_ARCHETYPE, "subject": ""},
                )

    def test_an_oversized_photo_is_refused_without_calling_anything(self):
        models = self._run([_reply(text='{"archetype": "bird", "subject": "x"}')])
        huge = b"x" * (pets_gemini.MAX_IMAGE_BYTES + 1)
        self.assertEqual(pets_gemini.analyse(huge, api_key="k", model="m")["archetype"],
                         pets_sprite.DEFAULT_ARCHETYPE)
        self.assertEqual(models.calls, [])


class GenerateFramesTests(GeminiTestCase):
    def test_all_three_frames_come_back_keyed_by_name(self):
        self._run([_reply(image=PNG), _reply(image=PNG), _reply(image=JPEG)])
        frames = pets_gemini.generate_frames(PHOTO, api_key="k", model="m")
        self.assertEqual(sorted(frames), ["attack", "idle_a", "idle_b"])

    def test_one_frame_failing_still_returns_the_others(self):
        """Two idle frames are a complete breathing loop. Discarding them because the
        attack pose failed would throw away a working sprite over a missing flourish."""
        self._run([_reply(image=PNG), RuntimeError("refused"), _reply(image=PNG)])
        frames = pets_gemini.generate_frames(PHOTO, api_key="k", model="m")
        self.assertEqual(sorted(frames), ["attack", "idle_a"])

    def test_a_blob_that_is_not_an_image_is_rejected(self):
        """Checked by magic bytes, not by the declared mime type: the declaration is the
        model's claim about the payload, and a corrupt file on disk is worse than a
        missing one because nothing later would ever re-check it."""
        self._run([_reply(image=b"<html>error page</html>"), _reply(image=PNG), _reply()])
        self.assertEqual(sorted(pets_gemini.generate_frames(PHOTO, api_key="k", model="m")),
                         ["idle_b"])

    def test_an_oversized_frame_is_rejected(self):
        self._run([_reply(image=PNG[:8] + b"x" * pets_gemini.MAX_FRAME_BYTES)])
        self.assertEqual(pets_gemini.generate_frames(PHOTO, api_key="k", model="m",
                                                    frames=("idle_a",)), {})

    def test_every_call_is_given_the_original_photograph(self):
        """Feeding a generated frame back in would compound its errors, and the character
        would drift a little further from the player's own paintwork with each pose."""
        models = self._run([_reply(image=PNG), _reply(image=PNG), _reply(image=PNG)])
        pets_gemini.generate_frames(PHOTO, api_key="k", model="m")
        self.assertEqual(len(models.calls), 3)
        for call in models.calls:
            self.assertEqual(call["contents"][0].inline_data.data, PHOTO)

    def test_the_prompt_carries_the_subject_and_the_archetype(self):
        models = self._run([_reply(image=PNG)])
        pets_gemini.generate_frames(PHOTO, api_key="k", model="m", frames=("attack",),
                                    subject="a red battle tank", archetype="vehicle")
        prompt = models.calls[0]["contents"][1]
        self.assertIn("a red battle tank", prompt)
        self.assertIn("idling engine", prompt)
        self.assertIn("transparent", prompt)


class AvailabilityTests(unittest.TestCase):
    def test_nothing_is_attempted_without_a_key_or_without_the_package(self):
        self.assertFalse(pets_gemini.available(""))
        self.assertFalse(pets_gemini.available("   "))
        with patch.object(pets_gemini, "genai", None):
            self.assertFalse(pets_gemini.available("a-real-key"))
            self.assertEqual(pets_gemini.generate_frames(PHOTO, api_key="k", model="m"), {})
            self.assertEqual(pets_gemini.analyse(PHOTO, api_key="k", model="m")["archetype"],
                             pets_sprite.DEFAULT_ARCHETYPE)


class SpriteStoreTests(unittest.TestCase):
    FILE_ID = "photo-abc"

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch.dict(os.environ, {"DATA_DIR": self._temporary.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)
        self.addCleanup(pets_sprite_store.forget, self.FILE_ID)
        pets_sprite_store.forget(self.FILE_ID)

    def _generate(self, frames, archetype="quadruped"):
        with patch.object(pets_gemini, "available", return_value=True), \
             patch.object(pets_gemini, "analyse",
                          return_value={"archetype": archetype, "subject": "a wolf"}), \
             patch.object(pets_gemini, "generate_frames", return_value=frames):
            return pets_sprite_store.generate(
                PHOTO, self.FILE_ID, api_key="k", vision_model="v", image_model="i",
                log=lambda *a: None,
            )

    def test_frames_are_written_and_the_manifest_names_them_in_play_order(self):
        """Ordered as pets_gemini declares them, not as the dict happened to iterate: the
        browser plays the list as given, and idle_b before idle_a is a backwards breath."""
        written = self._generate({"attack": PNG, "idle_b": PNG, "idle_a": PNG})
        self.assertEqual(written["frames"], ["idle_a", "idle_b", "attack"])
        self.assertEqual(pets_sprite_store.read_manifest(self.FILE_ID)["frames"],
                         ["idle_a", "idle_b", "attack"])
        for name in ("idle_a", "idle_b", "attack"):
            self.assertTrue(pets_sprite_store.frame_path(self.FILE_ID, name).is_file())

    def test_a_partial_generation_still_leaves_a_usable_sprite(self):
        written = self._generate({"idle_a": PNG, "idle_b": PNG})
        self.assertEqual(written["frames"], ["idle_a", "idle_b"])

    def test_generating_nothing_writes_no_manifest_but_keeps_the_archetype(self):
        """The archetype is worth having even with no frames: it picks which CSS idle
        animates the raw photograph, which is what the whole feature degrades to."""
        written = self._generate({}, archetype="machine")
        self.assertEqual(written["frames"], [])
        self.assertEqual(written["archetype"], "machine")
        self.assertEqual(pets_sprite_store.read_manifest(self.FILE_ID), {})

    def test_a_manifest_naming_a_missing_file_reads_as_empty(self):
        """Otherwise the page would request a 404 for that frame on every single render."""
        self._generate({"idle_a": PNG, "idle_b": PNG})
        pets_sprite_store.frame_path(self.FILE_ID, "idle_b").unlink()
        self.assertEqual(pets_sprite_store.read_manifest(self.FILE_ID)["frames"], ["idle_a"])

    def test_a_corrupt_manifest_reads_as_empty_rather_than_serving_wreckage(self):
        self._generate({"idle_a": PNG})
        path = pets_sprite_store.sprite_dir(self.FILE_ID) / pets_sprite_store.MANIFEST_NAME
        path.write_text("{ this was truncated mid-", encoding="utf-8")
        self.assertEqual(pets_sprite_store.read_manifest(self.FILE_ID), {})

    def test_a_different_photograph_gets_its_own_directory(self):
        """A file id is a content identity, so a changed picture regenerates by
        construction -- there is no cache to invalidate and no staleness to detect."""
        self.assertNotEqual(pets_sprite_store.sprite_dir("photo-abc"),
                            pets_sprite_store.sprite_dir("photo-xyz"))

    def test_only_one_job_may_claim_a_photograph(self):
        """The battle screen asks about both fighters every time it opens. Without this a
        slow first generation would be started again by every poll while it was working."""
        self.assertTrue(pets_sprite_store.claim(self.FILE_ID))
        self.assertFalse(pets_sprite_store.claim(self.FILE_ID))
        pets_sprite_store.forget(self.FILE_ID)
        self.assertTrue(pets_sprite_store.claim(self.FILE_ID))

    def test_nothing_happens_at_all_without_a_key(self):
        with patch.object(pets_gemini, "generate_frames") as generate:
            self.assertEqual(
                pets_sprite_store.generate(PHOTO, self.FILE_ID, api_key="",
                                           vision_model="v", image_model="i",
                                           log=lambda *a: None),
                {},
            )
        generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
