#!/usr/bin/env python3
"""
MyLocal (#Vocal4Local) explainer video generator.

Renders animated motion-graphics scenes with Pillow, narrates them with the
macOS `say` text-to-speech engine (Indian-English voice), and muxes everything
into an MP4 with ffmpeg. Every visual cue is synced to the start time of the
narrated sentence it illustrates.

Usage:
    python3 make_video.py            # full render -> MyLocal_Explainer.mp4
    python3 make_video.py --preview  # contact sheets of key frames only

Env overrides:
    APP_NAME="Vocal4Local" VOICE="Aman" RATE=165 python3 make_video.py
"""
import hashlib
import math
import os
import subprocess
import sys
import wave

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")
os.makedirs(BUILD, exist_ok=True)

APP_NAME = os.environ.get("APP_NAME", "MyLocal")
TAGLINE = "#Vocal4Local"
VOICE = os.environ.get("VOICE", "Tara")
RATE = os.environ.get("RATE", "168")
OUT = os.path.join(HERE, f"{APP_NAME}_Explainer.mp4")

W, H, FPS = 1920, 1080, 30
SR = 22050  # narration sample rate

# ---------------------------------------------------------------- palette ---
NAVY = (11, 16, 38)
NAVY2 = (24, 33, 82)
CARD = (30, 41, 88)
SAFFRON = (255, 138, 0)
GREEN = (18, 183, 106)
PINK = (255, 77, 141)
TEAL = (34, 211, 238)
RED = (240, 68, 56)
WHITE = (255, 255, 255)
MUTED = (169, 180, 208)

SF = "/System/Library/Fonts/SFNS.ttf"
EMOJI = "/System/Library/Fonts/Apple Color Emoji.ttc"

B = None  # current frame canvas (RGBA), set per frame


# ---------------------------------------------------------------- easing ----
def cl(x):
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def eo(x):  # ease-out cubic
    x = cl(x)
    return 1 - (1 - x) ** 3


def eio(x):  # smoothstep
    x = cl(x)
    return x * x * (3 - 2 * x)


def eback(x):  # ease-out back (overshoot)
    x = cl(x)
    c1 = 1.70158
    c3 = c1 + 1
    return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2


def ap(t, start, d=0.5):
    return eo((t - start) / d)


def lerp(a, b, u):
    return a + (b - a) * u


# ---------------------------------------------------------------- drawing ---
_fonts, _tiles, _emoji, _shapes = {}, {}, {}, {}


def font(size, weight="Bold"):
    k = (size, weight)
    if k not in _fonts:
        f = ImageFont.truetype(SF, size)
        f.set_variation_by_name(weight)
        _fonts[k] = f
    return _fonts[k]


def paste(tile, x, y, a=1.0):
    if a <= 0.003 or tile is None:
        return
    x, y = int(round(x)), int(round(y))
    if a < 0.997:
        tile = tile.copy()
        tile.putalpha(tile.getchannel("A").point(lambda v, a=a: int(v * a)))
    sx, sy = max(0, -x), max(0, -y)
    if sx or sy:
        tile = tile.crop((sx, sy, tile.width, tile.height))
        x, y = x + sx, y + sy
    if x >= W or y >= H or tile.width <= 0 or tile.height <= 0:
        return
    if x + tile.width > W or y + tile.height > H:
        tile = tile.crop((0, 0, min(tile.width, W - x), min(tile.height, H - y)))
    B.alpha_composite(tile, (x, y))


def paste_c(tile, cx, cy, a=1.0, scale=1.0):
    """Paste centred, optionally scaled."""
    if scale <= 0.01:
        return
    if abs(scale - 1) > 0.01:
        tile = tile.resize((max(1, int(tile.width * scale)), max(1, int(tile.height * scale))), Image.BILINEAR)
    paste(tile, cx - tile.width / 2, cy - tile.height / 2, a)


def ttile(s, size, weight, color):
    k = (s, size, weight, color)
    if k not in _tiles:
        f = font(size, weight)
        asc, desc = f.getmetrics()
        im = Image.new("RGBA", (int(f.getlength(s)) + 8, asc + desc + 8), (0, 0, 0, 0))
        ImageDraw.Draw(im).text((4, 4), s, font=f, fill=color)
        _tiles[k] = im
    return _tiles[k]


def txt(s, x, y, size=48, weight="Bold", color=WHITE, a=1.0, anchor="l"):
    im = ttile(s, size, weight, color)
    if anchor == "m":
        x -= im.width / 2
    elif anchor == "r":
        x -= im.width
    paste(im, x, y, a)
    return im.width


def emoji_tile(ch, size):
    k = (ch, size)
    if k not in _emoji:
        f = ImageFont.truetype(EMOJI, 160)
        im = Image.new("RGBA", (240, 240), (0, 0, 0, 0))
        ImageDraw.Draw(im).text((20, 20), ch, font=f, embedded_color=True)
        bb = im.getbbox()
        im = im.crop(bb) if bb else im
        r = size / max(im.size)
        _emoji[k] = im.resize((max(1, int(im.width * r)), max(1, int(im.height * r))), Image.LANCZOS)
    return _emoji[k]


def emo(ch, cx, cy, size, a=1.0, scale=1.0):
    paste_c(emoji_tile(ch, size), cx, cy, a, scale)


def rrect_tile(w, h, r, fill, outline=None, width=0, cache=True):
    k = (w, h, r, fill, outline, width)
    if cache and k in _shapes:
        return _shapes[k]
    ss = 2
    im = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle(
        (0, 0, w * ss - 1, h * ss - 1), r * ss, fill=fill, outline=outline, width=width * ss
    )
    im = im.resize((w, h), Image.LANCZOS)
    if cache:
        _shapes[k] = im
    return im


def rrect(x, y, w, h, r, fill, outline=None, width=0, a=1.0, cache=True):
    if w < 2 or h < 2:
        return
    paste(rrect_tile(int(w), int(h), int(min(r, h / 2, w / 2)), fill, outline, width, cache), x, y, a)


def circle_tile(r, fill=None, outline=None, width=0, cache=True):
    k = ("c", r, fill, outline, width)
    if cache and k in _shapes:
        return _shapes[k]
    ss = 2
    d = int(2 * r * ss) + 2
    im = Image.new("RGBA", (d, d), (0, 0, 0, 0))
    ImageDraw.Draw(im).ellipse((1, 1, d - 2, d - 2), fill=fill, outline=outline, width=int(width * ss))
    im = im.resize((d // ss, d // ss), Image.LANCZOS)
    if cache:
        _shapes[k] = im
    return im


def circle(cx, cy, r, fill=None, outline=None, width=0, a=1.0, cache=True):
    if r < 1:
        return
    paste_c(circle_tile(int(r), fill, outline, width, cache), cx, cy, a)


def pin_tile(size, color=SAFFRON):
    k = ("pin", size, color)
    if k not in _shapes:
        ss = 4
        w = size * ss
        h = int(size * 1.42) * ss
        im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.ellipse((0, 0, w - 1, w - 1), fill=color)
        d.polygon([(w * 0.12, w * 0.68), (w * 0.88, w * 0.68), (w / 2, h - 1)], fill=color)
        d.ellipse((w * 0.3, w * 0.3, w * 0.7, w * 0.7), fill=WHITE)
        _shapes[k] = im.resize((size, h // ss), Image.LANCZOS)
    return _shapes[k]


def pin(cx, cy, size, a=1.0, color=SAFFRON, scale=1.0):
    """cx, cy = centre of the round head."""
    tile = pin_tile(size, color)
    if abs(scale - 1) > 0.01:
        tile = tile.resize((max(1, int(tile.width * scale)), max(1, int(tile.height * scale))), Image.BILINEAR)
    paste(tile, cx - tile.width / 2, cy - tile.width / 2, a)


def check_badge(cx, cy, r, a=1.0, color=GREEN):
    circle(cx, cy, r, fill=color, a=a)
    txt("✓", cx, cy - r * 0.95, int(r * 1.3), "Heavy", WHITE, a, "m")


def heading(title, a=1.0, y=88, size=72, sub=None, sub_a=None):
    txt(title, W / 2, y + 24 * (1 - a), size, "Heavy", WHITE, a, "m")
    if sub:
        txt(sub, W / 2, y + size + 30, 36, "Medium", MUTED, a if sub_a is None else sub_a, "m")


def wrap(s, size, weight, maxw):
    f = font(size, weight)
    words, lines, cur = s.split(), [], ""
    for w_ in words:
        trial = (cur + " " + w_).strip()
        if f.getlength(trial) <= maxw:
            cur = trial
        else:
            lines.append(cur)
            cur = w_
    if cur:
        lines.append(cur)
    return lines


def caption(s, a):
    k = ("cap", s)
    if k not in _shapes:
        lines = wrap(s, 34, "Medium", 1500)
        f = font(34, "Medium")
        lh = 46
        w = int(max(f.getlength(l) for l in lines)) + 64
        h = lh * len(lines) + 26
        im = rrect_tile(w, h, 22, (0, 0, 0, 165)).copy()
        d = ImageDraw.Draw(im)
        for i, l in enumerate(lines):
            d.text(((w - f.getlength(l)) / 2, 12 + i * lh), l, font=f, fill=WHITE)
        _shapes[k] = im
    im = _shapes[k]
    paste(im, (W - im.width) / 2, H - 36 - im.height, a)


def logo_small(a=0.95):
    pin(76, 64, 34, a)
    txt(APP_NAME, 104, 42, 36, "Heavy", WHITE, a)


# ------------------------------------------------------------- backgrounds --
def make_bg(g1, g2):
    top = Image.new("RGB", (W, H), NAVY)
    bot = Image.new("RGB", (W, H), NAVY2)
    bg = Image.composite(bot, top, Image.linear_gradient("L").resize((W, H)))
    glow = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(glow)
    d.ellipse((-350, -350, 750, 650), fill=g1)
    d.ellipse((1250, 550, 2350, 1450), fill=g2)
    glow = glow.filter(ImageFilter.GaussianBlur(230))
    return ImageChops.add(bg, glow).convert("RGBA")


# ================================================================ SCENES ====
# Each scene: draw(t, d, S, E) where t = local time, d = scene duration,
# S/E = start/end times of each narrated sentence within the scene.

def sc_title(t, d, S, E):
    cx, cy = W / 2, 330
    drop = eback((t - 0.15) / 0.75)
    if t > 0.9:
        for k in range(3):
            ph = ((t - 0.9 - k * 0.7) % 2.1) / 2.1
            r = 70 + 300 * ph
            circle(cx, cy + 40, r, outline=SAFFRON + (255,), width=4, a=(1 - ph) * 0.55, cache=False)
    pin(cx, cy - 420 * (1 - drop), 190, cl(t / 0.25))
    a = ap(t, S[0] + 0.1, 0.6)
    txt(APP_NAME, W / 2, 520 + 30 * (1 - a), 160, "Heavy", WHITE, a, "m")
    txt("The super app for your neighbourhood", W / 2, 730, 50, "Semibold", WHITE, ap(t, S[1], 0.5), "m")
    a2 = ap(t, S[2], 0.5)
    txt(TAGLINE, W / 2, 815, 64, "Heavy", SAFFRON, a2, "m")
    txt("Social  •  Commerce  •  Community  •  Civic", W / 2, 930, 34, "Medium", MUTED, a2, "m")


def sc_problem(t, d, S, E):
    heading("The problem today", ap(t, 0.1))
    cards = [
        ("🧁", "Local shops are invisible", "Buried under global, viral content", RED),
        ("💸", "Up to 30% commission", "Aggregators squeeze small businesses", SAFFRON),
        ("🤖", "Fake followers", "Vanity numbers, no real community", PINK),
        ("📢", "Civic issues lost in noise", "Outrage online, no local action", TEAL),
    ]
    cw, ch, gx, gy = 770, 250, 60, 50
    x0, y0 = (W - 2 * cw - gx) / 2, 250
    for i, (e, title, sub, col) in enumerate(cards):
        a = ap(t, S[min(i, len(S) - 1)] - 0.1, 0.55)
        x = x0 + (i % 2) * (cw + gx)
        y = y0 + (i // 2) * (ch + gy) + 50 * (1 - a)
        rrect(x, y, cw, ch, 28, CARD + (235,), a=a)
        rrect(x, y + 30, 10, ch - 60, 5, col + (255,), a=a)
        emo(e, x + 110, y + ch / 2, 110, a, 0.6 + 0.4 * eback((t - S[min(i, len(S) - 1)]) / 0.6))
        txt(title, x + 200, y + 62, 46, "Bold", WHITE, a)
        txt(sub, x + 200, y + 135, 31, "Regular", MUTED, a)


def sc_radius(t, d, S, E):
    cx, cy = 600, 540
    L1 = E[1] - S[1]
    rings = [
        (120, "1 km", SAFFRON, S[1] + 0.1),
        (215, "3 km", PINK, S[1] + L1 * 0.38),
        (315, "City", TEAL, S[1] + L1 * 0.72),
        (415, "Global", GREEN, S[2] + 0.9),
    ]
    for r, label, col, st in reversed(rings):
        a = ap(t, st, 0.6)
        if a <= 0:
            continue
        rr = r * (0.55 + 0.45 * eo((t - st) / 0.6))
        circle(cx, cy, rr, fill=col + (26,), outline=col + (220,), width=4, a=a, cache=False)
        txt(label, cx, cy - rr + 10, 30, "Bold", col, a, "m")
    items = [
        ("🧁", -20, 70, 0), ("🛒", 150, 60, 0), ("💇", 215, 80, 0),
        ("🎤", 20, 170, 1), ("👗", 190, 165, 1), ("🏏", 315, 175, 1),
        ("🏛️", 110, 265, 2), ("🎬", 230, 270, 2), ("🍲", 335, 255, 2),
        ("🌍", 35, 370, 3), ("✈️", 155, 365, 3), ("🎶", 310, 360, 3),
    ]
    for e, ang, dist, ring in items:
        st = rings[ring][3] + 0.25
        a = ap(t, st, 0.5)
        if a <= 0:
            continue
        th = math.radians(ang)
        bob = 6 * math.sin(t * 2.2 + ang)
        emo(e, cx + dist * math.cos(th), cy + dist * math.sin(th) + bob, 54, a, 0.5 + 0.5 * eback((t - st) / 0.5))
    pin(cx, cy - 40, 56, ap(t, 0.2, 0.5), PINK, 0.5 + 0.5 * eback((t - 0.2) / 0.6))

    a0 = ap(t, S[0], 0.6)
    txt("Local first.", 1130, 300 + 30 * (1 - a0), 100, "Heavy", WHITE, a0)
    a2 = ap(t, S[2], 0.6)
    txt("Global ready.", 1130, 420 + 30 * (1 - a2), 100, "Heavy", SAFFRON, a2)
    # radius slider
    sa = ap(t, S[0] + 0.6, 0.6)
    x1, x2, y = 1150, 1750, 700
    rrect(x1, y - 6, x2 - x1, 12, 6, (255, 255, 255, 60), a=sa)
    pos = max(0.0, sum(eio((t - st) / 0.6) for *_, st in rings) - 1)
    kx = lerp(x1, x2, pos / 3)
    rrect(x1, y - 6, max(12, kx - x1), 12, 6, SAFFRON + (255,), a=sa, cache=False)
    for i, (_, label, col, st) in enumerate(rings):
        sx = lerp(x1, x2, i / 3)
        on = pos >= i - 0.05
        circle(sx, y, 12, fill=(col if on else MUTED) + (255,), a=sa)
        txt(label, sx, y + 34, 32, "Bold", WHITE if on else MUTED, sa, "m")
    circle(kx, y, 26, fill=WHITE + (255,), outline=SAFFRON + (255,), width=5, a=sa)
    txt("Neighbourhood → City → World", 1150, 820, 34, "Medium", MUTED, sa)


def sc_escalator(t, d, S, E):
    heading("The Local-to-Global Escalator", ap(t, 0.1), sub="Your street becomes your launchpad",
            sub_a=ap(t, S[2], 0.5))
    base, sw, gap = 905, 330, 40
    x0 = (W - (4 * sw + 3 * gap)) / 2
    steps = [("Neighbourhood", 150, SAFFRON, "1.2K"), ("City", 270, PINK, "18K"),
             ("State", 390, TEAL, "240K"), ("Global", 510, GREEN, "3.1M")]
    L1 = E[1] - S[1]
    J = [S[0] + 0.9, S[1] + L1 * 0.30, S[1] + L1 * 0.55, S[1] + L1 * 0.85]
    tops = []
    for i, (name, h, col, views) in enumerate(steps):
        g = eo((t - (0.3 + i * 0.18)) / 0.7)
        hh = h * g
        x = x0 + i * (sw + gap)
        tops.append((x + sw / 2, base - h))
        if hh > 4:
            rrect(x, base - hh, sw, hh, 22, col + (255,), cache=False)
            txt(name, x + sw / 2, base - hh + 22, 38, "Heavy", NAVY, g, "m")
        va = ap(t, J[i] + 0.25, 0.4)
        txt(f"{views} views", x + sw / 2, base - h + 80, 34, "Bold", NAVY, va, "m")
    # card jumping up the steps
    if t >= J[0] - 0.3:
        k = 0
        for i in range(1, 4):
            if t >= J[i] - 0.55:
                k = i
        if k == 0:
            px, py = tops[0]
            s = eback((t - (J[0] - 0.3)) / 0.5)
        else:
            u = eio((t - (J[k] - 0.55)) / 0.55)
            px = lerp(tops[k - 1][0], tops[k][0], u)
            py = lerp(tops[k - 1][1], tops[k][1], u) - 140 * math.sin(math.pi * u)
            s = 1
        rrect(px - 62, py - 190, 124, 170, 20, WHITE + (255,), a=1, cache=True) if s > 0.9 else None
        emo("🎬", px, py - 105, 90, 1, s)
        if k == 3 and t > J[3]:
            emo("🚀", px + 110, py - 200, 70, ap(t, J[3], 0.4))


_screen_cache = {}


def phone_screen(w, h):
    k = (w, h)
    if k not in _screen_cache:
        top = Image.new("RGB", (w, h), PINK)
        bot = Image.new("RGB", (w, h), SAFFRON)
        g = Image.composite(bot, top, Image.linear_gradient("L").resize((w, h))).convert("RGBA")
        mask = rrect_tile(w, h, 44, (255, 255, 255, 255)).getchannel("A")
        g.putalpha(mask)
        _screen_cache[k] = g
    return _screen_cache[k]


def sc_shop(t, d, S, E):
    px, py, pw, ph = 240, 100, 500, 860
    pa = ap(t, 0.1, 0.6)
    oy = 60 * (1 - pa)
    rrect(px, py + oy, pw, ph, 64, (12, 12, 18, 255), outline=(70, 75, 100, 255), width=6, a=pa)
    sx, sy, sw, sh = px + 18, py + 18 + oy, pw - 36, ph - 36
    paste(phone_screen(sw, sh), sx, sy, pa)
    txt("Shorts  •  Nearby", sx + 30, sy + 26, 28, "Semibold", WHITE, pa)
    emo("👗", sx + sw / 2 - 20, sy + 300 + 10 * math.sin(t * 2), 300, pa)
    # side actions
    L1 = E[1] - S[1]
    for i, e in enumerate(["❤️", "💬", "📞"]):
        ey = sy + 220 + i * 105
        sc = 1.0
        if e == "📞" and S[1] <= t <= S[1] + 2.2:
            sc = 1 + 0.25 * abs(math.sin((t - S[1]) * 5))
            circle(sx + sw - 50, ey, 48 * sc, fill=(255, 255, 255, 90), a=pa, cache=False)
        emo(e, sx + sw - 50, ey, 58, pa, sc)
    # info panel
    rrect(sx, sy + sh - 360, sw, 360, 44, (0, 0, 0, 120), a=pa)
    txt("Lakshmi Boutique", sx + 34, sy + sh - 335, 40, "Heavy", WHITE, pa)
    txt("1.2 km away  •  ★ 4.8", sx + 34, sy + sh - 280, 28, "Medium", (235, 235, 245), pa)
    txt("Silk saree   ₹1,499", sx + 34, sy + sh - 220, 38, "Bold", WHITE, pa)
    ba = ap(t, S[0] + 1.0, 0.5)
    bs = 0.7 + 0.3 * eback((t - (S[0] + 1.0)) / 0.5)
    bw, bh = 200, 74
    b1x, b2x, by = sx + 22, sx + 22 + bw + 20, sy + sh - 130
    paste_c(rrect_tile(bw, bh, 37, (255, 255, 255, 30), WHITE + (255,), 3), b1x + bw / 2, by + bh / 2, ba, bs)
    txt("Add to Cart", b1x + bw / 2, by + 16, 30, "Bold", WHITE, ba, "m")
    paste_c(rrect_tile(bw, bh, 37, WHITE + (255,)), b2x + bw / 2, by + bh / 2, ba, bs)
    txt("Order Now", b2x + bw / 2, by + 16, 30, "Heavy", (220, 60, 40), ba, "m")
    tap = S[1] + L1 * 0.8
    if tap <= t <= tap + 0.7:
        u = (t - tap) / 0.7
        circle(b2x + bw / 2, by + bh / 2, 30 + 90 * u, fill=(255, 255, 255, 140), a=1 - u, cache=False)
    ta = ap(t, tap + 0.35, 0.4)
    if ta > 0:
        rrect(sx + 70, sy + 90 - 20 * (1 - ta), sw - 140, 74, 37, GREEN + (255,), a=ta)
        txt("✓  Order placed", sx + sw / 2, sy + 104 - 20 * (1 - ta), 32, "Heavy", WHITE, ta, "m")

    a = ap(t, 0.2, 0.6)
    txt("Shoppable local video", 880, 150 + 20 * (1 - a), 78, "Heavy", WHITE, a)
    bullets = [
        ("Order Now & Add to Cart on every ad", S[0] + 0.5),
        ("Video-call the shop before you buy", S[1]),
        ("Kiranas, boutiques, bakeries, salons", S[1] + L1 * 0.45),
        ("AI turns 3 photos into a video ad", S[2]),
    ]
    for i, (b, st) in enumerate(bullets):
        a = ap(t, st, 0.5)
        y = 330 + i * 120
        check_badge(905, y + 30, 26, a)
        txt(b, 955 + 30 * (1 - a), y + 2, 46, "Semibold", WHITE, a)
    a = ap(t, S[2] + 0.6, 0.5)
    rrect(880, 830, 900, 90, 45, SAFFRON + (40,), outline=SAFFRON + (255,), width=3, a=a)
    emo("✨", 935, 875, 50, a)
    txt("AI Shop Studio: speak 10 sec, get a reel", 975, 850, 36, "Bold", WHITE, a)


def sc_delivery(t, d, S, E):
    heading("Fair for local shops", ap(t, 0.1))
    # batched neighbourhood run
    y = 300
    xs = [260, 720, 1060, 1400, 1700]
    ra = ap(t, 0.3, 0.5)
    for i in range(xs[0], xs[-1], 46):
        rrect(i, y - 4, 26, 8, 4, (255, 255, 255, 70), a=ra)
    emo("🏪", xs[0], y - 10, 110, ra)
    txt("Local shop", xs[0], y + 55, 28, "Semibold", MUTED, ra, "m")
    run = eio((t - (S[0] + 0.3)) / max(1.0, (E[0] - S[0]) + 0.4))
    rx = lerp(xs[0], xs[-1], run)
    for i, hx in enumerate(xs[1:]):
        emo("🏠", hx, y - 10, 90, ra)
        if rx >= hx - 10:
            check_badge(hx + 40, y - 55, 20, ap(t, S[0] + 0.3 + run, 0.01))
    txt("Batched drops  •  Riders from your area", W / 2, y + 100, 34, "Medium", MUTED, ra, "m")
    emo("🛵", rx, y - 20 + 4 * math.sin(t * 12), 100, ra)

    # commission comparison on a Rs 350 order
    L1 = E[1] - S[1]
    a = ap(t, S[1], 0.5)
    txt("On a ₹350 order, the shop pays:", 200, 470, 40, "Bold", WHITE, a)
    maxw = 1260
    g1 = eo((t - (S[1] + L1 * 0.15)) / 1.0)
    txt("Big aggregator apps  (25–30% commission)", 200, 545, 32, "Semibold", MUTED, a)
    rrect(200, 595, max(10, maxw * g1), 70, 16, RED + (255,), a=a, cache=False)
    txt(f"₹{int(87 * g1)}", 220 + maxw * g1, 598, 54, "Heavy", RED, a)
    g2 = eo((t - (S[1] + L1 * 0.45)) / 0.8)
    a2 = ap(t, S[1] + L1 * 0.4, 0.4)
    txt(f"{APP_NAME}  (₹2 fee + ~₹12 shared delivery)", 200, 700, 32, "Semibold", MUTED, a2)
    rrect(200, 750, max(10, maxw * 14 / 87 * g2), 70, 16, GREEN + (255,), a=a2, cache=False)
    txt(f"₹{int(14 * g2)}", 220 + maxw * 14 / 87 * g2, 753, 54, "Heavy", GREEN, a2)
    fa = ap(t, S[2], 0.5)
    fs = 0.7 + 0.3 * eback((t - S[2]) / 0.6)
    paste_c(rrect_tile(640, 84, 42, GREEN + (255,)), 1420, 785, fa, fs)
    txt("FREE delivery above ₹199", 1420, 765, 36, "Heavy", WHITE, fa if fs > 0.95 else 0, "m")


def sc_talent(t, d, S, E):
    heading("Local talent, real fame", ap(t, 0.1))
    L0 = max(1.2, E[0] - S[0])
    floats = [("💃", 330, 380), ("🏏", 640, 420), ("🎤", 360, 690), ("🪔", 650, 730)]
    for i, (e, x, y) in enumerate(floats):
        st = S[0] + i * L0 / 4
        emo(e, x, y + 12 * math.sin(t * 1.8 + i), 170, ap(t, st, 0.4), 0.5 + 0.5 * eback((t - st) / 0.5))
    cx, cy, cw, chh = 900, 230, 860, 640
    a = ap(t, 0.4, 0.6)
    rrect(cx, cy + 30 * (1 - a), cw, chh, 32, CARD + (240,), a=a)
    txt("Ward 12  •  Festival Contest", cx + 40, cy + 36, 40, "Heavy", WHITE, a)
    rrect(cx + cw - 140, cy + 40, 100, 44, 22, RED + (255,), a=a)
    txt("LIVE", cx + cw - 90, cy + 44, 26, "Heavy", WHITE, a, "m")
    rows = [("🥇", "Garba Queens", "Dance • Ward 12", 2431),
            ("🥈", "Galli Cricket XI", "Sports • Sector 4", 1987),
            ("🥉", "Riya Sharma", "Classical vocals", 1520)]
    L1 = E[1] - S[1]
    for i, (m, name, sub, votes) in enumerate(rows):
        st = S[1] + i * 0.35
        ra = ap(t, st, 0.5)
        ry = cy + 120 + i * 135
        rrect(cx + 30, ry + 20 * (1 - ra), cw - 60, 115, 22, (255, 255, 255, 18), a=ra)
        emo(m, cx + 95, ry + 57, 72, ra)
        txt(name, cx + 160, ry + 18, 40, "Bold", WHITE, ra)
        txt(sub, cx + 160, ry + 68, 28, "Regular", MUTED, ra)
        v = int(votes * eo((t - st) / 2.2))
        txt(f"{v:,} votes", cx + cw - 60, ry + 34, 36, "Heavy", SAFFRON, ra, "r")
    sa = ap(t, S[1] + L1 * 0.7, 0.5)
    rrect(cx + 30, cy + chh - 100, cw - 60, 70, 35, SAFFRON + (45,), outline=SAFFRON + (255,), width=2, a=sa)
    emo("🍬", cx + 80, cy + chh - 65, 44, sa)
    txt("Prize sponsored by Sharma Sweets", cx + 120, cy + chh - 88, 32, "Bold", WHITE, sa)


def sc_civic(t, d, S, E):
    heading("Civic voice that reaches the ward", ap(t, 0.1))
    x, y, w, h = 170, 230, 720, 600
    a = ap(t, S[0] + 0.1, 0.6)
    rrect(x, y + 30 * (1 - a), w, h, 30, CARD + (240,), a=a)
    rrect(x + 24, y + 24, w - 48, 270, 22, (55, 60, 75, 255), a=a)
    emo("🚧", x + w / 2 - 90, y + 160, 150, a)
    emo("🕳️", x + w / 2 + 100, y + 175, 130, a)
    ga = ap(t, S[1], 0.4)
    rrect(x + 44, y + 44, 230, 50, 25, (0, 0, 0, 160), a=ga)
    txt("● LIVE PHOTO", x + 159, y + 50, 26, "Bold", (255, 90, 90), ga, "m")
    txt("Pothole near City Bus Stop", x + 34, y + 320, 42, "Heavy", WHITE, a)
    txt("Ward 12  •  Geo-tagged  •  2 min ago", x + 34, y + 380, 28, "Medium", MUTED, ga)
    L2 = E[2] - S[2]
    va = ap(t, S[2], 0.4)
    rrect(x + 34, y + 460, w - 68, 96, 48, (255, 255, 255, 25), outline=GREEN + (255,), width=3, a=va)
    emo("👍", x + 95, y + 508, 54, va)
    txt("I face this too", x + 140, y + 482, 38, "Bold", WHITE, va)
    n = int(128 * eo((t - S[2]) / 2.5))
    txt(str(n), x + w - 70, y + 478, 46, "Heavy", GREEN, va, "r")

    steps = [("Reported", "Live geo-tagged photo", S[1]),
             ("Verified", "Neighbours upvote", S[2] + L2 * 0.05),
             ("Assigned", "Ward corporator notified", S[2] + L2 * 0.40),
             ("Fixed", "Before & after, in public", S[2] + L2 * 0.80)]
    sx, sy0, gap = 1060, 270, 150
    for i, (name, sub, st) in enumerate(steps):
        on = ap(t, st, 0.4)
        yy = sy0 + i * gap
        if i < 3:
            rrect(sx - 4, yy + 36, 8, gap - 36, 4, (255, 255, 255, 50))
            nxt = eo((t - steps[i + 1][2] + 0.4) / 0.4)
            if nxt > 0:
                rrect(sx - 4, yy + 36, 8, max(2, (gap - 36) * nxt), 4, GREEN + (255,), cache=False)
        circle(sx, yy + 30, 34, fill=(60, 70, 110, 255))
        if on > 0:
            check_badge(sx, yy + 30, 34, on, GREEN if name != "Fixed" else SAFFRON)
        txt(name, sx + 70, yy - 2, 44, "Heavy", WHITE if on > 0.5 else MUTED)
        txt(sub, sx + 70, yy + 52, 30, "Regular", MUTED)
    na = ap(t, steps[2][2] + 0.2, 0.4)
    emo("🔔", 1700, sy0 + 2 * gap + 30, 60, na, 1 + 0.15 * math.sin(t * 14) * na)


def sc_super(t, d, S, E):
    heading("One app for your whole neighbourhood", ap(t, 0.1))
    tiles = [("🎬", "Shorts"), ("📺", "Long videos"), ("💬", "Chat"), ("📞", "Voice & video"),
             ("🛒", "Shop nearby"), ("📣", "Local ads"), ("🏆", "Contests"), ("🏛️", "Civic voice")]
    tw, th, g = 360, 290, 40
    x0, y0 = (W - (4 * tw + 3 * g)) / 2, 250
    L0 = E[0] - S[0]
    glow = ap(t, S[1], 0.4)
    for i, (e, label) in enumerate(tiles):
        st = S[0] + i * L0 / 8.5
        a = ap(t, st, 0.4)
        s = 0.6 + 0.4 * eback((t - st) / 0.5)
        x = x0 + (i % 4) * (tw + g)
        y = y0 + (i // 4) * (th + g)
        tile = rrect_tile(tw, th, 30, CARD + (240,), SAFFRON + (255,) if glow > 0.5 else None, 4 if glow > 0.5 else 0)
        paste_c(tile, x + tw / 2, y + th / 2, a, s)
        emo(e, x + tw / 2, y + 120, 120, a, s)
        txt(label, x + tw / 2, y + 205, 40, "Bold", WHITE, a if s > 0.9 else 0, "m")


def sc_business(t, d, S, E):
    heading("Simple, sustainable revenue", ap(t, 0.1))
    cols = [("🧾", "₹1–2", "per order fee"), ("📣", "₹99/day", "hyperlocal video ads"),
            ("🧰", "₹199/mo", "shop growth tools")]
    cw, chh, g = 480, 330, 60
    x0, y0 = (W - (3 * cw + 2 * g)) / 2, 230
    L0 = E[0] - S[0]
    for i, (e, val, label) in enumerate(cols):
        st = S[0] + L0 * (0.25 + i * 0.25)
        a = ap(t, st, 0.5)
        x = x0 + i * (cw + g)
        yy = y0 + 40 * (1 - a)
        rrect(x, yy, cw, chh, 30, CARD + (240,), a=a)
        emo(e, x + cw / 2, yy + 85, 90, a)
        txt(val, x + cw / 2, yy + 150, 76, "Heavy", SAFFRON, a, "m")
        txt(label, x + cw / 2, yy + 255, 34, "Medium", MUTED, a, "m")
    # expansion pins
    labels = {0: "Pilot locality", 5: "10 localities", 11: "Multi-city"}
    L1 = max(1.5, E[1] - S[1])
    for i in range(12):
        st = S[1] + i * L1 / 13
        a = ap(t, st, 0.35)
        px = 300 + i * 120
        size = 40 + i * 3
        pin(px, 700 + 8 * math.sin(t * 2 + i), size, a, [SAFFRON, PINK, TEAL, GREEN][i % 4],
            0.4 + 0.6 * eback((t - st) / 0.5))
        if i in labels:
            txt(labels[i], px, 790, 30, "Bold", WHITE, a, "m")
    rrect(300, 838, 1320 * eo((t - S[1]) / L1), 6, 3, (255, 255, 255, 90), cache=False)


def sc_outro(t, d, S, E):
    cx, cy = W / 2, 300
    for k in range(3):
        ph = ((t - k * 0.7) % 2.1) / 2.1
        circle(cx, cy + 40, 70 + 320 * ph, outline=SAFFRON + (255,), width=4, a=(1 - ph) * 0.45, cache=False)
    pin(cx, cy, 170, ap(t, 0.0, 0.4), scale=0.6 + 0.4 * eback(t / 0.6))
    a = ap(t, S[0], 0.5)
    txt(APP_NAME, W / 2, 470 + 30 * (1 - a), 150, "Heavy", WHITE, a, "m")
    txt("Your neighbourhood. Your network.", W / 2, 670, 52, "Semibold", WHITE, ap(t, S[1], 0.5), "m")
    a2 = ap(t, S[2], 0.5)
    txt(TAGLINE, W / 2, 770, 80, "Heavy", SAFFRON, a2, "m")
    txt("Coming soon to your neighbourhood", W / 2, 900, 34, "Medium", MUTED, ap(t, S[2] + 0.8, 0.5), "m")


SCENES = [
    dict(draw=sc_title, bg=0, logo=False, cap=False, lines=[
        f"Meet {APP_NAME}.",
        "The super app for your neighbourhood.",
        "Vocal for local, built right in."]),
    dict(draw=sc_problem, bg=1, lines=[
        "Today, the neighbourhood bakery is invisible on global feeds.",
        "Delivery apps take up to thirty percent commission.",
        "Followers are often fake.",
        "And real civic problems get lost in online noise."]),
    dict(draw=sc_radius, bg=2, lines=[
        f"{APP_NAME} shows you what is happening around you first.",
        "One kilometre, three kilometres, your whole city.",
        "And whenever you want, the entire world."]),
    dict(draw=sc_escalator, bg=0, lines=[
        "Every short starts in its own neighbourhood.",
        "If locals love it, it climbs to the city, then the state, and then goes global.",
        "Your street becomes your launchpad."]),
    dict(draw=sc_shop, bg=1, lines=[
        "Shops post short video ads with Order Now and Add to Cart buttons.",
        "Video call the boutique, see the saree live, and buy in a single tap.",
        "AI can even turn three photos and a voice note into a ready-made video ad."]),
    dict(draw=sc_delivery, bg=2, lines=[
        "Delivery partners from your own area make batched neighbourhood runs.",
        "So on a three hundred and fifty rupee order, a shop pays about fourteen rupees, "
        "instead of nearly ninety on big aggregator apps.",
        "That makes free delivery possible."]),
    dict(draw=sc_talent, bg=0, lines=[
        "Dance, cricket, music and festivals.",
        "Local contests give talent real recognition from real neighbours, sponsored by local businesses."]),
    dict(draw=sc_civic, bg=1, lines=[
        "Spot a pothole or a broken streetlight?",
        "Post a geo-tagged report.",
        "Neighbours verify it, your ward representative is notified, and the fix is tracked in public."]),
    dict(draw=sc_super, bg=2, lines=[
        "Shorts and long videos, chat, voice and video calls, shopping, local ads, contests and civic voice.",
        "All in one app."]),
    dict(draw=sc_business, bg=0, lines=[
        f"{APP_NAME} earns from a tiny per-order fee, hyperlocal ads, and simple tools for shopkeepers.",
        "And it grows neighbourhood by neighbourhood, city by city."]),
    dict(draw=sc_outro, bg=1, logo=False, cap=False, lines=[
        f"{APP_NAME}.",
        "Your neighbourhood. Your network.",
        "Be vocal for local."]),
]

LEAD, GAP, TAIL = 0.5, 0.35, 0.9


# ----------------------------------------------------------------- audio ----
def spoken(s):
    return s.replace("MyLocal", "My Local")


def tts(text):
    h = hashlib.md5(f"{VOICE}|{RATE}|{text}".encode()).hexdigest()[:12]
    path = os.path.join(BUILD, f"tts_{h}.wav")
    if not os.path.exists(path):
        subprocess.run(["say", "-v", VOICE, "-r", str(RATE), "--file-format=WAVE",
                        f"--data-format=LEI16@{SR}", "-o", path, spoken(text)], check=True)
    with wave.open(path) as wf:
        assert wf.getframerate() == SR and wf.getsampwidth() == 2 and wf.getnchannels() == 1
        return wf.readframes(wf.getnframes())


def build_timeline():
    pcm = bytearray()
    for sc in SCENES:
        cur = LEAD
        S, E = [], []
        chunks = []
        for line in sc["lines"]:
            data = tts(line)
            dur = len(data) / 2 / SR
            S.append(cur)
            E.append(cur + dur)
            chunks.append((cur, data))
            cur += dur + GAP
        dur = cur - GAP + TAIL
        nfr = math.ceil(dur * FPS)
        dur = nfr / FPS
        buf = bytearray(int(round(dur * SR)) * 2)
        for st, data in chunks:
            o = int(round(st * SR)) * 2
            buf[o:o + len(data)] = data
        pcm += buf
        sc.update(S=S, E=E, dur=dur, nfr=nfr)
    path = os.path.join(BUILD, "narration.wav")
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(bytes(pcm))
    return path


# ---------------------------------------------------------------- render ----
BGS = None


def render(sc, t, gt, total):
    global B
    bg = BGS[sc["bg"]]
    B = bg.copy()
    sc["draw"](t, sc["dur"], sc["S"], sc["E"])
    if sc.get("logo", True):
        logo_small()
    if sc.get("cap", True):
        for i, line in enumerate(sc["lines"]):
            s0, e0 = sc["S"][i] - 0.05, sc["E"][i] + 0.25
            if s0 <= t <= e0:
                caption(line, min(cl((t - s0) / 0.15), cl((e0 - t) / 0.15)))
    rrect(0, H - 6, max(2, W * gt / total), 6, 0, SAFFRON + (255,), cache=False)
    fade = min(cl(t / 0.25), cl((sc["dur"] - t) / 0.3))
    if fade < 1:
        B = Image.blend(bg, B, fade)
    return B.convert("RGB")


def main():
    global BGS
    preview = "--preview" in sys.argv
    BGS = [make_bg((70, 35, 0), (0, 45, 60)), make_bg((60, 10, 40), (50, 30, 0)), make_bg((0, 40, 55), (45, 10, 40))]
    audio = build_timeline()
    total = sum(sc["dur"] for sc in SCENES)
    print(f"Timeline: {len(SCENES)} scenes, {total:.1f}s")

    if preview:
        thumbs = []
        gt = 0
        for sc in SCENES:
            t = min(sc["dur"] - 0.5, sc["E"][-1] + 0.2)
            thumbs.append(render(sc, t, gt + t, total).resize((960, 540)))
            gt += sc["dur"]
        for n in range(0, len(thumbs), 4):
            sheet = Image.new("RGB", (1920, 1080), (0, 0, 0))
            for j, th in enumerate(thumbs[n:n + 4]):
                sheet.paste(th, ((j % 2) * 960, (j // 2) * 540))
            p = os.path.join(BUILD, f"preview_{n // 4 + 1}.png")
            sheet.save(p)
            print("wrote", p)
        return

    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-i", audio,
           "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-shortest", OUT]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    gt = 0.0
    for si, sc in enumerate(SCENES):
        for f in range(sc["nfr"]):
            t = f / FPS
            proc.stdin.write(render(sc, t, gt + t, total).tobytes())
        gt += sc["dur"]
        print(f"  scene {si + 1}/{len(SCENES)} done", flush=True)
    proc.stdin.close()
    proc.wait()
    print("wrote", OUT)


if __name__ == "__main__":
    main()
