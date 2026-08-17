#!/usr/bin/env python3
"""Generate light-themed abstract artwork for the SandBoxer landing.

Deterministic (fixed seed) so re-runs are byte-stable. Produces:
  - assets/brand/sandboxer-banner.png          (hero, 1672x941)
  - assets/abstract/two-arenas-methodology.png (method figure, 1672x941)
"""
import math
import random
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")

# Palette (light, minimal, academic)
BG = (246, 245, 241)          # warm paper
INK = (26, 26, 24)            # near-black
HAIR = (214, 213, 205)        # hairline
BLUE = (58, 91, 217)          # deep blue
VIOLET = (122, 92, 224)       # violet
BLUE_SOFT = (224, 230, 248)   # pale blue fill
VIOLET_SOFT = (236, 230, 250) # pale violet fill
GRID = (235, 234, 228)        # background grid

W, H = 1672, 941


def dot_grid(draw, step=56, radius=1.6, color=GRID):
    for x in range(0, W, step):
        for y in range(0, H, step):
            draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=color)


def ring(draw, cx, cy, r, color, width=3):
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=width)


def flag(draw, cx, cy, color, size=26):
    """A small flag pin: a dot + a short mast with a pennant."""
    mast_top = cy - size
    draw.line([cx, cy, cx, mast_top], fill=color, width=4)
    draw.polygon(
        [(cx, mast_top), (cx + size * 0.9, mast_top + 7), (cx, mast_top + 14)],
        fill=color,
    )
    draw.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], fill=color)


def arena(draw, cx, cy, r, accent, soft):
    """One arena: a soft filled plate inside a hairline ring, with a flag."""
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=soft)
    ring(draw, cx, cy, r, accent, width=3)
    # inner hairline ring for depth
    ring(draw, cx, cy, int(r * 0.78), HAIR, width=2)
    flag(draw, cx, cy, accent)


def connection(draw, x1, y1, x2, y2, accent):
    """A dashed connecting line between two nodes."""
    dist = math.hypot(x2 - x1, y2 - y1)
    steps = max(1, int(dist // 18))
    for i in range(steps):
        t0 = i / steps
        t1 = (i + 0.55) / steps
        p0 = (x1 + (x2 - x1) * t0, y1 + (y2 - y1) * t0)
        p1 = (x1 + (x2 - x1) * t1, y1 + (y2 - y1) * t1)
        draw.line([p0, p1], fill=accent, width=3)


def build_banner():
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    dot_grid(draw)

    # Two arenas side by side, slightly overlapping the frame edges.
    cy = H * 0.52
    r = H * 0.46
    arena(draw, W * 0.30, cy, r, BLUE, BLUE_SOFT)
    arena(draw, W * 0.70, cy, r, VIOLET, VIOLET_SOFT)

    # A dashed traversal path between the two flags.
    connection(draw, W * 0.30, cy, W * 0.70, cy, INK)
    # Center node (the flag being contested).
    cx = W * 0.50
    draw.ellipse([cx - 14, cy - 14, cx + 14, cy + 14], fill=INK)
    draw.ellipse([cx - 6, cy - 6, cx + 6, cy + 6], fill=BG)

    img.save(os.path.join(ROOT, "assets/brand/sandboxer-banner.png"), optimize=True)


def build_method():
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    dot_grid(draw)

    # Two isolated Runners (rounded plates) + one protected control plane (diamond).
    box_w, box_h = W * 0.30, H * 0.42
    left = (W * 0.10, (H - box_h) / 2)
    right = (W * 0.60, (H - box_h) / 2)

    def runner(x, y, accent, soft):
        draw.rounded_rectangle(
            [x, y, x + box_w, y + box_h], radius=26, fill=soft, outline=accent, width=3
        )
        # inner plate
        draw.rounded_rectangle(
            [x + 26, y + 26, x + box_w - 26, y + box_h - 26],
            radius=18,
            outline=HAIR,
            width=2,
        )
        flag(draw, x + box_w / 2, y + box_h / 2, accent, size=24)

    runner(*left, BLUE, BLUE_SOFT)
    runner(*right, VIOLET, VIOLET_SOFT)

    # Central control plane (diamond) connected to both runners.
    cxc = W * 0.50
    cyc = H * 0.50
    half = 46
    connection(draw, left[0] + box_w, left[1] + box_h / 2, cxc - half, cyc, INK)
    connection(draw, right[0], right[1] + box_h / 2, cxc + half, cyc, INK)
    draw.polygon(
        [
            (cxc, cyc - half),
            (cxc + half, cyc),
            (cxc, cyc + half),
            (cxc - half, cyc),
        ],
        fill=INK,
    )
    draw.ellipse([cxc - 8, cyc - 8, cxc + 8, cyc + 8], fill=BG)

    img.save(
        os.path.join(ROOT, "assets/abstract/two-arenas-methodology.png"), optimize=True
    )


# ------------------------------------------------------------------ #
#  Lighten existing dark artwork so it matches the light theme.       #
#  The figures are drawn light-on-dark; we re-render them as ink on   #
#  paper (or as a transparent ink emblem for the logo).               #
# ------------------------------------------------------------------ #


def _luminance_mask(l, thr=60, soft=140, gamma=1.2):
    a = (l - thr) / soft
    a = min(1.0, max(0.0, a))
    return a ** gamma


def lighten_figure(src, dst, paper=BG, ink=INK):
    """Re-render a light-on-dark figure as ink on paper."""
    im = Image.open(src).convert("L")
    w, h = im.size
    L = im.load()
    out = Image.new("RGB", (w, h), paper)
    op = out.load()
    for y in range(h):
        for x in range(w):
            a = _luminance_mask(L[x, y])
            op[x, y] = tuple(round(paper[i] * (1 - a) + ink[i] * a) for i in range(3))
    out.save(dst, quality=92, optimize=True)


def lighten_logo(src, dst, ink=INK):
    """Re-render the light-on-dark logo emblem as transparent ink."""
    im = Image.open(src).convert("L")
    w, h = im.size
    L = im.load()
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    op = out.load()
    for y in range(h):
        for x in range(w):
            a = round(_luminance_mask(L[x, y], gamma=1.0) * 255)
            op[x, y] = (ink[0], ink[1], ink[2], a)
    out.save(dst, optimize=True)


if __name__ == "__main__":
    random.seed(7)
    build_banner()
    build_method()
    lighten_figure(
        os.path.join(ROOT, "assets/models/laguna-s-2.1-free.jpg"),
        os.path.join(ROOT, "assets/models/laguna-s-2.1-free.jpg"),
    )
    lighten_figure(
        os.path.join(ROOT, "assets/models/meta-muse-spark-1.2-contributor.jpg"),
        os.path.join(ROOT, "assets/models/meta-muse-spark-1.2-contributor.jpg"),
    )
    lighten_logo(
        os.path.join(ROOT, "assets/brand/sandboxer-logo.png"),
        os.path.join(ROOT, "assets/brand/sandboxer-logo.png"),
    )
    print("wrote banner + method art + lightened model art + logo")
