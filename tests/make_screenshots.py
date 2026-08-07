#!/usr/bin/env python3
"""
Regenerates the screenshots in docs/images/ that the README shows.

    python3 tests/make_screenshots.py

Everything is staged in a temporary folder, so this never reads or writes your
real saved searches or email settings. The gallery shot is taken from whichever
run folder has the most photos in it, because a gallery of real listings is the
only honest way to show what the tool produces.
"""
import json
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fb_marketplace_sweep as fb  # noqa: E402
import scheduling as sc  # noqa: E402
import settings_ui  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SHOTS = REPO / "docs" / "images"
WIDTH, HEIGHT = 1000, 1150

DEMO_SEARCHES = [
    {"name": "Defender 110", "query": "land rover defender 110",
     "cities": ["Medford, OR", "Sacramento, CA", "Boise, ID", "Phoenix, AZ",
                "Albuquerque, NM", "Dallas, TX"],
     "exclude": "hot wheels, model, diecast, poster",
     "interval": {"every": 1, "unit": "days"}},
    {"name": "Airstream project", "query": "airstream",
     "cities": ["Medford, OR", "Boise, ID", "Tallahassee, FL"],
     "min_price": 2000, "max_price": 25000,
     "interval": {"every": 2, "unit": "days"}},
    {"name": "Shop tools", "query": "bridgeport mill",
     "cities": ["Minneapolis, MN", "Des Moines, IA", "Pittsburgh, PA"],
     "interval": {"every": 12, "unit": "hours"}, "enabled": False},
]


def stage(root):
    """A believable set of saved searches, without touching the real ones."""
    sc.SEARCHES_PATH = root / "saved_searches.json"
    sc.EMAIL_CONFIG_PATH = root / "email_config.json"
    sc.STATE_DIR = root / ".schedule"
    sc.LOCK_PATH = sc.STATE_DIR / "run.lock"
    sc.TICK_LOG = sc.STATE_DIR / "tick.log"
    sc.SUPPORT_DIR = root / "support"
    sc.HEARTBEAT_PATH = sc.SUPPORT_DIR / "last-checkin.json"
    for rec in DEMO_SEARCHES:
        sc.add_search(rec)
    # A schedule that looks installed and healthy, without installing one.
    sc.schedule_installed = lambda: True
    sc.schedule_points_here = lambda: True
    sc.check_in("tick")
    sc.save_email_config({"provider": "gmail", "address": "you@gmail.com",
                          "app_password": "abcdefghijklmnop",
                          "default_to": "you@gmail.com"})
    # Two runs already behind them, so the cards show real dates rather than
    # "never".
    for rec, ago in zip(sc.load_searches(), (5, 20, 50)):
        sc.update_search(rec["id"], {
            "last_started": sc.iso(sc.now_local() - sc.timedelta(hours=ago)),
            "last_finished": sc.iso(sc.now_local() - sc.timedelta(hours=ago - 1)),
        })


def settings_shots():
    tmp = TemporaryDirectory()
    stage(Path(tmp.name))

    def script(page):
        page.set_viewport_size({"width": WIDTH, "height": HEIGHT})
        page.fill("#query", "land rover defender")
        page.fill("#exclude", "hot wheels, model, diecast, poster")
        page.wait_for_timeout(400)
        page.screenshot(path=str(SHOTS / "settings-search.png"))
        page.click("#tabSaved")
        page.wait_for_timeout(400)
        page.screenshot(path=str(SHOTS / "settings-saved.png"))
        page.click("#tabEmail")
        page.wait_for_timeout(600)
        page.screenshot(path=str(SHOTS / "settings-schedule.png"))
        page.evaluate("window.pyCancel()")

    settings_ui.collect_settings(
        list(fb.BUILTIN_LOCATIONS), fb.PACES,
        {"query": "", "exclude": "", "pace": "fast",
         "page_work": fb.PAGE_WORK_SECONDS, "photo_save": fb.PHOTO_SAVE_SECONDS,
         "descriptions_budget": 0},
        headless=True, hooks=sc.ui_hooks(),
        builtins=list(fb.BUILTIN_LOCATIONS),
        on_add=lambda label, text: (list(fb.BUILTIN_LOCATIONS), None),
        on_remove=lambda label: (list(fb.BUILTIN_LOCATIONS), None),
        on_ready=script)
    tmp.cleanup()


def best_gallery():
    """The run folder with the most saved photos, which makes the best picture."""
    best, best_count = None, -1
    for gallery in (REPO / "runs").rglob("gallery.html"):
        photos = gallery.parent / fb.THUMBS_DIRNAME
        count = len(list(photos.glob("*"))) if photos.exists() else 0
        if count > best_count:
            best, best_count = gallery, count
    return best


def gallery_shot(page):
    gallery = best_gallery()
    if not gallery:
        print("  no gallery to photograph yet — run a search first")
        return
    print(f"  gallery: {gallery}")
    page.set_viewport_size({"width": 1280, "height": 1000})
    page.goto(gallery.resolve().as_uri())
    page.wait_for_timeout(2500)
    # JPEG, because a page of photographs in PNG is several megabytes.
    page.screenshot(path=str(SHOTS / "gallery.jpg"), type="jpeg", quality=82)
    card = page.query_selector(".card") or page.query_selector("article")
    if card:
        card.scroll_into_view_if_needed()
        page.wait_for_timeout(600)
        card.screenshot(path=str(SHOTS / "gallery-card.jpg"), type="jpeg", quality=88)


def report_shot(page):
    """A report with new listings and removals, built from whatever is in the
    database so the text is real."""
    import sqlite3
    cols = ["item_id", "title", "price", "listing_location", "url"]
    rows = []
    if fb.DB_PATH.exists():
        con = sqlite3.connect(fb.DB_PATH)
        try:
            rows = [dict(zip(cols, r)) for r in con.execute(
                f"select {','.join(cols)} from listings "
                f"where title <> '' and price <> '' limit 6")]
        finally:
            con.close()
    if len(rows) < 6:
        print("  not enough listings in the database for a report shot")
        return
    search = {"name": "Defender 110", "query": "land rover defender 110",
              "cities": ["Medford, OR", "Sacramento, CA", "Boise, ID"],
              "interval": {"every": 1, "unit": "days"}}
    summary = {
        "status": "ok",
        "started": sc.iso(sc.now_local() - sc.timedelta(minutes=26)),
        "finished": sc.iso(sc.now_local()),
        "duration_seconds": 1560,
        "new_ids": [r["item_id"] for r in rows[:2]],
        "total_ids": [str(i) for i in range(147)],
        "new_rows": rows[:2],
        "removed": [{**rows[2], "removal": "sold"}, {**rows[3], "removal": "sold"},
                    {**rows[4], "removal": "gone"}],
        "descriptions_fetched": 2,
        "radius_km": 805,
        "per_city": {"Medford, OR": {"kept": 61, "cards": 540},
                     "Sacramento, CA": {"kept": 52, "cards": 480},
                     "Boise, ID": {"kept": 34, "cards": 390}},
        # Not the real home folder: this picture gets committed.
        "run_dir": "/Users/you/FaceplaceMarketbook/runs/saved/defender_110",
        "gallery": "/Users/you/FaceplaceMarketbook/runs/saved/defender_110/gallery.html",
    }
    _, _, html = sc.build_report(
        search, summary, sc.now_local() + sc.timedelta(hours=23), ())
    tmp = SHOTS / "_report.html"
    tmp.write_text(html, encoding="utf-8")
    page.set_viewport_size({"width": 760, "height": 900})
    page.goto(tmp.resolve().as_uri())
    page.wait_for_timeout(400)
    page.screenshot(path=str(SHOTS / "email-report.png"), full_page=True)
    tmp.unlink()


def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    print("settings window...")
    settings_shots()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        print("gallery...")
        gallery_shot(page)
        print("email report...")
        report_shot(page)
        browser.close()
    for f in sorted(SHOTS.iterdir()):
        print(f"  {f.name}  {f.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
