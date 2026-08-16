#!/usr/bin/env python3
"""
Drives a built gallery headlessly.

    python3 -m unittest tests.test_gallery_ui

The gallery is a single file of HTML with all of its behaviour inline, so the
only honest way to test it is to build one and open it. Nothing here reaches
Facebook or the network: the page is built from a CSV written a few lines above
it and loaded off disk. It does need Playwright's Chromium, so like
test_settings_ui it is kept out of the offline suite's file.

Galleries come in two shapes and both are exercised. Only a scheduled search on
its second run or later has first-found dates to put in its CSV; every other
gallery is built without the column, and the page has to leave the line and the
sort option out rather than offer either with nothing behind it.

The browser is given a fixed time zone and locale. The page deliberately shows
first-found times on the reader's own clock, and a test that let the machine
decide would pass or fail depending on where it was run.
"""
import json
import socket
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import build_gallery  # noqa: E402
import marks  # noqa: E402
import scheduling as sc  # noqa: E402
import storage  # noqa: E402

try:
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright
    HAVE_PLAYWRIGHT = True
except ImportError:
    HAVE_PLAYWRIGHT = False

ZONE, LOCALE = "America/Los_Angeles", "en-US"

# Three listings found on three different days, plus one from before the
# archive recorded such a thing, deliberately not in date order so a passing
# sort test can't be the CSV's doing.
LISTINGS = [
    {"item_id": "b", "title": "1972 Bronco", "price": "$8,000",
     "listing_location": "Medford, OR", "description": "Runs.",
     "first_found": "2026-07-04T16:30:00+00:00"},
    {"item_id": "a", "title": "1965 Land Cruiser", "price": "$12,000",
     "listing_location": "Boise, ID", "description": "Solid frame.",
     "first_found": "2026-08-15T21:00:00+00:00"},
    {"item_id": "d", "title": "1988 Range Rover", "price": "$3,500",
     "listing_location": "Reno, NV", "description": "Needs work.",
     "first_found": ""},
    {"item_id": "c", "title": "1994 Defender 110", "price": "$40,000",
     "listing_location": "Denver, CO", "description": "Galvanised chassis.",
     "first_found": "2026-06-01T09:15:00+00:00"},
    # Midnight on the thirtieth of a month whose name doesn't shorten: the
    # widest this line ever gets, which is what the narrow-column test needs.
    {"item_id": "e", "title": "2003 Mercedes G500", "price": "$29,000",
     "listing_location": "Sacramento, CA", "description": "Widest date there is.",
     "first_found": "2026-09-30T07:00:00+00:00"},
]
# The same for all of them, and none of it what any test turns on: it is here so
# the panel has the sweep line it would have in a real run.
for _r in LISTINGS:
    _r.update(location_searched=_r["listing_location"], query="defender",
              scraped_at="2026-08-15T21:04:00+00:00")


def spaces(s):
    """Intl puts a narrow no-break space before AM/PM, and which one depends on
    the ICU the browser was built with. Not what any of these tests are about."""
    return (s or "").replace("\u202f", " ").replace("\u00a0", " ")


@unittest.skipUnless(HAVE_PLAYWRIGHT, "needs Playwright")
class GalleryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.errors = []
        self.html = self.build()

    def build(self, dated=True, name="results"):
        """A gallery with or without the first-found column, which is the
        difference between a saved search's second run and everything else."""
        fields = storage.FIELDS + ([storage.FIRST_FOUND] if dated else [])
        csv_path = self.root / f"{name}.csv"
        storage.write_csv(LISTINGS, csv_path, fields)
        return Path(build_gallery.build(csv_path, csv_path.with_suffix(".html"),
                                        embed=False, quiet=True, images=False))

    def open(self, script, width=1180, html=None):
        """Opens a built gallery and runs `script(page)`."""
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(timezone_id=ZONE, locale=LOCALE,
                                      viewport={"width": width, "height": 900})
            page = ctx.new_page()
            page.on("console", lambda m: m.type == "error"
                    and self.errors.append(m.text))
            page.on("pageerror", lambda e: self.errors.append(str(e)))
            try:
                page.goto((html or self.html).as_uri())
                script(page)
            finally:
                ctx.close()
                browser.close()
        self.assertEqual(self.errors, [], f"JavaScript errors: {self.errors}")

    def sort_options(self, page):
        page.click("#sort .sel-btn")
        return page.eval_on_selector_all("#sort .sel-list li",
                                         "els => els.map(e => e.textContent)")

    def sort_by(self, page, label):
        page.click("#sort .sel-btn")
        page.click(f"#sort .sel-list li:text-is('{label}')")


    def titles(self, page):
        return page.eval_on_selector_all(
            "#grid .card .title", "els => els.map(e => e.textContent)")

    # ---------------------------------------------------------------- tests
    def test_every_card_says_when_the_listing_was_first_found(self):
        # 21:00 UTC on the 15th is 2pm the same day in the zone above.
        def script(page):
            found = {t: spaces(f) for t, f in zip(
                self.titles(page),
                page.eval_on_selector_all(
                    "#grid .card", "els => els.map(e => "
                    "e.querySelector('.meta.found')?.textContent ?? '')"))}
            self.assertEqual(found["1965 Land Cruiser"],
                             "First found Aug 15, 2026, 2:00 PM")
            self.assertEqual(found["1972 Bronco"],
                             "First found Jul 4, 2026, 9:30 AM")
            self.assertEqual(found["1994 Defender 110"],
                             "First found Jun 1, 2026, 2:15 AM")
        self.open(script)

    def test_the_date_sits_between_the_location_and_the_description(self):
        # Where it was asked for, and where it reads as part of the listing's
        # provenance rather than as a note on the description.
        def script(page):
            self.assertEqual(
                page.eval_on_selector(
                    "#grid .card .body",
                    "el => [...el.children].map(c => c.className)"),
                ["price", "title", "meta", "meta found", "desc", "foot"])
        self.open(script)

    def test_the_date_fits_on_one_line_in_the_narrowest_column(self):
        # It is the longest line on a card, and a column is never narrower than
        # 250px, so it wraps for the sake of its last two characters unless the
        # tracking is held down. September at midnight is the widest it gets.
        def script(page):
            # The measurement is only worth anything in the face the page is
            # actually set in, and that one arrives from Google Fonts.
            page.evaluate("() => document.fonts.ready")
            if not page.evaluate(
                    "() => document.fonts.check('10px \"Courier Prime\"')"):
                self.skipTest("Courier Prime didn't load, and the fallback "
                              "face is a different width")
            self.assertEqual(page.evaluate("""() => {
              const r = document.createRange();
              return Math.max(...[...document.querySelectorAll('.meta.found')]
                .map(el => (r.selectNodeContents(el),
                            r.getClientRects().length)));
            }"""), 1)
        # The one width at which the columns are exactly their 250px minimum:
        # 26px of padding each side, two columns and the 22px gap between them.
        self.open(script, width=26 * 2 + 250 * 2 + 22)

    def test_the_sort_menu_opens_inside_the_window(self):
        # Sort is the last control in the header, so a longer option than the
        # ones already there hangs off the right-hand side of the page.
        def script(page):
            page.click("#sort .sel-btn")
            self.assertTrue(page.evaluate("""() => {
              const box = document.querySelector('#sort .sel-list')
                                  .getBoundingClientRect();
              return box.left >= 0 && box.right <= window.innerWidth;
            }"""), "the sort menu runs off the page")
        self.open(script)

    def test_a_row_with_an_empty_date_says_nothing_rather_than_guessing(self):
        # A hand-edited or hand-built CSV can leave the cell blank. Better a
        # card that doesn't say than one reading "First found Invalid Date".
        def script(page):
            self.assertEqual(
                page.eval_on_selector_all(
                    "#grid .card", "els => els.filter(e => e.querySelector"
                    "('.title').textContent === '1988 Range Rover')"
                    ".map(e => e.querySelectorAll('.meta.found').length)"),
                [0])
        self.open(script)

    def test_a_gallery_with_no_dates_in_it_says_nothing_anywhere(self):
        # A one-off run, or a saved search's first run: every listing was found
        # by the run that built the page, so the line would say the same thing
        # on every card and mean nothing on any of them.
        undated = self.build(dated=False, name="undated")

        def script(page):
            self.assertEqual(page.locator(".meta.found").count(), 0)
            page.click("#grid .card:has-text('1965 Land Cruiser')")
            self.assertEqual(page.locator(".sheet .meta.found").count(), 0)
        self.open(script, html=undated)

    def test_the_sort_option_comes_and_goes_with_the_dates(self):
        # An option that couldn't reorder anything is a promise the page can't
        # keep, so it isn't offered.
        without = ["Original order", "Price: low → high", "Price: high → low",
                   "Year: newest first", "Year: oldest first", "Title A→Z"]
        with_them = without[:-1] + ["First found: newest first",
                                    "First found: oldest first", "Title A→Z"]
        undated = self.build(dated=False, name="undated")
        self.open(lambda page: self.assertEqual(self.sort_options(page), without),
                  html=undated)
        self.open(lambda page: self.assertEqual(self.sort_options(page), with_them))

    def test_the_opened_card_says_it_too(self):
        def script(page):
            page.click("#grid .card:has-text('1965 Land Cruiser')")
            self.assertEqual(spaces(page.text_content(".sheet .meta.found")),
                             "First found Aug 15, 2026, 2:00 PM")
        self.open(script)

    def test_the_opened_card_calls_the_other_date_a_last_sighting(self):
        # Two bare dates in one panel, one of them the opposite of the other,
        # is the confusion the label exists to prevent.
        def script(page):
            page.click("#grid .card:has-text('1965 Land Cruiser')")
            self.assertIn("last seen", page.text_content(".sheet-body"))
        self.open(script)

    def test_sorting_by_newest_first_found(self):
        def script(page):
            self.sort_by(page, "First found: newest first")
            self.assertEqual(self.titles(page),
                             ["2003 Mercedes G500", "1965 Land Cruiser",
                              "1972 Bronco", "1994 Defender 110",
                              "1988 Range Rover"])
        self.open(script)

    def test_sorting_by_oldest_first_found(self):
        # The undated listing stays at the bottom rather than leading, the same
        # way an undated listing does when sorting by year.
        def script(page):
            self.sort_by(page, "First found: oldest first")
            self.assertEqual(self.titles(page),
                             ["1994 Defender 110", "1972 Bronco",
                              "1965 Land Cruiser", "2003 Mercedes G500",
                              "1988 Range Rover"])
        self.open(script)

    def test_the_other_sorts_still_work(self):
        # byNumber() replaced byYear(), and the year sort is what it was.
        def script(page):
            self.sort_by(page, "Year: newest first")
            self.assertEqual(self.titles(page)[0], "2003 Mercedes G500")
            self.sort_by(page, "Year: oldest first")
            self.assertEqual(self.titles(page)[0], "1965 Land Cruiser")
            self.sort_by(page, "Price: low → high")
            self.assertEqual(self.titles(page)[0], "1988 Range Rover")
        self.open(script)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(HAVE_PLAYWRIGHT, "needs Playwright")
class MarksTest(unittest.TestCase):
    """Starring and hiding, from the click through to the file on disk.

    The gallery saves through a small server on this computer, because a page
    cannot write to the file it was opened from. So the page has two states, and
    both are here: with that server answering it saves, and without it the
    buttons go quiet rather than taking a click that would be lost.

    Everything runs against a real server on a spare port, with the page built
    to call that port instead of the usual one. Nothing touches the real one —
    a test that did would talk to whatever the machine already had running.
    """

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runs = Path(self.tmp.name) / "runs"
        self.folder = self.runs / "defender_08-15-2026"
        self.folder.mkdir(parents=True)
        self.port = free_port()
        for mod, name, value in ((marks, "PORT", self.port),
                                 (build_gallery, "RUNS_DIR", self.runs),
                                 (sc, "RUNS_DIR", self.runs)):
            self.addCleanup(setattr, mod, name, getattr(mod, name))
            setattr(mod, name, value)
        self.csv = self.folder / "results.csv"
        storage.write_csv(LISTINGS, self.csv, storage.FIELDS)
        self.errors = []

    def build(self, **kw):
        kw.setdefault("quiet", True)
        kw.setdefault("images", False)
        return Path(build_gallery.build(self.csv, self.folder / "gallery.html",
                                        **kw))

    def serve(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", self.port), sc._GalleryHandler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd

    def open(self, script, url=None, html=None):
        page_url = url or (html or self.build()).as_uri()
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(timezone_id=ZONE, locale=LOCALE,
                                      viewport={"width": 1180, "height": 900})
            page = ctx.new_page()
            page.on("pageerror", lambda e: self.errors.append(str(e)))
            try:
                page.goto(page_url)
                # The page draws from the file, then asks the app whether it can
                # save and draws again. Everything here is about the second one.
                page.wait_for_function("() => document.body.dataset.marks")
                script(page)
            finally:
                ctx.close()
                browser.close()
        self.assertEqual(self.errors, [], f"JavaScript errors: {self.errors}")

    def served_url(self, gallery):
        return f"http://127.0.0.1:{self.port}{gallery.resolve().as_posix()}"

    def marks_on_disk(self):
        return marks.read(self.folder)

    def baked(self, gallery=None):
        """The marks as they stand inside the file, which is the copy that goes
        with it when it's emailed."""
        html = (gallery or (self.folder / "gallery.html")).read_text(encoding="utf-8")
        start = html.index('<script id="marks" type="application/json">')
        start = html.index(">", start) + 1
        return json.loads(html[start:html.index("</script>", start)])

    def star(self, page, title):
        page.click(f".card:has-text('{title}') .star-btn")

    def is_off(self, page):
        return page.eval_on_selector_all(
            ".star-btn", "els => els.every(e => e.classList.contains('off'))")

    def note_shown(self, page):
        """Waits out the fade rather than reading opacity the instant after the
        hover, which catches it a twentieth of the way in."""
        try:
            page.wait_for_function(
                "() => getComputedStyle(document.querySelector('.locknote'))"
                ".opacity === '1'", timeout=2000)
            return True
        except PWTimeout:
            return False

    def press(self, locator):
        """Clicks a button the page has marked aria-disabled. Playwright won't
        do it on its own — it reads that the way a screen reader does, as a
        control that isn't taking clicks. A person's mouse has no such scruple,
        and being clicked anyway is exactly the case being tested."""
        locator.click(force=True)

    # ------------------------------------------------ nothing listening
    def test_with_no_app_running_the_buttons_go_quiet(self):
        def script(page):
            self.assertTrue(self.is_off(page))
            self.assertEqual(
                page.eval_on_selector_all(".star-btn",
                                          "els => els.map(e => e.ariaDisabled)"),
                ["true"] * len(LISTINGS))
        self.open(script)

    def test_the_quiet_buttons_say_why_on_hover(self):
        # Not a line across the top of the page: the buttons already only come
        # out on hover, so the reason rides the same gesture.
        def script(page):
            note = page.locator(".card").first.locator(".locknote")
            self.assertEqual(note.evaluate("el => getComputedStyle(el).opacity"),
                             "0")
            page.locator(".card").first.locator(".star-btn").hover()
            self.assertTrue(self.note_shown(page))
            self.assertIn("Faceplace Marketbook", note.inner_text())
        self.open(script)

    def test_clicking_a_quiet_button_says_it_too(self):
        # Hover is no help on a touchscreen, and people click before they read.
        def script(page):
            self.press(page.locator(".card").first.locator(".star-btn"))
            self.assertTrue(self.note_shown(page))
        self.open(script)

    def test_a_click_with_nowhere_to_go_does_not_star_anything(self):
        def script(page):
            self.press(page.locator(".card").first.locator(".star-btn"))
            self.assertEqual(page.locator(".card.is-starred").count(), 0)
        self.open(script)
        self.assertEqual(self.marks_on_disk(), marks.empty())

    def test_marks_already_in_the_file_still_show_without_the_app(self):
        # The whole point of keeping them in the file: someone opens a gallery
        # you sent them, on a machine with none of this on it, and sees what you
        # starred.
        marks.write(self.folder, {"starred": ["a"], "hidden": ["b"]})

        def script(page):
            self.assertEqual(page.locator(".card.is-starred .title").inner_text(),
                             "1965 Land Cruiser")
            self.assertTrue(self.is_off(page))
        self.open(script)

    # ------------------------------------------------ the app is running
    def test_a_star_reaches_the_run_folder(self):
        self.serve()
        gallery = self.build()

        def script(page):
            self.assertFalse(self.is_off(page))
            self.star(page, "1965 Land Cruiser")
            page.wait_for_timeout(900)
        self.open(script, url=self.served_url(gallery))
        self.assertEqual(self.marks_on_disk()["starred"], ["a"])

    def test_a_star_is_written_into_the_gallery_on_disk(self):
        # So the file is the one you'd want to email without rebuilding it.
        self.serve()
        gallery = self.build()

        def script(page):
            self.star(page, "1965 Land Cruiser")
            page.wait_for_timeout(900)
        self.open(script, url=self.served_url(gallery))
        self.assertEqual(self.baked()["starred"], ["a"])

    def test_a_star_survives_reopening_the_gallery(self):
        self.serve()
        gallery = self.build()

        def mark(page):
            self.star(page, "1965 Land Cruiser")
            page.wait_for_timeout(900)
        self.open(mark, url=self.served_url(gallery))

        def check(page):
            self.assertEqual(page.locator(".card.is-starred .title").inner_text(),
                             "1965 Land Cruiser")
        self.open(check, url=self.served_url(gallery))

    def test_hiding_a_starred_listing_unstars_it_on_disk_too(self):
        self.serve()
        gallery = self.build()

        def script(page):
            self.star(page, "1965 Land Cruiser")
            page.click(".card:has-text('1965 Land Cruiser') .hide-btn")
            page.wait_for_timeout(900)
        self.open(script, url=self.served_url(gallery))
        self.assertEqual(self.marks_on_disk(),
                         {"starred": [], "hidden": ["a"]})

    def test_a_gallery_built_to_be_emailed_never_asks(self):
        # It has no run id, so it stays read-only even sitting on the computer
        # that made it, with the app running and answering.
        self.serve()
        gallery = self.build(editable=False)

        def script(page):
            self.assertTrue(self.is_off(page))
        self.open(script, url=self.served_url(gallery))

    def test_the_app_wins_when_the_file_is_out_of_date(self):
        # A sweep rebuilt the page while it was open in another tab, or the
        # marks changed in the other gallery in the same folder.
        gallery = self.build()
        marks.write(self.folder, {"starred": ["c"], "hidden": []})
        self.serve()

        def script(page):
            self.assertEqual(page.locator(".card.is-starred .title").inner_text(),
                             "1994 Defender 110")
        self.open(script, url=self.served_url(gallery))

    def test_opening_the_app_wakes_a_gallery_that_was_already_open(self):
        """The note says to open Faceplace Marketbook. Doing that has to be
        enough on its own.

        Without this, following the instruction changes nothing until you also
        think to reload the page — and nothing on screen tells you to. Coming
        back to the window is what prompts the second look.

        The event is raised here rather than by really switching windows: a
        headless browser considers every page focused and visible, so there is
        no away to come back from. What that leaves untested is the browser
        raising the event, which is ordinary browser behaviour; what it does
        test is the listener, which is ours.
        """
        gallery = self.build()

        def script(page):
            self.assertEqual(page.evaluate("() => document.body.dataset.marks"),
                             "locked")
            self.assertTrue(self.is_off(page))
            self.serve()  # the app, opened while the gallery sat there
            page.evaluate("() => window.dispatchEvent(new Event('focus'))")
            page.wait_for_function(
                "() => document.body.dataset.marks === 'live'", timeout=5000)
            self.assertFalse(self.is_off(page))
            # And it saves, rather than merely looking as though it would.
            self.star(page, "1965 Land Cruiser")
            page.wait_for_timeout(900)
        self.open(script, html=gallery)
        self.assertEqual(self.marks_on_disk()["starred"], ["a"])

    def test_coming_back_to_a_sent_copy_does_not_keep_asking(self):
        # It has no run folder to ask about, so every one of these would be a
        # request that could only fail.
        gallery = self.build(editable=False)
        self.serve()

        def script(page):
            for _ in range(3):
                page.evaluate("() => window.dispatchEvent(new Event('focus'))")
            page.wait_for_timeout(300)
            self.assertTrue(self.is_off(page))
        self.open(script, html=gallery)

    def test_a_sent_copy_says_nothing_and_bars_the_cursor(self):
        """No note at all on a gallery someone emailed you.

        There's no app on the machine it landed on and no run folder for it, so
        every sentence available is advice that can't be taken. The barred
        cursor says the one true thing — this doesn't work — without claiming
        there's a fix.
        """
        gallery = self.build(editable=False)

        def script(page):
            self.assertEqual(page.locator(".locknote").count(), 0)
            star = page.locator(".card").first.locator(".star-btn")
            self.assertEqual(
                star.evaluate("el => getComputedStyle(el).cursor"),
                "not-allowed")
            # And clicking still can't leave a mark that looks taken.
            self.press(star)
            page.wait_for_timeout(600)
            self.assertEqual(page.locator(".card.is-starred").count(), 0)
        self.open(script, html=gallery)

    def test_a_gallery_that_could_work_keeps_its_note(self):
        # The barred cursor is only for the case with no way out. This one has
        # one, and the note names it.
        gallery = self.build()

        def script(page):
            star = page.locator(".card").first.locator(".star-btn")
            self.assertNotEqual(
                star.evaluate("el => getComputedStyle(el).cursor"),
                "not-allowed")
            self.assertIn("Open Faceplace Marketbook",
                          page.locator(".locknote").first.inner_text())
        self.open(script, html=gallery)

    def test_a_forwarded_original_keeps_the_marks_it_arrived_with(self):
        """The gallery from the run folder, emailed by hand instead of the
        attachment the app builds.

        It carries a real run id, so it asks — and lands on someone else's app,
        which has no folder by that name and says so. The answer that matters is
        that the sender's marks are still on screen afterwards: the reply is
        only allowed to replace them when it's a reply about the right run.
        """
        marks.write(self.folder, {"starred": ["a"], "hidden": ["b"]})
        gallery = self.build()
        forwarded = self.folder.parent / "forwarded.html"
        forwarded.write_text(
            gallery.read_text(encoding="utf-8").replace(
                json.dumps(self.folder.name), json.dumps("someone-elses-run")),
            encoding="utf-8")
        self.serve()

        def script(page):
            self.assertTrue(self.is_off(page))
            self.assertEqual(page.locator(".card.is-starred").count(), 1)
            self.assertIn("1 hidden",
                          page.locator("#hiddenToggle").inner_text().lower())
        self.open(script, html=forwarded)

    # ------------------------------------------------ opened from Finder
    def test_a_gallery_opened_as_a_file_can_still_reach_the_app(self):
        # The one that isn't ours to decide: whether a browser lets a page it
        # loaded off the disk talk to a server on this same computer. If it
        # ever stops allowing it, this fails and the honest answer is that
        # double-clicked galleries become read-only — which the page already
        # handles, and which is why the buttons check rather than assume.
        self.serve()
        gallery = self.build()

        def script(page):
            self.assertFalse(
                self.is_off(page),
                "a file:// gallery could not reach the local server, so "
                "double-clicking one from Finder now opens it read-only")
            self.star(page, "1965 Land Cruiser")
            page.wait_for_timeout(900)
        self.open(script, html=gallery)
        self.assertEqual(self.marks_on_disk()["starred"], ["a"])

    def test_every_browser_engine_on_this_machine_allows_it(self):
        """The same question as the test above, asked of Firefox and Safari's
        engine as well as Chrome's.

        Whether a page loaded off the disk may talk to a server on the same
        computer is a policy each browser decides for itself, and it is the
        single assumption this whole feature rests on. Engines that aren't
        installed are skipped rather than failed — `playwright install firefox
        webkit` adds them.
        """
        self.serve()
        gallery = self.build()
        checked, blocked = [], []
        with sync_playwright() as p:
            for name in ("chromium", "firefox", "webkit"):
                try:
                    browser = getattr(p, name).launch()
                except Exception:
                    continue
                try:
                    page = browser.new_page()
                    page.goto(gallery.as_uri())
                    page.wait_for_function("() => document.body.dataset.marks",
                                           timeout=20000)
                    state = page.evaluate("() => document.body.dataset.marks")
                    checked.append(name)
                    if state != "live":
                        blocked.append(name)
                finally:
                    browser.close()
        self.assertTrue(checked, "no browser engines installed")
        self.assertEqual(blocked, [], f"{blocked} would not let a file:// "
                                      f"gallery reach the local server")


if __name__ == "__main__":
    unittest.main()
