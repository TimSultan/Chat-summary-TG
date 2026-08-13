"""Try the sprite generator on one photo, from the command line, before deploying.

    python pets_sprite_preview.py моя_миниатюра.jpg

Reads GEMINI_API_KEY from .env exactly the way the bot does, makes the same four calls
the arena makes, writes the frames next to an HTML page and prints what came back. Open
the page in any browser to see the finished flipbook.

This exists because the rest of the feature is unfalsifiable from a test suite: every
automated test mocks the model, so they prove the plumbing survives a bad answer but say
nothing about whether the answers are any good. Whether Gemini actually returns a clean
cut-out of *your* painted miniature rather than a generic illustration is a question only
a real call with a real photo can settle, and finding that out should not require a
deploy, a Telegram client and a phone.

Nothing here touches the game: no pet store, no chat, no cache the arena reads.
"""

from __future__ import annotations

import argparse
import html
import sys
import time
from pathlib import Path

import pets_gemini
import pets_sprite

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - the bot depends on it; this tool tolerates it
    load_dotenv = None


PAGE = """<!doctype html>
<meta charset="utf-8">
<title>sprite preview — {name}</title>
<style>
  body {{ background:#17212b; color:#f5f5f5; font:14px/1.5 system-ui, sans-serif;
          margin:0; padding:24px; }}
  h1 {{ font-size:17px; margin:0 0 4px; }}
  .muted {{ color:#8a9aa9; font-size:12px; }}
  .row {{ display:flex; gap:18px; flex-wrap:wrap; margin-top:18px; align-items:flex-end; }}
  figure {{ margin:0; }}
  figure img {{ display:block; width:200px; height:240px; object-fit:contain;
                object-position:50% 100%; background:
                  repeating-conic-gradient(#2a3646 0 25%, #222c3a 0 50%) 0 0/16px 16px; }}
  figcaption {{ font-size:11px; color:#8a9aa9; margin-top:6px; text-align:center; }}
  /* The same cross-fade the arena plays, so this page shows the real thing rather than
     a contact sheet: whether the loop breathes or strobes is the whole question. */
  .stage {{ position:relative; width:200px; height:240px; }}
  .stage img {{ position:absolute; inset:0; width:100%; height:100%; object-fit:contain;
                object-position:50% 100%; opacity:0; animation:breathe 2.2s ease-in-out infinite; }}
  .stage img:nth-child(2) {{ animation-delay:-1.1s; }}
  @keyframes breathe {{ 0%,44% {{ opacity:1; }} 56%,100% {{ opacity:0; }} }}
</style>
<h1>{name}</h1>
<div class="muted">archetype: <b>{archetype}</b> — {title}<br>subject: {subject}</div>
<div class="row">
  <figure><div class="stage">{loop}</div><figcaption>idle loop, as the arena plays it</figcaption></figure>
  {stills}
</div>
<p class="muted">Checkerboard shows through where the background was removed. If you can
see a rectangle of photo instead, the cut-out did not work.</p>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("photo", help="a JPEG or PNG of a painted miniature")
    parser.add_argument("--out", default="sprite_preview", help="where to write the result")
    parser.add_argument("--key", default=None, help="override GEMINI_API_KEY")
    parser.add_argument("--vision-model", default=None)
    parser.add_argument("--image-model", default=None)
    args = parser.parse_args()

    import os
    if load_dotenv is not None:
        load_dotenv()
    api_key = args.key or os.getenv("GEMINI_API_KEY", "")
    vision_model = args.vision_model or os.getenv("GEMINI_VISION_MODEL", "gemini-2.5-flash")
    image_model = args.image_model or os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")

    if not pets_gemini.available(api_key):
        print("No Gemini available. Set GEMINI_API_KEY in .env (or pass --key), and make "
              "sure google-genai is installed: pip install -r requirements.txt")
        return 1

    source = Path(args.photo)
    if not source.is_file():
        print(f"No such file: {source}")
        return 1
    image = source.read_bytes()
    print(f"{source.name}: {len(image):,} bytes")

    started = time.monotonic()
    reading = pets_gemini.analyse(image, api_key=api_key, model=vision_model)
    print(f"  analysed in {time.monotonic() - started:.1f}s -> "
          f"{reading['archetype']} / {reading['subject'] or '(no subject)'}")
    if reading["archetype"] == pets_sprite.DEFAULT_ARCHETYPE and not reading["subject"]:
        print("  (that is the fallback -- the call failed, or the model could not tell)")

    started = time.monotonic()
    frames = pets_gemini.generate_frames(
        image, api_key=api_key, model=image_model,
        subject=reading["subject"], archetype=reading["archetype"],
    )
    print(f"  drew {len(frames)}/{len(pets_gemini.FRAMES)} frames in "
          f"{time.monotonic() - started:.1f}s")
    if not frames:
        print("  Nothing came back. The arena would fall back to animating the photo.")
        print(f"  Check that {image_model!r} is an image-output model on your key.")
        return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ordered = [name for name in pets_gemini.FRAMES if name in frames]
    for name in ordered:
        (out / f"{name}.png").write_bytes(frames[name])
        print(f"    {name}.png  {len(frames[name]):,} bytes")

    idle = [name for name in ordered if name.startswith("idle")] or ordered[:1]
    page = PAGE.format(
        name=html.escape(source.name),
        archetype=html.escape(reading["archetype"]),
        title=html.escape(pets_sprite.archetype(reading["archetype"])["title"]),
        subject=html.escape(reading["subject"] or "—"),
        loop="".join(f'<img src="{name}.png" alt="">' for name in idle),
        stills="".join(
            f'<figure><img src="{name}.png" alt=""><figcaption>{name}</figcaption></figure>'
            for name in ordered
        ),
    )
    (out / "index.html").write_text(page, encoding="utf-8")
    print(f"\nOpen: {(out / 'index.html').resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
