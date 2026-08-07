#!/usr/bin/env python3
"""
Which of the classifier's text markers actually appear on which kind of page?

    .venv/bin/python tests/marker_survey.py LIVE_URL GONE_URL

The text markers are only a fallback, used when the target listing's own record
can't be found. This measures whether that fallback can distinguish a removed
listing from a live one at all — Facebook ships its whole UI string bundle on
every page, so phrases like "isn't available right now" may well be present even
on a page for a listing that is perfectly fine.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fb_marketplace_sweep as fb
import scheduling as sc
from playwright.sync_api import sync_playwright


def survey(label, url, ctx):
    item_id = sc._id_from_url(url)
    resp = ctx.request.get(url, timeout=25000, max_redirects=5,
                           headers=sc.PROBE_HEADERS)
    body = resp.text()
    low = body.lower()
    rec = sc.listing_record(body, item_id)
    print(f"\n{label}\n  {url}")
    print(f"  HTTP {resp.status}, {len(body)} bytes")
    print(f"  record found: {bool(rec)}"
          + (f"  flags={ {k: rec.get(k) for k in sc.ITEM_FLAGS if k in rec} }"
             if rec else ""))
    print(f"  verdict: {sc.classify_listing(resp.status, resp.url, body, item_id)}")
    print("  GONE markers present:")
    for m in sc.GONE_MARKERS:
        print(f"    {low.count(m):5d}  {m}")
    print("  LIVE markers present:")
    for m in sc.LIVE_MARKERS:
        print(f"    {low.count(m):5d}  {m}")


def main(urls):
    labels = ["A LISTING THAT IS STILL UP", "A LISTING THAT DOES NOT EXIST"]
    with sync_playwright() as p:
        ctx = fb.launch_context(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        fb.ensure_logged_in(page, timeout_s=180)
        for i, url in enumerate(urls):
            survey(labels[i] if i < len(labels) else f"URL {i + 1}", url, ctx)
            fb.human_pause(1.5, 3.0)
        ctx.close()


if __name__ == "__main__":
    main(sys.argv[1:] or [
        "https://www.facebook.com/marketplace/item/1095490112439259",
        "https://www.facebook.com/marketplace/item/999999999999999"])
