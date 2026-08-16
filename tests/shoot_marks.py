#!/usr/bin/env python3
"""
Photographs the stars-and-hides states, for reading the copy and checking the
look of them.

    python3 tests/shoot_marks.py

Writes into .state/debug/marks-shots/, which git ignores. These aren't README
pictures; they're for looking at a change before it ships. Nothing real is
touched: a run is copied into a temporary folder and everything points there,
and the local server runs on a spare port so whatever is already listening on
the usual one is left alone.
"""
import shutil
import socket
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import build_gallery  # noqa: E402
import locations  # noqa: E402
import marks  # noqa: E402
import past_runs as pr  # noqa: E402
import scheduling as sc  # noqa: E402
import settings_ui  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

SHOTS = REPO / ".state" / "debug" / "marks-shots"
WIDTH, HEIGHT = 1280, 900


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def newest_run():
    """The most recent run with a CSV in it — the freshest photos, since
    Facebook's image links expire within hours."""
    folders = [f for f, _ in pr.run_folders() if (f / "results.csv").exists()]
    if not folders:
        raise SystemExit("nothing in runs/ to photograph — run a search first")
    return max(folders, key=lambda f: (f / "results.csv").stat().st_mtime)


def shoot(page, name, card=None):
    """The whole window, or a close-up of one card. The controls are 30px
    squares with a couple of lines of small type under them, which is not
    something you can judge in a picture of a whole page."""
    kw = {}
    if card is not None:
        box = page.locator(".card").nth(card).bounding_box()
        pad = 14
        kw["clip"] = {"x": box["x"] - pad, "y": box["y"] - pad,
                      "width": box["width"] + pad * 2,
                      "height": box["height"] + pad * 2}
    page.screenshot(path=str(SHOTS / name), **kw)
    print(f"  {name}")


def settle(page):
    page.wait_for_function("() => document.body.dataset.marks")
    page.wait_for_timeout(1200)  # photos


def gallery_shots(root, source):
    runs = root / "runs"
    folder = runs / source.name
    folder.mkdir(parents=True)
    shutil.copy(source / "results.csv", folder / "results.csv")

    port = free_port()
    marks.PORT = port
    build_gallery.RUNS_DIR = runs
    sc.RUNS_DIR = runs
    gallery = Path(build_gallery.build(folder / "results.csv", quiet=True))

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})

        # --- read-only: the app isn't running -------------------------------
        page.goto(gallery.as_uri())
        settle(page)
        page.locator(".card").first.locator(".star-btn").hover()
        page.wait_for_timeout(400)
        shoot(page, "1-readonly-hover-star.png")
        shoot(page, "1b-readonly-hover-star-close.png", card=0)

        page.locator(".card").nth(1).locator(".hide-btn").hover()
        page.wait_for_timeout(400)
        shoot(page, "2-readonly-hover-hide-close.png", card=1)

        # --- the app is running ---------------------------------------------
        httpd = ThreadingHTTPServer(("127.0.0.1", port), sc._GalleryHandler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        served = f"http://127.0.0.1:{port}{gallery.resolve().as_posix()}"

        page.goto(served)
        settle(page)
        page.locator(".card").first.locator(".star-btn").hover()
        page.wait_for_timeout(400)
        shoot(page, "3-live-hover-star.png")
        shoot(page, "3b-live-hover-star-close.png", card=0)

        page.locator(".card").nth(2).locator(".star-btn").click()
        page.locator(".card").nth(4).locator(".hide-btn").click()
        page.wait_for_timeout(900)
        page.mouse.move(WIDTH // 2, HEIGHT - 8)
        page.wait_for_timeout(400)
        shoot(page, "4-live-after-marking.png")

        # Reopened, to show the marks came back out of the file rather than
        # out of anything the browser was holding on to.
        page.goto(served)
        settle(page)
        shoot(page, "5-live-reopened.png")

        # The app closed with the gallery still open in front of you.
        httpd.shutdown()
        httpd.server_close()
        page.goto(gallery.as_uri())
        settle(page)
        page.locator(".card").first.locator(".star-btn").hover()
        page.wait_for_timeout(400)
        shoot(page, "6-app-closed.png")
        shoot(page, "6b-app-closed-close.png", card=0)

        # ...and the app opened again while it sat there, which the page is
        # supposed to notice on its own when you come back to the window.
        httpd = ThreadingHTTPServer(("127.0.0.1", port), sc._GalleryHandler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        page.evaluate("() => window.dispatchEvent(new Event('focus'))")
        page.wait_for_function("() => document.body.dataset.marks === 'live'")
        page.locator(".card").first.locator(".star-btn").hover()
        page.wait_for_timeout(400)
        shoot(page, "7-woke-up-after-app-opened.png", card=0)
        httpd.shutdown()
        httpd.server_close()

        # No sentence at all: the copy that was emailed to someone, where there
        # is no app to open and never will be. The barred cursor is the whole
        # of it, and a screenshot can't show a cursor — what this is here to
        # show is that nothing else appears on hover.
        sent = Path(build_gallery.build(folder / "results.csv", quiet=True,
                                        out=folder / "sent.html",
                                        editable=False))
        page.goto(sent.as_uri())
        settle(page)
        page.locator(".card").first.locator(".star-btn").hover()
        page.wait_for_timeout(400)
        shoot(page, "8-emailed-copy.png", card=0)

        browser.close()
    print(f"  marks.json now: {marks.read(folder)}")


def window_shot(root, source):
    """The Past searches tab, having opened a gallery that can't save."""
    runs = root / "runs2"
    folder = runs / source.name
    folder.mkdir(parents=True)
    for name in ("results.csv", "run.json", "gallery.html"):
        if (source / name).exists():
            shutil.copy(source / name, folder / name)
    pr.RUNS_DIR = runs
    sc.RUNS_DIR = runs
    # No server, no browser window: the point is the note left behind in the
    # tab, and both of those would be real if they weren't stubbed.
    sc.ensure_gallery_server = lambda *a, **kw: False
    pr.webbrowser.open = lambda url, **kw: True
    sc.schedule_installed = lambda: True
    sc.schedule_problems = lambda: []
    sc.scheduled_wakes = lambda: []
    sc.computer_settings = lambda: []

    def script(page):
        page.set_viewport_size({"width": 1000, "height": 900})
        page.click("#tabPast")
        page.wait_for_selector(".card.run")
        page.click(".card.run")
        page.wait_for_selector("#runMsg:not([hidden])")
        page.wait_for_timeout(500)
        shoot(page, "9-past-searches-readonly-note.png")
        page.evaluate("window.pyCancel()")

    cities = list(locations.load_locations())
    settings_ui.collect_settings(
        cities, {"steady": {"label": "Steady"}}, {"queries": [], "exclude": ""},
        headless=True,
        on_add=lambda label, text: (cities, "not here"),
        on_remove=lambda label: (cities, "not here"),
        hooks={**sc.ui_hooks(), **pr.ui_hooks(),
               "shortcut_offer": lambda: {"ask": False, "places": []},
               "update_offer": lambda: {"show": False}},
        on_ready=script)


def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    for old in SHOTS.glob("*.png"):
        old.unlink()
    source = newest_run()
    print(f"photographing {source.name}")
    with TemporaryDirectory() as tmp:
        gallery_shots(Path(tmp), source)
        window_shot(Path(tmp), source)
    print(f"in {SHOTS}")


if __name__ == "__main__":
    main()
