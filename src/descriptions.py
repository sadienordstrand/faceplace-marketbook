"""
descriptions.py

The second pass: visit each kept listing's detail page once for its description
and full-size photo, and save that photo to disk while its URL is still fresh.

This is the expensive stage — roughly seven seconds a listing — and the one
people interrupt, so each listing is handed back the moment it is done and
nothing gathered depends on reaching the end.
"""
import json
import time

import paths
from browser import goto_with_retry, human_pause
from listings import SCRIPT_JSON_JS, find_key, iter_json_docs

DEBUG_DIR = paths.DEBUG_DIR

# Randomized seconds to wait between detail-page hits. The one knob that trades
# runtime against how machine-like the traffic looks. Tuned so the totals land
# on ~7s and ~9s per listing once the fixed page cost below is added.
PACES = {"fast": (1.0, 2.5), "slow": (3.0, 5.0)}
DEFAULT_PACE = "fast"

# Fixed per-listing costs on top of the pause, from measured runs: ~3.5s to load
# a detail page and read its payload, plus ~1.5s to fetch and store the photo
# when thumbnails are on. No pace setting can go below these.
PAGE_WORK_SECONDS = 3.5
PHOTO_SAVE_SECONDS = 1.5


def description_seconds_each(pace, with_thumbs=True):
    lo, hi = PACES[pace]
    return (PAGE_WORK_SECONDS + (PHOTO_SAVE_SECONDS if with_thumbs else 0)
            + (lo + hi) / 2)


def detail_from_json(page):
    """Pull description/photo from the JSON blobs on a listing detail page.
    Far more reliable than og: meta tags, which Facebook often omits for
    logged-in sessions."""
    desc, img = "", ""
    for t in page.eval_on_selector_all('script[type="application/json"]', SCRIPT_JSON_JS):
        if "redacted_description" not in t and "listing_photos" not in t:
            continue
        for doc in iter_json_docs(t):
            if not desc:
                for rd in find_key(doc, "redacted_description"):
                    if isinstance(rd, dict) and rd.get("text"):
                        desc = rd["text"]
                        break
            if not img:
                for photos in find_key(doc, "listing_photos"):
                    if isinstance(photos, list):
                        for ph in photos:
                            uri = ((ph or {}).get("image") or {}).get("uri")
                            if uri:
                                img = uri
                                break
                    if img:
                        break
        if desc and img:
            break
    return desc, img


def wait_for_detail(page, timeout_s=8.0):
    """Poll for the listing payload instead of sleeping a fixed interval — the
    JSON is in the initial HTML, so it's usually ready in well under a second.
    Photos present with no description text means the listing simply has none."""
    start = time.time()
    desc = img = ""
    while time.time() - start < timeout_s:
        desc, img = detail_from_json(page)
        if desc:
            break
        if img and time.time() - start > 2.0:
            break
        time.sleep(0.4)
    return desc, img


def retrieve_descriptions(ctx, page, targets, thumbs_path=None, debug_dump=False,
                pace=DEFAULT_PACE, on_row=None):
    """Visit each target's detail page once: description, full-size photo, and
    (when thumbs_path is given) the photo saved to disk immediately, while its
    URL is only seconds old.

    `on_row` is called with each listing as soon as it is done, so the caller
    can persist it. This stage is the long pole of a run and gets interrupted
    often; nothing gathered should depend on reaching the end.

    Returns False if Ctrl-C ended it early, True otherwise."""
    lo, hi = PACES[pace]
    per = description_seconds_each(pace, thumbs_path is not None)
    print(f"\nRetrieving descriptions for {len(targets)} listings — one detail "
          f"page each at '{pace}' pacing ({lo:g}-{hi:g}s between hits), roughly "
          f"{max(1, round(len(targets) * per / 60))} min. "
          "(--limit / --match narrow it; --pace changes the throttle.)")
    if debug_dump:
        DEBUG_DIR.mkdir(exist_ok=True)
    # Do this stage on a page of our own. We arrive here sitting on the last
    # city's search feed: a tab that has been scrolled dozens of times, still
    # holds thousands of cards, and — being facebook.com — is attached to
    # Facebook's service worker. Turning on interception there makes every
    # in-flight request round-trip to Python, and page.route() has no timeout,
    # so a busy enough renderer wedges the run outright with no way back.
    # Navigating that tab to about:blank isn't enough, because the renderer and
    # its service worker survive the navigation; a new tab has neither. Opened
    # before the old one closes, since a persistent context with no pages left
    # shuts the browser down.
    feed_page, page = page, ctx.new_page()
    try:
        feed_page.close()
    except Exception:
        pass

    # Only the JSON payload matters here, so drop the photo/video/font requests
    # each detail page would otherwise pull. Routed on the page, not the
    # context, so the thumbnail fetches below still go through.
    def block_heavy_requests(route):
        # Every request on the page round-trips through here, and a request
        # still in flight when the page navigates away can no longer be
        # answered. Letting that raise would surface as a stalled page rather
        # than a dropped request, so failures here are swallowed by design.
        try:
            if route.request.resource_type in ("image", "media", "font"):
                route.abort()
            else:
                route.continue_()
        except Exception:
            pass

    got, saved, spent = 0, 0, []
    interrupted, routed = False, False
    try:
        # Skipping those requests only buys speed, so a failure to arrange it
        # isn't worth losing the stage over. Registered inside the try so a
        # Ctrl-C while it's being set up ends the run the same way one during
        # the loop does — with the CSV and gallery still written.
        try:
            page.route("**/*", block_heavy_requests)
            routed = True
        except Exception as e:
            print(f"  (couldn't skip images on detail pages: {e})")
        for i, r in enumerate(targets, 1):
            url = r.get("url") or f"https://www.facebook.com/marketplace/item/{r.get('item_id', '')}"
            print(f"  [{i}/{len(targets)}] {url}")
            t0 = time.time()
            if not goto_with_retry(page, url):
                human_pause(lo, hi)
                continue
            try:
                desc, img = wait_for_detail(page)

                def meta(prop):
                    el = page.query_selector(f'meta[property="{prop}"]')
                    return (el.get_attribute("content") or "") if el else ""
                img = img or meta("og:image")
                desc = desc or meta("og:description")
                if debug_dump and not desc:
                    dump = DEBUG_DIR / f"detail_{r.get('item_id', i)}"
                    dump.mkdir(parents=True, exist_ok=True)
                    scripts = page.eval_on_selector_all(
                        'script[type="application/json"]', SCRIPT_JSON_JS)
                    (dump / "scripts.json").write_text(json.dumps(scripts), encoding="utf-8")
                if img:
                    r["image"] = img
                if desc:
                    r["description"] = desc[:2000]
                    got += 1
                stored = False
                if thumbs_path and (r.get("image") or "").startswith("http"):
                    rel = save_image(ctx, r["image"], r["item_id"], thumbs_path)
                    if rel:
                        r["image"] = rel
                        saved += 1
                        stored = True
                note = f"{len(desc)} chars" if desc else "no description found"
                if stored:
                    note += ", photo saved"
                print(f"     {note} ({time.time() - t0:.1f}s)")
            except Exception as e:
                print(f"     failed: {e}")
            if on_row:
                on_row(r)
            spent.append(time.time() - t0)
            human_pause(lo, hi)
    except KeyboardInterrupt:
        interrupted = True
        print(f"\n  Interrupted after {len(spent)} of {len(targets)} listings. "
              "Everything gathered so far is saved; finishing up the outputs.")
    finally:
        # Not after a Ctrl-C. Raised out of a Playwright call, it leaves the
        # sync API wedged, and the next call into it — this one — never returns
        # at all: no exception to catch, just a run that stops here forever with
        # its CSV and gallery unwritten. Nothing needs unrouting anyway once the
        # stage is over, and on Windows the browser may already be gone besides,
        # since a console sends Ctrl-C to every process sharing it, Playwright's
        # driver included.
        if routed and not interrupted:
            try:
                page.unroute("**/*")
            except Exception:
                pass
    avg = sum(spent) / len(spent) if spent else 0
    print(f"  {got}/{len(spent) if interrupted else len(targets)} descriptions"
          + (f", {saved} photos saved" if thumbs_path else "")
          + f" ({avg:.1f}s avg per page, plus the {lo:g}-{hi:g}s pause between hits).")
    return not interrupted


# ---------- thumbnails ----------
def save_image(ctx, url, item_id, outdir):
    """Download one image with the logged-in session's cookies. Returns the
    path to store in the CSV, or "" on failure."""
    try:
        resp = ctx.request.get(url, timeout=20000)
        if not resp.ok:
            return ""
        ct = (resp.headers.get("content-type", "") or "").lower()
        if "image" not in ct:  # an error page saved as .jpg is worse than nothing
            return ""
        ext = ".png" if "png" in ct else ".webp" if "webp" in ct else ".jpg"
        outdir.mkdir(parents=True, exist_ok=True)
        fp = outdir / f"{item_id}{ext}"
        fp.write_bytes(resp.body())
        return f"{outdir.name}/{fp.name}"
    except Exception:
        return ""


def fetch_thumbs(ctx, rows, outdir):
    """Download any remaining remote images, reusing whatever is on disk."""
    outdir.mkdir(parents=True, exist_ok=True)
    existing = {p.stem: p.name for p in outdir.iterdir()
                if p.suffix in (".jpg", ".png", ".webp")}
    todo, reused, local = [], 0, 0
    for r in rows:
        img = r.get("image") or ""
        if img.startswith(f"{outdir.name}/"):
            local += 1
        elif r.get("item_id") in existing:
            r["image"] = f"{outdir.name}/{existing[r['item_id']]}"
            reused += 1
        elif img.startswith("http"):
            todo.append(r)
    print(f"\nThumbnails: {local} saved while retrieving descriptions, "
          f"{reused} already on disk, {len(todo)} to fetch.")
    ok = 0
    for i, r in enumerate(todo, 1):
        rel = save_image(ctx, r["image"], r["item_id"], outdir)
        if rel:
            r["image"] = rel
            ok += 1
        if i % 25 == 0:
            print(f"   {i}/{len(todo)}...")
        human_pause(0.3, 0.8)
    if todo:
        print(f"  downloaded {ok}/{len(todo)} "
              "(rows whose fetch failed keep their remote URL).")
