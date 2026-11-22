"""Draws Deckhand' icon and Steam library artwork into assets/steam/.

Run on any PC with Pillow:  python tools/make_art.py
Outputs (Steam's sizes): icon.png 256, portrait.png 600x900, capsule.png
920x430, hero.png 1920x620, logo.png 800x220 (transparent).
Fonts: Segoe UI if present (Windows), else DejaVu Sans, else Pillow's default.
"""
import math
import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "steam")
BG_TOP, BG_BOTTOM = (16, 24, 38), (30, 58, 86)
ACCENT = (102, 204, 255)
WHITE = (238, 244, 250)


def font(size, bold=True):
    names = (["segoeuib.ttf", "seguisb.ttf"] if bold else ["segoeui.ttf"]) + \
            (["DejaVuSans-Bold.ttf"] if bold else ["DejaVuSans.ttf"])
    for n in names:
        for d in (r"C:\Windows\Fonts", "/usr/share/fonts/TTF", "/usr/share/fonts/truetype/dejavu"):
            p = os.path.join(d, n)
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def gradient(w, h):
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        c = tuple(int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM))
        for x in range(w):
            px[x, y] = c
    return img


def fan_glyph(size, color=WHITE, blades=5):
    """A fan: rounded blades around a hub, drawn at 4x and downsampled."""
    s = size * 4
    layer = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    c = s / 2
    blade = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    bd = ImageDraw.Draw(blade)
    # one blade: an ellipse offset from the centre, slightly swept
    bd.ellipse([c - s * 0.075, c - s * 0.46, c + s * 0.135, c - s * 0.07], fill=color + (255,))
    bd.ellipse([c - s * 0.02, c - s * 0.44, c + s * 0.16, c - s * 0.16], fill=color + (255,))
    blade = blade.rotate(-28, center=(c, c), resample=Image.BICUBIC)
    for i in range(blades):
        layer.alpha_composite(blade.rotate(360 / blades * i, center=(c, c), resample=Image.BICUBIC))
    d = ImageDraw.Draw(layer)
    d.ellipse([c - s * 0.12, c - s * 0.12, c + s * 0.12, c + s * 0.12], fill=color + (255,))
    d.ellipse([c - s * 0.055, c - s * 0.055, c + s * 0.055, c + s * 0.055], fill=(0, 0, 0, 0))
    ring = s * 0.485
    d.ellipse([c - ring, c - ring, c + ring, c + ring], outline=color + (255,), width=int(s * 0.03))
    return layer.resize((size, size), Image.LANCZOS)


def icon(size=256):
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=255)
    bg = gradient(s, s).convert("RGBA")
    img.paste(bg, (0, 0), mask)
    g = fan_glyph(int(s * 0.7), ACCENT)
    img.alpha_composite(g, ((s - g.width) // 2, (s - g.height) // 2))
    return img.resize((size, size), Image.LANCZOS)


def glow(img, glyph, pos):
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    shadow.alpha_composite(glyph, pos)
    shadow = shadow.filter(ImageFilter.GaussianBlur(glyph.width // 14))
    img.alpha_composite(shadow)
    img.alpha_composite(glyph, pos)


def text_center(d, cx, y, text, f, fill):
    w = d.textlength(text, font=f)
    d.text((cx - w / 2, y), text, font=f, fill=fill)


def portrait():
    w, h = 600, 900
    img = gradient(w, h).convert("RGBA")
    g = fan_glyph(360, ACCENT)
    glow(img, g, ((w - 360) // 2, 170))
    d = ImageDraw.Draw(img)
    text_center(d, w / 2, 610, "Deckhand", font(96), WHITE)
    text_center(d, w / 2, 760, "for the Steam Frame", font(34, bold=False), ACCENT)
    text_center(d, w / 2, 812, "Fan · Rooms · Signal", font(32, bold=False), ACCENT)
    return img


def capsule():
    w, h = 920, 430
    img = gradient(w, h).convert("RGBA")
    g = fan_glyph(300, ACCENT)
    glow(img, g, (70, (h - 300) // 2))
    d = ImageDraw.Draw(img)
    d.text((420, 140), "Deckhand", font=font(104), fill=WHITE)
    d.text((424, 290), "Fan · Power · Rooms · Signal", font=font(30, bold=False), fill=ACCENT)
    return img


def hero():
    w, h = 1920, 620
    img = gradient(w, h).convert("RGBA")
    g = fan_glyph(820, ACCENT + ())
    faint = g.copy()
    faint.putalpha(faint.getchannel("A").point(lambda a: a * 0.28))
    img.alpha_composite(faint, (w - 1020, -100))
    return img


def logo():
    w, h = 800, 220
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    g = fan_glyph(180, ACCENT)
    img.alpha_composite(g, (10, 20))
    d = ImageDraw.Draw(img)
    d.text((220, 18), "Deckhand", font=font(84), fill=WHITE + (255,),
           stroke_width=3, stroke_fill=(10, 16, 26, 255))
    d.text((224, 130), "Fan · Power · Rooms · Signal", font=font(34, bold=False), fill=ACCENT + (255,),
           stroke_width=2, stroke_fill=(10, 16, 26, 255))
    return img


def main():
    os.makedirs(OUT, exist_ok=True)
    for name, make in (("icon", icon), ("portrait", portrait), ("capsule", capsule),
                       ("hero", hero), ("logo", logo)):
        im = make()
        im.save(os.path.join(OUT, f"{name}.png"))
        print(f"{name}.png {im.size[0]}x{im.size[1]}")


if __name__ == "__main__":
    main()
