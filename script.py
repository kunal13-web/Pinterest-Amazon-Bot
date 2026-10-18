"""StyleNest Pinterest bot.

Reads products.csv, builds a text-based pin image with Pillow on top of a
stock lifestyle photo (Pixabay or Pexels) (no Amazon product photos: Associates terms do not allow them without the API), and
posts it to Pinterest with your affiliate link.

Env vars:
  PINTEREST_ACCESS_TOKEN  Pinterest API v5 token (scopes: pins:write, boards:read)
  PINTEREST_BOARD_ID      Target board ID (a CSV row can override with board_id)
  AMAZON_TAG              Associates tag (default: stylenestus-20)
  PIXABAY_API_KEY         Free key from pixabay.com/api/docs (photos for the pins)
  PEXELS_API_KEY          Optional alternative photo source (pexels.com/api)
  PINS_PER_RUN            How many pins to post per run (default: 2)
  DRY_RUN                 "true" = build images, print payload, post nothing
  PINTEREST_API_BASE      Use https://api-sandbox.pinterest.com/v5 for trial/sandbox
"""
import base64
import csv
import hashlib
import io
import json
import os
import re
import sys
import textwrap
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).parent
CSV_PATH = Path(os.getenv("PRODUCTS_CSV", ROOT / "products.csv"))
POSTED_PATH = ROOT / "posted.json"
OUT_DIR = ROOT / "output"

TAG = os.getenv("AMAZON_TAG", "stylenestus-20")
API_BASE = os.getenv("PINTEREST_API_BASE", "https://api.pinterest.com/v5").rstrip("/")
TOKEN = os.getenv("PINTEREST_ACCESS_TOKEN", "")
PEXELS_KEY = os.getenv("PEXELS_API_KEY", "")
PIXABAY_KEY = os.getenv("PIXABAY_API_KEY", "")
DEFAULT_BOARD = os.getenv("PINTEREST_BOARD_ID", "")
PINS_PER_RUN = int(os.getenv("PINS_PER_RUN", "2"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true" or not TOKEN

W, H = 1000, 1500
ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
DISCLOSURE = "As an Amazon Associate I earn from qualifying purchases. #ad"

# (panel background, headline text, accent/button) per category
PALETTES = {
    "home": ((244, 239, 226), (34, 70, 52), (34, 87, 62)),
    "beauty": ((252, 232, 228), (120, 48, 72), (196, 98, 120)),
    "seasonal": ((250, 238, 220), (150, 28, 40), (150, 28, 40)),
    "garden": ((238, 243, 224), (40, 70, 34), (70, 110, 50)),
    "fashion": ((240, 232, 224), (60, 46, 46), (110, 78, 70)),
}
DEFAULT_PALETTE = ((240, 236, 228), (40, 44, 56), (52, 64, 84))

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_SERIF = "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"


def font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def wrap(draw, text, fnt, max_width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=fnt) <= max_width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _download(url):
    return Image.open(io.BytesIO(requests.get(url, timeout=60).content)).convert("RGB")


def _seed_int(seed):
    return int(hashlib.md5(seed.encode()).hexdigest(), 16)


def fetch_photos(query, seed, n=1, ids=None):
    """Download n portrait stock photos. Returns (list of PIL images, list of credits).

    ids: optional list of Pixabay photo IDs to use instead of searching.
    Uses Pixabay if PIXABAY_API_KEY is set, otherwise Pexels if PEXELS_API_KEY is set.
    """
    if not (PIXABAY_KEY or PEXELS_KEY) or not (query or ids):
        return [], []
    photos, credits = [], []
    try:
        if PIXABAY_KEY:
            hits = []
            if ids:
                for pid in ids:
                    r = requests.get("https://pixabay.com/api/", params={"key": PIXABAY_KEY, "id": pid}, timeout=30)
                    r.raise_for_status()
                    hits += r.json().get("hits", [])
            else:
                r = requests.get(
                    "https://pixabay.com/api/",
                    params={
                        "key": PIXABAY_KEY,
                        "q": query[:100],
                        "image_type": "photo",
                        "orientation": "vertical",
                        "min_height": 1200,
                        "safesearch": "true",
                        "per_page": 30,
                    },
                    timeout=30,
                )
                r.raise_for_status()
                found = r.json().get("hits", [])
                start = _seed_int(seed) % max(len(found), 1)
                hits = [found[(start + i * 3) % len(found)] for i in range(min(n, len(found)))] if found else []
            for p in hits[:n]:
                photos.append(_download(p["largeImageURL"]))
                credits.append(f"Photo by {p.get('user', 'unknown')} on Pixabay")
        else:
            r = requests.get(
                "https://api.pexels.com/v1/search",
                headers={"Authorization": PEXELS_KEY},
                params={"query": query, "orientation": "portrait", "size": "large", "per_page": 30},
                timeout=30,
            )
            r.raise_for_status()
            found = r.json().get("photos", [])
            start = _seed_int(seed) % max(len(found), 1)
            for i in range(min(n, len(found))):
                p = found[(start + i * 3) % len(found)]
                photos.append(_download(p["src"].get("large2x") or p["src"]["original"]))
                credits.append(f"Photo by {p.get('photographer', 'unknown')} on Pexels")
        if not photos:
            print(f"no photos found for {query!r}")
    except Exception as e:
        print(f"photo fetch failed for {query!r}: {e}")
    return photos, credits


def cover(img, w, h):
    scale = max(w / img.width, h / img.height)
    img = img.resize((int(img.width * scale) + 1, int(img.height * scale) + 1), Image.LANCZOS)
    left, top = (img.width - w) // 2, (img.height - h) // 4  # bias crop toward the top
    return img.crop((left, top, left + w, top + h))


def fit_text(d, text, path, max_w, max_h, start=76, minimum=44, max_lines=5):
    size = start
    while True:
        f = font(path, size)
        lines = wrap(d, text, f, max_w)
        step = int(size * 1.22)
        if (len(lines) <= max_lines and len(lines) * step <= max_h) or size <= minimum:
            return f, lines[:max_lines], step
        size -= 4


def bottom_shade(img, height=380, strength=190):
    shade = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)
    for i in range(height):
        sd.line([(0, H - height + i), (W, H - height + i)], fill=(0, 0, 0, int(strength * i / height)))
    return Image.alpha_composite(img.convert("RGBA"), shade).convert("RGB")


def draw_cta(d, y, fill, text_col, label="SHOP THE LOOK ON AMAZON", pad=70):
    d.rounded_rectangle([pad, y, W - pad, y + 104], radius=52, fill=fill)
    f = font(FONT_BOLD, 42)
    while d.textlength(label, font=f) > W - 2 * pad - 40:
        f = font(FONT_BOLD, f.size - 2)
    d.text(((W - d.textlength(label, font=f)) / 2, y + 52 - f.size / 2 - 4), label, font=f, fill=text_col)
    small = font(FONT_REG, 25)
    note = "#ad  As an Amazon Associate I earn from qualifying purchases"
    d.text(((W - d.textlength(note, font=small)) / 2, y + 136), note, font=small, fill=(235, 235, 235))


def centered(d, lines, f, step, y, fill):
    for line in lines:
        d.text(((W - d.textlength(line, font=f)) / 2, y), line, font=f, fill=fill)
        y += step
    return y


def layout_panel(photos, row, pal):
    """Headline panel on top, one photo below."""
    panel_bg, text_col, accent = pal
    img = bottom_shade(cover(photos[0], W, H))
    d = ImageDraw.Draw(img)
    pad = 70
    f, lines, step = fit_text(d, row["headline"].strip(), FONT_SERIF, W - 2 * pad, 4 * 90, start=70, max_lines=4)
    panel_h = 60 + 44 + len(lines) * step + 50
    d.rectangle([0, 0, W, panel_h], fill=panel_bg)
    d.text((pad, 52), "STYLENEST", font=font(FONT_BOLD, 28), fill=accent)
    d.line([(pad, 96), (pad + 90, 96)], fill=accent, width=4)
    y = 124
    for line in lines:
        d.text((pad, y), line, font=f, fill=text_col)
        y += step
    draw_cta(d, H - 250, panel_bg, text_col)
    return img


def layout_card(photos, row, pal):
    """Full-bleed photo with a centred headline card."""
    panel_bg, text_col, accent = pal
    img = bottom_shade(cover(photos[0], W, H))
    d = ImageDraw.Draw(img)
    f, lines, step = fit_text(d, row["headline"].strip(), FONT_SERIF, 700, 5 * 84, start=68, max_lines=5)
    card_h = 110 + len(lines) * step + 70
    top = 330
    card = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(card).rounded_rectangle([110, top, W - 110, top + card_h], radius=36, fill=panel_bg + (238,))
    img = Image.alpha_composite(img.convert("RGBA"), card).convert("RGB")
    d = ImageDraw.Draw(img)
    lab = "STYLENEST"
    lf = font(FONT_BOLD, 26)
    d.text(((W - d.textlength(lab, font=lf)) / 2, top + 40), lab, font=lf, fill=accent)
    d.line([(W / 2 - 45, top + 82), (W / 2 + 45, top + 82)], fill=accent, width=4)
    centered(d, lines, f, step, top + 110, text_col)
    draw_cta(d, H - 250, panel_bg, text_col)
    return img


def layout_collage(photos, row, pal):
    """Three-photo collage under a centred headline, accent footer."""
    panel_bg, text_col, accent = pal
    img = Image.new("RGB", (W, H), panel_bg)
    d = ImageDraw.Draw(img)
    pad = 60
    f, lines, step = fit_text(d, row["headline"].strip(), FONT_SERIF, W - 2 * pad, 4 * 80, start=64, max_lines=4)
    lf = font(FONT_BOLD, 26)
    d.text(((W - d.textlength("STYLENEST", font=lf)) / 2, 46), "STYLENEST", font=lf, fill=accent)
    y = centered(d, lines, f, step, 92, text_col)
    top, bottom, g = y + 24, H - 250, 12
    big_w = 560
    img.paste(cover(photos[0], big_w, bottom - top), (0, top))
    half = (bottom - top - g) // 2
    img.paste(cover(photos[1], W - big_w - g, half), (big_w + g, top))
    img.paste(cover(photos[2], W - big_w - g, bottom - top - g - half), (big_w + g, top + half + g))
    d.rectangle([0, bottom, W, H], fill=accent)
    draw_cta(d, bottom + 30, panel_bg, text_col)
    return img


def layout_banner(photos, row, pal):
    """Full-bleed photo, coloured headline banner low on the page, optional badge."""
    panel_bg, text_col, accent = pal
    img = bottom_shade(cover(photos[0], W, H), height=300)
    d = ImageDraw.Draw(img)
    pad = 70
    f, lines, step = fit_text(d, row["headline"].strip(), FONT_SERIF, W - 2 * pad - 20, 4 * 84, start=68, max_lines=4)
    band_h = 40 + len(lines) * step + 40
    band_top = H - 300 - band_h
    d.rectangle([0, band_top, W, band_top + band_h], fill=accent)
    centered(d, lines, f, step, band_top + 34, panel_bg)
    lf = font(FONT_BOLD, 26)
    d.text(((W - d.textlength("STYLENEST", font=lf)) / 2, 60), "STYLENEST", font=lf, fill="white")
    badge = (row.get("badge") or "").strip().upper()
    if badge:
        bf, blines, bstep = fit_text(d, badge, FONT_BOLD, 150, 120, start=30, minimum=18, max_lines=3)
        cx, cy, r = W - 150, 220, 105
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=panel_bg)
        d.ellipse([cx - r + 8, cy - r + 8, cx + r - 8, cy + r - 8], outline=accent, width=4)
        yy = cy - len(blines) * bstep / 2
        for line in blines:
            d.text((cx - d.textlength(line, font=bf) / 2, yy), line, font=bf, fill=text_col)
            yy += bstep
    draw_cta(d, H - 250, panel_bg, text_col)
    return img


LAYOUTS = {"panel": (layout_panel, 1), "card": (layout_card, 1), "collage": (layout_collage, 3), "banner": (layout_banner, 1)}
ORDER = ["panel", "collage", "banner", "card"]


def build_image(row, asin, variant=0):
    pal = PALETTES.get(row.get("category", "").strip().lower(), DEFAULT_PALETTE)
    name = (row.get("layout") or "").strip().lower()
    if name not in LAYOUTS:
        name = ORDER[variant % len(ORDER)]  # rotate so consecutive pins look different
    fn, n = LAYOUTS[name]
    query = (row.get("image_query") or "").strip() or row.get("keywords", "").split(",")[0].strip()
    ids = [i.strip() for i in (row.get("photo_ids") or "").replace("|", ",").split(",") if i.strip()]
    photos, credits = fetch_photos(query, asin, n, ids)
    if not photos:  # fallback: plain colour tiles so the bot never stops
        photos = [Image.new("RGB", (W, H), pal[2])]
    while len(photos) < n:
        photos.append(photos[len(photos) % len(photos)])
    print(f"{asin}: layout={name}, photos={len(credits)}")
    return fn(photos, row, pal), "; ".join(dict.fromkeys(credits))


def description(row, credit=""):
    bullets = [b.strip() for b in row.get("bullets", "").split("|") if b.strip()]
    tags = " ".join("#" + re.sub(r"\W+", "", k.strip().title()) for k in row.get("keywords", "").split(",") if k.strip())
    parts = [row["headline"].strip(), ""] + [f"- {b}" for b in bullets] + ["", DISCLOSURE] + ([credit] if credit else []) + [tags]
    return "\n".join(parts).strip()[:800]


def load_posted():
    if POSTED_PATH.exists():
        return set(json.loads(POSTED_PATH.read_text()))
    return set()


def save_posted(posted):
    POSTED_PATH.write_text(json.dumps(sorted(posted), indent=2) + "\n")


def load_rows():
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def post_pin(row, img, link, credit=""):
    board = (row.get("board_id") or "").strip() or DEFAULT_BOARD
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    payload = {
        "board_id": board,
        "title": row["headline"].strip()[:100],
        "description": description(row, credit),
        "link": link,
        "alt_text": row["headline"].strip()[:500],
        "media_source": {
            "source_type": "image_base64",
            "content_type": "image/jpeg",
            "data": base64.b64encode(buf.getvalue()).decode(),
        },
    }
    if DRY_RUN:
        shown = {**payload, "media_source": {**payload["media_source"], "data": "<omitted>"}}
        print("DRY RUN payload:", json.dumps(shown, indent=2))
        return None
    if not board:
        raise RuntimeError("No board id: set PINTEREST_BOARD_ID or add board_id to the CSV row")
    r = requests.post(
        f"{API_BASE}/pins",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Pinterest API {r.status_code}: {r.text[:400]}")
    return r.json().get("id")


def main():
    if not CSV_PATH.exists():
        sys.exit(f"Missing {CSV_PATH}")
    OUT_DIR.mkdir(exist_ok=True)
    if DRY_RUN:
        print("DRY RUN: no Pinterest token set or DRY_RUN=true, nothing will be posted")

    posted = load_posted()
    done = failed = 0
    for row in load_rows():
        if done >= PINS_PER_RUN:
            break
        asin = (row.get("asin") or "").strip().upper()
        if not ASIN_RE.match(asin):
            print(f"skip: invalid or placeholder ASIN {asin!r}")
            continue
        if asin in posted:
            continue
        if not (row.get("headline") or "").strip():
            print(f"skip {asin}: empty headline")
            continue
        link = f"https://www.amazon.com/dp/{asin}?tag={TAG}"
        try:
            img, credit = build_image(row, asin, len(posted) + done)
            img.save(OUT_DIR / f"{asin}.jpg", quality=90)
            pin_id = post_pin(row, img, link, credit)
        except Exception as e:  # keep going, report at the end
            print(f"FAILED {asin}: {e}")
            failed += 1
            continue
        done += 1
        if pin_id:
            posted.add(asin)
            print(f"posted {asin} -> pin {pin_id}")
    if not DRY_RUN:
        save_posted(posted)
    print(f"done: {done} ok, {failed} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
