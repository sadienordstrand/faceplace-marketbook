#!/usr/bin/env python3
"""
build_gallery.py
-----------------
Turn a results CSV into a single self-contained HTML gallery.

    python3 src/build_gallery.py runs/<folder>/results.csv

Writes gallery.html next to the CSV. Locally downloaded thumbnails are baked
into the file as data URIs by default, so the gallery is one portable file
with no dependency on the thumbnails/ folder, relative paths, or Facebook URLs.
Pass --no-embed to link the thumbnails instead and keep the file small; that's
how a run's lightweight_gallery.html is made.

The page itself is ui/gallery.html, with the listings and the shared palette
substituted in here. Everything ends up inline in the output because a gallery
gets emailed as an attachment and opened from wherever it lands, so it can't
depend on a file sitting next to it.
"""
import argparse
import base64
import csv
import json
import mimetypes
from pathlib import Path

import storage

EMBED_BUDGET_MB = 60
UI_DIR = Path(__file__).resolve().parent / "ui"


def _template():
    html = (UI_DIR / "gallery.html").read_text(encoding="utf-8")
    tokens = (UI_DIR / "tokens.css").read_text(encoding="utf-8")
    return html.replace("__TOKENS__", tokens)



def embed_images(rows, base_dir, budget_mb=EMBED_BUDGET_MB):
    """Rewrite local thumbnail paths to data URIs so the HTML stands alone.
    Stops embedding once the budget is used up and leaves the rest as paths, so
    a huge multi-city run can't produce an unopenable file."""
    budget = budget_mb * 1024 * 1024
    used, done, skipped = 0, 0, 0
    for r in rows:
        img = r.get("image") or ""
        if not img or img.startswith(("http", "data:")):
            continue
        p = Path(img)
        if not p.is_absolute():
            p = base_dir / p
        if not p.exists():
            continue
        size = p.stat().st_size
        if used + size > budget:
            skipped += 1
            continue
        mime = mimetypes.guess_type(p.name)[0] or "image/jpeg"
        r["image"] = (f"data:{mime};base64,"
                      + base64.b64encode(p.read_bytes()).decode("ascii"))
        used += size
        done += 1
    return done, skipped, used


def build(csv_in, out=None, embed=True, budget_mb=EMBED_BUDGET_MB, only_ids=None,
          quiet=False, images=True, dates=True):
    """only_ids limits the gallery to those item_ids, which is how the emailed
    "just the new listings" attachment is built from the same CSV.

    images=False leaves the photos out altogether, which is what the emailed
    attachments use. Dropping the image paths rather than only the embedding
    matters: a path to a thumbnails/ folder the recipient hasn't got shows up as
    "image expired", which reads as something having gone wrong.

    dates=False drops the first-found column, for a gallery whose listings were
    all found by the same run — there, the line says the same thing on every
    card and so says nothing on any of them."""
    src = Path(csv_in)
    with open(src, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    seen, uniq = set(), []
    keep = set(only_ids) if only_ids is not None else None
    for r in rows:
        iid = r.get("item_id")
        if keep is not None and iid not in keep:
            continue
        if iid and iid not in seen:
            seen.add(iid)
            uniq.append(r)
    if not dates:
        for r in uniq:
            r.pop(storage.FIRST_FOUND, None)
    note = ""
    if not images:
        for r in uniq:
            r["image"] = ""
        note = ", no photos"
    elif embed:
        done, skipped, used = embed_images(uniq, src.resolve().parent, budget_mb)
        note = f", {done} images baked in ({used / 1e6:.1f} MB)"
        if skipped:
            note += f"; {skipped} left as file links (over {budget_mb} MB budget)"
    # "</" must not appear inside a <script> block; escape it in the JSON.
    data = json.dumps(uniq, ensure_ascii=False).replace("</", "<\\/")
    out = Path(out) if out else src.with_name("gallery.html")
    out.write_text(_template().replace("__DATA__", data), encoding="utf-8")
    if not quiet:
        print(f"Wrote {out} ({len(uniq)} listings{note}).")
        if not embed:
            print("  Keep the thumbnails/ folder next to the HTML, and open the "
                  "file directly in a browser — preview panes often can't "
                  "resolve relative image paths.")
    return out


def main():
    ap = argparse.ArgumentParser(description="Build a browsable HTML gallery from a results CSV.")
    ap.add_argument("csv_in", metavar="CSV")
    ap.add_argument("--out", metavar="HTML", help="output path (default: gallery.html next to the CSV)")
    ap.add_argument("--no-embed", action="store_true",
                    help="link thumbnails by path instead of baking them into the HTML")
    a = ap.parse_args()
    build(a.csv_in, a.out, embed=not a.no_embed)


if __name__ == "__main__":
    main()
