#!/usr/bin/env python3
"""
PocketBurp -- tools/make_icons.py
Renders the Home-Screen / PWA icon set with Pillow (no external assets).

    python3 tools/make_icons.py          # writes static/icons/*.png

Design: near-black tile, orange shield (the proxy), dark lightning bolt
(interception). Drawn at 4x and downsampled for clean anti-aliasing.
"""

import os

from PIL import Image, ImageDraw, ImageFilter

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "static", "icons")
S = 1024          # master size
SS = 4            # supersampling factor -> 4096 px canvas

BG_TOP = (43, 43, 43)
BG_BOTTOM = (18, 18, 18)
ORANGE_TOP = (255, 128, 82)
ORANGE_BOTTOM = (214, 74, 25)
BOLT = (20, 20, 20)


def _gradient(size, top, bottom):
    w, h = size
    img = Image.new("RGB", (1, h))
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        px[0, y] = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    return img.resize((w, h), Image.BILINEAR)


def _shield_points(cx, top, half_w, shoulder_y, bottom_y):
    return [
        (cx, top),
        (cx + half_w, shoulder_y),
        (cx + half_w, int(top + (bottom_y - top) * 0.52)),
        (cx, bottom_y),
        (cx - half_w, int(top + (bottom_y - top) * 0.52)),
        (cx - half_w, shoulder_y),
    ]


def _bolt_points(cx, cy, size):
    """Bold lightning bolt centred on (cx, cy)."""
    u = size / 1024.0
    pts = [
        (430, 640), (262, 862), (508, 862),
        (430, 1062), (626, 806), (462, 806),
        (524, 640),
    ]
    return [(int(cx - 430 * u + x * u), int(cy - 851 * u + y * u)) for x, y in pts]


def render(scale=1.0, pad_bg=True):
    """scale<1 shrinks the emblem (for maskable icons)."""
    size = S * SS
    cx = size // 2

    base = Image.new("RGB", (size, size), BG_BOTTOM)
    if pad_bg:
        base.paste(_gradient((size, size), BG_TOP, BG_BOTTOM), (0, 0))

        # soft orange glow behind the shield
        glow = Image.new("L", (size, size), 0)
        gd = ImageDraw.Draw(glow)
        r = int(size * 0.30)
        gd.ellipse([cx - r, int(size * 0.18) - r, cx + r, int(size * 0.18) + r], fill=90)
        glow = glow.filter(ImageFilter.GaussianBlur(size // 14))
        base = Image.composite(Image.new("RGB", (size, size), (255, 102, 51)), base, glow)

    # emblem geometry (scaled about the centre)
    def sc(v):
        return int(round(v * scale))

    top = cx - sc(int(size * 0.255))
    bottom = cx + sc(int(size * 0.315))
    half_w = sc(int(size * 0.255))
    shoulder = top + sc(int(size * 0.075))

    shield = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shield)
    pts = _shield_points(cx, top, half_w, shoulder, bottom)

    # drop shadow
    shadow = Image.new("L", (size, size), 0)
    ImageDraw.Draw(shadow).polygon(pts, fill=170)
    shadow = shadow.filter(ImageFilter.GaussianBlur(size // 48))
    base.paste((0, 0, 0), (0, int(size * 0.012)), shadow)

    # shield body with a vertical orange gradient
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).polygon(pts, fill=255)
    grad = _gradient((size, size), ORANGE_TOP, ORANGE_BOTTOM)
    shield.paste(grad, (0, 0), mask)

    # inner rim for depth
    rim = Image.new("RGB", (size, size), (255, 173, 137))
    rim_mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(rim_mask).line(pts + [pts[0]], fill=255, width=int(size * 0.008))
    shield.paste(rim, (0, 0), rim_mask)

    # bolt
    bd = ImageDraw.Draw(shield)
    bd.polygon(_bolt_points(cx, (top + bottom) // 2, size), fill=BOLT + (255,))

    base = Image.alpha_composite(base.convert("RGBA"), shield)

    # corner rounding only for the "any" icons (iOS masks it anyway, harmless)
    out = base.convert("RGB").resize((S, S), Image.LANCZOS)
    return out


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    master = render(1.0)
    maskable = render(0.74)

    targets = [
        ("icon-1024.png", master, 1024),
        ("icon-512.png", master, 512),
        ("icon-192.png", master, 192),
        ("icon-180.png", master, 180),
        ("icon-167.png", master, 167),
        ("icon-152.png", master, 152),
        ("icon-120.png", master, 120),
        ("favicon-32.png", master, 32),
        ("maskable-512.png", maskable, 512),
    ]
    for name, img, px in targets:
        img.resize((px, px), Image.LANCZOS).save(os.path.join(OUT_DIR, name),
                                                 "PNG", optimize=True)
        print("wrote static/icons/%-18s %dx%d" % (name, px, px))

    # splash-ish wide preview for the README
    preview_path = os.path.join(OUT_DIR, "..", "..", "docs")
    try:
        os.makedirs(preview_path, exist_ok=True)
        master.resize((256, 256), Image.LANCZOS).save(
            os.path.join(preview_path, "icon-preview.png"), "PNG")
    except OSError:
        pass


if __name__ == "__main__":
    main()
