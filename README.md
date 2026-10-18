# Pinterest-Amazon-Bot

Posts text-based product pins to the StyleNest Pinterest account with Amazon Associates links.

## How it works
1. Add real products to `products.csv` (replace every `PASTE_ASIN_HERE`). Edit headline and bullets so they match the product.
2. `script.py` builds a 1000x1500 pin image with Pillow, links to `https://www.amazon.com/dp/<ASIN>?tag=<tag>` and posts through the Pinterest API v5.
3. `posted.json` remembers posted ASINs so nothing is posted twice. The workflow commits it back after each run.

## Setup
Repo -> Settings -> Secrets and variables -> Actions:
- Secrets: `PINTEREST_ACCESS_TOKEN`, `PINTEREST_BOARD_ID`
- Variables (optional): `AMAZON_TAG` (default `stylenestus-20`), `PINS_PER_RUN` (default 2)

Run it first from Actions -> Pinterest posting bot -> Run workflow with **dry run** ticked, then download the `pin-images` artifact and check the images.

## Notes
- No Amazon product photos are used. Pins are designed graphics only.
- Do not state prices or availability in headlines or bullets.
- Pinterest trial API access may need `PINTEREST_API_BASE=https://api-sandbox.pinterest.com/v5` until Standard access is approved.
