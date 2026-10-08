"""Draw the member app's home-screen icons (PNG) from the Sawazi mark's geometry (brand/sawazi-mark.svg).

    python scripts/make_app_icons.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "sawazi" / "app"
INK, MINT, GOLD = (13, 31, 24), (79, 199, 154), (242, 184, 75)
SS = 4  # draw large and shrink, for smooth edges


def mark(size: int, maskable: bool = False) -> Image.Image:
    big = size * SS
    img = Image.new("RGBA", (big, big), INK + (255,) if maskable else (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = big * 0.1 if maskable else 0  # maskable icons keep the mark inside the safe zone
    unit = (big - 2 * pad) / 100
    p = lambda x, y: (pad + x * unit, pad + y * unit)  # noqa: E731
    if not maskable:
        d.rounded_rectangle([0, 0, big - 1, big - 1], radius=24 * unit, fill=INK)
    w = 13 * unit

    def bar(x1, x2, y, colour):
        d.rectangle([*p(x1, y - 6.5), *p(x2, y + 6.5)], fill=colour)

    def cap(x, y, colour):
        d.ellipse([*p(x - 6.5, y - 6.5), *p(x + 6.5, y + 6.5)], fill=colour)

    # upper half (mint): from the top-right cap, along, round the left, back to the centre seam
    bar(42, 72, 22, MINT)
    d.arc([*p(28 - 6.5, 22 - 6.5), *p(56 + 6.5, 50 + 6.5)], 90, 270, fill=MINT, width=int(w))
    bar(42, 49, 50, MINT)
    cap(72, 22, MINT)
    # lower half (gold): from the seam, round the right, along to the bottom-left cap
    bar(51, 58, 50, GOLD)
    d.arc([*p(44 - 6.5, 50 - 6.5), *p(72 + 6.5, 78 + 6.5)], 270, 90, fill=GOLD, width=int(w))
    bar(28, 58, 78, GOLD)
    cap(28, 78, GOLD)
    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    for size in (192, 512):
        mark(size).save(OUT / f"icon-{size}.png")
    mark(512, maskable=True).save(OUT / "icon-maskable-512.png")
    print("icons written to", OUT)
