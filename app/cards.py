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
# Wikimedia requires a descriptive User-Agent with contact info (https://w.wiki/4wJS).
UA = "OutputRankCards/1.0 (https://github.com/outputsi/output; papimalon@users.noreply.github.com)"


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
        r = httpx.get(url, timeout=5, follow_redirects=True, headers={"User-Agent": UA})
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception:
        return None  # not cached, so a transient failure is retried next time
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
        if portrait:
            dr.text((ax + d / 2, ay + d + 22), c, font=_fit(dr, c, 18, W - 120), fill=MUTED, anchor="mm")
        else:  # left-aligned under the avatar so it never runs off the edge
            dr.text((ax, ay + d + 22), c, font=_fit(dr, c, 18, W - 120), fill=MUTED, anchor="lm")
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


def tab_card(title, rows, size="portrait", note="Sourced, counted entries only"):
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
        col = GREEN if sign == "+" else RED if sign == "-" else INK
        if portrait:  # name, then its number underneath
            dr.text((60, y), f"{i}.", font=font(52, True), fill=MUTED, anchor="lm")
            dr.text((150, y), name, font=_fit(dr, name, 52, W - 210, True), fill=INK, anchor="lm")
            dr.text((150, y + 64), lab, font=_fit(dr, lab, 46, W - 210, True), fill=col, anchor="lm")
        else:
            lf = _fit(dr, lab, 38, (W - 180) // 2, True)
            lw = dr.textlength(lab, font=lf)
            nf = font(38, True)
            n, room = name, W - 180 - lw - 40
            while len(n) > 3 and dr.textlength(n, font=nf) > room:
                n = n[:-2].rstrip() + "\u2026"
            dr.text((60, y), f"{i}.", font=font(38, True), fill=MUTED, anchor="lm")
            dr.text((120, y), n, font=nf, fill=INK, anchor="lm")
            dr.text((W - 60, y), lab, font=lf, fill=col, anchor="rm")
        y += step
    _footer(dr, W, H, note)
    return _png(im)


def favicon_ico():
    """Simple 'O' mark (white ring on dark rounded square) as a multi-size .ico."""
    s = 64
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=14, fill=(34, 34, 34, 255))
    d.ellipse((15, 15, 49, 49), outline=(255, 255, 255, 255), width=8)
    out = io.BytesIO()
    im.save(out, format="ICO", sizes=[(16, 16), (32, 32), (48, 48)])
    return out.getvalue()


def load_photo(path):
    """Mirrored local photo (app/static/people); cached like remote ones."""
    if path not in _photo_cache:
        try:
            _photo_cache[path] = Image.open(path).convert("RGB")
        except Exception:
            return None
    return _photo_cache[path]
