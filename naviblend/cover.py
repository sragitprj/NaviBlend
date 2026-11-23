"""Cover art for a blend playlist: everyone's colour as overlapping circles, plus the names."""
from __future__ import annotations

import io
import math

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

SIZE = 1000
SS = 3  # supersampling for smooth circle edges


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except (ValueError, IndexError):
        return (91, 155, 255)


def _font(size: int) -> ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def _fit(draw: ImageDraw.ImageDraw, text: str, max_width: int, start: int, smallest: int) -> ImageFont.ImageFont:
    size = start
    while size > smallest:
        f = _font(size)
        if draw.textlength(text, font=f) <= max_width:
            return f
        size -= 2
    return _font(smallest)


def _circle_centres(n: int, cx: float, cy: float, r: float) -> list[tuple[float, float]]:
    if n == 1:
        return [(cx, cy)]
    if n == 2:
        return [(cx - r * 0.55, cy), (cx + r * 0.55, cy)]
    ring = r * 0.62
    return [(cx + ring * math.cos(2 * math.pi * i / n - math.pi / 2),
             cy + ring * math.sin(2 * math.pi * i / n - math.pi / 2)) for i in range(n)]


def _disc(x: float, y: float, r: float, color: tuple[int, int, int]) -> Image.Image:
    """A full-size layer with one anti-aliased disc on black."""
    big = Image.new("RGB", (SIZE * SS, SIZE * SS), (0, 0, 0))
    ImageDraw.Draw(big).ellipse(((x - r) * SS, (y - r) * SS, (x + r) * SS, (y + r) * SS), fill=color)
    return big.resize((SIZE, SIZE), Image.LANCZOS)


def make_cover(colors: list[str], names: list[str], subtitle: str = "") -> bytes:
    """Return a square PNG. Kept clear of the edges so apps that crop or fade the art still show it well."""
    colors = (colors or ["#5B9BFF"])[:6]
    n = len(colors)

    # background: dark, slightly lighter in the middle
    bg = Image.new("RGB", (SIZE, SIZE), (12, 13, 17))
    halo = Image.radial_gradient("L").resize((SIZE, SIZE)).point(lambda v: 255 - v)
    bg = Image.composite(Image.new("RGB", (SIZE, SIZE), (26, 28, 36)), bg, halo)

    # circles, blended with "screen" so the overlaps glow like light
    r = SIZE * (0.20 if n <= 2 else 0.15 if n <= 4 else 0.14)
    cy = SIZE * (0.36 if n <= 2 else 0.35)
    canvas = Image.new("RGB", (SIZE, SIZE), (0, 0, 0))
    for (x, y), col in zip(_circle_centres(n, SIZE / 2, cy, r), colors):
        canvas = ImageChops.screen(canvas, _disc(x, y, r, tuple(int(c * 0.92) for c in _rgb(col))))
    glow = canvas.filter(ImageFilter.GaussianBlur(SIZE * 0.06))
    img = ImageChops.screen(bg, ImageChops.multiply(glow, Image.new("RGB", (SIZE, SIZE), (80, 80, 80))))
    img = ImageChops.screen(img, canvas)

    # names, centred just under the circles
    d = ImageDraw.Draw(img)
    title = " + ".join(names) if len(names) <= 3 else f"{', '.join(names[:-1])} & {names[-1]}"
    title = title or "Blend"
    f_title = _fit(d, title, int(SIZE * 0.84), int(SIZE * 0.085), int(SIZE * 0.045))
    d.text((SIZE / 2, SIZE * 0.735), title, font=f_title, fill=(250, 250, 252), anchor="ms",
           stroke_width=max(1, SIZE // 500), stroke_fill=(250, 250, 252))
    sub = subtitle or "Blend"
    f_sub = _fit(d, sub, int(SIZE * 0.84), int(SIZE * 0.042), int(SIZE * 0.028))
    d.text((SIZE / 2, SIZE * 0.80), sub.upper() if sub == "Blend" else sub, font=f_sub,
           fill=(176, 182, 194), anchor="ms")

    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()
