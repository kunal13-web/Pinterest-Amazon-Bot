"""StyleNest Pinterest bot.

Reads products.csv, builds a text-based pin image with Pillow (no Amazon
product photos, which Associates terms do not allow without the API), and
posts it to Pinterest with your affiliate link.

Env vars:
  PINTEREST_ACCESS_TOKEN  Pinterest API v5 token (scopes: pins:write, boards:read)
  PINTEREST_BOARD_ID      Target board ID (a CSV row can override with board_id)
  AMAZON_TAG              Associates tag (default: stylenestus-20)
  PINS_PER_RUN            How many pins to post per run (default: 2)
  DRY_RUN                 "true" = build images, print payload, post nothing
  PINTEREST_API_BASE      Use https://api-sandbox.pinterest.com/v5 for trial/sandbox
"""
import base64
import csv
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
DEFAULT_BOARD = os.getenv("PINTEREST_BOARD_ID", "")
PINS_PER_RUN = int(os.getenv("PINS_PER_RUN", "2"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true" or not TOKEN

W, H = 1000, 1500
ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
DISCLOSURE = "As an Amazon Associate I earn from qualifying purchases. #ad"

# (top colour, bottom colour, accent) per category
PALETTES = {
    "home": ((34, 87, 62), (18, 48, 36), (240, 200, 120)),
    "beauty": ((196, 98, 120), (120, 48, 72), (255, 226, 214)),
    "seasonal": ((150, 28, 40), (70, 14, 24), (245, 215, 130)),
    "garden": ((70, 110, 50), (34, 62, 28), (250, 235, 160)),
}
DEFAULT_PALETTE = ((52, 64, 84), (26, 32, 44), (240, 200, 120))

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


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


def gradient(top, bottom):
    img = Image.new("RGB", (W, H), top)
    px = ImageDraw.Draw(img)
    for y in range(H):
        t = y / (H - 1)
        px.line([(0, y), (W, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return img


def build_image(row):
    top, bottom, accent = PALETTES.get(row.get("category", "").strip().lower(), DEFAULT_PALETTE)
    img = gradient(top, bottom)
    d = ImageDraw.Draw(img)
    pad = 80

    d.text((pad, 90), "STYLENEST", font=font(FONT_BOLD, 34), fill=accent)
    d.line([(pad, 150), (pad + 120, 150)], fill=accent, width=4)

    y = 220
    title_font = font(FONT_BOLD, 84)
    for line in wrap(d, row["headline"].strip(), title_font, W - 2 * pad)[:5]:
        d.text((pad, y), line, font=title_font, fill="white")
        y += 104

    y += 40
    bullets = [b.strip() for b in row.get("bullets", "").split("|") if b.strip()][:4]
    body_font = font(FONT_REG, 44)
    for b in bullets:
        d.ellipse([pad, y + 12, pad + 24, y + 36], fill=accent)
        lines = wrap(d, b, body_font, W - 2 * pad - 60)[:2]
        for line in lines:
            d.text((pad + 52, y), line, font=body_font, fill="white")
            y += 58
        y += 26

    btn_y = H - 270
    d.rounded_rectangle([pad, btn_y, W - pad, btn_y + 110], radius=55, fill=accent)
    btn_font = font(FONT_BOLD, 44)
    label = "SHOP ON AMAZON"
    tw = d.textlength(label, font=btn_font)
    d.text(((W - tw) / 2, btn_y + 30), label, font=btn_font, fill=bottom)

    small = font(FONT_REG, 26)
    note = "#ad  As an Amazon Associate I earn from qualifying purchases"
    nw = d.textlength(note, font=small)
    d.text(((W - nw) / 2, H - 120), note, font=small, fill=(230, 230, 230))
    return img


def description(row):
    bullets = [b.strip() for b in row.get("bullets", "").split("|") if b.strip()]
    tags = " ".join("#" + re.sub(r"\W+", "", k.strip().title()) for k in row.get("keywords", "").split(",") if k.strip())
    parts = [row["headline"].strip(), ""] + [f"- {b}" for b in bullets] + ["", DISCLOSURE, tags]
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


def post_pin(row, img, link):
    board = (row.get("board_id") or "").strip() or DEFAULT_BOARD
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    payload = {
        "board_id": board,
        "title": row["headline"].strip()[:100],
        "description": description(row),
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
            img = build_image(row)
            img.save(OUT_DIR / f"{asin}.jpg", quality=90)
            pin_id = post_pin(row, img, link)
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
