"""The Gemini boundary: look at a painted miniature, then draw it as sprite frames.

This module owns the model calls and the prompts, and deliberately nothing else. It does
no storage (pets_sprite_store), no HTTP, no asyncio and no caching -- every function here
is a blocking call the caller runs in a thread. Keeping the boundary that thin is what
lets the rest of the feature be tested without a network at all.

Two jobs, in order:

* :func:`analyse` names what the photograph shows -- one of the twelve archetypes in
  ``pets_sprite`` (which decides how the creature idles) plus a short English description
  of the subject. That description exists only to keep the drawings on-model.
* :func:`generate_frames` redraws that same creature in the poses a flipbook needs, with
  the background taken away.

The whole module is optional. Without the ``google-genai`` package, without a key, or on
any failure at all, it returns nothing and the arena falls back to animating the raw
photograph with CSS -- exactly what it did before any of this existed. There is no error
a battle screen could usefully act on, so none is raised.
"""

from __future__ import annotations

import json

from pets_sprite import ARCHETYPES, DEFAULT_ARCHETYPE

try:
    from google import genai
    from google.genai import types
except ImportError:  # pragma: no cover - the package is optional on purpose
    genai = None
    types = None


# The frames a sprite is made of, in the order the browser plays them. Two idle poses are
# the minimum that reads as breathing rather than as a still, and the third is the only
# pose the fight actually needs on demand.
FRAMES: tuple[str, ...] = ("idle_a", "idle_b", "attack")

# Telegram photos sit comfortably inside any request limit, but a "sent as a document"
# upload can be many megabytes -- the same ceiling critique.py and pets_sprite.py use, for
# the same reason: refuse up front rather than spend a slow request that was never going
# to work.
MAX_IMAGE_BYTES = 8 * 1024 * 1024
# What comes BACK. A generated frame is a few hundred kilobytes; anything at this size is
# a model returning something we do not understand, and writing it to disk would cost real
# space and serve a broken image forever.
MAX_FRAME_BYTES = 12 * 1024 * 1024
# Accepted by magic bytes rather than by the declared mime type: the declaration is the
# model's claim about the payload, and a corrupt file on disk is worse than a missing one
# because nothing later would ever re-check it.
_MAGIC = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpeg"))


ANALYSIS_PROMPT = (
    "You are looking at a photograph of a hand-painted tabletop miniature. "
    "Answer with JSON only, no prose and no markdown fence, with exactly two keys.\n\n"
    '"archetype": which ONE of these best describes the thing in the picture — '
    + ", ".join(ARCHETYPES) + ".\n"
    '"subject": a short English noun phrase naming what it is, six words at most, '
    "describing the creature itself and never the photograph. Good: "
    '"a snarling grey wolf", "a red battle tank", "an armoured space marine in blue". '
    'Bad: "a photo of a model", "a miniature on a desk".\n\n'
    "Judge only what is actually visible. If you cannot tell what it is, answer "
    f'"{DEFAULT_ARCHETYPE}" and describe it as plainly as you can.'
)

# The rules every frame shares. The first paragraph is the entire point of the feature:
# players painted these themselves, and a sprite that is merely "a wolf" rather than
# THEIR wolf is worse than no sprite at all -- it silently throws their work away.
_FRAME_PREAMBLE = (
    "Redraw the creature from this photograph as a game sprite.\n\n"
    "This must be the SAME character as the photograph: the same colours, the same paint "
    "scheme, the same markings, the same armour and equipment, the same proportions. You "
    "are editing this specific painted model, not illustrating the idea of one. Keep "
    "every detail a person would recognise as their own paintwork.\n\n"
    "Remove the background completely: the character alone on a fully transparent "
    "background, with no scenery, no ground, no base, no baked-in shadow, no text, no "
    "border and no frame.\n\n"
    "If the photograph shows a whole body, draw the whole body. If it shows only a head "
    "or a bust, keep it a head or a bust -- do not invent a body that is not there.\n\n"
    "Face to the right. Centre the character, and keep the framing, the scale and the "
    "distance identical in every frame, so that the frames can be played in sequence "
    "without the character jumping or changing size."
)

# One line per frame. idle_b's wording is the fussiest on purpose: a model asked for "a
# different pose" returns a genuinely different drawing, and two different drawings played
# in a loop strobe instead of breathing.
_FRAME_PROMPTS = {
    "idle_a": (
        "Pose: standing at rest. Weight settled, calm, alert but not moving. "
        "This is the neutral frame the others are measured against."
    ),
    "idle_b": (
        "Pose: the SAME resting stance as before, changed only very slightly -- one "
        "breath in. Chest a little fuller, head a fraction higher, weight shifted a "
        "little. This is almost the same drawing, not a new one: if the difference is "
        "obvious, it is too big."
    ),
    "attack": (
        "Pose: lunging forward to the right in a committed attack, caught at the moment "
        "of impact. Body driving forward, weapon or limbs extended toward the target."
    ),
}

# How the archetype colours the motion. A machine does not shift its weight the way a dog
# does, and saying so is most of what makes a generated idle look like the right creature.
_ARCHETYPE_MOTION = {
    "humanoid": "It moves like a person: breathing from the chest, weight on the legs.",
    "quadruped": "It moves like a four-legged animal: the head leads, the body follows.",
    "bird": "It moves like a bird: light, quick, the wings and tail settling.",
    "insect": "It moves like an insect: sharp small movements, legs and antennae twitching.",
    "aquatic": "It moves like something swimming: smooth, weightless, never quite still.",
    "reptile": "It moves like a big reptile: slow, heavy, low to the ground.",
    "blob": "It has no skeleton: it swells and settles like something gelatinous.",
    "machine": "It moves like a machine: rigid joints, servos, nothing organic.",
    "vehicle": "It does not walk: it sits on the ground and vibrates like an idling engine.",
    "plant": "It barely moves at all: it sways like a plant in a light wind.",
    "spirit": "It is weightless and half-transparent: it drifts rather than stands.",
    "creature": "",
}


def available(api_key: str) -> bool:
    """Whether this module can do anything: the package is installed and a key exists."""
    return bool(genai is not None and str(api_key or "").strip())


def _client(api_key: str, timeout: float):
    # Timeout is in milliseconds in this SDK. Passing seconds by mistake would make a
    # 30-second budget into 30 milliseconds and fail every call for no visible reason.
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=int(max(1.0, timeout) * 1000)),
    )


def _looks_like_an_image(blob: bytes) -> bool:
    return any(blob.startswith(magic) for magic, _ in _MAGIC)


def _first_image(response) -> bytes | None:
    """The first real image on a response, or None.

    Walks the parts rather than trusting a convenience accessor: an image model can
    return several parts (a line of commentary and then the picture), and which index the
    picture lands on is not guaranteed.
    """
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            inline = getattr(part, "inline_data", None)
            blob = getattr(inline, "data", None) if inline is not None else None
            if isinstance(blob, (bytes, bytearray)) and blob:
                return bytes(blob)
    return None


def _strip_fence(text: str) -> str:
    """Undo a ```json fence. Models add one even when told not to, and a fenced reply is
    a correct answer wrapped in decoration -- throwing it away would be the wrong read."""
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else ""
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    return cleaned.strip()


def analyse(image: bytes, *, api_key: str, model: str, timeout: float = 30.0) -> dict:
    """Name what the photograph shows: {"archetype": <code>, "subject": <str>}.

    NEVER raises. Every failure returns the neutral archetype and an empty subject, which
    the caller treats as "animate the photograph plainly" rather than as an error.
    """
    blank = {"archetype": DEFAULT_ARCHETYPE, "subject": ""}
    if not available(api_key) or not image or len(image) > MAX_IMAGE_BYTES:
        return blank
    try:
        response = _client(api_key, timeout).models.generate_content(
            model=model,
            contents=[
                types.Part.from_bytes(data=bytes(image), mime_type="image/jpeg"),
                ANALYSIS_PROMPT,
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                # A noun phrase and one word out of twelve. Room to answer, no room to
                # start explaining the reasoning instead.
                max_output_tokens=200,
                temperature=0.0,
            ),
        )
        parsed = json.loads(_strip_fence(getattr(response, "text", "") or ""))
    except Exception:
        # Deliberately everything: an SDK error, a dropped connection, a reply that is not
        # JSON and a reply that is JSON but not an object are all equally survivable, and
        # none of them is worth a traceback reaching a battle screen.
        return blank
    if not isinstance(parsed, dict):
        return blank
    code = str(parsed.get("archetype") or "").strip().lower()
    subject = " ".join(str(parsed.get("subject") or "").split())[:80]
    return {
        "archetype": code if code in ARCHETYPES else DEFAULT_ARCHETYPE,
        "subject": subject,
    }


def frame_prompt(frame: str, *, subject: str = "", archetype: str = DEFAULT_ARCHETYPE) -> str:
    """The full instruction for one frame. Exposed so a test can read it and a person can
    review the wording without running a generation."""
    parts = [_FRAME_PREAMBLE]
    if subject:
        parts.append(f"The character is {subject}.")
    motion = _ARCHETYPE_MOTION.get(archetype, "")
    if motion:
        parts.append(motion)
    parts.append(_FRAME_PROMPTS.get(frame, _FRAME_PROMPTS["idle_a"]))
    return "\n\n".join(parts)


def generate_frames(image: bytes, *, api_key: str, model: str, subject: str = "",
                    archetype: str = DEFAULT_ARCHETYPE, frames=FRAMES,
                    timeout: float = 120.0) -> dict[str, bytes]:
    """Draw the named frames from one photograph. {frame_name: image_bytes}.

    Returns only the frames that actually came back, and never raises. A partial result is
    genuinely useful -- two idle frames are a complete breathing loop, and losing only the
    attack pose costs the lunge its pose change and nothing else -- so one frame failing
    must not discard the ones that worked.

    Every call is given the ORIGINAL photograph, never a previously generated frame.
    Feeding a generation back in would compound its errors, and the character would drift
    a little further from the player's paintwork with each pose.
    """
    if not available(api_key) or not image or len(image) > MAX_IMAGE_BYTES:
        return {}
    try:
        client = _client(api_key, timeout)
    except Exception:
        return {}
    drawn: dict[str, bytes] = {}
    for frame in frames:
        try:
            response = client.models.generate_content(
                model=model,
                contents=[
                    types.Part.from_bytes(data=bytes(image), mime_type="image/jpeg"),
                    frame_prompt(frame, subject=subject, archetype=archetype),
                ],
                config=types.GenerateContentConfig(response_modalities=["IMAGE"]),
            )
            blob = _first_image(response)
        except Exception:
            continue
        if blob and len(blob) <= MAX_FRAME_BYTES and _looks_like_an_image(blob):
            drawn[frame] = blob
    return drawn
