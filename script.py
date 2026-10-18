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


def _pick(items, seed):
    return items[int(hashlib.md5(seed.encode()).hexdigest(), 16) % len(items)]


def _download(url):
    return Image.open(io.BytesIO(requests.get(url, timeout=60).content)).convert("RGB")


def fetch_photo(query, seed):
    """Download a portrait stock photo. Returns (PIL image, credit) or (None, '').

    Uses Pixabay if PIXABAY_API_KEY is set, otherwise Pexels if PEXELS_API_KEY is set.
    """
    if not query or not (PIXABAY_KEY or PEXELS_KEY):
        return None, ""
    try:
        if PIXABAY_KEY:
            r = requests.get(
                "https://pixabay.com/api/",
                params={
                    "key": PIXABAY_KEY,
                    "q": query[:100],
                    "image_type": "photo",
                    "orientation": "vertical",
                    "min_height": 1200,
                    "safesearch": "true",
                    "per_page": 20,
                },
                timeout=30,
            )
            r.raise_for_status()
            hits = r.json().get("hits", [])
            if not hits:
                print(f"no Pixabay photos for {query!r}")
                return None, ""
            p = _pick(hits, seed)
            return _download(p["largeImageURL"]), f"Photo by {p.get('user', 'unknown')} on Pixabay"
        r = requests.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": PEXELS_KEY},
            params={"query": query, "orientation": "portrait", "size": "large", "per_page": 15},
            timeout=30,
        )
        r.raise_for_status()
        photos = r.json().get("photos", [])
        if not photos:
            print(f"no Pexels photos for {query!r}")
            return None, ""
        p = _pick(photos, seed)
        url = p["src"].get("large2x") or p["src"]["original"]
        return _download(url), f"Photo by {p.get('photographer', 'unknown')} on Pexels"
    except Exception as e:
        print(f"photo fetch failed for {query!r}: {e}")
        return None, ""


def cover(img, w, h):
    scale = max(w / img.width, h / img.height)
    img = img.resize((int(img.width * scale) + 1, int(img.height * scale) + 1), Image.LANCZOS)
    left, top = (img.width - w) // 2, (img.height - h) // 4  # bias crop toward the top
    return img.crop((left, top, left + w, top + h))


def build_image(row, asin):
    panel_bg, text_col, accent = PALETTES.get(row.get("category", "").strip().lower(), DEFAULT_PALETTE)
    query = (row.get("image_query") or "").strip() or (row.get("keywords", "").split(",")[0].strip())
    photo, credit = fetch_photo(query, asin)
    if photo is None:  # fallback: plain colour background so the bot never stops
        photo = Image.new("RGB", (W, H), accent)
    img = cover(photo, W, H)

    # bottom shade so the button and disclosure stay readable on any photo
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)
    for i in range(380):
        sd.line([(0, H - 380 + i), (W, H - 380 + i)], fill=(0, 0, 0, int(190 * i / 380)))
    img = Image.alpha_composite(img.convert("RGBA"), shade).convert("RGB")
    d = ImageDraw.Draw(img)

    # top headline panel
    pad = 70
    title_font = font(FONT_SERIF, 70)
    lines = wrap(d, row["headline"].strip(), title_font, W - 2 * pad)[:4]
    panel_h = 60 + 44 + len(lines) * 90 + 50
    d.rectangle([0, 0, W, panel_h], fill=panel_bg)
    d.text((pad, 52), "STYLENEST", font=font(FONT_BOLD, 28), fill=accent)
    d.line([(pad, 96), (pad + 90, 96)], fill=accent, width=4)
    y = 124
    for line in lines:
        d.text((pad, y), line, font=title_font, fill=text_col)
        y += 90

    # call to action
    btn_y = H - 250
    d.rounded_rectangle([pad, btn_y, W - pad, btn_y + 104], radius=52, fill=panel_bg)
    btn_font = font(FONT_BOLD, 42)
    label = "SHOP THE LOOK ON AMAZON"
    while d.textlength(label, font=btn_font) > W - 2 * pad - 40:
        btn_font = font(FONT_BOLD, btn_font.size - 2)
    tw = d.textlength(label, font=btn_font)
    d.text(((W - tw) / 2, btn_y + 52 - btn_font.size / 2 - 4), label, font=btn_font, fill=text_col)

    small = font(FONT_REG, 25)
    note = "#ad  As an Amazon Associate I earn from qualifying purchases"
    d.text(((W - d.textlength(note, font=small)) / 2, H - 112), note, font=small, fill=(235, 235, 235))
    return img, credit


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
            img, credit = build_image(row, asin)
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
