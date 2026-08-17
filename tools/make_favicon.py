"""Generate the SC// favicon set from a single definition.

Kept as a script rather than a one-off so the icons can be regenerated when the
mark changes, and so the geometry lives somewhere readable instead of only in
binary files.

    .venv/bin/python tools/make_favicon.py

Writes into app/static/img/:
    favicon.svg           vector, used by modern browsers
    favicon.ico           16/32/48, for the browser's default /favicon.ico
    favicon-32.png        explicit PNG for the <link> tag
    apple-touch-icon.png  180px, iOS home screen
    icon-192.png          Android / PWA
    icon-512.png          large tile + a source for any future resizing

The mark: a blue gradient disc carrying the two SC// slashes in white.
"""
import os

from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(__file__), "..", "app", "static", "img")

# Geometry on an 800x800 canvas, taken from the supplied artwork.
SIZE = 800
CIRCLE_INSET = 8                     # a hair of margin so the disc isn't clipped
SLASHES = [
    [(385, 152), (492, 152), (282, 634), (175, 634)],
    [(540, 152), (647, 152), (437, 634), (330, 634)],
]
# Deep blue at the top-left, brighter towards the right, as in the original.
GRAD_FROM = (21, 89, 155)
GRAD_TO = (11, 142, 209)
# Direction the gradient runs, as a vector across the canvas.
GRAD_DIR = (1.0, 0.45)

SUPERSAMPLE = 4                      # draw big, downsample: cheap anti-aliasing


def _gradient(size):
    """A linear gradient the size of the canvas, running along GRAD_DIR."""
    image = Image.new("RGB", (size, size))
    pixels = image.load()
    dx, dy = GRAD_DIR
    span = (dx + dy) * size or 1
    for y in range(size):
        for x in range(size):
            t = (x * dx + y * dy) / span
            t = 0.0 if t < 0 else 1.0 if t > 1 else t
            pixels[x, y] = tuple(
                round(a + (b - a) * t) for a, b in zip(GRAD_FROM, GRAD_TO))
    return image


def render(size=SIZE * SUPERSAMPLE):
    scale = size / SIZE
    base = _gradient(size).convert("RGBA")

    # Disc mask, so the gradient only shows inside the circle.
    mask = Image.new("L", (size, size), 0)
    inset = CIRCLE_INSET * scale
    ImageDraw.Draw(mask).ellipse(
        [inset, inset, size - inset, size - inset], fill=255)

    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    icon.paste(base, (0, 0), mask)

    # The two slashes, clipped to the disc so they can't spill over the edge.
    slash_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(slash_layer)
    for points in SLASHES:
        draw.polygon([(x * scale, y * scale) for x, y in points],
                     fill=(255, 255, 255, 255))
    slash_layer.putalpha(Image.composite(
        slash_layer.getchannel("A"), Image.new("L", (size, size), 0), mask))
    icon.alpha_composite(slash_layer)
    return icon


SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 800">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="0.45">
      <stop offset="0" stop-color="rgb{grad_from}"/>
      <stop offset="1" stop-color="rgb{grad_to}"/>
    </linearGradient>
    <clipPath id="disc"><circle cx="400" cy="400" r="{radius}"/></clipPath>
  </defs>
  <circle cx="400" cy="400" r="{radius}" fill="url(#g)"/>
  <g fill="#ffffff" clip-path="url(#disc)">
    <polygon points="{slash_one}"/>
    <polygon points="{slash_two}"/>
  </g>
</svg>
"""


def write_svg(path):
    points = [" ".join(f"{x},{y}" for x, y in shape) for shape in SLASHES]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(SVG.format(
            grad_from=GRAD_FROM, grad_to=GRAD_TO,
            radius=400 - CIRCLE_INSET,
            slash_one=points[0], slash_two=points[1]))


def main():
    os.makedirs(OUT, exist_ok=True)
    master = render()

    write_svg(os.path.join(OUT, "favicon.svg"))

    for name, px in (("favicon-32.png", 32), ("apple-touch-icon.png", 180),
                     ("icon-192.png", 192), ("icon-512.png", 512)):
        master.resize((px, px), Image.LANCZOS).save(os.path.join(OUT, name))

    # Multi-resolution .ico: browsers pick the size they need, and the small
    # ones are downsampled from the master rather than from each other.
    ico_sizes = [(16, 16), (32, 32), (48, 48)]
    master.resize((256, 256), Image.LANCZOS).save(
        os.path.join(OUT, "favicon.ico"), sizes=ico_sizes)

    for name in sorted(os.listdir(OUT)):
        path = os.path.join(OUT, name)
        print(f"  {name:22} {os.path.getsize(path):>7,} bytes")


if __name__ == "__main__":
    main()
