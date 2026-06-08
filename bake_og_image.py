"""Bake the 1200x630 OpenGraph PNG from `mimir/static/img/ratatoskr.png`.

Run on demand when the design changes:

    uv run python bake_og_image.py

Output: `mimir/static/img/og-image.png`. Pillow is a dev-only dep; the
runtime serves the resulting PNG as a static file via the
`/og-image.png` route.

Source image is the 17th-century Icelandic Edda manuscript depiction
of Ratatoskr (AM 738 4to era), Árni Magnússon Institute, public
domain. See https://lille.snl.no/Ratatosk_-_ordforklaring.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "mimir" / "static" / "img" / "ratatoskr.png"
DST = ROOT / "mimir" / "static" / "img" / "og-image.png"

CANVAS = (1200, 630)
PARCHMENT = (214, 179, 121)        # sampled from src top-left
INK_HEAD = (58, 42, 24)            # near-black sepia, high-contrast on parchment
INK_BODY = (92, 64, 48)            # softer sepia for the tagline
WORDMARK = "ratatoskr.run"
TAGLINE = "Indexed mailing-list archives"

# macOS system fonts. Palatino reads as old-style enough to pair with a
# medieval-manuscript squirrel without being theatrical about it.
FONT_DIR = Path("/System/Library/Fonts")
HEAD_FONT_PATH = FONT_DIR / "Palatino.ttc"
BODY_FONT_PATH = FONT_DIR / "Palatino.ttc"


def main() -> None:
    canvas = Image.new("RGB", CANVAS, PARCHMENT)
    src = Image.open(SRC).convert("RGBA")

    # Scale to canvas height, preserving aspect; squirrel sits flush left.
    scale = CANVAS[1] / src.size[1]
    new_w = round(src.size[0] * scale)
    squirrel = src.resize((new_w, CANVAS[1]), Image.LANCZOS)
    canvas.paste(squirrel, (0, 0), squirrel)

    # Soft vertical fade from squirrel edge into the parchment panel so
    # the right edge of the image doesn't read as a hard cut.
    fade_w = 40
    fade_x0 = new_w - fade_w
    overlay = Image.new("RGBA", (fade_w, CANVAS[1]), PARCHMENT + (0,))
    for x in range(fade_w):
        alpha = round(255 * (x / (fade_w - 1)))
        for y in range(CANVAS[1]):
            overlay.putpixel((x, y), PARCHMENT + (alpha,))
    canvas.paste(overlay, (fade_x0, 0), overlay)

    draw = ImageDraw.Draw(canvas)
    head_font = ImageFont.truetype(str(HEAD_FONT_PATH), 110)
    body_font = ImageFont.truetype(str(BODY_FONT_PATH), 38)

    # Right-panel layout: text block vertically centred on the canvas,
    # left-aligned just past the squirrel + fade.
    text_x = new_w + 30
    head_bbox = draw.textbbox((0, 0), WORDMARK, font=head_font)
    body_bbox = draw.textbbox((0, 0), TAGLINE, font=body_font)
    head_h = head_bbox[3] - head_bbox[1]
    body_h = body_bbox[3] - body_bbox[1]
    gap = 24
    block_h = head_h + gap + body_h
    head_y = (CANVAS[1] - block_h) // 2 - head_bbox[1]
    body_y = head_y + head_h + gap

    draw.text((text_x, head_y), WORDMARK, font=head_font, fill=INK_HEAD)
    draw.text((text_x, body_y), TAGLINE, font=body_font, fill=INK_BODY)

    DST.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(DST, "PNG", optimize=True)
    print(f"wrote {DST.relative_to(ROOT)} ({DST.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
