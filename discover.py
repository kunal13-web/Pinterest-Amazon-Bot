"""StyleNest trend discovery.

Pulls today's trending search keywords from the Pinterest Trends API (US by
default) and writes `candidates.csv` in the same column layout as products.csv,
so a row can be copied across once you have picked a product.

What it does NOT do: it does not find Amazon products or ASINs. Without
PA-API there is no compliant automatic source for ASINs, so each row carries an
`amazon_search_url` (with your affiliate tag) that you open to pick a product
and copy its ASIN into the `asin` column by hand.

Env vars:
  PINTEREST_ACCESS_TOKEN  Same token the bot uses (needs Trends access)
  PINTEREST_API_BASE      Default https://api.pinterest.com/v5 (sandbox also works)
  TREND_REGION            Default US
  TREND_TYPE              growing | monthly | yearly | seasonal (default growing)
  TREND_INTERESTS         Optional comma list to narrow categories; unset = all
  TREND_LIMIT             How many keywords to pull, max 50 (default 25)
  AMAZON_TAG              Default stylenestus-20
"""
import csv
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote_plus

import requests

API_BASE = os.getenv("PINTEREST_API_BASE", "https://api.pinterest.com/v5").rstrip("/")
TOKEN = os.getenv("PINTEREST_ACCESS_TOKEN", "")
REGION = os.getenv("TREND_REGION", "US")
TREND_TYPE = os.getenv("TREND_TYPE", "growing")
INTERESTS = os.getenv("TREND_INTERESTS", "").strip()
LIMIT = max(1, min(int(os.getenv("TREND_LIMIT", "25")), 50))
TAG = os.getenv("AMAZON_TAG", "stylenestus-20")
OUT = Path(os.getenv("CANDIDATES_CSV", Path(__file__).parent / "candidates.csv"))

COLUMNS = [
    "asin", "headline", "bullets", "category", "keywords", "board_id",
    "image_query", "layout", "badge", "photo_ids",
    "trend_keyword", "trend_growth", "amazon_search_url",
]

# Rough keyword -> palette category used by script.py. Anything else gets "".
CATEGORY_HINTS = {
    "home": ["decor", "organiz", "kitchen", "bedroom", "living room", "bathroom", "storage",
             "shelf", "furniture", "rug", "curtain", "closet", "apartment", "laundry"],
    "beauty": ["nail", "makeup", "skincare", "hair", "lip", "perfume", "beauty", "serum", "mascara"],
    "seasonal": ["christmas", "halloween", "thanksgiving", "holiday", "valentine", "easter",
                 "fall ", "autumn", "winter", "spring", "summer", "new year"],
    "garden": ["garden", "plant", "patio", "outdoor", "backyard", "flower"],
    "fashion": ["outfit", "dress", "sweater", "boots", "jeans", "jacket", "shoes", "style",
                "coat", "fashion", "bag", "skirt"],
}

# Neutral headline hooks: no prices, no availability, no unverifiable claims.
HOOKS = [
    "{kw} Ideas Worth Saving",
    "{kw} Inspiration for This Season",
    "How to Style {kw}",
    "{kw} to Try Right Now",
]


def guess_category(keyword):
    k = f" {keyword.lower()} "
    for cat, hints in CATEGORY_HINTS.items():
        if any(h in k for h in hints):
            return cat
    return ""


def title_case(text):
    return " ".join(w.capitalize() if not w.isupper() else w for w in text.split())


def headline_for(keyword):
    kw = title_case(keyword)
    if keyword.lower().endswith("ideas"):  # avoid "Ideas ... Ideas"
        hooks = ["{kw} Worth Saving", "{kw} to Try Right Now", "{kw} for This Season"]
    else:
        hooks = HOOKS
    return hooks[sum(map(ord, keyword)) % len(hooks)].format(kw=kw)[:100]


def keyword_tags(keyword):
    if keyword.lower().endswith("ideas"):
        return keyword
    return f"{keyword},{keyword} ideas"


def extract_trends(body):
    """Pull a list of {keyword, growth} out of the response, tolerant of shape changes."""
    items = None
    if isinstance(body, list):
        items = body
    elif isinstance(body, dict):
        for key in ("trends", "items", "keywords", "data"):
            if isinstance(body.get(key), list):
                items = body[key]
                break
    out = []
    for it in items or []:
        if isinstance(it, str):
            out.append({"keyword": it, "growth": ""})
        elif isinstance(it, dict) and it.get("keyword"):
            growth = it.get("pct_growth_wow", it.get("pct_growth_mom", it.get("pct_growth_yoy", "")))
            out.append({"keyword": str(it["keyword"]), "growth": growth})
    return out


def fetch_trends():
    params = {"limit": LIMIT}
    if INTERESTS:
        params["interests"] = INTERESTS
    url = f"{API_BASE}/trends/keywords/{REGION}/top/{TREND_TYPE}"
    r = requests.get(url, headers={"Authorization": f"Bearer {TOKEN}"}, params=params, timeout=30)
    if r.status_code in (401, 403):
        sys.exit(
            f"Pinterest Trends returned {r.status_code}: this token has no Trends access "
            f"(Pinterest limits Trends API to approved apps). Response: {r.text[:300]}"
        )
    if r.status_code != 200:
        sys.exit(f"Pinterest Trends error {r.status_code}: {r.text[:300]}")
    trends = extract_trends(r.json())
    if not trends:
        sys.exit(f"No keywords found in the response. Raw start: {r.text[:300]}")
    return trends


def build_rows(trends):
    rows = []
    for t in trends:
        kw = re.sub(r"\s+", " ", t["keyword"]).strip()
        if not kw:
            continue
        rows.append({
            "asin": "",
            "headline": headline_for(kw),
            "bullets": "",  # write 2-3 bullets that really match the product you pick
            "category": guess_category(kw),
            "keywords": keyword_tags(kw),
            "board_id": "",
            "image_query": kw,
            "layout": "",
            "badge": "",
            "photo_ids": "",
            "trend_keyword": kw,
            "trend_growth": t["growth"],
            "amazon_search_url": f"https://www.amazon.com/s?k={quote_plus(kw)}&tag={TAG}",
        })
    return rows


def main():
    if not TOKEN:
        sys.exit("PINTEREST_ACCESS_TOKEN is not set")
    rows = build_rows(fetch_trends())
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} candidates to {OUT}")
    for r in rows[:10]:
        print(f"- {r['trend_keyword']}  [{r['category'] or 'uncategorised'}]  growth={r['trend_growth']}")


if __name__ == "__main__":
    main()
