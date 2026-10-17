"""Server-rendered share cards (PNG). Only counted (approved) data; estimates labelled "est."."""
import io
import os

import httpx
from PIL import Image, ImageDraw, ImageFont

SIZES = {"portrait": (1080, 1350), "x": (1200, 675)}
FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
GREEN, RED, INK, MUTED, BG = (22, 128, 61), (185, 28, 28), (17, 17, 17), (100, 100, 100), (250, 250, 247)
FOOTER = "outputrank.com"
_photo_cache = {}


def font(px, bold=False):
    return ImageFont.truetype(os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"), px)


def _fit(draw, text, px, max_w, bold=False):
    while px > 14 and draw.textlength(text, font=font(px, bold)) > max_w:
        px -= 2
    return font(px, bold)


def fetch_photo(url):
    """Download a licensed photo once; None on any failure (card falls back to initials)."""
    if url in _photo_cache:
        return _photo_cache[url]
    img = None
    try:
        r = httpx.get(url, timeout=5, follow_redirects=True, headers={"User-Agent": "OutputRankCards/1.0 (outputrank.com)"})
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception:
        img = None
    _photo_cache[url] = img
    return img


def _avatar(img, d, initials):
    out = Image.new("RGB", (d, d), (220, 220, 215))
    if img is not None:
        w, h = img.size
        s = min(w, h)
        out = img.crop(((w - s) // 2, 0 if h > w else (h - s) // 2, (w - s) // 2 + s, (0 if h > w else (h - s) // 2) + s)).resize((d, d))
    else:
        dr = ImageDraw.Draw(out)
        f = font(d // 3, True)
        dr.text((d / 2, d / 2), initials, font=f, fill=MUTED, anchor="mm")
    mask = Image.new("L", (d, d), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, d, d), fill=255)
    return out, mask


def _png(im):
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _footer(dr, W, H, note=""):
    dr.line((60, H - 90, W - 60, H - 90), fill=(210, 210, 205), width=2)
    dr.text((60, H - 50), FOOTER, font=font(30, True), fill=INK, anchor="lm")
    if note:
        dr.text((W - 60, H - 50), note, font=_fit(dr, note, 20, W - 420), fill=MUTED, anchor="rm")


def person_card(name, initials, lines, size="portrait", photo=None, credit=None):
    """lines: [(sign, text, estimated)] with sign '+' (green) or '-' (red)."""
    W, H = SIZES[size]
    im = Image.new("RGB", (W, H), BG)
    dr = ImageDraw.Draw(im)
    portrait = size == "portrait"
    d = 360 if portrait else 300
    av, mask = _avatar(photo, d, initials)
    ax, ay = ((W - d) // 2, 90) if portrait else (60, (H - 90 - d) // 2)
    im.paste(av, (ax, ay), mask)
    if photo is not None and credit:
        c = f"Photo: {credit}"
        dr.text((ax + d / 2, ay + d + 22), c, font=_fit(dr, c, 18, d + 200), fill=MUTED, anchor="mm")
    if portrait:
        x, y, maxw, anchor = W / 2, ay + d + 110, W - 120, "mm"
    else:
        x, y, maxw, anchor = ax + d + 60, 110, W - (ax + d + 60) - 60, "lm"
    dr.text((x, y), name, font=_fit(dr, name, 76 if portrait else 60, maxw, True), fill=INK, anchor=anchor)
    y += 100 if portrait else 85
    step = 70 if portrait else 58
    shown = lines[: (10 if portrait else 6)]
    if not shown:
        dr.text((x, y), "No counted entries yet", font=font(40), fill=MUTED, anchor=anchor)
    for sign, text, est in shown:
        t = text + ("  est." if est else "")
        dr.text((x, y), t, font=_fit(dr, t, 48 if portrait else 40, maxw, True), fill=GREEN if sign == "+" else RED, anchor=anchor)
        y += step
    _footer(dr, W, H, "Sourced, counted entries only")
    return _png(im)


def tab_card(title, rows, size="portrait"):
    """rows: [(name, sign, label, estimated)] top 5."""
    W, H = SIZES[size]
    im = Image.new("RGB", (W, H), BG)
    dr = ImageDraw.Draw(im)
    portrait = size == "portrait"
    dr.text((60, 110 if portrait else 70), title, font=_fit(dr, title, 72 if portrait else 56, W - 120, True), fill=INK, anchor="lm")
    y = 260 if portrait else 160
    step = 190 if portrait else 88
    if not rows:
        dr.text((60, y), "No counted entries yet", font=font(40), fill=MUTED, anchor="lm")
    for i, (name, sign, label, est) in enumerate(rows[:5], 1):
        lab = label + ("  est." if est else "")
        lf = font(48 if portrait else 40, True)
        lw = dr.textlength(lab, font=lf)
        dr.text((60, y), f"{i}.", font=font(52 if portrait else 40, True), fill=MUTED, anchor="lm")
        dr.text((150 if portrait else 120, y), name, font=_fit(dr, name, 52 if portrait else 40, W - 240 - lw - 120, True), fill=INK, anchor="lm")
        dr.text((W - 60, y), lab, font=lf, fill=GREEN if sign == "+" else RED, anchor="rm")
        y += step
    _footer(dr, W, H, "Sourced, counted entries only")
    return _png(im)
