#!/usr/bin/env python3
"""
Offline tests for the sweep, and for what stopping one does to it.

    python3 -m unittest tests.test_sweep

A run takes hours, so stopping one has to be a way of finishing rather than a
way of failing: whatever has been found is kept, and the outputs are written
from it. That promise is the whole subject here, and it only means anything if
it holds at every stage, so each stage gets its own interrupt.

There are two ways to stop a run — Ctrl-C in the terminal and closing the browser
window — and they are meant to be the same ending, so both are tested against the
same promise.

No browser is opened. Playwright, the login, the page and the two stages that
download things are all stubs, which is also what lets a test say exactly where
the interruption lands.
"""
import csv
import io
import json
import re
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import browser
import fb_marketplace_sweep as fb
import locations
import storage

# Playwright's wording when the window has gone, which is all the app has to go
# on: the exception class it arrives as depends on which call was in flight.
GONE = ("Page.mouse.wheel: Target page, context or browser has been closed")

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
    fresh batch of cards, and the test says which turn the run is stopped on and
    whether it's stopped by Ctrl-C or by the window being closed."""

    def __init__(self, per_scroll=2, stop_on=None, closed_on=None):
        self.per_scroll, self.stop_on, self.scrolls = per_scroll, stop_on, 0
        self.closed_on = closed_on
        self.mouse = FakeMouse(self)

    def turn(self):
        self.scrolls += 1
        if self.scrolls == self.stop_on:
            raise KeyboardInterrupt
        if self.scrolls == self.closed_on:
            raise RuntimeError(GONE)

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

    def test_closing_the_window_keeps_them_the_same_way_ctrl_c_does(self):
        cards, _, _, stats = self.quietly(
            fb.collect_city, ScrollPage(closed_on=3), 20, None)
        self.assertEqual(len(cards), 6)
        self.assertEqual(stats["stop_reason"], fb.STOPPED_BY_HAND)
        self.assertIn("The Facebook window was closed", self.said)
        self.assertIn("Keeping the 6 cards", self.said)

    def test_the_on_page_notice_cannot_pass_for_facebooks_divider(self):
        # CARDS_JS finds the out-of-radius divider by looking for a short element
        # whose text says the results came from elsewhere. The note the app draws
        # over every page is a short element that mentions searching, and a false
        # divider is expensive: every card after it gets dropped.
        pattern = re.search(r"const rx = /(.+)/i;", fb.CARDS_JS).group(1)
        self.assertIsNone(re.search(pattern, browser.NOTICE_TEXT, re.I))
        # And the real thing still matches, so the check above means something.
        self.assertTrue(re.search(pattern, "Results from outside your search",
                                  re.I))

    def test_an_error_that_is_not_a_closed_window_is_still_an_error(self):
        # Only a window that has gone gets turned into an ending. Anything else
        # is a bug, and swallowing it would hide it behind a short run.
        page = ScrollPage()
        page.turn = lambda: (_ for _ in ()).throw(RuntimeError("something else"))
        with self.assertRaises(RuntimeError):
            self.quietly(fb.collect_city, page, 20, None)


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


class SweepHarness(Patched):
    """`run()` with the browser, the login and the two downloading stages all
    replaced, so a test can say exactly where a run is interrupted."""

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


class TestStoppingARun(SweepHarness):
    """Each test stops the run somewhere different and then asks the same
    question: is the work still there?"""

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


class TestClosingTheBrowserWindow(SweepHarness):
    """Closing the window is the other way to stop a run, and it has to end the
    same way — not least because the alternative was a process wedged inside
    Playwright, still holding the run lock, which left the next launch unable to
    start at all."""

    def gone(self, *a, **kw):
        raise browser.WindowClosed("The Facebook window was closed.")

    def test_a_window_closed_mid_sweep_keeps_the_cities_already_done(self):
        cities = iter(range(3))

        def navigate(page, url):
            if next(cities) >= 1:
                self.gone()
            return True
        self.patch(fb, "goto_with_retry", navigate)

        summary = self.sweep()
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(self.ids_in_csv(summary), ["10", "11"])
        self.assertTrue(Path(summary["gallery"]).exists())
        self.assertTrue(summary["interrupted"])
        self.assertEqual(summary["interrupted_during"], "sweep")

    def test_the_browser_is_not_closed_again_on_the_way_out(self):
        # Closing a context out of a Playwright call that already failed is what
        # used to hang the run one step short of writing anything.
        self.patch(fb, "goto_with_retry", self.gone)
        self.sweep()
        self.assertFalse(self.ctx.closed)

    def test_a_window_closed_before_the_sweep_says_which_it_was(self):
        self.patch(fb, "ensure_logged_in", self.gone)
        with self.assertRaises(SystemExit) as caught:
            self.sweep()
        self.assertIn("The Facebook window was closed", str(caught.exception))
        self.assertEqual(list((self.root / "runs").glob("*")), [])


class TestNamingTheRunFolder(SweepHarness):
    def folder(self, queries):
        return Path(self.quietly(
            fb.run, queries, 5, False, open_gallery=False, no_pause=True,
            only_labels=["Medford, OR"])["run_dir"]).name

    def test_one_query_names_the_folder(self):
        self.assertTrue(self.folder("defender 110").startswith("defender_110_"))

    def test_several_queries_are_named_for_the_first_alone(self):
        # All of them strung together makes a name too long for the systems that
        # have to hold it. The whole search is in run.json either way.
        name = self.folder(["defender 110", "land rover 90", "series iii"])
        self.assertTrue(name.startswith("defender_110_"), name)
        self.assertNotIn("land_rover", name)
        self.assertNotIn("series", name)

    def test_the_whole_search_is_still_written_down(self):
        summary = self.quietly(fb.run, ["defender 110", "land rover 90"], 5,
                               False, open_gallery=False, no_pause=True,
                               only_labels=["Medford, OR"])
        self.assertEqual(self.manifest(summary)["query"],
                         "defender 110 OR land rover 90")


class Args:
    """The command-line namespace run_from_ui reads its seed values from."""
    query = None
    exclude = ""
    pace = "fast"
    out = None
    keep_all = False
    match = None
    thumbs_dir = "thumbnails"
    no_open = False
    no_pause = True


class FakePage:
    def wait_for_timeout(self, ms):
        pass


class FakeWindow:
    """Stands in for the settings window. Each item in `answers` is what one
    opening of it submits; running out of them is the window being closed."""

    def __init__(self, test, answers):
        self.test, self.answers, self.seeds = test, list(answers), []

    def __call__(self, cities, paces, defaults, **kw):
        self.seeds.append(dict(defaults))
        self.test.events.append("window")
        if kw.get("on_ready"):
            kw["on_ready"](FakePage())
        return self.answers.pop(0) if self.answers else None


def search(**over):
    asked = {"action": "sweep", "queries": ["defender 110"],
             "cities": ["Medford, OR"], "exact": False, "min_price": None,
             "max_price": None, "min_year": None, "max_year": None,
             "include_no_year": True, "exclude": "", "do_descriptions": True,
             "do_thumbs": True, "debug_dump": False, "pace": "fast",
             "limit": None}
    return {**asked, **over}


class TestTheAppWindow(Patched):
    """run_from_ui, which is the app as far as anyone using it is concerned: the
    settings window, the searches started from it, and what it does between
    them."""

    def setUp(self):
        import past_runs
        import scheduling
        import settings_ui
        self.settings_ui, self.scheduling = settings_ui, scheduling
        self.events, self.ran = [], []
        self.patch(locations, "load_locations", lambda: dict(CITIES))
        self.patch(locations, "base_locations", lambda: dict(CITIES))
        self.patch(scheduling, "run_lock", lambda what: _Nothing())
        self.patch(scheduling, "ui_hooks", dict)
        self.patch(past_runs, "ui_hooks", dict)
        self.patch(fb.updater, "ui_hooks", dict)
        self.patch(fb, "show_gallery", lambda path: self.events.append("gallery"))

        def sweep(*a, **kw):
            self.events.append("search")
            self.ran.append(kw)
            return {"status": "ok", "gallery": "/nowhere/gallery.html"}
        self.patch(fb, "run", sweep)

    def open_window(self, *answers):
        window = FakeWindow(self, answers)
        self.patch(self.settings_ui, "collect_settings", window)
        return window

    def test_closing_the_window_is_how_the_app_is_quit(self):
        # Nothing is running and nothing is half-finished, so there is nothing to
        # keep the process — or the terminal window behind it — open for.
        self.open_window()
        with self.assertRaises(SystemExit) as caught:
            self.quietly(fb.run_from_ui, Args())
        self.assertEqual(caught.exception.code, fb.CLOSED_EXIT)

    def test_a_finished_search_comes_back_to_the_window(self):
        window = self.open_window(search())
        with self.assertRaises(SystemExit):
            self.quietly(fb.run_from_ui, Args())
        self.assertEqual(len(window.seeds), 2)

    def test_the_results_open_on_top_of_the_window_not_before_it(self):
        # The listings are what you want to be looking at, and the app ready for
        # the next search is what you want underneath them.
        self.open_window(search())
        with self.assertRaises(SystemExit):
            self.quietly(fb.run_from_ui, Args())
        self.assertEqual(self.events, ["window", "search", "window", "gallery"])
        # Which means the run itself must not open it on the way past.
        self.assertFalse(self.ran[0]["open_gallery"])

    def test_the_form_comes_back_holding_the_last_search(self):
        self.open_window(search(queries=["series iii"], exclude="rhd, parts"))
        window = self.settings_ui.collect_settings
        with self.assertRaises(SystemExit):
            self.quietly(fb.run_from_ui, Args())
        self.assertEqual(window.seeds[1]["queries"], ["series iii"])
        self.assertEqual(window.seeds[1]["exclude"], "rhd, parts")

    def test_nothing_is_opened_when_the_search_found_nothing(self):
        self.patch(fb, "run", lambda *a, **kw: {"status": "ok", "gallery": None})
        self.open_window(search())
        with self.assertRaises(SystemExit):
            self.quietly(fb.run_from_ui, Args())
        self.assertNotIn("gallery", self.events)

    def test_run_now_on_a_scheduled_search_comes_back_to_the_window_too(self):
        forced = []
        self.patch(self.scheduling, "tick",
                   lambda force=None: forced.append(force))
        window = self.open_window({"action": "run_saved", "id": "nightly"})
        with self.assertRaises(SystemExit):
            self.quietly(fb.run_from_ui, Args())
        self.assertEqual(forced, ["nightly"])
        self.assertEqual(len(window.seeds), 2)
        # Those results go out by email, so there's no gallery to put up.
        self.assertNotIn("gallery", self.events)

    def test_a_window_that_will_not_open_still_says_where_the_results_went(self):
        def broken(cities, paces, defaults, **kw):
            self.events.append("window")
            if len(self.events) > 1:
                raise RuntimeError("Chromium wouldn't start")
            return search()
        self.patch(self.settings_ui, "collect_settings", broken)
        with self.assertRaises(RuntimeError):
            self.quietly(fb.run_from_ui, Args())
        self.assertEqual(self.events, ["window", "search", "window", "gallery"])


class TestQuittingLeavesNothingBehind(unittest.TestCase):
    """The handshake between quitting the app and the launcher that started it.

    Quitting is worth nothing if the terminal window it ran in stays on screen:
    quit five times and there are five dead windows to clear up by hand. The exit
    code is how the app says which kind of ending this was, and it's a bare
    number written out in three files — so the only thing that can go wrong is
    them disagreeing, and that's what this pins down.
    """

    def launcher(self, name):
        return (Path(fb.__file__).resolve().parent.parent / name).read_text(
            encoding="utf-8")

    def test_both_launchers_watch_for_the_code_the_app_actually_exits_with(self):
        self.assertIn(f"CLOSED={fb.CLOSED_EXIT}",
                      self.launcher("Start Faceplace Marketbook (Mac).command"))
        self.assertIn(f'"%STATUS%"=="{fb.CLOSED_EXIT}"',
                      self.launcher("Start Faceplace Marketbook (Windows).bat"))

    def test_the_mac_launcher_closes_its_window_rather_than_just_exiting(self):
        # Exiting isn't enough on a Mac. Terminal decides for itself what to do
        # with a window whose shell has finished, and left alone it keeps it,
        # showing "[Process completed]" — so the window has to be closed by
        # asking Terminal to close it.
        script = self.launcher("Start Faceplace Marketbook (Mac).command")
        self.assertIn("close_this_window", script)
        # Only ever under Terminal. Anywhere else that would be one program
        # driving another, which macOS puts up a permission dialog for.
        self.assertIn('"$TERM_PROGRAM" = "Apple_Terminal"', script)
        # And only the window this script is running in, matched by its terminal
        # device. Someone's other Terminal windows are none of its business.
        self.assertIn("tty of item 1 of panes", script)

    def test_the_asking_waits_until_nothing_is_left_in_the_window(self):
        # The failure this is here to prevent: asking while this script, the
        # osascript and the last of Chromium are still on the terminal, which
        # gets "do you want to terminate running processes in this window?" put
        # up and left waiting for an answer. Which is worse than the dead window
        # it was meant to save. So the asking is detached into a session of its
        # own — that is what takes it out of the count Terminal makes — and holds
        # off until the terminal has emptied.
        script = self.launcher("Start Faceplace Marketbook (Mac).command")
        self.assertIn("os.setsid()", script)
        self.assertIn('"ps", "-t", terminal', script)
        # nohup rather than a bare &, or the hangup sent when the window's shell
        # goes would take the waiting process with it.
        self.assertIn("nohup", script)

    def test_a_launch_sharing_a_window_with_other_tabs_closes_nothing(self):
        # Terminal can close a window but has no way to close one tab of it, and
        # someone else's tab may be running a search. A dead tab left behind is
        # the smaller harm by a wide margin.
        script = self.launcher("Start Faceplace Marketbook (Mac).command")
        self.assertIn("(count of panes) is 1", script)


class _Nothing:
    """A context manager standing in for Playwright and for keep_awake()."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


if __name__ == "__main__":
    unittest.main()
