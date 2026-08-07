#!/usr/bin/env python3
"""
Diagnostic for the listing classifier. Not a test — a tool for looking at what
Facebook actually serves, so the rules in scheduling.py can be chosen from
evidence instead of guesses.

    .venv/bin/python tests/probe_diag.py URL [URL ...]

A listing page carries around twenty listings' worth of data: the one you asked
for plus the "related items" rail. So a plain substring search for a marker like
"is_sold":true tells you that SOMETHING on the page is sold, not that THIS
listing is — which is exactly the trap this tool exists to expose. It finds the
target listing's own record by id and prints its flags.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fb_marketplace_sweep as fb
import scheduling as sc
from playwright.sync_api import sync_playwright

FLAGS = ("is_sold", "is_pending", "is_live", "is_hidden", "is_viewer_seller")


def walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def json_blocks(body):
    """Every <script type="application/json"> payload on the page."""
    for m in re.finditer(
            r'<script[^>]+type="application/json"[^>]*>(.*?)</script>',
            body, re.S):
        try:
            yield json.loads(m.group(1))
        except ValueError:
            continue


def find_item_nodes(body, item_id):
    """Dicts that look like the target listing's own record."""
    out = []
    for block in json_blocks(body):
        for d in walk(block):
            if str(d.get("id")) != str(item_id):
                continue
            if any(f in d for f in FLAGS) or "marketplace_listing_title" in d:
                out.append(d)
    return out


def summarise(body, item_id):
    nodes = find_item_nodes(body, item_id)
    print(f"    nodes matching id {item_id}: {len(nodes)}")
    for d in nodes:
        keys = {f: d.get(f) for f in FLAGS if f in d}
        title = str(d.get("marketplace_listing_title"))[:48]
        print(f"      typename={d.get('__typename')} title={title!r} {keys}")
    # How badly a naive substring search would do on this same page.
    for needle in ('"is_sold":true', '"is_pending":true', '"is_sold":false'):
        print(f"    whole-page count of {needle}: {body.count(needle)}")


def main(urls):
    with sync_playwright() as p:
        ctx = fb.launch_context(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        fb.ensure_logged_in(page, timeout_s=180)
        for url in urls:
            item_id = (re.search(r"/item/(\d+)", url) or [None, "?"])[1]
            print(f"\n{'=' * 78}\n{url}")

            # Does the cheap request-only fetch work at all, with and without
            # browser-shaped headers?
            for label, headers in (("bare", None),
                                   ("with browser headers", sc.PROBE_HEADERS)):
                try:
                    resp = ctx.request.get(url, timeout=20000, max_redirects=5,
                                           headers=headers or {})
                    body = resp.text()
                    print(f"  tier1 {label}: HTTP {resp.status}, {len(body)} bytes"
                          f" -> {sc.classify_listing(resp.status, resp.url, body)}")
                    if resp.status == 200:
                        summarise(body, item_id)
                except Exception as e:
                    print(f"  tier1 {label} raised {type(e).__name__}: {e}")

            try:
                resp = page.goto(url, timeout=25000, wait_until="domcontentloaded")
                page.wait_for_timeout(1500)
                body = page.content()
                print(f"  tier2 rendered: HTTP {resp.status if resp else 0}, "
                      f"{len(body)} bytes -> "
                      f"{sc.classify_listing(resp.status if resp else 0, page.url, body)}")
                summarise(body, item_id)
                seen = page.inner_text("body")
                for word in ("Sold", "Pending", "no longer available",
                             "isn't available"):
                    if re.search(rf"\b{re.escape(word)}\b", seen, re.I):
                        print(f"    visible on page: {word!r}")
            except Exception as e:
                print(f"  tier2 raised {type(e).__name__}: {e}")
            fb.human_pause(1.5, 3.0)
        ctx.close()


if __name__ == "__main__":
    main(sys.argv[1:] or [
        "https://www.facebook.com/marketplace/item/1095490112439259"])
