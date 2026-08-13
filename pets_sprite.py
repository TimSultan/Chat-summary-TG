"""Owns the fixed archetype vocabulary a pet's idle battle-screen sprite is animated from,
the vision call that reads one photo into it, and the pure caching helpers around that.

The idle animation is supposed to resemble what is actually painted in the member's photo
-- a dog head should idle like a dog, a robot like a machine -- so the Mini App needs a
coarse "what kind of thing is this" label rather than the free-form miniature description
`critique.py` produces. Twelve codes is the entire contract with the front-end; nothing in
this file may return anything else.

Deliberately does NOT: touch storage (the caller persists the returned row on the pet's
record under `SPRITE_KEY` -- see pets.py's photo/debuff handling for the pattern), read a
clock (`sprite_row` takes `at` rather than stamping one, so this module stays free of time
I/O and the caller keeps using the app's own clock), or render or animate anything itself
-- the actual sprite motion lives client-side in the Mini App; this module only tells it
which of the twelve archetypes to play.
"""

import base64

from openai import OpenAI, OpenAIError

# Telegram photos are comfortably inside any request limit, but a full-resolution
# "sent as a document" upload can be many megabytes (see critique.py's identical limit,
# which this mirrors). Refuse up front rather than silently spending a request to
# classify something that was never going to fit -- the fallback below is exactly as good
# a sprite as a slow, possibly-failing upload would have bought.
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# The contract with the front-end: exactly these twelve codes, nothing more. `creature`
# is the deliberate catch-all -- an unrecognisable or ambiguous photo still needs SOME
# idle animation, and a generic "creature" idle is honest about not knowing more, rather
# than the front-end having to special-case a missing or invalid code.
ARCHETYPES: dict[str, dict] = {
    "humanoid": {
        "title": "Гуманоид",
        "hint": "Стоит на двух ногах и слегка покачивается, как боец перед схваткой.",
    },
    "quadruped": {
        "title": "Четвероногое",
        "hint": "Переминается на четырёх лапах и водит носом, как зверь.",
    },
    "bird": {
        "title": "Птица",
        "hint": "Складывает и расправляет крылья, держит голову высоко.",
    },
    "insect": {
        "title": "Насекомое",
        "hint": "Дёргано перебирает лапками и подрагивает усиками.",
    },
    "aquatic": {
        "title": "Морская тварь",
        "hint": "Плавно колышется из стороны в сторону, будто в толще воды.",
    },
    "reptile": {
        "title": "Ящер",
        "hint": "Крадётся низко к земле, хвост медленно ходит из стороны в сторону.",
    },
    "blob": {
        "title": "Слизь",
        "hint": "Пульсирует и слегка растекается бесформенной массой.",
    },
    "machine": {
        "title": "Машина",
        "hint": "Двигается резко и механически, будто на шарнирах и приводах.",
    },
    "vehicle": {
        "title": "Транспорт",
        "hint": "Подрагивает на месте на холостом ходу, а не переступает ногами.",
    },
    "plant": {
        "title": "Растение",
        "hint": "Едва заметно покачивается, будто на ветру.",
    },
    "spirit": {
        "title": "Дух",
        "hint": "Парит над землёй и подрагивает полупрозрачным контуром.",
    },
    "creature": {
        "title": "Существо",
        "hint": "Настороженно дышит на месте -- запасная анимация, когда облик не опознан.",
    },
}

DEFAULT_ARCHETYPE = "creature"

SPRITE_SYSTEM_PROMPT = """\
Ты определяешь по фото миниатюры, к какому архетипу движения она ближе всего -- это \
нужно для того, чтобы анимация существа в бою была похожа на то, что реально нарисовано \
на фото.

Опирайся ТОЛЬКО на то, что видно на фото, и не выдумывай деталей. Ответь РОВНО ОДНИМ \
словом -- одним из следующих кодов: humanoid, quadruped, bird, insect, aquatic, reptile, \
blob, machine, vehicle, plant, spirit, creature. Никаких пояснений, знаков препинания и \
других слов -- только код. Если не уверен или силуэт не подходит ни под один код, ответь \
creature.
"""

SPRITE_USER_PROMPT = "Определи архетип существа на этом фото."

# Reply parsing tolerates a trailing period, an exclamation mark someone's fine-tune adds
# for emphasis, or the model wrapping its one word in quotes -- none of that is the model
# being wrong, just decoration around a right answer, so it is stripped rather than
# treated as reason to fall back.
_STRIP_PUNCTUATION = " \t\r\n.,!?:;\"'«»()[]{}"

SPRITE_KEY = "sprite"


def _data_url(image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    return f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"


def archetype(code) -> dict:
    """Safe lookup: an unknown or missing code resolves to the `creature` entry.

    Every caller (pet card, battle screen) can therefore call this unconditionally
    instead of checking membership first -- a save written by an older or newer build,
    or a row whose classification never ran, reads the same as an explicit `creature`.
    """
    key = str(code or "").strip().lower()
    if key not in ARCHETYPES:
        key = DEFAULT_ARCHETYPE
    return {"code": key, **ARCHETYPES[key]}


def classify(image: bytes, *, api_key: str, model: str, timeout: float = 30.0) -> str:
    """Blocking OpenAI vision call -- callers run it via asyncio.to_thread, the same way
    critique.critique_work is run.

    NEVER raises: the caller is a battle screen building a sprite, not a purchase that
    needs to know whether to refund, so there is no exception a caller could usefully
    act on. Every failure -- a too-large photo, an API error, a malformed response, a
    dropped connection -- folds into the same generic `creature` sprite instead.
    """
    if not image or len(image) > MAX_IMAGE_BYTES:
        return DEFAULT_ARCHETYPE

    try:
        client = OpenAI(api_key=api_key, timeout=timeout)
        response = client.chat.completions.create(
            model=model,
            # The answer is one word from a twelve-item vocabulary; a few tokens of
            # headroom absorb stray punctuation without leaving room for the model to
            # start explaining itself instead of answering.
            max_tokens=6,
            messages=[
                {"role": "system", "content": SPRITE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": SPRITE_USER_PROMPT},
                        {"type": "image_url", "image_url": {"url": _data_url(image)}},
                    ],
                },
            ],
        )
        content = response.choices[0].message.content or ""
    except (OpenAIError, ValueError, TypeError):
        return DEFAULT_ARCHETYPE
    except Exception:
        # Anything else -- a dropped connection, a DNS failure, a malformed response the
        # SDK didn't wrap into OpenAIError -- is exactly as fatal to a fight as an API
        # error, and just as unworthy of one reaching the caller.
        return DEFAULT_ARCHETYPE

    first_word = content.strip().split()[0] if content.strip() else ""
    code = first_word.lower().strip(_STRIP_PUNCTUATION)
    return code if code in ARCHETYPES else DEFAULT_ARCHETYPE


def cached_archetype(record: dict | None) -> str | None:
    """The stored code, but ONLY if it was derived from the record's CURRENT
    photo_file_id.

    A new photo is a new subject -- the whole point of this feature is that the sprite
    resembles what is actually painted, so a stale row left over from a previous photo
    must read as a miss (prompting a fresh classification) rather than as a confident
    but wrong answer.
    """
    if not record:
        return None
    row = record.get(SPRITE_KEY)
    if not row:
        return None
    if row.get("photo_file_id") != record.get("photo_file_id"):
        return None
    code = row.get("archetype")
    # A row written by a build that still had a since-retired code must not crash the
    # screen reading it -- same reasoning as pets.py's debuff-definition-disappeared
    # case: treat it as if the classification never ran.
    return code if code in ARCHETYPES else None


def sprite_row(archetype_code: str, photo_file_id, at: str) -> dict:
    """Build the row `classify`'s result is stored under. Pure -- no I/O, no clock."""
    return {
        "archetype": str(archetype_code or DEFAULT_ARCHETYPE),
        "photo_file_id": photo_file_id,
        "at": str(at or ""),
    }
