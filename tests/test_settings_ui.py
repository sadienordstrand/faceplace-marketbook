#!/usr/bin/env python3
"""
Drives the settings window headlessly.

    python3 -m unittest tests.test_settings_ui

This opens the real page in real Chromium with the real hooks, so it catches the
things a Python-only test can't: a typo in the JavaScript, a button wired to an
element that doesn't exist, an exposed function the page never gets an answer
from. It never touches Facebook — the browser only ever loads this one local
page — but it does need Playwright's Chromium, so it's kept out of the offline
suite's file.
"""
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import listings
import scheduling as sc
import settings_ui

try:
    from playwright.sync_api import sync_playwright  # noqa: F401
    HAVE_PLAYWRIGHT = True
except ImportError:
    HAVE_PLAYWRIGHT = False

CITIES = ["Medford, OR", "Sacramento, CA", "Denver, CO"]
LONG_CITY = "A name someone typed by hand that is absurdly long"
LONG_ID = "loc-108173265878171108173265878171"
PACES = {"fast": (1.0, 2.5), "slow": (3.0, 5.0)}
OFFER = {"ask": True, "why": "Want something quicker to reach?",
         "places": [{"id": "desktop", "label": "Desktop", "on": True},
                    {"id": "dock", "label": "Dock", "on": False}],
         "note": "The Dock will blink as it restarts."}
# Scheduled searches report by email, so the window won't make one without this.
ACCOUNT = {"provider": "gmail", "address": "me@gmail.com",
           "app_password": "abcd efgh ijkl mnop"}
RUNS = [{"id": "defender_110_08-09-2026", "name": "defender 110",
         "scheduled": False, "when": "2026-08-09T23:14:26",
         "when_text": "today at 11:14 pm", "listings": 121, "new_listings": None,
         "cities": 2, "duration_text": "2m 8s", "earlier_runs": 0},
        {"id": "saved/nightly", "name": "nightly", "scheduled": True,
         "when": "2026-08-08T05:00:00", "when_text": "yesterday at 5:00 am",
         "listings": 40, "new_listings": 3, "cities": 1,
         "duration_text": "9m 2s", "earlier_runs": 4}]


@unittest.skipUnless(HAVE_PLAYWRIGHT, "needs Playwright")
class UITest(unittest.TestCase):
    """Each test supplies a script that drives the page; the window closes when
    the script returns."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self._saved = {k: getattr(sc, k) for k in
                       ("SEARCHES_PATH", "EMAIL_CONFIG_PATH", "SCHEDULE_DIR",
                        "LOCK_PATH", "TICK_LOG")}
        sc.SEARCHES_PATH = root / "saved_searches.json"
        sc.EMAIL_CONFIG_PATH = root / "email_config.json"
        sc.SCHEDULE_DIR = root / ".schedule"
        sc.LOCK_PATH = sc.SCHEDULE_DIR / "run.lock"
        sc.TICK_LOG = sc.SCHEDULE_DIR / "tick.log"
        # Never let a test reach launchd or Task Scheduler.
        self._installed = sc.schedule_installed
        sc.schedule_installed = lambda: False
        self._install = sc.install_schedule
        sc.install_schedule = lambda **kw: (True, ["pretend installed"])
        self._wake = sc.rearm_wake
        sc.rearm_wake = lambda *a, **k: False
        # A link in the window opens the everyday browser, which no test may
        # actually do; what would have opened is recorded instead.
        self.opened = []
        self._open_link = settings_ui.open_link
        settings_ui.open_link = lambda url: self.opened.append(url)
        self.addCleanup(self._restore)
        self.errors = []

    def _restore(self):
        sc.schedule_installed = self._installed
        sc.install_schedule = self._install
        sc.rearm_wake = self._wake
        settings_ui.open_link = self._open_link
        for k, v in self._saved.items():
            setattr(sc, k, v)

    def drive(self, script, defaults=None, cities=None, builtins=(),
              extra_hooks=None, email=True):
        """Opens the window, runs `script(page)`, returns what was submitted.

        `email` writes a working email account before the window opens, because
        a scheduled search can't be made without one — the tests that are about
        that refusal are the ones that turn it off."""
        result = {}
        cities = list(cities or CITIES)
        if email:
            sc.save_email_config(ACCOUNT)

        def ready(page):
            page.on("console", lambda m: m.type == "error"
                    and self.errors.append(m.text))
            page.on("pageerror", lambda e: self.errors.append(str(e)))
            try:
                script(page)
            finally:
                if not page.is_closed():
                    page.evaluate("window.pyCancel()")

        def remove(label):
            if label in builtins:
                return cities, f"'{label}' can't be removed."
            cities.remove(label)
            return cities, None

        result["data"] = settings_ui.collect_settings(
            cities, PACES,
            defaults or {"query": "", "exclude": "", "pace": "fast",
                         "page_work": 3.5, "photo_save": 1.5},
            headless=True, hooks={**sc.ui_hooks(), **(extra_hooks or {})},
            on_add=lambda label, text: (cities, None),
            on_remove=remove, builtins=builtins,
            on_ready=ready)
        self.assertEqual(self.errors, [], f"JavaScript errors: {self.errors}")
        return result["data"]

    def saved(self):
        return json.loads(sc.SEARCHES_PATH.read_text(encoding="utf-8"))["searches"]

    # ---------------------------------------------------------------- tests
    def test_the_window_opens_on_the_search_tab(self):
        def script(page):
            self.assertEqual(page.get_attribute("#tabNew", "aria-selected"), "true")
            self.assertFalse(page.is_hidden("#paneNew"))
            self.assertTrue(page.is_hidden("#paneSaved"))
            self.assertTrue(page.is_hidden("#paneEmail"))
        self.drive(script)

    def test_the_query_box_has_the_caret_on_arrival(self):
        # Which is only honest in the app window this opens in a real launch:
        # a browser window's address bar takes the keyboard on startup, and
        # then the caret blinks here while what you type goes there. Headless
        # Chromium has no window furniture either way, so the app flag itself
        # can't be checked from in here — see collect_settings.
        def script(page):
            self.assertEqual(page.evaluate("() => document.activeElement.id"),
                             "query")
        self.drive(script)

    def test_the_page_tells_the_browser_that_it_is_dark(self):
        # Otherwise the scrollbar, the selection highlight and a number box's
        # spinners are all drawn for a white page, which on this background
        # reads as a bright strip down the side of the window.
        def script(page):
            self.assertEqual(
                page.evaluate("() => getComputedStyle(document.documentElement)"
                              ".colorScheme"), "dark")
        self.drive(script)

    def test_the_tabs_switch_panes(self):
        def script(page):
            for tab, pane in (("#tabSaved", "#paneSaved"),
                              ("#tabEmail", "#paneEmail"),
                              ("#tabNew", "#paneNew")):
                page.click(tab)
                self.assertFalse(page.is_hidden(pane), pane)
                self.assertEqual(page.get_attribute(tab, "aria-selected"), "true")
        self.drive(script)

    def test_start_sweep_is_hidden_on_the_other_tabs(self):
        def script(page):
            page.click("#tabSaved")
            self.assertTrue(page.is_hidden("#start"))
            self.assertEqual(page.text_content("#cancel"), "Close")
            page.click("#tabNew")
            self.assertFalse(page.is_hidden("#start"))
            self.assertEqual(page.text_content("#cancel"), "Cancel")
        self.drive(script)

    def test_the_empty_saved_tab_says_where_to_start(self):
        def script(page):
            page.click("#tabSaved")
            self.assertIn("No scheduled searches yet", page.text_content("#savedList"))
        self.drive(script)

    def fill_and_save(self, page, name="Defender 110", every="1", unit="days"):
        page.fill("#query", "defender 110")
        page.fill("#save_name", name)
        page.fill("#save_every", every)
        page.select_option("#save_unit", unit)
        # Leave one city selected.
        page.click("#noCities")
        page.click(".cities .tog[data-city='Medford, OR']")
        page.click("#saveSearch")
        page.wait_for_selector("#saveMsg:not([hidden])")
        return page.text_content("#saveMsg")

    def test_saving_a_search_writes_it_to_disk(self):
        def script(page):
            msg = self.fill_and_save(page)
            self.assertIn("Saved", msg)
            self.assertIn("Defender 110", msg)
        self.drive(script)
        rows = self.saved()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Defender 110")
        self.assertEqual(rows[0]["queries"], ["defender 110"])
        self.assertEqual(rows[0]["query"], "defender 110")
        self.assertEqual(rows[0]["cities"], ["Medford, OR"])
        self.assertEqual(rows[0]["interval"], {"every": 1, "unit": "days"})

    def test_the_year_filter_survives_a_save_and_an_edit(self):
        def script(page):
            page.fill("#min_year", "1970")
            page.fill("#max_year", "1995")
            page.click("#include_no_year")
            self.fill_and_save(page)
            # Reopening it has to restore what was saved, not the defaults —
            # otherwise an edit of the name quietly re-widens the year range.
            page.fill("#min_year", "")
            page.fill("#max_year", "")
            page.click("#tabSaved")
            page.click(".card button[data-act=edit]")
            page.wait_for_selector("#cancelEdit:not([hidden])")
            self.assertEqual(page.input_value("#min_year"), "1970")
            self.assertEqual(page.input_value("#max_year"), "1995")
            self.assertEqual(
                page.get_attribute("#include_no_year", "aria-pressed"), "false")
        self.drive(script)
        row = self.saved()[0]
        self.assertEqual(row["min_year"], 1970)
        self.assertEqual(row["max_year"], 1995)
        self.assertIs(row["include_no_year"], False)

    def test_saving_without_a_name_is_refused_in_the_window(self):
        def script(page):
            page.fill("#query", "defender 110")
            page.click("#saveSearch")
            page.wait_for_selector("#saveMsg:not([hidden])")
            self.assertIn("name", page.text_content("#saveMsg"))
            self.assertIn("bad", page.get_attribute("#saveMsg", "class"))
        self.drive(script)
        self.assertFalse(sc.SEARCHES_PATH.exists())

    def unit_labels(self, page):
        return page.eval_on_selector_all(
            "#save_unit option", "els => els.map(e => e.textContent)")

    def test_the_interval_will_not_hold_a_number_it_cannot_run_on(self):
        # None of these reach the scheduler, which wants a whole count of one
        # or more; the box would hand over every one of them, so they're put
        # right where they're typed rather than refused at save time.
        def script(page):
            for typed, expect in (("0", "1"), ("-4", "1"), ("2.5", "2"),
                                  ("12", "12")):
                page.fill("#save_every", typed)
                self.assertEqual(page.input_value("#save_every"), expect, typed)
            # Emptying it to retype is left alone until focus goes elsewhere.
            page.fill("#save_every", "")
            self.assertEqual(page.input_value("#save_every"), "")
            page.click("#save_name")
            self.assertEqual(page.input_value("#save_every"), "1")
        self.drive(script)

    def test_the_units_are_singular_for_one_and_plural_for_the_rest(self):
        def script(page):
            self.assertEqual(self.unit_labels(page), ["hour", "day"])
            page.fill("#save_every", "3")
            page.click("#save_name")
            self.assertEqual(self.unit_labels(page), ["hours", "days"])
            page.fill("#save_every", "1")
            page.click("#save_name")
            self.assertEqual(self.unit_labels(page), ["hour", "day"])
        self.drive(script)

    def test_the_units_are_left_alone_until_the_number_is_finished(self):
        # Typing "10" passes through "1" on the way, and a unit that flicks to
        # the singular and back between two keystrokes reads as a glitch. The
        # label waits for the number to be settled — which is what leaving the
        # box means.
        def script(page):
            page.fill("#save_every", "3")
            page.click("#save_name")
            for halfway in ("1", "10"):
                page.fill("#save_every", halfway)
                self.assertEqual(self.unit_labels(page), ["hours", "days"],
                                 halfway)
            page.click("#save_name")
            self.assertEqual(self.unit_labels(page), ["hours", "days"])
        self.drive(script)

    def test_a_dropdown_shouts_its_choice_and_writes_its_list_plainly(self):
        # The chosen one sits among controls that are all capitals, so it
        # matches them; the list that drops down is prose, and reads better as
        # prose. The text itself stays as written, and the capitals are put on
        # by the box, which is the only part of a menu CSS can be sure of.
        def script(page):
            page.click("#tabEmail")
            case = page.eval_on_selector(
                "#mail_provider",
                "el => [getComputedStyle(el).textTransform,"
                " getComputedStyle(el.options[0]).textTransform]")
            self.assertEqual(case, ["uppercase", "none"])
            self.assertEqual(
                page.eval_on_selector("#mail_provider option", "el => el.textContent"),
                "Gmail")
        self.drive(script)

    def test_editing_a_search_relabels_the_units_for_its_interval(self):
        def script(page):
            self.fill_and_save(page, every="6", unit="hours")
            page.fill("#save_every", "1")
            page.click("#tabSaved")
            page.click(".card button[data-act=edit]")
            page.wait_for_selector("#cancelEdit:not([hidden])")
            self.assertEqual(page.input_value("#save_every"), "6")
            self.assertEqual(self.unit_labels(page), ["hours", "days"])
        self.drive(script)

    def test_the_saved_interval_is_still_the_plural_the_scheduler_stores(self):
        # Only the labels change with the number. A unit of "day" would fail
        # validation, and a search saved with one would never run.
        def script(page):
            self.fill_and_save(page, every="1", unit="days")
        self.drive(script)
        self.assertEqual(self.saved()[0]["interval"],
                         {"every": 1, "unit": "days"})

    def test_a_short_interval_warns_as_soon_as_it_is_picked(self):
        def script(page):
            page.fill("#save_every", "1")
            page.select_option("#save_unit", "hours")
            page.wait_for_selector("#saveWarn:not([hidden])")
            self.assertIn("limited or banned", page.text_content("#saveWarn"))
        self.drive(script)

    def test_a_duplicate_name_is_refused(self):
        def script(page):
            self.fill_and_save(page)
            page.fill("#save_name", "defender 110")
            page.click("#saveSearch")
            page.wait_for_function(
                "() => document.getElementById('saveMsg')"
                ".textContent.includes('already have')")
        self.drive(script)
        self.assertEqual(len(self.saved()), 1)

    def test_a_saved_search_shows_up_as_a_card(self):
        def script(page):
            self.fill_and_save(page)
            page.click("#tabSaved")
            card = page.text_content(".card")
            self.assertIn("Defender 110", card)
            self.assertIn("every day", card)
            self.assertIn("1 city", card)
            for act in ("run", "edit", "toggle", "del"):
                self.assertEqual(page.locator(f".card button[data-act={act}]")
                                 .count(), 1)
        self.drive(script)

    def test_pausing_from_the_card(self):
        def script(page):
            self.fill_and_save(page)
            page.click("#tabSaved")
            page.click(".card button[data-act=toggle]")
            page.wait_for_selector(".card.off")
            self.assertIn("paused", page.text_content(".card .pill"))
            self.assertIn("now paused", page.text_content("#savedMsg"))
            page.click(".card button[data-act=toggle]")
            page.wait_for_selector(".card:not(.off)")
        self.drive(script)
        self.assertTrue(self.saved()[0]["enabled"])

    def test_deleting_needs_two_clicks(self):
        def script(page):
            self.fill_and_save(page)
            page.click("#tabSaved")
            page.click(".card button[data-act=del]")
            self.assertIn("Really delete", page.text_content(".card button[data-act=del]"))
            self.assertEqual(page.locator(".card").count(), 1)
        self.drive(script)
        self.assertEqual(len(self.saved()), 1)

    def test_the_second_delete_click_removes_it(self):
        def script(page):
            self.fill_and_save(page)
            page.click("#tabSaved")
            page.click(".card button[data-act=del]")
            page.click(".card button[data-act=del]")
            page.wait_for_selector(".empty")
        self.drive(script)
        self.assertEqual(self.saved(), [])

    def test_editing_loads_the_search_back_into_the_form(self):
        def script(page):
            self.fill_and_save(page, name="Rover", every="2", unit="hours")
            page.click("#tabSaved")
            page.click(".card button[data-act=edit]")
            # It should jump back to the search tab with everything filled in.
            page.wait_for_selector("#paneNew:not([hidden])")
            self.assertEqual(page.input_value("#query"), "defender 110")
            self.assertEqual(page.input_value("#save_name"), "Rover")
            self.assertEqual(page.input_value("#save_every"), "2")
            self.assertEqual(page.input_value("#save_unit"), "hours")
            self.assertEqual(page.text_content("#saveSearch"), "Update scheduled search")
            self.assertFalse(page.is_hidden("#cancelEdit"))
            selected = page.eval_on_selector_all(
                ".cities .tog[aria-pressed=true]", "els => els.map(e => e.dataset.city)")
            self.assertEqual(selected, ["Medford, OR"])
            # Editing must update in place, not create a second search.
            page.fill("#save_name", "Rover 2")
            page.click("#saveSearch")
            page.wait_for_function(
                "() => document.getElementById('saveMsg')"
                ".textContent.includes('Updated')")
        self.drive(script)
        rows = self.saved()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Rover 2")

    def test_stop_editing_goes_back_to_adding(self):
        def script(page):
            self.fill_and_save(page, name="Rover")
            page.click("#tabSaved")
            page.click(".card button[data-act=edit]")
            page.wait_for_selector("#cancelEdit:not([hidden])")
            page.click("#cancelEdit")
            self.assertEqual(page.text_content("#saveSearch"), "Save scheduled search")
            self.assertTrue(page.is_hidden("#cancelEdit"))
            page.fill("#save_name", "Second one")
            page.click("#saveSearch")
            page.wait_for_function(
                "() => document.getElementById('saveMsg')"
                ".textContent.includes('Saved')")
        self.drive(script)
        self.assertEqual(len(self.saved()), 2)

    def test_run_now_closes_the_window_and_names_the_search(self):
        ids = {}

        def script(page):
            self.fill_and_save(page)
            ids["id"] = self.saved()[0]["id"]
            page.click("#tabSaved")
            page.click(".card button[data-act=run]")
            page.wait_for_timeout(300)
        data = self.drive(script)
        self.assertEqual(data, {"action": "run_saved", "id": ids["id"]})

    def test_start_sweep_returns_the_form(self):
        def script(page):
            page.fill("#query", "defender 110")
            page.fill("#exclude", "rhd, can am")
            page.fill("#max_price", "40000")
            page.fill("#min_year", "1970")
            page.fill("#max_year", "1995")
            page.click("#noCities")
            page.click(".cities .tog[data-city='Denver, CO']")
            page.click("#start")
            page.wait_for_timeout(300)
        data = self.drive(script)
        self.assertEqual(data["action"], "sweep")
        self.assertEqual(data["queries"], ["defender 110"])
        self.assertEqual(data["cities"], ["Denver, CO"])
        self.assertEqual(data["max_price"], 40000)
        self.assertEqual(data["min_year"], 1970)
        self.assertEqual(data["max_year"], 1995)
        self.assertEqual(data["exclude"], "rhd, can am")

    def test_the_form_no_longer_offers_to_stop_and_ask_mid_run(self):
        # Ctrl-C in the terminal already ends the description stage and keeps
        # everything gathered so far, so the budget prompt was a second way to
        # do the same thing, sitting in the window costing everyone a decision.
        def script(page):
            page.fill("#query", "defender 110")
            self.assertIsNone(page.query_selector("#descriptions_budget"))
            page.click("#start")
            page.wait_for_timeout(300)
        self.assertNotIn("descriptions_budget", self.drive(script))

    def test_the_gallery_is_no_longer_something_you_can_skip(self):
        # A run whose results can only be read as a CSV is a run nobody wanted,
        # and building the gallery costs seconds at the end of an hour.
        def script(page):
            self.assertIsNone(page.query_selector("#do_gallery"))
            page.fill("#query", "defender 110")
            page.click("#start")
            page.wait_for_timeout(300)
        self.assertNotIn("do_gallery", self.drive(script))

    def test_the_stages_card_says_what_turning_them_off_is_for(self):
        def script(page):
            self.assertIn("Save raw payloads", page.text_content("#debug_dump"))
            # The two stages, then why you'd skip one, then the debugging
            # option, which is a different sort of thing entirely.
            above = page.eval_on_selector(
                "#debug_dump",
                "el => el.closest('.toggles').previousElementSibling.textContent")
            self.assertIn("To speed up your search", above)
        self.drive(script)

    def test_the_limit_is_asked_for_before_the_pace(self):
        def script(page):
            self.assertEqual(
                page.eval_on_selector_all("#descBlock .lab",
                                          "els => els.map(e => e.textContent)"),
                ["Limit (blank = all)", "Retrieval pace"])
        self.drive(script)

    def test_undated_listings_are_included_unless_asked_otherwise(self):
        def script(page):
            page.fill("#query", "defender 110")
            self.assertEqual(
                page.get_attribute("#include_no_year", "aria-pressed"), "true")
            page.click("#include_no_year")
            page.click("#start")
            page.wait_for_timeout(300)
        self.assertIs(self.drive(script)["include_no_year"], False)

    def fill_range(self, page, sel, value):
        """A price or year box, filled and then left, the way a person fills
        one. The form waits for the box to be left before it judges what's in
        it, and page.fill leaves the cursor sitting in there."""
        page.fill(sel, value)
        page.locator(sel).blur()

    def test_a_backwards_or_negative_range_blocks_the_sweep(self):
        """Every one of these has to reach the button, not just the message:
        an explanation nobody reads still lets an hour-long sweep start on a
        range that can't match anything."""
        cases = [
            (("#min_price", "900"), ("#max_price", "100"), "minimum price"),
            (("#min_price", "-5"), ("#max_price", ""), "negative"),
            (("#min_year", "1995"), ("#max_year", "1970"), "minimum year"),
            (("#min_year", "12"), ("#max_year", ""), "between 1900"),
        ]

        def script(page):
            page.fill("#query", "defender 110")
            for (lo_id, lo), (hi_id, hi), expected in cases:
                self.fill_range(page, lo_id, lo)
                self.fill_range(page, hi_id, hi)
                self.assertFalse(page.is_hidden("#filterMsg"), expected)
                self.assertIn(expected, page.text_content("#filterMsg").lower())
                self.assertTrue(page.is_disabled("#start"), expected)
                self.assertTrue(page.is_disabled("#saveSearch"), expected)
                self.assertIn("fix quality filters", page.text_content("#est"))
                self.fill_range(page, lo_id, "")
                self.fill_range(page, hi_id, "")
            # and clearing the last of them puts the button back
            self.assertTrue(page.is_hidden("#filterMsg"))
            self.assertFalse(page.is_disabled("#start"))
        self.drive(script)

    # ------------------------------------------------------ the query boxes
    def add_query(self, page, text):
        """Click the + and fill the box it makes."""
        before = page.locator(".qbox").count()
        page.click("#addQuery")
        page.wait_for_function(
            "n => document.querySelectorAll('.qbox').length > n", arg=before)
        page.locator(".qbox").last.fill(text)

    def save_as(self, page, name):
        """fill_and_save, then wait for this particular save to come back —
        the message box may still be showing the last one."""
        self.fill_and_save(page, name=name)
        page.wait_for_function(
            "n => document.getElementById('saveMsg').textContent.includes(n)",
            arg=name)

    def test_a_second_query_appears_with_or_between_the_boxes(self):
        def script(page):
            page.fill("#query", "defender 110")
            self.assertEqual(page.locator(".qbox").count(), 1)
            self.assertEqual(page.locator(".qor").count(), 0)
            self.add_query(page, "land rover 90")
            self.assertEqual(page.locator(".qbox").count(), 2)
            # One OR, and it sits between the two boxes rather than after them.
            self.assertEqual(page.locator(".qor").count(), 1)
            self.assertEqual(
                page.text_content(".qor").strip().lower(), "or")
            page.click("#noCities")
            page.click(".cities .tog[data-city='Denver, CO']")
            page.click("#start")
            page.wait_for_timeout(300)
        data = self.drive(script)
        self.assertEqual(data["queries"], ["defender 110", "land rover 90"])

    def test_removing_a_query_takes_its_or_with_it(self):
        def script(page):
            page.fill("#query", "defender 110")
            self.add_query(page, "land rover 90")
            page.click(".qx")
            page.wait_for_function(
                "() => document.querySelectorAll('.qbox').length === 1")
            self.assertEqual(page.locator(".qor").count(), 0)
            self.assertEqual(page.input_value("#query"), "defender 110")
            page.click("#start")
            page.wait_for_timeout(300)
        self.assertEqual(self.drive(script)["queries"], ["defender 110"])

    def test_the_first_query_has_no_remove_button(self):
        # There is nothing sensible for it to do: a search needs one query.
        def script(page):
            self.assertEqual(page.locator(".qx").count(), 0)
            self.add_query(page, "land rover 90")
            self.assertEqual(page.locator(".qx").count(), 1)
        self.drive(script)

    def test_an_empty_box_is_not_a_query(self):
        def script(page):
            page.fill("#query", "defender 110")
            page.click("#addQuery")
            page.click("#start")
            page.wait_for_timeout(300)
        self.assertEqual(self.drive(script)["queries"], ["defender 110"])

    def test_the_offer_of_another_query_stops_at_the_limit(self):
        def script(page):
            page.fill("#query", "q1")
            for i in range(2, listings.MAX_QUERIES + 1):
                self.add_query(page, f"q{i}")
            self.assertEqual(page.locator(".qbox").count(),
                             listings.MAX_QUERIES)
            self.assertTrue(page.is_disabled("#addQuery"))
            page.locator(".qx").last.click()
            page.wait_for_function(
                "() => !document.getElementById('addQuery').disabled")
        self.drive(script)

    def test_several_queries_survive_a_save_and_an_edit(self):
        def script(page):
            self.add_query(page, "land rover 90")
            self.save_as(page, "Either")
            # Reopening it has to bring both boxes back, not just the first.
            page.click("#tabSaved")
            self.assertIn("“defender 110” or “land rover 90”",
                          page.text_content(".card .det"))
            page.click(".card button[data-act=edit]")
            page.wait_for_selector("#cancelEdit:not([hidden])")
            self.assertEqual(
                page.eval_on_selector_all(".qbox", "els => els.map(e => e.value)"),
                ["defender 110", "land rover 90"])
        self.drive(script)
        self.assertEqual(self.saved()[0]["queries"],
                         ["defender 110", "land rover 90"])

    def test_editing_a_one_query_search_clears_a_box_left_by_another(self):
        def script(page):
            self.add_query(page, "land rover 90")
            self.save_as(page, "Either")
            page.locator(".qx").last.click()
            self.save_as(page, "Just the one")
            # Load the two-query search, then the one-query one: the second must
            # not inherit the box the first put on the form.
            page.click("#tabSaved")
            page.click(".card:first-of-type button[data-act=edit]")
            page.wait_for_function(
                "() => document.querySelectorAll('.qbox').length === 2")
            page.click("#cancelEdit")
            page.click("#tabSaved")
            page.click(".card:last-of-type button[data-act=edit]")
            page.wait_for_function(
                "() => document.querySelectorAll('.qbox').length === 1")
            self.assertEqual(page.input_value("#query"), "defender 110")
        self.drive(script)

    def test_the_footer_quotes_every_query(self):
        def script(page):
            page.fill("#query", "defender 110")
            self.assertIn("“defender 110”", page.text_content("#est"))
            self.add_query(page, "land rover 90")
            self.assertIn("“defender 110” or “land rover 90”",
                          page.text_content("#est"))
        self.drive(script)

    def test_the_footer_describes_the_whole_search(self):
        # Four cards' worth of settings, several screens tall by the time
        # they're filled in; the footer is where they can be read at once.
        def script(page):
            page.fill("#query", "defender 110")
            page.click("#exact")
            page.fill("#min_price", "5000")
            page.fill("#max_price", "40000")
            page.fill("#min_year", "1970")
            page.fill("#max_year", "1995")
            page.click("#include_no_year")
            page.fill("#exclude", "rhd, can am, hot wheels")
            page.click("#do_thumbs")
            page.click("#debug_dump")
            page.fill("#limit", "100")
            est = page.text_content("#est")
            for bit in ("“defender 110”", "exact matching", "3 cities",
                        "$5,000–$40,000", "1970–1995", "no undated listings",
                        "3 excluded terms", "no thumbnails",
                        "save raw payloads", "100 descriptions max",
                        "fast retrieval"):
                self.assertIn(bit, est)
        self.drive(script)

    def test_the_footer_leaves_out_what_was_never_set(self):
        # Everything optional is silent when it's at its default, or the bar
        # would be a wall of text saying nothing in particular.
        def script(page):
            page.fill("#query", "defender 110")
            est = page.text_content("#est")
            self.assertIn("3 cities", est)
            self.assertIn("fast retrieval", est)
            for bit in ("exact matching", "undated", "excluded", "$", "–",
                        "raw payloads", "no descriptions", "no thumbnails",
                        "listings", "max"):
                self.assertNotIn(bit, est)
        self.drive(script)

    def test_the_footer_names_a_half_open_range(self):
        def script(page):
            page.fill("#query", "defender 110")
            page.fill("#min_price", "5000")
            self.assertIn("$5,000 and up", page.text_content("#est"))
            page.fill("#min_price", "")
            page.fill("#max_price", "20000")
            self.assertIn("up to $20,000", page.text_content("#est"))
            page.fill("#max_price", "")
            # A one-sided year is spoken about as a year, not as an amount.
            page.fill("#max_year", "1995")
            self.assertIn("1995 and earlier", page.text_content("#est"))
            page.fill("#max_year", "")
            page.fill("#min_year", "2000")
            self.assertIn("2000 and later", page.text_content("#est"))
        self.drive(script)

    def test_a_half_typed_year_is_left_alone_until_the_box_is(self):
        """1995 is 1, then 19, then 199 on the way in, and every one of those
        is outside the years the reader works in. The complaint waits."""
        def script(page):
            page.fill("#query", "defender 110")
            for part in ("1", "19", "199"):
                page.fill("#min_year", part)
                self.assertTrue(page.is_hidden("#filterMsg"), part)
                self.assertNotIn("fix quality filters", page.text_content("#est"))
                self.assertFalse(page.is_disabled("#start"), part)
            page.fill("#min_year", "1995")
            page.locator("#min_year").blur()
            self.assertTrue(page.is_hidden("#filterMsg"))
            # A number that's finished and still wrong is a different matter.
            self.fill_range(page, "#min_year", "12")
            self.assertIn("between 1900", page.text_content("#filterMsg"))
            self.assertTrue(page.is_disabled("#start"))
            # And picking the box back up to fix it puts the complaint away.
            page.fill("#min_year", "1")
            self.assertTrue(page.is_hidden("#filterMsg"))
        self.drive(script)

    def test_the_footer_folds_both_skipped_stages_into_one_phrase(self):
        def script(page):
            page.fill("#query", "defender 110")
            page.click("#do_descriptions")
            self.assertIn("no descriptions", page.text_content("#est"))
            page.click("#do_thumbs")
            est = page.text_content("#est")
            self.assertIn("no descriptions or thumbnails", est)
            # And with descriptions off there's no pace or limit to report.
            self.assertNotIn("retrieval", est)
            self.assertNotIn("listings", est)
        self.drive(script)

    def test_a_query_from_the_command_line_fills_the_boxes(self):
        def script(page):
            self.assertEqual(
                page.eval_on_selector_all(".qbox", "els => els.map(e => e.value)"),
                ["defender 110", "land rover 90"])
        self.drive(script, defaults={"queries": ["defender 110", "land rover 90"],
                                     "exclude": "", "pace": "fast",
                                     "page_work": 3.5, "photo_save": 1.5})

    def test_start_is_disabled_without_a_query_or_a_city(self):
        def script(page):
            self.assertTrue(page.is_disabled("#start"))
            page.fill("#query", "x")
            self.assertFalse(page.is_disabled("#start"))
            page.click("#noCities")
            self.assertTrue(page.is_disabled("#start"))
        self.drive(script)

    def test_every_reason_the_button_is_dead_is_written_beside_it(self):
        # A greyed-out button with nothing next to it reads as broken. The
        # setting that did it is usually scrolled off the top by now.
        def script(page):
            page.click("#noCities")
            est = page.text_content("#est")
            self.assertIn("query required", est)
            self.assertIn("select at least one city", est)
            page.fill("#query", "defender 110")
            self.assertNotIn("query required", page.text_content("#est"))
            self.assertTrue(page.is_disabled("#start"))
            page.click(".cities .tog[data-city='Denver, CO']")
            self.assertNotIn("select at least one city",
                             page.text_content("#est"))
            self.assertFalse(page.is_disabled("#start"))
        self.drive(script)

    def test_the_email_tab_saves_settings(self):
        def script(page):
            page.click("#tabEmail")
            page.fill("#mail_address", "me@gmail.com")
            page.fill("#mail_password", "abcd efgh ijkl mnop")
            page.click("#saveMail")
            page.wait_for_selector("#mailMsg:not([hidden])")
            self.assertIn("me@gmail.com", page.text_content("#mailMsg"))
        self.drive(script, email=False)
        cfg = json.loads(sc.EMAIL_CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(cfg["address"], "me@gmail.com")
        self.assertEqual(cfg["app_password"], "abcd efgh ijkl mnop")
        # Where reports go is a property of each scheduled search, and asking
        # again here only invited the two answers to disagree.
        self.assertNotIn("default_to", cfg)

    def test_the_custom_server_boxes_only_appear_for_other(self):
        def script(page):
            page.click("#tabEmail")
            self.assertTrue(page.is_hidden("#mailHostRow"))
            page.select_option("#mail_provider", "other")
            self.assertFalse(page.is_hidden("#mailHostRow"))
            page.select_option("#mail_provider", "gmail")
            self.assertTrue(page.is_hidden("#mailHostRow"))
        self.drive(script)

    def test_an_existing_config_is_shown_when_the_window_opens(self):
        sc.save_email_config({"address": "prior@gmail.com", "app_password": "pw",
                              "provider": "icloud"})

        def script(page):
            page.click("#tabEmail")
            self.assertEqual(page.input_value("#mail_address"), "prior@gmail.com")
            self.assertEqual(page.input_value("#mail_password"), "pw")
            self.assertEqual(page.input_value("#mail_provider"), "icloud")
        self.drive(script, email=False)

    def test_a_test_send_without_a_password_explains_itself(self):
        def script(page):
            page.click("#tabEmail")
            page.click("#testMail")
            page.wait_for_selector("#mailMsg:not([hidden])")
            self.assertIn("app password", page.text_content("#mailMsg"))
        self.drive(script, email=False)

    def test_the_schedule_tab_reports_that_runs_are_off(self):
        def script(page):
            page.click("#tabEmail")
            page.wait_for_function(
                "() => !document.getElementById('schedState')"
                ".textContent.includes('Checking')")
            self.assertIn("off", page.text_content("#schedState"))
            self.assertFalse(page.is_hidden("#schedOn"))
            self.assertTrue(page.is_hidden("#schedOff"))
        self.drive(script)

    def test_turning_automatic_runs_on_reports_back(self):
        def script(page):
            page.click("#tabEmail")
            page.click("#schedOn")
            page.wait_for_selector("#schedMsg:not([hidden])")
            self.assertIn("pretend installed", page.text_content("#schedMsg"))
        self.drive(script)

    def test_a_refused_install_shows_the_instructions_in_full(self):
        # macOS installs an agent it then denies every file to. The several
        # paragraphs that explain the fix must not be squeezed into the one-line
        # status, and the path the user has to paste has to survive intact.
        sc.install_schedule = lambda **kw: (False, sc.permission_help())

        def script(page):
            page.click("#tabEmail")
            page.click("#schedOn")
            page.wait_for_selector("#schedProblem:not([hidden])")
            text = page.text_content("#schedProblem")
            self.assertIn("Full Disk Access", text)
            self.assertIn(sc.python_exe(), text)
            self.assertTrue(page.is_hidden("#schedMsg"))
            self.assertEqual(
                page.text_content("#schedProblem code").strip(), sc.python_exe())
        self.drive(script)

    def test_an_installed_but_silent_scheduler_is_flagged_on_arrival(self):
        sc.schedule_installed = lambda: True
        self.addCleanup(setattr, sc, "schedule_problems", sc.schedule_problems)
        sc.schedule_problems = lambda: ["Nothing has checked in."]

        def script(page):
            page.click("#tabEmail")
            page.wait_for_selector("#schedProblem:not([hidden])")
            self.assertIn("Nothing has checked in.",
                          page.text_content("#schedProblem"))
            # Saying "on" next to instructions for fixing it would be a lie.
            self.assertIn("blocked", page.text_content("#schedState"))
            self.assertIn("stuck", page.get_attribute("#schedDot", "class"))
        self.drive(script)

    def test_no_problems_means_no_problem_box(self):
        def script(page):
            page.click("#tabEmail")
            page.wait_for_function(
                "() => !document.getElementById('schedState')"
                ".textContent.includes('Checking')")
            self.assertTrue(page.is_hidden("#schedProblem"))
        self.drive(script)

    def test_a_built_in_city_has_no_remove_button(self):
        def script(page):
            builtin = page.query_selector(
                '.tog[data-city="Medford, OR"] .tog-x')
            self.assertIsNone(builtin)
            self.assertIsNotNone(page.query_selector(
                '.tog[data-city="Denver, CO"] .tog-x'))
            self.assertIn("remove", page.text_content("#addCityMsg"))
        self.drive(script, cities=["Medford, OR", "Denver, CO"],
                   builtins=["Medford, OR"])

    def test_a_city_the_user_added_goes_on_the_first_click(self):
        def script(page):
            page.click('.tog[data-city="Denver, CO"] .tog-x')
            page.wait_for_selector('.tog[data-city="Denver, CO"]', state="detached")
            self.assertIn("Removed Denver, CO", page.text_content("#cityMsg"))
            # The same green note that saving a search gets, rather than a line
            # of grey that reads as more of the instructions above it.
            self.assertIn("ok", page.get_attribute("#cityMsg", "class"))
        self.drive(script, cities=["Medford, OR", "Denver, CO"],
                   builtins=["Medford, OR"])

    def test_a_city_that_was_added_is_announced_the_same_way(self):
        def script(page):
            self.assertTrue(page.is_hidden("#cityMsg"))
            page.fill("#new_city_label", "Boise, ID")
            page.fill("#new_city_url", "https://www.facebook.com/marketplace/boise")
            page.click("#addCity")
            page.wait_for_selector("#cityMsg:not([hidden])")
            self.assertIn("Added Boise, ID", page.text_content("#cityMsg"))
            self.assertIn("ok", page.get_attribute("#cityMsg", "class"))
            # And the boxes are empty again, ready for the next one.
            self.assertEqual(page.input_value("#new_city_label"), "")
        self.drive(script)

    def test_a_long_city_name_cannot_stretch_its_tile(self):
        # Two ways a long name used to deform the grid, so both are pinned here
        # against two rows, where uneven tiles have something to be uneven with:
        # an id with nothing to wrap on widened its own column, and a name with
        # spaces wrapped onto a second line and made its row taller.
        def script(page):
            page.wait_for_selector(".cities .tog")
            boxes = page.eval_on_selector_all(
                ".cities .tog", "els => els.map(e => e.getBoundingClientRect())")
            widths = {round(b["width"]) for b in boxes}
            heights = {round(b["height"]) for b in boxes}
            self.assertEqual(len(widths), 1, f"tiles came out uneven: {widths}")
            self.assertEqual(len(heights), 1, f"tiles wrapped: {heights}")
            # Nothing is lost to the cut: the whole name is there to hover over.
            clipped = page.eval_on_selector(
                '.tog[data-city="%s"] .lbl' % LONG_CITY,
                "el => el.scrollWidth > el.clientWidth && el.title")
            self.assertEqual(clipped, LONG_CITY)
        self.drive(script, cities=["Medford, OR", LONG_ID, "Denver, CO",
                                   "Boise, ID", LONG_CITY, "Dallas, TX"])

    def test_a_refused_removal_keeps_the_city_and_says_why(self):
        # The button is hidden for built-ins, so this can only be reached by a
        # stale page; it still must not drop the city from the list.
        def script(page):
            page.eval_on_selector(
                '.tog[data-city="Medford, OR"]',
                "el => el.insertAdjacentHTML('beforeend',"
                " '<button class=\"tog-x\">x</button>')")
            page.click('.tog[data-city="Medford, OR"] .tog-x')
            page.wait_for_function(
                "() => document.getElementById('cityMsg')"
                ".textContent.includes(\"can't be removed\")")
            self.assertIsNotNone(page.query_selector('.tog[data-city="Medford, OR"]'))
        self.drive(script, cities=["Medford, OR", "Denver, CO"],
                   builtins=["Medford, OR"])

    def test_the_help_links_open_in_the_everyday_browser(self):
        # This window is Playwright's: it has no address bar, no Facebook
        # login, and it closes the moment a search starts, so following a link
        # inside it would strand the person who clicked.
        def script(page):
            for pane, summary in (("#paneNew", "How to get a city's link"),
                                  ("#paneEmail", "How to get a Gmail app password")):
                if pane == "#paneEmail":
                    page.click("#tabEmail")
                page.click(f"{pane} details.help summary")
                page.click(f"{pane} details.help a")
            page.wait_for_timeout(150)
            self.assertEqual(page.url, "about:blank")
        self.drive(script)
        self.assertEqual(self.opened, ["https://www.facebook.com/marketplace",
                                       "https://myaccount.google.com/"])

    def test_a_broken_hook_shows_a_message_instead_of_hanging(self):
        # An exposed function that raises must still answer the page, or the
        # button it belongs to spins forever.
        def script(page):
            page.click("#tabSaved")
            page.click("#refreshSaved")
            page.wait_for_selector("#savedMsg:not([hidden])")
            self.assertIn("boom", page.text_content("#savedMsg"))

        real = sc.searches_for_ui
        sc.searches_for_ui = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            self.drive(script)
        finally:
            sc.searches_for_ui = real

    # ------------------------------------------ email as a step, not a surprise
    def test_a_search_cannot_be_saved_before_email_is_set_up(self):
        # The old window let one be saved and then said nothing more about it,
        # so a search that could never send anything looked exactly like a
        # working one.
        def script(page):
            page.fill("#query", "defender 110")
            self.assertFalse(page.is_hidden("#saveNeedsEmail"))
            self.assertTrue(page.is_disabled("#saveSearch"))
            for box in ("#save_name", "#save_email", "#save_every", "#save_unit"):
                self.assertTrue(page.is_disabled(box), box)
            # A sweep you sit and watch needs no email, and is left alone.
            self.assertFalse(page.is_disabled("#start"))
        self.drive(script, email=False)
        self.assertFalse(sc.SEARCHES_PATH.exists())

    def test_the_refusal_hands_you_over_to_the_email_tab(self):
        def script(page):
            page.click("#goEmail")
            page.wait_for_selector("#paneEmail:not([hidden])")
            self.assertEqual(page.get_attribute("#tabEmail", "aria-selected"),
                             "true")
            self.assertEqual(page.evaluate("document.activeElement.id"),
                             "mail_address")
        self.drive(script, email=False)

    def test_setting_email_up_unlocks_saving_there_and_then(self):
        def script(page):
            page.click("#goEmail")
            page.fill("#mail_address", "me@gmail.com")
            page.fill("#mail_password", "abcd efgh ijkl mnop")
            page.click("#saveMail")
            page.wait_for_selector("#mailMsg:not([hidden])")
            self.assertIn("Email is set up", page.text_content("#mailState"))
            self.assertIn("on", page.get_attribute("#mailDot", "class"))
            # And the way back to the search they were in the middle of saving.
            page.click("#backToSave")
            page.wait_for_selector("#paneNew:not([hidden])")
            self.assertTrue(page.is_hidden("#saveNeedsEmail"))
            self.assertFalse(page.is_disabled("#save_name"))
            self.assertIn("Saved", self.fill_and_save(page))
        self.drive(script, email=False)
        self.assertEqual(len(self.saved()), 1)

    def test_the_report_address_is_filled_in_rather_than_explained(self):
        # It used to be a blank box under a note about which address a blank
        # would mean. The address itself says that, and can be typed over.
        def script(page):
            self.assertEqual(page.input_value("#save_email"), "me@gmail.com")
            self.assertIsNone(page.query_selector("#saveEmailHint"))
            self.fill_and_save(page)
        self.drive(script)
        self.assertEqual(self.saved()[0]["email_to"], "me@gmail.com")

    def test_an_address_you_typed_yourself_is_never_written_over(self):
        def script(page):
            page.fill("#save_email", "someone@else.com")
            # Saving the email settings again re-runs the fill.
            page.click("#tabEmail")
            page.click("#saveMail")
            page.wait_for_selector("#mailMsg:not([hidden])")
            page.click("#tabNew")
            self.assertEqual(page.input_value("#save_email"), "someone@else.com")
            self.fill_and_save(page)
        self.drive(script)
        self.assertEqual(self.saved()[0]["email_to"], "someone@else.com")

    def test_the_address_appears_as_soon_as_email_is_set_up(self):
        # The box is empty and disabled until then, so this is the first moment
        # there's anything to put in it.
        def script(page):
            self.assertEqual(page.input_value("#save_email"), "")
            page.click("#goEmail")
            page.fill("#mail_address", "me@gmail.com")
            page.fill("#mail_password", "abcd efgh ijkl mnop")
            page.click("#saveMail")
            page.wait_for_selector("#mailMsg:not([hidden])")
            page.click("#backToSave")
            self.assertEqual(page.input_value("#save_email"), "me@gmail.com")
        self.drive(script, email=False)

    def test_editing_a_search_shows_the_address_it_reports_to(self):
        def script(page):
            page.fill("#save_email", "someone@else.com")
            self.fill_and_save(page)
            page.click("#tabSaved")
            page.click(".card button[data-act=edit]")
            page.wait_for_selector("#paneNew:not([hidden])")
            self.assertEqual(page.input_value("#save_email"), "someone@else.com")
        self.drive(script)

    def test_the_saved_tab_says_why_nothing_would_arrive(self):
        def script(page):
            page.click("#tabSaved")
            self.assertFalse(page.is_hidden("#savedNeedsEmail"))
            # The empty list sends you to the same place the banner does,
            # rather than to the New search tab where saving is still barred.
            self.assertIn("set up your email", page.text_content("#savedList"))
        self.drive(script, email=False)

    def test_a_window_that_opens_with_email_working_is_not_blocked(self):
        def script(page):
            self.assertTrue(page.is_hidden("#saveNeedsEmail"))
            self.assertFalse(page.is_disabled("#save_name"))
            page.click("#tabSaved")
            self.assertTrue(page.is_hidden("#savedNeedsEmail"))
            page.click("#tabEmail")
            self.assertIn("Email is set up", page.text_content("#mailState"))
            # And the save block is already holding the address it would use.
            self.assertEqual(page.input_value("#save_email"), "me@gmail.com")
        self.drive(script)

    # -------------------------------------------------------- past searches
    def run_hooks(self, runs=None, answer=None, on_delete=None):
        """Stands in for past_runs, which would otherwise need real run folders
        on disk and would open a real browser window on the machine running the
        tests."""
        self.opened_runs = []
        self.deleted_runs = []
        left = list(RUNS if runs is None else runs)

        def delete(run_id):
            self.deleted_runs.append(run_id)
            if on_delete:
                return on_delete
            left[:] = [r for r in left if r["id"] != run_id]
            return {"runs": list(left)}

        return {
            "list_runs": lambda: {"runs": list(left)},
            "open_run": lambda run_id: (self.opened_runs.append(run_id)
                                        or (answer or {})),
            "delete_run": delete,
        }

    def test_every_run_on_disk_gets_a_card(self):
        def script(page):
            page.click("#tabPast")
            page.wait_for_selector("#runList .card.run")
            self.assertEqual(page.locator("#runList .card.run").count(), 2)
            first = page.text_content("#runList .card.run:first-of-type")
            self.assertIn("defender 110", first)
            self.assertIn("121 listings", first)
            self.assertIn("2 cities", first)
            self.assertIn("took 2m 8s", first)
            self.assertIn("today at 11:14 pm", first)
            # Not where it lives. A card is how you get to a run; naming the
            # folder only suggests that one day you'll have to go and find it.
            self.assertNotIn("runs/", first)
        self.drive(script, extra_hooks=self.run_hooks())

    def test_a_scheduled_run_is_marked_as_one(self):
        def script(page):
            page.click("#tabPast")
            page.wait_for_selector("#runList .card.run")
            last = page.text_content("#runList .card.run:last-of-type")
            self.assertIn("scheduled", last)
            self.assertIn("3 new that run", last)
            self.assertIn("4 earlier runs kept", last)
            # Only the scheduled one is badged.
            self.assertEqual(page.locator("#runList .pill").count(), 1)
        self.drive(script, extra_hooks=self.run_hooks())

    def test_clicking_a_card_opens_that_run_and_then_says_nothing(self):
        def script(page):
            page.click("#tabPast")
            page.wait_for_selector("#runList .card.run")
            # Held open in the page so both halves are observable. The real one
            # takes long enough to be worth saying something during.
            page.evaluate("""() => {
              const real = window.pyOpenRun;
              window.pyOpenRun = async id => {
                await new Promise(r => setTimeout(r, 300));
                return real(id);
              };
            }""")
            page.click("#runList .card.run:first-of-type")
            page.wait_for_selector("#runList .card.run.busy")
            self.assertIn("Opening defender 110", page.text_content("#runMsg"))
            # And then nothing at all: what opened is a window in front of
            # them, which is the news, not a line left behind in here.
            page.wait_for_selector("#runMsg", state="hidden")
            self.assertEqual(page.text_content("#runMsg"), "")
            self.assertEqual(page.locator("#runList .card.run.busy").count(), 0)
        self.drive(script, extra_hooks=self.run_hooks())
        self.assertEqual(self.opened_runs, ["defender_110_08-09-2026"])

    def test_a_run_that_cannot_be_opened_says_why(self):
        def script(page):
            page.click("#tabPast")
            page.wait_for_selector("#runList .card.run")
            page.click("#runList .card.run:first-of-type")
            page.wait_for_selector("#runMsg:not([hidden])")
            self.assertIn("isn't in runs/", page.text_content("#runMsg"))
            self.assertIn("bad", page.get_attribute("#runMsg", "class"))
        self.drive(script, extra_hooks=self.run_hooks(
            answer={"error": "That folder isn't in runs/ any more."}))

    def test_deleting_a_run_asks_before_it_does_it(self):
        # The same second click the scheduled searches list asks for: this throws
        # away results that took a long search to gather.
        def script(page):
            page.click("#tabPast")
            page.wait_for_selector("#runList .card.run")
            btn = "#runList .card.run:first-of-type button[data-act=del]"
            page.click(btn)
            self.assertEqual(page.text_content(btn), "Really delete?")
            page.wait_for_timeout(150)
            self.assertEqual(page.locator("#runList .card.run").count(), 2)
        self.drive(script, extra_hooks=self.run_hooks())
        self.assertEqual(self.deleted_runs, [])
        # And the click that asked didn't also open the gallery underneath it.
        self.assertEqual(self.opened_runs, [])

    def test_the_second_click_deletes_the_run(self):
        def script(page):
            page.click("#tabPast")
            page.wait_for_selector("#runList .card.run")
            btn = "#runList .card.run:first-of-type button[data-act=del]"
            page.click(btn)
            page.click(btn)
            page.wait_for_function("() => document.querySelectorAll("
                                   "'#runList .card.run').length === 1")
            self.assertIn("nightly", page.text_content("#runList"))
            self.assertIn("Deleted “defender 110”", page.text_content("#runMsg"))
        self.drive(script, extra_hooks=self.run_hooks())
        self.assertEqual(self.deleted_runs, ["defender_110_08-09-2026"])
        self.assertEqual(self.opened_runs, [])

    def test_the_keyboard_reaches_the_button_without_opening_the_card(self):
        # Enter on a button inside a card that is itself one big button.
        def script(page):
            page.click("#tabPast")
            page.wait_for_selector("#runList .card.run")
            btn = "#runList .card.run:first-of-type button[data-act=del]"
            page.focus(btn)
            page.keyboard.press("Enter")
            self.assertEqual(page.text_content(btn), "Really delete?")
            page.wait_for_timeout(150)
        self.drive(script, extra_hooks=self.run_hooks())
        self.assertEqual(self.deleted_runs, [])
        self.assertEqual(self.opened_runs, [])

    def test_asking_about_one_run_and_then_opening_another_forgets_it(self):
        def script(page):
            page.click("#tabPast")
            page.wait_for_selector("#runList .card.run")
            page.click("#runList .card.run:first-of-type button[data-act=del]")
            page.click("#runList .card.run:last-of-type .nm")
            page.wait_for_timeout(250)
            self.assertEqual(
                page.text_content("#runList .card.run:first-of-type "
                                  "button[data-act=del]"), "Delete")
        self.drive(script, extra_hooks=self.run_hooks())
        self.assertEqual(self.opened_runs, ["saved/nightly"])
        self.assertEqual(self.deleted_runs, [])

    def test_a_run_that_cannot_be_deleted_says_why(self):
        def script(page):
            page.click("#tabPast")
            page.wait_for_selector("#runList .card.run")
            btn = "#runList .card.run:first-of-type button[data-act=del]"
            page.click(btn)
            page.click(btn)
            page.wait_for_selector("#runMsg:not([hidden])")
            self.assertIn("may have a file in it open",
                          page.text_content("#runMsg"))
            self.assertIn("bad", page.get_attribute("#runMsg", "class"))
            self.assertEqual(page.locator("#runList .card.run").count(), 2)
            # And the card isn't left greyed out as though it were still going.
            self.assertEqual(page.locator("#runList .card.run.busy").count(), 0)
        self.drive(script, extra_hooks=self.run_hooks(
            on_delete={"error": "Couldn't delete that folder. Something else "
                                "may have a file in it open."}))

    def test_a_runs_folder_with_nothing_in_it_says_so(self):
        def script(page):
            page.click("#tabPast")
            page.wait_for_selector("#runList .empty")
            self.assertIn("Nothing has finished yet",
                          page.text_content("#runList"))
        self.drive(script, extra_hooks=self.run_hooks(runs=[]))

    def test_the_runs_are_read_once_and_then_only_when_asked(self):
        # Reading every run's manifest isn't free, and the folder doesn't change
        # while the window is up unless a person changes it.
        calls = []
        hooks = self.run_hooks()
        listed = hooks["list_runs"]
        hooks["list_runs"] = lambda: calls.append(1) or listed()

        def script(page):
            page.click("#tabPast")
            page.wait_for_selector("#runList .card.run")
            page.click("#tabNew")
            page.click("#tabPast")
            page.wait_for_timeout(250)
            self.assertEqual(len(calls), 1)
            page.click("#refreshRuns")
            page.wait_for_timeout(250)
            self.assertEqual(len(calls), 2)
        self.drive(script, extra_hooks=hooks)

    # ------------------------------------------------ the shortcut offer
    def shortcut_hooks(self, offer=None, result=None):
        """Stands in for make_desktop_icon, which would otherwise put a real
        icon on the desktop of whoever ran the tests."""
        self.asked = []
        return {
            "shortcut_offer": lambda: offer if offer is not None else OFFER,
            "add_shortcut": lambda ids, never: (
                self.asked.append(("add", ids, never))
                or (result or {"added": ids, "ok": True,
                               "message": "Done. Your icon is on your desktop."})),
            "shortcut_never": lambda: self.asked.append(("never",)) or {"ok": True},
        }

    def test_no_offer_means_no_panel(self):
        def script(page):
            self.assertTrue(page.is_hidden("#shortcutAsk"))
        self.drive(script, extra_hooks=self.shortcut_hooks(offer={"ask": False}))

    def test_the_offer_is_the_only_time_it_comes_up(self):
        # It used to leave a button behind on the Email & Setup tab. Turning
        # the offer down is now the end of the subject for this launch, and
        # nothing in the window brings it back.
        def script(page):
            self.assertTrue(page.is_hidden("#shortcutAsk"))
            page.click("#tabEmail")
            self.assertEqual(page.locator("#shortcutOpen").count(), 0)
        self.drive(script, extra_hooks=self.shortcut_hooks(offer={"ask": False}))
        self.assertEqual(self.asked, [])

    def test_closing_the_sheet_hands_focus_to_the_form_behind_it(self):
        def script(page):
            page.wait_for_selector("#shortcutAsk:visible")
            page.click("#shortcutSkip")
            page.wait_for_selector("#shortcutAsk", state="hidden")
            self.assertEqual(page.evaluate("document.activeElement.id"), "query")
        self.drive(script, extra_hooks=self.shortcut_hooks())

    def test_the_panel_shows_the_places_python_offered(self):
        # Only the first is ticked: a Dock or Start menu entry is more intrusive
        # than a desktop icon, so it's opt-in.
        def script(page):
            page.wait_for_selector("#shortcutAsk:visible")
            self.assertEqual(
                page.eval_on_selector_all(
                    "#shortcutPlaces .tog",
                    "els => els.map(e => [e.dataset.place,"
                    " e.getAttribute('aria-pressed')])"),
                [["desktop", "true"], ["dock", "false"]])
            self.assertEqual(page.evaluate("document.activeElement.id"),
                             "shortcutAdd")
        self.drive(script, extra_hooks=self.shortcut_hooks())

    def test_adding_sends_every_ticked_place_and_then_only_offers_to_close(self):
        def script(page):
            page.wait_for_selector("#shortcutAsk:visible")
            page.click("#shortcutPlaces .tog[data-place=dock]")
            page.click("#shortcutAdd")
            page.wait_for_selector("#shortcutMsg:not([hidden])")
            self.assertIn("Done", page.text_content("#shortcutMsg"))
            for gone in ("#shortcutPlaces", "#shortcutNever", "#shortcutAdd"):
                self.assertTrue(page.is_hidden(gone), gone)
            self.assertEqual(page.text_content("#shortcutSkip").strip(), "Close")
            page.click("#shortcutSkip")
            page.wait_for_selector("#shortcutAsk", state="hidden")
        self.drive(script, extra_hooks=self.shortcut_hooks())
        self.assertEqual(self.asked, [("add", ["desktop", "dock"], False)])

    def test_a_refusal_leaves_the_panel_usable_and_says_why(self):
        hooks = self.shortcut_hooks(
            result={"error": "Windows wouldn't create the shortcut."})

        def script(page):
            page.wait_for_selector("#shortcutAsk:visible")
            page.click("#shortcutAdd")
            page.wait_for_selector("#shortcutMsg:not([hidden])")
            self.assertIn("wouldn't", page.text_content("#shortcutMsg"))
            self.assertFalse(page.is_hidden("#shortcutAdd"))
            self.assertFalse(page.is_disabled("#shortcutAdd"))
            self.assertEqual(page.text_content("#shortcutAdd").strip(),
                             "Add shortcut")
        self.drive(script, extra_hooks=hooks)

    def test_a_partial_success_is_not_dressed_up_as_a_whole_one(self):
        hooks = self.shortcut_hooks(
            result={"added": ["desktop"], "ok": False,
                    "message": "Done, on your desktop. The Dock said no."})

        def script(page):
            page.wait_for_selector("#shortcutAsk:visible")
            page.click("#shortcutAdd")
            page.wait_for_selector("#shortcutMsg:not([hidden])")
            self.assertNotIn("ok", page.get_attribute("#shortcutMsg", "class"))
        self.drive(script, extra_hooks=hooks)

    def test_not_now_only_records_it_when_the_box_is_ticked(self):
        def dismiss(page):
            page.wait_for_selector("#shortcutAsk:visible")
            page.click("#shortcutSkip")
            page.wait_for_selector("#shortcutAsk", state="hidden")
        self.drive(dismiss, extra_hooks=self.shortcut_hooks())
        self.assertEqual(self.asked, [])

        def dismiss_for_good(page):
            page.wait_for_selector("#shortcutAsk:visible")
            page.click("#shortcutNever")
            page.click("#shortcutSkip")
            page.wait_for_selector("#shortcutAsk", state="hidden")
        self.drive(dismiss_for_good, extra_hooks=self.shortcut_hooks())
        self.assertEqual(self.asked, [("never",)])

    def test_escape_closes_the_panel_without_abandoning_the_window(self):
        def script(page):
            page.wait_for_selector("#shortcutAsk:visible")
            page.keyboard.press("Escape")
            page.wait_for_selector("#shortcutAsk", state="hidden")
            page.fill("#query", "defender 110")
            page.click("#start")
        data = self.drive(script, extra_hooks=self.shortcut_hooks())
        self.assertEqual((data or {}).get("queries"), ["defender 110"])

    def test_clicking_beside_the_panel_closes_it(self):
        def script(page):
            page.wait_for_selector("#shortcutAsk:visible")
            box = page.query_selector("#shortcutAsk").bounding_box()
            page.mouse.click(box["x"] + 6, box["y"] + 6)
            page.wait_for_selector("#shortcutAsk", state="hidden")
        self.drive(script, extra_hooks=self.shortcut_hooks())

    def test_the_form_still_works_with_no_hooks_at_all(self):
        # settings_ui must not depend on scheduling.py being importable.
        def script(page):
            page.click("#tabSaved")
            page.click("#refreshSaved")
            page.wait_for_selector("#savedMsg:not([hidden])")
            self.assertIn("isn't available", page.text_content("#savedMsg"))
            page.click("#tabPast")
            page.wait_for_selector("#runMsg:not([hidden])")
            self.assertIn("isn't available", page.text_content("#runMsg"))
            page.click("#tabNew")
            page.fill("#query", "x")
            # Nothing here can say whether email works, so the block that needs
            # it stays shut rather than offering a save that would go nowhere.
            self.assertTrue(page.is_disabled("#saveSearch"))
            self.assertFalse(page.is_hidden("#saveNeedsEmail"))

        self.errors = []
        settings_ui.collect_settings(
            CITIES, PACES, {"query": "", "exclude": "", "pace": "fast"},
            headless=True, hooks=None,
            on_ready=lambda page: (
                page.on("pageerror", lambda e: self.errors.append(str(e))),
                script(page),
                page.evaluate("window.pyCancel()")))
        self.assertEqual(self.errors, [])


@unittest.skipUnless(HAVE_PLAYWRIGHT, "needs Playwright")
class UpdateBanner(unittest.TestCase):
    """The offer of a newer version, which most launches never show at all.

    Its own class rather than another UITest: the banner needs none of the
    saved-search plumbing, and driving it with only its three hooks also proves
    it doesn't quietly depend on the rest of the window being wired up.
    """

    OFFER = {"show": True, "version": "1.3.0", "current": "1.0.0"}

    def drive(self, script, offer=None, answer=None):
        self.waved_off = []
        self.errors = []
        hooks = {
            "update_offer": lambda: offer or {"show": False},
            "update_skip": lambda v: self.waved_off.append(v) or {"ok": True},
            "update_now": lambda: answer or {
                "ok": True, "version": "1.3.0", "notes": [],
                "message": "Updated to 1.3.0."},
        }

        def ready(page):
            page.on("pageerror", lambda e: self.errors.append(str(e)))
            try:
                script(page)
            finally:
                if not page.is_closed():
                    page.evaluate("window.pyCancel()")

        data = settings_ui.collect_settings(
            CITIES, PACES, {"query": "", "exclude": "", "pace": "fast"},
            headless=True, hooks=hooks, on_ready=ready)
        self.assertEqual(self.errors, [], f"JavaScript errors: {self.errors}")
        return data

    def test_a_copy_that_is_up_to_date_says_nothing(self):
        self.drive(lambda page:
                   self.assertTrue(page.query_selector("#updateBar").is_hidden()))

    def test_a_copy_that_is_behind_is_told_which_version_is_out(self):
        def script(page):
            page.wait_for_selector("#updateBar:visible")
            self.assertIn("1.3.0", page.text_content("#updLead"))
            self.assertIn("Update to the latest version?",
                          page.text_content("#updText"))
        self.drive(script, offer=self.OFFER)

    def test_being_far_behind_reads_the_same_as_being_one_behind(self):
        seen = []

        def script(page):
            page.wait_for_selector("#updateBar:visible")
            seen.append(page.text_content("#updText").strip())
        self.drive(script, offer={"show": True, "version": "1.0.1",
                                  "current": "1.0.0"})
        self.drive(script, offer={"show": True, "version": "9.4.0",
                                  "current": "1.0.0"})
        self.assertEqual(seen[0], seen[1])

    def test_not_now_puts_it_away_and_says_which_one_was_waved_off(self):
        def script(page):
            page.wait_for_selector("#updateBar:visible")
            page.click("#updSkip")
            page.wait_for_selector("#updateBar", state="hidden")
        self.drive(script, offer=self.OFFER)
        self.assertEqual(self.waved_off, ["1.3.0"])

    def test_an_update_that_lands_blocks_the_rest_of_the_window(self):
        # The code on disk is now newer than the code already running, so there
        # must be no way back to the form from here — only out.
        def script(page):
            page.click("#updGo")
            page.wait_for_selector("#updateDone:visible")
            self.assertTrue(page.query_selector("#updateBar").is_hidden())
            page.click("#updateDoneClose")
        self.assertEqual(self.drive(script, offer=self.OFFER),
                         {"action": "updated"})

    def test_a_window_that_can_be_restarted_bows_out_on_its_own(self):
        # Nothing here needs a decision: the new version is on disk and the only
        # thing in the way of it is this window.
        landed = {"ok": True, "version": "1.3.0", "notes": [], "restart": True,
                  "message": "Updated to 1.3.0. Restarting to finish."}

        def script(page):
            page.click("#updGo")
            page.wait_for_selector("#updateDone:visible")
            self.assertEqual(page.text_content("#updateDoneClose"), "Restart now")
            # Submitting doesn't close the window from the page's side — Python
            # notices and takes it down — so the guard that stops a click and
            # the timer both answering is what there is to watch.
            page.wait_for_function("finishUpdate.already === true", timeout=10000)
        self.assertEqual(self.drive(script, offer=self.OFFER, answer=landed),
                         {"action": "updated"})

    def test_a_window_with_a_note_in_it_waits_to_be_dismissed(self):
        # Something didn't go to plan, and closing on a timer would take the
        # explanation with it before it could be read.
        landed = {"ok": True, "version": "1.3.0", "restart": True,
                  "notes": ["“Start Faceplace Marketbook (Windows).bat” is "
                            "still on the old version; it was in use."],
                  "message": "Updated to 1.3.0. Choose Restart now to finish."}

        def script(page):
            page.click("#updGo")
            page.wait_for_selector("#updateDone:visible")
            self.assertIn("still on the old version",
                          page.text_content("#updateDoneText"))
            # Comfortably past the moment a window with nothing to say would
            # have bowed out.
            page.wait_for_timeout(4000)
            self.assertFalse(page.evaluate("finishUpdate.already === true"))
            page.click("#updateDoneClose")
        self.assertEqual(self.drive(script, offer=self.OFFER, answer=landed),
                         {"action": "updated"})

    def test_an_update_that_fails_says_why_and_leaves_the_button_alive(self):
        answer = {"error": "Couldn't reach GitHub. Check your internet connection."}

        def script(page):
            page.click("#updGo")
            page.wait_for_selector("#updMsg:not([hidden])")
            self.assertIn("Couldn't reach GitHub", page.text_content("#updMsg"))
            self.assertTrue(page.query_selector("#updateDone").is_hidden())
            # Still offered, because trying again is the right thing to do next.
            self.assertFalse(page.query_selector("#updGo").is_disabled())
        self.drive(script, offer=self.OFFER, answer=answer)


if __name__ == "__main__":
    unittest.main()
