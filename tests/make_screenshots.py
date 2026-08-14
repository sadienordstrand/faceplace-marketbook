#!/usr/bin/env python3
"""
Regenerates the screenshots the README shows.

    python3 tests/make_screenshots.py

The window it photographs is the real one, opened with the real hooks, so the
pictures can't drift away from the copy on the screen. Everything it needs is
staged in a temporary folder — invented saved searches, invented email settings
— so it never reads or writes your own, and the parts that would reach outside
the app are stubbed: no launchd or Task Scheduler, no password prompt, no
shortcut on your desktop.

Nothing in the pictures is mocked up except the saved searches: the gallery is a
real run's own gallery.html out of runs/, and the emailed report is built by the
code that builds the real ones, from listings really on this computer. So this
needs a search or two to have been run before it has anything to photograph.
"""
import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import descriptions  # noqa: E402
import locations  # noqa: E402
import past_runs  # noqa: E402
import scheduling as sc  # noqa: E402
import settings_ui  # noqa: E402
import storage  # noqa: E402
from fb_marketplace_sweep import THUMBS_DIRNAME  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

SHOTS = REPO / "docs" / "images"

# The settings window's own size, so the pictures are the window as it opens
# rather than a browser stretched to fit.
WIDTH, HEIGHT = 1000, 1180

# Believable searches for the Scheduled searches tab: one daily, one on a longer
# interval, one paused, so all three states are visible in the one picture.
DEMO_SEARCHES = [
    {"name": "Defender 110", "queries": ["land rover defender 110"],
     "cities": ["Medford, OR", "Sacramento, CA", "Boise, ID", "Phoenix, AZ",
                "Albuquerque, NM", "Dallas, TX"],
     "exclude": "hot wheels, model, diecast, poster",
     "email_to": "you@gmail.com",
     "interval": {"every": 1, "unit": "days"}},
    {"name": "Airstream project", "queries": ["airstream"],
     "cities": ["Medford, OR", "Boise, ID", "Tallahassee, FL"],
     "min_price": 2000, "max_price": 25000,
     "email_to": "you@gmail.com",
     "interval": {"every": 2, "unit": "days"}},
    {"name": "Shop tools", "queries": ["bridgeport mill"],
     "cities": ["Minneapolis, MN", "Des Moines, IA", "Pittsburgh, PA"],
     "email_to": "you@gmail.com",
     "interval": {"every": 12, "unit": "hours"}, "enabled": False},
]

# How many listings each demo search is carrying, for the "N listings tracked"
# line on its card. Normally read out of the database against a real search id.
TRACKING = (147, 38, 12)

# A shortcut panel that has something to offer but isn't asking, so the Email &
# Setup tab shows the Shortcut section while no sheet covers the search tab.
SHORTCUT = {"ask": False,
            "places": [{"id": "desktop", "label": "Desktop", "on": True},
                       {"id": "dock", "label": "Dock", "on": True}]}


def window(page, grow=False):
    """Sizes the window for the next picture.

    `grow` stretches it until the whole tab fits, which is how a tab taller than
    the window gets photographed in one piece. A full-page screenshot can't do
    it: the footer is pinned to the bottom of the window, so on a picture taller
    than one it lands across the middle of the page.
    """
    page.set_viewport_size({"width": WIDTH, "height": HEIGHT})
    page.wait_for_timeout(200)
    grown = 0
    while grow:
        needed = page.evaluate("document.documentElement.scrollHeight")
        # Stopping on "no taller than last time" as well as "it fits": a pane
        # that sizes itself off the window would otherwise grow with it forever.
        if needed <= page.viewport_size["height"] or needed <= grown:
            break
        grown = needed
        page.set_viewport_size({"width": WIDTH, "height": needed})
        page.wait_for_timeout(300)


def shoot(page, name, **kw):
    page.screenshot(path=str(SHOTS / name), **kw)
    print(f"  {name}")


# ------------------------------------------------------- the settings window
def stage(root):
    """Point everything that persists at a temporary folder, and cut the wires
    to the rest of the computer."""
    sc.SEARCHES_PATH = root / "saved_searches.json"
    sc.EMAIL_CONFIG_PATH = root / "email_config.json"
    sc.SCHEDULE_DIR = root / "schedule"
    sc.LOCK_PATH = sc.SCHEDULE_DIR / "run.lock"
    sc.TICK_LOG = sc.SCHEDULE_DIR / "tick.log"
    sc.SUPPORT_DIR = root / "support"
    sc.HEARTBEAT_PATH = sc.SUPPORT_DIR / "last-checkin.json"

    # Automatic runs on and healthy, without installing anything. install and
    # uninstall are stubbed too: the buttons aren't clicked here, but a stray
    # click must not reach launchd.
    sc.schedule_installed = lambda: True
    sc.schedule_problems = lambda: []
    sc.install_schedule = lambda **kw: (True, [])
    sc.uninstall_schedule = lambda: (True, [])
    # Reading the wake queue shells out to pmset and writing it raises a macOS
    # password prompt, so neither happens.
    sc.scheduled_wakes = lambda: []
    sc._admin_shell = lambda lines: True
    # Every setting card on show. The real answer is whatever this particular
    # computer happens to be set to, which is nobody else's documentation.
    sc.computer_settings = lambda: []
    # Tracked counts, which would otherwise be looked up against search ids
    # that have never run.
    counts = iter(TRACKING)
    sc.latest_run = lambda con, search_id: (1, None, None, next(counts, 0))

    for rec in DEMO_SEARCHES:
        _, err = sc.add_search(rec)
        if err:
            raise SystemExit(f"couldn't stage “{rec['name']}”: {err}")
    sc.save_email_config({"provider": "gmail", "address": "you@gmail.com",
                          "app_password": "abcdefghijklmnop"})
    # A scheduler that has checked in, so the tab says when it last looked
    # rather than that nothing ever has.
    sc.check_in("tick")
    # Runs already behind them, so the cards carry real dates rather than
    # "never".
    for rec, ago in zip(sc.load_searches(), (5, 20, 50)):
        sc.update_search(rec["id"], {
            "last_started": sc.iso(sc.now_local() - sc.timedelta(hours=ago)),
            "last_finished": sc.iso(sc.now_local() - sc.timedelta(hours=ago - 1)),
        })


def settings_shots():
    """The three tabs the README shows, photographed in one window."""
    tmp = TemporaryDirectory()
    stage(Path(tmp.name))
    cities = list(locations.load_locations())

    def script(page):
        window(page)
        page.fill("#query", "land rover defender 110")
        page.fill("#exclude", "hot wheels, model, diecast, poster")
        # Filling a box scrolls it into view, and the shot wants the top of the
        # form. Blurring as well, so no caret is left blinking in a picture.
        page.locator("#exclude").blur()
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(500)
        shoot(page, "settings-search.png")

        window(page)
        page.click("#tabSaved")
        page.wait_for_selector(".card")
        page.wait_for_timeout(400)
        shoot(page, "settings-saved.png")

        # Whole tab rather than a window's worth of it: this one picture is what
        # both halves of the setup in the README are read against, and the four
        # cards only come to a few hundred pixels more than the window holds.
        page.click("#tabEmail")
        page.wait_for_selector("#sysBlock:not([hidden])")
        page.wait_for_timeout(600)
        window(page, grow=True)
        shoot(page, "settings-schedule.png")

        page.evaluate("window.pyCancel()")

    settings_ui.collect_settings(
        cities, descriptions.PACES,
        {"queries": [], "exclude": "", "pace": descriptions.DEFAULT_PACE,
         "page_work": descriptions.PAGE_WORK_SECONDS,
         "photo_save": descriptions.PHOTO_SAVE_SECONDS},
        headless=True,
        builtins=list(locations.base_locations()),
        # The city list is read-only here: adding one writes to the real
        # locations file, which is not this script's business.
        on_add=lambda label, text: (cities, "Not while taking screenshots."),
        on_remove=lambda label: (cities, "Not while taking screenshots."),
        hooks={**sc.ui_hooks(), **past_runs.ui_hooks(),
               "shortcut_offer": lambda: SHORTCUT,
               "update_offer": lambda: {"show": False}},
        on_ready=script)
    tmp.cleanup()


# ---------------------------------------------------------------- the gallery
def candidates():
    """Every gallery on disk that could be photographed, best first.

    A run's own gallery.html is what gets photographed, since it's literally
    the artifact the tool produces. The ones that saved their thumbnails come
    first however few listings they hold: a run that left its photos on
    Facebook's CDN loses them within hours, and photographs as a grid of
    "image expired".
    """
    found = []
    for folder, _ in past_runs.run_folders():
        gallery = past_runs.gallery_in(folder)
        if gallery:
            thumbs = folder / THUMBS_DIRNAME
            local = len(list(thumbs.glob("*"))) if thumbs.is_dir() else 0
            found.append((local, past_runs.summarize(folder)["listings"] or 0,
                          gallery))
    found.sort(reverse=True)
    return [gallery for _, _, gallery in found]


def load_gallery(page, gallery):
    """Opens a gallery and waits for the picture to settle, then says how many
    of the photos in it are missing."""
    page.goto(gallery.resolve().as_uri())
    page.wait_for_selector(".card")
    # Only the photos near the top of the page: the rest of the grid is
    # loading="lazy" and never loads at all while the page sits still.
    in_shot = "e => e.getBoundingClientRect().top < innerHeight"
    page.wait_for_function(
        f"() => [...document.images].filter({in_shot})"
        f".every(i => i.complete)", timeout=60000)
    page.wait_for_timeout(800)
    return page.evaluate(f"() => [...document.querySelectorAll('.noimg')]"
                         f".filter({in_shot}).length")


def gallery_shot(page):
    page.set_viewport_size({"width": 1280, "height": 1000})
    best, fewest = None, None
    for gallery in candidates():
        missing = load_gallery(page, gallery)
        if fewest is None or missing < fewest:
            best, fewest = gallery, missing
        if not missing:
            break
    if not best:
        print("  nothing in runs/ to photograph yet — run a search first")
        return
    if fewest:
        print(f"  {fewest} photos are missing from the picture. Facebook's "
              f"photo links expire within hours, so run a fresh search with "
              f"thumbnails on and try again.")
        load_gallery(page, best)
    print(f"  from {best.parent.name}")
    # JPEG: a page of photographs as a PNG is several megabytes.
    shoot(page, "gallery.jpg", type="jpeg", quality=82)


# ----------------------------------------------------------- the email report
def tracked_listings(limit):
    """Listings the app is currently following, so the names and prices in the
    report are real ones. Photos don't come into it — a report is all text."""
    if not storage.DB_PATH.exists():
        return []
    con = sqlite3.connect(storage.DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(
            "SELECT l.item_id, l.title, l.price, l.listing_location, l.url "
            "FROM listings l JOIN listing_state s USING (item_id) "
            "WHERE l.title <> '' AND l.price <> '' ORDER BY l.rowid LIMIT ?",
            (limit,))]
    finally:
        con.close()


def report_shot(page, tmp):
    """A report with new listings and removals in it, built by the same code
    that builds the real ones, from listings that really exist."""
    rows = tracked_listings(6)
    if len(rows) < 6:
        print("  not enough listings in the database for a report")
        return
    search = {"name": "Defender 110", "queries": ["land rover defender 110"],
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
        "removed": [{**rows[2], "removal": sc.STATUS_SOLD},
                    {**rows[3], "removal": sc.STATUS_SOLD},
                    {**rows[4], "removal": sc.STATUS_GONE}],
        "descriptions_fetched": 2,
        "per_city": {"Medford, OR": {"kept": 61, "cards": 540},
                     "Sacramento, CA": {"kept": 52, "cards": 480},
                     "Boise, ID": {"kept": 34, "cards": 390}},
        # Not the real home folder: this picture gets committed.
        "run_dir": "/Users/you/FaceplaceMarketbook/runs/saved/defender_110",
        "gallery": "/Users/you/FaceplaceMarketbook/runs/saved/defender_110/gallery.html",
    }
    _, _, html = sc.build_report(
        search, summary, sc.now_local() + sc.timedelta(hours=23), ())
    page_path = Path(tmp) / "report.html"
    page_path.write_text(html, encoding="utf-8")
    page.set_viewport_size({"width": 760, "height": 900})
    page.goto(page_path.resolve().as_uri())
    page.wait_for_timeout(400)
    shoot(page, "email-report.png", full_page=True)


def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    print("settings window...")
    settings_shots()
    with TemporaryDirectory() as tmp, sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        print("gallery...")
        gallery_shot(page)
        print("email report...")
        report_shot(page, tmp)
        browser.close()
    print("in docs/images:")
    for f in sorted(SHOTS.iterdir()):
        print(f"  {f.name}  {f.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
