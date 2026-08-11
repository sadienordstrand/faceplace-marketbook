#!/usr/bin/env python3
"""
Offline tests for the sweep, and for what Ctrl-C does to one.

    python3 -m unittest tests.test_sweep

A run takes hours, so stopping one has to be a way of finishing rather than a
way of failing: whatever has been found is kept, and the outputs are written
from it. That promise is the whole subject here, and it only means anything if
it holds at every stage, so each stage gets its own interrupt.

No browser is opened. Playwright, the login, the page and the two stages that
download things are all stubs, which is also what lets a test say exactly where
the Ctrl-C lands.
"""
import csv
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import fb_marketplace_sweep as fb
import locations
import storage

CITIES = {"Medford, OR": "medford", "Sacramento, CA": "sacramento",
          "Denver, CO": "denver"}


def card(iid, title="1995 Land Rover Defender 110"):
    return {"href": f"https://www.facebook.com/marketplace/item/{iid}/",
            "text": f"$5,000\n{title}\nMedford, OR",
            "img": "", "outside": False, "dividerFound": False,
            "dividerText": ""}


class Patched(unittest.TestCase):
    said = ""

    def patch(self, obj, name, value):
        was = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(lambda: setattr(obj, name, was))

    def quietly(self, fn, *a, **kw):
        """The terminal is how a run reports itself, so it says a great deal.
        Kept out of the test output and put on self.said, where a test that
        cares what the user was told can read it."""
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                return fn(*a, **kw)
        finally:
            self.said = buf.getvalue()


# ------------------------------------------------------------ scrolling a city
class FakeMouse:
    def __init__(self, page):
        self.page = page

    def wheel(self, x, y):
        self.page.turn()


class ScrollPage:
    """Enough of a page for the scroll loop: every turn of the wheel brings a
    fresh batch of cards, and the test says which turn Ctrl-C lands on."""

    def __init__(self, per_scroll=2, stop_on=None):
        self.per_scroll, self.stop_on, self.scrolls = per_scroll, stop_on, 0
        self.mouse = FakeMouse(self)

    def turn(self):
        self.scrolls += 1
        if self.scrolls == self.stop_on:
            raise KeyboardInterrupt

    def evaluate(self, js):
        return [card(str(i))
                for i in range((self.scrolls + 1) * self.per_scroll)]


class TestScrolling(Patched):
    def setUp(self):
        self.patch(fb, "human_pause", lambda *a, **k: None)

    def test_it_scrolls_to_the_ceiling_when_cards_keep_coming(self):
        cards, _, _, stats = self.quietly(fb.collect_city, ScrollPage(), 4, None)
        self.assertEqual(len(cards), 10)
        self.assertEqual(stats["stop_reason"], "scroll ceiling")

    def test_ctrl_c_keeps_the_cards_already_read_off_the_page(self):
        # The cards are read incrementally because Facebook recycles the ones
        # scrolled past, so at any moment there is a real, complete set of them
        # in hand. Throwing that away for the sake of the scrolls that didn't
        # happen is the one thing this must not do.
        cards, _, _, stats = self.quietly(
            fb.collect_city, ScrollPage(stop_on=3), 20, None)
        self.assertEqual(len(cards), 6)
        self.assertEqual(stats["stop_reason"], fb.STOPPED_BY_HAND)
        self.assertEqual(stats["scrolls_used"], 3)
        self.assertIn("Keeping the 6 cards", self.said)


# ------------------------------------------------------- the whole run, stopped
class Ctx:
    def __init__(self, page):
        self.pages, self.closed = [page], False

    def close(self):
        self.closed = True


class Page:
    def __init__(self):
        self.listeners = []

    def on(self, event, fn):
        self.listeners.append((event, fn))

    def remove_listener(self, event, fn):
        self.listeners.remove((event, fn))

    def wait_for_selector(self, selector, timeout=None):
        return None

    def eval_on_selector_all(self, selector, js):
        return []


class TestStoppingARun(Patched):
    """`run()` with the browser replaced. Each test stops the run somewhere
    different and then asks the same question: is the work still there?"""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.patch(storage, "DB_PATH", self.root / "listings.sqlite3")
        self.patch(storage, "RUNS_DIR", self.root / "runs")
        self.patch(locations, "load_locations", lambda: dict(CITIES))

        self.page = Page()
        self.ctx = Ctx(self.page)
        self.patch(fb, "sync_playwright", lambda: _Nothing())
        self.patch(fb, "keep_awake", lambda: _Nothing())
        self.patch(fb, "launch_context", lambda p: self.ctx)
        self.patch(fb, "ensure_logged_in", lambda page, **kw: None)
        self.patch(fb, "preflight_pause", lambda page, url, skip=False: 805)
        self.patch(fb, "goto_with_retry", lambda page, url: True)
        self.patch(fb, "city_was_dropped", lambda page, seg: False)
        self.patch(fb, "human_pause", lambda *a, **k: None)

        # Which city (in sweep order) Ctrl-C lands on, and how.
        self.swept, self.raise_on, self.stop_on = 0, set(), set()
        self.patch(fb, "collect_city", self.collect)

        self.described, self.thumbed = [], []
        self.patch(fb, "retrieve_descriptions", self.describe)
        self.patch(fb, "fetch_thumbs", self.thumbs)
        self.descriptions_finish, self.thumbs_stop = True, False

    def collect(self, page, scrolls, probe, verbose=False):
        self.swept += 1
        n = self.swept
        if n in self.raise_on:
            raise KeyboardInterrupt
        stopped = n in self.stop_on
        stats = {"scrolls_used": 1, "scroll_ceiling": scrolls, "cards": 2,
                 "keepers_seen": 2, "last_keeper_scroll": 1,
                 "seconds_per_scroll_recent": 1.0,
                 "stop_reason": fb.STOPPED_BY_HAND if stopped else "divider"}
        cards = {f"{n}{i}": card(f"{n}{i}") for i in range(2)}
        return cards, True, "", stats

    def describe(self, ctx, page, targets, thumbs, dump, pace, on_row=None):
        self.described.append(len(targets))
        return self.descriptions_finish

    def thumbs(self, ctx, rows, path):
        self.thumbed.append(len(rows))
        if self.thumbs_stop:
            raise KeyboardInterrupt

    def sweep(self, **kw):
        return self.quietly(fb.run, "defender", 5, False, open_gallery=False,
                            no_pause=True, **kw)

    def ids_in_csv(self, summary):
        with open(summary["csv"], newline="", encoding="utf-8") as f:
            return sorted(r["item_id"] for r in csv.DictReader(f))

    def manifest(self, summary):
        return json.loads(
            (Path(summary["run_dir"]) / "run.json").read_text(encoding="utf-8"))

    # -- stopped during the sweep -------------------------------------------
    def test_a_sweep_stopped_at_the_second_city_keeps_the_first(self):
        self.raise_on = {2}
        summary = self.sweep()

        self.assertEqual(summary["status"], "ok")
        self.assertEqual(self.ids_in_csv(summary), ["10", "11"])
        self.assertTrue(Path(summary["gallery"]).exists())
        self.assertTrue(summary["interrupted"])
        self.assertEqual(summary["interrupted_during"], "sweep")
        self.assertIn("Stopped during the sweep", self.said)
        self.assertIn("Writing the 2 listings already found", self.said)

    def test_the_city_being_swept_is_kept_as_far_as_it_got(self):
        # collect_city hands back the cards it had when the scrolling stopped,
        # and they are worth exactly as much as a city that finished.
        self.stop_on = {2}
        summary = self.sweep()
        self.assertEqual(self.ids_in_csv(summary), ["10", "11", "20", "21"])

    def test_the_cities_after_it_are_not_swept(self):
        self.raise_on = {2}
        self.sweep()
        self.assertEqual(self.swept, 2)

    def test_stopping_the_sweep_skips_straight_to_the_gallery(self):
        self.raise_on = {2}
        summary = self.sweep()
        self.assertEqual(self.described, [])
        self.assertEqual(self.thumbed, [])
        self.assertTrue(Path(summary["gallery"]).exists())
        self.assertTrue(Path(summary["run_dir"], "run.json").exists())

    def test_what_was_swept_is_in_the_database_too(self):
        self.raise_on = {3}
        self.sweep()
        con = storage.open_db(storage.DB_PATH)
        n = con.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        con.close()
        self.assertEqual(n, 4)

    # -- stopped later on ----------------------------------------------------
    def test_stopping_the_descriptions_still_builds_the_gallery(self):
        self.descriptions_finish = False
        summary = self.sweep()
        self.assertEqual(self.described, [6])
        # The photos are a second pass over the same listings; there is no
        # sense in starting one after being asked to stop.
        self.assertEqual(self.thumbed, [])
        self.assertTrue(Path(summary["gallery"]).exists())
        self.assertEqual(summary["interrupted_during"], "descriptions")

    def test_stopping_the_thumbnails_keeps_the_ones_downloaded(self):
        self.thumbs_stop = True
        summary = self.sweep()
        self.assertEqual(self.thumbed, [6])
        self.assertTrue(Path(summary["gallery"]).exists())
        self.assertEqual(summary["interrupted_during"], "thumbnails")

    def test_a_run_nobody_stopped_says_so(self):
        summary = self.sweep()
        self.assertFalse(summary["interrupted"])
        self.assertIsNone(summary["interrupted_during"])
        self.assertEqual(len(self.ids_in_csv(summary)), 6)
        self.assertFalse(self.manifest(summary)["interrupted"])

    # -- stopped before there was anything -----------------------------------
    def test_quitting_at_the_login_leaves_no_run_folder_behind(self):
        # The folder is made before the browser opens, and the Past searches
        # tab lists folders, so an abandoned one would show up there as a run
        # that found nothing.
        def quit_now(page, **kw):
            raise KeyboardInterrupt
        self.patch(fb, "ensure_logged_in", quit_now)

        with self.assertRaises(SystemExit):
            self.sweep()
        self.assertEqual(list((self.root / "runs").glob("*")), [])

    def test_stopping_before_a_single_listing_saves_nothing(self):
        self.raise_on = {1}
        summary = self.sweep(only_labels=["Medford, OR"])
        self.assertEqual(summary["status"], "error")
        self.assertIn("Stopped", summary["error"])
        self.assertEqual(list((self.root / "runs").glob("*")), [])


class _Nothing:
    """A context manager standing in for Playwright and for keep_awake()."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


if __name__ == "__main__":
    unittest.main()
