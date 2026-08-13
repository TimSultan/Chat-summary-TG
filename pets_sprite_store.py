"""Where generated sprite frames live on disk, and the one job that fills them in.

Split out from :mod:`pets_sprite` (which owns the archetype vocabulary and the OpenAI
classifier) and from :mod:`pets_gemini` (which owns the model calls) because this is the
only part that touches the filesystem and the only part that has to think about two
requests arriving at once. Keeping it separate is what lets the model boundary stay a set
of pure blocking functions with no state.

Everything here is keyed on the PHOTOGRAPH, never on the player. A file id is already a
content identity -- Telegram issues a new one for a new upload -- so a changed picture
lands in a different directory and regenerates by construction, and two players who
somehow share a photo share one set of frames. The same reasoning the portrait cache in
pets_web uses, for the same reason.

Generation is deliberately fire-and-forget. It is four model calls and takes tens of
seconds, so nothing waits for it: a battle opens on whatever is ready, and the frames
appear on a later look. The failure mode of the whole feature is "the arena animates the
photograph", which is exactly what it did before any of this existed.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

import pets_gemini
import pets_sprite

# One directory per photograph, holding the frames plus a small manifest. The manifest is
# what makes a half-written directory legible: frames are written one at a time as the
# model returns them, so "which of these exist" cannot be inferred from the directory
# alone without racing a job that is still running.
MANIFEST_NAME = "sprite.json"
FRAME_SUFFIX = ".png"

# Jobs already running or already finished in THIS process. Generation is idempotent but
# not free, and the battle screen asks about both fighters every time it opens, so without
# this a slow first job would be started again by every poll while it was still working.
# Deliberately in memory rather than on disk: a restart should retry a photo whose
# generation crashed, and an empty set on boot is exactly that behaviour.
_claimed: set[str] = set()
_claim_lock = threading.Lock()


def sprite_dir(file_id: str) -> Path:
    """Where one photograph's frames live. Hashed, like the portrait cache, so a Telegram
    file id -- which is long, opaque and not guaranteed to be a legal filename -- never
    reaches the filesystem."""
    digest = hashlib.sha256(str(file_id).encode("utf-8")).hexdigest()[:32]
    return Path(os.getenv("DATA_DIR", ".")) / "pets" / "sprites" / digest


def frame_path(file_id: str, frame: str) -> Path:
    return sprite_dir(file_id) / f"{frame}{FRAME_SUFFIX}"


def read_manifest(file_id: str) -> dict:
    """What is on disk for this photograph. An empty dict means nothing usable.

    Tolerates a truncated or corrupt manifest by treating it as absent: a half-written
    file is a job that died mid-flight, and the right response is to generate again
    rather than to serve whatever survived.
    """
    path = sprite_dir(file_id) / MANIFEST_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    # A manifest naming frames that are no longer on disk is worse than no manifest: the
    # page would request a 404 for every one of them, once per render.
    frames = [
        name for name in data.get("frames", [])
        if isinstance(name, str) and frame_path(file_id, name).is_file()
    ]
    return {**data, "frames": frames} if frames else {}


def _write_manifest(file_id: str, payload: dict) -> None:
    directory = sprite_dir(file_id)
    directory.mkdir(parents=True, exist_ok=True)
    # Written last and atomically: a reader that finds the manifest must be able to trust
    # that every frame it names is already complete on disk.
    temporary = directory / (MANIFEST_NAME + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(directory / MANIFEST_NAME)


def claim(file_id: str) -> bool:
    """Take responsibility for generating this photograph's frames. False if somebody has.

    The caller must only start a job when this returns True, and must never release the
    claim on failure -- a photograph the model refuses once will be refused again, and
    retrying it on every poll would spend the budget in a loop. A restart clears the
    claims and is the intended way to retry.
    """
    with _claim_lock:
        if file_id in _claimed:
            return False
        _claimed.add(file_id)
        return True


def forget(file_id: str) -> None:
    """Drop the in-process claim, so the next look tries again. Tests and manual retries."""
    with _claim_lock:
        _claimed.discard(file_id)


def generate(image: bytes, file_id: str, *, api_key: str, vision_model: str,
             image_model: str, log=print) -> dict:
    """Analyse one photograph and generate its frames. Blocking; run it in a thread.

    Returns the manifest it wrote, or an empty dict when nothing usable came back. Writes
    each frame as it arrives so that a run which produces two of three still leaves a
    usable sprite behind -- two frames are a complete idle loop, and the third only costs
    the attack pose.
    """
    if not pets_gemini.available(api_key):
        return {}
    reading = pets_gemini.analyse(image, api_key=api_key, model=vision_model)
    archetype_code = str(reading.get("archetype") or pets_sprite.DEFAULT_ARCHETYPE)
    subject = str(reading.get("subject") or "")
    frames = pets_gemini.generate_frames(
        image, api_key=api_key, model=image_model,
        subject=subject, archetype=archetype_code,
    )
    written = []
    directory = sprite_dir(file_id)
    for name, blob in frames.items():
        try:
            directory.mkdir(parents=True, exist_ok=True)
            frame_path(file_id, name).write_bytes(blob)
            written.append(name)
        except OSError:
            log(f"[pets_sprite_store] could not write frame {name} for {file_id}")
    if not written:
        # The archetype is still worth having even with no frames: it picks which CSS idle
        # animates the raw photograph, which is the fallback this whole module degrades to.
        return {"archetype": archetype_code, "subject": subject, "frames": []}
    payload = {
        "archetype": archetype_code,
        "subject": subject,
        # Ordered as pets_gemini declares them, not as the dict happened to iterate, so
        # the browser's flipbook always plays idle_a before idle_b.
        "frames": [name for name in pets_gemini.FRAMES if name in written],
        "photo_file_id": str(file_id),
    }
    _write_manifest(file_id, payload)
    log(f"[pets_sprite_store] {file_id}: {archetype_code} + {len(written)} frames")
    return payload
