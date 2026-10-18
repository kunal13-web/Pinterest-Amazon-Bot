"""One-off check: does the Pinterest token work, which boards exist, and can it read Trends?

Prints board IDs/names and the raw Trends response. The token itself is never printed.
"""
import json
import os
import sys

import requests

BASE = os.getenv("PINTEREST_API_BASE", "https://api.pinterest.com/v5").rstrip("/")
TOKEN = os.getenv("PINTEREST_ACCESS_TOKEN", "")
if not TOKEN:
    sys.exit("PINTEREST_ACCESS_TOKEN secret is not set")
H = {"Authorization": f"Bearer {TOKEN}"}


def call(label, path, **params):
    print(f"\n=== {label}: GET {path} {params or ''}")
    try:
        r = requests.get(f"{BASE}{path}", headers=H, params=params, timeout=30)
    except Exception as e:
        print("request failed:", e)
        return None
    print("status:", r.status_code)
    try:
        body = r.json()
    except ValueError:
        print(r.text[:400])
        return None
    if r.status_code != 200:
        print(json.dumps(body, indent=2)[:600])
        return None
    return body


acct = call("Account", "/user_account")
if acct:
    print("username:", acct.get("username"), "| account_type:", acct.get("account_type"))

boards = call("Boards", "/boards", page_size=25)
if boards:
    for b in boards.get("items", []):
        print(f"board id={b.get('id')}  name={b.get('name')!r}  privacy={b.get('privacy')}")
    if not boards.get("items"):
        print("no boards found: create a board on Pinterest first")

trends = call("Trends (US, growing)", "/trends/keywords/US/top/growing", limit=5)
if trends:
    print(json.dumps(trends, indent=2)[:1500])
